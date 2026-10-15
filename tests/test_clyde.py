import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from fixture_provider import fetch_fixture
from provider_adapters import estimate_cost
from clyde.broker import Broker
from clyde.controller import fetch, Outcome, run
from clyde.protocol import MAX_MESSAGE, MAX_DOCUMENT

FIXTURE = Path(__file__).resolve().parents[1] / 'clyde_fixtures/introduction.txt'

class Integration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.audit = Path(self.temp.name) / 'audit'
        self.server = Broker(('127.0.0.1', 0), FIXTURE, self.audit, .15)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(1)
        self.server.server_close()
        self.temp.cleanup()

    def test_approval_denial_and_audit(self):
        self.assertEqual(fetch('127.0.0.1', self.port, 'sandbox-introduction', 'one')['document'], FIXTURE.read_text())
        self.assertEqual(fetch('127.0.0.1', self.port, 'forbidden', 'two')['status'], 'denied')
        records = [json.loads(line) for line in self.audit.read_text().splitlines()]
        self.assertEqual([r['decision'] for r in records], ['approved', 'denied'])
        self.assertEqual([r['request_id'] for r in records], ['one', 'two'])
        self.assertTrue(all(r['timestamp'] and r['operation'] == 'fixture-fetch' for r in records))
        self.assertEqual(self.audit.stat().st_mode & 0o777, 0o600)

    def rejected(self, payload):
        with socket.create_connection(('127.0.0.1', self.port), timeout=1) as sock:
            sock.sendall(payload)
            try:
                self.assertEqual(sock.recv(1), b'')
            except ConnectionResetError:
                pass

    def test_native_broker_policy_is_authoritative(self):
        policy_path = Path(self.server.backend.policy_path)
        policy = json.loads(policy_path.read_text())
        policy['admission_rules'] = {'default': 'deny', 'rules': []}
        policy_path.write_text(json.dumps(policy))
        with patch('ai_broker.call_provider', side_effect=AssertionError('provider must not run')):
            response = fetch('127.0.0.1', self.port, 'sandbox-introduction', 'denied')
        self.assertEqual(response['status'], 'denied')
        native = self.server.state_dir / 'ai-broker-audit.jsonl'
        self.assertIn('task_denied', native.read_text())

    def test_strict_no_fallback_or_external_provider(self):
        with patch('mcp_proxy_bridge.forward', side_effect=AssertionError('no fallback')), \
             patch('provider_adapters._call_shell', side_effect=AssertionError('no shell')), \
             patch('provider_adapters._call_local', side_effect=AssertionError('no LLM')):
            self.assertEqual(run('127.0.0.1', self.port), 0)
        native = (self.server.state_dir / 'ai-broker-audit.jsonl').read_text()
        self.assertIn('task_approved', native)
        self.assertIn('task_completed', native)
        self.assertIn('task_denied', native)
        self.assertNotIn('fallback_forwarded', native)
        self.assertEqual(estimate_cost('fixture', {}), 0)
        from mcp_proxy_bridge import is_eligible
        eligible, _ = is_eligible({'type': 'fixture'}, {'allow_task_types': ['fixture']})
        self.assertFalse(eligible)

    def test_invalid_requests(self):
        base = dict(version=1, request_id='test', operation='fixture-fetch', document_id='sandbox-introduction')
        variants = [b'[]\n', b'{bad}\n', b'{"version":1,"version":1}\n', b'\xff\n']
        for field, value in [('operation','shell'), ('version',True), ('document_id','/etc/passwd'),
                             ('document_id','https://example.com'), ('request_id',[]), ('extra','bad')]:
            request = dict(base)
            request[field] = value
            variants.append(json.dumps(request).encode() + b'\n')
        for payload in variants:
            with self.subTest(payload=payload):
                self.rejected(payload)
        records = [json.loads(line) for line in self.audit.read_text().splitlines()]
        self.assertEqual(len(records), len(variants))
        self.assertTrue(all(r['decision'] == 'invalid-or-timeout' for r in records))

    def test_oversized_message(self):
        self.rejected(b'x' * (MAX_MESSAGE + 1) + b'\n')

    def test_slow_request_is_closed(self):
        start = time.monotonic()
        self.rejected(b'{')
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(fetch('127.0.0.1', self.port, 'forbidden', 'after')['status'], 'denied')

    def test_fixture_limit_and_preserve_audit(self):
        fixture = Path(self.temp.name) / 'large'
        fixture.write_bytes(b'x' * (MAX_DOCUMENT + 1))
        with self.assertRaises(ValueError):
            Broker(('127.0.0.1',0), fixture, Path(self.temp.name)/'other')
        before = self.audit.read_bytes()
        with self.assertRaises(FileExistsError):
            Broker(('127.0.0.1',0), FIXTURE, self.audit)
        self.assertEqual(self.audit.read_bytes(), before)

class ClientFailures(unittest.TestCase):
    def fake(self, payload=None, delay=0):
        listener = socket.socket()
        listener.bind(('127.0.0.1',0))
        listener.listen(1)
        port = listener.getsockname()[1]
        def serve():
            with listener:
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(1)
                    data = b''
                    while not data.endswith(b'\n'):
                        data += conn.recv(1)
                    time.sleep(delay)
                    if payload is not None:
                        try:
                            conn.sendall(payload)
                        except OSError:
                            pass
        thread = threading.Thread(target=serve)
        thread.start()
        return port, thread

    def test_response_validation(self):
        base = dict(version=1, request_id='expected', status='approved', document='hello')
        cases = [b'garbage\n', b'{}\n', b'x'*(MAX_MESSAGE+1)+b'\n']
        for field, value in [('request_id','wrong'), ('version',True), ('status','other'),
                             ('document',42), ('document','\ud800'), ('document','x'*(MAX_DOCUMENT+1)), ('extra',1)]:
            response = dict(base)
            response[field] = value
            cases.append(json.dumps(response).encode()+b'\n')
        for payload in cases:
            port, thread = self.fake(payload)
            try:
                with self.assertRaises(Outcome) as caught:
                    fetch('127.0.0.1', port, 'sandbox-introduction', 'expected', .5)
                self.assertEqual(caught.exception.name, 'malformed_response')
            finally:
                thread.join(1)

    def test_timeout(self):
        port, thread = self.fake(delay=.2)
        start = time.monotonic()
        with self.assertRaises(Outcome) as caught:
            fetch('127.0.0.1', port, 'sandbox-introduction', 'expected', .05)
        self.assertEqual(caught.exception.name, 'timeout')
        self.assertLess(time.monotonic()-start, .5)
        thread.join(1)

    def test_unavailable(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0))
            port = listener.getsockname()[1]  # Bound but not listening.
            with self.assertRaises(Outcome) as caught:
                fetch('127.0.0.1', port, 'sandbox-introduction', 'expected', .1)
            self.assertEqual(caught.exception.name, 'unavailable')


class ProviderValidation(unittest.TestCase):
    def test_independent_validation_and_limits(self):
        task = dict(type='fixture', intent='fetch', payload={'document_id': 'sandbox-introduction'})
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / 'fixture'
            fixture.write_bytes(b'x' * MAX_DOCUMENT)
            policy = {'fixture_documents': {'sandbox-introduction': str(fixture)}}
            self.assertTrue(fetch_fixture(task, policy)['ok'])
            fixture.write_bytes(b'x' * (MAX_DOCUMENT + 1))
            self.assertFalse(fetch_fixture(task, policy)['ok'])
            fixture.write_bytes(b'\xff')
            self.assertFalse(fetch_fixture(task, policy)['ok'])
            for payload in [{'document_id': '/etc/passwd'}, {'document_id': 'unknown'},
                            {'document_id': 'sandbox-introduction', 'path': '/etc/passwd'}]:
                self.assertFalse(fetch_fixture(dict(task, payload=payload), policy)['ok'])
            fixture.unlink()
            fixture.symlink_to(FIXTURE)
            self.assertFalse(fetch_fixture(task, policy)['ok'])

class CLIOutcomes(unittest.TestCase):
    fake = ClientFailures.fake
    # Inherit peer helpers; verify real process exit codes and bounded termination.
    def test_cli_failure_codes(self):
        cases = [(b'{}\n', 0, 3),
                 (json.dumps(dict(version=1, request_id='clyde-1', status='denied',
                                  reason='not-approved')).encode()+b'\n', 0, 2),
                 (None, .2, 5)]
        for payload, delay, code in cases:
            port, thread = self.fake(payload, delay)
            result = subprocess.run([sys.executable, '-m', 'clyde.controller',
                                     '--port', str(port), '--timeout', '.05'],
                                    capture_output=True, text=True, timeout=2)
            thread.join(1)
            self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            result = subprocess.run([sys.executable, '-m', 'clyde.controller',
                                     '--port', str(listener.getsockname()[1])],
                                    capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 4)

if __name__ == '__main__':
    unittest.main()
