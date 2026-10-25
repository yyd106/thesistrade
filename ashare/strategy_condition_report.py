"""Administrator report projections; no model inputs, execution or record writes."""
from collections import Counter


STATUS_LABELS = {
    'CONSISTENT': '可识别的仓位数字一致',
    'INCONSISTENT': '与分析时结构化数字不一致',
    'SUSPECTED_MISMATCH': '疑似不一致（缺少模型分析时点记录）',
    'EXPLAINED_REBASE': '数字差异有发布重设记录解释',
    'UNKNOWN': '未能可靠核对',
}
RESULT_LABELS = {'PASS': '通过', 'FAIL': '不通过', 'UNKNOWN': '无法判断',
                 'NOT_APPLICABLE': '不适用'}
ROLE_LABELS = {'CURRENT': '当前仓位', 'TARGET': '目标仓位', 'REDUCTION_PP': '减仓幅度'}
CATEGORY_LABELS = {'LEGALITY': '格式合法性', 'AUTHORIZATION': '准入阻断', 'TARGET': '目标一致性',
                   'PORTFOLIO': '组合约束', 'PUBLICATION_TIGHTENING': '发布收紧',
                   'PROGRAM_ASSIGNMENT': '被动目标重设'}
REFERENCE_LABELS = {'ANALYSIS_CURRENT': '分析时当前仓位', 'MODEL_TARGET': '模型原目标',
                    'ANALYSIS_REDUCTION': '分析时减仓幅度', 'FINAL_CURRENT': '最终记录当前仓位',
                    'FINAL_TARGET': '最终记录目标', 'FINAL_REDUCTION': '最终记录减仓幅度'}


def inspect_item(decision, item, data=None):
    from . import condition_audit, strategy_numeric_audit
    from .strategy_report_audit import publication_item, publication_complete
    audit, row = publication_item(decision, item.get('key'), data)
    receipt = row.get('condition_audit') if isinstance(row, dict) else None
    if audit is None and any(k in decision.get('payload', {}) for k in ('publication_audit', 'publication_audit_ref')):
        # A damaged reference is not an old record with no receipt.
        audit, receipt = {}, {}
    complete = publication_complete(audit, row)
    linked = (complete and row.get('published_action') == item.get('action')
              and all(type(item.get(k)) is int for k in ('target_bps', 'current_bps'))
              and row.get('published_target_bps') == item.get('target_bps')
              and row['publication_basis'].get('current_bps') == item.get('current_bps'))
    valid = bool(linked and condition_audit.validate_receipt(receipt))
    numeric = strategy_numeric_audit.check(item, audit, row)
    return receipt, valid, numeric


def summarize(data):
    from .condition_audit import catalog
    counts, numeric_counts, checks, hits = Counter(), Counter(), Counter(), Counter()
    findings = []
    changes = Counter()
    previous = data.get('baseline')
    for decision in data.get('decisions', []):
        prior = {x.get('key'): x for x in previous.get('payload', {}).get('decisions', [])} if previous and not previous.get('damaged') else {}
        round_changes = set()
        for item in decision.get('payload', {}).get('decisions', []):
            counts['items'] += 1
            receipt, valid, numeric = inspect_item(decision, item, data)
            counts['recorded' if valid else 'incomplete' if receipt is not None else 'missing'] += 1
            # The unchanged model schema declares no structured economic causes.
            counts['model_trigger_not_recorded'] += 1
            if valid:
                for stage in ('analysis', 'publication', 'publication_recheck'):
                    for condition, result in receipt.get(stage, {}).items():
                        checks[(stage, condition, result)] += 1
                for condition in receipt.get('hit_ids', []):
                    hits[condition] += 1
                counts['items_with_program_hits'] += bool(receipt.get('hit_ids'))
            numeric_counts[numeric['status']] += 1
            if numeric.get('comparisons'):
                findings.append({'decision_id': decision.get('record', {}).get('id'),
                                 'key': item.get('key'), **numeric})
            before = prior.get(item.get('key'))
            if before is None or decision.get('damaged'):
                continue
            changes['paired_items'] += 1
            for field in ('evidence_ids', 'related_keys'):
                left, right = before.get(field), item.get(field)
                if not all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in (left, right)):
                    changes[field + '_unknown_pairs'] += 1
                    continue
                for method, changed in (('list', left != right), ('members', set(left) != set(right))):
                    if changed:
                        name = field + '_' + method
                        changes[name + '_items'] += 1
                        round_changes.add(name)
        delta = decision.get('generated_change', {})
        source_changes = sum('source_change' in d.get('categories', []) for d in delta.get('items', []))
        changes['source_change_items'] += source_changes
        changes['source_change_rounds'] += bool(source_changes)
        for name in round_changes:
            changes[name + '_rounds'] += 1
        previous = decision
    return {'version': 1, 'diagnostic_mode': 'POSTHOC_DIAGNOSTIC',
            'diagnostic_as_of': data.get('generated_at'), 'counts': dict(counts),
            'numeric_counts': dict(numeric_counts), 'numeric_findings': findings,
            'program_hit_counts': dict(hits), 'condition_counts': [
                {'stage': s, 'condition_id': c, 'result': r, 'count': n}
                for (s, c, r), n in sorted(checks.items())],
            'field_changes': dict(changes), 'catalog': catalog()}


def render_summary(data):
    summary = data.get('coverage', {}).get('condition_audit') or summarize(data)
    c, n, f = summary['counts'], summary['numeric_counts'], summary['field_changes']
    lines = ['## 条件目录、程序命中与数字一致性', '',
             '条件目录说明现有程序的检查范围，不是新批准的研究规则，也不是“研究评分→动作”的经济映射。',
             '触发类（新证据为何改变研究结论）与终止类（何种事实使命题失效）的模型条件编号尚未记录；不从理由倒推。阻断与一致性类列出已有校验。',
             '分析/发布检查属于同期快照的确定性校验重放；只有原发布分支当场留痕的命中编号，才表示程序实际收紧或重设。执行时价格、现金、心跳和成交结果不在本回执中，不能由发布通过推定。', '',
             f'逐标的条件回执：完整 {c.get("recorded", 0)} 项次；存在但不完整/不匹配 {c.get("incomplete", 0)}；未记录 {c.get("missing", 0)}。其中有程序发布命中 {c.get("items_with_program_hits", 0)} 项次。',
             f'模型结构化触发归因未记录 {c.get("model_trigger_not_recorded", 0)} 项次；这说明归因字段缺失，不等于原研究理由和证据全部缺失。', '',
             '| 数字诊断 | 项次 |', '| --- | ---: |']
    lines += [f'| {label} | {n.get(status, 0)} |' for status, label in STATUS_LABELS.items()]
    if summary['program_hit_counts']:
        lines += ['', '| 程序发布实际命中编号 | 项次 |', '| --- | ---: |']
        lines += [f'| {key} | {value} |' for key, value in sorted(summary['program_hit_counts'].items())]
    lines += ['', '数字诊断在导出时进行，只核对明确的仓位措辞；无法识别不等于一致。旧记录缺少分析时点，差异仅列疑似；HOLD/PAUSE 的合法发布重设单列解释。原文、决定、交易和历史记录均不改写。',
              '逐项数字、原文位置与比较字段见策略数据.json 的 coverage.condition_audit.numeric_findings；逐轮提示见交易策略变更.md。', '',
              '### 变化统计的固定口径', '',
              f'生成路线相邻同键配对 {f.get("paired_items", 0)} 项；首轮包含窗口前生成基线，新增/移出不进入字段配对。各轮次指标可重叠，不能相加。', '',
              '| 字段 | 原列表变化轮次 | 集合成员变化轮次 |', '| --- | ---: | ---: |']
    for field, label in (('evidence_ids', '证据引用'), ('related_keys', '关联项目')):
        lines.append(f'| {label} | {f.get(field + "_list_rounds", 0)} | {f.get(field + "_members_rounds", 0)} |')
    lines += [f'| 来源变化标签（另一口径） | {f.get("source_change_rounds", 0)} | 不适用 |', '',
              '来源变化比较来源编号/token并检查可用冻结内容；缺少冻结内容时，该标签不能证明内容实质新增。引用/关联字段变化也不代表新增事实、有效研究或独立机会。', '',
              '### 程序条件目录（有版本的检查定义）', '',
              '| 条件编号 | 类别 | 说明 | 阈值依据 / 单位 |', '| --- | --- | --- | --- |']
    from .strategy_export import _cell
    for entry in summary['catalog']:
        lines.append('| ' + ' | '.join(_cell(v) for v in (entry.get('id'), CATEGORY_LABELS.get(entry.get('category'), '未分类'),
                     entry.get('description'))) + ' | ' + _cell(str(entry.get('threshold_source', '未记录')) + ' / ' + str(entry.get('unit', '未记录'))) + ' |')
    lines += ['', '逐项 condition_audit_ref 指向 coverage.condition_receipts 中的完整条件回执，读取时校验摘要再展开；旧内嵌 condition_audit 仍兼容。目录版本、各条结果与同一发布回执的 analysis_basis / publication_basis、账户、约束和原风险分组对应。研究信心档位、自动失效卖出和收益准入标准未在本次新增。', '']
    return lines


def render_item(decision, item, data=None, *, compact=False):
    from .strategy_export import _safe_text, _weight
    receipt, valid, numeric = inspect_item(decision, item, data)
    lines = [] if compact else ['##### 条件归因与数字核对', '']
    if valid:
        ids = receipt.get('hit_ids', [])
        hit_text = '、'.join(ids) if ids else '无收紧或重设命中' if receipt.get('hit_attribution') == 'PROGRAM_OBSERVED' else '未记录同期分支回执'
        lines += ['程序发布实际命中：' + hit_text + '。模型经济触发/终止条件编号未记录；执行检查未采集。']
        if not compact:
            lines += ['', '| 校验重放阶段 | 条件编号 | 结果 |', '| --- | --- | --- |']
            for stage, label in (('analysis', '分析'), ('publication', '发布'), ('publication_recheck', '发布前重检')):
                for condition, result in receipt.get(stage, {}).items():
                    lines.append('| ' + label + ' | ' + _safe_text(condition) + ' | ' + RESULT_LABELS.get(result, '无法判断') + ' |')
    else:
        lines += ['条件回执未记录，或现有记录不完整/不匹配；模型触发归因未知，不以今日规则回填。']
    status = numeric['status']
    lines += ['数字核对：' + STATUS_LABELS.get(status, '未能可靠核对') + '（导出时诊断）。']
    for comparison in numeric.get('comparisons', []):
        if compact and comparison.get('status') == 'CONSISTENT':
            continue
        reference = _weight(comparison.get('reference_bps'))
        if comparison.get('role') == 'REDUCTION_PP' and type(comparison.get('reference_bps')) is int:
            reference = f'{comparison["reference_bps"] / 100:g} 个百分点'
        unit = ' 个百分点' if comparison.get('role') == 'REDUCTION_PP' else '%'
        lines.append('- ' + ROLE_LABELS.get(comparison.get('role'), '仓位') + '原文数字：' + _safe_text(comparison.get('mention')) + unit
                     + '；' + REFERENCE_LABELS.get(comparison.get('ref_role'), '比较值') + ' ' + reference
                     + '；' + STATUS_LABELS.get(comparison.get('status'), '未能可靠核对') + '。')
    return lines + ['']
