"""Compact strategy publications, completed-research receipts and cloud ledger feedback."""
import json
from datetime import datetime,timedelta
from .storage import now,normalize_time,digest,json_write
from .cloud_protocol import role,canonical,packed,request
from .cloud_runtime import value,put,contract
from . import cloud_ledger as ledger


def pull(store,config):
    if role(config)!='research':return None
    for _ in range(200):
        packet=request(config,'/api/sync/ledger',{'cursors':value(store,'ledger_cursors',{})})
        ledger.import_ledger(store,config,packet)
        if not packet['more']:return packet
    raise RuntimeError('账本仍在分页同步，完成前不进行组合决策或复盘')


def source_records(store,decisions):
    from .portfolio_strategy import source,row_token
    from .dynamic import history_stats
    from .global_market import targets
    tables={t:[] for t in ledger.SUPPORT};capsules={};seen={t:set() for t in tables}
    def add(t,r):
        if r and r['id'] not in seen[t]:tables[t].append(ledger.projection(t,r));seen[t].add(r['id'])
    for d in decisions:
        r=source(store,d['route'],d['identity'])
        if row_token(r)!=d['source_token']:raise ValueError('研究版本已更新，等待新的组合策略')
        c={'source_id':r['id'] if r else None,'symbol':d['symbol'],'route':d['route'],'identity':d['identity']}
        if r and d['route']=='watchlist':
            add('plans',r);s=store.db.execute('SELECT * FROM studies WHERE id=?',(r['study_id'],)).fetchone();add('studies',s)
            sn=store.db.execute('SELECT * FROM snapshots WHERE id=?',(s['snapshot_id'],)).fetchone();add('snapshots',sn)
            # Stable content fingerprints survive independent discovery timestamps.
            c['reviewed_documents']=[list(x) for x in store.db.execute('SELECT m.family_id,d.content_hash FROM snapshot_members s JOIN documents d ON d.id=s.doc_id JOIN document_meta m ON m.doc_id=d.id WHERE s.snapshot_id=?',(s['snapshot_id'],))]
        elif r and d['route']=='global':add('global_plans',r)
        elif r:
            add('dynamic_cases',r);add('dynamic_news',store.db.execute('SELECT * FROM dynamic_news WHERE id=?',(r['news_id'],)).fetchone())
            c['history']=history_stats(store,r['symbol'],r['event_type'],r['direction'],now())
        capsules[d['key']]=c
    return tables,capsules


def display_packet(config):
    from .dashboard import status
    s=status(config,overview=False)
    # UI summaries only; account, credentials, jobs, raw documents and model files are excluded.
    keep=('watchlist','observation','dynamic','reviews','followups','schedule','next_runs','calendar','quote_max_age_seconds')
    result={k:s[k] for k in keep};previews={}
    from .storage import Store
    local=Store(config['data_dir'])
    try:
        for item in s['watchlist']:
            p=item.get('plan')
            if not p:continue
            row=local.db.execute('SELECT packet_json FROM snapshots WHERE id=?',(p['snapshot_id'],)).fetchone()
            for e in json.loads(row[0] if row else '{}').get('evidence',[]):
                did=e.get('doc_id');doc=local.db.execute('SELECT * FROM documents WHERE id=?',(did,)).fetchone()
                if not doc or not doc['cloud_allowed']:continue
                preview=previews.setdefault(did,{k:doc[k] for k in ('id','title','url','source','symbol','published_at','cloud_allowed')})
                preview['scope']='CITED_EXCERPTS';preview['notice']='研究引用摘录；完整资料保存在本地。'
                chunks=preview.setdefault('chunks',[])
                if not any(c['text']==e['text'] for c in chunks):chunks.append({'page':e.get('page'),'ordinal':len(chunks),'text':e['text']})
    finally:local.close()
    result['document_previews']=previews
    return result


def queue_publication(store,config,decision_id):
    if role(config)!='research':return
    row=store.db.execute('SELECT * FROM portfolio_decisions WHERE id=?',(decision_id,)).fetchone()
    if not row:raise ValueError('缺少已完成的组合版本')
    decision=json.loads(row['payload_json']);tables,capsules=source_records(store,decision['decisions'])
    from .global_market import targets
    selected=targets(store,config,now())
    for k,c in capsules.items():
        if c['route']=='global':c['item']=selected.get(c['symbol'])
    watermark=store.db.execute("SELECT value FROM dynamic_state WHERE key='news_watermark'").fetchone()
    payload={'protocol':1,'kind':'strategy','bundle_id':row['id'],'completed_at':row['created_at'],'decision':dict(row),'sources':tables,'contracts':capsules,
             'ledger_version':decision.get('remote_ledger_version'),'news_watermark':watermark[0] if watermark else None,
             'active_assets':list({d['symbol'] for d in decision['decisions']}),'display':display_packet(config)}
    with store.db:
        if store.db.execute('SELECT 1 FROM cloud_outbox WHERE id=?',(row['id'],)).fetchone():return
        seq=value(store,'publication_sequence',0)+1;payload['sequence']=seq;put(store,'publication_sequence',seq)
        store.db.execute('INSERT INTO cloud_outbox VALUES(?,?,?,?,?)',(row['id'],now(),'PENDING',canonical(payload).decode(),None))
    json_write(store.root/'workflow/cloud-sync'/row['id']/'publication.json',payload)


def flush(store,config):
    rows=list(store.db.execute("SELECT * FROM cloud_outbox WHERE status='PENDING' ORDER BY created_at DESC,id DESC"))
    if not rows:return None
    latest=rows[0];payload=json.loads(latest['payload_json'])
    if payload['decision']['valid_until']<=now():
        with store.db:store.db.execute("UPDATE cloud_outbox SET status='EXPIRED',error='传输前策略已过期' WHERE id=?",(latest['id'],))
        return None
    try:
        answer=request(config,'/api/sync/strategy',payload)
        with store.db:
            store.db.execute("UPDATE cloud_outbox SET status='SENT',error=NULL WHERE id=?",(latest['id'],))
            store.db.execute("UPDATE cloud_outbox SET status='SUPERSEDED' WHERE status='PENDING' AND created_at<=?",(latest['created_at'],))
            put(store,'last_upload',{'at':now(),'bundle_id':latest['id'],'result':answer})
        json_write(store.root/'workflow/cloud-sync'/latest['id']/'receipt.json',answer)
        return answer
    except Exception as exc:
        with store.db:
            store.db.execute('UPDATE cloud_outbox SET error=? WHERE id=?',(str(exc)[:500],latest['id']))
            if 'LEDGER_CHANGED' in str(exc):
                store.db.execute("UPDATE cloud_outbox SET status='STALE' WHERE id=?",(latest['id'],))
                store.db.execute("INSERT OR REPLACE INTO service_state VALUES('portfolio_dirty',?)",(now(),))
        raise


def receive_strategy(store,config,body,at):
    from .portfolio_strategy import validate,row_token,source,cancel_incompatible_buys
    if not value(store,'initialized',False):raise ValueError('需要先迁移账本')
    if body.get('protocol')!=1 or body.get('kind')!='strategy':raise ValueError('同步协议不匹配')
    seq=body.get('sequence');bid=body.get('bundle_id');hashed=digest(canonical(body))
    if type(seq) is not int or seq<1 or not isinstance(bid,str):raise ValueError('无效发布序号')
    prior=store.db.execute('SELECT * FROM cloud_receipts WHERE sequence=? OR bundle_id=?',(seq,bid)).fetchone()
    if prior:
        if prior['payload_hash']!=hashed:raise ValueError('发布序号冲突')
        return {'status':'ALREADY_RECEIVED','bundle_id':bid,'completed_at':prior['completed_at']}
    if seq<=value(store,'last_sequence',0):raise ValueError('拒绝乱序策略')
    completed=normalize_time(body['completed_at']);age=(datetime.fromisoformat(at)-datetime.fromisoformat(completed)).total_seconds()
    if not 0<=age<43200 or completed<=value(store,'research_completed_at',''):raise ValueError('研究完成时间过期或回退')
    decision=body['decision']
    if decision['id']!=bid or decision['created_at']!=completed or decision['status']!='ACTIVE' or decision['version']!='cross_research_v1':raise ValueError('组合策略版本无效')
    until=normalize_time(decision['valid_until'])
    if not at<until<=normalize_time((datetime.fromisoformat(completed)+timedelta(hours=12)).isoformat()):raise ValueError('策略有效期无效')
    if body['ledger_version']!=ledger.version(store):raise ValueError('LEDGER_CHANGED: 分析期间云端持仓、现金或挂单变化，请重新判断')
    payload=json.loads(decision['payload_json']);items=payload['decisions']
    if not 1<=len(items)<=60 or len({d['key'] for d in items})!=len(items):raise ValueError('策略资产数量或重复项错误')
    if set(body['sources'])!=set(ledger.SUPPORT) or set(body['contracts'])!={d['key'] for d in items}:raise ValueError('策略与研究引用不完整')
    allowed=set(w['symbol'] for w in config['watchlist'])
    for d in items:
        if d['route'] not in ('watchlist','dynamic','global') or d['key']!=d['route']+':'+d['identity']:raise ValueError('交易线路错误')
        if d['route']=='watchlist' and d['symbol'] not in allowed:raise ValueError('未经配置的自选股')
        cap=min(config['paper_max_stock_pct'],20 if d['route']=='watchlist' else 5)*100
        if d['action']=='ALLOW' and d['target_bps']>cap:raise ValueError('买入目标超过硬上限')
    raw={k:payload[k] for k in ('summary','risk_groups')};raw['decisions']=[{k:d[k] for k in ('key','action','target_bps','reason','evidence_ids','related_keys')} for d in items]
    from .portfolio_risk import state as risk_state
    validate(raw,{'candidates':items,'halted':risk_state(store)['halted'],'valuation_ready':True,'gross_cap_bps':config['paper_max_gross_pct']*100,'symbol_cap_bps':config['paper_max_stock_pct']*100})
    store.db.execute('PRAGMA defer_foreign_keys=ON')
    for table,records in body['sources'].items():
        if table=='plans':
            for r in records:store.db.execute("UPDATE plans SET status='SUPERSEDED' WHERE symbol=? AND status='ACTIVE' AND id!=?",(r['symbol'],r['id']))
        if table=='global_plans':
            for r in records:store.db.execute("UPDATE global_plans SET status='SUPERSEDED' WHERE symbol=? AND status='ACTIVE' AND id!=?",(r['symbol'],r['id']))
        ledger.upsert(store,table,records,immutable=table in ('snapshots','studies'))
    for d in items:
        if d['source_token']!=row_token(source(store,d['route'],d['identity'])):raise ValueError('源策略摘要校验失败')
    store.db.execute('DELETE FROM cloud_contracts')
    for k,c in body['contracts'].items():store.db.execute('INSERT INTO cloud_contracts VALUES(?,?,?)',(k,completed,canonical(c).decode()))
    put(store,'active_assets',body['active_assets']);put(store,'display',body['display'])
    store.db.execute("UPDATE portfolio_decisions SET status='SUPERSEDED' WHERE status='ACTIVE'")
    ledger.upsert(store,'portfolio_decisions',[decision])
    if body.get('news_watermark'):store.db.execute("INSERT OR REPLACE INTO dynamic_state VALUES('news_watermark',?)",(normalize_time(body['news_watermark']),))
    put(store,'research_completed_at',completed);put(store,'research_received_at',at);put(store,'last_sequence',seq)
    store.db.execute('INSERT INTO cloud_receipts VALUES(?,?,?,?,?)',(seq,bid,at,completed,hashed))
    cancelled=cancel_incompatible_buys(store,config,at,bid)
    return {'status':'ACCEPTED','bundle_id':bid,'completed_at':completed,'cancelled_buys':cancelled,'ledger_version':ledger.version(store)}


def handle(store,config,path,body,at):
    if path=='/api/sync/bootstrap':return ledger.import_bootstrap(store,config,body,at)
    if path=='/api/sync/ledger':return ledger.export_ledger(store,body.get('cursors',{}),at)
    if path=='/api/sync/strategy':return receive_strategy(store,config,body,at)
    if path=='/api/sync/activate':
        if body.get('local_execution_disabled') is not True:raise ValueError('必须先停用本地交易执行')
        if not value(store,'initialized',False) or not value(store,'research_completed_at'):raise ValueError('账本与首轮策略尚未就绪')
        if body.get('ledger_version')!=ledger.version(store):raise ValueError('启动前账本版本不匹配')
        put(store,'execution_enabled',True);return {'status':'ACTIVE'}
    if path=='/api/sync/invalidate':
        changed=normalize_time(body['changed_at'])
        if abs((datetime.fromisoformat(at)-datetime.fromisoformat(changed)).total_seconds())>300:raise ValueError('失效通知时间无效')
        if changed>value(store,'research_completed_at',''):
            put(store,'invalidated_at',changed)
            from .portfolio_strategy import cancel_incompatible_buys
            cancel_incompatible_buys(store,config,at,'research-invalidation')
        return {'status':'ACCEPTED'}
    if path=='/api/sync/reviews':
        for t in ('reviews','lessons','research_methods','research_improvements'):ledger.upsert(store,t,body.get(t,[]),immutable=True)
        put(store,'display_reviews',body.get('display',[]));return {'status':'ACCEPTED'}
    raise ValueError('未知同步接口')


def sync_once(config):
    from .storage import Store
    from .workflow import task_lock
    store=Store(config['data_dir'])
    phase='pull'
    try:
        with task_lock(store.root,'cloud-sync'):
            offline_since=value(store,'offline_since')
            packet=pull(store,config)
            recovered_at=now()
            if offline_since:
                # A brief deploy restart must not restart all 22 stocks' research.
                with store.db:
                    if (datetime.fromisoformat(recovered_at)-datetime.fromisoformat(offline_since)).total_seconds()>=120:
                        put(store,'reconnect_pending',recovered_at)
                    put(store,'offline_since',None)
            phase='publish';answer=flush(store,config)
            with store.db:put(store,'last_sync',{'at':now(),'status':'OK','ledger_version':packet['ledger_version']})
            return answer or {'status':'SYNCED'}
    except Exception as exc:
        if str(exc).startswith('BUSY:'):return {'status':'BUSY'}
        with store.db:
            if phase=='pull' and not value(store,'offline_since'):put(store,'offline_since',now())
            put(store,'last_sync',{'at':now(),'status':'FAILED','phase':phase,'error':str(exc)[:500]})
        raise
    finally:store.close()
