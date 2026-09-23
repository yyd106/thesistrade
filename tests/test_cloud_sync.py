import copy,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timedelta
import test_portfolio_strategy as fixtures
from ashare.storage import Store,normalize_time,now
from ashare import cloud_protocol as protocol,cloud_runtime as runtime,cloud_ledger as ledger,cloud_sync as sync,portfolio_strategy as ps,global_paper

class CloudSyncTests(unittest.TestCase):
    quote=fixtures.PortfolioStrategyTests.quote
    plan=fixtures.PortfolioStrategyTests.plan
    later=fixtures.PortfolioStrategyTests.later
    output=fixtures.PortfolioStrategyTests.output
    def setUp(self):
        fixtures.PortfolioStrategyTests.setUp(self)
        self.cloud_tmp=tempfile.TemporaryDirectory();self.cloud=Store(self.cloud_tmp.name)
        self.cfg.update(deployment_role='research',portfolio_authorization_hours=12)
        self.cfg['watchlist']=[{'symbol':'sh600519','name':'测试股'}]
        self.cloud_cfg={**self.cfg,'deployment_role':'cloud','data_dir':self.cloud_tmp.name}
        self.cfg['sync_key_file']=str(Path(self.tmp.name)/'private-key.json')
        keys=protocol.create_keys(self.cfg['sync_key_file'])
        self.cloud_cfg.update(sync_public_key=keys['public_key'],sync_key_id=keys['key_id'])
        self.quote();self.plan()
        self.transact(lambda:ledger.import_bootstrap(self.cloud,self.cloud_cfg,ledger.bootstrap_packet(self.store,self.cfg),self.at))
        with self.cloud.db:runtime.put(self.cloud,'execution_enabled',True)
        with self.store.db:runtime.put(self.store,'remote_ledger_version',ledger.version(self.cloud))
    def tearDown(self):
        self.cloud.close();self.cloud_tmp.cleanup();fixtures.PortfolioStrategyTests.tearDown(self)
    def transact(self,fn):
        self.cloud.db.execute('BEGIN IMMEDIATE')
        try:r=fn();self.cloud.db.commit();return r
        except BaseException:self.cloud.db.rollback();raise
    def publication(self,at=None,overrides=None):
        at=at or self.at
        result=ps.run(self.store,self.cfg,at,model_fn=lambda p,*a:self.output(json.loads(p.split('<UNTRUSTED_INPUT>')[1].split('</UNTRUSTED_INPUT>')[0]),overrides),clock=lambda:at)
        self.assertEqual(result['status'],'SUCCEEDED',result)
        with patch('ashare.cloud_sync.display_packet',return_value={}),patch('ashare.cloud_sync.now',return_value=at):sync.queue_publication(self.store,self.cfg,result['decision_id'])
        return json.loads(self.store.db.execute('SELECT payload_json FROM cloud_outbox WHERE id=?',(result['decision_id'],)).fetchone()[0])
    def receive(self,body,at=None):return self.transact(lambda:sync.receive_strategy(self.cloud,self.cloud_cfg,body,at or self.at))
    def test_signed_body_identity_path_time_and_tampering(self):
        raw=protocol.packed({'sample':1});h=protocol.signed_headers(self.cfg,'/api/sync/strategy',raw,self.at)
        self.assertEqual(protocol.verify(self.cloud_cfg,'/api/sync/strategy',raw,h,self.at),h['X-Research-Nonce'])
        for path,body,headers,at in [('/api/sync/activate',raw,h,self.at),('/api/sync/strategy',raw+b'x',h,self.at),('/api/sync/strategy',raw,{**h,'X-Research-Key':'other'},self.at),('/api/sync/strategy',raw,h,self.later(301))]:
            with self.assertRaises(ValueError):protocol.verify(self.cloud_cfg,path,body,headers,at)
        self.assertNotIn('private_key',self.cloud_cfg)
        self.assertEqual(Path(self.cfg['sync_key_file']).stat().st_mode&0o777,0o600)
    def test_bootstrap_once_cash_and_positions_preserved(self):
        self.assertEqual(ledger.version(self.cloud),ledger.version(self.store))
        with self.assertRaises(ValueError):self.transact(lambda:ledger.import_bootstrap(self.cloud,self.cloud_cfg,ledger.bootstrap_packet(self.store,self.cfg),self.at))
    def test_publication_retry_never_renews_completion_lease(self):
        body=self.publication();self.assertEqual(self.receive(body)['status'],'ACCEPTED')
        self.assertTrue(runtime.lease(self.cloud,self.cloud_cfg,self.later(43199))['active'])
        result=self.receive(body,self.later(43201));self.assertEqual(result['status'],'ALREADY_RECEIVED')
        self.assertFalse(runtime.lease(self.cloud,self.cloud_cfg,self.later(43201))['active'])
        self.assertEqual(runtime.value(self.cloud,'research_received_at'),self.at)
    def test_old_conflicting_future_and_expired_publications_rejected_atomically(self):
        body=self.publication();self.receive(body)
        different=copy.deepcopy(body);different['display']={'changed':True}
        with self.assertRaises(ValueError):self.receive(different)
        before=runtime.value(self.cloud,'research_completed_at')
        for completed in (self.later(-1),self.later(1)):
            bad=copy.deepcopy(body);bad.update(sequence=2,bundle_id='new',completed_at=completed);bad['decision'].update(id='new',created_at=completed)
            with self.assertRaises(ValueError):self.receive(bad)
        self.assertEqual(runtime.value(self.cloud,'research_completed_at'),before)
    def test_cloud_ledger_changed_rejects_stale_portfolio_without_heartbeat(self):
        body=self.publication()
        with self.cloud.db:self.cloud.db.execute('UPDATE paper_accounts SET cash_cents=cash_cents-100')
        with self.assertRaisesRegex(ValueError,'LEDGER_CHANGED'):self.receive(body)
        self.assertIsNone(runtime.value(self.cloud,'research_completed_at'))
        self.assertEqual(self.cloud.db.execute('SELECT count(*) FROM cloud_contracts').fetchone()[0],0)
    def test_local_execution_prohibited_even_with_valid_plan(self):
        q=self.quote();p,i=self.plan('ETH')
        with self.assertRaisesRegex(ValueError,'LOCAL_RESEARCH_ONLY'):global_paper.submit(self.store,self.cfg,'BTC','BUY',q,p,i,self.at)
        with self.assertRaisesRegex(ValueError,'LOCAL_RESEARCH_ONLY'):global_paper.settle(self.store,self.cfg,self.at,{})
        for command in ('slot','settle','global_slot','dynamic_slot'):
            with self.assertRaises(ValueError):runtime.assert_command(self.cfg,command)
        for command in ('research','cycle','review','portfolio_strategy'):
            with self.assertRaises(ValueError):runtime.assert_command(self.cloud_cfg,command)
    def test_cloud_execution_fill_mirrors_once_and_review_uses_cloud_costs(self):
        body=self.publication(overrides={'global:BTC':{'action':'ALLOW','target_bps':100}});self.receive(body)
        from ashare.global_market import latest,targets
        from ashare.global_research import active_plan
        q=latest(self.cloud,'BTC',self.at);p=active_plan(self.cloud,'BTC',self.at);items=targets(self.cloud,self.cloud_cfg,self.at)
        o=global_paper.submit(self.cloud,self.cloud_cfg,'BTC','BUY',q,p,items['BTC'],self.at)
        self.assertEqual(o.get('status'),'OPEN',o)
        # Quote at the order time cannot be filled; a later independently received quote can.
        self.assertEqual(global_paper.settle(self.cloud,self.cloud_cfg,self.at,items),[])
        original=self.store;self.store=self.cloud;self.quote(at=self.later(60));self.store=original
        self.assertEqual(len(global_paper.settle(self.cloud,self.cloud_cfg,self.later(60),items)),1)
        packet=self.transact(lambda:ledger.export_ledger(self.cloud,{},self.later(60),limit=1))
        while True:
            ledger.import_ledger(self.store,self.cfg,packet)
            if not packet['more']:break
            packet=self.transact(lambda:ledger.export_ledger(self.cloud,packet['cursors'],self.later(60),limit=1))
        ledger.import_ledger(self.store,self.cfg,packet)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM global_fills').fetchone()[0],1)
        self.assertEqual(ledger.version(self.store),ledger.version(self.cloud))
        from ashare.review_portfolio import build
        review=build(self.store,self.cfg,self.later(-1),self.later(120),self.later(120))
        self.assertEqual(review['positions'][0]['symbol'],'BTC')
        self.assertGreater(review['positions'][0]['closing']['cost_cents'],0)
    def test_expiry_cancels_open_buy_keeps_unknown_and_allows_risk_sell(self):
        body=self.publication(overrides={'global:BTC':{'action':'ALLOW','target_bps':100}});self.receive(body)
        from ashare.global_market import latest,targets
        from ashare.global_research import active_plan
        q=latest(self.cloud,'BTC',self.at);p=active_plan(self.cloud,'BTC',self.at);i=targets(self.cloud,self.cloud_cfg,self.at)['BTC']
        o=global_paper.submit(self.cloud,self.cloud_cfg,'BTC','BUY',q,p,i,self.at)
        self.assertEqual(o.get('status'),'OPEN',o)
        self.assertEqual(runtime.fence(self.cloud,self.cloud_cfg,self.later(43200)),1)
        runtime.execution_allowed(self.cloud,self.cloud_cfg,self.later(43200),'SELL')
        with self.assertRaises(ValueError):runtime.execution_allowed(self.cloud,self.cloud_cfg,self.later(43200),'BUY')
        with self.cloud.db:self.cloud.db.execute("UPDATE global_orders SET status='UNKNOWN',reserved_cents=100 WHERE id=?",(o['id'],))
        self.assertEqual(runtime.fence(self.cloud,self.cloud_cfg,self.later(43201)),0)
        self.assertEqual(self.cloud.db.execute('SELECT reserved_cents FROM global_orders').fetchone()[0],100)
    def test_new_evidence_revokes_entry_without_renewing_heartbeat(self):
        body=self.publication(overrides={'global:BTC':{'action':'ALLOW','target_bps':100}});self.receive(body)
        self.assertIsNotNone(ps.decision(self.cloud,self.cloud_cfg,'global','BTC',self.at))
        self.transact(lambda:sync.handle(self.cloud,self.cloud_cfg,'/api/sync/invalidate',{'changed_at':self.later(5)},self.later(5)))
        self.assertIsNone(ps.decision(self.cloud,self.cloud_cfg,'global','BTC',self.later(5)))
        self.assertEqual(runtime.value(self.cloud,'research_completed_at'),self.at)
    def test_sync_does_not_refresh_strategy_timestamp_on_retry(self):
        body=self.publication();self.receive(body);self.receive(body,self.later(100))
        from ashare.cloud_dashboard import add_strategy_times
        r=add_strategy_times(self.cloud,{'watchlist':[{'symbol':'sh600519'}],'observation':{'items':[{'asset':'BTC'}]}})
        self.assertEqual(r['watchlist'][0]['last_strategy_updated_at'],self.at)
        self.assertEqual(r['observation']['items'][0]['last_strategy_updated_at'],self.at)
    def test_strategy_over_cap_and_unauthorized_source_never_replace_active(self):
        body=self.publication();self.receive(body)
        bad=copy.deepcopy(body);bad.update(sequence=2,bundle_id='bad',completed_at=self.later(5));bad['decision'].update(id='bad',created_at=self.later(5))
        p=json.loads(bad['decision']['payload_json']);d=next(d for d in p['decisions'] if d['key']=='global:BTC');d.update(action='ALLOW',target_bps=501,can_increase=True,max_bps=10000)
        bad['decision']['payload_json']=json.dumps(p)
        with self.assertRaises(ValueError):self.receive(bad,self.later(5))
        self.assertEqual(runtime.value(self.cloud,'last_sequence'),1)

class CloudHTTPTests(unittest.TestCase):
    def setUp(self):
        import test_dashboard as fixture
        fixture.DashboardTests.setUp(self)
        self.cfg=json.loads(self.path.read_text());self.cfg.update(deployment_role='cloud',public_origin='https://trade.example')
        key=Path(self.tmp.name)/'signer.json';keys=protocol.create_keys(key)
        self.signer={'sync_key_file':str(key)}
        self.cfg.update(sync_key_id=keys['key_id'],sync_public_key=keys['public_key'])
        self.path.write_text(json.dumps(self.cfg))
        from ashare.dashboard import make_handler
        self.handler=make_handler(self.path,'browser-csrf',8765)
    def tearDown(self):self.tmp.cleanup()
    def call(self,path,body,headers=None,*,sign=True,method='POST'):
        import test_dashboard as fixture
        raw=protocol.packed(body) if sign else json.dumps(body).encode()
        h={'Host':'trade.example','Content-Length':str(len(raw)),'Cookie':self.cookie}
        if sign:h.update(protocol.signed_headers(self.signer,path,raw))
        h.update(headers or {})
        req=(method+' '+path+' HTTP/1.0\r\n'+''.join(k+': '+v+'\r\n' for k,v in h.items())+'\r\n').encode()+raw
        sock=fixture.MemorySocket(req);sock.settimeout=lambda _:None
        self.handler(sock,('127.0.0.1',1234),None)
        head,payload=sock.response.getvalue().split(b'\r\n\r\n',1)
        return int(head.split()[1]),payload,head,h
    def test_browser_session_cannot_publish_or_enqueue_model(self):
        code,*_=self.call('/api/sync/strategy',{},sign=False,headers={'X-CSRF-Token':self.csrf,'Origin':'https://trade.example'})
        self.assertEqual(code,403)
        for kind in ('portfolio_strategy','cycle','review','global_research'):
            code,*_=self.call('/api/run',{'kind':kind},sign=False,headers={'X-CSRF-Token':self.csrf,'Origin':'https://trade.example'})
            self.assertEqual(code,400)
    def test_successful_signature_nonce_cannot_be_replayed(self):
        code,raw,_,headers=self.call('/api/sync/reviews',{})
        self.assertEqual(code,200,raw)
        self.assertEqual(protocol.unpack(raw)['status'],'ACCEPTED')
        self.assertEqual(self.call('/api/sync/reviews',{},headers)[0],403)
    def test_https_cookie_and_origin_checks(self):
        code,_,head,_=self.call('/api/login',{'username':'admin','password':'test-admin-password'},sign=False,headers={'X-CSRF-Token':self.csrf,'Origin':'https://trade.example'})
        self.assertEqual(code,200);self.assertIn(b'; Secure',head)
        self.assertEqual(self.call('/api/login',{},sign=False,headers={'X-CSRF-Token':self.csrf,'Origin':'https://evil.example'})[0],403)
    def test_cloud_setup_and_web_settings_rejected(self):
        h={'X-CSRF-Token':self.csrf,'Origin':'https://trade.example'}
        self.assertEqual(self.call('/api/setup',{'admin_password':'other-password-1','guest_password':'other-password-2'},h,sign=False)[0],403)
        self.assertEqual(self.call('/api/settings',{'watchlist':[]},h,sign=False)[0],403)
    def test_public_https_entry_does_not_expose_portfolio_or_bootstrap(self):
        h={'Cookie':''}
        for path in ('/api/status','/api/stock?symbol=sh600519','/api/observations','/api/document?id=test','/api/snapshot?id=test','/api/job?id=test','/api/settings'):
            code,raw,_,_=self.call(path,{},headers=h,sign=False,method='GET')
            self.assertEqual(code,401,(path,raw))
            self.assertEqual(json.loads(raw)['code'],'AUTH_REQUIRED')
        self.assertEqual(self.call('/',{},headers=h,sign=False,method='GET')[0],302)
        self.assertEqual(self.call('/api/sync/bootstrap',{},headers=h,sign=False)[0],403)
        code,raw,_,_=self.call('/health',{},headers=h,sign=False,method='GET')
        self.assertEqual(code,200)
        self.assertEqual(set(json.loads(raw)),{'ok','version','role'})

class SplitScheduleTests(unittest.TestCase):
    def test_sleep_catchup_and_role_separation(self):
        from test_config import load_config
        from ashare.scheduler import schedule_due,schedule_dynamic_due
        cfg=load_config(Path(__file__).resolve().parents[1]/'config.json')
        cfg.update(investment_policy='days_cash_v1',collection_times=['02:00','08:00','14:00','20:00'],slot_times=['09:30','10:00'],dynamic_enabled=True)
        for role in ('research','cloud'):
            with tempfile.TemporaryDirectory() as path:
                store=Store(path);c={**cfg,'deployment_role':role}
                with store.db:store.db.execute("INSERT INTO service_state VALUES('last_scan',?)",(normalize_time('2026-09-20T00:00:00+08:00'),))
                at=normalize_time('2026-09-23T10:00:01+08:00');schedule_due(store,c,at);schedule_dynamic_due(store,c,at)
                pending=[dict(r) for r in store.db.execute("SELECT * FROM jobs WHERE status='PENDING'")]
                if role=='research':
                    self.assertFalse(any(r['kind'] in ('slot','global_slot','dynamic_slot') for r in pending))
                    cycles=[r for r in pending if r['kind']=='cycle'];self.assertEqual(len(cycles),1)
                    self.assertEqual(cycles[0]['scheduled_at'],normalize_time('2026-09-23T08:00:00+08:00'))
                else:self.assertTrue(all(r['kind'] in ('slot','dynamic_slot') for r in pending))
                store.close()

    def test_reconnected_retries_latest_research_and_review_only_once(self):
        from test_config import load_config
        from ashare.scheduler import schedule_reconnected
        cfg=load_config(Path(__file__).resolve().parents[1]/'config.json')
        cfg.update(deployment_role='research',scheduler_enabled=True,dynamic_enabled=True,collection_times=['02:00','08:00','14:00','20:00'])
        with tempfile.TemporaryDirectory() as path:
            store=Store(path);at=normalize_time('2026-09-23T16:12:00+08:00')
            with store.db:runtime.put(store,'reconnect_pending',at)
            schedule_reconnected(store,cfg,at);schedule_reconnected(store,cfg,at)
            jobs=[dict(r) for r in store.db.execute('SELECT * FROM jobs')]
            self.assertEqual({r['kind'] for r in jobs},{'cycle','global_research','dynamic_cycle','review'})
            self.assertEqual(len(jobs),4)
            self.assertEqual(next(r for r in jobs if r['kind']=='cycle')['scheduled_at'],normalize_time('2026-09-23T14:00:00+08:00'))
            store.close()

class LedgerVersionTests(unittest.TestCase):
    def test_research_waits_for_background_ledger_sync_lock(self):
        from ashare.workflow import task_lock
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        entered=Event();waiting=Event()
        with tempfile.TemporaryDirectory() as root:
            def research():
                waiting.set()
                with task_lock(Path(root),'cloud-sync',wait_seconds=2):entered.set()
            with ThreadPoolExecutor(max_workers=1) as pool:
                with task_lock(Path(root),'cloud-sync'):
                    future=pool.submit(research);self.assertTrue(waiting.wait(1))
                    self.assertFalse(entered.wait(.05))
                    with self.assertRaisesRegex(RuntimeError,'BUSY'):
                        with task_lock(Path(root),'cloud-sync'):pass
                future.result(timeout=2)
                self.assertTrue(entered.is_set())

    def test_price_mark_refresh_does_not_discard_every_portfolio_inference(self):
        from ashare.finance import PaperLedger
        from ashare.portfolio_risk import state
        with tempfile.TemporaryDirectory() as root:
            store=Store(root);PaperLedger(store).initialize();before=ledger.version(store)
            risk=state(store)
            with store.db:store.db.execute('INSERT INTO portfolio_risk VALUES(?,?,?)',('DEMO_PAPER',now(),json.dumps({**risk,'drawdown_bps':25,'high_water':11000000})))
            self.assertEqual(ledger.version(store),before)
            # Risk latch changes do invalidate a proposal based on an earlier account.
            with store.db:store.db.execute('UPDATE portfolio_risk SET payload_json=?',(json.dumps({**risk,'halted':True}),))
            self.assertNotEqual(ledger.version(store),before)
            store.close()
