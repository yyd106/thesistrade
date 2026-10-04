"""Read-only event lifecycle and bounded news-page views.

These states describe the existing observation policy. They do not update research,
watch membership, approvals, source records, or measured outcomes.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta
from .observation_pool import policy
from .storage import normalize_time

LIBRARY_LIMIT = 40
REVISION_LIMIT = 3


def lifecycle(event, at, config=None, assessments=()):
    rules = policy(config)
    anchor = event.get('catalyst_at') or event['published_at']
    long = event.get('horizon') == 'MONTHS'
    review_hours = rules['long_review_days'] * 24 if long else rules['cooldown_hours']
    expiry_days = rules['long_archive_days'] if long else rules['archive_days']
    due = normalize_time((datetime.fromisoformat(anchor) + timedelta(hours=review_hours)).isoformat())
    end = normalize_time((datetime.fromisoformat(anchor) + timedelta(days=expiry_days)).isoformat())
    states = [a.get('state', 'PENDING') for a in assessments]
    if event['status'] == 'INVALIDATED':
        state, label, reason = 'INVALIDATED', '原判断已失效', '引用资料已修订，旧分析仅供回看。'
    elif at >= end:
        state, label, reason = 'ENDED', '观察已结束', '既有观察期限已结束，不再列作当前待跟进事项；这不等于判断成功或失败。'
    elif states and all(s == 'BACKGROUND' for s in states):
        state, label, reason = 'BACKGROUND', '背景资料', '现有评估未识别足够新增影响，保留来源和研究记录。'
    elif at >= due:
        state, label, reason = 'REVIEW_DUE', '已到复核期', '当前依据已到既有复核期限，需要新证据才能继续支持原判断。'
    elif not states or all(s == 'PENDING' for s in states):
        state, label, reason = 'PENDING_REVIEW', '待影响评估', '研究已形成，独立影响评估尚未完成。'
    elif any(a.get('admitted') for a in assessments):
        state, label, reason = 'TRACKING', '跟踪观察', '影响评估已通过，仍需观察成立条件与反证。'
    else:
        state, label, reason = 'WAITING_EVIDENCE', '待补关键证据', '现有证据尚未满足影响评估条件。'
    return {'state': state, 'label': label, 'reason': reason, 'catalyst_at': anchor,
            'review_due_at': due, 'expires_at': end,
            'actionable': state not in ('INVALIDATED', 'ENDED', 'BACKGROUND')}


def select_rows(store, at, start, config, assessments, screened):
    # One representative for both current and historical views. A representative
    # that did not exist at the requested time cannot hide an older event.
    rows = store.db.execute('''SELECT e.id,e.news_id,e.created_at,e.basis,e.theme,e.status,
        json_extract(e.payload_json,'$.horizon') AS horizon,n.source,n.title,n.url,n.published_at,
        coalesce((SELECT min(cn.published_at) FROM macro_news_queue cq
          JOIN dynamic_news cn ON cn.id=cq.news_id WHERE cq.representative_id=e.news_id
          AND cn.status!='REVISED' AND cn.published_at<=? AND cn.first_seen_at<=?),n.published_at) AS catalyst_at
        FROM macro_events e JOIN dynamic_news n ON n.id=e.news_id
        WHERE e.created_at<=? AND n.published_at<=? AND n.first_seen_at<=?
        AND NOT EXISTS (SELECT 1 FROM macro_news_queue q
          JOIN macro_events rep ON rep.news_id=q.representative_id
          JOIN dynamic_news rn ON rn.id=rep.news_id
          WHERE q.news_id=e.news_id AND q.representative_id!=e.news_id
          AND rep.status!='INVALIDATED' AND rep.created_at<=? AND rn.published_at<=? AND rn.first_seen_at<=?)
        ORDER BY n.published_at DESC,e.created_at DESC,e.id''', (at,) * 8).fetchall()
    by_event = {}
    for (eid, _), assessment in assessments.items():
        by_event.setdefault(eid, []).append(assessment)
    current, archive, followup, history = [], [], [], []
    for row in rows:
        event = dict(row)
        values = by_event.get(event['id'], [])
        event['lifecycle'] = lifecycle(event, at, config, values)
        screening = screened.get(event['news_id']) or {}
        prominent = any(a.get('admitted') for a in values) or (
            screening.get('decision') == 'DEEP' and not (values and all(a.get('state') == 'BACKGROUND' for a in values)))
        recent = event['published_at'] >= start and event['status'] != 'INVALIDATED'
        if recent:
            current.append(event)
        else:
            archive.append(event)
        if recent and prominent:
            continue
        (followup if event['lifecycle']['actionable'] else history).append(event)
    return {'items': current, 'archived_items': archive[:LIBRARY_LIMIT],
            'followup_items': followup[:LIBRARY_LIMIT], 'history_items': history[:LIBRARY_LIMIT],
            'library': {'limit': LIBRARY_LIMIT, 'page_size': 5, 'as_of': at,
                'followup_total': len(followup), 'history_total': len(history),
                'archive_total': len(archive), 'representative_total': len(rows),
                'followup_loaded': min(len(followup), LIBRARY_LIMIT),
                'history_loaded': min(len(history), LIBRARY_LIMIT)}}


def observation_waiting(spec, event, at):
    if not spec.get('series'):
        return '该市场历史序列尚未接入，仅保留定性研究，不会自动形成价格验证。'
    if event['status'] == 'INVALIDATED':
        return '原判断已失效，未完成的价格观察不再列为当前待办。'
    anchor = (event['created_at'] if event['basis'] == 'FORWARD' else event['published_at'])[:10]
    # The unchanged measurement rule only accepts a third observation within 14
    # days. After that date, distinguish a missing-data gap from a future window.
    if (datetime.fromisoformat(at[:10]) - datetime.fromisoformat(anchor)).days > 14:
        return '已超过可测量窗口，历史数据仍有缺口；缺失结果不计成功或失败。'
    if event.get('lifecycle', {}).get('state') == 'ENDED':
        return '研究观察期限已结束；价格记录仍待已接入来源补齐，不作为当前研究待办。'
    return '等待事件后数据发布并形成完整观察窗口。'


def diagnostic_observation(store, event, asset, at, markets):
    """Compute a missing page result without registering research evidence.

    Production macro.measure retains its original first-600-event scan. This
    separate read-only calculation covers only the bounded events being shown;
    it must never be inserted into macro_observations or sent to research.
    """
    from .macro_sources import ASSETS
    spec = ASSETS.get(asset, {})
    if not spec.get('series') or event['status'] == 'INVALIDATED':
        return None
    if event['created_at'] > at or event['published_at'] > at:
        return None
    if asset not in markets:
        row = store.db.execute("SELECT payload_json FROM macro_markets WHERE asset=? AND status='OK' AND checked_at<=?", (asset, at)).fetchone()
        markets[asset] = json.loads(row[0]) if row else None
    data = markets[asset]
    if not data:
        return None
    anchor = (event['created_at'] if event['basis'] == 'FORWARD' else event['published_at'])[:10]
    before = [point for point in data['points'] if point['date'] < anchor]
    after = [point for point in data['points'] if anchor < point['date'] < at[:10]]
    if not before or len(after) < 3:
        return None
    base, end = before[-1], after[2]
    if (datetime.fromisoformat(anchor) - datetime.fromisoformat(base['date'])).days > 7 or (datetime.fromisoformat(end['date']) - datetime.fromisoformat(anchor)).days > 14:
        return None
    unit = spec.get('change_unit', '%')
    if unit == '%' and base['value'] <= 0:
        return None
    change = (end['value'] - base['value']) * 100 if unit == 'bp' else (end['value'] / base['value'] - 1) * 100
    return {'baseline': base, 'end': end, 'change': round(change, 3), 'change_unit': unit,
            'value_unit': spec['unit'], 'url': data['url'], 'series': data['series'],
            'diagnostic_only': True,
            'method': '页面辅助计算，未登记为研究证据；事件/研究日前最近观测至之后第3个观测日；日级指标、发布有延迟，不是交易收益或因果验证；使用获取时的数据版本。'}


def next_step(event):
    life = event['lifecycle']
    if life['state'] == 'INVALIDATED':
        return '查看修订来源与后续研究；旧方向只供追溯。'
    if life['state'] == 'ENDED':
        observations = [r['observation'] for r in event.get('reactions', []) if r.get('observation')]
        measured = sum(not o.get('diagnostic_only') for o in observations)
        if observations and not measured:
            return '查看页面辅助计算的市场变化及数据缺口；这些结果未登记为研究证据。'
        return ('查看已记录的市场变化及数据缺口，供后续方案比较。' if measured else
                '保留原判断和数据缺口供回看；有新事实时按新事件研究。')
    if life['state'] == 'BACKGROUND':
        return '仅在出现新增冲击或新的可核验证据后重新评估。'
    gaps = []
    for impact in event['analysis'].get('impacts', []):
        assessment = (impact.get('materiality') or {}).get('assessment') or {}
        gap = assessment.get('missing_evidence') or impact.get('watch')
        if isinstance(gap, str) and gap.strip() and gap.strip() not in gaps:
            gaps.append(gap.strip())
    prefix = '等待独立影响评估；重点核实：' if life['state'] == 'PENDING_REVIEW' else '下一步核实：'
    return prefix + ('；'.join(gaps[:2])[:360] if gaps else '公开资料中的增量事实、影响规模和反证。')


def revisions(store, event, at):
    rows = store.db.execute('''SELECT replaced_at,previous_json FROM macro_event_revisions
        WHERE event_id=? AND replaced_at<=? ORDER BY replaced_at DESC,id DESC LIMIT ?''',
        (event['id'], at, REVISION_LIMIT)).fetchall()
    count = store.db.execute('SELECT count(*) FROM macro_event_revisions WHERE event_id=? AND replaced_at<=?', (event['id'], at)).fetchone()[0]
    current = json.loads(json.dumps(event['analysis']))
    for impact in current.get('impacts', []):
        impact.pop('materiality', None)
    fields = {'headline': '研究主题', 'facts': '事实摘要', 'transmission': '传导机制',
              'uncertainty': '不确定性', 'invalidation': '失效条件', 'horizon': '观察期限',
              'expectation_basis': '预期差依据', 'impacts': '标的、方向或推演', 'evidence': '引用依据'}
    result = []
    for row in rows:
        prior = json.loads(row['previous_json'])
        previous = json.loads(prior['payload_json'])
        def directions(analysis):
            return [{'asset': i['asset'], 'direction': i['direction']} for i in analysis.get('impacts', [])]
        result.append({'at': row['replaced_at'], 'changed': [label for key, label in fields.items() if previous.get(key) != current.get(key)],
                       'before_headline': previous.get('headline', ''), 'after_headline': current.get('headline', ''),
                       'before_directions': directions(previous), 'after_directions': directions(current)})
        current = previous
    evidence = sorted({event['news_id']} | {q['news_id'] for q in event['analysis'].get('evidence', [])})
    marks = ','.join('?' for _ in evidence)
    # Follow the complete source lineage, not just the first correction. UNION
    # deduplicates shared descendants and terminates even malformed cyclic data.
    rows = store.db.execute("""WITH RECURSIVE descendants(id) AS (
        SELECT id FROM dynamic_news WHERE revision_of IN (""" + marks + """)
          AND first_seen_at<=? AND published_at<=?
        UNION
        SELECT n.id FROM dynamic_news n JOIN descendants d ON n.revision_of=d.id
          WHERE n.first_seen_at<=? AND n.published_at<=?
      ) SELECT n.id,n.title,n.url,n.published_at,n.first_seen_at,n.status AS source_status,
        e.id AS event_id FROM descendants d JOIN dynamic_news n ON n.id=d.id
        LEFT JOIN macro_events e ON e.news_id=n.id AND e.created_at<=?
        WHERE n.id NOT IN (""" + marks + """)
        ORDER BY (n.status='REVISED'),n.first_seen_at DESC,n.id""", (*evidence, at, at, at, at, at, *evidence)).fetchall()
    replacements = [dict(row) for row in rows]
    return {'total': count, 'items': result, 'source_replacements': replacements[:REVISION_LIMIT],
            'source_replacement_total': len(replacements)}
