import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('emt_probe', Path(__file__).parents[1] / 'tools/emt_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class EmtQueryTests(unittest.TestCase):
    def test_tcp_refusal_is_not_an_authentication_failure(self):
        error = ConnectionRefusedError(probe.errno.ECONNREFUSED, 'untrusted raw detail')
        with patch.object(probe.socket, 'create_connection', side_effect=error):
            result = probe.tcp_check()
        self.assertEqual(result['status'], 'CONNECTION_REFUSED')
        self.assertNotIn('untrusted raw detail', json.dumps(result))

    def test_failed_preflight_never_prompts_for_credentials(self):
        args = SimpleNamespace(build=False, check=False, network_check=False, output=None)
        refused = {'status': 'CONNECTION_REFUSED'}
        process = probe.subprocess.CompletedProcess([], 0, probe.PREFIX + json.dumps(refused), '')
        with patch.object(probe, 'host_route', return_value={'interface': 'utun0', 'tunnel_detected': True}), \
             patch.object(probe, 'tcp_check', return_value=refused), \
             patch.object(probe, 'run_container', return_value=process) as run, \
             patch.object(probe.getpass, 'getpass') as prompt, \
             patch.object(probe, 'emit_result', return_value=1) as emit:
            self.assertEqual(probe.host_main(args), 1)
        prompt.assert_not_called()
        self.assertIsNone(run.call_args.args[2])
        result = emit.call_args.args[0]
        self.assertEqual(result['status'], 'NETWORK_BLOCKED')
        self.assertFalse(result['login_verified'])
        self.assertIn('VPN', result['next_step_zh'])

    def test_container_connectivity_determines_preflight_and_does_not_claim_login(self):
        result = probe.network_result({'status': 'CONNECTION_REFUSED'}, {'status': 'CONNECTED'}, {})
        self.assertEqual(result['status'], 'NETWORK_READY')
        self.assertFalse(result['login_verified'])
        result = probe.network_result({'status': 'CONNECTED'}, {'status': 'CONNECTION_REFUSED'}, {})
        self.assertEqual(result['status'], 'NETWORK_BLOCKED')

    def test_timeout_diagnostics_do_not_expose_native_output(self):
        output = b'private native login arguments\nEMT_PROBE_STAGE=login\nEMT_PROBE_STAGE=secret\n'
        self.assertEqual(probe.last_stage(output), 'login')
        self.assertEqual(probe.last_stage(b'private details'), 'container_start')

    def test_partial_wrong_session_and_wrong_request_cannot_finish(self):
        replies = probe.Replies()
        replies.begin(1, 'positions', 7)
        replies.receive('positions', {'ticker': '600519'}, {}, 1, False, 7)
        replies.receive('positions', {}, {}, 1, True, 8)
        replies.receive('positions', {}, {}, 2, True, 7)
        replies.receive('trades', {}, {}, 1, True, 7)
        with self.assertRaises(probe.ProbeError):
            replies.finish(1, 0)
        replies.receive('positions', {}, {}, 1, True, 7)
        self.assertEqual(replies.finish(1, 0), [{'ticker': '600519'}])

    def test_empty_and_failed_queries_are_distinct(self):
        replies = probe.Replies()
        replies.begin(1, 'positions', 7)
        replies.receive('positions', {}, {}, 1, True, 7)
        self.assertEqual(replies.finish(1, 0), [])
        replies.begin(2, 'assets', 7)
        replies.receive('assets', {}, {}, 2, True, 7)
        with self.assertRaises(probe.ProbeError):
            replies.finish(2, 0)
        replies.begin(3, 'positions', 7)
        replies.receive('positions', {}, {'error_id': 42, 'error_msg': 'secret'}, 3, True, 7)
        with self.assertRaisesRegex(probe.ProbeError, '^positions: broker error 42$'):
            replies.finish(3, 0)

    def test_disconnect_invalidates_partial_result_and_sensitive_fields_removed(self):
        replies = probe.Replies()
        replies.begin(1, 'assets', 7)
        replies.receive('assets', {'buying_power': 100, 'account': 'private', 'password': 'private'}, {}, 1, True, 7)
        self.assertEqual(replies.finish(1, 0), [{'buying_power': 100}])
        replies.fail('disconnected')
        with self.assertRaises(probe.ProbeError):
            replies.finish(1, 0)

    def test_probe_calls_only_queries_and_does_not_continue_after_rejection(self):
        class FakeApi:
            def __init__(self):
                self.replies = probe.Replies()
                self.calls = []

            def queryAsset(self, session, reqid):
                self.calls.append('assets')
                self.replies.receive('assets', {'account_type': 0}, {}, reqid, True, session)
                return 0

            def queryPosition(self, ticker, session, reqid):
                self.calls.append('positions')
                return 1

            def getApiLastError(self):
                return {'error_id': 99, 'error_msg': 'secret'}
        api = FakeApi()
        with self.assertRaisesRegex(probe.ProbeError, 'query rejected'):
            probe.run_queries(api, 7, timeout=0)
        self.assertEqual(api.calls, ['assets', 'positions'])


if __name__ == '__main__':
    unittest.main()
