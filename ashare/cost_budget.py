"""P06 cost-basis budget; the authoritative unit NAV and latch live in portfolio_risk.

No model decisions or automatic resets. A disabled budget leaves its saved latch
intact; re-enabling cannot silently create a new cost basis.
"""
import json
from decimal import Decimal

FEATURE = 'cost_budget_v1'


def semantic_state(risk):
    """Stable approval/research dependency; ordinary valuation polls do not change it."""
    return {'basis': risk.get('cost_basis', '1'), 'revision': risk.get('cost_basis_revision', 0),
            'locked': bool(risk.get('cost_locked'))}


def active(store, config):
    if config.get('deployment_role') == 'cloud':
        from .cloud_runtime import value
        return value(store, 'cost_budget_mode') == 'P06'
    if config.get('cost_budget') != 'P06':
        return False
    from .cloud_sync import remote_supports
    return remote_supports(store, FEATURE)


def cap_bps(nav, basis):
    u, b = Decimal(str(nav)), Decimal(str(basis))
    if not u.is_finite() or not b.is_finite() or b <= 0:
        raise ValueError('COST_UNIT_NAV_INVALID')
    return 0 if u <= 0 else max(0, min(9000, int(Decimal(100000) * (u - Decimal('.9') * b) / u)))


def updated(previous, result, config, at):
    """Pure transition, called only after portfolio_risk has a complete valuation."""
    from .calendar import local, trading_day
    basis = previous.get('cost_basis', '1')
    cap = cap_bps(result['unit_nav'], basis)
    locked = bool(previous.get('cost_locked'))
    d = local(at)
    close = bool(trading_day(d.date()) and d.strftime('%H:%M') >= '15:05')
    breach = Decimal(result['unit_nav']) <= Decimal('.9') * Decimal(basis)
    # Test the exact formula, not the rounded-down budget used for integer sizing.
    trigger = breach or (close and Decimal(result['unit_nav']) * 99 <= Decimal(basis) * 90)
    if locked:
        cap = min(cap, previous.get('cost_cap_bps', cap))
    changed = {**result, 'cost_basis': basis, 'cost_floor': str(Decimal('.9') * Decimal(basis)),
               'cost_cap_bps': cap, 'cost_locked': locked or trigger,
               'cost_checked_at': at, 'cost_automatic_resume': False}
    if trigger and not locked:
        changed['cost_locked_at'] = at
    # Reductions use the last complete day's cap on the next session. An opening
    # gap below the floor instead supplies a zero cap immediately.
    if close:
        changed['cost_close_cap_bps'] = cap
        changed['cost_close_day'] = d.date().isoformat()
    if breach:
        changed['cost_floor_breached'] = True
    return changed


def current(store, config, at):
    from .portfolio_risk import evaluate_inside
    return evaluate_inside(store, config, at, persist=False)


def buy_reason(store, config, at):
    if not active(store, config):
        return None
    risk = current(store, config, at)
    if risk.get('cost_locked'):
        return 'COST_BUDGET_LOCKED'
    if not risk.get('valuation_complete') or risk.get('historical_cashflow_gap'):
        return 'COST_VALUATION_INCOMPLETE'
    return None


def reduction_cap_pct(store, config, at):
    if not active(store, config):
        return None
    risk = current(store, config, at)
    if not risk.get('valuation_complete'):
        return None
    if risk.get('cost_floor_breached'):
        return risk.get('cost_cap_bps', 0) / 100
    from .exposure import next_session_opened
    if next_session_opened(risk.get('cost_close_day'), at):
        # Ordinary intraday tightening limits new buys immediately, but its sell
        # target becomes effective only after the next complete close/open cycle.
        # The floor-breach path above is the explicit immediate-exit exception.
        return risk['cost_close_cap_bps'] / 100
    return None


def _authority(config):
    if config.get('deployment_role') != 'cloud':
        raise ValueError('成本基准只能在云端权威账户重置')


def _preview(risk):
    return {'version': 'cost-basis-reset-v1', 'old_basis': risk.get('cost_basis', '1'),
            'basis_revision': risk.get('cost_basis_revision', 0),
            'unit_nav_at_request': risk['unit_nav'], 'new_basis': 'EXECUTION_TIME_UNIT_NAV',
            'cost_locked': bool(risk.get('cost_locked')), 'hard_halted': bool(risk.get('halted'))}


def request(store, config, at):
    from . import approvals, portfolio_risk
    _authority(config)
    if not active(store, config):
        raise ValueError('P06尚未启用')
    with approvals.atomic(store):
        risk = portfolio_risk.evaluate_inside(store, config, at)
        if not risk.get('valuation_complete') or Decimal(risk['unit_nav']) <= 0:
            raise ValueError('成本基准重置需要完整的正单位净值')
        preview = _preview(risk)
        approval = approvals.create_request(store, kind='COST_BASIS_RESET', subject_id='DEMO_PAPER',
            action='RESET', snapshot=preview, summary={'title': '重置模拟账户成本基准',
            **preview, 'notice': '重置会以当前净值建立新的成本基准，累计相对最初投入的亏损仍可能超过10%；25%硬停不变。'}, at=at, notify=False)
        return {'status': 'PREVIEW_READY', 'approval': approval, 'preview': preview}


def execute(store, config, request_id, user, expected_hash, at):
    from . import approvals, portfolio_risk
    from .storage import digest
    _authority(config)
    with approvals.atomic(store):
        approvals.require_admin_session(store, user, at=at)
        envelope = approvals.envelope(store, request_id)
        if envelope['kind'] != 'COST_BASIS_RESET' or expected_hash != envelope['hash']:
            raise ValueError('成本基准审批内容不匹配')
        used = store.db.execute('SELECT result_json FROM approval_consumptions WHERE request_id=?', (request_id,)).fetchone()
        if used:
            return {'status': 'ALREADY_EXECUTED', 'result': json.loads(used[0])}
        if not active(store, config):
            raise ValueError('P06尚未启用')
        risk = portfolio_risk.evaluate_inside(store, config, at)
        frozen = envelope['snapshot']
        if (risk.get('cost_basis', '1'), risk.get('cost_basis_revision', 0)) != (frozen.get('old_basis'), frozen.get('basis_revision')):
            raise ValueError('成本基准已经变化，请重新申请')
        if not risk.get('valuation_complete') or Decimal(risk['unit_nav']) <= 0:
            raise ValueError('成本基准重置需要完整的正单位净值')
        approvals.approve(store, request_id, expected_hash, user, authority='CLOUD_ADMIN', at=at)
        receipt = approvals.check_receipt(store, request_id, kind='COST_BASIS_RESET', subject_id='DEMO_PAPER',
                                         action='RESET', snapshot=frozen, at=at)
        basis = risk['unit_nav']
        result = {'status': 'RESET', 'at': at, 'old_basis': risk.get('cost_basis', '1'), 'new_basis': basis,
                  'approval_id': request_id, 'receipt_sha256': digest(approvals.canonical(receipt)),
                  'hard_halted': bool(risk.get('halted'))}
        risk.update(cost_basis=basis, cost_floor=str(Decimal('.9') * Decimal(basis)), cost_cap_bps=9000,
                    cost_locked=False, cost_basis_revision=risk.get('cost_basis_revision', 0) + 1,
                    cost_reset=result, cost_floor_breached=False)
        for key in ('cost_locked_at', 'cost_close_cap_bps', 'cost_close_day'):
            risk.pop(key, None)
        store.db.execute('UPDATE portfolio_risk SET updated_at=?,payload_json=? WHERE id=?',
                         (at, json.dumps(risk), 'DEMO_PAPER'))
        store.db.execute('INSERT INTO portfolio_risk_events VALUES(?,?,?)',
                         (digest('cost-reset:' + request_id), at, json.dumps({'kind': 'COST_BASIS_RESET', **result})))
        approvals.consume(store, request_id, result, at=at)
        return result
