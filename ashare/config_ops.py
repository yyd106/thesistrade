"""Audited configuration changes for operators and desktop agents.

Every setting belongs to one class:
  OPERATIONAL  schedules, collection volume, retention, evaluation cadence. No effect on what gets
               bought or sold. An agent may change these within the validated ranges and must tell the user.
  STRATEGY     anything that changes research judgments, trading rules, the model or how results are
               measured. Requires the name of the person who approved it (the user). Keys that change
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
from .storage import now, json_write

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
            'comparison_peers', 'business_keywords', 'model_enabled'}

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


def apply(config_path, changes, *, reason, approved_by=None, data_dir=None, setup=False):
    """changes: {key: new_value}. Returns the change log entries. Raises before writing on any violation.
    setup=True is used by the reports setup command alone, for the keys in SETUP."""
    if not reason or not reason.strip():
        raise ValueError('需要写明修改理由')
    path = Path(config_path).resolve()
    raw = json.loads(path.read_text())
    entries = []
    for key, value in changes.items():
        kind = classify(key)
        if setup and key in SETUP:
            kind = 'SETUP'
        if kind == 'FORBIDDEN':
            raise ValueError(f'{key} 不允许通过命令修改（本金、提取档位、市场范围、执行方式、硬风控与凭据类设置只能由用户本人决定）')
        if key in LIMITS:
            low, high = LIMITS[key]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'{key} 不允许设为 {value!r}：须为 {low}–{high} 的整数')
        if kind == 'STRATEGY' and not (approved_by and approved_by.strip()):
            raise ValueError(f'{key} 属于策略类设置，必须写明批准人（--approved-by，填用户本人确认的记录）')
        entries.append({'key': key, 'class': kind, 'before': raw.get(key), 'after': value})
        raw[key] = value
    from .pipeline import load_config
    from .build import info
    before_build = info(load_config(path))['build_id']
    # Validate the complete result with the normal loader before replacing the live file.
    fd, temp = tempfile.mkstemp(prefix='.config-check-', suffix='.json', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(raw, handle, ensure_ascii=False, indent=2)
        checked = load_config(temp)
    finally:
        Path(temp).unlink(missing_ok=True)
    after_build = info({**checked, 'data_dir': checked['data_dir']})['build_id']
    json_write(path, raw)
    stamp = now()
    log = Path(data_dir or checked['data_dir']) / 'workflow' / 'changes' / 'config-changes.jsonl'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('a', encoding='utf-8') as handle:
        for e in entries:
            handle.write(json.dumps({**e, 'at': stamp, 'reason': reason.strip(), 'approved_by': (approved_by or '').strip() or None,
                                     'build_before': before_build, 'build_after': after_build}, ensure_ascii=False) + '\n')
    return {'changes': entries, 'build_before': before_build, 'build_after': after_build, 'log': str(log)}
