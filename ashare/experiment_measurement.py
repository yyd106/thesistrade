"""Symmetric output-quality measurement for the first isolated forward trial.

This measures a prediction's explicit, machine-checkable structure and verbatim
citation membership. It does not verify a business claim or measure returns.
Production's schema is copied, never changed.
"""
import copy
import math
from collections import defaultdict
from datetime import datetime

from .calendar import local
from .model import TRADER_SCHEMA, validate_result

VERSION = 'verifiable-structure-v1'
OPERATORS = ('<', '<=', '>', '>=', '==', '!=')
HYPOTHESIS_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'status': {'type': 'string', 'enum': ['TESTABLE', 'UNKNOWN']},
        'claim': {'type': 'string', 'maxLength': 500},
        'metric': {'type': 'string', 'maxLength': 200},
        'operator': {'type': 'string', 'enum': [*OPERATORS, 'UNKNOWN']},
        'threshold': {'type': ['number', 'null']},
        'deadline': {'type': ['string', 'null']},
        'source_ids': {'type': 'array', 'maxItems': 6, 'items': {'type': 'string'}},
        'invalidation': {'type': 'string', 'maxLength': 500},
    },
    'required': ['status', 'claim', 'metric', 'operator', 'threshold', 'deadline',
                 'source_ids', 'invalidation'],
}
SCHEMA = copy.deepcopy(TRADER_SCHEMA)
_stock_schema = SCHEMA['properties']['stocks']['items']
_stock_schema['properties']['hypothesis_test'] = HYPOTHESIS_SCHEMA
_stock_schema['required'].append('hypothesis_test')

COMMON_INSTRUCTION = (
    '这是隔离实验研究输出，不授权交易。两组使用同一输出结构。每个stocks项须包含hypothesis_test：'
    'status为TESTABLE或UNKNOWN；claim为待验证命题，metric为可观测指标及口径，'
    'operator仅允许<、<=、>、>=、==、!=；threshold为有限数值；deadline为带时区的ISO8601时间，'
    '须晚于资料时点；source_ids仅用本次facts中已有逐字原文引文的证据编号；'
    'invalidation为明确的推翻条件。无法可靠填写时status为UNKNOWN，operator为UNKNOWN，'
    'threshold和deadline为null，source_ids为空列表，其余文字可留空或说明缺口。'
    '不为凑齐字段虚构数字、引文或日期；不把字段齐全当成预测正确。'
)
CANDIDATE_INSTRUCTION = (
    '额外执行一次明确反证检查：在完成结论前，尝试把最关键的经营判断写成一项有证据依据、'
    '有量化阈值和未来期限的可推翻假设，填写hypothesis_test；无法量化时保持UNKNOWN。'
)


def _stamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('需要带时区的时间')
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('时间缺少时区')
    return stamp


def _meaningful(value, limit):
    return (isinstance(value, str) and 0 < len(value.strip()) <= limit
            and value.strip().upper() not in {'UNKNOWN', 'N/A', 'NA', 'NONE', 'NULL',
                '未知', '无', '待定', '待核实', '不适用', '无法判断', '尚不能判断'})


def _citations(result, packet):
    """Count exact quote occurrences; source membership is not factual truth."""
    total = valid = 0
    valid_ids = set()
    try:
        stock = result['stocks'][0]
        symbol = packet['stocks'][0]['symbol']
        industry = {f['evidence_id'] for h in packet.get('industry_hypotheses', [])
                    for f in h.get('facts', [])}
        evidence = {e['evidence_id']: e for e in packet['evidence']
                    if e.get('symbol') in (symbol, 'MARKET') or e['evidence_id'] in industry}
        if not isinstance(stock.get('facts'), list):
            return total, valid, valid_ids
        for fact in stock['facts']:
            total += 1
            if not isinstance(fact, dict) or set(fact) != {'evidence_id', 'quote'}:
                continue
            identity, quote = fact['evidence_id'], fact['quote']
            if not isinstance(identity, str):
                continue
            source = evidence.get(identity)
            if (source and isinstance(quote, str) and 4 <= len(quote.strip())
                    and len(quote) <= 180 and quote in source.get('text', '')):
                valid += 1
                valid_ids.add(identity)
    except (KeyError, TypeError, IndexError, AttributeError):
        pass
    return total, valid, valid_ids


def measure(result, packet, at):
    """Return scalar statistics, including invalid/UNKNOWN output in the denominator.

    ``packet`` is the immutable, single-stock full research snapshot, not the
    presentation-only model packet. ``at`` is the actual measurement/completion
    time, at or after the packet's information cutoff. Malformed
    output returns INVALID rather than raising or exposing model text.
    """
    total, cited, valid_ids = _citations(result, packet)
    stats = {'status': 'INVALID', 'valid': 0, 'verifiable': 0,
             'citations_total': total, 'citations_valid': cited,
             'citation_rate': round(cited / total, 6) if total else None,
             'reason_code': 'OUTPUT_INVALID'}
    try:
        cutoff = _stamp(at)
        if (not isinstance(packet, dict) or len(packet.get('stocks', [])) != 1
                or not isinstance(packet.get('evidence'), list)):
            raise ValueError('单一股票资料缺失')
        if _stamp(packet.get('as_of', at)) > cutoff:
            raise ValueError('输入资料晚于配对时点')
        if (not isinstance(result, dict) or not isinstance(result.get('stocks'), list)
                or len(result['stocks']) != 1 or not isinstance(result['stocks'][0], dict)
                or set(result['stocks'][0]) != set(_stock_schema['required'])):
            raise ValueError('输出须匹配共同结构')
        stripped = copy.deepcopy(result)
        hypothesis = stripped['stocks'][0].pop('hypothesis_test')
        validate_result(stripped, packet)
        if total > 6 or cited != total:
            raise ValueError('引用未逐字核验')
        if (not isinstance(hypothesis, dict)
                or set(hypothesis) != set(HYPOTHESIS_SCHEMA['required'])):
            raise ValueError('假设字段不完整')
        for key, limit in (('claim', 500), ('metric', 200), ('invalidation', 500)):
            if not isinstance(hypothesis[key], str) or len(hypothesis[key]) > limit:
                raise ValueError('假设文字字段无效')
        ids = hypothesis['source_ids']
        if (not isinstance(ids, list) or len(ids) > 6
                or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids)):
            raise ValueError('假设证据编号无效')
        if hypothesis['status'] == 'UNKNOWN':
            if (hypothesis['operator'] != 'UNKNOWN' or hypothesis['threshold'] is not None
                    or hypothesis['deadline'] is not None or ids):
                raise ValueError('UNKNOWN不应包含伪确定条件')
            return {**stats, 'status': 'UNKNOWN', 'valid': 1, 'reason_code': 'UNKNOWN'}
        if hypothesis['status'] != 'TESTABLE':
            raise ValueError('假设状态无效')
        if (any(not _meaningful(hypothesis[k], n) for k, n in
                (('claim', 500), ('metric', 200), ('invalidation', 500)))
                or hypothesis['operator'] not in OPERATORS):
            raise ValueError('假设缺少可核验条件')
        threshold = hypothesis['threshold']
        if type(threshold) not in (int, float) or not math.isfinite(threshold):
            raise ValueError('阈值必须为有限数值')
        if _stamp(hypothesis['deadline']) <= cutoff:
            raise ValueError('假设期限必须在未来')
        if not ids or not set(ids) <= valid_ids:
            raise ValueError('假设必须引用已逐字核验的本次证据')
        return {**stats, 'status': 'TESTABLE', 'valid': 1, 'verifiable': 1,
                'reason_code': 'STRUCTURE_AND_CITATIONS_VALID'}
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError):
        return stats


def _arm(value):
    if value is None:
        return 0
    if not isinstance(value, dict) or type(value.get('verifiable')) is not int or value['verifiable'] not in (0, 1):
        raise ValueError('实验测量分数无效')
    if value['verifiable'] and (value.get('valid') != 1 or value.get('status') != 'TESTABLE'):
        raise ValueError('无效输出不能计作可检验')
    return value['verifiable']


def _describe(pairs, minimum_pairs):
    n = len(pairs)
    complete = sum(p['status'] == 'COMPLETE' for p in pairs)
    failed = sum(p['status'] == 'FAILED' for p in pairs)
    pending = n - complete - failed
    # Pending pairs have not produced a paired observation. Their two zeros keep
    # the enrollment denominator visible without claiming a partial-arm gain.
    a = [0 if p['status'] == 'PENDING' else _arm(p.get('baseline')) for p in pairs]
    b = [0 if p['status'] == 'PENDING' else _arm(p.get('candidate')) for p in pairs]
    deltas = [y - x for x, y in zip(a, b)]
    mean = sum(deltas) / n if n else None
    clusters = defaultdict(list)
    for pair, delta in zip(pairs, deltas):
        clusters[local(pair['as_of']).date().isoformat()].append(delta)
    k = len(clusters)
    ci = None
    if n and k >= 30 and not pending:
        residuals = [sum(v - mean for v in values) for values in clusters.values()]
        half = 1.96 * math.sqrt(k / (k - 1) * sum(r * r for r in residuals) / (n * n))
        ci = [round(max(-1.0, mean - half), 6), round(min(1.0, mean + half), 6)]
    sufficient = complete >= minimum_pairs and k >= 30 and not pending
    citation_stats = {}
    for arm in ('baseline', 'candidate'):
        measures = [p.get(arm) for p in pairs if p.get(arm) is not None]
        total = sum(m.get('citations_total', 0) for m in measures)
        valid = sum(m.get('citations_valid', 0) for m in measures)
        citation_stats.update({arm + '_citations_total': total, arm + '_citations_valid': valid,
                              arm + '_citation_rate': round(valid / total, 6) if total else None,
                              arm + '_unknown': sum(m.get('status') == 'UNKNOWN' for m in measures),
                              arm + '_invalid': sum(m.get('status') == 'INVALID' for m in measures)})
    return {'enrolled': n, 'complete': complete, 'failed': failed, 'pending': pending,
            'baseline_rate': round(sum(a) / n, 6) if n else None,
            'candidate_rate': round(sum(b) / n, 6) if n else None,
            'paired_delta': round(mean, 6) if mean is not None else None,
            'day_clusters': k, 'ci95': ci,
            'conclusion': 'DESCRIPTIVE_ONLY' if sufficient else 'INSUFFICIENT', **citation_stats}


def summarize(pairs, minimum_pairs):
    """Summarize enrolled pairs overall and in the two frozen windows.

    Pairs contain id, window (1/2), as_of, status (COMPLETE/FAILED/PENDING),
    baseline and candidate measurements (or None). Minimum pairs is the total
    trial requirement, not a per-window target. Day clusters are descriptive;
    neither 30 clusters nor a positive interval demonstrates factual validity.
    The runner, not this pure function, handles finalization and budgets.
    """
    if type(minimum_pairs) is not int or minimum_pairs < 1:
        raise ValueError('实验最小配对数无效')
    if not isinstance(pairs, (list, tuple)):
        raise ValueError('实验配对列表无效')
    seen = set()
    for pair in pairs:
        if not isinstance(pair, dict) or not isinstance(pair.get('id'), str) or not pair['id']:
            raise ValueError('实验配对编号无效')
        if pair['id'] in seen:
            raise ValueError('实验配对编号重复')
        seen.add(pair['id'])
        if type(pair.get('window')) is not int or pair['window'] not in (1, 2):
            raise ValueError('实验观察窗口无效')
        if pair.get('status') not in ('COMPLETE', 'FAILED', 'PENDING'):
            raise ValueError('实验配对状态无效')
        _stamp(pair.get('as_of'))
        for arm in ('baseline', 'candidate'):
            measurement = pair.get(arm)
            _arm(measurement)
            if measurement is not None:
                total, valid = (measurement.get(k, 0) for k in ('citations_total', 'citations_valid'))
                if type(total) is not int or type(valid) is not int or not 0 <= valid <= total:
                    raise ValueError('实验引用计数无效')
        if pair['status'] == 'COMPLETE' and any(pair.get(arm) is None for arm in ('baseline', 'candidate')):
            raise ValueError('完整配对缺少一组结果')
    result = _describe(pairs, minimum_pairs)
    return {**result, 'method': VERSION, 'minimum_pairs': minimum_pairs,
            'windows': [_describe([p for p in pairs if p['window'] == w], 1) | {'window': w}
                        for w in (1, 2)],
            'limits': ['只测可检验结构与逐字引用，不验证经营预测兑现或收益。',
                       '日簇不保证独立；同事件跨日相关性需由运行器预先去重。',
                       '失败及尚未完成的登记配对保留在分母；不自动通过或采纳。']}
