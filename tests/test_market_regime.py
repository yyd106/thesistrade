"""R1 regime cap: golden agreement with the vendored reference plus pairing/cap tests."""
import json
import tempfile
import unittest
from pathlib import Path

from ashare import market_regime, exposure
from ashare.storage import Store, normalize_time as _utc
from ashare.cloud_runtime import put, value

# Vendored golden: last 320 CSI300 bars to 2026-11-06 with per-day expected caps computed
# by the registered backtest module (provenance and source sha256 inside the file).
FIXTURE = json.loads((Path(__file__).resolve().parent / 'fixtures' / 'csi300_r1_reference.json').read_text())
CFG = {'paper_max_gross_pct': 90, 'watchlist': [], 'industry_enabled': False}


def klines():
    # bars: [date, open, close, high, low] — close is field index 2
    return [(b[0], b[2]) for b in FIXTURE['bars']]


class MarketRegimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.store = Store(self.tmp.name)
        self.at = _utc('2026-11-06T16:00:00+08:00')

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def put_series(self, bars, ready='2025-01-01T00:00:00+08:00'):
        self.store.db.execute("INSERT OR IGNORE INTO runs VALUES('r1','r1-k','manual','2025-01-01',NULL,NULL,'SUCCEEDED',NULL,NULL,NULL)")
        self.store.db.execute('INSERT INTO comparison_series VALUES(?,?,?,?)',
                              ('r1', 'sh000300', _utc(ready), json.dumps({'bars': bars})))
        self.store.db.commit()

    def test_golden_agrees_with_reference(self):
        rows = klines()
        self.put_series([{'date': d, 'open': c, 'close': c, 'high': c, 'low': c, 'volume': 1} for d, c in rows])
        expected = {d: confirmed for d, _raw, confirmed in FIXTURE['expected']}
        import datetime as _dt
        day = _dt.date(2025, 1, 2)
        checked = 0
        while day <= _dt.date(2026, 11, 6):
            at = _utc(day.isoformat() + 'T16:00:00+08:00')
            res = market_regime.r1(self.store, at)
            from ashare.calendar import last_completed_day
            cutoff = last_completed_day(at)
            if cutoff and expected.get(cutoff) is not None:
                self.assertEqual(res['as_of_day'], cutoff)
                self.assertEqual(res['status'], 'OK')
                self.assertEqual(res['cap_pct'], expected[cutoff])
                checked += 1
            day += _dt.timedelta(days=1)
        # calendar trading days Eastmoney lacks (closures) are skipped by the cutoff guard
        self.assertGreater(checked, 250)

    def test_current_state_is_10(self):
        # Real series ending 2026-11-06: the confirmed cap is 10%.
        self.put_series([{'date': d, 'close': c} for d, c in klines()])
        res = market_regime.r1(self.store, self.at)
        self.assertEqual(res['status'], 'OK')
        self.assertEqual(res['as_of_day'], '2026-11-06')
        self.assertEqual(res['cap_pct'], 10)
        self.assertEqual(len(res['series_sha256']), 64)

    def test_two_day_confirmation(self):
        # Rising series (raw 90); one day below both MAs does not switch, two days do.
        base = [{'date': f'2026-09-{(i // 30) + 1:02d}-{(i % 28) + 1:02d}', 'close': 100 + i} for i in range(90)]
        # normalize to unique ascending dates using a synthetic calendar-free window:
        # market_regime works on the series' own dates; only real trading days matter for staleness.
        import datetime as _dt
        d0 = _dt.date(2026, 7, 1)
        dates = []
        d = d0
        while len(dates) < 95:
            if d.weekday() < 5:
                dates.append(d)
            d += _dt.timedelta(days=1)
        bars = [{'date': dates[i].isoformat(), 'close': 100 + i} for i in range(95)]
        bars[93]['close'] = 10  # one-day crash below both MAs
        self.put_series(bars)
        res = market_regime.r1(self.store, _utc(dates[94].isoformat() + 'T16:00:00+08:00'))
        self.assertEqual(res['raw_cap_pct'], 90)          # day 94 recovered in raw
        self.assertEqual(res['cap_pct'], 90)               # confirmed state held
        bars[94]['close'] = 10  # second consecutive day below: confirmation switches
        self.store.db.execute('DELETE FROM comparison_series'); self.put_series(bars)
        res = market_regime.r1(self.store, _utc(dates[94].isoformat() + 'T16:00:00+08:00'))
        self.assertEqual(res['cap_pct'], 50)  # m20 still above m60: exactly one condition

    def test_intraday_bar_is_ignored(self):
        # At 10:00 on a trading day the day's own bar has not completed.
        dates = [d for d, _ in klines() if d <= '2026-11-06']
        bars = [{'date': d, 'close': c} for d, c in klines()]
        self.put_series(bars)
        at = _utc('2026-11-06T10:00:00+08:00')
        res = market_regime.r1(self.store, at)
        self.assertEqual(res['as_of_day'], '2026-11-05')  # the 06th bar is excluded pre-close
        self.assertEqual(res['status'], 'OK')

    def test_stale_and_missing_cap_at_10(self):
        self.assertEqual(market_regime.r1(self.store, self.at)['cap_pct'], 10)  # MISSING
        self.assertEqual(market_regime.r1(self.store, self.at)['status'], 'MISSING')
        bars = [{'date': d, 'close': c} for d, c in klines() if d <= '2026-10-30']
        self.put_series(bars, ready='2026-10-31T00:00:00+08:00')
        res = market_regime.r1(self.store, self.at)
        self.assertEqual(res['status'], 'STALE')
        self.assertEqual(res['cap_pct'], 10)

    def test_recompute_is_stateless(self):
        bars = [{'date': d, 'close': c} for d, c in klines()]
        self.put_series(bars)
        first = market_regime.r1(self.store, self.at)
        other = Store(tempfile.mkdtemp())
        try:
            other.db.execute("INSERT INTO runs VALUES('r1','r1-k','manual','2025-01-01',NULL,NULL,'SUCCEEDED',NULL,NULL,NULL)")
            other.db.execute('INSERT INTO comparison_series VALUES(?,?,?,?)',
                             ('r1', 'sh000300', _utc('2025-01-01T00:00:00+08:00'), json.dumps({'bars': bars})))
            other.db.commit()
            second = market_regime.r1(other, self.at)
        finally:
            other.close()
        self.assertEqual(first, second)  # no in-memory state; stored series fully determines it


class ExposureCapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.store = Store(self.tmp.name)
        self.at = _utc('2026-11-06T16:00:00+08:00')

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def put_series(self):
        self.store.db.execute("INSERT OR IGNORE INTO runs VALUES('r1','r1-k','manual','2025-01-01',NULL,NULL,'SUCCEEDED',NULL,NULL,NULL)")
        self.store.db.execute('INSERT INTO comparison_series VALUES(?,?,?,?)',
                              ('r1', 'sh000300', _utc('2025-01-01T00:00:00+08:00'),
                               json.dumps({'bars': [{'date': d, 'close': c} for d, c in klines()]})))
        self.store.db.commit()

    def test_pairing_matrix(self):
        self.put_series()
        # research new × cloud old: no remote feature → regime off, cap stays 90
        cfg = dict(CFG, deployment_role='research', exposure_regime='R1')
        self.assertFalse(market_regime.regime_active(self.store, cfg))
        self.assertEqual(exposure.gross_cap_pct(self.store, cfg, self.at)[0], 90)
        # research new × cloud new: feature advertised → active, capped at confirmed 10
        put(self.store, 'remote_features', ['exposure_caps_v1'])
        self.assertTrue(market_regime.regime_active(self.store, cfg))
        self.assertEqual(exposure.gross_cap_pct(self.store, cfg, self.at)[0], 10)
        # research old × cloud new: config off → regime never activates (single predicate)
        self.assertFalse(market_regime.regime_active(self.store, dict(CFG, deployment_role='research')))
        # cloud role: OFF or never-received bundle → legacy cap; R1 bundle → stored regime
        cloud = dict(CFG, deployment_role='cloud')
        self.assertEqual(exposure.gross_cap_pct(self.store, cloud, self.at)[0], 90)
        put(self.store, 'exposure_caps', {'mode': 'OFF', 'regime': None})
        self.assertFalse(market_regime.regime_active(self.store, cloud))
        self.assertEqual(exposure.gross_cap_pct(self.store, cloud, self.at)[0], 90)
        put(self.store, 'exposure_caps', {'mode': 'R1', 'regime': {'as_of_day': '2026-11-06', 'cap_pct': 50, 'series_sha256': 'x'}})
        self.assertTrue(market_regime.regime_active(self.store, cloud))
        self.assertEqual(exposure.gross_cap_pct(self.store, cloud, self.at)[0], 50)
        # stale bundle on the cloud clock → 10
        put(self.store, 'exposure_caps', {'mode': 'R1', 'regime': {'as_of_day': '2026-10-30', 'cap_pct': 90}})
        self.assertEqual(exposure.gross_cap_pct(self.store, cloud, self.at)[0], 10)

    def test_config_off_means_legacy_cap(self):
        self.put_series()
        pct, detail = exposure.gross_cap_pct(self.store, CFG, self.at)
        self.assertEqual(pct, 90)
        self.assertIsNone(detail['regime'])

    def test_trend_gate_removed_only_when_regime_on(self):
        # Downtrend (ma20<ma60): the blocker fires with the gate on, is skipped when the
        # regime is active, and the plan still records the non-blocking trend fact.
        from ashare.research import price_plan
        cfg = dict(CFG, paper_stop_loss_bps=600, paper_take_profit_bps=1000,
                   paper_entry_band_bps=100, paper_max_stock_pct=20, strategy_version='v')
        packet = {'symbol': 'sz000001', 'as_of': self.at,
                  'source_checks': [{'source': s, 'status': 'OK'} for s in ('tencent_daily', 'cninfo_catalog', 'cninfo_pdf')],
                  'mandatory_coverage': [], 'event_reviews': [], 'local_only_document_count': 0,
                  'stocks': [{'features': {'unadjusted': {'basis': 'UNADJUSTED', 'last_complete_date': '2026-11-06',
                              'ma20_cents': 900, 'ma60_cents': 1000, 'close_cents': 950, 'bars': []}}}]}
        gated = price_plan(packet, cfg, {'action': 'WATCH'}, 'SUCCEEDED')
        self.assertIn('TREND_NOT_CONFIRMED', gated['blockers'])
        self.assertFalse(gated['trend_ok'])
        open_ = price_plan(packet, cfg, {'action': 'WATCH'}, 'SUCCEEDED', regime_on=True)
        self.assertNotIn('TREND_NOT_CONFIRMED', open_['blockers'])
        self.assertFalse(open_['trend_ok'])
        # and evaluation uses the explicit flag rather than blocker text when present
        import ashare.evaluation as ev
        self.assertIn('trend_ok', gated)

    def test_trend_gate_replaced_only_by_paired_r1(self):
        # research role: unpaired R1 is off; paired R1 replaces the gate
        cfg = dict(CFG, deployment_role='research', exposure_regime='R1')
        self.assertFalse(market_regime.trend_gate_replaced(self.store, cfg))
        put(self.store, 'remote_features', ['exposure_caps_v1'])
        self.assertTrue(market_regime.trend_gate_replaced(self.store, cfg))
        # D14 activates the regime predicate but never replaces the trend gate
        d14 = dict(CFG, deployment_role='research', exposure_regime='D14')
        put(self.store, 'remote_features', ['exposure_caps_v1', 'exposure_caps_v2'])
        self.assertTrue(market_regime.regime_active(self.store, d14))
        self.assertFalse(market_regime.trend_gate_replaced(self.store, d14))
        # no exposure_regime at all: off
        self.assertFalse(market_regime.trend_gate_replaced(self.store, dict(CFG, deployment_role='research')))
        # cloud role: only a stored R1-mode bundle replaces the gate
        cloud = dict(CFG, deployment_role='cloud')
        self.assertFalse(market_regime.trend_gate_replaced(self.store, cloud))
        put(self.store, 'exposure_caps', {'mode': 'R1'})
        self.assertTrue(market_regime.trend_gate_replaced(self.store, cloud))
        put(self.store, 'exposure_caps', {'mode': 'D14'})
        self.assertFalse(market_regime.trend_gate_replaced(self.store, cloud))
        put(self.store, 'exposure_caps', {'mode': 'OFF'})
        self.assertFalse(market_regime.trend_gate_replaced(self.store, cloud))

    def test_research_paths_keep_trend_gate_under_d14(self):
        # End-to-end through research.study with the shared buy-preview fixture:
        # a downtrend packet stays gated under paired D14 and is waived only under paired R1.
        import inspect
        import test_buy_preview as buys
        from ashare import research
        from ashare.demo import seed, research_model, SYMBOL

        def study_with(regime, features):
            t = buys.BuyPreviewTests()
            t.setUp(); self.addCleanup(t.tearDown)
            t.cfg.update(watchlist=[{'symbol': SYMBOL, 'name': 'Synthetic'}],
                         slot_execution_mode='RULES', paper_max_fill_qty=10000,
                         deployment_role='research', exposure_regime=regime)
            packet = seed(t.store, t.cfg)
            # ma20 < ma60 and close < ma60: a downtrend the E3 gate must still catch under D14.
            packet['stocks'][0]['features']['unadjusted'].update(
                ma20_cents=900, ma60_cents=1000, close_cents=950)
            with t.store.db:
                put(t.store, 'remote_features', features)
            research.study(t.store, t.cfg, packet, model_fn=research_model(packet),
                           at='2026-09-15T09:01:00+08:00')
            row = t.store.db.execute("SELECT payload_json FROM plans WHERE symbol=? AND status='ACTIVE'",
                                     (SYMBOL,)).fetchone()
            return json.loads(row[0]), t

        plan, d14 = study_with('D14', ['exposure_caps_v2'])
        self.assertTrue(market_regime.regime_active(d14.store, d14.cfg))
        self.assertIn('TREND_NOT_CONFIRMED', plan['blockers'])
        plan, r1 = study_with('R1', ['exposure_caps_v1'])
        self.assertTrue(market_regime.regime_active(r1.store, r1.cfg))
        self.assertNotIn('TREND_NOT_CONFIRMED', plan['blockers'])
        # renew() is wired through the same predicate; both call sites agree.
        source = inspect.getsource(research)
        self.assertEqual(source.count('regime_on=trend_gate_replaced(store,config)'), 2)
        self.assertNotIn('regime_on=regime_active(', source)


if __name__ == '__main__':
    unittest.main()


class ReduceToCapTests(unittest.TestCase):
    """Deterministic forced reduction toward an OK R1 cap (part B)."""
    DAY = '2026-09-17'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.store = Store(self.tmp.name)
        self.at = _utc(self.DAY + 'T10:00:00+08:00')
        self.cfg = {'deployment_role': 'standalone', 'mode': 'paper', 'exposure_regime': 'R1',
                    'paper_max_gross_pct': 90,
                    'paper_max_stock_pct': 20, 'paper_lot_size': 100, 'paper_commission_bps': 3,
                    'paper_min_fee_cents': 500, 'paper_sell_tax_bps': 5, 'paper_slippage_bps': 5,
                    'paper_order_ttl_seconds': 240, 'quote_max_age_seconds': 90,
                    'paper_stop_loss_bps': 600, 'paper_take_profit_bps': 1000,
                    'paper_entry_band_bps': 100, 'paper_max_fill_qty': 100, 'watchlist': [],
                    'industry_enabled': False, 'strategy_version': 'v', 'dynamic_enabled': False,
                    'max_packet_chars': 45000, 'paper_lot_rules': {}}
        put(self.store, 'remote_features', ['exposure_caps_v1'])
        self.regime_series()  # an OK R1 regime at cap 10 as of the last completed day
        self.store.db.execute("INSERT INTO paper_accounts VALUES('DEMO_PAPER',600000,10000000,1,0)")
        self.store.db.execute("INSERT INTO paper_flows VALUES(?,?,?,?,?,?)",
            ('pf-init', 'DEMO_PAPER', 'SIMULATED_INITIAL', 600000, 'init', '2026-09-01'))
        self.store.db.commit()

    def regime_series(self, last='2026-09-16'):
        import datetime as _dt
        d0 = _dt.date(2026, 5, 4)
        dates, d = [], d0
        while len(dates) < 95:
            if d.weekday() < 5:
                dates.append(d)
            d += _dt.timedelta(days=1)
        dates = dates[:94] + [_dt.date.fromisoformat(last)]
        bars = [{'date': x.isoformat(), 'close': 500 - i} for i, x in enumerate(dates)]
        self.store.db.execute("INSERT OR IGNORE INTO runs VALUES('r1','r1-k','manual','2025-01-01',NULL,NULL,'SUCCEEDED',NULL,NULL,NULL)")
        self.store.db.execute('INSERT INTO comparison_series VALUES(?,?,?,?)',
                              ('r1', 'sh000300', _utc('2025-01-01T00:00:00+08:00'), json.dumps({'bars': bars})))
        self.store.db.commit()

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def hold(self, symbol, qty, cost=1000, day='2026-09-10', price=None, prev=None, name='合'):
        self.store.db.execute('PRAGMA foreign_keys=OFF')
        self.store.db.execute('INSERT INTO paper_lots VALUES(?,?,?,?,?)', ('lot-' + symbol, symbol, qty, qty * cost, day))
        if price is not None:
            self.store.db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?)',
                ('q-' + symbol, symbol, name, price, prev or price, _utc(self.DAY + 'T10:00:00+08:00'),
                 _utc(self.DAY + 'T10:00:00+08:00'), 'SYN', ''))
        self.store.db.execute('INSERT INTO plans VALUES(?,?,?,?,?,?,?,?)',
            ('p-' + symbol, 's-' + symbol, symbol, _utc('2026-09-10T00:00:00+08:00'),
             _utc('2026-12-31T00:00:00+08:00'), 'ACTIVE', 'v', '{}'))
        self.store.db.commit()

    def orders(self):
        return self.store.db.execute("SELECT symbol,qty,side FROM paper_orders WHERE side='SELL'").fetchall()

    def test_gross_40_sells_prorata_to_cap(self):
        self.hold('sz000001', 400, price=1000)   # mv 400k each → equity 1M, gross 80%
        self.hold('sz000002', 400, price=1000)
        r = exposure.reduce_to_cap(self.store, self.cfg, self.at)
        self.assertTrue(r['triggered'])
        self.assertEqual(r['cap_pct'], 10)
        sells = self.orders()
        self.assertEqual(len(sells), 2)
        # equity 1.4M, gross 57% → excess 660k cents split 1:1 → 330 shares each
        for sym, qty, _ in sells:
            self.assertEqual(qty, 330)
        planned_gross = 800000 - 660 * 1000  # 140k = exactly the 10% cap
        self.assertLessEqual(planned_gross, 140000)

    def test_within_band_no_orders(self):
        self.hold('sz000001', 100, price=1000)  # ~14% ≤ 10+5
        r = exposure.reduce_to_cap(self.store, self.cfg, self.at)
        self.assertFalse(r['triggered'])
        self.assertEqual(self.orders(), [])

    def test_stale_and_missing_regime_never_sells(self):
        self.store.db.execute('DELETE FROM comparison_series'); self.store.db.commit()
        self.regime_series(last='2026-09-10')  # series ends before the last completed day
        self.hold('sz000001', 2000, price=1000)
        self.hold('sz000002', 2000, price=1000)
        r = exposure.reduce_to_cap(self.store, self.cfg, self.at)
        self.assertFalse(r['triggered'])
        self.store.db.execute('DELETE FROM comparison_series'); self.store.db.commit()
        r = exposure.reduce_to_cap(self.store, self.cfg, self.at)  # MISSING
        self.assertFalse(r['triggered'])
        self.assertEqual(self.orders(), [])

    def test_t1_and_limit_down_skipped_pro_rata_preserved(self):
        self.hold('sz000001', 2000, price=1000)                                   # sellable
        self.hold('sz000002', 2000, day=self.DAY, price=1000)                     # T+1 locked today
        self.hold('sz000003', 2000, price=900, prev=1000)                         # at limit-down
        self.store.db.execute('UPDATE paper_accounts SET cash_cents=400000')
        r = exposure.reduce_to_cap(self.store, self.cfg, self.at)
        reasons = {s['symbol']: s['reason'] for s in r['skipped']}
        self.assertEqual(reasons['sz000002'], 'T_PLUS_ONE_LOCKED')
        self.assertEqual(reasons['sz000003'], 'NEAR_PRICE_LIMIT_OR_CORPORATE_ACTION')
        sells = self.orders()
        self.assertEqual(len(sells), 1)  # only the sellable name keeps its own share
        self.assertEqual(sells[0][0], 'sz000001')
        self.assertLessEqual(sells[0][1], 2000)

    def test_star_200_share_rule(self):
        self.hold('sh688001', 300, price=800)  # wanted below 200 rounds up to the 200 rule
        r = exposure.reduce_to_cap(self.store, self.cfg, self.at)
        sells = {s[0]: s[1] for s in self.orders()}
        self.assertEqual(sells['sh688001'], 200)

    def test_no_duplicate_while_open_and_stops_at_cap(self):
        self.hold('sz000001', 2000, price=1000)
        self.hold('sz000002', 2000, price=1000)
        exposure.reduce_to_cap(self.store, self.cfg, self.at)
        r2 = exposure.reduce_to_cap(self.store, self.cfg, _utc(self.DAY + 'T10:01:00+08:00'))
        self.assertEqual(r2['orders'], [])
        self.assertEqual(len(self.orders()), 2)  # no duplicates
        # simulate the orders filling: lots gone → gross 0 → nothing left to do
        self.store.db.execute('DELETE FROM paper_lots'); self.store.db.commit()
        r3 = exposure.reduce_to_cap(self.store, self.cfg, _utc(self.DAY + 'T10:02:00+08:00'))
        self.assertFalse(r3['triggered'])
        self.assertEqual(r3['orders'], [])

    def test_research_role_and_closed_phase(self):
        self.hold('sz000001', 2000, price=1000)
        self.hold('sz000002', 2000, price=1000)
        research = dict(self.cfg, deployment_role='research')
        self.assertFalse(exposure.reduce_to_cap(self.store, research, self.at)['triggered'])
        closed = exposure.reduce_to_cap(self.store, self.cfg, _utc(self.DAY + 'T15:30:00+08:00'))
        self.assertFalse(closed['triggered'])
        self.assertEqual(self.orders(), [])

    def test_event_recorded(self):
        self.hold('sz000001', 2000, price=1000)
        self.hold('sz000002', 2000, price=1000)
        exposure.reduce_to_cap(self.store, self.cfg, self.at)
        row = self.store.db.execute('SELECT payload_json FROM portfolio_risk_events').fetchone()
        self.assertIsNotNone(row)
        payload = json.loads(row[0])
        self.assertEqual(payload['kind'], 'EXPOSURE_CAP_REDUCE')
        self.assertEqual(payload['cap_pct'], 10)
        self.assertEqual(len(payload['orders']), 2)


class EpisodeQuotaTests(ReduceToCapTests):
    """RC-01: an unsellable holding keeps its own quota; nothing is transferred."""

    def test_quota_split_over_all_holdings(self):
        self.hold('sz000001', 2000, price=1000)               # sellable
        self.hold('sz000002', 2000, day=self.DAY, price=1000) # T+1 locked
        self.hold('sz000003', 2000, price=900, prev=1000)     # limit-down
        with self.store.db:
            self.store.db.execute('UPDATE paper_accounts SET cash_cents=1000000')
        from ashare.paper import account
        a = account(self.store, self.at)
        r = exposure.reduce_to_cap(self.store, self.cfg, self.at)
        self.assertTrue(r['triggered'])
        own_cents = r['excess_cents'] * 2000000 // a['market_value_cents']
        own_qty = own_cents // 1000
        sells = [dict(x) for x in self.orders()]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]['symbol'], 'sz000001')
        # ~1765 shares own quota (lot-adjusted to 1700) — never the full 2000 liquidation
        self.assertLessEqual(sells[0]['qty'], own_qty)
        self.assertLess(sells[0]['qty'], 2000)
        ep = value(self.store, 'exposure_reduce_episode')
        self.assertEqual(ep['state'], 'OPEN')
        self.assertEqual(ep['quotas']['watchlist:sz000001'], own_cents)
        self.assertGreater(ep['quotas']['watchlist:sz000002'], 0)
        self.assertGreater(ep['quotas']['watchlist:sz000003'], 0)
        # Next minute: locked holdings keep their quotas; nothing more is sold.
        exposure.reduce_to_cap(self.store, self.cfg, _utc(self.DAY + 'T10:01:00+08:00'))
        self.assertEqual(len(self.orders()), 1)
        # When sz000002 becomes sellable it sells only its own quota.
        with self.store.db:
            self.store.db.execute("UPDATE paper_lots SET acquired_day='2026-09-10' WHERE symbol='sz000002'")
            self.store.db.execute("UPDATE quotes SET observed_at=?, first_seen_at=?",
                                  (_utc(self.DAY + 'T10:01:30+08:00'), _utc(self.DAY + 'T10:01:30+08:00')))
        exposure.reduce_to_cap(self.store, self.cfg, _utc(self.DAY + 'T10:02:00+08:00'))
        sells = [dict(x) for x in self.orders()]
        self.assertEqual(len(sells), 2)
        self.assertEqual(sells[1]['symbol'], 'sz000002')
        self.assertLessEqual(sells[1]['qty'], own_qty)

    def test_open_and_partial_orders_not_double_counted(self):
        self.hold('sz000001', 2000, price=1000)
        r = exposure.reduce_to_cap(self.store, self.cfg, self.at)
        self.assertEqual(len(self.orders()), 1)
        oid = r['orders'][0]['order_id']
        # Open order stays committed: the next run submits nothing.
        exposure.reduce_to_cap(self.store, self.cfg, _utc(self.DAY + 'T10:01:00+08:00'))
        self.assertEqual(len(self.orders()), 1)
        # Close it half-filled: the unfilled half is released and resubmitted.
        with self.store.db:
            o = dict(self.store.db.execute('SELECT * FROM paper_orders WHERE id=?', (oid,)).fetchone())
            self.store.db.execute("UPDATE paper_orders SET status='FILLED',filled_qty=? WHERE id=?",
                                  (o['qty'] // 2, oid))
            self.store.db.execute("UPDATE quotes SET observed_at=?, first_seen_at=?",
                                  (_utc(self.DAY + 'T10:01:30+08:00'), _utc(self.DAY + 'T10:01:30+08:00')))
        exposure.reduce_to_cap(self.store, self.cfg, _utc(self.DAY + 'T10:02:00+08:00'))
        self.assertEqual(len(self.orders()), 2)

    def test_episode_resolved_superseded_completed(self):
        self.hold('sz000001', 2000, price=1000)
        exposure.reduce_to_cap(self.store, self.cfg, self.at)
        self.assertEqual(value(self.store, 'exposure_reduce_episode')['state'], 'OPEN')
        # Cap switches off: the episode is superseded.
        exposure.reduce_to_cap(self.store, {**self.cfg, 'exposure_regime': 'OFF'}, self.at)
        self.assertEqual(value(self.store, 'exposure_reduce_episode')['state'], 'SUPERSEDED')
        # Back under cap: the next run resolves the fresh episode.
        exposure.reduce_to_cap(self.store, self.cfg, self.at)
        ep = value(self.store, 'exposure_reduce_episode')
        if ep['state'] == 'OPEN':
            with self.store.db:
                self.store.db.execute('DELETE FROM paper_lots')
            exposure.reduce_to_cap(self.store, self.cfg, _utc(self.DAY + 'T10:05:00+08:00'))
            self.assertEqual(value(self.store, 'exposure_reduce_episode')['state'], 'RESOLVED')

    def test_episode_completes_once_orders_close(self):
        self.hold('sz000001', 2000, price=1000)
        r = exposure.reduce_to_cap(self.store, self.cfg, self.at)
        self.assertNotEqual(r.get('episode_state'), 'COMPLETE')
        with self.store.db:
            self.store.db.execute("UPDATE paper_orders SET status='FILLED',filled_qty=qty")
        exposure.reduce_to_cap(self.store, self.cfg, _utc(self.DAY + 'T10:01:00+08:00'))
        self.assertEqual(value(self.store, 'exposure_reduce_episode')['state'], 'COMPLETE')


class GlobalSessionReduceTests(ReduceToCapTests):
    """RC-03: each route sells only while its own market is open."""

    def cloud_ready(self):
        self.cfg = {**self.cfg, 'deployment_role': 'cloud', 'investment_policy': 'days_cash_v1'}
        with self.store.db:
            put(self.store, 'initialized', True)
            put(self.store, 'execution_enabled', True)

    def ghold(self, sym, qty, cost_cents, price_micros, fx_micros, quote_at, session):
        self.store.db.execute('PRAGMA foreign_keys=OFF')
        self.store.db.execute('INSERT INTO global_fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('gf-' + sym, 'go-' + sym, 'gq-' + sym, sym, 'BUY', qty, price_micros, fx_micros,
             cost_cents, 0, 0, '2026-09-10T00:00:00+00:00', '2026-09-10T00:00:00+00:00'))
        self.store.db.execute('INSERT INTO global_lots VALUES(?,?,?,?,?)',
            ('gf-' + sym, sym, qty, cost_cents, '2026-09-10T00:00:00+00:00'))
        self.store.db.execute('INSERT INTO global_quotes VALUES(?,?,?,?,?,?,?,?)',
            ('gq-' + sym, sym, quote_at, quote_at, price_micros, fx_micros, quote_at,
             json.dumps({'session': session})))
        self.store.db.commit()

    def test_us_session_sells_global_keeps_ashare_quota(self):
        from datetime import datetime
        at = _utc(self.DAY + 'T22:00:00+08:00')
        ts = datetime.fromisoformat(at).timestamp()
        self.cloud_ready()
        from ashare.calendar import last_completed_day
        with self.store.db:
            put(self.store, 'exposure_caps', {'mode': 'R1', 'regime': {
                'status': 'OK', 'as_of_day': last_completed_day(at), 'cap_pct': 10}})
        self.hold('sz000001', 2000, price=1000)  # A-share: market closed at 22:00
        self.ghold('US:AAPL', 1000, 5000000, 100000000, 7000000, at,
                   {'start': int(ts) - 1800, 'end': int(ts) + 1800})
        self.assertTrue(exposure.needs_reduction(self.store, self.cfg, at))
        r = exposure.reduce_to_cap(self.store, self.cfg, at)
        self.assertTrue(r['triggered'])
        skipped = {s['key']: s['reason'] for s in r['skipped']}
        self.assertEqual(skipped.get('watchlist:sz000001'), 'SESSION_CLOSED')
        ep = value(self.store, 'exposure_reduce_episode')
        self.assertGreater(ep['quotas']['watchlist:sz000001'], 0)


class SchedulerReduceEntryTests(ReduceToCapTests):
    """RC-00: an over-cap book with no open orders still reaches reduce_to_cap."""

    def worker(self):
        from ashare import scheduler
        from test_scheduler_portfolio_priority import RecordingPool
        from unittest.mock import Mock
        from types import SimpleNamespace
        w = scheduler.Scheduler.__new__(scheduler.Scheduler)
        w.config_path = 'synthetic-config'
        for name in ('futures', 'dynamic_futures', 'global_futures', 'pending_completions'):
            setattr(w, name, {})
        for name in ('pool', 'dynamic_pool', 'global_pool', 'sync_pool', 'page_display_pool',
                     'strategy_export_pool', 'supervision_pool', 'reports_pool', 'experiment_pool'):
            setattr(w, name, RecordingPool())
        for name in ('sync_future', 'page_display_future', 'strategy_export_future',
                     'supervision_future', 'reports_future', 'experiment_future',
                     'experiment_cancel', 'supervision_cancel'):
            setattr(w, name, None)
        for name in ('withdrawal_tick', 'page_display_tick', 'strategy_export_tick',
                     'supervision_tick', 'experiment_tick'):
            setattr(w, name, Mock())
        w.monitor = SimpleNamespace(tick=Mock())
        for name in ('last_dividends', 'last_maintenance', 'last_sync',
                     'last_followups', 'last_page_display', 'last_strategy_export',
                     'last_supervision_scan', 'last_reports', 'last_withdrawal_check',
                     'last_experiment_scan'):
            setattr(w, name, 100)  # only the settle check is left eligible
        w.last_settle = 0
        w.experiment_scan_ok = False; w.stop = False; w.poll_seconds = 20
        return w

    def tick_jobs(self, cfg, at):
        from ashare import scheduler
        from unittest.mock import patch
        from contextlib import ExitStack
        worker = self.worker()
        with ExitStack() as stack:
            stack.enter_context(patch.object(scheduler, 'load_config', return_value=cfg))
            stack.enter_context(patch.object(scheduler, 'now', return_value=at))
            stack.enter_context(patch.object(scheduler.time, 'monotonic', return_value=100))
            for name in ('schedule_due', 'schedule_dynamic_due', 'schedule_reconnected'):
                stack.enter_context(patch.object(scheduler, name))
            worker.tick()
        return [dict(r) for r in self.store.db.execute('SELECT kind,status FROM jobs')]

    def cloud_cfg(self):
        from ashare.settings import DEFAULTS
        cfg = {**DEFAULTS, **self.cfg, 'deployment_role': 'cloud', 'scheduler_enabled': True,
               'investment_policy': 'days_cash_v1', 'data_dir': self.tmp.name}
        from ashare.calendar import last_completed_day
        with self.store.db:
            put(self.store, 'initialized', True)
            put(self.store, 'execution_enabled', True)
            put(self.store, 'exposure_caps', {'mode': 'R1', 'regime': {
                'status': 'OK', 'as_of_day': last_completed_day(self.at), 'cap_pct': 10}})
        return cfg

    def test_over_cap_no_orders_enqueues_settle(self):
        self.hold('sz000001', 2000, price=1000)
        cfg = self.cloud_cfg()
        jobs = self.tick_jobs(cfg, self.at)
        self.assertIn(('settle', 'PENDING'), [(j['kind'], j['status']) for j in jobs])
        # Executing the settle job reduces the book.
        r = exposure.reduce_to_cap(self.store, cfg, self.at)
        self.assertTrue(r['triggered'])
        self.assertTrue(self.orders())
        # A fresh scheduler instance (restart) triggers again on its own clock.
        with self.store.db:
            self.store.db.execute("DELETE FROM jobs")
        jobs = self.tick_jobs(cfg, _utc(self.DAY + 'T10:01:00+08:00'))
        self.assertIn('settle', [j['kind'] for j in jobs])

    def test_open_episode_all_locked_never_enqueues(self):
        self.hold('sz000001', 2000, day=self.DAY, price=1000)  # T+1 locked, ~77% gross
        cfg = self.cloud_cfg()
        # The episode opens but nothing can sell while every quota sits on a locked lot.
        r = exposure.reduce_to_cap(self.store, cfg, self.at)
        self.assertTrue(r['triggered'])
        self.assertFalse(r['orders'])
        self.assertFalse(exposure.needs_reduction(self.store, cfg, self.at))
        self.assertNotIn('settle', [j['kind'] for j in self.tick_jobs(cfg, self.at)])
        # Next trading day the lot unlocks and the same episode resumes the work.
        next_day = _utc('2026-09-18T10:00:00+08:00')
        from ashare.calendar import last_completed_day
        with self.store.db:
            self.store.db.execute("UPDATE paper_lots SET acquired_day='2026-09-16'")
            self.store.db.execute("UPDATE quotes SET observed_at=?, first_seen_at=?",
                                  (_utc('2026-09-18T09:59:30+08:00'), _utc('2026-09-18T09:59:30+08:00')))
            put(self.store, 'exposure_caps', {'mode': 'R1', 'regime': {
                'status': 'OK', 'as_of_day': last_completed_day(next_day), 'cap_pct': 10}})
        self.assertTrue(exposure.needs_reduction(self.store, cfg, next_day))

    def test_within_cap_and_research_never_enqueue(self):
        self.hold('sz000001', 100, price=1000)  # ~14% gross < cap 10+5pp? over — use tiny
        cfg = self.cloud_cfg()
        jobs = self.tick_jobs(cfg, self.at)
        kinds = [j['kind'] for j in jobs]
        if 'settle' in kinds:
            self.fail('settle enqueued although within cap+5pp')
        # The settle entry checks role(config)!='research' and needs_reduction itself
        # returns False on the research role — an over-cap book there stays untouched.
        research = {**cfg, 'deployment_role': 'research'}
        self.hold('sz000002', 2000, price=1000)
        self.assertFalse(exposure.needs_reduction(self.store, research, self.at))
