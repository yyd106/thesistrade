import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ashare import quote_health as qh
from ashare.sources import collect_quotes
from ashare.storage import Store, now
from test_config import load_config

SYMBOL, OTHER = 'sh600000', 'sz000001'
DAY = '20260915'


def fields(symbol, price='10.00', prev='10.00', stamp=DAY + '100000'):
    v = [''] * 50
    v[0], v[1], v[2], v[3], v[4], v[30] = '1', '测试' + symbol[-2:], symbol[2:], price, prev, stamp
    return v


def batch(*symbols):
    return ''.join('v_' + s + '="' + '~'.join(fields(s)) + '";' for s in symbols).encode('gb18030')


def minute(symbol, price='9.90', prev='10.00', minutes=None, date=DAY):
    series = minutes if minutes is not None else ['0930 10.00 100 100000.00', '1000 9.95 100 99500.00', '1001 9.30 100 93000.00', '1002 9.90 100 99000.00']
    body = {'code': 0, 'data': {symbol: {'data': {'data': series, 'date': date}, 'qt': {symbol: fields(symbol, price, prev)}}}}
    return json.dumps(body, ensure_ascii=False).encode()


class Endpoints:
    """Fake network: the batch endpoint and the minute endpoint fail or answer independently."""
    def __init__(self, batch_body=None, minute_ok=True, series=None):
        self.batch_body, self.minute_ok, self.series, self.calls = batch_body, minute_ok, series, []

    def __call__(self, url, **kw):
        self.calls.append(url)
        if url.startswith('https://qt.gtimg.cn/'):
            if self.batch_body is None:
                raise OSError('primary down')
            return self.batch_body
        symbol = url.rsplit('=', 1)[1]
        if not self.minute_ok:
            raise OSError('minute down')
        return minute(symbol, minutes=self.series)

    def minute_calls(self):
        return [u for u in self.calls if 'minute' in u]


class QuoteHealthTests(unittest.TestCase):
    def setUp(self):
        # Fixtures carry fixed past timestamps; the age rule for live trading is tested on its own below.
        self.live = patch('ashare.quote_health.live', return_value=False)
        self.live.start()
        self.addCleanup(self.live.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cfg = load_config(Path(__file__).resolve().parents[1] / 'config.json')
        self.cfg.update(data_dir=self.tmp.name, deployment_role='cloud')
        self.store.db.execute("INSERT INTO runs(id,job_key,kind,started_at,status) VALUES('r','r','collect',?,'RUNNING')", (now(),))
        self.store.db.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def events(self):
        return [dict(r) for r in self.store.db.execute('SELECT * FROM quote_health ORDER BY started_at,kind')]

    def hold(self, symbol=SYMBOL, qty=100, cost=100000, stop=None, sell=None):
        self.store.db.execute('PRAGMA foreign_keys=OFF')
        with self.store.db:
            self.store.db.execute('INSERT INTO paper_lots VALUES(?,?,?,?,?)', ('lot-' + symbol, symbol, qty, cost, '2026-09-10'))
            if stop or sell:
                payload = {'kind': 'PAPER_TRADE', 'levels': {'stop_cents': stop, 'sell_cents': sell}}
                self.store.db.execute('INSERT INTO plans VALUES(?,?,?,?,?,?,?,?)', ('plan-' + symbol, 'study', symbol, '2026-09-15T01:00:00+00:00',
                                      '2026-09-16T01:00:00+00:00', 'ACTIVE', 'paper_baseline_v1', json.dumps(payload)))

    def test_minute_endpoint_parses_quote_and_series(self):
        row, series = qh.parse_minute(minute(SYMBOL), SYMBOL)
        self.assertEqual((row['price_cents'], row['prev_close_cents'], row['symbol']), (990, 1000, SYMBOL))
        self.assertEqual(series['date'], DAY)
        self.assertEqual(series['minutes'][2], ('1001', 930))
        # A price outside the widest daily limit means a misread field, never a trading price.
        with self.assertRaises(ValueError):
            qh.parse_minute(minute(SYMBOL, price='12.20'), SYMBOL)
        with self.assertRaises(ValueError):
            qh.parse_minute(minute(SYMBOL).replace(b'"600000"', b'"600001"'), SYMBOL)

    def test_backup_endpoint_supplies_missing_symbol_and_event_closes_on_recovery(self):
        net = Endpoints(batch_body=batch(OTHER))  # batch omits SYMBOL
        collect_quotes(self.store, 'r', [SYMBOL, OTHER], self.cfg, fetch_fn=net)
        sources = dict(self.store.db.execute('SELECT symbol,source FROM quotes'))
        self.assertEqual(sources, {SYMBOL: 'tencent_minute_fallback', OTHER: 'tencent_public_research'})
        (event,) = self.events()
        self.assertEqual((event['kind'], event['ended_at'], json.loads(event['symbols_json'])), ('PRIMARY_DOWN', None, [SYMBOL]))
        self.assertIn('1只改用分时接口', self.store.db.execute("SELECT detail FROM source_checks WHERE source='tencent_quotes' ORDER BY id DESC").fetchone()[0])
        net.batch_body = batch(SYMBOL, OTHER)
        collect_quotes(self.store, 'r', [SYMBOL, OTHER], self.cfg, fetch_fn=net)
        (event,) = self.events()
        self.assertIsNotNone(event['ended_at'])
        self.assertEqual(json.loads(self.store.db.execute("SELECT value FROM service_state WHERE key='quote_health_open'").fetchone()[0]), {})

    def test_backup_is_asked_at_most_once_a_minute_per_symbol(self):
        net = Endpoints(batch_body=None)
        collect_quotes(self.store, 'r', [SYMBOL], self.cfg, fetch_fn=net)
        collect_quotes(self.store, 'r', [SYMBOL], self.cfg, fetch_fn=net)  # fresh backup quote: nothing fetched, no error
        self.assertEqual(len(net.minute_calls()), 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM quotes').fetchone()[0], 1)
        # A failed backup attempt is not retried within the minute either, and the symbol counts as missing.
        net.minute_ok = False
        marks = json.loads(self.store.db.execute("SELECT value FROM service_state WHERE key='quote_fallback_at'").fetchone()[0])
        marks[SYMBOL]['at'] = '2000-01-01T00:00:00+00:00'
        with self.store.db:
            self.store.db.execute("UPDATE service_state SET value=? WHERE key='quote_fallback_at'", (json.dumps(marks),))
        with self.assertRaises(OSError):
            collect_quotes(self.store, 'r', [SYMBOL], self.cfg, fetch_fn=net)
        with self.assertRaises(OSError):
            collect_quotes(self.store, 'r', [SYMBOL], self.cfg, fetch_fn=net)
        self.assertEqual(len(net.minute_calls()), 2)
        kinds = [e['kind'] for e in self.events()]
        self.assertEqual(sorted(kinds), ['NO_QUOTE', 'PRIMARY_DOWN'])

    def test_without_config_or_when_disabled_no_backup_is_used(self):
        net = Endpoints(batch_body=batch(OTHER))
        with self.assertRaises(ValueError):
            collect_quotes(self.store, 'r', [SYMBOL, OTHER], None, fetch_fn=net)
        self.assertEqual(self.events(), [])  # callers without a config (tests, tools) record nothing
        with self.assertRaises(ValueError):
            collect_quotes(self.store, 'r', [SYMBOL, OTHER], {**self.cfg, 'quote_fallback_enabled': False}, fetch_fn=net)
        self.assertEqual(net.minute_calls(), [])
        self.assertEqual([e['kind'] for e in self.events()], ['NO_QUOTE', 'PRIMARY_DOWN'])

    def test_suspended_stock_is_not_an_outage(self):
        body = ''.join('v_' + s + '="' + '~'.join(fields(s, price='0.00' if s == SYMBOL else '10.00')) + '";' for s in (SYMBOL, OTHER)).encode('gb18030')
        net = Endpoints(batch_body=body)
        with self.assertRaises(ValueError):
            collect_quotes(self.store, 'r', [SYMBOL, OTHER], self.cfg, fetch_fn=net)
        self.assertEqual(net.minute_calls(), [])
        self.assertEqual(self.events(), [])
        self.assertIn('停牌', self.store.db.execute("SELECT detail FROM data_attempts WHERE symbol=? ORDER BY id DESC", (SYMBOL,)).fetchone()[0])

    def test_research_replica_records_nothing(self):
        net = Endpoints(batch_body=None, minute_ok=False)
        with self.assertRaises(OSError):
            collect_quotes(self.store, 'r', [SYMBOL], {**self.cfg, 'deployment_role': 'research'}, fetch_fn=net)
        self.assertEqual(self.events(), [])

    def test_gap_check_records_stop_that_would_have_triggered_without_trading(self):
        # 100 shares at 10.00 (cost 1000.00 yuan): the 6% cost stop sits at 9.40, the plan stop at 9.50.
        self.hold(stop=950, sell=1100)
        start, end = '2026-09-15T01:59:00+00:00', '2026-09-15T02:03:00+00:00'  # 09:59-10:03 Beijing
        qh.observe(self.store, self.cfg, start, [SYMBOL], [SYMBOL])
        qh.observe(self.store, self.cfg, '2026-09-15T02:01:00+00:00', [SYMBOL], [SYMBOL])
        orders = self.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0]
        net = Endpoints()
        qh.observe(self.store, self.cfg, end, [], [], fetch=net)
        self.assertEqual(net.calls, [])  # the check is queued, never run inside the quote refresh
        qh.sweep(self.store, self.cfg, end, fetch=lambda url, **kw: minute(SYMBOL))
        no_quote = next(e for e in self.events() if e['kind'] == 'NO_QUOTE')
        (check,) = json.loads(no_quote['check_json'])
        self.assertEqual((check['low_cents'], check['cost_stop_cents'], check['plan_stop_cents']), (930, 940, 950))
        self.assertEqual(check['breached'], ['cost_stop_cents', 'plan_stop_cents'])
        self.assertFalse(check['exit_crossed'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0], orders)
        s = qh.summary(self.store, '2026-09-15T00:00:00+00:00', '2026-09-15T15:00:00+00:00')
        self.assertEqual((s['minutes']['NO_QUOTE'], s['held_minutes'], s['open']), (4, 4, 0))
        self.assertEqual(s['breaches'][0]['symbol'], SYMBOL)

    def test_pause_in_trading_ends_event_at_last_observation(self):
        qh.observe(self.store, self.cfg, '2026-09-15T03:25:00+00:00', [SYMBOL], [])  # 11:25
        qh.observe(self.store, self.cfg, '2026-09-15T03:29:50+00:00', [SYMBOL], [])  # 11:29:50, then lunch
        qh.observe(self.store, self.cfg, '2026-09-15T05:00:10+00:00', [SYMBOL], [])  # 13:00:10, still failing
        first, second = self.events()
        self.assertEqual(first['ended_at'], '2026-09-15T03:29:50+00:00')
        self.assertEqual((second['started_at'], second['ended_at']), ('2026-09-15T05:00:10+00:00', None))
        s = qh.summary(self.store, '2026-09-15T00:00:00+00:00', '2026-09-15T15:50:00+00:00')
        self.assertEqual((s['minutes']['PRIMARY_DOWN'], s['open']), (5, 1))  # the open event counts to its last observation

    def test_an_event_left_open_by_a_crash_is_closed_at_its_last_observation(self):
        qh.observe(self.store, self.cfg, '2026-09-15T02:00:00+00:00', [SYMBOL], [])
        with self.store.db:  # a second refresher's event that never reached the shared state
            self.store.db.execute("INSERT INTO quote_health VALUES('orphan','NO_QUOTE','2026-09-15T02:00:05+00:00','2026-09-15T02:00:20+00:00',NULL,'[]','{}','x',NULL,'2026-09-15T02:00:20+00:00')")
        qh.observe(self.store, self.cfg, '2026-09-15T02:00:30+00:00', [SYMBOL], [])
        rows = {r['id']: dict(r) for r in self.store.db.execute('SELECT id,ended_at FROM quote_health')}
        self.assertEqual(rows['orphan']['ended_at'], '2026-09-15T02:00:20+00:00')
        self.assertEqual(sum(1 for r in rows.values() if r['ended_at'] is None), 1)  # the live PRIMARY_DOWN event stays open

    def test_gap_across_days_is_not_checked_against_the_wrong_day(self):
        self.hold()
        qh.observe(self.store, self.cfg, '2026-09-15T06:50:00+00:00', [SYMBOL], [SYMBOL])
        net = Endpoints()
        qh.observe(self.store, self.cfg, '2026-09-16T01:30:00+00:00', [], [], fetch=net)
        self.assertEqual(net.calls, [])
        check = json.loads(next(e for e in self.events() if e['kind'] == 'NO_QUOTE')['check_json'])
        self.assertIn('跨日', check[0]['error'])

    def test_replica_mirrors_rows_and_rejects_foreign_shapes(self):
        qh.observe(self.store, self.cfg, '2026-09-15T02:00:00+00:00', [SYMBOL], [])
        rows = qh.changed_since(self.store, None, now())
        after = (datetime.fromisoformat(rows[-1]['updated_at']) + timedelta(seconds=1)).isoformat()
        self.assertEqual(qh.changed_since(self.store, after, now()), [])
        # A stop check written after the event closed moves updated_at, so a replica that already has the row gets it again.
        qh._record_checks(self.store, rows[0]['id'], [{'symbol': SYMBOL, 'error': 'late'}])
        self.assertEqual(len(qh.changed_since(self.store, rows[0]['updated_at'], now())), 1)
        other = Store(self.tmp.name + '/replica')
        try:
            qh.upsert(other, rows)
            qh.upsert(other, rows)
            self.assertEqual(other.db.execute('SELECT count(*) FROM quote_health').fetchone()[0], 1)
            with self.assertRaises(ValueError):
                qh.upsert(other, [{**rows[0], 'extra': 1}])
        finally:
            other.close()

    def test_daily_digest_reports_outage_and_stop_it_would_have_hit(self):
        from ashare.digest import build, markdown
        self.hold(stop=950, sell=1100)
        start = datetime(2026, 9, 15, 1, 58, tzinfo=timezone.utc)  # 09:58 Beijing, refreshed every 10 seconds
        for i in range(36):
            qh.observe(self.store, self.cfg, (start + timedelta(seconds=10 * i)).isoformat(), [SYMBOL], [SYMBOL])
        qh.observe(self.store, self.cfg, (start + timedelta(minutes=6)).isoformat(), [], [])
        qh.sweep(self.store, self.cfg, (start + timedelta(minutes=6)).isoformat(), fetch=lambda url, **kw: minute(SYMBOL))
        d = build(self.store, self.cfg, '2026-09-15')
        self.assertEqual((d['quotes']['held_minutes'], d['quotes']['minutes']['PRIMARY_DOWN']), (6, 6))
        self.assertIn('持仓股票有 6 分钟两个行情接口都没有可用报价，期间无法按止损卖出', d['flags'])
        self.assertIn('行情中断时 sh600000 的分时最低价 9.30 元触及成本止损价、计划止损价（09:58–10:04），程序没有补单', d['flags'])
        self.assertFalse(any('主行情接口累计' in f for f in d['flags']))  # under 30 minutes
        page = markdown(d)
        self.assertIn('## 行情源', page)
        self.assertIn('09:58–10:04 两个接口都没有可用报价（1 只，持仓 sh600000）', page)
        self.assertIn('程序只记录，不补单', page)

    def test_held_stock_only_carries_its_own_missing_stretch(self):
        # W (watchlist only) has no quote 10:00-10:30; held H misses only the 10:29:50 refresh.
        # H traded at 9.30 at 10:05, below its 9.40 stop, while its own quotes were arriving.
        self.hold()
        t = datetime(2026, 9, 15, 2, 0, tzinfo=timezone.utc)
        for i in range(180):
            missing = [OTHER] + ([SYMBOL] if i == 179 else [])
            qh.observe(self.store, self.cfg, (t + timedelta(seconds=10 * i)).isoformat(), missing, missing)
        series = ['1000 9.95 1 1', '1005 9.30 1 1', '1010 9.90 1 1', '1029 9.95 1 1', '1030 9.96 1 1']
        qh.observe(self.store, self.cfg, (t + timedelta(minutes=30)).isoformat(), [], [])
        qh.sweep(self.store, self.cfg, (t + timedelta(minutes=30)).isoformat(), fetch=lambda url, **kw: minute(SYMBOL, price='9.96', minutes=series))
        event = next(e for e in self.events() if e['kind'] == 'NO_QUOTE')
        self.assertEqual(json.loads(event['held_json']), {SYMBOL: [['2026-09-15T02:29:50+00:00', '2026-09-15T02:30:00+00:00']]})
        (check,) = json.loads(event['check_json'])
        self.assertEqual((check['from'], check['to'], check['low_cents'], check['breached']), ('10:29', '10:30', 995, []))
        s = qh.summary(self.store, '2026-09-15T00:00:00+00:00', '2026-09-15T15:00:00+00:00')
        self.assertEqual((s['minutes']['NO_QUOTE'], s['held_minutes'], s['breaches']), (30, 0, []))

    def test_many_short_outages_add_up_before_rounding(self):
        t = datetime(2026, 9, 15, 1, 30, tzinfo=timezone.utc)  # 09:30 Beijing; two refreshes in three fail
        for i in range(720):
            at = (t + timedelta(seconds=10 * i)).isoformat()
            qh.observe(self.store, self.cfg, at, [SYMBOL] if i % 3 else [], [])
        s = qh.summary(self.store, '2026-09-15T00:00:00+00:00', '2026-09-15T08:00:00+00:00')
        self.assertEqual(len(s['events']), 240)
        self.assertEqual(s['minutes']['PRIMARY_DOWN'], 80)  # each event runs 20 s, from its first failure to the next good refresh

    def test_after_the_close_idle_events_end_and_are_checked(self):
        self.hold(stop=950)
        qh.observe(self.store, self.cfg, '2026-09-15T06:55:00+00:00', [SYMBOL], [SYMBOL])  # 14:55
        qh.observe(self.store, self.cfg, '2026-09-15T06:56:50+00:00', [SYMBOL], [SYMBOL])
        series = ['1455 9.60 1 1', '1456 9.45 1 1', '1457 9.50 1 1']
        self.assertEqual(qh.sweep(self.store, self.cfg, '2026-09-15T07:00:00+00:00', fetch=lambda url, **kw: minute(SYMBOL, minutes=series)), [])
        ended = qh.sweep(self.store, self.cfg, '2026-09-15T07:05:00+00:00', fetch=lambda url, **kw: minute(SYMBOL, minutes=series))
        self.assertEqual(sorted(ended), ['NO_QUOTE', 'PRIMARY_DOWN'])
        event = next(e for e in self.events() if e['kind'] == 'NO_QUOTE')
        self.assertEqual(event['ended_at'], '2026-09-15T06:56:50+00:00')
        (check,) = json.loads(event['check_json'])
        self.assertEqual((check['low_cents'], check['breached']), (945, ['plan_stop_cents']))  # the 9.40 cost stop was not reached
        self.assertEqual(qh.sweep(self.store, self.cfg, '2026-09-15T07:10:00+00:00'), [])

    def test_in_live_trading_a_batch_that_stopped_updating_is_replaced_from_the_backup(self):
        self.live.stop()
        at = datetime.now(timezone.utc)
        stamp = lambda seconds: (at - timedelta(seconds=seconds)).astimezone(timezone(timedelta(hours=8))).strftime('%Y%m%d%H%M%S')
        stale = ('v_' + SYMBOL + '="' + '~'.join(fields(SYMBOL, stamp=stamp(300))) + '";').encode('gb18030')
        fresh = {'code': 0, 'data': {SYMBOL: {'data': {'data': [], 'date': stamp(0)[:8]}, 'qt': {SYMBOL: fields(SYMBOL, '10.01', '10.00', stamp(3))}}}}
        calls = []
        def net(url, **kw):
            calls.append(url)
            return stale if 'qt.gtimg.cn' in url else json.dumps(fresh).encode()
        with patch('ashare.quote_health.live', return_value=True):
            collect_quotes(self.store, 'r', [SYMBOL], self.cfg, fetch_fn=net)
        self.assertEqual(len(calls), 2)
        self.assertEqual(dict(self.store.db.execute('SELECT symbol,source FROM quotes')), {SYMBOL: 'tencent_minute_fallback'})
        self.assertEqual([e['kind'] for e in self.events()], ['PRIMARY_DOWN'])

    def test_one_quiet_stock_in_a_live_batch_is_not_an_outage(self):
        self.live.stop()
        now_bj = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
        stamp = lambda seconds: (now_bj - timedelta(seconds=seconds)).strftime('%Y%m%d%H%M%S')
        body = lambda old: ''.join('v_' + s + '="' + '~'.join(fields(s, stamp=stamp(old if s == SYMBOL else 3))) + '";' for s in (SYMBOL, OTHER)).encode('gb18030')
        net = Endpoints(batch_body=body(300))
        with patch('ashare.quote_health.live', return_value=True):
            collect_quotes(self.store, 'r', [SYMBOL, OTHER], self.cfg, fetch_fn=net)  # SYMBOL has not traded for 5 minutes
        self.assertEqual(net.minute_calls(), [])
        self.assertEqual(self.events(), [])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM quotes').fetchone()[0], 2)

    def test_a_feed_stopped_for_one_exchange_is_caught_while_the_other_runs(self):
        self.live.stop()
        now_bj = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
        stamp = lambda seconds: (now_bj - timedelta(seconds=seconds)).strftime('%Y%m%d%H%M%S')
        shanghai, shenzhen = ['sh600000', 'sh600001', 'sh600002'], ['sz000001', 'sz000002', 'sz000003']
        body = ''.join('v_' + s + '="' + '~'.join(fields(s, stamp=stamp(300 if s.startswith('sz') else 3))) + '";' for s in shanghai + shenzhen).encode('gb18030')
        net = Endpoints(batch_body=body, minute_ok=False)
        with patch('ashare.quote_health.live', return_value=True):
            with self.assertRaises(ValueError):
                collect_quotes(self.store, 'r', shanghai + shenzhen, self.cfg, fetch_fn=net)
        self.assertEqual(sorted(u.rsplit('=', 1)[1] for u in net.minute_calls()), shenzhen)
        self.assertEqual(sorted(r[0] for r in self.store.db.execute('SELECT symbol FROM quotes')), shanghai)
        self.assertEqual(sorted(e['kind'] for e in self.events()), ['NO_QUOTE', 'PRIMARY_DOWN'])

    def test_stop_checks_for_one_stock_share_a_request_at_most_once_a_minute(self):
        self.hold(stop=950)
        t = datetime(2026, 9, 15, 2, 0, tzinfo=timezone.utc)
        net = Endpoints(series=['1000 9.95 1 1', '1003 9.80 1 1'])
        for i in range(60):  # the held stock misses every other refresh for ten minutes
            at = (t + timedelta(seconds=10 * i)).isoformat()
            missing = [SYMBOL] if i % 2 == 0 else []
            qh.observe(self.store, self.cfg, at, missing, missing)
            if i % 6 == 5:  # the monitor's sweep, once a minute
                qh.sweep(self.store, self.cfg, at, fetch=net)
        qh.sweep(self.store, self.cfg, (t + timedelta(minutes=11)).isoformat(), fetch=net)
        checks = [c for e in self.events() if e['check_json'] for c in json.loads(e['check_json'])]
        self.assertEqual(len(checks), 30)  # every stretch is checked
        self.assertLessEqual(len(net.minute_calls()), 11)  # but with one request per minute at most

    def test_live_rule_starts_two_minutes_into_each_session(self):
        self.live.stop()
        cases = {'2026-09-15T09:31:00+08:00': False, '2026-09-15T09:32:00+08:00': True, '2026-09-15T11:29:59+08:00': True,
                 '2026-09-15T12:00:00+08:00': False, '2026-09-15T13:01:30+08:00': False, '2026-09-15T13:05:00+08:00': True,
                 '2026-09-15T14:57:00+08:00': False, '2026-09-19T10:00:00+08:00': False}
        self.assertEqual({k: qh.live(k) for k in cases}, cases)

    def test_a_slow_backup_never_holds_back_good_quotes(self):
        import time
        def net(url, **kw):
            if 'qt.gtimg.cn' in url:
                return batch(OTHER)
            time.sleep(0.5)
            return minute(SYMBOL)
        began = time.monotonic()
        with patch('ashare.quote_health.FALLBACK_DEADLINE', 0.1), patch('ashare.quote_health.fetch_many.__defaults__', (None, 0.1)):
            with self.assertRaises(ValueError):
                collect_quotes(self.store, 'r', [SYMBOL, OTHER], self.cfg, fetch_fn=net)
        self.assertLess(time.monotonic() - began, 0.45)
        self.assertEqual([r[0] for r in self.store.db.execute('SELECT symbol FROM quotes')], [OTHER])
        self.assertIn('超过', self.store.db.execute("SELECT detail FROM data_attempts WHERE symbol=? ORDER BY id DESC", (SYMBOL,)).fetchone()[0])

    def test_a_backup_request_in_flight_is_not_repeated_by_a_second_refresher(self):
        due, covered, failed = qh.reserve_fallback(self.store, [SYMBOL, OTHER], '2026-09-15T02:00:00+00:00')
        self.assertEqual((due, covered, failed), ([SYMBOL, OTHER], set(), set()))
        # Still being asked by the first refresher: not asked again, and not counted as having a quote.
        self.assertEqual(qh.reserve_fallback(self.store, [SYMBOL], '2026-09-15T02:00:05+00:00'), ([], set(), {SYMBOL}))
        qh.mark_fallback(self.store, {SYMBOL: False, OTHER: True}, '2026-09-15T02:00:00+00:00')
        self.assertEqual(qh.reserve_fallback(self.store, [SYMBOL, OTHER], '2026-09-15T02:00:30+00:00'), ([], {OTHER}, {SYMBOL}))
        self.assertEqual(qh.reserve_fallback(self.store, [SYMBOL], '2026-09-15T02:01:00+00:00')[0], [SYMBOL])


if __name__ == '__main__':
    unittest.main()
