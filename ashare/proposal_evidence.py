"""Bounded proposal evidence summaries from frozen program diagnostics only.

Never follow an evidence path or read research/judgment bodies. The proposal and
its append-only links stay untouched; the material hash binds every valid link,
including records omitted from the bounded display.
"""
import hashlib
import json
import math
import re

VERSION = 'proposal-evidence-v1'
MAX_COHORTS = 8
MAX_REFERENCES = 20
ROUTES = ('watchlist', 'portfolio', 'dynamic', 'global')
BASE_LIMITATIONS = [
    '价格与超额分组仅为观察性诊断，不是实际成交收益，不能证明因果、策略优劣或未来盈利。',
    '同一判断在不同报告或期限中可能重复；各版本的样本和时间簇不得相加。',
    '候选实验尚未运行；监督意见不等于用户批准，也不改变生产规则。',
    '全球价格诊断不含汇率与费用；动态净反应含估算成本，其他价格诊断不等于扣费后的策略收益。',
]


def _encode(value):
    # Matches the frozen selfcheck report/signature encoding.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_encode(value).encode()).hexdigest()


def _identifier(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9:_.-]{0,199}', value))


def _ids(value):
    if not isinstance(value, list) or not value or any(not _identifier(x) for x in value) or len(set(value)) != len(value):
        raise ValueError('证据编号清单为空或格式无效')
    return sorted(value)


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _stats(value):
    if not isinstance(value, dict) or not _count(value.get('n')):
        raise ValueError('冻结统计格式无效')
    result = {'n': value['n']}
    for key in ('mean_bps', 'median_bps', 'hit_rate'):
        if key in value:
            if not _number(value[key]):
                raise ValueError('冻结统计格式无效')
            result[key] = value[key]
    if 'time_clusters' in value:
        if not _count(value['time_clusters']) or value['time_clusters'] > value['n']:
            raise ValueError('冻结时间簇统计无效')
        result['time_clusters'] = value['time_clusters']
    if 'ci95_bps' in value:
        ci = value['ci95_bps']
        if ci is not None and (not isinstance(ci, list) or len(ci) != 2 or not all(_number(n) for n in ci) or ci[0] > ci[1]):
            raise ValueError('冻结区间统计无效')
        result['ci95_bps'] = ci
    return result


def _scope(proposal, payload):
    scope = payload.get('applicability')
    if not isinstance(scope, dict) or set(scope) != {'route', 'build_id', 'environment'}:
        raise ValueError('结构化证据缺少明确适用范围')
    if scope['route'] not in ROUTES or proposal.get('target') != scope['route'] or any(not _identifier(v) for v in scope.values()):
        raise ValueError('证据适用范围与提案不一致')
    if scope['build_id'] == 'UNKNOWN':
        raise ValueError('证据缺少冻结版本')
    return dict(scope)


def _resolve(store, run_id, ids, scope):
    if not isinstance(run_id, str) or not re.fullmatch(r'SC-[0-9a-f]{20}', run_id):
        raise ValueError('自检报告编号无效')
    row = store.db.execute('SELECT id,created_at,evidence_hash,payload_json FROM selfcheck_runs WHERE id=?', (run_id,)).fetchone()
    if not row:
        raise LookupError('引用的冻结自检报告缺失')
    report = json.loads(row['payload_json'])
    if not isinstance(report, dict) or report.get('id') != row['id'] or report.get('created_at') != row['created_at']:
        raise ValueError('冻结报告元数据不一致')
    content = {k: v for k, v in report.items() if k not in ('id', 'created_at')}
    if _hash(content) != row['evidence_hash'] or run_id != 'SC-' + row['evidence_hash'][:20] or report.get('method') != 'selfcheck-v1':
        raise ValueError('冻结报告哈希或方法不一致')
    selected = set(ids)
    found, cohorts = set(), []
    if not isinstance(report.get('cohorts'), list):
        raise ValueError('冻结报告缺少分组统计')
    for c in report['cohorts']:
        if not isinstance(c, dict):
            raise ValueError('冻结分组格式无效')
        group_ids = set(_ids(c.get('evidence_ids')))
        if not group_ids & selected:
            continue
        # Full-cohort statistics cannot be represented as statistics for a subset.
        if not group_ids <= selected or any(c.get(k) != v for k, v in scope.items()) or c.get('provenance') != 'LIVE':
            raise ValueError('证据编号与提案范围或完整分组不匹配')
        safe = {k: c.get(k) for k in ('route', 'build_id', 'environment', 'horizon_days', 'stance', 'provenance', 'benchmark', 'score_method', 'evidence_status')}
        if not _count(safe['horizon_days']) or safe['horizon_days'] == 0 or any(not _identifier(safe[k]) for k in ('stance', 'benchmark', 'score_method')):
            raise ValueError('冻结分组口径无效')
        if safe['evidence_status'] not in ('INSUFFICIENT', 'DESCRIPTIVE_ONLY'):
            raise ValueError('冻结分组证据状态无效')
        safe.update({k: _stats(c.get(k)) for k in ('price', 'excess', 'market', 'net_direction')})
        if any(safe[k]['n'] > len(group_ids) for k in ('price', 'excess', 'market', 'net_direction')):
            raise ValueError('冻结分组样本数与编号不一致')
        safe.update(run_id=run_id, reference_count=len(group_ids), reference_hash=_hash(sorted(group_ids)))
        cohorts.append(safe)
        found.update(group_ids)
    if found != selected:
        raise ValueError('引用的证据未出现在适用的冻结分组中')
    # Only metadata/hashes and program scores from the frozen report are used to
    # verify a link. No per-judgment score or raw contract enters the summary.
    proof = [e for e in report.get('evidence', []) if isinstance(e, dict) and e.get('id') in selected]
    if len(proof) != len(selected) or {e.get('id') for e in proof} != selected or any(e.get('status') != 'SCORED' for e in proof):
        raise ValueError('冻结证据清单与分组不一致')
    cohorts.sort(key=_encode)
    snapshot = {'run_id': run_id, 'created_at': row['created_at'], 'report_hash': row['evidence_hash'],
                'reference_count': len(ids), 'reference_hash': _hash(ids), 'cohorts': cohorts}
    return snapshot, _hash(proof), sorted(_hash(e) for e in proof)


def _failure(source, status, reason, additional_count=0):
    return {'version': VERSION, 'source': source, 'status': status, 'text': reason,
            'cohorts': [], 'references': [], 'references_count': 0, 'additional_count': additional_count,
            'snapshots': [], 'limitations': [reason, *BASE_LIMITATIONS]}


def summary(store, proposal, payload):
    """Return bounded, deterministic presentation/review material; never write.

    RECORDED means an unverified legacy description, INSUFFICIENT means valid
    frozen diagnostics with no validated candidate experiment. MISSING/INVALID
    must not be submitted as verified evidence. ``material_hash`` binds all
    original/additional snapshots even when the display budget omits some.
    """
    if not isinstance(payload, dict):
        return _failure('UNKNOWN', 'INVALID', '提案内容格式无效')
    evidence = payload.get('evidence')
    source = 'TEXT' if isinstance(evidence, str) else 'SELFCHECK'
    links = store.db.execute('SELECT evidence_hash,run_id,created_at,payload_json FROM selfcheck_evidence WHERE proposal_id=? ORDER BY created_at,rowid', (proposal.get('id'),)).fetchall()
    additional_count = len(links)
    if isinstance(evidence, str) and evidence.strip() and not links:
        return {'version': VERSION, 'source': 'TEXT', 'status': 'RECORDED', 'text': evidence,
                'cohorts': [], 'references': [], 'references_count': 0, 'additional_count': 0,
                'snapshots': [], 'limitations': ['原提案文字仅作证据说明；未读取路径或核验其所指原始资料。', *BASE_LIMITATIONS]}
    try:
        if not isinstance(evidence, dict) or set(evidence) != {'run_id', 'ids', 'status'} or evidence['status'] != 'UNVALIDATED_HYPOTHESIS':
            raise ValueError('证据须为非空文字或受支持的自检引用结构')
        scope = _scope(proposal, payload)
        ids = _ids(evidence['ids'])
        original, original_hash, original_signatures = _resolve(store, evidence['run_id'], ids, scope)
        original.update(kind='ORIGINAL', link_hash=original_hash)
        all_snapshots = [original]
        references = {evidence['run_id'], *ids}
        additional_count = 0
        for link in links:
            p = json.loads(link['payload_json'])
            if not isinstance(p, dict) or set(p) != {'ids', 'signatures'}:
                raise ValueError('追加证据结构无效')
            linked_ids = _ids(p['ids'])
            frozen, fingerprint, signatures = _resolve(store, link['run_id'], linked_ids, scope)
            if link['evidence_hash'] != fingerprint or p['signatures'] != signatures:
                raise ValueError('追加证据指纹与冻结报告不一致')
            references.update([link['run_id'], *linked_ids])
            # selfcheck records the original snapshot in the link table too.
            if link['run_id'] == original['run_id'] and fingerprint == original_hash and signatures == original_signatures:
                continue
            frozen.update(kind='ADDITIONAL', link_hash=fingerprint, linked_at=link['created_at'])
            all_snapshots.append(frozen)
            additional_count += 1
        latest = all_snapshots[-1]
        cohorts = latest['cohorts'][:MAX_COHORTS]
        limitations = list(BASE_LIMITATIONS)
        if any(c['excess'].get('time_clusters', 0) < 30 for c in latest['cohorts']):
            limitations.append('至少一个适用分组不足30个时间簇；簇之间也不保证独立，当前不能判断策略有效性。')
        limitations.append('LIVE表示判断当时已冻结，不等于已经完成基线与候选的前向对照实验。')
        limitations.append('股票期限使用交易日口径；全球期限按可用日线观察窗计算，不能直接按日历日或跨路线比较。')
        if len(latest['cohorts']) > MAX_COHORTS:
            limitations.append(f'当前仅展示前{MAX_COHORTS}个分组；材料哈希覆盖全部分组。')
        if len(references) > MAX_REFERENCES:
            limitations.append(f'编号仅展示前{MAX_REFERENCES}项；材料哈希覆盖全部引用。')
        displayed_snapshots = [all_snapshots[0]] if len(all_snapshots) == 1 else [all_snapshots[0], all_snapshots[-1]]
        snapshots = [{k: v for k, v in item.items() if k != 'cohorts'} | {'cohort_count': len(item['cohorts'])} for item in displayed_snapshots]
        text = '证据不足：以下为冻结价格诊断，候选实验尚未运行。'
        for c in cohorts:
            stats = c['excess']
            horizon_unit = '根观测日线' if c['benchmark'] == 'CASH_USD' else '个交易日'
            interval = stats.get('ci95_bps')
            interval_text = f"95%区间 {interval[0]} 至 {interval[1]} bps" if interval else '95%区间尚不能估计'
            text += (f"\n{c['route']} / {c['build_id']} / {c['horizon_days']}{horizon_unit} / {c['stance']} / {c['provenance']}："
                     f"样本 {stats['n']}，时间簇 {stats.get('time_clusters', 0)}，"
                     f"平均超额 {stats.get('mean_bps', '未知')} bps，{interval_text}。")
        text += (f"\n适用环境 {scope['environment']}。原始报告 {original['run_id']} 引用{original['reference_count']}个判断；"
                 f"追加{additional_count}个证据版本。最新报告 {latest['run_id']} 引用{latest['reference_count']}个判断、"
                 f"{len(latest['cohorts'])}个分组。各版本与重叠期限不得累计样本。")
        return {'version': VERSION, 'source': 'SELFCHECK', 'status': 'INSUFFICIENT', 'text': text,
                'cohorts': cohorts, 'cohort_count': len(latest['cohorts']), 'references': sorted(references)[:MAX_REFERENCES],
                'references_count': len(references), 'additional_count': additional_count, 'snapshots': snapshots,
                'snapshot_count': len(all_snapshots), 'applicability': scope, 'limitations': limitations,
                'material_hash': _hash({'version': VERSION, 'scope': scope, 'evidence': evidence, 'snapshots': all_snapshots})}
    except LookupError as exc:
        return _failure(source, 'MISSING', str(exc), additional_count)
    except (ValueError, TypeError, KeyError, OverflowError):
        # Do not echo nested, malformed or unknown evidence content into a model.
        return _failure(source, 'INVALID', '证据结构、冻结指纹或提案适用范围不一致，需核对引用。', additional_count)
