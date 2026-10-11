"""Simulated money actions use authenticated cloud HTTP and isolated SQLite only."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ashare import approvals, auth, withdrawals
from ashare.dashboard import make_handler
from ashare.finance import PaperLedger
from ashare.storage import Store, now
import test_dashboard as fixtures


class WithdrawalHttpTests(unittest.TestCase):
    request = fixtures.DashboardTests.request

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'config.json'
        self.cfg = json.loads((Path(__file__).resolve().parents[1] / 'config.example.json').read_text())
        self.cfg.update(data_dir=str(Path(self.tmp.name) / 'data'), deployment_role='cloud',
                        scheduler_enabled=False, model_enabled=False, public_origin='https://withdrawal.example')
        self.path.write_text(json.dumps(self.cfg))
        self.data = self.cfg['data_dir']
        with closing(Store(self.data)) as store:
            auth.setup(store, 'test-admin-password', 'test-guest-password')
            token, self.user = auth.login(store, 'admin', 'test-admin-password', 'withdrawal-admin')
            self.cookie = auth.COOKIE + '=' + token
            self.csrf = self.user['csrf_token']
            guest_token, guest = auth.login(store, 'guest', 'test-guest-password', 'withdrawal-guest')
            self.guest_headers = {'Cookie': auth.COOKIE + '=' + guest_token, 'X-CSRF-Token': guest['csrf_token']}
            PaperLedger(store).initialize()
            with store.db:
                # A synthetic all-cash account; no production ledger is read.
                store.db.execute('UPDATE paper_accounts SET cash_cents=16000000')
        self.handler = make_handler(self.path, 'page-token', 8765)

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, path, body, headers=None):
        code, raw, _ = self.request('POST', path, body,
            {'X-CSRF-Token': self.csrf, 'Origin': self.cfg['public_origin'], **(headers or {})})
        return code, json.loads(raw)

    def preview(self, action='TRANSFER'):
        code, value = self.post('/api/withdrawals/request', {'action': action})
        self.assertEqual(code, 200, value)
        self.assertEqual(value['status'], 'PREVIEW_READY', value)
        return value['approval']

    def execute(self, request, headers=None, **changes):
        return self.post('/api/withdrawals/execute',
            {'approval_id': request['id'], 'expected_hash': request['hash'], **changes}, headers)

    def ledger(self):
        with closing(Store(self.data)) as store:
            return tuple(store.db.execute('SELECT cash_cents,current_stage,withdrawn_cents FROM paper_accounts').fetchone())

    def test_preview_then_explicit_confirmation_and_replay_change_cash_once(self):
        request = self.preview()
        self.assertEqual(self.ledger(), (16000000, 1, 0))
        with closing(Store(self.data)) as store:
            self.assertIsNone(approvals.receipt_state(store, request['id']))
        code, result = self.execute(request)
        self.assertEqual(code, 200, result)
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual(self.ledger(), (12000000, 2, 4000000))
        self.assertEqual(self.execute(request), (code, result))
        self.assertEqual(self.ledger(), (12000000, 2, 4000000))
        code, raw, _ = self.request('GET', '/api/withdrawals/result?approval_id=' + request['id'])
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)['result'], result)
        with closing(Store(self.data)) as store:
            self.assertEqual(store.db.execute("SELECT count(*) FROM paper_flows WHERE kind='SIMULATED_WITHDRAWAL'").fetchone()[0], 1)
            self.assertEqual(store.db.execute('SELECT count(*) FROM approval_consumptions WHERE request_id=?', (request['id'],)).fetchone()[0], 1)

    def test_concurrent_duplicate_confirmation_is_idempotent(self):
        request = self.preview()
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: self.execute(request), range(2)))
        self.assertEqual([code for code, _ in replies], [200, 200], replies)
        self.assertEqual(replies[0], replies[1])
        self.assertEqual(self.ledger(), (12000000, 2, 4000000))

    def test_unreceived_execution_can_be_read_and_explicitly_reconfirmed(self):
        request = self.preview()
        # The browser lost contact before its execute request reached the server.
        code, raw, _ = self.request('GET', '/api/withdrawals/result?approval_id=' + request['id'])
        self.assertEqual(code, 200)
        value = json.loads(raw)
        self.assertEqual(value['status'], 'PENDING')
        self.assertIsNone(value['result'])
        self.assertEqual(value['approval']['snapshot'], request['snapshot'])
        self.assertEqual(value['approval']['hash'], request['hash'])
        self.assertEqual(self.ledger(), (16000000, 1, 0))
        with closing(Store(self.data)) as store:
            self.assertEqual(withdrawals.reservation(store), 4000000)
            self.assertIsNone(approvals.receipt_state(store, request['id']))
        self.assertEqual(self.execute(value['approval'])[1]['status'], 'COMPLETED')
        self.assertEqual(self.ledger(), (12000000, 2, 4000000))

    def test_expired_unreceived_request_can_be_replaced_without_releasing_reserve(self):
        request = self.preview()
        later = (datetime.fromisoformat(request['expires_at']) + timedelta(seconds=1)).isoformat()
        with closing(Store(self.data)) as store:
            token, user = auth.login(store, 'admin', 'test-admin-password', 'withdrawal-relogin', at=later)
            self.cookie, self.csrf = auth.COOKIE + '=' + token, user['csrf_token']
        with patch('ashare.approvals.now', return_value=later), patch('ashare.dashboard.now', return_value=later), patch('ashare.auth.now', return_value=later):
            code, raw, _ = self.request('GET', '/api/withdrawals/result?approval_id=' + request['id'])
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(raw)['status'], 'EXPIRED')
            self.assertIsNone(json.loads(raw)['result'])
            with closing(Store(self.data)) as store:
                self.assertEqual(withdrawals.reservation(store), 4000000)
            self.assertEqual(self.execute(request)[0], 409)
            replacement = self.preview()
            self.assertNotEqual(replacement['id'], request['id'])
            self.assertEqual(replacement['snapshot']['amount_cents'], request['snapshot']['amount_cents'])
            self.assertEqual(self.execute(replacement)[1]['status'], 'COMPLETED')
        self.assertEqual(self.ledger(), (12000000, 2, 4000000))

    def test_stale_preview_and_wrong_hash_cannot_move_money(self):
        request = self.preview()
        self.assertEqual(self.execute(request, expected_hash='f' * 64)[0], 400)
        with closing(Store(self.data)) as store, store.db:
            store.db.execute('UPDATE paper_accounts SET cash_cents=15500000')
        code, value = self.execute(request)
        self.assertEqual(code, 409, value)
        self.assertIn(value['status'], ('STALE', 'BLOCKED'))
        self.assertEqual(self.ledger(), (15500000, 1, 0))
        with closing(Store(self.data)) as store:
            self.assertIsNone(approvals.receipt_state(store, request['id']))
            self.assertEqual(store.db.execute('SELECT count(*) FROM approval_consumptions').fetchone()[0], 0)
            self.assertEqual(store.db.execute("SELECT count(*) FROM paper_flows WHERE kind='SIMULATED_WITHDRAWAL'").fetchone()[0], 0)
        self.assertEqual(self.execute(request)[0], 409)
        self.assertEqual(self.ledger(), (15500000, 1, 0))

    def test_auth_origin_csrf_and_forged_fields_are_rejected_before_execution(self):
        request = self.preview()
        for headers, code in [({'Cookie': ''}, 401), (self.guest_headers, 403),
                              ({'X-CSRF-Token': 'wrong'}, 403), ({'Origin': 'https://foreign.example'}, 403),
                              ({'Host': 'foreign.example'}, 403)]:
            with self.subTest(headers=tuple(headers)):
                self.assertEqual(self.execute(request, headers)[0], code)
        for changes in ({'amount_cents': 4000000}, {'user': {'role': 'ADMIN'}}, {'authority': 'CLOUD_ADMIN'}):
            self.assertEqual(self.execute(request, **changes)[0], 400)
        self.assertEqual(self.post('/api/withdrawals/request', {'action': 'TRANSFER', 'amount_cents': 1})[0], 400)
        self.assertEqual(self.post('/api/withdrawals/request', {'action': 'REAL_TRANSFER'})[0], 400)
        self.assertEqual(self.ledger(), (16000000, 1, 0))

    def test_cancel_needs_separate_preview_and_never_changes_cash_or_stage(self):
        self.preview()
        request = self.preview('CANCEL')
        self.assertEqual(request['snapshot']['amount_cents'], 0)
        self.assertEqual(request['snapshot']['release_cents'], 4000000)
        code, result = self.execute(request)
        self.assertEqual(code, 200, result)
        self.assertEqual(result['status'], 'CANCELLED')
        self.assertEqual(self.ledger(), (16000000, 1, 0))
        self.assertEqual(self.execute(request)[1], result)

    def test_completed_request_replay_still_requires_hash_and_current_admin_session(self):
        request = self.preview()
        self.assertEqual(self.execute(request)[1]['status'], 'COMPLETED')
        self.assertEqual(self.execute(request, expected_hash='f' * 64)[0], 400)
        self.assertEqual(self.execute(request, self.guest_headers)[0], 403)
        with closing(Store(self.data)) as store, store.db:
            store.db.execute("UPDATE app_sessions SET expires_at='2000-01-01T00:00:00+00:00' WHERE username='admin'")
        self.assertEqual(self.execute(request)[0], 401)
        self.assertEqual(self.request('GET', '/api/withdrawals/result?approval_id=' + request['id'])[0], 401)
        self.assertEqual(self.ledger(), (12000000, 2, 4000000))

    def test_guest_summary_is_read_only_and_private_results_are_admin_only(self):
        request = self.preview()
        with closing(Store(self.data)) as store:
            tables = ('paper_accounts', 'paper_flows', 'withdrawal_cycles', 'withdrawal_events', 'approval_consumptions')
            before = {name: list(map(tuple, store.db.execute('SELECT * FROM ' + name))) for name in tables}
            code, raw, _ = self.request('GET', '/api/withdrawals', headers=self.guest_headers)
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(raw)['current_stage'], 1)
            self.assertEqual({name: list(map(tuple, store.db.execute('SELECT * FROM ' + name))) for name in tables}, before)
        self.assertEqual(self.request('GET', '/api/withdrawals/result?approval_id=' + request['id'], headers=self.guest_headers)[0], 403)
        self.assertEqual(self.post('/api/withdrawals/request', {'action': 'TRANSFER'}, self.guest_headers)[0], 403)
        with closing(Store(self.data)) as store:
            other = approvals.create_request(store, kind='CONFIG', subject_id='fixture', action='APPLY',
                snapshot={'setting': True}, summary={'title': 'unrelated'}, at=now())
        self.assertEqual(self.request('GET', '/api/withdrawals/result?approval_id=' + other['id'])[0], 400)
        self.assertEqual(self.request('GET', '/api/withdrawals/result?approval_id=missing')[0], 400)

    def test_main_dashboard_includes_the_authoritative_withdrawal_summary(self):
        self.preview()
        with closing(Store(self.data)) as store, store.db:
            store.db.execute('INSERT OR REPLACE INTO cloud_state VALUES(?,?)',
                ('display', json.dumps({'watchlist': [], 'observation': {'items': []}})))
        code, raw, _ = self.request('GET', '/api/status')
        self.assertEqual(code, 200, raw)
        summary = json.loads(raw)['withdrawals']
        self.assertEqual((summary['status'], summary['authority'], summary['candidate_cents']), ('READY', 'cloud', 4000000))
        self.assertTrue(summary['can_transfer'])

    def test_research_reads_mirror_and_cannot_generate_or_execute_local_money(self):
        self.cfg.update(deployment_role='research')
        self.path.write_text(json.dumps(self.cfg));self.handler = make_handler(self.path, 'page-token', 8765)
        code, raw, _ = self.request('GET', '/api/withdrawals')
        self.assertEqual(code, 200, raw)
        unsynced = json.loads(raw)
        self.assertEqual(unsynced['status'], 'NOT_SYNCED')
        self.assertIsNone(unsynced['cash_cents'])
        self.assertFalse(unsynced['can_transfer'])
        mirror = {'version': 'simulated-withdrawal-v1', 'status': 'WAITING', 'current_stage': 3,
                  'threshold_cents': 30000000, 'retain_cents': 20000000, 'equity_cents': 33000000}
        with closing(Store(self.data)) as store, store.db:
            store.db.execute('INSERT OR REPLACE INTO cloud_state VALUES(?,?)', ('withdrawals', json.dumps(mirror)))
        code, raw, _ = self.request('GET', '/api/withdrawals')
        self.assertEqual(code, 200, raw)
        self.assertEqual(json.loads(raw)['current_stage'], 3)
        for path, body in [('/api/withdrawals/request', {'action': 'TRANSFER'}),
                           ('/api/withdrawals/execute', {'approval_id': 'anything', 'expected_hash': 'f' * 64})]:
            self.assertEqual(self.post(path, body, {'Origin': 'http://127.0.0.1:8765'})[0], 403)
        self.assertEqual(self.request('GET', '/api/withdrawals/result?approval_id=anything')[0], 403)
        self.assertEqual(self.ledger(), (16000000, 1, 0))


if __name__ == '__main__':
    unittest.main()
