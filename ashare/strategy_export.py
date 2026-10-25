"""Bounded, read-only strategy history downloads, with no app initialization.

Only allowlisted database fields are read. The archive contains fixed filenames,
never source files, model inputs, credentials or configuration. A generated
decision is not an accepted publication; both timelines retain their own baseline.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import zipfile
from zoneinfo import ZoneInfo

from .strategy_change_log import load_records, render_records

SCHEMA = 'strategy-export-v1'
TIMEZONE = 'Asia/Shanghai'
MAX_ROWS = 20000
MAX_UNCOMPRESSED = 96_000_000
MAX_ARCHIVE = 14_500_000
ENTRIES = ('总览.md', '研究策略.md', '交易策略变更.md', '策略数据.json')
SOURCE_TABLES = {'watchlist': 'plans', 'global': 'global_plans', 'dynamic': 'dynamic_cases'}
COLUMNS = {
    'portfolio_decisions': 'id created_at valid_until status version payload_json',
    'plans': 'id study_id symbol activated_at valid_until status strategy_version payload_json',
    'studies': 'id symbol created_at model_status result_json',
    'global_plans': 'id symbol created_at valid_until fingerprint status payload_json',
    'dynamic_cases': 'id news_id symbol name theme event_type direction created_at expires_at basis status analysis_json plan_json',
    'dynamic_news': 'id source url published_at first_seen_at title status revision_of',
    'industry_hypotheses': 'id thesis_key previous_id symbol domain method created_at review_at expires_at state fingerprint payload_json',
    'macro_events': 'id news_id created_at basis theme status payload_json',
    'macro_impact_assessments': 'id event_id asset created_at input_hash version payload_json features_json',
    'macro_impact_sources': 'id event_id published_at first_seen_at title url',
    'macro_event_revisions': 'id event_id replaced_at previous_json',
    'research_methods': 'id created_at status payload_json',
    'strategy_guidance': 'id route scope text status proposal_id adopted_at retired_at approved_by payload_json',
    'portfolio_history_snapshots': 'decision_id history_json',
    'portfolio_source_events': 'seq epoch route identity source_id source_order recorded_at observed_at event record_json',
    'portfolio_source_coverage': 'version epoch started_at observed_through',
    'cloud_outbox': 'id created_at status',
    'cloud_receipts': 'sequence bundle_id received_at completed_at',
}
JSON_KEYS = {
    'portfolio_decisions': set('summary decisions risk_groups as_of account build model publication_audit'.split()),
    'studies': {'summary', 'stocks'},
    'plans': set('kind strategy_version research_membership_token holding_unit holding_days recheck_hours research_method '
                 'event_reviews corporate_actions basis levels blockers formula max_stock_pct risk_parameters thesis '
                 'counterpoints evidence_ids notice analysis model build research_reuse previous_plan_id renewed_from'.split()),
    'global_plans': set('analysis_as_of analysis_finished_at research_contract kind blockers analysis thesis holding_days '
                        'holding_unit recheck_hours method_id event_fingerprint levels max_position_pct model_status '
                        'history_count formula model build'.split()),
    'dynamic_cases': set('news_id symbol name theme event_type direction novelty evidence direct_company_evidence '
                         'rationale reason thesis analysis counterpoints missing_fields next_checks causal_chain '
                         'impact business_link pricing priced_in invalidation history '
                         'holding_days max_position_pct stop_bps take_profit_bps buy_low_cents buy_high_cents '
                         'reference_quote_at strategy_version blockers'.split()),
    'industry_hypotheses': set('symbol name category domain method topic thesis state next_check invalidation alternatives '
                               'profit_capture causal_chain counterpoints missing facts forecasts admission_gaps revenue_scenario'.split()),
    'macro_events': set('news_id headline theme regions facts transmission uncertainty invalidation horizon expectations '
                        'expectation_basis impacts evidence'.split()),
    'macro_impact_assessments': set('event_id asset event_kind scope novelty exposure target_fit magnitude direction scale '
                                   'basis transmission missing_evidence impact_basis expectation_test citations'.split()),
    'research_methods': set('policy research_contract sources filters input full_research_hours plan_recheck_hours '
                            'max_global_model_calls max_focus holding_days_range automatic_promotion metal_macro_context'.split()),
    'strategy_guidance': set('text route scope rationale evidence proposal_id version adopted_at'.split()),
}
DECISION_KEYS = set('key route identity symbol name source_id source_token source_valid_until current_value_cents '
                    'qty current_bps max_bps can_increase reference_id cost_cents action target_bps reason '
                    'evidence_ids related_keys membership_token'.split())
BLOCKED_KEYS = re.compile(r'(?:^|_)(?:password|passwd|secrets?|credentials?|private_key|api_key|access_token|'
                          r'refresh_token|session_token|id_token|auth_token|sync_key|signing_key|key_file|'
                          r'authorization|cookies?|config|prompt|packet|raw|report_path|input|body)(?:$|_)', re.I)
STOCK_KEYS = set('symbol action analysis facts counterpoints missing_fields next_checks decision dimensions hypothesis_test'.split())
ANALYSIS_KEYS = STOCK_KEYS | set('stance thesis holding_days evidence_ids impact business_link pricing priced_in invalidation '
                                'news_id name theme event_type direction novelty evidence direct_company_evidence'.split())
MODEL_KEYS = set('requested_model requested_effort actual_model actual_effort cli_version tokens_used elapsed_seconds timed_out exit_code'.split())
BUILD_KEYS = set('version rule code model guidance build_id behavior_identity'.split())
TABLE_LABELS = {'studies': '公司研究原文', 'plans': '公司交易计划', 'global_plans': '全球资产研究与计划',
                'dynamic_cases': '动态事件研究', 'dynamic_news': '新闻引用信息', 'industry_hypotheses': '行业研究假设',
                'macro_impact_assessments': '宏观独立影响评估', 'macro_impact_sources': '宏观评估引用信息',
                'macro_events': '宏观事件研究', 'macro_event_revisions': '被替换的宏观研究版本',
                'research_methods': '研究方法', 'strategy_guidance': '已记录研究规则'}
CHANGE_LABELS = {'missing_baseline': '缺少前置版本，首次观察', 'unreadable_comparison': '记录损坏，无法可靠比较',
                 'renewal_only': '仅重新签发或续期', 'reason_update': '理由或总结更新',
                 'position_drift': '当前仓位占比漂移', 'holding_change': '持仓数量或成本变化',
                 'action_target_change': '动作或主动目标调整', 'source_change': '研究来源版本变化',
                 'source_renewal': '来源计划续期', 'scope_change': '组合项目增减',
                 'risk_groups_change': '风险分组变化', 'eligibility_change': '准入条件变化'}


class ExportTooLarge(ValueError):
    """The complete export exceeds an explicit resource limit; nothing truncated."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _time(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        normalized = re.sub(r'(\d{2}:\d{2}:\d{2})[.,](\d{1,6})(?=[+-]|$)',
                            lambda m: m[1] + '.' + m[2].ljust(6, '0'), value.replace('Z', '+00:00'))
        result = datetime.fromisoformat(normalized)
    else:
        raise ValueError('时间必须为带时区的 ISO 时间')
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('时间必须包含时区')
    return result.astimezone(timezone.utc)


def _stamp(value):
    return _time(value).isoformat()


def _clean(value, depth=0):
    if depth > 30:
        raise ValueError('研究字段嵌套过深')
    if isinstance(value, dict):
        return {k: _clean(v, depth + 1) for k, v in value.items()
                if isinstance(k, str) and not BLOCKED_KEYS.search(k)}
    if isinstance(value, list):
        return [_clean(v, depth + 1) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError('不支持的研究字段类型')


def project_record(table, record):
    """Public allowlist projection. Malformed JSON is an error, not exported text."""
    if table not in COLUMNS:
        raise ValueError('未允许的研究数据表')
    out = {}
    for field in COLUMNS[table].split():
        if field not in record:
            continue
        value = record[field]
        if field.endswith('_json'):
            decoded = json.loads(value) if isinstance(value, str) else value
            if not isinstance(decoded, dict):
                raise ValueError('研究 JSON 必须是对象')
            if table in JSON_KEYS:
                allowed = JSON_KEYS[table]
                if table == 'macro_impact_assessments' and field == 'features_json':
                    allowed = set('measurement_resolution intraday_confirmation key published_at direction basis version '
                                  'measurable gap baseline daily_volatility volatility_points change_unit benchmark '
                                  'market_url proxy market_available_at method'.split())
                decoded = {k: v for k, v in decoded.items() if k in allowed}
                if table == 'studies' and 'stocks' in decoded:
                    stocks = decoded['stocks']
                    if not isinstance(stocks, list) or any(not isinstance(s, dict) for s in stocks):
                        raise ValueError('公司研究结构损坏')
                    projected_stocks = []
                    for stock in stocks:
                        stock = {k: v for k, v in stock.items() if k in STOCK_KEYS}
                        if isinstance(stock.get('decision'), dict):
                            stock['decision'] = {k: v for k, v in stock['decision'].items()
                                                 if k in ('inclination', 'key_evidence', 'pricing', 'trigger', 'invalidation')}
                        if isinstance(stock.get('dimensions'), list):
                            stock['dimensions'] = [{k: v for k, v in d.items() if k in ('id', 'summary', 'uncertainty', 'evidence_ids')}
                                                   for d in stock['dimensions'] if isinstance(d, dict)]
                        if isinstance(stock.get('facts'), list):
                            stock['facts'] = [{k: v for k, v in f.items() if k in ('evidence_id', 'quote')}
                                              for f in stock['facts'] if isinstance(f, dict)]
                        projected_stocks.append(stock)
                    decoded['stocks'] = projected_stocks
                for key, allowed in (('analysis', ANALYSIS_KEYS), ('model', MODEL_KEYS), ('build', BUILD_KEYS)):
                    if isinstance(decoded.get(key), dict):
                        decoded[key] = {k: v for k, v in decoded[key].items() if k in allowed}
                if table == 'portfolio_decisions':
                    if 'publication_audit' in decoded:
                        from .strategy_report_audit import project_publication_audit
                        decoded['publication_audit'] = project_publication_audit(decoded['publication_audit'])
                    items = decoded.get('decisions', [])
                    if not isinstance(items, list) or any(not isinstance(d, dict) for d in items):
                        raise ValueError('组合项目结构损坏')
                    keys = [d.get('key') for d in items]
                    if any(not isinstance(k, str) or not k for k in keys) or len(set(keys)) != len(keys):
                        raise ValueError('组合项目标识缺失或重复')
                    decoded['decisions'] = [{k: v for k, v in d.items() if k in DECISION_KEYS} for d in items]
            elif table == 'macro_event_revisions':
                decoded = project_record('macro_events', decoded)
            elif table == 'portfolio_source_events':
                route = record.get('route')
                if route not in SOURCE_TABLES:
                    raise ValueError('未知研究来源路线')
                decoded = project_record(SOURCE_TABLES[route], decoded)
            elif table == 'portfolio_history_snapshots':
                # The collector handles each frozen source separately and never
                # exports the full unprojected history_json.
                raise ValueError('冻结组合须逐条投影来源')
            method_input = decoded.get('input') if table == 'research_methods' else None
            decoded = _clean(decoded)
            if isinstance(method_input, str):
                decoded['input'] = method_input
            value = _json(decoded)
        out[field] = value
    return out


def _payload(record, name='payload_json'):
    try:
        value = json.loads(record.get(name) or '{}')
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


class _Reader:
    def __init__(self, db):
        self.db = db
        self.tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.issues = []
        self.total_rows = 0

    def issue(self, code, table, identity=None):
        value = {'code': code, 'table': table, 'id': str(identity) if identity is not None else None}
        if value not in self.issues:
            self.issues.append(value)

    def raw(self, table, where='', args=(), *, order='rowid', limit=None, fields=None):
        if table not in self.tables:
            return []
        columns = {r[1] for r in self.db.execute('PRAGMA table_info("' + table + '")')}
        if fields is not None and any(c not in COLUMNS[table].split() for c in fields):
            raise ValueError('未允许的研究数据字段')
        fields = [c for c in (fields or COLUMNS[table].split()) if c in columns]
        query = 'SELECT rowid AS _order,' + ','.join('"' + c + '"' for c in fields) + ' FROM "' + table + '"'
        if where:
            query += ' WHERE ' + where
        cursor = self.db.execute(query + ' ORDER BY ' + order + ' LIMIT ?', (*args, limit or MAX_ROWS + 1))
        names = [d[0] for d in cursor.description]
        rows = [dict(zip(names, row)) for row in cursor]
        if len(rows) > MAX_ROWS:
            raise ExportTooLarge('单表导出超过记录上限，未截断：' + table)
        self.total_rows += len(rows)
        if self.total_rows > MAX_ROWS * 8:
            raise ExportTooLarge('导出总记录超过上限，未截断')
        return rows

    def window(self, table, clock, start, end):
        # Widen SQLite's millisecond clock comparison, then use precise aware
        # datetime comparisons below. Invalid clocks stay visible as issues.
        rows = self.raw(table, f'(julianday("{clock}") BETWEEN julianday(?) AND julianday(?)) OR julianday("{clock}") IS NULL',
                        ((start - timedelta(seconds=1)).isoformat(), (end + timedelta(seconds=1)).isoformat()))
        result = []
        for row in rows:
            try:
                at = _time(row.get(clock))
            except (ValueError, TypeError, OverflowError):
                self.issue('INVALID_TIME', table, row.get('id', row.get('seq')))
                continue
            if start <= at <= end:
                row[clock] = at.isoformat()
                result.append(row)
        return sorted(result, key=lambda r: (_time(r[clock]), r['_order'], str(r.get('id', r.get('seq', '')))))

    def projected(self, table, row):
        try:
            return project_record(table, row)
        except (ValueError, TypeError, RecursionError):
            self.issue('INVALID_RESEARCH_JSON', table, row.get('id', row.get('seq')))
            # Retain identity and clock for an explicit damaged placeholder.
            return {k: v for k, v in row.items() if k in COLUMNS[table].split() and not k.endswith('_json')}


def _changes(before, after, frozen_sources):
    if before is None:
        return {'categories': ['missing_baseline'], 'items': []}
    if before.get('damaged') or after.get('damaged'):
        return {'categories': ['unreadable_comparison'], 'items': []}
    old = {r['key']: r for r in before['payload'].get('decisions', []) if isinstance(r.get('key'), str)}
    new = {r['key']: r for r in after['payload'].get('decisions', []) if isinstance(r.get('key'), str)}
    categories, items = set(), []
    if before['payload'].get('summary') != after['payload'].get('summary'):
        categories.add('reason_update')
    if before['payload'].get('risk_groups') != after['payload'].get('risk_groups'):
        categories.add('risk_groups_change')
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        kinds = set()
        if a is None or b is None:
            kinds.add('scope_change')
        else:
            qty_changed = any(a.get(k) != b.get(k) for k in ('qty', 'cost_cents'))
            if qty_changed:
                kinds.add('holding_change')
            drift = (a.get('action') == b.get('action') and b.get('action') in ('HOLD', 'PAUSE')
                     and all(d.get('target_bps') == d.get('current_bps') for d in (a, b)))
            if a.get('action') != b.get('action') or a.get('target_bps') != b.get('target_bps') and not drift:
                kinds.add('action_target_change')
            elif a.get('target_bps') != b.get('target_bps') or a.get('current_bps') != b.get('current_bps'):
                kinds.add('holding_change' if qty_changed else 'position_drift')
            if any(a.get(k) != b.get(k) for k in ('reason', 'evidence_ids', 'related_keys')):
                kinds.add('reason_update')
            if a.get('can_increase') != b.get('can_increase'):
                kinds.add('eligibility_change')
            if any(a.get(k) != b.get(k) for k in ('source_id', 'source_token')):
                source_a = frozen_sources.get(before.get('source_refs', {}).get(key), {})
                source_b = frozen_sources.get(after.get('source_refs', {}).get(key), {})
                comparable = bool(source_a and source_b)
                def content(source):
                    record = source.get('record', {})
                    payload = _payload(record, 'plan_json' if source.get('table') == 'dynamic_cases' else 'payload_json')
                    for k in ('research_reuse', 'previous_plan_id', 'renewed_from'):
                        payload.pop(k, None)
                    return {k: v for k, v in record.items() if k not in
                            ('id', 'activated_at', 'created_at', 'valid_until', 'expires_at', 'status', 'payload_json', 'plan_json')}, payload
                kinds.add('source_renewal' if comparable and content(source_a) == content(source_b) else 'source_change')
        if kinds:
            # Original reasons and targets remain once in each decision payload.
            # References are lossless and avoid duplicating that prose in every
            # adjacent comparison (and on both publication timelines).
            items.append({'key': key, 'categories': sorted(kinds),
                          'before_ref': {'decision_id': before['record']['id'], 'key': key} if a is not None else None,
                          'after_ref': {'decision_id': after['record']['id'], 'key': key} if b is not None else None})
            categories.update(kinds)
    return {'categories': sorted(categories) or ['renewal_only'], 'items': items}


def collect(db, at=None):
    """Read a supplied connection; caller owns its consistent read transaction."""
    end = _time(at or datetime.now(timezone.utc).replace(microsecond=0))
    local = end.astimezone(ZoneInfo(TIMEZONE))
    start = (local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=13)).astimezone(timezone.utc)
    reader = _Reader(db)
    missing = [t for t in TABLE_LABELS if t not in reader.tables]
    rows = reader.window('portfolio_decisions', 'created_at', start, end)
    receipt_safe_before = end - timedelta(seconds=1)
    receipt_boundary_ids = []
    if 'cloud_receipts' in reader.tables:
        boundary_rows = reader.raw('cloud_receipts',
            'julianday(received_at) BETWEEN julianday(?) AND julianday(?)',
            ((receipt_safe_before - timedelta(seconds=1)).isoformat(), (end + timedelta(seconds=1)).isoformat()))
        for row in boundary_rows:
            try:
                if _time(row['received_at']) <= end:
                    receipt_boundary_ids.append(int(row['_order']))
            except (ValueError, TypeError, OverflowError):
                reader.issue('INVALID_PUBLICATION_TIME', 'cloud_receipts', row.get('bundle_id'))
    # Only two pre-window rows are needed. Old retained history must not make a
    # small 14-day export hit the current-window resource limits years later.
    def baseline(confirmed=False):
        proof, proof_args = '', []
        if confirmed:
            proofs = []
            if 'cloud_receipts' in reader.tables:
                # Only integer rowids from this readonly snapshot enter SQL.
                # Exact-filter the cutoff neighborhood before using receipts as
                # publication proof; julianday alone merges future microseconds.
                exact_boundary = ','.join(str(n) for n in receipt_boundary_ids) or 'NULL'
                proofs.append('EXISTS(SELECT 1 FROM cloud_receipts r WHERE r.bundle_id=portfolio_decisions.id '
                              'AND (julianday(r.received_at)<julianday(?) OR r.rowid IN (' + exact_boundary + ')))')
                proof_args.append(receipt_safe_before.isoformat())
            if 'cloud_outbox' in reader.tables:
                proofs.append("EXISTS(SELECT 1 FROM cloud_outbox o WHERE o.id=portfolio_decisions.id AND o.status='SENT')")
            if not proofs:
                return None
            proof = ' AND (' + ' OR '.join(proofs) + ')'
        found = reader.raw('portfolio_decisions', 'julianday(created_at)<julianday(?)' + proof,
                           tuple([start.isoformat(), *proof_args]),
                           order='julianday(created_at) DESC,rowid DESC', limit=1)
        # SQLite date arithmetic has only millisecond precision. Inspect tiny
        # neighborhoods of the boundary and coarse candidate, then compare exact
        # UTC datetimes. This handles offsets and distinct submillisecond rows
        # without loading the pre-window history.
        anchors = [start]
        if found:
            try:
                anchors.append(_time(found[0]['created_at']))
            except (ValueError, TypeError, OverflowError):
                reader.issue('INVALID_TIME', 'portfolio_decisions', found[0].get('id'))
        candidates = {row['id']: row for row in found}
        for anchor in anchors:
            nearby = reader.raw('portfolio_decisions',
                'julianday(created_at) BETWEEN julianday(?) AND julianday(?)' + proof,
                tuple([(anchor - timedelta(seconds=1)).isoformat(), (anchor + timedelta(seconds=1)).isoformat(), *proof_args]))
            candidates.update({row['id']: row for row in nearby})
        preceding = []
        for row in candidates.values():
            try:
                stamp = _time(row['created_at'])
                if stamp < start:
                    row['created_at'] = stamp.isoformat()
                    preceding.append(row)
            except (ValueError, TypeError, OverflowError):
                reader.issue('INVALID_TIME', 'portfolio_decisions', row.get('id'))
        return max(preceding, key=lambda r: (_time(r['created_at']), r['_order'])) if preceding else None
    baseline_row, published_row = baseline(), baseline(True)
    selected = {r['id']: r for r in rows}
    for row in (baseline_row, published_row):
        if row:
            selected[row['id']] = row

    def selected_rows(table, identity):
        keys = list(selected)
        found = []
        for offset in range(0, len(keys), 500):
            part = keys[offset:offset + 500]
            found.extend(reader.raw(table, identity + ' IN (' + ','.join('?' for _ in part) + ')', tuple(part)))
        return found
    outbox = {r['id']: r for r in selected_rows('cloud_outbox', 'id')}
    receipts = {}
    for r in selected_rows('cloud_receipts', 'bundle_id'):
        try:
            if _time(r['received_at']) <= end:
                receipts[r['bundle_id']] = {k: v for k, v in r.items() if k != '_order'}
        except (ValueError, TypeError):
            reader.issue('INVALID_PUBLICATION_TIME', 'cloud_receipts', r.get('bundle_id'))
    def publication(row):
        identity = row['id']
        if identity in receipts:
            return {'state': 'ACKNOWLEDGED', 'evidence': 'cloud_receipts', 'receipt': receipts[identity],
                    'notice': '云端接收确认，不等于已成交或在整个有效期一直可买入。'}
        item = outbox.get(identity)
        if item and item.get('status') == 'SENT':
            return {'state': 'ACKNOWLEDGED', 'evidence': 'local_outbox_SENT', 'received_at': None,
                    'notice': '本机记录收到发布成功回执；精确接收时刻未在本表留存，状态为导出时观察。'}
        return {'state': item.get('status') if item else 'UNKNOWN', 'evidence': 'local_outbox' if item else 'NONE',
                'notice': '仅能证明本地生成或排队，不能据此认定云端已接受。'}
    snapshots = {}
    for identity in selected:
        values = reader.raw('portfolio_history_snapshots', 'decision_id=?', (identity,))
        if values:
            snapshots[identity] = values[0]
    frozen_sources, linked = {}, {t: set() for t in ('plans', 'studies', 'global_plans', 'dynamic_cases', 'dynamic_news',
                                                   'research_methods', 'macro_events', 'macro_impact_sources')}
    def freeze_source(table, raw):
        record = reader.projected(table, raw)
        value = {'table': table, 'record': record}
        identity = hashlib.sha256(_json(value).encode()).hexdigest()
        frozen_sources[identity] = value
        if record.get('study_id'):
            linked['studies'].add(record['study_id'])
        if record.get('news_id'):
            linked['dynamic_news'].add(record['news_id'])
        return identity
    decisions = {}
    for identity, row in selected.items():
        projected = reader.projected('portfolio_decisions', row)
        payload = _payload(projected)
        decision = {'record': {k: v for k, v in projected.items() if k != 'payload_json'},
                    'payload': payload, 'publication': publication(row), 'source_refs': {}, 'frozen_history': None,
                    'damaged': 'payload_json' not in projected, 'source_order': row['_order']}
        snap = snapshots.get(identity)
        if snap:
            try:
                history = json.loads(snap['history_json'])
                if not isinstance(history, dict) or not isinstance(history.get('sources'), dict):
                    raise ValueError('冻结来源结构无效')
                decision['frozen_history'] = {k: history[k] for k in ('version', 'epoch', 'coverage_started_at', 'frozen_at', 'cursor') if k in history}
                for key, value in sorted(history['sources'].items()):
                    route = key.split(':', 1)[0]
                    raw = value.get('record') if isinstance(value, dict) else None
                    if route in SOURCE_TABLES and isinstance(raw, dict):
                        decision['source_refs'][key] = freeze_source(SOURCE_TABLES[route], raw)
                    elif raw is not None:
                        reader.issue('INVALID_FROZEN_SOURCE', 'portfolio_history_snapshots', identity)
            except (ValueError, TypeError, AttributeError):
                reader.issue('INVALID_FROZEN_HISTORY', 'portfolio_history_snapshots', identity)
        if decision['frozen_history'] is None:
            reader.issue('FROZEN_HISTORY_UNAVAILABLE', 'portfolio_decisions', identity)
        for item in payload.get('decisions', []):
            table = SOURCE_TABLES.get(item.get('route'))
            if table and item.get('source_id'):
                linked[table].add(item['source_id'])
        decisions[identity] = decision
    source_events = []
    for row in reader.window('portfolio_source_events', 'recorded_at', start, end):
        event = reader.projected('portfolio_source_events', row)
        raw = _payload(event, 'record_json')
        table = SOURCE_TABLES.get(event.get('route'))
        event.pop('record_json', None)
        event['source_ref'] = freeze_source(table, raw) if table and raw else None
        source_events.append(event)
    research = {}
    for table in TABLE_LABELS:
        if table in ('dynamic_news', 'macro_impact_sources'):
            continue
        clock = 'activated_at' if table == 'plans' else 'replaced_at' if table == 'macro_event_revisions' else 'adopted_at' if table == 'strategy_guidance' else 'created_at'
        chosen = {str(r['id']): r for r in reader.window(table, clock, start, end)}
        current_guidance = set()
        if table == 'strategy_guidance':
            for record in reader.raw(table, "status='ADOPTED'"):
                chosen[str(record['id'])] = record
                current_guidance.add(str(record['id']))
        for identity in sorted(linked.get(table, ())):
            if identity not in chosen:
                found = reader.raw(table, 'id=?', (identity,))
                if found:
                    chosen[identity] = found[0]
                else:
                    reader.issue('LINKED_RESEARCH_UNAVAILABLE', table, identity)
        result = []
        for identity, raw in chosen.items():
            record = reader.projected(table, raw)
            try:
                source_at = _time(record.get(clock))
                if source_at > end:
                    reader.issue('LINKED_RESEARCH_AFTER_CUTOFF', table, identity)
                    continue
                record[clock] = source_at.isoformat()
            except (ValueError, TypeError, OverflowError):
                reader.issue('INVALID_TIME', table, identity)
                continue
            if record.get('study_id'):
                linked['studies'].add(record['study_id'])
            if record.get('news_id'):
                linked['dynamic_news'].add(record['news_id'])
            if table == 'global_plans' and _payload(record).get('method_id'):
                linked['research_methods'].add(_payload(record)['method_id'])
            if table == 'macro_impact_assessments':
                linked['macro_events'].add(record['event_id'])
                for citation in _payload(record).get('citations', []):
                    if isinstance(citation, dict) and isinstance(citation.get('source_id'), str):
                        linked['macro_impact_sources'].add(citation['source_id'])
            item = {'record': record, 'outside_window_linked': source_at < start}
            if identity in current_guidance:
                item['current_state_baseline'] = True
            result.append(item)
        research[table] = sorted(result, key=lambda item: (_time(item['record'][clock]), str(item['record']['id'])))
    # Plans occur after studies in TABLE_LABELS. Close their references once,
    # preserving actual study output rather than copying snapshot model inputs.
    known_studies = {r['record']['id'] for r in research['studies']}
    for identity in sorted(linked['studies'] - known_studies):
        found = reader.raw('studies', 'id=?', (identity,))
        if found:
            record = reader.projected('studies', found[0])
            try:
                when = _time(record['created_at'])
                if when <= end:
                    record['created_at'] = when.isoformat()
                    research['studies'].append({'record': record, 'outside_window_linked': when < start})
                else:
                    reader.issue('LINKED_RESEARCH_AFTER_CUTOFF', 'studies', identity)
            except (ValueError, TypeError):
                reader.issue('INVALID_TIME', 'studies', identity)
        else:
            reader.issue('LINKED_RESEARCH_UNAVAILABLE', 'studies', identity)
    research['studies'].sort(key=lambda r: (_time(r['record']['created_at']), str(r['record']['id'])))
    research['dynamic_news'] = []
    research['macro_impact_sources'] = []
    for identity in sorted(linked['macro_impact_sources']):
        found = reader.raw('macro_impact_sources', 'id=?', (identity,))
        if found:
            research['macro_impact_sources'].append({'record': reader.projected('macro_impact_sources', found[0]), 'metadata_at_capture': True})
        else:
            linked['dynamic_news'].add(identity)
    for identity in sorted(linked['dynamic_news']):
        found = reader.raw('dynamic_news', 'id=?', (identity,))
        if found:
            research['dynamic_news'].append({'record': reader.projected('dynamic_news', found[0]), 'metadata_at_capture': True})
        else:
            reader.issue('LINKED_NEWS_METADATA_UNAVAILABLE', 'dynamic_news', identity)
    baseline = decisions.get(baseline_row['id']) if baseline_row else None
    published_baseline = decisions.get(published_row['id']) if published_row else None
    previous, accepted_previous = baseline, published_baseline
    timeline, accepted_ids = [], []
    for row in rows:
        item = decisions[row['id']]
        item['previous_generated_id'] = previous['record']['id'] if previous else None
        item['generated_change'] = _changes(previous, item, frozen_sources)
        if item['publication']['state'] == 'ACKNOWLEDGED':
            item['previous_acknowledged_id'] = accepted_previous['record']['id'] if accepted_previous else None
            item['acknowledged_change'] = _changes(accepted_previous, item, frozen_sources)
            if item['previous_acknowledged_id'] == item['previous_generated_id']:
                item['acknowledged_change'] = {
                    'categories': item['generated_change']['categories'],
                    'items_ref': 'generated_change.items',
                    'notice': '两条路线的前置版本相同，逐项前后原记录复用生成路线比较，未删减研究内容。'}
            accepted_previous = item
            accepted_ids.append(row['id'])
        else:
            item['previous_acknowledged_id'] = None
            item['acknowledged_change'] = None
        timeline.append(item)
        previous = item
    coverage_rows = reader.raw('portfolio_source_coverage')
    coverage = {'missing_tables': missing, 'generated_decisions': len(timeline), 'acknowledged_decisions': len(accepted_ids),
                'frozen_decisions': sum(d['frozen_history'] is not None for d in timeline),
                'research_counts': {t: len(v) for t, v in research.items()},
                'source_history': [{k: v for k, v in r.items() if k != '_order'} for r in coverage_rows],
                'full_history_proven': False,
                'limitations': ['导出数据库当前保留的研究版本，不将无记录解释为没有研究；缺表或缺失关联见 issues。',
                    '研究输出包含原文引用片段；不包含整份原始资料、模型输入、配置或凭据。',
                    '组合理由是该次模型原话；变化标签是确定性字段比较，不是事后推断原因。',
                    '已确认发布也不代表已成交；没有发布回执的本地决定不视为执行策略。',
                    '原生研究表的生命周期状态和新闻引用信息是导出时可见值；只有 frozen_sources 是决策当时冻结的来源投影。',
                    '来源更新记录只表示单个来源，不是完整历史组合；不跨缺失历史补写当时结论。']}
    data = {'schema': SCHEMA, 'generated_at': end.isoformat(), 'start_at': start.isoformat(), 'end_at': end.isoformat(),
            'timezone': TIMEZONE, 'scope': '近14个北京时间自然日，含今天，截止生成时刻；额外保留窗口前基线及明确关联研究。',
            'coverage': coverage, 'baseline': baseline, 'published_baseline': published_baseline, 'decisions': timeline,
            'published_decision_ids': accepted_ids, 'research': research, 'frozen_sources': frozen_sources,
            'source_events': source_events, 'issues': reader.issues}
    from .strategy_report_audit import guidance_summary, summarize
    guidance = reader.raw('strategy_guidance', fields=('status', 'adopted_at', 'retired_at')) if 'strategy_guidance' in reader.tables else None
    coverage['guidance_summary'] = guidance_summary(guidance, start.isoformat(), end.isoformat())
    coverage['report_audit'] = summarize(data)
    from .strategy_condition_report import summarize as summarize_conditions
    coverage['condition_audit'] = summarize_conditions(data)
    return data


def _safe_text(value):
    # Escape Markdown/HTML control syntax in untrusted model prose.
    text = str(value if value is not None else '未记录').replace('\r', '')
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return re.sub(r'([\\`*_{}\[\]()#+!|])', r'\\\1', text)


def _local(value):
    try:
        return _time(value).astimezone(ZoneInfo(TIMEZONE)).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return '未记录'


def _weight(value):
    return f'{value / 100:g}%' if type(value) in (int, float) else '未记录'


FIELD_LABELS = {
    'id': '记录编号', 'symbol': '标的代码', 'name': '名称', 'created_at': '形成时间',
    'activated_at': '签发时间', 'valid_until': '有效期至', 'expires_at': '到期时间',
    'model_status': '研究生成状态', 'status': '记录状态', 'state': '判断状态',
    'result_json': '研究结果', 'payload_json': '完整研究与计划', 'analysis_json': '事件研究',
    'plan_json': '事件计划', 'previous_json': '修订前研究', 'summary': '总结', 'stocks': '公司判断',
    'analysis': '分析', 'decision': '研究判断', 'action': '动作', 'thesis': '研究依据',
    'trigger': '成立条件', 'invalidation': '失效条件', 'counterpoints': '反证',
    'next_checks': '尚待核验', 'facts': '已引用事实', 'key_evidence': '关键证据',
    'evidence_id': '证据编号', 'evidence_ids': '证据引用', 'quote': '引用内容',
    'implication': '研究含义', 'confidence': '置信程度', 'reason': '理由', 'rationale': '研究理由',
    'kind': '计划类别', 'blockers': '限制条件', 'levels': '价格条件', 'formula': '价格计算规则',
    'source': '来源', 'url': '来源链接', 'title': '标题', 'published_at': '来源发布时间',
    'first_seen_at': '首次取得时间', 'basis': '证据性质', 'direction': '方向', 'theme': '主题',
    'event_type': '事件类别', 'news_id': '新闻编号', 'study_id': '研究编号', 'domain': '研究领域',
    'method': '研究方法', 'method_id': '研究方法编号', 'review_at': '复核时间',
    'uncertainty': '不确定性', 'alternatives': '其他解释', 'causal_chain': '因果推理',
    'missing': '缺失资料', 'missing_fields': '尚缺资料', 'admission_gaps': '准入缺口',
    'forecasts': '待检验预测', 'next_check': '下次核验', 'profit_capture': '利润兑现分析',
    'holding_days': '计划持有天数', 'holding_unit': '持有期单位', 'recheck_hours': '复核间隔小时',
    'max_position_pct': '仓位上限百分比', 'max_stock_pct': '单股上限百分比',
    'current_bps': '当前占比基点', 'target_bps': '目标占比基点', 'related_keys': '关联标的',
    'evidence': '引用证据', 'direct_company_evidence': '公司直接证据', 'novelty': '新增信息',
    'research_contract': '研究约定', 'research_reuse': '沿用研究记录',
    'analysis_as_of': '研究依据截止时间', 'analysis_finished_at': '研究完成时间',
    'model': '研究模型记录', 'build': '版本追溯', 'strategy_version': '策略规则版本',
    'risk_groups': '共同风险约束', 'members': '组内标的', 'cap_bps': '上限基点',
    'research_method': '研究方法', 'event_reviews': '公告核验', 'corporate_actions': '公司行为',
    'risk_parameters': '风险参数', 'notice': '说明', 'code': '缺口类别', 'table': '记录类别',
    'inclination': '研究倾向', 'pricing': '估值与价格反映', 'priced_in': '价格是否已反映',
    'dimensions': '分维度分析', 'hypothesis_test': '可检验假设', 'claim': '假设命题',
    'metric': '检验指标', 'operator': '比较方式', 'threshold': '阈值', 'deadline': '核验期限',
    'impact': '事件冲击', 'business_link': '业务传导', 'stance': '研究方向',
    'buy_low_cents': '买入下限（人民币分）', 'buy_high_cents': '买入上限（人民币分）',
    'stop_cents': '止损价（人民币分）', 'sell_cents': '止盈价（人民币分）',
    'buy_low_micros': '买入下限（美元百万分之一）', 'buy_high_micros': '买入上限（美元百万分之一）',
    'stop_micros': '止损价（美元百万分之一）', 'sell_micros': '止盈价（美元百万分之一）',
    'stop_bps': '成本止损基点', 'take_profit_bps': '成本止盈基点',
    'paper_stop_loss_bps': '程序止损参数（基点）', 'paper_take_profit_bps': '程序止盈参数（基点）',
    'paper_entry_band_bps': '程序入场区间参数（基点）', 'last_complete_date': '行情截至交易日',
    'current_value_cents': '持仓估值（人民币分）', 'qty': '持仓数量原始值', 'cost_cents': '持仓成本（人民币分）',
    'max_bps': '仓位上限（基点）', 'can_increase': '当时增加资格', 'source_id': '来源编号',
    'source_valid_until': '来源有效期', 'source_kind': '原计划类别', 'recorded_blockers': '已记录限制',
    'publication': '发布时校验',
}
ACTION_LABELS = {'ALLOW': '允许新增', 'HOLD': '保持', 'PAUSE': '暂停新增', 'REDUCE': '减仓', 'EXIT': '退出'}
ROUTE_LABELS = {'watchlist': '公司研究', 'dynamic': '事件研究', 'global': '全球资产'}


def _action(item):
    if not item:
        return '未列入本轮'
    return _safe_text(ACTION_LABELS.get(item.get('action'), item.get('action', '未记录'))) + ' / ' + _weight(item.get('target_bps'))


def _cell(value):
    return _safe_text(value).replace('\n', '；')


def _details(value, depth=0):
    """Lossless readable hierarchy; no arbitrary string/list truncation."""
    lines = []
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                lines.append('  ' * depth + '- ' + _safe_text(FIELD_LABELS.get(key, key)) + '：')
                lines.extend(_details(child, depth + 1))
            else:
                lines.append('  ' * depth + '- ' + _safe_text(FIELD_LABELS.get(key, key)) + '：' + _safe_text(child).replace('\n', '\n' + '  ' * (depth + 1)))
    elif isinstance(value, list):
        for index, child in enumerate(value, 1):
            if isinstance(child, (dict, list)):
                lines.append('  ' * depth + f'- 第 {index} 项：')
                lines.extend(_details(child, depth + 1))
            else:
                lines.append('  ' * depth + '- ' + _safe_text(child))
    else:
        lines.append('  ' * depth + '- ' + _safe_text(value))
    return lines


def _present(value):
    return value is not None and value != '' and value != [] and value != {}


def _select_fields(value, keys):
    return {key: value[key] for key in keys if isinstance(value, dict) and _present(value.get(key))}


def _source_clock(record):
    return record.get('activated_at') or record.get('created_at')


def _no_later(value, cutoff):
    try:
        return _time(value) <= _time(cutoff)
    except (ValueError, TypeError, OverflowError):
        return False


def _frozen_source(data, decision, item):
    """Only the matching, contemporaneous frozen source may explain a decision."""
    ref = decision.get('source_refs', {}).get(item.get('key'))
    source = data['frozen_sources'].get(ref, {})
    record = source.get('record', {})
    if (source.get('table') == SOURCE_TABLES.get(item.get('route'))
            and record.get('id') == item.get('source_id')
            and record.get('symbol') == item.get('symbol')
            and _no_later(_source_clock(record), decision['record']['created_at'])):
        return ref, source
    return None, None


def _report_cards(data):
    """Presentation-only index; never adds inferred research to the export data."""
    cards, referenced = {}, set()
    for decision in data['decisions']:
        for item in decision['payload'].get('decisions', []):
            ref, source = _frozen_source(data, decision, item)
            identity = ('frozen', ref) if source else ('missing', item.get('key'), item.get('source_id'))
            card = cards.setdefault(identity, {
                'table': source['table'] if source else SOURCE_TABLES.get(item.get('route')),
                'record': source['record'] if source else {}, 'ref': ref,
                'symbol': item.get('symbol'), 'name': item.get('name'), 'route': item.get('route'),
                'source_id': item.get('source_id'), 'decisions': [], 'origin': 'frozen' if source else 'missing'})
            card['decisions'].append((decision, item))
            if source:
                referenced.add((source['table'], source['record']['id']))
    for route, table in SOURCE_TABLES.items():
        for entry in data['research'].get(table, []):
            record = entry['record']
            if (table, record['id']) in referenced:
                continue
            cards[('retained', table, record['id'])] = {
                'table': table, 'record': record, 'ref': None, 'symbol': record.get('symbol'),
                'name': record.get('name'), 'route': route, 'source_id': record['id'],
                'decisions': [], 'origin': 'retained'}
    studies = {entry['record']['id']: entry['record'] for entry in data['research'].get('studies', [])}
    linked_stocks = {(c['record'].get('study_id'), c['symbol']) for c in cards.values()
                     if _no_later(studies.get(c['record'].get('study_id'), {}).get('created_at'),
                                  _source_clock(c['record']))}
    for entry in data['research'].get('studies', []):
        record = entry['record']
        stocks = _payload(record, 'result_json').get('stocks', [])
        for index, stock in enumerate(stocks or [{'symbol': record.get('symbol')}]):
            if (record['id'], stock.get('symbol')) in linked_stocks:
                continue
            cards[('study', record['id'], index)] = {
                'table': 'studies', 'record': record, 'stock': stock, 'ref': None,
                'symbol': stock.get('symbol'), 'name': None, 'route': 'watchlist',
                'source_id': record['id'], 'decisions': [], 'origin': 'retained'}
    for entry in data['research'].get('industry_hypotheses', []):
        record = entry['record']
        payload = _payload(record)
        cards[('industry', record['id'])] = {
            'table': 'industry_hypotheses', 'record': record, 'ref': None,
            'symbol': record.get('symbol'), 'name': payload.get('name'), 'route': 'industry',
            'source_id': record['id'], 'decisions': [], 'origin': 'retained'}
    return sorted(cards.values(), key=lambda c: (str(c['symbol'] or ''), str(c['route'] or ''),
                                               _source_clock(c['record']) or '', str(c['ref'] or c['source_id'] or '')))


def _research_material(studies, card):
    record, table = card['record'], card['table']
    plan = _payload(record, 'plan_json' if table == 'dynamic_cases' else 'payload_json')
    study, stock = None, card.get('stock', {})
    if table == 'plans' and record.get('study_id'):
        candidate = studies.get(record['study_id'])
        if candidate and _no_later(candidate.get('created_at'), _source_clock(record)):
            study = candidate
            stock = next((s for s in _payload(study, 'result_json').get('stocks', [])
                          if s.get('symbol') == card['symbol']), {})
    if table == 'dynamic_cases':
        analysis = _payload(record, 'analysis_json')
    elif table in ('studies', 'plans'):
        analysis = stock or (plan.get('analysis') if isinstance(plan.get('analysis'), dict) else {})
    elif table == 'industry_hypotheses':
        analysis = plan
    else:
        analysis = plan.get('analysis') if isinstance(plan.get('analysis'), dict) else {}
    judgment = analysis.get('decision') if isinstance(analysis.get('decision'), dict) else {}
    return plan, analysis, judgment, study


def _report_section(title, groups, missing):
    lines = ['##### ' + title, '']
    populated = [(label, value) for label, value in groups if _present(value)]
    if not populated:
        return lines + [missing, '']
    for label, value in populated:
        lines += ['**' + label + '**', ''] + _details(value) + ['']
    return lines


def _price_rule(plan, low, high, scale, currency):
    levels = plan.get('levels') if isinstance(plan.get('levels'), dict) else plan
    def amount(key):
        value = levels.get(key)
        if type(value) not in (int, float):
            return '未记录'
        rendered = format(Decimal(str(value)) / scale, 'f')
        if '.' in rendered:
            rendered = rendered.rstrip('0').rstrip('.')
        return rendered + ' ' + currency
    return amount(low) + ' — ' + amount(high)


def _stock_reports(data):
    cards = _report_cards(data)
    studies = {entry['record']['id']: entry['record'] for entry in data['research'].get('studies', [])}
    news = {entry['record']['id']: entry['record'] for entry in data['research'].get('dynamic_news', [])}
    lines = ['## 逐标的研究与交易方案', '',
             '每个标的、每条研究路线展开最后一次组合决定引用的方案；尚无组合决定的，展开最后一份留存研究。历史来源逐项列在索引，完整研究与逐次决定仍保留在原始记录及交易文档中。',
             '内容来自已留存字段的确定性编排，不新增投资判断；缺失项不能视为条件已满足。固定时间栏使用北京时间；引用原文中的时间保留原记录格式。', '']
    if not cards:
        return lines + ['本窗口没有可整理的逐标的研究或组合项目；请查看末尾原始记录与总览中的证据缺口。', '']
    latest = {}
    def order(card):
        if card['decisions']:
            decision = card['decisions'][-1][0]
            return True, decision['record']['created_at'], decision.get('source_order', 0)
        return False, _source_clock(card['record']) or '', 0
    for card in cards:
        key = (card['symbol'], card['route'])
        if key not in latest or order(card) > order(latest[key]):
            latest[key] = card
    detail_numbers = {id(card): number for number, card in enumerate(latest.values(), 1)}
    lines += ['#### 历史来源版本索引', '',
              f'共 {len(cards)} 份来源版本，展开 {len(latest)} 份最新方案。索引仅省去重复排版，不删除任何原始版本或决策。',
              '按来源编号在下方原始研究记录检索；冻结引用对应策略数据.json 的 frozen_sources，逐次对应关系见 decisions 的 source_refs。', '',
              '| 标的代码 / 路线 | 来源编号 | 形成时间 | 来源性质 | 冻结引用 | 窗口内决定次数及首末编号 | 最新方案 |',
              '| --- | --- | --- | --- | --- | --- | --- |']
    for card in cards:
        decisions = card['decisions']
        span = (str(len(decisions)) + ' 次；' + str(decisions[0][0]['record']['id']) + ' → ' +
                str(decisions[-1][0]['record']['id'])) if decisions else '未找到匹配冻结组合'
        lines.append('| ' + ' | '.join((
            _cell(str(card['symbol'] or '未记录') + ' / ' + ROUTE_LABELS.get(card['route'], '产业研究')),
            _cell(card['source_id']), _local(_source_clock(card['record'])),
            {'frozen': '决策时冻结', 'missing': '历史来源缺失', 'retained': '导出时留存'}[card['origin']],
            _cell(card['ref'] or '—'), _cell(span),
            str(detail_numbers[id(card)]) if id(card) in detail_numbers else '见原始记录')) + ' |')
    lines += ['']
    for number, card in enumerate(latest.values(), 1):
        record, table = card['record'], card['table']
        plan, analysis, judgment, study = _research_material(studies, card)
        name = card.get('name') or analysis.get('name') or '名称未记录'
        symbol = card.get('symbol') or '代码未记录'
        title = f'{name}（{symbol}）'
        lines += [f'### {number}. {_safe_text(title)} · {_safe_text(ROUTE_LABELS.get(card["route"], "产业研究"))}', '',
                  '来源记录：' + _safe_text(card['source_id']) + '；冻结来源引用：' + _safe_text(card['ref']) + '。', '',
                  '#### 一、覆盖范围与版本', '']
        if card['origin'] == 'frozen':
            lines += ['此卡使用组合决定当时冻结的来源。明确关联的公司研究按研究编号查询；只有不晚于计划形成时间的同一标的研究才在本卡展开。', '']
        elif card['origin'] == 'missing':
            lines += ['**历史研究缺口：缺少匹配且不晚于决策时点的冻结来源，无法还原当时研究与程序价格规则。未用当前来源补写历史。**', '']
        else:
            lines += ['此卡是导出时留存的研究版本，本窗口未找到引用此版本的匹配冻结组合；不据此推断已经发布或授权交易。', '']
        lines += [f'- 研究/计划形成时间：{_local(_source_clock(record))}',
                  '- 研究依据截止时间：' + _local(plan.get('analysis_as_of')),
                  '- 行情截至交易日：' + _safe_text((plan.get('basis') or {}).get('last_complete_date') if isinstance(plan.get('basis'), dict) else None),
                  '- 关联公司研究：' + _safe_text(record.get('study_id') or (record.get('id') if table == 'studies' else None)),
                  '- 公司研究形成时间：' + _local((study or (record if table == 'studies' else {})).get('created_at')),
                  '- 原记录状态：' + _safe_text(record.get('status') or record.get('state') or record.get('model_status')),
                  '- 计划有效期至：' + _local(record.get('valid_until') or record.get('expires_at')), '']
        if not plan.get('analysis_as_of'):
            lines += ['研究依据的统一截止时点未单独留存；形成时间、行情日期和资料发布时间各有含义，不能相互替代。', '']
        if table == 'plans' and record.get('study_id') and (study is None or not analysis):
            reason = '关联研究未留存或晚于计划形成时间' if study is None else '关联研究没有同一标的的输出'
            lines += ['公司研究缺口：' + reason + '；只展示计划自身留存的内容。', '']
        lines += ['#### 二、研究方案与分析依据', '']
        lines += _report_section('1. 研究问题、假设与判断', [
            ('明确命题与可检验假设', _select_fields(analysis, ('topic', 'thesis', 'hypothesis_test', 'stance', 'action'))),
            ('研究倾向', judgment.get('inclination')),
            ('计划留存的研究依据', plan.get('thesis'))],
            '未记录明确研究问题或假设；不能把组合选中结果倒推成研究结论。')
        evidence = _select_fields(analysis, ('facts', 'evidence', 'direct_company_evidence', 'evidence_ids'))
        lines += _report_section('2. 证据、来源与取得时点', [
            ('研究引用', evidence), ('关键证据及交易含义', judgment.get('key_evidence')),
            ('计划证据编号', plan.get('evidence_ids')),
            ('明确关联新闻的引用信息', news.get(record.get('news_id')))],
            '未记录可核对的证据引用；研究判断的事实基础在本报告中无法复核。')
        lines += ['证据编号用于回溯，不代表已提供原始文件。未列出资料标题、链接、发布时间或取得时间的引用，其元数据在本报告中缺失；不从编号猜测。新闻元数据为导出时查询。', '']
        lines += _report_section('3. 分析推导与价格反映', [
            ('原始分析', _select_fields(analysis, ('analysis', 'rationale', 'reason', 'impact', 'business_link', 'causal_chain', 'profit_capture', 'dimensions'))),
            ('价格反映判断', _select_fields(judgment, ('pricing',)) or _select_fields(analysis, ('pricing', 'priced_in'))),
            ('研究方法与约定', _select_fields(plan, ('research_method', 'research_contract', 'method_id')))],
            '未单独记录分析推导或价格反映判断；不得仅凭入场区间解释为估值结论。')
        lines += _report_section('4. 催化观察、反证与核验计划', [
            ('研究成立条件', judgment.get('trigger')),
            ('后续观察与待检验预测', _select_fields(analysis, ('next_checks', 'next_check', 'forecasts'))),
            ('反证与其他解释', _select_fields(analysis, ('counterpoints', 'alternatives')) or _select_fields(plan, ('counterpoints',))),
            ('研究失效条件', judgment.get('invalidation') or analysis.get('invalidation'))],
            '未记录催化观察、反证或核验计划；不能视为没有反证或风险。')
        lines += ['#### 三、程序交易规则（记录值）', '',
                  '以下价格与限制是该版本的程序计划，研究观点和组合目标分别见前后两节。记录缺失时不套用当前默认参数。', '']
        if table == 'global_plans':
            interval = _price_rule(plan, 'buy_low_micros', 'buy_high_micros', 1_000_000, '美元')
            exits = _price_rule(plan, 'stop_micros', 'sell_micros', 1_000_000, '美元')
        else:
            interval = _price_rule(plan, 'buy_low_cents', 'buy_high_cents', 100, '元')
            exits = _price_rule(plan, 'stop_cents', 'sell_cents', 100, '元')
        cap = plan.get('max_stock_pct', plan.get('max_position_pct'))
        lines += ['- 入场价格区间：' + interval,
                  '- 止损价 / 止盈价：' + exits,
                  '- 计划仓位上限：' + (f'{cap:g}%' if type(cap) in (int, float) else '未记录'), '']
        lines += _report_section('入场触发、限制与退出条件', [
            ('计划类别、公式及限制', _select_fields(plan, ('kind', 'formula', 'blockers'))),
            ('止损止盈参数', _select_fields(plan, ('risk_parameters', 'stop_bps', 'take_profit_bps'))),
            ('持有期、复核与报价基准', _select_fields(plan, ('holding_days', 'holding_unit', 'recheck_hours', 'reference_quote_at')))],
            '未记录程序入场、退出与复核条件，无法从研究观点补齐执行规则。')
        lines += ['未留存明确持有期或退出条件的部分均为缺口；研究失效条件不自动等于程序已实现的卖出条件。', '',
                  '#### 四、组合决定与交易对应', '']
        if not card['decisions']:
            lines += ['本窗口未找到与此卡对应的冻结组合决定；不代表没有其他版本的决定。', '']
        else:
            lines += [f'本来源被窗口内组合决定引用 {len(card["decisions"])} 次；此处展开最后一次。全部当轮理由、动作、仓位及发布变化见“交易策略变更.md”，首末编号见历史来源版本索引。', '']
        for decision, item in card['decisions'][-1:]:
            from .strategy_report_audit import render_publication_item
            lines += render_publication_item(decision, item, data)
            from .strategy_condition_report import render_item as render_conditions
            lines += render_conditions(decision, item, data)
            lines += ['- 决策编号：' + _safe_text(decision['record']['id']) + '；形成时间：' + _local(decision['record']['created_at']),
                      '- 动作：' + _action(item) + '；当时当前仓位：' + _weight(item.get('current_bps')) + '；单项上限：' + _weight(item.get('max_bps')),
                      '- 当轮记录理由：' + _safe_text(item.get('reason')),
                      '- 组合证据引用：' + _safe_text('、'.join(map(str, item.get('evidence_ids') or [])) or '未记录'),
                      '- 发布状态：' + _safe_text(decision['publication']['state']) + '；组合有效期至：' + _local(decision['record'].get('valid_until')),
                      '- 来源有效期至：' + _local(item.get('source_valid_until')) + '；允许增加：' + ('是' if item.get('can_increase') is True else '否' if item.get('can_increase') is False else '未记录'), '']
        lines += ['**成交核对：本报告未纳入模拟成交明细，无法确认是否成交、成交价、数量或盈亏。应以云端账本核对决策编号、来源计划编号和标的代码；已确认发布不等于已成交。**', '',
                  '#### 五、缺口与版本追溯', '']
        coverage = (
            ('明确研究命题/可检验假设', _select_fields(analysis, ('topic', 'thesis', 'hypothesis_test'))),
            ('证据引用', evidence or judgment.get('key_evidence') or plan.get('evidence_ids')),
            ('统一研究依据截止时间', plan.get('analysis_as_of')),
            ('价格反映判断', judgment.get('pricing') or analysis.get('pricing')),
            ('研究成立条件', judgment.get('trigger')),
            ('反证/其他解释', analysis.get('counterpoints') or analysis.get('alternatives') or plan.get('counterpoints')),
            ('研究失效条件', judgment.get('invalidation') or analysis.get('invalidation')),
            ('后续核验计划', analysis.get('next_checks') or analysis.get('next_check')),
            ('明确计划持有期', plan.get('holding_days')),
        )
        lines += ['| 核对字段 | 留存情况 |', '| --- | --- |']
        lines += ['| ' + label + ' | ' + ('已留存，见上文' if _present(value) else '未记录，不能补推') + ' |'
                  for label, value in coverage]
        lines += ['', '“已留存”只描述字段覆盖，不评价证据质量或策略可执行性。', '']
        lines += _report_section('研究资料缺口', [
            ('原研究披露的缺口', _select_fields(analysis, ('missing_fields', 'missing', 'admission_gaps')))],
            '原研究未单列资料缺口；不代表资料已齐全。')
        lines += _report_section('版本与适用范围', [
            ('来源版本', _select_fields(record, ('strategy_version', 'fingerprint', 'previous_id'))),
            ('计划版本与沿用关系', _select_fields(plan, ('strategy_version', 'build', 'model', 'research_reuse', 'previous_plan_id', 'renewed_from')))],
            '版本信息未记录；仅可凭来源记录编号追溯。')
    return lines


def _overview(data):
    c = data['coverage']
    lines = ['# 近14天研究与交易策略', '',
             f'北京时间 {_local(data["start_at"])} 至 {_local(data["end_at"])}。', '',
             f'生成组合 {c["generated_decisions"]} 次，其中可确认发布 {c["acknowledged_decisions"]} 次；'
             f'具备冻结来源 {c["frozen_decisions"]} 次。', '',
             '[研究策略](研究策略.md) · [研究评审与交易策略变更](交易策略变更.md) · [结构化完整投影](策略数据.json)', '',
             '## 阅读入口', '',
             '1. 在下方标的索引定位股票代码和研究路线，再打开“研究策略.md”的“逐标的研究与交易方案”。',
             '2. 每张卡按研究问题与证据、分析推导、催化与反证、程序价格规则、组合仓位与发布、缺口与版本阅读。',
             '3. 需要核对调整原因时读“交易策略变更.md”；需要复核原始字段时读研究文档后半部及“策略数据.json”。', '',
             '研究判断回答“为什么关注”，程序规则回答“什么条件允许执行”，组合决定回答“该轮分配多少”。三者分别保留，不从选股结果反推研究理由。',
             '窗口是近14个北京时间自然日；模板不表示策略还能继续执行两周，执行期限须逐项查看。', '',
             '## 标的索引（窗口内最后一次生成决定）', '',
             '这里是窗口内最后一条留存决定，不自动代表当前有效、已发布或已成交。尚未进入组合的研究见研究文档中的来源版本卡。', '',
             '| 股票/资产 | 代码 | 研究路线 | 最后决定时间 | 动作 / 目标仓位 | 发布状态 | 当时研究来源 |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    latest = {}
    for decision in data['decisions']:
        for item in decision['payload'].get('decisions', []):
            latest[item['key']] = (decision, item)
    for key in sorted(latest):
        decision, item = latest[key]
        ref, source = _frozen_source(data, decision, item)
        lines.append('| ' + ' | '.join((
            _cell(item.get('name') or '名称未记录'), _cell(item.get('symbol')),
            _cell(ROUTE_LABELS.get(item.get('route'), item.get('route'))),
            _local(decision['record']['created_at']), _action(item), _cell(decision['publication']['state']),
            _cell(item.get('source_id')) + ('；匹配冻结来源' if source else '；历史来源缺失'))) + ' |')
    if not latest:
        lines += ['| 本窗口没有可用组合决定 | — | — | — | — | — | — |']
    lines += ['',
             f'本窗口留存研究策略评审 {len(data["research"].get("strategy_reviews", []))} 份；'
             '详见前两份文档开头，逐项区分事实澄清、认可但待实施、已实施及其原因。', '',
             '## 可用范围与限制', '']
    lines += ['- ' + text for text in c['limitations']]
    from .strategy_report_audit import render_summary
    lines += [''] + render_summary(data)
    lines += ['', '## 研究版本数量', '']
    lines += [f'- {TABLE_LABELS[t]}：{n}' for t, n in c['research_counts'].items()]
    if c['missing_tables']:
        lines += ['', '未保留的数据表：' + '、'.join(_safe_text(t) for t in c['missing_tables'])]
    lines += ['', '## 证据缺口', '']
    lines += _details(data['issues']) if data['issues'] else ['未发现本次解析或关联缺口；这不证明历史收集从未中断。']
    return '\n'.join(lines) + '\n'


def _research(data):
    lines = ['# 研究策略', '', '依照研究形成时间列出本窗口各次可用输出。窗口外的关联研究另作标记。',
             'WATCH 表示资料足以观察，不直接授权下单；原记录状态不等于现在仍有效。', '']
    lines += render_records(data['research'].get('strategy_reviews', []))
    lines += _stock_reports(data)
    lines += ['## 原始研究记录（完整追溯）', '',
              '以下保留原有逐表、逐版本记录；上方方案卡仅重排已留存字段，不替代原始记录。', '']
    for table, entries in data['research'].items():
        if table == 'strategy_reviews':
            continue
        lines += ['## ' + TABLE_LABELS[table], '']
        if not entries:
            lines += ['本窗口没有可用记录；不能据此认定未发生研究。', '']
        for item in entries:
            record = item['record']
            clock = record.get('activated_at', record.get('created_at', record.get('replaced_at', record.get('adopted_at', record.get('published_at')))))
            title = record.get('symbol') or record.get('theme') or record.get('title') or record['id']
            lines += [f'### {_local(clock)} · {_safe_text(title)}', '', '记录：' + _safe_text(record['id']), '']
            if item.get('outside_window_linked'):
                lines += ['此记录早于窗口，因被窗口内策略或前置基线明确引用而保留。', '']
            if item.get('current_state_baseline'):
                lines += ['当前适用基准：这是导出时仍适用的指导，不据此推断它在每个历史时点均已适用。', '']
            if item.get('metadata_at_capture'):
                lines += ['新闻引用元数据为导出时查询，不宣称在原决策时完整冻结。', '']
            readable = {k: _payload(record, k) if k.endswith('_json') else v for k, v in record.items()}
            lines += _details(readable) + ['']
    return '\n'.join(lines) + '\n'


def _trading(data):
    versions = {d['record']['id']: d for d in
                [data.get('baseline'), data.get('published_baseline'), *data['decisions']] if d}

    def resolve(ref):
        if not ref:
            return None
        version = versions.get(ref['decision_id'], {})
        return next((item for item in version.get('payload', {}).get('decisions', [])
                     if item.get('key') == ref['key']), None)

    lines = ['# 研究策略评审与交易策略变更', '',
             '本文件先列研究策略评审与采纳状态，再列模拟交易决定的变化及当轮依据。', '']
    lines += render_records(data['research'].get('strategy_reviews', []))
    lines += ['## 交易策略变化', '', '生成记录与已确认发布记录分别比较。窗口首项使用窗口前基线；缺少基线时只记首次观察。',
             '原始理由不改写；文字与数字不一致时，实际授权动作和仓位以该版本结构化 action、target_bps、current_bps 为准。HOLD/PAUSE目标跟随当前占比，不自动代表主动仓位选择；新版模型原动作及发布重设记录按组合payload.publication_audit_ref在策略数据.json的coverage.publication_audits中查阅。',
             '每次保留原始组合总结与每个项目的理由；账面占比变化不自动解释为主动调仓或行情波动。',
             '“当轮理由”是当时记录的决策依据。系统没有单独记录明确调整原因时，不从前后差异推断新的原因。', '',
             '## 每日变化概览', '',
             '下列按已确认发布版本统计；同一版本可以同时有动作调整、理由更新和来源更新。', '',
             '| 北京日期 | 生成版本 | 确认发布 | 动作或主动目标变化 | 理由或总结更新 | 仅续签 |',
             '| --- | ---: | ---: | ---: | ---: | ---: |']
    daily = {}
    for decision in data['decisions']:
        day = _local(decision['record']['created_at'])[:10]
        counts = daily.setdefault(day, [0, 0, 0, 0, 0])
        counts[0] += 1
        change = decision.get('acknowledged_change')
        if change is not None:
            counts[1] += 1
            counts[2] += 'action_target_change' in change['categories']
            counts[3] += 'reason_update' in change['categories']
            counts[4] += change['categories'] == ['renewal_only']
    for day, counts in sorted(daily.items()):
        lines.append('| ' + day + ' | ' + ' | '.join(map(str, counts)) + ' |')
    lines += ['', '## 逐次决策与依据', '']
    for title, key in (('生成路线前置基线', 'baseline'), ('已确认发布路线前置基线', 'published_baseline')):
        baseline = data[key]
        lines += [f'{title}：' + (f'{_local(baseline["record"]["created_at"])} / {_safe_text(baseline["record"]["id"])}' if baseline else '无可用基线。'), '']
    if not data['decisions']:
        lines += ['本窗口没有可用组合决定。', '']
    for decision in data['decisions']:
        record, payload = decision['record'], decision['payload']
        lines += [f'## {_local(record["created_at"])} · {_safe_text(record["id"])}', '',
                  '发布状态：' + _safe_text(decision['publication']['state']) + '。' + decision['publication']['notice'],
                  '有效期至：' + _local(record.get('valid_until')) + '。', '',
                  '生成路线变化：' + '、'.join(CHANGE_LABELS[c] for c in decision['generated_change']['categories']) + '。']
        if decision['acknowledged_change'] is not None:
            lines += ['已确认发布路线变化：' + '、'.join(CHANGE_LABELS[c] for c in decision['acknowledged_change']['categories']) + '。']
        lines += ['', '原始组合总结：', '', _safe_text(payload.get('summary', '原总结不可用。')), '']
        for item in payload.get('decisions', []):
            name = item.get('name') or item.get('symbol') or item.get('key')
            lines += [f'### {_safe_text(name)}（{_safe_text(item.get("symbol"))}） · {_safe_text(ROUTE_LABELS.get(item.get("route"), item.get("route")))}', '',
                      f'动作：{_safe_text(ACTION_LABELS.get(item.get("action"), item.get("action")))}；当前占比 {_weight(item.get("current_bps"))}；目标占比 {_weight(item.get("target_bps"))}。', '',
                      '当轮记录理由（原始理由）：' + _safe_text(item.get('reason')), '',
                      '来源：' + _safe_text(item.get('source_id')) + '；冻结来源引用：' + _safe_text(decision['source_refs'].get(item.get('key'), '缺失，未用当前来源补写历史')) + '。',
                      '研究与程序规则：在“研究策略.md”的逐标的方案中按代码、来源编号及冻结引用核对。', '']
            from .strategy_condition_report import render_item as render_conditions
            lines += render_conditions(decision, item, data, compact=True)
        comparisons = [('相对上一生成版本的变化', decision['generated_change'])]
        published = decision['acknowledged_change']
        if published is not None and 'items_ref' not in published:
            comparisons.append(('相对上一已确认发布版本的变化', published))
        elif published is not None:
            lines += ['已确认发布路线与本次生成路线使用相同前置版本，逐项变化相同。', '']
        for comparison_title, change in comparisons:
            lines += ['### ' + comparison_title, '',
                      '| 标的 / 线路 | 变化类型 | 上轮动作 / 目标 | 本轮动作 / 目标 | 当轮记录理由 |',
                      '| --- | --- | --- | --- | --- |']
            for delta in change['items']:
                before, after = resolve(delta.get('before_ref')), resolve(delta.get('after_ref'))
                item = after or before or {}
                name = (item.get('name') or item.get('symbol') or delta['key']) + ' / ' + ROUTE_LABELS.get(item.get('route'), item.get('route', ''))
                kinds = '、'.join(CHANGE_LABELS[k] for k in delta['categories'])
                reason = (after or {}).get('reason', '本轮未列入；没有单独记录移出原因。')
                lines.append('| ' + ' | '.join((_cell(name), _cell(kinds), _action(before),
                                               _action(after), _cell(reason))) + ' |')
            if not change['items']:
                lines.append('| 全部标的 | ' + _cell('、'.join(CHANGE_LABELS[c] for c in change['categories'])) +
                             ' | — | — | 逐项动作及目标未发现变化，或因缺少基准无法比较；以本轮说明为准。 |')
            lines += ['']
        if payload.get('risk_groups'):
            lines += ['### 原始风险分组', ''] + _details(payload['risk_groups']) + ['']
    lines += ['## 来源更新与冻结研究', '',
              f'记录 {len(data["source_events"])} 次单来源更新，冻结来源投影去重后 {len(data["frozen_sources"])} 份。',
              '逐条事件、原始来源投影及每次组合引用的映射见策略数据.json；单来源更新不冒充完整组合。', '']
    return '\n'.join(lines) + '\n'


def build_archive(data_dir, at=None):
    """Return (ZIP bytes, ASCII filename), without writing any application state."""
    root = Path(data_dir).expanduser().resolve()
    database = root if root.is_file() else root / 'agent.sqlite3'
    db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=20)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        data = collect(db, at)
        db.rollback()
    finally:
        db.close()
    data['research']['strategy_reviews'] = load_records(database.parent, data['start_at'], data['end_at'])
    from .strategy_report_audit import export_projection
    rendered = (_overview(data), _research(data), _trading(data), _json(export_projection(data)) + '\n')
    encoded = [value.encode('utf-8') for value in rendered]
    if sum(map(len, encoded)) > MAX_UNCOMPRESSED:
        raise ExportTooLarge('完整策略导出解压后超过96MB，未截断任何记录')
    result = io.BytesIO()
    date = _time(data['generated_at']).astimezone(ZoneInfo(TIMEZONE))
    zip_date = (max(1980, min(2107, date.year)), date.month, date.day, date.hour, date.minute, date.second)
    with zipfile.ZipFile(result, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in zip(ENTRIES, encoded):
            info = zipfile.ZipInfo(name, zip_date)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, content, compresslevel=9)
    value = result.getvalue()
    if len(value) > MAX_ARCHIVE:
        raise ExportTooLarge('完整策略导出压缩后超过14.5MB，未截断任何记录')
    start_day = _time(data['start_at']).astimezone(ZoneInfo(TIMEZONE)).strftime('%Y%m%d')
    return value, 'thesistrade-strategies-' + start_day + '-' + date.strftime('%Y%m%d') + '.zip'
