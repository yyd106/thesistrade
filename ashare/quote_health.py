"""Quote-source health on the executing node.

The batch quote request (qt.gtimg.cn) is the only A-share price source. When it fails, leaves a symbol out,
or (in settled continuous trading) its whole answer has stopped updating, the affected symbols are fetched
from Tencent's minute endpoint instead: at most once a minute per symbol, whether or not the last attempt
worked, a few at a time and within an overall deadline. That protects against one endpoint failing or
changing its format, not against Tencent blocking this server entirely.

Every outage is recorded as an event so the daily digest, evaluation batches and the LLM reviews can judge
whether a paid, stable feed is needed; nothing here alerts the user. For each held stock the stretch with
no usable quote is tracked on its own and queued; the minute series is checked later, at most once a minute
per stock and away from the quote refresh, for a stop that would have triggered during that stretch. The
check only records what happened: it never places or back-dates an order.
"""
import json
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from .storage import digest, normalize_time, now
from .calendar import SH, local

MINUTE_URL = 'https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={symbol}'
FALLBACK_SECONDS = 60
FALLBACK_WORKERS = 4
FALLBACK_DEADLINE = 20  # seconds for all backup requests of one refresh together
# Refreshes only run in continuous trading, so a longer silence (lunch break, close, restart) ends an
# event at its last observation instead of stretching it across the pause.
GAP_SECONDS = 300
CHECK_SECONDS = 60  # stop checks for one stock run at most once a minute; its queued stretches share one request
KINDS = {'PRIMARY_DOWN': '主行情接口没有给出可用报价', 'NO_QUOTE': '主接口和备用接口都没有可用报价'}
KEEP_DAYS = 8  # how far back a research replica is sent rows; older ones stay on the executing node
COLUMNS = ('id', 'kind', 'started_at', 'last_seen_at', 'ended_at', 'symbols_json', 'held_json', 'detail', 'check_json', 'updated_at')


class PriceUnavailable(ValueError):
    """The source answered, but with no usable price (usually a suspended stock). Not an outage, and the
    minute endpoint would say the same, so no backup request is made."""


def quote_fields(symbol, v):
    """One quote row from Tencent's '~' field list (the same layout in both endpoints)."""
    from .sources import cents
    if len(v) < 38 or v[2] != symbol[2:]:
        raise ValueError('行情字段结构或证券标识不一致')
    observed = datetime.strptime(v[30], '%Y%m%d%H%M%S').replace(tzinfo=SH)
    if observed > datetime.now(SH) + timedelta(minutes=2):
        raise ValueError('行情时间在未来')
    price, prev = cents(v[3]), cents(v[4])
    if min(price, prev) <= 0:
        raise PriceUnavailable('行情价格无效/可能停牌，需复核')
    return {'symbol': symbol, 'name': v[1], 'price_cents': price, 'prev_close_cents': prev,
            'observed_at': normalize_time(observed.isoformat())}


def parse_minute(raw, symbol):
    """Latest quote and the day's minute prices from the minute endpoint.

    The latest price must sit within 21% of the previous close (the widest A-share daily limit), so a
    field read from the wrong position can never become a trading price."""
    from .sources import cents
    node = json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw)['data'][symbol]
    fields = node['qt'][symbol]
    if isinstance(fields, str):
        fields = fields.split('~')
    row = quote_fields(symbol, fields)
    if abs(row['price_cents'] - row['prev_close_cents']) * 100 > row['prev_close_cents'] * 21:
        raise ValueError('分时接口价格超出涨跌幅范围，拒绝使用')
    day = node['data'].get('date')
    minutes = []
    for entry in node['data'].get('data') or []:
        parts = str(entry).split()
        if len(parts) >= 2 and len(parts[0]) == 4 and parts[0].isdigit():
            try:
                minutes.append((parts[0], cents(parts[1])))
            except (ValueError, ArithmeticError):
                continue
    return row, {'date': day, 'minutes': minutes}


def fetch_minute(symbol, fetch=None):
    from . import sources
    return (fetch or sources.fetch)(MINUTE_URL.format(symbol=symbol), max_bytes=400000)


def fetch_many(symbols, fetch=None, deadline=FALLBACK_DEADLINE):
    """{symbol: body or exception}. A request still running at the deadline counts as failed; its thread
    finishes on its own (the fetch has its own timeouts) without holding up the caller."""
    if not symbols:
        return {}
    pool = ThreadPoolExecutor(max_workers=min(FALLBACK_WORKERS, len(symbols)), thread_name_prefix='quote-fallback')
    try:
        futures = {pool.submit(fetch_minute, s, fetch): s for s in symbols}
        done, _ = wait(futures, timeout=deadline)
        out = {}
        for future, symbol in futures.items():
            if future not in done:
                out[symbol] = TimeoutError(f'备用分时接口超过 {deadline} 秒未返回')
            elif future.exception() is not None:
                out[symbol] = future.exception()
            else:
                out[symbol] = future.result()
        return out
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _state(store, key, default):
    r = store.db.execute('SELECT value FROM service_state WHERE key=?', (key,)).fetchone()
    return json.loads(r[0]) if r else default


def _put(store, key, value):
    store.db.execute('INSERT OR REPLACE INTO service_state VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False, sort_keys=True)))


def _seconds(a, b):
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()


def age(row, at):
    return _seconds(row['observed_at'], at)


def live(at):
    """Continuous trading past the first two minutes of each session. Only then does a quote older than the
    usable age mean the source is not updating; just after 09:30 and 13:00 the auction or pre-lunch price
    is still the latest one."""
    from .calendar import phase
    if phase(at) != 'CONTINUOUS':
        return False
    hm = local(at).strftime('%H:%M')
    return '09:32' <= hm < '11:30' or '13:02' <= hm < '14:57'


def _atomic(store, fn):
    """Read-modify-write of shared state: the background refresher and the settle job may both observe.
    Inside a caller's transaction it simply joins it."""
    if store.db.in_transaction:
        return fn()
    store.db.execute('BEGIN IMMEDIATE')
    try:
        result = fn()
        store.db.commit()
        return result
    except BaseException:
        store.db.rollback()
        raise


def reserve_fallback(store, symbols, at):
    """Split symbols the batch could not supply into (due, covered, waiting).
    due: ask the minute endpoint now (reserved, so a concurrent refresher does not ask as well);
    covered: the last answer, under a minute ago, was a usable quote;
    waiting: asked under a minute ago without a usable quote yet (failed, or still being asked by another
    refresher). These have no usable quote now and are not asked again until the minute is up."""
    def update():
        marks = _state(store, 'quote_fallback_at', {})
        due, covered, waiting = [], set(), set()
        for s in symbols:
            last = marks.get(s) if isinstance(marks.get(s), dict) else None
            if last is None or _seconds(last['at'], at) >= FALLBACK_SECONDS:
                due.append(s)
                marks[s] = {'at': at, 'ok': bool(last and last.get('ok')), 'pending': True}
            elif last.get('ok'):
                covered.add(s)
            else:
                waiting.add(s)
        _put(store, 'quote_fallback_at', marks)
        return due, covered, waiting
    return _atomic(store, update)


def mark_fallback(store, results, at):
    """results: {symbol: True when the minute endpoint supplied a usable quote}."""
    if not results:
        return
    def update():
        marks = _state(store, 'quote_fallback_at', {})
        marks.update({s: {'at': at, 'ok': bool(ok)} for s, ok in results.items()})
        _put(store, 'quote_fallback_at', marks)
    _atomic(store, update)


def held_symbols(store):
    return sorted(r[0] for r in store.db.execute('SELECT DISTINCT symbol FROM paper_lots WHERE qty>0'))


def _hm(stamp):
    return local(stamp).strftime('%H:%M')


def _track(current, missing_held, at, checks):
    """Per held symbol, the runs of refreshes in which it had no usable quote. A run that ends is queued
    for its stop check."""
    spans = current.setdefault('spans', {})
    for s in missing_held:
        runs = spans.setdefault(s, [])
        if not runs or runs[-1][1] is not None:
            runs.append([at, None])
    for s, runs in spans.items():
        if runs and runs[-1][1] is None and s not in missing_held:
            runs[-1][1] = at
            checks.append([current['id'], s, runs[-1][0], at])


def _end(store, kind, current, end, at, checks):
    """Close an event inside the caller's transaction; its held stocks' last stretches are queued for checks."""
    end = max(end, current['started_at'])  # two refreshers may report slightly out of order
    cross_day = []
    for s, runs in current.get('spans', {}).items():
        if runs and runs[-1][1] is None:
            runs[-1][1] = end
            if local(end).date() == local(at).date():
                checks.append([current['id'], s, runs[-1][0], end])
            else:
                cross_day.append({'symbol': s, 'from': _hm(runs[-1][0]), 'to': _hm(end), 'error': '中断跨日后才核对，分时数据已无法补查'})
    row = store.db.execute('SELECT check_json FROM quote_health WHERE id=?', (current['id'],)).fetchone()
    existing = json.loads(row[0]) if row and row[0] else []
    store.db.execute('UPDATE quote_health SET ended_at=?,last_seen_at=?,held_json=?,check_json=?,updated_at=? WHERE id=?',
                     (end, max(current['last_seen'], end), json.dumps(current.get('spans', {})),
                      json.dumps(existing + cross_day, ensure_ascii=False) if existing or cross_day else None, now(), current['id']))


def _record_checks(store, eid, entries):
    def update():
        row = store.db.execute('SELECT check_json FROM quote_health WHERE id=?', (eid,)).fetchone()
        if row is None:
            return
        existing = json.loads(row[0]) if row[0] else []
        store.db.execute('UPDATE quote_health SET check_json=?,updated_at=? WHERE id=?',
                         (json.dumps(existing + entries, ensure_ascii=False), now(), eid))
    _atomic(store, update)


def _queue(store, checks):
    """Inside the caller's transaction: stretches waiting for their stop check."""
    if checks:
        _put(store, 'quote_checks_pending', _state(store, 'quote_checks_pending', []) + checks)


def _claim(store, at):
    """Inside the caller's transaction: take the queued stretches of every stock not checked in the last minute."""
    pending = _state(store, 'quote_checks_pending', [])
    if not pending:
        return []
    last = _state(store, 'quote_check_at', {})
    ready = {s for _, s, _, _ in pending if not last.get(s) or _seconds(last[s], at) >= CHECK_SECONDS}
    claimed = [c for c in pending if c[1] in ready]
    if claimed:
        _put(store, 'quote_checks_pending', [c for c in pending if c[1] not in ready])
        _put(store, 'quote_check_at', {**last, **{s: at for s in ready}})
    return claimed


def _run_checks(store, config, checks, fetch):
    """One minute-series request per stock covers all of its claimed stretches."""
    if not checks:
        return
    entries = gap_check(store, config, [(s, a, b) for _, s, a, b in checks], fetch=fetch)
    by_event = {}
    for (eid, _, _, _), entry in zip(checks, entries):
        by_event.setdefault(eid, []).append(entry)
    for eid, items in by_event.items():
        _record_checks(store, eid, items)


def observe(store, config, at, primary_failed, failed, detail='', fetch=None):
    """Open, extend or close outage events after one quote refresh. Research replicas do not record."""
    if (config or {}).get('deployment_role') == 'research':
        return []
    at = normalize_time(at)
    held = set(held_symbols(store))
    checks, changed = [], []

    def update():
        open_events = _state(store, 'quote_health_open', {})
        for kind, symbols in (('PRIMARY_DOWN', set(primary_failed)), ('NO_QUOTE', set(failed))):
            current = open_events.get(kind)
            if current and _seconds(current['last_seen'], at) > GAP_SECONDS:
                _end(store, kind, current, current['last_seen'], at, checks)
                del open_events[kind]
                current = None
                changed.append(kind)
            if symbols:
                if not current:
                    current = {'id': digest(kind + ':' + at)[:24], 'started_at': at, 'last_seen': at, 'symbols': [], 'spans': {}}
                    store.db.execute('INSERT OR IGNORE INTO quote_health VALUES(?,?,?,?,?,?,?,?,?,?)',
                                     (current['id'], kind, at, at, None, '[]', '{}', (detail or KINDS[kind])[:300], None, now()))
                    open_events[kind] = current
                current['symbols'] = sorted(set(current['symbols']) | symbols)
                current['last_seen'] = max(current['last_seen'], at)
                if kind == 'NO_QUOTE':
                    _track(current, symbols & held, at, checks)
                store.db.execute('UPDATE quote_health SET symbols_json=?,held_json=?,last_seen_at=?,updated_at=? WHERE id=?',
                                 (json.dumps(current['symbols']), json.dumps(current.get('spans', {})), current['last_seen'], now(), current['id']))
                changed.append(kind)
            elif current:
                _end(store, kind, current, at, at, checks)
                del open_events[kind]
                changed.append(kind)
        _put(store, 'quote_health_open', open_events)
        _queue(store, checks)
        _heal(store, open_events)

    _atomic(store, update)
    return changed


def _heal(store, open_events):
    """Any other row left open (a crash between writes) ends at its last observation."""
    ids = [e['id'] for e in open_events.values()]
    store.db.execute('UPDATE quote_health SET ended_at=last_seen_at,updated_at=? WHERE ended_at IS NULL'
                     + (' AND id NOT IN (' + ','.join('?' * len(ids)) + ')' if ids else ''), [now(), *ids])


def sweep(store, config, at=None, fetch=None):
    """Run once a minute by the market monitor, apart from the quote refresh. Ends events idle past the gap
    (lunch break, after the close, a stalled refresher) and runs the queued stop checks that are due, while
    the day's minute series is still available. Returns the kinds of the events it ended."""
    if (config or {}).get('deployment_role') == 'research':
        return []
    if not _state(store, 'quote_health_open', {}) and not _state(store, 'quote_checks_pending', []):
        return []
    at = normalize_time(at or now())
    queued, ended = [], []

    def update():
        open_events = _state(store, 'quote_health_open', {})
        for kind in list(open_events):
            current = open_events[kind]
            if _seconds(current['last_seen'], at) > GAP_SECONDS:
                _end(store, kind, current, current['last_seen'], at, queued)
                del open_events[kind]
                ended.append(kind)
        if ended:
            _put(store, 'quote_health_open', open_events)
        _queue(store, queued)
        return _claim(store, at)

    _run_checks(store, config, _atomic(store, update), fetch)
    return ended


def stop_levels(store, config, symbol, at):
    """The prices at which the cost stop and the active plan's stop would trigger for the shares held now
    (the same inequalities as slots.hard_reason), plus the plan's take-profit exit."""
    from .paper import positions
    p = positions(store, at).get(symbol) or {}
    levels = {}
    qty, cost = p.get('qty') or 0, p.get('cost_cents') or 0
    if qty and cost:
        bps = config.get('paper_stop_loss_bps', 600)
        levels['cost_stop_cents'] = int((cost * (10000 - bps) // 10000 - (p.get('dividend_cents') or 0)) // qty)
    plan = store.db.execute("SELECT payload_json FROM plans WHERE symbol=? AND status='ACTIVE' ORDER BY activated_at DESC LIMIT 1", (symbol,)).fetchone()
    if plan:
        plan_levels = json.loads(plan[0]).get('levels') or {}
        if plan_levels.get('stop_cents'):
            levels['plan_stop_cents'] = plan_levels['stop_cents']
        if plan_levels.get('sell_cents'):
            levels['plan_exit_cents'] = plan_levels['sell_cents']
    return levels


def gap_check(store, config, items, fetch=None):
    """items: [(symbol, start, end)], each a stretch in which that held symbol had no usable quote. For each,
    the lowest and highest minute price inside the stretch, against its stops (breached) and its take-profit
    exit (exit_crossed)."""
    bodies = fetch_many(sorted({s for s, _, _ in items}), fetch)
    result = []
    for symbol, start, end in items:
        a, b = local(start), local(end)
        entry = {'symbol': symbol, 'from': a.strftime('%H:%M'), 'to': b.strftime('%H:%M')}
        try:
            body = bodies[symbol]
            if isinstance(body, Exception):
                raise body
            _, series = parse_minute(body, symbol)
            if series['date'] != a.strftime('%Y%m%d'):
                raise ValueError('分时数据不是中断当天的')
            inside = [c for hhmm, c in series['minutes'] if a.strftime('%H%M') <= hhmm <= b.strftime('%H%M')]
            if not inside:
                raise ValueError('中断时段没有分时数据')
            low, high = min(inside), max(inside)
            levels = stop_levels(store, config, symbol, end)
            entry.update(low_cents=low, high_cents=high, minutes=len(inside), **levels)
            entry['breached'] = sorted(k for k in ('cost_stop_cents', 'plan_stop_cents') if k in levels and low <= levels[k])
            entry['exit_crossed'] = 'plan_exit_cents' in levels and high >= levels['plan_exit_cents']
        except Exception as exc:
            entry['error'] = f'{type(exc).__name__}: {str(exc)[:160]}'
        result.append(entry)
    return result


def changed_since(store, since, at, limit=500):
    """Rows a research replica has not seen yet: every row written at or after `since` (updated_at is the
    executing node's clock at each write), never older than KEEP_DAYS."""
    floor = normalize_time((datetime.fromisoformat(normalize_time(at)) - timedelta(days=KEEP_DAYS)).isoformat())
    since = max(normalize_time(since), floor) if isinstance(since, str) else floor
    return [dict(r) for r in store.db.execute('SELECT * FROM quote_health WHERE updated_at>=? ORDER BY updated_at LIMIT ?', (since, limit))]


def upsert(store, rows):
    """Research replica: mirror the executing node's events (they change while open and when checked)."""
    for r in rows or []:
        if not isinstance(r, dict) or set(r) != set(COLUMNS) or r['kind'] not in KINDS:
            raise ValueError('行情健康记录字段不匹配')
        store.db.execute('INSERT OR REPLACE INTO quote_health VALUES(?,?,?,?,?,?,?,?,?,?)', tuple(r[c] for c in COLUMNS))


def _union_seconds(intervals):
    total, cur = 0.0, None
    for a, b in sorted(intervals):
        if cur and a <= cur[1]:
            cur[1] = max(cur[1], b)
            continue
        if cur:
            total += _seconds(*cur)
        cur = [a, b]
    if cur:
        total += _seconds(*cur)
    return total


def summary(store, a, b):
    """Events overlapping [a, b]. Durations run to the end, or for an event still open to its last
    observation, so a pause in trading is never counted as outage. Seconds are added up before rounding,
    so many short outages still count; held minutes are the time at least one held stock had no quote."""
    rows = [dict(r) for r in store.db.execute(
        'SELECT * FROM quote_health WHERE started_at<? AND COALESCE(ended_at,last_seen_at)>=? ORDER BY started_at', (b, a))]
    seconds = {k: 0.0 for k in KINDS}
    held = []
    out = {'events': [], 'breaches': [], 'exits_missed': [], 'check_errors': 0, 'open': 0}
    for r in rows:
        start, end = max(r['started_at'], a), min(r['ended_at'] or r['last_seen_at'], b)
        secs = max(0.0, _seconds(start, end))
        seconds[r['kind']] += secs
        spans = json.loads(r['held_json'] or '{}')
        if r['kind'] == 'NO_QUOTE':
            for runs in spans.values():
                for f, t in runs:
                    f, t = max(f, a), min(t or r['last_seen_at'], b)
                    if t > f:
                        held.append((f, t))
        if not r['ended_at']:
            out['open'] += 1
        for c in json.loads(r['check_json']) if r['check_json'] else []:
            if c.get('error'):
                out['check_errors'] += 1
                continue
            if c.get('breached'):
                out['breaches'].append({'symbol': c['symbol'], 'low_cents': c['low_cents'], 'breached': c['breached'],
                                        'from': c['from'], 'to': c['to']})
            if c.get('exit_crossed'):
                out['exits_missed'].append({'symbol': c['symbol'], 'high_cents': c['high_cents'], 'from': c['from'], 'to': c['to']})
        out['events'].append({'kind': r['kind'], 'started_at': r['started_at'], 'ended_at': r['ended_at'], 'minutes': round(secs / 60),
                              'symbols': len(json.loads(r['symbols_json'])), 'held': sorted(spans)})
    out['minutes'] = {k: round(v / 60) for k, v in seconds.items()}
    out['held_minutes'] = round(_union_seconds(held) / 60)
    return out
