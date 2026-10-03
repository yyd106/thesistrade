"""Compact strategy publications, completed-research receipts and cloud ledger feedback."""
import json
from datetime import datetime,timedelta
from .storage import now,normalize_time,digest,json_write
from .cloud_protocol import role,canonical,packed,request
from .cloud_runtime import value,put,contract
from . import cloud_ledger as ledger


def notices_unresolved(store):
    from .notices import unresolved
    return unresolved(store)


def pull(store,config):
    if role(config)!='research':return None
    for _ in range(200):
        # An older cloud ignores the v2 fields and answers with the v1 full-table packet.
        packet=request(config,'/api/sync/ledger',{'cursors':value(store,'ledger_cursors',{}),'protocol':2,
            'change_cursor':value(store,'ledger_change_cursor'),'support_since':value(store,'ledger_support_since'),
            'extras':{'quote_health':value(store,'quote_health_since'),
                      'notices':{'since':value(store,'cloud_notices_since'),'known':notices_unresolved(store)}}})
        ledger.import_ledger(store,config,packet)
        if not packet['more']:return packet
    raise RuntimeError('账本仍在分页同步，完成前不进行组合决策或复盘')


def remote_supports(store,feature):
    return feature in (value(store,'remote_features') or [])


def deliver_notices(store,config):
    """Send notices written here to the cloud. Dean's answers, and notices the cloud raised itself, come
    back with every ledger pull."""
    from . import notices
    if role(config)!='research' or not remote_supports(store,'notices'):return None
    new=notices.pending(store)
    if not new:return None
    at=now()
    answer=request(config,'/api/sync/notices',{'notices':[notices.outgoing(n) for n in new],'known':[]})
    states=answer.get('states') or {}
    with store.db:
        notices.mark_delivered(store,[n['id'] for n in new if n['id'] in states],at)
        notices.apply_states(store,states)
    return {'delivered':sum(1 for n in new if n['id'] in states)}


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
    keep=('fixed_watchlist','watchlist','observation','dynamic','reviews','followups','schedule','next_runs','calendar','quote_max_age_seconds','supervision','industry')
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


# The cloud recomputes these per-asset fields from its own ledger and received strategies, so they are
# never sent. last_strategy_updated_at changes with every new strategy for every asset; sending it would
# mark every section as changed and defeat the delta.
VOLATILE=('quote','last_decision','open_orders','last_strategy_updated_at')


def display_sections(display):
    """Split the UI summary into independently versioned parts: one per watchlist stock plus each top-level block."""
    sections={}
    for key,item in display.items():
        if key=='watchlist':
            sections['watchlist:__order__']=[w['symbol'] for w in item]
            for w in item:sections['watchlist:'+w['symbol']]={k:v for k,v in w.items() if k not in VOLATILE}
        elif key=='observation' and isinstance(item,dict) and isinstance(item.get('items'),list):
            sections['top:'+key]={**item,'items':[{k:v for k,v in i.items() if k not in VOLATILE} if isinstance(i,dict) else i for i in item['items']]}
        else:sections['top:'+key]=item
    return sections


def assemble_display(sections):
    display={k[4:]:v for k,v in sections.items() if k.startswith('top:')}
    display['watchlist']=[sections['watchlist:'+s] for s in sections.get('watchlist:__order__',[]) if 'watchlist:'+s in sections]
    return display


def section_hashes(sections):
    return {k:digest(canonical(v))[:16] for k,v in sections.items()}


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
    display=display_packet(config)
    payload={'protocol':1,'kind':'strategy','bundle_id':row['id'],'completed_at':row['created_at'],'decision':dict(row),'sources':tables,'contracts':capsules,
             'ledger_version':decision.get('remote_ledger_version'),'news_watermark':watermark[0] if watermark else None,
             'active_assets':list({d['symbol'] for d in decision['decisions']}),'display':display}
    if config.get('industry_enabled'):
        if not remote_supports(store,'industry_lists_v1'):raise ValueError('云端尚不支持双名单，等待云端升级')
        from .universe import publication
        payload['research_membership']=publication(store,config,row['created_at'],row['valid_until'])
    if remote_supports(store,'display_delta'):
        # Send only the parts of the UI summary the cloud does not already hold.
        sections=display_sections(display);hashes=section_hashes(sections);known=value(store,'remote_display_hashes') or {}
        payload['display']=None
        payload['display_delta']={'hashes':hashes,'sections':{k:v for k,v in sections.items() if known.get(k)!=hashes[k]}}
    with store.db:
        if store.db.execute('SELECT 1 FROM cloud_outbox WHERE id=?',(row['id'],)).fetchone():return
        seq=value(store,'publication_sequence',0)+1;payload['sequence']=seq;put(store,'publication_sequence',seq)
        body=canonical(payload).decode()
        store.db.execute('INSERT INTO cloud_outbox VALUES(?,?,?,?,?)',(row['id'],now(),'PENDING',body,None))
    # A small manifest replaces the multi-megabyte publication copy; the database keeps the body until sent.
    enc=lambda v:len(canonical(v))
    json_write(store.root/'workflow/cloud-sync'/row['id']/'manifest.json',{'bundle_id':row['id'],'sequence':seq,'created_at':now(),
        'sha256':digest(body.encode()),'bytes':len(body.encode()),'sections':{k:enc(v) for k,v in payload.items() if k not in ('sequence',)},
        'display_mode':'DELTA' if payload['display'] is None else 'FULL',
        'display_sections_sent':sorted(payload.get('display_delta',{}).get('sections',{}))})


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
            receipt={k:v for k,v in answer.items() if k!='display_hashes'}
            put(store,'last_upload',{'at':now(),'bundle_id':latest['id'],'result':receipt})
            if isinstance(answer.get('display_hashes'),dict):put(store,'remote_display_hashes',answer['display_hashes'])
            elif payload.get('display') is not None:put(store,'remote_display_hashes',{})
        json_write(store.root/'workflow/cloud-sync'/latest['id']/'receipt.json',receipt)
        from .maintenance import prune_outbox
        prune_outbox(store)
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
    if config.get('industry_enabled'):
        from .universe import validate_publication
        members=validate_publication(store,config,body.get('research_membership'),items,completed,until)
        allowed.update(members)
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
    put(store,'active_assets',body['active_assets'])
    if config.get('industry_enabled'):put(store,'research_membership',body['research_membership'])
    display_hashes=receive_display(store,body)
    # Key invalidations older than this decision no longer apply.
    put(store,'invalidated_keys',{k:v for k,v in (value(store,'invalidated_keys') or {}).items() if v>completed})
    store.db.execute("UPDATE portfolio_decisions SET status='SUPERSEDED' WHERE status='ACTIVE'")
    ledger.upsert(store,'portfolio_decisions',[decision])
    if body.get('news_watermark'):store.db.execute("INSERT OR REPLACE INTO dynamic_state VALUES('news_watermark',?)",(normalize_time(body['news_watermark']),))
    put(store,'research_completed_at',completed);put(store,'research_received_at',at);put(store,'last_sequence',seq)
    store.db.execute('INSERT INTO cloud_receipts VALUES(?,?,?,?,?)',(seq,bid,at,completed,hashed))
    cancelled=cancel_incompatible_buys(store,config,at,bid)
    return {'status':'ACCEPTED','bundle_id':bid,'completed_at':completed,'cancelled_buys':cancelled,'ledger_version':ledger.version(store),
            'display_hashes':display_hashes}


def receive_display(store,body):
    """Full display (older research nodes) or a delta over the sections the cloud already holds.
    A section whose base is missing keeps its last value; the returned hashes tell the sender what to resend."""
    delta=body.get('display_delta')
    if body.get('display') is None and isinstance(delta,dict):
        wanted=delta.get('hashes') or {};provided=delta.get('sections') or {};stored=value(store,'display_sections') or {}
        merged={}
        for key,expected in wanted.items():
            if key in provided and digest(canonical(provided[key]))[:16]==expected:merged[key]=provided[key]
            elif key in stored:merged[key]=stored[key]
    else:
        merged=display_sections(body.get('display') or {})
    put(store,'display_sections',merged);put(store,'display',assemble_display(merged))
    return section_hashes(merged)


def handle(store,config,path,body,at):
    if path=='/api/sync/bootstrap':return ledger.import_bootstrap(store,config,body,at)
    if path=='/api/sync/ledger':return ledger.export_ledger(store,body.get('cursors',{}),at,body=body)
    if path=='/api/sync/strategy':return receive_strategy(store,config,body,at)
    if path=='/api/sync/activate':
        if body.get('local_execution_disabled') is not True:raise ValueError('必须先停用本地交易执行')
        if not value(store,'initialized',False) or not value(store,'research_completed_at'):raise ValueError('账本与首轮策略尚未就绪')
        if body.get('ledger_version')!=ledger.version(store):raise ValueError('启动前账本版本不匹配')
        put(store,'execution_enabled',True);return {'status':'ACTIVE'}
    if path=='/api/sync/invalidate':
        changed=normalize_time(body['changed_at'])
        if abs((datetime.fromisoformat(at)-datetime.fromisoformat(changed)).total_seconds())>300:raise ValueError('失效通知时间无效')
        keys=body.get('keys')
        if changed>value(store,'research_completed_at',''):
            if isinstance(keys,list) and keys and '*' not in keys:
                # Only the listed candidates (and decisions depending on them) stop buying.
                if len(keys)>200 or any(not isinstance(k,str) or not 3<=len(k)<=120 for k in keys):raise ValueError('失效标的格式无效')
                current=value(store,'invalidated_keys') or {}
                for k in keys:current[k]=max(current.get(k,''),changed)
                put(store,'invalidated_keys',current)
            else:put(store,'invalidated_at',changed)
            from .portfolio_strategy import cancel_incompatible_buys
            cancel_incompatible_buys(store,config,at,'research-invalidation')
        return {'status':'ACCEPTED','scope':'KEYS' if isinstance(keys,list) and keys and '*' not in keys else 'ALL'}
    if path=='/api/sync/notices':
        from .notices import receive
        return receive(store,body,at)
    if path=='/api/sync/reviews':
        if 'page_display' in body:
            if set(body) != {'page_display'}:
                raise ValueError('独立展示摘要不能混入账本、策略或监督数据')
            from .page_display import receive
            return receive(store,body['page_display'],at)
        for t in ('reviews','lessons','research_methods','research_improvements'):ledger.upsert(store,t,body.get(t,[]),immutable=True)
        if 'display' in body:put(store,'display_reviews',body['display'])
        if 'supervision' in body:
            summary=body['supervision']
            if not isinstance(summary,dict) or not isinstance(summary.get('items'),list) or len(summary['items'])>30 or len(canonical(summary))>1_000_000:
                raise ValueError('监督摘要格式或大小无效')
            put(store,'display_supervision',summary)
        return {'status':'ACCEPTED'}
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
                failure=(value(store,'last_sync') or {}).get('error')
                long_enough=(datetime.fromisoformat(recovered_at)-datetime.fromisoformat(offline_since)).total_seconds()>=120
                with store.db:
                    if long_enough:put(store,'reconnect_pending',recovered_at)
                    put(store,'offline_since',None)
                if long_enough:
                    from .connectivity import local_network_error,log_interval
                    log_interval(store.root,'offline',offline_since,recovered_at,cause='NETWORK' if local_network_error(failure) else 'CLOUD')
            if config.get('industry_enabled'):
                from .portfolio_strategy import changed_keys,request as request_portfolio
                changed=changed_keys(store,config,now())
                if changed:
                    # Persist once per distinct change; a retry must not keep moving the invalidation clock.
                    from .portfolio_strategy import candidate_sources,row_token
                    from .universe import membership
                    marker=digest(canonical([changed,{k:row_token(v) for k,v in candidate_sources(store,config,now()).items()},[(m['symbol'],m['fingerprint']) for m in membership(store,config,now())]]))
                    if value(store,'membership_invalidation_marker')!=marker:
                        queue_invalidation(store,changed,now())
                        with store.db:put(store,'membership_invalidation_marker',marker)
                    request_portfolio(store,config,now(),changed=True)
                else:
                    with store.db:put(store,'membership_invalidation_marker',None)
            phase='invalidate';deliver_invalidation(store,config)
            phase='publish';answer=flush(store,config)
            with store.db:put(store,'last_sync',{'at':now(),'status':'OK','ledger_version':packet['ledger_version']})
            try:
                delivered=deliver_notices(store,config)
                if delivered is not None:
                    with store.db:put(store,'notices_sync',{'at':now(),'status':'OK',**delivered})
            except Exception as exc:
                # Notices wait for the next minute's sync; they never hold up the ledger or strategy.
                with store.db:put(store,'notices_sync',{'at':now(),'status':'FAILED','error':str(exc)[:300]})
            try:
                deliver_supervision(store,config)
            except Exception as exc:
                with store.db:put(store,'supervision_sync',{'at':now(),'status':'FAILED','error':str(exc)[:300]})
            try:
                deliver_page_display(store,config)
            except Exception as exc:
                # A stale page must never stop accounting or renew strategy authority.
                with store.db:put(store,'page_display_sync',{'at':now(),'status':'FAILED','error':str(exc)[:300]})
            return answer or {'status':'SYNCED'}
    except Exception as exc:
        if str(exc).startswith('BUSY:'):return {'status':'BUSY'}
        with store.db:
            if phase=='pull' and not value(store,'offline_since'):put(store,'offline_since',now())
            put(store,'last_sync',{'at':now(),'status':'FAILED','phase':phase,'error':str(exc)[:500]})
        raise
    finally:store.close()


def deliver_supervision(store,config):
    """Display-only publication. This endpoint never writes research_completed_at or a strategy."""
    if not remote_supports(store,'supervision_summary'):return None
    from .supervision import view
    summary=view(store);fingerprint=digest(canonical(summary))
    if value(store,'supervision_sent_hash')==fingerprint:return None
    answer=request(config,'/api/sync/reviews',{'supervision':summary})
    if answer.get('status')!='ACCEPTED':raise ValueError('云端未确认监督摘要')
    with store.db:
        put(store,'supervision_sent_hash',fingerprint)
        put(store,'supervision_sync',{'at':now(),'status':'OK'})
    return answer


def deliver_page_display(store,config):
    """Publish only bounded review/news presentation, independently of research."""
    from .page_display import FEATURE, collect
    if role(config)!='research' or not remote_supports(store,FEATURE):return None
    packet=collect(store,config)
    fingerprint=packet['content_hash']
    if value(store,'page_display_sent_hash')==fingerprint:return None
    answer=request(config,'/api/sync/reviews',{'page_display':packet})
    receipt=answer.get('page_display') or {}
    if answer.get('status')!='ACCEPTED' or receipt.get('status') not in ('UPDATED','UNCHANGED','IGNORED_STALE'):
        raise ValueError('云端未确认复盘与新闻展示摘要')
    if receipt.get('status')=='IGNORED_STALE':
        with store.db:put(store,'page_display_sync',{'at':now(),'status':'SUPERSEDED','generated_at':receipt.get('generated_at')})
        return answer
    if receipt.get('content_hash')!=fingerprint or receipt.get('generated_at')!=packet['generated_at']:
        raise ValueError('云端展示摘要回执与本次发送不一致')
    with store.db:
        put(store,'page_display_sent_hash',fingerprint)
        put(store,'page_display_sync',{'at':now(),'status':'OK','generated_at':packet['generated_at'],
            'content_hash':fingerprint,'review_bytes':len(canonical(packet['reviews'])),'news_bytes':len(canonical(packet['macro']))})
    return answer


def queue_invalidation(store,keys,at):
    """Persist revocation before networking; failed sends survive process restarts."""
    prior=value(store,'pending_invalidation') or {}
    combined=set(prior.get('keys',[]))|set(keys)
    with store.db:put(store,'pending_invalidation',{'changed_at':max(at,prior.get('changed_at','')),'keys':sorted(combined)})


def deliver_invalidation(store,config):
    pending=value(store,'pending_invalidation')
    if not pending:return None
    body={'changed_at':pending['changed_at']}
    if '*' not in pending['keys'] and remote_supports(store,'targeted_invalidation'):body['keys']=pending['keys']
    result=request(config,'/api/sync/invalidate',body)
    with store.db:
        # Another worker may have appended a later revocation while this request ran.
        if value(store,'pending_invalidation')==pending:put(store,'pending_invalidation',None)
        put(store,'last_invalidation',{**body,'receipt':result,'acknowledged_at':now()})
    return result
