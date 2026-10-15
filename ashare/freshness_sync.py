"""Bounded freshness telemetry. Never grants or renews execution authority."""
import copy
from datetime import datetime, timedelta

from .storage import now, normalize_time, digest
from .cloud_protocol import canonical
from .cloud_runtime import value, put, lease

FEATURE = 'strategy_freshness_v1'
VERSION = 'strategy-freshness-display-v1'
HISTORY_VERSION = 'strategy-freshness-history-v1'
STATE = 'display_strategy_freshness'
HISTORY_LIMIT = 12000
MAX_BYTES = 65536


def history(store, at):
    """Complete bounded receipt window, plus its predecessor; no research content."""
    at = normalize_time(at)
    start = normalize_time((datetime.fromisoformat(at)-timedelta(days=8)).isoformat())
    first = store.db.execute('SELECT min(received_at) FROM cloud_receipts WHERE received_at<=?', (at,)).fetchone()[0]
    rows = [dict(r) for r in store.db.execute('''SELECT sequence,received_at,completed_at FROM cloud_receipts
        WHERE received_at<=? ORDER BY received_at DESC,sequence DESC LIMIT ?''', (at, HISTORY_LIMIT+1))]
    selected = [r for r in rows if r['received_at'] >= start]
    before = next((r for r in rows if r['received_at'] < start), None)
    if before:
        selected.append(before)
    selected = selected[:HISTORY_LIMIT]
    selected.reverse()
    coverage = max(start, first) if first else None
    if selected and len(rows) > HISTORY_LIMIT and selected[0]['received_at'] > start:
        coverage = max(coverage, selected[0]['received_at'])
    latest = selected[-1] if selected else {}
    return {'version': HISTORY_VERSION, 'observed_from': coverage, 'observed_until': at,
            'initialized': bool(value(store, 'initialized', False)),
            'execution_enabled': bool(value(store, 'execution_enabled', False)),
            'completed_at': latest.get('completed_at'), 'received_at': latest.get('received_at'), 'rows': selected}


def mirror_history(store, packet):
    """Called inside the ledger extra SAVEPOINT; receipts are immutable locally."""
    keys = {'version','observed_from','observed_until','initialized','execution_enabled','completed_at','received_at','rows'}
    if (not isinstance(packet, dict) or set(packet) != keys or packet['version'] != HISTORY_VERSION
            or type(packet['initialized']) is not bool or type(packet['execution_enabled']) is not bool
            or not isinstance(packet['rows'], list) or len(packet['rows']) > HISTORY_LIMIT
            or len(canonical(packet)) > 2_000_000):
        raise ValueError('策略时间记录格式无效')
    until = normalize_time(packet['observed_until'])
    start = normalize_time(packet['observed_from']) if packet['observed_from'] is not None else None
    if start and start > until:
        raise ValueError('策略时间覆盖窗口无效')
    previous = value(store, 'freshness_history') or {}
    if previous.get('observed_until', '') > until:
        return
    seen = set()
    for row in packet['rows']:
        if (not isinstance(row, dict) or set(row) != {'sequence','received_at','completed_at'}
                or type(row['sequence']) is not int or row['sequence'] < 1 or row['sequence'] in seen):
            raise ValueError('策略时间凭证无效')
        seen.add(row['sequence'])
        completed, received = normalize_time(row['completed_at']), normalize_time(row['received_at'])
        age = (datetime.fromisoformat(received)-datetime.fromisoformat(completed)).total_seconds()
        if not 0 <= age < 43200 or received > until:
            raise ValueError('策略时间凭证超出有效期')
        old = store.db.execute('SELECT received_at,completed_at FROM freshness_receipts WHERE sequence=?', (row['sequence'],)).fetchone()
        if old and tuple(old) != (received, completed):
            raise ValueError('策略时间凭证与既有记录冲突')
        store.db.execute('INSERT OR IGNORE INTO freshness_receipts VALUES(?,?,?)', (row['sequence'],received,completed))
    if packet['rows'] and not start:
        raise ValueError('策略时间缺少覆盖起点')
    for key in ('completed_at','received_at'):
        if packet[key] is not None:
            normalize_time(packet[key])
    if packet['rows']:
        last = max(packet['rows'], key=lambda r:(r['received_at'],r['sequence']))
        if any(packet[k] != last[k] for k in ('received_at','completed_at')):
            raise ValueError('策略时间最新凭证不匹配')
    elif packet['completed_at'] is not None or packet['received_at'] is not None or start:
        raise ValueError('空策略时间记录不能声明完整覆盖')
    # Keep accumulated coverage only when there is no unobserved gap between pulls.
    if start and previous.get('observed_from') and previous.get('observed_until', '') >= start:
        start = min(start, previous['observed_from'])
    put(store, 'freshness_history', {k:v for k,v in packet.items() if k not in ('rows','version')} | {'observed_from': start})


def local_view(store, config, at=None):
    if not config.get('strategy_freshness_enabled', True):
        return None
    at = normalize_time(at or now())
    from .strategy_freshness import summary, validate_public_summary
    try:
        result = validate_public_summary(summary(store, config, at))
        return {'status': 'OK', 'as_of': at, 'summary': result, 'stale': False}
    except Exception:
        # An unavailable statistic is not an empty or healthy result.
        return {'status': 'UNAVAILABLE', 'as_of': at, 'summary': None, 'stale': False}


def packet_hash(packet):
    return digest(canonical({k:v for k,v in packet.items() if k != 'content_hash'}))


def receive(store, packet, at):
    from .strategy_freshness import validate_public_summary
    required = {'version','generated_at','enabled','status','summary','content_hash'}
    if (not isinstance(packet, dict) or set(packet) != required or packet['version'] != VERSION
            or type(packet['enabled']) is not bool or packet['status'] not in ('OK','UNAVAILABLE','DISABLED')
            or len(canonical(packet)) > MAX_BYTES):
        raise ValueError('策略新鲜度摘要格式无效')
    generated, received = normalize_time(packet['generated_at']), normalize_time(at)
    if datetime.fromisoformat(generated) > datetime.fromisoformat(received)+timedelta(minutes=5):
        raise ValueError('策略新鲜度摘要时间超前')
    if packet['status'] == 'OK':
        if not packet['enabled']:
            raise ValueError('关闭的策略新鲜度不能包含统计')
        validate_public_summary(packet['summary'])
        if packet['summary']['as_of'] != generated:
            raise ValueError('策略新鲜度截止时间不一致')
    elif packet['summary'] is not None or (packet['status'] == 'DISABLED') == packet['enabled']:
        raise ValueError('策略新鲜度空态无效')
    if packet['content_hash'] != packet_hash(packet):
        raise ValueError('策略新鲜度摘要校验不符')
    prior = value(store, STATE)
    status = 'UPDATED'
    if prior and generated < prior['generated_at']:
        status = 'IGNORED_STALE'
    elif prior and generated == prior['generated_at']:
        if prior['content_hash'] != packet['content_hash']:
            raise ValueError('同一时间的新鲜度摘要冲突')
        status = 'UNCHANGED'
    else:
        put(store, STATE, packet)
    latest = value(store, STATE)
    return {'status': 'ACCEPTED', 'freshness': {'status':status,
            'generated_at':latest['generated_at'], 'content_hash':latest['content_hash']}}


def deliver(store, config, cancel_event=None):
    from .cloud_sync import remote_supports, request
    if config.get('deployment_role') != 'research' or not remote_supports(store, FEATURE):
        return None
    at = normalize_time(now())
    view = local_view(store, config, at)
    packet = {'version': VERSION, 'generated_at': at, 'enabled': view is not None,
              'status': view['status'] if view else 'DISABLED', 'summary': view['summary'] if view else None}
    packet['content_hash'] = packet_hash(packet)
    if cancel_event is not None and cancel_event.is_set():
        return None
    # A disabled feature needs only a tombstone; live metrics are refreshed each minute.
    if not packet['enabled'] and value(store, 'freshness_sent_disabled', False):
        return None
    # The cloud may accept a request whose response is lost. Clear the local
    # tombstone cache before enabling so a later disable still reaches it.
    if packet['enabled']:
        with store.db:
            put(store, 'freshness_sent_disabled', False)
    answer = request(config, '/api/sync/freshness', packet)
    receipt = answer.get('freshness') or {}
    if answer.get('status') != 'ACCEPTED' or receipt.get('status') not in ('UPDATED','UNCHANGED','IGNORED_STALE'):
        raise ValueError('云端未确认策略新鲜度摘要')
    if receipt['status'] != 'IGNORED_STALE' and any(receipt.get(k) != packet[k] for k in ('generated_at','content_hash')):
        raise ValueError('云端策略新鲜度回执不匹配')
    with store.db:
        put(store, 'freshness_sent_disabled', not packet['enabled'] and receipt['status'] != 'IGNORED_STALE')
        put(store, 'freshness_sync', {'at':at, 'status':'OK', 'generated_at':receipt['generated_at']})
    return answer


def cloud_view(store, config, at=None):
    if not config.get('strategy_freshness_enabled', True):
        return None
    at = normalize_time(at or now())
    packet = value(store, STATE)
    if packet and not packet['enabled']:
        return None
    result = {'status':packet['status'] if packet else 'AWAITING_SYNC',
              'as_of':packet['generated_at'] if packet else None,
              'summary':copy.deepcopy(packet['summary']) if packet else None,
              'stale':not packet or (datetime.fromisoformat(at)-datetime.fromisoformat(packet['generated_at'])).total_seconds()>180}
    # Computed on the authoritative node at read time, even when the local Mac is offline.
    current = lease(store, config, at)
    current['remaining_seconds'] = max(0, int((datetime.fromisoformat(current['expires_at'])-datetime.fromisoformat(at)).total_seconds())) if current.get('expires_at') else None
    result['current_lease'] = current
    return result
