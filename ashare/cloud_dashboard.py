"""Cloud UI combines signed research summaries with authoritative live accounting."""
import copy,json
from .storage import now
from .cloud_runtime import value,lease
from .calendar import phase,CALENDAR_VERSION


def add_strategy_times(store,result):
    times={}
    for row in store.db.execute('SELECT created_at,payload_json FROM portfolio_decisions ORDER BY created_at DESC,rowid DESC'):
        for d in json.loads(row['payload_json']).get('decisions',[]):times.setdefault(d['symbol'],row['created_at'])
    for w in result.get('watchlist',[]):w['last_strategy_updated_at']=times.get(w['symbol'])
    for w in result.get('observation',{}).get('items',[]):w['last_strategy_updated_at']=times.get(w['asset'])
    return result


def status(store,config):
    from . import __version__
    from .paper import account
    from .portfolio_risk import state
    from .investment_policy import public
    from .reporting import portfolio,trade_effects,next_runs
    from .portfolio_strategy import view
    from .global_market import latest
    from .global_paper import balance
    from .global_research import active_plan
    at=now();a=account(store,at);a['risk']=state(store);a['investment_policy']=public()
    cache=copy.deepcopy(value(store,'display',{}))
    if not cache:
        # No public account setup is available before the signed migration completes.
        raise ValueError('等待本地研究端完成首次策略同步')
    for w in cache['watchlist']:
        w['quote']=store.latest_quote(w['symbol'],at)
        w['open_orders']=[{k:o[k] for k in ('id','side','qty','filled_qty','limit_cents','status','created_at')} for o in a['orders'] if o['symbol']==w['symbol'] and not o.get('origin')]
        row=store.latest_trade_check(w['symbol'],at)
        w['last_decision']=dict(row) if row else None
        if row:w['last_decision']['buy_blockers']=json.loads(row['payload_json']).get('input_buy_blockers',[])
        p=w.get('plan')
        if p:p['effective_status']='EXPIRED' if p['valid_until']<=at else p['status']
    held=balance(store,at)['positions']
    for item in cache['observation']['items']:
        item['spot_quote']=latest(store,item['asset'],at);item['position']=held.get(item['asset'])
        if item['category']=='CN':
            from .dynamic_sources import latest_quote
            item['quote']=latest_quote(store,item['asset'],at)
        p=active_plan(store,item['asset'],at)
        item['trade_plan']={**p,'payload':json.loads(p['payload_json'])} if p else None
    cache['reviews']=value(store,'display_reviews',cache['reviews'])
    result={**cache,'version':__version__,'at':at,'mode':'paper','live_execution':False,'model_auth':'LOCAL_RESEARCH','deployment_role':'cloud',
      'research_lease':lease(store,config,at),'scheduler_enabled':config['scheduler_enabled'],'calendar':CALENDAR_VERSION,'market_phase':phase(at),
      'account':a,'portfolio':portfolio(store,config,a,at),'trade_effects':trade_effects(store,config,at),'portfolio_strategy':view(store,config,at),
      'next_runs':next_runs(config,at),'active_jobs':[dict(r) for r in store.db.execute("SELECT * FROM jobs WHERE status IN ('PENDING','RUNNING') ORDER BY scheduled_at")],
      'jobs':[dict(r) for r in store.db.execute('SELECT * FROM jobs ORDER BY scheduled_at DESC LIMIT 20')],
      'decisions':[dict(r) for r in store.db.execute('SELECT * FROM decisions WHERE EXISTS(SELECT 1 FROM paper_orders o WHERE o.decision_id=decisions.id) ORDER BY at DESC LIMIT 30')],
      'fills':[dict(r) for r in store.db.execute('SELECT * FROM paper_fills ORDER BY recorded_at DESC LIMIT 20')],
      'state':dict(store.db.execute('SELECT key,value FROM service_state')),'source_checks':[],'background_failures':[],'documents':0,'snapshots':0}
    return add_strategy_times(store,result)
