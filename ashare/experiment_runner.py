"""One local forward prompt trial, isolated from production plans and learning.

Registration is not activation. Inputs are enrolled by the live research hook,
before either experimental answer exists. Reservations, failures and results are
append-only. This measures verifiable structure, never investment performance.
"""
import json
from contextlib import contextmanager
from datetime import datetime, timedelta

from . import build, experiments
from .judgments import encode
from .storage import digest, normalize_time, now
from .calendar import local

VERSION = 'forward-runner-v1'
CHANGE_ID = 'EXPLICIT_INVALIDATION'
METRIC = 'verifiable_prediction_rate'
ACTIVE = 'RUNNING'
TERMINAL = ('COMPLETED', 'INCONCLUSIVE', 'CANCELLED', 'REJECTED')
MAX_INPUT_AGE_SECONDS = 6 * 3600


@contextmanager
def atomic(store):
    store.db.execute('BEGIN IMMEDIATE')
    try:
        yield
        store.db.commit()
    except BaseException:
        store.db.rollback()
        raise


def _local(config):
    if config.get('deployment_role') == 'cloud':
        raise ValueError('实验只在本机研究端运行')
    if config.get('live_execution_enabled') or config.get('paid_api_fallback'):
        raise ValueError('实验不接入实盘或付费API')


def _status(store, identity):
    row = store.db.execute('SELECT status FROM experiment_events WHERE experiment_id=? ORDER BY id DESC LIMIT 1', (identity,)).fetchone()
    return row[0] if row else None


def _manifest(store, identity):
    row = store.db.execute('SELECT * FROM experiment_runs WHERE experiment_id=?', (identity,)).fetchone()
    if not row:
        return None
    value = json.loads(row['manifest_json'])
    if digest(encode(value)) != row['manifest_hash']:
        raise ValueError('实验运行合同指纹不一致')
    return value


def prepare(store, config, at=None):
    """Register the first supported candidate as a DRAFT; never approve a strategy."""
    _local(config)
    from .experiment_measurement import CANDIDATE_INSTRUCTION
    from .governance import draft_proposal
    at = normalize_time(at or now())
    current = build.info(config, store)
    key = 'forward-pilot:' + VERSION + ':' + current['build_id']
    with atomic(store):
        prior = store.db.execute('SELECT id FROM strategy_proposals WHERE dedupe_key=?', (key,)).fetchone()
        if prior:
            row = store.db.execute('SELECT id FROM experiment_designs WHERE proposal_id=?', (prior['id'],)).fetchone()
            if row:
                return {'proposal_id': prior['id'], 'id': row['id'], 'status': _status(store, row['id'])}
            raise ValueError('候选登记不完整，需核对原记录')
        spec = {'version': experiments.VERSION, 'baseline_build': current['build_id'], 'route': 'watchlist',
            'environment': 'ALL', 'change': {'id': CHANGE_ID, 'text': CANDIDATE_INSTRUCTION},
            'enrollment': {'start_after': at, 'window_days': 50, 'embargo_days': 40, 'windows': 2, 'minimum_pairs': 30},
            'primary_metric': METRIC,
            'failure_criteria': '固定两窗口结束后比较可检验结构比例；确认窗未改善或引文逐字匹配率下降则不支持候选。'
                '总计不足30对、任一窗不足15对、少于30个日期簇或有未完成配对时证据不足；日期簇不保证独立。'
                '不等于预测兑现或收益证据。',
            'rollback': '关闭隔离实验并保留全部记录；生产研究、模拟交易和研究记忆保持原路径。',
            'budget': {'arms': 2, 'major_changes': 1, 'max_model_calls': 120, 'max_retries': 2, 'max_wait_days': 180},
            'controls': ['SAME_INFORMATION_TIME', 'SAME_EXECUTION_AND_COSTS', 'ISOLATED_LEDGER',
                         'PURGE_UNMATURED_LABELS', 'KEEP_ALL_FAILURES', 'NO_PRODUCTION_PROMOTION']}
        payload = {'hypothesis': '增加一条明确失效条件的提示要求，可能提高输出的可核验程度；目前尚无前向比较证据。',
            'change': CANDIDATE_INSTRUCTION,
            'evidence': '用户指定先验证一个候选、一个主要变化。本候选属于工程假设，尚无前向结果，不据此更改生产策略。',
            'test_plan': '新快照在回答前登记；两臂同一输出格式、同一资料与模型，仅候选追加一条提示。'
                '50天观察、40天隔离、50天确认；每天最多2对，每窗最多60次调用；失败和UNKNOWN保留分母。'
                '主要指标为可检验结构比例，引用按给定原文逐字核对；不使用价格或经营兑现标签。',
            'failure_criteria': spec['failure_criteria'], 'rollback': spec['rollback'],
            'counter_explanations': ['输出形式改善不代表研究判断改善', '同日及同来源样本相关', '基线也使用共同扩展格式'],
            'applicability': {'route': 'watchlist', 'build_id': current['build_id'], 'environment': 'ALL'},
            'experiment_design': spec, 'observations': []}
        pid = draft_proposal(store, source='user_requested_experiment', kind='PROMPT', target='watchlist',
            title='前向实验：结构化可核验失效条件', payload=payload, at=at, dedupe_key=key)
        identity = experiments.register(store, pid, spec, at)
    return {'proposal_id': pid, 'id': identity, 'status': 'DESIGNED'}


def start(store, config, identity, note, at=None):
    _local(config)
    from . import experiment_measurement as measurement
    if not isinstance(note, str) or not note.strip():
        raise ValueError('启动实验须记录本轮授权与目的，不改变提案审批状态')
    if not config.get('model_enabled') or not config.get('model_name'):
        raise ValueError('实验需要启用并固定订阅模型')
    at = normalize_time(at or now())
    with atomic(store):
        if _status(store, identity) != 'DESIGNED':
            raise ValueError('实验须从未运行的DESIGNED启动，不重复或重开试验')
        running = store.db.execute("SELECT experiment_id FROM experiment_runs r WHERE (SELECT status FROM experiment_events e WHERE e.experiment_id=r.experiment_id ORDER BY id DESC LIMIT 1)='RUNNING'").fetchone()
        if running:
            raise ValueError('同时最多运行一个候选')
        row = store.db.execute('SELECT * FROM experiment_designs WHERE id=?', (identity,)).fetchone()
        spec = json.loads(row['spec_json'])
        if digest(encode(spec)) != row['spec_hash'] or identity != 'EX-' + digest(row['proposal_id'] + row['spec_hash'])[:20]:
            raise ValueError('实验设计指纹不一致')
        experiments.validate(spec, row['created_at'])
        if (spec['route'], spec['change']['id'], spec['primary_metric']) != ('watchlist', CHANGE_ID, METRIC):
            raise ValueError('首期只支持自选股结构化失效条件及可检验结构指标')
        if spec['change']['text'] != measurement.CANDIDATE_INSTRUCTION or spec['environment'] != 'ALL':
            raise ValueError('候选实现与冻结设计不一致，请登记明确的新设计')
        if spec['enrollment']['windows'] != 2:
            raise ValueError('首期只运行两个固定窗口')
        if spec['enrollment']['minimum_pairs'] > 60:
            raise ValueError('最小配对数超过本轮120次调用可支持的范围')
        if at < normalize_time(spec['enrollment']['start_after']):
            raise ValueError('尚未到设计规定的最早启动时间')
        current = build.record(store, config, at)
        if current['build_id'] != spec['baseline_build']:
            raise ValueError('原设计基线版本已变化；须登记当前实现的新设计，不冒充旧基线')
        proposal = store.db.execute('SELECT status FROM strategy_proposals WHERE id=?', (row['proposal_id'],)).fetchone()
        if not proposal or proposal[0] not in ('DRAFT', 'READY'):
            raise ValueError('实验仅接受待验证候选')
        cursor = datetime.fromisoformat(at)
        windows = []
        for index in (1, 2):
            end = cursor + timedelta(days=spec['enrollment']['window_days'])
            windows.append({'index': index, 'start': normalize_time(cursor.isoformat()), 'end': normalize_time(end.isoformat())})
            cursor = end + timedelta(days=spec['enrollment']['embargo_days'])
        symbols = sorted(item['symbol'] for item in config.get('watchlist', []))
        if not symbols:
            raise ValueError('没有现有自选股可供前向观察')
        manifest = {'version': VERSION, 'experiment_id': identity, 'proposal_id': row['proposal_id'],
            'design_hash': row['spec_hash'], 'baseline_build': current['build_id'], 'build': current,
            'model_name': config['model_name'], 'model_effort': config.get('model_reasoning_effort'),
            'schema_hash': digest(encode(measurement.SCHEMA)), 'measurement_version': measurement.VERSION,
            'common_instruction': measurement.COMMON_INSTRUCTION, 'candidate_instruction': measurement.CANDIDATE_INSTRUCTION,
            'started_at': at, 'ends_at': windows[-1]['end'], 'windows': windows,
            'symbols': symbols, 'primary_metric': METRIC, 'minimum_pairs': spec['enrollment']['minimum_pairs'],
            'minimum_per_window': (spec['enrollment']['minimum_pairs'] + 1) // 2,
            'max_model_calls': 120, 'max_calls_per_window': 60, 'max_retries': 2, 'max_pairs_per_day': 2,
            'max_input_age_seconds': MAX_INPUT_AGE_SECONDS, 'sampling': 'FIRST_TWO_NEW_COMPANY_EVENT_FAMILIES_PER_DAY',
            'minimum_day_clusters': 30, 'confirmation_rule': 'POSITIVE_STRUCTURE_DELTA_AND_NONDECREASING_EXACT_CITATION_RATE',
            'missing_policy': 'ALL_ENROLLED_IN_DENOMINATOR', 'cost_basis': 'NOT_APPLICABLE_NO_TRADES',
            'production_changes': False, 'profit_evidence': False}
        store.db.execute('INSERT INTO experiment_runs VALUES(?,?,?,?)', (identity, at, digest(encode(manifest)), encode(manifest)))
        store.db.execute('INSERT INTO experiment_events(experiment_id,at,status,note) VALUES(?,?,?,?)',
            (identity, at, ACTIVE, note.strip()[:1000]))
    return summary(store, identity)


def _running(store):
    return [r[0] for r in store.db.execute("SELECT experiment_id FROM experiment_runs r WHERE (SELECT status FROM experiment_events e WHERE e.experiment_id=r.experiment_id ORDER BY id DESC LIMIT 1)='RUNNING'")]


def _compatible(store, config, manifest):
    from . import experiment_measurement as measurement
    return (manifest['baseline_build'] == build.info(config, store)['build_id'] and
            manifest['schema_hash'] == digest(encode(measurement.SCHEMA)) and
            manifest['measurement_version'] == measurement.VERSION)


def _window(manifest, stamp):
    return next((w for w in manifest['windows'] if w['start'] <= stamp < w['end']), None)


def _counts(store, identity, window=None):
    sql = 'SELECT count(*) FROM experiment_attempts a JOIN experiment_pairs p ON p.id=a.pair_id WHERE p.experiment_id=?'
    return store.db.execute(sql + (' AND p.window_index=?' if window else ''), (identity, window) if window else (identity,)).fetchone()[0]


def _new_targets(store, identity, packet):
    """Shared context may recur, but an experimental target event cannot recur."""
    fresh = set(packet.get('learning', {}).get('new_chunk_ids', []))
    used = {r[0] for r in store.db.execute('SELECT source_key FROM experiment_sources WHERE experiment_id=?', (identity,))}
    targets = {}
    for evidence in packet['evidence']:
        if evidence['evidence_id'] not in fresh or evidence.get('symbol') != packet['symbol']:
            continue
        doc = evidence.get('doc_id')
        if not doc:
            continue
        row = store.db.execute('SELECT family_id FROM document_meta WHERE doc_id=?', (doc,)).fetchone()
        source_key = 'family:' + row[0] if row else 'document:' + doc
        if source_key in used:
            continue
        targets.setdefault(source_key, []).append(evidence['evidence_id'])
    return targets


def enroll(store, config, packet, at=None):
    """Only called for a newly persisted live snapshot, before production study."""
    _local(config)
    at = normalize_time(at or now())
    active = _running(store)
    if not active:
        return {'status': 'IDLE'}
    identity = active[0]
    manifest = _manifest(store, identity)
    as_of = normalize_time(packet['as_of'])
    window = _window(manifest, as_of)
    if not window or not window['start'] <= at < window['end'] or as_of < manifest['started_at']:
        return {'status': 'OUTSIDE_WINDOW'}
    if not 0 <= (datetime.fromisoformat(at) - datetime.fromisoformat(as_of)).total_seconds() <= 60:
        return {'status': 'NOT_LIVE_INPUT'}
    if not _compatible(store, config, manifest):
        return {'status': 'IMPLEMENTATION_CHANGED'}
    if packet.get('symbol') not in manifest['symbols'] or not packet.get('evidence'):
        return {'status': 'NOT_ELIGIBLE'}
    if any(normalize_time(e.get('ready_at') or as_of) > as_of for e in packet['evidence']):
        return {'status': 'FUTURE_EVIDENCE'}
    row = store.db.execute('SELECT packet_json FROM snapshots WHERE id=?', (packet['snapshot_id'],)).fetchone()
    if not row or json.loads(row[0]) != packet:
        return {'status': 'SNAPSHOT_MISMATCH'}
    if store.db.execute('SELECT 1 FROM studies WHERE snapshot_id=?', (packet['snapshot_id'],)).fetchone():
        return {'status': 'ALREADY_RESEARCHED'}
    from .research import build_prompt
    raw = encode(packet)
    prompt = build_prompt(packet, config)
    marker = '<UNTRUSTED_PACKET>'
    if prompt.count(marker) != 1:
        raise ValueError('研究提示词边界无效')
    prefix, data = prompt.split(marker, 1)
    day = local(as_of).date().isoformat()
    with atomic(store):
        if _status(store, identity) != ACTIVE:
            return {'status': 'CLOSED'}
        existing = store.db.execute('SELECT id FROM experiment_pairs WHERE experiment_id=? AND (snapshot_id=? OR (symbol=? AND day_key=?))',
            (identity, packet['snapshot_id'], packet['symbol'], day)).fetchone()
        if existing:
            return {'status': 'DUPLICATE', 'id': existing[0]}
        targets = _new_targets(store, identity, packet)
        if not targets:
            return {'status': 'NO_NEW_EVENT'}
        event_key = digest(encode(sorted(targets)))
        target_ids = sorted({e for ids in targets.values() for e in ids})
        common = prefix + manifest['common_instruction'] + '本次实验目标是以下新资料，hypothesis_test如可核验，至少引用一项目标资料：' + encode(target_ids) + '。'
        baseline = common + marker + data
        candidate = common + manifest['candidate_instruction'] + marker + data
        existing = store.db.execute('SELECT id FROM experiment_pairs WHERE experiment_id=? AND (snapshot_id=? OR event_key=? OR (symbol=? AND day_key=?))',
            (identity, packet['snapshot_id'], event_key, packet['symbol'], day)).fetchone()
        if existing:
            return {'status': 'DUPLICATE', 'id': existing[0]}
        daily = store.db.execute('SELECT count(*) FROM experiment_pairs WHERE experiment_id=? AND day_key=?', (identity, day)).fetchone()[0]
        in_window = store.db.execute('SELECT count(*) FROM experiment_pairs WHERE experiment_id=? AND window_index=?', (identity, window['index'])).fetchone()[0]
        if daily >= 2 or in_window >= 30 or _counts(store, identity, window['index']) >= 60:
            return {'status': 'BUDGET_LIMIT'}
        pid = 'EP-' + digest(identity + packet['snapshot_id'])[:24]
        store.db.execute('INSERT INTO experiment_pairs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (pid, identity, window['index'], at, as_of, packet['symbol'], packet['snapshot_id'], digest(raw), raw,
             baseline, candidate, event_key, encode(target_ids), day))
        store.db.executemany('INSERT INTO experiment_sources VALUES(?,?,?)', [(identity, key, pid) for key in sorted(targets)])
    return {'status': 'ENROLLED', 'id': pid}


def _arm(store, pid, arm):
    rows = store.db.execute('''SELECT a.*,r.status,r.measurement_json FROM experiment_attempts a
        LEFT JOIN experiment_results r ON r.attempt_id=a.id WHERE a.pair_id=? AND a.arm=? ORDER BY a.ordinal''', (pid, arm)).fetchall()
    final = next((r for r in rows if r['status'] in ('SUCCEEDED', 'INVALID')), None)
    if final:
        return {'terminal': True, 'measurement': json.loads(final['measurement_json']) or None, 'status': final['status'], 'attempts': len(rows)}
    return {'terminal': len(rows) >= 3 and all(r['status'] for r in rows),
            'measurement': None, 'status': rows[-1]['status'] if rows else None, 'attempts': len(rows)}


def _pair_status(store, pair):
    event = store.db.execute('SELECT status FROM experiment_pair_events WHERE pair_id=? ORDER BY id DESC LIMIT 1', (pair['id'],)).fetchone()
    arms = {a: _arm(store, pair['id'], a) for a in ('baseline', 'candidate')}
    if all(v['status'] == 'SUCCEEDED' for v in arms.values()):
        status = 'COMPLETE'
    elif event or all(v['terminal'] for v in arms.values()):
        status = 'FAILED'
    else:
        status = 'PENDING'
    return status, arms


def _pairs(store, identity):
    return [dict(r) for r in store.db.execute('SELECT * FROM experiment_pairs WHERE experiment_id=? ORDER BY enrolled_at,id', (identity,))]


def _measurements(store, identity):
    rows = []
    for pair in _pairs(store, identity):
        state, arms = _pair_status(store, pair)
        rows.append({'id': pair['id'], 'window': pair['window_index'], 'as_of': pair['as_of'], 'status': state,
                     **{a: arms[a]['measurement'] for a in arms}})
    return rows


def _pair_end(store, pair, stamp, reason):
    if _pair_status(store, pair)[0] == 'PENDING':
        store.db.execute('INSERT INTO experiment_pair_events(pair_id,at,status,reason) VALUES(?,?,?,?)', (pair['id'], stamp, 'FAILED', reason))


def _finish(store, identity, stamp, status, reason):
    if _status(store, identity) != ACTIVE:
        return
    for pair in _pairs(store, identity):
        _pair_end(store, pair, stamp, reason)
    store.db.execute('INSERT INTO experiment_events(experiment_id,at,status,note) VALUES(?,?,?,?)', (identity, stamp, status, reason))
    result = _summary(store, identity, stop_reason=reason)
    store.db.execute('INSERT INTO experiment_conclusions VALUES(?,?,?,?,?)', (identity, stamp, status, reason, encode(result)))


def advance(store, config, at=None):
    _local(config)
    at = normalize_time(at or now())
    with atomic(store):
        for identity in _running(store):
            manifest = _manifest(store, identity)
            p = store.db.execute('SELECT status FROM strategy_proposals WHERE id=?', (manifest['proposal_id'],)).fetchone()
            if not p or p[0] not in ('DRAFT', 'READY'):
                _finish(store, identity, at, 'INCONCLUSIVE', 'PROPOSAL_CLOSED')
                continue
            if not _compatible(store, config, manifest):
                _finish(store, identity, at, 'INCONCLUSIVE', 'IMPLEMENTATION_CHANGED')
                continue
            for pair in _pairs(store, identity):
                w = manifest['windows'][pair['window_index'] - 1]
                if at >= w['end'] or (datetime.fromisoformat(at) - datetime.fromisoformat(pair['as_of'])).total_seconds() > MAX_INPUT_AGE_SECONDS:
                    _pair_end(store, pair, at, 'INPUT_EXPIRED')
                elif _counts(store, identity, pair['window_index']) >= 60:
                    # Do not discard a reserved call while its worker is still finishing.
                    active_call = store.db.execute('SELECT 1 FROM experiment_attempts a LEFT JOIN experiment_results r ON r.attempt_id=a.id WHERE a.pair_id=? AND r.attempt_id IS NULL', (pair['id'],)).fetchone()
                    if not active_call:
                        _pair_end(store, pair, at, 'CALL_BUDGET_EXHAUSTED')
            if at >= manifest['ends_at']:
                values = _measurements(store, identity)
                from .experiment_measurement import summarize
                measured = summarize(values, manifest['minimum_pairs'])
                sufficient = (len(values) >= manifest['minimum_pairs'] and
                    all(sum(v['window'] == i for v in values) >= manifest['minimum_per_window'] for i in (1, 2)) and
                    measured['day_clusters'] >= manifest['minimum_day_clusters'] and
                    not any(v['status'] != 'COMPLETE' for v in values))
                _finish(store, identity, at, 'COMPLETED' if sufficient else 'INCONCLUSIVE', 'WINDOW_COMPLETE')
    return {'experiments': [summary(store, r[0]) for r in store.db.execute('SELECT experiment_id FROM experiment_runs ORDER BY started_at DESC LIMIT 10')]}


def next_pending(store, config, at=None):
    _local(config)
    at = normalize_time(at or now())
    for identity in _running(store):
        manifest = _manifest(store, identity)
        if not _compatible(store, config, manifest):
            continue
        for pair in _pairs(store, identity):
            if _pair_status(store, pair)[0] != 'PENDING' or not _window(manifest, at):
                continue
            if _window(manifest, at)['index'] != pair['window_index']:
                continue
            age = (datetime.fromisoformat(at) - datetime.fromisoformat(pair['as_of'])).total_seconds()
            if not 0 <= age <= MAX_INPUT_AGE_SECONDS or _counts(store, identity, pair['window_index']) >= 60:
                continue
            return {'id': pair['id'], 'experiment_id': identity}
    return None


def _reserve(store, pair, arm, stamp):
    """Commit a call slot before launch; a crashed process still consumes it."""
    with atomic(store):
        manifest = _manifest(store, pair['experiment_id'])
        if _status(store, pair['experiment_id']) != ACTIVE or _pair_status(store, pair)[0] != 'PENDING':
            return None
        if not manifest['windows'][pair['window_index'] - 1]['start'] <= stamp < manifest['windows'][pair['window_index'] - 1]['end']:
            return None
        if _counts(store, pair['experiment_id']) >= 120 or _counts(store, pair['experiment_id'], pair['window_index']) >= 60:
            return None
        state = _arm(store, pair['id'], arm)
        if state['terminal'] or state['attempts'] >= 3:
            return None
        ordinal = state['attempts'] + 1
        aid = 'EA-' + digest(pair['id'] + arm + str(ordinal))[:24]
        store.db.execute('INSERT INTO experiment_attempts VALUES(?,?,?,?,?)', (aid, pair['id'], arm, ordinal, stamp))
    return aid


def run_pair(store, config, pid, cancel_event=None, *, model_fn=None, clock=None):
    """Caller holds the exclusive worker lock. Injectable model/clock are for tests."""
    _local(config)
    if not config.get('model_enabled'):
        return {'status': 'MODEL_DISABLED'}
    from . import model, experiment_measurement as measurement
    clock = clock or now
    row = store.db.execute('SELECT * FROM experiment_pairs WHERE id=?', (pid,)).fetchone()
    if not row:
        raise ValueError('未找到实验配对')
    pair = dict(row)
    manifest = _manifest(store, pair['experiment_id'])
    if not _compatible(store, config, manifest):
        advance(store, config, clock())
        return {'status': 'IMPLEMENTATION_CHANGED'}
    # Any orphan reservation belongs to a dead worker because the caller owns the lock.
    with atomic(store):
        for r in store.db.execute('SELECT a.id FROM experiment_attempts a LEFT JOIN experiment_results r ON r.attempt_id=a.id WHERE a.pair_id=? AND r.attempt_id IS NULL', (pid,)).fetchall():
            store.db.execute('INSERT INTO experiment_results VALUES(?,?,?,?,?,?,?)', (r[0], normalize_time(clock()), 'INTERRUPTED', '{}', '{}', 'WORKER_INTERRUPTED', '{}'))
    packet = json.loads(pair['input_json'])
    if digest(encode(packet)) != pair['input_hash']:
        raise ValueError('实验冻结输入指纹不一致')
    order = ('baseline', 'candidate') if int(digest(pid)[-1], 16) % 2 == 0 else ('candidate', 'baseline')
    for arm in order:
        if cancel_event and cancel_event.is_set():
            break
        stamp = normalize_time(clock())
        advance(store, config, stamp)
        if _status(store, pair['experiment_id']) != ACTIVE or _pair_status(store, pair)[0] != 'PENDING':
            break
        aid = _reserve(store, pair, arm, stamp)
        if not aid:
            continue
        folder = store.root / 'workflow' / 'experiments' / pair['experiment_id'] / pid / aid
        prompt = pair[arm + '_prompt']
        output, measured, meta, error = {}, {}, {}, None
        try:
            if not model_fn and model.pinned() != {'name': manifest['model_name'], 'effort': manifest['model_effort']}:
                raise RuntimeError('MODEL_PIN_MISMATCH')
            output = (model_fn or model.run_json)(prompt, measurement.SCHEMA, folder,
                config.get('model_timeout_seconds', 240), cancel_event=cancel_event)
            finished = normalize_time(clock())
            # Validate the future condition against output availability, not just the input time.
            measured = measurement.measure(output, packet, finished)
            if measured['verifiable'] and not set(output['stocks'][0]['hypothesis_test']['source_ids']) & set(json.loads(pair['target_json'])):
                measured = {**measured, 'verifiable': 0, 'status': 'OFF_TARGET', 'reason_code': 'NO_NEW_EVENT_REFERENCE'}
            meta = model.call_meta(folder) or {}
            if not model_fn and (meta.get('actual_model') != manifest['model_name'] or
                                 (manifest['model_effort'] and meta.get('actual_effort') != manifest['model_effort'])):
                error = 'MODEL_IDENTITY_MISMATCH'
                status, measured = 'INVALID', measurement.measure(None, packet, finished)
            else:
                status = 'SUCCEEDED' if measured.get('valid') == 1 else 'INVALID'
                if status == 'INVALID':
                    error = 'INVALID_OUTPUT'
            if finished >= manifest['windows'][pair['window_index'] - 1]['end'] or (datetime.fromisoformat(finished) - datetime.fromisoformat(pair['as_of'])).total_seconds() > MAX_INPUT_AGE_SECONDS:
                status, measured, error = 'INVALID', measurement.measure(None, packet, finished), 'OUTPUT_EXPIRED'
            if _status(store, pair['experiment_id']) != ACTIVE:
                status, measured, error = 'INVALID', measurement.measure(None, packet, finished), 'EXPERIMENT_CLOSED'
        except model.ModelYield:
            status, error = 'YIELDED', 'PRODUCTION_PRIORITY'
        except Exception as exc:
            status, error = 'FAILED', type(exc).__name__
        # Non-JSON / non-finite malformed model output must not strand its reservation.
        try:
            output_json = encode(output)
        except (TypeError, ValueError, OverflowError):
            output_json = '{}'
            status, error = 'INVALID', 'NON_JSON_OUTPUT'
            measured = measurement.measure(None, packet, normalize_time(clock()))
        with atomic(store):
            store.db.execute('INSERT INTO experiment_results VALUES(?,?,?,?,?,?,?)',
                (aid, normalize_time(clock()), status, output_json, encode(measured), error,
                 encode({k: meta.get(k) for k in ('actual_model', 'actual_effort', 'prompt_sha256', 'elapsed_seconds')})))
        if status == 'YIELDED':
            break
    advance(store, config, clock())
    return {'id': pid, 'status': _pair_status(store, pair)[0]}


def worker(config, pid, cancel_event=None):
    from .storage import Store
    from .workflow import task_lock
    store = Store(config['data_dir'])
    try:
        with task_lock(store.root, 'forward-experiment'):
            return run_pair(store, config, pid, cancel_event)
    finally:
        store.close()


def close(store, identity, status, note, at=None):
    if status not in ('CANCELLED', 'REJECTED') or not isinstance(note, str) or not note.strip():
        raise ValueError('关闭实验须明确状态及原因')
    at = normalize_time(at or now())
    with atomic(store):
        if _status(store, identity) != ACTIVE:
            raise ValueError('实验没有运行，或已关闭')
        _finish(store, identity, at, status, 'USER_' + status)
        # Keep the user's explanation without changing the immutable final statistics.
        store.db.execute('INSERT INTO experiment_events(experiment_id,at,status,note) VALUES(?,?,?,?)', (identity, at, status, note.strip()[:1000]))
    return {'id': identity, 'status': status}


def _summary(store, identity, stop_reason=None):
    from .experiment_measurement import summarize
    manifest = _manifest(store, identity)
    values = _measurements(store, identity)
    stats = summarize(values, manifest['minimum_pairs'])
    status = _status(store, identity)
    calls = _counts(store, identity)
    arms = store.db.execute('SELECT count(*) FROM (SELECT DISTINCT a.pair_id,a.arm FROM experiment_attempts a JOIN experiment_pairs p ON p.id=a.pair_id WHERE p.experiment_id=?)', (identity,)).fetchone()[0]
    confirmation = stats['windows'][1]
    assessment = 'PENDING' if status == ACTIVE else 'INSUFFICIENT'
    if status == 'COMPLETED':
        a, b = confirmation['baseline_citation_rate'], confirmation['candidate_citation_rate']
        assessment = ('STRUCTURE_IMPROVEMENT_ONLY' if (confirmation['paired_delta'] or 0) > 0
                      and a is not None and b is not None and b >= a else 'NOT_SUPPORTED')
    fields = ('enrolled', 'complete', 'failed', 'pending', 'baseline_rate', 'candidate_rate', 'paired_delta',
              'day_clusters', 'baseline_citation_rate', 'candidate_citation_rate')
    return {'version': VERSION, 'manifest_hash': digest(encode(manifest)), 'baseline_build': manifest['baseline_build'],
        'started_at': manifest['started_at'], 'ends_at': manifest['ends_at'], 'status': status,
        'windows': manifest['windows'], 'primary_metric': METRIC, 'metric_label': '可检验结构比例',
        'model_calls': calls, 'max_model_calls': 120, 'retries': calls - arms, 'minimum_pairs': manifest['minimum_pairs'],
        **{k: stats[k] for k in fields},
        'assessment': assessment, 'window_results': [{'window': w['window'], **{k: w[k] for k in fields}} for w in stats['windows']],
        'conclusion': ('COLLECTING' if status == ACTIVE else status if status in ('CANCELLED', 'REJECTED') else
                       'INSUFFICIENT' if status == 'INCONCLUSIVE' else 'DESCRIPTIVE_ONLY'),
        'stop_reason': stop_reason, 'production_changes': False, 'profit_evidence': False}


def summary(store, identity):
    if not _manifest(store, identity):
        return None
    row = store.db.execute('SELECT summary_json FROM experiment_conclusions WHERE experiment_id=?', (identity,)).fetchone()
    return json.loads(row[0]) if row else _summary(store, identity)
