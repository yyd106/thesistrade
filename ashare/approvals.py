"""Version-bound approvals from authenticated administrators, never approver-name strings.

These records protect application entry points. They do not isolate a process
with unrestricted write access to the same operating-system account/database.
Only action summaries and fingerprints travel over the existing notice channel.
"""
import json
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from .storage import digest, now, normalize_time

VERSION = 'approval-v1'
KINDS = {'PROPOSAL_DECISION', 'CONFIG'}
REQUEST_FIELDS = {'id', 'version', 'kind', 'subject_id', 'action', 'snapshot', 'summary',
                  'created_at', 'expires_at', 'notice_id', 'hash'}
RECEIPT_FIELDS = {'id', 'request_id', 'request_hash', 'decision', 'actor', 'authority', 'issued_at'}
REQUEST_ID = re.compile(r'AR-[0-9a-f]{32}')
RECEIPT_ID = re.compile(r'AC-[0-9a-f]{32}')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


@contextmanager
def atomic(store):
    """Lock before checking state; a nested operation never commits its caller."""
    nested = store.db.in_transaction
    savepoint = 'approval_' + uuid.uuid4().hex
    store.db.execute('SAVEPOINT ' + savepoint if nested else 'BEGIN IMMEDIATE')
    try:
        yield
        if nested:
            store.db.execute('RELEASE SAVEPOINT ' + savepoint)
        else:
            store.db.commit()
    except BaseException:
        if nested:
            store.db.execute('ROLLBACK TO SAVEPOINT ' + savepoint)
            store.db.execute('RELEASE SAVEPOINT ' + savepoint)
        else:
            store.db.rollback()
        raise


def _validate(value):
    if not isinstance(value, dict) or set(value) != REQUEST_FIELDS:
        raise ValueError('审批请求字段不匹配')
    if not isinstance(value['id'], str) or not REQUEST_ID.fullmatch(value['id']):
        raise ValueError('审批请求编号无效')
    if value['notice_id'] != 'N-' + value['id'] or value['version'] != VERSION or value['kind'] not in KINDS:
        raise ValueError('审批请求版本或来源无效')
    if not all(isinstance(value[k], str) and 0 < len(value[k]) <= 200 for k in ('subject_id', 'action')):
        raise ValueError('审批对象或动作无效')
    actions = {'APPROVED', 'ADOPTED', 'RETIRED', 'REJECTED', 'SUPERSEDED'} if value['kind'] == 'PROPOSAL_DECISION' else {'APPLY'}
    if value['action'] not in actions or not isinstance(value['snapshot'], dict) or not isinstance(value['summary'], dict):
        raise ValueError('审批动作或冻结预览无效')
    if len(canonical(value).encode()) > 65536:
        raise ValueError('审批摘要过长，请整理为可完整核对的单项方案')
    start, end = (normalize_time(value[k]) for k in ('created_at', 'expires_at'))
    if value['created_at'] != start or value['expires_at'] != end or datetime.fromisoformat(end) - datetime.fromisoformat(start) != timedelta(hours=24):
        raise ValueError('审批有效期无效')
    if value['hash'] != digest(canonical({k: v for k, v in value.items() if k != 'hash'})):
        raise ValueError('审批请求内容哈希不一致')
    return value


def envelope(store, identity):
    row = store.db.execute('SELECT payload_json FROM approval_requests WHERE id=?', (identity,)).fetchone()
    if not row:
        raise ValueError('未找到审批请求')
    return _validate(json.loads(row[0]))


def receive_request(store, value):
    """Import immutable request content only; no client-supplied authority fields."""
    value = _validate(value)
    with atomic(store):
        old = store.db.execute('SELECT payload_json FROM approval_requests WHERE id=?', (value['id'],)).fetchone()
        if old:
            if canonical(json.loads(old[0])) != canonical(value):
                raise ValueError('同一审批编号的冻结内容发生冲突')
        else:
            store.db.execute('INSERT INTO approval_requests VALUES(?,?,?,?,?,?,?,?)',
                             (value['id'], value['created_at'], value['expires_at'], value['kind'], value['subject_id'],
                              value['action'], value['hash'], canonical(value)))
    return value


def create_request(store, *, kind, subject_id, action, snapshot, summary, at=None):
    stamp = normalize_time(at or now())
    with atomic(store):
        for row in store.db.execute('''SELECT r.id FROM approval_requests r
                LEFT JOIN approval_receipts a ON a.request_id=r.id
                LEFT JOIN approval_consumptions c ON c.request_id=r.id
                WHERE r.kind=? AND r.subject_id=? AND r.action=? AND r.expires_at>?
                AND c.request_id IS NULL AND (a.decision IS NULL OR a.decision='APPROVE')
                ORDER BY r.created_at DESC''', (kind, subject_id, action, stamp)):
            old = envelope(store, row['id'])
            if canonical(old['snapshot']) == canonical(snapshot) and canonical(old['summary']) == canonical(summary):
                return request_info(store, row['id'], at=stamp)
        identity = 'AR-' + uuid.uuid4().hex
        value = {'id': identity, 'version': VERSION, 'kind': kind, 'subject_id': subject_id,
                 'action': action, 'snapshot': snapshot, 'summary': summary, 'created_at': stamp,
                 'expires_at': normalize_time((datetime.fromisoformat(stamp) + timedelta(hours=24)).isoformat()),
                 'notice_id': 'N-' + identity}
        value['hash'] = digest(canonical(value))
        receive_request(store, value)
        from .notices import insert
        title = '确认治理变更：' + str(summary.get('title') or subject_id)
        insert(store, title=title[:80], body='请核对下方冻结的对象、内容版本、动作和替代范围。批准只授权本次动作，程序仍会在实施前再次核验；这不是策略有效性的证明。',
               kind='DECISION', author='agent', at=stamp, notice_id=value['notice_id'],
               payload={'deadline': value['expires_at'], 'approval_request': value})
        return request_info(store, identity, at=stamp)


def receipt_state(store, identity):
    row = store.db.execute('SELECT * FROM approval_receipts WHERE request_id=?', (identity,)).fetchone()
    return dict(row) if row else None


def _window(request, at):
    if not request['created_at'] <= at < request['expires_at']:
        raise ValueError('审批请求已过期或尚未生效，请重新申请并由管理员确认')


def approve(store, identity, expected_hash, user, decision='APPROVE', authority='LOCAL_ADMIN', at=None):
    """The HTTP handler supplies the verified session; no named CLI approval exists."""
    stamp = normalize_time(at or now())
    with atomic(store):
        token_hash = user.get('token_hash') if isinstance(user, dict) else None
        session = store.db.execute('''SELECT s.username FROM app_sessions s JOIN app_users u ON u.username=s.username
            WHERE s.token_hash=? AND s.expires_at>? AND s.created_at<=? AND u.role='ADMIN' ''',
                                  (token_hash, stamp, stamp)).fetchone()
        if not session or session['username'] != 'admin':
            raise ValueError('审批须由已登录管理员确认，批准人文字不能授权')
        if authority not in ('LOCAL_ADMIN', 'CLOUD_ADMIN') or decision not in ('APPROVE', 'REJECT'):
            raise ValueError('审批来源或决定无效')
        request = envelope(store, identity)
        if expected_hash != request['hash']:
            raise ValueError('页面审批内容已变化，请重新核对')
        old = receipt_state(store, identity)
        if old:
            if old['decision'] != decision:
                raise ValueError('该审批已有决定，不能覆盖历史回执')
            return old
        _window(request, stamp)
        receipt = {'id': 'AC-' + uuid.uuid4().hex, 'request_id': identity, 'request_hash': request['hash'],
                   'decision': decision, 'actor': session['username'], 'authority': authority, 'issued_at': stamp}
        store.db.execute('INSERT INTO approval_receipts VALUES(?,?,?,?,?,?,?)', tuple(receipt[k] for k in
                         ('id', 'request_id', 'request_hash', 'decision', 'actor', 'authority', 'issued_at')))
        return receipt


def mirror_receipt(store, receipt):
    """Called only on the authenticated cloud response, never on uploaded notices."""
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS:
        raise ValueError('审批回执字段不匹配')
    if (not isinstance(receipt['id'], str) or not RECEIPT_ID.fullmatch(receipt['id'])
            or receipt['actor'] != 'admin' or receipt['authority'] != 'CLOUD_ADMIN'
            or receipt['decision'] not in ('APPROVE', 'REJECT')):
        raise ValueError('审批回执来源无效')
    with atomic(store):
        request = envelope(store, receipt['request_id'])
        issued = normalize_time(receipt['issued_at'])
        _window(request, issued)
        if receipt['request_hash'] != request['hash'] or issued != receipt['issued_at']:
            raise ValueError('审批回执与本机冻结请求不一致')
        old = receipt_state(store, receipt['request_id'])
        if old:
            if canonical(old) != canonical(receipt):
                raise ValueError('审批回执冲突，保留原决定')
        else:
            store.db.execute('INSERT INTO approval_receipts VALUES(?,?,?,?,?,?,?)', tuple(receipt[k] for k in
                             ('id', 'request_id', 'request_hash', 'decision', 'actor', 'authority', 'issued_at')))
    return receipt


def _reservation(store, identity):
    row = store.db.execute('SELECT * FROM approval_reservations WHERE request_id=? AND released_at IS NULL', (identity,)).fetchone()
    return dict(row) if row else None


def _usable(store, identity, stamp, operation_id=None):
    request = envelope(store, identity)
    receipt = receipt_state(store, identity)
    if not receipt or receipt['decision'] != 'APPROVE' or receipt['request_hash'] != request['hash']:
        raise ValueError('需要已登录管理员确认的审批回执，批准人文字不能授权')
    if store.db.execute('SELECT 1 FROM approval_consumptions WHERE request_id=?', (identity,)).fetchone():
        raise ValueError('审批回执已经使用，不能重复实施')
    reservation = _reservation(store, identity)
    if reservation and reservation['operation_id'] != operation_id:
        raise ValueError('审批回执已有实施中的操作，请先恢复该操作')
    authorized_at = reservation['reserved_at'] if reservation else stamp
    if receipt['issued_at'] > authorized_at or authorized_at > stamp:
        raise ValueError('审批实施时间不能早于确认或预留时间')
    _window(request, authorized_at)
    return receipt


def check_receipt(store, approval_id, *, kind, subject_id, action, snapshot, at=None, operation_id=None):
    if not approval_id:
        raise ValueError('需要审批回执 --approval；批准人文字不能授权')
    request = envelope(store, approval_id)
    if (request['kind'], request['subject_id'], request['action']) != (kind, subject_id, action):
        raise ValueError('审批回执的对象或动作不匹配')
    if canonical(request['snapshot']) != canonical(snapshot):
        raise ValueError('审批后的内容版本、原状态或替代范围已变化，请重新申请')
    return _usable(store, approval_id, normalize_time(at or now()), operation_id)


def reserve(store, identity, operation_id, at=None):
    stamp = normalize_time(at or now())
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 120:
        raise ValueError('实施操作编号无效')
    with atomic(store):
        _usable(store, identity, stamp, operation_id)
        old = _reservation(store, identity)
        if not old:
            store.db.execute('INSERT INTO approval_reservations VALUES(?,?,?,NULL)', (identity, operation_id, stamp))
        return {'status': 'APPLYING', 'request_id': identity, 'operation_id': operation_id}


def release(store, identity, operation_id, at=None):
    with atomic(store):
        if store.db.execute('SELECT 1 FROM approval_consumptions WHERE request_id=?', (identity,)).fetchone():
            raise ValueError('已完成的审批不能释放')
        reservation = _reservation(store, identity)
        if reservation and reservation['operation_id'] != operation_id:
            raise ValueError('不能释放其他操作的审批')
        store.db.execute('UPDATE approval_reservations SET released_at=? WHERE request_id=? AND operation_id=? AND released_at IS NULL',
                         (normalize_time(at or now()), identity, operation_id))


def consume(store, identity, result, at=None, operation_id=None):
    stamp = normalize_time(at or now())
    with atomic(store):
        _usable(store, identity, stamp, operation_id)
        store.db.execute('INSERT INTO approval_consumptions VALUES(?,?,?)', (identity, stamp, canonical(result)))
    return {'status': 'CONSUMED', 'request_id': identity, 'consumed_at': stamp}


def request_info(store, identity, at=None):
    value = envelope(store, identity)
    receipt = receipt_state(store, identity)
    used = store.db.execute('SELECT consumed_at,result_json FROM approval_consumptions WHERE request_id=?', (identity,)).fetchone()
    reservation = _reservation(store, identity)
    expired = normalize_time(at or now()) >= value['expires_at']
    status = ('CONSUMED' if used else 'APPLYING' if reservation else 'REJECTED' if receipt and receipt['decision'] == 'REJECT'
              else 'EXPIRED' if expired else 'APPROVED' if receipt else 'PENDING')
    return {**value, 'status': status, 'receipt': receipt,
            'consumption': {'at': used['consumed_at'], 'result': json.loads(used['result_json'])} if used else None,
            'reservation': reservation}


def listing(store, limit=50):
    return [request_info(store, r[0]) for r in store.db.execute('SELECT id FROM approval_requests ORDER BY created_at DESC,id DESC LIMIT ?', (limit,))]
