"""Read-only receipts for existing portfolio checks, never decision authority.

Validation results replay the two frozen program snapshots. Hit IDs come only
from the publication branches that supplied observed_hits, never from replay or
from interpreting the model's economic rationale.
"""
VERSION = 1
CATALOG_VERSION = 'portfolio-conditions-v1'
ACTIONS = ('ALLOW', 'HOLD', 'PAUSE', 'REDUCE', 'EXIT')
STATES = ('PASS', 'FAIL', 'UNKNOWN', 'NOT_APPLICABLE')
CHECK_IDS = ('ACTION_TARGET_BOUNDS', 'ALLOW_AUTHORIZATION', 'PASSIVE_TARGET_CURRENT',
             'REDUCE_POSITION_TARGET', 'EXIT_POSITION_TARGET', 'REDUCTION_VALUATION',
             'PORTFOLIO_TOTAL_CAP', 'SYMBOL_TOTAL_CAP', 'RISK_GROUP_CAP')
RECHECK_IDS = ('PUB_ALLOW_GUARD', 'PUB_REDUCTION_GUARD', 'PUB_PASSIVE_REBASE')


def catalog():
    """The version lives on each receipt; thresholds reference retained basis."""
    rows = (
        ('ACTION_TARGET_BOUNDS', 'LEGALITY', ACTIONS, '动作与整数目标须在合法范围内',
         'portfolio_strategy.validate: action枚举；整数target_bps在0至10000之间', 'bps'),
        ('ALLOW_AUTHORIZATION', 'AUTHORIZATION', ('ALLOW',), '增加资格、目标范围、账户及估值同时通过',
         'basis.can_increase=true；current_bps<target_bps<=max_bps；halted=false；valuation_ready=true', 'bps/boolean'),
        ('PASSIVE_TARGET_CURRENT', 'TARGET', ('HOLD', 'PAUSE'), '保持或暂停目标等于当前占比',
         'target_bps=basis.current_bps', 'bps'),
        ('REDUCE_POSITION_TARGET', 'TARGET', ('REDUCE',), '已有持仓且正目标至少低于当前占比1个百分点',
         'basis.qty非零；0<target_bps<=basis.current_bps-100', 'bps'),
        ('EXIT_POSITION_TARGET', 'TARGET', ('EXIT',), '已有持仓且退出目标为零',
         'basis.qty非零；target_bps=0', 'bps'),
        ('REDUCTION_VALUATION', 'AUTHORIZATION', ('REDUCE', 'EXIT'), '组合减仓或退出需要可用估值',
         'constraints.valuation_ready=true', 'boolean'),
        ('PORTFOLIO_TOTAL_CAP', 'PORTFOLIO', ACTIONS, '总目标受组合上限约束；已有超限可保持',
         'sum(target_bps)<=max(constraints.gross_cap_bps,sum(current_bps))', 'bps'),
        ('SYMBOL_TOTAL_CAP', 'PORTFOLIO', ACTIONS, '同标的跨路线目标受上限约束；已有超限可保持',
         '同symbol的sum(target_bps)<=max(constraints.symbol_cap_bps,同symbol的sum(current_bps))', 'bps'),
        ('RISK_GROUP_CAP', 'PORTFOLIO', ACTIONS, '所属风险组的目标合计不超过该组预算；未分组不适用',
         'result.risk_groups中所属组的sum(target_bps)<=group.max_bps；两次校验使用相同组定义', 'bps'),
        ('PUB_ALLOW_GUARD', 'PUBLICATION_TIGHTENING', ('ALLOW',), '原ALLOW发布前重新核验；不通过时程序降为PAUSE',
         '发布basis.can_increase=true；原target_bps>发布current_bps；估值可用且未停买', 'bps/boolean'),
        ('PUB_REDUCTION_GUARD', 'PUBLICATION_TIGHTENING', ('REDUCE', 'EXIT'), '原减仓/退出发布前重新核验；不通过时程序改为HOLD',
         '发布估值可用；REDUCE还需发布current_bps-原target_bps>=100', 'bps/boolean'),
        ('PUB_PASSIVE_REBASE', 'PROGRAM_ASSIGNMENT', ('HOLD', 'PAUSE'), '发布时保持/暂停目标按当前占比重设；仅数值改变时记实际命中，不代表主动调仓',
         '发布target_bps=发布basis.current_bps', 'bps'),
    )
    return [dict(id=i, category=category, actions=list(actions), description=description,
                 threshold_source=threshold, unit=unit)
            for i, category, actions, description, threshold, unit in rows]


def degraded():
    return {'version': VERSION, 'catalog_version': CATALOG_VERSION, 'status': 'DEGRADED',
            'evaluation': 'VALIDATION_REPLAY', 'analysis': {}, 'publication': {},
            'publication_recheck': {}, 'hit_ids': [], 'hit_attribution': 'NOT_RECORDED',
            'model_trigger': 'NOT_RECORDED', 'model_termination': 'NOT_RECORDED',
            'execution': 'NOT_CAPTURED', 'error_code': 'AUDIT_UNAVAILABLE'}


def _integer(value):
    if type(value) is not int:
        raise ValueError('integer unavailable')
    return value


def _boolean(value):
    if type(value) is not bool:
        raise ValueError('boolean unavailable')
    return value


def _state(check):
    try:
        return 'PASS' if check() else 'FAIL'
    except (KeyError, TypeError, ValueError, AttributeError):
        return 'UNKNOWN'


def _scoped(action, actions, check):
    if action not in ACTIONS:
        return 'UNKNOWN'
    return _state(check) if action in actions else 'NOT_APPLICABLE'


def _checks(packet, choices, groups, key):
    candidates = {c['key']: c for c in packet['candidates']}
    selected = {d['key']: d for d in choices}
    c, d = candidates[key], selected[key]
    action, target = d.get('action'), d.get('target_bps')

    def allow():
        eligible = _boolean(c['can_increase']); current = _integer(c['current_bps'])
        maximum = _integer(c['max_bps']); value = _integer(target)
        halted = _boolean(packet['halted']); valued = _boolean(packet['valuation_ready'])
        return eligible and current < value <= maximum and not halted and valued

    def total_cap():
        values = [_integer(d['target_bps']) for d in choices]
        current = [_integer(c['current_bps']) for c in candidates.values()]
        return sum(values) <= max(_integer(packet['gross_cap_bps']), sum(current))

    def symbol_cap():
        symbol = c['symbol']
        if not isinstance(symbol, str) or not symbol:
            raise ValueError('symbol unavailable')
        same = [x for x in candidates.values() if x['symbol'] == symbol]
        return sum(_integer(selected[x['key']]['target_bps']) for x in same) <= max(
            _integer(packet['symbol_cap_bps']), sum(_integer(x['current_bps']) for x in same))

    def group_cap():
        if not isinstance(groups, list):
            return 'UNKNOWN'
        try:
            relevant = [g for g in groups if key in g['keys']]
            if not relevant:
                return 'NOT_APPLICABLE'
            return _state(lambda: all(sum(_integer(selected[k]['target_bps']) for k in g['keys'])
                                      <= _integer(g['max_bps']) for g in relevant))
        except (KeyError, TypeError):
            return 'UNKNOWN'

    return {
        'ACTION_TARGET_BOUNDS': _state(lambda: action in ACTIONS and 0 <= _integer(target) <= 10000),
        'ALLOW_AUTHORIZATION': _scoped(action, ('ALLOW',), allow),
        'PASSIVE_TARGET_CURRENT': _scoped(action, ('HOLD', 'PAUSE'), lambda: _integer(target) == _integer(c['current_bps'])),
        'REDUCE_POSITION_TARGET': _scoped(action, ('REDUCE',), lambda: bool(_integer(c['qty'])) and 0 < _integer(target) <= _integer(c['current_bps']) - 100),
        'EXIT_POSITION_TARGET': _scoped(action, ('EXIT',), lambda: bool(_integer(c['qty'])) and _integer(target) == 0),
        'REDUCTION_VALUATION': _scoped(action, ('REDUCE', 'EXIT'), lambda: _boolean(packet['valuation_ready'])),
        'PORTFOLIO_TOTAL_CAP': _state(total_cap), 'SYMBOL_TOTAL_CAP': _state(symbol_cap),
        'RISK_GROUP_CAP': group_cap(),
    }


def _recheck(fresh, chosen, published, c):
    def allow():
        eligible = _boolean(c['can_increase']); current = _integer(c['current_bps'])
        target = _integer(chosen['target_bps'])
        valued = _boolean(fresh['valuation_ready']); halted = _boolean(fresh['halted'])
        return eligible and target > current and valued and not halted

    def reduction():
        valued = _boolean(fresh['valuation_ready'])
        return valued and (chosen['action'] != 'REDUCE' or _integer(c['current_bps']) - _integer(chosen['target_bps']) >= 100)

    return {'PUB_ALLOW_GUARD': _scoped(chosen.get('action'), ('ALLOW',), allow),
            'PUB_REDUCTION_GUARD': _scoped(chosen.get('action'), ('REDUCE', 'EXIT'), reduction),
            'PUB_PASSIVE_REBASE': _scoped(published.get('action'), ('HOLD', 'PAUSE'),
                lambda: _integer(published['target_bps']) == _integer(c['current_bps']))}


def build(packet, fresh, model_choices, result, published_at, observed_hits=None):
    """Return per-key, bounded receipts; timestamps/numbers stay in parent audit."""
    output = {}
    published = {d['key']: d for d in result['decisions']}
    candidates = {c['key']: c for c in fresh['candidates']}
    # run() never changes risk_groups between the two validations. Missing
    # groups in older/synthetic callers remain UNKNOWN rather than invented [].
    groups = result.get('risk_groups')
    for chosen in model_choices:
        key = chosen['key']
        try:
            receipt = {**degraded(), 'status': 'RECORDED'}
            receipt.pop('error_code')
            receipt['analysis'] = _checks(packet, model_choices, groups, key)
            receipt['publication'] = _checks(fresh, result['decisions'], groups, key)
            receipt['publication_recheck'] = _recheck(fresh, chosen, published[key], candidates[key])
            if isinstance(observed_hits, dict) and key in observed_hits:
                hits = observed_hits[key]
                if not isinstance(hits, list) or any(type(i) is not str or i not in RECHECK_IDS for i in hits) or len(hits) != len(set(hits)):
                    raise ValueError('invalid observed receipt')
                receipt['hit_ids'] = list(hits)
                receipt['hit_attribution'] = 'PROGRAM_OBSERVED'
            if any('UNKNOWN' in receipt[stage].values() for stage in ('analysis', 'publication', 'publication_recheck')):
                receipt.update(status='DEGRADED', error_code='CONDITION_DATA_INCOMPLETE')
            output[key] = receipt
        except Exception:
            output[key] = degraded()
    return output


def project(value):
    """Filter field names AND scalar values; malformed receipts cannot carry prose."""
    if not isinstance(value, dict):
        return {}
    result = {}
    if type(value.get('version')) is int:
        result['version'] = value['version']
    if 'catalog_version' in value:
        result['catalog_version'] = CATALOG_VERSION if value['catalog_version'] == CATALOG_VERSION else 'UNSUPPORTED'
    allowed = {'status': ('RECORDED', 'DEGRADED'), 'evaluation': ('VALIDATION_REPLAY',),
               'hit_attribution': ('PROGRAM_OBSERVED', 'NOT_RECORDED'),
               'model_trigger': ('NOT_RECORDED',), 'model_termination': ('NOT_RECORDED',),
               'execution': ('NOT_CAPTURED',), 'error_code': ('AUDIT_UNAVAILABLE', 'CONDITION_DATA_INCOMPLETE')}
    for key, values in allowed.items():
        if type(value.get(key)) is str and value[key] in values:
            result[key] = value[key]
    for stage, ids in (('analysis', CHECK_IDS), ('publication', CHECK_IDS), ('publication_recheck', RECHECK_IDS)):
        if isinstance(value.get(stage), dict):
            result[stage] = {key: state for key, state in value[stage].items()
                             if key in ids and type(state) is str and state in STATES}
    if isinstance(value.get('hit_ids'), list):
        result['hit_ids'] = [i for i in value['hit_ids'] if type(i) is str and i in RECHECK_IDS]
    # Filtering must not repair a malformed receipt into apparently complete
    # evidence. Keep bounded diagnostics, but carry the original invalidity.
    if not validate_receipt(value):
        result.update(status='DEGRADED', error_code=result.get('error_code','AUDIT_UNAVAILABLE'))
    return result


def validate_receipt(value):
    """True only for a complete supported receipt, not an investment approval."""
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != VERSION:
        return False
    expected = {'version','catalog_version','status','evaluation','analysis','publication','publication_recheck',
                'hit_ids','hit_attribution','model_trigger','model_termination','execution'}
    if set(value) != expected:
        return False
    if value.get('catalog_version') != CATALOG_VERSION or value.get('status') != 'RECORDED' or value.get('evaluation') != 'VALIDATION_REPLAY':
        return False
    if any(value.get(k) != v for k, v in (('model_trigger', 'NOT_RECORDED'), ('model_termination', 'NOT_RECORDED'), ('execution', 'NOT_CAPTURED'))):
        return False
    for stage, ids in (('analysis', CHECK_IDS), ('publication', CHECK_IDS), ('publication_recheck', RECHECK_IDS)):
        checks = value.get(stage)
        if not isinstance(checks, dict) or set(checks) != set(ids) or any(type(v) is not str or v not in STATES or v == 'UNKNOWN' for v in checks.values()):
            return False
        if stage in ('analysis','publication') and 'FAIL' in checks.values():
            return False
    hits = value.get('hit_ids')
    if not isinstance(hits, list) or any(type(i) is not str or i not in RECHECK_IDS for i in hits) or len(hits) != len(set(hits)):
        return False
    attribution = value.get('hit_attribution')
    if attribution not in ('PROGRAM_OBSERVED', 'NOT_RECORDED') or (attribution == 'NOT_RECORDED' and hits):
        return False
    if attribution == 'PROGRAM_OBSERVED' and any(value['publication_recheck'][i] == 'FAIL' and i not in hits for i in ('PUB_ALLOW_GUARD','PUB_REDUCTION_GUARD')):
        return False
    return all(value['publication_recheck'][i] == ('PASS' if i == 'PUB_PASSIVE_REBASE' else 'FAIL') for i in hits)
