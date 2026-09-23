"""Current paper-account presentation. Never infer trades from model decisions."""
from datetime import timedelta
from .calendar import local, trading_day
from .storage import normalize_time


def next_runs(config, at):
    """The next scheduled start for each workflow, using the scheduler's calendar."""
    if not config['scheduler_enabled']:
        return []
    current = local(at)
    found = {}
    for offset in range(15):
        day = current + timedelta(days=offset)
        for kind, times in (
            ('cycle', config['collection_times']),
            ('review', [config['review_time']]),
            ('slot', config['slot_times'] if trading_day(day.date()) is True else []),
        ):
            for hhmm in sorted(times):
                hour, minute = map(int, hhmm.split(':'))
                stamp = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if stamp > current and kind not in found:
                    found[kind] = {'kind': kind, 'scheduled_at': normalize_time(stamp.isoformat())}
        if len(found) == 3:
            break
    return sorted(found.values(), key=lambda row: row['scheduled_at'])


def pct(value, base):
    return round(value * 100 / base, 2) if base else None


def portfolio(store, config, a, at):
    names = {w['symbol']: w['name'] for w in config['watchlist']}
    holdings = []
    combined=[(symbol,p,'watchlist') for symbol,p in a['positions'].items()]+[(symbol,p,'dynamic') for symbol,p in a.get('dynamic_positions',{}).items()]
    for symbol, position, origin in combined:
        from .dynamic_sources import latest_quote as dynamic_quote
        q = dynamic_quote(store,symbol,at) if origin=='dynamic' else store.latest_quote(symbol, at)
        # Remaining lot basis includes allocated buy fees. Keep sub-cent precision
        # until display, and never round this average back into the ledger.
        cost = position['cost_cents']
        unrealized = position['market_value_cents'] - cost if q else None
        holdings.append({
            'symbol': symbol, 'origin':origin,'name': names.get(symbol, q['name'] if q else symbol),
            **position,
            'weight_pct': pct(position['market_value_cents'], a['equity_cents']),
            'average_cost_cents': cost / position['qty'] if position['qty'] else None,
            'unrealized_cents': unrealized,
            'unrealized_return_pct': pct(unrealized, cost) if unrealized is not None else None,
            'quote_same_day': bool(q and local(q['observed_at']).date() == local(at).date()),
            'valuation_basis': 'LAST_QUOTE' if q else 'COST_FALLBACK',
        })
    from .investment_policy import FIXED
    for symbol,p in a.get('global_positions',{}).items():
        value=p['market_value_cents'];cost=p['cost_cents'];unrealized=value-cost if p.get('price_micros') else None
        holdings.append({**p,'symbol':symbol,'name':FIXED.get(symbol,{}).get('name',symbol),'origin':'global',
            'qty':p['qty']/p['qty_scale'],'sellable_qty':p['qty']/p['qty_scale'],'weight_pct':pct(value,a['equity_cents']),
            'unrealized_cents':unrealized,'unrealized_return_pct':pct(unrealized,cost) if unrealized is not None else None,
            'quote_same_day':p['valuation_complete'],'valuation_basis':'LAST_QUOTE' if p.get('price_micros') else 'COST_FALLBACK'})
    return {
        'holdings': sorted(holdings, key=lambda p: (-p['market_value_cents'], p['symbol'])),
        'cash_weight_pct': pct(a['cash_cents'], a['equity_cents']),
        'stock_weight_pct': pct(a['market_value_cents'], a['equity_cents']),
        'total_profit_cents': a['equity_cents'] + a['withdrawn_cents'] - a['initial_cents'],
        'unrealized_cents': sum(p['unrealized_cents'] for p in holdings)
            if all(p['unrealized_cents'] is not None for p in holdings) else None,
    }


def trade_effects(store, config, at):
    """Aggregate partial fills by order; show last BUY and SELL independently.

    BUY P&L is only the unclosed lots of that order, including allocated buy fees.
    SELL P&L comes from FIFO realized entries, including allocated buy and sell fees.
    This is a current ledger view, not historical portfolio reconstruction.
    """
    names = {w['symbol']: w['name'] for w in config['watchlist']}
    result = {}
    for side in ('BUY', 'SELL'):
        order = store.db.execute('''SELECT o.*, d.reason FROM paper_orders o
            JOIN decisions d ON d.id=o.decision_id
            WHERE o.id=(SELECT order_id FROM paper_fills WHERE side=?
                ORDER BY occurred_at DESC, recorded_at DESC, rowid DESC LIMIT 1)''', (side,)).fetchone()
        if not order:
            result[side.lower()] = None
            continue
        o = dict(order)
        fills = [dict(r) for r in store.db.execute(
            'SELECT * FROM paper_fills WHERE order_id=? ORDER BY occurred_at,id', (o['id'],))]
        qty = sum(f['qty'] for f in fills)
        gross = sum(f['qty'] * f['price_cents'] for f in fills)
        fees = sum(f['fee_cents'] for f in fills)
        realized = sum(f['realized_cents'] for f in fills)
        q = store.latest_quote(o['symbol'], at)
        effect = {
            'order_id': o['id'], 'symbol': o['symbol'],
            'name': names.get(o['symbol'], q['name'] if q else o['symbol']),
            'side': side, 'order_status': o['status'], 'requested_qty': o['qty'],
            'filled_qty': qty, 'fill_count': len(fills), 'gross_cents': gross,
            'average_price_cents': gross / qty, 'fee_cents': fees,
            'first_fill_at': min(f['occurred_at'] for f in fills),
            'last_fill_at': max(f['occurred_at'] for f in fills), 'reason': o['reason'],
        }
        if side == 'SELL':
            basis = gross - fees - realized
            effect.update(realized_cents=realized, cost_basis_cents=basis,
                          realized_return_pct=pct(realized, basis))
        else:
            lots = store.db.execute('''SELECT coalesce(sum(l.qty),0), coalesce(sum(l.cost_cents),0)
                FROM paper_lots l JOIN paper_fills f ON f.id=l.id WHERE f.order_id=?''', (o['id'],)).fetchone()
            remaining, cost = lots
            value = remaining * q['price_cents'] if q else None
            unrealized = value - cost if value is not None and remaining else None
            effect.update(remaining_qty=remaining, remaining_cost_cents=cost,
                          remaining_value_cents=value, unrealized_cents=unrealized,
                          unrealized_return_pct=pct(unrealized, cost) if unrealized is not None else None,
                          mark_cents=q['price_cents'] if q else None,
                          quote_at=q['observed_at'] if q else None)
        result[side.lower()] = effect
    totals = store.db.execute('''SELECT count(*), coalesce(sum(fee_cents),0),
        coalesce(sum(realized_cents),0) FROM paper_fills''').fetchone()
    result['totals'] = dict(zip(('fill_count', 'fee_cents', 'realized_cents'), totals))
    return result
