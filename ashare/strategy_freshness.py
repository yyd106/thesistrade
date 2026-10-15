"""Read-only, bounded strategy-age diagnostics; never authorizes an execution.

The receipt timeline measures research-lease availability, not host uptime or
historical activation. A receipt covers [received_at, completed_at + 12 hours).
Unobserved history remains unknown. Fill groups are disjoint and fill-weighted.
"""
import json
import math
from datetime import datetime, timedelta

from .storage import normalize_time, now

METHOD = 'strategy-freshness-v1'
MAX_BYTES = 32768
LEASE_SECONDS = 43200
ROUTES = ('watchlist', 'global', 'dynamic')
FILL_GROUPS = ('BUY', 'SELL', 'PROTECTIVE_EXIT', 'UNKNOWN')
MAX_NUMBER = 10 ** 12


def _time(value):
    try:
        return datetime.fromisoformat(normalize_time(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _stamp(value):
    return normalize_time(value.isoformat()) if value else None


def _object(value):
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _state(store, key):
    row = store.db.execute('SELECT value FROM cloud_state WHERE key=?', (key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return None


def _has_table(store, table):
    return bool(store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _age(at, origin):
    origin = _time(origin)
    if origin is None or origin > at:
        return None
    seconds = (at - origin).total_seconds()
    return seconds if seconds <= MAX_NUMBER else None


def _ages(values, unknown=False):
    if unknown:
        return {'known': None, 'unknown': None, 'mean_seconds': None, 'max_seconds': None}
    known = [v for v in values if v is not None]
    return {'known': len(known), 'unknown': len(values) - len(known),
            'mean_seconds': round(sum(known) / len(known), 1) if known else None,
            'max_seconds': round(max(known), 1) if known else None}


def _heartbeat(store, config, at):
    cloud = config.get('deployment_role') == 'cloud'
    table = 'cloud_receipts' if cloud else 'freshness_receipts'
    rows = list(store.db.execute('SELECT sequence,received_at,completed_at FROM ' + table)) if _has_table(store, table) else []
    history = _object(_state(store, 'freshness_history'))
    valid, invalid = [], 0
    for row in rows:
        received, completed = _time(row['received_at']), _time(row['completed_at'])
        if received and received > at:
            continue
        if received is None or completed is None or completed > received or received >= completed + timedelta(seconds=LEASE_SECONDS):
            invalid += 1
            continue
        valid.append((received, completed + timedelta(seconds=LEASE_SECONDS), completed))
    # The original cloud table is the complete accepted-receipt log. A replica
    # must use explicit export coverage, never its earliest copied receipt.
    observed_from = min((r[0] for r in valid), default=None) if cloud else _time(history.get('observed_from'))
    observed_until = at if cloud and observed_from else _time(history.get('observed_until'))
    if observed_until:
        observed_until = min(at, observed_until)
    if not observed_from or not observed_until or observed_from > observed_until or invalid:
        observed_from = observed_until = None
    intervals = []
    for start, end, _ in sorted(valid):
        if intervals and start <= intervals[-1][1]:
            intervals[-1][1] = max(end, intervals[-1][1])
        else:
            intervals.append([start, end])
    latest = max(valid, default=None, key=lambda r: r[0])
    # Replica state is known only through its last observation; do not extrapolate
    # a healthy cloud state beyond an offline synchronization gap.
    current_known = observed_until is not None and observed_until >= at
    active = bool(latest and latest[0] <= at < latest[1])
    result = {'state': ('ACTIVE' if active else 'EXPIRED') if current_known else 'UNKNOWN',
              'initialized': _state(store, 'initialized') if cloud else history.get('initialized'),
              'execution_enabled': _state(store, 'execution_enabled') if cloud else history.get('execution_enabled'),
              'completed_at': _stamp(latest[2]) if latest else None,
              'received_at': _stamp(latest[0]) if latest else None,
              'observed_from': _stamp(observed_from), 'observed_until': _stamp(observed_until),
              'invalid_receipt_count': invalid, 'windows': {}}
    for key in ('initialized', 'execution_enabled'):
        if type(result[key]) is not bool:
            result[key] = None
    for label, duration in (('24h', 86400), ('7d', 7 * 86400)):
        start = at - timedelta(seconds=duration)
        a = max(start, observed_from) if observed_from else None
        b = min(at, observed_until) if observed_until else None
        observed = max(0, (b - a).total_seconds()) if a and b else 0
        gaps = []
        if observed:
            cursor = a
            for left, right in intervals:
                if right <= a or left >= b:
                    continue
                if left > cursor:
                    gaps.append((cursor, min(left, b)))
                cursor = max(cursor, min(right, b))
            if cursor < b:
                gaps.append((cursor, b))
        seconds = [(right - left).total_seconds() for left, right in gaps]
        result['windows'][label] = {
            'start_at': _stamp(start), 'end_at': _stamp(at),
            'observed_seconds': observed, 'unknown_seconds': duration - observed,
            'coverage_complete': observed == duration,
            'interruption_count': len(gaps) if observed else None,
            'interruption_seconds': sum(seconds) if observed else None,
            'longest_interruption_seconds': max(seconds, default=0) if observed else None,
            'ongoing_interruption': bool(gaps and gaps[-1][1] == b) if observed else None}
    return result


def _plan_rows(store, route, at):
    # Archived plans can contain large research payloads. Only today's active
    # rows and actual, already-recorded fills in this report's seven-day window
    # can contribute to the diagnostic. Late ledger rows must not widen it.
    start, end = _stamp(at - timedelta(days=7)), _stamp(at)
    args = (start, end, end)
    if route == 'watchlist':
        return store.db.execute('''SELECT p.id,p.activated_at issued_at,p.valid_until,p.status,
            p.payload_json,s.created_at research_at,s.model_status FROM plans p
            LEFT JOIN studies s ON s.id=p.study_id WHERE p.status='ACTIVE' OR p.id IN (
                SELECT o.plan_id FROM paper_fills f JOIN paper_orders o ON o.id=f.order_id
                WHERE f.occurred_at>=? AND f.occurred_at<? AND f.recorded_at<=?)''', args)
    if route == 'global':
        return store.db.execute('''SELECT id,created_at issued_at,valid_until,status,payload_json,
            created_at research_at,NULL model_status FROM global_plans WHERE status='ACTIVE' OR id IN (
                SELECT o.plan_id FROM global_fills f JOIN global_orders o ON o.id=f.order_id
                WHERE f.occurred_at>=? AND f.occurred_at<? AND f.recorded_at<=?)''', args)
    return store.db.execute('''SELECT id,created_at issued_at,expires_at valid_until,status,
        plan_json payload_json,created_at research_at,NULL model_status FROM dynamic_cases
        WHERE status IN ('RESEARCH','READY','HOLDING') OR id IN (
            SELECT o.case_id FROM dynamic_fills f JOIN dynamic_orders o ON o.id=f.order_id
            WHERE f.occurred_at>=? AND f.occurred_at<? AND f.recorded_at<=?)''', args)


def _plan_times(row, route):
    if row is None:
        return None, None
    payload = _object(row['payload_json'])
    successful = (row['model_status'] == 'SUCCEEDED') if route == 'watchlist' else (
        payload.get('model_status') == 'SUCCEEDED' if route == 'global' else True)
    return row['issued_at'], row['research_at'] if successful else None


def _fill_rows(store, route, start, end):
    if route == 'watchlist':
        return store.db.execute('''SELECT f.occurred_at,f.side,o.plan_id,p.payload_json plan_payload,
            d.reason,d.payload_json exit_payload FROM paper_fills f
            LEFT JOIN paper_orders o ON o.id=f.order_id LEFT JOIN plans p ON p.id=o.plan_id
            LEFT JOIN decisions d ON d.id=o.decision_id
            WHERE f.occurred_at>=? AND f.occurred_at<? AND f.recorded_at<=?''', (start, end, end))
    if route == 'global':
        return store.db.execute('''SELECT f.occurred_at,f.side,o.plan_id,NULL plan_payload,
            NULL reason,o.payload_json exit_payload FROM global_fills f
            LEFT JOIN global_orders o ON o.id=f.order_id
            WHERE f.occurred_at>=? AND f.occurred_at<? AND f.recorded_at<=?''', (start, end, end))
    return store.db.execute('''SELECT f.occurred_at,f.side,o.case_id plan_id,NULL plan_payload,
        o.reason,o.terms_json exit_payload FROM dynamic_fills f
        LEFT JOIN dynamic_orders o ON o.id=f.order_id
        WHERE f.occurred_at>=? AND f.occurred_at<? AND f.recorded_at<=?''', (start, end, end))


def _fill_group(row, route):
    if row['side'] == 'BUY':
        return 'BUY', False
    if row['side'] != 'SELL':
        return 'UNKNOWN', True
    payload = _object(row['exit_payload'])
    from .review_checks import WATCHLIST_EXIT_CODES, GLOBAL_EXIT_CODES, DYNAMIC_EXIT_CODES
    if route == 'watchlist':
        if _object(row['plan_payload']).get('kind') == 'RISK_EXIT_ONLY':
            return 'PROTECTIVE_EXIT', False
        code = payload.get('risk_trigger') if 'risk_trigger' in payload else (row['reason'] or '').split('；', 1)[0]
        known = code in WATCHLIST_EXIT_CODES if isinstance(code, str) else False
    elif route == 'global':
        code = payload.get('reason')
        known = code in GLOBAL_EXIT_CODES if isinstance(code, str) else False
    else:
        code = DYNAMIC_EXIT_CODES.get(row['reason'])
        known = code is not None
    return ('PROTECTIVE_EXIT' if known and code != 'PORTFOLIO_REDUCE' else 'SELL'), not known


def _fills(store, config, at, plans):
    start = _stamp(at - timedelta(days=7))
    end = _stamp(at)
    cloud = config.get('deployment_role') == 'cloud'
    observed = at if cloud else _time(_state(store, 'remote_ledger_at'))
    result = {'window_start': start, 'window_end': end, 'ledger_observed_until': _stamp(min(at, observed)) if observed else None,
              'coverage_state': 'CURRENT' if cloud else 'REPLICA' if observed else 'UNKNOWN', 'routes': {}}
    for route in ROUTES:
        groups = {key: {'plan': [], 'research': [], 'classification_unknown_count': 0} for key in FILL_GROUPS}
        for row in _fill_rows(store, route, start, end):
            when = _time(row['occurred_at'])
            key, uncertain = _fill_group(row, route)
            group = groups[key]
            issued, research = _plan_times(plans[route].get(row['plan_id']), route)
            group['plan'].append(_age(when, issued) if when else None)
            group['research'].append(_age(when, research) if when else None)
            group['classification_unknown_count'] += uncertain
        result['routes'][route] = {
            key: {'count': len(group['plan']), 'classification_unknown_count': group['classification_unknown_count'],
                  'plan_age': _ages(group['plan']), 'research_age': _ages(group['research'])}
            for key, group in groups.items()}
    return result


def _current_plans(config, at, plans, checked):
    current = abs((checked - at).total_seconds()) <= 60
    result = {'as_of': _stamp(at), 'status': 'CURRENT' if current else 'UNKNOWN_HISTORICAL_STATUS', 'routes': {}}
    for route in ROUTES:
        active_states = ('ACTIVE',) if route != 'dynamic' else ('RESEARCH', 'READY', 'HOLDING')
        rows = [row for row in plans[route].values() if row['status'] in active_states]
        valid, future, expired, unknown = [], 0, 0, 0
        for row in rows:
            issued, expiry = _time(row['issued_at']), _time(row['valid_until'])
            if not issued or not expiry or expiry <= issued:
                unknown += 1
            elif issued > at:
                future += 1
            elif expiry <= at:
                expired += 1
            else:
                valid.append(row)
        threshold = config.get('plan_max_age_hours') if route == 'watchlist' else None
        if type(threshold) not in (int, float) or not math.isfinite(threshold) or threshold <= 0:
            threshold = None
        issued_ages = [_age(at, _plan_times(row, route)[0]) for row in valid]
        research_ages = [_age(at, _plan_times(row, route)[1]) for row in valid]
        remaining = [(_time(row['valid_until']) - at).total_seconds() for row in valid]
        age_eligible = [_age(at, row['issued_at']) for row in rows]
        result['routes'][route] = {
            'status_rows': len(rows) if current else None,
            'time_valid_count': len(valid) if current else None,
            'expired_active_count': expired if current else None,
            'future_active_count': future if current else None,
            'unknown_validity_count': unknown if current else None,
            'protective_count': sum(_object(row['payload_json']).get('kind') == 'RISK_EXIT_ONLY' for row in valid) if current else None,
            'ready_count': sum(row['status'] == 'READY' for row in valid) if current and route == 'dynamic' else None,
            'max_age_threshold_seconds': threshold * 3600 if threshold else None,
            'over_max_age_count': sum(age is not None and age > threshold * 3600 for age in age_eligible) if current and threshold else None,
            'plan_age': _ages(issued_ages, unknown=not current),
            'research_age': _ages(research_ages, unknown=not current),
            'min_remaining_seconds': min(remaining) if current and remaining else None,
            'max_remaining_seconds': max(remaining) if current and remaining else None}
    return result


def summary(store, config, at):
    """Return fixed-schema public aggregates, or None when the feature is off.

    Windows are [start, end), measured in elapsed seconds with Asia/Shanghai
    labels. A partial/unknown coverage window is never reported as zero outage.
    Current ACTIVE status is not replayable for all routes; a historical request
    returns unknown plan-state counts instead of recycling today's state.
    """
    if not config.get('strategy_freshness_enabled', True):
        return None
    stamp = normalize_time(at)
    when = _time(stamp)
    plans = {route: {row['id']: dict(row) for row in _plan_rows(store, route, when)} for route in ROUTES}
    result = {'method': METHOD, 'as_of': stamp, 'timezone': 'Asia/Shanghai', 'lease_seconds': LEASE_SECONDS,
              'heartbeat': _heartbeat(store, config, when), 'fills': _fills(store, config, when, plans),
              'plans': _current_plans(config, when, plans, _time(now()))}
    return validate_public_summary(result)


# Only these fixed keys, timestamps, enums, and bounded numerical aggregates may
# cross the display boundary. Validation rejects prose, symbols and extra fields.
_AGE_SCHEMA = {'known': 'count', 'unknown': 'count', 'mean_seconds': 'number', 'max_seconds': 'number'}
_WINDOW_SCHEMA = {'start_at': 'time', 'end_at': 'time', 'observed_seconds': 'number', 'unknown_seconds': 'number',
                  'coverage_complete': 'bool', 'interruption_count': 'count', 'interruption_seconds': 'number',
                  'longest_interruption_seconds': 'number', 'ongoing_interruption': 'bool'}
_FILL_SCHEMA = {'count': 'count', 'classification_unknown_count': 'count', 'plan_age': _AGE_SCHEMA, 'research_age': _AGE_SCHEMA}
_PLAN_SCHEMA = {**{key: 'count' for key in ('status_rows', 'time_valid_count', 'expired_active_count', 'future_active_count',
                 'unknown_validity_count', 'protective_count', 'ready_count', 'over_max_age_count')},
                **{key: 'number' for key in ('max_age_threshold_seconds', 'min_remaining_seconds', 'max_remaining_seconds')},
                'plan_age': _AGE_SCHEMA, 'research_age': _AGE_SCHEMA}
PUBLIC_SCHEMA = {
    'method': (METHOD,), 'as_of': 'required_time', 'timezone': ('Asia/Shanghai',), 'lease_seconds': (LEASE_SECONDS,),
    'heartbeat': {'state': ('ACTIVE', 'EXPIRED', 'UNKNOWN'), 'initialized': 'bool', 'execution_enabled': 'bool',
                  'completed_at': 'time', 'received_at': 'time', 'observed_from': 'time', 'observed_until': 'time',
                  'invalid_receipt_count': 'count', 'windows': {'24h': _WINDOW_SCHEMA, '7d': _WINDOW_SCHEMA}},
    'fills': {'window_start': 'time', 'window_end': 'time', 'ledger_observed_until': 'time',
              'coverage_state': ('CURRENT', 'REPLICA', 'UNKNOWN'),
              'routes': {route: {group: _FILL_SCHEMA for group in FILL_GROUPS} for route in ROUTES}},
    'plans': {'as_of': 'time', 'status': ('CURRENT', 'UNKNOWN_HISTORICAL_STATUS'),
              'routes': {route: _PLAN_SCHEMA for route in ROUTES}}}


def validate_public_summary(packet):
    """Reject unsafe or malformed display packets; return the validated object."""
    def check(value, schema):
        if isinstance(schema, dict):
            if not isinstance(value, dict) or set(value) != set(schema):
                raise ValueError('策略新鲜度字段不符')
            for key in schema:
                check(value[key], schema[key])
        elif isinstance(schema, tuple):
            if value not in schema or type(value) is not type(schema[0]):
                raise ValueError('策略新鲜度枚举不符')
        elif value is None and schema != 'required_time':
            return
        elif schema in ('number', 'count'):
            allowed = (int,) if schema == 'count' else (int, float)
            if type(value) not in allowed or not math.isfinite(value) or not 0 <= value <= MAX_NUMBER:
                raise ValueError('策略新鲜度数值不符')
        elif schema == 'bool':
            if type(value) is not bool:
                raise ValueError('策略新鲜度状态不符')
        elif schema in ('time', 'required_time'):
            if type(value) is not str or _time(value) is None or normalize_time(value) != value:
                raise ValueError('策略新鲜度时间不符')
    check(packet, PUBLIC_SCHEMA)
    if len(json.dumps(packet, ensure_ascii=False, allow_nan=False).encode()) > MAX_BYTES:
        raise ValueError('策略新鲜度摘要过大')
    return packet
