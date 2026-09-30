"""Historical whole-account review facts, reconstructed from immutable fills."""
from __future__ import annotations
import json
from datetime import datetime
from .calendar import local, last_completed_day

VERSION='portfolio-review-3'


def executions(store, end, known_at):
    result=[]
    for origin, table, orders, reference in (
        ('watchlist','paper_fills','paper_orders','plan_id'),
        ('dynamic','dynamic_fills','dynamic_orders','case_id')):
        result += [{**dict(r),'origin':origin} for r in store.db.execute(
            f'''SELECT f.*,o.{reference} research_id,o.created_at order_created_at
            FROM {table} f JOIN {orders} o ON o.id=f.order_id
            WHERE f.occurred_at<? AND f.recorded_at<=? ORDER BY f.occurred_at,f.recorded_at,f.rowid''',(end,known_at))]
    for r in store.db.execute('''SELECT f.*,o.plan_id research_id,o.created_at order_created_at FROM global_fills f
        JOIN global_orders o ON o.id=f.order_id WHERE f.occurred_at<? AND f.recorded_at<=?''',(end,known_at)):
        from .global_market import notional,SCALE
        result.append({**dict(r),'origin':'global','price_cents':notional(SCALE,r['price_micros'],r['fx_micros']),'qty_scale':SCALE})
    for fill in result:
        route=fill['origin']
        query={'watchlist':'SELECT config_json FROM paper_order_terms WHERE order_id=?',
               'dynamic':'SELECT terms_json FROM dynamic_orders WHERE id=?',
               'global':'SELECT payload_json FROM global_orders WHERE id=?'}[route]
        terms=store.db.execute(query,(fill['order_id'],)).fetchone()
        context=json.loads(terms[0]).get('portfolio_decision') if terms else None
        if context:fill['portfolio_decision']=context
    return sorted(result,key=lambda r:(r['occurred_at'],r['recorded_at'],r['id']))


def ledger_at(fills, boundary):
    states={}
    for f in fills:
        if f['occurred_at']>=boundary:continue
        key=f['origin']+':'+f['symbol']
        p=states.setdefault(key,{'symbol':f['symbol'],'origin':f['origin'],'qty':0,'cost_cents':0,'realized_cents':0,'lots':[]})
        gross=f.get('gross_cents',f['qty']*f['price_cents'])
        if f['side']=='BUY':
            p['qty']+=f['qty'];p['cost_cents']+=gross+f['fee_cents']
            p['lots'].append({'id':f['id'],'qty':f['qty'],'research_id':f['research_id'],'day':local(f['occurred_at']).date().isoformat()})
        else:
            p['qty']-=f['qty'];p['cost_cents']-=gross-f['fee_cents']-f['realized_cents']
            p['realized_cents']+=f['realized_cents'];remaining=f['qty']
            for lot in sorted(p['lots'],key=lambda l:(l['day'],l['id'])):
                if f['origin']=='dynamic' and lot['research_id']!=f['research_id']:continue
                take=min(lot['qty'],remaining);lot['qty']-=take;remaining-=take
                if not remaining:break
            if remaining:raise ValueError('复盘成交历史不完整：卖出数量超过已知买入')
        if p['qty']<0 or p['cost_cents']<0 or (not p['qty'] and p['cost_cents']):
            raise ValueError('复盘成交历史份额或剩余成本不一致')
    return states


def valuation(store,p,boundary,known_at):
    if not p or not p['qty']:
        return {'qty':0,'cost_cents':0,'market_value_cents':0,'unrealized_cents':0,'average_cost_cents':None,'price_cents':None,'quote_at':None,'quality':'NO_POSITION','late_quote':False}
    if p['origin']=='global':
        from .global_market import notional,SCALE,fresh,session_open
        q=store.db.execute('SELECT * FROM global_quotes WHERE symbol=? AND observed_at<? AND fx_at<? AND first_seen_at<=? ORDER BY observed_at DESC,first_seen_at DESC LIMIT 1',(p['symbol'],boundary,boundary,known_at)).fetchone()
        value=notional(p['qty'],q['price_micros'],q['fx_micros']) if q else None
        # Historical revision can use a late receipt, but discloses it explicitly.
        quality='MISSING'
        if q:
            age=(datetime.fromisoformat(boundary)-datetime.fromisoformat(q['observed_at'])).total_seconds()
            fx_age=(datetime.fromisoformat(boundary)-datetime.fromisoformat(q['fx_at'])).total_seconds()
            quality='RECENT' if 0<=age<=300 and 0<=fx_age<=3600 else 'STALE'
        return {'qty':p['qty'],'qty_scale':SCALE,'cost_cents':p['cost_cents'],'average_cost_cents':p['cost_cents']*SCALE/p['qty'],
            'market_value_cents':value,'unrealized_cents':value-p['cost_cents'] if value is not None else None,
            'price_cents':notional(SCALE,q['price_micros'],q['fx_micros']) if q else None,'price_micros':q['price_micros'] if q else None,
            'currency':'USD','fx_micros':q['fx_micros'] if q else None,'fx_at':q['fx_at'] if q else None,
            'quote_at':q['observed_at'] if q else None,'quality':quality,'late_quote':bool(q and q['first_seen_at']>boundary)}
    # Observation time defines the cutoff; receipt time defines the revision's
    # knowledge. Late historical quotes are disclosed, never treated as known then.
    quotes=[]
    for table in (('quotes','dynamic_quotes') if p['origin']=='dynamic' else ('quotes',)):
        q=store.db.execute(f'''SELECT * FROM {table} WHERE symbol=? AND observed_at<? AND first_seen_at<=?
            ORDER BY observed_at DESC,first_seen_at DESC LIMIT 1''',(p['symbol'],boundary,known_at)).fetchone()
        if q:quotes.append(dict(q))
    q=max(quotes,key=lambda q:(q['observed_at'],q['first_seen_at'])) if quotes else None
    expected=last_completed_day(boundary)
    quality='MISSING'
    if q:
        stamp=local(q['observed_at']);day=stamp.date().isoformat()
        quality='CLOSE' if day==expected and stamp.strftime('%H:%M')>='15:00' else 'INTRADAY_LAST' if day==expected else 'STALE'
    value=p['qty']*q['price_cents'] if q else None
    return {'qty':p['qty'],'cost_cents':p['cost_cents'],'average_cost_cents':p['cost_cents']/p['qty'],
        'market_value_cents':value,'unrealized_cents':value-p['cost_cents'] if value is not None else None,
        'price_cents':q['price_cents'] if q else None,'quote_at':q['observed_at'] if q else None,
        'quote_first_seen_at':q['first_seen_at'] if q else None,'quality':quality,'late_quote':bool(q and q['first_seen_at']>boundary)}


def research_record(store,origin,rid,cutoff,known_at=None):
    if origin=='global':
        r=store.db.execute('SELECT * FROM global_plans WHERE id=? AND created_at<?',(rid,cutoff)).fetchone()
        if not r:return None
        payload=json.loads(r['payload_json'])
        return {'id':rid,'symbol':r['symbol'],'origin':origin,'at':r['created_at'],'role':'ENTRY','analysis':payload.get('analysis',{}),'plan':payload}
    if origin=='dynamic':
        r=store.db.execute('''SELECT o.* FROM dynamic_orders o JOIN dynamic_fills f ON f.order_id=o.id
            WHERE o.case_id=? AND o.side='BUY' AND o.created_at<? AND f.occurred_at<? AND f.recorded_at<=?
            ORDER BY f.occurred_at,f.recorded_at,f.id LIMIT 1''',(rid,cutoff,cutoff,known_at or cutoff)).fetchone()
        if not r:return None
        terms=json.loads(r['terms_json'])
        if 'analysis' not in terms or 'plan' not in terms:return None
        return {'id':rid,'symbol':r['symbol'],'origin':origin,'at':r['created_at'],'role':'ENTRY',
            'plan':terms['plan'], 'analysis':terms['analysis'], 'basis':'ORDER_SNAPSHOT', 'order_id':r['id']}
    r=store.db.execute('''SELECT p.id,p.symbol,p.activated_at,p.payload_json,s.result_json,s.created_at FROM plans p
        JOIN studies s ON s.id=p.study_id WHERE p.id=? AND p.activated_at<? AND s.created_at<?''',(rid,cutoff,cutoff)).fetchone()
    if not r:return None
    result=json.loads(r['result_json']);stock=next((i for i in result.get('stocks',[]) if i['symbol']==r['symbol']),{})
    return {'id':rid,'symbol':r['symbol'],'origin':origin,'at':r['activated_at'],'role':'ENTRY',
        'analysis':stock,'plan':json.loads(r['payload_json'])}


def build(store,config,start,end,known_at):
    fills=executions(store,end,known_at);opening=ledger_at(fills,start);closing=ledger_at(fills,end)
    window=[f for f in fills if start<=f['occurred_at']<end]
    keys={k for k,p in opening.items() if p['qty']}|{k for k,p in closing.items() if p['qty']}|{f['origin']+':'+f['symbol'] for f in window}
    names={r['symbol']:r['name'] for r in store.db.execute('SELECT symbol,name FROM dynamic_cases WHERE created_at<?',(end,))}
    from .investment_policy import FIXED
    names.update({k:v['name'] for k,v in FIXED.items()})
    names.update({r['symbol']:r['name'] for r in config['watchlist']});positions=[];research={}
    for key in sorted(keys):
        p=closing.get(key) or opening[key];symbol=p['symbol'];origin=p['origin']
        first=valuation(store,opening.get(key),start,known_at);last=valuation(store,closing.get(key),end,known_at)
        relevant=[f for f in window if (f['origin'],f['symbol'])==(origin,symbol)]
        realized=sum(f['realized_cents'] for f in relevant);unrealized=last['unrealized_cents']
        change=realized+unrealized-first['unrealized_cents'] if unrealized is not None and first['unrealized_cents'] is not None else None
        related_ids={l['research_id'] for state in (opening.get(key,{}),closing.get(key,{})) for l in state.get('lots',[]) if l['qty']}
        related_ids.update(f['research_id'] for f in relevant if f['research_id'])
        records=[]
        for rid in sorted(related_ids):
            record=research_record(store,origin,rid,end,known_at)
            if record:records.append(record);research[rid]=record
        if origin=='watchlist':
            latest=store.db.execute('''SELECT p.id FROM plans p JOIN studies s ON s.id=p.study_id
                WHERE p.symbol=? AND p.activated_at<? AND s.created_at<? AND s.model_status='SUCCEEDED'
                ORDER BY p.activated_at DESC,p.rowid DESC LIMIT 1''',(symbol,end,end)).fetchone()
            if latest and latest['id'] not in related_ids:
                record=research_record(store,origin,latest['id'],end)
                if record:record['role']='FOLLOW_UP';records.append(record);research[record['id']]=record
        source_fills=[f for f in fills if (f['origin'],f['symbol'])==(origin,symbol) and (f in relevant or f['id'] in {l['id'] for state in (opening.get(key,{}),closing.get(key,{})) for l in state.get('lots',[]) if l['qty']})]
        positions.append({'key':key,'symbol':symbol,'origin':origin,'name':names.get(symbol,symbol),
            'opening':first,'closing':last,'period_realized_cents':realized,'period_profit_cents':change,
            'cumulative_realized_cents':p['realized_cents'],'cumulative_profit_cents':p['realized_cents']+unrealized if unrealized is not None else None,
            'period_fee_cents':sum(f['fee_cents'] for f in relevant),'period_fill_count':len(relevant),
            'entry_research_ids':[r['id'] for r in records if r['role']=='ENTRY'],'research_ids':[r['id'] for r in records],
            'research_gap':len([rid for rid in related_ids if rid not in research]),'fill_ids':[f['id'] for f in source_fills]})
    def total(values):return sum(values) if all(v is not None for v in values) else None
    def balance(boundary,states):
        valuations=[valuation(store,p,boundary,known_at) for p in states.values() if p['qty']]
        flows=list(store.db.execute("SELECT * FROM paper_flows WHERE account_id='DEMO_PAPER' AND created_at<?",(boundary,)))
        cash=sum(f['amount_cents'] for f in flows)+sum((1 if f['side']=='SELL' else -1)*f.get('gross_cents',f['qty']*f['price_cents'])-f['fee_cents'] for f in fills if f['occurred_at']<boundary)
        mv=total([p['market_value_cents'] for p in valuations])
        initial=next((f for f in flows if f['kind']=='SIMULATED_INITIAL'),None)
        return {'cash_cents':cash if initial else None,'equity_cents':cash+mv if initial and mv is not None else None,
            'market_value_cents':mv,'cost_cents':sum(p['cost_cents'] for p in valuations),'unrealized_cents':total([p['unrealized_cents'] for p in valuations]),
            'cumulative_realized_cents':sum(p['realized_cents'] for p in states.values()),
            'valuation_complete':all(p['quality'] in ('CLOSE','RECENT','NO_POSITION') for p in valuations)}
    first,last=balance(start,opening),balance(end,closing)
    cumulative=last['cumulative_realized_cents']+last['unrealized_cents'] if last['unrealized_cents'] is not None else None
    from .dividends import parse,KIND
    dividends=[]
    for f in store.db.execute("SELECT reference,amount_cents,created_at FROM paper_flows WHERE kind=? AND created_at>=? AND created_at<?",(KIND,start,end)):
        d=parse(f['reference'])
        if d:dividends.append({'symbol':d['symbol'],'ex_date':d['ex_date'],'record_date':d['record_date'],'qty':d['qty'],
                               'cash_per_share':format(d['cash'],'f'),'amount_cents':f['amount_cents'],'credited_at':f['created_at']})
    return {'version':VERSION,'scope':'ALL_POSITIONS','window_start':start,'window_end':end,
        'positions':sorted(positions,key=lambda p:-(p['closing']['market_value_cents'] or 0)),'research':list(research.values()),
        'fills':[{**({'portfolio_decision':f['portfolio_decision']} if f.get('portfolio_decision') else {}),**{k:f.get(k) for k in ('qty_scale','gross_cents','price_micros','fx_micros')},**{k:f[k] for k in ('id','order_id','research_id','origin','symbol','side','qty','price_cents','fee_cents','realized_cents','occurred_at','recorded_at')}} for f in fills if f['id'] in {i for p in positions for i in p['fill_ids']}],
        'opening':first,'closing':last,'totals':{'holding_count':sum(p['closing']['qty']>0 for p in positions),'reviewed_position_count':len(positions),
            'period_fill_count':len(window),'period_fee_cents':sum(f['fee_cents'] for f in window),'period_realized_cents':sum(f['realized_cents'] for f in window),
            'period_profit_cents':total([p['period_profit_cents'] for p in positions]),'cumulative_profit_cents':cumulative},
        'dividends':dividends,
        'valuation_notice':'按截止前最后报价估值；非收盘、缺失和事后补齐的历史报价逐项标注。期内损益=期内已实现+期末浮动−期初浮动，已计费用；短期盈亏不等于研究因果已验证。'
            '现金分红记入现金（见dividends），不在持仓损益内；除息日股价按分红下调不是亏损。'}


def model_view(portfolio,budget=40000):
    """Every position survives compaction; no untraded holding can be sampled out."""
    for length in (900,450,180):
        research=[]
        for r in portfolio['research']:
            a=r['analysis'];plan=r['plan']
            research.append({k:r[k] for k in ('id','symbol','origin','at','role')}|{
                'analysis':str(a.get('analysis',a.get('mechanism','')))[:length],
                'decision':a.get('decision',{}),'counterpoints':a.get('counterpoints',[])[:3],
                'next_checks':a.get('next_checks',[])[:3],'thesis':str(plan.get('thesis',''))[:length],
                'levels':plan.get('levels'),'event_analysis':{k:a[k] for k in ('direction','rationale','invalidation','conditions') if k in a}})
        view={k:portfolio[k] for k in ('version','scope','window_start','window_end','positions','opening','closing','totals','valuation_notice')}
        if portfolio.get('dividends'):view['dividends']=portfolio['dividends']
        view['research']=research;view['fills']=portfolio['fills']
        if len(json.dumps(view,ensure_ascii=False))<=budget:return view
    raise ValueError('全仓复盘资料超过预算，事实已保存；需减少单条研究长度或分批处理')
