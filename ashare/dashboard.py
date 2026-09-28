"""Loopback dashboard with renewable page sessions and same-origin mutations."""
from __future__ import annotations
import json
import secrets
import signal
import threading
import urllib.parse
import re
import os
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from .pipeline import load_config
from .storage import Store,now,json_write,normalize_time
from .paper import account
from .calendar import CALENDAR_VERSION,review_window,phase
from .scheduler import Scheduler,enqueue
from .workflow import task_lock
from .reporting import next_runs,portfolio,trade_effects
from .presentation import outstanding_failures,trader_report
from .guidance import trade_guidance
from .notices import open_for_display as open_notices
from . import auth,__version__


def status(config, *, overview=False):
    store=Store(config['data_dir'])
    try:
        at=now()
        if config.get('deployment_role')=='cloud':
            from .cloud_dashboard import status as cloud_status
            return cloud_status(store,config)
        from .investment_policy import enabled,seed,public
        if enabled(config):seed(store,at)
        a=account(store,at)
        from .portfolio_risk import state as risk_state
        a['risk']=risk_state(store)
        a['investment_policy']=public() if enabled(config) else None
        plans=[]
        for item in config['watchlist']:
            p=store.db.execute("SELECT p.*,s.snapshot_id,s.model_status,s.result_json FROM plans p JOIN studies s ON s.id=p.study_id WHERE p.symbol=? ORDER BY CASE WHEN p.status='ACTIVE' AND p.valid_until>? THEN 0 ELSE 1 END,p.activated_at DESC,p.rowid DESC LIMIT 1",(item['symbol'],now())).fetchone()
            plan=dict(p) if p else None
            if plan:
                plan['payload']=json.loads(plan.pop('payload_json'));plan['research']=json.loads(plan.pop('result_json'))
                plan['effective_status']='EXPIRED' if plan['valid_until']<=now() else plan['status']
                snapshot=store.db.execute('SELECT packet_json FROM snapshots WHERE id=?',(plan['snapshot_id'],)).fetchone()
                packet=json.loads(snapshot[0]) if snapshot else {}
                plan['learning']={k:v for k,v in packet.get('learning',{}).items() if k not in ('new_chunk_ids','revised_documents')}
                plan['external_events']=packet.get('external_events',[])
                plan['background_events']=packet.get('background_events',[])
                from .fundamentals import view as dossier_view
                plan['company_dossier']=dossier_view(packet.get('company_dossier',{}),(packet.get('stocks') or [{}])[0].get('quote'))
                plan['market_context']=((packet.get('stocks') or [{}])[0].get('features') or {}).get('market_context',{})
                plan['coverage']=[m for m in packet.get('mandatory_coverage',[]) if not m.get('required',True)]
                plan['trade_guidance']=trade_guidance(store,config,item['symbol'],plan,packet,at)
            latest=store.db.execute('SELECT id,created_at,model_status FROM studies WHERE symbol=? ORDER BY created_at DESC,rowid DESC LIMIT 1',(item['symbol'],)).fetchone()
            decision=store.latest_trade_check(item['symbol'],at)
            last_decision=dict(decision) if decision else None
            if last_decision:
                last_decision['buy_blockers']=json.loads(last_decision.pop('payload_json')).get('input_buy_blockers',[])
            successful=store.db.execute("SELECT max(created_at) FROM studies WHERE symbol=? AND model_status='SUCCEEDED'",(item['symbol'],)).fetchone()[0]
            from .recovery import recovery_status
            report=trader_report(plan,item['symbol']) if plan else None
            if overview and plan:
                # Full evidence remains available on the individual stock endpoint.
                for key in ('research','learning','external_events','background_events','company_dossier','market_context','coverage'):
                    plan.pop(key,None)
                report={k:report.get(k) for k in ('overview','next_checks')}
            plans.append({**item,'plan':plan,'report':report,
                'recovery':recovery_status(store,config,item['symbol'],at),
                'failures':outstanding_failures(store,item['symbol'],config),
                'last_decision':last_decision,
                'open_orders':[{k:o[k] for k in ('id','side','qty','filled_qty','limit_cents','status','created_at')}
                    for o in a['orders'] if o['symbol']==item['symbol'] and o.get('origin')!='dynamic'],
                'latest_study':dict(latest) if latest else None,'last_research_at':successful,'quote':store.latest_quote(item['symbol'],at)})
        reviews=[]
        for r in store.db.execute('''SELECT r.* FROM reviews r WHERE NOT EXISTS(
            SELECT 1 FROM reviews newer WHERE newer.window_start=r.window_start AND newer.window_end=r.window_end
            AND newer.revision>r.revision) ORDER BY r.window_end DESC LIMIT 5'''):
            d=dict(r);d['payload']=json.loads(d.pop('payload_json'))
            retry_count=store.db.execute("SELECT count(*) FROM jobs WHERE kind='review' AND id LIKE ?",('review-retry:'+r['window_end']+':%',)).fetchone()[0]
            d['automatic_retries_remaining']=max(0,2-retry_count) if config['scheduler_enabled'] and config['model_enabled'] and r['window_end']==normalize_time(review_window(at,config['review_time'])[1].isoformat()) else 0
            if overview:
                p=d['payload'];d['payload']={'facts':{'statistics':p['facts']['statistics']},'analysis':p['analysis'],
                    'analysis_error':p.get('analysis_error')}
                if 'daily_portfolio' in p['facts']:
                    d['payload']['facts']['daily_accounting']={k:p['facts']['daily_portfolio'][k] for k in ('window_start','window_end','totals')}
                if 'context_48h' in p['facts']:
                    d['payload']['facts']['context_48h']={k:p['facts']['context_48h'][k] for k in ('window_start','window_end','totals','positions','learning_notice')}
                if 'portfolio' in p['facts']:
                    portfolio_facts=p['facts']['portfolio']
                    d['payload']['facts']['portfolio']={k:portfolio_facts[k] for k in ('version','scope','positions','opening','closing','totals','valuation_notice','research')}
            reviews.append(d)
        from .portfolio_strategy import view as portfolio_strategy_view
        from .followups import view as followup_view
        from .dynamic import view as dynamic_view
        from .observation import view as observation_view
        from .cloud_dashboard import add_strategy_times
        return add_strategy_times(store,{'deployment_role':config.get('deployment_role','standalone'),'version':__version__,'at':at,'mode':config['mode'],'live_execution':False,'model_auth':'CHATGPT_SUBSCRIPTION',
            'scheduler_enabled':config['scheduler_enabled'],'calendar':CALENDAR_VERSION,
            'market_phase':phase(at),'quote_max_age_seconds':config['quote_max_age_seconds'],'schedule':{'collection':config['collection_times'],'slots':config['slot_times'],'review':config['review_time'],
                                               'execution_mode':config['slot_execution_mode']},
            'watchlist':plans,'observation':observation_view(store,at,config),'dynamic':dynamic_view(store,config,at),'account':a,'reviews':reviews,'followups':followup_view(store,at),
            'next_runs':next_runs(config,at),'portfolio':portfolio(store,config,a,at),
            'trade_effects':trade_effects(store,config,at),'portfolio_strategy':portfolio_strategy_view(store,config,at),
            'active_jobs':[dict(r) for r in store.db.execute("SELECT * FROM jobs WHERE status IN ('PENDING','RUNNING') ORDER BY scheduled_at")],
            'jobs':[dict(r) for r in store.db.execute('SELECT * FROM jobs ORDER BY scheduled_at DESC LIMIT 20')],
            'decisions':[dict(r) for r in store.db.execute('SELECT * FROM decisions WHERE EXISTS(SELECT 1 FROM paper_orders o WHERE o.decision_id=decisions.id) ORDER BY at DESC LIMIT 30')],
            'fills':[dict(r) for r in store.db.execute('SELECT * FROM paper_fills ORDER BY recorded_at DESC LIMIT 20')],
            'source_checks':[dict(r) for r in store.db.execute('SELECT * FROM source_checks ORDER BY id DESC LIMIT 20')],
            'background_failures':outstanding_failures(store,'MARKET'),
            'state':dict(store.db.execute('SELECT key,value FROM service_state')),
            'documents':store.db.execute('SELECT count(*) FROM documents').fetchone()[0],
            'snapshots':store.db.execute('SELECT count(*) FROM snapshots').fetchone()[0],
            # A research node delivers its notices to the cloud, where Dean answers them.
            'notices':open_notices(store) if config.get('deployment_role','standalone')=='standalone' else []})
    finally:store.close()


def make_handler(config_path,token,port):
    initial=load_config(config_path)
    origin=initial.get('public_origin') if initial.get('deployment_role')=='cloud' else None
    allowed_hosts={f'127.0.0.1:{port}',f'localhost:{port}'}
    if origin:
        parsed=urllib.parse.urlsplit(origin)
        if parsed.scheme!='https' or parsed.path not in ('','/') or not parsed.netloc:raise ValueError('云端页面须配置HTTPS公开地址')
        origin=origin.rstrip('/');allowed_hosts.add(parsed.netloc);allowed_hosts.add('healthcheck.railway.app')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,code,value,ctype='application/json; charset=utf-8',headers=None):
            raw=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
            self.send_response(code);self.send_header('Content-Type',ctype)
            self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            for k,v in (headers or {}).items():self.send_header(k,v)
            try:
                self.end_headers();self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):
                # A closed tab or an aborted read cannot receive another error response.
                self.close_connection=True
        def user(self,cfg):
            store=Store(cfg['data_dir'])
            try:return auth.session(store,self.headers.get('Cookie'))
            finally:store.close()
        def require_user(self,cfg,admin=False):
            user=self.user(cfg)
            if not user:self.send(401,{'code':'AUTH_REQUIRED','error':'请先登录。'});return None
            if admin and user['role']!='ADMIN':self.send(403,{'code':'ADMIN_REQUIRED','error':'此页面或操作仅管理员可用。'});return None
            return user
        def host_ok(self):
            if self.headers.get('Host') not in allowed_hosts:
                self.send(403,{'code':'HOST_FORBIDDEN','error':'请通过本机工作台地址打开页面。'});return False
            return True
        def origin_ok(self):
            origin=self.headers.get('Origin')
            if (origin and origin!=(initial.get('public_origin','').rstrip('/') if initial.get('deployment_role')=='cloud' else 'http://'+self.headers.get('Host',''))) or self.headers.get('Sec-Fetch-Site')=='cross-site':
                self.send(403,{'code':'ORIGIN_FORBIDDEN','error':'页面来源不匹配，请从本机工作台地址重新打开。'});return False
            return True
        def do_GET(self):
            if not self.host_ok():return
            path=urllib.parse.urlsplit(self.path)
            try:
                cfg=load_config(config_path)
                public=path.path in ('/login','/login.js','/app.js','/app.css','/api/session','/health')
                user=self.user(cfg)
                if not public and not user:
                    if path.path=='/' or path.path.startswith(('/stocks/','/admin/')):
                        self.send(302,b'',headers={'Location':'/login?next='+urllib.parse.quote(path.path,safe='')})
                    else:self.send(401,{'code':'AUTH_REQUIRED','error':'请先登录。'})
                    return
                admin_route=path.path.startswith('/admin/') or path.path in ('/api/feedback','/api/job','/api/snapshot','/api/search','/help/recovery')
                if admin_route and user['role']!='ADMIN':self.send(403,{'code':'ADMIN_REQUIRED','error':'此页面仅管理员可用。'});return
                if path.path=='/' or path.path in ('/admin/feedback','/admin/settings') or re.fullmatch(r'/stocks/(sh|sz)\d{6}',path.path):
                    template=(Path(__file__).parent/'web'/'index.html').read_text()
                    self.send(200,template.encode(),'text/html; charset=utf-8')
                elif path.path=='/login':self.send(200,(Path(__file__).parent/'web'/'login.html').read_bytes(),'text/html; charset=utf-8')
                elif path.path in ('/app.js','/app.css','/login.js'):
                    asset=Path(__file__).parent/'web'/path.path[1:]
                    self.send(200,asset.read_bytes(),'text/javascript; charset=utf-8' if path.path.endswith('.js') else 'text/css; charset=utf-8')
                elif path.path=='/help/recovery':
                    self.send(200,(Path(__file__).resolve().parents[1]/'docs'/'RECOVERY.md').read_bytes(),'text/plain; charset=utf-8')
                elif path.path=='/api/session':
                    if not self.origin_ok():return
                    store=Store(cfg['data_dir'])
                    try:initial=auth.setup_required(store)
                    finally:store.close()
                    self.send(200,{'csrf_token':user['csrf_token'] if user else token,'authenticated':bool(user),'setup_required':initial,
                        'user':{'username':user['username'],'role':user['role']} if user else None})
                elif path.path=='/api/job':
                    query=urllib.parse.parse_qs(path.query);store=Store(cfg['data_dir'])
                    try:
                        row=store.db.execute('SELECT * FROM jobs WHERE id=?',(query.get('id',[''])[0],)).fetchone()
                        self.send(200,dict(row)) if row else self.send(404,{'error':'未找到此任务记录。'})
                    finally:store.close()
                elif path.path=='/health':
                    from .cloud_ledger import FEATURES
                    from .build import code_fingerprint
                    self.send(200,{'ok':True,'version':__version__,'role':cfg.get('deployment_role','standalone'),
                        'features':list(FEATURES),'code':code_fingerprint(),'calendar':CALENDAR_VERSION})
                elif path.path=='/api/status':
                    value=status(cfg,overview=True)
                    if user['role']!='ADMIN':
                        for key in ('jobs','active_jobs','source_checks','background_failures','notices'):value[key]=[]
                        value['state']={k:v for k,v in value['state'].items() if k=='heartbeat'}
                    self.send(200,value)
                elif path.path=='/api/observations':
                    from .observation import view as observation_view
                    query=urllib.parse.parse_qs(path.query);offset=int(query.get('offset',['0'])[0])
                    if not 0<=offset<=100000:raise ValueError('分页位置无效。')
                    store=Store(cfg['data_dir'])
                    try:self.send(200,observation_view(store,now(),cfg,offset,query.get('asset',[None])[0]))
                    finally:store.close()
                elif path.path=='/api/stock':
                    from .stock_detail import detail
                    query=urllib.parse.parse_qs(path.query)
                    offset=int(query.get('offset',['0'])[0])
                    if not 0<=offset<=100000:raise ValueError('分页位置无效。')
                    self.send(200,detail(cfg,query.get('symbol',[''])[0],offset))
                elif path.path=='/api/feedback':
                    from .feedback import listing
                    query=urllib.parse.parse_qs(path.query);offset=int(query.get('offset',['0'])[0])
                    if not 0<=offset<=100000:raise ValueError('分页位置无效。')
                    store=Store(cfg['data_dir'])
                    try:self.send(200,listing(store,query.get('status',[None])[0],query.get('symbol',[None])[0],offset))
                    finally:store.close()
                elif path.path in ('/api/snapshot','/api/document','/api/search'):
                    query=urllib.parse.parse_qs(path.query);store=Store(cfg['data_dir'])
                    try:
                        if path.path=='/api/search':
                            result=store.search(query.get('q',[''])[0][:200],now(),query.get('symbol',[None])[0],limit=20)
                        elif path.path=='/api/snapshot':
                            row=store.db.execute('SELECT packet_json FROM snapshots WHERE id=?',(query.get('id',[''])[0],)).fetchone()
                            if not row:raise ValueError('快照不存在')
                            result=json.loads(row[0])
                        else:
                            if cfg.get('deployment_role')=='cloud':
                                from .cloud_runtime import value
                                preview=value(store,'display',{}).get('document_previews',{}).get(query.get('id',[''])[0])
                                if preview:
                                    self.send(200,preview);return
                            row=store.db.execute('SELECT d.*,m.ready_at,m.claim_type FROM documents d JOIN document_meta m ON m.doc_id=d.id WHERE d.id=?',(query.get('id',[''])[0],)).fetchone()
                            if not row:raise ValueError('文档不存在')
                            if user['role']!='ADMIN' and not row['cloud_allowed']:
                                self.send(403,{'error':'此资料未开放给访客。'});return
                            result={**dict(row),'chunks':[dict(r) for r in store.db.execute('SELECT page,ordinal,text FROM chunks WHERE doc_id=? ORDER BY ordinal',(row['id'],))]}
                            if user['role']!='ADMIN':result.pop('raw_path',None)
                        self.send(200,result)
                    finally:store.close()
                else:self.send(404,{'error':'Not found'})
            except Exception as exc:self.send(400,{'error':str(exc)[:500]})
        def sync_request(self):
            from .cloud_protocol import MAX_BODY,verify,unpack,packed
            from .cloud_sync import handle
            store=None
            try:
                cfg=load_config(config_path);length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=MAX_BODY or self.headers.get('Transfer-Encoding'):raise ValueError('无效同步请求长度')
                self.connection.settimeout(30);raw=self.rfile.read(length)
                if len(raw)!=length:raise ValueError('同步请求不完整')
                stamp=now();nonce=verify(cfg,self.path,raw,self.headers,stamp);body=unpack(raw)
                store=Store(cfg['data_dir']);store.db.execute('BEGIN IMMEDIATE')
                store.db.execute('INSERT INTO cloud_nonces VALUES(?,?)',(nonce,stamp))
                result=handle(store,cfg,self.path,body,stamp);store.db.commit()
                self.send(200,packed(result),'application/octet-stream')
            except Exception as exc:
                if store and store.db.in_transaction:store.db.rollback()
                self.send(409 if 'LEDGER_CHANGED' in str(exc) else 403,{'error':str(exc)[:250]})
            finally:
                if store:store.close()

        def do_POST(self):
            if not self.host_ok():return
            if self.path.startswith('/api/sync/'):
                self.sync_request();return
            if not self.origin_ok():return
            try:
                cfg=load_config(config_path);user=self.user(cfg)
                public=self.path in ('/api/login','/api/setup')
                if not public and not user:self.send(401,{'code':'AUTH_REQUIRED','error':'请先登录。'});return
                expected=user['csrf_token'] if user else token
                if not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),expected):
                    self.send(403,{'code':'SESSION_EXPIRED','error':'页面连接已更新，请重试此操作。'});return
                length=int(self.headers.get('Content-Length','0'))
                if length<0 or length>20000:raise ValueError('请求过大')
                body=json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(body,dict):raise ValueError('请求内容无效。')
                if not public and self.path not in ('/api/logout','/api/feedback') and user['role']!='ADMIN':
                    self.send(403,{'code':'ADMIN_REQUIRED','error':'此操作仅管理员可用。'});return
                if self.path in ('/api/setup','/api/login','/api/logout','/api/password'):
                    store=Store(cfg['data_dir'])
                    try:
                        if self.path=='/api/setup':
                            if cfg.get('deployment_role')=='cloud':raise auth.AuthError('云端账号通过签名迁移初始化',403)
                            auth.setup(store,body.get('admin_password'),body.get('guest_password'))
                            self.send(200,{'status':'CONFIGURED'})
                        elif self.path=='/api/login':
                            secret,current=auth.login(store,body.get('username'),body.get('password'),self.client_address[0])
                            self.send(200,{'username':current['username'],'role':current['role']},headers={'Set-Cookie':auth.cookie_header(secret,secure=cfg.get('deployment_role')=='cloud')})
                        elif self.path=='/api/logout':
                            auth.logout(store,user);self.send(200,{'status':'LOGGED_OUT'},headers={'Set-Cookie':auth.cookie_header(secure=cfg.get('deployment_role')=='cloud')})
                        else:
                            auth.reset_password(store,body.get('username',''),body.get('password'))
                            self.send(200,{'status':'PASSWORD_CHANGED','reauthenticate':body.get('username','').lower()==user['username']})
                    finally:store.close()
                elif self.path in ('/api/feedback','/api/feedback/update'):
                    from . import feedback
                    store=Store(cfg['data_dir'])
                    try:self.send(200,feedback.submit(store,cfg,user,body) if self.path=='/api/feedback' else feedback.update(store,body))
                    finally:store.close()
                elif self.path=='/api/run':
                    kind=body.get('kind')
                    from .cloud_runtime import assert_command
                    assert_command(cfg,kind)
                    if kind not in ('cycle','collect','research','slot','review','repair','dynamic_cycle','global_research','portfolio_strategy'):raise ValueError('未知操作')
                    symbol=body.get('symbol')
                    if kind=='repair' and symbol not in {i['symbol'] for i in cfg['watchlist']}:raise ValueError('请选择当前自选股')
                    store=Store(cfg['data_dir'])
                    try:
                        store.db.execute('BEGIN IMMEDIATE')
                        if kind=='repair':
                            busy=store.db.execute("SELECT j.id FROM jobs j JOIN job_inputs i ON i.job_id=j.id WHERE j.kind='repair' AND j.status IN ('PENDING','RUNNING') AND json_extract(i.payload_json,'$.symbol')=? LIMIT 1",(symbol,)).fetchone()
                        else:busy=store.db.execute("SELECT id FROM jobs WHERE kind=? AND status IN ('PENDING','RUNNING') LIMIT 1",(kind,)).fetchone()
                        when=review_window(now(),cfg['review_time'])[1].isoformat() if kind=='review' else now()
                        jid=busy[0] if busy else enqueue(store,kind,when,payload={'symbol':symbol} if kind=='repair' else None)
                        store.db.commit()
                        self.send(202,{'job_id':jid,'status':'QUEUED','reused':bool(busy)})
                    finally:store.close()
                elif self.path=='/api/notices/decide':
                    from .notices import decide
                    if cfg.get('deployment_role')=='research':raise auth.AuthError('通知在云端网页处理',403)
                    if not isinstance(body.get('id'),str) or not isinstance(body.get('action'),str):raise ValueError('请求内容无效。')
                    store=Store(cfg['data_dir'])
                    try:
                        row=decide(store,body['id'],body['action'],user['username'])
                        self.send(200,{'id':row['id'],'status':row['status'],'answered_at':row['acked_at']})
                    finally:store.close()
                elif self.path=='/api/settings':
                    if cfg.get('deployment_role')=='cloud':raise auth.AuthError('交易配置由本地研究端管理',403)
                    raw=json.loads(Path(config_path).read_text())
                    for key in body:
                        if key not in ('watchlist','scheduler_enabled','dynamic_enabled'):raise ValueError('此设置不支持网页修改')
                    raw.update(body)
                    from .settings import validate_settings
                    import re
                    symbols=[x['symbol'] for x in raw['watchlist']]
                    if not 1<=len(symbols)<=50:raise ValueError('请保留 1 至 50 只自选股。')
                    if len(set(symbols))!=len(symbols):raise ValueError('有重复的股票代码，请删除重复项后再保存。')
                    if any(not re.fullmatch(r'(sh|sz)\d{6}',s) for s in symbols):raise ValueError('股票代码格式不正确，请填写 sh600519 或 sz000333 这样的代码。')
                    if any(not isinstance(x.get('name'),str) or len(x['name'])>40 for x in raw['watchlist']):raise ValueError('股票名称请填写不超过 40 个字的文本。')
                    from .investment_policy import enabled,FIXED,MAX_ASSETS
                    if enabled(raw):
                        from .observation_pool import protected_assets
                        check_store=Store(cfg['data_dir'])
                        try:
                            if len(set(symbols)|set(FIXED)|protected_assets(check_store))>MAX_ASSETS:raise ValueError('新列表加上固定资产及持仓/未完成委托超过40，请保留受保护持仓的名额。')
                        finally:check_store.close()
                    validate_settings(raw);json_write(Path(config_path),raw)
                    self.send(200,{'status':'SAVED'})
                else:self.send(404,{'error':'Not found'})
            except auth.AuthError as exc:self.send(exc.code,{'error':str(exc)})
            except Exception as exc:self.send(400,{'error':str(exc)[:500]})
    return Handler


def serve(config_path,port=None):
    config=load_config(config_path);port=port or int(os.environ.get('PORT',config['ui_port']))
    scheduler=Scheduler(config_path)
    Handler=make_handler(config_path,secrets.token_urlsafe(32),port)
    root=Path(config['data_dir']);root.mkdir(parents=True,exist_ok=True)
    with task_lock(root,'service'):
        http=ThreadingHTTPServer(('0.0.0.0' if config.get('deployment_role')=='cloud' else '127.0.0.1',port),Handler)
        thread=threading.Thread(target=scheduler.run,name='scheduler',daemon=True);thread.start()
        def stop_service(signum,frame):
            scheduler.stop.set()
            from .model import cancel_models
            try:cancel_models()
            finally:threading.Thread(target=http.shutdown,daemon=True).start()
        old_handler=signal.signal(signal.SIGTERM,stop_service)
        print(f'本地控制台 http://127.0.0.1:{port} · 仅模拟 · Ctrl+C停止',flush=True)
        try:http.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:pass
        finally:
            from .model import cancel_models
            try:cancel_models()
            finally:
                signal.signal(signal.SIGTERM,old_handler)
                scheduler.stop.set();http.server_close();thread.join(timeout=15);scheduler.close()
