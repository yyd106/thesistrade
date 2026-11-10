"""R1 integration regression: real filled lots, live ALLOW, shared order budget.

Place under tests/ for the normal offline suite. No production configuration or
data is used; the suite's versioned example-config fixture is sufficient.
"""
import json
import unittest
from datetime import date, timedelta

import test_buy_preview as preview_fixtures
from ashare import calendar, exposure, paper, portfolio_strategy as ps
from ashare.cloud_runtime import put
from ashare.demo import SYMBOL, put_quote


class IntegratedRegimeBuyBudgetTests(unittest.TestCase):
    def prepare_existing_allow(self, first_target):
        t = preview_fixtures.BuyPreviewTests()
        t.setUp()
        self.addCleanup(t.tearDown)
        t.prepare(target=first_target)
        initial = t.slot()
        self.assertEqual(initial['operation_count'], 1, initial)
        t.at = t.later(60)
        put_quote(t.store, t.at)
        fills = paper.settle(t.store, t.cfg, t.at)
        self.assertEqual(len(fills), 1, fills)
        t.q = t.store.latest_quote(SYMBOL, t.at)
        t.approve({'watchlist:'+SYMBOL: {'action': 'ALLOW', 'target_bps': 2000}}, at=t.at)
        self.assertIsNotNone(ps.decision(t.store, t.cfg, 'watchlist', SYMBOL, t.at))
        # A real, completed descending index series activates R1=10 without
        # mocking gross_cap_pct, account values, or any execution gate.
        end = date.fromisoformat(calendar.last_completed_day(t.at))
        dates = []
        while len(dates) < 90:
            if calendar.trading_day(end):
                dates.append(end.isoformat())
            end -= timedelta(days=1)
        series = [{'date': d, 'close': 500-i} for i,d in enumerate(sorted(dates))]
        with t.store.db:
            t.store.db.execute("INSERT INTO runs VALUES('cross-r1','cross-r1','manual',?,NULL,NULL,'SUCCEEDED',NULL,NULL,NULL)", (t.at,))
            t.store.db.execute('INSERT INTO comparison_series VALUES(?,?,?,?)',
                               ('cross-r1','sh000300',t.at,json.dumps({'bars':series})))
            put(t.store, 'remote_features', ['exposure_caps_v1'])
        t.cfg['exposure_regime'] = 'R1'
        self.assertEqual(exposure.gross_cap_pct(t.store,t.cfg,t.at)[0], 10)
        self.assertEqual(ps.decision(t.store,t.cfg,'watchlist',SYMBOL,t.at)['action'], 'ALLOW')
        return t

    def test_r1_remaining_room_below_one_lot_blocks_preview_and_real_submit(self):
        t = self.prepare_existing_allow(1000)  # real fill: 900 shares at 10 yuan
        account = paper.account(t.store,t.at)
        remaining = account['equity_cents']//10-account['market_value_cents']-account['reserved_cents']
        minimum = 100*t.q['price_cents']+paper.fee(t.cfg,'BUY',100*t.q['price_cents'])
        self.assertGreater(remaining,0)
        self.assertLess(remaining,minimum)
        # Counterfactual verifies the cap, rather than some unrelated blocker,
        # is what prevents a second order.
        legacy = t.preview(cfg={**t.cfg,'exposure_regime':'OFF'})
        self.assertEqual(legacy['status'],'READY',legacy)
        before = t.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0]
        shown = t.preview()
        actual = paper.submit(t.store,t.cfg,'cross-blocked',t.p,'BUY',t.q,t.at)
        self.assertEqual(shown,{'status':'BLOCKED','reason':'INSUFFICIENT_BUDGET_OR_TARGET_REACHED'})
        self.assertEqual(actual['status'],'BLOCKED',actual)
        self.assertEqual(actual['reason'],shown['reason'])
        self.assertEqual(t.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],before)

    def test_r1_remaining_room_sizes_preview_and_real_submit_identically(self):
        t = self.prepare_existing_allow(900)  # real fill: 800 shares at 10 yuan
        account = paper.account(t.store,t.at)
        remaining = account['equity_cents']//10-account['market_value_cents']-account['reserved_cents']
        shown = t.preview()
        self.assertEqual(shown['status'],'READY',shown)
        self.assertEqual(shown['qty'],100)
        self.assertLessEqual(shown['reserved_cents'],remaining)
        legacy = t.preview(cfg={**t.cfg,'exposure_regime':'OFF'})
        self.assertGreater(legacy['qty'],shown['qty'])
        # A fresh slot creates the decision/FK and invokes the real submit path.
        slot = t.slot()
        self.assertEqual(slot['operation_count'],1,slot)
        actual = dict(t.store.db.execute("SELECT * FROM paper_orders WHERE status='OPEN'").fetchone())
        self.assertEqual(actual['status'],'OPEN',actual)
        for field in ('qty','limit_cents','reserved_cents'):
            self.assertEqual(shown[field],actual[field],field)


if __name__ == '__main__':
    unittest.main()
