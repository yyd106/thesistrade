"""Cross-feature contracts found in the independent D14/P06 review."""
import copy
import json
import sqlite3
import unittest

from ashare import cost_budget, exposure, global_paper, portfolio_risk, trader_view
from ashare.cloud_runtime import put
from ashare.storage import normalize_time
import test_d14_p06 as d14
import test_withdrawals as withdrawals
import test_trader_view as views
import test_portfolio_strategy as portfolios


class ReviewTests(unittest.TestCase):
    def withdrawal(self):
        t = withdrawals.WithdrawalTests(); t.setUp()
        self.addCleanup(t.doCleanups)
        with t.store.db: put(t.store, 'cost_budget_mode', 'P06')
        t.cash(16_000_000)
        return t

    def test_cost_poll_does_not_expire_withdrawal_but_real_cost_change_does(self):
        t = self.withdrawal(); preview = t.preview()
        before = portfolio_risk.state(t.store)
        self.assertEqual(t.preview(at=t.later(1))['approval']['id'], preview['approval']['id'])
        self.assertNotEqual(portfolio_risk.state(t.store)['cost_checked_at'], before['cost_checked_at'])
        self.assertEqual(t.execute(preview, t.later(1))['status'], 'COMPLETED')
        self.assertEqual(portfolio_risk.state(t.store)['cost_basis'], before['cost_basis'])

        t = self.withdrawal(); preview = t.preview()
        risk = portfolio_risk.state(t.store)
        risk.update(cost_basis='1.7', cost_locked=True, cost_cap_bps=500)
        with t.store.db:
            t.store.db.execute('UPDATE portfolio_risk SET payload_json=?', (json.dumps(risk),))
        self.assertEqual(t.execute(preview, t.later(1))['status'], 'STALE')
        self.assertEqual(t.store.db.execute('SELECT count(*) FROM withdrawal_transfers').fetchone()[0], 0)

    def test_empty_account_day_end_slot_persists_first_cost_lock(self):
        fixture = d14.CostTests(); t = fixture.fixture(); self.addCleanup(fixture.doCleanups)
        at = normalize_time('2026-11-11T15:05:00+08:00')
        with t.store.db: t.store.db.execute('UPDATE paper_accounts SET cash_cents=9090000')
        self.assertNotIn('cost_basis', portfolio_risk.state(t.store))
        self.assertEqual(global_paper.tick(t.store, t.cfg, at, fetch_quotes=False)['status'], 'SUCCEEDED')
        risk = portfolio_risk.state(t.store)
        self.assertTrue(risk['valuation_complete']); self.assertTrue(risk['cost_locked'])
        self.assertEqual(risk['cost_close_day'], '2026-11-11')
        self.assertIsNotNone(cost_budget.reduction_cap_pct(t.store, t.cfg, normalize_time('2026-11-12T09:30:00+08:00')))

    def test_all_page_routes_recompute_gate_without_writing_or_changing_plan(self):
        t = d14.D14Tests(); t.setUp(); self.addCleanup(t.doCleanups); t.bundle()
        plan = {'kind':'PAPER_TRADE', 'vol20_annual_pct':80, 'blockers':[]}
        state = {'watchlist':[{'plan':{'payload':copy.deepcopy(plan)}}],
                 'observation':{'items':[{'trade_plan':{'payload':copy.deepcopy(plan)}}]},
                 'dynamic':{'items':[{'plan':copy.deepcopy(plan)}]}}
        before = t.store.db.total_changes
        t.store.db.set_authorizer(lambda action,*args: sqlite3.SQLITE_OK if action in (
            sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE) else sqlite3.SQLITE_DENY)
        try: exposure.display_buy_gates(state, t.store, t.cfg, t.at)
        finally: t.store.db.set_authorizer(None)
        self.assertEqual(t.store.db.total_changes, before)
        items = state['watchlist']+state['observation']['items']+state['dynamic']['items']
        for item in items:
            self.assertEqual(item['current_buy_blocker'], 'BEAR_HIGH_VOLATILITY')
            self.assertIn('80.00%', item['current_buy_reason'])
        self.assertEqual(state['dynamic']['items'][0]['plan'], plan)
        t.bundle(90); exposure.display_buy_gates(state, t.store, t.cfg, t.at)
        self.assertTrue(all(item['current_buy_blocker'] is None for item in items))

    def test_held_view_never_suggests_rejected_addition_or_over_symbol_cap(self):
        item = views.item(plan=views.plan(), quote=views.quote(1550),
                          current_buy_blocker='BEAR_HIGH_VOLATILITY', current_buy_reason='波动80.00%，暂停新增买入。')
        state = views.status(watchlist=[item], portfolio={'holdings':[views.holding(weight=8)]},
                             portfolio_strategy=views.ps(views.decision(target=1500)), exposure={'symbol_cap_pct':10})
        row = views.build(state)['rows'][0]
        self.assertEqual(row['action'], 'HOLD'); self.assertFalse(row['zone_actionable'])
        self.assertIn('80.00%', row['reason']); self.assertNotIn('允许加仓', row['reason'])
        item.update(current_buy_blocker=None, current_buy_reason=None)
        row = views.build(state)['rows'][0]
        self.assertEqual(row['suggested_pct'], 10)
        state['portfolio']['holdings'][0]['weight_pct'] = 12
        self.assertFalse(views.build(state)['rows'][0]['zone_actionable'])

    def test_real_cost_reset_refreshes_research_without_poll_or_cap_churn(self):
        from ashare import auth, cloud_ledger, portfolio_strategy as ps
        t = portfolios.PortfolioStrategyTests(); t.setUp(); self.addCleanup(t.tearDown)
        t.at = normalize_time('2026-11-11T15:05:00+08:00')
        t.cfg.update(cost_budget='P06', deployment_role='research', scheduler_enabled=True)
        with t.store.db:
            put(t.store, 'remote_features', ['cost_budget_v1'])
            put(t.store, 'cost_budget_mode', 'P06')
            t.store.db.execute('UPDATE paper_accounts SET cash_cents=9090000')
        portfolio_risk.refresh(t.store, t.cfg, t.at)
        t.approve()
        version = cloud_ledger.version(t.store); guard = ps.snapshot(t.store, t.cfg, t.at)['guard']
        portfolio_risk.refresh(t.store, t.cfg, t.later(30))
        self.assertEqual(cloud_ledger.version(t.store), version)
        self.assertEqual(ps.snapshot(t.store, t.cfg, t.later(30))['guard'], guard)
        self.assertIsNone(ps.request(t.store, t.cfg, t.later(30)))
        risk = portfolio_risk.state(t.store)
        risk['cost_cap_bps'] -= 1
        with t.store.db: t.store.db.execute('UPDATE portfolio_risk SET payload_json=?', (json.dumps(risk),))
        self.assertEqual(cloud_ledger.version(t.store), version)
        self.assertEqual(ps.forced_reasons(t.store, t.cfg, t.later(31)), [])

        auth.setup(t.store, 'synthetic-admin-password', 'synthetic-guest-password')
        token, _ = auth.login(t.store, 'admin', 'synthetic-admin-password', 'synthetic', at=t.at)
        user = auth.session(t.store, auth.COOKIE+'='+token, at=t.at)
        cloud = {**t.cfg, 'deployment_role':'cloud'}
        approval = cost_budget.request(t.store, cloud, t.later(60))['approval']
        cost_budget.execute(t.store, cloud, approval['id'], user, approval['hash'], t.later(61))
        self.assertNotEqual(cloud_ledger.version(t.store), version)
        self.assertNotEqual(ps.snapshot(t.store, t.cfg, t.later(61))['guard'], guard)
        self.assertEqual(ps.forced_reasons(t.store, t.cfg, t.later(95)), ['COST_STATE_CHANGED'])
        self.assertTrue(ps.request(t.store, t.cfg, t.later(95)))
        before = ps.snapshot(t.store, {**t.cfg, 'cost_budget':None}, t.later(95))
        self.assertIsNone(before['cost_state'])

    def test_cost_intraday_tightening_waits_next_open_and_preserves_existing_episode(self):
        from ashare import paper
        from ashare.investment_policy import VERSION
        fixture = d14.ReductionProgressTests(); t = fixture.fixture(); self.addCleanup(fixture.doCleanups)
        t.cfg.update(exposure_regime=None, cost_budget='P06', investment_policy=VERSION)
        with t.store.db: put(t.store, 'remote_features', ['cost_budget_v1'])

        def mark(text, cash=None):
            at = normalize_time(text)
            with t.store.db:
                if cash is not None: t.store.db.execute('UPDATE paper_accounts SET cash_cents=?', (cash,))
                t.store.db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?)',
                    (at, 'sz000001', '合', 1000, 1000, at, at, 'SYN', ''))
            return at, portfolio_risk.refresh(t.store, t.cfg, at)

        close, risk = mark('2026-09-17T15:05:00+08:00')
        self.assertEqual(risk['cost_close_cap_bps'], 9000)
        intraday, risk = mark('2026-09-18T10:00:00+08:00', 3500000)
        self.assertEqual(risk['unit_nav'], '0.95')
        self.assertEqual(exposure.gross_cap_pct(t.store, t.cfg, intraday)[0], 52.63)
        self.assertEqual(cost_budget.reduction_cap_pct(t.store, t.cfg, intraday), 90)
        self.assertFalse(exposure.reduce_to_cap(t.store, t.cfg, intraday)['triggered'])
        self.assertEqual(t.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0], 0)
        close, risk = mark('2026-09-18T15:05:00+08:00')
        self.assertEqual(risk['cost_close_cap_bps'], 5263)
        before_open = normalize_time('2026-09-21T09:29:59+08:00')
        self.assertIsNone(cost_budget.reduction_cap_pct(t.store, t.cfg, before_open))
        opened, _ = mark('2026-09-21T09:30:00+08:00')
        order = exposure.reduce_to_cap(t.store, t.cfg, opened)
        self.assertEqual(order['cap_pct'], 52.63); self.assertEqual(len(order['orders']), 1)
        episode = exposure._episode(t.store)['id']
        later, risk = mark('2026-09-21T09:30:30+08:00', 3400000)
        self.assertEqual(risk['cost_cap_bps'], 4255)
        self.assertEqual(cost_budget.reduction_cap_pct(t.store, t.cfg, later), 52.63)
        fills = paper.settle(t.store, t.cfg, later)
        self.assertEqual(fills[0]['qty'], 600)
        exposure.reduce_to_cap(t.store, t.cfg, later)
        self.assertEqual(exposure._episode(t.store)['id'], episode)
        self.assertEqual(exposure._episode(t.store)['cap_pct'], 52.63)
        self.assertEqual(t.store.db.execute('SELECT status FROM paper_orders').fetchone()[0], 'PARTIAL')
        gap, risk = mark('2026-09-21T09:31:00+08:00', 3000000)
        self.assertTrue(risk['cost_floor_breached']); self.assertTrue(risk['cost_locked'])
        self.assertEqual(cost_budget.reduction_cap_pct(t.store, t.cfg, gap), 0)

    def test_locked_cap_ratchets_but_ordinary_reduction_still_waits_next_open(self):
        fixture = d14.CostTests(); t = fixture.fixture(); self.addCleanup(fixture.doCleanups)
        def refresh(text, cash):
            at = normalize_time(text)
            with t.store.db: t.store.db.execute('UPDATE paper_accounts SET cash_cents=?', (cash,))
            return at, portfolio_risk.refresh(t.store, t.cfg, at)
        _, first = refresh('2026-11-11T15:05:00+08:00', 9090000)
        self.assertTrue(first['cost_locked'])
        at, lower = refresh('2026-11-12T10:00:00+08:00', 9080000)
        self.assertLess(lower['cost_cap_bps'], first['cost_cap_bps'])
        self.assertEqual(cost_budget.reduction_cap_pct(t.store, t.cfg, at), first['cost_close_cap_bps']/100)
        _, rebound = refresh('2026-11-12T11:00:00+08:00', 9500000)
        self.assertTrue(rebound['cost_locked']); self.assertEqual(rebound['cost_cap_bps'], lower['cost_cap_bps'])
        _, close = refresh('2026-11-12T15:05:00+08:00', 9500000)
        self.assertEqual(close['cost_close_cap_bps'], lower['cost_cap_bps'])
        at, _ = refresh('2026-11-13T09:30:00+08:00', 9500000)
        self.assertEqual(cost_budget.reduction_cap_pct(t.store, t.cfg, at), lower['cost_cap_bps']/100)
