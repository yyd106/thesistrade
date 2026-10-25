"""Conservative, read-only comparison of explicit Chinese position statements.

This is a post-hoc diagnostic, never a trading condition or a rewrite of a
decision. Unsupported language remains UNKNOWN. A historical final snapshot
cannot prove what account values the model saw, so its mismatches are suspect.
"""
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import json
import re


VERSION = 1
_CURRENT = r'(?:当前|现有|现时|目前)(?:的)?(?:仓位占比|持仓占比|持仓比例|持仓权重|仓位|占比)'
_TARGET = r'(?:目标(?:仓位|持仓占比|持仓比例|权重|占比)?|(?:保持|维持)(?:当前|现有)?(?:仓位|持仓占比|持仓比例|占比)?)'
_NUMBER = r'\d{1,3}(?:\.\d{1,6})?'
_JOIN = r'\s*(?:设为|定为|调整为|控制在|维持在|保持在|约为|为|是|在|至|约)?\s*'
_PERCENT = r'(?P<value>' + _NUMBER + r')\s*[%％]'
_TRANSITION = re.compile(r'(?:从|由)\s*(?P<current>' + _NUMBER + r')\s*[%％]\s*'
                         r'(?:减至|降至|降到|减到|调整至|调至|调整为)\s*'
                         r'(?P<target>' + _NUMBER + r')\s*[%％]')
_PATTERNS = (
    ('TARGET', re.compile(_TARGET + _JOIN + _PERCENT)),
    ('CURRENT', re.compile(_CURRENT + r'\s*(?:约为|为|是|约)?\s*' + _PERCENT)),
    ('REDUCTION_PP', re.compile(r'(?:减仓|减持|降低仓位|下调仓位|减少仓位|'
                              r'(?:仓位|持仓占比)(?:降低|减少|下调|下降))\s*'
                              r'(?:幅度为|约|了)?\s*(?P<value>' + _NUMBER + r')\s*(?:个)?百分点')),
)
_SENTENCE = '。；;\n!?！？'
_CLAUSE = _SENTENCE + '，,：:'
_CONDITIONAL = re.compile(r'如果|假如|假设|若|一旦|情景|示例|例如|譬如|可能|或许|预计|预期|届时|否则|待.+(?:后|时)|触发后|满足后')
_NON_POSITION = re.compile(r'利润率|毛利率|净利率|收益率|回报率|增长率|利率|税率|胜率|市占率|'
                           r'营收|收入占比|价格|目标价|股价|估值|涨幅|跌幅|回撤|波动率|'
                           r'风险上限|仓位上限|单股上限|最大仓位|最大持仓|总仓位|组合仓位|总敞口|现金占比|'
                           r'其他标的|其他股票|另一只|另一标的|上限|下限|区间|ROE|ROA|IRR|CAGR', re.I)
_NEGATED_OR_PAST = re.compile(r'不|未|无须|无需|否认|避免|禁止|昨日|上轮|此前|先前|过去|历史|原来|曾经|原目标|旧目标|之前')
_RANGE_SUFFIX = re.compile(r'^\s*(?:[-~～—–至到/]|以内|以下|以上|左右|附近|上下|上限|下限|或|、|和|与)')
_RETRACTED_SUFFIX = re.compile(r'不成立|不适用|无效|错误|不是|并非|不再|不应|不可|被否定|已取消|仅为|只是')
_POSITION_SUBJECT = re.compile(r'(?:仓位|持仓占比|持仓比例|持仓权重)\s*$')
_DIRECT_PREFIX = re.compile(r'\s*(?:(?:本轮|本次|因此|故|仍|继续|暂时)\s*)?(?:(?:建议|决定)\s*)?')


def _valid_bps(value):
    return type(value) is int and 0 <= value <= 10000


def _start(text, at, separators):
    return max((text.rfind(mark, 0, at) for mark in separators), default=-1) + 1


def _eligible(text, match_start, value_start, value_end):
    # A condition can govern the next comma-separated clause, so conditional
    # scope is the sentence; finance/negation checks use the local clause.
    prefix = text[_start(text, match_start, _CLAUSE):match_start]
    sentence_prefix = text[_start(text, match_start, _SENTENCE):match_start]
    if _CONDITIONAL.search(sentence_prefix) or _NEGATED_OR_PAST.search(prefix) or _NON_POSITION.search(prefix):
        return False
    suffix = text[value_end:]
    # Skip the unit itself, then reject bounds, ranges and obvious other units.
    suffix = re.sub(r'^\s*(?:[%％]|(?:个)?百分点)', '', suffix, count=1)
    tail = re.split('[' + re.escape(_CLAUSE) + ']', suffix, maxsplit=1)[0]
    if (_RANGE_SUFFIX.search(tail) or _NON_POSITION.search(tail[:12])
            or _RETRACTED_SUFFIX.search(tail) or _CONDITIONAL.search(tail)):
        return False
    return not (value_start and text[value_start - 1] in '.+-0123456789')


def _mentions(text):
    found = {}

    def add(match, name, role, subject_required=False):
        start, end = match.span(name)
        if (start, end) in found or not _eligible(text, match.start(), start, end):
            return
        prefix = text[_start(text, match.start(), _CLAUSE):match.start()]
        if subject_required and not (_POSITION_SUBJECT.search(prefix) or _DIRECT_PREFIX.fullmatch(prefix)):
            return
        value = Decimal(match[name])
        if value < 0 or value > 100:
            return
        found[(start, end)] = (role, match[name])

    # Explicit from/to language establishes roles before the shorter patterns.
    for match in _TRANSITION.finditer(text):
        add(match, 'current', 'CURRENT', subject_required=True)
        add(match, 'target', 'TARGET', subject_required=True)
    for role, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            # Bare "目标/保持" is meaningful only as a direct statement, not
            # "成功率目标" or "订单转化率保持". Do not guess unknown subjects.
            phrase = text[match.start():match.start('value')]
            add(match, 'value', role, subject_required=not re.search(r'仓位|持仓', phrase))
    return [(start, end, *found[(start, end)]) for start, end in sorted(found)]


def _receipt(item, audit, row):
    """Use a complete same-key receipt only if its final values match the item."""
    if audit is None and row is None:
        return None, 'FINAL_ONLY', None
    if not isinstance(audit, dict) or type(audit.get('version')) is not int or audit['version'] != 1:
        return None, 'UNAVAILABLE', 'AUDIT_UNUSABLE'
    rows = audit.get('decisions')
    key = item.get('key')
    matches = [r for r in rows if isinstance(r, dict) and r.get('key') == key] if isinstance(rows, list) and isinstance(key, str) and key else []
    if len(matches) != 1:
        return None, 'UNAVAILABLE', 'AUDIT_ITEM_UNAVAILABLE'
    recorded = matches[0]
    if row is not None:
        try:
            if json.dumps(row, sort_keys=True, allow_nan=False) != json.dumps(recorded, sort_keys=True, allow_nan=False):
                return None, 'UNAVAILABLE', 'AUDIT_ITEM_MISMATCH'
        except (TypeError, ValueError, OverflowError, RecursionError):
            return None, 'UNAVAILABLE', 'AUDIT_UNUSABLE'
    from .strategy_report_audit import publication_complete
    try:
        if not publication_complete(audit, recorded):
            return None, 'UNAVAILABLE', 'AUDIT_UNUSABLE'
        times = [datetime.fromisoformat(audit[k].replace('Z', '+00:00')) for k in ('analysis_as_of', 'published_at')]
        if any(t.tzinfo is None for t in times) or times[0] > times[1]:
            return None, 'UNAVAILABLE', 'AUDIT_UNUSABLE'
        values = [recorded['model_target_bps'], recorded['published_target_bps'],
                  recorded['analysis_basis']['current_bps'], recorded['publication_basis']['current_bps']]
        if not all(_valid_bps(v) for v in values):
            return None, 'UNAVAILABLE', 'AUDIT_UNUSABLE'
        if (recorded['published_action'] != item.get('action') or
                recorded['published_target_bps'] != item.get('target_bps') or
                recorded['publication_basis']['current_bps'] != item.get('current_bps') or
                not all(_valid_bps(item.get(k)) for k in ('current_bps', 'target_bps'))):
            return None, 'UNAVAILABLE', 'AUDIT_FINAL_VALUE_MISMATCH'
    except (TypeError, ValueError, KeyError, OverflowError, AttributeError):
        return None, 'UNAVAILABLE', 'AUDIT_UNUSABLE'
    return recorded, 'MODEL_ANALYSIS', None


def _matches(value, expected):
    # Display precision defines rounding, rather than an arbitrary fixed epsilon.
    return expected is not None and (Decimal(expected) / 100).quantize(
        Decimal(1).scaleb(value.as_tuple().exponent), rounding=ROUND_HALF_UP) == value


def _rebase(row):
    return bool(row and row['model_action'] in ('HOLD', 'PAUSE')
                and row['published_action'] == row['model_action']
                and row['model_target_bps'] == row['analysis_basis']['current_bps']
                and row['published_target_bps'] == row['publication_basis']['current_bps']
                and row['model_target_bps'] != row['published_target_bps']
                and 'HOLD_PAUSE_CURRENT_REBASE' in row['adjustment_codes'])


def check(item, audit=None, row=None):
    """Return JSON-safe numeric findings; never mutate inputs or infer a cause.

    CURRENT/TARGET numbers use percentages; REDUCTION_PP uses percentage points,
    not a relative percentage decrease. Unknown/negated/range/other-financial
    language is deliberately excluded. CONSISTENT means only the recognized
    displayed numbers agree under ROUND_HALF_UP at their written precision.
    """
    result = {'version': VERSION, 'mode': 'POSTHOC_DIAGNOSTIC', 'basis': 'UNAVAILABLE',
              'status': 'UNKNOWN', 'rounding': 'ROUND_HALF_UP_AT_DISPLAY_PRECISION',
              'comparisons': [], 'issues': []}
    if not isinstance(item, dict) or not isinstance(item.get('reason'), str):
        result['issues'].append('INVALID_REASON')
        return result
    receipt, basis, problem = _receipt(item, audit, row)
    result['basis'] = basis
    if problem:
        result['issues'].append(problem)
    final_current = item.get('current_bps') if _valid_bps(item.get('current_bps')) else None
    final_target = item.get('target_bps') if _valid_bps(item.get('target_bps')) else None
    final_values = {'CURRENT': final_current, 'TARGET': final_target,
                    'REDUCTION_PP': final_current - final_target if final_current is not None and final_target is not None else None}
    for start, end, role, written in _mentions(item['reason']):
        value = Decimal(written)
        expected = final_values[role] if basis == 'FINAL_ONLY' else None
        ref_role = {'CURRENT': 'FINAL_CURRENT', 'TARGET': 'FINAL_TARGET', 'REDUCTION_PP': 'FINAL_REDUCTION'}[role]
        field = {'CURRENT': 'item.current_bps', 'TARGET': 'item.target_bps', 'REDUCTION_PP': 'item.current_bps-item.target_bps'}[role]
        if receipt:
            expected = {'CURRENT': receipt['analysis_basis']['current_bps'], 'TARGET': receipt['model_target_bps'],
                        'REDUCTION_PP': receipt['analysis_basis']['current_bps'] - receipt['model_target_bps']}[role]
            ref_role = {'CURRENT': 'ANALYSIS_CURRENT', 'TARGET': 'MODEL_TARGET', 'REDUCTION_PP': 'ANALYSIS_REDUCTION'}[role]
            field = {'CURRENT': 'analysis_basis.current_bps', 'TARGET': 'model_target_bps',
                     'REDUCTION_PP': 'analysis_basis.current_bps-model_target_bps'}[role]
        final = final_values[role]
        status = ('UNKNOWN' if expected is None else 'CONSISTENT' if _matches(value, expected)
                  else 'INCONSISTENT' if receipt else 'SUSPECTED_MISMATCH')
        if status == 'CONSISTENT' and role == 'TARGET' and _rebase(receipt) and not _matches(value, final):
            status = 'EXPLAINED_REBASE'
        tolerance = Decimal(50).scaleb(value.as_tuple().exponent)
        result['comparisons'].append({'role': role, 'mention': written, 'span': [start, end],
            'unit': 'PERCENTAGE_POINTS' if role == 'REDUCTION_PP' else 'PERCENT',
            'stated_bps': format(value * 100, 'f'), 'tolerance_bps': format(tolerance, 'f'),
            'ref_role': ref_role if expected is not None else 'UNAVAILABLE',
            'reference_field': field if expected is not None else None, 'reference_bps': expected,
            'final_bps': final, 'final_matches': _matches(value, final) if final is not None else None,
            'status': status})
    if not result['comparisons']:
        result['issues'].append('NO_UNAMBIGUOUS_WEIGHT_ASSERTION')
    elif any(c['reference_bps'] is None for c in result['comparisons']) and not problem:
        result['issues'].append('STRUCTURED_VALUE_UNAVAILABLE')
    statuses = {c['status'] for c in result['comparisons']}
    for status in ('INCONSISTENT', 'SUSPECTED_MISMATCH', 'UNKNOWN', 'EXPLAINED_REBASE', 'CONSISTENT'):
        if status in statuses:
            result['status'] = status
            break
    return result
