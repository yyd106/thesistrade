import copy
import json
import tempfile
import unittest
from datetime import date,timedelta
from pathlib import Path
from unittest.mock import patch

from ashare import fundamentals as f,market_context as market,materiality
from ashare.sources import select_announcements
from ashare.storage import Store,normalize_time
from test_config import load_config
from ashare.demo import seed,SYMBOL,research_model
from ashare.research import make_snapshot,price_plan,study,model_packet
from ashare.model import validate_result


class DecisionDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.cfg=load_config(Path(__file__).resolve().parents[1]/'config.json')
        self.cfg.update(data_dir=self.tmp.name,watchlist=[{'symbol':SYMBOL,'name':'美的集团'}])
        self.stamp='2026-09-15T09:00:00+08:00'

    def tearDown(self):self.store.close();self.tmp.cleanup()

    def doc(self,title,body=None,kind='company_report',symbol=SYMBOL,url=None):
        return self.store.add_document(symbol=symbol,kind=kind,title=title,source='fixture',
            url=url or 'https://example.test/'+title,published_at=self.stamp,first_seen_at=self.stamp,ready_at=self.stamp,
            pages=[(1,body or title)],raw_path='fixture',cloud_allowed=True)[0]

    def plan(self,p):return price_plan(p,self.cfg,{'action':'WATCH'},'SUCCEEDED')

    def test_prefixed_reports_missing_queue_and_separate_revision_budget(self):
        title='迈威生物2026 年半年度报告'
        self.assertTrue(materiality.periodic(title));self.assertFalse(materiality.periodic(title+'摘要'))
        announcements=[{'announcementTitle':t,'adjunctUrl':str(i)+'.pdf','announcementTime':i} for i,t in enumerate([title,'公司章程','重大合同公告'])]
        self.doc(title,url='https://static.cninfo.com.cn/0.pdf')
        self.cfg.update(pdf_downloads_per_stock=1,pdf_revision_checks_per_stock=0)
        chosen=select_announcements(self.store,announcements,SYMBOL,self.cfg)
        self.assertEqual(chosen[0]['announcementTitle'],'重大合同公告')
        self.cfg['pdf_revision_checks_per_stock']=1
        chosen=select_announcements(self.store,announcements,SYMBOL,self.cfg)
        self.assertEqual([r['announcementTitle'] for r in chosen],['重大合同公告',title])

    def test_failed_top_item_does_not_starve_unattempted_peer(self):
        rows=[{'announcementTitle':t,'adjunctUrl':str(i)+'.pdf','announcementTime':i} for i,t in enumerate(['重大合同甲','重大合同乙'])]
        self.store.record_attempt('cninfo_pdf',SYMBOL,'FAILED','timeout',resource_key='https://static.cninfo.com.cn/1.pdf')
        chosen=select_announcements(self.store,rows,SYMBOL,{**self.cfg,'pdf_downloads_per_stock':1})
        self.assertEqual(chosen[0]['announcementTitle'],'重大合同甲')

    def test_optional_missing_body_does_not_block_but_key_report_does(self):
        p=seed(self.store,self.cfg)
        optional=self.doc('公司章程',kind='announcement_metadata')
        p=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:02:00+08:00')
        self.assertNotIn('UNREAD_DOCUMENT:'+optional,self.plan(p)['blockers'])
        key=self.doc('美的集团2026年半年度报告',kind='announcement_metadata')
        p=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:03:00+08:00')
        self.assertIn('UNREAD_DOCUMENT:'+key,self.plan(p)['blockers'])

    def test_core_sections_before_appendix_and_optional_backlog_does_not_gate(self):
        self.cfg['max_packet_chars']=6000
        seed(self.store,self.cfg)
        did=self.doc('美的集团2026年半年度报告',body='程序性附录说明。'*1800+'\n营业总收入增长，经营活动现金流改善。')
        p=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:02:00+08:00')
        self.assertTrue(any('经营活动现金流' in e['text'] for e in p['evidence']))
        cov=next(m for m in p['mandatory_coverage'] if m['doc_id']==did)
        self.assertGreater(cov['pending_chunks'],0);self.assertEqual(cov['required_pending_chunks'],0)
        self.assertNotIn('UNREAD_DOCUMENT:'+did,self.plan(p)['blockers'])

    def test_dividend_and_adverse_events_have_distinct_blocks(self):
        seed(self.store,self.cfg)
        div=self.doc('2026年半年度权益分派实施公告');risk=self.doc('关于重大诉讼的公告')
        p=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:02:00+08:00');b=self.plan(p)['blockers']
        self.assertIn('CORPORATE_ACTION_UNVERIFIED:'+div,b);self.assertIn('UNRESOLVED_EVENT:'+risk,b)

    def test_routine_documents_do_not_take_budget_before_core_sections(self):
        seed(self.store,self.cfg);self.cfg['max_packet_chars']=14000
        for i in range(25):self.doc(f'公司章程第{i}次修订',body='一般章程说明。'*100)
        key=self.doc('美的集团2026年半年度报告',body='营业收入及经营现金流分析。'*400)
        p=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:02:00+08:00')
        core=[e for e in p['evidence'] if e['doc_id']==key]
        self.assertGreaterEqual(len(core),2)
        order=[e['doc_id'] for e in p['evidence']]
        first_routine=next((i for i,e in enumerate(p['evidence']) if '公司章程' in e['title']),len(order))
        self.assertTrue(all(i<first_routine for i,e in enumerate(p['evidence']) if e['doc_id']==key))

    def test_financial_and_comparison_gaps_fail_closed(self):
        p=seed(self.store,self.cfg);p['company_dossier']['status']='PARTIAL'
        p['stocks'][0]['features']['market_context']['缺口']=['同行行情过时']
        b=self.plan(p)['blockers'];self.assertIn('FINANCIAL_BASELINE_INCOMPLETE',b);self.assertIn('MARKET_CONTEXT_INCOMPLETE',b)
        p['stocks'][0]['features']['market_context'].update({'缺口':[],'截至交易日':'2026-09-11'})
        self.assertIn('MARKET_CONTEXT_INCOMPLETE',self.plan(p)['blockers'])

    def test_quarterly_and_trailing_require_adjacent_periods_and_use_correct_year(self):
        periods={p:{'values':{'net_profit':v}} for p,v in [('2025-03-31',10),('2025-06-30',30),('2025-09-30',60),('2025-12-31',100),('2026-03-31',15),('2026-06-30',40)]}
        self.assertEqual(f.quarterly(periods,'2025-12-31','net_profit'),40)
        self.assertEqual(f.trailing(periods,'2026-06-30','net_profit'),110)
        del periods['2025-09-30'];self.assertIsNone(f.trailing(periods,'2026-06-30','net_profit'))
        self.assertIsNone(f.yoy(20,0));self.assertIsNone(f.yoy(20,-10));self.assertEqual(f.yoy(20,10),100)

    def test_financial_parser_rejects_wrong_symbol_currency_nonfinite_and_future(self):
        row={'SECUCODE':'000333.SZ','CURRENCY':'CNY','REPORT_DATE':'2026-06-30 00:00:00','NOTICE_DATE':'2026-08-29 00:00:00','TOTALOPERATEREVE':100}
        def parse(r):return f.parse_response(json.dumps({'success':True,'result':{'data':[r]}}),SYMBOL,'main',normalize_time(self.stamp))
        self.assertEqual(parse(row)['2026-06-30']['values']['revenue'],100)
        for bad in ({**row,'SECUCODE':'600519.SH'},{**row,'CURRENCY':'USD'},{**row,'TOTALOPERATEREVE':'NaN'},{**row,'NOTICE_DATE':'2026-09-16 00:00:00'}):
            with self.assertRaises(ValueError):parse(bad)
        self.assertIsNone(parse(row)['2026-06-30']['values']['net_profit'])

    def test_financial_statement_balance_and_version_hash(self):
        groups={'balance':{'2026-06-30':{'notice_date':'2026-08-29','updated_date':'2026-08-29','values':{'assets':100,'liabilities':40,'equity':50}}}}
        with self.assertRaises(ValueError):f.build_payload(SYMBOL,groups,{'balance':{}},normalize_time(self.stamp))
        groups['balance']['2026-06-30']['values']['equity']=60
        p=f.build_payload(SYMBOL,groups,{'balance':{}},normalize_time(self.stamp));self.assertEqual(p['status'],'PARTIAL')
        before=f.document_pages(p);p['gaps'].append('来源修订待核验');self.assertNotEqual(before,f.document_pages(p))

    def test_valid_financial_collection_clears_prior_assembly_failure(self):
        from ashare.presentation import outstanding_failures
        packet=seed(self.store,self.cfg)
        self.store.record_attempt('financials',SYMBOL,'FAILED','资产负债表勾稽关系不成立')
        row={'SECUCODE':'000333.SZ','CURRENCY':'CNY','REPORT_DATE':'2026-06-30','NOTICE_DATE':'2026-08-29',
             'TOTALOPERATEREVE':100,'PARENTNETPROFIT':10,'KCFJCXSYJLR':9,'NETCASH_OPERATE':8,
             'TOTAL_ASSETS':100,'TOTAL_LIABILITIES':40,'TOTAL_EQUITY':60,'MONETARYFUNDS':20}
        raw=json.dumps({'success':True,'result':{'data':[row]}}).encode()
        with patch('ashare.sources.fetch',return_value=raw):f.collect(self.store,packet['batch_id'],SYMBOL)
        self.assertFalse(any(r['label']=='公司财务底稿' for r in outstanding_failures(self.store,SYMBOL)))

    def series(self):
        bars=[[(date(2026,5,1)+timedelta(days=i)).isoformat(),str(10+i*.1),str(10+i*.1),str(10.2+i*.1),str(9.8+i*.1),str(100+i)] for i in range(70)]
        return market.parse(json.dumps({'data':{SYMBOL:{'qfqday':bars}}}),SYMBOL,'2026-09-15')

    def test_market_features_and_same_date_comparison(self):
        s=self.series();v=market.features(s)
        self.assertGreater(v['近20日涨跌幅（%）'],0);self.assertGreater(v['14日平均真实波幅占收盘价（%）'],0)
        self.assertEqual(market.comparison(s,s,20)['个股超额（百分点）'],0)
        stale=copy.deepcopy(s);stale['bars'].pop();self.assertIsNone(market.comparison(s,stale,20))
        plain=copy.deepcopy(s);plain['basis']='UNADJUSTED';self.assertIsNone(market.comparison(s,plain,20))

    def test_comparison_cache_recovers_transient_failure_without_future_or_stale_data(self):
        packet=seed(self.store,self.cfg);run=packet['batch_id'];series=self.series()
        code=market.BENCHMARK['symbol'];series.update(symbol=code,raw_path='original.json')
        ready='2026-09-15T01:00:00+00:00'
        self.store.db.execute('INSERT OR REPLACE INTO comparison_series VALUES(?,?,?,?)',(run,code,ready,json.dumps(series)))
        self.store.db.commit()
        cfg={**self.cfg,'comparison_peers':{SYMBOL:[]}}
        own_bars=[[b['date'],b['open'],b['close'],b['high'],b['low'],b['volume']] for b in self.series()['bars']]
        raw=json.dumps({'data':{SYMBOL:{'qfqday':own_bars}}})
        def assemble(at):return market.assemble(self.store,cfg,'failed-new-batch',SYMBOL,raw,'2026-09-15',at=at)['市场与行业对照'][0]
        self.store.record_attempt('comparison_series','MARKET','FAILED','network',resource_key=code)
        row=assemble(ready)
        self.assertIsNotNone(row['60日']);self.assertEqual(row['使用方式'],'复用同交易日历史资料')
        self.assertEqual(row['获取时间'],ready);self.assertEqual(row['来源批次'],run)
        self.assertIsNone(assemble('2026-09-15T00:59:59+00:00')['60日'])
        # The failed fetch remains visible, rather than being forged into a success.
        self.assertEqual(self.store.db.execute("SELECT status FROM data_attempts WHERE resource_key=? ORDER BY id DESC LIMIT 1",(code,)).fetchone()[0],'FAILED')
        for bad in ('stale','basis','symbol'):
            changed=copy.deepcopy(series)
            if bad=='stale':changed['bars'].pop()
            if bad=='basis':changed['basis']='UNADJUSTED'
            if bad=='symbol':changed['symbol']='sz000001'
            self.store.db.execute('UPDATE comparison_series SET payload_json=? WHERE run_id=? AND symbol=?',(json.dumps(changed),run,code))
            self.assertIsNone(assemble(ready)['60日'],bad)

    def test_current_incomparable_revision_does_not_fall_back_to_older_good_prices(self):
        packet=seed(self.store,self.cfg);older=packet['batch_id'];code=market.BENCHMARK['symbol']
        series=self.series();series.update(symbol=code,raw_path='original.json')
        self.store.db.execute('INSERT OR REPLACE INTO comparison_series VALUES(?,?,?,?)',(older,code,'2026-09-15T01:00:00+00:00',json.dumps(series)))
        self.store.db.execute("INSERT INTO runs(id,job_key,kind,started_at,status) VALUES('new','new','collect','2026-09-15T01:01:00+00:00','OK')")
        stale=copy.deepcopy(series);stale['bars'].pop()
        self.store.db.execute('INSERT INTO comparison_series VALUES(?,?,?,?)',('new',code,'2026-09-15T01:01:00+00:00',json.dumps(stale)))
        raw=json.dumps({'data':{SYMBOL:{'qfqday':[[b['date'],b['open'],b['close'],b['high'],b['low'],b['volume']] for b in series['bars']]}}})
        cfg={**self.cfg,'comparison_peers':{SYMBOL:[]}}
        for run in ('new','third-missing-batch'):
            ctx=market.assemble(self.store,cfg,run,SYMBOL,raw,'2026-09-15',at='2026-09-15T01:02:00+00:00')
            self.assertIsNone(ctx['市场与行业对照'][0]['60日'])

    def test_entry_band_is_versioned_per_plan_and_does_not_remove_other_gates(self):
        p=seed(self.store,self.cfg);cfg={**self.cfg,'paper_entry_band_bps':200}
        plan=price_plan(p,cfg,{'action':'WATCH'},'SUCCEEDED')
        ma=p['stocks'][0]['features']['unadjusted']['ma20_cents']
        self.assertEqual(plan['levels']['buy_low_cents'],ma*98//100)
        self.assertEqual(plan['levels']['buy_high_cents'],ma*102//100)
        self.assertEqual(plan['risk_parameters']['paper_entry_band_bps'],200)
        self.assertIn('上下2%',model_packet(p,cfg)['模拟交易规则'])
        p['stocks'][0]['features']['unadjusted']['ma60_cents']=ma+1
        p['company_dossier']['status']='PARTIAL'
        blocked=price_plan(p,cfg,{'action':'REVIEW_REQUIRED'},'SUCCEEDED')
        for code in ['TREND_NOT_CONFIRMED','FINANCIAL_BASELINE_INCOMPLETE','RESEARCH_VETO']:
            self.assertIn(code,blocked['blockers'])
        self.assertEqual(blocked['kind'],'NO_ENTRY')
        from ashare.settings import validate_settings
        for value in [True,0,301]:
            with self.assertRaises(ValueError):validate_settings({**cfg,'paper_entry_band_bps':value})
        with self.assertRaises(ValueError):validate_settings({**cfg,'paper_stop_loss_bps':200})

    def test_completed_daily_bar_excludes_intraday_and_includes_settled_close(self):
        from ashare.calendar import completed_bar_cutoff,last_completed_day
        self.assertEqual(last_completed_day('2026-09-18T14:30:00+08:00'),'2026-09-17')
        self.assertEqual(completed_bar_cutoff('2026-09-18T15:04:59+08:00'),'2026-09-18')
        self.assertEqual(completed_bar_cutoff('2026-09-18T15:05:00+08:00'),'2026-09-19')
        self.assertEqual(last_completed_day('2026-09-19T16:00:00+08:00'),'2026-09-18')

    def test_background_failures_are_visible_without_repeating_on_unrelated_stocks(self):
        from ashare.presentation import outstanding_failures
        for source,key,title in [('external_news_list','un','公共新闻目录'),
                ('external_news_article','a','红海航运受阻'),
                ('external_news_article','b','家电以旧换新政策发布'),
                ('comparison_series','sz000651','格力电器'),
                ('comparison_series','sz000858','五粮液')]:
            self.store.record_attempt(source,'MARKET','FAILED','timeout',resource_key=key,title=title)
        self.store.record_attempt('market_comparison',SYMBOL,'PARTIAL','口径缺口')
        shown=outstanding_failures(self.store,SYMBOL,self.cfg)
        self.assertEqual({r['title'] for r in shown},{'家电以旧换新政策发布','格力电器'})
        self.assertEqual(len(outstanding_failures(self.store,'MARKET')),5)

    def test_five_questions_require_cited_evidence(self):
        p=seed(self.store,self.cfg);e=p['evidence'][0];result=research_model(p)();s=result['stocks'][0]
        s['facts']=[{'evidence_id':e['evidence_id'],'quote':e['text'][:20]}]
        s['decision']={'inclination':'观察','key_evidence':[{'evidence_id':e['evidence_id'],'implication':'等待核实经营改善'}],
            'pricing':'缺少预期，无法确认定价程度','trigger':'等待下一期现金流','invalidation':'现金流恶化'}
        validate_result(result,p)
        s['decision']['key_evidence'][0]['evidence_id']='unknown'
        with self.assertRaises(ValueError):validate_result(result,p)

    def test_background_does_not_invalidate_plan_or_return_through_memory(self):
        p=seed(self.store,self.cfg)
        study(self.store,self.cfg,p,model_fn=research_model(p),at='2026-09-15T09:01:00+08:00')
        self.doc('碧根果反倾销','碧根果进口反倾销调查',symbol='MARKET',kind='news')
        self.doc('红海航运风险','国际航运与战争风险',symbol='MARKET',kind='news')
        p=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:03:00+08:00')
        text=json.dumps(model_packet(p,self.cfg),ensure_ascii=False)
        self.assertNotIn('碧根果',text);self.assertNotIn('红海',text);self.assertEqual(len(p['background_events']),2)


if __name__=='__main__':unittest.main()
