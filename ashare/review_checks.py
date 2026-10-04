"""Deterministic daily consistency checks. They verify execution against the rules it was given and
the health of data, sync and model calls. No model is involved, and failures become engineering issues."""
import json
from datetime import datetime, timedelta
from .storage import normalize_time

WATCHLIST_EXIT_CODES = frozenset(('COST_STOP_TRIGGER', 'PLAN_STOP_TRIGGER', 'PLAN_EXIT_TRIGGER',
                                 'PORTFOLIO_REDUCE', 'ACCOUNT_DRAWDOWN_EXIT'))
GLOBAL_EXIT_CODES = frozenset(('COST_STOP_TRIGGER', 'PLAN_EXIT_TRIGGER', 'PORTFOLIO_REDUCE',
                              'ACCOUNT_DRAWDOWN_EXIT', 'DAY_HORIZON_EXIT'))
DYNAMIC_EXIT_CODES = {'事件判断已失效': 'EVENT_INVALIDATED', '动态持仓成本止损': 'COST_STOP_TRIGGER',
                      '动态持仓止盈': 'PLAN_EXIT_TRIGGER', '事件持有期结束': 'DAY_HORIZON_EXIT',
                      'PORTFOLIO_REDUCE': 'PORTFOLIO_REDUCE', 'ACCOUNT_DRAWDOWN_EXIT': 'ACCOUNT_DRAWDOWN_EXIT'}


def _object(value):
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def exit_evidence(store, route, order, terms):
    """Inspect recorded execution evidence, never infer authorization from prose or alter orders.

    Pre-structured watchlist records keep a narrow, labelled compatibility path for the exact
    program-generated leading code. A present but empty/invalid risk_trigger never uses that path.
    """
    if not order:
        return {'evidence_source': 'MISSING_ORDER'}, False, 'order / exit evidence'
    reason, code, source, missing = None, None, None, None
    if route == 'watchlist':
        decision = store.db.execute('SELECT reason,payload_json FROM decisions WHERE id=?', (order['decision_id'],)).fetchone()
        if not decision:
            return {'evidence_source': 'MISSING_DECISION'}, False, 'decision / exit evidence'
        reason = decision['reason']
        payload = _object(decision['payload_json'])
        if payload is None:
            return {'evidence_source': 'INVALID_DECISION_PAYLOAD'}, False, 'decision payload / exit evidence'
        if 'risk_trigger' in payload:
            code, source = payload['risk_trigger'], 'DECISION_RISK_TRIGGER'
            allowed = WATCHLIST_EXIT_CODES
        else:
            source = 'LEGACY_REASON_PREFIX'
            code = reason.split('；', 1)[0] if isinstance(reason, str) else None
            if code not in WATCHLIST_EXIT_CODES:
                missing = 'legacy decision / verifiable leading exit code'
            allowed = WATCHLIST_EXIT_CODES
    elif route == 'global':
        code, source = terms.get('reason'), 'GLOBAL_ORDER_REASON'
        reason, allowed = code, GLOBAL_EXIT_CODES
    else:
        reason, source = order.get('reason'), 'DYNAMIC_ORDER_REASON'
        code = DYNAMIC_EXIT_CODES.get(reason) if isinstance(reason, str) else None
        # Unknown non-empty fixed reasons are invalid, rather than a missing successful lookup.
        if reason and code is None:
            return {'reason': str(reason)[:160], 'evidence_source': source}, True, None
        allowed = frozenset(DYNAMIC_EXIT_CODES.values())
    if code is None or code == '':
        missing = missing or 'recorded exit code'
    failed = not missing and (not isinstance(code, str) or code not in allowed)
    detail = {'reason': str(reason)[:160] if reason is not None else None,
              'exit_code': str(code)[:80] if code is not None else None, 'evidence_source': source}
    if source == 'LEGACY_REASON_PREFIX':
        detail['compatibility'] = '旧记录仅核验程序生成的首段退出码，不把自由文本当作授权。'
    return detail, bool(failed), missing


def _result(key, failures, *, checked, detail=''):
    return {'check': key, 'status': 'FAIL' if failures else ('PASS' if checked else 'NOT_APPLICABLE'), 'checked': checked,
            'failures': len(failures), 'examples': failures[:5], 'detail': detail}


def execution(store, config, start, end, ready):
    keys = ('CHECK_BUY_OUTSIDE_PLAN_BAND', 'CHECK_BUY_WITH_PLAN_BLOCKERS',
            'CHECK_BUY_WITHOUT_PORTFOLIO_ALLOW', 'CHECK_BUY_WHILE_HALTED', 'CHECK_SELL_WITHOUT_REASON')
    checks = {k: {'failures': [], 'checked': 0, 'missing': [], 'routes': {}, 'evidence_sources': {}, 'legacy_checked': 0} for k in keys}
    evidence, route_counts, missing_groups = [], {}, {}
    from .portfolio_risk import state
    risk = state(store)

    def check(k, route, example, failed=False, missing=None):
        c = checks[k]
        if example.get('evidence_source'):
            source = example['evidence_source']
            c['evidence_sources'][source] = c['evidence_sources'].get(source, 0) + 1
            if source == 'LEGACY_REASON_PREFIX' and not missing and not failed:
                c['legacy_checked'] += 1
        counts = c['routes'].setdefault(route, {'checked': 0, 'failed': 0, 'missing': 0})
        if missing:
            gap = {**example, 'check': k, 'missing': missing}
            c['missing'].append(gap);evidence.append(gap);counts['missing'] += 1
            group = missing_groups.setdefault((k, route, missing),
                {'check': k, 'route': route, 'missing': missing, 'count': 0, 'examples': []})
            group['count'] += 1
            if len(group['examples']) < 2:
                group['examples'].append(gap)
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
                    terms = (_object(t[0]) or {}) if t else {}
                else:
                    terms = _object(o['terms_json'] if route == 'dynamic' else o['payload_json']) or {}
                if route == 'dynamic' and f['side'] == 'BUY':
                    if terms.get('plan'):
                        snapshot = terms.get('case_snapshot') or {}
                        p = {'kind': 'PAPER_TRADE' if snapshot.get('status') == 'READY' else None,
                             'blockers': snapshot.get('blockers'), 'levels': terms['plan']}
                elif f['side'] == 'BUY':
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
                detail, failed, missing = exit_evidence(store, route, o, terms)
                check(keys[4], route, {**example, **detail}, failed, missing)
    results = []
    for k, c in checks.items():
        r = _result(k, c['failures'], checked=c['checked'])
        r['missing'] = len(c['missing'])
        if c['evidence_sources']:
            r['evidence_sources'] = c['evidence_sources']
        if k == 'CHECK_SELL_WITHOUT_REASON':
            r['legacy_checked'] = c['legacy_checked']
            r['detail'] = '核验订单留存的退出依据，不重新计算当时的触发条件。'
            if c['legacy_checked']:
                r['detail'] += f"其中{c['legacy_checked']}笔旧记录按程序生成的首段退出码兼容核验，未使用自由文本推断授权。"
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
    r['missing'] = len(evidence)
    r['missing_groups'] = list(missing_groups.values())
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
    from .model_identity_audit import check as identity_check
    results.append(identity_check(store, start, end, ready))
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
                evidence = [json.dumps(e, ensure_ascii=False)[:200] for e in c['examples']]
                if c.get('missing_groups'):
                    # One stable issue retains every check/route count without creating an issue
                    # per fill. Full representative examples remain in the immutable review.
                    groups = [{k: g[k] for k in ('check', 'route', 'missing', 'count')} for g in c['missing_groups']]
                    evidence.insert(0, json.dumps({'missing_groups': groups}, ensure_ascii=False))
                record_issue(store, c['check'], 'MARKET', c['detail'] or c['check'], evidence, ready)
    return checks
