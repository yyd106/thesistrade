"""Isolated end-to-end display transport; no keys, network or model calls."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ashare import cloud_sync as sync, cloud_runtime as runtime, cloud_ledger as ledger, page_display as pages
from ashare.cloud_dashboard import status as cloud_status
from ashare.finance import PaperLedger
from ashare.storage import Store
from test_config import load_config
from test_review_presentation import insert_review_fixture
import test_macro_relevance as macro_fixtures

AT='2026-10-04T01:00:00+00:00'
LATER='2026-10-04T01:01:00+00:00'
OLD='2026-10-03T01:00:00+00:00'


class PageDisplaySyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.cloud_tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name); self.cloud=Store(self.cloud_tmp.name)
        self.cfg=load_config(Path(__file__).resolve().parents[1]/'config.json')
        self.cfg.update(data_dir=self.tmp.name,deployment_role='research',scheduler_enabled=False,model_enabled=False,industry_enabled=False)
        self.cloud_cfg={**self.cfg,'deployment_role':'cloud','data_dir':self.cloud_tmp.name}
        insert_review_fixture(self.store)
        macro_fixtures.MacroRelevanceTests.event(self,'fresh',published='2026-10-03T10:00:00+00:00')
        PaperLedger(self.cloud).initialize()
        with self.store.db:runtime.put(self.store,'remote_features',[pages.FEATURE])
        with self.cloud.db:
            runtime.put(self.cloud,'research_completed_at',OLD)
            runtime.put(self.cloud,'research_received_at',OLD)
            runtime.put(self.cloud,'initialized',True)
            runtime.put(self.cloud,'execution_enabled',True)
            runtime.put(self.cloud,'display',{'watchlist':[],'observation':{'items':[]},'reviews':[],
                'dynamic':{'global':{'items':[],'window_end':OLD},'enabled':True,'opaque':'preserve'}})
        self.calls=[]

    def tearDown(self):
        self.store.close();self.cloud.close();self.tmp.cleanup();self.cloud_tmp.cleanup()

    def transport(self,config,path,body):
        self.calls.append(copy.deepcopy(body))
        self.assertEqual(path,'/api/sync/reviews')
        with self.cloud.db:return sync.handle(self.cloud,self.cloud_cfg,path,body,body['page_display']['generated_at'])

    def snapshot(self,store):
        tables=('paper_accounts','paper_flows','paper_orders','paper_fills','paper_lots','global_orders','global_fills',
                'dynamic_orders','dynamic_fills','portfolio_decisions','cloud_contracts','cloud_receipts','reviews','lessons','strategy_guidance')
        return {t:[tuple(r) for r in store.db.execute('SELECT * FROM '+t)] for t in tables}

    def packet(self,at=AT):return pages.collect(self.store,self.cfg,at)

    def test_collect_is_lightweight_read_only_and_drops_raw_payloads(self):
        with self.store.db:
            row=self.store.db.execute('SELECT id,payload_json FROM reviews').fetchone();payload=json.loads(row['payload_json'])
            payload['facts']['slot_inputs']=[{'input_json':'PRIVATE_RAW_MARKER'*10000}]
            payload['facts']['portfolio']['research']=[{'id':'r1','analysis':{'analysis':'public summary','raw':'PRIVATE_RAW_MARKER',
                'decision':{'trigger':'explicit condition','invalidation':'failure condition','body':'PRIVATE_RAW_MARKER'}},'plan':{'thesis':'saved research','private_key':'PRIVATE_RAW_MARKER'}}]
            self.store.db.execute('UPDATE reviews SET payload_json=? WHERE id=?',(json.dumps(payload),row['id']))
        before=self.snapshot(self.store);changes=self.store.db.total_changes
        with patch('ashare.dashboard.status',side_effect=AssertionError('must not calculate full dashboard')),patch('pathlib.Path.read_text',side_effect=AssertionError('must not read extra files')):
            packet=self.packet()
        self.assertEqual(before,self.snapshot(self.store));self.assertEqual(changes,self.store.db.total_changes)
        self.assertNotIn('PRIVATE_RAW_MARKER',json.dumps(packet))
        self.assertLess(len(pages.canonical(packet['reviews'])),pages.REVIEW_BYTES)
        self.assertLess(len(pages.canonical(packet['macro'])),pages.MACRO_BYTES)
        self.assertEqual(packet['reviews'][0]['presentation']['daily']['dividend_cents'],175)
        facts=packet['reviews'][0]['payload']['facts']
        self.assertEqual(facts['portfolio']['positions'][0]['symbol'],'sz000001')
        self.assertEqual(facts['portfolio']['totals']['period_profit_cents'],1200)
        self.assertEqual(facts['daily_accounting']['totals']['period_profit_cents'],-450)
        self.assertEqual(packet['macro']['library']['followup_total'],1)
        self.assertIn('review_news_display_v1',ledger.FEATURES)

    def test_independent_send_updates_cloud_without_research_or_ledger_changes(self):
        before=self.snapshot(self.cloud);lease=runtime.lease(self.cloud,self.cloud_cfg,AT);version=ledger.version(self.cloud)
        with patch('ashare.page_display.now',return_value=AT),patch('ashare.cloud_sync.request',side_effect=self.transport):
            ack=sync.deliver_page_display(self.store,self.cfg)
        self.assertEqual(ack['page_display']['status'],'UPDATED')
        self.assertEqual(set(self.calls[0]),{'page_display'})
        self.assertEqual(set(self.calls[0]['page_display']),{'version','generated_at','reviews','macro','content_hash'})
        self.assertEqual(before,self.snapshot(self.cloud));self.assertEqual(version,ledger.version(self.cloud))
        self.assertEqual(lease,runtime.lease(self.cloud,self.cloud_cfg,AT));self.assertFalse(lease['active'])
        self.assertEqual(runtime.value(self.cloud,'research_received_at'),OLD)
        self.assertEqual(runtime.value(self.store,'page_display_sync')['status'],'OK')
        with patch('ashare.cloud_dashboard.now',return_value=AT):shown=cloud_status(self.cloud,self.cloud_cfg)
        self.assertEqual(shown['dynamic']['global']['library']['followup_total'],1)
        self.assertEqual(shown['dynamic']['opaque'],'preserve')
        self.assertEqual(shown['reviews'][0]['presentation']['checks']['counts']['FAIL'],1)
        self.assertEqual(shown['reviews'][0]['payload']['facts']['portfolio']['positions'][0]['symbol'],'sz000001')
        self.assertEqual(shown['page_display_sync']['generated_at'],AT)

    def test_clock_only_updates_do_not_resend_but_state_changes_do(self):
        with patch('ashare.cloud_sync.request',side_effect=self.transport):
            with patch('ashare.page_display.now',return_value=AT):sync.deliver_page_display(self.store,self.cfg)
            with patch('ashare.page_display.now',return_value=LATER):self.assertIsNone(sync.deliver_page_display(self.store,self.cfg))
            self.assertEqual(len(self.calls),1)
            with self.store.db:self.store.db.execute("UPDATE macro_events SET status='INVALIDATED'")
            with patch('ashare.page_display.now',return_value=LATER):sync.deliver_page_display(self.store,self.cfg)
        self.assertEqual(len(self.calls),2)
        new=runtime.value(self.cloud,pages.STATE)
        self.assertEqual(new['macro']['library']['history_total'],1)
        self.assertEqual(new['macro']['library']['followup_total'],0)

    def test_receiver_is_idempotent_and_rejects_older_or_conflicting_snapshots(self):
        packet=self.packet(LATER)
        with self.cloud.db:first=pages.receive(self.cloud,packet,LATER)
        changes=self.cloud.db.total_changes
        with self.cloud.db:again=pages.receive(self.cloud,packet,LATER)
        self.assertEqual(again['page_display']['status'],'UNCHANGED');self.assertEqual(changes,self.cloud.db.total_changes)
        old=self.packet(AT)
        with self.cloud.db:ignored=pages.receive(self.cloud,old,LATER)
        self.assertEqual(ignored['page_display']['status'],'IGNORED_STALE')
        self.assertEqual(runtime.value(self.cloud,pages.STATE),packet)
        conflict=copy.deepcopy(packet);conflict['macro']['event_count']=999;conflict['content_hash']=pages.content_hash(conflict)
        with self.assertRaisesRegex(ValueError,'冲突'),self.cloud.db:pages.receive(self.cloud,conflict,LATER)
        self.assertEqual(runtime.value(self.cloud,pages.STATE)['content_hash'],first['page_display']['content_hash'])

    def test_old_cloud_gate_and_unrecognized_ack_never_mark_delivery(self):
        with self.store.db:runtime.put(self.store,'remote_features',['supervision_summary'])
        with patch('ashare.cloud_sync.request',side_effect=AssertionError('old cloud')):
            self.assertIsNone(sync.deliver_page_display(self.store,self.cfg))
        with self.store.db:runtime.put(self.store,'remote_features',[pages.FEATURE])
        with patch('ashare.cloud_sync.request',return_value={'status':'ACCEPTED'}):
            with self.assertRaisesRegex(ValueError,'未确认'):sync.deliver_page_display(self.store,self.cfg)
        self.assertIsNone(runtime.value(self.store,'page_display_sent_hash'))

    def test_mixed_strategy_or_oversize_content_is_rejected_before_any_mutation(self):
        packet=self.packet();before=self.snapshot(self.cloud)
        with self.assertRaisesRegex(ValueError,'不能混入'),self.cloud.db:
            sync.handle(self.cloud,self.cloud_cfg,'/api/sync/reviews',{'page_display':packet,'reviews':[{'id':'bad'}]},AT)
        oversized=copy.deepcopy(packet);oversized['reviews']=[{}]*6
        with self.assertRaisesRegex(ValueError,'复盘展示'),self.cloud.db:pages.receive(self.cloud,oversized,AT)
        self.assertEqual(before,self.snapshot(self.cloud));self.assertIsNone(runtime.value(self.cloud,pages.STATE))

    def test_newer_legacy_generation_wins_over_an_older_independent_packet(self):
        packet=self.packet(AT)
        with self.cloud.db:pages.receive(self.cloud,packet,AT)
        cache={'reviews':[{'id':'new-review','ready_at':LATER}],
               'dynamic':{'global':{'window_end':LATER,'items':[{'id':'new-event'}]}}}
        shown=pages.apply(self.cloud,cache)
        self.assertEqual(shown['reviews'][0]['id'],'new-review')
        self.assertEqual(shown['dynamic']['global']['items'][0]['id'],'new-event')
        with self.cloud.db:runtime.put(self.cloud,'display_reviews',[{'id':'old-daily','ready_at':OLD}])
        shown=pages.apply(self.cloud,cache)
        self.assertEqual(shown['reviews'][0]['id'],'new-review')

    def test_periodic_sync_isolates_display_failure_from_accounting(self):
        # The existing minute loop must call the new channel even with no strategy
        # publication, and record its failure separately from successful ledger sync.
        with patch('ashare.cloud_sync.pull',return_value={'ledger_version':'same'}),patch('ashare.cloud_sync.flush',return_value=None),\
             patch('ashare.cloud_sync.deliver_notices'),patch('ashare.cloud_sync.deliver_supervision'),\
             patch('ashare.cloud_sync.deliver_page_display',side_effect=ValueError('synthetic display failure')) as delivery:
            result=sync.sync_once(self.cfg)
        self.assertEqual(result['status'],'SYNCED');delivery.assert_called_once()
        self.assertEqual(runtime.value(self.store,'last_sync')['status'],'OK')
        self.assertEqual(runtime.value(self.store,'page_display_sync')['status'],'FAILED')
        self.assertIsNone(runtime.value(self.store,'research_completed_at'))

    def test_news_budget_and_truncation_keep_truthful_total_counts(self):
        event={'id':'public','analysis':{'facts':'很长的公开研究'*1000,'raw_path':'PRIVATE_RAW_MARKER'}}
        sample={'items':[dict(event,id='current-'+str(i)) for i in range(80)],'archived_items':[],
                'followup_items':[event],'history_items':[],'library':{'followup_total':120,'history_total':200,'followup_loaded':1,'history_loaded':0},
                'assets':{},'markets':[],'news_counts':{},'observation_counts':{},'execution':'仅研究。','event_count':320}
        with patch('ashare.macro.view',return_value=sample):result=pages.macro_packet(self.store,self.cfg,AT)
        self.assertEqual(len(result['items']),60)
        self.assertEqual(result['library']['current_total'],80)
        self.assertEqual(result['library']['current_loaded'],60)
        self.assertEqual(result['delivery']['omitted']['items'],20)
        self.assertEqual(result['library']['followup_total'],120)
        self.assertIn('摘要预算',result['execution']);self.assertNotIn('PRIVATE_RAW_MARKER',json.dumps(result))
        self.assertLessEqual(len(result['items'][0]['analysis']['facts']),1800)

    def test_unknown_nested_fields_are_removed_locally_and_rejected_by_cloud(self):
        original=self.packet()['macro']
        original['items'][0]['analysis']['internal_trace']='SYNTHETIC_PRIVATE_MARKER'
        with patch('ashare.macro.view',return_value=original):
            filtered=pages.macro_packet(self.store,self.cfg,AT)
        self.assertNotIn('SYNTHETIC_PRIVATE_MARKER',json.dumps(filtered))
        forged=self.packet();forged['macro']['items'][0]['analysis']['internal_trace']='SYNTHETIC_PRIVATE_MARKER'
        forged['content_hash']=pages.content_hash(forged)
        with self.assertRaisesRegex(ValueError,'未授权字段'),self.cloud.db:
            pages.receive(self.cloud,forged,AT)
        self.assertIsNone(runtime.value(self.cloud,pages.STATE))
        self.assertEqual(len(pages.bounded('x'*2000,[0])),1800)
        assets={'assets':{key:{'name':'测试名称','category':'CN'} for key in ('sz000001','sh600000','US:NVDA','US10Y')}}
        self.assertEqual(pages.bounded(assets,[0]),assets)
        coverage={'news_screening':{'recent':[{'content_basis':'RSS_SUMMARY','content_available_at':AT}]},
                  'source_coverage':{'sources':[{'name':'公开来源','window_count':7}]},
                  'sources':[{'name':'公开来源','window_count':7}]}
        self.assertEqual(pages.bounded(coverage,[0]),coverage)

    def test_important_current_events_keep_priority_over_background(self):
        background={'id':'background','screening':{'decision':'BACKGROUND'},'analysis':{'impacts':[]}}
        important={'id':'important','screening':{'decision':'DEEP'},'analysis':{'impacts':[{'asset':'GOLD','materiality':{'state':'PENDING','admitted':False}}]}}
        sample={'items':[dict(background,id='b'+str(i)) for i in range(90)]+[dict(important,id='i'+str(i)) for i in range(65)],
                'followup_items':[],'history_items':[],'archived_items':[],'library':{},'execution':'Research only'}
        with patch('ashare.macro.view',return_value=sample):result=pages.macro_packet(self.store,self.cfg,AT)
        self.assertEqual(result['library']['current_total'],65)
        self.assertEqual(result['library']['current_loaded'],60)
        self.assertTrue(all(e['id'].startswith('i') for e in result['items']))


if __name__=='__main__':unittest.main()
