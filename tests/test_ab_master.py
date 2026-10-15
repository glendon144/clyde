import os
import unittest
from unittest.mock import Mock, patch
from ab_master import Gate, peer_uid

class GateTests(unittest.TestCase):
    def setUp(self):
        self.broker = Mock()
        self.broker.review_task.return_value = {'approved': True, 'provider': 'shell'}
        self.now = 0
        self.gate = Gate(self.broker, {'role': 'developer', 'operations': [{'argv': ['/usr/bin/pwd'], 'cwd': os.path.realpath(os.getcwd())}]}, lambda: self.now)
        self.request = {'action': 'submit', 'argv': ['/usr/bin/pwd'], 'cwd': os.getcwd()}
    def submit(self):
        return self.gate.submit(self.request, os.getuid())
    def test_submit_and_human_deny_never_execute(self):
        with patch('ab_master.subprocess.run') as run:
            r = self.submit()
            self.assertEqual(r['status'], 'pending_human_approval')
            self.assertTrue(self.gate.administer({'action': 'deny', 'request_id': r['request_id']})['ok'])
            run.assert_not_called()
    def test_hard_denial(self):
        self.broker.review_task.return_value = {'approved': False}
        self.assertEqual(self.submit()['status'], 'denied')
        self.assertFalse(self.gate.pending)
    def test_spoofed_role_and_arguments_denied(self):
        self.request['role'] = 'maintainer'
        self.assertFalse(self.submit()['ok'])
        self.request.pop('role')
        self.request['argv'].append('unexpected')
        self.assertFalse(self.submit()['ok'])
    def test_digest_single_use(self):
        r = self.submit()
        a = {'action': 'approve', 'request_id': r['request_id'], 'digest': 'wrong'}
        self.assertFalse(self.gate.administer(a)['ok'])
        a['digest'] = r['digest']
        with patch('ab_master.subprocess.run') as run:
            run.return_value = Mock(returncode=0, stdout='ok', stderr='')
            self.assertTrue(self.gate.administer(a)['ok'])
            self.assertFalse(self.gate.administer(a)['ok'])
            run.assert_called_once()
            self.assertFalse(run.call_args.kwargs['shell'])
    def test_expiry_and_policy_recheck(self):
        r = self.submit()
        self.now = 301
        self.assertFalse(self.gate.administer({'action': 'show', 'request_id': r['request_id']})['ok'])
        r = self.submit()
        self.broker.review_task.return_value = {'approved': False}
        with patch('ab_master.subprocess.run') as run:
            self.assertEqual(self.gate.administer({'action': 'approve', 'request_id': r['request_id'], 'digest': r['digest']})['status'], 'denied_on_recheck')
            run.assert_not_called()
    def test_review_failures(self):
        self.gate.config.update(reviewer_provider='openai', reviewer_role='reviewer')
        for text in ['garbage', '{"recommend":"deny","reason":"unjustified"}']:
            self.broker.process_task.return_value = {'ok': True, 'result': {'text': text}}
            self.assertFalse(self.submit()['ok'])
            self.assertFalse(self.gate.pending)
    def test_peer_identity(self):
        import socket
        a, b = socket.socketpair()
        try:
            self.assertEqual(peer_uid(a), os.getuid())
        finally:
            a.close()
            b.close()

    def test_live_harmless_execution(self):
        r = self.submit()
        result = self.gate.administer({'action': 'approve', 'request_id': r['request_id'], 'digest': r['digest']})
        self.assertTrue(result['ok'])
        self.assertEqual(result['stdout'].strip(), os.getcwd())
    def test_positive_model_review_still_pending(self):
        self.gate.config.update(reviewer_provider='openai', reviewer_role='reviewer')
        self.broker.process_task.return_value = {'ok': True, 'result': {'text': '{"recommend":"proceed","reason":"bounded inspection"}'}}
        with patch('ab_master.subprocess.run') as run:
            self.assertEqual(self.submit()['status'], 'pending_human_approval')
            run.assert_not_called()
    def test_operating_hours_are_hard_denial(self):
        self.gate.config['utc_hours'] = [0, 0]
        self.assertEqual(self.submit()['status'], 'denied')
        self.broker.review_task.assert_not_called()
