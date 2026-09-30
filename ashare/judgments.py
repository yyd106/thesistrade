"""Freeze existing outputs and their known-at-decision context without changing research.

These contracts describe price diagnostics, not realized trading returns. Business
claims with no machine-verifiable test remain UNSTRUCTURED, never inferred from prices.
"""
import json
import math
from .storage import normalize_time, now
from .calendar import completed_bar_cutoff

METHOD = 'judgment-contract-v1'


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def market_context(store, route, symbol, at, benchmark):
    """Only rows already available, and completed daily bars, may label the context."""
    missing = {'status': 'MISSING', 'label': 'UNKNOWN', 'method': 'trend20-vol20-v1'}
    if benchmark:
        row = store.db.execute('''SELECT ready_at,payload_json FROM comparison_series
            WHERE symbol=? AND ready_at<=? ORDER BY ready_at DESC,rowid DESC LIMIT 1''', (benchmark, at)).fetchone()
        if not row and route == 'dynamic':
            row = store.db.execute('SELECT updated_at,payload_json FROM dynamic_market WHERE symbol=? AND updated_at<=?', (benchmark, at)).fetchone()
        cutoff = completed_bar_cutoff(at)
    else:
        row = store.db.execute('SELECT checked_at,payload_json FROM global_market WHERE symbol=? AND checked_at<=?', (symbol, at)).fetchone()
        cutoff = at[:10]
    if not row:
        return missing
    values = {}
    for b in json.loads(row[1]).get('bars', []):
        try:
            day = b['date'] if isinstance(b, dict) else b[0]
            price = b.get('close', b.get('price_micros')) if isinstance(b, dict) else b[2]
            price = float(price)
            if day < cutoff and price > 0 and math.isfinite(price):
                values[day] = price
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    days = sorted(values)[-21:]
    if len(days) < 21:
        return {**missing, 'available_at': row[0], 'bars': len(days)}
    from datetime import date
    if (date.fromisoformat(at[:10]) - date.fromisoformat(days[-1])).days > 10:
        return {**missing, 'available_at': row[0], 'last_day': days[-1], 'reason': 'STALE_CONTEXT'}
    closes = [values[d] for d in days]
    returns = [(b / a - 1) * 10000 for a, b in zip(closes, closes[1:])]
    mean = sum(returns) / len(returns)
    vol = math.sqrt(sum((r - mean) ** 2 for r in returns) / len(returns))
    trend = 'UP' if closes[-1] > sum(closes[-20:]) / 20 else 'DOWN_OR_FLAT'
    band = 'HIGH_VOL' if vol >= 200 else 'NORMAL_VOL'
    return {'status': 'KNOWN', 'label': trend + ':' + band, 'method': missing['method'],
            'benchmark': benchmark or symbol, 'basis': 'MARKET' if benchmark else 'ASSET_SELF',
            'available_at': row[0], 'last_day': days[-1], 'volatility_bps': round(vol, 2),
            'return20_bps': round((closes[-1] / closes[0] - 1) * 10000, 2)}


def freeze(store, *, identity, route, symbol, source_id, at, build_id, days, benchmark,
           judgment, research=None, provenance='LIVE', frozen_at=None):
    """Caller owns transaction. Existing identity is never reinterpreted or overwritten."""
    if store.db.execute('SELECT 1 FROM judgment_contracts WHERE id=?', (identity,)).fetchone():
        return
    at = normalize_time(at)
    research = research or {}
    decision = research.get('decision') or {}
    context = market_context(store, route, symbol, at, benchmark) if provenance == 'LIVE' else {
        'status': 'MISSING', 'label': 'UNKNOWN', 'reason': '历史环境未在当时冻结，不用后续资料补造'}
    stance = (judgment.get('direction') if route == 'dynamic' else
              judgment.get('stance') if route == 'global' else
              judgment.get('action') if route == 'portfolio' else judgment.get('plan_kind'))
    payload = {'method': METHOD, 'benchmark': benchmark or 'CASH_USD', 'judgment': judgment,
        'prediction': {'stance': stance, 'horizon_days': days, 'clock': 'TRADING_DAYS' if benchmark else 'OBSERVED_DAILY_BARS',
                       'object': 'PRICE_DIAGNOSTIC', 'invalidation': decision.get('invalidation') or research.get('invalidation'),
                       'trigger': decision.get('trigger'), 'next_checks': research.get('next_checks', [])},
        'business_test': {'status': 'UNSTRUCTURED', 'reason': '原输出没有逐项量化的经营预测、期限和判定条件；价格不能验证经营事实'},
        'research': {k: research.get(k) for k in ('analysis', 'thesis', 'impact', 'business_link', 'counterpoints') if research.get(k)},
        'evidence_ids': list(dict.fromkeys(research.get('evidence_ids', []) +
            [x['evidence_id'] for x in research.get('facts', []) if x.get('evidence_id')] +
            [x['news_id'] for x in research.get('evidence', []) if x.get('news_id')])),
        'market': context,
        'limits': ['价格诊断不是实际成交收益', '历史补登记不可作为前向实验', '经营事实待单独验证']}
    store.db.execute('INSERT INTO judgment_contracts VALUES(?,?,?,?,?,?,?,?,?,?)',
        (identity, route, symbol, source_id, at, normalize_time(frozen_at or at), provenance,
         build_id or 'UNKNOWN', int(days), encode(payload)))


def dynamic(store, config, cid, *, provenance='LIVE', frozen_at=None):
    c = store.db.execute('SELECT * FROM dynamic_cases WHERE id=?', (cid,)).fetchone()
    if not c or store.db.execute('SELECT 1 FROM judgment_contracts WHERE id=?', ('dynamic:' + cid,)).fetchone():
        return
    analysis, plan = json.loads(c['analysis_json']), json.loads(c['plan_json'])
    build_id = None
    if provenance == 'LIVE':
        from .build import record
        build_id = record(store, config, c['created_at'])['build_id']
    freeze(store, identity='dynamic:' + cid, route='dynamic', symbol=c['symbol'], source_id=cid,
           at=c['created_at'], build_id=build_id, days=plan.get('holding_days', 3), benchmark='sh000300',
           judgment={'direction': c['direction'], 'basis': c['basis'], 'event_type': c['event_type'],
                     'news_id': c['news_id'], 'plan': plan}, research=analysis,
           provenance=provenance if c['basis'] == 'FORWARD' else 'RETROSPECTIVE', frozen_at=frozen_at)


def backfill(store, config, at=None):
    """Add explicitly legacy contracts; never recalculate a known-at-time label."""
    at = normalize_time(at or now())
    count = 0
    with store.db:
        rows = store.db.execute('''SELECT r.* FROM signal_registry r WHERE created_at<=?
            AND NOT EXISTS(SELECT 1 FROM judgment_contracts c WHERE c.id=r.id)''', (at,)).fetchall()
        for r in rows:
            freeze(store, identity=r['id'], route=r['route'], symbol=r['symbol'], source_id=r['source_id'],
                at=r['created_at'], build_id=r['build_id'], days=r['horizon_days'], benchmark=r['benchmark'],
                judgment=json.loads(r['judgment_json']), provenance='LEGACY', frozen_at=at)
            count += 1
        for r in store.db.execute('''SELECT id FROM dynamic_cases c WHERE created_at<=?
            AND NOT EXISTS(SELECT 1 FROM judgment_contracts j WHERE j.id='dynamic:'||c.id)''', (at,)).fetchall():
            dynamic(store, config, r['id'], provenance='LEGACY', frozen_at=at)
            count += 1
    return count
