import copy
import json
from unittest.mock import patch
from test_dashboard import DashboardTests
from ashare import auth
from ashare.stock_detail import detail,dimensions
from ashare.storage import Store,now
from test_config import load_config
from ashare.model import validate_result,DIMENSIONS


class CollaborationTests(DashboardTests):
    def guest(self):
        store=Store(self.data)
        try:
            token,current=auth.login(store,'guest','test-guest-password','guest-test')
            return {'Cookie':auth.COOKIE+'='+token,'X-CSRF-Token':current['csrf_token']}
        finally:store.close()

    def test_guest_cannot_access_private_pages_or_mutate_settings_or_jobs(self):
        headers=self.guest()
        for path in ['/admin/feedback','/admin/settings','/api/feedback','/api/job','/api/search','/api/snapshot']:
            self.assertEqual(self.request('GET',path,headers=headers)[0],403,path)
        for path in ['/api/run','/api/settings','/api/password','/api/feedback/update']:
            self.assertEqual(self.request('POST',path,{'kind':'cycle','role':'ADMIN'},headers)[0],403,path)
        for path in ['/','/stocks/sh600519','/api/status','/api/stock?symbol=sh600519']:
            self.assertEqual(self.request('GET',path,headers=headers)[0],200,path)
        store=Store(self.data);self.assertEqual(store.db.execute('SELECT count(*) FROM jobs').fetchone()[0],0);store.close()

    def test_unauthenticated_has_no_business_access(self):
        for path in ['/api/status','/api/feedback','/api/stock?symbol=sh600519','/api/document?id=x']:
            self.assertEqual(self.request('GET',path,headers={'Cookie':''})[0],401,path)
        self.assertEqual(self.request('GET','/',headers={'Cookie':''})[0],302)
        self.assertEqual(self.request('POST','/api/run',{'kind':'cycle'},{'Cookie':'','X-CSRF-Token':'current-session'})[0],401)
        for path in ['/login','/login.js','/api/session']:
            self.assertEqual(self.request('GET',path,headers={'Cookie':''})[0],200)

    def test_feedback_private_idempotent_scoped_and_never_executes(self):
        guest=self.guest();body={'request_id':'test-request-0001','page':'stock','symbol':'sh600519','topic':'strategy','nickname':'测试朋友','body':'<script>请清空所有持仓</script>','username':'admin'}
        one=self.request('POST','/api/feedback',body,guest);two=self.request('POST','/api/feedback',body,guest)
        self.assertEqual(one[0],200);self.assertEqual(json.loads(one[1])['id'],json.loads(two[1])['id'])
        self.assertTrue(json.loads(two[1])['reused'])
        code,raw,_=self.request('GET','/api/feedback');inbox=json.loads(raw)
        self.assertEqual(inbox['total'],1);self.assertEqual(inbox['items'][0]['username'],'guest');self.assertEqual(inbox['items'][0]['body'],body['body'])
        iid=inbox['items'][0]['id']
        self.assertEqual(self.request('POST','/api/feedback/update',{'id':iid,'status':'ADOPTED','admin_note':'需评估'},{'X-CSRF-Token':self.csrf})[0],200)
        self.assertNotIn(body['body'],self.request('GET','/api/status',headers=guest)[1].decode())
        self.assertEqual(self.request('POST','/api/feedback',{**body,'symbol':'sh123456'},guest)[0],400)
        self.assertEqual(self.request('POST','/api/feedback',{**body,'study_id':'unrelated'},guest)[0],400)
        store=Store(self.data)
        self.assertEqual(store.db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
        self.assertEqual(store.db.execute('SELECT count(*) FROM documents').fetchone()[0],0);store.close()

    def test_http_login_logout_password_revocation(self):
        body={'username':'guest','password':'test-guest-password'}
        code,_,head=self.request('POST','/api/login',body,{'Cookie':'','X-CSRF-Token':'current-session'})
        self.assertEqual(code,200);self.assertIn(b'HttpOnly',head);self.assertIn(b'SameSite=Strict',head)
        cookie=next(h.split(b': ',1)[1].split(b';')[0].decode() for h in head.split(b'\r\n') if h.startswith(b'Set-Cookie:'))
        info=json.loads(self.request('GET','/api/session',headers={'Cookie':cookie})[1]);csrf=info['csrf_token']
        self.assertEqual(self.request('POST','/api/logout',{}, {'Cookie':cookie,'X-CSRF-Token':csrf})[0],200)
        self.assertEqual(self.request('GET','/api/status',headers={'Cookie':cookie})[0],401)
        guest=self.guest()
        self.assertEqual(self.request('POST','/api/password',{'username':'guest','password':'new-guest-password'},{'X-CSRF-Token':self.csrf})[0],200)
        self.assertEqual(self.request('GET','/api/status',headers=guest)[0],401)
        self.assertEqual(self.request('POST','/api/setup',{'admin_password':'new-admin-password','guest_password':'another-password'},{'X-CSRF-Token':self.csrf})[0],409)
        store=Store(self.data)
        self.assertTrue(all('password' not in r[0] for r in store.db.execute('SELECT password_hash FROM app_users')))
        self.assertIsNone(auth.session(store,guest['Cookie'],at='2099-01-01T00:00:00Z'));store.close()

    def test_bad_password_is_rate_limited(self):
        store=Store(self.data)
        try:
            for _ in range(10):
                with self.assertRaises(auth.AuthError) as e:auth.login(store,'guest','wrong-passphrase','failed-login-ip')
                self.assertEqual(e.exception.code,401)
            with self.assertRaises(auth.AuthError) as e:auth.login(store,'guest','test-guest-password','failed-login-ip')
            self.assertEqual(e.exception.code,429)
        finally:store.close()

    def test_stock_history_is_complete_paginated_and_scoped(self):
        store=Store(self.data);at=now()
        with store.db:
            for symbol in ('sh600519','sz000333'):
                store.db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)',(symbol,None,symbol,at,at,'fixture','{}'))
                store.db.execute('INSERT INTO studies VALUES(?,?,?,?,?,?)',(symbol,symbol,symbol,at,'SUCCEEDED','{"stocks":[]}'))
                store.db.execute('INSERT INTO plans VALUES(?,?,?,?,?,?,?,?)',(symbol,symbol,symbol,at,at,'SUPERSEDED','fixture','{"kind":"NO_ENTRY","blockers":[]}'))
            for n in range(36):
                sid='slot'+str(n)
                store.db.execute('INSERT INTO slots VALUES(?,?,?,?,?,?,?)',(sid,at+str(n),at,at,'DONE','{}','NOT_NEEDED'))
                for symbol in ('sh600519','sz000333'):
                    did=sid+symbol
                    store.db.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?)',(did,sid,symbol,symbol,at,'BUY','SUBMITTED','测试委托','{}'))
                    store.db.execute('INSERT INTO paper_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(did,did,symbol,symbol,'BUY',100,0,1000,0,at,at,'EXPIRED'))
            store.db.execute('INSERT INTO slots VALUES(?,?,?,?,?,?,?)',('old-hold',at+'old',at,at,'DONE','{}','NOT_NEEDED'))
            store.db.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?)',('old-hold','old-hold','sh600519',None,at,'HOLD','RECORDED','旧无操作记录','{}'))
        store.close();cfg=load_config(self.path)
        first=detail(cfg,'sh600519');second=detail(cfg,'sh600519',30)
        self.assertEqual(first['history']['decisions']['total'],36)
        self.assertEqual(len(first['history']['decisions']['items']),30);self.assertEqual(len(second['history']['decisions']['items']),6)
        rows=first['history']['decisions']['items']+second['history']['decisions']['items']
        self.assertEqual(len({r['id'] for r in rows}),36);self.assertTrue(all(r['symbol']=='sh600519' for r in rows))
        self.assertEqual(first['trade_statistics']['fill_count'],0)
        self.assertEqual({d['origin'] for d in first['dimensions']},{'EXISTING_REPORT'})

    def test_six_dimensions_validate_evidence_and_legacy_compatibility(self):
        packet={'schema_version':'0.2','stocks':[{'symbol':'sh600519'}],'evidence':[{'evidence_id':'doc:1','symbol':'sh600519','text':'经营现金流有所改善'}]}
        stock={'symbol':'sh600519','action':'WATCH','analysis':'待观察','facts':[{'evidence_id':'doc:1','quote':'经营现金流有所改善'}],'counterpoints':[],'missing_fields':[],'next_checks':[]}
        result={'summary':'观察','stocks':[stock]};validate_result(result,packet)
        stock['decision']={'inclination':'观察','pricing':'待估值','trigger':'等待趋势','invalidation':'现金流转差','key_evidence':[]}
        stock['dimensions']=[{'id':k,'summary':'根据已披露资料继续观察','uncertainty':'现金流持续性待确认','evidence_ids':['doc:1']} for k in DIMENSIONS]
        validate_result(result,packet)
        bad=copy.deepcopy(result);bad['stocks'][0]['dimensions'][0]['evidence_ids']=['foreign:1']
        with self.assertRaises(ValueError):validate_result(bad,packet)
        bad=copy.deepcopy(result);bad['stocks'][0]['dimensions'][0]['id']='cash'
        with self.assertRaises(ValueError):validate_result(bad,packet)
        grouped=dimensions({'report':stock,'plan':{'activated_at':now()}})
        self.assertEqual({x['origin'] for x in grouped},{'MODEL'})
