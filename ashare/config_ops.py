"""Audited configuration changes for operators and desktop agents.

Every setting belongs to one class:
  OPERATIONAL  schedules, collection volume, retention, evaluation cadence. No effect on what gets
               bought or sold. An agent may change these within the validated ranges and must tell the user.
  STRATEGY     anything that changes research judgments, trading rules, the model or how results are
               measured. Requires a receipt for this exact change, confirmed by an authenticated administrator. Keys that change
               judgments or rules are part of the build id (build.STRATEGY_KEYS); evaluation settings are
               not, but still need approval so the yardstick cannot move quietly.
  FORBIDDEN    capital, withdrawal rules, market scope, execution mode, hard risk caps, fees, identity and
               credentials. Not changeable here at all; the user edits these deliberately, if ever.
Each change is validated by the normal loader before it is written, and logged with before/after values.
"""
import json
import os
import tempfile
from pathlib import Path
from .storage import Store, now
from . import config_journal as journal

OPERATIONAL = {'collection_times', 'review_time', 'evaluation_time', 'weekly_report_time', 'weekly_report_weekday', 'digest_time',
               'research_reuse_hours', 'portfolio_refresh_minutes', 'external_news_enabled', 'external_news_articles_per_source',
               'external_news_lookback_days', 'pdf_downloads_per_stock', 'pdf_revision_checks_per_stock', 'document_recheck_hours',
               'announcement_lookback_days', 'max_announcement_pages', 'quote_poll_seconds', 'announcement_poll_seconds',
               'recovery_interval_seconds', 'recovery_daily_limit', 'research_attempts', 'backup_hourly_keep', 'backup_daily_keep',
               'disk_free_warn_gb', 'db_size_warn_gb', 'evaluation_enabled', 'shadow_books_enabled', 'model_timeout_seconds',
               'dynamic_model_timeout_seconds', 'scheduler_enabled', 'quote_fallback_enabled', 'evaluation_min_trading_days',
               'evaluation_auto_trading_days', 'reports_sync_enabled', 'supervision_enabled', 'supervision_timeout_seconds'}
STRATEGY = {'paper_entry_band_bps', 'paper_stop_loss_bps', 'paper_take_profit_bps', 'plan_max_age_hours', 'model_name',
            'model_reasoning_effort', 'watchlist', 'dynamic_enabled', 'max_packet_chars', 'max_news_packet_pct',
            'evaluation_horizon_days', 'shadow_risk_per_trade_bps', 'shadow_start_date', 'research_topics',
            'comparison_peers', 'business_keywords', 'model_enabled', 'industry_enabled', 'industry_policy'}

# Written only by `./agent reports setup` (where the node's reports go), never by `config set`.
SETUP = {'reports_remote'}

# Operational keys the general loader leaves unbounded; changes made here must stay inside these ranges.
LIMITS = {'pdf_downloads_per_stock': (1, 12), 'max_announcement_pages': (1, 20),
          'announcement_lookback_days': (14, 180), 'model_timeout_seconds': (60, 900)}


def classify(key):
    if key in OPERATIONAL:
        return 'OPERATIONAL'
    if key in STRATEGY:
        return 'STRATEGY'
    return 'FORBIDDEN'


def parse_value(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def _subject(path):
    return journal.sha(str(path).encode())


def _prepare(path, changes, reason, setup=False):
    """Validate locally; return raw data only to the publisher, never to the approval request."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('需要写明修改理由')
    if len(reason.strip()) > 2000:
        raise ValueError('修改理由不能超过2000字')
    if not isinstance(changes, dict) or not changes:
        raise ValueError('至少指定一项设置修改')
    if any(not isinstance(key, str) for key in changes):
        raise ValueError('设置键必须是字符串')
    raw_bytes = path.read_bytes()
    raw = json.loads(raw_bytes)
    entries = []
    for key, value in sorted(changes.items()):
        kind = 'SETUP' if setup and key in SETUP else classify(key)
        if kind == 'FORBIDDEN':
            raise ValueError(f'{key} 不允许通过命令修改（本金、提取档位、市场范围、执行方式、硬风控与凭据类设置只能由用户本人决定）')
        if key in LIMITS:
            low, high = LIMITS[key]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'{key} 不允许设为 {value!r}：须为 {low}–{high} 的整数')
        entries.append({'key': key, 'class': kind, 'before': raw.get(key),
                        'before_present': key in raw, 'after': value})
    from .pipeline import load_config
    from .build import info
    before = load_config(path, configure_model=False)
    changed = {**raw, **changes}
    fd, temporary = tempfile.mkstemp(prefix='.config-check-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(changed, handle, ensure_ascii=False, indent=2)
        checked = load_config(temporary, configure_model=False)
    finally:
        Path(temporary).unlink(missing_ok=True)
    before_build, after_build = info(before), info(checked)
    # Include all editable effective settings in the state hash. Build identity alone deliberately
    # omits e.g. evaluation settings, which nevertheless require exact approval here.
    state = {key: before.get(key) for key in sorted(OPERATIONAL | STRATEGY | SETUP)}
    snapshot = {'version': 'config-change-v1', 'changes': entries, 'reason': reason.strip(),
                'before': {'build': before_build,
                           'editable_config_hash': journal.sha(json.dumps(state, sort_keys=True, ensure_ascii=False).encode())},
                'after_build': after_build}
    return {'entries': entries, 'snapshot': snapshot, 'checked': checked,
            'before_sha': journal.sha(raw_bytes), 'after_sha': journal.sha(journal.encoded(changed)),
            'build_before': before_build['build_id'], 'build_after': after_build['build_id']}


def _store(path, data_dir):
    from .pipeline import load_config
    store = Store(data_dir or load_config(path, configure_model=False)['data_dir'])
    journal.ensure(store)
    return store


def request_change(config_path, changes, *, reason, data_dir=None):
    """Freeze one exact STRATEGY change for authenticated review; does not modify configuration."""
    from . import approvals
    path = Path(config_path).resolve()
    with journal.locked(path):
        store = _store(path, data_dir)
        try:
            journal.recover_locked(store, path, _subject(path))
            with approvals.atomic(store):
                prepared = _prepare(path, changes, reason)
                if not any(e['class'] == 'STRATEGY' for e in prepared['entries']):
                    raise ValueError('运行类设置无需策略审批，请直接使用 config set')
                return approvals.create_request(store, kind='CONFIG', subject_id=_subject(path), action='APPLY',
                    snapshot=prepared['snapshot'], summary={'title': '策略设置变更',
                        'reason': reason.strip(), 'changes': prepared['entries'],
                        'build_before': prepared['build_before'], 'build_after': prepared['build_after']})
        finally:
            store.close()


def recover(config_path, *, data_dir=None):
    """Finish a previously authorized interrupted publication. Never approves a new operation."""
    path = Path(config_path).resolve()
    with journal.locked(path):
        store = _store(path, data_dir)
        try:
            return journal.recover_locked(store, path, _subject(path))
        finally:
            store.close()


def apply(config_path, changes, *, reason, approved_by=None, approval_id=None, data_dir=None, setup=False):
    """Publish exact approved strategy changes, or validated operational/setup changes.

    approved_by is a legacy compatibility argument and grants no permission. All writers share a lock;
    strategy authorization and the durable publication intent are reserved in one SQLite transaction.
    """
    from . import approvals
    import uuid
    path = Path(config_path).resolve()
    with journal.locked(path):
        store = _store(path, data_dir)
        try:
            journal.recover_locked(store, path, _subject(path))
            operation_id = 'CC-' + uuid.uuid4().hex
            with approvals.atomic(store):
                prepared = _prepare(path, changes, reason, setup)
                strategy = any(e['class'] == 'STRATEGY' for e in prepared['entries'])
                if strategy and not approval_id:
                    raise ValueError('策略类设置需要已确认的精确审批收据（--approval-id）；批准人字符串不授予权限')
                if approval_id and not strategy:
                    raise ValueError('运行类设置不使用策略审批收据')
                receipt = None
                if strategy:
                    receipt = approvals.check_receipt(store, approval_id, kind='CONFIG', subject_id=_subject(path),
                                                       action='APPLY', snapshot=prepared['snapshot'])
                    approvals.reserve(store, approval_id, operation_id)
                payload = {k: prepared[k] for k in ('entries', 'snapshot', 'before_sha', 'after_sha', 'build_before', 'build_after')}
                payload.update(at=now(), reason=reason.strip(), actor=receipt.get('actor') if receipt else None,
                               approval_id=approval_id)
                store.db.execute('''INSERT INTO config_apply_journal
                    (id,subject_id,approval_id,status,created_at,completed_at,payload_json) VALUES(?,?,?,?,?,?,?)''',
                    (operation_id, _subject(path), approval_id, 'APPLYING', payload['at'], None,
                     json.dumps(payload, ensure_ascii=False, sort_keys=True)))
            row = store.db.execute('SELECT * FROM config_apply_journal WHERE id=?', (operation_id,)).fetchone()
            return journal.finish(store, path, row)
        finally:
            store.close()
