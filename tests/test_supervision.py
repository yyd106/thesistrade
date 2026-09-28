import json
import tempfile
import subprocess
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from ashare import evaluation_batches as batches, governance, supervision as s, model
from ashare.storage import Store, normalize_time
from test_config import load_config

AT = '2026-09-28T08:00:00+00:00'


def answer(packet, verdict='INSUFFICIENT'):
    return {'verdict': verdict, 'summary': '独立样本不足，继续观察，不能据此修改策略。',
            'checks': [{'id': k, 'status': 'PASS' if verdict=='RECOMMEND' else 'UNKNOWN',
                        'reason': '本批次资料不足，未断言有效。', 'evidence_refs': ['manifest']} for k in s.CHECKS],
            'counterexamples': ['收益可能来自市场整体上涨。'], 'effectiveness': [], 'next_steps': ['等待前向样本到期。']}


class SupervisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cfg = load_config(Path(__file__).resolve().parents[1] / 'config.json')
        self.cfg.update(data_dir=self.tmp.name, deployment_role='research', model_enabled=True)
        self.bid = batches.start(self.store, self.cfg, at=AT)['id']

    def tearDown(self):
        self.store.close();self.tmp.cleanup()

    def proposal(self, status='READY'):
        payload = {k: '固定的检验标准和方案：'+k for k in s.FIELDS}
        payload['change'] = 'AUTHOR_ONLY_MARKER 修改提案'
        with self.store.db:
            pid = governance.draft_proposal(self.store, source='manual', kind='RULE', target='趋势过滤', title='试验提案', payload=payload, at=AT)
        governance.decide(self.store, pid, 'READY', decided_by=None, note='方案完整', at=AT)
        if status == 'ADOPTED':
            governance.decide(self.store, pid, 'APPROVED', decided_by='Dean synthetic test', note='测试批准', at=AT)
            governance.decide(self.store, pid, 'ADOPTED', decided_by='Dean synthetic test', note='版本 test-build', at=AT)
        return pid

    def runner(self, prompts, verdict='INSUFFICIENT'):
        def run(prompt, schema, folder, timeout):
            prompts.append((prompt, folder))
            packet = json.loads(prompt.split('<UNTRUSTED_SUMMARIES>')[1].split('</UNTRUSTED_SUMMARIES>')[0])
            return answer(packet, verdict)
        return run

    def test_two_fresh_sessions_frozen_versions_and_no_production_writes(self):
        pid = self.proposal()
        batches.note(self.store, self.bid, 'AUTHOR_NOTE_MARKER 作者认为应该立即改变趋势过滤策略。')
        rid = s.request(self.store, self.cfg, 'PROPOSAL', pid, at=AT)
        before = dict(self.store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (pid,)).fetchone())
        original = batches.show(self.store, self.bid)['integrity']
        prompts = []
        result = s.run(self.store, self.cfg, rid, model_fn=self.runner(prompts, 'RECOMMEND'))
        self.assertEqual(result['status'], 'SUCCEEDED')
        self.assertEqual(len(prompts), 2)
        self.assertNotIn('AUTHOR_ONLY_MARKER', prompts[0][0]);self.assertNotIn('AUTHOR_NOTE_MARKER', prompts[0][0])
        self.assertIn('AUTHOR_ONLY_MARKER', prompts[1][0]);self.assertIn('AUTHOR_NOTE_MARKER', prompts[1][0])
        self.assertNotEqual(prompts[0][1], prompts[1][1])
        self.assertEqual(before, dict(self.store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (pid,)).fetchone()))
        self.assertEqual(original, batches.show(self.store, self.bid)['integrity'])
        for table in ('strategy_guidance', 'paper_flows', 'paper_orders', 'cloud_receipts'):
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM '+table).fetchone()[0], 0)
        self.assertIsNone(self.store.db.execute("SELECT value FROM cloud_state WHERE key='research_completed_at'").fetchone())
        self.assertEqual(s.listing(self.store)[0]['approval'], 'WAITING_USER')
        self.assertEqual(s.run(self.store, self.cfg, rid, model_fn=lambda *a: self.fail('No repeated model call'))['status'], 'SUCCEEDED')

    def test_new_material_invalidates_previous_recommendation_and_keeps_attempt(self):
        pid=self.proposal();rid=s.request(self.store,self.cfg,'PROPOSAL',pid)
        s.run(self.store,self.cfg,rid,model_fn=self.runner([], 'RECOMMEND'))
        saved=list((self.store.root/'workflow'/'supervision'/rid).glob('*/result.json'))
        before=saved[0].read_bytes()
        with self.store.db:
            p=json.loads(self.store.db.execute('SELECT payload_json FROM strategy_proposals WHERE id=?',(pid,)).fetchone()[0])
            p['change']='另一个改动'
            self.store.db.execute('UPDATE strategy_proposals SET payload_json=? WHERE id=?',(json.dumps(p),pid))
        newer=s.request(self.store,self.cfg,'PROPOSAL',pid)
        self.assertNotEqual(rid,newer)
        self.assertEqual(self.store.db.execute('SELECT status FROM supervision_reviews WHERE id=?',(rid,)).fetchone()[0],'STALE')
        self.assertEqual(saved[0].read_bytes(),before)

    def test_notes_changed_while_reviewing_result_is_stale(self):
        rid=s.request(self.store,self.cfg,'BATCH',self.bid);calls=[]
        def runner(prompt,schema,folder,timeout):
            calls.append(folder)
            if len(calls)==2:batches.note(self.store,self.bid,'审查过程中新增作者说明，旧结果不能覆盖新材料。')
            return answer({})
        self.assertEqual(s.run(self.store,self.cfg,rid,model_fn=runner)['status'],'STALE')
        self.assertEqual(batches.listing(self.store)[0]['local_review']['status'],'STALE')
        self.assertEqual(batches.show(self.store,self.bid)['local_review']['id'],rid)

    def test_tamper_missing_file_and_symlink_refuse_model(self):
        folder=batches.folder(self.store,self.bid)
        file=folder/'report.json';text=file.read_text()
        file.write_text('{}')
        with self.assertRaisesRegex(ValueError,'哈希'):s.request(self.store,self.cfg,'BATCH',self.bid)
        file.write_text(text)
        file.unlink();file.symlink_to(folder/'quotes-health.json')
        with self.assertRaisesRegex(ValueError,'符号链接'):s.request(self.store,self.cfg,'BATCH',self.bid)
        s.discover(self.store,self.cfg)
        self.assertIn('符号链接',s.view(self.store)['discovery']['errors'][0])

    def test_retry_failure_limit_and_history(self):
        rid=s.request(self.store,self.cfg,'BATCH',self.bid,at=AT)
        def fail(*args):raise RuntimeError('订阅超时')
        for _ in range(3):
            self.assertEqual(s.run(self.store,self.cfg,rid,model_fn=fail,clock=lambda:AT)['status'],'DEFERRED')
        self.assertIsNone(s.next_pending(self.store,'2026-10-20T00:00:00+00:00'))
        self.assertEqual(len(list((self.store.root/'workflow'/'supervision'/rid).glob('*/result.json'))),3)
        s.retry(self.store,rid)
        self.assertIsNotNone(s.next_pending(self.store,AT))

    def test_research_priority_and_yield_does_not_exhaust_retry(self):
        rid=s.request(self.store,self.cfg,'BATCH',self.bid)
        with self.store.db:self.store.db.execute("INSERT INTO jobs(id,kind,scheduled_at,status) VALUES('test','research',?,'PENDING')",(AT,))
        self.assertEqual(s.run(self.store,self.cfg,rid,model_fn=lambda *a:self.fail())['status'],'WAITING_RESEARCH')
        with self.store.db:self.store.db.execute("UPDATE jobs SET status='DONE'")
        cancel=Event()
        def runner(*args):cancel.set();return answer({})
        self.assertTrue(s.run(self.store,self.cfg,rid,model_fn=runner,cancel_event=cancel)['yielded'])
        self.assertEqual(s.listing(self.store)[0]['attempts'],0)

    def test_server_restart_recovers_and_cloud_never_calls_model(self):
        rid=s.request(self.store,self.cfg,'BATCH',self.bid)
        with self.store.db:self.store.db.execute("UPDATE supervision_reviews SET status='RUNNING'")
        s.recover(self.store)
        self.assertEqual(s.listing(self.store)[0]['status'],'DEFERRED')
        cfg={**self.cfg,'deployment_role':'cloud'}
        with self.assertRaises(ValueError):s.run(self.store,cfg,rid,model_fn=lambda *a:self.fail())
        with self.assertRaises(ValueError):s.request(self.store,cfg,'BATCH',self.bid)
        self.assertEqual(s.discover(self.store,cfg),[])

    def test_unknown_citations_and_weak_statistics_fail_closed(self):
        packet=s.packet(s.snapshot(self.store,self.cfg,'BATCH',self.bid),'FACTS')
        r=answer(packet);r['checks'][0]['evidence_refs']=['private-document']
        with self.assertRaisesRegex(ValueError,'证据'):s.validate(r,packet)
        r=answer(packet,'RECOMMEND');r['checks'][0]['status']='UNKNOWN'
        with self.assertRaisesRegex(ValueError,'不能建议通过'):s.validate(r,packet)
        groups=packet['facts']['registry']['all_time']['groups']
        group=next(iter(groups))
        r=answer(packet);r['effectiveness']=[{'comparison':group,'conclusion':'SUPPORTED','reason':'声称有效'}]
        with self.assertRaisesRegex(ValueError,'样本不足'):s.validate(r,packet)
        r['effectiveness'][0]['conclusion']='UNKNOWN'
        s.validate(r,packet)

    def test_model_cancellation_targets_only_this_call_and_keeps_metadata(self):
        cancel=Event()
        class Process:
            pid=12345
            returncode=-15
            def communicate(self,*args,**kwargs):
                cancel.set();raise subprocess.TimeoutExpired('fake',1)
            def wait(self,timeout=None):return -15
        proc=Process();folder=self.store.root/'test-model-call'
        with patch('ashare.model.codex_executable',return_value='fake-codex'), \
             patch('ashare.model.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='ChatGPT',stderr='')), \
             patch('ashare.model.subprocess.Popen',return_value=proc), patch('ashare.model._signal') as signal:
            with self.assertRaises(model.ModelYield):
                model.run_json('合成资料',s.SCHEMA,folder,timeout=30,cancel_event=cancel)
            self.assertEqual(signal.call_count,1)
            self.assertIs(signal.call_args.args[0],proc)
        self.assertTrue(model.call_meta(folder)['yielded'])

    def test_repaired_materials_get_new_record_without_reviving_stale_history(self):
        rid=s.request(self.store,self.cfg,'BATCH',self.bid)
        s.run(self.store,self.cfg,rid,model_fn=self.runner([]))
        file=batches.folder(self.store,self.bid)/'report.json';before=file.read_text()
        file.write_text('{}');s.discover(self.store,self.cfg)
        self.assertEqual(s.listing(self.store)[0]['status'],'STALE')
        file.write_text(before)
        newer=s.request(self.store,self.cfg,'BATCH',self.bid)
        self.assertNotEqual(rid,newer)
        self.assertEqual(s.request(self.store,self.cfg,'BATCH',self.bid),newer)

    def test_new_batch_during_proposal_review_cannot_revive_old_recommendation(self):
        pid=self.proposal();rid=s.request(self.store,self.cfg,'PROPOSAL',pid)
        calls=[]
        def runner(*args):
            calls.append(1)
            if len(calls)==2:
                batches.start(self.store,self.cfg,force=True,at='2026-09-29T08:00:00+00:00')
                s.request(self.store,self.cfg,'PROPOSAL',pid)
            return answer({},'RECOMMEND')
        self.assertEqual(s.run(self.store,self.cfg,rid,model_fn=runner)['status'],'STALE')

    def test_discovery_dedupes_and_reviews_adopted_proposal_after_next_batch(self):
        pid=self.proposal();adopted=self.proposal('ADOPTED')
        s.discover(self.store,self.cfg,AT);s.discover(self.store,self.cfg,AT)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM supervision_reviews').fetchone()[0],2)
        newer=batches.start(self.store,self.cfg,force=True,at='2026-09-29T08:00:00+00:00')['id']
        s.discover(self.store,self.cfg,'2026-09-29T08:00:00+00:00')
        follow=next(r for r in s.listing(self.store) if r['kind']=='FOLLOWUP')
        self.assertEqual(follow['subject_id'],adopted);self.assertEqual(follow['batch_id'],newer)
        s.run(self.store,self.cfg,follow['id'],model_fn=self.runner([]))
        self.assertEqual(self.store.db.execute('SELECT status FROM strategy_proposals WHERE id=?',(adopted,)).fetchone()[0],'ADOPTED')

    def test_generic_external_identity_retains_claude_history(self):
        batches.record_check(self.store,self.bid,'旧 Claude 检查文本','reports:checks/old.md',at=AT)
        batches.record_check(self.store,self.bid,'新外部检查文本','reports:checks/new.md',reviewer='chatgpt',model='test-model',review_version='v2')
        folder=batches.folder(self.store,self.bid)
        self.assertEqual((folder/'claude-check.md').read_text(),'旧 Claude 检查文本')
        self.assertTrue((folder/'review-check.md').exists())
        self.assertEqual(len(list((folder/'check-history').glob('*.md'))),2)
        item=batches.listing(self.store)[0]
        self.assertTrue(item['review_check']);self.assertEqual(item['reviewer'],'chatgpt')
        self.assertFalse(item['claude_check'])

    def test_display_sync_never_renews_strategy_lease(self):
        from ashare.cloud_sync import handle
        # The authenticated route applies this display-only payload; no strategy heartbeat changes.
        with self.store.db:
            self.store.db.execute("INSERT INTO cloud_state VALUES('research_completed_at',?)",(json.dumps(AT),))
            self.store.db.execute("INSERT INTO cloud_state VALUES('display_reviews','[1]')")
        result=handle(self.store,self.cfg,'/api/sync/reviews',{'supervision':{'items':[]}},AT)
        self.assertEqual(result['status'],'ACCEPTED')
        self.assertEqual(json.loads(self.store.db.execute("SELECT value FROM cloud_state WHERE key='research_completed_at'").fetchone()[0]),AT)
        self.assertEqual(json.loads(self.store.db.execute("SELECT value FROM cloud_state WHERE key='display_reviews'").fetchone()[0]),[1])
        self.assertEqual(json.loads(self.store.db.execute("SELECT value FROM cloud_state WHERE key='display_supervision'").fetchone()[0]),{'items':[]})

    def test_scheduler_runs_supervision_only_after_research_is_idle(self):
        from ashare.scheduler import Scheduler
        from concurrent.futures import Future
        scheduler=object.__new__(Scheduler)
        scheduler.supervision_future=None;scheduler.supervision_cancel=Event();scheduler.last_supervision_scan=None
        class Pool:
            def __init__(self):self.calls=[]
            def submit(self,*args):self.calls.append(args);return Future()
        scheduler.supervision_pool=Pool()
        with self.store.db:self.store.db.execute("INSERT INTO jobs(id,kind,scheduled_at,status) VALUES('research-priority','cycle',?,'PENDING')",(AT,))
        scheduler.supervision_tick(self.store,self.cfg,AT)
        self.assertEqual(scheduler.supervision_pool.calls,[])
        with self.store.db:self.store.db.execute("UPDATE jobs SET status='DONE'")
        scheduler.supervision_tick(self.store,self.cfg,AT)
        self.assertEqual(len(scheduler.supervision_pool.calls),1)
        scheduler.supervision_tick(self.store,self.cfg,AT,offline=True)
        self.assertTrue(scheduler.supervision_cancel.is_set())


if __name__=='__main__':unittest.main()
