"""Regression cases for the seven defects found in the requirements review."""
import copy,json,time,unittest,tempfile
from datetime import datetime
from unittest.mock import patch
import test_industry as fixtures
from ashare import industry,universe,industry_sources,industry_presentation
from ashare.storage import Store

class ReviewTests(unittest.TestCase):
    setUp=fixtures.IndustryTests.setUp;tearDown=fixtures.IndustryTests.tearDown
    document=fixtures.IndustryTests.document;fact=fixtures.IndustryTests.fact
    proposal=fixtures.IndustryTests.proposal;save=fixtures.IndustryTests.save;later=fixtures.IndustryTests.later
    def test_bottleneck_rejects_incompatible_product_period_entity_and_unknown_claims(self):
        for field,value in [('product','变压器'),('period','2025年'),('entity','无关供应商')]:
            p=self.proposal('2');p['topic']+=field;p['facts'][1][field]=value
            if field=='entity':p['facts'][1]['project']='无关项目'
            hid=self.save(p);h=next(h for h in industry.latest(self.store,self.at) if h['id']==hid)
            self.assertEqual(h['state'],'WAITING')
        for field in ('alternatives','profit_capture'):
            p=self.proposal('2');p['topic']+=field;p[field]='未知';hid=self.save(p)
            self.assertEqual(next(h for h in industry.latest(self.store,self.at) if h['id']==hid)['state'],'WAITING')
    def test_bottleneck_rejects_unsupported_or_wrongly_scoped_supply_constraint(self):
        for metric in ('SUPPLY_CONSTRAINT','ALTERNATIVE_SUPPLY','PROFIT_CAPTURE'):
            p=self.proposal('2');p['topic']+=metric;p['facts']=[f for f in p['facts'] if f['metric']!=metric]
            hid=self.save(p);self.assertEqual(next(h for h in industry.latest(self.store,self.at) if h['id']==hid)['state'],'WAITING')
        p=self.proposal('2');p['facts'][2]['product']='无关产品';hid=self.save(p)
        self.assertEqual(next(h for h in industry.latest(self.store,self.at) if h['id']==hid)['state'],'WAITING')
    def test_old_bottleneck_record_loses_permission_without_rewriting_history(self):
        p=self.proposal('2');p['facts']=p['facts'][:2]
        with patch('ashare.industry.bottleneck_gaps',return_value=[]):hid=self.save(p)
        h=industry.latest(self.store,self.at)[0]
        self.assertEqual(h['state'],'ACTIVE');self.assertEqual(industry.state(self.store,h,self.at),'REVIEW')
        self.assertFalse(universe.permit(self.store,self.cfg,'sz300499',self.at))
        self.assertEqual(self.store.db.execute('SELECT state FROM industry_hypotheses WHERE id=?',(hid,)).fetchone()[0],'ACTIVE')
    def forecast_and_actual(self):
        p=self.proposal();p['forecasts']=[dict(metric='ORDERS',baseline='90',low='100',high='120',unit='元',period='2026年第四季度',due_at=self.later(1))];self.save(p)
        self.text+=' 公司本期订单110元。';self.doc=self.document(self.text,at=self.later(2),url='https://example.org/result')
        p=self.proposal();p['topic']='后续经营披露';p['facts'].append(self.fact('METRIC','ORDERS'));p['facts'][-1]['value']='110';self.save(p,self.later(2));return p
    def test_actual_survives_later_hypothesis_that_omits_it_and_scoring_is_idempotent(self):
        p=self.forecast_and_actual();self.assertEqual(industry.evaluate(self.store,self.later(2))[0]['status'],'IN_RANGE')
        p['facts']=p['facts'][:2];p['thesis']='继续跟踪交付';self.save(p,self.later(3))
        self.assertEqual(industry.evaluate(self.store,self.later(3))[0]['status'],'IN_RANGE')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM industry_outcomes').fetchone()[0],1)
    def test_revised_source_replaces_actual_and_as_of_does_not_see_future(self):
        p=self.forecast_and_actual();industry.evaluate(self.store,self.later(2))
        self.text='更正原报告，本期订单80元。';self.doc=self.document(self.text,at=self.later(4),url='https://example.org/result')
        p=self.proposal();p['topic']='更正披露';p['facts']=[self.fact('METRIC','ORDERS')];p['facts'][0]['value']='80';self.save(p,self.later(4))
        self.assertEqual(industry.evaluate(self.store,self.later(4))[0]['status'],'OUTSIDE_RANGE')
        self.assertEqual(industry.view(self.store,self.cfg,self.later(3))['forecasts'][0]['outcome']['status'],'IN_RANGE')
        outcome=industry.view(self.store,self.cfg,self.later(4))['forecasts'][0]['outcome']
        self.assertEqual(outcome['payload']['observations'][0]['value'],'80')
    def test_conflicting_disclosures_are_not_silently_discarded(self):
        self.forecast_and_actual();self.text+='另一次披露订单80元。';self.doc=self.document(self.text,at=self.later(3),url='https://example.org/another-result')
        p=self.proposal();p['facts']=[self.fact('METRIC','ORDERS')];p['facts'][0]['value']='80';p['topic']='另一份实际披露';self.save(p,self.later(3))
        self.assertEqual(industry.evaluate(self.store,self.later(3))[0]['status'],'CONFLICT')
    def test_company_detail_and_complete_evidence_survive_archive(self):
        self.save();universe.reconcile(self.store,self.cfg,self.at)
        for day in range(1,13):
            p=self.proposal();p['thesis']='第'+str(day)+'次核对';self.save(p,self.later(day/100))
        at=self.later(31)
        from ashare.finance import PaperLedger
        from ashare.stock_detail import detail
        PaperLedger(self.store).initialize()
        with patch('ashare.dashboard.now',return_value=at):result=detail(self.cfg,'sz300499')
        self.assertTrue(result['stock']['archived'])
        self.assertEqual(result['industry']['members'][0]['research_status'],'ARCHIVED')
        versions=result['industry']['hypotheses'][0]['versions'];self.assertEqual(len(versions),13)
        self.assertTrue(all(v['payload']['facts'][0]['quote']==self.text for v in versions))
    def test_membership_changes_reason_without_eligibility_change_are_recorded(self):
        self.save();universe.reconcile(self.store,self.cfg,self.at)
        members=universe.membership(self.store,self.cfg,self.at);members[-1]['reason']='补充核对后的跟踪理由'
        before=self.store.db.execute('SELECT count(*) FROM industry_memberships').fetchone()[0]
        with patch('ashare.universe.membership',return_value=members):universe.reconcile(self.store,self.cfg,self.later(.1))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM industry_memberships').fetchone()[0],before+1)

class SourceQueueTests(unittest.TestCase):
    def test_multiple_rounds_continue_after_successes_and_retry_failures_fairly(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp);calls=[];pages=[];fail={0}
            def fetch(url,**kw):
                if url.endswith('/query'):
                    page=kw['form']['pageNum'];pages.append(page)
                    rows=[dict(secCode='300499',adjunctUrl=f'/review-{i}.pdf',announcementTitle=f'液冷采购{i}',announcementTime=int(datetime.now().timestamp()*1000)) for i in range((page-1)*4,page*4)]
                    return json.dumps(dict(announcements=rows,hasMore=page<2)).encode()
                if 'ccgp' in url:raise OSError('synthetic source unavailable')
                calls.append(url);i=int(url.split('review-')[1].split('.')[0])
                if i in fail:fail.remove(i);raise OSError('temporary PDF failure')
                return url.encode()
            with patch('ashare.sources.pdf_pages',side_effect=lambda body:[(1,'原文'+body.decode())]):
                for cycle in range(5):industry_sources.collect(store,{},str(cycle),'ai',time.monotonic()+30,fetch_fn=fetch)
            self.assertEqual(pages[:2],[1,2]);self.assertEqual(store.db.execute("SELECT count(*) FROM industry_source_queue WHERE status='DONE'").fetchone()[0],8)
            self.assertEqual(calls.count('https://static.cninfo.com.cn/review-0.pdf'),2)
            self.assertEqual(store.db.execute("SELECT count(*) FROM documents WHERE kind='company_report'").fetchone()[0],8)
            store.close()
    def test_unreadable_pdf_is_not_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp)
            def fetch(url,**kw):
                if url.endswith('/query'):return json.dumps(dict(announcements=[dict(secCode='300499',adjunctUrl='/blank.pdf',announcementTitle='空白正文',announcementTime=int(datetime.now().timestamp()*1000))],hasMore=False)).encode()
                if 'ccgp' in url:raise OSError('unavailable')
                return b'pdf'
            with patch('ashare.sources.pdf_pages',return_value=[]):industry_sources.collect(store,{},'one','ai',time.monotonic()+30,fetch_fn=fetch)
            self.assertEqual(store.db.execute('SELECT status FROM industry_source_queue').fetchone()[0],'PENDING');store.close()

class CloudDisplayTests(unittest.TestCase):
    def setUp(self):
        self.data={'members':[dict(symbol='sz300499',membership='DYNAMIC',tier='ACTIVE',buy_eligible=True,review_at='2026-10-07T00:00:00Z',fingerprint='f')], 'hypotheses':[dict(symbol='sz300499',state='ACTIVE',review_at='2026-10-07T00:00:00Z',expires_at='2026-10-30T00:00:00Z')]}
        self.cert={'as_of':'2026-10-01T00:00:00Z','members':copy.deepcopy(self.data['members'])}
    def test_expired_snapshot_cannot_display_active_permission(self):
        result=industry_presentation.current(self.data,'2026-10-08T00:00:00Z',signed=self.cert,research_lease={'active':True})
        self.assertFalse(result['members'][0]['buy_eligible']);self.assertFalse(result['members'][0]['entry_allowed'])
        self.assertEqual(result['hypotheses'][0]['effective_state'],'REVIEW');self.assertTrue(self.cert['members'][0]['buy_eligible'])
    def test_revocation_and_lease_expiry_take_priority_over_cached_display(self):
        for kwargs in [dict(research_lease={'active':False}),dict(revoked={'watchlist:sz300499':'2026-10-02T00:00:00Z'}),dict(revoked_at='2026-10-02T00:00:00Z')]:
            result=industry_presentation.current(self.data,'2026-10-03T00:00:00Z',signed=self.cert,**kwargs)
            self.assertFalse(result['members'][0]['entry_allowed']);self.assertTrue(self.data['members'][0]['buy_eligible'])
    def test_signed_review_deadline_is_checked_independently(self):
        self.cert['members'][0]['review_at']='2026-10-02T00:00:00Z'
        result=industry_presentation.current(self.data,'2026-10-03T00:00:00Z',signed=self.cert,research_lease={'active':True})
        self.assertFalse(result['members'][0]['entry_allowed'])
