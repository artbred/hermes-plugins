
import importlib.util
from pathlib import Path
import tempfile
import os
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('host_cutover', ROOT / 'cutover_local.py')
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CutoverContract(unittest.TestCase):
    def test_named_targets_and_path_are_preserved(self):
        for port in module.PORTS:
            for field in ['url', 'address']:
                src = {field: f'http://172.23.0.1:{port}/v1/capabilities?probe=1'}
                out = module.rewrite_config(src)
                self.assertEqual(out[field], f'http://hermes-host-ingress:{port}/v1/capabilities?probe=1')
                self.assertEqual(src[field], f'http://172.23.0.1:{port}/v1/capabilities?probe=1')

    def test_secrets_policy_and_other_upstreams_unchanged(self):
        src = {'Authorization': 'Bearer test-fixture', 'X-Hermes-Session-Token': 'fixture',
               'sourceRange': ['172.23.0.0/16'], 'url': 'https://openrouter.ai/api/v1',
               'other': 'http://172.23.0.1:8642', 'address': 'not-a-url'}
        self.assertEqual(module.rewrite_config(src), src)

    def test_authority_and_idempotence(self):
        src = {'Host': '127.0.0.1:9119', 'servers': [{'url': 'http://172.23.0.1:9122'}]}
        out = module.rewrite_config(src)
        self.assertEqual(out['Host'], 'localhost:9119')
        self.assertEqual(module.rewrite_config(out), out)
        self.assertEqual(module.rewrite_config({'Host': 'example.com:9119'}), {'Host': 'example.com:9119'})

    def test_capability_identity_and_bind_must_both_match(self):
        valid = {'object': 'hermes.api_server.capabilities'}
        with mock.patch.object(module, 'request', return_value=(200, valid)), \
             mock.patch.object(module, 'loopback_only', return_value=True):
            self.assertTrue(module.ready('fixture'))
        with mock.patch.object(module, 'request', return_value=(200, valid)), \
             mock.patch.object(module, 'loopback_only', return_value=False):
            self.assertFalse(module.ready('fixture'))
        with mock.patch.object(module, 'request', return_value=(200, {'object': 'other'})):
            self.assertFalse(module.ready('fixture'))

    def test_check_pending_never_applies(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR', '/root/.hermes/cache/scratch')) as temp:
            with mock.patch.object(module, 'STATE', Path(temp)), \
                 mock.patch.object(module, 'secret', return_value='fixture'), \
                 mock.patch.object(module, 'ready', return_value=False), \
                 mock.patch.object(module, 'apply') as apply, \
                 mock.patch('sys.argv', ['cutover', '--check']):
                self.assertEqual(module.main(), 75)
                apply.assert_not_called()

    def test_prepare_push_is_guarded_and_preserves_credentials(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR', '/root/.hermes/cache/scratch')) as temp:
            base = Path(temp)
            stage = base / 'stage'
            (stage / 'backends').mkdir(parents=True)
            (stage / 'systemd').mkdir()
            (stage / 'cutover_local.py').write_text('fixture')
            (stage / 'push-server.py').write_text('new-source')
            original = 'original-source'
            (stage / 'push-stage.json').write_text(__import__('json').dumps({
                'expected_original_sha256': module.hashlib.sha256(original.encode()).hexdigest()}))
            (stage / 'backends/push-runtime.conf').write_text('PUSH_HOST=localhost4\n')
            (stage / 'systemd/hermes-push-loopback.conf').write_text('[Service]\nRestart=on-failure\n')
            active = base / 'opt/hermes-push/server.py'
            active.parent.mkdir(parents=True)
            active.write_text(original)
            env = base / 'etc/hermes-push.env'
            env.parent.mkdir(parents=True)
            env.write_text('API_SERVER_KEY=fixture-secret\nHERMES_NATIVE_URL=http://172.23.0.1:8642\n')
            (base / 'etc/hermes-host-ingress').mkdir()
            (base / 'etc/systemd/system').mkdir(parents=True)
            backup = base / 'backup'
            backup.mkdir()
            def mapped(value):
                if str(value) == module.__file__:
                    return stage / 'cutover_local.py'
                path = Path(value)
                return base / str(path).lstrip('/') if path.is_absolute() else path
            with mock.patch.object(module, 'Path', side_effect=mapped), \
                 mock.patch.object(module.subprocess, 'run') as run:
                module.prepare_push(backup)
                self.assertEqual(active.read_text(), 'new-source')
                self.assertEqual(env.read_text(), 'API_SERVER_KEY=fixture-secret\n')
                self.assertEqual((backup / 'hermes-push.env').stat().st_mode & 0o777, 0o600)
                self.assertTrue(run.called)
                module.prepare_push(backup)  # Idempotent when staged version is installed.
                active.write_text('unexpected-user-change')
                with self.assertRaisesRegex(RuntimeError, 'refusing overwrite'):
                    module.prepare_push(backup)
                self.assertEqual(active.read_text(), 'unexpected-user-change')

    def test_no_messaging_restart_command(self):
        text = (ROOT / 'cutover_local.py').read_text()
        self.assertNotIn('hermes-gateway.service', text)
        self.assertIn("'hermes-push.service'", text)
        self.assertIn('calling_services_touched', text)


if __name__ == '__main__':
    unittest.main()
