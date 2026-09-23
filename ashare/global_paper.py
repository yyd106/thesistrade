"""Fractional spot/US paper ledger, atomic reservations against the same CNY cash.

No broker APIs, borrowing, short selling, derivatives or leveraged ETFs.
"""
import json
from datetime import datetime, timedelta
from .storage import now, normalize_time, digest
from .investment_policy import enabled
from .global_market import SCALE, notional, latest, fresh, session_open, targets

OPEN = "('OPEN','PARTIAL','UNKNOWN')"


def balance(store, at):
    positions = {}
    for lot in store.db.execute('SELECT l.*,f.fx_micros FROM global_lots l JOIN global_fills f ON f.id=l.id WHERE l.qty>0 ORDER BY l.acquired_at,l.id'):
        p = positions.setdefault(lot['symbol'], {'qty': 0, 'cost_cents': 0, 'first_at': lot['acquired_at'], 'qty_scale': SCALE,'native_cost_micros':0})
        p['qty'] += lot['qty'];p['cost_cents'] += lot['cost_cents']
        p['native_cost_micros'] += lot['cost_cents']*10**12//(100*lot['fx_micros'])
    total = 0;complete = True
    for symbol, p in positions.items():
        q = latest(store, symbol, at)
        value = notional(p['qty'], q['price_micros'], q['fx_micros']) if q else p['cost_cents']
        p.update(market_value_cents=value, quote_at=q['observed_at'] if q else None,
                 price_micros=q['price_micros'] if q else None, fx_micros=q['fx_micros'] if q else None,
                 fx_at=q['fx_at'] if q else None, average_cost_cents=p['cost_cents']*SCALE/p['qty'],average_native_cost_micros=p['native_cost_micros']*SCALE//p['qty'])
        total += value
        # A last close can value the account out of session, but never execute an order.
        allowed_age = 90 if q and session_open(symbol, at, json.loads(q['payload_json'])) else 4*86400
        valid = q and 0 <= (datetime.fromisoformat(at)-datetime.fromisoformat(q['observed_at'])).total_seconds() <= allowed_age
        valid = valid and 0 <= (datetime.fromisoformat(at)-datetime.fromisoformat(q['fx_at'])).total_seconds() <= 4*86400
        p['valuation_complete'] = bool(valid);complete = complete and bool(valid)
    orders = [{**dict(r), 'origin': 'global'} for r in store.db.execute('SELECT * FROM global_orders WHERE status IN '+OPEN)]
    return {'positions': positions, 'orders': orders, 'market_value_cents': total, 'valuation_complete': complete}


def exit_reason(store, config, symbol, p, q, at):
    if not p or not p['qty'] or not q:
        return None
    from .portfolio_risk import halted
    if halted(store):
        return 'ACCOUNT_DRAWDOWN_EXIT'
    value = notional(p['qty'], q['price_micros'], q['fx_micros'])
    if value*10000 <= p['cost_cents']*(10000-config['paper_stop_loss_bps']):
        return 'COST_STOP_TRIGGER'
    if value*10000 >= p['cost_cents']*(10000+config['paper_take_profit_bps']):
        return 'PLAN_EXIT_TRIGGER'
    # Holding duration belongs to the entry plan, never reset by hourly rechecks.
    entry = store.db.execute('''SELECT p.payload_json FROM global_lots l JOIN global_fills f ON f.id=l.id
        JOIN global_orders o ON o.id=f.order_id JOIN global_plans p ON p.id=o.plan_id
        WHERE l.symbol=? AND l.qty>0 ORDER BY l.acquired_at,l.id LIMIT 1''', (symbol,)).fetchone()
    if entry and (datetime.fromisoformat(at)-datetime.fromisoformat(p['first_at'])).total_seconds() >= json.loads(entry[0])['holding_days']*86400:
        return 'DAY_HORIZON_EXIT'
    return None


def submit(store, config, symbol, side, q, plan, item, at):
    from .cloud_runtime import execution_allowed
    execution_allowed(store,config,at)
    from .paper import account_inside_transaction, fee
    from .portfolio_risk import refresh, halted, valuation_ready
    from .global_research import eligibility
    if not enabled(config) or config['mode'] != 'paper':
        return {'status': 'BLOCKED', 'reason': 'PAPER_ONLY'}
    from . import portfolio_strategy as ps
    if side not in ('BUY', 'SELL'):
        raise ValueError('无效方向')
    refresh(store, config, at)
    intent = (plan['id'] if plan else symbol)+':'+side+(':'+at[:16] if side == 'SELL' else '')
    if side=='BUY' and ps.enabled(config):
        d=ps.decision(store,config,'global',symbol,at)
        intent+=':portfolio:'+(d['portfolio_id'] if d else 'pending')
    store.db.execute('BEGIN IMMEDIATE')
    try:
        from .portfolio_risk import evaluate_inside,valuation_ready
        risk=evaluate_inside(store,config,at)
        if side=='BUY' and risk['halted']:
            store.db.commit();return {'status':'BLOCKED','reason':'ACCOUNT_DRAWDOWN_HALT'}
        old = store.db.execute('SELECT * FROM global_orders WHERE intent_key=?', (intent,)).fetchone()
        if old:
            store.db.commit();return dict(old)
        if not fresh(q, at) or not session_open(symbol, at, json.loads(q['payload_json'])):
            raise ValueError('报价/汇率过期或市场休市')
        a = account_inside_transaction(store, at)
        p = a.get('global_positions', {}).get(symbol)
        if any(o['status'] == 'UNKNOWN' for o in a['orders']):
            raise ValueError('存在待核对委托')
        if any(o['symbol'] == symbol for o in a['orders']):
            raise ValueError('已有挂单')
        terms = {**{k: v for k, v in config.items() if k.startswith('paper_')}, 'paper_sell_tax_bps': 0}
        if side == 'BUY':
            if halted(store):
                raise ValueError('ACCOUNT_DRAWDOWN_HALT')
            # Re-read plan in the reservation transaction, reject superseded inference.
            current = store.db.execute('SELECT * FROM global_plans WHERE id=?', (plan['id'],)).fetchone() if plan else None
            blockers = eligibility(store, dict(current) if current else None, item, at)
            if blockers:
                raise ValueError('；'.join(blockers))
            if p or not valuation_ready(store,a,at) or a['withdrawal_reserved_cents']:
                raise ValueError('已有持仓、估值缺失或提取处理中')
            content = json.loads(current['payload_json']);levels = content['levels']
            if not levels['buy_low_micros'] <= q['price_micros'] <= levels['buy_high_micros']:
                raise ValueError('报价不在买入区间')
            limit = min(levels['buy_high_micros'], (q['price_micros']*(10000+config['paper_slippage_bps'])+9999)//10000)
            # Include a 1% FX buffer, but settlement still checks actual reserved funds.
            reserve_fx = (q['fx_micros']*101+99)//100
            budget = min(a['available_cents'], a['equity_cents']*min(5, config['paper_max_stock_pct'])//100,
                         a['equity_cents']*config['paper_max_gross_pct']//100-a['market_value_cents']-a['reserved_cents'])
            if ps.enabled(config) and store.db.execute("SELECT 1 FROM global_orders o JOIN global_fills f ON f.order_id=o.id WHERE o.plan_id=? AND f.side='BUY' LIMIT 1",(plan['id'],)).fetchone():raise ValueError('该研究已执行入场')
            budget=min(budget,ps.buy_budget(store,config,'global',symbol,at,a))
            step = SCALE if symbol.startswith('US:') else 10_000
            qty = max(0, (budget-config['paper_min_fee_cents'])*SCALE*10**12//(limit*reserve_fx*100)//step*step)
            while qty > 0:
                gross = notional(qty, limit, reserve_fx)
                if gross+fee(terms, 'BUY', gross) <= budget:
                    break
                qty -= step
            if qty <= 0:
                raise ValueError('可用额度不足')
            reserved = gross+fee(terms, 'BUY', gross)
            reason = 'DAY_PLAN_ENTRY'
        else:
            reason = exit_reason(store, config, symbol, p, q, at)
            portfolio_exit=not reason
            qty=ps.exit_quantity(store,config,'global',symbol,at,q) if portfolio_exit else p['qty']
            if not qty:
                raise ValueError('退出条件未满足')
            if portfolio_exit:reason='PORTFOLIO_REDUCE'
            reserved = 0
            limit = max(1, q['price_micros']*(10000-config['paper_slippage_bps'])//10000)
        oid = digest('global:'+intent)[:24]
        expiry = normalize_time((datetime.fromisoformat(at)+timedelta(seconds=config['paper_order_ttl_seconds'])).isoformat())
        if side == 'BUY':
            expiry = min(expiry, plan['valid_until'])
        terms.update(portfolio_decision=ps.order_context(store,config,'global',symbol,at),portfolio_exit=side=='SELL' and portfolio_exit,reason=reason, currency='USD', qty_scale=SCALE, quote_id=q['id'], plan_id=plan['id'] if plan else None)
        store.db.execute('INSERT INTO global_orders VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?)',
                         (oid, intent, plan['id'] if plan else None, symbol, side, qty, limit, reserved, at, expiry, 'OPEN', json.dumps(terms)))
        store.db.commit()
        return dict(store.db.execute('SELECT * FROM global_orders WHERE id=?', (oid,)).fetchone())
    except ValueError as exc:
        store.db.rollback();return {'status': 'BLOCKED', 'reason': str(exc)}
    except BaseException:
        store.db.rollback();raise


def settle(store, config, at, selected):
    from .cloud_runtime import execution_allowed,fence
    execution_allowed(store,config,at)
    fence(store,config,at)
    from .paper import account_inside_transaction, fee
    from .portfolio_risk import refresh, halted, valuation_ready
    from .global_research import eligibility
    refresh(store, config, at)
    store.db.execute('BEGIN IMMEDIATE')
    fills = []
    from . import portfolio_strategy as ps
    try:
        for row in store.db.execute("SELECT * FROM global_orders WHERE status IN ('OPEN','PARTIAL') ORDER BY created_at,id").fetchall():
            o = dict(row)
            def cancel(status='CANCELLED'):
                store.db.execute('UPDATE global_orders SET status=?,reserved_cents=0 WHERE id=?', (status, o['id']))
            if o['expires_at'] <= at:
                cancel('EXPIRED');continue
            plan = store.db.execute('SELECT * FROM global_plans WHERE id=?', (o['plan_id'],)).fetchone()
            if o['side'] == 'BUY' and (halted(store) or eligibility(store, dict(plan) if plan else None, selected.get(o['symbol']), at)):
                cancel();continue
            q = latest(store, o['symbol'], at)
            if not fresh(q, at) or q['observed_at'] <= o['created_at'] or not session_open(o['symbol'], at, json.loads(q['payload_json'])):
                continue
            if store.db.execute('SELECT 1 FROM global_fills WHERE order_id=? AND quote_id=?', (o['id'], q['id'])).fetchone():
                continue
            terms = json.loads(o['payload_json'])
            price = (q['price_micros']*(10000+terms['paper_slippage_bps'])+9999)//10000 if o['side'] == 'BUY' else q['price_micros']*(10000-terms['paper_slippage_bps'])//10000
            if o['side'] == 'BUY' and price > o['limit_micros'] or o['side'] == 'SELL' and price < o['limit_micros']:
                continue
            step = SCALE if o['symbol'].startswith('US:') else 10_000
            # At most CNY 5,000 per new observation; one whole share allowed above that.
            max_qty = max(step, 500_000*SCALE*10**12//(price*q['fx_micros']*100)//step*step)
            qty = min(o['qty']-o['filled_qty'], max_qty)
            if terms.get('portfolio_exit'):
                qty=min(qty,ps.exit_quantity(store,config,'global',o['symbol'],at,q))
                if not qty:
                    cancel();continue
            gross = notional(qty, price, q['fx_micros'])
            prior = store.db.execute('SELECT coalesce(sum(gross_cents),0),coalesce(sum(fee_cents),0) FROM global_fills WHERE order_id=?', (o['id'],)).fetchone()
            costs = fee(terms, o['side'], prior[0]+gross)-prior[1]
            a = account_inside_transaction(store, at)
            realized = 0
            if o['side'] == 'BUY':
                available = a['cash_cents']-a['reserved_cents']+o['reserved_cents']-a['withdrawal_reserved_cents']
                gross_room = a['equity_cents']*config['paper_max_gross_pct']//100-a['market_value_cents']-(a['reserved_cents']-o['reserved_cents'])
                own = a['global_positions'].get(o['symbol'], {}).get('market_value_cents', 0)
                stock_room = a['equity_cents']*min(5, config['paper_max_stock_pct'])//100-own
                portfolio_room=o['reserved_cents']
                if ps.enabled(config):
                    try:portfolio_room=ps.fill_budget(store,config,'global',o['symbol'],at,a,o)
                    except ValueError:
                        cancel();continue
                if not valuation_ready(store,a,at) or gross+costs > min(available, o['reserved_cents'], gross_room, stock_room,portfolio_room):
                    cancel();continue
                store.db.execute("UPDATE paper_accounts SET cash_cents=cash_cents-? WHERE id='DEMO_PAPER'", (gross+costs,))
            else:
                if a['cash_cents']+gross<costs:
                    cancel();continue
                remaining = qty;basis = 0
                for lot in store.db.execute('SELECT * FROM global_lots WHERE symbol=? AND qty>0 ORDER BY acquired_at,id', (o['symbol'],)).fetchall():
                    take = min(remaining, lot['qty']);allocated = lot['cost_cents']*take//lot['qty']
                    store.db.execute('UPDATE global_lots SET qty=qty-?,cost_cents=cost_cents-? WHERE id=?', (take, allocated, lot['id']))
                    remaining -= take;basis += allocated
                    if not remaining:break
                if remaining:
                    raise ValueError('禁止卖空：份额不足')
                realized = gross-costs-basis
                store.db.execute("UPDATE paper_accounts SET cash_cents=cash_cents+? WHERE id='DEMO_PAPER'", (gross-costs,))
            fid = digest(o['id']+q['id'])[:24]
            store.db.execute('INSERT INTO global_fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (fid, o['id'], q['id'], o['symbol'], o['side'], qty, price, q['fx_micros'], gross, costs, realized, at, at))
            if o['side'] == 'BUY':
                store.db.execute('INSERT INTO global_lots VALUES(?,?,?,?,?)', (fid, o['symbol'], qty, gross+costs, at))
            filled = o['filled_qty']+qty;left = o['qty']-filled
            reserved = max(0, o['reserved_cents']-gross-costs) if left and o['side'] == 'BUY' else 0
            store.db.execute('UPDATE global_orders SET filled_qty=?,reserved_cents=?,status=? WHERE id=?', (filled, reserved, 'PARTIAL' if left else 'FILLED', o['id']))
            fills.append(fid)
        store.db.commit()
    except BaseException:
        store.db.rollback();raise
    return fills


def tick(store, config, at=None, fetch_quotes=True):
    if not enabled(config):
        return {'status': 'DISABLED'}
    from .investment_policy import seed
    from .global_market import refresh
    from .global_research import active_plan
    at = normalize_time(at or now());seed(store, at)
    selected = targets(store, config, at)
    market = refresh(store, list(selected)) if fetch_quotes else None
    at = normalize_time(now()) if fetch_quotes else at
    fills = settle(store, config, at, selected)
    held = balance(store, at)['positions'];results = []
    for symbol in sorted(selected, key=lambda s: (s not in held, s)):
        q = latest(store, symbol, at);p = held.get(symbol);plan = active_plan(store, symbol, at)
        reason = exit_reason(store, config, symbol, p, q, at)
        if not reason:
            from .portfolio_strategy import exit_quantity
            if exit_quantity(store,config,'global',symbol,at,q):reason='PORTFOLIO_REDUCE'
        if reason or not p:
            result = submit(store, config, symbol, 'SELL' if reason else 'BUY', q, plan, selected[symbol], at)
            results.append({'symbol': symbol, **result})
    from .paper import mark_equity
    mark_equity(store, at)
    return {'status': 'SUCCEEDED', 'market': market, 'fills': len(fills), 'submitted': sum('id' in r for r in results),
            'checks': [{'symbol': r['symbol'], 'status': r['status'], 'reason': r.get('reason')} for r in results]}
