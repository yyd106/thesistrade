import copy,json,tempfile,unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
from ashare import macro,macro_impact as impact,impact_history as history,observation,observation_pool
from ashare.storage import Store,normalize_time,digest
from test_config import load_config
from ashare import dynamic_sources as ds
from impact_fixtures import assessment,reviewer

class ImpactTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name);self.cfg=load_config(Path(__file__).resolve().parents[1]/'config.json');self.at=normalize_time('2026-09-22T02:00:00+00:00')
 def tearDown(self):self.s.close();self.tmp.cleanup()
 def when(self,days=0,hours=0):return normalize_time((datetime.fromisoformat(self.at)+timedelta(days=days,hours=hours)).isoformat())
 def event(self,key='one',asset='US10Y',published=None):
  body='The central bank implements a new rate policy across the national market. '+key
  ds.ingest(self.s,[{'title':body[:90],'body':body,'source':'美联储','url':'https://www.federalreserve.gov/'+key+'.htm','published_at':published or self.at}],self.at,'test')
  n=dict(self.s.db.execute('SELECT * FROM dynamic_news ORDER BY rowid DESC LIMIT 1').fetchone());eid=digest(n['id'])[:24]
  a={'headline':body[:80],'horizon':'DAYS','theme':'MONETARY','impacts':[{'asset':asset,'direction':'UP','strength':'HIGH','mechanism':'政策传导','watch':'观察后续','logic_chain':[{'kind':'FACT','statement':'政策变化','news_id':n['id'],'quote':body[:70]},{'kind':'INFERENCE','statement':'价格传导','news_id':'','quote':''}]}]}
  with self.s.db:
   self.s.db.execute('INSERT INTO macro_events VALUES(?,?,?,?,?,?,?)',(eid,n['id'],self.at,'FORWARD','MONETARY','TRACKING',json.dumps(a)))
   observation.sync_event(self.s,eid,a,self.at,{**observation.registry(self.s),asset:{'asset':asset,'name':asset,'kind':'STOCK','category':'CN','unit':'元'}})
  return eid,n
 def result(self,**changes):
  pending=impact.candidates(self.s,self.at)
  return {'assessments':[assessment(b['input'],**changes) for b in pending]}
 def review(self,**changes):return impact.review(self.s,self.cfg,self.at,lambda *args:self.result(**changes))
 def prices(self,asset='US10Y',future=False,checked=None):
  points=[]
  for i in range(61):
   day=(datetime.fromisoformat(self.at)-timedelta(days=61-i)).date().isoformat()
   points.append({'date':day,'value':4+(.025 if i%2 else -.025)})
  if future:points.extend({'date':self.when(days=i)[:10],'value':4.5+.1*i} for i in range(4))
  p={'points':points,'url':'https://fred.stlouisfed.org/series/test','raw_path':'frozen.csv','as_of':checked or self.at,'unit':'%','series':'DGS10'}
  with self.s.db:self.s.db.execute('INSERT OR REPLACE INTO macro_markets VALUES(?,?,?,?,?)',(asset,checked or self.at,'OK',json.dumps(p),None))
 def test_first_stage_high_never_admits_without_independent_review(self):
  self.event();r=observation.view(self.s,self.at,self.cfg)
  self.assertFalse(r['items']);self.assertEqual(r['archived_items'][0]['pool_tier'],'COOLING')
  self.assertEqual(r['archived_items'][0]['links'][0]['materiality']['state'],'PENDING')
 def test_local_implementation_rejected_despite_empty_slots_and_high_nomination(self):
  eid,n=self.event(asset='CN_EQUITY');self.review(event_kind='FUNDING',scope='LOCAL',novelty='IMPLEMENTATION',magnitude='LOW',target_fit='MISMATCH',exposure='INFERRED')
  r=observation.view(self.s,self.at,self.cfg);self.assertEqual(r['counts']['active'],0);self.assertEqual(r['archived_items'][0]['pool_tier'],'ARCHIVED')
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM macro_events').fetchone()[0],1)
 def test_cold_start_admission_requires_economic_evidence_and_discloses_gap(self):
  self.event();self.review();r=observation.view(self.s,self.at,self.cfg);v=r['items'][0]['links'][0]['materiality']
  self.assertTrue(v['admitted']);self.assertEqual(v['history']['sample_count'],0);self.assertIsNone(v['history']['train']['probability']);self.assertIn('尚未确认',v['reason'])
  self.assertIn('行情',v['measurement_gap'])
 def test_original_and_protected_assets_survive_rejected_review(self):
  self.event(asset=self.cfg['watchlist'][0]['symbol']);self.event('two',asset='GOLD');self.review(magnitude='LOW')
  with patch('ashare.observation_pool.protected_assets',return_value={'GOLD'}):r=observation.view(self.s,self.at,self.cfg)
  self.assertEqual({i['pool_tier'] for i in r['items']},{'CORE','ACTIVE'});self.assertEqual(r['counts']['original'],22)
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],0)
 def test_review_is_separate_public_only_bounded_and_cached(self):
  for i in range(8):self.event(str(i))
  calls=[]
  def model(prompt,*args):calls.append(prompt);return reviewer(prompt)
  a=impact.review(self.s,self.cfg,self.at,model);self.assertEqual(a['reviewed'],6);self.assertEqual(len(calls),1)
  text=calls[0].split('<UNTRUSTED_IMPACT_INPUT>')[1]
  for secret in ('cash_cents','paper_accounts','protected','priority_score','strength','direction'):self.assertNotIn(secret,text)
  impact.review(self.s,self.cfg,self.at,model);third=impact.review(self.s,self.cfg,self.at,model);self.assertEqual(third['reviewed'],0);self.assertEqual(len(calls),2)
 def test_failure_cannot_admit_and_retries_are_spaced(self):
  self.event();calls=[]
  def fail(*args):calls.append(1);raise ValueError('bad model response')
  with self.assertRaises(ValueError):impact.review(self.s,self.cfg,self.at,fail)
  self.assertFalse(observation.view(self.s,self.at,self.cfg)['items']);impact.review(self.s,self.cfg,self.at,fail);self.assertEqual(len(calls),1)
  with self.assertRaises(ValueError):impact.review(self.s,self.cfg,self.when(hours=6),fail)
  self.assertEqual(len(calls),2)
 def test_original_research_jobs_defer_the_extra_model(self):
  self.event()
  from ashare.scheduler import enqueue
  # Use table metadata to avoid touching actual scheduler behavior.
  enqueue(self.s,'research',self.at,key='busy',status='RUNNING')
  called=[];r=impact.review(self.s,self.cfg,self.at,lambda *args:called.append(1));self.assertEqual(r['reviewed'],0);self.assertFalse(called)
 def test_invented_citations_or_missing_pairs_are_rejected(self):
  self.event();batch=impact.candidates(self.s,self.at);result=self.result()
  bad=copy.deepcopy(result);bad['assessments'][0]['citations'][0]['quote']='A completely fabricated primary source'
  with self.assertRaises(ValueError):impact.validate(bad,batch)
  bad=copy.deepcopy(result);bad['assessments'][0]['citations']=[bad['assessments'][0]['citations'][0]]
  with self.assertRaises(ValueError):impact.validate(bad,batch)
  with self.assertRaises(ValueError):impact.validate({'assessments':[]},batch)
 def test_numeric_scale_requires_sourced_comparable_denominator(self):
  self.event();batch=impact.candidates(self.s,self.at);item=batch[0]['input'];item['sources'][0]['body']='2026年资金200亿元，占比需要对比产业规模1000亿元。'
  a=assessment(item);q=item['sources'][0]['body'];source=item['sources'][0]['id']
  for c in a['citations']:c['quote']=q
  a['scale']={'kind':'NUMERIC','numerator':{'value':200,'unit':'亿元','period':'2026年','source_id':source,'quote':q},'denominator':{'value':1000,'unit':'亿元','period':'2026年','source_id':source,'quote':q},'explanation':'相同期间资金与产业规模'}
  impact.validate({'assessments':[a]},batch)
  for key,value in [('value',999),('unit','美元'),('period','2025年')]:
   bad=copy.deepcopy(a);bad['scale']['denominator'][key]=value
   with self.assertRaises(ValueError):impact.validate({'assessments':[bad]},batch)
 def test_source_supplement_invalidates_cache_without_deleting_history(self):
  eid,n=self.event();self.review();self.assertTrue(observation.view(self.s,self.at,self.cfg)['items'])
  later=self.when(hours=1)
  with self.s.db:self.s.db.execute('INSERT INTO macro_impact_sources VALUES(?,?,?,?,?,?,?,?)',('extra',eid,self.at,later,'New source','https://www.federalreserve.gov/extra','Revised policy details with substantive new evidence.','source.txt'))
  self.assertTrue(observation.view(self.s,self.at,self.cfg)['items']);self.assertFalse(observation.view(self.s,later,self.cfg)['items']);self.assertEqual(len(impact.candidates(self.s,later)),1)
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM macro_impact_assessments').fetchone()[0],1)
 def test_outcomes_freeze_features_measure_rejected_cases_and_keep_yield_units(self):
  self.event();self.prices();self.review(magnitude='LOW');r=self.s.db.execute('SELECT * FROM macro_impact_assessments').fetchone();f=json.loads(r['features_json']);self.assertTrue(f['measurable'])
  self.assertEqual(history.measure(self.s,self.at),0);self.prices(future=True,checked=self.when(days=5))
  self.assertEqual(history.measure(self.s,self.when(days=4)),0);self.assertEqual(history.measure(self.s,self.when(days=5)),1)
  o=json.loads(self.s.db.execute('SELECT payload_json FROM macro_impact_outcomes').fetchone()[0]);self.assertEqual(o['change_unit'],'bp');self.assertAlmostEqual(o['change'],82.5);self.assertTrue(o['material'])
  self.assertEqual(history.measure(self.s,self.when(days=5)),0);self.assertEqual(self.s.db.execute('SELECT features_json FROM macro_impact_assessments').fetchone()[0],r['features_json'])
  self.assertEqual(history.learn(self.s,self.when(days=5))['forward_samples'],1)
 def test_no_series_stale_history_and_late_review_do_not_fabricate_samples(self):
  eid,n=self.event(asset='GOLD');self.review();f=json.loads(self.s.db.execute('SELECT features_json FROM macro_impact_assessments').fetchone()[0]);self.assertFalse(f['measurable'])
  eid,n=self.event('late',published=self.when(days=-3));self.prices();self.review();f=json.loads(self.s.db.execute('SELECT features_json FROM macro_impact_assessments ORDER BY rowid DESC LIMIT 1').fetchone()[0]);self.assertEqual(f['basis'],'LATE_REVIEW')
  self.prices(future=True,checked=self.when(days=5));history.learn(self.s,self.when(days=5));self.assertFalse(history.models(self.s,self.when(days=5)))
 def test_chronological_holdout_prevents_early_success_from_hiding_recent_failure(self):
  def samples(hits):return [{'id':str(i),'created_at':str(i).zfill(4),'outcome':{'material':hit,'change':3,'change_unit':'%'}} for i,hit in enumerate(hits)]
  self.assertEqual(history.fit(samples([True]*29))['state'],'INSUFFICIENT')
  self.assertEqual(history.fit(samples([True]*30))['state'],'SUPPORTED');self.assertEqual(history.fit(samples([False]*30))['state'],'WEAK')
  mixed=history.fit(samples([True]*20+[False]*10));self.assertEqual(mixed['state'],'INCONCLUSIVE');self.assertEqual(mixed['validation']['material_count'],0)
  a=assessment({'event_id':'e','asset':'a','sources':[{'id':'s','body':'A verified broad policy change in all markets.'}]})
  self.assertFalse(impact.decision(a,history.fit(samples([False]*30)))['admitted'])
 def test_overlap_and_duplicate_windows_cannot_multiply_training_samples(self):
  self.event();self.event('two');self.prices();self.review();self.prices(future=True,checked=self.when(days=5));history.learn(self.s,self.when(days=5))
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM macro_impact_outcomes').fetchone()[0],2)
  self.assertEqual(sum(m['sample_count'] for m in history.models(self.s,self.when(days=5)).values()),1)
  self.assertFalse(history.models(self.s,self.at))
 def test_nasdaq_uses_market_relative_reaction_not_broad_market_rally(self):
  eid,n=self.event(asset='NASDAQ')
  for asset in ('NASDAQ','SP500'):self.prices(asset)
  # Identical daily moves have no excess volatility: cannot invent normalization.
  self.review();f=json.loads(self.s.db.execute('SELECT features_json FROM macro_impact_assessments').fetchone()[0]);self.assertFalse(f['measurable']);self.assertIn('波动',f['gap'])


 def test_mainland_proxy_uses_existing_public_index_without_new_network(self):
  self.event(asset='CN_EQUITY');self.prices();r=self.s.db.execute("SELECT payload_json FROM macro_markets WHERE asset='US10Y'").fetchone();points=json.loads(r[0])['points']
  with self.s.db:self.s.db.execute('INSERT INTO dynamic_market VALUES(?,?,?)',('sh000300',self.at,json.dumps({'bars':[[p['date'],p['value'],p['value']] for p in points],'raw_path':'index.json'})))
  self.review();f=json.loads(self.s.db.execute('SELECT features_json FROM macro_impact_assessments').fetchone()[0]);self.assertTrue(f['measurable']);self.assertIn('沪深300',f['proxy']);self.assertEqual(f['change_unit'],'%')
 def test_actual_nasdaq_market_move_is_subtracted_from_outcome(self):
  self.event(asset='NASDAQ');base_date=datetime.fromisoformat(self.at)
  def save(asset,checked,future=False):
   points=[{'date':(base_date-timedelta(days=61-i)).date().isoformat(),'value':100+i*.1+(i%2)*(.3 if asset=='NASDAQ' else .1)} for i in range(61)]
   if future:points.extend({'date':self.when(days=i)[:10],'value':120 if asset=='NASDAQ' else 118} for i in range(4))
   with self.s.db:self.s.db.execute('INSERT OR REPLACE INTO macro_markets VALUES(?,?,?,?,?)',(asset,checked,'OK',json.dumps({'points':points,'url':'https://fred.stlouisfed.org/series/test','raw_path':'test.csv'}),None))
  for asset in ('NASDAQ','SP500'):save(asset,self.at)
  self.review()
  for asset in ('NASDAQ','SP500'):save(asset,self.when(days=5),True)
  history.measure(self.s,self.when(days=5));o=json.loads(self.s.db.execute('SELECT payload_json FROM macro_impact_outcomes').fetchone()[0])
  self.assertAlmostEqual(o['change'],(120-118)/106*100,places=4);self.assertGreater(o['benchmark_change'],10)
 def test_history_weakness_reallocates_without_model_or_ledger_writes(self):
  self.event();self.review();self.assertTrue(observation.view(self.s,self.at,self.cfg)['items'])
  key=next(iter(impact.context(self.s,self.at).values()))['assessment'];cohort=history.feature_key('US10Y',key)
  weak=history.fit([{'id':str(i),'created_at':str(i).zfill(4),'outcome':{'material':False,'change':.1,'change_unit':'bp'}} for i in range(30)])
  with patch('ashare.impact_history.models',return_value={cohort:weak}):
   r=observation_pool.reconcile(self.s,self.cfg,self.at)
   self.assertFalse(r['items']);self.assertEqual(r['archived_items'][0]['pool_tier'],'COOLING');self.assertIn('历史',r['archived_items'][0]['pool_reason'])
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM macro_impact_assessments').fetchone()[0],1);self.assertEqual(self.s.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],0)

 def test_dynamic_cycle_reviews_backlog_even_without_new_news(self):
  from ashare import dynamic
  self.event();calls=[]
  def model(prompt,*args):calls.append(prompt);return reviewer(prompt)
  with patch('ashare.dynamic.now',return_value=self.at),patch('ashare.macro.select_news',return_value=[]),patch('ashare.macro_sources.refresh_markets',return_value=[]),patch('ashare.sources.stock_catalog',return_value=[]),patch('ashare.dynamic_sources.refresh_market'):
   r=dynamic.cycle(self.s,self.cfg,collect_fn=lambda *args:{'failures':[]},impact_model_fn=model)
  self.assertEqual(r['impact_review']['reviewed'],1);self.assertEqual(len(calls),1);self.assertEqual(r['global_selected'],0)
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM dynamic_orders').fetchone()[0],0)

 def test_rejected_cause_cannot_change_active_direction_or_focus_context(self):
  self.event('strong');self.review();eid,n=self.event('weak');r=self.s.db.execute('SELECT payload_json FROM macro_events WHERE id=?',(eid,)).fetchone();a=json.loads(r[0]);a['impacts'][0]['direction']='DOWN'
  with self.s.db:self.s.db.execute('UPDATE macro_events SET payload_json=? WHERE id=?',(json.dumps(a),eid))
  self.review(magnitude='LOW',direction='DOWN');item=observation.view(self.s,self.at,self.cfg)['items'][0]
  self.assertEqual(item['direction'],'UP');self.assertFalse(item['conflicting'])
  calls=[]
  def model(prompt,*args):calls.append(prompt);return {'summary':'No new facts','events':[]}
  macro.research(self.s,self.cfg,[n],self.at,model)
  packet=json.loads(calls[0].split('<UNTRUSTED_WORLD_NEWS>')[1].split('</UNTRUSTED_WORLD_NEWS>')[0]);causes=packet['priority_research_assets'][0]['prior_public_causes']
  self.assertEqual(len(causes),1);self.assertEqual(causes[0]['direction'],'UP')

if __name__=='__main__':unittest.main()
