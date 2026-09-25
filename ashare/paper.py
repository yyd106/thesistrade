"""Local-only paper execution. No broker client, account secret or live order endpoint exists."""
from __future__ import annotations
import json
import re
from datetime import datetime, timedelta
from .calendar import local, phase
from .finance import PaperLedger
from .storage import digest, now, normalize_time

OPEN_STATES=('OPEN','PARTIAL','UNKNOWN')


def fee(config,side,notional):
    commission=max(config['paper_min_fee_cents'],(notional*config['paper_commission_bps']+9999)//10000)
    return commission + ((notional*config['paper_sell_tax_bps']+9999)//10000 if side=='SELL' else 0)


def positions(store,at):
    day=local(at).date().isoformat()
    result={}
    for r in store.db.execute('SELECT * FROM paper_lots WHERE qty>0'):
        p=result.setdefault(r['symbol'],{'qty':0,'sellable_qty':0,'cost_cents':0})
        p['qty']+=r['qty'];p['cost_cents']+=r['cost_cents']
        if r['acquired_day']<day:p['sellable_qty']+=r['qty']
    return result


def account(store,at):
    PaperLedger(store).initialize()
    a=dict(store.db.execute("SELECT * FROM paper_accounts WHERE id='DEMO_PAPER'").fetchone())
    p=positions(store,at); market_value=0;complete=True
    for sym,pos in p.items():
        q=store.latest_quote(sym,at)
        pos['mark_cents']=q['price_cents'] if q else None
        pos['quote_at']=q['observed_at'] if q else None
        value=pos['qty']*q['price_cents'] if q else pos['cost_cents']
        pos['market_value_cents']=value;market_value+=value
        if not q or local(q['observed_at']).date()!=local(at).date():complete=False
    from .dynamic_paper import balance as dynamic_balance
    dynamic=dynamic_balance(store,at)
    from .global_paper import balance as global_balance
    global_account=global_balance(store,at)
    orders=[dict(r) for r in store.db.execute("SELECT * FROM paper_orders WHERE status IN ('OPEN','PARTIAL','UNKNOWN')")]+dynamic['orders']+global_account['orders']
    reserved=sum(o['reserved_cents'] for o in orders if o['side']=='BUY')
    withdrawal=store.db.execute("SELECT coalesce(sum(planned_cents-transferred_cents),0) FROM paper_withdrawals WHERE status!='COMPLETED'").fetchone()[0]
    market_value+=dynamic['market_value_cents']+global_account['market_value_cents'];complete=complete and dynamic['valuation_complete'] and global_account['valuation_complete']
    a.update({'positions':p,'dynamic_positions':dynamic['positions'],'global_positions':global_account['positions'],'orders':orders,'reserved_cents':reserved,'withdrawal_reserved_cents':withdrawal,
        'available_cents':max(0,a['cash_cents']-reserved-withdrawal),
        'equity_cents':a['cash_cents']+market_value,'market_value_cents':market_value,'valuation_complete':complete})
    return a


def mark_equity(store,at):
    a=account(store,at)
    dynamic_realized=store.db.execute('SELECT coalesce(sum(realized_cents),0) FROM dynamic_fills').fetchone()[0]
    dynamic_unrealized=sum(p['market_value_cents']-p['cost_cents'] for p in a.get('dynamic_positions',{}).values())
    # Preserve watchlist review attribution while the physical account remains shared.
    global_realized=store.db.execute('SELECT coalesce(sum(realized_cents),0) FROM global_fills').fetchone()[0]
    global_unrealized=sum(p['market_value_cents']-p['cost_cents'] for p in a.get('global_positions',{}).values())
    a['watchlist_equity_cents']=a['equity_cents']-dynamic_realized-dynamic_unrealized-global_realized-global_unrealized
    payload=json.dumps(a,ensure_ascii=False,sort_keys=True)
    with store.db:
        store.db.execute('INSERT OR IGNORE INTO equity_marks VALUES(?,?,?,?,?,?,?,?)',
            (digest(at+payload)[:24],at,now(),a['cash_cents'],a['equity_cents'],a['withdrawn_cents'],int(a['valuation_complete']),payload))
    if a['valuation_complete']:
        PaperLedger(store).plan(a['equity_cents'])
    return a


def quote_ok(q,config,at):
    if not q or q['price_cents']<=0:return False
    age=(datetime.fromisoformat(normalize_time(at))-datetime.fromisoformat(q['observed_at'])).total_seconds()
    return 0<=age<=config['quote_max_age_seconds'] and q['first_seen_at']<=normalize_time(at)


BOARDS={'MAIN':('sh600','sh601','sh603','sh605','sz000','sz001','sz002','sz003'),
        'CHINEXT':('sz300','sz301'),'STAR':('sh688','sh689')}
# Daily price limits are 10% (main board) and 20% (ChiNext/STAR); stay clear of the limit itself.
LIMIT_GUARD={'MAIN':0.095,'CHINEXT':0.195,'STAR':0.195}
# Risk warning (ST/*ST/S*ST), delisting arrangement (退), first-days listing prefixes (N/C + Chinese name).
SPECIAL_NAME=re.compile(r'^S?\*?ST|退|^[NC](?=[^\x00-\x7f])')


def board(symbol):
    return next((name for name,prefixes in BOARDS.items() if symbol.startswith(prefixes)),None)


def lot_rules(symbol):
    """STAR buy orders need at least 200 shares; every supported board steps in 100 shares here."""
    b=board(symbol)
    if not b:return None
    return {'board':b,'min_buy':200 if b=='STAR' else 100,'step':100,'min_sell':200 if b=='STAR' else 1}


def sell_quantity(symbol,wanted,held,sellable):
    """Round a sell to the board's declaration rule. STAR: each sell >=200 shares unless the whole
    remaining holding is below 200, which must then be sold in one order. Returns 0 if impossible now."""
    rules=lot_rules(symbol)
    wanted=min(wanted,sellable)
    if wanted<=0 or not rules or rules['board']!='STAR':return max(0,wanted)
    if held<200:return held if sellable>=held else 0
    if wanted<200:wanted=200 if sellable>=200 else 0
    return wanted


def market_guard(symbol,q):
    if not board(symbol):return 'UNSUPPORTED_BOARD'
    if SPECIAL_NAME.search((q.get('name') or '').strip().upper()):return 'SPECIAL_SECURITY'
    if abs(q['price_cents']/q['prev_close_cents']-1)>=LIMIT_GUARD[board(symbol)]:return 'NEAR_PRICE_LIMIT_OR_CORPORATE_ACTION'
    return None


def submit(store,config,decision_id,plan,side,q,at,*,decision_record=None,slot_input_json=None,portfolio_exit=False):
    from .cloud_runtime import execution_allowed
    execution_allowed(store,config,at)
    """A single SQLite write transaction serializes cash reservation across all stocks."""
    if config['mode']!='paper':return {'status':'BLOCKED','reason':'RESEARCH_MODE'}
    if side not in ('BUY','SELL'):raise ValueError('无效方向')
    symbol=plan['symbol'];pid=plan['id'];at=normalize_time(at)
    from .portfolio_risk import refresh, halted
    refresh(store,config,at)
    if side=='BUY' and halted(store):return {'status':'BLOCKED','reason':'ACCOUNT_DRAWDOWN_HALT'}
    store.db.execute('BEGIN IMMEDIATE')
    try:
        from .portfolio_risk import evaluate_inside
        risk=evaluate_inside(store,config,at)
        if side=='BUY' and risk['halted']:
            store.db.commit();return {'status':'BLOCKED','reason':'ACCOUNT_DRAWDOWN_HALT'}
        old=store.db.execute('SELECT * FROM paper_orders WHERE decision_id=?',(decision_id,)).fetchone()
        if old:
            store.db.commit();return dict(old)
        fresh=store.db.execute('SELECT * FROM plans WHERE id=?',(pid,)).fetchone()
        from . import portfolio_strategy as ps
        reduction_qty=ps.exit_quantity(store,config,'watchlist',symbol,at,q) if side=='SELL' and portfolio_exit else 0
        if not fresh or (not reduction_qty and (fresh['status']!='ACTIVE' or not fresh['activated_at']<=at<fresh['valid_until'])):
            raise ValueError('PLAN_INACTIVE')
        if phase(at)!='CONTINUOUS':raise ValueError('MARKET_CLOSED')
        if not quote_ok(q,config,at):raise ValueError('STALE_QUOTE')
        guard=market_guard(symbol,q)
        if guard:raise ValueError(guard)
        a=account_inside_transaction(store,at)
        p=a['positions'].get(symbol,{'qty':0,'sellable_qty':0,'cost_cents':0})
        params=json.loads(fresh['payload_json']);levels=params.get('levels') or {}
        price=q['price_cents'];lot=config['paper_lot_size']
        same=[o for o in a['orders'] if o['symbol']==symbol]
        if any(o['status']=='UNKNOWN' for o in a['orders']):raise ValueError('UNKNOWN_ORDER_RECONCILE_REQUIRED')
        if same:raise ValueError('EXISTING_OPEN_ORDER')
        if side=='BUY':
            from .plan_recheck import check
            blockers=check(store,config,dict(fresh),at)
            if blockers:raise ValueError('PLAN_RECHECK: '+','.join(blockers))
            if params['kind']!='PAPER_TRADE':raise ValueError('NO_ENTRY_PLAN')
            if not levels['buy_low_cents']<=price<=levels['buy_high_cents']:raise ValueError('OUTSIDE_BUY_ZONE')
            if a['withdrawal_reserved_cents']:raise ValueError('WITHDRAWAL_PENDING')
            if not a['valuation_complete']:raise ValueError('VALUATION_INCOMPLETE')
            limit=min(levels['buy_high_cents'],(price*(10000+config['paper_slippage_bps'])+9999)//10000)
            stock_budget=a['equity_cents']*min(params['max_stock_pct'],config['paper_max_stock_pct'])//100-(p['qty']+a.get('dynamic_positions',{}).get(symbol,{}).get('qty',0))*price
            gross_budget=a['equity_cents']*config['paper_max_gross_pct']//100-a['market_value_cents']-a['reserved_cents']
            minimum=lot_rules(symbol)['min_buy']
            other=min(gross_budget,a['available_cents'],ps.buy_budget(store,config,'watchlist',symbol,at,a))
            budget=min(stock_budget,other)
            qty=max(0,budget//limit//lot*lot)
            while qty>0 and qty*limit+fee(config,'BUY',qty*limit)>budget:qty-=lot
            if qty<minimum:
                # Name the binding constraint: a whole minimum lot larger than the per-stock cap is structural.
                smallest=minimum*limit+fee(config,'BUY',minimum*limit)
                cap=a['equity_cents']*min(params['max_stock_pct'],config['paper_max_stock_pct'])//100
                raise ValueError('LOT_EXCEEDS_CAP' if smallest>cap and other>=smallest else 'INSUFFICIENT_BUDGET_OR_TARGET_REACHED')
            reserve=qty*limit+fee(config,'BUY',qty*limit)
        else:
            qty=min(p['sellable_qty'],reduction_qty) if portfolio_exit else p['sellable_qty']
            qty=sell_quantity(symbol,qty,p['qty'],p['sellable_qty'])
            if qty<=0:raise ValueError('T_PLUS_ONE_OR_NO_POSITION')
            limit=max(1,price*(10000-config['paper_slippage_bps'])//10000)
            reserve=0
        oid=digest(decision_id)[:24]
        expires=normalize_time((datetime.fromisoformat(at)+timedelta(seconds=config['paper_order_ttl_seconds'])).isoformat())
        if not portfolio_exit:expires=min(fresh['valid_until'],expires)
        if decision_record is not None:
            if (decision_record['id'],decision_record['symbol'],decision_record['plan_id'],decision_record['action'],decision_record['at'])!=(decision_id,symbol,pid,side,at):
                raise ValueError('DECISION_RECORD_MISMATCH')
            # An operation, its input and the order are committed together. Rejected
            # attempts never leave a historical decision behind.
            store.db.execute('INSERT INTO decisions VALUES(:id,:slot_id,:symbol,:plan_id,:at,:action,:status,:reason,:payload_json)',decision_record)
            if slot_input_json is not None:
                store.db.execute('UPDATE slots SET input_json=? WHERE id=?',(slot_input_json,decision_record['slot_id']))
        store.db.execute('INSERT INTO paper_orders VALUES(?,?,?,?,?,?,0,?,?,?,?,?)',
            (oid,decision_id,pid,symbol,side,qty,limit,reserve,at,expires,'OPEN'))
        store.db.execute('INSERT INTO paper_order_terms VALUES(?,?)',(oid,json.dumps({**{k:v for k,v in config.items() if k.startswith('paper_')},'portfolio_decision':ps.order_context(store,config,'watchlist',symbol,at),'portfolio_exit':bool(portfolio_exit)},sort_keys=True)))
        store.db.commit()
        return dict(store.db.execute('SELECT * FROM paper_orders WHERE id=?',(oid,)).fetchone())
    except ValueError as exc:
        store.db.rollback();return {'status':'BLOCKED','reason':str(exc)}
    except BaseException:
        store.db.rollback();raise


def account_inside_transaction(store,at):
    # account() must not initialize/commit in the reservation transaction.
    original=store.db.in_transaction
    if not original:raise RuntimeError('需要资金事务')
    return _account_without_initialize(store,at)


def _account_without_initialize(store,at):
    a=dict(store.db.execute("SELECT * FROM paper_accounts WHERE id='DEMO_PAPER'").fetchone())
    p=positions(store,at);mv=0;complete=True
    for sym,pos in p.items():
        q=store.latest_quote(sym,at)
        value=q['price_cents']*pos['qty'] if q else pos['cost_cents']
        pos['market_value_cents']=value;mv+=value
        if not q or local(q['observed_at']).date()!=local(at).date():complete=False
    from .dynamic_paper import balance as dynamic_balance
    dynamic=dynamic_balance(store,at)
    from .global_paper import balance as global_balance
    global_account=global_balance(store,at)
    orders=[dict(r) for r in store.db.execute("SELECT * FROM paper_orders WHERE status IN ('OPEN','PARTIAL','UNKNOWN')")]+dynamic['orders']+global_account['orders']
    reserved=sum(o['reserved_cents'] for o in orders if o['side']=='BUY')
    withdrawal=store.db.execute("SELECT coalesce(sum(planned_cents-transferred_cents),0) FROM paper_withdrawals WHERE status!='COMPLETED'").fetchone()[0]
    mv+=dynamic['market_value_cents']+global_account['market_value_cents'];complete=complete and dynamic['valuation_complete'] and global_account['valuation_complete']
    return {**a,'positions':p,'dynamic_positions':dynamic['positions'],'global_positions':global_account['positions'],'orders':orders,'reserved_cents':reserved,'withdrawal_reserved_cents':withdrawal,
        'available_cents':max(0,a['cash_cents']-reserved-withdrawal),'market_value_cents':mv,
        'equity_cents':a['cash_cents']+mv,'valuation_complete':complete}


def settle(store,config,at):
    from .cloud_runtime import execution_allowed,fence
    execution_allowed(store,config,at)
    fence(store,config,at)
    """Match only quotes strictly after order creation. Conservative lot-limited fills, no queue model."""
    at=normalize_time(at);results=[]
    from .portfolio_risk import refresh
    refresh(store,config,at)
    store.db.execute('BEGIN IMMEDIATE')
    try:
        for order in store.db.execute("SELECT * FROM paper_orders WHERE status IN ('OPEN','PARTIAL') ORDER BY created_at,id").fetchall():
            o=dict(order)
            terms=store.db.execute('SELECT config_json FROM paper_order_terms WHERE order_id=?',(o['id'],)).fetchone()
            order_config={**config,**json.loads(terms[0])} if terms else config
            plan=store.db.execute('SELECT * FROM plans WHERE id=?',(o['plan_id'],)).fetchone()
            from . import portfolio_strategy as ps
            portfolio_exit=order_config.get('portfolio_exit',False)
            if o['expires_at']<=at or not plan or (not portfolio_exit and (plan['status']!='ACTIVE' or plan['valid_until']<=at)):
                store.db.execute("UPDATE paper_orders SET status='EXPIRED',reserved_cents=0 WHERE id=?",(o['id'],));continue
            q=store.latest_quote(o['symbol'],at)
            if o['side']=='BUY':
                from .portfolio_risk import halted
                from .plan_recheck import check
                if halted(store) or check(store,config,dict(plan),at):
                    store.db.execute("UPDATE paper_orders SET status='CANCELLED',reserved_cents=0 WHERE id=?",(o['id'],));continue
            if phase(at)!='CONTINUOUS' or not quote_ok(q,config,at) or q['observed_at']<=o['created_at'] or market_guard(o['symbol'],q):continue
            if store.db.execute('SELECT 1 FROM paper_fills WHERE order_id=? AND quote_id=?',(o['id'],q['id'])).fetchone():continue
            price=(q['price_cents']*(10000+order_config['paper_slippage_bps'])+9999)//10000 if o['side']=='BUY' else q['price_cents']*(10000-order_config['paper_slippage_bps'])//10000
            if (o['side']=='BUY' and price>o['limit_cents']) or (o['side']=='SELL' and price<o['limit_cents']):continue
            qty=min(o['qty']-o['filled_qty'],order_config['paper_max_fill_qty'])
            if portfolio_exit:
                qty=min(qty,ps.exit_quantity(store,config,'watchlist',o['symbol'],at,q))
                if not qty:
                    store.db.execute("UPDATE paper_orders SET status='CANCELLED',reserved_cents=0 WHERE id=?",(o['id'],));continue
            gross=qty*price
            # Commission minimum applies to the order's cumulative consideration, not each partial fill.
            prev=store.db.execute('SELECT coalesce(sum(qty*price_cents),0),coalesce(sum(fee_cents),0) FROM paper_fills WHERE order_id=?',(o['id'],)).fetchone()
            costs=fee(order_config,o['side'],prev[0]+gross)-prev[1]
            realized=0
            fid=digest(o['id']+':'+q['id'])[:24]
            if o['side']=='BUY':
                if ps.enabled(config):
                    try:
                        if gross+costs>ps.fill_budget(store,config,'watchlist',o['symbol'],at,account_inside_transaction(store,at),o):raise ValueError('组合成交额度不足')
                    except ValueError:
                        store.db.execute("UPDATE paper_orders SET status='CANCELLED',reserved_cents=0 WHERE id=?",(o['id'],));continue
                cash=store.db.execute("SELECT cash_cents FROM paper_accounts WHERE id='DEMO_PAPER'").fetchone()[0]
                if cash<gross+costs:raise ValueError('模拟资金账本透支')
                store.db.execute("UPDATE paper_accounts SET cash_cents=cash_cents-? WHERE id='DEMO_PAPER'",(gross+costs,))
            else:
                remaining=qty;cost_basis=0
                for lot in store.db.execute('SELECT * FROM paper_lots WHERE symbol=? AND qty>0 AND acquired_day<? ORDER BY acquired_day,id',(o['symbol'],local(at).date().isoformat())).fetchall():
                    take=min(remaining,lot['qty']);allocated=lot['cost_cents']*take//lot['qty']
                    store.db.execute('UPDATE paper_lots SET qty=qty-?,cost_cents=cost_cents-? WHERE id=?',(take,allocated,lot['id']))
                    cost_basis+=allocated;remaining-=take
                    if not remaining:break
                if remaining:raise ValueError('模拟可卖数量不足')
                realized=gross-costs-cost_basis
                store.db.execute("UPDATE paper_accounts SET cash_cents=cash_cents+? WHERE id='DEMO_PAPER'",(gross-costs,))
            store.db.execute('INSERT INTO paper_fills VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (fid,o['id'],q['id'],o['symbol'],o['side'],qty,price,costs,realized,q['observed_at'],at))
            if o['side']=='BUY':store.db.execute('INSERT INTO paper_lots VALUES(?,?,?,?,?)',(fid,o['symbol'],qty,gross+costs,local(at).date().isoformat()))
            filled=o['filled_qty']+qty;remaining=o['qty']-filled
            reserve=remaining*o['limit_cents']+max(0,fee(order_config,'BUY',prev[0]+gross+remaining*o['limit_cents'])-prev[1]-costs) if o['side']=='BUY' and remaining else 0
            store.db.execute('UPDATE paper_orders SET filled_qty=?,reserved_cents=?,status=? WHERE id=?',(filled,reserve,'FILLED' if not remaining else 'PARTIAL',o['id']))
            results.append({'fill_id':fid,'order_id':o['id'],'symbol':o['symbol'],'side':o['side'],'qty':qty,'price_cents':price})
        store.db.commit()
    except BaseException:
        store.db.rollback();raise
    return results
