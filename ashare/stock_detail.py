"""Read-only stock views: one current research version, complete paginated trade history."""
import json
import re
from .storage import Store

TITLES={'business':'经营与增长','cash':'现金流与资产负债','valuation':'估值与价格反映',
        'price':'价格趋势','events':'事件与行业风险','conditions':'交易触发与失效条件'}


def dimensions(item):
    report=item.get('report') or {};p=item.get('plan') or {};d=report.get('decision') or {}
    actual={r['id']:r for r in report.get('dimensions',[])}
    financial=p.get('company_dossier') or {};latest=financial.get('最新财务',{})
    def figures(pattern):return '；'.join(str(k)+'：'+str(v) for k,v in latest.items() if re.search(pattern,k))
    summaries={
        'business':figures('收入|利润') or '该版本尚无独立经营分析，请结合总论与关键证据阅读。',
        'cash':figures('现金|负债|应收|存货') or '该版本尚无独立现金流分析，不能据此确认回款质量。',
        'valuation':d.get('pricing') or '尚不能确认当前价格已反映多少经营预期。',
        'price':'；'.join(g['why'] for g in p.get('trade_guidance',{}).get('groups',[]) if g['key']=='TREND_NOT_CONFIRMED') or '当前价位与趋势条件见交易计划；尚无独立价格趋势文字分析。',
        'events':'；'.join(report.get('risks',[])) or '当前版本尚未给出独立事件风险判断。',
        'conditions':d.get('trigger') or '；'.join(report.get('next_checks',[])) or '等待有效研究和盘面条件。'}
    return [{**(actual.get(k) or {'id':k,'summary':summaries[k],'uncertainty':d.get('invalidation','') if k=='conditions' else '', 'evidence_ids':[]}),
        'title':title,'origin':'MODEL' if k in actual else 'EXISTING_REPORT','as_of':p.get('activated_at')} for k,title in TITLES.items()]


def detail(config,symbol,offset=0):
    from .dashboard import status
    current=status(config);item=next((x for x in current['watchlist'] if x['symbol']==symbol),None)
    store=Store(config['data_dir'])
    try:
        industry=current.get('industry') or {}
        member=next((m for m in industry.get('members',[]) if m['symbol']==symbol),None)
        if not item:
            item=historical_item(store,config,symbol,member,current['at'])
        if not item:raise ValueError('尚未建立这家公司的研究档案。')
        if member:item={**item,**member,'archived':member.get('research_status')=='ARCHIVED'}
        company_industry={**industry,'members':[member] if member else [],
            'hypotheses':[h for h in industry.get('hypotheses',[]) if h['symbol']==symbol],
            'history':[h for h in industry.get('history',[]) if h['symbol']==symbol],
            'forecasts':[f for f in industry.get('forecasts',[]) if f['symbol']==symbol],
            'checks':[],'coverage':[]}
        def page(table,order):
            condition=' WHERE symbol=?'+(' AND EXISTS(SELECT 1 FROM paper_orders o WHERE o.decision_id=decisions.id)' if table=='decisions' else '')
            total=store.db.execute('SELECT count(*) FROM '+table+condition,(symbol,)).fetchone()[0]
            rows=[dict(r) for r in store.db.execute('SELECT * FROM '+table+condition+' ORDER BY '+order+' DESC,rowid DESC LIMIT 30 OFFSET ?',(symbol,offset))]
            return {'items':rows,'total':total,'offset':offset,'limit':30}
        fills=page('paper_fills','occurred_at');orders=page('paper_orders','created_at');decisions=page('decisions','at')
        total=store.db.execute("SELECT count(*),coalesce(sum(fee_cents),0),coalesce(sum(realized_cents),0) FROM paper_fills WHERE symbol=?",(symbol,)).fetchone()
        from .calendar import local
        followups={**current['followups'],'items':[i for i in current['followups']['items'] if i['symbol'] in (symbol,'MARKET')],
            'today_failures':[f for f in current['followups']['today_failures'] if f['symbol'] in (symbol,'MARKET')],'history':[],'today_jobs':[]}
        followups.update(failed_today=len(followups['today_failures']),recovered_today=sum(f['recovered'] for f in followups['today_failures']),
            carried_over=sum(local(i['opened_at']).date()<local(current['at']).date() for i in followups['items']),
            owners={owner:sum(i['owner']==owner for i in followups['items']) for owner in followups['owners']})
        return {'at':current['at'],'stock':item,'dimensions':dimensions(item),'industry':company_industry,
            'position':next((h for h in current['portfolio']['holdings'] if h['symbol']==symbol),None),
            'trade_statistics':dict(zip(('fill_count','fee_cents','realized_cents'),total)),
            'history':{'fills':fills,'orders':orders,'decisions':decisions},'followups':followups}
    finally:store.close()


def historical_item(store,config,symbol,member,at):
    """Archive affects future research allocation, never the address of a dossier."""
    row=store.db.execute('''SELECT p.*,s.snapshot_id,s.model_status,s.result_json FROM plans p
        JOIN studies s ON s.id=p.study_id WHERE p.symbol=? ORDER BY p.activated_at DESC,p.rowid DESC LIMIT 1''',(symbol,)).fetchone()
    if not member and not row:return None
    item={**(member or {}),'symbol':symbol,'name':(member or {}).get('name',symbol),'archived':not member or member.get('research_status')=='ARCHIVED',
          'plan':None,'report':None,'quote':store.latest_quote(symbol,at),'open_orders':[],
          'failures':[],'last_decision':None,'latest_study':None,'last_research_at':None}
    if row:
        from .presentation import trader_report
        p=dict(row);p['payload']=json.loads(p.pop('payload_json'));p['research']=json.loads(p.pop('result_json'))
        p['effective_status']='EXPIRED' if p['valid_until']<=at else p['status']
        snapshot=store.db.execute('SELECT packet_json FROM snapshots WHERE id=?',(p['snapshot_id'],)).fetchone()
        packet=json.loads(snapshot[0]) if snapshot else {}
        from .fundamentals import view as dossier
        p['company_dossier']=dossier(packet.get('company_dossier',{}),item['quote'])
        p['learning']={k:v for k,v in packet.get('learning',{}).items() if k not in ('new_chunk_ids','revised_documents')}
        p['external_events']=packet.get('external_events',[]);p['background_events']=packet.get('background_events',[])
        p['market_context']=((packet.get('stocks') or [{}])[0].get('features') or {}).get('market_context',{})
        p['coverage']=[]
        item.update(plan=p,report=trader_report(p,symbol),last_research_at=p['activated_at'])
    return item
