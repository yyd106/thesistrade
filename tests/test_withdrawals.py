"""State-machine regressions using public example settings and isolated ledgers."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ashare import approvals, auth, finance, portfolio_risk, withdrawals
from ashare.storage import Store, now, normalize_time


class WithdrawalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.cfg = json.loads((Path(__file__).resolve().parents[1]/'config.example.json').read_text())
        self.cfg.update(data_dir=self.tmp.name, deployment_role='cloud', portfolio_strategy=None,
                        industry_enabled=False, watchlist=[])
        self.at = now()
        finance.PaperLedger(self.store).initialize()
        auth.setup(self.store, 'synthetic-admin-password', 'synthetic-guest-password')
        token, _ = auth.login(self.store, 'admin', 'synthetic-admin-password', 'synthetic', at=self.at)
        self.user = auth.session(self.store, auth.COOKIE+'='+token, at=self.at)

    def later(self, seconds):
        return normalize_time((datetime.fromisoformat(self.at)+timedelta(seconds=seconds)).isoformat())

    def cash(self, amount):
        with self.store.db:
            self.store.db.execute('UPDATE paper_accounts SET cash_cents=?', (amount,))

    def preview(self, action='TRANSFER', at=None):
        return withdrawals.request(self.store, self.cfg, at or self.at, action)

    def execute(self, preview, at=None, store=None):
        a = preview['approval']
        return withdrawals.execute(store or self.store, self.cfg, a['id'], self.user, a['hash'], at or self.at)

    def global_position(self, value):
        from ashare.global_market import SCALE
        qty = value*SCALE//100
        with self.store.db:
            self.store.db.execute('INSERT INTO global_quotes VALUES(?,?,?,?,?,?,?,?)',
                ('quote', 'BTC', self.at, self.at, 1_000_000, 1_000_000, self.at, '{}'))
            self.store.db.execute('INSERT INTO global_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('order', 'intent', None, 'BTC', 'BUY', qty, qty, 1_000_000, 0, self.at, self.later(60), 'FILLED', '{}'))
            self.store.db.execute('INSERT INTO global_fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('fill', 'order', 'quote', 'BTC', 'BUY', qty, 1_000_000, 1_000_000, value, 0, 0, self.at, self.at))
            self.store.db.execute('INSERT INTO global_lots VALUES(?,?,?,?,?)', ('fill', 'BTC', qty, value, self.at))
            self.store.db.execute('UPDATE paper_accounts SET cash_cents=?', (16_000_000-value,))

    def test_strict_threshold_and_odd_cent_whole_transfer(self):
        self.cash(15_000_000)
        self.assertEqual(withdrawals.refresh(self.store, self.cfg, self.at)['status'], 'IDLE')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM withdrawal_cycles').fetchone()[0], 0)
        self.cash(15_000_001)
        p = self.preview()
        self.assertEqual(p['status'], 'PREVIEW_READY')
        self.assertEqual(p['preview']['amount_cents'], 3_000_001)
        before = portfolio_risk.state(self.store)
        result = self.execute(p)
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual((result['after_equity_cents'], result['withdrawn_cents']), (12_000_000, 3_000_001))
        after = portfolio_risk.refresh(self.store, self.cfg, self.at)
        self.assertEqual(before['unit_nav'], after['unit_nav'])
        self.assertEqual(before['peak_unit_nav'], after['peak_unit_nav'])
        self.assertFalse(withdrawals.pending(self.store))

    def test_drawback_invalidates_preview_and_rebound_reuses_cycle(self):
        self.cash(16_000_000); old = self.preview()
        self.cash(15_000_000)
        view = withdrawals.refresh(self.store, self.cfg, self.later(1))
        self.assertEqual(view['status'], 'PAUSED_THRESHOLD')
        self.assertTrue(view['pending'])
        self.assertEqual(self.execute(old, self.later(1))['status'], 'STALE')
        self.assertEqual(self.preview(at=self.later(1))['status'], 'BLOCKED')
        self.cash(15_500_000); fresh = self.preview(at=self.later(2))
        self.assertEqual(fresh['preview']['cycle_id'], old['preview']['cycle_id'])
        self.assertEqual(fresh['preview']['amount_cents'], 3_500_000)
        self.assertEqual(self.execute(old, self.later(2))['status'], 'STALE')
        self.assertEqual(self.execute(fresh, self.later(2))['status'], 'COMPLETED')

    def test_cancel_releases_only_pending_and_rearms_same_stage_after_new_crossing(self):
        self.cash(16_000_000); transfer = self.preview()
        cancel = self.preview('CANCEL')
        self.assertEqual(cancel['preview']['amount_cents'], 0)
        self.assertEqual(cancel['preview']['release_cents'], 4_000_000)
        self.assertEqual(self.execute(cancel)['status'], 'CANCELLED')
        self.assertFalse(withdrawals.pending(self.store))
        self.assertEqual(withdrawals.reservation(self.store), 0)
        self.assertEqual(self.execute(transfer)['status'], 'STALE')
        self.assertEqual(self.preview()['status'], 'BLOCKED')
        self.cash(15_000_000); withdrawals.refresh(self.store, self.cfg, self.later(1))
        self.cash(16_500_000); again = self.preview(at=self.later(2))
        self.assertEqual(again['preview']['cycle_id'], transfer['preview']['cycle_id'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM withdrawal_cycles').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM paper_flows WHERE kind='SIMULATED_WITHDRAWAL'").fetchone()[0], 0)
        self.assertEqual(again['preview']['amount_cents'], 4_500_000)

    def test_multi_tier_jump_completes_only_current_tier(self):
        self.cash(35_000_000); p = self.preview()
        self.assertEqual(p['preview']['amount_cents'], 23_000_000)
        result = self.execute(p)
        self.assertEqual(result['withdrawal']['current_stage'], 2)
        self.assertEqual(withdrawals.refresh(self.store, self.cfg, self.later(1))['status'], 'IDLE')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM withdrawal_cycles').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT current_stage FROM paper_accounts').fetchone()[0], 2)

    def test_two_distinct_requests_same_cycle_cannot_double_transfer(self):
        self.cash(16_000_000); old = self.preview()
        self.cash(16_000_001); fresh = self.preview(at=self.later(1))
        self.assertNotEqual(old['approval']['id'], fresh['approval']['id'])
        def apply(p):
            s = Store(self.tmp.name)
            try: return self.execute(p, self.later(1), s)
            finally: s.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(apply, [old, fresh]))
        self.assertEqual(sorted(r['status'] for r in results), ['COMPLETED', 'STALE'])
        self.assertEqual(tuple(self.store.db.execute('SELECT cash_cents,current_stage,withdrawn_cents FROM paper_accounts').fetchone()),
                         (12_000_000, 2, 4_000_001))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM withdrawal_transfers').fetchone()[0], 1)

    def test_unchanged_poll_dedupes_preview_and_consumed_retry_requires_live_admin(self):
        self.cash(16_000_000); p = self.preview()
        self.assertEqual(self.preview(at=self.later(1))['approval']['id'], p['approval']['id'])
        result = self.execute(p, self.later(1))
        self.assertEqual(self.execute(p, self.later(2)), result)
        with self.store.db:
            self.store.db.execute('DELETE FROM app_sessions')
        with self.assertRaisesRegex(ValueError, '已登录管理员'):
            self.execute(p, self.later(3))

    def test_expired_preview_does_not_sign_or_transfer(self):
        self.cash(16_000_000); p = self.preview()
        later = self.later(86401)
        with self.store.db:
            self.store.db.execute('UPDATE app_sessions SET expires_at=?', (self.later(172800),))
        self.assertEqual(self.execute(p, later)['status'], 'STALE')
        self.assertIsNone(approvals.receipt_state(self.store, p['approval']['id']))
        self.assertEqual(self.store.db.execute('SELECT cash_cents FROM paper_accounts').fetchone()[0], 16_000_000)

    def test_accounting_disagreement_rolls_back_cash_units_and_latches_review(self):
        self.cash(16_000_000); p = self.preview()
        before = portfolio_risk.state(self.store)
        original = finance.transfer_inside
        def corrupt_result(*args, **kwargs):
            result = original(*args, **kwargs)
            return {**result, 'after_equity_cents': result['after_equity_cents']+1}
        with patch.object(finance, 'transfer_inside', side_effect=corrupt_result):
            result = self.execute(p)
        self.assertEqual(result['status'], 'REVIEW_REQUIRED')
        self.assertEqual(self.store.db.execute('SELECT cash_cents FROM paper_accounts').fetchone()[0], 16_000_000)
        self.assertEqual(portfolio_risk.state(self.store)['capital_units_ratio'], before['capital_units_ratio'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM withdrawal_transfers').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM paper_flows WHERE kind='SIMULATED_WITHDRAWAL'").fetchone()[0], 0)
        self.assertTrue(withdrawals.pending(self.store))
        self.assertEqual(withdrawals.reservation(self.store), 4_000_000)
        self.assertEqual(self.preview('CANCEL')['status'], 'BLOCKED')

    def test_unattributed_withdrawal_is_not_reconciled_by_matching_account_total(self):
        self.cash(16_000_000)
        with self.store.db:
            self.store.db.execute('UPDATE paper_accounts SET withdrawn_cents=100')
            self.store.db.execute('INSERT INTO paper_flows VALUES(?,?,?,?,?,?)',
                                 ('unknown', 'DEMO_PAPER', 'SIMULATED_WITHDRAWAL', -100, 'unknown', self.at))
        result = self.preview()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['withdrawal']['status'], 'REVIEW_REQUIRED')
        self.assertTrue(result['withdrawal']['pending'])
        self.assertIn('ACCOUNTING_MISMATCH', [x['code'] for x in result['withdrawal']['blockers']])

    def test_legacy_partial_history_is_preserved_and_remaining_is_not_double_subtracted(self):
        self.cash(17_000_000)
        portfolio_risk.refresh(self.store, self.cfg, self.at)
        ledger = finance.PaperLedger(self.store)
        old = ledger.plan(17_000_000)
        ledger.reconcile_simulated_transfer(old['id'], 1_000_000, 'synthetic-legacy-partial')
        frozen = tuple(self.store.db.execute('SELECT * FROM paper_withdrawals').fetchone())
        p = self.preview(at=self.later(2))
        self.assertEqual(p['status'], 'PREVIEW_READY', p)
        self.assertEqual(p['preview']['amount_cents'], 4_000_000)
        with self.assertRaisesRegex(ValueError, '旧测试入口'):
            ledger.reconcile_simulated_transfer(old['id'], 1, 'forbidden-legacy-reentry')
        result = self.execute(p, self.later(2))
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual(result['withdrawn_cents'], 5_000_000)
        self.assertEqual(tuple(self.store.db.execute('SELECT * FROM paper_withdrawals').fetchone()), frozen)
        self.assertFalse(withdrawals.pending(self.store))

    def test_legacy_partial_below_threshold_pauses_using_current_remaining(self):
        self.cash(17_000_000); portfolio_risk.refresh(self.store, self.cfg, self.at)
        ledger = finance.PaperLedger(self.store); old = ledger.plan(17_000_000)
        ledger.reconcile_simulated_transfer(old['id'], 1_000_000, 'synthetic-legacy-partial')
        self.cash(14_000_000)
        view = withdrawals.refresh(self.store, self.cfg, self.later(2))
        self.assertEqual(view['status'], 'PAUSED_THRESHOLD')
        self.assertEqual(view['candidate_cents'], 2_000_000)
        self.assertTrue(view['pending'])
        self.assertFalse(view['can_transfer'])

    def test_research_role_uses_mirror_and_never_mutates_account(self):
        self.cash(16_000_000)
        mirror = withdrawals.refresh(self.store, self.cfg, self.at)
        replica_tmp = tempfile.TemporaryDirectory(); self.addCleanup(replica_tmp.cleanup)
        replica = Store(replica_tmp.name); self.addCleanup(replica.close)
        with replica.db:
            replica.db.execute('INSERT INTO cloud_state VALUES(?,?)', ('withdrawals', json.dumps(mirror)))
        cfg = {**self.cfg, 'deployment_role': 'research'}
        self.assertEqual(withdrawals.status(replica, cfg, self.at), mirror)
        self.assertEqual(withdrawals.reservation(replica), 4_000_000)
        self.assertTrue(withdrawals.pending(replica))
        with self.assertRaises(ValueError): withdrawals.request(replica, cfg, self.at)
        self.assertIsNone(replica.db.execute('SELECT * FROM paper_accounts').fetchone())

    def test_new_quote_even_at_same_price_invalidates_confirmation_and_old_quotes_wait(self):
        self.global_position(500_000)
        p = self.preview()
        self.assertEqual(p['status'], 'PREVIEW_READY', p)
        with self.store.db:
            self.store.db.execute('INSERT INTO global_quotes VALUES(?,?,?,?,?,?,?,?)',
                ('quote-new', 'BTC', self.later(1), self.later(1), 1_000_000, 1_000_000, self.at, '{}'))
        self.assertEqual(self.execute(p, self.later(1))['status'], 'STALE')
        self.assertIsNone(approvals.receipt_state(self.store, p['approval']['id']))
        fresh = self.preview(at=self.later(1))
        self.assertEqual(fresh['status'], 'PREVIEW_READY')
        stale = withdrawals.refresh(self.store, self.cfg, self.later(92))
        self.assertEqual(stale['status'], 'WAITING')
        self.assertIn('VALUATION_INCOMPLETE', [b['code'] for b in stale['blockers']])
        self.assertTrue(stale['pending'])

    def test_global_five_percent_cap_is_checked_after_withdrawal_without_tolerance(self):
        self.global_position(600_000)
        self.assertEqual(self.preview()['status'], 'PREVIEW_READY')
        with self.store.db:
            self.store.db.execute('INSERT INTO global_quotes VALUES(?,?,?,?,?,?,?,?)',
                ('quote-new', 'BTC', self.later(1), self.later(1), 1_000_001, 1_000_000, self.at, '{}'))
        blocked = self.preview(at=self.later(1))
        self.assertEqual(blocked['status'], 'BLOCKED')
        self.assertIn('GLOBAL_LIMIT', [b['code'] for b in blocked['withdrawal']['blockers']])
        self.assertEqual(blocked['withdrawal']['equity_cents'], 16_000_001)
        self.assertTrue(blocked['withdrawal']['pending'])


if __name__ == '__main__':
    unittest.main()
