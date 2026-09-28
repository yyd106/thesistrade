"""Evaluation batches: numbered bundles of what the program recorded over a stretch of trading days.

A batch is what the desktop agent summarizes and Claude checks, so both read exactly the same material.
It covers the time since the previous batch (the first one: the last seven days) and holds only
program-generated files:
  report.md / report.json   evaluation over the period: signal registry, shadow books, builds, model use,
                            open engineering issues and proposals (the weekly report, over this period)
  digests.md                the daily digests rolled up over the period: one row per day and every flag
  governance.json           settings changes, proposals, research rules and engineering issues in the period
  quotes-health.json        quote-source outages, minutes without quotes and the after-the-fact stop checks
  notices.json              notices raised for Dean in the period, and any still open, with his answers
  manifest.json             id, trigger, period, trading days, build and the SHA-256 of every file above
Added later without touching those files:
  notes.md                  the desktop agent's summary (`./agent evaluation note`)
  claude-check.md           Claude's check, imported from the reports repository

Ids read EV-YYYYMMDD-HHMM (Beijing time of the cut). A trading day counts once its close falls inside the
period. A request with fewer than evaluation_min_trading_days new trading days is refused unless forced
(the manifest then says so); the daily digest job cuts a batch by itself once evaluation_auto_trading_days
trading days have closed since the previous one.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta
from .storage import now, normalize_time, json_write
from .calendar import SH, local, trading_day

FIRST_DAYS = 7
CLOSE_AT = (15, 0)
MAX_ROLLUP_DAYS = 62
ID = re.compile(r'EV-\d{8}-\d{4}')
GENERATED = ('report.md', 'report.json', 'digests.md', 'governance.json', 'quotes-health.json', 'notices.json')


def closes(since, until):
    """Trading days whose close falls in (since, until], oldest first."""
    a, b = local(since), local(until)
    days, d = [], a.date()
    while d <= b.date():
        if trading_day(d) is True and a < datetime(d.year, d.month, d.day, *CLOSE_AT, tzinfo=SH) <= b:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def _row(r):
    if not r:
        return None
    d = dict(r)
    d['manifest'] = json.loads(d.pop('manifest_json'))
    d['annex'] = json.loads(d.pop('annex_json'))
    return d


def latest(store):
    return _row(store.db.execute('SELECT * FROM evaluation_batches ORDER BY created_at DESC LIMIT 1').fetchone())


def get(store, bid):
    return _row(store.db.execute('SELECT * FROM evaluation_batches WHERE id=?', (bid,)).fetchone())


def due(store, config, at=None):
    """Trading days closed since the previous batch, against the manual minimum and the automatic cadence."""
    at = normalize_time(at or now())
    prev = latest(store)
    since = prev['period_end'] if prev else normalize_time((datetime.fromisoformat(at) - timedelta(days=FIRST_DAYS)).isoformat())
    return {'since': since, 'previous': prev['id'] if prev else None, 'trading_days': closes(since, at),
            'minimum': config['evaluation_min_trading_days'], 'auto_every': config['evaluation_auto_trading_days']}


def folder(store, bid):
    if not ID.fullmatch(bid or ''):
        raise ValueError('批次编号格式应为 EV-YYYYMMDD-HHMM')
    return store.root / 'workflow' / 'evaluations' / bid


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _notices(store, since):
    from .notices import export
    return [n for n in export(store) if n['created_at'] >= since or n['status'] == 'OPEN']


def start(store, config, *, trigger='manual', force=False, at=None):
    """Cut a batch. Returns TOO_SOON (nothing written) when too few trading days have closed and not forced.
    One cut at a time: the digest job and a manual request cannot both take the same period."""
    from .workflow import task_lock
    with task_lock(store.root, 'evaluation-batch', wait_seconds=60):
        return _start(store, config, trigger=trigger, force=force, at=at)


def _start(store, config, *, trigger, force, at):
    at = normalize_time(at or now())
    d = due(store, config, at)
    n = len(d['trading_days'])
    if trigger == 'auto':
        # Decided inside the lock, so a manual batch cut a moment earlier resets the count.
        if not d['auto_every'] or n < d['auto_every']:
            return None
        force = False
    elif n < d['minimum'] and not force:
        prev = d['previous']
        return {'status': 'TOO_SOON', 'trading_days': n, 'minimum': d['minimum'], 'previous': prev,
                'message': (f"距离上一批 {prev}（{local(d['since']).strftime('%m-%d %H:%M')}）只收盘了 {n} 个交易日，至少需要 {d['minimum']} 个。"
                            if prev else f"最近 {FIRST_DAYS} 天只收盘了 {n} 个交易日，至少需要 {d['minimum']} 个。")
                           + '样本太少的批次看不出变化；确实需要时加 --force，批次清单会注明是强制生成的。'}
    bid = 'EV-' + local(at).strftime('%Y%m%d-%H%M')
    if get(store, bid):
        raise ValueError(f'{bid} 已经生成，请过一分钟再试')
    out = folder(store, bid)
    if out.exists() and any(out.iterdir()):
        raise ValueError(f'{out} 已存在文件，未覆盖')
    out.mkdir(parents=True, exist_ok=True)
    since = d['since']
    from .weekly import collect, markdown as report_markdown
    from .digest import governance, rollup
    from . import quote_health, __version__
    from .build import info
    days = (datetime.fromisoformat(at) - datetime.fromisoformat(since)).total_seconds() / 86400
    report = collect(store, config, at, days=days)
    json_write(out / 'report.json', report)
    (out / 'report.md').write_text(report_markdown(report, title=f'评估批次 {bid} 报告', period='本期'), encoding='utf-8')
    first_day = max(local(since).date(), local(at).date() - timedelta(days=MAX_ROLLUP_DAYS))
    rollup(store, config, first_day.isoformat(), local(at).date().isoformat(), folder=out, stem='digests')
    (out / 'digests.json').unlink(missing_ok=True)  # the per-day JSON is large; the digests themselves stay on the node
    json_write(out / 'governance.json', governance(store, since, at))
    json_write(out / 'quotes-health.json', quote_health.summary(store, since, at))
    json_write(out / 'notices.json', _notices(store, since))
    manifest = {'id': bid, 'created_at': at, 'trigger': trigger, 'forced': bool(trigger == 'manual' and force and n < d['minimum']),
                'previous': d['previous'], 'period': {'from': since, 'to': at}, 'trading_days': d['trading_days'],
                'minimum_trading_days': d['minimum'], 'version': __version__, 'build_id': info(config, store)['build_id'],
                'rollup_from': first_day.isoformat(),
                'files': {name: {'sha256': _sha(out / name), 'bytes': (out / name).stat().st_size} for name in GENERATED}}
    json_write(out / 'manifest.json', manifest)
    with store.db:
        store.db.execute('INSERT INTO evaluation_batches VALUES(?,?,?,?,?,?,?,?,?)',
                         (bid, at, trigger, since, at, n, 'READY', json.dumps(manifest, ensure_ascii=False, sort_keys=True), '{}'))
    return {'status': 'READY', 'id': bid, 'folder': str(out.relative_to(store.root)), 'period': manifest['period'],
            'trading_days': n, 'forced': manifest['forced'], 'files': sorted(GENERATED) + ['manifest.json']}


def auto(store, config, at=None):
    """Called after the daily digest: cut a batch once evaluation_auto_trading_days trading days have closed
    since the previous one (0 turns this off). Returns None when no batch is due."""
    if not config.get('evaluation_auto_trading_days'):
        return None
    at = normalize_time(at or now())
    if len(due(store, config, at)['trading_days']) < config['evaluation_auto_trading_days']:
        return None  # the common case, without taking the lock
    return start(store, config, trigger='auto', at=at)


def _annex(store, bid, key, value, status=None):
    """Add one entry to a batch's annex; read and written in one transaction, so a note and a check
    imported at the same moment cannot drop each other. A checked batch stays CHECKED."""
    from .quote_health import _atomic
    def update():
        row = get(store, bid)
        annex = {**row['annex'], key: value}
        new_status = 'CHECKED' if 'check' in annex else (status or row['status'])
        store.db.execute('UPDATE evaluation_batches SET annex_json=?,status=? WHERE id=?',
                         (json.dumps(annex, ensure_ascii=False, sort_keys=True), new_status, bid))
        return annex
    return _atomic(store, update)


def note(store, bid, text, *, replace=False, at=None):
    """The desktop agent's summary of a batch, kept beside the program's files."""
    row = get(store, bid)
    if not row:
        raise ValueError('没有这个批次：' + bid)
    text = (text or '').strip()
    if not 20 <= len(text) <= 60000:
        raise ValueError('批次小结须为20到60000字')
    path = folder(store, bid) / 'notes.md'
    existed = path.exists()
    if existed and not replace:
        raise ValueError('这个批次已有小结；要替换时加 --replace')
    path.write_text(text + '\n', encoding='utf-8')
    annex = _annex(store, bid, 'notes', {'sha256': _sha(path), 'at': normalize_time(at or now()), 'replaced': existed}, 'NOTED')
    return {'status': 'CHECKED' if 'check' in annex else 'NOTED', 'id': bid, 'notes': str(path.relative_to(store.root))}


def record_check(store, bid, text, source, at=None):
    """Claude's check, imported from the reports repository. The same text twice changes nothing."""
    row = get(store, bid)
    if not row:
        return None
    sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
    if (row['annex'].get('check') or {}).get('sha256') == sha:
        return None
    path = folder(store, bid) / 'claude-check.md'
    path.write_text(text, encoding='utf-8')
    _annex(store, bid, 'check', {'sha256': sha, 'at': normalize_time(at or now()), 'source': source}, 'CHECKED')
    return bid


def listing(store, limit=30):
    return [{'id': r['id'], 'created_at': r['created_at'], 'trigger': r['trigger'], 'trading_days': r['trading_days'],
             'status': r['status'], 'forced': r['manifest'].get('forced'), 'notes': bool(r['annex'].get('notes')),
             'claude_check': bool(r['annex'].get('check'))}
            for r in map(_row, store.db.execute('SELECT * FROM evaluation_batches ORDER BY created_at DESC LIMIT ?', (limit,)))]


def show(store, bid):
    row = get(store, bid)
    if not row:
        raise ValueError('没有这个批次：' + bid)
    out = folder(store, bid)
    files = {}
    for name, meta in row['manifest']['files'].items():
        path = out / name
        files[name] = 'OK' if path.exists() and _sha(path) == meta['sha256'] else ('CHANGED' if path.exists() else 'MISSING')
    return {**row, 'folder': str(out.relative_to(store.root)), 'integrity': files,
            'notes_file': (out / 'notes.md').exists(), 'check_file': (out / 'claude-check.md').exists()}
