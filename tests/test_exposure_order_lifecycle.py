"""Real ledger units, fills and rollback boundaries for exposure episodes."""
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from ashare import exposure, cloud_runtime, paper, global_paper, dynamic_paper
from ashare.calendar import last_completed_day
from ashare.global_market import SCALE, notional, quantity_for_notional
from ashare.storage import normalize_time
import test_market_regime as fixtures


class ExposureOrderLifecycleTests(unittest.TestCase):
    def fixture(self, global_assets=False):
        t = fixtures.GlobalSessionReduceTests() if global_assets else fixtures.ReduceToCapTests()
        t.setUp()
        self.addCleanup(t.tearDown)
        return t

    def capital(self, t, cash):
        with t.store.db:
            t.store.db.execute('UPDATE paper_accounts SET cash_cents=?', (cash,))
            t.store.db.execute('UPDATE paper_flows SET amount_cents=10000000')

    def bundle(self, t, cap=10, status='OK', day=None):
        return {'mode': 'R1', 'regime': {'method': 'R1-v1', 'status': status,
                'cap_pct': cap, 'raw_cap_pct': cap, 'confirmed_cap_pct': cap,
                'as_of_day': day or last_completed_day(t.at), 'series_sha256': 'a' * 64}}

    def cloud(self, t):
        t.cfg['deployment_role'] = 'cloud'
        with t.store.db:
            cloud_runtime.put(t.store, 'initialized', True)
            cloud_runtime.put(t.store, 'execution_enabled', True)
            cloud_runtime.put(t.store, 'exposure_caps', self.bundle(t))

    def fresh(self, t, seconds=60):
        at = normalize_time((datetime.fromisoformat(t.at) + timedelta(seconds=seconds)).isoformat())
        with t.store.db:
            t.store.db.execute('UPDATE quotes SET observed_at=?,first_seen_at=?', (at, at))
            t.store.db.execute('UPDATE dynamic_quotes SET observed_at=?,first_seen_at=?', (at, at))
            t.store.db.execute('UPDATE global_quotes SET observed_at=?,first_seen_at=?,fx_at=?', (at, at, at))
        return at

    def watchlist_order(self):
        t = self.fixture()
        t.hold('sz000001', 2000, price=1000)
        self.capital(t, 8000000)
        self.cloud(t)
        result = exposure.reduce_to_cap(t.store, t.cfg, t.at)
        self.assertEqual(result['orders'][0]['qty'], 1000)
        return t, result['orders'][0]['order_id']

    def global_order(self, symbol='US:AAPL'):
        t = self.fixture(True)
        t.at = normalize_time(t.DAY + 'T22:00:00+08:00')
        ts = int(datetime.fromisoformat(t.at).timestamp())
        t.cloud_ready()
        self.cloud(t)
        t.hold('sz000001', 2000, price=1000)
        t.ghold(symbol, 100*SCALE, 7000000, 100000000, 7000000, t.at,
                {'start': ts-1800, 'end': ts+1800})
        self.capital(t, 1000000)
        result = exposure.reduce_to_cap(t.store, t.cfg, t.at)
        self.assertEqual(len(result['orders']), 1)
        return t, result['orders'][0]['order_id']

    def dynamic_order(self):
        t = self.fixture()
        t.cfg['dynamic_enabled'] = True
        t.store.db.execute('PRAGMA foreign_keys=OFF')
        plan = {'holding_days': 30, 'stop_bps': 600, 'take_profit_bps': 1000}
        with t.store.db:
            t.store.db.execute('INSERT INTO dynamic_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('case', 'news', 'sz000001', '合', 'theme', 'APPROVAL', 'BULLISH', t.at,
                 '2026-12-31T00:00:00+00:00', 'FORWARD', 'HOLDING', '{}', json.dumps(plan)))
            t.store.db.execute('INSERT INTO dynamic_lots VALUES(?,?,?,?,?,?)',
                ('lot', 'case', 'sz000001', 2000, 2000000, '2026-09-16'))
            t.store.db.execute('INSERT INTO dynamic_quotes VALUES(?,?,?,?,?,?,?,?)',
                ('quote', 'sz000001', '合', 1000, 1000, t.at, t.at, 'synthetic'))
        self.capital(t, 8000000)
        self.cloud(t)
        result = exposure.reduce_to_cap(t.store, t.cfg, t.at)
        self.assertEqual(result['orders'][0]['qty'], 1000)
        return t, result['orders'][0]['order_id']

    def test_global_real_shares_have_bounded_cny_quota_and_can_fill(self):
        t, oid = self.global_order()
        order = t.store.db.execute('SELECT * FROM global_orders WHERE id=?', (oid,)).fetchone()
        self.assertEqual(order['qty'], 88*SCALE)
        ep = cloud_runtime.value(t.store, 'exposure_reduce_episode')
        self.assertLessEqual(notional(order['qty'], 100000000, 7000000), ep['quotas']['global:US:AAPL'])
        self.assertTrue(any(o['order_id'] == oid for o in ep['orders']))
        at = self.fresh(t)
        self.assertEqual(len(global_paper.settle(t.store, t.cfg, at, {})), 1)
        self.assertEqual(global_paper.balance(t.store, at)['positions']['US:AAPL']['qty'], 93*SCALE)
        # Actual partial fill then cancelled remainder releases precisely the remainder.
        with t.store.db:
            t.store.db.execute("UPDATE global_orders SET status='CANCELLED' WHERE id=?", (oid,))
        at = self.fresh(t, 120)
        result = exposure.reduce_to_cap(t.store, t.cfg, at)
        self.assertEqual(result['orders'][0]['qty'], 81*SCALE)

    def test_spot_quantity_uses_fractional_step_without_exceeding_budget(self):
        qty = quantity_for_notional('BTC', 1234567, 50000*1000000, 7000000)
        self.assertEqual(qty % 10000, 0)
        self.assertGreater(qty, 0)
        self.assertLessEqual(notional(qty, 50000*1000000, 7000000), 1234567)

    def test_settle_before_reduce_cancels_off_rise_and_degraded_orders(self):
        for change in ('OFF', 'RISE', 'MISSING', 'STALE'):
            with self.subTest(change=change):
                t, oid = self.watchlist_order()
                bundle = ({'mode': 'OFF', 'regime': None} if change == 'OFF' else
                          self.bundle(t, 90 if change == 'RISE' else 10,
                                      status=change if change in ('MISSING', 'STALE') else 'OK'))
                with t.store.db:
                    cloud_runtime.put(t.store, 'exposure_caps', bundle)
                self.assertEqual(paper.settle(t.store, t.cfg, self.fresh(t)), [])
                self.assertEqual(t.store.db.execute('SELECT status FROM paper_orders WHERE id=?', (oid,)).fetchone()[0], 'CANCELLED')
                self.assertEqual(cloud_runtime.value(t.store, 'exposure_reduce_episode')['state'], 'SUPERSEDED')

    def test_stale_by_cloud_clock_cancels_without_incoming_publication(self):
        t, oid = self.watchlist_order()
        with t.store.db:
            cloud_runtime.put(t.store, 'exposure_caps', self.bundle(t, day='2026-09-15'))
        self.assertEqual(paper.settle(t.store, t.cfg, self.fresh(t)), [])
        self.assertEqual(t.store.db.execute('SELECT status FROM paper_orders WHERE id=?', (oid,)).fetchone()[0], 'CANCELLED')

    def test_all_three_routes_cancel_their_partial_remainder(self):
        for route, maker, settle in (
                ('watchlist', self.watchlist_order, lambda t, at: paper.settle(t.store, t.cfg, at)),
                ('dynamic', self.dynamic_order, lambda t, at: dynamic_paper.settle(t.store, t.cfg, at)),
                ('global', self.global_order, lambda t, at: global_paper.settle(t.store, t.cfg, at, {}))):
            with self.subTest(route=route):
                t, oid = maker()
                self.assertTrue(settle(t, self.fresh(t)))
                table = {'watchlist': 'paper_orders', 'dynamic': 'dynamic_orders', 'global': 'global_orders'}[route]
                before = dict(t.store.db.execute('SELECT * FROM ' + table + ' WHERE id=?', (oid,)).fetchone())
                self.assertEqual(before['status'], 'PARTIAL')
                with t.store.db:
                    cloud_runtime.put(t.store, 'exposure_caps', {'mode': 'OFF', 'regime': None})
                self.assertEqual(settle(t, self.fresh(t, 120)), [])
                after = dict(t.store.db.execute('SELECT * FROM ' + table + ' WHERE id=?', (oid,)).fetchone())
                self.assertEqual(after['status'], 'CANCELLED')
                self.assertEqual(after['filled_qty'], before['filled_qty'])

    def test_ending_episode_does_not_cancel_an_ordinary_sell_or_unknown(self):
        t, oid = self.watchlist_order()
        t.hold('sz000002', 1000, price=1000)
        q = t.store.latest_quote('sz000002', t.at)
        plan = dict(t.store.db.execute("SELECT * FROM plans WHERE symbol='sz000002'").fetchone())
        ordinary = paper.submit(t.store, t.cfg, 'ordinary-sell', plan, 'SELL', q, t.at, sell_qty=100)
        self.assertIn('id', ordinary)
        with t.store.db:
            cloud_runtime.put(t.store, 'exposure_caps', {'mode': 'OFF', 'regime': None})
        exposure.reduce_to_cap(t.store, t.cfg, t.at)
        fills = paper.settle(t.store, t.cfg, self.fresh(t))
        self.assertEqual([f['order_id'] for f in fills], [ordinary['id']])
        self.assertEqual(t.store.db.execute('SELECT status FROM paper_orders WHERE id=?', (oid,)).fetchone()[0], 'CANCELLED')
        u, unknown = self.watchlist_order()
        with u.store.db:
            u.store.db.execute("UPDATE paper_orders SET status='UNKNOWN' WHERE id=?", (unknown,))
            cloud_runtime.put(u.store, 'exposure_caps', {'mode': 'OFF', 'regime': None})
        exposure.reduce_to_cap(u.store, u.cfg, u.at)
        self.assertEqual(u.store.db.execute('SELECT status FROM paper_orders WHERE id=?', (unknown,)).fetchone()[0], 'UNKNOWN')

    def test_back_inside_band_and_missing_episode_prevent_stale_fills(self):
        t, oid = self.watchlist_order()
        with t.store.db:
            t.store.db.execute('UPDATE quotes SET price_cents=600,prev_close_cents=600')
        self.assertEqual(paper.settle(t.store, t.cfg, self.fresh(t)), [])
        self.assertEqual(cloud_runtime.value(t.store, 'exposure_reduce_episode')['state'], 'RESOLVED')
        t, oid = self.watchlist_order()
        with t.store.db:
            t.store.db.execute("DELETE FROM cloud_state WHERE key='exposure_reduce_episode'")
        self.assertEqual(paper.settle(t.store, t.cfg, self.fresh(t)), [])
        self.assertEqual(t.store.db.execute('SELECT status FROM paper_orders WHERE id=?', (oid,)).fetchone()[0], 'CANCELLED')

    def test_registration_failure_rolls_back_order_and_ownership_together(self):
        t = self.fixture()
        t.hold('sz000001', 2000, price=1000)
        self.capital(t, 8000000)
        with patch('ashare.exposure.register_order_inside', side_effect=ValueError('simulated registration rejection')):
            result = exposure.reduce_to_cap(t.store, t.cfg, t.at)
        self.assertEqual(result['orders'], [])
        self.assertEqual(t.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0], 0)
        self.assertEqual(cloud_runtime.value(t.store, 'exposure_reduce_episode')['orders'], [])

    def test_expiry_is_processed_even_when_whole_account_valuation_is_missing(self):
        t, oid = self.watchlist_order()
        at = self.fresh(t, 300)
        with t.store.db:
            t.store.db.execute('DELETE FROM quotes')
        self.assertEqual(paper.settle(t.store, t.cfg, at), [])
        self.assertEqual(t.store.db.execute('SELECT status FROM paper_orders WHERE id=?', (oid,)).fetchone()[0], 'EXPIRED')
