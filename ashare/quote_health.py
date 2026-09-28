"""Quote-source health on the executing node.

The batch quote request (qt.gtimg.cn) is the only A-share price source. When it fails or leaves a symbol
out, each missing symbol is fetched from Tencent's minute endpoint instead (at most once a minute per
symbol, whether or not the last attempt worked; a quote stays usable for 90 seconds). That protects
against one endpoint failing or changing its format, not against Tencent blocking this server entirely.

Every outage is recorded as an event so the daily digest, evaluation batches and the LLM reviews can judge
whether a paid, stable feed is needed; nothing here alerts the user. When no quote at all was available
for held shares, the minute series is checked afterwards for a stop that would have triggered during the
gap. That check only records what happened: it never places or back-dates an order.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from .storage import digest, normalize_time
from .calendar import SH, local

MINUTE_URL = 'https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={symbol}'
FALLBACK_SECONDS = 60
FALLBACK_WORKERS = 4
# Refreshes only run in continuous trading, so a longer silence (lunch break, close, restart) ends an
# event at its last observation instead of stretching it across the pause.
GAP_SECONDS = 300
KINDS = {'PRIMARY_DOWN': '主行情接口失败，已改用分时接口', 'NO_QUOTE': '两个接口都没有取到报价'}
KEEP_DAYS = 8  # rows a research replica pulls; older ones stay on the executing node
COLUMNS = ('id', 'kind', 'started_at', 'last_seen_at', 'ended_at', 'symbols_json', 'held_json', 'detail', 'check_json')


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


def fetch_many(symbols, fetch=None):
    """{symbol: body or exception}, a few requests at a time so a slow endpoint cannot stall the refresh."""
    def one(symbol):
        try:
            return symbol, fetch_minute(symbol, fetch)
        except Exception as exc:
            return symbol, exc
    if len(symbols) <= 1:
        return dict(one(s) for s in symbols)
    with ThreadPoolExecutor(max_workers=FALLBACK_WORKERS, thread_name_prefix='quote-fallback') as pool:
        return dict(pool.map(one, symbols))


def _state(store, key, default):
    r = store.db.execute('SELECT value FROM service_state WHERE key=?', (key,)).fetchone()
    return json.loads(r[0]) if r else default


def _put(store, key, value):
    store.db.execute('INSERT OR REPLACE INTO service_state VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False, sort_keys=True)))


def _seconds(a, b):
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()


def fallback_due(store, symbol, at):
    """(due, last_ok): whether the minute endpoint may be asked now, and whether its last answer worked."""
    last = _state(store, 'quote_fallback_at', {}).get(symbol)
    if not isinstance(last, dict):
        return True, None
    return _seconds(last['at'], at) >= FALLBACK_SECONDS, bool(last.get('ok'))


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


def mark_fallback(store, results, at):
    """results: {symbol: True when the minute endpoint supplied a quote}."""
    if not results:
        return
    def update():
        marks = _state(store, 'quote_fallback_at', {})
        marks.update({s: {'at': at, 'ok': bool(ok)} for s, ok in results.items()})
        _put(store, 'quote_fallback_at', marks)
    _atomic(store, update)


def held_symbols(store):
    return sorted(r[0] for r in store.db.execute('SELECT DISTINCT symbol FROM paper_lots WHERE qty>0'))


def _end(store, kind, current, end, at, checks):
    """Close an event inside the caller's transaction. A NO_QUOTE gap with held shares is checked afterwards,
    outside the transaction, because it needs the network."""
    end = max(end, current['started_at'])  # two refreshers may report slightly out of order
    check = None
    if kind == 'NO_QUOTE' and current['held']:
        if local(end).date() != local(at).date():
            check = [{'symbol': s, 'error': '中断跨日后才恢复，分时数据已无法补查'} for s in current['held']]
        else:
            checks.append((current['id'], current['started_at'], end, current['held']))
    store.db.execute('UPDATE quote_health SET ended_at=?,last_seen_at=?,check_json=? WHERE id=?',
                     (end, max(current['last_seen'], end), json.dumps(check, ensure_ascii=False) if check is not None else None, current['id']))


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
                if current:
                    current.update(symbols=sorted(set(current['symbols']) | symbols),
                                   held=sorted(set(current['held']) | (symbols & held)), last_seen=at)
                    store.db.execute('UPDATE quote_health SET symbols_json=?,held_json=?,last_seen_at=? WHERE id=?',
                                     (json.dumps(current['symbols']), json.dumps(current['held']), at, current['id']))
                else:
                    eid = digest(kind + ':' + at)[:24]
                    current = {'id': eid, 'started_at': at, 'last_seen': at, 'symbols': sorted(symbols), 'held': sorted(symbols & held)}
                    store.db.execute('INSERT OR IGNORE INTO quote_health VALUES(?,?,?,?,?,?,?,?,?)',
                                     (eid, kind, at, at, None, json.dumps(current['symbols']), json.dumps(current['held']),
                                      (detail or KINDS[kind])[:300], None))
                    open_events[kind] = current
                changed.append(kind)
            elif current:
                _end(store, kind, current, at, at, checks)
                del open_events[kind]
                changed.append(kind)
        _put(store, 'quote_health_open', open_events)
        # Any other row left open (a crash between writes) ends at its last observation.
        ids = [e['id'] for e in open_events.values()]
        store.db.execute('UPDATE quote_health SET ended_at=last_seen_at WHERE ended_at IS NULL'
                         + (' AND id NOT IN (' + ','.join('?' * len(ids)) + ')' if ids else ''), ids)

    _atomic(store, update)
    for eid, start, end, symbols in checks:
        result = gap_check(store, config, start, end, symbols, fetch=fetch)
        with store.db:
            store.db.execute('UPDATE quote_health SET check_json=? WHERE id=?', (json.dumps(result, ensure_ascii=False), eid))
    return changed


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


def gap_check(store, config, start, end, symbols, fetch=None):
    """For each held symbol, the lowest and highest minute price while no quote was available, against its
    stops (breached) and its take-profit exit (exit_crossed)."""
    a, b = local(start), local(end)
    result = []
    bodies = fetch_many(list(symbols), fetch)
    for symbol in symbols:
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
    """Rows a research replica has not seen yet: everything observed at or after `since`, never older than
    KEEP_DAYS. An event still open is sent again each time, because it keeps changing."""
    floor = normalize_time((datetime.fromisoformat(normalize_time(at)) - timedelta(days=KEEP_DAYS)).isoformat())
    since = max(normalize_time(since), floor) if isinstance(since, str) else floor
    return [dict(r) for r in store.db.execute('SELECT * FROM quote_health WHERE last_seen_at>=? ORDER BY last_seen_at LIMIT ?', (since, limit))]


def upsert(store, rows):
    """Research replica: mirror the executing node's events (they change while open)."""
    for r in rows or []:
        if not isinstance(r, dict) or set(r) != set(COLUMNS) or r['kind'] not in KINDS:
            raise ValueError('行情健康记录字段不匹配')
        store.db.execute('INSERT OR REPLACE INTO quote_health VALUES(?,?,?,?,?,?,?,?,?)', tuple(r[c] for c in COLUMNS))


def summary(store, a, b):
    """Events overlapping [a, b]. Minutes run to the end, or for an event still open to its last
    observation, so a pause in trading is never counted as outage."""
    rows = [dict(r) for r in store.db.execute(
        'SELECT * FROM quote_health WHERE started_at<? AND COALESCE(ended_at,last_seen_at)>=? ORDER BY started_at', (b, a))]
    out = {'events': [], 'minutes': {k: 0 for k in KINDS}, 'held_minutes': 0, 'breaches': [], 'exits_missed': [],
           'check_errors': 0, 'open': 0}
    for r in rows:
        start, end = max(r['started_at'], a), min(r['ended_at'] or r['last_seen_at'], b)
        minutes = max(0, round(_seconds(start, end) / 60))
        held = json.loads(r['held_json'])
        check = json.loads(r['check_json']) if r['check_json'] else None
        out['minutes'][r['kind']] += minutes
        if r['kind'] == 'NO_QUOTE' and held:
            out['held_minutes'] += minutes
        if not r['ended_at']:
            out['open'] += 1
        for c in check or []:
            if c.get('error'):
                out['check_errors'] += 1
                continue
            if c.get('breached'):
                out['breaches'].append({'symbol': c['symbol'], 'low_cents': c['low_cents'], 'breached': c['breached'],
                                        'from': c['from'], 'to': c['to']})
            if c.get('exit_crossed'):
                out['exits_missed'].append({'symbol': c['symbol'], 'high_cents': c['high_cents'], 'from': c['from'], 'to': c['to']})
        out['events'].append({'kind': r['kind'], 'started_at': r['started_at'], 'ended_at': r['ended_at'], 'minutes': minutes,
                              'symbols': len(json.loads(r['symbols_json'])), 'held': held})
    return out
