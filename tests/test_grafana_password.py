"""Real loopback HTTP fixture covers the observed newline and crash recovery."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/grafana-password.py'


class PasswordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if SCRIPT.exists():
            spec = importlib.util.spec_from_file_location('grafana_password', SCRIPT)
            cls.helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.helper)
        else:
            cls.helper = None

    def setUp(self):
        self.assertIsNotNone(self.helper, 'password migration helper is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'password'
        self.real_refresh = self.helper.refresh_grafana_env
        rollout = patch.object(self.helper, 'refresh_grafana_env', create=True)
        self.rollout = rollout.start()
        self.addCleanup(rollout.stop)
        sleep = patch.object(self.helper, 'wait_for_lockout', create=True)
        self.wait = sleep.start()
        self.addCleanup(sleep.stop)

    def server(self, password, status=200):
        state = {'password': password, 'changes': 0, 'gets': 0}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def authorized(self):
                expected = 'Basic ' + base64.b64encode(b'admin:' + state['password']).decode()
                return self.headers.get('Authorization') == expected
            def reply(self, code):
                self.send_response(code)
                if code == 302:
                    self.send_header('Location', '/api/user')
                self.end_headers(); self.wfile.write(b'{}')
            def do_GET(self):
                state['gets'] += 1
                self.reply(status if status != 200 else (200 if self.authorized() else 401))
            def do_PUT(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path != '/api/user/password' or not self.authorized() or body['oldPassword'].encode() != state['password']:
                    return self.reply(400)
                state['password'] = body['newPassword'].encode()
                state['changes'] += 1
                self.reply(200)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}', state

    def test_prepare_creates_hex_without_newline_and_preserves_existing(self):
        self.helper.prepare(self.path)
        raw = self.path.read_bytes()
        self.assertRegex(raw, rb'^[0-9a-f]{48}$')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.path.write_bytes(b'keep spaces \r\n')
        self.helper.prepare(self.path)
        self.assertEqual(self.path.read_bytes(), b'keep spaces \r\n')

    def test_migrates_newline_preserving_spaces_and_file_metadata(self):
        raw = b'  existing secret with spaces  \r\n'
        self.path.write_bytes(raw); self.path.chmod(0o640)
        before = self.path.stat()
        url, state = self.server(raw)
        with patch.object(self.helper, 'reconcile_secret') as secret:
            self.helper.migrate(self.path, url)
            self.assertEqual(secret.call_args.args, (raw.rstrip(b'\r\n'),))
        self.assertEqual(state['changes'], 1)
        self.assertEqual(self.path.read_bytes(), raw.rstrip(b'\r\n'))
        self.assertEqual((self.path.stat().st_uid, self.path.stat().st_gid, stat.S_IMODE(self.path.stat().st_mode)), (before.st_uid, before.st_gid, 0o640))

    def test_recovers_after_database_changed_before_file(self):
        self.path.write_bytes(b'password123\n')
        url, state = self.server(b'password123')
        with patch.object(self.helper, 'reconcile_secret') as secret:
            self.helper.migrate(self.path, url)
            secret.assert_called_once_with(b'password123')
        self.assertEqual(state['changes'], 0)
        self.assertEqual(self.path.read_bytes(), b'password123')

    def test_reconciles_secret_after_file_changed(self):
        self.path.write_bytes(b'password123')
        url, state = self.server(b'password123')
        with patch.object(self.helper, 'reconcile_secret') as secret:
            self.helper.migrate(self.path, url)
            secret.assert_called_once_with(b'password123')
        self.assertEqual(state['changes'], 0)

    def test_unrelated_manual_password_is_not_reset(self):
        self.path.write_bytes(b'password123\n')
        url, state = self.server(b'manually-changed')
        with patch.object(self.helper, 'reconcile_secret') as secret:
            with self.assertRaisesRegex(ValueError, 'credentials'):
                self.helper.migrate(self.path, url)
            secret.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b'password123\n')
        self.assertEqual(state['changes'], 0)

    def test_http_failure_does_not_trigger_password_change(self):
        self.path.write_bytes(b'password123\n')
        url, state = self.server(b'password123\n', status=503)
        with patch.object(self.helper, 'reconcile_secret') as secret:
            with self.assertRaisesRegex(ValueError, '503'):
                self.helper.migrate(self.path, url)
            secret.assert_not_called()
        self.assertEqual(state['changes'], 0)
        self.assertEqual(state['gets'], 1)

    def test_secret_failure_can_be_retried_without_changing_password_twice(self):
        self.path.write_bytes(b'password123\n')
        url, state = self.server(b'password123\n')
        with patch.object(self.helper, 'reconcile_secret', side_effect=ValueError('unavailable')):
            with self.assertRaises(ValueError):
                self.helper.migrate(self.path, url)
        self.assertEqual(self.path.read_bytes(), b'password123')
        self.assertEqual(state['password'], b'password123')
        with patch.object(self.helper, 'reconcile_secret') as secret:
            self.helper.migrate(self.path, url)
            secret.assert_called_once_with(b'password123')
        self.assertEqual(state['changes'], 1)

    def test_authenticated_requests_do_not_follow_redirects(self):
        self.path.write_bytes(b'password123\n')
        url, state = self.server(b'password123\n', status=302)
        with patch.object(self.helper, 'reconcile_secret') as secret:
            with self.assertRaisesRegex(ValueError, '302'):
                self.helper.migrate(self.path, url)
            secret.assert_not_called()
        self.assertEqual(state['gets'], 1)
        self.assertEqual(self.path.read_bytes(), b'password123\n')

    def test_clean_file_refreshes_stale_sidecar_before_authentication(self):
        self.path.write_bytes(b'password123')
        url, state = self.server(b'password123')
        def refresh(password):
            self.assertEqual(password, b'password123')
            self.assertEqual(state['gets'], 0)
        self.rollout.side_effect = refresh
        with patch.object(self.helper, 'reconcile_secret'):
            self.helper.migrate(self.path, url)
        self.rollout.assert_called_once_with(b'password123')

    def test_newline_migration_refreshes_sidecar_only_after_database_and_file(self):
        self.path.write_bytes(b'password123\n')
        url, state = self.server(b'password123\n')
        def refresh(password):
            self.assertEqual(self.path.read_bytes(), password)
            self.assertEqual(state['password'], password)
            self.assertEqual(state['changes'], 1)
        self.rollout.side_effect = refresh
        with patch.object(self.helper, 'reconcile_secret'):
            self.helper.migrate(self.path, url)
        self.rollout.assert_called_once_with(b'password123')

    def test_recovers_from_persisted_lockout_once_sidecars_are_current(self):
        self.path.write_bytes(b'password123')
        url, state = self.server(b'locked')
        self.wait.side_effect = lambda: state.update(password=b'password123')
        with patch.object(self.helper, 'reconcile_secret'):
            self.helper.migrate(self.path, url)
        self.wait.assert_called_once_with()
        self.assertEqual(state['changes'], 0)
        self.assertEqual(state['gets'], 2)

    def test_interrupted_final_rollout_is_repaired_before_next_auth(self):
        self.path.write_bytes(b'password123\n')
        url, state = self.server(b'password123\n')
        self.rollout.side_effect = ValueError('rollout interrupted')
        with patch.object(self.helper, 'reconcile_secret'):
            with self.assertRaisesRegex(ValueError, 'interrupted'):
                self.helper.migrate(self.path, url)
        self.assertEqual(state['password'], b'password123')
        self.assertEqual(self.path.read_bytes(), b'password123')
        self.rollout.side_effect = None
        with patch.object(self.helper, 'reconcile_secret'):
            self.helper.migrate(self.path, url)
        self.assertEqual(state['changes'], 1)
        self.assertEqual(self.rollout.call_count, 2)

    def test_stable_annotation_and_wrong_password_fails_without_cooldown(self):
        self.path.write_bytes(b'password123')
        url, state = self.server(b'manually-changed')
        self.rollout.return_value = False
        with patch.object(self.helper, 'reconcile_secret'):
            with self.assertRaisesRegex(ValueError, 'credentials'):
                self.helper.migrate(self.path, url)
        self.wait.assert_not_called()
        self.assertEqual(state['changes'], 0)

    def test_rollout_annotation_is_stable_for_identical_credentials(self):
        import hashlib
        from subprocess import CompletedProcess
        fingerprint = hashlib.sha256(b'password123').hexdigest()
        for existing, ready, expected in [(None, True, True), (fingerprint, True, False), (fingerprint, False, True)]:
            annotations = {} if existing is None else {'mtc-hack/grafana-password-sha256': existing}
            snapshot = {'metadata': {'generation': 2}, 'spec': {'replicas': 1, 'template': {'metadata': {'annotations': annotations}}},
                        'status': {'observedGeneration': 2, 'updatedReplicas': 1, 'readyReplicas': int(ready), 'availableReplicas': int(ready)}}
            responses = [CompletedProcess([], 0, json.dumps(snapshot).encode()), CompletedProcess([], 0), CompletedProcess([], 0)]
            with patch.object(self.helper.subprocess, 'run', side_effect=responses) as run:
                self.assertEqual(self.real_refresh(b'password123'), expected)
                patch_body = json.loads(run.call_args_list[1].kwargs['input'])
                self.assertEqual(patch_body['spec']['template']['metadata']['annotations']['mtc-hack/grafana-password-sha256'], fingerprint)
                self.assertNotIn('password123', ' '.join(run.call_args_list[1].args[0]))
                self.assertIn('rollout', run.call_args_list[2].args[0])

    def test_rejects_remote_or_ambiguous_urls(self):
        for url in ['http://example.com', 'https://localhost', 'http://localhost.evil', 'http://admin@localhost', 'http://localhost/path', 'http://localhost?x=1']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.helper.validate_base_url(url)

    def test_secret_uses_stdin_never_command_arguments(self):
        from subprocess import CompletedProcess
        with patch.object(self.helper.subprocess, 'run', return_value=CompletedProcess([], 0)) as run:
            self.helper.reconcile_secret(b'password123')
        args, kwargs = run.call_args
        self.assertNotIn('password123', ' '.join(args[0]))
        document = json.loads(kwargs['input'])
        self.assertEqual(document['metadata'], {'namespace': 'observability', 'name': 'grafana-admin'})
        self.assertEqual(base64.b64decode(document['data']['admin-password']), b'password123')
        self.assertEqual(base64.b64decode(document['data']['admin-user']), b'admin')


if __name__ == '__main__':
    unittest.main()
