"""Read-only 今日操作清单 built from the already-assembled dashboard status.

Pure presentation for Dean's manual trading: one row per A-share with a single
action (卖出/减仓/买入/等买点/持有/观望), the paper strategy's mechanical price
levels, and a market-regime banner. Semantics follow the paper account — a row
is BUY only when the paper system itself would buy (active PAPER_TRADE plan +
authorized ALLOW portfolio decision + fresh quote inside the buy zone +
read-only execution eligibility and order budget check), and
exits mirror slots.hard_reason exactly. The optional buy_check performs read-only execution validation; this module never
writes orders, plans, the ledger or configuration."""
from datetime import datetime, timedelta

from .calendar import last_completed_day

VERSION = 'trader-view-v1'

# Blocker categories in display priority order; anything unlisted falls to OTHER.
def _blocker_rank(code):
    key = (code or '').partition(':')[0]
    if key == 'RESEARCH_VETO': return (0, 'RESEARCH_VETO')
    if key in ('UNRESOLVED_EVENT', 'CORPORATE_ACTION_UNVERIFIED'): return (1, 'EVENT')
    if key == 'TREND_NOT_CONFIRMED': return (2, 'TREND')
    if key == 'LOT_EXCEEDS_CAP': return (3, 'LOT')
    if key == 'UNSUPPORTED_BOARD': return (4, 'BOARD')
    if key == 'RESEARCH_MEMBERSHIP_REVIEW': return (5, 'MEMBERSHIP')
    if (key.startswith('SOURCE_GAP') or key.startswith('UNREAD_DOCUMENT') or key in (
            'NO_COLLECTION_COVERAGE', 'FINANCIAL_BASELINE_INCOMPLETE', 'MARKET_CONTEXT_INCOMPLETE',
            'STALE_DAILY_BARS', 'UNADJUSTED_SERIES_MISSING', 'LOCAL_ONLY_DOCUMENTS_UNREVIEWED',
            'PRICE_DISCONTINUITY')):
        return (6, 'DATA')
    if key in ('MODEL_NOT_READY', 'SOURCE_CHANGED_DURING_RESEARCH',
               'MEMBERSHIP_CHANGED_DURING_RESEARCH'):
        return (7, 'MODEL')
    return (8, 'OTHER')


_BLOCKER_REASON = {
    'EVENT': '有待核验的公告或公司行为事项，核验通过前不新增买入。',
    'LOT': '按当前净值，最小申报数量超过单股仓位上限，只研究不交易。',
    'BOARD': '该板块尚无模拟交易规则，只研究不交易。',
    'MEMBERSHIP': '该标的当前不在可买入名单中，等待名单复核。',
    'DATA': '系统资料或行情数据尚未齐全，属于数据侧问题，不是对该股票的否定判断。',
    'MODEL': '本轮研究尚未完成或研究期间资料发生变化，等待新的研究结论。',
    'OTHER': '存在尚未解释的交易限制，等待研究或规则核对。',
}

_HEADLINE = {'SELL': '卖出', 'REDUCE': '减仓', 'BUY': '买入',
             'WAIT_PRICE': '等买点', 'HOLD': '持有', 'WATCH': '观望'}


def _parse(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def _age_seconds(at, stamp):
    a, b = _parse(at), _parse(stamp)
    if a is None or b is None:
        return None
    return (a - b).total_seconds()


def _stale(quote, at, market_phase, max_age):
    """Exactly app.js quoteIsStale: stale only inside CONTINUOUS with a bad or old quote."""
    if not quote or market_phase != 'CONTINUOUS':
        return False
    age = _age_seconds(at, quote.get('observed_at'))
    return age is None or age < 0 or age > max_age


def _yuan(cents):
    return f'{cents / 100:.2f}' if isinstance(cents, int) else '待更新'


def _short(text, limit=60):
    text = (text or '').split('。')[0].strip(' ；;')
    return text[:limit] if text else ''


def _plan_active(plan, at):
    return bool(plan and (plan.get('effective_status') or plan.get('status')) == 'ACTIVE'
                and (plan.get('valid_until') or '') > at)


def _cost_stop(holding, bps):
    """Per-share cost stop, same formula as quote_health.stop_levels."""
    qty = holding.get('qty') or 0
    if not qty or bps is None:
        return None
    return int((holding['cost_cents'] * (10000 - bps) // 10000
                - (holding.get('dividend_cents') or 0)) // qty)


def _trend_condition(basis):
    ma20, ma60, close = basis.get('ma20_cents'), basis.get('ma60_cents'), basis.get('close_cents')
    return (f'需收盘站上60日线（{_yuan(ma60)} 元），且20日线（{_yuan(ma20)} 元）高于60日线；'
            f'最近收盘 {_yuan(close)} 元。')


def _top_blocker(blockers, basis, report):
    if not blockers:
        return 'OTHER', _BLOCKER_REASON['OTHER'], None
    (rank, category), _ = min((_blocker_rank(b), b) for b in blockers)
    condition = None
    if category == 'TREND':
        condition = _trend_condition(basis)
        reason = '价格趋势尚未满足买入条件，等待趋势确认。'
    elif category == 'RESEARCH_VETO':
        checks = (report or {}).get('next_checks') or []
        reason = '研究判断暂不支持买入。' + (('接下来观察：' + _short(checks[0], 80)) if checks else '')
    else:
        reason = _BLOCKER_REASON[category]
    return category, reason, condition


def _decision_for(decisions, symbol):
    return decisions.get('watchlist:' + symbol)


def _open_order(item, account_orders):
    orders = item.get('open_orders')
    if orders is None:
        orders = [o for o in account_orders if o.get('symbol') == item.get('symbol')
                  and o.get('origin') in (None, 'watchlist')]
    if not orders:
        return None
    o = orders[0]
    return {k: o.get(k) for k in ('side', 'qty', 'filled_qty', 'limit_cents', 'status')}


def _entry_reason(code):
    messages = {
        'EXISTING_OPEN_ORDER': '已有该标的未完成委托，等待处理后再判断买入。',
        'UNKNOWN_ORDER_RECONCILE_REQUIRED': '存在结果未知的委托，等待核对。',
        'INSUFFICIENT_BUDGET_OR_TARGET_REACHED': '可用资金或剩余目标额度不足最小申报数量，暂不可买入。',
        'LOT_EXCEEDS_CAP': '最小申报数量超过单股上限，暂不可买入。',
        'NEAR_PRICE_LIMIT_OR_CORPORATE_ACTION': '报价触及价格保护，暂不可买入。',
        'MARKET_CLOSED': '当前不在交易时段，等待开市后重新核验。',
        'STALE_QUOTE': '报价已过期，等待新报价后重新核验。',
        'WITHDRAWAL_PENDING': '待提取资金正在占用账户，暂停新买入。',
        'VALUATION_INCOMPLETE': '账户估值不完整，等待更新后重新核验。',
        'BEAR_HIGH_VOLATILITY': '偏空时近20日年化波动超过37%，等待波动条件满足。',
        'BEAR_VOLATILITY_MISSING': '偏空时缺少近20日年化波动，等待数据补齐。',
        'COST_BUDGET_LOCKED': '成本价回撤锁已触发，等待管理员确认重置。',
        'COST_VALUATION_INCOMPLETE': '成本预算估值不完整，等待估值更新。',
        'ACCOUNT_DRAWDOWN_HALT': '账户回撤风控暂停新买入。',
    }
    if code.startswith('LOCAL_RESEARCH_ONLY'):
        return '本机是研究端，当前可买状态请以云端模拟账户为准。'
    if code.startswith('RESEARCH_HEARTBEAT_EXPIRED'):
        return '研究心跳已过期，等待同步后重新核验。'
    return messages.get(code, '下单条件尚未通过核验，暂不可买入。')


def _row(symbol, name, action, code, reason, **kw):
    row = {'symbol': symbol, 'name': name, 'action': action, 'code': code,
           'headline': _HEADLINE[action], 'reason': reason, 'price_cents': None,
           'quote_at': None, 'quote_stale': None, 'buy_low_cents': None,
           'buy_high_cents': None, 'stop_cents': None, 'target_cents': None,
           'cost_stop_cents': None, 'zone_actionable': False, 'suggested_pct': None,
           'current_pct': None, 'held_qty': 0, 'sellable_qty': 0, 'distance_pct': None,
           'condition': None, 'plan_valid_until': None, 'research_at': None,
           'portfolio_reason': None, 'open_order': None, 'blocker_count': 0}
    row.update(kw)
    return row


def build(status, config, *, buy_check=None):
    """Assemble the trader view from a dashboard status dict. Never mutates status."""
    from .slots import hard_reason
    at = status.get('at') or ''
    market_phase = status.get('market_phase')
    max_age = status.get('quote_max_age_seconds', 90)
    bps = config.get('paper_stop_loss_bps')
    account = status.get('account') or {}
    risk = account.get('risk') or {}
    halted = bool(risk.get('halted'))
    portfolio = status.get('portfolio') or {}
    holdings = {h['symbol']: h for h in portfolio.get('holdings') or []
                if h.get('origin') == 'watchlist' and h.get('qty')}
    ps = status.get('portfolio_strategy') or {}
    ps_active = bool(ps.get('enabled')) and ps.get('status') == 'ACTIVE'
    decisions = {d.get('key'): d for d in ps.get('decisions') or []}
    account_orders = account.get('orders') or []

    def decision(symbol):
        d = _decision_for(decisions, symbol)
        return d if ps_active and d and d.get('current_authorization') is True else None

    def held_row(symbol, name, item, holding):
        quote = (item or {}).get('quote')
        price = quote.get('price_cents') if quote else None
        stale = _stale(quote, at, market_phase, max_age)
        plan = (item or {}).get('plan')
        payload = (plan or {}).get('payload') or {}
        active = _plan_active(plan, at)
        levels = (payload.get('levels') or {}) if active else {}
        params = payload if active else {}
        cost_stop = _cost_stop(holding, bps)
        plan_stop = levels.get('stop_cents')
        stop = max((s for s in (cost_stop, plan_stop) if s is not None), default=None)
        target = levels.get('sell_cents')
        d = decision(symbol)
        portfolio_reason = _short(d.get('reason')) if d else None
        base = {'price_cents': price, 'quote_at': quote.get('observed_at') if quote else None,
                'quote_stale': stale, 'buy_low_cents': levels.get('buy_low_cents'),
                'buy_high_cents': levels.get('buy_high_cents'), 'stop_cents': stop,
                'target_cents': target, 'cost_stop_cents': cost_stop,
                'current_pct': holding.get('weight_pct'), 'held_qty': holding.get('qty') or 0,
                'sellable_qty': holding.get('sellable_qty') or 0,
                'plan_valid_until': plan.get('valid_until') if plan else None,
                'research_at': (item or {}).get('last_research_at'),
                'portfolio_reason': portfolio_reason,
                'open_order': _open_order(item or {'symbol': symbol}, account_orders),
                'blocker_count': len(payload.get('blockers') or [])}
        if halted:
            return _row(symbol, name, 'SELL', 'ACCOUNT_DRAWDOWN_EXIT',
                        '账户回撤触及硬性风控，按规则有序退出持仓。', **base)
        if quote and not stale and bps is not None:
            hit = hard_reason(quote, holding, params, config)
            if hit:
                text = {'COST_STOP_TRIGGER': '价格已触及成本止损线，系统将按规则卖出。',
                        'PLAN_STOP_TRIGGER': '价格已触及计划止损线，系统将按规则卖出。',
                        'PLAN_EXIT_TRIGGER': '价格已达到计划止盈目标，系统将按规则卖出。'}[hit]
                return _row(symbol, name, 'SELL', hit, text, **base)
        if d and d.get('action') == 'EXIT':
            return _row(symbol, name, 'SELL', 'PORTFOLIO_EXIT',
                        '组合判断要求退出该持仓。', **base)
        if d and d.get('action') == 'REDUCE':
            return _row(symbol, name, 'REDUCE', 'PORTFOLIO_REDUCE',
                        f"组合判断将目标仓位降至 {d.get('target_bps', 0) / 100:.2f}%。",
                        suggested_pct=d.get('target_bps', 0) / 100, **base)
        reason = '持仓正常，未触发退出条件。'
        actionable = False
        suggested = None
        target_pct = min((d.get('target_bps') or 0) / 100 if d else 0,
                         (status.get('exposure') or {}).get('symbol_cap_pct', 20))
        weight = holding.get('weight_pct')
        if d and d.get('action') == 'ALLOW' and weight is not None and target_pct > weight:
            reason = f"持仓正常；组合允许加仓至 {target_pct:.2f}%（价格在买入区间时）。"
            actionable, suggested = True, target_pct
        if (item or {}).get('current_buy_blocker'):
            reason = '持仓未触发退出条件；' + item['current_buy_reason']
            actionable, suggested = False, None
        if not quote or stale:
            reason += ' 报价过期或未更新，止损检查等待新报价。'
        return _row(symbol, name, 'HOLD', 'HELD', reason, zone_actionable=actionable,
                    suggested_pct=suggested, **base)

    def unheld_row(symbol, name, item):
        quote = item.get('quote')
        price = quote.get('price_cents') if quote else None
        stale = _stale(quote, at, market_phase, max_age)
        plan = item.get('plan')
        payload = (plan or {}).get('payload') or {}
        levels = payload.get('levels') or {}
        basis = payload.get('basis') or {}
        blockers = payload.get('blockers') or []
        d = _decision_for(decisions, symbol)
        authorized = ps_active and d and d.get('current_authorization') is True
        base = {'price_cents': price, 'quote_at': quote.get('observed_at') if quote else None,
                'quote_stale': stale, 'plan_valid_until': plan.get('valid_until') if plan else None,
                'research_at': item.get('last_research_at'),
                'open_order': _open_order(item, account_orders),
                'blocker_count': len(blockers),
                'buy_low_cents': levels.get('buy_low_cents') if _plan_active(plan, at) else None,
                'buy_high_cents': levels.get('buy_high_cents') if _plan_active(plan, at) else None}
        if halted:
            return _row(symbol, name, 'WATCH', 'HALTED',
                        '账户回撤触及硬性风控，暂停新增买入。', **base)
        if not plan:
            return _row(symbol, name, 'WATCH', 'NO_PLAN',
                        '尚未形成研究计划，等待研究。', **base)
        if not _plan_active(plan, at):
            return _row(symbol, name, 'WATCH', 'PLAN_EXPIRED',
                        '研究计划已过期，等待新的研究结论。', **base)
        extra=item.get('current_buy_blocker')
        if (status.get('exposure') or {}).get('cost_enabled') and risk.get('cost_locked'):
            extra='COST_BUDGET_LOCKED'
        if (status.get('exposure') or {}).get('defensive'):
            import math
            vol=payload.get('vol20_annual_pct')
            if type(vol) not in (int,float) or not math.isfinite(vol) or vol<0:extra=extra or 'BEAR_VOLATILITY_MISSING'
            elif vol>37:extra=extra or 'BEAR_HIGH_VOLATILITY'
        if extra:
            vol=payload.get('vol20_annual_pct')
            reason=item.get('current_buy_reason') or _entry_reason(extra)+(f' 当前波动 {vol:.2f}%。' if type(vol) in (int,float) else ' 当前波动缺失。')
            row=_row(symbol,name,'WATCH',extra,reason,vol20_annual_pct=vol,**base)
            row['headline']='等待'
            return row
        if item.get('entry_allowed') is False:
            return _row(symbol, name, 'WATCH', 'ENTRY_PAUSED',
                        item.get('entry_reason') or '暂停新增买入。', **base)
        if payload.get('kind') != 'PAPER_TRADE':
            category, reason, condition = _top_blocker(blockers, basis, item.get('report'))
            return _row(symbol, name, 'WATCH', category, reason, condition=condition, **base)
        if not ps_active or d is None:
            return _row(symbol, name, 'WATCH', 'NO_PORTFOLIO',
                        '研究通过，等待组合判断。', **base)
        base['portfolio_reason'] = _short(d.get('reason'))
        if d.get('action') != 'ALLOW' or not authorized:
            return _row(symbol, name, 'WATCH', 'PORTFOLIO_PAUSE',
                        '组合判断暂不允许新增买入' + (f"：{base['portfolio_reason']}" if base['portfolio_reason'] else '。'),
                        **base)
        target_pct = min((d.get('target_bps') or 0) / 100,
                         (status.get('exposure') or {}).get('symbol_cap_pct', 20))
        base.update(stop_cents=levels.get('stop_cents'), target_cents=levels.get('sell_cents'),
                    suggested_pct=target_pct, zone_actionable=True)
        if not quote:
            return _row(symbol, name, 'WAIT_PRICE', 'NO_QUOTE',
                        '暂无可用报价，等待行情更新。', **base)
        if stale:
            return _row(symbol, name, 'WAIT_PRICE', 'STALE_QUOTE',
                        '报价已过期，等待行情更新后再判断买点。', **base)
        low, high = levels.get('buy_low_cents'), levels.get('buy_high_cents')
        if low is not None and high is not None and low <= price <= high:
            checked = buy_check(plan, quote, at) if buy_check else {'status': 'BLOCKED', 'reason': 'ENTRY_UNVERIFIED'}
            if checked.get('status') != 'READY' or not checked.get('qty'):
                code = checked.get('reason') or 'ENTRY_UNVERIFIED'
                base['zone_actionable'] = False
                return _row(symbol, name, 'WATCH', code, _entry_reason(code), **base)
            return _row(symbol, name, 'BUY', 'IN_ZONE',
                        f'现价 {_yuan(price)} 元在计划买入区间内，可按组合目标仓位买入。', **base)
        if high is not None and price > high:
            return _row(symbol, name, 'WAIT_PRICE', 'ABOVE_ZONE',
                        '现价高于买入区间上沿，等待回落至区间内再买入。',
                        distance_pct=round((price - high) * 100 / high, 2) if high else None, **base)
        if low is not None and price < low:
            return _row(symbol, name, 'WAIT_PRICE', 'BELOW_ZONE',
                        '现价低于买入区间下沿，系统只会在价格回到区间内时买入。',
                        distance_pct=round((price - low) * 100 / low, 2) if low else None, **base)
        return _row(symbol, name, 'WAIT_PRICE', 'NO_ZONE',
                    '有效计划缺少买入区间价格，等待研究更新。', **base)

    rows = []
    seen = set()
    for item in status.get('watchlist') or []:
        symbol = item.get('symbol')
        if not symbol:
            continue
        seen.add(symbol)
        holding = holdings.get(symbol)
        rows.append(held_row(symbol, item.get('name') or symbol, item, holding)
                    if holding else unheld_row(symbol, item.get('name') or symbol, item))
    for symbol, holding in holdings.items():
        if symbol in seen:
            continue
        rows.append(held_row(symbol, holding.get('name') or symbol, None, holding))

    regime = _regime(status.get('market_regime'), at, status.get('exposure'))
    freshness = _freshness(rows, ps, status.get('research_lease'), at)
    gross = portfolio.get('stock_weight_pct')
    suggested_max = (status.get('exposure') or {}).get('gross_cap_pct',config.get('paper_max_gross_pct',90))
    counts = {a: sum(1 for r in rows if r['action'] == a)
              for a in ('SELL', 'REDUCE', 'BUY', 'WAIT_PRICE', 'HOLD', 'WATCH')}
    return {
        'version': VERSION, 'at': at, 'regime': regime,
        'account': {'equity_cents': account.get('equity_cents'), 'gross_pct': gross,
                    'cash_pct': portfolio.get('cash_weight_pct'), 'halted': halted,
                    'drawdown_bps': risk.get('drawdown_bps'),
                    'suggested_max_pct': suggested_max,
                    'over_cap': regime['enforced'] and gross is not None and gross > suggested_max + 5},
        'freshness': freshness,
        'counts': counts, 'rows': _sort(rows),
        'notice': ('页面价位是模拟策略的机械参考（20日均价买入区间、止损、止盈），'
                   '研究与组合判断只决定是否可以买，策略尚未经过长期验证。'
                   '仓位百分比均按账户总资金计算。')}


_WATCH_CODE_ORDER = {'PORTFOLIO_PAUSE': 0, 'NO_PORTFOLIO': 1, 'RESEARCH_VETO': 2, 'EVENT': 3,
                     'TREND': 4, 'LOT': 5, 'BOARD': 6, 'MEMBERSHIP': 7, 'DATA': 8, 'MODEL': 9}


def _sort(rows):
    def key(r):
        action = r['action']
        if action == 'SELL': return (0, -(r['current_pct'] or 0), r['symbol'])
        if action == 'REDUCE': return (1, -(r['current_pct'] or 0), r['symbol'])
        if action == 'BUY': return (2, r['symbol'])
        if action == 'WAIT_PRICE':
            d = r['distance_pct']
            return (3, abs(d) if d is not None else float('inf'), r['symbol'])
        if action == 'HOLD': return (4, -(r['current_pct'] or 0), r['symbol'])
        return (5, _WATCH_CODE_ORDER.get(r['code'], 10), r['symbol'])
    return sorted(rows, key=key)


def _regime(regime_in, at, exposure=None):
    """Display view of the R1 series with a 6-hour freshness grace for the evening collection."""
    regime_in = regime_in or {}
    as_of = regime_in.get('as_of_day')
    confirmed = regime_in.get('confirmed_cap_pct')
    raw = regime_in.get('raw_cap_pct')
    ref = _parse(at)
    cutoff = last_completed_day(ref - timedelta(hours=6)) if ref else None
    stale = as_of is None or cutoff is None or as_of < cutoff
    cap = confirmed if not stale and confirmed is not None else 10
    label = {90: '偏多', 50: '震荡', 10: '偏空'}.get(cap, '偏空')
    stance = {90: 'AGGRESSIVE', 50: 'NEUTRAL', 10: 'DEFENSIVE'}[cap]
    if stale:
        label = '数据过期'
    exposure=exposure or {}
    enforced=exposure.get('mode') in ('R1','D14')
    summary='市场状态仅供参考；模拟账户目前按总仓 90%、单只 20% 和现有买入条件执行'
    if exposure.get('mode')=='D14':
        summary=('新买入要求近 20 日年化波动 ≤ 37%，单只 ≤ 10%，总仓 ≤ 50%'
                 if exposure.get('defensive') else '当前市场状态下，模拟账户按总仓 90%、单只 20% 和现有买入条件执行')
        if exposure.get('defensive') and stale:summary+='；数据过期只限制新买，不触发市场状态减仓'
    elif exposure.get('mode')=='R1':
        summary=f"R1 已生效：模拟账户总仓 ≤ {exposure.get('gross_cap_pct',90):g}%"
    if exposure.get('cost_enabled'):
        summary+=f"；成本预算已启用，当前有效总仓上限 {exposure.get('gross_cap_pct',90):g}%"
    close, ma20, ma60 = regime_in.get('close'), regime_in.get('ma20'), regime_in.get('ma60')
    return {'method': regime_in.get('method'), 'source_status': regime_in.get('status'),
            'as_of_day': as_of, 'close': close, 'ma20': ma20, 'ma60': ma60,
            'raw_cap_pct': raw, 'confirmed_cap_pct': confirmed, 'cap_pct': cap,
            'stale': stale,
            'conditions': {'close_above_ma60': None if close is None or ma60 is None else close > ma60,
                           'ma20_above_ma60': None if ma20 is None or ma60 is None else ma20 > ma60},
            'label': label, 'stance': stance,
            'pending_change': (f"最新一日信号为 { {90:'偏多',50:'震荡',10:'偏空'}.get(raw,'未知')}，需再确认一个交易日。"
                               if not stale and raw is not None and raw != confirmed else None),
            'summary': summary, 'enforced': enforced,
            'enforced_note': '仓位与买入限制以模拟账户当前有效规则为准。'}


def _freshness(rows, ps, lease, at):
    research_at = max((r['research_at'] for r in rows if r.get('research_at')), default=None)
    notes = []
    stale = False
    age = _age_seconds(at, research_at)
    if research_at is None:
        notes.append('尚无完成的研究记录。')
        stale = True
    elif age is None or age > 8 * 3600:
        stale = True
        notes.append('最近一次成功研究距今超过 8 小时，结论可能已过期。')
    ps_status = ps.get('status') if ps.get('enabled') else None
    if ps.get('enabled') and ps_status != 'ACTIVE':
        stale = True
        notes.append('组合策略已过期，需等待新的组合判断。')
    if lease and lease.get('active') is False:
        stale = True
        notes.append('研究心跳已过期，云端已停止新买入。')
    return {'research_at': research_at, 'portfolio_at': ps.get('created_at'),
            'portfolio_valid_until': ps.get('valid_until'), 'portfolio_status': ps_status,
            'stale': stale, 'notes': notes}
