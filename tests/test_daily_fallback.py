"""Daily-bar fallback sources; every response is synthetic, network is fully stubbed."""
import json
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch
from ashare import calendar, sources
from ashare.storage import Store, normalize_time

SYMBOL = 'sz000001'

def mk_bars(n=90, close=100, drop_last=0):
    """Bars over the n real trading days ending at the last completed day."""
    d = date.fromisoformat(calendar.last_completed_day(sources.now()))
    days = []
    while len(days) < n:
        if calendar.trading_day(d):
            days.append(d.isoformat())
        d -= timedelta(days=1)
    days = sorted(days)[drop_last:]
    return [[x, '1', str(close), '1.1', '0.9', '10000'] for x in days]

def tq(bars): return json.dumps({'data': {SYMBOL: {'day': bars}}}).encode()
def tqfq(bars): return json.dumps({'data': {SYMBOL: {'qfqday': bars}}}).encode()
def em(bars): return json.dumps({'data': {'klines': [','.join(b) for b in bars]}}).encode()
def sina(bars): return json.dumps([{'day': b[0], 'open': b[1], 'close': b[2], 'high': b[3], 'low': b[4], 'volume': int(float(b[5])) * 100} for b in bars]).encode()

BARS = mk_bars()

def fetch_map(tq_plain=None, tq_qfq=None, emm=None, sina_=None, calls=None):
    def f(url, form=None, max_bytes=0):
        if 'gtimg.cn' in url:
            body = tq_qfq if 'qfq' in url else tq_plain
            label = 'tencent_qfq' if 'qfq' in url else 'tencent'
        elif 'eastmoney' in url:
            body, label = emm, 'eastmoney'
        else:
            body, label = sina_, 'sina'
        if calls is not None:
            calls.append(label)
        if isinstance(body, Exception):
            raise body
        return body
    return f

def attempts(store):
    return [(r[0], r[1], r[2]) for r in store.db.execute("SELECT source,status,detail FROM data_attempts ORDER BY id").fetchall()]

class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.db.execute("INSERT INTO runs VALUES('r1','k','manual','2026-01-01',NULL,NULL,'SUCCEEDED',NULL,NULL,NULL)")
        self.store.db.execute('CREATE TABLE market_features(run_id TEXT,symbol TEXT,created_at TEXT,raw_path TEXT,payload TEXT,PRIMARY KEY(run_id,symbol))')

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def save_ref(self, bars=BARS):
        self.store.db.execute('INSERT INTO market_features VALUES(?,?,?,?,?)',
            ('old', SYMBOL, '2026-01-01', '', json.dumps({'unadjusted': {'bars': bars}})))

    def collect(self, m):
        with patch('ashare.sources.fetch', side_effect=m), \
             patch('ashare.market_context.assemble', return_value={'缺口': []}):
            sources.collect_history(self.store, 'r1', SYMBOL, {})
        return json.loads(self.store.db.execute('SELECT payload FROM market_features WHERE symbol=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (SYMBOL,)).fetchone()[0])

    def test_tencent_ok_unchanged(self):
        s = self.collect(fetch_map(tq_plain=tq(BARS), tq_qfq=tqfq(BARS)))
        self.assertEqual(s['unadjusted']['source'], 'tencent_daily')
        self.assertEqual(s['qfq_source'], 'tencent_daily')

    def test_bad_tencent_is_failure_and_fallback_runs(self):
        # Empty / duplicate-date / short Tencent payloads are Tencent failures, not OK rows.
        for bad in ([], BARS[:-30], BARS + [BARS[-1]]):
            store = Store(tempfile.TemporaryDirectory().name)
            store.db.execute("INSERT INTO runs VALUES('r1','k','manual','2026-01-01',NULL,NULL,'SUCCEEDED',NULL,NULL,NULL)")
            store.db.execute('CREATE TABLE market_features(run_id TEXT,symbol TEXT,created_at TEXT,raw_path TEXT,payload TEXT,PRIMARY KEY(run_id,symbol))')
            store.db.execute('INSERT INTO market_features VALUES(?,?,?,?,?)',
                ('old', SYMBOL, '2026-01-01', '', json.dumps({'unadjusted': {'bars': BARS}})))
            with patch('ashare.sources.fetch', side_effect=fetch_map(tq_plain=tq(bad), tq_qfq=tqfq(BARS), emm=em(BARS), sina_=sina(BARS))), \
                 patch('ashare.market_context.assemble', return_value={'缺口': []}):
                sources.collect_history(store, 'r1', SYMBOL, {})
            s = json.loads(store.db.execute('SELECT payload FROM market_features WHERE symbol=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (SYMBOL,)).fetchone()[0])
            self.assertEqual(s['unadjusted']['source'], 'eastmoney_daily', bad[:1])
            rows = store.db.execute("SELECT status,detail FROM data_attempts WHERE source='tencent_daily'").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], 'FAILED')
            self.assertIn('腾讯', rows[0][1])
            store.close()
        # Stale Tencent (one day behind) is also a Tencent failure.
        self.save_ref()
        s = self.collect(fetch_map(tq_plain=tq(BARS[:-1]), tq_qfq=tqfq(BARS), emm=em(BARS), sina_=sina(BARS)))
        self.assertEqual(s['unadjusted']['source'], 'eastmoney_daily')

    def test_eastmoney_accepted_with_saved_overlap_and_sina_never_called(self):
        self.save_ref()
        calls = []
        s = self.collect(fetch_map(tq_plain=ValueError('HTTP 501'), tq_qfq=tqfq(BARS),
                                   emm=em(BARS), sina_=ValueError('SHOULD_NOT_BE_CALLED'), calls=calls))
        self.assertEqual(s['unadjusted']['source'], 'eastmoney_daily')
        self.assertNotIn('sina', calls)
        a = attempts(self.store)
        self.assertIn(('eastmoney_daily', 'ACCEPTED'), [(x[0], x[1]) for x in a])
        self.assertEqual([x[1] for x in a if x[0] == 'sina_daily'], ['SKIPPED'])

    def test_short_eastmoney_rejected_sina_used(self):
        # 20-bar Eastmoney fails the >=60 validation; fresh Sina matches the reference.
        self.save_ref()
        s = self.collect(fetch_map(tq_plain=ValueError('HTTP 501'), tq_qfq=tqfq(BARS),
                                   emm=em(BARS[-20:]), sina_=sina(BARS)))
        self.assertEqual(s['unadjusted']['source'], 'sina_daily')
        row = [x for x in attempts(self.store) if x[0] == 'eastmoney_daily'][0]
        self.assertEqual(row[1], 'REJECTED')
        self.assertIn('不足60', row[2])

    def test_stale_eastmoney_rejected_sina_used(self):
        self.save_ref(BARS[:-1])  # saved reference itself is one day older
        s = self.collect(fetch_map(tq_plain=ValueError('HTTP 501'), tq_qfq=tqfq(BARS),
                                   emm=em(BARS[:-1]), sina_=sina(BARS)))
        self.assertEqual(s['unadjusted']['source'], 'sina_daily')
        row = [x for x in attempts(self.store) if x[0] == 'eastmoney_daily'][0]
        self.assertEqual(row[1], 'REJECTED')
        self.assertIn('最新完整交易日', row[2])

    def test_mirrors_disagreeing_with_reference_rejected_original_error_surfaces(self):
        self.save_ref()
        bad1 = [b[:] for b in BARS]; bad1[-1][2] = '100.02'  # +2 cents vs reference
        bad2 = [b[:] for b in BARS]; bad2[-1][2] = '100.03'  # +3 cents
        with patch('ashare.sources.fetch', side_effect=fetch_map(tq_plain=ValueError('SYNTHETIC_TENCENT_501'), tq_qfq=tqfq(BARS), emm=em(bad1), sina_=sina(bad2))), \
             patch('ashare.market_context.assemble', return_value={'缺口': []}):
            with self.assertRaises(ValueError) as cm:
                sources.collect_history(self.store, 'r1', SYMBOL, {})
        self.assertIn('SYNTHETIC_TENCENT_501', str(cm.exception))
        a = {x[0]: x for x in attempts(self.store) if x[0] in ('eastmoney_daily', 'sina_daily')}
        self.assertEqual(a['eastmoney_daily'][1], 'REJECTED')
        self.assertIn('不一致', a['eastmoney_daily'][2]); self.assertIn('"max_diff_cents": 2', a['eastmoney_daily'][2])
        self.assertEqual(a['sina_daily'][1], 'REJECTED')
        self.assertIn('"max_diff_cents": 3', a['sina_daily'][2])

    def test_no_reference_mirrors_agree_or_not(self):
        # No saved reference: agreeing mirrors accepted (Eastmoney preferred), disagreeing rejected.
        s = self.collect(fetch_map(tq_plain=ValueError('HTTP 501'), tq_qfq=tqfq(BARS), emm=em(BARS), sina_=sina(BARS)))
        self.assertEqual(s['unadjusted']['source'], 'eastmoney_daily')
        store2 = Store(tempfile.TemporaryDirectory().name)
        store2.db.execute("INSERT INTO runs VALUES('r2','k','manual','2026-01-01',NULL,NULL,'SUCCEEDED',NULL,NULL,NULL)")
        store2.db.execute('CREATE TABLE market_features(run_id TEXT,symbol TEXT,created_at TEXT,raw_path TEXT,payload TEXT,PRIMARY KEY(run_id,symbol))')
        bad = [b[:] for b in BARS]; bad[-1][2] = '100.02'
        with patch('ashare.sources.fetch', side_effect=fetch_map(tq_plain=ValueError('x'), tq_qfq=tqfq(BARS), emm=em(BARS), sina_=sina(bad))), \
             patch('ashare.market_context.assemble', return_value={'缺口': []}):
            with self.assertRaises(ValueError):
                sources.collect_history(store2, 'r2', SYMBOL, {})
        rows = attempts(store2)
        self.assertEqual([x[1] for x in rows if x[0] in ('eastmoney_daily', 'sina_daily')], ['REJECTED', 'REJECTED'])
        store2.close()

    def test_audit_distinguishes_fetch_failure_from_validation(self):
        self.save_ref()
        with self.assertRaises(ValueError):
            with patch('ashare.sources.fetch', side_effect=fetch_map(tq_plain=ValueError('x'), tq_qfq=tqfq(BARS), emm=ValueError('NETWORK_DOWN'), sina_=sina(BARS[:-1]))), \
                 patch('ashare.market_context.assemble', return_value={'缺口': []}):
                sources.collect_history(self.store, 'r1', SYMBOL, {})
        a = {x[0]: x for x in attempts(self.store) if x[0] in ('eastmoney_daily', 'sina_daily')}
        self.assertEqual(a['eastmoney_daily'][1], 'FETCH_FAILED')  # fetch succeeded? no — network failure
        self.assertEqual(a['sina_daily'][1], 'REJECTED')          # fetched OK, failed validation

    def test_sina_volume_rounds_half_up(self):
        # 50-share remainder rounds up to one lot instead of truncating to zero.
        b = [x[:] for x in BARS]; b[-1][5] = '150'  # 150 shares in sina payload -> 2 lots? see below
        rows = json.loads(sina(b))
        rows[-1]['volume'] = 150  # 1.5 手 -> 2
        raw = json.dumps(rows).encode()
        with patch('ashare.sources.fetch', side_effect=fetch_map(emm=None, sina_=raw)):
            out = sources.sina_daily(SYMBOL)
        self.assertEqual(out[-1][5], '2')
        rows[-1]['volume'] = 149  # 1.49 手 -> 1
        with patch('ashare.sources.fetch', side_effect=fetch_map(emm=None, sina_=json.dumps(rows).encode())):
            out = sources.sina_daily(SYMBOL)
        self.assertEqual(out[-1][5], '1')


    def test_held_candidate_gets_audit_row_when_later_accepted(self):
        # P2-01: eastmoney was requested and held (10-day overlap) but sina was
        # accepted via 40-day overlap — the held request must leave an audit row.
        saved = mk_bars(140)[:-50]          # 90 bars ending 50 trading days ago
        self.save_ref(saved)
        rows = self.collect(fetch_map(tq_plain=ValueError('x'), tq_qfq=tqfq(BARS),
                                    emm=em(mk_bars(60)), sina_=sina(BARS)))
        self.assertEqual(rows['unadjusted']['source'], 'sina_daily')
        att = attempts(self.store)
        em_row = [a for a in att if a[0] == 'eastmoney_daily']
        self.assertEqual(len(em_row), 1)
        self.assertEqual(em_row[0][1], 'REJECTED')
        detail = json.loads(em_row[0][2])
        self.assertEqual(detail['overlap'], 10)
        self.assertIn('重叠', detail['reason'])
        names = [a[0] for a in att]
        sina_row = att[names.index('sina_daily')]
        self.assertEqual(sina_row[1], 'ACCEPTED')
        self.assertLess(names.index('eastmoney_daily'), names.index('sina_daily'))

    def test_non_finite_and_bad_prices_rejected_then_fallback(self):
        # P2-02: NaN / ±Infinity / non-numeric OHLC must reject the candidate and
        # still let the next valid source through — no crash, no silent accept.
        for field, value in ((2, 'NaN'), (1, 'Infinity'), (1, '-Infinity'), (2, 'abc')):
            store = Store(tempfile.TemporaryDirectory().name)
            store.db.execute("INSERT INTO runs VALUES('r1','k','manual','2026-01-01',NULL,NULL,'SUCCEEDED',NULL,NULL,NULL)")
            store.db.execute('CREATE TABLE market_features(run_id TEXT,symbol TEXT,created_at TEXT,raw_path TEXT,payload TEXT,PRIMARY KEY(run_id,symbol))')
            store.db.execute('INSERT INTO market_features VALUES(?,?,?,?,?)',
                ('old', SYMBOL, '2026-01-01', '', json.dumps({'unadjusted': {'bars': BARS}})))
            bad = [b[:] for b in BARS]
            bad[0][field] = value
            with patch('ashare.sources.fetch', side_effect=fetch_map(
                    tq_plain=ValueError('x'), tq_qfq=tqfq(BARS), emm=em(bad), sina_=sina(BARS))), \
                 patch('ashare.market_context.assemble', return_value={'缺口': []}):
                sources.collect_history(store, 'r1', SYMBOL, {})
            att = attempts(store)
            em_row = [a for a in att if a[0] == 'eastmoney_daily']
            self.assertEqual(em_row[0][1], 'REJECTED', (field, value))
            self.assertIn('价格', json.loads(em_row[0][2])['reason'])
            self.assertEqual([a for a in att if a[0] == 'sina_daily'][0][1], 'ACCEPTED', (field, value))
            store.close()

    def test_qfq_fallback(self):
        s = self.collect(fetch_map(tq_plain=tq(BARS), tq_qfq=ValueError('HTTP 501'), emm=em(BARS)))
        self.assertEqual(s['qfq_source'], 'eastmoney_daily')

    def test_price_plan_gap_naming(self):
        from ashare import research
        def gaps(checks):
            p = {'symbol': SYMBOL, 'as_of': '2026-01-01T10:00:00+08:00', 'source_checks': checks,
                 'mandatory_coverage': [], 'local_only_required_count': 0, 'local_only_document_count': 0,
                 'stocks': [{'features': {}}]}
            return research.price_plan(p, {'paper_stop_loss_bps': 600, 'paper_take_profit_bps': 1000,
                'strategy_version': 't', 'paper_max_stock_pct': 20}, {'action': 'WATCH'}, 'SUCCEEDED')['blockers']
        self.assertNotIn('SOURCE_GAP:daily_bars', gaps([{'source': 'daily_bars', 'status': 'OK'}, {'source': 'cninfo_catalog', 'status': 'OK'}, {'source': 'cninfo_pdf', 'status': 'OK'}]))
        self.assertNotIn('SOURCE_GAP:daily_bars', gaps([{'source': 'tencent_daily', 'status': 'OK'}, {'source': 'cninfo_catalog', 'status': 'OK'}, {'source': 'cninfo_pdf', 'status': 'OK'}]))
        self.assertIn('SOURCE_GAP:daily_bars', gaps([{'source': 'cninfo_catalog', 'status': 'OK'}, {'source': 'cninfo_pdf', 'status': 'OK'}]))


    def test_one_tencent_attempt_row_per_round(self):
        self.collect(fetch_map(tq_plain=tq(BARS), tq_qfq=tqfq(BARS)))
        rows = self.store.db.execute("SELECT status,detail FROM data_attempts WHERE source='tencent_daily'").fetchall()
        self.assertEqual([tuple(r) for r in rows], [('OK', '')])
        store2 = Store(tempfile.TemporaryDirectory().name)
        store2.db.execute("INSERT INTO runs VALUES('r2','k','manual','2026-01-01',NULL,NULL,'SUCCEEDED',NULL,NULL,NULL)")
        with patch('ashare.sources.fetch', side_effect=fetch_map(tq_plain=ValueError('HTTP 501'), tq_qfq=ValueError('HTTP 501'), emm=em(BARS), sina_=sina(BARS))), \
             patch('ashare.market_context.assemble', return_value={'缺口': []}):
            sources.collect_history(store2, 'r2', SYMBOL, {})
        rows = store2.db.execute("SELECT status,detail FROM data_attempts WHERE source='tencent_daily'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 'FAILED')
        self.assertIn('复权', rows[0][1]); self.assertIn('未复权', rows[0][1]); self.assertIn('501', rows[0][1])
        store2.close()

    def test_fallback_never_exceeds_90_bars(self):
        long_bars = mk_bars(120)
        s = self.collect(fetch_map(tq_plain=ValueError('x'), tq_qfq=tqfq(BARS), emm=em(long_bars), sina_=sina(long_bars)))
        self.assertLessEqual(len(s['unadjusted']['bars']), 90)
        # The fetchers themselves trim to the 90 most recent bars, like Tencent's ,day,,,90, window.
        with patch('ashare.sources.fetch', side_effect=fetch_map(emm=em(long_bars), sina_=sina(long_bars))):
            self.assertLessEqual(len(sources.eastmoney_daily(SYMBOL)), 90)
            self.assertLessEqual(len(sources.sina_daily(SYMBOL)), 90)


if __name__ == '__main__':
    unittest.main()
