"""Deterministic long-running diagnostics and bounded, evidence-linked candidate drafts.

No model calls, strategy changes, causal claims or experiment execution. Immutable
reports and append-only evidence links let a later experiment runner start cleanly.
"""
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from .storage import now, normalize_time, digest, json_write
from .calendar import local
from .judgments import encode, backfill
from .evaluation import SCORE_METHOD, daily_samples, non_overlapping, time_clusters, describe

METHOD = 'selfcheck-v1'
WEEKLY_BUDGET = 3
LIMITATIONS = [
    '所有收益分组均为观察性诊断，不能证明因果、策略优劣或未来盈利。',
    '全球价格诊断不含汇率与费用；动态净反应含估算成本，空头方向不代表可做空。',
    '股票超额仅相对沪深300；尚未控制行业、风险暴露及选股差异。',
    '事实预测、仓位优劣需要单独检验；当前不以涨跌替代经营证据。',
    '重叠时间簇不足时维持现行方法；一期不运行候选实验，不自动采纳提案。']


def outcomes(store, at):
    """Join frozen predictions to only outcomes known at this check's cutoff."""
    rows = []
    since = normalize_time((datetime.fromisoformat(at) - timedelta(days=180)).isoformat())
    for r in store.db.execute('SELECT * FROM judgment_contracts WHERE created_at>=? AND created_at<=? AND frozen_at<=? ORDER BY created_at,id', (since, at, at)):
        c = dict(r); p = json.loads(c.pop('payload_json'))
        score, status = {}, 'AWAITING_SCORE'
        if c['route'] == 'dynamic':
            s = store.db.execute('SELECT * FROM dynamic_observations WHERE case_id=? AND ready_at<=?', (c['source_id'], at)).fetchone()
            if s:
                raw = json.loads(s['payload_json'])
                status = 'SCORED' if raw.get('status') == 'MEASURED' else 'UNSCORABLE'
                score = {'entry_date': s['entry_at'][:10], 'exit_date': s['exit_at'][:10],
                    'return_bps': raw.get('market_return_bps'), 'net_direction_bps': raw.get('net_return_bps'),
                    'excess_bps': raw.get('excess_return_bps'), 'cost_bps': raw.get('cost_bps'),
                    'method': 'dynamic-reaction-v1', 'status': raw.get('status')}
        else:
            s = store.db.execute('SELECT * FROM signal_scores WHERE signal_id=? AND method=? AND scored_at<=?', (c['id'], SCORE_METHOD, at)).fetchone()
            if s:
                status, score = s['status'], json.loads(s['score_json'])
        rows.append({**c, 'contract': p, 'judgment': p['judgment'], 'status': status, 'score': score})
    return rows


def cohorts(rows):
    # Sample before grouping by build, environment or action, preventing selection
    # of different overlapping observations for each favorable subgroup.
    eligible = [r for r in rows if r['status'] == 'SCORED' and r['provenance'] != 'RETROSPECTIVE']
    groups = defaultdict(list)
    for route in ('watchlist', 'portfolio', 'dynamic', 'global'):
        chosen = time_clusters(non_overlapping(daily_samples([r for r in eligible if r['route'] == route]), 0))
        for r in chosen:
            c = r['contract']
            # A legacy replay is never counted as prospective validation.
            key = (route, r['build_id'], c['market']['label'], r['horizon_days'],
                   c['prediction']['stance'] or 'UNKNOWN', r['provenance'], c['benchmark'], r['score'].get('method'))
            groups[key].append(r)
    result = []
    for key, selected in sorted(groups.items(), key=lambda item: str(item[0])):
        route, build, environment, days, stance, provenance, benchmark, score_method = key
        stats = describe(selected)
        returns = describe(selected, 'return_bps')
        market = describe(selected, 'benchmark_return_bps')
        result.append({'route': route, 'build_id': build, 'environment': environment, 'horizon_days': days,
            'stance': stance, 'provenance': provenance, 'benchmark': benchmark, 'score_method': score_method,
            'price': returns, 'excess': stats, 'market': market,
            'net_direction': describe(selected, 'net_direction_bps') if route == 'dynamic' else {'n': 0},
            'evidence_ids': [r['id'] for r in selected],
            'evidence_status': 'INSUFFICIENT' if stats.get('time_clusters', 0) < 30 else 'DESCRIPTIVE_ONLY'})
    return result


def execution_summary(store, at):
    row = store.db.execute('SELECT id,window_end,ready_at,payload_json FROM reviews WHERE ready_at<=? ORDER BY window_end DESC,revision DESC LIMIT 1', (at,)).fetchone()
    if not row:
        return {'status': 'MISSING', 'checks': [], 'reason': '尚无复盘一致性核验'}
    checks = json.loads(row['payload_json']).get('consistency_checks', [])
    expired = datetime.fromisoformat(at) - datetime.fromisoformat(row['window_end']) > timedelta(days=2)
    return {'status': 'STALE' if expired else ('ATTENTION' if any(c.get('status') in ('FAIL', 'INSUFFICIENT', 'ERROR') for c in checks) else 'RECORDED'),
            'review_id': row['id'], 'window_end': row['window_end'],
            'checks': [{k: c.get(k) for k in ('check', 'status', 'checked', 'failures', 'missing', 'routes')} for c in checks],
            'note': '逐项沿用程序核验；缺证据不是违规，已有工程问题继续按原流程跟踪'}


def analyze(store, at):
    rows = outcomes(store, at)
    cs = cohorts(rows)
    gaps = Counter()
    for r in rows:
        if r['contract']['market']['status'] != 'KNOWN':gaps['unknown_market'] += 1
        if r['contract']['business_test']['status'] == 'UNSTRUCTURED':gaps['unstructured_business_test'] += 1
        if not r['contract']['prediction'].get('invalidation'):gaps['missing_invalidation'] += 1
        if r['build_id'] == 'UNKNOWN':gaps['unknown_build'] += 1
    frozen_evidence = [{'id': r['id'], 'contract_hash': digest(encode(r['contract'])),
                        'status': r['status'], 'score': r['score']} for r in rows]
    return {'method': METHOD, 'counts': dict(Counter(r['route'] + ':' + r['status'] for r in rows)),
        'provenance': dict(Counter(r['provenance'] for r in rows)), 'gaps': dict(gaps),
        'cohorts': cs, 'execution': execution_summary(store, at),
        'layers': {'data': '见缺失、不可评分和历史补登记统计',
                   'business_facts': 'UNSTRUCTURED：当前未自动核验经营预测',
                   'price_prediction': '按路线、版本、环境、期限、基准和行动分组；观望不解释为预测下跌',
                   'portfolio': '分组描述组合选择后价格表现，尚不足以归因仓位效果',
                   'execution': '沿用复盘原始程序检查'},
        'evidence': frozen_evidence, 'lookback_days': 180,
        'limitations': LIMITATIONS + ['诊断读取最近180天判断；更早合同、提案、失败实验和报告保留，不删除。']}, rows


def candidate_specs(report, rows):
    """Small explicit catalog of single-factor hypotheses, never arbitrary code generation."""
    candidates = []
    for c in report['cohorts']:
        if c['provenance'] != 'LIVE' or c['build_id'] == 'UNKNOWN':
            continue
        selected = [r for r in rows if r['id'] in set(c['evidence_ids'])]
        if c['route'] != 'portfolio' and any(r['contract']['business_test']['status'] == 'UNSTRUCTURED' for r in selected):
            candidates.append((c, 'EXPLICIT_INVALIDATION', '补充可核验的反证条件',
                '在研究输出中增加单一结构字段 hypothesis_test：claim、metric、operator、threshold、deadline、source_ids、invalidation；'
                '无法量化时明确 UNKNOWN，不虚构数值。候选仅在实验区使用，生产决策保持现行版本。',
                '增加明确验证条件可能提高研究的可检验性；当前缺失本身不能证明盈利受损。',
                'verifiable_prediction_rate'))
        active = c['stance'] in ('PAPER_TRADE', 'LONG', 'BULLISH', 'ALLOW')
        if active and c['excess'].get('n', 0) >= 5 and c['excess'].get('mean_bps', 0) < 0:
            candidates.append((c, 'COUNTEREVIDENCE_CHECK', '增加一次反向证据核对',
                '在最终结论前增加一个固定反向证据检查字段：引用一项可能推翻当前判断的已提供证据及影响；'
                '无反证则写未发现，不得检索未来资料或更改仓位、入场及风控规则。',
                '该组已到期价格反应偏弱；反证检查是待验证解释，大盘、暴露及样本选择也可能解释差异。',
                'paired_net_excess_bps'))
    # Multiple stances/horizons are evidence for the same proposed change, not new trials.
    merged = {}
    for c, change, title, text, hypothesis, metric in candidates:
        key = digest(encode([METHOD, c['route'], c['build_id'], c['environment'], change, text]))
        if key not in merged:
            merged[key] = {'key': key, 'cohort': c, 'change_id': change, 'title': title,
                           'change': text, 'hypothesis': hypothesis, 'metric': metric, 'evidence_ids': []}
        if c['horizon_days'] > merged[key]['cohort']['horizon_days']:
            merged[key]['cohort'] = c
        merged[key]['evidence_ids'] = sorted(set(merged[key]['evidence_ids']) | set(c['evidence_ids']))
    return sorted(merged.values(), key=lambda c: c['key'])


def design(candidate, at):
    from .experiments import VERSION
    c = candidate['cohort']
    horizon = c['horizon_days']
    # Conservative calendar windows; future runner must additionally purge labels
    # still unresolved at a boundary. Design time is not an enrollment timestamp.
    return {'version': VERSION, 'baseline_build': c['build_id'], 'route': c['route'],
        'environment': c['environment'], 'change': {'id': candidate['change_id'], 'text': candidate['change']},
        'enrollment': {'start_after': normalize_time((datetime.fromisoformat(at) + timedelta(days=1)).isoformat()),
            'window_days': min(120, max(30, horizon * 2 + 10)), 'embargo_days': min(120, max(7, horizon * 2)),
            'windows': 2, 'minimum_pairs': 30},
        'primary_metric': candidate['metric'],
        'failure_criteria': ('后续隔离窗口中可核验预测比例未高于基线，或引文正确率下降，则否决；资料不足则继续观察。'
                             if candidate['metric'] == 'verifiable_prediction_rate' else
                             '后续隔离窗口中扣费后配对超额不高于基线，或回撤恶化，则否决；不足30对或时间相关性无法处理则证据不足。'),
        'rollback': '候选仅在隔离实验中；关闭候选并保留所有失败记录，生产继续原版本。',
        'budget': {'arms': 2, 'major_changes': 1, 'max_model_calls': 120, 'max_retries': 2, 'max_wait_days': 180},
        'controls': ['SAME_INFORMATION_TIME', 'SAME_EXECUTION_AND_COSTS', 'ISOLATED_LEDGER',
                     'PURGE_UNMATURED_LABELS', 'KEEP_ALL_FAILURES', 'NO_PRODUCTION_PROMOTION']}


def propose(store, report, rows, at):
    from .governance import draft_proposal
    from .experiments import register
    current = local(at)
    week = f'{current.isocalendar()[0]}-W{current.isocalendar()[1]:02d}'
    results = []
    # Serialize budget and dedupe with other local workers.
    store.db.execute('BEGIN IMMEDIATE')
    try:
        used = store.db.execute("SELECT count(*) FROM strategy_proposals WHERE source='selfcheck' AND json_extract(payload_json,'$.generation_week')=?", (week,)).fetchone()[0]
        for candidate in candidate_specs(report, rows):
            key = 'selfcheck:' + candidate['key']
            row = store.db.execute('SELECT id,status FROM strategy_proposals WHERE dedupe_key=?', (key,)).fetchone()
            proof = [e for e in report['evidence'] if e['id'] in set(candidate['evidence_ids'])]
            fingerprint = digest(encode(proof))
            signatures = sorted(digest(encode(e)) for e in proof)
            if row:
                seen = {s for e in store.db.execute('SELECT payload_json FROM selfcheck_evidence WHERE proposal_id=?', (row['id'],))
                        for s in json.loads(e[0]).get('signatures', [])}
                if set(signatures) <= seen:continue
            if row:
                pid = row['id']
            else:
                if used >= WEEKLY_BUDGET:continue
                spec = design(candidate, at)
                c = candidate['cohort']
                payload = {'hypothesis': candidate['hypothesis'], 'change': candidate['change'],
                    'evidence': {'run_id': report['id'], 'ids': candidate['evidence_ids'], 'status': 'UNVALIDATED_HYPOTHESIS'},
                    'test_plan': '第二期运行器启用后，冻结基线与候选，使用同一时点输入；先观察窗口1，再经隔离窗口2确认，不按窗口1结果调参。'
                        '窗口间留空并剔除未到期标签；同一事件不跨集合。指标、费用、成交与资金限制两组相同；当前仅登记未运行。',
                    'failure_criteria': spec['failure_criteria'], 'rollback': spec['rollback'],
                    'metrics': {'primary': spec['primary_metric'], 'secondary': ['drawdown', 'costs', 'evidence_accuracy']},
                    'observation_period': spec['enrollment'], 'generation_week': week, 'experiment_design': spec,
                    'applicability': {'route': c['route'], 'build_id': c['build_id'], 'environment': c['environment']},
                    'counter_explanations': ['市场共同变化', '暴露差异', '样本选择', '资料或执行缺口'],
                    'decision': '证据不足；维持现行方法，候选尚未运行', 'observations': []}
                pid = draft_proposal(store, source='selfcheck', kind='PROMPT', target=c['route'],
                    title=c['route'] + ' · ' + candidate['title'], payload=payload, at=at, dedupe_key=key)
                register(store, pid, spec, at)
                used += 1
            # Lifecycle decisions and frozen proposal contents remain untouched.
            store.db.execute('INSERT INTO selfcheck_evidence VALUES(?,?,?,?,?)',
                             (pid, fingerprint, report['id'], at, encode({'ids': candidate['evidence_ids'], 'signatures': signatures})))
            results.append({'proposal_id': pid, 'action': 'EVIDENCE_APPENDED' if row else 'CREATED',
                            'status': row['status'] if row else 'DRAFT'})
        store.db.commit()
    except BaseException:
        store.db.rollback()
        raise
    return {'week': week, 'budget': WEEKLY_BUDGET, 'used': used, 'updates': results}


def markdown(report):
    lines = ['# 长期自检报告', '', f"生成：{report['created_at']}；编号：{report['id']}。",
        '', '**结论：维持现行方法。以下为诊断与待验证假设，未运行候选实验，未修改生产策略。**',
        '', '## 数据与判断', '', '注册及评分：' + encode(report['counts']),
        '来源性质：' + encode(report['provenance']), '可检验性缺口：' + encode(report['gaps']),
        '', '## 到期表现分组', '', '| 路线 | 版本 | 环境 | 期限 | 行动 | 来源 | 样本/时间簇 | 平均超额 bps |',
        '|---|---|---|---|---|---|---|---|']
    for c in report['cohorts']:
        s = c['excess']
        lines.append(f"| {c['route']} | {c['build_id']} | {c['environment']} | {c['horizon_days']} | {c['stance']} | {c['provenance']} | {s.get('n', 0)}/{s.get('time_clusters', 0)} | {s.get('mean_bps', '—')} |")
    lines += ['', '## 分层诊断', ''] + [f'- {k}：{v}' for k, v in report['layers'].items()]
    execution = report['execution']
    lines += ['', f"执行核验：{execution['status']}；复盘：{execution.get('review_id', '无')}。"]
    lines += [f"- {c['check']}：{c['status']}，已查 {c['checked']}，失败 {c['failures']}。" for c in execution['checks']]
    lines += ['', '## 边界', ''] + ['- ' + text for text in report['limitations']]
    lines += ['', '证据编号、冻结合同哈希及评分见同名 JSON；提案及新增证据用 self-check status / proposals show 查询。']
    return '\n'.join(lines) + '\n'


def run(store, config, at=None, *, generate=False):
    if config.get('deployment_role') == 'cloud':
        return {'status': 'NOT_APPLICABLE', 'reason': '长期自检只在本机研究端运行'}
    at = normalize_time(at or now())
    backfill(store, config, at)
    content, rows = analyze(store, at)
    fingerprint = digest(encode(content))
    identity = 'SC-' + fingerprint[:20]
    with store.db:
        store.db.execute('INSERT OR IGNORE INTO selfcheck_runs VALUES(?,?,?,?)',
            (identity, at, fingerprint, encode({**content, 'id': identity, 'created_at': at})))
    report = json.loads(store.db.execute('SELECT payload_json FROM selfcheck_runs WHERE id=?', (identity,)).fetchone()[0])
    folder = store.root / 'workflow' / 'selfcheck'
    # Regenerating from frozen DB payload repairs an interrupted file write.
    json_write(folder / (identity + '.json'), report)
    (folder / (identity + '.md')).write_text(markdown(report), encoding='utf-8')
    candidates = propose(store, report, rows, at) if generate else None
    summary = {'status': 'SUCCEEDED', 'id': identity, 'at': at, 'report': str((folder / (identity + '.md')).relative_to(store.root)),
               'counts': report['counts'], 'gaps': report['gaps'], 'candidates': candidates}
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO service_state VALUES('selfcheck_last',?)", (encode(summary),))
    return summary


def status(store, identity=None):
    if identity:
        row = store.db.execute('SELECT payload_json FROM selfcheck_runs WHERE id=?', (identity,)).fetchone()
        if not row:raise ValueError('未找到自检报告')
        return json.loads(row[0])
    last = store.db.execute("SELECT value FROM service_state WHERE key='selfcheck_last'").fetchone()
    candidates = [dict(r) for r in store.db.execute("SELECT id,title,status,created_at FROM strategy_proposals WHERE source='selfcheck' ORDER BY created_at DESC LIMIT 30")]
    for c in candidates:
        c['evidence_versions'] = store.db.execute('SELECT count(*) FROM selfcheck_evidence WHERE proposal_id=?', (c['id'],)).fetchone()[0]
    return {'last': json.loads(last[0]) if last else None, 'candidates': candidates, 'weekly_budget': WEEKLY_BUDGET,
            'experiment_runner': 'NOT_IMPLEMENTED', 'production_changes': False}
