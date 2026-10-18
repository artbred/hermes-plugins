#!/usr/bin/env python3
"""One-time, readiness-gated local migration. Never controls the messaging process."""
import argparse

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

PORTS = {8642, 9120, 9122, 9123}
STATE = Path('/var/lib/hermes-host-ingress')
DYNAMIC = Path('/etc/dokploy/traefik/dynamic')
OLD_UNITS = ('hermes-speech-bridge.socket', 'hermes-speech-bridge.service',
             'hermes-attachments-bridge.socket', 'hermes-attachments-bridge.service',
             'hermes-mobile-speech-bridge.socket', 'hermes-mobile-speech-bridge.service')


def rewrite_config(value, key=None):
    if isinstance(value, dict):
        return {k: rewrite_config(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [rewrite_config(v, key) for v in value]
    if not isinstance(value, str):
        return value
    if key in ('url', 'address'):
        try:
            u = urllib.parse.urlsplit(value)
            if not u.hostname:
                return value
            ipaddress.ip_address(u.hostname)
            if u.scheme == 'http' and u.port in PORTS and not u.username:
                return urllib.parse.urlunsplit((u.scheme, f'hermes-host-ingress:{u.port}',
                                                u.path, u.query, u.fragment))
        except (ValueError, TypeError):
            pass
    if key == 'Host':
        try:
            u = urllib.parse.urlsplit('http://' + value)
            if not u.hostname:
                return value
            if ipaddress.ip_address(u.hostname).is_loopback and u.port in (9119, 9121):
                return f'localhost:{u.port}'
        except (ValueError, TypeError):
            pass
    return value


def secret(name):
    for line in Path('/root/.hermes/.env').read_text().splitlines():
        line = line.strip().removeprefix('export ').lstrip()
        k, sep, v = line.partition('=')
        if sep and k.strip() == name:
            values = shlex.split(v, comments=True)
            if values:
                return values[0]
    raise RuntimeError('Required named credential is absent')


def request(url, token=None, body=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        return code, None


def loopback_only(port):
    result = subprocess.run(['ss', '-H', '-ltn', 'sport', '=', ':' + str(port)],
                            capture_output=True, text=True, check=True)
    hosts = [line.split()[3].rsplit(':', 1)[0].strip('[]')
             for line in result.stdout.splitlines() if line.strip()]
    return bool(hosts) and all(ipaddress.ip_address(host).is_loopback for host in hosts)


def ready(token):
    try:
        code, data = request('http://localhost:8642/v1/capabilities', token)
        return (code == 200 and isinstance(data, dict)
                and data.get('object') == 'hermes.api_server.capabilities'
                and loopback_only(8642))
    except (OSError, ValueError):
        return False


def atomic_write(path, text, mode):
    stage = path.with_name(path.name + '.cutover-new')
    stage.write_text(text)
    stage.chmod(mode)
    stage.replace(path)


def prepare_push(backup):
    stage = Path(__file__).resolve().parent
    active = Path('/opt/hermes-push/server.py')
    new_source = stage / 'push-server.py'
    meta = json.loads((stage / 'push-stage.json').read_text())
    current_hash = hashlib.sha256(active.read_bytes()).hexdigest()
    new_hash = hashlib.sha256(new_source.read_bytes()).hexdigest()
    if current_hash not in (meta['expected_original_sha256'], new_hash):
        raise RuntimeError('Push source changed since staging; refusing overwrite')
    if current_hash != new_hash:
        saved = backup / 'push-server.py'
        if not saved.exists():
            shutil.copy2(active, saved)
            saved.chmod(0o600)
        atomic_write(active, new_source.read_text(), active.stat().st_mode & 0o777)
    env = Path('/etc/hermes-push.env')
    lines = env.read_text().splitlines()
    kept = [line for line in lines if line.partition('=')[0].strip()
            not in ('PUSH_HOST', 'HERMES_NATIVE_URL')]
    if kept != lines:
        saved = backup / 'hermes-push.env'
        if not saved.exists():
            shutil.copy2(env, saved)
            saved.chmod(0o600)
        atomic_write(env, '\n'.join(kept) + '\n', env.stat().st_mode & 0o777)
    shutil.copy2(stage / 'backends/push-runtime.conf', '/etc/hermes-host-ingress/push-runtime.conf')
    folder = Path('/etc/systemd/system/hermes-push.service.d')
    folder.mkdir(exist_ok=True)
    shutil.copy2(stage / 'systemd/hermes-push-loopback.conf', folder / '20-loopback.conf')
    subprocess.run(['systemctl', 'daemon-reload'], check=True)


def apply(token):
    import yaml  # Existing Hermes interpreter dependency, not a new package.
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = STATE / 'cutover-backup'
    backup.mkdir(mode=0o700, exist_ok=True)
    prepare_push(backup)
    changed = []
    for path in sorted(DYNAMIC.glob('hermes*.yml')):
        before = yaml.safe_load(path.read_text())
        after = rewrite_config(before)
        if after == before:
            continue
        saved = backup / path.name
        if not saved.exists():
            shutil.copy2(path, saved)
            saved.chmod(0o600)
        atomic_write(path, yaml.safe_dump(after, sort_keys=False), path.stat().st_mode & 0o777)
        changed.append(path.name)
    if not loopback_only(9123):
        subprocess.run(['systemctl', 'restart', 'hermes-push.service'], check=True)
    for _ in range(40):
        if loopback_only(9123):
            break
        time.sleep(0.25)
    if not loopback_only(9123):
        raise RuntimeError('Push listener has not moved to loopback')
    expected = [('/v1/capabilities', None, 200),
                ('/api/audio/transcribe', b'{}', 422),
                ('/api/audio/speak', b'{}', 422),
                ('/api/files/upload-stream', b'{}', 422),
                ('/v1/runs', b'{}', 400)]
    statuses = {}
    for path, body, wanted in expected:
        code = 0
        for _ in range(20):
            try:
                code, _ = request('https://hermes.sashakuzina.com' + path, token, body)
            except OSError:
                code = 0
            if code == wanted:
                break
            time.sleep(1)
        if code != wanted:
            raise RuntimeError('Private ingress verification failed on ' + path)
        statuses[path] = code
    code, _ = request('https://hermes.sashakuzina.com/v1/capabilities')
    if code != 401:
        raise RuntimeError('Missing-key rejection failed')
    for name in OLD_UNITS:
        subprocess.run(['systemctl', 'disable', '--now', name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        old = Path('/etc/systemd/system') / name
        if old.exists():
            retired = backup / (name + '.retired')
            if not retired.exists():
                shutil.copy2(old, retired)
                retired.chmod(0o600)
            old.unlink()
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    rules = subprocess.check_output(['ufw', 'status', 'numbered'], text=True)
    indexes = []
    for line in rules.splitlines():
        match = re.match(r'^\[\s*(\d+)\]\s+(.*)', line)
        if match and re.search(r'(?<!\d)(?:8642|9120|9122|9123|9119)(?:/tcp)?(?:\s|$)', match[2]):
            indexes.append(int(match[1]))
    for index in sorted(indexes, reverse=True):
        subprocess.run(['ufw', '--force', 'delete', str(index)], check=True,
                       stdout=subprocess.DEVNULL)
    for path in DYNAMIC.glob('hermes*.yml'):
        obj = yaml.safe_load(path.read_text())
        if rewrite_config(obj) != obj:
            raise RuntimeError('An owned numeric service target remains')
    receipt = {'status': 'complete', 'verified_at_unix': time.time(),
               'changed_files': changed, 'http_statuses': statuses,
               'api_loopback_only': True, 'push_loopback_only': True,
               'missing_key_rejected': True, 'obsolete_firewall_rules_removed': len(indexes),
               'calling_services_touched': False}
    atomic_write(STATE / 'cutover.json', json.dumps(receipt, indent=2) + '\n', 0o600)
    subprocess.run(['systemctl', 'disable', '--now', 'hermes-host-ingress-cutover.path'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(json.dumps(receipt))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--wait', type=int, default=0)
    args = parser.parse_args()
    if (STATE / 'cutover.json').exists():
        print('{"status":"already-complete"}')
        return 0
    token = secret('API_SERVER_KEY')
    deadline = time.monotonic() + args.wait
    while not ready(token):
        if args.check or time.monotonic() >= deadline:
            print('{"status":"waiting-for-loopback-api","changed":false}')
            return 75
        time.sleep(2)
    if args.check:
        print('{"status":"ready","changed":false}')
        return 0
    apply(token)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
