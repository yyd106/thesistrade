"""Result-only HTTP views for every role other than ADMIN.

Research storage and signed sync stay complete. These explicit allowlists are
applied only at the authenticated HTTP boundary, so new research fields cannot
silently become guest-visible through a nested payload or a cloud display cache.
"""


def _pick(value, fields):
    value = value if isinstance(value, dict) else {}
    return {key: value[key] for key in fields.split() if key in value
            and (value[key] is None or isinstance(value[key], (str, int, float, bool)))}


IDENTITY = 'symbol asset name category kind unit membership research_status tier pool_tier fixed protected status entry_allowed buy_eligible archived last_research_at last_strategy_updated_at added_at updated_at'
QUOTE = 'symbol asset name price_cents price_micros observed_at first_seen_at source currency unit fx_micros fx_at'
POSITION = 'symbol name origin qty qty_scale sellable_qty cost_cents market_value_cents mark_cents price_micros quote_at fx_micros fx_at average_cost_cents average_native_cost_micros native_cost_micros first_at weight_pct unrealized_cents unrealized_return_pct quote_same_day quote_source quote_first_seen_at valuation_basis valuation_complete dividend_cents'
ORDER = 'id symbol side qty qty_scale filled_qty limit_cents limit_micros status created_at updated_at reserved_cents origin'
FILL = 'id order_id symbol side qty qty_scale price_cents price_micros fx_micros fee_cents realized_cents occurred_at recorded_at origin'
DECISION = 'id symbol at action status'
LEVELS = 'buy_low_cents buy_high_cents sell_cents stop_cents buy_low_micros buy_high_micros sell_micros stop_micros'
STATISTICS = 'fill_count fee_cents realized_cents'


def is_archived(item):
    return bool(item.get('archived')) or item.get('research_status') == 'ARCHIVED' or any(
        item.get(key) in ('COOLING', 'ARCHIVED') for key in ('tier', 'pool_tier'))


def _plan(value):
    if not isinstance(value, dict):
        return None
    result = _pick(value, 'id study_id symbol created_at activated_at valid_until status effective_status model_status')
    payload = value.get('payload') or {}
    result['payload'] = _pick(payload, 'kind')
    if isinstance(payload.get('levels'), dict):
        result['payload']['levels'] = _pick(payload['levels'], LEVELS)
    return result


def stock_result(item):
    result = _pick(item, IDENTITY)
    result.update(plan=_plan(item.get('plan')), report=None,
                  quote=_pick(item['quote'], QUOTE) if item.get('quote') else None,
                  open_orders=[_pick(row, ORDER) for row in item.get('open_orders', [])],
                  last_decision=_pick(item['last_decision'], DECISION) if item.get('last_decision') else None)
    return result


def _observation(item):
    result = _pick(item, IDENTITY)
    result.update(trade_plan=_plan(item.get('trade_plan')),
                  quote=_pick(item['quote'], QUOTE) if item.get('quote') else None,
                  spot_quote=_pick(item['spot_quote'], QUOTE) if item.get('spot_quote') else None,
                  position=_pick(item['position'], POSITION) if item.get('position') else None)
    return result


def _account(value):
    result = _pick(value, 'id initial_cents cash_cents withdrawn_cents current_stage reserved_cents withdrawal_reserved_cents withdrawal_pending available_cents equity_cents market_value_cents valuation_complete')
    for key in ('positions', 'dynamic_positions', 'global_positions'):
        result[key] = {symbol: _pick(item, POSITION) for symbol, item in (value.get(key) or {}).items()}
    result['orders'] = [_pick(row, ORDER) for row in value.get('orders', [])]
    result['risk'] = _pick(value.get('risk'), 'halted drawdown_bps')
    result['investment_policy'] = {'enabled': True} if value.get('investment_policy') else None
    return result


def _portfolio(value):
    result = _pick(value, 'cash_weight_pct stock_weight_pct total_profit_cents cumulative_dividend_cents cumulative_dividend_tax_cents cumulative_net_dividend_cents dividend_accounting_version cumulative_realized_cents unrealized_cents')
    result['holdings'] = [_pick(row, POSITION) for row in value.get('holdings', [])]
    return result


def _effects(value):
    fields = 'order_id symbol name side order_status requested_qty filled_qty fill_count gross_cents average_price_cents fee_cents first_fill_at last_fill_at realized_cents cost_basis_cents realized_return_pct remaining_qty remaining_cost_cents remaining_value_cents unrealized_cents unrealized_return_pct mark_cents quote_at'
    return {**{side: _pick(value[side], fields) if value.get(side) else None for side in ('buy', 'sell')},
            'totals': _pick(value.get('totals'), STATISTICS)}


def withdrawal_result(value):
    if not isinstance(value, dict):
        return None
    fields = 'id stage status created_at updated_at transferred_cents reserved_cents revision planned_cents'
    result = _pick(value, 'version authority account_id current_stage threshold_cents retain_cents equity_cents cash_cents candidate_cents withdrawn_cents reserved_cents pending status')
    result.update(can_transfer=False, can_cancel=False, blockers=[],
                  cycle=_pick(value['cycle'], fields) if value.get('cycle') else None,
                  history=[_pick(row, fields) for row in value.get('history', [])],
                  legacy=[_pick(row, fields) for row in value.get('legacy', [])])
    return result


def guest_status(value):
    """Never mutate the shared/admin object or return opaque nested payloads."""
    result = _pick(value, 'deployment_role version at mode live_execution model_auth scheduler_enabled calendar market_phase quote_max_age_seconds')
    schedule = value.get('schedule') or {}
    result['schedule'] = {**_pick(schedule, 'review execution_mode'),
                          **{key: [v for v in schedule.get(key, []) if isinstance(v, str)] for key in ('collection', 'slots')}}
    result['fixed_watchlist'] = [_pick(row, 'symbol name') for row in value.get('fixed_watchlist', [])]
    result['watchlist'] = [stock_result(row) for row in value.get('watchlist', []) if not is_archived(row)]
    observation = value.get('observation') or {}
    items = [_observation(row) for row in observation.get('items', []) if not is_archived(row)]
    result['observation'] = {'items': items, 'archived_items': [], 'archive_count': 0, 'archive_offset': 0,
                             'membership': {row['asset']: _pick(row, 'tier category') for row in items if row.get('asset')}}
    industry = value.get('industry') or {}
    result['industry'] = {'enabled': bool(industry.get('enabled')), 'members': [
        _pick(row, IDENTITY) for row in industry.get('members', []) if not is_archived(row)]}
    result['account'] = _account(value.get('account') or {})
    result['portfolio'] = _portfolio(value.get('portfolio') or {})
    result['trade_effects'] = _effects(value.get('trade_effects') or {})
    result['withdrawals'] = withdrawal_result(value.get('withdrawals'))
    result['decisions'] = [_pick(row, DECISION) for row in value.get('decisions', [])]
    result['fills'] = [_pick(row, FILL) for row in value.get('fills', [])]
    result['next_runs'] = [_pick(row, 'kind scheduled_at') for row in value.get('next_runs', [])]
    result['state'] = _pick(value.get('state'), 'heartbeat')
    result['research_lease'] = _pick(value.get('research_lease'), 'active completed_at') if value.get('research_lease') else None
    dynamic = value.get('dynamic') or {}
    result['dynamic'] = {**_pick(dynamic, 'enabled fill_count fees_cents realized_cents'), 'items': [],
                         'fills': [_pick(row, FILL) for row in dynamic.get('fills', [])]}
    result.update(research_access=False, portfolio_strategy=None, followups=None, reviews=[], supervision=None,
                  strategy_freshness=None, jobs=[], active_jobs=[], source_checks=[], background_failures=[],
                  notices=[], documents=0, snapshots=0)
    return result


def guest_stock(value):
    result = _pick(value, 'at')
    result.update(research_access=False, stock=stock_result(value['stock']), dimensions=[], industry=None, followups=None,
                  position=_pick(value['position'], POSITION) if value.get('position') else None,
                  trade_statistics=_pick(value.get('trade_statistics'), STATISTICS))
    result['history'] = {}
    for kind, fields in (('fills', FILL), ('orders', ORDER), ('decisions', DECISION)):
        page = (value.get('history') or {}).get(kind) or {}
        result['history'][kind] = {**_pick(page, 'total offset limit'),
                                   'items': [_pick(row, fields) for row in page.get('items', [])]}
    return result
