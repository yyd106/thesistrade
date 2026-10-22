"""Bounded, read-only strategy history downloads, with no app initialization.

Only allowlisted database fields are read. The archive contains fixed filenames,
never source files, model inputs, credentials or configuration. A generated
decision is not an accepted publication; both timelines retain their own baseline.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import zipfile
from zoneinfo import ZoneInfo

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
    'portfolio_decisions': set('summary decisions risk_groups as_of account build model'.split()),
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

    def raw(self, table, where='', args=(), *, order='rowid', limit=None):
        if table not in self.tables:
            return []
        columns = {r[1] for r in self.db.execute('PRAGMA table_info("' + table + '")')}
        fields = [c for c in COLUMNS[table].split() if c in columns]
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
    return {'schema': SCHEMA, 'generated_at': end.isoformat(), 'start_at': start.isoformat(), 'end_at': end.isoformat(),
            'timezone': TIMEZONE, 'scope': '近14个北京时间自然日，含今天，截止生成时刻；额外保留窗口前基线及明确关联研究。',
            'coverage': coverage, 'baseline': baseline, 'published_baseline': published_baseline, 'decisions': timeline,
            'published_decision_ids': accepted_ids, 'research': research, 'frozen_sources': frozen_sources,
            'source_events': source_events, 'issues': reader.issues}


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


def _overview(data):
    c = data['coverage']
    lines = ['# 近14天研究与交易策略', '',
             f'北京时间 {_local(data["start_at"])} 至 {_local(data["end_at"])}。', '',
             f'生成组合 {c["generated_decisions"]} 次，其中可确认发布 {c["acknowledged_decisions"]} 次；'
             f'具备冻结来源 {c["frozen_decisions"]} 次。', '',
             '[研究策略](研究策略.md) · [交易策略变更](交易策略变更.md) · [结构化完整投影](策略数据.json)', '',
             '## 可用范围与限制', '']
    lines += ['- ' + text for text in c['limitations']]
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
    for table, entries in data['research'].items():
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

    lines = ['# 交易策略变更', '', '生成记录与已确认发布记录分别比较。窗口首项使用窗口前基线；缺少基线时只记首次观察。',
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
            lines += [f'### {_safe_text(name)} · {_safe_text(ROUTE_LABELS.get(item.get("route"), item.get("route")))}', '',
                      f'动作：{_safe_text(ACTION_LABELS.get(item.get("action"), item.get("action")))}；当前占比 {_weight(item.get("current_bps"))}；目标占比 {_weight(item.get("target_bps"))}。', '',
                      '当轮记录理由（原始理由）：' + _safe_text(item.get('reason')), '',
                      '来源：' + _safe_text(item.get('source_id')) + '；冻结来源引用：' + _safe_text(decision['source_refs'].get(item.get('key'), '缺失，未用当前来源补写历史')) + '。', '']
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
    rendered = (_overview(data), _research(data), _trading(data), _json(data) + '\n')
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
