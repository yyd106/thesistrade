"""Durable no-order boundary. Removing a UI/config flag never releases this lock.

Activation is an approved config operation under the same SQLite write lock as
orders. Both nodes must be upgraded and preflighted before changing configuration.
No unlock operation is provided by this change.
"""
import json
import re
from datetime import datetime
from .storage import now

KEY = 'research_only_locks_v1'
FEATURE = 'research_only_v1'
NOTICE = '参考区间，只研究不交易'
PREFLIGHT_MAX_AGE_SECONDS = 120


def configured(config):
    return {w['symbol'] for w in config.get('watchlist', []) if w.get('research_only') is True}


def locks(store):
    row = store.db.execute('SELECT value FROM cloud_state WHERE key=?', (KEY,)).fetchone()
    result = json.loads(row[0]) if row else {}
    if not isinstance(result, dict) or any(not re.fullmatch(r'(?:sh|sz)\d{6}', s) for s in result):
        raise ValueError('RESEARCH_ONLY_LOCK_INVALID')
    return result


def symbols(store, config):
    return configured(config) | set(locks(store))


def contains(store, config, symbol):
    return symbol in symbols(store, config)


def preflight(store, selected):
    """Read-only, local authoritative/mirrored ledger check; unknown is not flat."""
    if not selected:
        return
    if not store.db.execute("SELECT 1 FROM paper_accounts WHERE id='DEMO_PAPER'").fetchone():
        raise ValueError('RESEARCH_ONLY_ACCOUNT_UNKNOWN')
    for symbol in sorted(selected):
        for table in ('paper_lots', 'dynamic_lots', 'global_lots'):
            if store.db.execute('SELECT 1 FROM '+table+' WHERE symbol=? AND (qty IS NULL OR qty!=0) LIMIT 1', (symbol,)).fetchone():
                raise ValueError('RESEARCH_ONLY_EXPOSURE:'+symbol)
        for table in ('paper_orders', 'dynamic_orders', 'global_orders'):
            # An unfamiliar status is unresolved, never treated as a terminal order.
            if store.db.execute('SELECT 1 FROM '+table+" WHERE symbol=? AND (status IS NULL OR status NOT IN ('FILLED','CANCELLED','EXPIRED','REJECTED')) LIMIT 1", (symbol,)).fetchone():
                raise ValueError('RESEARCH_ONLY_OPEN_ORDER:'+symbol)


def activate_inside(store, selected, operation_id, *, config=None):
    """Caller owns BEGIN IMMEDIATE; durable intent commits locks before config I/O."""
    if not store.db.in_transaction:
        raise RuntimeError('RESEARCH_ONLY_REQUIRES_WRITE_TRANSACTION')
    current = locks(store)
    if selected and config and not selected<=set(current):
        from .cloud_runtime import value
        role=config.get('deployment_role','standalone')
        if role=='cloud' and value(store,'initialized') is not True:
            raise ValueError('RESEARCH_ONLY_CLOUD_LEDGER_UNKNOWN')
        if role=='research':
            remote=value(store,'remote_research_only') or {}
            stamp=remote.get('at')
            try:age=(datetime.fromisoformat(now())-datetime.fromisoformat(stamp)).total_seconds()
            except (TypeError,ValueError):age=None
            from .cloud_ledger import version
            if (FEATURE not in (value(store,'remote_features') or []) or age is None or not 0<=age<=PREFLIGHT_MAX_AGE_SECONDS
                    or not selected<=set(remote.get('symbols',[]))
                    or remote.get('ledger_version')!=value(store,'remote_ledger_version')
                    or remote.get('ledger_version')!=version(store)):
                raise ValueError('RESEARCH_ONLY_CLOUD_PREFLIGHT_REQUIRED')
    preflight(store, selected)
    for symbol in sorted(selected):
        current.setdefault(symbol, {'activated_at': now(), 'operation_id': operation_id})
    if selected:
        store.db.execute('INSERT INTO cloud_state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (KEY, json.dumps(current, sort_keys=True)))


def import_remote_preflight(store, packet):
    """A completed ledger page carries cloud locks; never overwrites local locks."""
    from .cloud_runtime import put
    raw=packet.get('research_only')
    if (not isinstance(raw,list) or any(not isinstance(s,str) or not re.fullmatch(r'(?:sh|sz)\d{6}',s) for s in raw)
            or raw!=sorted(set(raw))):
        put(store,'remote_research_only',None)
        return
    put(store,'remote_research_only',{'symbols':raw,'at':packet['at'],'ledger_version':packet['ledger_version']})


def enforce_plan(store, config, symbol, plan):
    if contains(store, config, symbol) or plan.get('research_only') is True:
        plan['research_only'] = True
        plan['kind'] = 'RESEARCH_ONLY'
        plan['research_gaps'] = list(dict.fromkeys(plan.get('research_gaps', []) + [
            b for b in plan.get('blockers', []) if b not in ('LOT_EXCEEDS_CAP', 'RESEARCH_MEMBERSHIP_REVIEW')]))
        plan['blockers'] = []
        plan['notice'] = NOTICE
    return plan


def trading_sample(plan):
    """Use the frozen historical plan, never today's membership, for denominators."""
    return plan.get('kind') != 'RESEARCH_ONLY' and plan.get('research_only') is not True
