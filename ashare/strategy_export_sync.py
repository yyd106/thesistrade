"""Signed, bounded strategy archives for display only; never execution authority."""
import base64
import binascii
import io
import json
import re
import sqlite3
import stat
import zipfile
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .cloud_protocol import MAX_BODY, MAX_JSON, canonical, packed, request, role
from .cloud_runtime import put, value
from .storage import digest, normalize_time, now

FEATURE = 'strategy_export_v1'
SCHEMA = 'strategy-export-v1'
STATE = 'strategy_export_sync'
ENDPOINT = '/api/sync/strategy-export'
ENTRIES = {'总览.md', '研究策略.md', '交易策略变更.md', '策略数据.json'}
DATA_FIELDS = {'schema', 'generated_at', 'start_at', 'end_at', 'timezone', 'scope', 'coverage',
               'baseline', 'published_baseline', 'decisions', 'published_decision_ids', 'research',
               'frozen_sources', 'source_events', 'issues'}
FIELDS = {'base64_zip', 'sha256', 'filename', 'generated_at', 'start_at', 'end_at'}
MAX_ZIP = 14_500_000
MAX_UNPACKED = 96_000_000
ZONE = ZoneInfo('Asia/Shanghai')
NOT_READY = '近两周策略档案尚未准备完成，请稍后重试。'


def _timestamp(raw):
    if not isinstance(raw, str) or normalize_time(raw) != raw:
        raise ValueError('策略档案时间格式无效，须使用 UTC 秒精度')
    return datetime.fromisoformat(raw)


def _metadata(meta, at):
    generated = _timestamp(meta['generated_at'])
    start, end = _timestamp(meta['start_at']), _timestamp(meta['end_at'])
    current = datetime.fromisoformat(normalize_time(at))
    expected = (generated.astimezone(ZONE).replace(hour=0, minute=0, second=0, microsecond=0)
                - timedelta(days=13))
    if generated > current or end != generated or start != expected:
        raise ValueError('策略档案必须覆盖生成当日及前 13 天，截止生成时点')
    filename = 'thesistrade-strategies-{}-{}.zip'.format(
        start.astimezone(ZONE).strftime('%Y%m%d'), generated.astimezone(ZONE).strftime('%Y%m%d'))
    if meta['filename'] != filename:
        raise ValueError('策略档案文件名无效')
    if not isinstance(meta['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', meta['sha256']):
        raise ValueError('策略档案摘要无效')
    return generated


def _object(pairs):
    result = {}
    for key, item in pairs:
        if key in result:
            raise ValueError('策略档案 JSON 含重复字段')
        result[key] = item
    return result


def _invalid_constant(_):
    raise ValueError('策略档案 JSON 含无效数值')


def _archive(raw, meta, at):
    _metadata(meta, at)
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_ZIP:
        raise ValueError('策略档案压缩文件过大或为空')
    if digest(raw) != meta['sha256']:
        raise ValueError('策略档案摘要不匹配')
    if not raw.startswith(b'PK\x03\x04') or raw[-22:-18] != b'PK\x05\x06':
        raise ValueError('策略档案不能包含额外的前缀、尾随内容或注释')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if archive.comment or len(entries) != len(ENTRIES) or {e.filename for e in entries} != ENTRIES:
                raise ValueError('策略档案必须只包含四个约定文件')
            if sum(e.file_size for e in entries) > MAX_UNPACKED:
                raise ValueError('策略档案解压后过大')
            remaining = MAX_UNPACKED
            metadata = None
            for entry in entries:
                mode = (entry.external_attr >> 16) & 0xFFFF
                if (entry.orig_filename != entry.filename or entry.is_dir() or entry.flag_bits & 1
                        or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                    raise ValueError('策略档案文件类型无效')
                with archive.open(entry) as stream:
                    data = stream.read(remaining + 1)
                remaining -= len(data)
                if remaining < 0 or len(data) != entry.file_size:
                    raise ValueError('策略档案解压大小无效')
                text = data.decode('utf-8')
                if entry.filename == '策略数据.json':
                    metadata = json.loads(text, object_pairs_hook=_object, parse_constant=_invalid_constant)
            if (not isinstance(metadata, dict) or set(metadata) != DATA_FIELDS or metadata.get('schema') != SCHEMA
                    or metadata.get('timezone') != 'Asia/Shanghai'
                    or any(metadata.get(k) != meta[k] for k in ('generated_at', 'start_at', 'end_at'))):
                raise ValueError('策略档案内部时间或格式与发布信息不匹配')
    except (zipfile.BadZipFile, UnicodeDecodeError, RuntimeError, EOFError, NotImplementedError, zlib.error):
        raise ValueError('策略档案 ZIP 内容无效') from None
    return metadata


def receive(store, body, at):
    """Called in the signed request's transaction. No commits or trading writes."""
    if not isinstance(body, dict) or set(body) != FIELDS:
        raise ValueError('策略档案发布字段无效')
    encoded = body['base64_zip']
    if not isinstance(encoded, str) or len(encoded) > ((MAX_ZIP + 2) // 3) * 4:
        raise ValueError('策略档案编码过大或无效')
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('策略档案编码无效') from None
    _archive(raw, body, at)
    prior = store.db.execute('SELECT generated_at,sha256 FROM strategy_export_cache WHERE id=1').fetchone()
    if prior and body['generated_at'] < prior['generated_at']:
        return {'status': 'IGNORED_STALE', 'generated_at': prior['generated_at'], 'sha256': prior['sha256']}
    if prior and body['generated_at'] == prior['generated_at']:
        if prior['sha256'] != body['sha256']:
            raise ValueError('同一生成时间的策略档案内容冲突')
        return {'status': 'UNCHANGED', 'generated_at': body['generated_at'], 'sha256': body['sha256']}
    store.db.execute('''INSERT INTO strategy_export_cache
        (id,generated_at,start_at,end_at,filename,sha256,archive,received_at) VALUES(1,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET generated_at=excluded.generated_at,start_at=excluded.start_at,
        end_at=excluded.end_at,filename=excluded.filename,sha256=excluded.sha256,
        archive=excluded.archive,received_at=excluded.received_at''',
        (body['generated_at'], body['start_at'], body['end_at'], body['filename'], body['sha256'], raw, normalize_time(at)))
    return {'status': 'ACCEPTED', 'generated_at': body['generated_at'], 'sha256': body['sha256']}


def read_cache(data_dir, at):
    """Read an already published archive without schema initialization or writes."""
    path = Path(data_dir).resolve() / 'agent.sqlite3'
    try:
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            row = db.execute('SELECT * FROM strategy_export_cache WHERE id=1').fetchone()
        finally:
            db.close()
    except sqlite3.OperationalError:
        raise ValueError(NOT_READY) from None
    if not row:
        raise ValueError(NOT_READY)
    meta = dict(row)
    generated = _metadata(meta, at)
    if generated.astimezone(ZONE).date() != datetime.fromisoformat(normalize_time(at)).astimezone(ZONE).date():
        raise ValueError('今日的近两周策略档案尚未准备完成，请稍后重试。')
    raw = meta['archive']
    _archive(raw, meta, at)
    return raw, meta['filename']


def deliver(store, config, cancel_event=None, *, at=None):
    if role(config) != 'research':
        return {'status': 'SKIPPED'}
    if FEATURE not in (value(store, 'remote_features') or []):
        return {'status': 'UNSUPPORTED'}
    if cancel_event is not None and cancel_event.is_set():
        return {'status': 'CANCELLED'}
    from .strategy_export import build_archive
    stamp = normalize_time(at or now())
    raw, filename = build_archive(config['data_dir'], stamp)
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_ZIP:
        raise ValueError('策略档案超过发布大小限制；完整内容未被截断')
    # Metadata originates in the same readonly database snapshot as the documents.
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entry = archive.getinfo('策略数据.json')
        if entry.file_size > MAX_UNPACKED or len(raw) > MAX_ZIP:
            raise ValueError('策略档案超过发布大小限制；完整内容未被截断')
        with archive.open(entry) as stream:
            metadata = json.loads(stream.read(MAX_UNPACKED + 1), object_pairs_hook=_object, parse_constant=_invalid_constant)
    body = {k: metadata[k] for k in ('generated_at', 'start_at', 'end_at')}
    body.update(filename=filename, sha256=digest(raw))
    _archive(raw, body, stamp)
    body['base64_zip'] = base64.b64encode(raw).decode('ascii')
    if len(canonical(body)) > MAX_JSON or len(packed(body)) > MAX_BODY:
        raise ValueError('完整策略档案超过同步传输限制；内容未被截断')
    if cancel_event is not None and cancel_event.is_set():
        return {'status': 'CANCELLED'}
    # No SQLite transaction or execution lock spans the network call.
    answer = request(config, ENDPOINT, body)
    if (not isinstance(answer, dict) or answer.get('status') not in ('ACCEPTED', 'UNCHANGED')
            or any(answer.get(k) != body[k] for k in ('generated_at', 'sha256'))):
        raise ValueError('云端未确认完整策略档案，保留既有缓存')
    with store.db:
        put(store, STATE, {'at': stamp, 'status': 'OK', 'generated_at': stamp, 'sha256': body['sha256']})
    return answer


def worker(config, cancel_event=None):
    """Independent scheduler work, failures recorded only in this feature's state."""
    if role(config) != 'research' or (cancel_event is not None and cancel_event.is_set()):
        return {'status': 'SKIPPED'}
    from .storage import Store
    from .workflow import task_lock
    store = None
    try:
        store = Store(config['data_dir'])
        with task_lock(store.root, 'strategy-export'):
            return deliver(store, config, cancel_event)
    except Exception as exc:
        if str(exc).startswith('BUSY:'):
            return {'status': 'BUSY'}
        # Exception strings may contain remote response text or local data. Keep
        # only a safe class and a fixed user-facing message in persistent state.
        failure = {'at': now(), 'status': 'FAILED', 'error': '策略档案同步未完成，将自动重试。',
                   'error_type': type(exc).__name__}
        if store is not None:
            try:
                if store.db.in_transaction:
                    store.db.rollback()
                with store.db:
                    put(store, STATE, failure)
            except Exception:
                pass
        return failure
    finally:
        if store is not None:
            store.close()
