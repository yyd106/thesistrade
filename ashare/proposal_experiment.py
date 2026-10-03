"""Read-only summaries of immutable forward experiment designs.

Only design metadata is exposed: never proposal bodies, event notes or files.
DESIGNED means registration only. ``enrollment.start_after`` is the earliest
permitted enrollment time in the design, not an actual experiment start time.
"""
import json
import re

from . import experiments
from .judgments import encode
from .storage import digest, normalize_time

STATUSES = ('DESIGNED', 'CANCELLED', 'REJECTED')
META_FIELDS = ('version', 'baseline_build', 'route', 'environment', 'primary_metric')
ENROLLMENT_FIELDS = ('start_after', 'window_days', 'embargo_days', 'windows', 'minimum_pairs')
BUDGET_FIELDS = ('arms', 'major_changes', 'max_model_calls', 'max_retries', 'max_wait_days')


def _identifier(value):
    # Preserve short metric/environment labels, including Chinese labels, while
    # rejecting paths, multiline descriptions and arbitrary nested values.
    return isinstance(value, str) and bool(re.fullmatch(r'\w[\w:.-]{0,199}', value))


def summary(store, pid):
    """Return an allowlisted design summary, None if absent, or raise ValueError.

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
        return {'id': row['id'], 'status': event['status'],
                **{key: spec[key] for key in META_FIELDS},
                'enrollment': {key: enrollment[key] for key in ENROLLMENT_FIELDS},
                'budget': {key: budget[key] for key in BUDGET_FIELDS},
                'controls': list(spec['controls']), 'spec_hash': hashed}
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        # Never echo malformed stored content into a public/model summary.
        raise ValueError('冻结实验设计的指纹、字段或状态无效，需核对原登记。') from None
