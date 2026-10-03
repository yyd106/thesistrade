"""Read-only, bounded proposal summaries for the CLI and signed dashboard display.

No lifecycle transitions, source-file reads, live statistics or model calls occur here.
"""
import json
from .storage import now, normalize_time
from .review_presentation import age_days

from .proposal_evidence import summary as evidence_summary
from .proposal_experiment import summary as experiment_summary

FIELDS = ('hypothesis', 'change', 'test_plan', 'failure_criteria', 'rollback')
MAX_CARDS = 20
CARD_BUDGET = 300_000
REVIEW_BUDGET = 550_000


def text(value, limit=1600):
    if not isinstance(value, str):
        return ''
    value = value.strip()
    return value if len(value) <= limit else value[:limit] + '…（摘要已截短；完整方案见本机提案）'


def size(value):
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode())


def _next_step(status, readiness, review, experiment=None):
    execution = (experiment or {}).get('execution')
    if status in ('DRAFT', 'READY') and execution:
        if execution['status'] == 'RUNNING':
            return '前向实验正在采集配对观察；等待两个观察窗口完成后复核结构指标，策略变更仍需用户批准。'
        if execution['status'] == 'COMPLETED':
            if execution.get('assessment') == 'NOT_SUPPORTED':
                return '分窗结果不支持候选；保留观察与失败记录，不能据此采纳该改动。'
            if execution.get('assessment') == 'INSUFFICIENT':
                return '实验观察已结束但证据不足；核对两个窗口的样本与失败记录，尚不能判断候选效果。'
            return '前向实验已完成，仅提供结构可检验性的描述统计；复核当前证据后再决定是否提交监督审查。'
        if execution['status'] == 'INCONCLUSIVE':
            return '前向实验已结束，尚不能判断；核对样本量、失败记录与停止原因后再提出下一份实验设计。'
    if status == 'DRAFT':
        return '补齐方案与证据后再提交监督审查。' if readiness['status'] != 'READY' else '草稿已具备审查材料；整理为待审方案后进入监督审查。'
    if status == 'APPROVED':
        return '已获用户批准；按批准范围实施、验证并记录上线。'
    if status == 'ADOPTED':
        return '已上线；继续按原检验方案与失败标准复核。'
    if status in ('REJECTED', 'RETIRED', 'SUPERSEDED'):
        return {'REJECTED': '已驳回，保留方案与证据供追溯。', 'RETIRED': '已撤下，保留历史记录。', 'SUPERSEDED': '已被新版提案替代；后续请查看新版。'}[status]
    if readiness['status'] != 'READY':
        return '审查材料不完整，需先补齐或核对引用。'
    if not review:
        return '等待独立监督审查；尚无审查结论。'
    if not review['current']:
        return '旧审查仅供追溯；等待针对当前材料的审查。'
    state = review['status']
    if state in ('PENDING', 'RUNNING'):
        return '等待本轮独立监督审查完成。'
    if state == 'DEFERRED':
        return '审查暂未完成，查看监督详情中的原因与重试安排。'
    if state == 'SUCCEEDED':
        return {'RECOMMEND': '监督建议通过，仍需 Dean 明确批准。',
                'INSUFFICIENT': '证据不足，继续积累或完善验证方案。',
                'REVISE': '按监督意见修订为新版提案后重新审查。',
                'REJECT': '监督建议驳回，由用户结合证据决定。'}.get(review.get('verdict'), '核对监督详情后再决定。')
    return '核对监督状态及当前材料后再决定。'


def card(store, row):
    """Summarize one stored proposal. The original payload is never modified."""
    from .supervision import listing

    row = dict(row)
    try:
        payload = json.loads(row['payload_json'])
        if not isinstance(payload, dict):
            raise ValueError('提案内容结构无效')
    except (ValueError, TypeError):
        payload = {}
    evidence = evidence_summary(store, row, payload)
    # Summary construction may keep extra snapshot metadata for hashing. The UI
    # receives only the public evidence fields, not internal hashes or raw payloads.
    public_evidence = {k: evidence.get(k) for k in
                       ('text', 'status', 'cohorts', 'references', 'additional_count', 'limitations')}
    public_evidence['text'] = text(evidence.get('text'), 4200)
    if payload.get('evidence') is None or (isinstance(payload.get('evidence'), str) and not payload['evidence'].strip()):
        public_evidence.update(status='MISSING', text='尚未整理证据摘要，请补充依据及检验方案。',
                               limitations=['草稿材料尚未补齐，不能据此判断方案效果。'])
    missing = [key for key in FIELDS if not isinstance(payload.get(key), str) or not payload[key].strip()]
    if evidence['status'] in ('MISSING', 'INVALID'):
        missing.append('evidence')
    try:
        experiment = experiment_summary(store, row['id'])
    except ValueError:
        experiment = {'id': '', 'status': 'INVALID'}
        missing.append('实验设计')
    readiness = {'status': 'INCOMPLETE' if missing else 'READY',
                 'reason': '需补齐或核对：' + '、'.join(missing) if missing else '方案字段完整；证据是否充分由监督与用户判断。'}
    reviews = listing(store, limit=1, subject_id=row['id'])
    review = None
    if reviews:
        r = reviews[0]
        review = {k: r.get(k) for k in ('id', 'status', 'verdict', 'summary')}
        review['summary'] = text(review['summary'], 1200)
        review['current'] = r['status'] != 'STALE'
    applicability = payload.get('applicability')
    applicability = applicability if isinstance(applicability, dict) else {}
    counterpoints = payload.get('counter_explanations')
    counterpoints = counterpoints if isinstance(counterpoints, list) else []
    return {**{key: row[key] for key in ('id', 'kind', 'status', 'created_at')},
            'title': text(row.get('title'), 200), **{key: text(payload.get(key)) for key in FIELDS},
            'applicability': {key: text(applicability.get(key), 120) for key in ('route', 'build_id', 'environment')},
            'counter_explanations': [text(v, 400) for v in counterpoints[:5] if isinstance(v, str)],
            'evidence': public_evidence, 'experiment': experiment, 'supervision': review,
            'readiness': readiness, 'next_step': _next_step(row['status'], readiness, review, experiment)}


def cards(store, at=None):
    # Scalar selection across the full index keeps old, actionable work visible.
    # Repeated narrative observations/last_seen_at are not new validation evidence.
    at = normalize_time(at or now())
    descriptors = [dict(row) for row in store.db.execute('''SELECT p.id,p.status,p.source,p.created_at,
        max(p.created_at,coalesce(p.decided_at,''),coalesce(d.created_at,''),coalesce(e.at,''),
            coalesce((SELECT max(created_at) FROM selfcheck_evidence WHERE proposal_id=p.id),'')) progress_at,
        e.status experiment_status FROM strategy_proposals p
        LEFT JOIN experiment_designs d ON d.proposal_id=p.id
        LEFT JOIN experiment_events e ON e.id=(SELECT max(id) FROM experiment_events WHERE experiment_id=d.id)''')]
    for row in descriptors:
        live = row['experiment_status'] == 'RUNNING' and row['status'] in ('DRAFT', 'READY')
        old = (age_days(row['progress_at'], at) or 0) > 14
        history = row['status'] in ('REJECTED', 'RETIRED', 'SUPERSEDED') or (old and row['status'] in ('DRAFT', 'ADOPTED') and not live)
        row.update(display_bucket='HISTORY' if history else 'CURRENT', active_experiment=live,
                   display_priority=0 if live else {'APPROVED': 1, 'READY': 2, 'ADOPTED': 3, 'DRAFT': 4}.get(row['status'], 5))
    descriptors.sort(key=lambda r: r['id'])
    descriptors.sort(key=lambda r: r['progress_at'], reverse=True)
    descriptors.sort(key=lambda r: (r['display_bucket'] == 'HISTORY', r['display_priority']))
    total = len(descriptors)
    items = []
    for meta in descriptors[:MAX_CARDS]:
        row = store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (meta['id'],)).fetchone()
        item = card(store, row)
        item.update(display_bucket=meta['display_bucket'], progress_at=meta['progress_at'],
                    active_experiment=meta['active_experiment'],
                    observation_only=meta['source']=='review' and item.get('readiness', {}).get('status')=='INCOMPLETE')
        if meta['display_bucket']=='HISTORY':
            item['display_reason'] = '超过14日没有新的验证证据或状态进展；原记录和状态保留。' if meta['status'] in ('DRAFT', 'ADOPTED') else '该提案已关闭或被新版替代，仅供追溯。'
        if size(items + [item]) > CARD_BUDGET:
            break
        items.append(item)
    result = {'items': items, 'total': total, 'shown': len(items)}
    if total:
        result.update(current_total=sum(r['display_bucket']=='CURRENT' for r in descriptors),
                      history_total=sum(r['display_bucket']=='HISTORY' for r in descriptors), omitted=total-len(items),
                      selection_notice='优先展示运行中的实验、已批准待上线和待决定提案。草稿或已采纳方案超过14日无新验证证据/状态进展移至历史展示；重复复盘叙述不刷新期限，原记录不变。')
    return result


def _review_selection(store, supplied, at):
    from .supervision import listing
    rows = store.db.execute('''SELECT r.id,r.subject_id,r.status,r.created_at,r.finished_at,p.status proposal_status
        FROM supervision_reviews r LEFT JOIN strategy_proposals p ON p.id=r.subject_id
        ORDER BY r.created_at DESC,r.rowid DESC''').fetchall()
    if not rows:
        return supplied, len(supplied)
    selected = {}
    for row in rows:
        selected.setdefault(row['subject_id'], dict(row))
    def priority(row):
        closed = row['status']=='STALE' or row['proposal_status'] in ('REJECTED','RETIRED','SUPERSEDED')
        return (closed, {'RUNNING':0,'DEFERRED':1,'PENDING':2,'SUCCEEDED':3}.get(row['status'],4))
    candidates = sorted(selected.values(), key=priority)
    public = []
    current_count = history_count = 0
    for row in candidates:
        definitely_history = row['status']=='STALE' or row['proposal_status'] in ('REJECTED','RETIRED','SUPERSEDED')
        definitely_history = definitely_history or (row['status']=='SUCCEEDED' and (age_days(row['finished_at'] or row['created_at'],at) or 0)>14)
        if definitely_history and history_count >= 10:
            continue  # Old completed records do not require repeated hash reads.
        values = listing(store, limit=1, subject_id=row['subject_id'])
        if not values:
            continue
        item = values[0]
        history = item['status']=='STALE' or item.get('proposal_status') in ('REJECTED','RETIRED','SUPERSEDED')
        history = history or (item['status']=='SUCCEEDED' and (age_days(item['finished_at'] or item['created_at'],at) or 0)>14)
        item['display_bucket'] = 'HISTORY' if history else 'CURRENT'
        # The stored status can become stale without a database mutation. Verify
        # freshness before it consumes the current-work budget.
        if history and history_count < 10:
            public.append(item)
            history_count += 1
        elif not history and current_count < 20:
            public.append(item)
            current_count += 1
        if current_count >= 20 and history_count >= 10:
            break
    public.sort(key=lambda r: r['display_bucket']=='HISTORY')
    return public, len(rows)


def attach(store, summary):
    """Keep the complete signed display below the existing 1 MB receiver limit."""
    at = now()
    selected, total = _review_selection(store, summary['items'], at)
    reviews = []
    for item in selected:
        if size(reviews + [item]) > REVIEW_BUDGET:
            break
        reviews.append(item)
    discovery = summary.get('discovery')
    if isinstance(discovery, dict):
        errors = discovery.get('errors')
        errors = errors if isinstance(errors, list) else []
        discovery = {'at': text(discovery.get('at'), 60),
                     'errors': [text(x, 300) for x in errors[:20]], 'error_count': len(errors)}
    else:
        discovery = None
    return {**summary, 'items': reviews, 'discovery': discovery,
            'reviews_total': total, 'reviews_shown': len(reviews),
            'reviews_omitted': total - len(reviews),
            'reviews_selection_notice': '每个审查对象仅展示最近一次；优先待处理，已失效或完成超过14日的审查折叠为历史。完整尝试记录保留在本机。',
            'proposals': cards(store, at=at)}
