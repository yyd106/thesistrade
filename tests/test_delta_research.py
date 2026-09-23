import json
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
from ashare.storage import Store,normalize_time
from test_config import load_config
from ashare.research import make_snapshot,study,model_packet,begin_batch
from ashare.demo import seed,research_model,SYMBOL
from ashare.events import related
from ashare.slots import unreviewed_events
from ashare.presentation import outstanding_failures
from ashare import external_news


class DeltaResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.cfg=load_config(Path(__file__).resolve().parents[1]/'config.json')
        self.cfg.update(data_dir=self.tmp.name,watchlist=[{'symbol':SYMBOL,'name':'合成测试'}])
        self.start='2026-09-15T09:00:00+08:00'

    def tearDown(self):self.store.close();self.tmp.cleanup()

    def document(self,title='经营变化',text='公司披露经营现金流增加。',symbol=SYMBOL,url='https://example.test/new',ready='2026-09-15T09:04:00+08:00',published=None,cloud=True):
        return self.store.add_document(symbol=symbol,kind='news',title=title,source='fixture',url=url,
            published_at=published or ready,first_seen_at=ready,ready_at=ready,pages=[(1,text)],raw_path='fixture',cloud_allowed=cloud)[0]

    def finish(self,packet,at='2026-09-15T09:01:00+08:00',use_model=True):
        return study(self.store,self.cfg,packet,model_fn=research_model(packet),at=at,use_model=use_model)

    def test_success_uses_memory_without_resending_old_fulltext(self):
        first=seed(self.store,self.cfg);done=self.finish(first)
        second=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:02:00+08:00')
        self.assertEqual(second['learning']['new_chunks'],0)
        self.assertEqual(second['learning']['pending_chunks'],0)
        self.assertEqual(second['previous_research']['study_id'],done['study_id'])
        self.assertIn(first['evidence'][0]['text'],json.dumps(model_packet(second,self.cfg),ensure_ascii=False))
        self.assertEqual(second['evidence'][0]['use'],'固定财务底稿原文')
        self.assertTrue(second['mandatory_coverage'][0]['all_chunks_accounted'])

    def test_failed_attempt_and_equal_timestamp_never_skip_unread_data(self):
        first=seed(self.store,self.cfg);self.finish(first,use_model=False)
        again=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:02:00+08:00')
        self.assertEqual(set(again['learning']['new_chunk_ids']),set(first['learning']['new_chunk_ids']))
        self.assertIsNone(again['previous_research'])
        self.finish(again,at='2026-09-15T09:03:00+08:00')
        self.document(ready='2026-09-15T09:02:00+08:00')
        next_packet=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:04:00+08:00')
        self.assertEqual(next_packet['learning']['new_documents'],1)

    def test_upgrade_backfills_only_full_chunks_actually_sent_to_successful_studies(self):
        first=seed(self.store,self.cfg);self.finish(first)
        self.document()
        failed=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:05:00+08:00')
        self.finish(failed,'2026-09-15T09:06:00+08:00',use_model=False)
        self.store.db.execute('DELETE FROM learned_chunks')
        self.store.db.execute("DELETE FROM metadata WHERE key='learned_chunks_backfilled'")
        self.store.db.commit();self.store.close();self.store=Store(self.tmp.name)
        learned={r[0] for r in self.store.db.execute('SELECT chunk_id FROM learned_chunks WHERE symbol=?',(SYMBOL,))}
        self.assertEqual(learned,set(first['learning']['new_chunk_ids']))
        self.assertFalse(learned.intersection(failed['learning']['new_chunk_ids']))

    def test_local_audit_size_does_not_consume_model_context_budget(self):
        seed(self.store,self.cfg)
        row=self.store.db.execute('SELECT rowid,payload FROM market_features LIMIT 1').fetchone()
        payload=json.loads(row['payload']);payload['local_audit']='LOCAL_ONLY_AUDIT'*6000
        self.store.db.execute('UPDATE market_features SET payload=? WHERE rowid=?',(json.dumps(payload),row['rowid']))
        self.store.db.commit()
        p=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:05:00+08:00')
        self.assertGreater(len(json.dumps(p)),self.cfg['max_packet_chars'])
        packed=json.dumps(model_packet(p,self.cfg),ensure_ascii=False,sort_keys=True)
        self.assertLessEqual(len(packed),self.cfg['max_packet_chars'])
        self.assertNotIn('LOCAL_ONLY_AUDIT',packed)

    def test_growing_catalog_is_retained_locally_without_overflowing_model_input(self):
        self.cfg['max_packet_chars']=5000
        for i in range(80):
            self.document(title=f'第{i}期经营公告'+('收入成本及现金流说明'*8),url=f'https://example.test/report/{i}',
                text=f'第{i}期：'+('公司披露收入、成本与现金流变化。'*8),ready=self.start)
        p=seed(self.store,self.cfg)
        self.assertGreaterEqual(len(p['mandatory_coverage']),80)
        self.assertGreater(p['learning']['pending_chunks'],0)
        packed=model_packet(p,self.cfg)
        self.assertEqual(packed['资料覆盖总览']['相关资料总数'],len(p['mandatory_coverage']))
        self.assertLessEqual(len(json.dumps(packed,ensure_ascii=False,sort_keys=True)),5000)

    def test_late_revision_and_duplicate_are_handled_by_version_not_publication_window(self):
        first=seed(self.store,self.cfg);self.finish(first)
        late=self.document(published='2026-07-01T09:00:00+08:00')
        p=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:05:00+08:00');self.finish(p,'2026-09-15T09:06:00+08:00')
        self.assertIn(late,{e['doc_id'] for e in p['evidence']})
        self.document(url='https://example.test/reprint',ready='2026-09-15T09:07:00+08:00')
        p=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:08:00+08:00')
        self.assertEqual(p['learning']['new_chunks'],0)
        changed=self.document(text='公司修订公告：现金流减少。',ready='2026-09-15T09:09:00+08:00')
        p=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:10:00+08:00')
        self.assertIn(changed,p['learning']['revised_documents'])
        self.assertNotIn(late,{d['version_id'] for d in p['document_manifest']})

    def test_large_document_continues_without_relearning_or_claiming_complete(self):
        self.cfg['max_packet_chars']=5000
        self.document(text='这是一份用于测试的长报告，其中记载收入和成本变化。'*700,ready=self.start)
        p=seed(self.store,self.cfg)
        self.assertGreater(p['learning']['pending_chunks'],0)
        seen=set();stamp=datetime.fromisoformat('2026-09-15T09:01:00+08:00')
        for i in range(40):
            new=set(p['learning']['new_chunk_ids'])
            self.assertFalse(seen&new);seen|=new
            self.assertLessEqual(len(json.dumps(model_packet(p,self.cfg),ensure_ascii=False,sort_keys=True)),self.cfg['max_packet_chars'])
            self.finish(p,stamp.isoformat())
            if not p['learning']['pending_chunks']:break
            stamp+=timedelta(minutes=2)
            p=make_snapshot(self.store,self.cfg,SYMBOL,at=(stamp-timedelta(minutes=1)).isoformat())
        self.assertEqual(p['learning']['pending_chunks'],0)
        self.assertGreater(len(seen),10)

    def test_market_event_routes_by_sector_and_is_not_learned_for_other_stock(self):
        self.finish(seed(self.store,self.cfg))
        geo=self.document('红海冲突影响航运','红海局势使部分船舶调整航线，运输成本变化需核实。','MARKET','https://example.test/redsea')
        appliance=self.document('家电以旧换新政策','家电以旧换新政策涉及终端需求。','MARKET','https://example.test/home')
        drug=self.document('医保药品调整','医保药品政策调整涉及药物支付标准。','MARKET','https://example.test/drug')
        p=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:05:00+08:00')
        self.assertNotIn(geo,{e['doc_id'] for e in p['evidence']});self.assertNotIn(drug,{e['doc_id'] for e in p['evidence']})
        self.assertIn(appliance,{e['doc_id'] for e in p['evidence']})
        self.assertIn('敞口',p['external_events'][0]['exposure_status'])
        self.finish(p,'2026-09-15T09:06:00+08:00')
        other=make_snapshot(self.store,self.cfg,'sh688062',at='2026-09-15T09:07:00+08:00')
        self.assertNotIn(geo,{e['doc_id'] for e in other['evidence']});self.assertIn(drug,{e['doc_id'] for e in other['evidence']})
        self.assertIsNone(other['previous_research'])

    def test_revoked_permission_invalidates_transitive_summary_and_historical_quotes(self):
        first=seed(self.store,self.cfg);self.finish(first)
        second=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:02:00+08:00')
        self.finish(second,'2026-09-15T09:03:00+08:00')
        # The second successful report inherited the first, even if it sent no new raw text.
        self.store.db.execute('UPDATE documents SET cloud_allowed=0 WHERE id=?',(first['evidence'][0]['doc_id'],))
        self.store.db.commit()
        third=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:04:00+08:00')
        self.assertEqual(third['previous_research']['source_ids'],[])
        self.assertIn('旧结论需重新核对',third['previous_research']['analysis'])
        self.assertFalse(third['evidence'])

    def test_future_and_private_external_evidence_never_enter_packet(self):
        seed(self.store,self.cfg)
        self.document('红海私人消息','PRIVATE_GEO_SECRET','MARKET','https://example.test/private',cloud=False)
        future=self.document('红海冲突消息','红海冲突将影响航运。','MARKET','https://example.test/future',published='2026-09-16T09:00:00+08:00')
        p=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:05:00+08:00')
        self.assertNotIn('PRIVATE_GEO_SECRET',json.dumps(p));self.assertNotIn(future,{e['doc_id'] for e in p['evidence']})

    def test_new_external_event_invalidates_old_buy_plan_and_publication_race(self):
        first=seed(self.store,self.cfg);r=self.finish(first)
        plan=dict(self.store.db.execute('SELECT * FROM plans WHERE id=?',(r['plan_id'],)).fetchone())
        background=self.document('红海冲突','红海冲突使运输条件变化。','MARKET','https://example.test/background')
        self.assertNotIn(background,unreviewed_events(self.store,plan,normalize_time('2026-09-15T09:05:00+08:00'),self.cfg))
        event=self.document('公司重大合同变更','000333重大合同调整涉及公司收入。','MARKET','https://example.test/risk')
        self.assertIn(event,unreviewed_events(self.store,plan,normalize_time('2026-09-15T09:05:00+08:00'),self.cfg))
        p=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:05:00+08:00')
        def model(*args):
            self.document('新增重大诉讼','000333涉及新的重大诉讼。','MARKET','https://example.test/tariff',ready='2026-09-15T09:06:00+08:00')
            return research_model(p)()
        r=study(self.store,self.cfg,p,model_fn=model,at='2026-09-15T09:07:00+08:00')
        payload=json.loads(self.store.db.execute('SELECT payload_json FROM plans WHERE id=?',(r['plan_id'],)).fetchone()[0])
        self.assertIn('SOURCE_CHANGED_DURING_RESEARCH',payload['blockers'])

    def test_article_parser_excludes_navigation_and_feed_is_not_fulltext(self):
        raw=('<html><meta name="PubDate" content="2026-09-17 10:30"><nav>PRIVATE_NAV</nav>'
            '<div class="art-con"><p>'+('贸易政策对出口行业形成影响。'*10)+'</p></div><footer>FOOTER</footer></html>').encode()
        text,stamp,precision=external_news.parse_article(raw,{'published_at':self.start,'precision':'date'})
        self.assertNotIn('PRIVATE_NAV',text);self.assertNotIn('FOOTER',text)
        self.assertEqual(stamp,normalize_time('2026-09-17T10:30:00+08:00'))
        brief='商务部副部长会见企业负责人，双方就扩大在华业务、清洁环保合作及经贸关系等议题进行了交流。'
        short=('<div class="art-con">'+brief+'</div>').encode()
        self.assertEqual(external_news.parse_article(short,{'published_at':self.start,'precision':'date'})[0],brief)
        with self.assertRaises(ValueError):external_news.parse_article(b'<nav>navigation</nav>',{'published_at':self.start,'precision':'date'})

    def test_external_article_failure_clears_only_after_same_article_success(self):
        bid,_=begin_batch(self.store,self.cfg)
        feed='https://news.un.org/feed/subscribe/zh/news/all/rss.xml';url='https://news.un.org/zh/story/test'
        raw=f'<rss><channel><item><title>红海冲突影响航运</title><link>{url}</link><pubDate>Thu, 17 Sep 2026 12:00:00 +0000</pubDate><description>红海运输环境变化。</description></item></channel></rss>'.encode()
        body=('<div class="field--name-field-text-column">'+'红海冲突对运输带来影响，仍需核实公司实际情况。'*10+'</div>').encode()
        fail=[True]
        def fetch(address,**kwargs):
            if address==feed:return raw
            if fail[0]:raise TimeoutError('fixture')
            return body
        with patch.object(external_news,'FEEDS',(('un_news','联合国新闻',feed,'rss'),)),patch('ashare.sources.fetch',side_effect=fetch):
            external_news.collect(self.store,bid['id'],self.cfg)
            self.assertTrue(any(x['title']=='红海冲突影响航运' for x in outstanding_failures(self.store,SYMBOL)))
            fail[0]=False;external_news.collect(self.store,bid['id'],self.cfg)
            self.assertFalse(outstanding_failures(self.store,SYMBOL))
            n=self.store.db.execute('SELECT count(*) FROM documents').fetchone()[0]
            external_news.collect(self.store,bid['id'],self.cfg)
            self.assertEqual(n,self.store.db.execute('SELECT count(*) FROM documents').fetchone()[0])


if __name__=='__main__':unittest.main()
