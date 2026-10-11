"""Cross-module cashflow invariants; all balances and credentials are synthetic."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from ashare import portfolio_risk, reporting, withdrawals
from ashare.finance import PaperLedger
from ashare.investment_policy import VERSION
from ashare.paper import account
from ashare.pipeline import load_config
from ashare.storage import Store, normalize_time
from approval_fixture import admin_session


class WithdrawalInvariantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cfg = load_config(Path(__file__).resolve().parents[1] / 'config.example.json', configure_model=False)
        self.cfg.update(data_dir=self.tmp.name, investment_policy=VERSION, portfolio_strategy=None,
                        watchlist=[], deployment_role='cloud', paper_slippage_bps=0)
        self.at = normalize_time('2026-09-23T10:00:00+08:00')
        PaperLedger(self.store).initialize()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def cash(self, amount, withdrawn=0):
        with self.store.db:
            self.store.db.execute('UPDATE paper_accounts SET cash_cents=?,withdrawn_cents=?', (amount, withdrawn))

    def cancel_units_and_cash(self, amount):
        """Exercise the accounting primitive in the same transaction as cash."""
        self.store.db.execute('BEGIN IMMEDIATE')
        try:
            portfolio_risk.adjust_withdrawal_inside(self.store, self.at, amount)
            self.store.db.execute('UPDATE paper_accounts SET cash_cents=cash_cents-?,withdrawn_cents=withdrawn_cents+?', (amount, amount))
            self.store.db.commit()
        except BaseException:
            self.store.db.rollback()
            raise

    def test_odd_cent_unit_cancellation_preserves_exact_nav_and_cumulative_profit(self):
        self.cash(15_000_001)
        before = portfolio_risk.refresh(self.store, self.cfg, self.at)
        profit = reporting.portfolio(self.store, self.cfg, account(self.store, self.at), self.at)['total_profit_cents']
        self.cancel_units_and_cash(3_000_001)
        after = portfolio_risk.refresh(self.store, self.cfg, self.at)
        self.assertEqual(Fraction(before['equity_cents']) / portfolio_risk.capital_units(before),
                         Fraction(after['equity_cents']) / portfolio_risk.capital_units(after))
        self.assertEqual(before['unit_nav'], after['unit_nav'])
        self.assertEqual(before['peak_unit_nav'], after['peak_unit_nav'])
        self.assertEqual(after['drawdown_bps'], 0)
        self.assertEqual(reporting.portfolio(self.store, self.cfg, account(self.store, self.at), self.at)['total_profit_cents'], profit)

    def test_unit_cancellation_does_not_clear_a_latched_drawdown(self):
        self.cash(20_000_000)
        portfolio_risk.refresh(self.store, self.cfg, self.at)
        self.cash(15_000_000)
        before = portfolio_risk.refresh(self.store, self.cfg, self.at)
        self.assertTrue(before['halted'])
        self.assertEqual(before['drawdown_bps'], 2500)
        self.cancel_units_and_cash(3_000_000)
        after = portfolio_risk.refresh(self.store, self.cfg, self.at)
        self.assertTrue(after['halted'])
        self.assertEqual(after['drawdown_bps'], 2500)
        self.assertEqual(after['triggered_at'], before['triggered_at'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM portfolio_risk_events').fetchone()[0], 1)

    def test_legacy_missing_cashflow_evidence_stays_insufficient_after_refresh_and_restart(self):
        self.cash(12_000_000, withdrawn=4_000_000)
        for _ in range(3):
            result = portfolio_risk.refresh(self.store, self.cfg, self.at)
            self.assertTrue(result['historical_cashflow_gap'])
            self.assertFalse(result['valuation_complete'])
            self.assertIsNone(result['drawdown_bps'])
        self.store.close()
        self.store = Store(self.tmp.name)
        result = portfolio_risk.refresh(self.store, self.cfg, self.at)
        self.assertTrue(result['historical_cashflow_gap'])
        with self.assertRaisesRegex(ValueError, '历史提取'):
            self.cancel_units_and_cash(1)
        self.assertEqual(account(self.store, self.at)['cash_cents'], 12_000_000)

    def test_rollback_preserves_units_and_cash_as_one_atomic_state(self):
        self.cash(16_000_000)
        before = portfolio_risk.refresh(self.store, self.cfg, self.at)
        observer = Store(self.tmp.name)
        try:
            self.store.db.execute('BEGIN IMMEDIATE')
            portfolio_risk.adjust_withdrawal_inside(self.store, self.at, 4_000_000)
            self.store.db.execute('UPDATE paper_accounts SET cash_cents=12000000,withdrawn_cents=4000000')
            self.assertEqual(observer.db.execute('SELECT cash_cents FROM paper_accounts').fetchone()[0], 16_000_000)
            self.assertEqual(portfolio_risk.state(observer), before)
            self.store.db.rollback()
            self.assertEqual(observer.db.execute('SELECT cash_cents FROM paper_accounts').fetchone()[0], 16_000_000)
            self.assertEqual(portfolio_risk.state(self.store), before)
        finally:
            self.store.db.rollback()
            observer.close()

    def test_authenticated_transfer_and_second_connection_retry_preserve_profit_and_nav(self):
        self.cash(15_000_001)
        user = admin_session(self.store, self.at)
        request = withdrawals.request(self.store, self.cfg, self.at)
        self.assertEqual(request['status'], 'PREVIEW_READY', request)
        before = portfolio_risk.state(self.store)
        result = withdrawals.execute(self.store, self.cfg, request['approval']['id'], user,
                                     request['approval']['hash'], self.at)
        self.assertEqual(result['status'], 'COMPLETED', result)
        self.assertEqual(result['amount_cents'], 3_000_001)
        self.assertEqual(result['withdrawal']['current_stage'], 2)
        after = portfolio_risk.refresh(self.store, self.cfg, self.at)
        self.assertEqual(before['unit_nav'], after['unit_nav'])
        self.assertEqual(before['peak_unit_nav'], after['peak_unit_nav'])
        self.assertEqual(reporting.portfolio(self.store, self.cfg, account(self.store, self.at), self.at)['total_profit_cents'], 5_000_001)
        other = Store(self.tmp.name)
        try:
            retried = withdrawals.execute(other, self.cfg, request['approval']['id'], user,
                                          request['approval']['hash'], self.at)
            self.assertEqual(retried, result)
            self.assertEqual(other.db.execute('SELECT count(*) FROM withdrawal_transfers').fetchone()[0], 1)
            self.assertEqual(other.db.execute("SELECT sum(amount_cents) FROM paper_flows WHERE kind='SIMULATED_WITHDRAWAL'").fetchone()[0], -3_000_001)
            self.assertEqual(other.db.execute('SELECT current_stage FROM paper_accounts').fetchone()[0], 2)
        finally:
            other.close()

    def test_preflight_then_other_connection_crosses_threshold_blocks_each_buy_in_transaction(self):
        from ashare import paper, dynamic_paper, global_paper
        routes = (
            lambda: paper.submit(self.store, self.cfg, 'synthetic-decision', {'id': 'synthetic-plan', 'symbol': 'sh600000'}, 'BUY', {}, self.at),
            lambda: dynamic_paper.submit(self.store, self.cfg, {'id': 'synthetic-case', 'symbol': 'sh600000'}, 'BUY', {}, self.at, '合成买入'),
            lambda: global_paper.submit(self.store, self.cfg, 'BTC', 'BUY', {}, {'id': 'synthetic-global'}, {'asset': 'BTC', 'links': []}, self.at),
        )
        self.cfg.update(deployment_role='standalone', dynamic_enabled=True)
        real_refresh = withdrawals.refresh
        for route in routes:
            with self.subTest(route=route):
                # Use a fresh account for each independent race.
                self.store.close()
                self.tmp.cleanup()
                self.tmp = tempfile.TemporaryDirectory()
                self.store = Store(self.tmp.name)
                self.cfg['data_dir'] = self.tmp.name
                PaperLedger(self.store).initialize()
                self.cash(14_000_000)
                other = Store(self.tmp.name)
                def cross_after_preflight(store, config, at):
                    result = real_refresh(store, config, at)
                    self.assertFalse(result['pending'])
                    with other.db:
                        other.db.execute('UPDATE paper_accounts SET cash_cents=16000000')
                    return result
                try:
                    with patch('ashare.withdrawals.refresh', side_effect=cross_after_preflight):
                        result = route()
                    self.assertEqual(result['status'], 'BLOCKED', result)
                    self.assertEqual(result['reason'], 'WITHDRAWAL_PENDING', result)
                    self.assertTrue(withdrawals.pending(self.store))
                    self.assertEqual(withdrawals.reservation(self.store), 4_000_000)
                    self.assertEqual(other.db.execute('SELECT count(*) FROM withdrawal_cycles').fetchone()[0], 1)
                    for table in ('paper_orders', 'dynamic_orders', 'global_orders'):
                        self.assertEqual(other.db.execute('SELECT count(*) FROM '+table).fetchone()[0], 0)
                finally:
                    other.close()

    def test_trigger_cancels_unfilled_buys_on_all_routes_but_preserves_unknown_and_sells(self):
        """Lifecycle fixtures retain partial-fill counters and unknown reservations."""
        expiry = normalize_time('2026-09-24T10:00:00+08:00')
        db = self.store.db
        with db:
            db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)', ('snapshot', None, 'sh600000', self.at, self.at, 'synthetic', '{}'))
            db.execute('INSERT INTO studies VALUES(?,?,?,?,?,?)', ('study', 'snapshot', 'sh600000', self.at, 'SUCCEEDED', '{}'))
            db.execute('INSERT INTO plans VALUES(?,?,?,?,?,?,?,?)', ('plan', 'study', 'sh600000', self.at, expiry, 'ACTIVE', 'synthetic', '{"kind":"PAPER_TRADE"}'))
            db.execute('INSERT INTO dynamic_news VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('news', 'synthetic', 'https://example.invalid/news', self.at, self.at, '合成标题', '合成内容', 'cluster', None, 'DONE', 1, self.at, ''))
            db.execute('INSERT INTO dynamic_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       ('case', 'news', 'sh600000', '合成标的', '合成主题', 'EVENT', 'LONG', self.at, expiry, 'LIVE', 'READY', '{}', '{}'))
            for index, (state, side) in enumerate((('OPEN', 'BUY'), ('PARTIAL', 'BUY'), ('UNKNOWN', 'BUY'), ('OPEN', 'SELL'))):
                name = str(index)
                filled = 20 if state == 'PARTIAL' else 0
                stamp = normalize_time((datetime.fromisoformat(self.at)+timedelta(minutes=index)).isoformat())
                db.execute('INSERT INTO slots VALUES(?,?,?,?,?,?,?)', ('slot'+name, stamp, stamp, stamp, 'DONE', '{}', 'SUCCEEDED'))
                db.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?)', ('decision'+name, 'slot'+name, 'sh600000', 'plan', stamp, side, 'SUBMITTED', '合成委托', '{}'))
                db.execute('INSERT INTO paper_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           ('paper'+name, 'decision'+name, 'plan', 'sh600000', side, 100, filled, 1000, 10000, self.at, expiry, state))
                db.execute('INSERT INTO dynamic_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           ('dynamic'+name, 'dynamic-intent'+name, 'case', 'sh600000', side, 100, filled, 1000, 10000, self.at, expiry, state, '合成委托', '{}'))
                db.execute('INSERT INTO global_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           ('global'+name, 'global-intent'+name, None, 'BTC', side, 100, filled, 100000000, 10000, self.at, expiry, state, '{}'))
        self.cash(16_000_000)
        result = withdrawals.refresh(self.store, self.cfg, self.at)
        self.assertEqual(result['status'], 'REVIEW_REQUIRED', result)
        self.assertIn('UNKNOWN_ORDERS', [b['code'] for b in result['blockers']])
        self.assertTrue(result['pending'])
        self.assertFalse(result['can_cancel'])
        self.assertEqual(result['reserved_cents'], 4_000_000)
        for table in ('paper_orders', 'dynamic_orders', 'global_orders'):
            rows = list(db.execute('SELECT status,reserved_cents,filled_qty FROM '+table+' ORDER BY id'))
            self.assertEqual([tuple(row) for row in rows], [
                ('CANCELLED', 0, 0), ('CANCELLED', 0, 20), ('UNKNOWN', 10000, 0), ('OPEN', 10000, 0)])
        self.assertEqual(account(self.store, self.at)['available_cents'], 11_970_000)

    def global_holding(self, symbol, value):
        from ashare.global_market import SCALE
        from ashare import portfolio_strategy as ps
        expiry = normalize_time('2026-09-23T22:00:00+08:00')
        qty = value*SCALE//1000  # Synthetic USD 10 quote and USD/CNY 1.
        with self.store.db:
            self.store.db.execute('INSERT INTO global_quotes VALUES(?,?,?,?,?,?,?,?)',
                                  ('q'+symbol, symbol, self.at, self.at, 10000000, 1000000, self.at, '{}'))
            self.store.db.execute('INSERT INTO global_plans VALUES(?,?,?,?,?,?,?)',
                                  ('p'+symbol, symbol, self.at, expiry, 'synthetic', 'ACTIVE', '{"kind":"PAPER_TRADE"}'))
            self.store.db.execute('INSERT INTO global_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                  ('o'+symbol, 'i'+symbol, 'p'+symbol, symbol, 'BUY', qty, qty, 10000000, 0, self.at, expiry, 'FILLED', '{}'))
            self.store.db.execute('INSERT INTO global_fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                  ('f'+symbol, 'o'+symbol, 'q'+symbol, symbol, 'BUY', qty, 10000000, 1000000, value, 0, 0, self.at, self.at))
            self.store.db.execute('INSERT INTO global_lots VALUES(?,?,?,?,?)', ('f'+symbol, symbol, qty, value, self.at))
        return {'key': 'global:'+symbol, 'route': 'global', 'identity': symbol, 'symbol': symbol, 'action': 'ALLOW',
                'target_bps': 500, 'related_keys': [], 'evidence_ids': [], 'reference_id': 'p'+symbol,
                'source_token': ps.row_token(ps.source(self.store, 'global', symbol))}

    def portfolio_authorization(self, decisions, group_bps):
        from ashare import portfolio_strategy as ps
        self.cfg['portfolio_strategy'] = ps.VERSION
        payload = {'decisions': decisions, 'risk_groups': [{'keys': [d['key'] for d in decisions], 'max_bps': group_bps}]}
        with self.store.db:
            self.store.db.execute('INSERT INTO portfolio_decisions VALUES(?,?,?,?,?,?)',
                                  ('portfolio', self.at, normalize_time('2026-09-23T22:00:00+08:00'), 'ACTIVE', ps.VERSION, json.dumps(payload)))

    def test_post_withdrawal_denominator_rechecks_existing_research_risk_group_budget(self):
        decisions = [self.global_holding(symbol, 400_000) for symbol in ('BTC', 'ETH')]
        self.cash(15_200_000)  # Equity 160k; exposure 8k fits 6%, but exceeds 6% of retained 120k.
        self.portfolio_authorization(decisions, 600)
        result = withdrawals.refresh(self.store, self.cfg, self.at)
        self.assertEqual(result['status'], 'WAITING', result)
        self.assertEqual([b['code'] for b in result['blockers']], ['PORTFOLIO_LIMIT'])
        self.assertFalse(result['can_transfer'])
        self.assertEqual(self.store.db.execute('SELECT withdrawn_cents FROM paper_accounts').fetchone()[0], 0)

    def test_changed_research_dependency_invalidates_a_previously_ready_withdrawal(self):
        decisions = [self.global_holding(symbol, 400_000) for symbol in ('BTC', 'ETH')]
        self.cash(15_200_000)
        self.portfolio_authorization(decisions, 1000)
        user = admin_session(self.store, self.at)
        request = withdrawals.request(self.store, self.cfg, self.at)
        self.assertEqual(request['status'], 'PREVIEW_READY', request)
        with self.store.db:
            self.store.db.execute("UPDATE global_plans SET status='SUPERSEDED' WHERE id='pETH'")
        result = withdrawals.execute(self.store, self.cfg, request['approval']['id'], user,
                                     request['approval']['hash'], self.at)
        self.assertEqual(result['status'], 'STALE', result)
        self.assertIn('PORTFOLIO_UNAVAILABLE', [b['code'] for b in result['withdrawal']['blockers']])
        self.assertEqual(self.store.db.execute('SELECT withdrawn_cents FROM paper_accounts').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM approval_consumptions').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
