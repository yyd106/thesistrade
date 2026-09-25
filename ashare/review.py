"""Fixed 24-hour reviews. Append-only revisions, no strategy self-modification.

Review findings are routed, not injected: program and data defects become engineering issues,
strategy observations become DRAFT change proposals. Neither reaches a research prompt."""
from __future__ import annotations
import json
import uuid
from datetime import datetime,timedelta
from .calendar import review_window
from .model import run_json
from .research import encode
from .storage import now,normalize_time,digest,json_write
from .governance import REVIEW_ISSUE_KEYS

DEFECT_CATEGORIES=('DATA','EXECUTION','SYSTEM')
REVIEW_SCHEMA={'type':'object','additionalProperties':False,'properties':{
 'summary':{'type':'string'},'lessons':{'type':'array','items':{'type':'object','additionalProperties':False,
 'properties':{'symbol':{'type':'string'},'category':{'type':'string','enum':['DATA','RESEARCH','EXECUTION','SYSTEM','OBSERVATION']},
 'issue_key':{'type':'string','enum':REVIEW_ISSUE_KEYS+['STRATEGY_OBSERVATION']},
 'lesson':{'type':'string'},'decision_ids':{'type':'array','items':{'type':'string'}},
 'fill_ids':{'type':'array','items':{'type':'string'}},'applicability':{'type':'string'}},
    'required':['symbol','category','issue_key','lesson','decision_ids','fill_ids','applicability']}}},'required':['summary','lessons']}


def route_lessons(store,rid,lessons,at):
    """Defects collapse into one engineering issue per key and symbol; observations become drafts."""
    from .governance import record_issue,draft_proposal
    routed=[]
    for n,lesson in enumerate(lessons):
        evidence=['review:'+rid]+['decision:'+i for i in lesson.get('decision_ids',[])]+['fill:'+i for i in lesson.get('fill_ids',[])]
        if lesson['category'] in DEFECT_CATEGORIES:
            key=lesson.get('issue_key')
            if key not in REVIEW_ISSUE_KEYS:key='OTHER_'+lesson['category']
            iid=record_issue(store,key,lesson['symbol'],lesson['lesson'],evidence,at)
            routed.append({'lesson':n,'to':'engineering_issue','id':iid,'issue_key':key})
        else:
            pid=draft_proposal(store,source='review',kind='OTHER',target=lesson['symbol'],title=lesson['lesson'][:60],
                payload={'observations':[{'review_id':rid,'lesson':lesson['lesson'],'applicability':lesson.get('applicability'),
                    'category':lesson['category'],'evidence':evidence}],
                    'note':'复盘产生的策略观察草稿：尚未验证，不进入研究输入。需整理成含检验方法的完整提案并经用户批准。'},
                at=at,dedupe_key='review:'+rid+':'+str(n))
            routed.append({'lesson':n,'to':'proposal_draft','id':pid})
    return routed


def model_facts(facts,budget=110000):
    """Preserve full audit facts on disk; bound minute-level input with explicit coverage."""
    if len(encode(facts))<=budget:return facts
    decisions=facts['decisions']+facts['related_decisions_outside_window']
    by_id={d['id']:d for d in decisions};groups={};by_stock={}
    for d in facts['decisions']:
        group=groups.setdefault((d['symbol'],d['action'],d['status'],d['reason']),[]);group.append(d['id'])
        row=by_stock.setdefault(d['symbol'],{'symbol':d['symbol'],'decision_count':0,'actions':{},'statuses':{}})
        row['decision_count']+=1
        for key,value in (('actions',d['action']),('statuses',d['status'])):row[key][value]=row[key].get(value,0)+1
    slot_rows={s['id']:s for s in facts['slot_inputs']};parsed={}
    orders={o['id']:o for o in facts['orders_known_at_review']}
    filled_decisions=[orders[f['order_id']]['decision_id'] for f in facts['fills'] if f['order_id'] in orders]
    priority=list(dict.fromkeys(filled_decisions+[d['id'] for d in decisions if d['action']!='HOLD']+
                               [v[-1] for v in groups.values()]+[v[0] for v in groups.values()]))
    for limit in (60,30,15,6,1):
        # Include both recent and early examples instead of only the first active stock.
        ids=list(dict.fromkeys(priority[-(limit//2):]+priority[:max(1,limit//2)]))[:limit] if limit>1 else priority[:1]
        selected=[by_id[i] for i in ids if i in by_id];inputs=[]
        for d in selected:
            if d['slot_id'] not in parsed:
                parsed[d['slot_id']]=json.loads(slot_rows[d['slot_id']]['input_json']) if d['slot_id'] in slot_rows else {}
            p=parsed[d['slot_id']];entry=next((s for s in p.get('stocks',[]) if s['symbol']==d['symbol']),None)
            stock=None
            if entry:
                plan=entry.get('plan') or {};content=entry.get('plan_content') or {};q=entry.get('quote') or {}
                stock={'symbol':d['symbol'],'plan_id':plan.get('id'),'valid_until':plan.get('valid_until'),
                    'levels':content.get('levels'),'thesis':content.get('thesis','')[:400],'position':entry.get('position'),
                    'buy_blockers':entry.get('buy_blockers'),'risk_trigger':entry.get('risk_trigger'),
                    'quote':{k:q.get(k) for k in ('price_cents','observed_at','first_seen_at')}}
            inputs.append({'decision_id':d['id'],'slot_id':d['slot_id'],'input_as_of':p.get('input_as_of'),
                           'account':{k:p.get('account',{}).get(k) for k in ('available_cents','equity_cents','reserved_cents')},'stock':stock})
        fills=(facts['fills'][:limit//2]+facts['fills'][-limit//2:]) if len(facts['fills'])>limit else facts['fills']
        fills=[{k:v for k,v in f.items() if k!='payload_json'} for f in fills]
        order_ids={f['order_id'] for f in fills}|{o['id'] for o in orders.values() if o['decision_id'] in ids}
        packet={'window_start':facts['window_start'],'window_end':facts['window_end'],'statistics':facts['statistics'],
            'stock_statistics':list(by_stock.values()),'claim_type':'EXECUTION_FACTS',
            'decisions':[{k:v for k,v in d.items() if k!='payload_json'} for d in selected],
            'related_decisions_outside_window':[],'fills':fills,'orders_known_at_review':[orders[i] for i in order_ids if i in orders],
            'slot_inputs':inputs,'equity_marks':[{k:v for k,v in m.items() if k!='payload_json'} if m else None for m in facts['equity_marks']],
            'coverage':{'sampled':True,'all_decisions':len(decisions),'included_decisions':len(selected),
                        'all_fills':len(facts['fills']),'included_fills':len(fills),
                        'notice':'统计使用全量记录；明细和对应当时输入为限量样本，完整记录已保存。只评价给出的样本，不从样本推断全体频率或把缺失输入视为通过。'}}
        if len(encode(packet))<=budget:return packet
    raise ValueError('复盘汇总超过输入预算，完整事实已保存')


def run_review(store,config,end=None,use_model=True,model_fn=None,clock=now):
    ready=normalize_time(clock())
    if end:
        finish=datetime.fromisoformat(normalize_time(end));start=finish-timedelta(hours=24)
    else:start,finish=review_window(ready,config['review_time'])
    a,b=normalize_time(start.isoformat()),normalize_time(finish.isoformat())
    if b>ready:raise ValueError('不能复盘尚未结束的窗口')
    decisions=[dict(r) for r in store.db.execute('SELECT * FROM decisions WHERE at>=? AND at<? AND EXISTS(SELECT 1 FROM paper_orders o WHERE o.decision_id=decisions.id) ORDER BY at,id',(a,b))]
    fills=[dict(r) for r in store.db.execute('SELECT * FROM paper_fills WHERE occurred_at>=? AND occurred_at<? AND recorded_at<=? ORDER BY occurred_at,id',(a,b,ready))]
    order_ids={f['order_id'] for f in fills}
    orders=[dict(r) for r in store.db.execute('SELECT * FROM paper_orders WHERE created_at>=? AND created_at<? ORDER BY created_at,id',(a,b))]
    for oid in order_ids-{o['id'] for o in orders}:
        orders.append(dict(store.db.execute('SELECT * FROM paper_orders WHERE id=?',(oid,)).fetchone()))
    # Include the originating decision even when the order was created before the review window.
    related=[];ids={d['id'] for d in decisions}
    for o in orders:
        if o['decision_id'] not in ids:
            related.append(dict(store.db.execute('SELECT * FROM decisions WHERE id=?',(o['decision_id'],)).fetchone()));ids.add(o['decision_id'])
    marks=[]
    for boundary in (a,b):
        row=store.db.execute('SELECT * FROM equity_marks WHERE at<=? AND recorded_at<=? ORDER BY at DESC,rowid DESC LIMIT 1',(boundary,ready)).fetchone()
        mark=dict(row) if row else None
        if mark:
            allocation=json.loads(mark['payload_json'])
            if 'watchlist_equity_cents' in allocation:
                mark['watchlist_equity_cents']=allocation['watchlist_equity_cents']
        marks.append(mark)
    stats={'recording_policy':'OPERATIONS_ONLY','decision_count':len(decisions),'actions':{k:sum(d['action']==k for d in decisions) for k in ('BUY','SELL','HOLD')},
        'recording_notice':'仅记录已提交委托和成交；无操作检查不逐次保存。没有买卖记录不代表没有检查，不能据此评价错过的机会或未操作的频率。',
        'blocked_count':sum(d['status'] in ('BLOCKED','EXPIRED') for d in decisions),
        'fill_count':len(fills),'fee_cents':sum(f['fee_cents'] for f in fills),
        'realized_pnl_cents':sum(f['realized_cents'] for f in fills),'win_rate':None,
        'cashflow_adjusted_change_between_marks_cents':None,'mark_times':[m['at'] if m else None for m in marks],
        'notice':'未平仓不计算最终胜率；已实现损益含对应成本和费用；提取为资金流。净值差只覆盖实际估值时点。'}
    if all(m and m['complete'] for m in marks):
        stats['cashflow_adjusted_change_between_marks_cents']=marks[1].get('watchlist_equity_cents',marks[1]['equity_cents'])-marks[0].get('watchlist_equity_cents',marks[0]['equity_cents'])+marks[1]['withdrawn_cents']-marks[0]['withdrawn_cents']
    if any(m and 'watchlist_equity_cents' in m for m in marks):
        stats['notice']+=' 观察栏净值差剔除动态线路损益；原始现金与总资产仍为共用账户快照。'
    # Saved Slot inputs preserve prices/plans then known. Never join today's active plan as historical input.
    slot_ids={d['slot_id'] for d in decisions+related}
    inputs=[dict(store.db.execute('SELECT id,scheduled_at,input_json FROM slots WHERE id=?',(sid,)).fetchone()) for sid in sorted(slot_ids)]
    facts={'window_start':a,'window_end':b,'decisions':decisions,'related_decisions_outside_window':related,
        'orders_known_at_review':orders,'fills':fills,'statistics':stats,'slot_inputs':inputs,
        'equity_marks':marks,'claim_type':'EXECUTION_FACTS'}
    from .review_portfolio import build
    facts['portfolio']=build(store,config,a,b,ready)
    from .investment_policy import enabled
    if enabled(config):
        context_start=normalize_time((finish-timedelta(hours=48)).isoformat())
        facts['context_48h']=build(store,config,context_start,b,ready)
        facts['daily_portfolio']=facts['portfolio']
        facts['portfolio']=facts['context_48h']
        facts['context_48h']['learning_notice']='滚动48小时上下文会重叠；每日收益仍用24小时窗口，不当作独立重复样本。'
    fp=digest(encode(facts))
    prior=store.db.execute('SELECT * FROM reviews WHERE window_start=? AND window_end=? AND (fingerprint=? OR fingerprint LIKE ?) ORDER BY revision DESC LIMIT 1',(a,b,fp,fp+':%')).fetchone()
    if prior and (prior['model_status'] in ('SUCCEEDED','NOT_NEEDED') or not use_model or not config['model_enabled']):
        return {'review_id':prior['id'],'status':'ALREADY_DONE','revision':prior['revision']}
    revision=store.db.execute('SELECT coalesce(max(revision),0)+1 FROM reviews WHERE window_start=? AND window_end=?',(a,b)).fetchone()[0]
    rid=uuid.uuid4().hex;folder=store.root/'workflow'/'reviews'/rid
    if prior:fp += ':'+rid  # Same facts, new analysis attempt; keep the failed revision intact.
    json_write(folder/'facts.json',facts)
    # Deterministic checks sit outside the fingerprint: sync and disk health vary between attempts.
    from . import review_checks
    checks=review_checks.run(store,config,a,b,ready,facts['portfolio'])
    model_status='NOT_RUN'
    result={'summary':f"窗口内提交{len(decisions)}笔模拟委托、发生{len(fills)}笔模拟成交。无操作检查不逐次保存。",'lessons':[]}
    failure=None
    try:
        if use_model and not facts['portfolio']['positions'] and not decisions and not fills:
            model_status='NOT_NEEDED';result={'summary':'本期无持仓、无成交，没有持仓研究可验证。账户事实复盘已完成。','positions':[],'lessons':[]}
            store.record_attempt('review_analysis','MARKET','OK',run_id=rid)
        elif use_model and config['model_enabled']:
            from .review_analysis import facts_for_model,schema,validate,PROMPT
            bounded=facts_for_model(facts)
            bounded['program_checks']=[{k:c[k] for k in ('check','status','failures','detail')} for c in checks]
            json_write(folder/'model-facts.json',bounded)
            prompt=PROMPT+'\n<UNTRUSTED_REVIEW>'+encode(bounded)+'</UNTRUSTED_REVIEW>'
            if len(prompt)>65000:raise ValueError('全仓复盘资料超过单轮预算，事实已保存')
            raw=(model_fn or run_json)(prompt,schema(REVIEW_SCHEMA,bounded),folder/'model',config['model_timeout_seconds'])
            json_write(folder/'model-output.json',raw)
            result=validate(raw,bounded);model_status='SUCCEEDED'
            store.record_attempt('review_analysis','MARKET','OK',run_id=rid)
    except Exception as exc:
        from .review_analysis import failure_message
        failure=failure_message(exc,folder)
        model_status='DEFERRED';result={'summary':'全仓盈亏已核算，研究判断分析等待补完。','positions':[],'lessons':[]}
        store.record_attempt('review_analysis','MARKET','FAILED',failure,run_id=rid)
    ready=normalize_time(clock());expires=normalize_time((datetime.fromisoformat(ready)+timedelta(days=30)).isoformat())
    from .research import model_record,record_build
    payload={'facts':facts,'analysis':result,'revision':revision,'internal_only':True,'analysis_error':failure,'analysis_scope':'ALL_POSITIONS',
             'consistency_checks':checks,'model':model_record(folder/'model'),'build':record_build(store,config,ready)}
    with store.db:
        payload['routing']=route_lessons(store,rid,result['lessons'],ready)
        store.db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)',(rid,a,b,revision,ready,fp,model_status,encode(payload)))
        for n,lesson in enumerate(result['lessons']):
            # Kept for display only; research never reads this table.
            store.db.execute('INSERT INTO lessons VALUES(?,?,?,?,?,?)',(rid+':'+str(n),rid,lesson['symbol'],ready,expires,
                encode({**lesson,'validation_status':'ROUTED','claim_type':'REVIEW_FINDING','routed_to':payload['routing'][n]})))
    json_write(folder/'review.json',payload)
    return {'review_id':rid,'status':model_status,'revision':revision,'window_start':a,'window_end':b,'statistics':stats,'model_status':model_status,'analysis_error':failure,'portfolio_totals':facts['portfolio']['totals']}
