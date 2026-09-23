"""News-strategy paper orders and lots; the only shared mutation is account cash."""
from __future__ import annotations
import json
from datetime import datetime,timedelta
from .calendar import local,phase,trading_day
from .storage import normalize_time,digest,now
from .dynamic_sources import latest_quote

OPEN="('OPEN','PARTIAL','UNKNOWN')"

def positions(store,at):
 result={};day=local(at).date().isoformat()
 for r in store.db.execute('SELECT * FROM dynamic_lots WHERE qty>0'):
  p=result.setdefault(r['symbol'],{'qty':0,'sellable_qty':0,'cost_cents':0})
  p['qty']+=r['qty'];p['cost_cents']+=r['cost_cents']
  if r['acquired_day']<day:p['sellable_qty']+=r['qty']
 return result

def balance(store,at):
 p=positions(store,at);mv=0;complete=True
 for sym,v in p.items():
  q=latest_quote(store,sym,at);value=v['qty']*q['price_cents'] if q else v['cost_cents']
  v.update(market_value_cents=value,mark_cents=q['price_cents'] if q else None,quote_at=q['observed_at'] if q else None)
  mv+=value
  if not q or local(q['observed_at']).date()!=local(at).date():complete=False
 orders=[{**dict(r),'origin':'dynamic'} for r in store.db.execute('SELECT * FROM dynamic_orders WHERE status IN '+OPEN)]
 return {'positions':p,'orders':orders,'market_value_cents':mv,'valuation_complete':complete,
         'reserved_cents':sum(o['reserved_cents'] for o in orders if o['side']=='BUY')}

def case_position(store,cid,at):
 rows=list(store.db.execute('SELECT * FROM dynamic_lots WHERE case_id=? AND qty>0',(cid,)))
 return {'qty':sum(r['qty'] for r in rows),'cost_cents':sum(r['cost_cents'] for r in rows),
         'sellable_qty':sum(r['qty'] for r in rows if r['acquired_day']<local(at).date().isoformat()),
         'first_day':min((r['acquired_day'] for r in rows),default=None)}

def exit_reason(case,p,q,at):
 if not p['qty']:return None
 plan=json.loads(case['plan_json'])
 if case['status']=='INVALIDATED':return '事件判断已失效'
 price=q['price_cents']
 if price*p['qty']*10000<=p['cost_cents']*(10000-plan.get('stop_bps',600)):return '动态持仓成本止损'
 if price*p['qty']*10000>=p['cost_cents']*(10000+plan.get('take_profit_bps',1000)):return '动态持仓止盈'
 first=datetime.fromisoformat(p['first_day']).date();today=local(at).date()
 days=sum(trading_day(first+timedelta(days=i)) is True for i in range(1,(today-first).days+1))
 if days>=plan.get('holding_days',3):return '事件持有期结束'
 return None

def submit(store,config,case,side,q,at,reason):
 from .cloud_runtime import execution_allowed
 execution_allowed(store,config,at)
 from . import portfolio_strategy as ps
 from .paper import account_inside_transaction,quote_ok,market_guard,fee
 at=normalize_time(at)
 if config['mode']!='paper':return {'status':'BLOCKED','reason':'当前为研究模式'}
 if side not in ('BUY','SELL'):raise ValueError('无效方向')
 cid=case['id'];sym=case['symbol']
 # A BUY is a single intent for the entire case, even after it expires/fills.
 # SELL can retry after an unfilled order expires, using a new minute key.
 intent=cid+':'+side+(':'+at[:16] if side=='SELL' else '')
 if side=='BUY' and ps.enabled(config):
  d=ps.decision(store,config,'dynamic',cid,at)
  intent+=':portfolio:'+(d['portfolio_id'] if d else 'pending')
 from .portfolio_risk import refresh,halted
 refresh(store,config,at)
 if side=='BUY' and halted(store):return {'status':'BLOCKED','reason':'ACCOUNT_DRAWDOWN_HALT'}
 store.db.execute('BEGIN IMMEDIATE')
 try:
  from .portfolio_risk import evaluate_inside
  risk=evaluate_inside(store,config,at)
  if side=='BUY' and risk['halted']:
   store.db.commit();return {'status':'BLOCKED','reason':'ACCOUNT_DRAWDOWN_HALT'}
  old=store.db.execute('SELECT * FROM dynamic_orders WHERE intent_key=?',(intent,)).fetchone()
  if old:store.db.commit();return dict(old)
  fresh=store.db.execute('SELECT * FROM dynamic_cases WHERE id=?',(cid,)).fetchone()
  if not fresh:raise ValueError('动态计划不存在')
  plan=json.loads(fresh['plan_json']);p=case_position(store,cid,at)
  if phase(at)!='CONTINUOUS':raise ValueError('当前不在交易时段')
  if not quote_ok(q,config,at):raise ValueError('动态报价过期')
  guard=market_guard(sym,q)
  if guard:raise ValueError(guard)
  a=account_inside_transaction(store,at)
  if any(o['status']=='UNKNOWN' for o in a['orders']):raise ValueError('共用账户有待核对委托')
  if any(o['symbol']==sym for o in a['orders']):raise ValueError('共用账户该股票已有挂单')
  if side=='BUY':
   from .dynamic import eligibility
   blockers,validated=eligibility(store,config,dict(fresh),at)
   if fresh['status']!='READY' or blockers or not fresh['created_at']<=at<fresh['expires_at']:
    raise ValueError('动态计划未满足执行条件：'+'；'.join(blockers))
   if p['qty'] or store.db.execute('SELECT 1 FROM dynamic_lots WHERE symbol=? AND qty>0',(sym,)).fetchone():raise ValueError('动态线路已持有该股票')
   low,high=plan.get('buy_low_cents',0),plan.get('buy_high_cents',0)
   if not low<=q['price_cents']<=high:raise ValueError('已超出事件入场区间')
   if a['withdrawal_reserved_cents']:raise ValueError('共用底池有待处理提取')
   if not a['valuation_complete']:raise ValueError('共用底池持仓估值不完整')
   limit=min(high,(q['price_cents']*(10000+config['paper_slippage_bps'])+9999)//10000)
   own=a['positions'].get(sym,{}).get('qty',0)+a.get('dynamic_positions',{}).get(sym,{}).get('qty',0)
   stock_cap=a['equity_cents']*config['paper_max_stock_pct']//100-own*q['price_cents']
   gross_cap=a['equity_cents']*config['paper_max_gross_pct']//100-a['market_value_cents']-a['reserved_cents']
   if ps.enabled(config) and store.db.execute("SELECT 1 FROM dynamic_orders o JOIN dynamic_fills f ON f.order_id=o.id WHERE o.case_id=? AND f.side='BUY' LIMIT 1",(cid,)).fetchone():raise ValueError('该事件已执行入场')
   budget=min(a['available_cents'],stock_cap,gross_cap,a['equity_cents']*plan.get('max_position_pct',5)//100,ps.buy_budget(store,config,'dynamic',cid,at,a))
   qty=max(0,budget//limit//100*100)
   while qty and qty*limit+fee(config,'BUY',qty*limit)>budget:qty-=100
   if not qty:raise ValueError('共用底池额度不足或已达到仓位上限')
   reserve=qty*limit+fee(config,'BUY',qty*limit)
  else:
   portfolio_exit=not halted(store) and not exit_reason(dict(fresh),p,q,at)
   reduction_qty=ps.exit_quantity(store,config,'dynamic',cid,at,q) if portfolio_exit else 0
   if portfolio_exit and not reduction_qty:raise ValueError('动态退出条件未满足')
   qty=min(p['sellable_qty'],reduction_qty) if portfolio_exit else p['sellable_qty']
   if not qty:raise ValueError('动态持仓受T+1限制或已卖出')
   limit=max(1,q['price_cents']*(10000-config['paper_slippage_bps'])//10000);reserve=0
  expiry=normalize_time((datetime.fromisoformat(at)+timedelta(seconds=config['paper_order_ttl_seconds'])).isoformat())
  if side=='BUY':expiry=min(expiry,fresh['expires_at'])
  oid=digest('dynamic:'+intent)[:24]
  terms={k:v for k,v in config.items() if k.startswith('paper_')}
  terms.update(portfolio_decision=ps.order_context(store,config,'dynamic',cid,at),portfolio_exit=side=='SELL' and portfolio_exit,plan=plan,quote=q,source_news_id=fresh['news_id'],analysis=json.loads(fresh['analysis_json']),
               account_at_order={k:a[k] for k in ('cash_cents','equity_cents','available_cents','reserved_cents','market_value_cents')})
  store.db.execute('INSERT INTO dynamic_orders VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?,?)',
   (oid,intent,cid,sym,side,qty,limit,reserve,at,expiry,'OPEN',reason,json.dumps(terms,ensure_ascii=False)))
  store.db.commit();return dict(store.db.execute('SELECT * FROM dynamic_orders WHERE id=?',(oid,)).fetchone())
 except ValueError as exc:
  store.db.rollback();return {'status':'BLOCKED','reason':str(exc)}
 except BaseException:store.db.rollback();raise

def settle(store,config,at):
 from .cloud_runtime import execution_allowed,fence
 execution_allowed(store,config,at)
 fence(store,config,at)
 from .paper import quote_ok,market_guard,fee
 at=normalize_time(at);fills=[]
 from . import portfolio_strategy as ps
 from .paper import account_inside_transaction
 from .portfolio_risk import refresh,halted
 refresh(store,config,at)
 store.db.execute('BEGIN IMMEDIATE')
 try:
  for row in store.db.execute("SELECT * FROM dynamic_orders WHERE status IN ('OPEN','PARTIAL') ORDER BY created_at,id").fetchall():
   o=dict(row);terms=json.loads(o['terms_json']);terms={**config,**terms}
   if o['side']=='BUY' and halted(store):
    store.db.execute("UPDATE dynamic_orders SET status='CANCELLED',reserved_cents=0 WHERE id=?",(o['id'],));continue
   case=dict(store.db.execute('SELECT * FROM dynamic_cases WHERE id=?',(o['case_id'],)).fetchone())
   if o['expires_at']<=at or (o['side']=='BUY' and (case['status'] not in ('READY','HOLDING') or case['expires_at']<=at)):
    store.db.execute("UPDATE dynamic_orders SET status='EXPIRED',reserved_cents=0 WHERE id=?",(o['id'],));continue
   q=latest_quote(store,o['symbol'],at)
   if phase(at)!='CONTINUOUS' or not quote_ok(q,config,at) or q['observed_at']<=o['created_at'] or market_guard(o['symbol'],q):continue
   if store.db.execute('SELECT 1 FROM dynamic_fills WHERE order_id=? AND quote_id=?',(o['id'],q['id'])).fetchone():continue
   price=(q['price_cents']*(10000+terms['paper_slippage_bps'])+9999)//10000 if o['side']=='BUY' else q['price_cents']*(10000-terms['paper_slippage_bps'])//10000
   if (o['side']=='BUY' and price>o['limit_cents']) or (o['side']=='SELL' and price<o['limit_cents']):continue
   qty=min(o['qty']-o['filled_qty'],terms['paper_max_fill_qty'])
   if terms.get('portfolio_exit'):
    qty=min(qty,ps.exit_quantity(store,config,'dynamic',o['case_id'],at,q))
    if not qty:
     store.db.execute("UPDATE dynamic_orders SET status='CANCELLED',reserved_cents=0 WHERE id=?",(o['id'],));continue
   gross=qty*price
   prev=store.db.execute('SELECT coalesce(sum(qty*price_cents),0),coalesce(sum(fee_cents),0) FROM dynamic_fills WHERE order_id=?',(o['id'],)).fetchone()
   costs=fee(terms,o['side'],prev[0]+gross)-prev[1];realized=0;fid=digest(o['id']+q['id'])[:24]
   if o['side']=='BUY':
    if ps.enabled(config):
     try:
      if gross+costs>ps.fill_budget(store,config,'dynamic',o['case_id'],at,account_inside_transaction(store,at),o):raise ValueError('组合成交额度不足')
     except ValueError:
      store.db.execute("UPDATE dynamic_orders SET status='CANCELLED',reserved_cents=0 WHERE id=?",(o['id'],));continue
    cash=store.db.execute("SELECT cash_cents FROM paper_accounts WHERE id='DEMO_PAPER'").fetchone()[0]
    if cash<gross+costs:raise ValueError('共用现金不足，停止撮合')
    store.db.execute("UPDATE paper_accounts SET cash_cents=cash_cents-? WHERE id='DEMO_PAPER'",(gross+costs,))
   else:
    remaining=qty;basis=0
    for lot in store.db.execute('SELECT * FROM dynamic_lots WHERE case_id=? AND qty>0 AND acquired_day<? ORDER BY acquired_day,id',(o['case_id'],local(at).date().isoformat())).fetchall():
     take=min(remaining,lot['qty']);allocated=lot['cost_cents']*take//lot['qty']
     store.db.execute('UPDATE dynamic_lots SET qty=qty-?,cost_cents=cost_cents-? WHERE id=?',(take,allocated,lot['id']))
     basis+=allocated;remaining-=take
     if not remaining:break
    if remaining:raise ValueError('动态可卖数量不足')
    realized=gross-costs-basis
    store.db.execute("UPDATE paper_accounts SET cash_cents=cash_cents+? WHERE id='DEMO_PAPER'",(gross-costs,))
   store.db.execute('INSERT INTO dynamic_fills VALUES(?,?,?,?,?,?,?,?,?,?,?)',(fid,o['id'],q['id'],o['symbol'],o['side'],qty,price,costs,realized,q['observed_at'],at))
   if o['side']=='BUY':
    store.db.execute('INSERT INTO dynamic_lots VALUES(?,?,?,?,?,?)',(fid,o['case_id'],o['symbol'],qty,gross+costs,local(at).date().isoformat()))
    if case['status']!='INVALIDATED':store.db.execute("UPDATE dynamic_cases SET status='HOLDING' WHERE id=?",(o['case_id'],))
   elif not case_position(store,o['case_id'],at)['qty']:
    store.db.execute("UPDATE dynamic_cases SET status='CLOSED' WHERE id=?",(o['case_id'],))
   filled=o['filled_qty']+qty;left=o['qty']-filled
   reserve=left*o['limit_cents']+max(0,fee(terms,'BUY',prev[0]+gross+left*o['limit_cents'])-prev[1]-costs) if o['side']=='BUY' and left else 0
   store.db.execute('UPDATE dynamic_orders SET filled_qty=?,reserved_cents=?,status=? WHERE id=?',(filled,reserve,'PARTIAL' if left else 'FILLED',o['id']))
   fills.append(fid)
  store.db.commit()
 except BaseException:store.db.rollback();raise
 return fills

def run_slot(store,config,at=None,refresh=True):
 from .paper import quote_ok
 from .dynamic_sources import refresh_quotes
 at=normalize_time(at or now())
 scheduled=at
 if refresh and (datetime.fromisoformat(now())-datetime.fromisoformat(scheduled)).total_seconds()>=config['slot_deadline_seconds']:return {'status':'MISSED','submitted':0,'fills':0}
 cases=[dict(r) for r in store.db.execute("SELECT * FROM dynamic_cases WHERE status='READY' OR id IN (SELECT case_id FROM dynamic_lots WHERE qty>0) ORDER BY created_at,id")]
 symbols={c['symbol'] for c in cases}|{r[0] for r in store.db.execute('SELECT symbol FROM dynamic_orders WHERE status IN '+OPEN)}
 if refresh and symbols:
  try:refresh_quotes(store,symbols)
  except Exception as exc:
   with store.db:store.db.execute("INSERT OR REPLACE INTO dynamic_state VALUES('quote_error',?)",(str(exc)[:200],))
  else:
   with store.db:store.db.execute("DELETE FROM dynamic_state WHERE key='quote_error'")
 if refresh:
  from .dynamic_sources import refresh_announcements
  from .sources import stock_catalog
  from .dynamic import market
  for c in cases:
   if c['status']!='READY' or case_position(store,c['id'],now())['qty']:continue
   if (datetime.fromisoformat(now())-datetime.fromisoformat(scheduled)).total_seconds()>=config['slot_deadline_seconds']-12:break
   m=market(store,c['symbol'],now());checked=m.get('announcement_checked_at')
   if checked and (datetime.fromisoformat(now())-datetime.fromisoformat(checked)).total_seconds()<config['announcement_max_age_seconds']-30:continue
   try:refresh_announcements(store,c['symbol'],stock_catalog(store),now())
   except Exception:
    if m:
     m['announcement_status']='FAILED'
     with store.db:store.db.execute('UPDATE dynamic_market SET payload_json=? WHERE symbol=?',(json.dumps(m,ensure_ascii=False),c['symbol']))
  at=now()
  if (datetime.fromisoformat(at)-datetime.fromisoformat(scheduled)).total_seconds()>=config['slot_deadline_seconds']:return {'status':'MISSED','submitted':0,'fills':0}
 fills=settle(store,config,at);submitted=0
 for c in sorted(cases,key=lambda c:(0 if case_position(store,c['id'],at)['qty'] else 1,c['created_at'],c['id'])):
  q=latest_quote(store,c['symbol'],at);p=case_position(store,c['id'],at)
  action='HOLD';state='BLOCKED';reason='等待有效动态报价'
  if quote_ok(q,config,at):
   from .portfolio_risk import halted
   reason='ACCOUNT_DRAWDOWN_EXIT' if halted(store) and p['qty'] else exit_reason(c,p,q,at)
   if not reason:
    from .portfolio_strategy import exit_quantity
    if exit_quantity(store,config,'dynamic',c['id'],at,q):reason='PORTFOLIO_REDUCE'
   if reason:action='SELL'
   elif not p['qty'] and config.get('dynamic_enabled',False):action='BUY';reason='事件研究与历史验证满足条件'
   else:reason='继续按动态计划持有'
   if action!='HOLD':
    order=submit(store,config,c,action,q,at,reason)
    state=order.get('status','BLOCKED');reason=order.get('reason',reason);submitted+=int('id' in order and order['created_at']==at)
  with store.db:store.db.execute('INSERT OR REPLACE INTO dynamic_checks VALUES(?,?,?,?,?)',(c['id'],at,action,state,reason))
 return {'status':'SUCCEEDED','submitted':submitted,'fills':len(fills),'checked':len(cases)}
