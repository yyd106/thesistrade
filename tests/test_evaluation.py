import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from ashare.storage import Store, normalize_time
from ashare import evaluation, shadow, weekly
from ashare.calendar import trading_day
from ashare.demo import seed, research_model, SYMBOL
from ashare.research import study


def trading_days(start, count):
    d = date.fromisoformat(start);out = []
    while len(out) < count:
        if trading_day(d) is True:out.append(d.isoformat())
        d += timedelta(days=1)
    return out


class Fixture(unittest.TestCase):
    def setUp(self):
        from test_config import load_config
        self.tmp = tempfile.TemporaryDirectory();self.store = Store(self.tmp.name)
        self.cfg = load_config(Path(__file__).resolve().parents[1] / 'config.json')
        self.cfg.update(data_dir=self.tmp.name, watchlist=[{'symbol': SYMBOL, 'name': '合成测试'}], model_enabled=True,
                        paper_slippage_bps=0, paper_entry_band_bps=200, evaluation_horizon_days=5)
        self.packet = seed(self.store, self.cfg)

    def tearDown(self):
        self.store.close();self.tmp.cleanup()

    def features(self, bars, created_at='2026-09-30T10:00:00+00:00', qfq=None):
        """bars: [(date, open, close, high, low)] in yuan."""
        raw = {'data': {SYMBOL: {'qfqday': [[d, str(o), str(c), str(h), str(l), '1000'] for d, o, c, h, l in (qfq or bars)]}}}
        path = self.store.root / 'raw' / 'fixture.json';path.parent.mkdir(exist_ok=True);path.write_text(json.dumps(raw))
        payload = {'unadjusted': {'bars': [[d, str(o), str(c), str(h), str(l), '1000'] for d, o, c, h, l in bars]}}
        with self.store.db:
            self.store.db.execute('INSERT INTO market_features VALUES(?,?,?,?,?)', ('fixture', SYMBOL, created_at, 'raw/fixture.json', json.dumps(payload)))

    def benchmark(self, days, closes):
        bars = [{'date': d, 'open': c, 'close': c, 'high': c, 'low': c, 'volume': 1} for d, c in zip(days, closes)]
        run = self.store.db.execute('SELECT id FROM runs LIMIT 1').fetchone()[0]
        with self.store.db:
            self.store.db.execute('INSERT INTO comparison_series VALUES(?,?,?,?)', (run, 'sh000300', '2026-09-30T10:00:00+00:00', json.dumps({'symbol': 'sh000300', 'bars': bars})))


class RegistryTests(Fixture):
    def test_every_plan_is_registered_and_scored_against_the_benchmark(self):
        done = study(self.store, self.cfg, self.packet, model_fn=research_model(self.packet), at='2026-09-15T09:01:00+08:00')
        row = self.store.db.execute('SELECT * FROM signal_registry').fetchone()
        self.assertEqual(row['id'], 'watchlist:' + done['plan_id']);self.assertEqual(row['as_of_day'], '2026-09-14')
        judgment = json.loads(row['judgment_json'])
        self.assertEqual(judgment['model_action'], 'WATCH');self.assertTrue(judgment['trend_ok'])
        days = trading_days('2026-09-10', 12)
        after = [d for d in days if d > '2026-09-14']
        bars = [(d, 10.0, 10.0, 10.0, 10.0) for d in days if d <= '2026-09-14'] + \
               [(d, 10.0 + i, 10.5 + i, 11.0 + i, 9.5 + i) for i, d in enumerate(after)]
        self.features(bars)
        closes = [4000.0] * 4 + [4000.0 + 40 * i for i in range(len(days) - 4)]
        self.benchmark(days, closes)
        self.assertEqual(evaluation.score(self.store, self.cfg, '2026-09-30T10:00:00+08:00')['scored'], 1)
        score = json.loads(self.store.db.execute('SELECT score_json FROM signal_scores').fetchone()[0])
        self.assertIsNone(self.store.db.execute('SELECT score_json FROM signal_registry').fetchone()[0])
        # Entry is the next trading day's open (10.00); exit the fifth day's close (14.50).
        self.assertEqual((score['entry_date'], score['exit_date']), (after[0], after[4]))
        self.assertEqual(score['return_bps'], 4500.0)
        bench = closes[days.index(after[4])] / closes[days.index(after[0])] - 1
        self.assertAlmostEqual(score['excess_bps'], round(4500.0 - bench * 10000, 1), places=0)
        self.assertEqual(score['max_down_bps'], -500.0)

    def test_pending_until_horizon_and_statistics_use_independent_samples(self):
        study(self.store, self.cfg, self.packet, model_fn=research_model(self.packet), at='2026-09-15T09:01:00+08:00')
        self.features([(d, 10.0, 10.0, 10.0, 10.0) for d in trading_days('2026-09-10', 5)])
        self.assertEqual(evaluation.score(self.store, self.cfg, '2026-09-16T10:00:00+08:00'), {'scored': 0, 'pending': 1, 'unscorable': 0})
        samples = [{'symbol': 'a', 'score': {'entry_date': d, 'exit_date': e, 'excess_bps': v}} for d, e, v in
                   (('2026-09-01', '2026-09-05', 100), ('2026-09-02', '2026-09-06', 50), ('2026-09-06', '2026-09-10', -20), ('2026-09-07', '2026-09-11', 10))]
        kept = evaluation.non_overlapping(samples, 5)
        self.assertEqual([s['score']['entry_date'] for s in kept], ['2026-09-01', '2026-09-06'])
        self.assertEqual(evaluation.describe(kept)['n'], 2)
        self.assertEqual(evaluation.describe([])['n'], 0)


class ShadowBookTests(Fixture):
    def setUp(self):
        super().setUp()
        self.days = trading_days('2026-05-06', 66)
        base = [(d, 10.0 + 0.01 * i, 10.0 + 0.01 * i, 10.1 + 0.01 * i, 9.9 + 0.01 * i) for i, d in enumerate(self.days[:64])]
        self.entry_day, self.exit_day = self.days[64], self.days[65]
        closes = [b[2] for b in base]
        ma20 = sum(closes[-20:]) / 20
        # Entry day dips into the band; the next day gaps below the stop.
        base.append((self.entry_day, ma20 * 1.05, ma20, ma20 * 1.05, ma20 * 0.99))
        base.append((self.exit_day, ma20 * 0.90, ma20 * 0.90, ma20 * 0.91, ma20 * 0.89))
        self.features(base)
        self.cfg['shadow_start_date'] = self.entry_day

    def test_mechanical_book_enters_on_band_touch_and_stops_out(self):
        result = shadow.run(self.store, self.cfg, normalize_time(self.exit_day + 'T20:00:00+08:00'))
        self.assertEqual(result['days'], [self.entry_day, self.exit_day])
        trades = {r['book']: [] for r in self.store.db.execute('SELECT book FROM shadow_trades_v2')}
        for r in self.store.db.execute('SELECT * FROM shadow_trades_v2 ORDER BY day'):trades[r['book']].append((r['side'], r['reason'], r['qty']))
        self.assertEqual([t[:2] for t in trades['A-lot']], [('BUY', 'ENTRY'), ('SELL', 'STOP')])
        self.assertEqual(trades['A-lot'][0][2] % 100, 0);self.assertNotEqual(trades['A-frac'][0][2] % 100, 0)
        self.assertNotIn('B-lot', trades)  # no research plan was active
        summary = shadow.summary(self.store, self.cfg)
        self.assertLess(summary['A-lot']['return_pct'], 0);self.assertEqual(summary['A-lot']['closed_trades'], 1)
        # Re-running is a no-op: each book-day is recorded once.
        self.assertEqual(shadow.run(self.store, self.cfg, normalize_time(self.exit_day + 'T20:00:00+08:00'))['days'], [])

    def test_research_portfolio_and_rule_sized_books_follow_their_gates(self):
        done = study(self.store, self.cfg, self.packet, model_fn=research_model(self.packet), at='2026-09-15T09:01:00+08:00')
        sig = shadow.indicators(shadow.bars_for(self.store, SYMBOL), self.entry_day, self.cfg)
        payload = json.loads(self.store.db.execute('SELECT payload_json FROM plans WHERE id=?', (done['plan_id'],)).fetchone()[0])
        payload.update(kind='PAPER_TRADE', blockers=[], levels=sig['levels'])
        with self.store.db:
            self.store.db.execute('UPDATE plans SET activated_at=?,valid_until=?,payload_json=? WHERE id=?',
                                  (normalize_time(self.entry_day + 'T08:00:00+08:00'), normalize_time(self.exit_day + 'T20:00:00+08:00'), json.dumps(payload), done['plan_id']))
            self.store.db.execute('INSERT INTO portfolio_decisions VALUES(?,?,?,?,?,?)', ('pd1', normalize_time(self.entry_day + 'T09:00:00+08:00'),
                                  normalize_time(self.exit_day + 'T20:00:00+08:00'), 'ACTIVE', 'cross_research_v1',
                                  json.dumps({'decisions': [{'key': 'watchlist:' + SYMBOL, 'action': 'ALLOW', 'target_bps': 1500}]})))
        shadow.run(self.store, self.cfg, normalize_time(self.exit_day + 'T20:00:00+08:00'))
        buys = {r['book']: r for r in self.store.db.execute("SELECT * FROM shadow_trades_v2 WHERE side='BUY'")}
        self.assertTrue({'A-lot', 'B-lot', 'C-lot', 'D-lot'} <= set(buys))
        notional = {b: buys[b]['qty'] * buys[b]['price_cents'] for b in buys}
        self.assertLessEqual(notional['C-frac'], 1_500_000 + 1)  # 15% target of CNY 100,000
        self.assertLess(notional['D-frac'], notional['C-frac'])   # 0.5% risk over a volatility stop is smaller here
        self.assertGreater(notional['B-frac'], notional['C-frac'])  # B sizes to the 20% stock cap


class WeeklyTests(Fixture):
    def test_daily_job_and_weekly_report_are_written(self):
        study(self.store, self.cfg, self.packet, model_fn=research_model(self.packet), at='2026-09-15T09:01:00+08:00')
        daily = weekly.run_daily(self.store, self.cfg, '2026-09-18T20:00:00+08:00')
        self.assertIn('registry', daily);self.assertTrue(list((self.store.root / 'workflow/evaluation/decision-time-v2/daily').glob('2026-09-18-*.json')))
        result = weekly.weekly_report(self.store, self.cfg, '2026-09-19T10:00:00+08:00')
        text = (self.store.root / result['report']).read_text()
        for heading in ('## 结论注册表', '## 对照账本', '## 版本变化', '## 未关闭的工程问题', '## 变更提案'):
            self.assertIn(heading, text)
        self.assertIn('build ', text)

    def test_jobs_scheduled_for_research_only(self):
        from ashare.scheduler import schedule_due
        cfg = {**self.cfg, 'deployment_role': 'research'}
        scan = normalize_time('2026-09-18T18:00:00+08:00')
        with self.store.db:self.store.db.execute("INSERT OR REPLACE INTO service_state VALUES('last_scan',?)", (scan,))
        items = schedule_due(self.store, cfg, '2026-09-19T11:00:00+08:00')
        kinds = {k for k, _ in items}
        self.assertIn('evaluate', kinds);self.assertIn('weekly_report', kinds)
        with self.store.db:self.store.db.execute("INSERT OR REPLACE INTO service_state VALUES('last_scan',?)", (scan,))
        self.assertFalse({k for k, _ in schedule_due(self.store, {**self.cfg, 'deployment_role': 'cloud'}, '2026-09-19T11:00:00+08:00')} & {'evaluate', 'weekly_report'})


if __name__ == '__main__':
    unittest.main()
