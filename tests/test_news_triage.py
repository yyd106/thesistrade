import copy,json,tempfile,unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
from ashare.storage import Store,normalize_time
from test_config import load_config
from ashare import dynamic_sources as ds,macro,macro_queue,macro_sources,news_triage as nt,news_catalog
from news_fixtures import screen_deep

class NewsSelectionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name);self.at=normalize_time('2026-09-22T10:00:00+08:00')
  self.cfg=load_config(Path(__file__).resolve().parents[1]/'config.json');self.cfg['data_dir']=self.tmp.name
 def tearDown(self):self.s.close();self.tmp.cleanup()
 def news(self,key,body='A new broad export control policy on semiconductor supply has been announced.',source='新浪财经快讯'):
  ds.ingest(self.s,[{'title':body[:95],'body':body,'url':'https://finance.sina.com.cn/'+key,'source':source,'published_at':self.at}],self.at,'fixture')
  return dict(self.s.db.execute('SELECT * FROM dynamic_news ORDER BY rowid DESC LIMIT 1').fetchone())
 def queue(self):return macro.select_news(self.s,self.at,self.at,self.at,self.cfg,candidate_limit=64)
 def test_cache_budget_diversity_and_no_observation_side_effects(self):
  for i in range(70):self.news(str(i),body=f'New semiconductor export control rule {i} announced at 10 am.')
  for i,source in enumerate(('美联储','Financial Times','FDA药品与医疗监管')):self.news('special'+str(i),body=f'Official policy for global supply {100+i} in force.',source=source)
  candidates=self.queue();calls=[]
  def model(prompt,*a):calls.append((prompt,a));return screen_deep(prompt)
  selected,result=nt.select(self.s,self.cfg,candidates,self.at,model)
  self.assertEqual(len(candidates),64);self.assertEqual(result['reviewed'],8);self.assertEqual(len(selected),4)
  packet=json.loads(calls[0][0].split('<UNTRUSTED_NEWS>')[1].split('</UNTRUSTED_NEWS>')[0])
  self.assertEqual(len({n['source'] for n in packet['news']}),4);self.assertEqual(calls[0][1][-1],90)
  nt.select(self.s,self.cfg,selected,self.at,model);self.assertEqual(len(calls),1)
  for t in ('macro_watchlist','paper_orders','dynamic_orders','plans'):self.assertEqual(self.s.db.execute('SELECT count(*) FROM '+t).fetchone()[0],0)
 def test_watch_and_background_do_not_reenter_deep_queue(self):
  news=[self.news('local',body='上海公布约11.20亿元节能降碳及特别国债资金安排，具体产业敞口仍待核实。'),self.news('trump',body='Trump made a general statement about the economy without concrete new policy.')]
  def model(prompt,*a):
   result=screen_deep(prompt)
   for i,v in enumerate(result['items']):v['decision']='BACKGROUND' if i==0 else 'WATCH'
   return result
  selected,r=nt.select(self.s,self.cfg,news,self.at,model);self.assertFalse(selected);self.assertFalse(self.queue())
  self.assertEqual(nt.summary(self.s,self.at)['counts'],{'BACKGROUND':1,'WATCH':1})
 def test_quote_and_pricing_fabrication_fail_closed_and_backoff(self):
  n=self.news('one');calls=[]
  def model(prompt,*a):
   calls.append(1);r=screen_deep(prompt);r['items'][0]['quote']='Fabricated central bank statement.';return r
  selected,result=nt.select(self.s,self.cfg,[n],self.at,model);self.assertFalse(selected);self.assertIn('error',result)
  nt.select(self.s,self.cfg,[n],self.at,model);self.assertEqual(len(calls),1)
  self.assertFalse(self.queue())
  self.assertEqual(self.s.db.execute('SELECT status FROM macro_news WHERE news_id=?',(n['id'],)).fetchone()[0],'SCREENING_WAIT')
  later=normalize_time((datetime.fromisoformat(self.at)+timedelta(hours=6)).isoformat());nt.select(self.s,self.cfg,[n],later,model);self.assertEqual(len(calls),2)
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM macro_news_triage').fetchone()[0],0)
 def test_pricing_requires_specific_quote(self):
  n=self.news('pricing');brief={**n,'body':n['body'][:1400]};r=screen_deep('<UNTRUSTED_NEWS>'+json.dumps({'news':[brief]})+'</UNTRUSTED_NEWS>')
  r['items'][0]['pricing']='SURPRISE'
  with self.assertRaises(ValueError):nt.validate(r,[brief])
 def test_low_impact_and_unconfirmed_rumours_are_not_deep(self):
  n=self.news('rumour')
  for field,value in [('potential','LOW'),('novelty','REPEAT'),('evidence','CLAIM'),('stage','COMMENTARY')]:
   def model(prompt,*a):
    r=screen_deep(prompt);r['items'][0][field]=value;return r
   selected,_=nt.select(self.s,self.cfg,[n],self.at,model);self.assertFalse(selected)
   with self.s.db:self.s.db.execute('DELETE FROM macro_news_triage');self.s.db.execute('DELETE FROM macro_triage_attempts')
 def test_original_research_priority_and_disabled_model(self):
  n=self.news('pending');model=lambda *a:self.fail('Model should not run')
  with patch('ashare.news_triage.busy',return_value=True):
   selected,r=nt.select(self.s,self.cfg,[n],self.at,model);self.assertFalse(selected);self.assertIn('deferred',r)
  self.cfg['model_enabled']=False
  with patch('ashare.news_triage.run_json',model):self.assertFalse(nt.select(self.s,self.cfg,[n],self.at)[0])
 def test_truncated_brief_cannot_cite_unseen_tail(self):
  n=self.news('long',body='semiconductor supply restriction ' + 'x'*1600+' UNSEEN FINAL EVIDENCE')
  def model(prompt,*a):
   r=screen_deep(prompt);r['items'][0]['quote']='UNSEEN FINAL EVIDENCE';return r
  selected,r=nt.select(self.s,self.cfg,[n],self.at,model);self.assertFalse(selected);self.assertIn('error',r)
 def test_name_alone_does_not_receive_catalyst_bonus(self):
  a=self.news('name',body='Trump schedules another speech on general economic policies.');b=self.news('shock',body='New export control restrictions on critical semiconductor supply take effect immediately.')
  self.queue();scores=dict(self.s.db.execute('SELECT news_id,priority FROM macro_news_queue'));self.assertGreater(scores[b['id']],scores[a['id']])
 def test_revision_invalidates_screening_cache(self):
  n=self.news('rev');nt.select(self.s,self.cfg,[n],self.at,screen_deep)
  n['body']+=' This decision has now been withdrawn.';self.assertIsNone(nt.context(self.s,n))
 def test_missing_and_duplicate_results_fail(self):
  n=self.news('one');m=self.news('two',body='FDA approves a new therapy affecting a large patient population.')
  r=screen_deep('<UNTRUSTED_NEWS>'+json.dumps({'news':[n]})+'</UNTRUSTED_NEWS>')
  with self.assertRaises(ValueError):nt.validate(r,[n,m])
  r['items']*=2
  with self.assertRaises(ValueError):nt.validate(r,[n])
 def test_feed_atom_rdf_invalid_item_isolation_and_https(self):
  atom=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>GDP release</title><link href="https://www.bea.gov/news/test"/><published>2026-09-21T08:30:00-04:00</published><summary>GDP rose 2 percent.</summary></entry></feed>'
  self.assertEqual(macro_sources.parse_feed(atom,'BEA美国经济数据')[0]['published_at'],'2026-09-21T12:30:00+00:00')
  rdf=b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"><item><title>Export regulation</title><link>http://www.federalregister.gov/test</link><dc:date>2026-09-21T00:00:00Z</dc:date></item></rdf:RDF>'
  self.assertTrue(macro_sources.parse_feed(rdf,'美国出口管制公告')[0]['url'].startswith('https:'))
  with self.assertRaises(ValueError):macro_sources.parse_feed(atom.replace(b'published',b'updated'),'BEA美国经济数据')
  with self.assertRaises(ValueError):ds.check_url('https://www.bloomberg.com.evil.test/news')
  with self.assertRaises(ValueError):macro_sources.parse_feed(atom,'美联储')
 def test_coverage_old_feed_and_missing_channel_are_not_reported_available(self):
  with self.s.db:
   self.s.db.execute('INSERT INTO dynamic_feed_checks VALUES(?,?,?,?,?)',('WHO全球卫生',self.at,'OK',0,'ok'))
   self.s.db.execute('INSERT INTO macro_feed_health VALUES(?,?,?,?,?)',('WHO全球卫生',self.at,'2026-02-25T00:00:00+00:00','2026-02-01T00:00:00+00:00',25))
  cov=news_catalog.coverage(self.s,self.at);who=next(s for s in cov['sources'] if s['source']=='WHO全球卫生');self.assertEqual(who['status'],'STALE');self.assertEqual(cov['available'],0)
 def test_future_only_feed_is_failure_not_current_coverage(self):
  xml=b'<rss><channel><item><title>Future news</title><link>https://www.fda.gov/test</link><pubDate>Wed, 30 Sep 2026 10:00:00 +0000</pubDate></item></channel></rss>'
  with patch('ashare.macro_sources.FEEDS',[('FDA药品与医疗监管','https://www.fda.gov/feed')]):r=macro_sources.collect(self.s,self.at,self.at,self.at,lambda url:xml)
  self.assertTrue(r['failures']);self.assertEqual(r['added'],0)
