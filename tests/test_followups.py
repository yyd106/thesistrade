import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ashare.storage import Store,normalize_time
from test_config import load_config
from ashare.followups import reconcile,view,next_market,build
from ashare.scheduler import enqueue
from ashare.demo import SYMBOL,seed,research_model
from ashare.research import study


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.cfg=load_config(Path(__file__).resolve().parents[1]/'config.json')
        self.cfg.update(data_dir=self.tmp.name,watchlist=[{'symbol':SYMBOL,'name':'测试股票'}])
        self.at=normalize_time('2026-09-18T16:00:00+08:00')

    def tearDown(self):self.store.close();self.tmp.cleanup()

    def fail(self,resource='one',source='cninfo_pdf',symbol=SYMBOL,at=None,status='FAILED',detail='网络连接失败'):
        self.store.record_attempt(source,symbol,status,detail,resource_key=resource,title='半年度报告',at=at or self.at)

    def test_exact_recovery_keeps_other_failure_and_daily_history(self):
        self.fail();self.fail('two');first=reconcile(self.store,self.cfg,self.at)
        self.assertEqual(first['failed_today'],2)
        failure_ids={x['key']:x['id'] for x in first['items'] if x['key'].startswith('failure:')}
        self.fail(status='OK',at='2026-09-18T16:01:00+08:00')
        result=reconcile(self.store,self.cfg,'2026-09-18T16:02:00+08:00')
        self.assertEqual(result['failed_today'],2);self.assertEqual(result['recovered_today'],1)
        self.assertEqual(len([x for x in result['items'] if x['key'].startswith('failure:')]),1)
        one=failure_ids['failure:'+SYMBOL+':cninfo_pdf:one']
        self.assertEqual(self.store.db.execute('SELECT status FROM followup_items WHERE id=?',(one,)).fetchone()[0],'CLEARED')

    def test_latest_attempt_uses_observation_time_and_excludes_future_recovery(self):
        self.fail(at='2026-09-18T15:59:00+08:00')
        self.fail(status='OK',at='2026-09-18T16:01:00+08:00')
        # Backfilled records can have higher IDs than newer observations.
        self.fail(status='OK',at='2026-09-18T15:58:00+08:00')
        key='failure:'+SYMBOL+':cninfo_pdf:one'
        items,_=build(self.store,self.cfg,self.at)
        self.assertIn(key,{x['key'] for x in items})
        # At the same timestamp the later record takes precedence.
        self.fail(status='OK',at='2026-09-18T15:59:00+08:00')
        items,_=build(self.store,self.cfg,self.at)
        self.assertNotIn(key,{x['key'] for x in items})

    def test_overnight_restart_preserves_owner_plan_first_seen_and_recurrence(self):
        self.fail();first=reconcile(self.store,self.cfg,self.at)
        original=next(x for x in first['items'] if x['key'].startswith('failure:'))
        self.store.close();self.store=Store(self.tmp.name)
        second=reconcile(self.store,self.cfg,'2026-09-19T08:00:00+08:00')
        item=next(x for x in second['items'] if x['id']==original['id'])
        self.assertEqual(item['first_seen_at'],self.at);self.assertGreater(second['carried_over'],0)
        self.assertTrue(item['next_action']);self.assertTrue(item['trigger']);self.assertTrue(item['completion'])
        self.assertEqual(view(self.store,'2026-09-19T08:00:30+08:00')['history'][0]['day'],'2026-09-18')
        self.fail(status='OK',at='2026-09-19T08:01:00+08:00');reconcile(self.store,self.cfg,'2026-09-19T08:02:00+08:00')
        self.fail(at='2026-09-19T08:03:00+08:00');third=reconcile(self.store,self.cfg,'2026-09-19T08:04:00+08:00')
        reopened=next(x for x in third['items'] if x['id']==original['id'])
        self.assertEqual(reopened['occurrences'],2);self.assertEqual(reopened['first_seen_at'],self.at)

    def test_exhausted_budget_has_escalation_and_regular_schedule_not_dead_end(self):
        for i in range(2):
            jid=enqueue(self.store,'repair',self.at,'repair:2026-09-18:'+SYMBOL+':'+str(i),payload={'symbol':SYMBOL})
            with self.store.db:self.store.db.execute("UPDATE jobs SET status='DONE' WHERE id=?",(jid,))
        self.fail()
        result=reconcile(self.store,self.cfg,self.at)
        failure=next(x for x in result['items'] if x['key'].startswith('failure:'))
        self.assertEqual(failure['state'],'ESCALATED');self.assertIn('定时研究',failure['next_action'])
        self.assertEqual(failure['next_action_at'],normalize_time('2026-09-18T20:00:00+08:00'))
        self.assertIn('2/2',failure['escalation']);self.assertEqual(self.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0],2)

    def test_login_ocr_and_background_failure_have_different_owners(self):
        self.fail(source='research_analysis',detail='需要订阅登录')
        self.fail('scan',detail='PDF文字不足，需OCR')
        self.fail('news',source='official_news',symbol='MARKET')
        items,_=build(self.store,self.cfg,self.at)
        bykey={x['key']:x for x in items}
        auth=bykey['failure:'+SYMBOL+':research_analysis:one'];self.assertEqual(auth['owner'],'USER');self.assertIsNone(auth['next_action_at'])
        self.assertEqual(bykey['failure:'+SYMBOL+':cninfo_pdf:scan']['owner'],'USER')
        self.assertIn('不直接',bykey['failure:MARKET:official_news:news']['impact'])

    def test_program_gap_and_market_wait_are_not_pointless_download_retries(self):
        p=seed(self.store,self.cfg);study(self.store,self.cfg,p,model_fn=research_model(p),at=p['as_of'])
        def g(key):return {'key':key,'title':'标题','why':'原因','waiting':'等待条件','user_action':'用户动作','release':'完成条件','documents':[]}
        unknown=g('UNKNOWN_RULE');unknown['title']='交易条件仍需核对'
        with patch('ashare.followups.trade_guidance',return_value={'groups':[g('PRICE_DISCONTINUITY'),g('TREND_NOT_CONFIRMED'),unknown]}):
            items,_=build(self.store,self.cfg,self.at)
        issue=next(x for x in items if x['key'].endswith('PRICE_DISCONTINUITY'))
        self.assertEqual(issue['owner'],'ENGINEERING');self.assertIsNone(issue['next_action_at']);self.assertIsNone(issue['run'])
        self.assertIn('没有后台自动修代码',issue['next_action'])
        trend=next(x for x in items if x['key'].endswith('TREND_NOT_CONFIRMED'))
        self.assertEqual(trend['owner'],'MARKET');self.assertIsNone(trend['run'])
        self.assertEqual(next(x for x in items if x['key'].endswith('UNKNOWN_RULE'))['owner'],'ENGINEERING')

    def test_paused_scheduler_does_not_promise_next_automatic_attempt(self):
        self.cfg['scheduler_enabled']=False;self.fail();result=reconcile(self.store,self.cfg,self.at)
        self.assertTrue(any(x['key']=='service:paused' for x in result['items']))
        for x in result['items']:
            self.assertIsNone(x['next_action_at']);self.assertEqual(x['owner'],'USER')

    def test_failed_job_not_cleared_by_another_stock_success(self):
        for sym,status in [(SYMBOL,'FAILED'),('sh600519','DONE')]:
            jid=enqueue(self.store,'repair',self.at,payload={'symbol':sym})
            with self.store.db:self.store.db.execute('UPDATE jobs SET status=?,finished_at=? WHERE id=?',(status,self.at,jid))
        result=reconcile(self.store,self.cfg,self.at)
        self.assertTrue(any(x['key']=='job:repair:'+SYMBOL for x in result['items']))
        self.assertEqual(len(result['today_jobs']),1)

    def test_calendar_and_stale_report_are_explicit_and_no_trade_mutation(self):
        self.assertEqual(next_market(self.at,30),normalize_time('2026-09-21T09:30:00+08:00'))
        self.assertIsNone(next_market('2027-01-04T09:30:00+08:00'))
        reconcile(self.store,self.cfg,self.at)
        self.assertTrue(view(self.store,'2026-09-18T16:03:00+08:00')['stale'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],0)
        self.assertTrue((Path(self.tmp.name)/'workflow/followups/2026-09-18.json').exists())

    def test_queued_retry_does_not_claim_failed_job_recovered(self):
        old=enqueue(self.store,'review',self.at)
        with self.store.db:self.store.db.execute("UPDATE jobs SET status='FAILED' WHERE id=?",(old,))
        enqueue(self.store,'review','2026-09-18T16:01:00+08:00')
        result=reconcile(self.store,self.cfg,'2026-09-18T16:02:00+08:00')
        item=next(x for x in result['items'] if x['key']=='job:review:MARKET')
        self.assertEqual(item['state'],'RUNNING');self.assertIn('不算恢复',item['trigger'])

    def test_unsupported_board_is_routed_before_any_order_is_attempted(self):
        # B shares remain outside the supported boards; STAR and ChiNext are supported.
        self.cfg['watchlist']=[{'symbol':'sh900901','name':'测试B股'},{'symbol':'sh688062','name':'测试科创板'}]
        result=reconcile(self.store,self.cfg,self.at)
        self.assertFalse(any(x['key']=='capability:sh688062' for x in result['items']))
        item=next(x for x in result['items'] if x['key']=='capability:sh900901')
        self.assertEqual(item['owner'],'ENGINEERING');self.assertIsNone(item['next_action_at'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],0)


if __name__=='__main__':unittest.main()
