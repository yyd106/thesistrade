import json
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
from ashare.storage import Store,now,normalize_time
from test_config import load_config
from ashare.demo import seed,research_model,put_quote,SYMBOL
from ashare.research import study,price_plan
from ashare.workflow import execute
from ashare.slots import run_slot,decision_packet
from ashare.monitor import cached_checks
from ashare.recovery import enqueue_recovery,recovery_status
from ashare.scheduler import enqueue
from ashare.presentation import failure_help,failure_reason
from ashare.event_review import cash_terms,evaluate
from types import SimpleNamespace
from unittest.mock import MagicMock


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.cfg=load_config(Path(__file__).resolve().parents[1]/'config.json')
        self.cfg.update(slot_execution_mode='MODEL',slot_deadline_seconds=240)
        self.cfg.update(data_dir=self.tmp.name,watchlist=[{'symbol':SYMBOL,'name':'测试'}])
        self.at=normalize_time('2026-09-18T10:00:00+08:00')
        self.store.db.execute("INSERT INTO runs(id,job_key,kind,started_at,status) VALUES('r','r','test',?,'RUNNING')",(self.at,));self.store.db.commit()

    def tearDown(self):self.store.close();self.tmp.cleanup()

    def check(self,status,symbol=SYMBOL,at=None):
        with self.store.db:
            self.store.db.execute('INSERT INTO source_checks(run_id,source,symbol,status,detail,checked_at) VALUES(?,?,?,?,?,?)',
                ('r','slot_events',symbol,status,'test',at or self.at))

    def test_cache_requires_each_stock_check_and_global_failure_is_not_cleared_by_catalog(self):
        self.check('OK')
        with patch('ashare.monitor.now',return_value=self.at):
            self.assertEqual(cached_checks(self.store,self.cfg)['event_status'][SYMBOL],'OK')
            self.check('FAILED',None)
            self.assertEqual(cached_checks(self.store,self.cfg)['event_status'][SYMBOL],'FAILED')
            self.check('OK',None)
            self.assertEqual(cached_checks(self.store,self.cfg)['event_status'][SYMBOL],'FAILED')
            self.check('OK')
            self.assertEqual(cached_checks(self.store,self.cfg)['event_status'][SYMBOL],'OK')
        late=(datetime.fromisoformat(self.at)+timedelta(seconds=181)).isoformat()
        with patch('ashare.monitor.now',return_value=late):
            self.assertEqual(cached_checks(self.store,self.cfg)['event_status'][SYMBOL],'STALE')

    def test_partial_catalog_never_becomes_complete_or_applies_to_other_stock(self):
        self.check('PARTIAL')
        self.cfg['watchlist'].append({'symbol':'sh600000','name':'另股'})
        with patch('ashare.monitor.now',return_value=self.at):
            checks=cached_checks(self.store,self.cfg)['event_status']
        self.assertEqual(checks[SYMBOL],'PARTIAL');self.assertEqual(checks['sh600000'],'STALE')

    def test_all_blocked_stocks_skip_model_and_never_place_order(self):
        put_quote(self.store,self.at)
        with patch('ashare.slots.run_json',side_effect=AssertionError('no model expected')) as model:
            result=run_slot(self.store,self.cfg,self.at,clock=lambda:self.at,
                refresh_fn=lambda *a:{'event_status':{SYMBOL:'FAILED'}})
        model.assert_not_called()
        self.assertEqual(result['model_status'],'NOT_NEEDED')
        self.assertEqual(result['decisions'][0]['status'],'BLOCKED')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],0)

    def test_decision_input_omits_large_audit_arrays_but_keeps_prices_and_plan(self):
        packet={'slot_id':'s','input_as_of':self.at,'deadline':self.at,'mode':'paper','account':{'available_cents':123,'positions':{'ignored':'large'}}}
        entry={'symbol':SYMBOL,'plan':{'id':'p','valid_until':self.at},'plan_content':{'levels':{'buy_low_cents':99,'buy_high_cents':101},'basis':{'bars':['large']*10000},'thesis':'观点'*1000},'position':None,'quote':{'price_cents':100,'raw_path':'private-path'},'buy_blockers':[],'risk_trigger':None}
        out=decision_packet(packet,[entry]);text=json.dumps(out,ensure_ascii=False)
        self.assertNotIn('large',text);self.assertNotIn('private-path',text)
        self.assertLess(len(text),2000);self.assertEqual(out['stocks'][0]['levels']['buy_high_cents'],101)

    def test_research_retries_fresh_snapshot_once_and_keeps_audit_failure(self):
        packet=seed(self.store,self.cfg);calls=[]
        def fake(store,config,p,use_model):
            calls.append(p['snapshot_id'])
            if len(calls)==1:raise TimeoutError('研究超时')
            return study(store,config,p,model_fn=research_model(p))
        with patch('ashare.workflow.study',side_effect=fake):result=execute(self.cfg,'research')
        self.assertEqual(result[0]['attempts'],2);self.assertEqual(result[0]['status'],'SUCCEEDED')
        self.assertNotEqual(calls[0],calls[1])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM data_attempts WHERE source='research_pipeline' AND status='FAILED'").fetchone()[0],1)

    def test_login_failure_does_not_keep_retrying(self):
        seed(self.store,self.cfg)
        with patch('ashare.workflow.study',side_effect=RuntimeError('需要ChatGPT订阅登录')) as fn:
            result=execute(self.cfg,'research')
        self.assertEqual(fn.call_count,1);self.assertEqual(result[0]['status'],'DEFERRED')

    def test_retry_budget_survives_restart_and_manual_job_deduplicates_by_payload(self):
        enqueue_recovery(self.store,self.cfg,self.at)
        row=self.store.db.execute("SELECT id FROM jobs WHERE kind='repair'").fetchone()
        self.assertTrue(row)
        self.assertEqual(json.loads(self.store.db.execute('SELECT payload_json FROM job_inputs WHERE job_id=?',(row[0],)).fetchone()[0])['symbol'],SYMBOL)
        with self.store.db:self.store.db.execute("UPDATE jobs SET status='DONE'")
        later=normalize_time('2026-09-18T10:20:00+08:00')
        enqueue_recovery(self.store,self.cfg,later)
        with self.store.db:self.store.db.execute("UPDATE jobs SET status='DONE'")
        self.store.close();self.store=Store(self.tmp.name)
        enqueue_recovery(self.store,self.cfg,normalize_time('2026-09-18T10:40:00+08:00'))
        state=recovery_status(self.store,self.cfg,SYMBOL,later)
        self.assertEqual(state['state'],'LIMIT_REACHED');self.assertEqual(state['automatic_attempts'],2)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE kind='repair'").fetchone()[0],2)

    def test_paused_scheduler_never_enqueues_recovery(self):
        self.cfg['scheduler_enabled']=False;enqueue_recovery(self.store,self.cfg,self.at)
        self.assertFalse(self.store.db.execute('SELECT 1 FROM jobs').fetchone())
        self.assertIn('已暂停',recovery_status(self.store,self.cfg,SYMBOL,self.at)['next_step'])

    def test_failure_guidance_is_specific_and_untrusted_links_are_rejected(self):
        self.assertIn('网址',failure_reason('nodename nor servname provided, or not known'))
        q=failure_help('tencent_quotes','nodename nor servname provided',config=self.cfg)
        self.assertIn('买入和卖出',q['trading_effect']);self.assertIn('热点',q['next_step'])
        optional=failure_help('cninfo_pdf','timeout','法律意见书','https://evil.example/secret')
        self.assertIsNone(optional['source_url']);self.assertIn('不直接',optional['trading_effect'])
        auth=failure_help('research_analysis','需要ChatGPT订阅登录')
        self.assertIn('codex login',auth['next_step']);self.assertIn('暂停',auth['recovery'])

    def event_fixture(self):
        text='证券代码：'+SYMBOL[2:]+'。每10股派发现金红利5.50元（含税），不送红股，不以资本公积金转增股本。股权登记日为2026年8月18日，除权除息日为2026年8月19日。'
        doc={'id':'event','title':'2026年半年度权益分派实施公告','url':'https://static.cninfo.com.cn/example.pdf',
             'symbol':SYMBOL,'kind':'company_report','source':'cninfo','content_hash':'a','cloud_allowed':True}
        chunks={'event':[{'id':'event:1','text':text}]}
        bars=[[(datetime(2026,7,1)+timedelta(days=n)).date().isoformat(),'10','10','10','10'] for n in range(79)]
        features={'unadjusted':{'bars':bars,'ma20_cents':1000,'ma60_cents':1000,'close_cents':1000,'basis':'UNADJUSTED','last_complete_date':'2026-09-17'}}
        return doc,chunks,features

    def test_verified_cash_dividend_changes_only_pre_ex_closes_and_is_auditable(self):
        doc,chunks,features=self.event_fixture()
        reviews,adjusted=evaluate(self.store,SYMBOL,[doc],chunks,features,self.at)
        self.assertEqual(reviews[0]['status'],'VERIFIED')
        self.assertEqual(reviews[0]['facts']['cash_per_share_cents'],55)
        self.assertEqual(reviews[0]['evidence_ids'],['event:1'])
        self.assertEqual(adjusted['basis'],'CASH_DIVIDEND_ADJUSTED')
        self.assertEqual(adjusted['bars'][0][2],'9.45');self.assertEqual(adjusted['bars'][-1][2],'10')
        self.assertEqual(features['unadjusted']['bars'][0][2],'10')
        self.assertEqual(adjusted['close_cents'],1000)

    def test_ambiguous_cash_or_wrong_stock_is_never_verified(self):
        doc,chunks,features=self.event_fixture()
        chunks['event'][0]['text']+='每10股派发现金红利8元（含税）。'
        terms,missing=cash_terms(chunks['event'][0]['text'],SYMBOL)
        self.assertIsNone(terms['cash_per_share_cents']);self.assertTrue(missing)
        self.assertTrue(cash_terms(chunks['event'][0]['text'],'sz002415')[1])
        review,adjusted=evaluate(self.store,SYMBOL,[doc],chunks,features,self.at)
        self.assertEqual(review[0]['status'],'NEEDS_EVIDENCE');self.assertEqual(adjusted,features['unadjusted'])

    def test_missing_full_text_future_dividend_and_unsupported_actions_remain_blocked(self):
        doc,chunks,features=self.event_fixture()
        for change in ({'kind':'announcement_metadata'},{'source':'user_import'},{'title':'权益分派实施更正公告'}):
            reviews,_=evaluate(self.store,SYMBOL,[{**doc,**change}],chunks,features,self.at)
            self.assertEqual(reviews[0]['status'],'NEEDS_EVIDENCE')
        features['unadjusted']['last_complete_date']='2026-08-18'
        reviews,_=evaluate(self.store,SYMBOL,[doc],chunks,features,self.at)
        self.assertEqual(reviews[0]['status'],'NEEDS_EVIDENCE')
        self.assertIn('等待除息日',str(reviews[0]['missing']))

    def test_existing_dividend_entitlement_requires_ledger_reconciliation(self):
        doc,chunks,features=self.event_fixture()
        db=MagicMock();db.execute.return_value.fetchone.return_value=(100,)
        reviews,u=evaluate(SimpleNamespace(db=db),SYMBOL,[doc],chunks,features,self.at)
        self.assertEqual(reviews[0]['status'],'NEEDS_EVIDENCE');self.assertIn('分红入账',str(reviews[0]['missing']))
        self.assertEqual(u,features['unadjusted'])

    def test_duplicate_implementation_and_risk_not_misrepresented_as_resolved(self):
        doc,chunks,features=self.event_fixture();other={**doc,'id':'other'}
        chunks['other']=[{'id':'other:1','text':chunks['event'][0]['text']}]
        reviews,u=evaluate(self.store,SYMBOL,[doc,other],chunks,features,self.at)
        self.assertTrue(all(r['status']=='NEEDS_EVIDENCE' for r in reviews));self.assertEqual(u,features['unadjusted'])
        reviews,_=evaluate(self.store,SYMBOL,[{**doc,'title':'诉讼进展公告'}],chunks,features,self.at)
        self.assertEqual(reviews[0]['status'],'NEEDS_EVIDENCE');self.assertIn('正式结论',str(reviews[0]['missing']))

    def test_future_event_not_verified_even_when_input_bars_claim_completion(self):
        doc,chunks,features=self.event_fixture()
        reviews,_=evaluate(self.store,SYMBOL,[doc],chunks,features,normalize_time('2026-08-18T10:00:00+08:00'))
        self.assertEqual(reviews[0]['status'],'NEEDS_EVIDENCE')
        self.assertIn('尚未到达',str(reviews[0]['missing']))

    def test_event_verification_never_removes_unread_or_research_veto(self):
        packet=seed(self.store,self.cfg)
        doc=packet['mandatory_coverage'][0]
        doc.update(critical=True,importance='CORPORATE_ACTION',fulltext=False,required=True)
        packet['event_reviews']=[{'doc_id':doc['doc_id'],'status':'VERIFIED'}]
        plan=price_plan(packet,self.cfg,{'action':'AVOID'},'SUCCEEDED')
        self.assertNotIn('CORPORATE_ACTION_UNVERIFIED:'+doc['doc_id'],plan['blockers'])
        self.assertIn('UNREAD_DOCUMENT:'+doc['doc_id'],plan['blockers'])
        self.assertIn('RESEARCH_VETO',plan['blockers']);self.assertEqual(plan['kind'],'NO_ENTRY')

    def test_inbox_original_url_is_linked_without_forging_official_source(self):
        from ashare.inbox import import_inbox
        root=Path(self.tmp.name)/'inbox';root.mkdir(exist_ok=True)
        (root/'report.txt').write_text('公开报告正文。现金流和经营情况。'*30)
        url='https://static.cninfo.com.cn/example.pdf'
        (root/'report.json').write_text(json.dumps({'file':'report.txt','symbol':SYMBOL,'kind':'company_report',
            'title':'半年度报告','published_at':self.at,'source_url':url,'cloud_allowed':True}))
        import_inbox(self.store,self.cfg,'r')
        row=self.store.db.execute("SELECT source,url,cloud_allowed FROM documents WHERE title='半年度报告'").fetchone()
        self.assertEqual(row['url'],url);self.assertEqual(row['source'],'user_import');self.assertEqual(row['cloud_allowed'],1)


if __name__=='__main__':unittest.main()
