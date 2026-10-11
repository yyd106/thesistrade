"""Withdrawal reporting uses the same cashflow boundaries as each valuation."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ashare import digest
from ashare.finance import PaperLedger
from ashare.pipeline import load_config
from ashare.storage import Store, normalize_time


class WithdrawalReportingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cfg = load_config(Path(__file__).resolve().parents[1] / 'config.example.json', configure_model=False)
        self.cfg.update(data_dir=self.tmp.name, watchlist=[])
        PaperLedger(self.store).initialize()
        self.day = '2026-09-23'
        self.a, self.b = digest.day_window(self.day)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def stamp(self, text):
        return normalize_time(text + ':00+08:00')

    def mark(self, name, when, equity, withdrawn=0, complete=1):
        at = self.stamp(when)
        with self.store.db:
            self.store.db.execute('INSERT INTO equity_marks VALUES(?,?,?,?,?,?,?,?)',
                                  (name, at, at, equity, equity, withdrawn, complete, '{}'))

    def flow(self, name, when, amount):
        with self.store.db:
            self.store.db.execute('INSERT INTO paper_flows VALUES(?,?,?,?,?,?)',
                                  (name, 'DEMO_PAPER', 'SIMULATED_WITHDRAWAL', -amount, name, self.stamp(when)))

    def execution(self):
        return digest.execution(self.store, self.cfg, self.a, self.b)

    def test_pure_withdrawal_reduces_assets_but_has_zero_investment_loss(self):
        self.mark('before', '2026-09-22T15:00', 16_000_000)
        self.flow('transfer', '2026-09-23T10:00', 4_000_000)
        self.mark('after', '2026-09-23T15:00', 12_000_000, 4_000_000)
        result = self.execution()
        self.assertEqual(result['equity_change_cents'], -4_000_000)
        self.assertEqual(result['withdrawal_change_cents'], 4_000_000)
        self.assertEqual(result['cashflow_adjusted_change_cents'], 0)
        self.assertEqual(result['withdrawals'], {'count': 1, 'amount_cents': 4_000_000})

    def test_only_new_withdrawals_are_added_back_to_investment_change(self):
        self.mark('before', '2026-09-22T15:00', 20_000_000, 4_000_000)
        self.flow('first', '2026-09-23T10:00', 2_000_000)
        self.flow('second', '2026-09-23T12:00', 3_000_000)
        self.mark('after', '2026-09-23T15:00', 15_012_345, 9_000_000)
        result = self.execution()
        self.assertEqual(result['cashflow_adjusted_change_cents'], 12_345)
        self.assertEqual(result['withdrawal_change_cents'], 5_000_000)
        self.assertEqual(result['withdrawals'], {'count': 2, 'amount_cents': 5_000_000})

    def test_late_day_transfer_is_visible_without_inflating_earlier_mark_profit(self):
        self.mark('before', '2026-09-22T15:00', 16_000_000)
        self.mark('after', '2026-09-23T15:00', 16_010_000)
        self.flow('late', '2026-09-23T20:00', 4_010_000)
        result = self.execution()
        self.assertEqual(result['cashflow_adjusted_change_cents'], 10_000)
        self.assertEqual(result['withdrawal_change_cents'], 0)
        self.assertEqual(result['withdrawals']['amount_cents'], 4_010_000)

    def test_missing_start_keeps_investment_change_unknown(self):
        self.flow('transfer', '2026-09-23T10:00', 4_000_000)
        self.mark('after', '2026-09-23T15:00', 12_000_000, 4_000_000)
        result = self.execution()
        self.assertIsNone(result['cashflow_adjusted_change_cents'])
        self.assertIsNone(result['withdrawal_change_cents'])
        self.assertEqual(result['withdrawals']['amount_cents'], 4_000_000)

    def test_same_second_uses_last_complete_mark_and_excludes_next_day_flow(self):
        self.mark('before', '2026-09-22T15:00', 16_000_000)
        self.mark('pre-transfer', '2026-09-23T15:00', 16_000_000)
        self.mark('post-transfer', '2026-09-23T15:00', 12_000_000, 4_000_000)
        self.mark('incomplete', '2026-09-23T15:01', 1, 0, complete=0)
        self.flow('next-day', '2026-09-24T00:00', 99)
        result = self.execution()
        self.assertEqual(result['equity_end_cents'], 12_000_000)
        self.assertEqual(result['cashflow_adjusted_change_cents'], 0)
        self.assertEqual(result['withdrawals']['count'], 0)

    def test_daily_and_rollup_labels_distinguish_assets_cashflow_and_profit(self):
        self.mark('before', '2026-09-22T15:00', 16_000_000)
        self.flow('transfer', '2026-09-23T10:00', 4_000_000)
        self.mark('after', '2026-09-23T15:00', 12_000_000, 4_000_000)
        with patch('ashare.digest.now', return_value=self.stamp('2026-09-24T01:00')):
            report = digest.write(self.store, self.cfg, self.day)
            rollup = digest.rollup(self.store, self.cfg, self.day, self.day)
        daily = (self.store.root / report['report']).read_text()
        summary = (self.store.root / rollup['report']).read_text()
        self.assertIn('总资产 120,000.00 元', daily)
        self.assertIn('当天模拟利润提取：1 笔，共 40,000.00 元', daily)
        self.assertIn('资产变化 -40,000.00 元', daily)
        self.assertIn('投资损益 +0.00 元', daily)
        self.assertNotIn('较前一日', daily)
        self.assertIn('总资产（元）', summary)
        self.assertIn('不能直接将资产下降当作亏损', summary)

    def test_failed_summary_sync_is_visible_preserves_old_amounts_and_clears_on_recovery(self):
        from ashare import cloud_ledger, cloud_runtime, withdrawals
        cfg = {**self.cfg, 'deployment_role': 'research'}
        summary = {'version': withdrawals.VERSION, 'authority': 'cloud', 'account_id': 'DEMO_PAPER',
                   'current_stage': 1, 'reserved_cents': 4_000_000, 'equity_cents': 16_000_000,
                   'status': 'READY', 'pending': True, 'can_transfer': True, 'can_cancel': True, 'blockers': []}
        with self.store.db:
            cloud_ledger.import_extras(self.store, cfg, {'withdrawals': summary})
            cloud_ledger.import_extras(self.store, cfg, {'withdrawals_error': 'synthetic unavailable'})
        shown = withdrawals.status(self.store, cfg, self.a)
        self.assertEqual(shown['equity_cents'], 16_000_000)
        self.assertEqual(shown['reserved_cents'], 4_000_000)
        self.assertTrue(shown['pending'])
        self.assertFalse(shown['can_transfer'])
        self.assertFalse(shown['can_cancel'])
        self.assertEqual([b['code'] for b in shown['blockers']], ['MIRROR_SYNC_ERROR'])
        self.assertIn('上次同步结果', shown['blockers'][0]['message'])
        self.assertEqual(cloud_runtime.value(self.store, 'withdrawals'), summary)
        with patch('ashare.digest.now', return_value=self.stamp('2026-09-24T01:00')):
            daily = digest.build(self.store, cfg, self.day)
        self.assertTrue(any('模拟提取摘要没有同步到本机' in flag for flag in daily['flags']))
        updated = {**summary, 'current_stage': 2, 'reserved_cents': 0, 'equity_cents': 12_000_000,
                   'status': 'IDLE', 'pending': False, 'can_transfer': False, 'can_cancel': False}
        with self.store.db:
            cloud_ledger.import_extras(self.store, cfg, {'withdrawals': updated})
        self.assertEqual(withdrawals.status(self.store, cfg, self.a), updated)
        self.assertNotIn('withdrawals', digest.extras_errors(self.store))

    def test_invalid_summary_keeps_previous_projection_with_explicit_sync_error(self):
        from ashare import cloud_ledger, withdrawals
        cfg = {**self.cfg, 'deployment_role': 'research'}
        summary = {'version': withdrawals.VERSION, 'authority': 'cloud', 'account_id': 'DEMO_PAPER',
                   'current_stage': 1, 'reserved_cents': 4_000_000, 'equity_cents': 16_000_000,
                   'status': 'WAITING', 'pending': True, 'can_transfer': False, 'can_cancel': True,
                   'blockers': [{'code': 'INSUFFICIENT_CASH', 'message': '现金不足'}]}
        with self.store.db:
            cloud_ledger.import_extras(self.store, cfg, {'withdrawals': summary})
            cloud_ledger.import_extras(self.store, cfg, {'withdrawals': {**summary, 'reserved_cents': -1}})
        shown = withdrawals.status(self.store, cfg, self.a)
        self.assertEqual(shown['reserved_cents'], 4_000_000)
        self.assertEqual([b['code'] for b in shown['blockers']], ['INSUFFICIENT_CASH', 'MIRROR_SYNC_ERROR'])
        self.assertFalse(shown['can_cancel'])
        self.assertFalse(shown['can_transfer'])


if __name__ == '__main__':
    unittest.main()
