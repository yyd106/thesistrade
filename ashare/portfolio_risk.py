"""A persistent, shared, cashflow-adjusted high-water drawdown circuit breaker.

The account currently permits initial capital and withdrawals only. Unitized
NAV adjusts units at each reconciled transfer so withdrawals are not losses.
No inferred or cost-fallback valuations can set a new high-water mark.
"""
import json
from decimal import Decimal
from .storage import normalize_time, digest
from .investment_policy import DRAWDOWN_BPS, enabled


def state(store):
    row = store.db.execute("SELECT payload_json FROM portfolio_risk WHERE id='DEMO_PAPER'").fetchone()
    return json.loads(row[0]) if row else {'halted': False, 'drawdown_bps': None}


def halted(store):
    return bool(state(store).get('halted'))


def evaluate_inside(store, config, at, account=None):
    """Caller owns transaction. Must commit the latch even when rejecting a BUY."""
    if not enabled(config):
        return state(store)
    if not store.db.in_transaction:
        raise RuntimeError('Risk check requires account transaction')
    from .paper import account_inside_transaction
    a = account or account_inside_transaction(store, at)
    at = normalize_time(at)
    previous = state(store)
    units = Decimal(previous.get('capital_units', str(a['initial_cents'])))
    peak_nav = Decimal(previous.get('peak_unit_nav', '1'))
    uncertainty = False
    if 'capital_units' not in previous:
        # Existing installation has no withdrawals. Do not guess unit NAV if an
        # imported history lacks valuations at historical cashflow boundaries.
        uncertainty = a['withdrawn_cents'] != 0
        historical = store.db.execute('SELECT max(equity_cents) FROM equity_marks WHERE complete=1 AND withdrawn_cents=0 AND at<=? AND recorded_at<=?', (at, at)).fetchone()[0]
        peak_nav = max(peak_nav, Decimal(historical or a['initial_cents'])/units)
    complete = valuation_ready(store, a, at) and not uncertainty
    nav = Decimal(a['equity_cents'])/units if units > 0 else Decimal(0)
    if complete:
        peak_nav = max(peak_nav, nav)
    drawdown = max(0, int((peak_nav-nav)*10000/peak_nav)) if complete and peak_nav else None
    breached = drawdown is not None and drawdown >= DRAWDOWN_BPS
    result = {**previous, 'halted': previous.get('halted', False) or breached,
              'capital_units': str(units), 'peak_unit_nav': str(peak_nav), 'unit_nav': str(nav),
              'equity_cents': a['equity_cents'], 'drawdown_bps': drawdown,
              'threshold_bps': DRAWDOWN_BPS, 'valuation_complete': complete,
              'checked_at': at, 'basis': 'CASHFLOW_UNITIZED_NAV', 'automatic_resume': False,
              'historical_cashflow_gap': uncertainty}
    if breached and not previous.get('halted'):
        result['triggered_at'] = at
        result['trigger_equity_cents'] = a['equity_cents']
        store.db.execute('INSERT INTO portfolio_risk_events VALUES(?,?,?)',
                         (digest('risk:'+at), at, json.dumps(result)))
    if result['halted']:
        for table in ('paper_orders', 'dynamic_orders', 'global_orders'):
            store.db.execute("UPDATE "+table+" SET status='CANCELLED',reserved_cents=0 WHERE side='BUY' AND status IN ('OPEN','PARTIAL')")
    store.db.execute('INSERT OR REPLACE INTO portfolio_risk VALUES(?,?,?)', ('DEMO_PAPER', at, json.dumps(result)))
    return result


def refresh(store, config, at):
    store.db.execute('BEGIN IMMEDIATE')
    try:
        result = evaluate_inside(store, config, at)
        store.db.commit()
        return result
    except BaseException:
        store.db.rollback()
        raise


def valuation_ready(store, a, at):
    from .calendar import phase, local, last_completed_day
    from .paper import quote_ok
    from .dynamic_sources import latest_quote as dynamic_quote
    from .global_paper import balance
    if not balance(store, at)['valuation_complete']:
        return False
    for group in ('positions', 'dynamic_positions'):
        for symbol in a.get(group, {}):
            q = dynamic_quote(store, symbol, at) if group == 'dynamic_positions' else store.latest_quote(symbol, at)
            if not q:
                return False
            if phase(at) == 'CONTINUOUS':
                if not quote_ok(q, {'quote_max_age_seconds': 90}, at):
                    return False
            elif not last_completed_day(at) or local(q['observed_at']).date().isoformat() < last_completed_day(at):
                return False
    return True


def adjust_withdrawal_inside(store, at, amount_cents):
    """Unit cancellation at the transfer's actual pre-flow portfolio valuation."""
    from .paper import account_inside_transaction
    previous = state(store)
    if 'capital_units' not in previous:
        return
    a = account_inside_transaction(store, at)
    if not valuation_ready(store, a, at) or amount_cents >= a['equity_cents']:
        raise ValueError('提取时估值不足，无法核算单位净值')
    units = Decimal(previous['capital_units'])*(a['equity_cents']-amount_cents)/a['equity_cents']
    previous.update(capital_units=str(units), last_transfer_at=at)
    store.db.execute('UPDATE portfolio_risk SET updated_at=?,payload_json=? WHERE id=?', (at, json.dumps(previous), 'DEMO_PAPER'))
