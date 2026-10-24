"""Read-only, bounded review records; never research or execution authority.

The only input is workflow/strategy-changes/*.json under the supplied data root.
Evidence is a title and hash, never a path to follow. Dates describe the review;
recorded_at describes when this version became available for historical export.
"""
from datetime import date, datetime, time, timezone
import json
import os
from pathlib import Path
import re
import stat
from zoneinfo import ZoneInfo

SCHEMA = 'research-strategy-change-v1'
ZONE = ZoneInfo('Asia/Shanghai')
MAX_FILES = 4096
MAX_DIRECTORY_ENTRIES = 8192
MAX_FILE_BYTES = 256_000
MAX_TOTAL_BYTES = 8_000_000
MAX_ITEMS = 100
MAX_EVIDENCE = 100
SOURCES = {'llm_review': 'LLM 评审', 'human_review': '人工评审', 'system_review': '系统核验'}
STATUSES = {'accepted_pending': '认可方向，尚未实施', 'clarified': '已核验澄清，不表示规则变更', 'implemented': '已实施（按留存记录）'}
SCOPES = {'documentation': '文档与记录', 'research': '研究流程或规则', 'transaction': '模拟交易规则'}
REQUIRED = {'schema', 'id', 'occurred_on', 'recorded_at', 'title', 'source', 'production_change', 'items', 'evidence'}
ITEM_FIELDS = {'topic', 'before', 'accepted', 'reason', 'status', 'next_step', 'effect_scope'}


def _timestamp(raw):
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, str) and len(raw) <= 40:
        normalized = raw.replace('Z', '+00:00')
        normalized = re.sub(r'(\d{2}:\d{2}:\d{2})[.,](\d{1,6})(?=[+-]|$)',
                            lambda m: m[1] + '.' + m[2].ljust(6, '0'), normalized)
        value = datetime.fromisoformat(normalized)
    else:
        raise ValueError('策略评审时间必须是带时区的 ISO 时间')
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('策略评审时间必须包含时区')
    return value.astimezone(timezone.utc)


def _text(value, limit):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any((ord(c) < 32 and c not in '\n\t') or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError('策略评审文本类型、长度或控制字符无效')
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('策略评审 JSON 含重复字段')
        result[key] = value
    return result


def _invalid_number(_):
    raise ValueError('策略评审 JSON 含非标准数值')


def _validate(raw):
    if not isinstance(raw, dict) or set(raw) != REQUIRED or raw.get('schema') != SCHEMA:
        raise ValueError('策略评审记录字段或格式版本无效')
    identity = raw['id']
    if not isinstance(identity, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,79}', identity):
        raise ValueError('策略评审编号无效')
    occurred = raw['occurred_on']
    if not isinstance(occurred, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', occurred):
        raise ValueError('策略评审日期须为北京时间 YYYY-MM-DD')
    occurred = date.fromisoformat(occurred)
    if not isinstance(raw['recorded_at'], str):
        raise ValueError('补录时间须为字符串')
    recorded = _timestamp(raw['recorded_at'])
    if occurred > recorded.astimezone(ZONE).date():
        raise ValueError('评审日期不能晚于补录日期')
    _text(raw['title'], 300)
    if not isinstance(raw['source'], str) or raw['source'] not in SOURCES:
        raise ValueError('策略评审来源类别无效')
    if type(raw['production_change']) is not bool:
        raise ValueError('生产规则变更标志须为布尔值')
    if not isinstance(raw['items'], list) or not 1 <= len(raw['items']) <= MAX_ITEMS:
        raise ValueError('策略评审事项数量无效')
    items, changed = [], False
    for item in raw['items']:
        if not isinstance(item, dict):
            raise ValueError('策略评审事项须为对象')
        implemented = item.get('status') == 'implemented'
        if set(item) != ITEM_FIELDS | ({'implemented_at'} if implemented else set()):
            raise ValueError('策略评审事项字段无效')
        for key in ('topic', 'before', 'accepted', 'reason', 'next_step'):
            _text(item[key], 200 if key == 'topic' else 12000)
        if not isinstance(item['status'], str) or item['status'] not in STATUSES:
            raise ValueError('策略评审采纳状态无效')
        if not isinstance(item['effect_scope'], str) or item['effect_scope'] not in SCOPES:
            raise ValueError('策略评审作用范围无效')
        cleaned = dict(item)
        if implemented:
            if not isinstance(item['implemented_at'], str):
                raise ValueError('实施时间须为字符串')
            when = _timestamp(item['implemented_at'])
            if not datetime.combine(occurred, time.min, ZONE) <= when <= recorded:
                raise ValueError('实施时间须处于评审日期开始至补录时间之间')
            cleaned['implemented_at'] = when.isoformat()
            changed |= item['effect_scope'] in ('research', 'transaction')
        items.append(cleaned)
    if raw['production_change'] != changed:
        raise ValueError('生产规则变更标志与已实施事项不一致')
    if not isinstance(raw['evidence'], list) or len(raw['evidence']) > MAX_EVIDENCE:
        raise ValueError('策略评审证据数量无效')
    evidence = []
    for item in raw['evidence']:
        if not isinstance(item, dict) or set(item) != {'title', 'sha256'}:
            raise ValueError('策略评审证据仅允许标题与 SHA256')
        _text(item['title'], 300)
        if not isinstance(item['sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', item['sha256']):
            raise ValueError('策略评审证据摘要无效')
        evidence.append(dict(item))
    return {**raw, 'recorded_at': recorded.isoformat(), 'items': items, 'evidence': evidence}


def _open_directory(path, *, dir_fd=None):
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)


def load_records(root, start, end):
    """Load valid records available at end whose Beijing review date is in range.

    Missing directories yield []. Invalid files, symlinks, duplicate IDs and
    exceeded bounds fail explicitly; no partial or silently truncated result.
    """
    start, end = _timestamp(start), _timestamp(end)
    if start > end:
        raise ValueError('策略评审导出时间范围无效')
    first_day, last_day = start.astimezone(ZONE).date(), end.astimezone(ZONE).date()
    root = Path(root).expanduser().absolute()
    fds = []
    try:
        try:
            fds.append(_open_directory(root))
            fds.append(_open_directory('workflow', dir_fd=fds[-1]))
            fds.append(_open_directory('strategy-changes', dir_fd=fds[-1]))
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise ValueError('策略评审目录须为真实目录，不允许符号链接') from exc
        names = []
        with os.scandir(fds[-1]) as entries:
            for count, entry in enumerate(entries, 1):
                if count > MAX_DIRECTORY_ENTRIES:
                    raise ValueError('策略评审目录条目超过上限，未截断')
                if entry.name.endswith('.json'):
                    names.append(entry.name)
                    if len(names) > MAX_FILES:
                        raise ValueError('策略评审文件数超过上限，未截断')
        records, seen, total = [], set(), 0
        for name in sorted(names):
            fd = None
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fds[-1])
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('策略评审输入须为普通文件')
                if info.st_size > MAX_FILE_BYTES:
                    raise ValueError('策略评审单文件超过上限，未截断')
                pieces, size = [], 0
                while True:
                    part = os.read(fd, min(65536, MAX_FILE_BYTES + 1 - size))
                    if not part:
                        break
                    pieces.append(part)
                    size += len(part)
                    if size > MAX_FILE_BYTES:
                        raise ValueError('策略评审单文件超过上限，未截断')
                after = os.fstat(fd)
                if ((info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                        != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                    raise ValueError('策略评审记录在读取期间发生变化，请重试')
                total += size
                if total > MAX_TOTAL_BYTES:
                    raise ValueError('策略评审总字节超过上限，未截断')
                raw = json.loads(b''.join(pieces).decode('utf-8'), object_pairs_hook=_object, parse_constant=_invalid_number)
                record = _validate(raw)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                raise ValueError('策略评审记录不可安全读取或格式损坏') from exc
            finally:
                if fd is not None:
                    os.close(fd)
            if record['id'] in seen:
                raise ValueError('策略评审编号重复，不能判断有效版本')
            seen.add(record['id'])
            if first_day <= date.fromisoformat(record['occurred_on']) <= last_day and _timestamp(record['recorded_at']) <= end:
                records.append(record)
        return sorted(records, key=lambda r: (r['occurred_on'], _timestamp(r['recorded_at']), r['id']))
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _escape(value):
    value = value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return re.sub(r'([\\`*_{}\[\]()#+!|~-])', r'\\\1', value).replace('\n', '\n  ')


def render_records(records):
    """Return safe Markdown lines, explicitly separating agreement from rollout."""
    if not isinstance(records, list) or len(records) > MAX_FILES:
        raise ValueError('策略评审记录列表无效')
    records = [_validate(record) for record in records]
    lines = ['## 研究策略评审与采纳记录', '',
             '评审认可不等于上线。认可方向、事实澄清与已实施事项分别列示；本节是留痕，不向研究模型或交易规则注入新指令。',
             '评审日期按北京时间记录；补录时间表示这份记录实际可用于导出的时间，不倒填为评审当时已有的记录。', '']
    if not records:
        return lines + ['本窗口没有已留存且在截止时刻前补录的评审记录。', '']
    for record in records:
        recorded = _timestamp(record['recorded_at']).astimezone(ZONE).isoformat()
        lines += ['### ' + _escape(record['occurred_on'] + ' · ' + record['title']), '',
                  '- 评审日期（北京时间）：' + record['occurred_on'],
                  '- 补录时间（北京时间）：' + recorded,
                  '- 来源：' + SOURCES[record['source']],
                  '- 记录编号：' + _escape(record['id']),
                  '- 生产规则变更：' + ('本记录包含已实施的研究或模拟交易规则事项；以各项实施时间为准。' if record['production_change']
                                        else '否。本记录不表示研究规则或模拟交易规则已改变。'), '']
        for index, item in enumerate(record['items'], 1):
            lines += ['#### ' + str(index) + '. ' + _escape(item['topic']), '',
                      '- 状态：' + STATUSES[item['status']], '- 作用范围：' + SCOPES[item['effect_scope']],
                      '- 原情况：' + _escape(item['before']),
                      '- ' + ('澄清结论' if item['status'] == 'clarified' else '采纳内容') + '：' + _escape(item['accepted']),
                      '- 依据与理由：' + _escape(item['reason']), '- 下一步：' + _escape(item['next_step'])]
            if item['status'] == 'implemented':
                lines.append('- 实施时间（北京时间）：' + _timestamp(item['implemented_at']).astimezone(ZONE).isoformat())
            lines.append('')
        lines += ['证据留痕（只列标题与内容摘要，不读取证据文件）：', '']
        lines += ['- ' + _escape(e['title']) + '；SHA256：`' + e['sha256'] + '`' for e in record['evidence']]
        if not record['evidence']:
            lines.append('- 未附证据摘要。')
        lines.append('')
    return lines
