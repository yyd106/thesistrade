"""Deterministic daily consistency checks. They verify execution against the rules it was given and
the health of data, sync and model calls. No model is involved, and failures become engineering issues."""
import json
from datetime import datetime, timedelta
from .storage import normalize_time

EXIT_REASONS = ('COST_STOP_TRIGGER', 'PLAN_STOP_TRIGGER', 'PLAN_EXIT_TRIGGER', 'PORTFOLIO_REDUCE', 'ACCOUNT_DRAWDOWN_EXIT', 'DAY_HORIZON_EXIT',
                '事件判断已失效', '动态持仓成本止损', '动态持仓止盈', '事件持有期结束')


def _result(key, failures, *, checked, detail=''):
    return {'check': key, 'status': 'FAIL' if failures else ('PASS' if checked else 'NOT_APPLICABLE'), 'checked': checked,
            'failures': len(failures), 'examples': failures[:5], 'detail': detail}


def execution(store, config, start, end, ready):
    keys = ('CHECK_BUY_OUTSIDE_PLAN_BAND', 'CHECK_BUY_WITH_PLAN_BLOCKERS',
            'CHECK_BUY_WITHOUT_PORTFOLIO_ALLOW', 'CHECK_BUY_WHILE_HALTED', 'CHECK_SELL_WITHOUT_REASON')
    checks = {k: {'failures': [], 'checked': 0, 'missing': [], 'routes': {}} for k in keys}
    evidence, route_counts = [], {}
    from .portfolio_risk import state
    risk = state(store)

    def check(k, route, example, failed=False, missing=None):
        c = checks[k]
        counts = c['routes'].setdefault(route, {'checked': 0, 'failed': 0, 'missing': 0})
        if missing:
            gap = {**example, 'missing': missing}
            c['missing'].append(gap);evidence.append(gap);counts['missing'] += 1
        else:
            c['checked'] += 1;counts['checked'] += 1
            if failed:
                c['failures'].append(example);counts['failed'] += 1

    for route, prefix in (('watchlist', 'paper'), ('dynamic', 'dynamic'), ('global', 'global')):
        fills = [dict(r) for r in store.db.execute(f'SELECT * FROM {prefix}_fills WHERE occurred_at>=? AND occurred_at<? AND recorded_at<=?', (start, end, ready))]
        route_counts[route] = {'fills': len(fills), 'buys': sum(f['side']=='BUY' for f in fills), 'sells': sum(f['side']=='SELL' for f in fills)}
        for f in fills:
            example = {'route': route, 'fill_id': f['id'], 'symbol': f['symbol']}
            row = store.db.execute(f'SELECT * FROM {prefix}_orders WHERE id=?', (f['order_id'],)).fetchone()
            o = dict(row) if row else None
            terms, p = {}, None
            if o:
                if route == 'watchlist':
                    t = store.db.execute('SELECT config_json FROM paper_order_terms WHERE order_id=?', (o['id'],)).fetchone()
                    terms = json.loads(t[0]) if t else {}
                else:
                    terms = json.loads(o['terms_json'] if route == 'dynamic' else o['payload_json'])
                if route == 'dynamic':
                    if terms.get('plan'):
                        snapshot = terms.get('case_snapshot') or {}
                        p = {'kind': 'PAPER_TRADE' if snapshot.get('status') == 'READY' else None,
                             'blockers': snapshot.get('blockers'), 'levels': terms['plan']}
                else:
                    table = 'plans' if route == 'watchlist' else 'global_plans'
                    r = store.db.execute(f'SELECT payload_json FROM {table} WHERE id=?', (o['plan_id'],)).fetchone()
                    p = json.loads(r[0]) if r else None
            if f['side'] == 'BUY':
                high_key = 'buy_high_micros' if route == 'global' else 'buy_high_cents'
                price_key = 'price_micros' if route == 'global' else 'price_cents'
                high = (p.get('levels') or {}).get(high_key) if p else None
                check(keys[0], route, example, bool(high and f[price_key] > high),
                      None if o and high else 'order / plan / upper band')
                check(keys[1], route, example, bool(p and (p.get('kind') != 'PAPER_TRADE' or p.get('blockers'))),
                      None if p and p.get('kind') is not None and p.get('blockers') is not None else 'original plan eligibility')
                decision = terms.get('portfolio_decision') or {}
                # Old terms explicitly stored None when the portfolio layer was disabled.
                required = terms.get('portfolio_strategy', bool(decision))
                if required:
                    check(keys[2], route, example, decision.get('action') != 'ALLOW')
                elif not o or 'portfolio_decision' not in terms:
                    check(keys[2], route, example, missing='order portfolio policy snapshot')
                # A pre-halt order filling after the latch is also a violation.
                triggered = risk.get('triggered_at')
                check(keys[3], route, example, bool(triggered and f['occurred_at'] >= triggered))
            else:
                reason = None
                if o and route == 'watchlist':
                    r = store.db.execute('SELECT reason FROM decisions WHERE id=?', (o['decision_id'],)).fetchone()
                    reason = r[0] if r else None
                elif o:
                    reason = o.get('reason') if route == 'dynamic' else terms.get('reason')
                check(keys[4], route, {**example, 'reason': reason}, bool(reason and not any(r in reason for r in EXIT_REASONS)),
                      None if reason else 'order / exit reason')
    results = []
    for k, c in checks.items():
        r = _result(k, c['failures'], checked=c['checked'])
        r['missing'] = len(c['missing'])
        if c['missing'] and r['status'] != 'FAIL':r['status'] = 'INSUFFICIENT'
        r['by_route'] = {route: {**c['routes'].get(route, {'checked': 0, 'failed': 0, 'missing': 0}),
            'status': 'FAIL' if c['routes'].get(route, {}).get('failed') else 'INSUFFICIENT' if c['routes'].get(route, {}).get('missing')
            else 'PASS' if c['routes'].get(route, {}).get('checked') else 'NOT_APPLICABLE'} for route in route_counts}
        if c['missing']:r['examples'] += c['missing'][:5]
        results.append(r)
    r = _result('CHECK_EXECUTION_EVIDENCE', evidence, checked=sum(c['fills'] for c in route_counts.values()),
                detail='三路成交均核验；缺订单、原始计划或退出引用必须补证，不计作通过。')
    if evidence:r['status'] = 'INSUFFICIENT'
    r['by_route'] = route_counts
    return results + [r]


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
            if c['status'] == 'FAIL' or (c['status'] == 'INSUFFICIENT' and c['check'] == 'CHECK_EXECUTION_EVIDENCE'):
                record_issue(store, c['check'], 'MARKET', c['detail'] or c['check'], [json.dumps(e, ensure_ascii=False)[:200] for e in c['examples']], ready)
    return checks
