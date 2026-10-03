"""Model-free short-horizon diagnostics; never an approval or candidate input.

Only scalar program statistics enter the signed display. Original scores and
frozen proposal/self-check evidence are neither rewritten nor augmented here.
"""
import json
import math
import re
from datetime import date

from .storage import now, normalize_time, json_write, digest

VERSION = 'quick-diagnostics-v1'
STATE_KEY = 'quick_diagnostics_last'
MAX_BYTES = 90_000
MAX_BUILDS = 24
NOTICE = ('两种期限分别统计，样本集合与成熟时间不同，不能把均值直接作为期限优劣比较。'
          '5 日仅作短期价格辅助诊断，不证明预测兑现或策略盈利，不进入自动候选、监督有效性判断或批准。'
          '少于30个时间簇不显示区间；时间簇仍不保证独立。')
QUICK_NOTICE = ('A股从判断可用后的首个开盘起，含入场日共5个交易日；全球沿原口径，'
                '使用6个已完成美元收盘点之间的5期变化，并非5个A股交易日。'
                '未计交易费用；全球未计汇率。历史补登记与来源未分类样本仅作回顾诊断，不是前向验证。')
GROUPS = {'trend_filter': ('trend_ok', 'trend_fail'), 'research_veto': ('WATCH', 'NOT_WATCH'),
          'portfolio_allow': ('ALLOW', 'NOT_ALLOW'), 'global_stance': ('LONG', 'WAIT')}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _count(value):
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else 0


def _time(value):
    try:
        return normalize_time(value) if isinstance(value, str) and len(value) <= 64 else None
    except (ValueError, TypeError):
        return None


def _day(value):
    try:
        return date.fromisoformat(value).isoformat() if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) else None
    except ValueError:
        return None


def _coverage(value):
    value = value if isinstance(value, dict) else {}
    return {'scope': 'ALL_HISTORY', **{key: _time(value.get(key)) for key in
        ('registered_from', 'registered_through', 'scored_judgment_from', 'scored_judgment_through', 'last_scored_at')},
        **{key: _day(value.get(key)) for key in ('observation_from', 'observation_through')}}


def _build_time(value):
    value = value if isinstance(value, dict) else {}
    return {key: _time(value.get(key)) for key in ('first_judgment_at', 'last_judgment_at', 'last_scored_at')}


def _stats(value):
    value = value if isinstance(value, dict) else {}
    result = {k: _count(value.get(k)) for k in ('n', 'time_clusters')}
    for key in ('mean_bps', 'median_bps', 'hit_rate'):
        result[key] = value.get(key) if _number(value.get(key)) else None
    ci = value.get('ci95_bps')
    result['ci95_bps'] = (ci if result['time_clusters'] >= 30 and isinstance(ci, list)
                         and len(ci) == 2 and all(_number(n) for n in ci) and ci[0] <= ci[1] else None)
    return result


def _groups(value):
    value = value if isinstance(value, dict) else {}
    return {group: {label: {sample: _stats((value.get(group, {}).get(label, {}) or {}).get(sample))
                           for sample in ('daily', 'non_overlapping')}
                    for label in labels} for group, labels in GROUPS.items()}


def _comparison(value, *, quick=False):
    from .evaluation import SCORE_METHOD, QUICK_SCORE_METHOD
    value = value if isinstance(value, dict) else {}
    counts = value.get('counts') if isinstance(value.get('counts'), dict) else {}
    builds = value.get('by_build') if isinstance(value.get('by_build'), dict) else {}
    ids = [k for k in builds if isinstance(k, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', k)]
    timing = value.get('build_times') if isinstance(value.get('build_times'), dict) else {}
    timing = {k: _build_time(timing.get(k)) for k in ids}
    # Build hashes are random, not a chronology. Unknown dates keep their input
    # order for old summaries; never label that legacy order as newest-first.
    ids.sort(key=lambda k: timing[k]['last_judgment_at'] or '', reverse=True)
    shown = ids[:MAX_BUILDS]
    result = {'method': QUICK_SCORE_METHOD if quick else SCORE_METHOD,
              'horizon_days': 5 if quick else _count(value.get('horizon_days')),
              'counts': {k: _count(v) for k, v in counts.items()
                         if isinstance(k, str) and re.fullmatch(r'(watchlist|portfolio|global):(OPEN|SCORED|UNSCORABLE)', k)},
              'coverage': _coverage(value.get('coverage')),
              'groups': _groups(value.get('groups')), 'by_build': {k: _groups(builds[k]) for k in shown},
              'build_times': {k: timing[k] for k in shown},
              'mixed_builds': value.get('mixed_builds') is True,
              'omitted_builds': _count(value.get('omitted_builds')) + max(0, len(ids) - MAX_BUILDS),
              'notice': QUICK_NOTICE if quick else '原有评分方法与期限保持不变；自选股及组合通常20个交易日，全球按原登记期限。原评分同样是价格观察，不能单独确认策略盈利。'}
    if quick:
        result.update(auxiliary_only=True, approval_eligible=False)
        sources = value.get('sample_sources')
        if isinstance(sources, dict):
            result['sample_sources'] = {scope: {k: _count((sources.get(scope) or {}).get(k))
                                               for k in ('LIVE', 'LEGACY', 'UNCLASSIFIED')}
                                        for scope in ('scored_rows', 'daily_samples')}
    return result


def public(value):
    """Bounded allowlist, also used when loading a stored summary for display."""
    if not isinstance(value, dict) or value.get('version') != VERSION:
        return {'version': VERSION, 'status': 'NOT_RUN', 'notice': NOTICE}
    at = value.get('generated_at')
    try:
        at = normalize_time(at) if isinstance(at, str) else None
    except (ValueError, TypeError):
        at = None
    result = {'version': VERSION, 'status': 'READY' if value.get('status') == 'READY' else 'ERROR',
              'generated_at': at, 'notice': NOTICE}
    if result['status'] != 'READY':
        return result
    result.update(primary=_comparison(value.get('primary')), quick=_comparison(value.get('quick'), quick=True))
    while len(json.dumps(result, ensure_ascii=False).encode()) > MAX_BYTES:
        arm = max(('primary', 'quick'), key=lambda key: len(result[key]['by_build']))
        if not result[arm]['by_build']:
            break
        oldest = next(reversed(result[arm]['by_build']))
        del result[arm]['by_build'][oldest]
        del result[arm]['build_times'][oldest]
        result[arm]['omitted_builds'] += 1
    result['omitted_builds'] = sum(result[k]['omitted_builds'] for k in ('primary', 'quick'))
    return result


def view(store):
    row = store.db.execute('SELECT value FROM service_state WHERE key=?', (STATE_KEY,)).fetchone()
    try:
        return public(json.loads(row[0]) if row else None)
    except (ValueError, TypeError, AttributeError):
        return {'version': VERSION, 'status': 'ERROR', 'notice': NOTICE}


def collect(store, config, at=None):
    """Read-only snapshot of both methods, without scoring or governance writes."""
    from .evaluation import comparisons, quick_comparisons, registry_counts, SCORE_METHOD, QUICK_SCORE_METHOD
    at = normalize_time(at or now())
    primary = comparisons(store, config, at=at)
    primary['counts'] = registry_counts(store, at=at)
    quick = quick_comparisons(store, config, at=at)
    quick['counts'] = registry_counts(store, method=QUICK_SCORE_METHOD, at=at)
    for comparison, method in ((primary, SCORE_METHOD), (quick, QUICK_SCORE_METHOD)):
        comparison.update(_timing(store, method, at))
    return public({'version': VERSION, 'status': 'READY', 'generated_at': at,
                   'primary': primary, 'quick': quick})


def _timing(store, method, at):
    """Read scalar coverage dates only; never rewrite scores or registry rows.

    Coverage describes all registered/scored records before the snapshot, not
    independent samples. Per-build recency is the latest scored judgment time,
    so rescoring/backfilling an old build cannot make it look like a new build.
    """
    registered = store.db.execute('''SELECT min(created_at) registered_from,
        max(created_at) registered_through FROM signal_registry
        WHERE route IN ('watchlist','portfolio','global') AND created_at<=?''', (at,)).fetchone()
    source = ''' FROM signal_registry r JOIN signal_scores s ON s.signal_id=r.id
        WHERE s.method=? AND s.status='SCORED' AND r.created_at<=? AND s.scored_at<=?
        AND r.route IN ('watchlist','portfolio','global')'''
    scored = store.db.execute('''SELECT min(r.created_at) scored_judgment_from,
        max(r.created_at) scored_judgment_through, max(s.scored_at) last_scored_at,
        min(CASE WHEN json_valid(s.score_json) THEN json_extract(s.score_json,'$.entry_date') END) observation_from,
        max(CASE WHEN json_valid(s.score_json) THEN json_extract(s.score_json,'$.exit_date') END) observation_through'''
        + source, (method, at, at)).fetchone()
    builds = store.db.execute('''SELECT coalesce(nullif(r.build_id,''),'UNKNOWN') build,
        min(r.created_at) first_judgment_at, max(r.created_at) last_judgment_at,
        max(s.scored_at) last_scored_at''' + source + ' GROUP BY build', (method, at, at))
    return {'coverage': {**dict(registered), **dict(scored)},
            'build_times': {row['build']: {k: row[k] for k in
                ('first_judgment_at', 'last_judgment_at', 'last_scored_at')} for row in builds}}


def run(store, config, at=None):
    """Only append q5 scores and a new diagnostic report; no model invocation."""
    from .evaluation import score_quick, QUICK_SCORE_METHOD
    from .workflow import task_lock
    if config.get('deployment_role') == 'cloud':
        raise ValueError('5日辅助诊断只在本机生成，云端只显示签名统计摘要')
    at = normalize_time(at or now())
    with task_lock(store.root, 'quick-diagnostics', wait_seconds=10):
        scores = score_quick(store, config, at)
        result = collect(store, config, at)
        report = {**result, 'new_scores': scores}
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)
        path = store.root / 'workflow' / 'evaluation' / QUICK_SCORE_METHOD / (at[:10] + '-' + digest(encoded)[:12] + '.json')
        json_write(path, report)
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO service_state VALUES(?,?)', (STATE_KEY, encoded))
        return {**report, 'report': str(path.relative_to(store.root))}


def safe_run(store, config, at=None):
    """Keep an auxiliary failure from blocking the existing daily evaluation."""
    at = normalize_time(at or now())
    try:
        return run(store, config, at)
    except Exception as exc:
        result = {'version': VERSION, 'status': 'ERROR', 'generated_at': at,
                  'notice': NOTICE, 'error_type': type(exc).__name__}
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO service_state VALUES(?,?)',
                             (STATE_KEY, json.dumps(result, ensure_ascii=False)))
        return result


def markdown(value):
    """Separate section for human reports, excluded from supervision facts."""
    value = public(value)
    lines = ['## 5 日辅助诊断（不参与批准）', '', NOTICE, '', QUICK_NOTICE, '']
    if value['status'] != 'READY':
        return '\n'.join(lines + ['尚未生成可用诊断。'])
    coverage = value['quick']['coverage']
    lines += [f"统计时间：{value['generated_at']}；全历史累计，非最近5日新增样本；原期限评分见前面的结论注册表。",
              f"登记覆盖：{coverage['registered_from'] or '未提供'} 至 {coverage['registered_through'] or '未提供'}；"
              f"已评分价格观察覆盖：{coverage['observation_from'] or '未提供'} 至 {coverage['observation_through'] or '未提供'}。", '',
              '| 版本 | 比较项 / 分组 | 每日样本 | 不重叠样本 / 时间簇 | 5日平均超额 | 95%聚类近似区间 |',
              '|---|---|---|---|---|---|']
    names = {'trend_filter': '趋势过滤', 'research_veto': '研究否决', 'portfolio_allow': '组合放行', 'global_stance': '全球方向'}
    for build, groups in [('汇总（描述）', value['quick']['groups']), *value['quick']['by_build'].items()]:
        for group, labels in groups.items():
            for label, stats in labels.items():
                sample = stats['non_overlapping']
                if not stats['daily']['n']:
                    continue
                mean, ci = sample['mean_bps'], sample['ci95_bps']
                average = '—' if mean is None else f'{mean / 100:+.2f}%'
                interval = '样本不足' if ci is None else f'{ci[0] / 100:+.2f}% ~ {ci[1] / 100:+.2f}%'
                lines.append(f"| {build} | {names[group]} / {label} | {stats['daily']['n']} | {sample['n']} / {sample['time_clusters']} | {average} | {interval} |")
    lines += ['', '到期状态：' + json.dumps(value['quick']['counts'], ensure_ascii=False),
              '样本来源：' + json.dumps(value['quick'].get('sample_sources', {}), ensure_ascii=False),
              '前向登记仍不代表本次5日指标已预注册；只作辅助观察，不加入实验样本或自动候选。']
    if value['omitted_builds']:
        lines.append(f"展示上限省略{value['omitted_builds']}组版本；完整评分保留在本机。")
    return '\n'.join(lines)
