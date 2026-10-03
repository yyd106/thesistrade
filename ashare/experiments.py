"""Immutable experiment designs; explicit activation uses the isolated runner."""
import json
from .storage import digest, normalize_time, now
from .judgments import encode

VERSION = 'forward-design-v1'


def validate(spec, at):
    required = {'version', 'baseline_build', 'route', 'environment', 'change', 'enrollment',
                'primary_metric', 'failure_criteria', 'rollback', 'budget', 'controls'}
    if not isinstance(spec, dict) or set(spec) != required or spec['version'] != VERSION:
        raise ValueError('实验设计字段或版本无效')
    if spec['route'] not in ('watchlist', 'dynamic', 'global', 'portfolio'):
        raise ValueError('实验路线无效')
    for key in ('baseline_build', 'environment', 'primary_metric', 'failure_criteria', 'rollback'):
        if not isinstance(spec[key], str) or not spec[key].strip():
            raise ValueError('实验字段不能为空：' + key)
    if not isinstance(spec['change'], dict) or set(spec['change']) != {'id', 'text'} or not all(isinstance(v, str) and v.strip() for v in spec['change'].values()):
        raise ValueError('每个实验须有且仅有一项明确变更')
    e = spec['enrollment']
    if not isinstance(e, dict) or set(e) != {'start_after', 'window_days', 'embargo_days', 'windows', 'minimum_pairs'}:
        raise ValueError('需要固定观察窗口和间隔')
    if normalize_time(e['start_after']) < normalize_time(at):
        raise ValueError('实验不得追溯开始或复用已有结果')
    for key, lo, hi in (('window_days', 1, 120), ('embargo_days', 1, 120), ('windows', 2, 24), ('minimum_pairs', 5, 10000)):
        if type(e[key]) is not int or not lo <= e[key] <= hi:
            raise ValueError('实验观察范围无效：' + key)
    budget = spec['budget']
    if budget != {'arms': 2, 'major_changes': 1, 'max_model_calls': 120, 'max_retries': 2, 'max_wait_days': 180}:
        raise ValueError('实验超出一期登记预算')
    if e['windows'] * e['window_days'] + (e['windows'] - 1) * e['embargo_days'] > budget['max_wait_days']:
        raise ValueError('观察与隔离窗口超过最长等待时间')
    if spec['controls'] != ['SAME_INFORMATION_TIME', 'SAME_EXECUTION_AND_COSTS', 'ISOLATED_LEDGER',
                            'PURGE_UNMATURED_LABELS', 'KEEP_ALL_FAILURES', 'NO_PRODUCTION_PROMOTION']:
        raise ValueError('实验隔离条件不完整')
    return spec


def register(store, proposal_id, spec, at=None):
    """Caller owns transaction. A proposal has one frozen design, including failed designs."""
    at = normalize_time(at or now())
    row = store.db.execute('SELECT id,spec_hash FROM experiment_designs WHERE proposal_id=?', (proposal_id,)).fetchone()
    hashed = digest(encode(spec))
    if row:
        if row['spec_hash'] != hashed:
            raise ValueError('已登记实验不可修改；新方案须新提案并保留原设计')
        return row['id']
    proposal = store.db.execute('SELECT status FROM strategy_proposals WHERE id=?', (proposal_id,)).fetchone()
    if not proposal or proposal['status'] not in ('DRAFT', 'READY'):
        raise ValueError('只接受待验证提案的实验设计')
    validate(spec, at)
    identity = 'EX-' + digest(proposal_id + hashed)[:20]
    store.db.execute('INSERT INTO experiment_designs VALUES(?,?,?,?,?)', (identity, proposal_id, at, hashed, encode(spec)))
    store.db.execute('INSERT INTO experiment_events(experiment_id,at,status,note) VALUES(?,?,?,?)',
                     (identity, at, 'DESIGNED', '仅登记设计；尚未启动实验或调用模型'))
    return identity


def close(store, identity, status, note, at=None):
    if store.db.execute('SELECT 1 FROM experiment_runs WHERE experiment_id=?', (identity,)).fetchone():
        from .experiment_runner import close as close_run
        return close_run(store, identity, status, note, at)
    if status not in ('CANCELLED', 'REJECTED') or not isinstance(note, str) or not note.strip():
        raise ValueError('只允许关闭设计，并须记录原因')
    with store.db:
        row = store.db.execute('SELECT status FROM experiment_events WHERE experiment_id=? ORDER BY id DESC LIMIT 1', (identity,)).fetchone()
        if not row or row[0] != 'DESIGNED':
            raise ValueError('实验不存在或已经关闭')
        store.db.execute('INSERT INTO experiment_events(experiment_id,at,status,note) VALUES(?,?,?,?)',
                         (identity, normalize_time(at or now()), status, note.strip()[:1000]))
    return {'id': identity, 'status': status}


def view(store, identity=None):
    rows = store.db.execute('SELECT * FROM experiment_designs' + (' WHERE id=?' if identity else '') +
                            ' ORDER BY created_at DESC,id LIMIT 100', (identity,) if identity else ()).fetchall()
    if identity and not rows:
        raise ValueError('未找到实验设计')
    result = []
    for r in rows:
        events = [dict(e) for e in store.db.execute('SELECT at,status,note FROM experiment_events WHERE experiment_id=? ORDER BY id', (r['id'],))]
        result.append({'id': r['id'], 'proposal_id': r['proposal_id'], 'created_at': r['created_at'],
                       'status': events[-1]['status'], 'spec': json.loads(r['spec_json']), 'history': events})
        from .experiment_runner import summary
        execution = summary(store, r['id'])
        if execution:
            result[-1]['execution'] = execution
    return result[0] if identity else result
