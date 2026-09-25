"""Conclusion registry: every research, portfolio and global judgment becomes a dated prediction
that the program scores after a fixed horizon, without any model involvement.

Scoring uses daily bars only. Entry is the first trading day's open after the data the judgment
saw; exit is the close `horizon` trading days later; excess is measured against CSI 300 over the
same dates. Samples are de-duplicated to one per symbol per entry day before any statistics.
"""
import json
import math
import statistics
from datetime import datetime, timedelta
from .storage import now, normalize_time
from .calendar import last_completed_day, completed_bar_cutoff

BENCHMARK = 'sh000300'
# Blockers that come from the model's judgment, as opposed to data completeness or price rules.
RESEARCH_BLOCKERS = ('RESEARCH_VETO', 'MODEL_NOT_READY')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def horizon(config):
    return config.get('evaluation_horizon_days', 20)


def _insert(store, rid, route, symbol, source_id, created_at, as_of_day, build_id, days, benchmark, judgment):
    store.db.execute('INSERT OR IGNORE INTO signal_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (rid, route, symbol, source_id, created_at, as_of_day, build_id, days, benchmark, 'OPEN',
                      encode(judgment), None, None))


def register_plan(store, config, plan_id, symbol, at, plan, stock_result):
    """Watchlist plan. Caller owns the transaction. Records what the mechanical rule said, independent
    of whether the model vetoed it, so the veto itself can be scored."""
    basis = plan.get('basis') or {}
    levels = plan.get('levels') or {}
    ma20, ma60, close = (basis.get(k) for k in ('ma20_cents', 'ma60_cents', 'close_cents'))
    numeric = all(type(v) is int and v > 0 for v in (ma20, ma60, close))
    trend = bool(ma20 > ma60 and close >= ma60) if numeric else None
    in_band = bool(levels) and numeric and levels['buy_low_cents'] <= close <= levels['buy_high_cents']
    blockers = list(plan.get('blockers', []))
    judgment = {'model_action': (stock_result or {}).get('action'), 'plan_kind': plan.get('kind'),
                'blockers': blockers, 'trend_ok': trend, 'close_in_band': in_band,
                'research_blocked': any(b in RESEARCH_BLOCKERS for b in blockers),
                'data_blocked': any(b not in RESEARCH_BLOCKERS + ('TREND_NOT_CONFIRMED',) for b in blockers),
                'levels': levels, 'renewed': bool(plan.get('research_reuse')),
                'model': (plan.get('model') or {}).get('actual_model') or (plan.get('model') or {}).get('requested_model')}
    _insert(store, 'watchlist:' + plan_id, 'watchlist', symbol, plan_id, normalize_time(at), basis.get('last_complete_date'),
            (plan.get('build') or {}).get('build_id'), horizon(config), BENCHMARK, judgment)


def register_portfolio(store, config, decision_id, created_at, payload):
    """One row per candidate the portfolio layer actually had a choice on (can_increase or held)."""
    day = last_completed_day(created_at)
    build_id = (payload.get('build') or {}).get('build_id')
    for d in payload.get('decisions', []):
        if d.get('route') not in ('watchlist', 'global'):
            continue
        if not d.get('can_increase') and not d.get('qty'):
            continue
        judgment = {'action': d['action'], 'target_bps': d['target_bps'], 'current_bps': d.get('current_bps'),
                    'can_increase': d.get('can_increase'), 'held': bool(d.get('qty')), 'source_id': d.get('source_id'),
                    'key': d['key']}
        _insert(store, 'portfolio:' + decision_id + ':' + d['key'], 'portfolio', d['symbol'], decision_id,
                normalize_time(created_at), day if d['route'] == 'watchlist' else created_at[:10], build_id,
                horizon(config), BENCHMARK if d['route'] == 'watchlist' else None, judgment)


def register_global(store, config, plan_id, symbol, at, payload):
    analysis = payload.get('analysis') or {}
    blockers = payload.get('blockers', [])
    judgment = {'stance': analysis.get('stance'), 'plan_kind': payload.get('kind'), 'blockers': blockers,
                'trend_ok': not any('趋势条件未成立' in b for b in blockers), 'model_status': payload.get('model_status'),
                'holding_days': payload.get('holding_days')}
    days = payload.get('holding_days') if analysis.get('stance') == 'LONG' else 5
    _insert(store, 'global:' + plan_id, 'global', symbol, plan_id, normalize_time(at), normalize_time(at)[:10],
            (payload.get('build') or {}).get('build_id'), int(days or 5), None, judgment)


def backfill(store, config):
    """Register judgments made before the registry existed, from the records they left behind.
    Each row keeps its original time, so scoring stays point-in-time. Idempotent."""
    counts = {'watchlist': 0, 'portfolio': 0, 'global': 0}
    with store.db:
        for p in store.db.execute("SELECT p.*,s.result_json FROM plans p JOIN studies s ON s.id=p.study_id WHERE p.status!='DRAFT' AND json_extract(p.payload_json,'$.kind')!='RISK_EXIT_ONLY'").fetchall():
            stock = next(iter(json.loads(p['result_json']).get('stocks', [])), {})
            before = store.db.total_changes
            register_plan(store, config, p['id'], p['symbol'], p['activated_at'], json.loads(p['payload_json']), stock)
            counts['watchlist'] += store.db.total_changes - before
        for d in store.db.execute('SELECT * FROM portfolio_decisions').fetchall():
            before = store.db.total_changes
            register_portfolio(store, config, d['id'], d['created_at'], json.loads(d['payload_json']))
            counts['portfolio'] += store.db.total_changes - before
        for g in store.db.execute("SELECT * FROM global_plans").fetchall():
            payload = json.loads(g['payload_json'])
            if payload.get('model_status') == 'DEFERRED':
                continue
            before = store.db.total_changes
            register_global(store, config, g['id'], g['symbol'], g['created_at'], payload)
            counts['global'] += store.db.total_changes - before
    return counts


# ---------------------------------------------------------------- price history

def _raw_bars(store, symbol, at):
    """Latest forward-adjusted daily bars for a watchlist symbol: {date: (open, close, high, low)}."""
    row = store.db.execute("SELECT raw_path FROM market_features WHERE symbol=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (symbol,)).fetchone() \
        if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='market_features'").fetchone() else None
    if not row or not row[0]:
        return {}
    try:
        obj = json.loads((store.root / row[0]).read_bytes())
        series = obj['data'][symbol]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    cutoff = completed_bar_cutoff(at)
    out = {}
    for b in series.get('qfqday') or series.get('day') or []:
        try:
            if b[0] < cutoff:
                out[b[0]] = tuple(float(x) for x in (b[1], b[2], b[3], b[4]))
        except (IndexError, ValueError, TypeError):
            continue
    return out


def _benchmark_bars(store):
    row = store.db.execute('SELECT payload_json FROM comparison_series WHERE symbol=? ORDER BY ready_at DESC,rowid DESC LIMIT 1', (BENCHMARK,)).fetchone()
    if not row:
        return {}
    return {b['date']: (b['open'], b['close'], b['high'], b['low']) for b in json.loads(row[0]).get('bars', [])}


def _global_bars(store, symbol):
    row = store.db.execute('SELECT payload_json FROM global_market WHERE symbol=?', (symbol,)).fetchone()
    if not row:
        return {}
    return {b['date']: b['price_micros'] for b in json.loads(row[0]).get('bars', [])}


def _window(bars, as_of_day, days):
    dates = sorted(d for d in bars if d > (as_of_day or ''))
    if len(dates) < days:
        return None
    return dates[:days]


def score_row(row, bars, bench):
    """Returns a score dict, None if the horizon has not elapsed yet."""
    days = _window(bars, row['as_of_day'], row['horizon_days'])
    if not days:
        return None
    entry, exit_ = bars[days[0]][0], bars[days[-1]][1]
    if entry <= 0:
        return {'status': 'UNSCORABLE', 'reason': '入场价格无效'}
    ret = (exit_ / entry - 1) * 10000
    highs = [bars[d][2] for d in days]
    lows = [bars[d][3] for d in days]
    result = {'entry_date': days[0], 'exit_date': days[-1], 'entry': entry, 'exit': exit_,
              'return_bps': round(ret, 1), 'max_up_bps': round((max(highs) / entry - 1) * 10000, 1),
              'max_down_bps': round((min(lows) / entry - 1) * 10000, 1)}
    if row['benchmark']:
        if days[0] not in bench or days[-1] not in bench:
            return {'status': 'UNSCORABLE', 'reason': '基准缺少同区间行情', **result}
        b = (bench[days[-1]][1] / bench[days[0]][0] - 1) * 10000
        result.update(benchmark_return_bps=round(b, 1), excess_bps=round(ret - b, 1))
    else:
        result['excess_bps'] = result['return_bps']
    return {'status': 'SCORED', **result}


def score_global_row(row, closes):
    dates = sorted(d for d in closes if d > (row['as_of_day'] or ''))
    if len(dates) <= row['horizon_days']:
        return None
    entry, exit_ = closes[dates[0]], closes[dates[row['horizon_days']]]
    ret = (exit_ / entry - 1) * 10000
    return {'status': 'SCORED', 'entry_date': dates[0], 'exit_date': dates[row['horizon_days']],
            'return_bps': round(ret, 1), 'excess_bps': round(ret, 1), 'basis': '美元收盘价，未计汇率'}


def score(store, config, at=None):
    """Score every open registry row whose horizon has elapsed. Idempotent; model-free."""
    at = normalize_time(at or now())
    bench = _benchmark_bars(store)
    cache = {}
    scored = pending = unscorable = 0
    stale = normalize_time((datetime.fromisoformat(at) - timedelta(days=120)).isoformat())
    rows = store.db.execute("SELECT * FROM signal_registry WHERE status='OPEN' ORDER BY created_at").fetchall()
    with store.db:
        for row in rows:
            if row['route'] == 'global' or (row['route'] == 'portfolio' and not row['benchmark']):
                key = ('g', row['symbol'])
                if key not in cache:
                    cache[key] = _global_bars(store, row['symbol'])
                result = score_global_row(row, cache[key]) if cache[key] else None
            else:
                key = ('a', row['symbol'])
                if key not in cache:
                    cache[key] = _raw_bars(store, row['symbol'], at)
                result = score_row(row, cache[key], bench) if cache[key] and row['as_of_day'] else None
            if result is None:
                if row['created_at'] < stale:
                    result = {'status': 'UNSCORABLE', 'reason': '超过120天仍缺少可用行情'}
                else:
                    pending += 1
                    continue
            status = result.pop('status')
            store.db.execute('UPDATE signal_registry SET status=?,score_json=?,scored_at=? WHERE id=?',
                             (status, encode(result), at, row['id']))
            scored += status == 'SCORED'
            unscorable += status == 'UNSCORABLE'
    return {'scored': scored, 'pending': pending, 'unscorable': unscorable}


# ---------------------------------------------------------------- statistics

def daily_samples(rows):
    """One sample per (route, symbol, as-of day): the last judgment made on data up to that day."""
    latest = {}
    for r in rows:
        key = (r['route'], r['symbol'], r['as_of_day'])
        if key not in latest or r['created_at'] > latest[key]['created_at']:
            latest[key] = r
    return list(latest.values())


def non_overlapping(samples, days):
    """Per symbol, keep samples whose holding windows do not overlap (every `days` trading days)."""
    kept = []
    by_symbol = {}
    for s in sorted(samples, key=lambda s: (s['symbol'], s['score']['entry_date'])):
        last = by_symbol.get(s['symbol'])
        if last is None or s['score']['entry_date'] > last['score']['exit_date']:
            kept.append(s)
            by_symbol[s['symbol']] = s
    return kept


def describe(samples, field='excess_bps'):
    values = [s['score'][field] for s in samples if s['score'].get(field) is not None]
    n = len(values)
    if not n:
        return {'n': 0}
    mean = sum(values) / n
    sd = statistics.stdev(values) if n > 1 else None
    half = 1.96 * sd / math.sqrt(n) if sd is not None else None
    return {'n': n, 'mean_bps': round(mean, 1), 'median_bps': round(statistics.median(values), 1),
            'hit_rate': round(sum(v > 0 for v in values) / n, 3),
            'ci95_bps': [round(mean - half, 1), round(mean + half, 1)] if half is not None else None}


def load(store, route, since=None):
    sql = "SELECT * FROM signal_registry WHERE route=? AND status='SCORED'" + (' AND created_at>=?' if since else '')
    rows = []
    for r in store.db.execute(sql, (route, since) if since else (route,)):
        rows.append({**dict(r), 'judgment': json.loads(r['judgment_json']), 'score': json.loads(r['score_json'])})
    return rows


def comparisons(store, config, since=None):
    """The fixed comparisons the weekly report tracks. Each group lists n, mean, CI and hit rate for
    daily samples plus the non-overlapping subset, which is the honest sample size."""
    days = horizon(config)
    out = {}

    def group(name, samples, split, labels):
        result = {}
        for label in labels:
            chosen = [s for s in samples if split(s) == label]
            result[label] = {'daily': describe(chosen), 'independent': describe(non_overlapping(chosen, days))}
        out[name] = result

    watch = daily_samples(load(store, 'watchlist', since))
    group('trend_filter', watch, lambda s: 'trend_ok' if s['judgment'].get('trend_ok') else 'trend_fail', ('trend_ok', 'trend_fail'))
    trend = [s for s in watch if s['judgment'].get('trend_ok')]
    group('research_veto', trend, lambda s: 'WATCH' if s['judgment'].get('model_action') == 'WATCH' else 'NOT_WATCH', ('WATCH', 'NOT_WATCH'))
    choice = [s for s in daily_samples(load(store, 'portfolio', since)) if s['judgment'].get('can_increase') and s['benchmark']]
    group('portfolio_allow', choice, lambda s: 'ALLOW' if s['judgment']['action'] == 'ALLOW' else 'NOT_ALLOW', ('ALLOW', 'NOT_ALLOW'))
    glob = [s for s in daily_samples(load(store, 'global', since)) if s['judgment'].get('trend_ok')]
    group('global_stance', glob, lambda s: s['judgment'].get('stance') or 'UNKNOWN', ('LONG', 'WAIT'))
    return {'horizon_days': days, 'groups': out,
            'notice': '每日样本同一标的相邻日期的持有期高度重叠，只能看方向；“independent”为持有期不重叠的子样本，是可信的样本量。独立样本少于30时不下结论。'}


def registry_counts(store):
    return {f"{r['route']}:{r['status']}": r['n'] for r in store.db.execute('SELECT route,status,count(*) n FROM signal_registry GROUP BY route,status')}
