import copy,json,tempfile,time,unittest
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
from ashare import industry,universe,industry_research
from ashare.storage import Store,normalize_time
from ashare.pipeline import load_config

class IndustryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.at=normalize_time('2026-09-25T12:00:00+08:00')
        self.cfg=load_config(Path(__file__).resolve().parents[1]/'config.example.json')
        self.cfg.update(data_dir=self.tmp.name,industry_enabled=True,investment_policy='days_cash_v1',watchlist=[{'symbol':'sh600519','name':'贵州茅台'}])
        self.identities={s:{'asset':s,'symbol':s,'name':n,'category':'CN','kind':'STOCK'} for s,n in [('sh600519','贵州茅台'),('sz300499','高澜股份')]}
        self.text='终端客户新增订单，一级客户向高澜股份采购液冷产品。预算已经落实，采购招标启动。供需交期拉长，产能扩张尚需半年。'
        self.doc=self.document(self.text)
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def later(self,days):return normalize_time((datetime.fromisoformat(self.at)+timedelta(days=days)).isoformat())
    def document(self,text,at=None,url='https://example.org/report',published=None,quality='text',symbol='sz300499'):
        stamp=at or self.at
        return self.store.add_document(symbol=symbol,kind='company_report',title='液冷产品项目采购披露',source='company',url=url,published_at=published or stamp,first_seen_at=stamp,ready_at=stamp,pages=[(1,text)],raw_path=self.store.raw(text.encode(),'.txt'),quality=quality,cloud_allowed=True)[0]
    def fact(self,kind='MILESTONE',metric='BUDGET',**kw):
        return dict(kind=kind,entity='高澜股份',counterparty='',product='液冷产品',project='项目A',owner='客户A',lot='一标段',metric=metric,unit='元',period='2026年第四季度',value='100',effective_from='',effective_until='',claim_type='DISCLOSED',evidence_id=self.doc+':0',quote=self.text,**kw)
    def proposal(self,method='8',symbol='sz300499'):
        p={'symbol':symbol,'domain':'ai','method':method,'topic':'项目A液冷产品','thesis':'项目采购或形成新增业务','state':'ACTIVE','next_check':'核对合同及供货份额','invalidation':'项目取消或公司不供货','alternatives':'还存在两家供应商','profit_capture':'需要核实价格及成本','causal_chain':['客户采购启动','液冷供应商可能获益'],'counterpoints':['尚无收入确认'],'missing':['供货份额未知'],'facts':[self.fact(),self.fact(metric='TENDER')],'forecasts':[]}
        if method=='1':
            a=self.fact('RELATION');a.update(entity='终端客户',counterparty='一级客户')
            b=self.fact('RELATION');b.update(entity='一级客户',counterparty='高澜股份')
            p['facts']=[a,b,self.fact('EXPOSURE',metric='BUSINESS')]
        if method=='2':p['facts']=[self.fact('METRIC','DEMAND'),self.fact('METRIC','LEAD_TIME')]
        return p
    def save(self,p=None,at=None):return industry.save(self.store,p or self.proposal(),at or self.at,self.identities)
    def test_all_four_methods_admit_only_their_evidence_gates(self):
        for method in ('1','2','3','8'):
            with self.subTest(method=method):
                p=self.proposal(method);hid=self.save(p)
                h=next(h for h in industry.latest(self.store,self.at) if h['id']==hid)
                self.assertEqual(h['state'],'ACTIVE')
                p['facts']=p['facts'][:1];p['topic']+='不完整'
                bad=self.save(p);self.assertEqual(next(h for h in industry.latest(self.store,self.at) if h['id']==bad)['state'],'WAITING')
    def test_guidance_is_not_funding_or_delivered_revenue(self):
        p=self.proposal();p['facts'][0]['claim_type']='GUIDANCE';self.save(p)
        self.assertEqual(industry.latest(self.store,self.at)[0]['state'],'WAITING')
        self.assertFalse(universe.permit(self.store,self.cfg,'sz300499',self.at))
    def test_unrelated_projects_cannot_supply_each_others_funding(self):
        p=self.proposal();p['facts'][1]['project']='另一项目';self.save(p)
        self.assertEqual(industry.latest(self.store,self.at)[0]['state'],'WAITING')
    def test_missing_alternatives_prevents_bottleneck_admission(self):
        p=self.proposal('2');p['alternatives']='';self.save(p)
        self.assertEqual(industry.latest(self.store,self.at)[0]['state'],'WAITING')
    def test_non_watchlist_company_reaches_full_company_universe(self):
        self.save();targets=universe.company_targets(self.store,self.cfg,self.at)
        self.assertEqual({m['symbol'] for m in targets},{'sh600519','sz300499'})
        self.assertEqual(len(self.cfg['watchlist']),1)
        self.assertTrue(next(m for m in targets if m['symbol']=='sz300499')['buy_eligible'])
    def test_fixed_company_discovery_does_not_consume_another_seat(self):
        self.cfg['watchlist'].append({'symbol':'sz300499','name':'高澜股份'});self.save()
        rows=universe.membership(self.store,self.cfg,self.at)
        self.assertEqual(len(rows),2);self.assertEqual(next(m for m in rows if m['symbol']=='sz300499')['membership'],'CORE')
    def test_invalidating_one_hypothesis_preserves_other_hypotheses(self):
        self.save();p=self.proposal('3');self.save(p)
        p['state']='INVALIDATED';self.save(p,self.later(.1))
        self.assertTrue(universe.permit(self.store,self.cfg,'sz300499',self.later(.1)))
        p=self.proposal();p['state']='INVALIDATED';self.save(p,self.later(.2))
        self.assertFalse(universe.permit(self.store,self.cfg,'sz300499',self.later(.2)))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM industry_hypotheses').fetchone()[0],4)
    def test_expiry_is_not_extended_by_reanalysis(self):
        self.save();self.save(at=self.later(6))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM industry_hypotheses').fetchone()[0],1)
        self.assertFalse(universe.permit(self.store,self.cfg,'sz300499',self.later(7)))
        self.assertEqual(industry.state(self.store,industry.latest(self.store,self.later(31))[0],self.later(31)),'ARCHIVED')
    def test_business_expiry_shortens_signed_dynamic_authorization(self):
        p=self.proposal();p['facts'][0]['effective_until']=self.later(1);self.save(p)
        before=universe.publication(self.store,self.cfg,self.at,self.later(.5))
        self.assertEqual(next(m for m in before['members'] if m['symbol']=='sz300499')['review_at'],self.later(1))
        self.assertFalse(universe.permit(self.store,self.cfg,'sz300499',self.later(1)))
    def test_late_arrival_not_available_in_earlier_snapshot(self):
        self.doc=self.document(self.text+' 迟到资料',at=self.later(2),published=self.at,url='https://example.org/late')
        with self.assertRaisesRegex(ValueError,'当时可用'):self.save()
        self.save(at=self.later(2));self.assertEqual(industry.latest(self.store,self.at),[])
    def test_revision_invalidates_existing_hypothesis_without_deleting_history(self):
        self.save();self.document(self.text+' 公司否认供货',at=self.later(1))
        self.assertTrue(universe.permit(self.store,self.cfg,'sz300499',self.at))
        self.assertFalse(universe.permit(self.store,self.cfg,'sz300499',self.later(1)))
    def test_fabricated_quote_and_unknown_identity_are_rejected(self):
        p=self.proposal();p['facts'][0]['quote']='这是不存在的连续原文'
        with self.assertRaises(ValueError):self.save(p)
        p=self.proposal();p['symbol']='sz999999'
        with self.assertRaises(ValueError):self.save(p)
    def test_archived_unknown_orders_remain_protected_without_new_buys(self):
        self.save();self.store.db.execute('PRAGMA foreign_keys=OFF')
        with self.store.db:self.store.db.execute('INSERT INTO paper_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('o','d','p','sz300499','BUY',100,0,100,10000,self.at,self.later(2),'UNKNOWN'))
        rows=universe.company_targets(self.store,self.cfg,self.later(8));m=next(m for m in rows if m['symbol']=='sz300499')
        self.assertTrue(m['protected']);self.assertFalse(m['buy_eligible'])
        self.assertEqual(self.store.db.execute('SELECT status,reserved_cents FROM paper_orders').fetchone()[0],'UNKNOWN')
    def test_19_core_plus_four_fixed_leaves_17_shared_dynamic_seats(self):
        self.cfg['watchlist']=[{'symbol':'sh60'+str(i).zfill(4),'name':str(i)} for i in range(19)]
        for i in range(21):
            symbol='sz30'+str(i).zfill(4);self.identities[symbol]={'name':symbol,'category':'CN','kind':'STOCK'};self.doc=self.document(self.text,url='https://example.org/'+symbol,symbol=symbol);self.save(self.proposal(symbol=symbol))
        active=[m for m in universe.membership(self.store,self.cfg,self.at) if m['membership']=='DYNAMIC' and m['buy_eligible']]
        self.assertEqual(len(active),17)
    def test_membership_history_idempotent_and_withdrawal_is_versioned(self):
        self.save();universe.reconcile(self.store,self.cfg,self.at);count=self.store.db.execute('SELECT count(*) FROM industry_memberships').fetchone()[0]
        universe.reconcile(self.store,self.cfg,self.at);self.assertEqual(self.store.db.execute('SELECT count(*) FROM industry_memberships').fetchone()[0],count)
        universe.reconcile(self.store,self.cfg,self.later(8));self.assertGreater(self.store.db.execute('SELECT count(*) FROM industry_memberships').fetchone()[0],count)
    def test_operating_unknown_is_not_failure_and_numeric_scenarios_are_decimal(self):
        p=self.proposal();p['forecasts']=[{'metric':'ORDERS','baseline':'90','low':'100','high':'120','unit':'元','period':'2026年第四季度','due_at':self.later(1)}];self.save(p)
        self.assertEqual(industry.evaluate(self.store,self.later(2))[0]['status'],'UNKNOWN')
        self.assertEqual(industry.scenario({})['status'],'MISSING')
        result=industry.scenario({'incremental_units':['100','200'],'content_per_unit':['2','3'],'unit_price':['0.1','0.2'],'supplier_share':['0.25','0.5']})
        self.assertEqual((result['low'],result['high']),('5.000','60.00'))
    def test_discovery_resumes_completed_domains_and_checks_all_four_methods(self):
        with self.store.db:
            for s,v in self.identities.items():self.store.db.execute('INSERT INTO macro_instruments VALUES(?,?,?,?)',(s,'CN',self.at,json.dumps(v)))
        calls=[]
        def model(prompt,*args):
            packet=json.loads(prompt.split('<DATA>')[1].split('</DATA>')[0]);calls.append(packet['domain'])
            return {'hypotheses':[self.proposal()] if packet['domain']=='ai' else [],'method_coverage':{m:'已核查给定原文，资料不足不补造事实' for m in industry.METHODS}}
        r=industry_research.run(self.store,self.cfg,'testcycle',at=self.at,collect_fn=False,model_fn=model)
        self.assertEqual(r['status'],'DONE');before=len(calls)
        industry_research.run(self.store,self.cfg,'testcycle',at=self.at,collect_fn=False,model_fn=model)
        self.assertEqual(len(calls),before)
    def test_signed_membership_requires_version_identity_capacity_and_deadline(self):
        self.save();body=universe.publication(self.store,self.cfg,self.at,self.later(.5))
        universe.validate_publication(self.store,self.cfg,body,[],self.at,self.later(.5))
        for mutate in (lambda b:b.update(version='old'),lambda b:b.update(core=[]),lambda b:b['members'][-1].update(buy_eligible='true'),lambda b:b['members'][-1].update(fingerprint='bad')):
            bad=copy.deepcopy(body);mutate(bad)
            with self.assertRaises(ValueError):universe.validate_publication(self.store,self.cfg,bad,[],self.at,self.later(.5))
        m=next(m for m in body['members'] if m['symbol']=='sz300499')
        item={'symbol':'sz300499','route':'watchlist','action':'ALLOW','membership_token':m['fingerprint']}
        universe.validate_publication(self.store,self.cfg,body,[item],self.at,self.later(.5))
        item['route']='dynamic'
        with self.assertRaises(ValueError):universe.validate_publication(self.store,self.cfg,body,[item],self.at,self.later(.5))

    def test_unknown_business_period_is_saved_as_a_lead_not_discarded(self):
        p=self.proposal();p['facts'][0]['period']='';self.save(p)
        h=industry.latest(self.store,self.at)[0]
        self.assertEqual(h['state'],'WAITING');self.assertEqual(h['payload']['facts'][0]['period'],'UNKNOWN')
        self.assertFalse(universe.permit(self.store,self.cfg,'sz300499',self.at))
    def test_republished_identical_facts_cannot_reset_catalyst_clock(self):
        self.save();self.doc=self.document(self.text,at=self.later(8),url='https://example.org/reprint',published=self.later(8))
        self.save(at=self.later(8));self.assertFalse(universe.permit(self.store,self.cfg,'sz300499',self.later(8)))
    def test_new_company_snapshot_includes_hypotheses_but_gaps_still_block_trading(self):
        self.save()
        from ashare.finance import PaperLedger
        from ashare.research import make_snapshot,price_plan
        PaperLedger(self.store).initialize()
        packet=make_snapshot(self.store,self.cfg,'sz300499',at=self.at)
        self.assertEqual(packet['research_membership']['membership'],'DYNAMIC')
        self.assertEqual(len(packet['industry_hypotheses']),1)
        plan=price_plan(packet,self.cfg,{'action':'WATCH'},'SUCCEEDED')
        self.assertEqual(plan['kind'],'NO_ENTRY');self.assertIn('FINANCIAL_BASELINE_INCOMPLETE',plan['blockers'])

    def test_without_model_disables_discovery_in_standalone_and_full_cycle(self):
        from ashare.workflow import execute
        self.cfg['portfolio_strategy']='';self.cfg['deployment_role']='local'
        for command in ('industry_research','cycle'):
            with self.subTest(command=command), patch('ashare.industry_research.run',return_value={}) as discover, patch('ashare.workflow.collect_batch',return_value={}):
                execute(self.cfg,command,use_model=False)
                self.assertFalse(discover.call_args.args[1]['model_enabled'])

class IndustryCloudTests(unittest.TestCase):
    from test_cloud_sync import CloudSyncTests as F
    quote=F.quote;plan=F.plan;later=F.later;output=F.output;transact=F.transact;publication=F.publication;receive=F.receive
    document=IndustryTests.document;fact=IndustryTests.fact;proposal=IndustryTests.proposal;save=IndustryTests.save
    def setUp(self):
        self.F.setUp(self)
        self.cfg['industry_enabled']=True;self.cloud_cfg['industry_enabled']=True
        from ashare.cloud_runtime import put
        with self.store.db:put(self.store,'remote_features',['industry_lists_v1','targeted_invalidation'])
        self.identities={'sz300499':{'name':'高澜股份','category':'CN','kind':'STOCK'}}
        self.text='预算已经落实，采购招标启动；液冷产品供货份额仍需核实。';self.doc=self.document(self.text);self.save()
    def tearDown(self):self.F.tearDown(self)
    def test_dynamic_company_publishes_without_changing_fixed_cloud_config(self):
        with patch('ashare.universe.now',return_value=self.later(40*86400)):
            body=self.publication()
        self.assertIn('watchlist:sz300499',{d['key'] for d in json.loads(body['decision']['payload_json'])['decisions']})
        self.assertEqual(self.receive(body)['status'],'ACCEPTED')
        from ashare.cloud_runtime import value
        members=value(self.cloud,'research_membership')['members']
        self.assertIn('sz300499',{m['symbol'] for m in members})
        self.assertNotIn('sz300499',{m['symbol'] for m in self.cloud_cfg['watchlist']})
        self.assertIn('sz300499',{m['symbol'] for m in universe.company_targets(self.cloud,self.cloud_cfg,self.at)})
    def test_missing_or_tampered_certificate_is_rejected_atomically(self):
        body=self.publication()
        for remove in (True,False):
            bad=copy.deepcopy(body)
            if remove:bad.pop('research_membership')
            else:bad['research_membership']['members'][-1]['fingerprint']='fabricated'
            with self.assertRaises(ValueError):self.receive(bad)
        from ashare.cloud_runtime import value
        self.assertIsNone(value(self.cloud,'research_completed_at'))
    def test_failed_revocation_remains_durable_until_acknowledged(self):
        from ashare import cloud_sync as sync
        from ashare.cloud_runtime import value
        sync.queue_invalidation(self.store,['watchlist:sz300499'],self.at)
        with patch('ashare.cloud_sync.request',side_effect=OSError('offline')):
            with self.assertRaises(OSError):sync.deliver_invalidation(self.store,self.cfg)
        self.assertIsNotNone(value(self.store,'pending_invalidation'))
        with patch('ashare.cloud_sync.request',return_value={'status':'ACCEPTED'}):sync.deliver_invalidation(self.store,self.cfg)
        self.assertIsNone(value(self.store,'pending_invalidation'))
        self.assertEqual(value(self.store,'last_invalidation')['changed_at'],self.at)
    def test_unknown_orders_keep_protection_when_membership_expires(self):
        body=self.publication();self.receive(body)
        self.assertTrue(universe.permit(self.cloud,self.cloud_cfg,'sz300499',self.at))
        self.assertFalse(universe.permit(self.cloud,self.cloud_cfg,'sz300499',self.later(8*86400)))

class IndustrySourceTests(unittest.TestCase):
    def test_failed_public_sources_report_gaps_not_zero_events(self):
        from ashare.industry_sources import collect
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(tmp)
            try:
                collect(s,{},'cycle','medical',time.monotonic()+30,fetch_fn=lambda *a,**k:(_ for _ in ()).throw(OSError('source unavailable')))
                rows=list(s.db.execute('SELECT * FROM industry_coverage'))
                self.assertEqual(len(rows),2);self.assertTrue(all(r['status']=='FAILED' for r in rows))
                self.assertEqual(s.db.execute('SELECT count(*) FROM industry_hypotheses').fetchone()[0],0)
            finally:s.close()

class CrossCompanyEvidenceTests(unittest.TestCase):
    def test_only_hypothesis_linked_external_evidence_can_be_cited(self):
        from ashare.model import validate_result
        packet={'schema_version':'0.2','stocks':[{'symbol':'sz300499'}],'evidence':[{'evidence_id':'upstream:0','symbol':'sh600001','text':'客户预算已经落实且招标启动。'}]}
        result={'summary':'原文研究','stocks':[{'symbol':'sz300499','action':'WATCH','analysis':'客户采购可能传导','facts':[{'evidence_id':'upstream:0','quote':'客户预算已经落实'}],'counterpoints':['份额未知'],'missing_fields':[],'next_checks':['核实份额']}]}
        with self.assertRaises(ValueError):validate_result(copy.deepcopy(result),packet)
        packet['industry_hypotheses']=[{'facts':[{'evidence_id':'upstream:0'}]}]
        self.assertEqual(validate_result(result,packet)['stocks'][0]['facts'][0]['evidence_id'],'upstream:0')
    def test_sourced_revenue_scenario_rejects_mixed_periods_and_units(self):
        specs=[('INCREMENTAL_UNITS','台','100-200'),('CONTENT_PER_UNIT','件/台','2'),('UNIT_PRICE','元/件','50'),('SUPPLIER_SHARE','比例','0.1-0.2')]
        facts=[{'metric':m,'unit':u,'value':v,'period':'2026Q4','claim_type':'GUIDANCE','evidence_id':str(i)} for i,(m,u,v) in enumerate(specs)]
        r=industry.revenue_scenario(facts);self.assertEqual((r['status'],r['low'],r['high']),('SCENARIO','1000.0','4000.0'))
        facts[0]['period']='2027Q1';self.assertEqual(industry.revenue_scenario(facts)['status'],'MISSING')

class USCompanyDossierTests(unittest.TestCase):
    def test_sec_dossier_preserves_periods_and_blocks_missing_usd_fundamentals(self):
        from ashare.us_company import collect,dossier,TAGS
        from ashare.storage import now
        at=now();day=at[:10]
        facts={'cik':1,'facts':{'us-gaap':{tags[0]:{'units':{'USD':[{'start':day[:4]+'-01-01','end':day,'filed':day,'form':'10-Q','val':100}]}} for tags in TAGS.values()}}}
        submissions={'name':'Sample issuer','tickers':['TEST'],'filings':{'recent':{'form':['10-Q'],'filingDate':[day],'accessionNumber':['0000000001-26-000001'],'primaryDocument':['report.htm']}}}
        def fetch(url,**kwargs):
            if url.endswith('company_tickers.json'):return json.dumps({'0':{'ticker':'TEST','cik_str':1}}).encode()
            if '/companyfacts/' in url:return json.dumps(facts).encode()
            if '/submissions/' in url:return json.dumps(submissions).encode()
            return ('<html><body><p>'+'Revenue and cash disclosures. '*30+'</p></body></html>').encode()
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp)
            try:
                p=collect(store,'US:TEST',at,fetch_fn=fetch)
                self.assertEqual(p['status'],'READY');self.assertEqual(len(p['documents']),1)
                observed=p['metrics']['operating_cashflow'][0]
                self.assertEqual((observed['start'],observed['end'],observed['val']),(day[:4]+'-01-01',day,100))
                self.assertEqual(dossier(store,'US:TEST',now())['metrics'],p['metrics'])
                facts['facts']['us-gaap']['Assets']['units']={'shares':[{'end':day,'filed':day,'form':'10-Q','val':100}]}
                p=collect(store,'US:TEST',now(),fetch_fn=fetch)
                self.assertEqual(p['status'],'PARTIAL');self.assertIn('assets',p['gaps'])
                facts['cik']=2
                with self.assertRaisesRegex(ValueError,'主体不匹配'):collect(store,'US:TEST',now(),fetch_fn=fetch)
            finally:store.close()
