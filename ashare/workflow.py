from __future__ import annotations
import fcntl
import time
from contextlib import contextmanager
from .storage import Store,now
from .finance import PaperLedger
from .research import collect_batch,make_snapshot,study
from .slots import run_slot,refresh_market
from .paper import settle,mark_equity
from .review import run_review
import re


@contextmanager
def task_lock(root,name,wait_seconds=0):
    path=root/'locks';path.mkdir(exist_ok=True)
    with (path/(name+'.lock')).open('a+') as handle:
        deadline=time.monotonic()+wait_seconds
        while True:
            try:
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise RuntimeError('BUSY: '+name)
                time.sleep(max(0,min(.1,deadline-time.monotonic())))
        try:yield
        finally:fcntl.flock(handle,fcntl.LOCK_UN)


def execute(config,command,*,use_model=True,key=None,batch_id=None,symbol=None,scheduled_at=None,end=None):
    if config.get('live_execution_enabled') or config.get('paid_api_fallback'):
        raise ValueError('没有实盘或付费API执行路径')
    from .cloud_runtime import assert_command
    from .cloud_protocol import role
    assert_command(config,command)
    store=Store(config['data_dir'])
    try:
        if role(config)=='research' and command in ('portfolio_strategy','review'):
            from .cloud_sync import pull
            with task_lock(store.root,'cloud-sync',wait_seconds=50):pull(store,config)
        PaperLedger(store).initialize()
        from .investment_policy import enabled,seed
        if enabled(config):seed(store,now())
        def research_once(sym,selected_batch=None):
            try:
                packet=make_snapshot(store,config,sym,selected_batch,persist=False)
                store.record_attempt('research_input',sym,'OK')
            except Exception as exc:
                store.record_attempt('research_input',sym,'FAILED',str(exc))
                return {'symbol':sym,'status':'DEFERRED'}
            try:
                from .research import reusable,renew,persist_snapshot
                reuse=reusable(store,config,packet) if use_model else None
                if reuse:
                    result=renew(store,config,packet,reuse)
                else:
                    persist_snapshot(store,packet)
                    result=study(store,config,packet,use_model)
                store.record_attempt('research_pipeline',sym,'OK')
                return result
            except Exception as exc:
                store.record_attempt('research_pipeline',sym,'FAILED',str(exc))
                return {'symbol':sym,'status':'DEFERRED'}
        def research_stock(sym,selected_batch=None):
            result=None
            for attempt in range(config.get('research_attempts',2) if use_model else 1):
                result=research_once(sym,selected_batch)
                if result.get('status')!='DEFERRED':break
                failure=store.db.execute("SELECT detail FROM data_attempts WHERE symbol=? AND source IN ('research_input','research_analysis','research_pipeline') AND status!='OK' ORDER BY id DESC LIMIT 1",(sym,)).fetchone()
                if failure and re.search(r'登录|认证|额度|quota|rate.?limit|usage.?limit|unauthorized',failure[0],re.I):break
                # A fresh snapshot creates a new auditable study. The prior DRAFT stays
                # intact; failed reads never advance learning or supersede valid plans.
            return {**result,'attempts':attempt+1}
        scope='collection' if command in ('collect','cycle') else command
        with task_lock(store.root,scope):
            if command in ('collect','cycle'):
                studies=[]
                def after_ready(sym):
                    with task_lock(store.root,'research-'+sym):
                        studies.append(research_stock(sym))
                result=collect_batch(store,config,key,after_ready if command=='cycle' else None)
                result['studies']=studies
            elif command=='research':
                result=[]
                for item in config['watchlist']:
                    if symbol and item['symbol']!=symbol:continue
                    with task_lock(store.root,'research-'+item['symbol']):
                        result.append(research_stock(item['symbol'],batch_id))
            elif command=='repair':
                if symbol not in {i['symbol'] for i in config['watchlist']}:raise ValueError('只可恢复当前自选股')
                from .recovery import recovery_need
                need=recovery_need(store,config,symbol,now())
                with task_lock(store.root,'research-'+symbol):
                    if need['action']=='collect':
                        # Only this stock: avoid fetching public news and every other
                        # company's reports when recovering one missing disclosure.
                        from . import sources, fundamentals, market_context
                        from .research import begin_batch,finish_stock
                        batch,_=begin_batch(store,config)
                        bid=batch['id']
                        def collect_one(source,fn):
                            try:fn()
                            except Exception as exc:store.check(bid,source,symbol,'FAILED',str(exc)[:300])
                        from .inbox import import_inbox
                        collect_one('report_inbox',lambda:import_inbox(store,config,bid))
                        collect_one('market_comparison',lambda:market_context.collect_comparisons(store,bid,{**config,'watchlist':[i for i in config['watchlist'] if i['symbol']==symbol]}))
                        collect_one('tencent_daily',lambda:sources.collect_history(store,bid,symbol,config))
                        collect_one('financials',lambda:fundamentals.collect(store,bid,symbol))
                        collect_one('cninfo_catalog',lambda:sources.collect_announcements(store,bid,{'symbol':symbol},config,sources.stock_catalog(store)))
                        finish_stock(store,bid,{'symbol':symbol},None)
                        failed=store.db.execute("SELECT 1 FROM source_checks WHERE run_id=? AND status!='OK' LIMIT 1",(bid,)).fetchone()
                        state='PARTIAL' if failed else 'READY'
                        with store.db:
                            store.db.execute('UPDATE runs SET status=?,as_of=?,finished_at=? WHERE id=?',(state,now(),now(),bid))
                            store.db.execute('UPDATE batches SET status=?,finished_at=? WHERE id=?',(state,now(),bid))
                        result=research_stock(symbol,bid)
                    elif need['action']=='research':result=research_stock(symbol)
                    else:result={'status':'NEEDS_INPUT','reason':need['why']}
            elif command=='global_research':
                from .global_research import run
                result=run(store,config)
            elif command=='portfolio_strategy':
                from .portfolio_strategy import run
                result=run(store,config)
            elif command=='global_slot':
                from .global_paper import tick
                result=tick(store,config)
            elif command=='dynamic_cycle':
                from .dynamic import cycle
                result=cycle(store,config,end)
                if enabled(config) and config.get('scheduler_enabled'):
                    changed=store.db.execute("SELECT max(created_at) FROM macro_events WHERE created_at>(SELECT coalesce(max(created_at),'') FROM global_plans)").fetchone()[0]
                    pending=store.db.execute("SELECT 1 FROM jobs WHERE kind='global_research' AND status IN ('PENDING','RUNNING')").fetchone()
                    if changed and not pending:
                        from .scheduler import enqueue
                        enqueue(store,'global_research',now(),'global-event:'+changed)
            elif command=='dynamic_slot':
                from .dynamic_paper import run_slot as dynamic_slot
                result=dynamic_slot(store,config,scheduled_at)
            elif command=='evaluate':
                from .weekly import run_daily
                result=run_daily(store,config)
            elif command=='weekly_report':
                from .weekly import weekly_report
                result=weekly_report(store,config)
            elif command=='slot':result=run_slot(store,config,scheduled_at,use_model)
            elif command=='review':result=run_review(store,config,end,use_model)
            elif command=='settle':
                refresh_market(store,config,False)
                result={'fills':settle(store,config,now()),'account':mark_equity(store,now())}
            else:raise ValueError('未知阶段')
            changed=[]
            if command in ('cycle','research','repair','dynamic_cycle','global_research'):
                from .portfolio_strategy import request,changed_keys,enabled as portfolio_enabled
                # Only a real change in some candidate's research re-runs the portfolio and pauses its buys.
                changed=changed_keys(store,config,now()) if portfolio_enabled(config) else ['*']
                if changed:request(store,config,now(),changed=True)
            if role(config)=='research':
                from . import cloud_sync
                from .cloud_protocol import request as sync_request
                from .cloud_runtime import put,value
                try:
                    if command=='portfolio_strategy' and result.get('status')=='SUCCEEDED':
                        cloud_sync.queue_publication(store,config,result['decision_id'])
                        with task_lock(store.root,'cloud-sync'):cloud_sync.flush(store,config)
                    elif changed:
                        body={'changed_at':now()}
                        if '*' not in changed and cloud_sync.remote_supports(store,'targeted_invalidation'):body['keys']=changed
                        sync_request(config,'/api/sync/invalidate',body)
                        with store.db:put(store,'last_invalidation',{**body,'command':command})
                    elif command=='review':
                        from .cloud_ledger import rows
                        # Send only rows the cloud has not acknowledged; it stores them append-only.
                        cursors=value(store,'review_sync_cursors') or {}
                        tables=('reviews','lessons','research_methods','research_improvements');batch={};latest={}
                        for t in tables:
                            found=[dict(r) for r in store.db.execute('SELECT rowid AS sync_rowid,* FROM '+t+' WHERE rowid>? ORDER BY rowid',(cursors.get(t,0),))]
                            latest[t]=found[-1]['sync_rowid'] if found else cursors.get(t,0)
                            batch[t]=[{k:v for k,v in r.items() if k!='sync_rowid'} for r in found]
                        sync_request(config,'/api/sync/reviews',{**batch,'display':cloud_sync.display_packet(config)['reviews']})
                        with store.db:put(store,'review_sync_cursors',latest)
                except Exception as exc:
                    with store.db:put(store,'last_upload_error',{'at':now(),'error':str(exc)[:500]})
            store.periodic_backup(hourly_keep=config.get('backup_hourly_keep',6),daily_keep=config.get('backup_daily_keep',7))
            return result
    finally:store.close()
