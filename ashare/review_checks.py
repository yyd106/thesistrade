"""Deterministic daily consistency checks. They verify execution against the rules it was given and
the health of data, sync and model calls. No model is involved, and failures become engineering issues."""
import json
from datetime import datetime, timedelta
from .storage import normalize_time

EXIT_REASONS = ('COST_STOP_TRIGGER', 'PLAN_STOP_TRIGGER', 'PLAN_EXIT_TRIGGER', 'PORTFOLIO_REDUCE', 'ACCOUNT_DRAWDOWN_EXIT')


def _result(key, failures, *, checked, detail=''):
    return {'check': key, 'status': 'FAIL' if failures else 'PASS', 'checked': checked,
            'failures': len(failures), 'examples': failures[:5], 'detail': detail}


def execution(store, config, start, end, ready):
    fills = [dict(r) for r in store.db.execute("SELECT * FROM paper_fills WHERE occurred_at>=? AND occurred_at<? AND recorded_at<=?", (start, end, ready))]
    orders = {}
    for f in fills:
        if f['order_id'] not in orders:
            row = store.db.execute('SELECT * FROM paper_orders WHERE id=?', (f['order_id'],)).fetchone()
            orders[f['order_id']] = dict(row) if row else None
    def plan(pid):
        row = store.db.execute('SELECT payload_json FROM plans WHERE id=?', (pid,)).fetchone()
        return json.loads(row[0]) if row else None
    def terms(oid):
        row = store.db.execute('SELECT config_json FROM paper_order_terms WHERE order_id=?', (oid,)).fetchone()
        return json.loads(row[0]) if row else {}
    outside, blocked, unauthorized, unexplained = [], [], [], []
    buys = [f for f in fills if f['side'] == 'BUY']
    for f in buys:
        o = orders.get(f['order_id'])
        if not o:
            continue
        p = plan(o['plan_id'])
        if p is None:
            continue
        levels = p.get('levels') or {}
        # The order limit is capped at the band's upper edge, so any fill above it breaks the rule.
        if levels and f['price_cents'] > levels.get('buy_high_cents', 0):
            outside.append({'fill_id': f['id'], 'symbol': f['symbol'], 'price_cents': f['price_cents'], 'buy_high_cents': levels.get('buy_high_cents')})
        if p.get('kind') != 'PAPER_TRADE' or p.get('blockers'):
            blocked.append({'fill_id': f['id'], 'symbol': f['symbol'], 'plan_id': o['plan_id'], 'blockers': p.get('blockers', [])[:5]})
        if config.get('portfolio_strategy'):
            decision = terms(o['id']).get('portfolio_decision') or {}
            if decision.get('action') != 'ALLOW':
                unauthorized.append({'fill_id': f['id'], 'symbol': f['symbol'], 'portfolio_action': decision.get('action')})
    from .portfolio_risk import state
    risk = state(store)
    halted_buys = [{'fill_id': f['id'], 'symbol': f['symbol']} for f in buys
                   if risk.get('halted') and risk.get('triggered_at') and orders.get(f['order_id'])
                   and orders[f['order_id']]['created_at'] > risk['triggered_at']]
    sells = {f['order_id'] for f in fills if f['side'] == 'SELL'}
    for oid in sells:
        o = orders.get(oid)
        if not o:
            continue
        row = store.db.execute('SELECT reason FROM decisions WHERE id=?', (o['decision_id'],)).fetchone()
        if row and not any(r in row[0] for r in EXIT_REASONS):
            unexplained.append({'order_id': oid, 'symbol': o['symbol'], 'reason': row[0][:120]})
    return [_result('CHECK_BUY_OUTSIDE_PLAN_BAND', outside, checked=len(buys)),
            _result('CHECK_BUY_WITH_PLAN_BLOCKERS', blocked, checked=len(buys)),
            _result('CHECK_BUY_WITHOUT_PORTFOLIO_ALLOW', unauthorized, checked=len(buys)),
            _result('CHECK_BUY_WHILE_HALTED', halted_buys, checked=len(buys)),
            _result('CHECK_SELL_WITHOUT_REASON', unexplained, checked=len(sells))]


def health(store, config, start, end, ready, portfolio_facts=None):
    results = []
    late = [p['key'] for p in (portfolio_facts or {}).get('positions', []) if (p.get('valuation') or {}).get('late_quote')]
    results.append(_result('CHECK_LATE_QUOTE_IN_REVIEW', [{'position': k} for k in late], checked=len((portfolio_facts or {}).get('positions', [])),
                           detail='复盘截止后才入库的收盘报价，不能当作截止时已知'))
    if config.get('deployment_role') == 'research':
        from .cloud_runtime import value
        last = value(store, 'last_sync') or {}
        stale = not last or last.get('status') != 'OK' or (datetime.fromisoformat(ready) - datetime.fromisoformat(last['at'])).total_seconds() > 600
        results.append(_result('CHECK_SYNC_STALE', [{'last_sync': last}] if stale else [], checked=1))
        pending = store.db.execute("SELECT count(*) FROM cloud_outbox WHERE status='PENDING'").fetchone()[0]
        results.append(_result('CHECK_OUTBOX_BACKLOG', [{'pending': pending}] if pending > 3 else [], checked=1))
    attempts = [dict(r) for r in store.db.execute("SELECT status,detail FROM data_attempts WHERE source='research_analysis' AND checked_at>=? AND checked_at<?", (start, end))]
    identity = [a['detail'][:160] for a in attempts if '模型实际运行配置' in (a['detail'] or '')]
    results.append(_result('CHECK_MODEL_IDENTITY', [{'detail': d} for d in identity], checked=len(attempts)))
    failed = sum(a['status'] != 'OK' for a in attempts)
    high = len(attempts) >= 10 and failed / len(attempts) > 0.3
    results.append(_result('CHECK_RESEARCH_FAILURE_RATE', [{'failed': failed, 'total': len(attempts)}] if high else [], checked=len(attempts)))
    from .maintenance import disk_status
    disk = disk_status(store, config)
    results.append(_result('CHECK_DISK_SPACE', [disk] if disk['warning'] else [], checked=1))
    return results


def run(store, config, start, end, ready, portfolio_facts=None):
    checks = execution(store, config, start, end, ready) + health(store, config, start, end, ready, portfolio_facts)
    from .governance import record_issue
    with store.db:
        for c in checks:
            if c['status'] == 'FAIL':
                record_issue(store, c['check'], 'MARKET', c['detail'] or c['check'], [json.dumps(e, ensure_ascii=False)[:200] for e in c['examples']], ready)
    return checks
