"""Conclusion registry: every research, portfolio and global judgment becomes a dated prediction
that the program scores after a fixed horizon, without any model involvement.

Scoring uses daily bars only. Entry is the first trading open after the judgment was available
and its data day; exit is the close of the `horizon`th day; excess is measured against CSI 300 over the
same dates. Samples are de-duplicated to one per symbol per entry day before any statistics.
"""
import json
import math
import statistics
from datetime import date, datetime, timedelta
from .storage import now, normalize_time
from .calendar import last_completed_day, completed_bar_cutoff, trading_day, local

BENCHMARK = 'sh000300'
SCORE_METHOD = 'decision-time-v2'
# Blockers that come from the model's judgment, as opposed to data completeness or price rules.
RESEARCH_BLOCKERS = ('RESEARCH_VETO', 'MODEL_NOT_READY')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def horizon(config):
    return config.get('evaluation_horizon_days', 20)


def _insert(store, rid, route, symbol, source_id, created_at, as_of_day, build_id, days, benchmark, judgment, *, research=None, historical=False):
    inserted = store.db.execute('INSERT OR IGNORE INTO signal_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (rid, route, symbol, source_id, created_at, as_of_day, build_id, days, benchmark, 'OPEN',
                      encode(judgment), None, None)).rowcount
    if inserted and not historical:
        from .judgments import freeze
        freeze(store, identity=rid, route=route, symbol=symbol, source_id=source_id, at=created_at,
               build_id=build_id, days=days, benchmark=benchmark, judgment=judgment, research=research)


def register_plan(store, config, plan_id, symbol, at, plan, stock_result, *, historical=False):
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
            (plan.get('build') or {}).get('build_id'), horizon(config), BENCHMARK, judgment,
            research=stock_result or plan, historical=historical)


def register_portfolio(store, config, decision_id, created_at, payload, *, historical=False):
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
                horizon(config), BENCHMARK if d['route'] == 'watchlist' else None, judgment,
                research={'thesis': d.get('reason'), 'evidence_ids': d.get('evidence_ids', [])}, historical=historical)


def register_global(store, config, plan_id, symbol, at, payload, *, historical=False):
    analysis = payload.get('analysis') or {}
    blockers = payload.get('blockers', [])
    judgment = {'stance': analysis.get('stance'), 'plan_kind': payload.get('kind'), 'blockers': blockers,
                'trend_ok': not any('趋势条件未成立' in b for b in blockers), 'model_status': payload.get('model_status'),
                'holding_days': payload.get('holding_days')}
    days = payload.get('holding_days') if analysis.get('stance') == 'LONG' else 5
    _insert(store, 'global:' + plan_id, 'global', symbol, plan_id, normalize_time(at), normalize_time(at)[:10],
            (payload.get('build') or {}).get('build_id'), int(days or 5), None, judgment,
            research=analysis, historical=historical)


def backfill(store, config):
    """Register judgments made before the registry existed, from the records they left behind.
    Each row keeps its original time, so scoring stays point-in-time. Idempotent."""
    counts = {'watchlist': 0, 'portfolio': 0, 'global': 0}
    with store.db:
        for p in store.db.execute("SELECT p.*,s.result_json FROM plans p JOIN studies s ON s.id=p.study_id WHERE p.status!='DRAFT' AND json_extract(p.payload_json,'$.kind')!='RISK_EXIT_ONLY'").fetchall():
            stock = next(iter(json.loads(p['result_json']).get('stocks', [])), {})
            before = store.db.total_changes
            register_plan(store, config, p['id'], p['symbol'], p['activated_at'], json.loads(p['payload_json']), stock, historical=True)
            counts['watchlist'] += store.db.total_changes - before
        for d in store.db.execute('SELECT * FROM portfolio_decisions').fetchall():
            before = store.db.total_changes
            register_portfolio(store, config, d['id'], d['created_at'], json.loads(d['payload_json']), historical=True)
            counts['portfolio'] += store.db.total_changes - before
        for g in store.db.execute("SELECT * FROM global_plans").fetchall():
            payload = json.loads(g['payload_json'])
            if payload.get('model_status') == 'DEFERRED':
                continue
            before = store.db.total_changes
            register_global(store, config, g['id'], g['symbol'], g['created_at'], payload, historical=True)
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


def _window(bars, as_of_day, days, created_at):
    # A daily open is usable only after the judgment was actually available.
    available = normalize_time(created_at)
    if not bars:
        return None
    first = max(local(available).date(), date.fromisoformat(as_of_day) + timedelta(days=1))
    dates, last = [], max(bars)
    while first.isoformat() <= last and len(dates) < days:
        day = first.isoformat()
        if trading_day(first) is True and normalize_time(day + 'T09:30:00+08:00') > available:
            dates.append(day)
        first += timedelta(days=1)
    # A missing day must not silently shift entry or lengthen the holding window.
    return dates if len(dates) == days and all(d in bars for d in dates) else None


def score_row(row, bars, bench):
    """Returns a score dict, None if the horizon has not elapsed yet."""
    days = _window(bars, row['as_of_day'], row['horizon_days'], row['created_at'])
    if not days:
        return None
    entry, exit_ = bars[days[0]][0], bars[days[-1]][1]
    if entry <= 0:
        return {'status': 'UNSCORABLE', 'reason': '入场价格无效'}
    ret = (exit_ / entry - 1) * 10000
    highs = [bars[d][2] for d in days]
    lows = [bars[d][3] for d in days]
    result = {'entry_date': days[0], 'exit_date': days[-1], 'entry': entry, 'exit': exit_,
              'entry_at': normalize_time(days[0] + 'T09:30:00+08:00'),
              'decision_available_at': normalize_time(row['created_at']), 'method': SCORE_METHOD,
              'return_bps': round(ret, 1), 'max_up_bps': round((max(highs) / entry - 1) * 10000, 1),
              'max_down_bps': round((min(lows) / entry - 1) * 10000, 1)}
    if row['benchmark']:
        if days[0] not in bench or days[-1] not in bench:
            return None  # Missing benchmark may arrive later; do not finalize a false failure.
        b = (bench[days[-1]][1] / bench[days[0]][0] - 1) * 10000
        result.update(benchmark_return_bps=round(b, 1), excess_bps=round(ret - b, 1))
    else:
        result['excess_bps'] = result['return_bps']
    return {'status': 'SCORED', **result}


def score_global_row(row, closes):
    dates = sorted(d for d in closes if d > max(row['as_of_day'] or '', normalize_time(row['created_at'])[:10]))
    if len(dates) <= row['horizon_days']:
        return None
    entry, exit_ = closes[dates[0]], closes[dates[row['horizon_days']]]
    ret = (exit_ / entry - 1) * 10000
    return {'status': 'SCORED', 'entry_date': dates[0], 'exit_date': dates[row['horizon_days']],
            'return_bps': round(ret, 1), 'excess_bps': round(ret, 1), 'method': SCORE_METHOD,
            'decision_available_at': normalize_time(row['created_at']), 'basis': '美元收盘价，未计汇率和费用；预测诊断，非策略收益'}


def score(store, config, at=None):
    """Score every open registry row whose horizon has elapsed. Idempotent; model-free."""
    at = normalize_time(at or now())
    bench = _benchmark_bars(store)
    cache = {}
    scored = pending = unscorable = 0
    stale = normalize_time((datetime.fromisoformat(at) - timedelta(days=120)).isoformat())
    rows = store.db.execute('''SELECT r.* FROM signal_registry r WHERE NOT EXISTS
        (SELECT 1 FROM signal_scores s WHERE s.signal_id=r.id AND s.method=?) ORDER BY created_at''', (SCORE_METHOD,)).fetchall()
    with store.db:
        for row in rows:
            if row['route'] == 'global' or (row['route'] == 'portfolio' and not row['benchmark']):
                key = ('g', row['symbol'])
                if key not in cache:
                    cache[key] = {d:v for d,v in _global_bars(store, row['symbol']).items() if d < at[:10]}
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
            # The original registry and any v1 score remain untouched for audit/reversion.
            store.db.execute('INSERT OR IGNORE INTO signal_scores VALUES(?,?,?,?,?)',
                             (row['id'], SCORE_METHOD, status, encode(result), at))
            scored += status == 'SCORED'
            unscorable += status == 'UNSCORABLE'
    return {'scored': scored, 'pending': pending, 'unscorable': unscorable}


# ---------------------------------------------------------------- statistics

def daily_samples(rows):
    """One sample per route/symbol/eligible entry: last available judgment wins."""
    latest = {}
    for r in rows:
        key = (r['route'], r['symbol'], r['score']['entry_date'])
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


def time_clusters(samples):
    """Connected holding windows across ALL symbols share a time cluster.

    A cluster is not asserted independent. This conservative diagnostic withholds
    intervals for short histories rather than treating correlated stocks as replications.
    """
    end, cluster = '', -1
    result = []
    for s in sorted(samples, key=lambda s: (s['score']['entry_date'], s['symbol'])):
        if s['score']['entry_date'] > end:
            cluster += 1
        end = max(end, s['score']['exit_date'])
        result.append({**s, 'time_cluster': cluster})
    return result


def describe(samples, field='excess_bps'):
    samples = [s for s in samples if s['score'].get(field) is not None]
    values = [s['score'][field] for s in samples]
    n = len(values)
    if not n:
        return {'n': 0}
    mean = sum(values) / n
    residuals = {}
    for s in samples:
        # Missing cluster metadata must never fabricate independent observations.
        k = s.get('time_cluster', 0)
        residuals[k] = residuals.get(k, 0) + s['score'][field] - mean
    clusters = len(residuals)
    half = 1.96 * math.sqrt(clusters / (clusters - 1) * sum(v*v for v in residuals.values()) / n**2) if clusters >= 30 else None
    return {'n': n, 'mean_bps': round(mean, 1), 'median_bps': round(statistics.median(values), 1),
            'time_clusters': clusters,
            'hit_rate': round(sum(v > 0 for v in values) / n, 3),
            'ci95_bps': [round(mean - half, 1), round(mean + half, 1)] if half is not None else None}


def load(store, route, since=None):
    sql = """SELECT r.*,s.score_json current_score FROM signal_registry r JOIN signal_scores s
        ON s.signal_id=r.id AND s.method=? WHERE r.route=? AND s.status='SCORED'""" + (' AND r.created_at>=?' if since else '')
    rows = []
    for r in store.db.execute(sql, (SCORE_METHOD, route, since) if since else (SCORE_METHOD, route)):
        rows.append({**dict(r), 'judgment': json.loads(r['judgment_json']), 'score': json.loads(r['current_score'])})
    return rows


def comparisons(store, config, since=None):
    """Descriptive comparisons. Select windows BEFORE any comparison or build split."""
    days = horizon(config)
    out = {}

    cohorts = {route: time_clusters(daily_samples(load(store, route, since)))
               for route in ('watchlist', 'portfolio', 'global')}
    kept_ids = {s['id'] for samples in cohorts.values() for s in non_overlapping(samples, days)}

    def group(name, samples, split, labels):
        result = {}
        for label in labels:
            chosen = [s for s in samples if split(s) == label]
            result[label] = {'daily': describe(chosen), 'non_overlapping': describe([s for s in chosen if s['id'] in kept_ids])}
        out[name] = result

    def groups(build=None):
        out.clear()
        selected = {r: [s for s in ss if build is None or (s.get('build_id') or 'UNKNOWN') == build] for r, ss in cohorts.items()}
        watch = selected['watchlist']
        group('trend_filter', watch, lambda s: 'trend_ok' if s['judgment'].get('trend_ok') else 'trend_fail', ('trend_ok', 'trend_fail'))
        trend = [s for s in watch if s['judgment'].get('trend_ok')]
        group('research_veto', trend, lambda s: 'WATCH' if s['judgment'].get('model_action') == 'WATCH' else 'NOT_WATCH', ('WATCH', 'NOT_WATCH'))
        choice = [s for s in selected['portfolio'] if s['judgment'].get('can_increase') and s['benchmark']]
        group('portfolio_allow', choice, lambda s: 'ALLOW' if s['judgment']['action'] == 'ALLOW' else 'NOT_ALLOW', ('ALLOW', 'NOT_ALLOW'))
        glob = [s for s in selected['global'] if s['judgment'].get('trend_ok')]
        group('global_stance', glob, lambda s: s['judgment'].get('stance') or 'UNKNOWN', ('LONG', 'WAIT'))
        return dict(out)
    pooled = groups()
    builds = sorted({s.get('build_id') or 'UNKNOWN' for ss in cohorts.values() for s in ss})
    return {'method': SCORE_METHOD, 'horizon_days': days, 'groups': pooled,
            'by_build': {b: groups(b) for b in builds}, 'mixed_builds': len(builds) > 1,
            'notice': '汇总仅作描述；采样在分组和分版本前完成。同标的持有期不重叠仍不等于独立，跨标的重叠窗口归为同一时间簇；不足30簇不显示区间。区间为聚类近似，未证明因果或未来盈利。全球组持有期不同且不含费用汇率，不作直接优劣结论。'}


def registry_counts(store):
    return {f"{r['route']}:{r['current_status']}": r['n'] for r in store.db.execute('''SELECT r.route,
        coalesce(s.status,'OPEN') current_status,count(*) n FROM signal_registry r LEFT JOIN signal_scores s
        ON s.signal_id=r.id AND s.method=? GROUP BY r.route,current_status''', (SCORE_METHOD,))}
