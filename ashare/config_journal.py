"""Durable configuration publication. The journal contains diffs and hashes, never a raw config.

The JSON file and SQLite cannot share a transaction. A committed intent reserves authorization,
then file replacement, append-only audit logging and receipt consumption are retried as one operation.
Every config_ops writer takes the same lock. Out-of-band edits are detected and never overwritten.
"""
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from .storage import now


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(raw):
    return json.dumps(raw, ensure_ascii=False, indent=2).encode('utf-8')


def _sync_directory(path):
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextmanager
def locked(path):
    """The sidecar survives atomic replacement of the configuration inode."""
    with path.with_name('.' + path.name + '.change.lock').open('a+b') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure(store):
    store.db.execute('''CREATE TABLE IF NOT EXISTS config_apply_journal(
        id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, approval_id TEXT,
        status TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT,
        payload_json TEXT NOT NULL, abort_reason TEXT)''')
    if 'abort_reason' not in {row['name'] for row in store.db.execute('PRAGMA table_info(config_apply_journal)')}:
        store.db.execute('ALTER TABLE config_apply_journal ADD COLUMN abort_reason TEXT')
    store.db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS config_apply_live_approval
        ON config_apply_journal(approval_id) WHERE approval_id IS NOT NULL AND status != 'ABORTED' ''')
    store.db.commit()


def _publish(path, data):
    """Replace and fsync both file and containing directory; do not use a predictable temp filename."""
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.publish-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _append_log(log, payload, operation_id):
    """Append missing operation/key rows. Existing complete or partial rows are never rewritten."""
    new_directories = []
    parent = log.parent
    while not parent.exists():
        new_directories.append(parent)
        parent = parent.parent
    log.parent.mkdir(parents=True, exist_ok=True)
    existing = set()
    data = log.read_bytes() if log.exists() else b''
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(row, dict) and row.get('operation_id') == operation_id:
            existing.add(row.get('key'))
    rows = []
    for entry in payload['entries']:
        if entry['key'] not in existing:
            rows.append({**{k: v for k, v in entry.items() if k != 'before_present'},
                         'at': payload['at'], 'reason': payload['reason'],
                         'approved_by': payload['actor'], 'approval_id': payload['approval_id'],
                         'operation_id': operation_id, 'build_before': payload['build_before'],
                         'build_after': payload['build_after']})
    if not rows:
        # The previous attempt may have written all bytes but failed its durability confirmation.
        with log.open('rb') as handle:
            os.fsync(handle.fileno())
        _sync_directory(log.parent)
        return
    # A crash can leave a partial trailing row. Preserve it and start fresh on a new line.
    chunk = (b'\n' if data and not data.endswith(b'\n') else b'') + b''.join(
        (json.dumps(row, ensure_ascii=False) + '\n').encode() for row in rows)
    with log.open('ab') as handle:
        handle.write(chunk)
        handle.flush()
        os.fsync(handle.fileno())
    _sync_directory(log.parent)
    for directory in new_directories:
        _sync_directory(directory.parent)


def result(payload, operation_id, log):
    return {'changes': [{k: v for k, v in e.items() if k != 'before_present'} for e in payload['entries']],
            'build_before': payload['build_before'], 'build_after': payload['build_after'],
            'log': str(log), 'operation_id': operation_id, 'approval_id': payload['approval_id']}


def _has_publication(log, operation_id):
    """Malformed audit fragments prevent automatic cancellation: their owner is uncertain."""
    if not log.exists():
        return False
    for line in log.read_bytes().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return True
        if not isinstance(entry, dict) or entry.get('operation_id') == operation_id:
            return True
    return False


def finish(store, path, row):
    """Caller holds the file lock. Resume only the precise before/after bytes in the committed intent."""
    from . import approvals
    try:
        # The durable intent is already committed. Hold the DB write lock during validation and
        # publication so concurrent guidance changes cannot invalidate the frozen build mid-write.
        with approvals.atomic(store):
            return _finish_transaction(store, path, row)
    except OSError:
        payload = json.loads(row['payload_json'])
        # A normal failed write before replace releases authorization. If replace happened, preserve
        # the committed intent regardless of the rolled-back completion transaction.
        if sha(path.read_bytes()) == payload['before_sha']:
            with approvals.atomic(store):
                if payload['approval_id']:
                    approvals.release(store, payload['approval_id'], row['id'])
                store.db.execute("UPDATE config_apply_journal SET status='ABORTED',completed_at=? WHERE id=?",
                                 (now(), row['id']))
        raise


def _finish_transaction(store, path, row):
    from . import approvals
    operation_id = row['id']
    payload = json.loads(row['payload_json'])
    approval_id = payload['approval_id']
    if approval_id:
        approvals.check_receipt(store, approval_id, kind='CONFIG', subject_id=row['subject_id'],
                                action='APPLY', snapshot=payload['snapshot'], operation_id=operation_id)
    current = path.read_bytes()
    current_hash = sha(current)
    if current_hash == payload['before_sha']:
        # A reservation survives a restart, but it does not authorize publishing under a newly
        # changed build or effective configuration. Already-published bytes below only need logging.
        from .config_ops import _prepare
        changes = {entry['key']: entry['after'] for entry in payload['entries']}
        prepared = _prepare(path, changes, payload['reason'],
                            setup=any(entry['class'] == 'SETUP' for entry in payload['entries']))
        if approvals.canonical(prepared['snapshot']) != approvals.canonical(payload['snapshot']):
            log = store.root / 'workflow' / 'changes' / 'config-changes.jsonl'
            if payload['before_sha'] != payload['after_sha'] and not _has_publication(log, operation_id):
                # The precise original file and absence of publication evidence prove that nothing
                # was applied. Cancel the stale intent, preserve its history and require new review.
                reason = '有效设置或程序版本已变化；未发布的操作已取消，需要重新申请审批'
                if approval_id:
                    approvals.release(store, approval_id, operation_id)
                store.db.execute("UPDATE config_apply_journal SET status='ABORTED',completed_at=?,abort_reason=? WHERE id=?",
                                 (now(), 'STATE_CHANGED_BEFORE_PUBLICATION', operation_id))
                return {'status': 'ABORTED', 'operation_id': operation_id, 'approval_id': approval_id,
                        'changes': [], 'reason': reason, 'requires_new_approval': bool(approval_id)}
            raise ValueError('配置恢复前的有效设置或程序版本已变化；请先核对未完成操作')
        raw = json.loads(current)
        for entry in payload['entries']:
            if (entry['key'] in raw) != entry['before_present'] or raw.get(entry['key']) != entry['before']:
                raise ValueError('配置恢复前值不一致；请核对未完成操作')
            raw[entry['key']] = entry['after']
        after = encoded(raw)
        if sha(after) != payload['after_sha']:
            raise ValueError('配置恢复结果与已授权操作不一致')
        _publish(path, after)
    elif current_hash != payload['after_sha']:
        raise ValueError('配置存在未完成操作且文件已被其他修改改变；已停止恢复，未覆盖现有配置')
    log = store.root / 'workflow' / 'changes' / 'config-changes.jsonl'
    _append_log(log, payload, operation_id)
    answer = result(payload, operation_id, log)
    if approval_id:
        approvals.consume(store, approval_id, answer, operation_id=operation_id)
    store.db.execute("UPDATE config_apply_journal SET status='COMPLETE',completed_at=? WHERE id=?",
                     (now(), operation_id))
    return answer


def recover_locked(store, path, subject_id):
    rows = list(store.db.execute("SELECT * FROM config_apply_journal WHERE subject_id=? AND status='APPLYING' ORDER BY created_at,id",
                                 (subject_id,)))
    return [finish(store, path, row) for row in rows]
