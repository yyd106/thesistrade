import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from ashare.storage import Store, now
from test_config import load_config
from ashare.presentation import outstanding_failures, trader_report, INTERNAL_WORDS
from ashare.sources import collect_announcements, collect_news, collect_quotes, history_summary
from ashare.research import study, make_snapshot, model_packet
from ashare.model import validate_result
from ashare.demo import seed, research_model, SYMBOL
from ashare.dashboard import status
from ashare.workflow import execute


class ResearchPresentationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.cfg=load_config(Path(__file__).resolve().parents[1]/'config.json')
        self.cfg.update(data_dir=self.tmp.name,watchlist=[{'symbol':SYMBOL,'name':'测试股票'}])
        self.store.db.execute("INSERT INTO runs(id,job_key,kind,started_at,status) VALUES('r','r','collect',?,'RUNNING')",(now(),));self.store.db.commit()

    def tearDown(self):self.store.close();self.tmp.cleanup()

    def failures(self,symbol=SYMBOL):return outstanding_failures(self.store,symbol)

    def test_only_matching_item_success_clears_failure_and_recurrence_returns(self):
        for url in ('a','b'):
            self.store.record_attempt('cninfo_pdf',SYMBOL,'FAILED','下载超时',resource_key=url,title='报告'+url)
        self.store.record_attempt('tencent_daily',SYMBOL,'OK')
        self.store.record_attempt('cninfo_pdf','sh600000','OK',resource_key='a')
        self.assertEqual(len(self.failures()),2)
        self.store.record_attempt('cninfo_pdf',SYMBOL,'OK',resource_key='a')
        self.assertEqual([x['title'] for x in self.failures()],['报告b'])
        self.store.close();self.store=Store(self.tmp.name)
        self.assertEqual(len(self.failures()),1)
        self.store.record_attempt('cninfo_pdf',SYMBOL,'FAILED','下载超时',resource_key='a')
        self.assertEqual(len(self.failures()),2)

    def test_public_failure_shows_on_each_stock_until_same_resource_succeeds(self):
        self.store.record_attempt('news_article','MARKET','FAILED','HTTP Error 503',resource_key='article')
        self.assertTrue(self.failures()[0]['shared']);self.assertEqual(len(self.failures('sh600000')),1)
        self.store.record_attempt('official_news','MARKET','OK')
        self.assertEqual(len(self.failures()),1)
        self.store.record_attempt('news_article','MARKET','OK',resource_key='article')
        self.assertEqual(self.failures(),[])

    def test_history_failure_survives_unrelated_success_and_old_completion(self):
        self.store.record_attempt('tencent_daily',SYMBOL,'FAILED',"KeyError: 'qfqday'",at='2026-09-17T04:00:00Z')
        for _ in range(25):self.store.record_attempt('tencent_quotes',SYMBOL,'OK')
        self.store.record_attempt('tencent_daily',SYMBOL,'OK',at='2026-09-17T03:00:00Z')
        self.assertEqual(len(self.failures()),1)
        self.assertNotIn('KeyError',str(self.failures()))
        self.store.record_attempt('tencent_daily',SYMBOL,'OK',at='2026-09-17T05:00:00Z')
        self.assertEqual(self.failures(),[])

    def test_pdf_directory_only_and_other_document_do_not_clear_failure(self):
        announcements=[{'secCode':SYMBOL[2:],'announcementTitle':'报告'+s,'adjunctUrl':s+'.pdf','announcementTime':1700000000000} for s in ('a','b')]
        listing=json.dumps({'announcements':announcements,'hasMore':False}).encode()
        fail={'a.pdf'}
        def fetch(url,form=None,**kwargs):
            if form:return listing
            if url.rsplit('/',1)[-1] in fail:raise TimeoutError('下载超时')
            return b'%PDF-'+url.encode()
        config={**self.cfg,'pdf_downloads_per_stock':2}
        catalog=[{'code':SYMBOL[2:],'orgId':'fixture'}]
        with patch('ashare.sources.fetch',side_effect=fetch),patch('ashare.sources.pdf_pages',return_value=[(1,'公司收入与经营情况')]):
            collect_announcements(self.store,'r',{'symbol':SYMBOL},config,catalog)
            self.assertEqual(len(self.failures()),1)
            self.store.record_attempt('cninfo_catalog',SYMBOL,'FAILED','分页未完成')
            collect_announcements(self.store,'r',{'symbol':SYMBOL},{**config,'pdf_downloads_per_stock':0,'_intraday':True},catalog)
            self.assertEqual(len(self.failures()),2)
            fail.clear()
            collect_announcements(self.store,'r',{'symbol':SYMBOL},config,catalog)
            self.assertEqual(self.failures(),[])
            # Unchanged bytes are valid recovery too, because the document was already parsed.
            self.store.record_attempt('cninfo_pdf',SYMBOL,'FAILED','临时下载失败',resource_key='https://static.cninfo.com.cn/a.pdf')
            collect_announcements(self.store,'r',{'symbol':SYMBOL},config,catalog)
            self.assertEqual(self.failures(),[])

    def test_partial_news_coverage_without_errors_is_not_failure(self):
        index=''.join(f'<a href="/news/c_{n}.html">交易所发布市场重要消息第{n}条详细内容</a>' for n in range(3))
        with patch('ashare.sources.fetch',side_effect=lambda url,**kw:(index if url.endswith('/index') else '<p>2026-09-16 '+'公告原文内容。'*30+'</p>').encode()):
            collect_news(self.store,'r','https://www.sse.com.cn/index',fulltext_limit=1)
        self.assertEqual(self.failures(),[])

    def test_quote_error_is_attached_to_missing_stock_only(self):
        fields=['']*38;fields[1]='测试';fields[2]=SYMBOL[2:];fields[3]='10';fields[4]='9';fields[30]='20260915100000'
        raw=('v_'+SYMBOL+'="'+'~'.join(fields)+'";').encode('gb18030')
        with patch('ashare.sources.fetch',return_value=raw):
            with self.assertRaises(ValueError):collect_quotes(self.store,'r',[SYMBOL,'sh600000'])
        self.assertEqual(self.failures(),[]);self.assertEqual(len(self.failures('sh600000')),1)

    def test_original_daily_format_supported_without_claiming_adjusted(self):
        bars=[[(date(2026,5,1)+timedelta(days=i)).isoformat(),'10','10','10','10','100'] for i in range(65)]
        result=history_summary(json.dumps({'data':{SYMBOL:{'day':bars}}}),SYMBOL,'2026-09-17')
        self.assertEqual(result['adjustment'],'provider_unadjusted_fallback');self.assertEqual(result['ma20'],'10')
        with self.assertRaises(ValueError):history_summary(json.dumps({'data':{SYMBOL:{'day':bars[:3]}}}),SYMBOL,'2026-09-17')

    def test_failed_new_study_visible_alongside_prior_active_report_then_recovers(self):
        packet=seed(self.store,self.cfg)
        good=study(self.store,self.cfg,packet,model_fn=research_model(packet),at='2026-09-15T09:01:00+08:00')
        packet=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:05:00+08:00')
        study(self.store,self.cfg,packet,model_fn=lambda *a:(_ for _ in ()).throw(TimeoutError('模型超时')),at='2026-09-15T09:06:00+08:00')
        self.assertIn('研究报告生成',[x['label'] for x in self.failures()])
        self.assertEqual(self.store.db.execute("SELECT id FROM plans WHERE status='ACTIVE'").fetchone()[0],good['plan_id'])
        self.assertTrue(status(self.cfg)['watchlist'][0]['failures'])
        packet=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:07:00+08:00')
        study(self.store,self.cfg,packet,model_fn=research_model(packet),at='2026-09-15T09:08:00+08:00')
        self.assertEqual(self.failures(),[])

    def test_prompt_view_omits_raw_exception_and_old_report_preserves_business_risk(self):
        packet=seed(self.store,self.cfg)
        packet['source_checks'].append({'source':'tencent_daily','status':'FAILED','detail':"KeyError: 'qfqday'"})
        self.assertNotIn('KeyError',json.dumps(model_packet(packet,self.cfg)))
        stock={'symbol':SYMBOL,'analysis':'日线来源报错且features为空。公司试验仅80例，仍需确认疗效。','counterpoints':['临床试验可能失败。'],
            'next_checks':['修复日线获取失败后重新计算特征。','核对9MW1911后续试验结果。'],'facts':[]}
        plan={'payload':{},'research':{'stocks':[stock]}}
        before=copy.deepcopy(plan);view=trader_report(plan,SYMBOL)
        self.assertFalse(INTERNAL_WORDS.search(str(view)));self.assertIn('80例',view['analysis']);self.assertEqual(len(view['next_checks']),1)
        self.assertEqual(plan,before)

    def test_program_jargon_rejected_without_replacing_previous_plan(self):
        packet=seed(self.store,self.cfg)
        result=research_model(packet)();result['stocks'][0]['analysis']='features为空，paper_baseline_v1未就绪'
        out=study(self.store,self.cfg,packet,model_fn=lambda *a:result,at='2026-09-15T09:01:00+08:00')
        self.assertEqual(out['status'],'DEFERRED');self.assertEqual(len(self.failures()),1)

    def test_pdf_quote_whitespace_restored_but_changed_number_still_rejected(self):
        packet=seed(self.store,self.cfg);entry=packet['evidence'][0];entry['text']='归母净利润\n同比 增长 10.25%'
        result=research_model(packet)();result['stocks'][0]['facts']=[{'evidence_id':entry['evidence_id'],'quote':'归母净利润同比增长10.25%'}]
        validate_result(result,packet)
        self.assertEqual(result['stocks'][0]['facts'][0]['quote'],entry['text'])
        result['stocks'][0]['facts'][0]['quote']='归母净利润同比增长11.25%'
        with self.assertRaises(ValueError):validate_result(result,packet)

    def test_writer_receives_exact_reference_prices_in_yuan(self):
        packet=seed(self.store,self.cfg);packet['stocks'][0]['features']['unadjusted']['ma20_cents']=129380
        view=model_packet(packet,{**self.cfg,'paper_entry_band_bps':100})
        self.assertEqual(view['给定参考价位']['买入观察上限'],'1306.73元')

    def test_overview_uses_readable_existing_judgement_and_is_bounded(self):
        stock={'symbol':SYMBOL,'analysis':'收入增长，现金回款承压。需要继续观察。',
               'decision':{'inclination':'盈利仍需观察。第二句话不重复展开。'}}
        plan={'payload':{},'research':{'stocks':[stock]}}
        self.assertEqual(trader_report(plan,SYMBOL)['overview'],'盈利仍需观察；第二句话不重复展开')
        stock['decision']['inclination']='features为空，paper_baseline_v1未就绪。'
        self.assertEqual(trader_report(plan,SYMBOL)['overview'],'收入增长，现金回款承压；需要继续观察')
        stock['decision']['inclination']='现金回款仍需持续观察'*30
        self.assertLessEqual(len(trader_report(plan,SYMBOL)['overview']),110)

    def test_legacy_prose_does_not_publish_second_set_of_rounded_prices(self):
        plan={'payload':{},'research':{'stocks':[{'symbol':SYMBOL,'analysis':'等待利润改善。现价低于27.64—28.20元观察区间；20日均价为27.92元。公司收入增长10%，归母净利润下降2%。'}]}}
        text=trader_report(plan,SYMBOL)['analysis']
        self.assertNotIn('28.20',text);self.assertIn('公司收入增长10%',text)

    def test_legacy_source_and_model_failures_backfill_once_without_coverage_noise(self):
        packet=seed(self.store,self.cfg)
        study(self.store,self.cfg,packet,use_model=False,at='2026-09-15T09:01:00+08:00')
        self.store.check('r','tencent_daily',SYMBOL,'FAILED',"KeyError: 'qfqday'")
        self.store.check('r','official_news','MARKET','PARTIAL','15条标题；错误[]')
        with self.store.db:
            self.store.db.execute('DELETE FROM data_attempts')
            self.store.db.execute("DELETE FROM metadata WHERE key IN ('data_attempts_backfilled','study_failures_backfilled')")
        self.store.close();self.store=Store(self.tmp.name)
        self.assertEqual({x['label'] for x in self.failures()},{'历史价格与均线','研究报告生成'})
        count=self.store.db.execute('SELECT count(*) FROM data_attempts').fetchone()[0]
        self.store.close();self.store=Store(self.tmp.name)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM data_attempts').fetchone()[0],count)

    def test_one_stock_packet_failure_does_not_stop_other_stock(self):
        self.cfg['watchlist'].append({'symbol':'sh600000','name':'第二只'})
        def packet(store,config,sym,*args,**kwargs):
            if sym==SYMBOL:raise ValueError('资料超过预算')
            return make_snapshot(store,config,sym,*args,**kwargs)
        with patch('ashare.workflow.make_snapshot',side_effect=packet):out=execute(self.cfg,'research',use_model=False)
        self.assertEqual(len(out),2);self.assertEqual(self.store.db.execute('SELECT count(*) FROM studies').fetchone()[0],1)
        self.assertEqual(self.failures()[0]['label'],'研究资料整理')


if __name__=='__main__':unittest.main()
