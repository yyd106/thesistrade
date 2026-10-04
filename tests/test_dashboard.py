"""Exercise HTTP handlers using in-memory sockets, with isolated data and no worker."""
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from ashare.dashboard import make_handler,status
from ashare.storage import Store,now
from ashare import auth


class MemorySocket:
    def __init__(self, request):
        self.request=io.BytesIO(request)
        self.response=io.BytesIO()
    def makefile(self,*args):return self.request
    def sendall(self,data):self.response.write(data)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'config.json'
        cfg=json.loads((Path(__file__).resolve().parents[1]/'config.json').read_text())
        cfg.update(data_dir=str(Path(self.tmp.name)/'data'),scheduler_enabled=False,model_enabled=False)
        self.path.write_text(json.dumps(cfg));self.data=cfg['data_dir']
        store=Store(self.data);auth.setup(store,'test-admin-password','test-guest-password')
        token,self.user=auth.login(store,'admin','test-admin-password','test-admin');self.cookie=auth.COOKIE+'='+token
        self.csrf=self.user['csrf_token'];store.close()
        self.handler=make_handler(self.path,'current-session',8765)

    def tearDown(self):self.tmp.cleanup()

    def request(self,method,path,body=None,headers=None,handler=None):
        raw=json.dumps(body or {}).encode() if method=='POST' else b''
        h={'Host':'127.0.0.1:8765','Content-Length':str(len(raw)),'Cookie':self.cookie,**(headers or {})}
        request=(f'{method} {path} HTTP/1.0\r\n'+''.join(f'{k}: {v}\r\n' for k,v in h.items())+'\r\n').encode()+raw
        sock=MemorySocket(request);(handler or self.handler)(sock,('127.0.0.1',1234),None)
        head,payload=sock.response.getvalue().split(b'\r\n\r\n',1)
        code=int(head.split(b' ')[1]);return code,payload,head

    def post(self,kind,token=None,**headers):
        code,raw,_=self.request('POST','/api/run',{'kind':kind},{'X-CSRF-Token':token or self.csrf,'Origin':'http://127.0.0.1:8765',**headers})
        return code,json.loads(raw)

    def test_expired_page_cannot_mutate_and_can_obtain_new_session(self):
        code,body=self.post('cycle','old-session')
        self.assertEqual((code,body['code']),(403,'SESSION_EXPIRED'))
        store=Store(self.data)
        self.assertEqual(store.db.execute('SELECT count(*) FROM jobs').fetchone()[0],0);store.close()
        code,raw,head=self.request('GET','/api/session')
        self.assertIn(b'Cache-Control: no-store',head)
        self.assertEqual(code,200)
        fresh=json.loads(raw)['csrf_token']
        self.assertEqual(self.post('cycle',fresh)[0],202)
        restarted=make_handler(self.path,'after-restart',8765)
        code,raw,_=self.request('GET','/api/session',handler=restarted)
        self.assertEqual(json.loads(raw)['csrf_token'],self.csrf)

    def test_all_five_actions_and_settings_work(self):
        for kind in ('cycle','collect','research','slot','review'):
            code,body=self.post(kind);self.assertEqual(code,202)
            code,raw,_=self.request('GET','/api/job?id='+body['job_id'])
            self.assertEqual(code,200);self.assertEqual(json.loads(raw)['kind'],kind)
        code,raw,_=self.request('POST','/api/settings',{'scheduler_enabled':True},{'X-CSRF-Token':self.csrf})
        self.assertEqual(code,200)
        self.assertTrue(json.loads(self.path.read_text())['scheduler_enabled'])

    def test_fixed_list_edit_requires_exact_approval_and_status_separates_fixed_members(self):
        original=json.loads(self.path.read_text())['watchlist']
        code,raw,_=self.request('GET','/api/status')
        self.assertEqual(json.loads(raw)['fixed_watchlist'],original)
        changed=[{**w,'name':w['name']+'核对'} if i==0 else w for i,w in enumerate(original)]
        code,raw,_=self.request('POST','/api/settings',{'watchlist':changed},{'X-CSRF-Token':self.csrf})
        self.assertEqual(code,202,raw)
        response=json.loads(raw)
        self.assertEqual(response['status'],'APPROVAL_PENDING')
        self.assertEqual(json.loads(self.path.read_text())['watchlist'],original)
        self.assertFalse((Path(self.data)/'workflow/changes/config-changes.jsonl').exists())
        request=response['approval_request']
        self.assertEqual(request['summary']['changes'][0]['after'],changed)
        self.assertEqual(request['summary']['changes'][0]['before'],original)

    def test_foreign_origin_or_host_remains_blocked(self):
        code,body=self.post('cycle',Origin='https://foreign.example')
        self.assertEqual((code,body['code']),(403,'ORIGIN_FORBIDDEN'))
        for headers in ({'Origin':'https://foreign.example'},{'Sec-Fetch-Site':'cross-site'},{'Host':'foreign.example'}):
            self.assertEqual(self.request('GET','/api/session',headers=headers)[0],403)
        self.assertEqual(self.post('cycle',Host='foreign.example')[0],403)
        self.assertEqual(self.post('cycle',**{'Sec-Fetch-Site':'cross-site'})[0],403)
        self.assertEqual(self.request('POST','/api/run',{'kind':'cycle'})[0],403)

    def test_concurrent_clicks_create_one_job(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses=list(pool.map(lambda _:self.post('research'),range(6)))
        self.assertTrue(all(code==202 for code,_ in responses))
        self.assertEqual(len({r['job_id'] for _,r in responses}),1)
        self.assertEqual(sum(not r['reused'] for _,r in responses),1)

    def test_repair_is_scoped_deduplicated_and_rejects_unknown_stock(self):
        cfg=json.loads(self.path.read_text());symbol=cfg['watchlist'][0]['symbol']
        headers={'X-CSRF-Token':self.csrf}
        first=self.request('POST','/api/run',{'kind':'repair','symbol':symbol},headers)
        second=self.request('POST','/api/run',{'kind':'repair','symbol':symbol},headers)
        self.assertEqual(first[0],202);self.assertEqual(second[0],202)
        jid=json.loads(first[1])['job_id'];self.assertEqual(jid,json.loads(second[1])['job_id'])
        store=Store(self.data)
        try:self.assertEqual(json.loads(store.db.execute('SELECT payload_json FROM job_inputs WHERE job_id=?',(jid,)).fetchone()[0]),{'symbol':symbol})
        finally:store.close()
        self.assertEqual(self.request('POST','/api/run',{'kind':'repair','symbol':'not-a-stock'},headers)[0],400)

    def test_reviews_display_latest_revision_per_window(self):
        store=Store(self.data)
        try:
            payload=json.dumps({'facts':{'statistics':{}},'analysis':{'summary':'version','lessons':[]}})
            with store.db:
                for rid,end,rev in [('old','2026-09-21T11:30:00+00:00',1),('new','2026-09-21T11:30:00+00:00',2),('next','2026-09-22T11:30:00+00:00',1)]:
                    store.db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)',(rid,'2026-09-20T11:30:00+00:00',end,rev,'2026-09-23T01:00:00+00:00',rid,'SUCCEEDED',payload))
        finally:store.close()
        code,raw,_=self.request('GET','/api/status');self.assertEqual(code,200)
        self.assertEqual([r['id'] for r in json.loads(raw)['reviews']],['next','new'])

    def test_assets_and_status_are_readable(self):
        for path in ('/','/app.js','/app.css','/api/status','/help/recovery'):
            code,raw,_=self.request('GET',path);self.assertEqual(code,200)
            if path=='/api/status':
                s=json.loads(raw);self.assertIn('market_phase',s);self.assertEqual(s['active_jobs'],[])
        self.assertEqual(self.request('GET','/../config.json')[0],404)

    def test_research_update_keeps_prior_success_after_new_failure(self):
        store=Store(self.data)
        try:
            with store.db:
                for key,symbol,at,status in [('ok','sh600519','2026-09-18T00:00:00+00:00','SUCCEEDED'),('failed','sh600519','2026-09-19T00:00:00+00:00','DEFERRED')]:
                    store.db.execute('INSERT INTO snapshots VALUES(?,NULL,?,?,?,?,?)',(key,symbol,at,at,key,'{}'))
                    store.db.execute('INSERT INTO studies VALUES(?,?,?,?,?,?)',(key,key,symbol,at,status,'{}'))
            code,raw,_=self.request('GET','/api/status')
            self.assertEqual(code,200)
            rows={w['symbol']:w for w in json.loads(raw)['watchlist']}
            self.assertEqual(rows['sh600519']['last_research_at'],'2026-09-18T00:00:00+00:00')
            self.assertEqual(rows['sh600519']['latest_study']['model_status'],'DEFERRED')
            self.assertIsNone(rows['sz000333']['last_research_at'])
        finally:store.close()

    def test_overview_keeps_home_data_and_stock_retains_full_evidence(self):
        from test_config import load_config
        from ashare.demo import seed,research_model,SYMBOL
        from ashare.research import study
        from ashare.stock_detail import detail
        cfg=load_config(self.path);cfg['watchlist']=[{'symbol':SYMBOL,'name':'测试股票'}]
        self.path.write_text(json.dumps(cfg))
        store=Store(self.data)
        try:
            packet=seed(store,cfg)
            study(store,cfg,packet,model_fn=research_model(packet),at='2026-09-15T09:01:00+08:00')
            review={'facts':{'statistics':{'decision_count':0,'fill_count':0,'realized_pnl_cents':0},'source_records':['large evidence']},
                    'analysis':{'summary':'暂无成交','lessons':[]}}
            with store.db:store.db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)',('r','start','end',1,now(),'fp','SUCCEEDED',json.dumps(review)))
        finally:store.close()
        full=status(cfg);code,raw,_=self.request('GET','/api/status');self.assertEqual(code,200)
        compact=json.loads(raw);a=full['watchlist'][0];b=compact['watchlist'][0]
        for key in ('payload','trade_guidance','effective_status','activated_at'):self.assertEqual(a['plan'][key],b['plan'][key])
        for key in ('overview','next_checks'):self.assertEqual(a['report'][key],b['report'][key])
        self.assertNotIn('research',b['plan']);self.assertNotIn('background_events',b['plan'])
        self.assertNotIn('source_records',compact['reviews'][0]['payload']['facts'])
        self.assertEqual(compact['reviews'][0]['payload']['analysis'],review['analysis'])
        self.assertEqual(detail(cfg,SYMBOL)['stock']['plan']['research'],a['plan']['research'])

    def test_closed_connection_does_not_attempt_second_http_response(self):
        for error in (BrokenPipeError,ConnectionResetError):
            class ClosedSocket(MemorySocket):
                sends=0
                def sendall(self,data):
                    self.sends+=1
                    if self.sends==2:raise error('browser stopped reading')
                    super().sendall(data)
            sock=ClosedSocket(b'GET /health HTTP/1.0\r\nHost: 127.0.0.1:8765\r\n\r\n')
            self.handler(sock,('127.0.0.1',1234),None)
            self.assertEqual(sock.sends,2)

    def test_existing_database_gets_ordered_document_lookup(self):
        store=Store(self.data)
        try:
            store.db.execute('DROP INDEX chunks_document_order');store.db.commit()
        finally:store.close()
        store=Store(self.data)
        try:
            plan=' '.join(r[3] for r in store.db.execute("EXPLAIN QUERY PLAN SELECT text FROM chunks WHERE doc_id=? ORDER BY ordinal",('one',)))
            self.assertIn('SEARCH chunks USING INDEX chunks_document_order',plan)
            self.assertNotIn('TEMP B-TREE',plan)
        finally:store.close()

    def test_home_has_latest_stock_decision_and_actual_blockers(self):
        store=Store(self.data);at=now()
        try:
            with store.db:
                store.db.execute('INSERT INTO slots VALUES(?,?,?,?,?,?,?)',('s',at,at,at,'SUCCEEDED','{}','NOT_NEEDED'))
                store.db.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?)',('d','s','sh600519',None,at,'HOLD','BLOCKED','条件未满足',json.dumps({'input_buy_blockers':['STALE_QUOTE','EVENT_SOURCE_UNAVAILABLE'],'quote':{'raw_path':'not-for-overview'}})))
            code,raw,_=self.request('GET','/api/status');self.assertEqual(code,200)
            rows={w['symbol']:w for w in json.loads(raw)['watchlist']}
            last=rows['sh600519']['last_decision']
            self.assertEqual(last['buy_blockers'],['STALE_QUOTE','EVENT_SOURCE_UNAVAILABLE'])
            self.assertNotIn('payload_json',last);self.assertIsNone(rows['sz000333']['last_decision'])
            self.assertEqual(json.loads(raw)['decisions'],[])
            with store.db:
                store.db.execute('INSERT INTO latest_trade_checks VALUES(?,?,?,?,?,?)',('sz000333',at,'BUY','BLOCKED','EXISTING_OPEN_ORDER','{}'))
            code,raw,_=self.request('GET','/api/status')
            current=next(w for w in json.loads(raw)['watchlist'] if w['symbol']=='sz000333')
            self.assertEqual(current['last_decision']['reason'],'EXISTING_OPEN_ORDER')
        finally:store.close()

    def test_admin_sees_and_answers_notices_guest_does_not(self):
        # Dean answers notices on the page; guests never see them and cannot answer.
        from ashare import notices
        store=Store(self.data)
        n=notices.create(store,title='购买稳定行情源',body='两周内持仓有 35 分钟没有报价，建议购买付费行情源。',kind='DECISION',author='claude')
        guest_token,guest=auth.login(store,'guest','test-guest-password','test-guest');store.close()
        code,raw,_=self.request('GET','/api/status')
        self.assertEqual([x['id'] for x in json.loads(raw)['notices']],[n['id']])
        code,raw,_=self.request('GET','/api/status',headers={'Cookie':auth.COOKIE+'='+guest_token})
        self.assertEqual((code,json.loads(raw)['notices']),(200,[]))
        code,raw,_=self.request('POST','/api/notices/decide',{'id':n['id'],'action':'APPROVE'},
            {'X-CSRF-Token':guest['csrf_token'],'Origin':'http://127.0.0.1:8765','Cookie':auth.COOKIE+'='+guest_token})
        self.assertEqual(code,403)
        code,raw,_=self.request('POST','/api/notices/decide',{'id':n['id'],'action':'VETO'},{'X-CSRF-Token':self.csrf,'Origin':'http://127.0.0.1:8765'})
        self.assertEqual(code,400)
        code,raw,_=self.request('POST','/api/notices/decide',{'id':n['id'],'action':'APPROVE'},{'X-CSRF-Token':self.csrf,'Origin':'http://127.0.0.1:8765'})
        self.assertEqual((code,json.loads(raw)['status']),(200,'APPROVED'))
        code,raw,_=self.request('GET','/api/status')
        self.assertEqual(json.loads(raw)['notices'],[])


if __name__=='__main__':unittest.main()
