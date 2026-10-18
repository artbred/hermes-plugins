"""Opt-in Docker integration: disposable private network, no published ports.

HERMES_INGRESS_INTEGRATION=1 python3 -m unittest discover -s tests -v
Needs root, Docker, nsenter, unshare/mount, and systemd-socket-activate/proxyd.
No live network attachment, host configuration writes, or systemctl calls.
"""
import contextlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import cast
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "nginx@sha256:1d13701a5f9f3fb01aaa88cef2344d65b6b5bf6b7d9fa4cf0dca557a8d7702ba"
PORTS = {"api": 8642, "speech": 9120, "uploads": 9122, "push": 9123}


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


def client():
    """Runs only inside the test container's network namespace (host filesystem)."""
    request = json.load(sys.stdin)
    conn = http.client.HTTPConnection("localhost", request["port"], timeout=7)
    headers = {"Host": "Original.Example:443", "X-Forwarded-Proto": "https",
               "X-Forwarded-For": "fixture-client", "X-Test-Identity": "preserved"}
    if request.get("auth", True):
        headers["Authorization"] = "Bearer fixture-only-not-a-credential"
    started = time.monotonic()
    if request.get("upload"):
        total = 2 * 1024 * 1024 + 17
        conn.putrequest("POST", "/upload?keep=%2F", skip_host=True)
        headers["Content-Length"] = str(total)
        if request.get("chunked"):
            del headers["Content-Length"]
            headers["Transfer-Encoding"] = "chunked"
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        first = b"a" * 8192
        conn.send((b"2000\r\n" + first + b"\r\n") if request.get("chunked") else first)
        deadline = time.monotonic() + 4
        while not Path(request["event"]).exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Upstream did not receive initial upload before completion")
            time.sleep(0.02)
        rest = b"b" * (total - len(first))
        conn.send((f"{len(rest):x}\r\n".encode() + rest + b"\r\n0\r\n\r\n")
                  if request.get("chunked") else rest)
    else:
        conn.request("GET", request.get("path", "/echo/a%2Fb?literal=%2F"), headers=headers)
    response = conn.getresponse()
    if request.get("sse"):
        first = response.readline().decode()
        first_elapsed = time.monotonic() - started
        body = first + response.read().decode()
    else:
        first_elapsed = None
        body = response.read().decode()
    print(json.dumps({"status": response.status, "headers": dict(response.getheaders()),
                      "body": body, "first_elapsed": first_elapsed,
                      "elapsed": time.monotonic() - started}))
    conn.close()


class FixtureServer(ThreadingHTTPServer):
    tag: str
    event: str


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def fixture_server(self):
        return cast(FixtureServer, self.server)

    def log_message(self, format, *args):
        pass

    def reply(self, status, body, **headers):
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if "Authorization" not in self.headers:
            return self.reply(401, "upstream-auth-required", WWW_Authenticate='Bearer realm="fixture"')
        if self.path == "/sse":
            first, second = b"data: first\n\n", b"data: second\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(first) + len(second)))
            self.send_header("X-Accel-Buffering", "yes")  # Adapter must ignore override.
            self.end_headers()
            self.wfile.write(first)
            self.wfile.flush()
            time.sleep(1.5)
            self.wfile.write(second)
            return
        return self.reply(200, json.dumps({"backend": self.fixture_server.tag, "path": self.path,
                                          "headers": dict(self.headers)}),
                          Location="https://Original.Example:443/unchanged")

    def do_POST(self):
        size = 0
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            while True:
                length = int(self.rfile.readline().strip().split(b";", 1)[0], 16)
                if length == 0:
                    self.rfile.readline()
                    break
                size += len(self.rfile.read(length))
                self.rfile.read(2)
                Path(self.fixture_server.event).touch()
        else:
            size = len(self.rfile.read(8192))
            Path(self.fixture_server.event).touch()
            size += len(self.rfile.read(int(self.headers["Content-Length"]) - size))
        self.reply(200, json.dumps({"bytes": size, "path": self.path,
                                   "auth": self.headers.get("Authorization")}))


@unittest.skipUnless(os.getenv("HERMES_INGRESS_INTEGRATION") == "1", "opt-in disposable Docker fixtures")
class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cleanup = contextlib.ExitStack()
        cls.addClassCleanup(cls.cleanup.close)
        cls.base = Path(cls.cleanup.enter_context(tempfile.TemporaryDirectory(
            prefix="host-ingress-test-", dir=os.environ.get("TMPDIR", "/root/.hermes/cache/scratch"))))
        cls.socketdir = cls.base / "sockets"
        cls.socketdir.mkdir(mode=0o750)
        os.chown(cls.socketdir, 0, 65534)
        os.chmod(cls.base, 0o700)
        cls.hosts = cls.base / "hosts"
        cls.hosts.write_text("127.0.0.1 localhost localhost4\n::1 localhost ip6-localhost\n")
        cls.suffix = uuid.uuid4().hex[:12]
        cls.network = "host-ingress-test-" + cls.suffix
        cls.container = "host-ingress-test-" + cls.suffix
        run("docker", "network", "create", "--internal", cls.network)
        cls.cleanup.callback(lambda: subprocess.run(["docker", "network", "rm", cls.network], capture_output=True))
        cls.servers = {}
        cls.proxies = {}
        cls.command = ["docker", "run", "--rm", "--pull", "never", "--network", cls.network,
                       "--network-alias", "hermes-host-ingress", "--user", "65534:65534", "--read-only",
                       "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                       "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m,uid=65534,gid=65534,mode=0700",
                       "--mount", f"type=bind,source={ROOT / 'nginx.conf'},target=/etc/nginx/hermes-nginx.conf,readonly",
                       "--mount", f"type=bind,source={cls.socketdir},target=/sockets,readonly",
                       "--entrypoint", "nginx", IMAGE]
        config_test = run(*cls.command, "-t", "-c", "/etc/nginx/hermes-nginx.conf")
        assert "test is successful" in config_test.stderr, config_test.stderr
        # Start with no socket files: mounted directory exists; nginx must still start.
        command = cls.command[:2] + ["--detach", "--name", cls.container] + cls.command[2:]
        run(*command, "-c", "/etc/nginx/hermes-nginx.conf", "-g", "daemon off;")
        cls.cleanup.callback(lambda: subprocess.run(["docker", "rm", "--force", cls.container], capture_output=True))
        info = json.loads(run("docker", "inspect", cls.container).stdout)[0]
        cls.pid = str(info["State"]["Pid"])
        assert not info["HostConfig"]["PortBindings"], info["HostConfig"]["PortBindings"]
        assert info["HostConfig"]["ReadonlyRootfs"]
        for attempt in range(40):
            try:
                result = cls.request("api")
                if result["status"] == 502:
                    break
            except subprocess.CalledProcessError:
                time.sleep(0.05)
        else:
            raise RuntimeError("nginx failed to start with missing socket files")
        cls.initial_missing = result
        cls.cleanup.callback(cls.stop_proxies)
        for name in PORTS:
            server = FixtureServer(("127.0.0.1", 0), Handler)
            server.daemon_threads = True
            server.tag = name
            server.event = str(cls.base / (name + ".upload-event"))
            cls.servers[name] = server
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cls.cleanup.callback(server.server_close)
            cls.cleanup.callback(server.shutdown)
            cls.start_proxy(name)

    @classmethod
    def start_proxy(cls, name):
        path = cls.socketdir / (name + ".sock")
        path.unlink(missing_ok=True)
        # Private mount namespace makes localhost4 mapping disposable and deterministic.
        # Host /etc/hosts and cloud-init are never edited or replaced globally.
        script = ('mount --bind "$1" /etc/hosts && '
                  'exec /usr/bin/systemd-socket-activate --listen="$2" '
                  '/usr/lib/systemd/systemd-socket-proxyd "$3"')
        log = cls.cleanup.enter_context(open(cls.base / (name + ".proxy.log"), "a"))
        proc = subprocess.Popen(["unshare", "--mount", "--propagation", "private", "/bin/sh", "-c", script,
                                 "fixture", str(cls.hosts), str(path),
                                 "localhost4:" + str(cls.servers[name].server_port)],
                                stdout=log, stderr=log, start_new_session=True)
        cls.proxies[name] = proc
        for attempt in range(80):
            if proc.poll() is not None:
                raise RuntimeError((cls.base / (name + ".proxy.log")).read_text())
            if path.exists():
                os.chown(path, 0, 65534)
                os.chmod(path, 0o660)
                return
            time.sleep(0.025)
        raise RuntimeError("fixture socket did not appear: " + name)

    @classmethod
    def stop_proxy(cls, name):
        proc = cls.proxies.pop(name, None)
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)

    @classmethod
    def stop_proxies(cls):
        for name in list(cls.proxies):
            cls.stop_proxy(name)

    @classmethod
    def request(cls, name, **kwargs):
        request = {"port": PORTS[name], **kwargs}
        result = run("nsenter", "--target", cls.pid, "--net", sys.executable,
                     str(Path(__file__).resolve()), "--client", input=json.dumps(request), timeout=12)
        return json.loads(result.stdout)

    def test_01_missing_sockets_then_all_four_routes(self):
        self.assertEqual(self.initial_missing["status"], 502)
        for name in PORTS:
            with self.subTest(name=name):
                response = self.request(name)
                self.assertEqual(response["status"], 200)
                body = json.loads(response["body"])
                self.assertEqual(body["backend"], name)
                self.assertEqual(body["path"], "/echo/a%2Fb?literal=%2F")
                self.assertEqual(body["headers"]["Host"], "Original.Example:443")
                self.assertEqual(body["headers"]["Authorization"], "Bearer fixture-only-not-a-credential")
                self.assertEqual(body["headers"]["X-Forwarded-Proto"], "https")
                self.assertEqual(body["headers"]["X-Forwarded-For"], "fixture-client")
                self.assertEqual(body["headers"]["X-Test-Identity"], "preserved")
                self.assertEqual(response["headers"]["Location"], "https://Original.Example:443/unchanged")

    def test_02_upstream_auth_not_bypassed(self):
        for name in PORTS:
            response = self.request(name, auth=False)
            self.assertEqual(response["status"], 401)
            self.assertEqual(response["headers"]["WWW-Authenticate"], 'Bearer realm="fixture"')

    def test_03_sse_unbuffered(self):
        for name in PORTS:
            response = self.request(name, path="/sse", sse=True)
            self.assertEqual(response["status"], 200)
            self.assertEqual(response["body"], "data: first\n\ndata: second\n\n")
            self.assertLess(response["first_elapsed"], 1.0)
            self.assertGreater(response["elapsed"], 1.4)

    def test_04_streaming_uploads_above_one_megabyte(self):
        for chunked in (False, True):
            for name in PORTS:
                with self.subTest(name=name, chunked=chunked):
                    event = Path(self.servers[name].event)
                    event.unlink(missing_ok=True)
                    response = self.request(name, upload=True, chunked=chunked, event=str(event))
                    self.assertEqual(response["status"], 200)
                    body = json.loads(response["body"])
                    self.assertEqual(body["bytes"], 2 * 1024 * 1024 + 17)
                    self.assertEqual(body["path"], "/upload?keep=%2F")
                    self.assertEqual(body["auth"], "Bearer fixture-only-not-a-credential")

    def test_05_permissions_and_socket_recreation(self):
        for name in PORTS:
            path = self.socketdir / (name + ".sock")
            mode = path.stat()
            self.assertEqual((mode.st_uid, mode.st_gid, mode.st_mode & 0o777), (0, 65534, 0o660))
            os.chmod(path, 0)
            try:
                self.assertEqual(self.request(name)["status"], 502)
            finally:
                os.chmod(path, 0o660)
            self.assertEqual(self.request(name)["status"], 200)
            self.stop_proxy(name)
            path.unlink()
            self.assertEqual(self.request(name)["status"], 502)
            self.start_proxy(name)
            self.assertEqual(self.request(name)["status"], 200)

    def test_06_named_docker_dns_without_published_ports(self):
        response = run("docker", "run", "--rm", "--pull", "never", "--network", self.network,
                       "--read-only", "--user", "65534:65534", "--cap-drop", "ALL",
                       "--security-opt", "no-new-privileges:true", "--entrypoint", "wget", IMAGE,
                       "-qO-", "--header=Authorization: Bearer fixture-only-not-a-credential",
                       "http://hermes-host-ingress:8642/dns")
        self.assertEqual(json.loads(response.stdout)["backend"], "api")

    def test_07_container_hardening_is_effective(self):
        info = json.loads(run("docker", "inspect", self.container).stdout)[0]
        self.assertFalse(info["HostConfig"]["PortBindings"])
        self.assertEqual(set(info["NetworkSettings"]["Networks"]), {self.network})
        status = run("docker", "exec", self.container, "cat", "/proc/1/status").stdout
        for expected in ("Uid:\t65534\t65534\t65534\t65534", "Gid:\t65534\t65534\t65534\t65534",
                         "CapEff:\t0000000000000000", "NoNewPrivs:\t1"):
            self.assertIn(expected, status)
        denied = subprocess.run(["docker", "exec", self.container, "touch", "/etc/fixture-write-test"], capture_output=True)
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual((self.base.stat().st_mode & 0o777), 0o700)
        self.assertEqual((self.socketdir.stat().st_mode & 0o777), 0o750)


if __name__ == "__main__":
    if sys.argv[1:] == ["--client"]:
        client()
    else:
        unittest.main()
