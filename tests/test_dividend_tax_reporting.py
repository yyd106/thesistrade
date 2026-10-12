"""Synthetic reporting checks for posted dividend tax; no production inputs."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ashare import digest, page_display, paper, reporting, review_portfolio, review_presentation
from ashare.settings import DEFAULTS
from ashare.storage import Store

INITIAL = '2026-09-01T00:00:00+00:00'
START = '2026-10-12T00:00:00+00:00'
POSTED = '2026-10-12T08:00:00+00:00'
END = '2026-10-13T00:00:00+00:00'


class DividendTaxReportingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.cfg = {**DEFAULTS, **json.loads((Path(__file__).resolve().parents[1] / 'config.example.json').read_text()),
                    'data_dir': self.tmp.name, 'watchlist': [], 'model_enabled': False, 'scheduler_enabled': False}
        with self.store.db:
            self.store.db.execute('INSERT INTO paper_accounts VALUES(?,?,?,1,0)', ('DEMO_PAPER', 10020080, 10000000))
        self.flow('initial', 'SIMULATED_INITIAL', 10000000, INITIAL)
        self.flow('gross', 'CASH_DIVIDEND', 25100, '2026-09-30T08:00:00+00:00')
        self.flow('tax', 'DIVIDEND_TAX', -5020, POSTED)

    def flow(self, identity, kind, amount, at, account='DEMO_PAPER'):
        with self.store.db:
            self.store.db.execute('INSERT OR IGNORE INTO paper_accounts VALUES(?,?,?,1,0)', (account, 10000000, 10000000))
            self.store.db.execute('INSERT INTO paper_flows VALUES(?,?,?,?,?,?)', (identity, account, kind, amount, identity, at))

    def frozen(self, end=END, equity=10028001):
        return {'window_start': START, 'window_end': end, 'dividends': [],
                'totals': {'period_profit_cents': 0, 'cumulative_profit_cents': 7921},
                'opening': {'equity_cents': 10033021},
                'closing': {'equity_cents': equity, 'unrealized_cents': 34100,
                            'cumulative_realized_cents': -26179}, 'positions': []}

    def test_current_account_keeps_gross_tax_and_net_separate_without_double_charge(self):
        account = paper.account(self.store, END)
        view = reporting.portfolio(self.store, self.cfg, account, END)
        self.assertEqual(view['total_profit_cents'], 20080)
        self.assertEqual(view['cumulative_dividend_cents'], 25100)
        self.assertEqual(view['cumulative_dividend_tax_cents'], 5020)
        self.assertEqual(view['cumulative_net_dividend_cents'], 20080)
        self.assertEqual(view['cumulative_realized_cents'], 0)

    def test_frozen_reconciliation_assigns_tax_to_posted_day_and_is_read_only(self):
        frozen = self.frozen()
        before = copy.deepcopy(frozen)
        changes = self.store.db.total_changes
        view = review_presentation.accounting(frozen, self.store, END)
        self.assertEqual(view['cumulative_dividend_tax_cents'], 5020)
        self.assertEqual(view['cumulative_net_dividend_cents'], 20080)
        self.assertEqual(view['account_cumulative_profit_cents'], 28001)
        self.assertEqual(view['reconciliation_difference_cents'], 0)
        self.assertEqual(view['period_dividend_tax_cents'], 5020)
        self.assertEqual(view['account_period_profit_cents'], -5020)
        self.assertEqual(frozen, before)
        self.assertEqual(self.store.db.total_changes, changes)

    def test_old_cutoff_does_not_receive_future_catchup_and_excludes_other_account(self):
        self.flow('other-tax', 'DIVIDEND_TAX', -9900, INITIAL, 'OTHER')
        view = review_presentation.accounting(self.frozen(POSTED, 10033021), self.store, END)
        self.assertEqual(view['cumulative_dividend_tax_cents'], 0)
        self.assertEqual(view['cumulative_net_dividend_cents'], 25100)
        self.assertEqual(view['reconciliation_difference_cents'], 0)
        self.assertEqual(view['account_period_profit_cents'], 0)

    def test_true_unexplained_difference_is_not_hidden_as_tax(self):
        view = review_presentation.accounting(self.frozen(equity=10028101), self.store, END)
        self.assertEqual(view['reconciliation_difference_cents'], 100)

    def test_incomplete_replica_keeps_signed_tax_values_and_unknowns(self):
        with self.store.db:
            self.store.db.execute('DELETE FROM paper_flows')
        frozen = self.frozen()
        frozen.update(cumulative_dividend_cents=25100, cumulative_dividend_tax_cents=5020,
                      cumulative_net_dividend_cents=20080, account_cumulative_profit_cents=28001,
                      reconciliation_difference_cents=0, period_dividend_tax_cents=5020,
                      account_period_profit_cents=-5020, dividend_accounting_version='CASH_DIVIDEND_TAX_V1')
        view = review_presentation.accounting(frozen, self.store, END)
        self.assertEqual(view['cumulative_dividend_tax_cents'], 5020)
        self.assertEqual(view['account_cumulative_profit_cents'], 28001)
        self.assertEqual(view['reconciliation_difference_cents'], 0)
        unknown = review_presentation.accounting(self.frozen(), self.store, END)
        self.assertIsNone(unknown['cumulative_dividend_tax_cents'])
        self.assertIsNone(unknown['cumulative_net_dividend_cents'])
        self.assertNotIn('period_dividend_tax_cents', unknown)

    def test_review_reconstruction_cash_and_account_period_use_posted_tax_once(self):
        tax = {'symbol': 'sz000001', 'qty': 100, 'record_date': '2026-09-20', 'ex_date': '2026-09-21',
               'sold_at': '2026-09-25T07:00:00+00:00', 'posted_at': POSTED, 'tax_cents': 5020}
        with patch('ashare.dividend_tax.tax_history', return_value=[tax]):
            facts = review_portfolio.build(self.store, self.cfg, START, END, END)
        self.assertEqual(facts['opening']['cash_cents'], 10025100)
        self.assertEqual(facts['closing']['cash_cents'], 10020080)
        self.assertEqual(facts['totals']['period_profit_cents'], 0)
        self.assertEqual(facts['totals']['cumulative_profit_cents'], 0)
        self.assertEqual(facts['account_period_profit_cents'], -5020)
        self.assertEqual(facts['account_cumulative_profit_cents'], 20080)
        self.assertEqual(facts['dividend_taxes'], [tax])
        self.assertEqual(review_portfolio.model_view(facts)['period_dividend_tax_cents'], 5020)
        self.assertEqual(review_presentation.accounting(facts, self.store, END)['reconciliation_difference_cents'], 0)

    def test_digest_attributes_actual_posting_and_does_not_add_tax_back_to_profit(self):
        for identity, at, equity in [('before', '2026-10-11T23:00:00+00:00', 10025100), ('after', POSTED, 10020080)]:
            with self.store.db:
                self.store.db.execute('INSERT INTO equity_marks VALUES(?,?,?,?,?,?,?,?)',
                    (identity, at, at, equity, equity, 0, 1, '{}'))
        view = digest.execution(self.store, self.cfg, START, END)
        self.assertEqual(view['dividend_tax_cents'], 5020)
        self.assertEqual(view['net_dividend_cents'], -5020)
        self.assertEqual(view['cashflow_adjusted_change_cents'], -5020)
        earlier = digest.execution(self.store, self.cfg, INITIAL, POSTED)
        self.assertEqual(earlier['dividend_tax_cents'], 0)
        self.flow('legacy-gross', 'CASH_DIVIDEND', 2000, POSTED)
        self.flow('other-gross', 'CASH_DIVIDEND', 9000, POSTED, 'OTHER')
        self.flow('other-tax', 'DIVIDEND_TAX', -1800, POSTED, 'OTHER')
        # A legacy reference may lack stock details, but the actual cashflow
        # remains in the account net amount. Other accounts never leak in.
        revised = digest.execution(self.store, self.cfg, START, END)
        self.assertEqual(revised['net_dividend_cents'], -3020)
        self.assertEqual(revised['dividend_tax_cents'], 5020)

    def test_public_page_keeps_new_tax_fields_and_does_not_publish_private_references(self):
        fields = {'dividend_accounting_version': 'CASH_DIVIDEND_TAX_V1',
                  'period_dividend_tax_cents': 5020, 'cumulative_dividend_tax_cents': 5020,
                  'period_net_dividend_cents': -5020, 'cumulative_net_dividend_cents': 20080,
                  'account_period_profit_cents': -5020,
                  'dividend_taxes': [{'symbol': 'sz000001', 'tax_cents': 5020, 'posted_at': POSTED,
                                      'sold_at': INITIAL, 'raw_path': 'PRIVATE'}]}
        result = page_display.bounded(fields, [0])
        self.assertEqual(result['cumulative_dividend_tax_cents'], 5020)
        self.assertEqual(result['account_period_profit_cents'], -5020)
        self.assertEqual(result['dividend_taxes'][0]['posted_at'], POSTED)
        self.assertNotIn('PRIVATE', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
