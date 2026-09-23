import copy
import tempfile
import unittest
from pathlib import Path
from ashare.storage import Store
from test_config import load_config
from ashare.guidance import trade_guidance


class TradeGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.cfg=load_config(Path(__file__).resolve().parents[1]/'config.json')
        self.symbol='sz002415';self.at='2026-09-18T08:00:00+08:00'
        self.packet={'mandatory_coverage':[],'document_manifest':[]}
        self.plan={'payload':{'kind':'NO_ENTRY','blockers':[]},'effective_status':'ACTIVE','activated_at':self.at}

    def tearDown(self):self.store.close();self.tmp.cleanup()

    def document(self,title,kind='announcement_metadata',url='https://static.cninfo.com.cn/test.pdf',cloud=True):
        did,_=self.store.add_document(symbol=self.symbol,kind=kind,title=title,source='cninfo',url=url,
            published_at='2026-09-17T09:00:00+08:00',first_seen_at='2026-09-17T09:00:00+08:00',
            ready_at='2026-09-17T09:01:00+08:00',pages=[(1,title+'，公告原文。')],raw_path='fixture',cloud_allowed=cloud)
        return did

    def groups(self):return trade_guidance(self.store,self.cfg,self.symbol,self.plan,self.packet,self.at)['groups']

    def test_dividend_explains_conditional_verification_without_mutating_the_old_plan(self):
        did=self.document('半年度权益分派实施公告',kind='company_report')
        self.plan['payload']['blockers']=['UNRESOLVED_EVENT:'+did]
        self.packet['mandatory_coverage']=[{'doc_id':did,'fulltext':True,'all_chunks_accounted':True}]
        before=copy.deepcopy(self.plan)
        g=self.groups()[0]
        self.assertIn('分红或除权',g['title']);self.assertNotIn('重大事件',g['title'])
        self.assertIn('这不代表',g['why']);self.assertIn('价格是否可比',g['waiting'])
        self.assertIn('重新核验',g['user_action']);self.assertIn('全部通过',g['release']);self.assertIn('正文已读',g['documents'][0]['state'])
        self.assertEqual(self.plan,before)

    def test_missing_body_recovery_changes_guidance_but_keeps_old_plan_blocked(self):
        did=self.document('公司经营公告')
        self.plan['payload']['blockers']=['UNREAD_DOCUMENT:'+did]
        self.packet['mandatory_coverage']=[{'doc_id':did,'fulltext':False}]
        missing=self.groups()[0]
        self.assertEqual(missing['key'],'missing');self.assertIn('不保证',missing['waiting'])
        self.assertEqual(missing['documents'][0]['url'],'https://static.cninfo.com.cn/test.pdf')
        self.document('公司经营公告',kind='company_report')
        recovered=self.groups()[0]
        self.assertEqual(recovered['key'],'updated');self.assertEqual(recovered['run'],'research')
        self.assertEqual(self.plan['payload']['blockers'],['UNREAD_DOCUMENT:'+did])

    def test_reading_backlog_does_not_ask_to_redownload_or_promise_one_click_completion(self):
        did=self.document('半年度报告',kind='company_report')
        self.plan['payload']['blockers']=['UNREAD_DOCUMENT:'+did]
        self.packet['mandatory_coverage']=[{'doc_id':did,'fulltext':True,'pending_chunks':200}]
        g=self.groups()[0]
        self.assertEqual(g['key'],'reading');self.assertIn('无需重新下载',g['user_action'])
        self.assertIn('不能承诺点击一次',g['release']);self.assertEqual(g['run'],'research')

    def test_pause_and_research_rejection_get_specific_instructions(self):
        self.cfg['scheduler_enabled']=False
        self.plan['payload']['blockers']=['MODEL_NOT_READY','RESEARCH_VETO']
        self.plan['research']={'stocks':[{'symbol':self.symbol,'next_checks':['关注下一期经营现金流能否改善。']}]}
        groups=self.groups()
        self.assertIn('已暂停',groups[0]['waiting']);self.assertNotIn('每天',groups[0]['waiting'])
        self.assertIn('经营现金流',groups[1]['waiting'])

    def test_private_permission_and_untrusted_urls_never_become_authorization_or_clickable_links(self):
        did=self.document('私人研究笔记',kind='broker_report',url='javascript:alert(1)',cloud=False)
        self.plan['payload']['blockers']=['LOCAL_ONLY_DOCUMENTS_UNREVIEWED','UNREAD_DOCUMENT:'+did]
        self.packet['document_manifest']=[{'version_id':did}]
        groups=self.groups()
        self.assertIn('不会因点击更新而自动授权',groups[0]['user_action'])
        for g in groups:
            for d in g['documents']:self.assertIsNone(d['url'])
        self.assertEqual(self.store.db.execute('SELECT cloud_allowed FROM documents WHERE id=?',(did,)).fetchone()[0],0)

    def test_expired_plan_and_unknown_code_remain_visible_without_raw_jargon(self):
        self.plan['payload']['blockers']=['FUTURE_INTERNAL_CODE:fixture']
        self.plan['effective_status']='EXPIRED'
        groups=self.groups()
        self.assertEqual(len(groups),2)
        self.assertNotIn('FUTURE_INTERNAL_CODE',' '.join(g['title']+g['why']+g['user_action'] for g in groups))
        self.assertIn('过期',groups[-1]['title'])


if __name__=='__main__':unittest.main()
