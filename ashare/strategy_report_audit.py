"""Read-only, deterministic report measures; never an investment scoring model."""
from collections import Counter


ACTIONS = ('ALLOW', 'HOLD', 'PAUSE', 'REDUCE', 'EXIT')
PASSIVE = ('HOLD', 'PAUSE')
ACCOUNT_KEYS = ('cash_cents', 'equity_cents', 'available_cents', 'reserved_cents',
                'withdrawal_reserved_cents', 'market_value_cents')
CONSTRAINT_KEYS = ('valuation_ready', 'halted', 'gross_cap_bps', 'symbol_cap_bps')
BASIS_KEYS = ('current_bps', 'current_value_cents', 'qty', 'cost_cents', 'max_bps',
              'can_increase', 'source_id', 'source_valid_until', 'source_kind', 'recorded_blockers')
ADJUSTMENT_LABELS = {
    'ALLOW_ELIGIBILITY_LOST': '发布时增加资格失效',
    'ALLOW_TARGET_REACHED': '发布时当前占比已达到模型目标',
    'VALUATION_UNAVAILABLE': '发布时估值不可用',
    'ACCOUNT_HALTED': '发布时账户处于停买状态',
    'REDUCE_MINIMUM_GAP_LOST': '发布时减仓幅度不足原有最小幅度',
    'HOLD_PAUSE_CURRENT_REBASE': '保持/暂停目标按发布时当前占比重置',
}


def _pick(value, keys):
    return {k: value[k] for k in keys if isinstance(value, dict) and k in value}


def project_publication_audit(value):
    """Explicit nested allowlist: do not export full packets or model responses."""
    result = _pick(value, ('version', 'target_method', 'analysis_as_of', 'published_at'))
    if not isinstance(value, dict):
        return result
    for key in ('analysis_account', 'publication_account'):
        if isinstance(value.get(key), dict):
            result[key] = _pick(value[key], ACCOUNT_KEYS)
    for key in ('analysis_constraints', 'publication_constraints'):
        if isinstance(value.get(key), dict):
            result[key] = _pick(value[key], CONSTRAINT_KEYS)
    if isinstance(value.get('validation'), dict):
        result['validation'] = _pick(value['validation'], ('analysis', 'publication'))
    if isinstance(value.get('decisions'), list):
        result['decisions'] = []
        for item in value['decisions']:
            if not isinstance(item, dict):
                continue
            row = _pick(item, ('key', 'model_action', 'model_target_bps', 'published_action',
                               'published_target_bps', 'adjustment_codes'))
            for key in ('analysis_basis', 'publication_basis'):
                if isinstance(item.get(key), dict):
                    row[key] = _pick(item[key], BASIS_KEYS)
            if 'condition_audit' in item:
                from .condition_audit import project
                row['condition_audit'] = project(item['condition_audit'])
            result['decisions'].append(row)
    return result


def _at(value):
    from .strategy_export import _time
    try:
        return _time(value)
    except (ValueError, TypeError, OverflowError):
        return None


def guidance_summary(records, start, end):
    """Counts retained guidance metadata, separately from program rules/proposals."""
    if records is None:
        return {'available': False}
    start, end = _at(start), _at(end)
    counts = dict.fromkeys(('retained_total', 'adopted_by_cutoff', 'active_at_cutoff',
                           'adopted_in_window', 'retired_in_window', 'unclassifiable', 'after_cutoff'), 0)
    for row in records:
        counts['retained_total'] += 1
        adopted, retired = _at(row.get('adopted_at')), _at(row.get('retired_at'))
        if adopted is None:
            counts['unclassifiable'] += 1
            continue
        if adopted > end:
            counts['after_cutoff'] += 1
            continue
        counts['adopted_by_cutoff'] += 1
        counts['adopted_in_window'] += start <= adopted <= end
        counts['retired_in_window'] += retired is not None and start <= retired <= end
        if retired is not None:
            counts['active_at_cutoff'] += retired > end
        elif row.get('status') == 'ADOPTED' and not row.get('retired_at'):
            counts['active_at_cutoff'] += 1
        else:
            counts['unclassifiable'] += 1
    return {'available': True, **counts}


def _same_audit(left, right):
    # JSON equality must distinguish true from 1, including in damaged records.
    from .strategy_export import _json
    return _json(left) == _json(right)


def _condition_digest(receipt):
    import hashlib
    from .strategy_export import _json
    return hashlib.sha256(_json(receipt).encode('utf-8')).hexdigest()


def _expand_conditions(audit, data=None):
    """Resolve a whole audit, so returned rows and their parent stay identical."""
    if not isinstance(audit, dict) or not isinstance(audit.get('decisions'), list):
        return audit, None
    index = (data or {}).get('coverage', {}).get('condition_receipts', {})
    rows = []
    for row in audit['decisions']:
        if not isinstance(row, dict) or 'condition_audit_ref' not in row:
            rows.append(row)
            continue
        reference = row['condition_audit_ref']
        if not isinstance(reference, str) or not isinstance(index, dict) or reference not in index:
            return None, '条件审计引用对应的集中记录缺失。'
        receipt = index[reference]
        try:
            matching = reference == _condition_digest(receipt)
        except (TypeError, ValueError, OverflowError):
            matching = False
        if not matching:
            return None, '条件审计引用与内容摘要不匹配。'
        if 'condition_audit' in row and not _same_audit(row['condition_audit'], receipt):
            return None, '内嵌条件审计与集中记录冲突。'
        expanded = dict(row)
        del expanded['condition_audit_ref']
        expanded['condition_audit'] = receipt
        rows.append(expanded)
    return {**audit, 'decisions': rows}, None


def _publication_audit(decision, data=None):
    """Resolve only this decision's receipt; never fall back across identities."""
    payload = decision.get('payload', {})
    identity = decision.get('record', {}).get('id')
    index = (data or {}).get('coverage', {}).get('publication_audits', {})
    has_inline = 'publication_audit' in payload
    if 'publication_audit_ref' in payload:
        reference = payload['publication_audit_ref']
        if not isinstance(reference, str) or not reference or reference != identity:
            return None, '审计引用与本组合决定编号不一致。'
        if not isinstance(index, dict) or reference not in index:
            return None, '审计引用对应的集中记录缺失。'
        receipt, problem = _expand_conditions(index[reference], data)
        if problem:
            return None, problem
        if has_inline:
            inline, problem = _expand_conditions(payload['publication_audit'], data)
            if problem:
                return None, problem
            if not _same_audit(inline, receipt):
                return None, '内嵌审计与集中记录冲突。'
        return receipt, None
    if has_inline:
        inline, problem = _expand_conditions(payload['publication_audit'], data)
        if problem:
            return None, problem
        if isinstance(index, dict) and isinstance(identity, str) and identity in index:
            receipt, problem = _expand_conditions(index[identity], data)
            if problem:
                return None, problem
            if not _same_audit(inline, receipt):
                return None, '内嵌审计与集中记录冲突。'
        return inline, None
    return None, None


def export_projection(data):
    """Group complete receipts for ZIP compression, without changing input data.

    Only the exported JSON layout changes. The same decision appearing as both
    comparison baselines shares one receipt; conflicting copies fail explicitly.
    Existing indexed exports are accepted and orphan index entries are retained.
    """
    coverage = dict(data.get('coverage', {}))
    existing = coverage.get('publication_audits', {})
    if not isinstance(existing, dict):
        raise ValueError('集中审计索引格式异常，未丢弃任何记录')
    conditions = coverage.get('condition_receipts', {})
    if not isinstance(conditions, dict):
        raise ValueError('条件审计索引格式异常，未丢弃任何记录')
    condition_index = dict(conditions)
    for reference, receipt in condition_index.items():
        if not isinstance(reference, str) or reference != _condition_digest(receipt):
            raise ValueError('条件审计索引内容摘要不匹配，未丢弃任何记录')
    resolving = {**data, 'coverage': {**coverage, 'condition_receipts': condition_index}}

    def compact_conditions(receipt):
        expanded, problem = _expand_conditions(receipt, resolving)
        if problem:
            raise ValueError(problem + '未丢弃任何记录')
        if not isinstance(expanded, dict) or not isinstance(expanded.get('decisions'), list):
            return expanded
        rows = []
        for row in expanded['decisions']:
            if not isinstance(row, dict) or 'condition_audit' not in row:
                rows.append(row)
                continue
            value = row['condition_audit']
            reference = _condition_digest(value)
            if reference in condition_index and not _same_audit(condition_index[reference], value):
                raise ValueError('条件审计内容摘要冲突，未丢弃任何记录')
            condition_index[reference] = value
            compact = dict(row)
            del compact['condition_audit']
            compact['condition_audit_ref'] = reference
            rows.append(compact)
        return {**expanded, 'decisions': rows}

    index = {identity: compact_conditions(receipt) for identity, receipt in existing.items()}
    versions = [d for d in (data.get('baseline'), data.get('published_baseline'), *data.get('decisions', [])) if d]
    for decision in versions:
        payload = decision.get('payload', {})
        if 'publication_audit' not in payload:
            continue
        identity = decision.get('record', {}).get('id')
        if not isinstance(identity, str) or not identity:
            raise ValueError('审计记录缺少组合决定编号，未丢弃任何记录')
        receipt = compact_conditions(payload['publication_audit'])
        if identity in index and not _same_audit(index[identity], receipt):
            raise ValueError('同一组合决定的审计记录冲突，未丢弃任何记录')
        index[identity] = receipt
    if index or 'publication_audits' in coverage:
        coverage['publication_audits'] = index
    if condition_index or 'condition_receipts' in coverage:
        coverage['condition_receipts'] = condition_index
    result = {**data, 'coverage': coverage}

    def projected(decision):
        if not decision:
            return decision
        payload = decision.get('payload', {})
        if 'publication_audit' not in payload and 'publication_audit_ref' not in payload:
            return decision
        _, problem = _publication_audit(decision, result)
        if problem:
            raise ValueError(problem + '未丢弃任何记录')
        replacement = dict(payload)
        if 'publication_audit' in replacement:
            del replacement['publication_audit']
            replacement['publication_audit_ref'] = decision['record']['id']
        return {**decision, 'payload': replacement}

    for key in ('baseline', 'published_baseline'):
        if key in data:
            result[key] = projected(data[key])
    result['decisions'] = [projected(d) for d in data.get('decisions', [])]
    return result


def publication_item(decision, key, data=None):
    audit, problem = _publication_audit(decision, data)
    if problem:
        return None, None
    if not isinstance(audit, dict) or type(audit.get('version')) is not int or audit['version'] != 1:
        return None, None
    rows = audit.get('decisions')
    matches = [item for item in rows if isinstance(item, dict) and item.get('key') == key] if isinstance(rows, list) else []
    return (audit, matches[0]) if len(matches) == 1 else (audit, None)


def publication_complete(audit, row):
    if not audit or not row or audit.get('target_method') != 'MODEL_DISCRETION_WITH_HARD_CONSTRAINTS':
        return False
    if any(_at(audit.get(k)) is None for k in ('analysis_as_of', 'published_at')):
        return False
    if any(not isinstance(audit.get(k), dict) or any(type(audit[k].get(f)) is not int for f in ACCOUNT_KEYS)
           for k in ('analysis_account', 'publication_account')):
        return False
    for key in ('analysis_constraints', 'publication_constraints'):
        value = audit.get(key, {})
        if not isinstance(value, dict) or any(type(value.get(f)) is not bool for f in ('valuation_ready', 'halted')) or any(type(value.get(f)) is not int for f in ('gross_cap_bps', 'symbol_cap_bps')):
            return False
    validation = audit.get('validation', {})
    if not isinstance(validation, dict) or any(validation.get(k) != 'PASSED' for k in ('analysis', 'publication')):
        return False
    if any(row.get(k) not in ACTIONS for k in ('model_action', 'published_action')) or any(type(row.get(k)) is not int for k in ('model_target_bps', 'published_target_bps')):
        return False
    for key in ('analysis_basis', 'publication_basis'):
        basis = row.get(key, {})
        if not isinstance(basis, dict) or any(type(basis.get(k)) is not int for k in ('current_bps', 'current_value_cents', 'qty', 'cost_cents', 'max_bps')) or type(basis.get('can_increase')) is not bool:
            return False
        if any(k not in basis for k in ('source_id', 'source_valid_until', 'source_kind', 'recorded_blockers')):
            return False
        if any(basis[k] is not None and not isinstance(basis[k], str) for k in ('source_id', 'source_valid_until', 'source_kind')):
            return False
        if not isinstance(basis['recorded_blockers'], list) or any(not isinstance(v, str) for v in basis['recorded_blockers']):
            return False
    return isinstance(row.get('adjustment_codes'), list) and all(isinstance(c, str) and c in ADJUSTMENT_LABELS for c in row['adjustment_codes'])


def _held(item):
    qty = item.get('qty')
    return 'held' if type(qty) in (int, float) and qty > 0 else 'unheld' if type(qty) in (int, float) and qty == 0 else 'unknown'


def _eligible(item):
    return 'true' if item.get('can_increase') is True else 'false' if item.get('can_increase') is False else 'unknown'


def summarize(data):
    from .strategy_export import _frozen_source
    versions = data['decisions']
    metrics = dict.fromkeys(('item_occurrences', 'comparable_action_pairs', 'action_changes',
                            'unpaired_current_items', 'added_items', 'removed_items',
                            'active_target_changes', 'passive_target_follow_changes', 'unclassified_target_changes',
                            'passive_target_reset_changes',
                            'passive_target_equal_current', 'passive_target_inconsistent',
                            'active_action_items', 'source_references', 'items_without_source_id',
                            'publication_audit_items', 'incomplete_publication_audit_items', 'items_without_publication_audit',
                            'source_references_without_token'), 0)
    actions, allow_matrix, holding, eligibility, action_matrix = Counter(), Counter(), Counter(), Counter(), Counter()
    sources = set()
    previous = data.get('baseline')
    for decision in versions:
        items = decision.get('payload', {}).get('decisions', [])
        prior = {i['key']: i for i in previous.get('payload', {}).get('decisions', [])} if previous and not previous.get('damaged') else None
        current = {i['key']: i for i in items}
        if prior is not None and not decision.get('damaged'):
            metrics['removed_items'] += len(set(prior) - set(current))
            metrics['added_items'] += len(set(current) - set(prior))
        for item in items:
            metrics['item_occurrences'] += 1
            action = item.get('action')
            actions[action if action in ACTIONS else 'UNKNOWN'] += 1
            action_matrix[(action if action in ACTIONS else 'UNKNOWN') + '/' + _eligible(item) + '/' + _held(item)] += 1
            holding[_held(item)] += 1
            eligibility[_eligible(item)] += 1
            if action == 'ALLOW':
                allow_matrix[_held(item) + '/' + _eligible(item)] += 1
            if action in PASSIVE:
                field = 'passive_target_equal_current' if type(item.get('target_bps')) is int and item.get('target_bps') == item.get('current_bps') else 'passive_target_inconsistent'
                metrics[field] += 1
            elif action in ('ALLOW', 'REDUCE', 'EXIT'):
                metrics['active_action_items'] += 1
            if isinstance(item.get('source_id'), str) and item['source_id']:
                metrics['source_references'] += 1
                token = item.get('source_token') if isinstance(item.get('source_token'), str) else None
                metrics['source_references_without_token'] += not bool(token)
                sources.add((item['key'], item['source_id'], token))
            else:
                metrics['items_without_source_id'] += 1
            audit, audit_item = publication_item(decision, item['key'], data)
            metrics['publication_audit_items' if publication_complete(audit, audit_item) else
                    'incomplete_publication_audit_items' if audit_item is not None else 'items_without_publication_audit'] += 1
            before = prior.get(item['key']) if prior is not None and not decision.get('damaged') else None
            if not before or before.get('action') not in ACTIONS or action not in ACTIONS:
                metrics['unpaired_current_items'] += 1
                continue
            metrics['comparable_action_pairs'] += 1
            metrics['action_changes'] += before['action'] != action
            if type(before.get('target_bps')) is int and type(item.get('target_bps')) is int and before['target_bps'] != item['target_bps']:
                field = ('passive_target_follow_changes' if action in PASSIVE and before['action'] == action
                         and item['target_bps'] == item.get('current_bps') and before['target_bps'] == before.get('current_bps')
                         else 'passive_target_reset_changes' if action in PASSIVE and item['target_bps'] == item.get('current_bps')
                         else 'active_target_changes' if action in ('ALLOW', 'REDUCE', 'EXIT') else 'unclassified_target_changes')
                metrics[field] += 1
        previous = decision
    metrics.update(rounds=len(versions), acknowledged_rounds=len(data.get('published_decision_ids', [])),
                   acknowledged_changed_rounds=sum('action_target_change' in (d.get('acknowledged_change') or {}).get('categories', []) for d in versions),
                   action_counts=dict(actions), holdings=dict(holding), eligibility=dict(eligibility),
                   allow_matrix=dict(allow_matrix), action_eligibility_holding=dict(action_matrix), distinct_source_versions=len(sources),
                   repeated_source_references=metrics['source_references'] - len(sources))

    # Snapshot metadata and matching individual sources measure different things.
    all_versions = {d['record']['id']: d for d in [data.get('baseline'), data.get('published_baseline'), *versions] if d}
    window_ids = {d['record']['id'] for d in versions}
    boundaries = [_at(r.get('started_at')) for r in data['coverage'].get('source_history', []) if r.get('version') == 'portfolio-source-history-v1']
    snapshots = [d for d in all_versions.values() if isinstance(d.get('frozen_history'), dict)
                 and d['frozen_history'].get('version') == 'portfolio-source-history-v1']
    boundaries += [_at(d['frozen_history'].get('coverage_started_at')) for d in snapshots]
    end = _at(data.get('end_at'))
    boundaries = [b for b in boundaries if b is not None and (end is None or b <= end)]
    boundary = min(boundaries) if boundaries else None
    observed = [_at(d['record'].get('created_at')) for d in snapshots if _at(d['frozen_history'].get('frozen_at')) is not None]
    observed = [at for at in observed if at is not None and (end is None or at <= end)]
    first_observed = min(observed) if observed else None
    frozen = dict.fromkeys(('window_metadata_present', 'window_legacy_metadata_missing', 'window_new_metadata_missing',
                           'window_unclassified_metadata_missing', 'baseline_metadata_missing',
                           'window_items', 'window_matching_sources', 'window_missing_source_id',
                           'window_source_unmatched', 'window_metadata_present_source_unmatched'), 0)
    for identity, decision in all_versions.items():
        within = identity in window_ids
        has_metadata = decision.get('frozen_history') is not None
        if within and has_metadata:
            frozen['window_metadata_present'] += 1
        elif not has_metadata:
            at = _at(decision['record'].get('created_at'))
            field = ('baseline_metadata_missing' if not within else 'window_unclassified_metadata_missing'
                     if boundary is None or at is None else 'window_legacy_metadata_missing'
                     if at < boundary else 'window_new_metadata_missing')
            frozen[field] += 1
        if within:
            for item in decision.get('payload', {}).get('decisions', []):
                frozen['window_items'] += 1
                if _frozen_source(data, decision, item)[1] is not None:
                    frozen['window_matching_sources'] += 1
                elif not item.get('source_id'):
                    frozen['window_missing_source_id'] += 1
                else:
                    frozen['window_source_unmatched'] += 1
                    frozen['window_metadata_present_source_unmatched'] += has_metadata
    frozen['classification_boundary'] = boundary.isoformat() if boundary else None
    frozen['first_observed_frozen_decision'] = first_observed.isoformat() if first_observed else None
    return {'version': 1, 'activity': metrics, 'frozen': frozen}


def render_summary(data):
    from .strategy_export import _local
    audit = data['coverage'].get('report_audit') or summarize(data)
    m, f = audit['activity'], audit['frozen']
    denominator = m['comparable_action_pairs']
    rate = f'{m["action_changes"] / denominator:.2%}' if denominator else '不可计算（分母为0）'
    round_rate = f'{m["acknowledged_changed_rounds"] / m["acknowledged_rounds"]:.2%}' if m['acknowledged_rounds'] else '不可计算（分母为0）'
    lines = ['## 决策活动与统计口径', '',
             f'组合轮次 {m["rounds"]} 次（确认发布 {m["acknowledged_rounds"]} 次）；标的项 {m["item_occurrences"]} 项次。',
             '一轮是一份组合决定；标的项是该轮中的一个路线/标的键，同一股票跨路线或跨轮分别计数，不能作为独立投资样本数。', '',
             f'**已确认发布轮次的动作或主动目标变化占比：{m["acknowledged_changed_rounds"]} / {m["acknowledged_rounds"]} = {round_rate}。** 分母是全部确认发布轮次，分子是原比较记录标记 action_target_change 的轮次；这是变化轮次占比，缺少比较基线的轮次不据此认定无变化。',
             f'**同键配对动作变化率：{m["action_changes"]} / {denominator} = {rate}。** 分母为相邻生成轮次中同键、动作可读的标的配对，窗口首轮使用窗口前生成基线；分子只数动作枚举发生变化。',
             f'未配对项 {m["unpaired_current_items"]} 项次；可比较轮次中的新增项 {m["added_items"]}、移出项 {m["removed_items"]}，均不进入动作变化率分母。此数不是研究转交易率、准入通过率或成交率。', '',
             '| 最终动作 | 项次 |', '| --- | ---: |']
    lines += [f'| {action} | {m["action_counts"].get(action, 0)} |' for action in ACTIONS]
    if m['action_counts'].get('UNKNOWN'):
        lines.append('| 动作不可判定 | ' + str(m['action_counts']['UNKNOWN']) + ' |')
    lines += ['', 'ALLOW 仅指该轮最终允许增加的动作；can_increase 是当时记录的增加资格，两者均不证明已经成交。', '',
              '| can_increase=true 时的最终动作 | 已持仓 | 未持仓 | 持仓不可判定 |', '| --- | ---: | ---: | ---: |']
    for action in ACTIONS:
        lines.append('| ' + action + ' | ' + ' | '.join(str(m['action_eligibility_holding'].get(action + '/true/' + h, 0)) for h in ('held', 'unheld', 'unknown')) + ' |')
    lines += ['', '有增加资格的重复项仍可能被模型选择HOLD或PAUSE，不能把它们直接称为待买机会。', '']
    if m['action_counts'].get('ALLOW'):
        lines += ['| ALLOW 的持仓情况 | can_increase=true | false | 未记录 |', '| --- | ---: | ---: | ---: |']
        for state, label in (('held', '已有持仓（qty>0）'), ('unheld', '未持仓（qty=0）'), ('unknown', '持仓数量不可判定')):
            lines.append('| ' + label + ' | ' + ' | '.join(str(m['allow_matrix'].get(state + '/' + e, 0)) for e in ('true', 'false', 'unknown')) + ' |')
    else:
        lines += ['最终ALLOW为0项次，ALLOW的资格/持仓交叉项亦为0。']
    lines += ['', f'全部标的项：持仓 {m["holdings"].get("held", 0)}、未持仓 {m["holdings"].get("unheld", 0)}、不可判定 {m["holdings"].get("unknown", 0)}；增加资格 true {m["eligibility"].get("true", 0)}、false {m["eligibility"].get("false", 0)}、未记录 {m["eligibility"].get("unknown", 0)}。',
              f'来源版本：{m["source_references"]} 次引用、{m["distinct_source_versions"]} 个不同的（路线标的键、来源编号、source_token）组合、重复引用 {m["repeated_source_references"]} 次；来源编号缺失 {m["items_without_source_id"]} 项次。',
              f'其中来源token未记录 {m["source_references_without_token"]} 项次，仅可按来源编号分组。这里统计来源版本复用，不把重复引用解释为新增研究，也不据此判断研究内容是否相同。', '',
              '## 主动目标与被动占比随动', '',
              f'ALLOW/REDUCE/EXIT 主动动作 {m["active_action_items"]} 项次；HOLD/PAUSE 且目标等于当时当前占比 {m["passive_target_equal_current"]} 项次；不满足该等式或数值缺失 {m["passive_target_inconsistent"]} 项次。',
              f'在可比较配对中：最终为主动动作的目标数值变化 {m["active_target_changes"]} 次；前后同一HOLD/PAUSE且两轮目标均等于当前占比的随动变化 {m["passive_target_follow_changes"]} 次；动作切换等情况下进入被动目标的重设 {m["passive_target_reset_changes"]} 次；其他不可归类目标变化 {m["unclassified_target_changes"]} 次。',
              '被动占比变化可能来自估值/净值变化，不自动代表主动调仓；没有成交与估值流水时，不反推具体原因。历史理由保持原文，实际动作与目标以该版本结构化 action、target_bps、current_bps 为准；文字与数字冲突不通过改写历史理由消除。', '',
              '## 研究资格、模型选择、校验与执行', '',
              '研究与程序准入先形成候选及 can_increase；组合模型选择 action/target_bps 并说明理由；分析时与发布时分别受硬约束校验，发布时可能收紧为保持或暂停；执行层仍须逐次检查价格、时效、现金与风险条件。',
              '平台没有确定的“研究分数→主动仓位”公式或统一研究得分阈值。主动目标由模型在硬约束内选择；不能从理由文字猜出阈值。HOLD/PAUSE 的确定性规则是目标等于发布时当前占比。',
              '| 环节/动作 | 已有合法性条件 |', '| --- | --- |',
              '| 研究资格 | 公司研究WATCH仅表示可观察；仍须有效计划、研究成员资格与原生准入限制允许，can_increase才可能为true |',
              '| ALLOW | can_increase=true，current_bps < target_bps ≤ max_bps，且估值可用、账户未停买 |',
              '| HOLD / PAUSE | target_bps = current_bps；均不再买入 |',
              '| REDUCE / EXIT | 需已有持仓及可用估值；REDUCE目标至少低1个百分点且大于0，EXIT目标为0 |',
              '| 共同约束与执行 | 总仓位、跨路线同标的与风险组预算须通过；执行时仍核对计划时效、价格、现金及原生风险限制 |', '',
              '这些是动作/目标的合法性条件，不是模型必须选择该动作或某个主动仓位的充分条件。',
              f'模型原动作及发布校验审计：{m["publication_audit_items"]} 项次字段完整、{m["incomplete_publication_audit_items"]} 项次存在但不完整、{m["items_without_publication_audit"]} 项次未留存或版本不支持；旧记录只能核对最终结构化结果，不能反推模型原始目标或当时重置原因。',
              '本报告没有模拟成交明细，因此这里只能串联到授权与发布；不能计算成交转化率或声称ALLOW已执行。', '',
              '## 冻结来源完整性', '',
              f'窗口内 {m["rounds"]} 轮：冻结记录存在 {f["window_metadata_present"]}；最早可证起点前遗留缺口 {f["window_legacy_metadata_missing"]}；起点后新增缺口 {f["window_new_metadata_missing"]}；起点不可判定缺口 {f["window_unclassified_metadata_missing"]}。窗口前基线缺口另计 {f["baseline_metadata_missing"]}。',
              '最早可证覆盖起点：' + _local(f['classification_boundary']) + '；档案首次观察到的冻结决定：' + _local(f['first_observed_frozen_decision']) + '。仅认可已知版本中明确记录的覆盖起点；首次成功冻结不单独证明此前未启用，起点未知时缺口不强分遗留/新增。这里不证明历史连续完整。',
              f'窗口内逐来源核对：{f["window_items"]} 项次中匹配 {f["window_matching_sources"]}；有来源编号但冻结来源不匹配/缺失 {f["window_source_unmatched"]}（其中冻结记录存在仍不匹配 {f["window_metadata_present_source_unmatched"]}）；决定本身未记录来源编号 {f["window_missing_source_id"]}。',
              '冻结记录存在与逐来源的路线、编号、代码、时间匹配是两个指标。历史缺口保留，当前研究不能补写当时冻结证据；原始issues仍在下方列出。', '',
              '## 研究指导规则与程序规则', '']
    g = data['coverage'].get('guidance_summary', {})
    if g.get('available'):
        lines += [f'研究指导表累计留存 {g["retained_total"]} 条；截至报告时点已采纳 {g["adopted_by_cutoff"]} 条，其中按采纳/退役时间判定仍适用 {g["active_at_cutoff"]} 条；窗口内新采纳 {g["adopted_in_window"]}、退役 {g["retired_in_window"]} 条。',
                  f'无法可靠归类 {g["unclassifiable"]} 条；采纳时点晚于报告截止 {g["after_cutoff"]} 条。']
    else:
        lines += ['该旧档案或数据库未提供完整规则表统计；不能把缺失当作0。']
    lines += ['上述仅统计独立研究指导 strategy_guidance，不包括写在程序中的交易/风控规则，也不等于所有已采纳提案数。后文“已记录研究规则”的版本数是本报告选入数量（窗口内采纳＋当前适用基准），不是全库规则总数。', '']
    from .strategy_condition_report import render_summary as render_conditions
    return lines + render_conditions(data)


def render_publication_item(decision, item, data=None):
    from .strategy_export import _safe_text, _local, _weight, _details
    audit, row = publication_item(decision, item.get('key'), data)
    lines = ['##### 模型选择与发布校验', '']
    _, problem = _publication_audit(decision, data)
    if problem:
        return lines + ['**' + problem + '未采用其他决定的审计记录。**', '']
    if row is None:
        return lines + ['未留存此标的的模型原动作/原目标与发布时校验审计；以下最终结构化值不能代替缺失的模型原输出。', '']
    if not publication_complete(audit, row):
        lines += ['**新版审计记录存在但字段不完整，以下只列已记录值，不视为完整校验证据。**', '']
    lines += ['分析依据时间：' + _local(audit.get('analysis_as_of')) + '；发布时点：' + _local(audit.get('published_at')) + '。',
              '模型原动作 / 目标：' + _safe_text(row.get('model_action')) + ' / ' + _weight(row.get('model_target_bps')) + '。',
              '审计记录的发布动作 / 目标：' + _safe_text(row.get('published_action')) + ' / ' + _weight(row.get('published_target_bps')) + '。',
              '主动目标采用模型裁量并受硬约束；没有固定研究评分或主动仓位推导公式。', '']
    lines += ['本节账户净值、持仓估值与成本的金额单位均为人民币分；全球资产的研究价格仍按其美元价格字段展示。', '']
    if item.get('route') == 'global':
        lines += ['全球路线的 qty 是账本缩放整数，每100,000,000单位代表1个实际资产单位（按标的为股、枚或盎司），不是直接的股数。', '']
    for label, key in (('分析时资格与仓位', 'analysis_basis'), ('发布时资格与仓位', 'publication_basis')):
        lines += ['**' + label + '**', ''] + _details(row.get(key, {})) + ['']
        qty = row.get(key, {}).get('qty') if isinstance(row.get(key), dict) else None
        if item.get('route') == 'global' and type(qty) is int:
            from decimal import Decimal
            converted = format(Decimal(qty) / 100_000_000, 'f').rstrip('0').rstrip('.') if qty % 100_000_000 else str(qty // 100_000_000)
            lines += ['实际资产数量：' + converted + '。', '']
    lines += ['当前占比的程序计算：ceil(持仓估值 × 10000 / max(1, 账户净值))，单位为基点；100基点=1%。', '']
    for label, basis_key, account_key in (('分析时', 'analysis_basis', 'analysis_account'), ('发布时', 'publication_basis', 'publication_account')):
        basis, account = row.get(basis_key, {}), audit.get(account_key, {})
        basis = basis if isinstance(basis, dict) else {}
        account = account if isinstance(account, dict) else {}
        equity, value = account.get('equity_cents'), basis.get('current_value_cents')
        calculated = (value * 10000 + max(1, equity) - 1) // max(1, equity) if type(equity) is int and type(value) is int else None
        lines += [label + '：净值（人民币分）' + _safe_text(equity) + '，持仓估值（人民币分）' + _safe_text(value)
                  + '，公式复算占比 ' + _weight(calculated) + '，原记录占比 ' + _weight(basis.get('current_bps')) + '。', '']
    lines += ['recorded_blockers 只列原候选已记录的限制，不是 can_increase=false 原因的穷尽清单。', '',
              '校验结果：'] + _details(audit.get('validation', {})) + ['']
    codes = row.get('adjustment_codes')
    lines += ['发布调整：' + ('；'.join(_safe_text(ADJUSTMENT_LABELS.get(code, code)) if isinstance(code, str) else '格式异常的调整码' for code in codes)
                             if isinstance(codes, list) and codes else '未记录调整码；不能据此补推未记录原因。'), '']
    if row.get('published_action') != item.get('action') or row.get('published_target_bps') != item.get('target_bps'):
        lines += ['**审计发布值与最终结构化项不一致，请人工核对；本报告保留两者，不改写原记录。**', '']
    return lines
