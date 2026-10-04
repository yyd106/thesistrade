"""Independent signed review/news display; never a strategy or ledger input."""
import copy
import math
import re
from functools import lru_cache
from datetime import datetime, timedelta

from .storage import now, normalize_time, digest
from .cloud_protocol import canonical
from .cloud_runtime import value, put

FEATURE = 'review_news_display_v2'
VERSION = 'review-news-display-v2'
LEGACY_VERSION = 'review-news-display-v1'
REVIEW_BYTES = 1_000_000
MACRO_BYTES = 8_000_000
EVENT_LIMIT = 60
STATE = 'display_review_news'
FORBIDDEN = {'body', 'raw', 'raw_path', 'prompt', 'input_json', 'packet_json', 'payload_json',
             'model_input', 'model_output', 'credentials', 'private_key', 'token', 'config'}
TEXT_LIMIT = 1800
SHORT_NOTICE = '…（页面摘要已截短，完整记录保留在本机）'


@lru_cache(maxsize=1)
def public_fields():
    # Only explicit presentation fields plus the already-validated model schemas
    # are transferable. Adding a new producer field does not publish it by default.
    from .macro import SCHEMA as macro_schema
    from .macro_impact import SCHEMA as impact_schema
    from .news_triage import SCHEMA as screening_schema
    from .review import REVIEW_SCHEMA
    fields = set('''id symbol origin at role version scope window_start window_end positions opening closing totals
      valuation_notice research portfolio statistics daily_accounting context_48h learning_notice analysis analysis_error
      consistency_checks routing presentation history revision ready_at model_status automatic_retries_remaining payload
      checks counts total omitted items current ordinal lesson applicability category to status updated_at expires_at expired
      findings notice daily context status_at data_as_of shown check checked failures missing examples route fill_id last_sync
      pending failed free_gb db_gb backups_gb free_bytes database_bytes warning phase position detail name qty qty_scale cost_cents average_cost_cents
      market_value_cents unrealized_cents cumulative_realized_cents price_cents price_micros currency fx_micros fx_at quote_at
      quality late_quote quote_first_seen_at cash_cents equity_cents valuation_complete key period_realized_cents
      period_profit_cents cumulative_profit_cents period_fee_cents period_fill_count entry_research_ids research_ids research_gap
      fill_ids holding_count reviewed_position_count dividend_cents dividend_count dividends_known plan thesis decision trigger
      position_key verdict reason supported_points contradicted_points pending_points next_check validation_warnings
      decision_count recording_policy recording_notice blocked_count fill_count fee_cents realized_pnl_cents win_rate
      cashflow_adjusted_change_between_marks_cents mark_times actions created_at basis source title url published_at first_seen_at
      catalyst_at lifecycle screening reactions related_reports citations revisions theme_label materiality assessment admitted
      assessed_at measurement_gap measurement_proxy measurement_basis sources state label review_due_at actionable next_step
      observation diagnostic_only waiting changed before_headline after_headline before_directions after_directions source_replacements
      source_replacement_total source_status replaced_at event_id archived_items followup_items history_items library
      window_hours assets markets news_counts article_coverage impact_learning news_screening source_coverage event_count
      observation_counts execution extracted note assessed admitted_pairs background measured_outcomes forward_samples
      calibrated_cohorts required_samples method recent latest date value series unit change_unit value_unit group category kind
      identity_source checked_at error available configured gaps coverage family max_age_hours latest_at oldest_at valid_count
      content_available_at content_basis window_count
      fetched inserted range_start range_end duration_seconds baseline end change count representative_total followup_total
      history_total archive_total followup_loaded history_loaded current_total current_loaded as_of limit page_size n
      material_count probability interval sample_count required_count train validation median_absolute_change material_z_threshold
      sample_ids limitation delivery event_limit max_bytes shortened_fields'''.split())
    def add(schema):
        if not isinstance(schema,dict):return
        fields.update(schema.get('properties',{}))
        for child in schema.get('properties',{}).values():add(child)
        add(schema.get('items'))
    for schema in (macro_schema,impact_schema,screening_schema,REVIEW_SCHEMA):add(schema)
    return fields


def clipped(text):
    return text if len(text)<=TEXT_LIMIT else text[:TEXT_LIMIT-len(SHORT_NOTICE)]+SHORT_NOTICE


def bounded(value, changes, *, depth=0, parent=None):
    """Bound already-presented summaries; never follow a path or load a source."""
    if depth > 16:
        changes[0] += 1
        return None
    if isinstance(value, str):
        if len(value) > TEXT_LIMIT:
            changes[0] += 1
            return clipped(value)
        return value
    if isinstance(value, list):
        if len(value) > 100:
            changes[0] += len(value)-100
        return [bounded(v, changes, depth=depth+1,parent=parent) for v in value[:100]]
    if isinstance(value, dict):
        result={}
        for key,item in value.items():
            if not isinstance(key,str) or key in FORBIDDEN:continue
            allowed=key in public_fields()
            if parent in ('counts','news_counts','observation_counts','actions'):
                allowed=bool(re.fullmatch(r'[A-Z_]{1,50}',key)) and type(item) is int
            elif parent=='assets':
                allowed=bool(re.fullmatch(r'(?:[A-Z][A-Z0-9:._-]{0,30}|(?:sh|sz)[0-9]{6})',key)) and isinstance(item,dict)
            if allowed:result[key]=bounded(item,changes,depth=depth+1,parent=key)
        return result
    if value is None or type(value) in (bool, int):
        return value
    return value if type(value) is float and math.isfinite(value) else None


def review_packet(store, config, at):
    from .review_presentation import listing
    result, changes = [], [0]
    for row in listing(store, config, at, overview=True):
        payload = row['payload']
        facts = payload.get('facts') or {}
        # The existing overview is already factual/analytical summaries. Strip
        # complete research payloads, retaining only the text used by the page.
        portfolio = copy.deepcopy(facts.get('portfolio'))
        if portfolio:
            portfolio['research'] = [{k: r.get(k) for k in ('id','symbol','origin','at','role')} | {
                'analysis': {k: (r.get('analysis') or {}).get(k) for k in ('analysis','mechanism')} | {
                    'decision': {k: ((r.get('analysis') or {}).get('decision') or {}).get(k) for k in ('trigger','invalidation')}},
                'plan': {'thesis': (r.get('plan') or {}).get('thesis')}} for r in portfolio.get('research', [])]
        public_facts = {k: facts[k] for k in ('statistics','daily_accounting','context_48h') if k in facts}
        if portfolio:
            public_facts['portfolio'] = portfolio
        item = {k: row[k] for k in ('id','window_start','window_end','revision','ready_at','model_status','automatic_retries_remaining','presentation','history') if k in row}
        item['payload'] = {'facts': public_facts, **{k: payload.get(k) for k in ('analysis','analysis_error','consistency_checks','routing')}}
        result.append(bounded(item, changes))
    total = result[0].get('history', {}).get('total', len(result)) if result else 0
    while len(canonical(result)) > REVIEW_BYTES and len(result) > 1:
        result.pop()
        changes[0] += 1
    if result:
        result[0]['history'] = {'total': total, 'shown': len(result), 'omitted': max(0,total-len(result))}
        if changes[0]:
            analysis = result[0]['payload'].setdefault('analysis', {})
            note='\n【页面展示说明，非模型结论】部分长文或较早记录已按摘要预算缩短；完整记录保留在本机。'
            analysis['summary'] = (analysis.get('summary') or '')[:TEXT_LIMIT-len(note)]+note
    if len(canonical(result)) > REVIEW_BYTES:
        raise ValueError('最新复盘摘要仍超过独立展示预算，保留云端上次成功摘要')
    return result


def macro_packet(store, config, at):
    from .macro import view
    original = view(store, at, config)
    keys = ('items','archived_items','followup_items','history_items','library','window_hours','window_start','window_end',
            'assets','markets','news_counts','article_coverage','impact_learning','news_screening','source_coverage',
            'event_count','observation_counts','sources','scope','execution')
    def prominent(event):
        # Exactly matches macroProminent in app.js, including legacy entries.
        if 'screening' not in event:return True
        impacts=(event.get('analysis') or {}).get('impacts') or []
        if any((i.get('materiality') or {}).get('admitted') for i in impacts):return True
        return (event.get('screening') or {}).get('decision')=='DEEP' and not all((i.get('materiality') or {}).get('state')=='BACKGROUND' for i in impacts)
    changes = [0]
    selected={k: original[k] for k in keys if k in original}
    selected['items']=sorted(original.get('items',[]),key=lambda event:not prominent(event))
    result = bounded(selected, changes)
    removed = {}
    for key in ('items','archived_items','followup_items','history_items'):
        rows = result.get(key, [])
        removed[key] = max(0, len(original.get(key, []))-EVENT_LIMIT)
        result[key] = rows[:EVENT_LIMIT]
    # Old archived_items is only a compatibility list. The two purpose-specific
    # lists remain authoritative and expose their complete totals in library.
    while len(canonical(result)) > MACRO_BYTES:
        key = next((k for k in ('archived_items','history_items','followup_items','items') if result.get(k)), None)
        if key is None:
            raise ValueError('新闻摘要超过独立展示预算，保留云端上次成功摘要')
        result[key].pop()
        removed[key] += 1
    library = result.setdefault('library', {})
    library['followup_loaded'] = len(result['followup_items'])
    library['history_loaded'] = len(result['history_items'])
    library['current_total'] = sum(prominent(event) for event in original.get('items',[]))
    library['current_loaded'] = sum(prominent(event) for event in result['items'])
    result['delivery'] = {'version': VERSION, 'event_limit': EVENT_LIMIT, 'max_bytes': MACRO_BYTES,
                          'omitted': removed, 'shortened_fields': changes[0]}
    if any(removed.values()) or changes[0]:
        note=' 页面按摘要预算展示；长文已缩短，未载入事项计数保留在对应列表，完整记录可在本机追溯。'
        result['execution'] = (result.get('execution') or '')[:TEXT_LIMIT-len(note)]+note
    if len(canonical(result)) > MACRO_BYTES:
        raise ValueError('新闻摘要及说明超过独立展示预算')
    return result


def content_hash(packet):
    # A minute's clock tick alone is not new evidence. Lifecycle changes, new
    # observations, revised sources and routed issue status remain in the hash.
    data = copy.deepcopy({k: packet[k] for k in ('reviews','macro')})
    for row in data['reviews']:
        (row.get('presentation') or {}).pop('status_at', None)
    data['macro'].pop('window_start', None)
    data['macro'].pop('window_end', None)
    (data['macro'].get('library') or {}).pop('as_of', None)
    return digest(canonical(data))


def collect(store, config, at=None):
    at = normalize_time(at or now())
    packet = {'version': VERSION, 'generated_at': at, 'reviews': review_packet(store, config, at),
              'macro': macro_packet(store, config, at)}
    packet['content_hash'] = content_hash(packet)
    return packet


def receive(store, packet, at):
    required = {'version','generated_at','reviews','macro','content_hash'}
    if not isinstance(packet, dict) or set(packet) != required or packet['version'] not in (VERSION, LEGACY_VERSION):
        raise ValueError('复盘与新闻独立展示摘要版本或字段无效')
    stamp = normalize_time(packet['generated_at'])
    if datetime.fromisoformat(stamp) > datetime.fromisoformat(normalize_time(at)) + timedelta(minutes=5):
        raise ValueError('展示摘要时间超前')
    if not isinstance(packet['reviews'], list) or len(packet['reviews']) > 5 or len(canonical(packet['reviews'])) > REVIEW_BYTES:
        raise ValueError('复盘展示摘要格式或大小无效')
    macro = packet['macro']
    if not isinstance(macro, dict) or len(canonical(macro)) > MACRO_BYTES:
        raise ValueError('新闻展示摘要格式或大小无效')
    for key in ('items','archived_items','followup_items','history_items'):
        if not isinstance(macro.get(key), list) or len(macro[key]) > EVENT_LIMIT:
            raise ValueError('新闻展示列表大小无效')
    if bounded(packet['reviews'],[0])!=packet['reviews'] or bounded(macro,[0])!=macro:
        raise ValueError('展示摘要包含未授权字段或超限内容')
    def contains_diagnostic(value):
        if isinstance(value, dict):
            return 'diagnostic_only' in value or any(contains_diagnostic(v) for v in value.values())
        return isinstance(value, list) and any(contains_diagnostic(v) for v in value)
    if packet['version'] == LEGACY_VERSION and (contains_diagnostic(packet['reviews']) or contains_diagnostic(macro)):
        raise ValueError('页面辅助计算标记需要新版展示摘要协议')
    if packet['content_hash'] != content_hash(packet):
        raise ValueError('展示摘要内容哈希不符')
    prior = value(store, STATE)
    status = 'UPDATED'
    if prior and stamp < prior['generated_at']:
        status = 'IGNORED_STALE'
    elif prior and stamp == prior['generated_at']:
        if packet['content_hash'] != prior['content_hash']:
            raise ValueError('同一生成时间的展示内容发生冲突')
        status = 'UNCHANGED'
    else:
        put(store, STATE, {**packet, 'generated_at': stamp})
    latest = value(store, STATE)
    return {'status': 'ACCEPTED', 'page_display': {'status': status,
            'generated_at': latest['generated_at'], 'content_hash': latest['content_hash']}}


def apply(store, cache):
    """Overlay independent summaries only when at least as recent as old paths."""
    packet = value(store, STATE)
    if not packet:
        return cache
    at = packet['generated_at']
    def valid(stamp):
        try:
            return normalize_time(stamp) if isinstance(stamp,str) else ''
        except (ValueError,TypeError):
            return ''
    # Daily reviews and strategy publications predate this independent channel.
    # Their delivery order is not their generation order.
    legacy = value(store, 'display_reviews')
    def review_time(rows):
        return max((valid((r.get('presentation') or {}).get('status_at') or r.get('ready_at')) for r in rows if isinstance(r,dict)), default='')
    if isinstance(legacy,list) and review_time(legacy)>review_time(cache.get('reviews', [])):
        cache['reviews']=legacy
    review_at = review_time(cache.get('reviews', []))
    if at >= review_at:
        cache['reviews'] = copy.deepcopy(packet['reviews'])
    current_macro = (cache.get('dynamic') or {}).get('global') or {}
    macro_at = valid((current_macro.get('library') or {}).get('as_of') or current_macro.get('window_end'))
    if at >= macro_at:
        cache.setdefault('dynamic', {})['global'] = copy.deepcopy(packet['macro'])
    cache['page_display_sync'] = {'generated_at': at, 'content_hash': packet['content_hash']}
    return cache
