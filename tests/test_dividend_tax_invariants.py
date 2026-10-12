"""Independent dividend-tax invariants, using temporary synthetic ledgers only.

Unverified month-end anniversary windows must block a deduction. Ordinary
anniversaries and daily netting follow the tax authority's 2012/2015 guidance;
tax rounding follows ChinaClear's 2025 participant interface (page 25).
"""
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from ashare import dividend_tax, dividends, review_portfolio
from ashare.finance import PaperLedger
from ashare.storage import Store, normalize_time


SYMBOL = 'SH600000'
ACCOUNT = 'DEMO_PAPER'
AT = '2026-10-12T18:00:00+08:00'


class DividendTaxInvariantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cfg = {'deployment_role': 'standalone', 'mode': 'paper', 'watchlist': []}
        self.fill_sequence = 0
        PaperLedger(self.store).initialize()
        with self.store.db:
            self.store.db.execute("UPDATE paper_flows SET created_at=? WHERE kind='SIMULATED_INITIAL'",
                                  (normalize_time('2024-01-01T00:00:00+08:00'),))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def fill(self, fid, side, qty, at, symbol=SYMBOL):
        """Insert synthetic completed orders/fills and their zero-fee cash movement."""
        stamp = normalize_time(at)
        self.fill_sequence += 1
        scheduled = (datetime.fromisoformat(stamp) + timedelta(microseconds=self.fill_sequence)).isoformat()
        plan = 'plan-' + symbol
        with self.store.db:
            self.store.db.execute('INSERT OR IGNORE INTO snapshots VALUES(?,NULL,?,?,?,?,?)',
                ('snapshot-' + symbol, symbol, stamp, stamp, 'synthetic', '{}'))
            self.store.db.execute('INSERT OR IGNORE INTO studies VALUES(?,?,?,?,?,?)',
                ('study-' + symbol, 'snapshot-' + symbol, symbol, stamp, 'SUCCEEDED', '{}',))
            self.store.db.execute('INSERT OR IGNORE INTO plans VALUES(?,?,?,?,?,?,?,?)',
                (plan, 'study-' + symbol, symbol, stamp, stamp, 'ACTIVE', 'synthetic', '{}'))
            self.store.db.execute('INSERT INTO slots VALUES(?,?,?,?,?,?,?)',
                (fid + '-slot', scheduled, scheduled, scheduled, 'COMPLETED', '{}', 'SUCCEEDED'))
            self.store.db.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?)',
                (fid + '-decision', fid + '-slot', symbol, plan, stamp, side, 'SUBMITTED', 'synthetic', '{}'))
            self.store.db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?)',
                (fid + '-quote', symbol, 'Synthetic', 1000, 1000, stamp, stamp, 'synthetic', 'synthetic'))
            self.store.db.execute('''INSERT INTO paper_orders
                (id,decision_id,plan_id,symbol,side,qty,filled_qty,limit_cents,
                 reserved_cents,created_at,expires_at,status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'FILLED')''',
                (fid + '-order', fid + '-decision', plan, symbol,
                 side, qty, qty, 1000, 0, stamp, stamp))
            self.store.db.execute('INSERT INTO paper_fills VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (fid, fid + '-order', fid + '-quote', symbol, side, qty, 1000, 0, 0, stamp, stamp))
            self.store.db.execute('UPDATE paper_accounts SET cash_cents=cash_cents+? WHERE id=?',
                                  ((1 if side == 'SELL' else -1) * qty * 1000, ACCOUNT))

    def credit(self, record='2026-09-24', ex='2026-09-25', cash='2.51', at='2026-09-25T18:00:00+08:00'):
        return dividends.apply(self.store, SYMBOL,
                               {'record_date': record, 'ex_date': ex, 'cash': cash}, at, self.cfg)

    def cash(self, store=None):
        return (store or self.store).db.execute('SELECT cash_cents FROM paper_accounts WHERE id=?',
                                               (ACCOUNT,)).fetchone()[0]

    def standard(self):
        self.fill('buy', 'BUY', 100, '2026-09-22T10:00:00+08:00')
        self.credit()
        self.fill('sell', 'SELL', 100, '2026-10-08T10:00:00+08:00')

    def test_natural_month_year_and_unambiguous_month_end_boundaries(self):
        cases = [
            ('2026-01-08', '2026-02-08', 2000), ('2026-01-08', '2026-02-09', 1000),
            ('2026-01-08', '2027-01-08', 1000), ('2026-01-08', '2027-01-09', 0),
            ('2026-01-31', '2026-02-28', 2000), ('2026-01-31', '2026-03-04', 1000),
            ('2024-01-31', '2024-02-29', 2000), ('2024-01-31', '2024-03-03', 1000),
            ('2024-02-29', '2025-02-28', 1000), ('2024-02-29', '2025-03-02', 0),
            ('2023-03-01', '2024-03-01', 1000), ('2023-03-01', '2024-03-02', 0),
        ]
        for bought, sold, expected in cases:
            with self.subTest(bought=bought, sold=sold):
                self.assertEqual(dividend_tax.holding_rate_bps(bought, sold), expected)

    def test_unverified_missing_anniversary_dates_do_not_guess_a_tax_rate(self):
        cases = [('2026-01-31', '2026-03-01'), ('2026-01-31', '2026-03-03'),
                 ('2024-01-31', '2024-03-01'), ('2024-01-31', '2024-03-02'),
                 ('2024-02-29', '2025-03-01')]
        for bought, sold in cases:
            with self.subTest(bought=bought, sold=sold):
                with self.assertRaisesRegex(ValueError, 'HOLDING_BOUNDARY_UNVERIFIED'):
                    dividend_tax.holding_rate_bps(bought, sold)

    def test_27_synthetic_fills_one_dividend_yields_exactly_5020_cents(self):
        self.standard()
        for i in range(12):
            self.fill(f'other-buy-{i}', 'BUY', 100, '2026-09-01T10:00:00+08:00', 'SZ000001')
            self.fill(f'other-sell-{i}', 'SELL', 100, '2026-09-02T10:00:00+08:00', 'SZ000001')
        self.fill('other-held', 'BUY', 100, '2026-09-03T10:00:00+08:00', 'SZ000002')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_fills').fetchone()[0], 27)
        before = self.cash()
        due = dividend_tax.tax_due(self.store, AT)
        self.assertEqual(due['due_cents'], 5020)
        self.assertEqual(len(due['due']), 1)
        done = dividend_tax.reconcile_taxes(self.store, self.cfg, AT)
        self.assertEqual(done['tax_cents'], 5020)
        self.assertEqual(self.cash(), before - 5020)
        row = dividend_tax.tax_history(self.store)[0]
        self.assertEqual((row['tax_cents'], row['amount_cents']), (5020, -5020))
        self.assertEqual(row['posted_at'], normalize_time(AT))

    def test_partial_sales_fifo_and_post_record_purchase_are_distinct(self):
        self.fill('old', 'BUY', 100, '2026-08-01T10:00:00+08:00')
        self.fill('new', 'BUY', 100, '2026-09-22T10:00:00+08:00')
        self.credit(cash='1')
        self.fill('after-record', 'BUY', 100, '2026-09-28T10:00:00+08:00')
        self.fill('partial', 'SELL', 150, '2026-10-08T10:00:00+08:00')
        self.assertEqual(dividend_tax.reconcile_taxes(self.store, self.cfg, AT)['tax_cents'], 2000)
        self.fill('rest', 'SELL', 150, '2026-10-13T10:00:00+08:00')
        self.assertEqual(dividend_tax.reconcile_taxes(self.store, self.cfg, '2026-10-13T18:00:00+08:00')['tax_cents'], 1000)
        self.assertEqual(sum(r['qty'] for r in dividend_tax.tax_history(self.store)), 200)

    def test_record_date_intraday_round_trip_preserves_old_holding_period(self):
        self.fill('old', 'BUY', 100, '2025-09-01T10:00:00+08:00')
        self.fill('record-sell', 'SELL', 100, '2026-09-24T10:00:00+08:00')
        self.fill('record-buy', 'BUY', 100, '2026-09-24T14:00:00+08:00')
        self.credit(cash='1')
        self.fill('final', 'SELL', 100, '2026-10-08T10:00:00+08:00')
        self.assertEqual(dividend_tax.reconcile_taxes(self.store, self.cfg, AT)['tax_cents'], 0)

    def test_intraday_sale_does_not_post_before_later_same_day_buyback(self):
        self.fill('buy', 'BUY', 100, '2026-09-22T10:00:00+08:00')
        self.credit()
        self.fill('sell', 'SELL', 100, '2026-10-08T10:00:00+08:00')
        before = self.cash()
        self.assertEqual(dividend_tax.reconcile_taxes(self.store, self.cfg, '2026-10-08T11:00:00+08:00')['tax_cents'], 0)
        self.assertEqual(self.cash(), before)
        self.fill('buyback', 'BUY', 100, '2026-10-08T14:00:00+08:00')
        self.assertEqual(dividend_tax.reconcile_taxes(self.store, self.cfg, AT)['tax_cents'], 0)

    def test_same_day_net_sale_only_taxes_net_reduction(self):
        self.fill('buy', 'BUY', 200, '2026-09-22T10:00:00+08:00')
        self.credit(cash='1')
        self.fill('sell', 'SELL', 200, '2026-10-08T10:00:00+08:00')
        self.fill('buyback', 'BUY', 100, '2026-10-08T14:00:00+08:00')
        self.assertEqual(dividend_tax.reconcile_taxes(self.store, self.cfg, AT)['tax_cents'], 2000)

    def test_multiple_dividends_each_taxed_once_for_same_sale(self):
        self.fill('buy', 'BUY', 100, '2026-09-01T10:00:00+08:00')
        self.credit('2026-09-03', '2026-09-04', '1', '2026-09-04T18:00:00+08:00')
        self.credit(cash='2')
        self.fill('sell', 'SELL', 100, '2026-10-08T10:00:00+08:00')
        self.assertEqual(dividend_tax.reconcile_taxes(self.store, self.cfg, AT)['tax_cents'], 3000)
        self.assertEqual(dividend_tax.reconcile_taxes(self.store, self.cfg, AT)['tax_cents'], 0)
        refs = [r['reference'] for r in dividend_tax.tax_history(self.store)]
        self.assertEqual(len(refs), 2)
        self.assertEqual(len(set(refs)), 2)

    def test_late_dividend_credit_and_tax_commit_together(self):
        self.fill('buy', 'BUY', 100, '2026-09-22T10:00:00+08:00')
        self.fill('sell', 'SELL', 100, '2026-10-08T10:00:00+08:00')
        before = self.cash()
        self.credit(at=AT)
        self.assertEqual(self.cash(), before + 25100 - 5020)
        self.assertEqual(dividend_tax.reconcile_taxes(self.store, self.cfg, AT)['tax_cents'], 0)

    def test_late_dividend_tax_failure_rolls_back_dividend_as_well(self):
        self.fill('buy', 'BUY', 100, '2026-09-22T10:00:00+08:00')
        self.fill('sell', 'SELL', 100, '2026-10-08T10:00:00+08:00')
        before = self.cash()
        self.store.db.executescript("""CREATE TRIGGER fail_tax BEFORE INSERT ON paper_flows
            WHEN NEW.kind='DIVIDEND_TAX' BEGIN SELECT RAISE(ABORT,'injected tax failure'); END;""")
        with self.assertRaises(sqlite3.DatabaseError):
            self.credit(at=AT)
        self.assertEqual(self.cash(), before)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM paper_flows WHERE kind IN ('CASH_DIVIDEND','DIVIDEND_TAX')").fetchone()[0], 0)

    def test_cash_update_failure_rolls_back_new_tax_flow(self):
        self.standard()
        before = self.cash()
        self.store.db.executescript("""CREATE TRIGGER fail_cash BEFORE UPDATE ON paper_accounts
            BEGIN SELECT RAISE(ABORT,'injected cash failure'); END;""")
        with self.assertRaises(sqlite3.DatabaseError):
            dividend_tax.reconcile_taxes(self.store, self.cfg, AT)
        self.assertEqual(self.cash(), before)
        self.assertEqual(dividend_tax.tax_history(self.store), [])
        self.assertEqual(dividend_tax.tax_due(self.store, AT)['due_cents'], 5020)

    def test_outer_transaction_rollback_restores_cash_and_tax(self):
        self.standard()
        before = self.cash()
        self.store.db.execute('BEGIN IMMEDIATE')
        try:
            self.assertEqual(dividend_tax.reconcile_taxes_inside(self.store, self.cfg, AT)['tax_cents'], 5020)
        finally:
            self.store.db.rollback()
        self.assertEqual(self.cash(), before)
        self.assertEqual(dividend_tax.tax_history(self.store), [])

    def test_concurrent_retries_charge_once(self):
        self.standard()
        before = self.cash()
        barrier = threading.Barrier(2)
        def run():
            store = Store(self.tmp.name)
            try:
                barrier.wait(timeout=5)
                return dividend_tax.reconcile_taxes(store, self.cfg, AT)['tax_cents']
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: run(), range(2)))
        self.assertEqual(sorted(results), [0, 5020])
        self.assertEqual(self.cash(), before - 5020)
        self.assertEqual(len(dividend_tax.tax_history(self.store)), 1)

    def test_research_node_never_charges(self):
        self.standard()
        before = self.cash()
        result = dividend_tax.reconcile_taxes(self.store, {'deployment_role': 'research'}, AT)
        self.assertEqual(result['tax_cents'], 0)
        self.assertEqual(self.cash(), before)
        self.assertEqual(dividend_tax.tax_history(self.store), [])

    def test_cloud_requires_initialization_and_activation_but_not_fresh_research(self):
        from ashare.cloud_runtime import put
        self.standard()
        cloud = {**self.cfg, 'deployment_role': 'cloud'}
        before = self.cash()
        self.assertEqual(dividend_tax.reconcile_taxes(self.store, cloud, AT)['tax_cents'], 0)
        with self.store.db:
            put(self.store, 'initialized', True)
        self.assertEqual(dividend_tax.reconcile_taxes(self.store, cloud, AT)['tax_cents'], 0)
        self.assertEqual(self.cash(), before)
        with self.store.db:
            put(self.store, 'execution_enabled', True)
            put(self.store, 'research_completed_at', normalize_time('2026-09-01T10:00:00+08:00'))
            put(self.store, 'research_received_at', normalize_time('2026-09-01T10:00:00+08:00'))
        self.assertEqual(dividend_tax.reconcile_taxes(self.store, cloud, AT)['tax_cents'], 5020)
        self.assertEqual(self.cash(), before - 5020)

    def test_closed_day_cutoff_uses_shanghai_clock_for_utc_input(self):
        self.standard()
        self.assertEqual(dividend_tax.tax_due(self.store, '2026-10-08T06:59:59+00:00')['due_cents'], 0)
        self.assertEqual(dividend_tax.tax_due(self.store, '2026-10-08T07:00:00+00:00')['due_cents'], 5020)

    def test_posted_history_conflict_never_issues_silent_refund_or_second_debit(self):
        self.standard()
        dividend_tax.reconcile_taxes(self.store, self.cfg, AT)
        # A deliberately late synthetic fill changes the historical daily net.
        self.fill('late-buyback', 'BUY', 100, '2026-10-08T14:00:00+08:00')
        before = self.cash()
        with self.assertRaisesRegex(ValueError, '已记流水'):
            dividend_tax.reconcile_taxes(self.store, self.cfg, AT)
        self.assertEqual(self.cash(), before)
        self.assertEqual(self.store.db.execute("SELECT sum(amount_cents) FROM paper_flows WHERE kind='DIVIDEND_TAX'").fetchone()[0], -5020)

    def test_high_precision_dividend_tax_uses_original_per_share_price(self):
        self.fill('buy', 'BUY', 100, '2026-09-22T10:00:00+08:00')
        self.credit(cash='0.00025')  # gross credit truncates 2.5 cents to 2 cents.
        self.fill('sell', 'SELL', 100, '2026-10-08T10:00:00+08:00')
        # ChinaClear: original price * 20% * 100 = 0.5 cent, rounded to 1.
        self.assertEqual(dividend_tax.reconcile_taxes(self.store, self.cfg, AT)['tax_cents'], 1)

    def test_rounding_is_per_dividend_day_and_rate_not_per_fill(self):
        self.fill('buy', 'BUY', 400, '2026-09-22T10:00:00+08:00')
        self.credit(cash='0.00025')
        self.fill('sell-1a', 'SELL', 100, '2026-10-08T10:00:00+08:00')
        self.fill('sell-1b', 'SELL', 100, '2026-10-08T14:00:00+08:00')
        self.fill('sell-2', 'SELL', 100, '2026-10-09T10:00:00+08:00')
        self.fill('sell-3', 'SELL', 100, '2026-10-12T10:00:00+08:00')
        # Day 1: 0.5 + 0.5 cents rounds once to 1; each later day rounds 0.5 to 1.
        done = dividend_tax.reconcile_taxes(self.store, self.cfg, AT)
        self.assertEqual(done['tax_cents'], 3)
        self.assertEqual(sorted(r['tax_cents'] for r in dividend_tax.tax_history(self.store)), [1, 1, 1])

    def test_due_preview_is_read_only_and_ignores_future_sales(self):
        self.standard()
        before = self.cash()
        self.assertEqual(dividend_tax.tax_due(self.store, '2026-10-07T18:00:00+08:00')['due_cents'], 0)
        for _ in range(3):
            self.assertEqual(dividend_tax.tax_due(self.store, AT)['due_cents'], 5020)
        self.assertEqual(self.cash(), before)
        self.assertEqual(dividend_tax.tax_history(self.store), [])

    def test_mismatched_dividend_credit_blocks_deduction(self):
        self.standard()
        with self.store.db:
            self.store.db.execute("UPDATE paper_flows SET amount_cents=amount_cents+1 WHERE kind='CASH_DIVIDEND'")
        before = self.cash()
        with self.assertRaises(ValueError):
            dividend_tax.reconcile_taxes(self.store, self.cfg, AT)
        self.assertEqual(self.cash(), before)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM paper_flows WHERE kind='DIVIDEND_TAX'").fetchone()[0], 0)

    def test_second_tax_failure_rolls_back_entire_reconciliation_batch(self):
        self.fill('buy', 'BUY', 100, '2026-09-01T10:00:00+08:00')
        self.credit('2026-09-03', '2026-09-04', '1', '2026-09-04T18:00:00+08:00')
        self.credit(cash='2')
        self.fill('sell', 'SELL', 100, '2026-10-08T10:00:00+08:00')
        before = self.cash()
        self.store.db.executescript("""CREATE TRIGGER fail_second_tax BEFORE INSERT ON paper_flows
            WHEN NEW.kind='DIVIDEND_TAX' AND NEW.amount_cents=-2000
            BEGIN SELECT RAISE(ABORT,'injected second tax failure'); END;""")
        with self.assertRaises(sqlite3.DatabaseError):
            dividend_tax.reconcile_taxes(self.store, self.cfg, AT)
        self.assertEqual(self.cash(), before)
        self.assertEqual(dividend_tax.tax_history(self.store), [])

    def test_review_cash_and_total_profit_include_posting_once_without_backdating(self):
        self.standard()
        dividend_tax.reconcile_taxes(self.store, self.cfg, AT)
        end = normalize_time('2026-10-13T00:00:00+08:00')
        facts = review_portfolio.build(self.store, self.cfg, normalize_time('2026-09-01T00:00:00+08:00'), end, end)
        self.assertEqual(facts['closing']['cash_cents'], self.cash())
        self.assertEqual(facts['closing']['cumulative_dividend_cents'], 25100)
        self.assertEqual(facts['closing']['cumulative_dividend_tax_cents'], 5020)
        self.assertEqual(facts['closing']['cumulative_net_dividend_cents'], 20080)
        self.assertEqual(facts['closing']['equity_cents'] - 10000000, 20080)
        earlier = normalize_time('2026-10-09T00:00:00+08:00')
        old = review_portfolio.build(self.store, self.cfg, normalize_time('2026-09-01T00:00:00+08:00'), earlier, end)
        self.assertEqual(old['closing']['cumulative_dividend_tax_cents'], 0)
        self.assertEqual(old['closing']['equity_cents'] - 10000000, 25100)


if __name__ == '__main__':
    unittest.main()
