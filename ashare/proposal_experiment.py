"""Read-only summaries of frozen designs and bounded execution statistics.

Only metadata and aggregate counts are exposed: never model inputs/outputs,
proposal bodies, event notes or files. Execution statistics have no refresh time.
DESIGNED means registration only. ``enrollment.start_after`` is the earliest
permitted enrollment time in the design, not an actual experiment start time.
"""
import json
import math
import re

from . import experiments
from .judgments import encode
from .storage import digest, normalize_time

RUN_STATUSES = ('RUNNING', 'COMPLETED', 'INCONCLUSIVE', 'CANCELLED', 'REJECTED')
STATUSES = ('DESIGNED', *RUN_STATUSES)
META_FIELDS = ('version', 'baseline_build', 'route', 'environment', 'primary_metric')
ENROLLMENT_FIELDS = ('start_after', 'window_days', 'embargo_days', 'windows', 'minimum_pairs')
BUDGET_FIELDS = ('arms', 'major_changes', 'max_model_calls', 'max_retries', 'max_wait_days')
COUNT_FIELDS = ('model_calls', 'max_model_calls', 'retries', 'minimum_pairs',
                'enrolled', 'complete', 'failed', 'pending', 'day_clusters')
WINDOW_COUNTS = ('enrolled', 'complete', 'failed', 'pending', 'day_clusters')
RATE_FIELDS = ('baseline_rate', 'candidate_rate', 'paired_delta',
               'baseline_citation_rate', 'candidate_citation_rate')


def _identifier(value):
    # Preserve short metric/environment labels, including Chinese labels, while
    # rejecting paths, multiline descriptions and arbitrary nested values.
    return isinstance(value, str) and bool(re.fullmatch(r'\w[\w:.-]{0,199}', value))


def _time(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('实验运行时间格式无效')
    return normalize_time(value)


def _runner_summary(store, identity):
    from .experiment_runner import summary as runner_summary
    return runner_summary(store, identity)


def _counts(value, fields):
    for key in fields:
        if type(value[key]) is not int or not 0 <= value[key] <= 1_000_000_000:
            raise ValueError('实验运行计数无效')


def _rates(value):
    for key in RATE_FIELDS:
        rate = value[key]
        low = -1 if key == 'paired_delta' else 0
        if rate is not None and (type(rate) not in (int, float) or not math.isfinite(rate) or not low <= rate <= 1):
            raise ValueError('实验运行比例无效')


def _execution(value, status):
    """Project deterministic execution statistics; never expose model material."""
    if not isinstance(value, dict) or value.get('version') != 'forward-runner-v1':
        raise ValueError('实验运行摘要版本无效')
    if value.get('status') != status or status not in RUN_STATUSES:
        raise ValueError('实验运行状态不一致')
    if (value.get('primary_metric') != 'verifiable_prediction_rate'
            or value.get('production_changes') is not False
            or value.get('profit_evidence') is not False):
        raise ValueError('实验运行范围无效')
    hashed = value.get('manifest_hash')
    if not isinstance(hashed, str) or not re.fullmatch('[0-9a-f]{64}', hashed):
        raise ValueError('实验运行指纹无效')
    if not _identifier(value.get('baseline_build')):
        raise ValueError('实验运行基线无效')
    _counts(value, COUNT_FIELDS)
    if value['max_model_calls'] != 120 or value['model_calls'] > value['max_model_calls']:
        raise ValueError('实验运行预算无效')
    _rates(value)
    if value.get('conclusion') not in ('COLLECTING', 'DESCRIPTIVE_ONLY', 'INSUFFICIENT', 'CANCELLED', 'REJECTED'):
        raise ValueError('实验运行结论无效')
    if value.get('assessment') not in ('PENDING', 'INSUFFICIENT', 'NOT_SUPPORTED', 'STRUCTURE_IMPROVEMENT_ONLY'):
        raise ValueError('实验运行评估无效')
    window_results = value.get('window_results')
    if not isinstance(window_results, list) or len(window_results) != 2:
        raise ValueError('实验分窗结果缺失')
    safe_results = []
    for index, item in enumerate(window_results, 1):
        if not isinstance(item, dict) or type(item.get('window')) is not int or item['window'] != index:
            raise ValueError('实验分窗结果编号无效')
        _counts(item, WINDOW_COUNTS)
        _rates(item)
        safe_results.append({'window': index, **{key: item[key] for key in (*WINDOW_COUNTS, *RATE_FIELDS)}})
    reason = value.get('stop_reason')
    if reason is not None and (not isinstance(reason, str) or not re.fullmatch('[A-Z][A-Z0-9_]{0,79}', reason)):
        raise ValueError('实验停止原因格式无效')
    start, end = _time(value['started_at']), _time(value['ends_at'])
    windows = value.get('windows')
    if not isinstance(windows, list) or len(windows) != 2 or start >= end:
        raise ValueError('实验运行窗口无效')
    safe_windows = []
    last_end = start
    for index, window in enumerate(windows, 1):
        if not isinstance(window, dict) or type(window.get('index')) is not int or window['index'] != index:
            raise ValueError('实验运行窗口编号无效')
        window_start, window_end = _time(window['start']), _time(window['end'])
        if window_start < last_end or window_start >= window_end or window_end > end:
            raise ValueError('实验运行窗口顺序无效')
        safe_windows.append({'index': index, 'start': window_start, 'end': window_end})
        last_end = window_end
    return {'version': 'forward-runner-v1', 'manifest_hash': hashed,
            'baseline_build': value['baseline_build'], 'started_at': start, 'ends_at': end,
            'status': status, 'windows': safe_windows, 'primary_metric': 'verifiable_prediction_rate',
            'metric_label': '可检验结构比例', **{key: value[key] for key in (*COUNT_FIELDS, *RATE_FIELDS)},
            'conclusion': value['conclusion'], 'assessment': value['assessment'],
            'window_results': safe_results, 'stop_reason': reason,
            'production_changes': False, 'profit_evidence': False}


def summary(store, pid):
    """Return an allowlisted experiment summary, None if absent, or raise ValueError.

    The frozen spec hash and deterministic ID are verified before reading its
    fields. No lifecycle transitions, filesystem reads or model calls occur.
    """
    row = store.db.execute('SELECT * FROM experiment_designs WHERE proposal_id=?', (pid,)).fetchone()
    if not row:
        return None
    try:
        spec = json.loads(row['spec_json'])
        hashed = digest(encode(spec))
        if row['spec_hash'] != hashed or row['id'] != 'EX-' + digest(pid + hashed)[:20]:
            raise ValueError('实验设计冻结指纹不一致')
        experiments.validate(spec, row['created_at'])
        if any(not _identifier(spec[key]) for key in META_FIELDS):
            raise ValueError('实验设计元数据格式无效')
        enrollment = spec['enrollment']
        stamp = enrollment['start_after']
        if not isinstance(stamp, str) or len(stamp) > 40:
            raise ValueError('实验登记时间格式无效')
        normalize_time(stamp)
        budget = spec['budget']
        if any(type(budget[key]) is not int for key in BUDGET_FIELDS):
            raise ValueError('实验预算须为整数')
        event = store.db.execute('SELECT status FROM experiment_events WHERE experiment_id=? ORDER BY id DESC LIMIT 1', (row['id'],)).fetchone()
        if not event or event['status'] not in STATUSES:
            raise ValueError('实验设计状态缺失或不受支持')
        result = {'id': row['id'], 'status': event['status'],
                  **{key: spec[key] for key in META_FIELDS},
                  'enrollment': {key: enrollment[key] for key in ENROLLMENT_FIELDS},
                  'budget': {key: budget[key] for key in BUDGET_FIELDS},
                  'controls': list(spec['controls']), 'spec_hash': hashed}
        # Preserve legacy DESIGNED snapshots exactly. Closed registrations can
        # predate the runner; active/completed experiments require real execution.
        if event['status'] != 'DESIGNED':
            execution = _runner_summary(store, row['id'])
            if execution is not None:
                result['execution'] = _execution(execution, event['status'])
            elif event['status'] not in ('CANCELLED', 'REJECTED'):
                raise ValueError('实验状态缺少运行记录')
        return result
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        # Never echo malformed stored content into a public/model summary.
        raise ValueError('冻结实验设计的指纹、字段或状态无效，需核对原登记。') from None
