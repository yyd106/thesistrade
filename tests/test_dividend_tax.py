"""Production entry-point tests with synthetic trades and temporary databases."""
import tempfile
import unittest
from pathlib import Path

from ashare import dividend_tax, dividends, paper
from ashare.cloud_runtime import put
from ashare.demo import SYMBOL, put_quote
from ashare.pipeline import load_config
from ashare.scheduler import Scheduler
from ashare.settings import DEFAULTS
from ashare.storage import Store, normalize_time
import test_workflow as workflow_fixtures


class DividendTaxExecutionTests(unittest.TestCase):
    plan = workflow_fixtures.WorkflowTests.plan
    slot = workflow_fixtures.WorkflowTests.slot

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cfg = load_config(Path(__file__).resolve().parents[1] / 'config.example.json', configure_model=False)
        for key in ('collection_times', 'slot_times', 'slot_deadline_seconds', 'slot_execution_mode'):
            self.cfg[key] = DEFAULTS[key]
        self.cfg.update(data_dir=self.tmp.name, deployment_role='standalone', investment_policy=None,
                        portfolio_strategy=None, model_enabled=True, paper_slippage_bps=0,
                        paper_max_fill_qty=10000, watchlist=[{'symbol': SYMBOL, 'name': '合成测试'}])
        self.at = '2026-09-15T10:00:00+08:00'

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def cash(self):
        return self.store.db.execute("SELECT cash_cents FROM paper_accounts WHERE id='DEMO_PAPER'").fetchone()[0]

    def hold(self):
        self.plan()
        put_quote(self.store, self.at)
        self.slot()
        put_quote(self.store, '2026-09-15T10:00:10+08:00')
        paper.settle(self.store, self.cfg, '2026-09-15T10:00:10+08:00')
        return paper.positions(self.store, normalize_time(self.at))[SYMBOL]['qty']

    def credit(self, at='2026-09-16T09:00:00+08:00', config=None):
        return dividends.apply(self.store, SYMBOL,
                               {'record_date': '2026-09-15', 'ex_date': '2026-09-16', 'cash': '0.1'},
                               at, config or self.cfg)

    def sell(self):
        at = '2026-09-16T10:00:00+08:00'
        put_quote(self.store, at, 925)
        self.slot('SELL', at=at)
        put_quote(self.store, '2026-09-16T10:00:10+08:00', 925)
        return paper.settle(self.store, self.cfg, '2026-09-16T10:00:10+08:00')

    def test_actual_settlement_waits_for_close_and_preserves_original_fills_and_costs(self):
        qty = self.hold()
        self.credit()
        self.assertTrue(self.sell())
        before = self.cash()
        fills = [tuple(r) for r in self.store.db.execute('SELECT * FROM paper_fills ORDER BY id')]
        lots = [tuple(r) for r in self.store.db.execute('SELECT * FROM paper_lots ORDER BY id')]
        self.assertEqual(dividend_tax.tax_history(self.store), [])
        self.assertEqual(paper.settle(self.store, self.cfg, '2026-09-16T15:01:00+08:00'), [])
        self.assertEqual(self.cash(), before - qty * 2)
        self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT * FROM paper_fills ORDER BY id')], fills)
        self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT * FROM paper_lots ORDER BY id')], lots)
        self.assertEqual(tuple(self.store.db.execute('SELECT initial_cents,withdrawn_cents FROM paper_accounts').fetchone()), (10000000, 0))
        self.assertEqual(dividend_tax.reconcile_taxes(self.store, self.cfg, '2026-09-17T18:00:00+08:00')['tax_cents'], 0)

    def test_scheduler_catches_up_tax_even_when_dividend_and_sale_are_older_than_40_days(self):
        qty = self.hold()
        self.credit()
        self.sell()
        before = self.cash()
        done = Scheduler.credit_dividends(None, self.store, self.cfg, '2026-12-01T18:00:00+08:00')
        self.assertIsNotNone(done)
        self.assertEqual(self.cash(), before - qty * 2)
        row = dividend_tax.tax_history(self.store)[0]
        self.assertEqual(row['sell_day'], '2026-09-16')
        self.assertEqual(row['posted_at'], normalize_time('2026-12-01T18:00:00+08:00'))

    def test_insufficient_cash_rolls_back_and_scheduler_records_explicit_retryable_failure(self):
        qty = self.hold()
        self.credit()
        self.sell()
        with self.store.db:
            self.store.db.execute('UPDATE paper_accounts SET cash_cents=0')
        at = '2026-09-16T18:00:00+08:00'
        with self.assertRaisesRegex(ValueError, 'DIVIDEND_TAX_INSUFFICIENT_CASH'):
            dividend_tax.reconcile_taxes(self.store, self.cfg, at)
        self.assertEqual(self.cash(), 0)
        self.assertEqual(dividend_tax.tax_history(self.store), [])
        self.assertIsNone(Scheduler.credit_dividends(None, self.store, self.cfg, at))
        error = self.store.db.execute("SELECT value FROM service_state WHERE key='dividend_error'").fetchone()[0]
        self.assertIn('DIVIDEND_TAX_INSUFFICIENT_CASH', error)
        self.assertEqual(dividend_tax.tax_due(self.store, at)['due_cents'], qty * 2)

    def test_unactivated_cloud_cannot_post_but_expired_research_heartbeat_does_not_erase_debt(self):
        qty = self.hold()
        self.credit()
        self.sell()
        at = '2026-09-16T18:00:00+08:00'
        cloud = {**self.cfg, 'deployment_role': 'cloud'}
        for initialized, enabled in ((False, False), (True, False), (False, True)):
            with self.store.db:
                put(self.store, 'initialized', initialized)
                put(self.store, 'execution_enabled', enabled)
            self.assertEqual(dividend_tax.reconcile_taxes(self.store, cloud, at)['status'], 'SKIPPED')
            self.assertEqual(dividend_tax.tax_history(self.store), [])
        with self.store.db:
            put(self.store, 'initialized', True)
            put(self.store, 'execution_enabled', True)
            put(self.store, 'research_completed_at', normalize_time('2026-09-01T00:00:00+08:00'))
        self.assertEqual(dividend_tax.reconcile_taxes(self.store, cloud, at)['tax_cents'], qty * 2)

    def test_research_credit_entry_point_is_read_only(self):
        self.hold()
        self.sell()
        before = self.cash()
        research = {**self.cfg, 'deployment_role': 'research'}
        self.assertIsNone(self.credit('2026-09-16T18:00:00+08:00', research))
        self.assertEqual(dividends.credit(self.store, research, '2026-09-16T18:00:00+08:00')['credited'], [])
        self.assertEqual(self.cash(), before)
        self.assertEqual(dividend_tax.tax_history(self.store), [])

    def test_no_tax_without_actual_dividend_credit_and_inside_requires_lock(self):
        self.hold()
        self.sell()
        at = '2026-09-16T18:00:00+08:00'
        self.assertEqual(dividend_tax.tax_due(self.store, at)['due_cents'], 0)
        with self.assertRaisesRegex(RuntimeError, '资金事务'):
            dividend_tax.reconcile_taxes_inside(self.store, self.cfg, at)


if __name__ == '__main__':
    unittest.main()
