import copy,json,tempfile,unittest
from datetime import timedelta,datetime
from pathlib import Path
from unittest.mock import patch
from ashare.storage import Store,normalize_time
from test_config import load_config
from ashare import macro,macro_sources as ms,dynamic,dynamic_sources as ds
from ashare import observation
from news_fixtures import screen_deep

class MacroTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
  self.cfg=load_config(Path(__file__).resolve().parents[1]/'config.json');self.cfg.update(data_dir=self.tmp.name,dynamic_enabled=True)
  self.at=normalize_time('2026-09-14T10:00:00+08:00')
 def tearDown(self):self.store.close();self.tmp.cleanup()
 def news(self,key='fed',body='Federal Reserve changes monetary policy; global interest rates and the US dollar remain sensitive to inflation.',published=None,source='美联储',url=None):
  ds.ingest(self.store,[{'title':body[:75],'body':body,'source':source,'url':url or 'https://www.federalreserve.gov/'+key+'.htm','published_at':published or self.at}],self.at,'test.xml')
  return dict(self.store.db.execute('SELECT * FROM dynamic_news ORDER BY rowid DESC LIMIT 1').fetchone())
 def result(self,n):
  return {'summary':'研究美国利率和全球资产影响，无A股关联也保留。','events':[{'news_id':n['id'],'headline':'美联储政策变化与全球传导','theme':'MONETARY','regions':['US','GLOBAL'],'facts':'政策信息需要继续核验','transmission':'利率影响资金成本与跨境定价','uncertainty':'缺少市场预期调查','invalidation':'政策没有实施','horizon':'DAYS','expectations':'UNKNOWN','expectation_basis':'原文没有市场一致预期','impacts':[{'asset':'US10Y','direction':'UP','mechanism':'利率路径可能抬升收益率','watch':'下一次政策说明'},{'asset':'GOLD','direction':'MIXED','mechanism':'实际利率与避险需求相互作用','watch':'美元与实际利率'}],'evidence':[{'news_id':n['id'],'quote':n['body'][:100]}]}]}
 def research(self,n):
  macro.select_news(self.store,*dynamic.window('2026-09-14T10:30:00+08:00'),self.at)
  result=self.result(n);captured=[]
  def model(prompt,*args):captured.append(prompt);return result
  macro.research(self.store,self.cfg,[n],self.at,model)
  return dict(self.store.db.execute('SELECT * FROM macro_events ORDER BY rowid DESC LIMIT 1').fetchone()),captured[0]
 def series(self,asset,values,at='2026-09-20T10:00:00+08:00'):
  at=normalize_time(at);spec=ms.ASSETS[asset]
  p={'series':spec['series'],'url':'https://fred.stlouisfed.org/series/'+spec['series'],'raw_path':'test.csv','unit':spec['unit'],'points':[{'date':d,'value':v} for d,v in values],'as_of':at}
  with self.store.db:self.store.db.execute('INSERT OR REPLACE INTO macro_markets VALUES(?,?,?,?,?)',(asset,at,'OK',json.dumps(p),None))
 def test_pure_foreign_news_is_selected_without_stock_catalog(self):
  n=self.news();self.assertEqual(ds.candidates(n,[]),[])
  selected=macro.select_news(self.store,*dynamic.window('2026-09-14T10:30:00+08:00'),self.at)
  self.assertEqual([x['id'] for x in selected],[n['id']])
  event,prompt=self.research(n);self.assertEqual(event['basis'],'FORWARD')
  self.assertIn('US10Y',prompt);self.assertIn('黄金',prompt);self.assertNotIn('paper_accounts',prompt)
  for table in ['dynamic_cases','dynamic_orders','paper_orders','plans','documents']:
   self.assertEqual(self.store.db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
 def test_old_stock_background_news_is_reconsidered_globally(self):
  n=self.news()
  with self.store.db:self.store.db.execute("UPDATE dynamic_news SET status='BACKGROUND' WHERE id=?",(n['id'],))
  self.assertEqual(len(macro.select_news(self.store,*dynamic.window('2026-09-14T10:30:00+08:00'),self.at)),1)
 def test_company_registration_is_not_macro_catalyst(self):
  n=self.news(body='某公司注册资本增加，工商信息发生变更，董事会成员变动。',source='新浪财经快讯')
  self.assertFalse(macro.topics(n));self.assertFalse(macro.select_news(self.store,*dynamic.window('2026-09-14T10:30:00+08:00'),self.at))
 def test_validator_rejects_invented_asset_and_quote(self):
  n=self.news();r=self.result(n);macro.validate(r,[n])
  bad=copy.deepcopy(r);bad['events'][0]['impacts'][0]['asset']='sh600547'
  with self.assertRaises(ValueError):macro.validate(bad,[n])
  bad=copy.deepcopy(r);bad['events'][0]['evidence'][0]['quote']='An invented quote never published.'
  with self.assertRaises(ValueError):macro.validate(bad,[n])
 def test_global_cycle_survives_a_share_catalog_failure(self):
  n=self.news();stamp=normalize_time('2026-09-14T10:30:00+08:00')
  with patch('ashare.dynamic.now',return_value=stamp),patch('ashare.macro_sources.refresh_markets',return_value=[]),patch('ashare.sources.stock_catalog',side_effect=ValueError('catalog down')),patch('ashare.dynamic_sources.refresh_market'):
   r=dynamic.cycle(self.store,self.cfg,end=stamp,model_fn=lambda *args:self.result(n),triage_model_fn=screen_deep,collect_fn=lambda *args:{'added':0,'failures':[]})
  self.assertIn('global_research',r);self.assertIn('execution_mapping_error',r)
  self.assertEqual(self.store.db.execute('SELECT count(*) FROM macro_events').fetchone()[0],1)
  self.assertEqual(self.store.db.execute('SELECT count(*) FROM dynamic_cases').fetchone()[0],0)
 def test_news_revision_invalidates_global_research(self):
  n=self.news();event,prompt=self.research(n)
  self.news(body='Federal Reserve corrects the previous statement on monetary policy; the prior statement was withdrawn.')
  self.assertEqual(self.store.db.execute('SELECT status FROM macro_events WHERE id=?',(event['id'],)).fetchone()[0],'INVALIDATED')
 def test_yield_change_uses_basis_points_not_bond_return(self):
  n=self.news();event,_=self.research(n)
  self.series('US10Y',[('2026-09-11',4.0),('2026-09-14',4.1),('2026-09-15',4.15),('2026-09-16',4.2),('2026-09-17',4.25)])
  at=normalize_time('2026-09-20T11:00:00+08:00');self.assertEqual(macro.measure(self.store,at),1)
  r=json.loads(self.store.db.execute('SELECT payload_json FROM macro_observations').fetchone()[0]);self.assertEqual(r['change_unit'],'bp');self.assertEqual(r['change'],25)
  self.assertEqual(r['baseline']['date'],'2026-09-11');self.assertEqual(r['end']['date'],'2026-09-17')
  self.assertEqual(macro.measure(self.store,at),0)
 def test_outcomes_wait_for_published_data_and_keep_gold_qualitative(self):
  n=self.news();event,_=self.research(n)
  self.series('US10Y',[('2026-09-11',4.0),('2026-09-15',4.15),('2026-09-16',4.2)])
  self.assertEqual(macro.measure(self.store,normalize_time('2026-09-20T11:00:00+08:00')),0)
  v=macro.view(self.store,normalize_time('2026-09-20T11:00:00+08:00'));gold=next(r for r in v['archived_items'][0]['reactions'] if r['asset']=='GOLD')
  self.assertIsNone(gold['observation']);self.assertIn('尚未接入',gold['waiting'])
 def test_historical_reaction_not_visible_before_available(self):
  n=self.news(published=normalize_time('2026-09-01T10:00:00+08:00'));event,_=self.research(n)
  self.assertEqual(event['basis'],'RETROSPECTIVE')
  with self.store.db:self.store.db.execute('INSERT INTO macro_observations VALUES(?,?,?,?,?)',(event['id'],'US10Y',normalize_time('2026-09-15T12:00:00+08:00'),'RETROSPECTIVE',json.dumps({'secret_future_fact':'future yield'})))
  newer=self.news(key='fed2');_,prompt=self.research(newer)
  self.assertNotIn('secret_future_fact',prompt)
 def test_official_rss_times_links_and_entity_guard(self):
  raw=b'<rss><channel><item><title>Monetary policy decision</title><link>http://www.boj.or.jp/en/test.htm</link><pubDate>Mon, 14 Sep 2026 09:00:00 +0900</pubDate><description>Policy rates are unchanged.</description></item></channel></rss>'
  r=ms.parse_feed(raw,'日本央行')[0];self.assertEqual(r['published_at'],'2026-09-14T00:00:00+00:00');self.assertTrue(r['url'].startswith('https:'))
  with self.assertRaises(ValueError):ms.parse_feed(raw.replace(b'www.boj.or.jp',b'evil.invalid'),'日本央行')
  with self.assertRaises(ValueError):ms.parse_feed(b'<!DOCTYPE rss>'+raw,'日本央行')
 def test_csv_missing_future_and_invalid_values(self):
  raw=b'observation_date,DGS10\n2026-09-10,4.1\n2026-09-11,.\n2026-09-14,4.2\n2026-09-20,4.5\n'
  self.assertEqual(ms.parse_series(raw,'DGS10',self.at),[{'date':'2026-09-10','value':4.1}])
  with self.assertRaises(ValueError):ms.parse_series(raw.replace(b'4.1',b'NaN'),'DGS10',self.at)
 def test_source_failure_does_not_discard_other_global_news(self):
  raw=b'<rss><channel><item><title>Federal Reserve monetary policy news</title><link>https://www.federalreserve.gov/test.htm</link><pubDate>Mon, 14 Sep 2026 00:00:00 +0000</pubDate><description>Rates are unchanged.</description></item></channel></rss>'
  def fetch(url):
   if 'ecb' in url:raise OSError('source unavailable')
   return raw
  with patch('ashare.macro_sources.FEEDS',[f for f in ms.FEEDS if f[0] in ('美联储','欧洲央行')]):r=ms.collect(self.store,'2026-09-13T00:00:00+00:00',self.at,self.at,fetch)
  self.assertEqual(r['added'],1);self.assertEqual(len(r['failures']),1)
  self.assertEqual(self.store.db.execute("SELECT status FROM dynamic_feed_checks WHERE source='美联储'").fetchone()[0],'OK')
  self.assertEqual(self.store.db.execute('SELECT count(*) FROM source_checks').fetchone()[0],0)

 def test_recent_view_uses_publication_window_and_separate_archive(self):
  cutoff=datetime.fromisoformat(self.at)-timedelta(hours=48)
  ids={}
  for key,published in [('now',self.at),('boundary',cutoff.isoformat()),('old',(cutoff-timedelta(seconds=1)).isoformat()),('revised',self.at),('future',(datetime.fromisoformat(self.at)+timedelta(seconds=1)).isoformat()),('not_yet_researched',self.at)]:
   n=self.news(key,body='Federal Reserve monetary policy '+key+' changes global interest rates.',published=normalize_time(published))
   event,_=self.research(n);ids[key]=event['id']
  with self.store.db:
   self.store.db.execute("UPDATE macro_events SET status='INVALIDATED' WHERE id=?",(ids['revised'],))
   self.store.db.execute('UPDATE macro_events SET created_at=? WHERE id=?',((datetime.fromisoformat(self.at)+timedelta(seconds=1)).isoformat(),ids['not_yet_researched']))
  v=macro.view(self.store,self.at)
  self.assertEqual([e['id'] for e in v['items']],[ids['now'],ids['boundary']])
  self.assertEqual({e['id'] for e in v['archived_items']},{ids['old'],ids['revised']})
  self.assertEqual(v['window_start'],cutoff.isoformat())
  self.assertEqual(v['window_end'],self.at)
  # As the window rolls forward, only the expired event moves to the archive.
  later=macro.view(self.store,(datetime.fromisoformat(self.at)+timedelta(seconds=1)).isoformat())
  self.assertNotIn(ids['boundary'],[e['id'] for e in later['items']])
  self.assertIn(ids['boundary'],[e['id'] for e in later['archived_items']])

 def test_recent_view_is_not_truncated_by_archive_limit(self):
  for i in range(46):
   n=self.news(f'event-{i}',body=f'Federal Reserve monetary policy event {i} affects global interest rates.')
   self.research(n)
  self.assertEqual(len(macro.view(self.store,self.at)['items']),46)
  later=normalize_time((datetime.fromisoformat(self.at)+timedelta(hours=49)).isoformat())
  v=macro.view(self.store,later)
  self.assertFalse(v['items']);self.assertEqual(len(v['archived_items']),40)

 def chain_result(self,n,asset='GOLD',direction='UP'):
  r=self.result(n);i=r['events'][0]['impacts'][0]
  i.update(asset=asset,direction=direction,strength='HIGH',strength_basis='若政策实施，可能改变全行业成本',conditions='政策落地且企业敞口核实',invalidation='政策取消或成本被完全对冲',logic_chain=[
   {'kind':'FACT','statement':'新闻报告政策表态','news_id':n['id'],'quote':n['body'][:100]},
   {'kind':'INFERENCE','statement':'若政策落地，相关产业成本可能提高','news_id':'','quote':''},
   {'kind':'INFERENCE','statement':'若该标的存在相关敞口，利润或价格可能受到影响','news_id':'','quote':''}])
  r['events'][0]['impacts']=[i];return r

 def test_indirect_chain_adds_verified_us_stock_without_touching_execution(self):
  n=self.news(body='Trump says the administration may tighten semiconductor export restrictions. The policy is not yet implemented.',source='新浪财经快讯')
  spec={'asset':'US:NVDA','symbol':'NVDA','name':'NVIDIA Corporation - Common Stock','category':'US','kind':'STOCK','unit':'美元','identity_source':'official-test'}
  with self.store.db:self.store.db.execute('INSERT INTO macro_instruments VALUES(?,?,?,?)',('US:NVDA','US',self.at,json.dumps(spec)))
  r=self.chain_result(n,'US:NVDA','DOWN');before=copy.deepcopy(self.cfg['watchlist'])
  macro.research(self.store,self.cfg,[n],self.at,lambda *args:r)
  item=observation.view(self.store,self.at)['archived_items'][0]
  self.assertEqual(item['pool_tier'],'COOLING')
  self.assertFalse(item['links'][0]['materiality']['admitted'])
  self.assertEqual(item['asset'],'US:NVDA');self.assertEqual(item['category'],'US');self.assertEqual(item['direction'],'DOWN')
  self.assertEqual(len(item['links'][0]['impact']['logic_chain']),3)
  self.assertEqual(self.cfg['watchlist'],before)
  for table in ('plans','studies','paper_orders','dynamic_orders','paper_lots'):
   self.assertEqual(self.store.db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
  self.assertEqual(macro.measure(self.store,self.at),0)
  self.assertEqual(macro.view(self.store,self.at)['assets']['US:NVDA']['name'],spec['name'])

 def test_chain_rejects_false_quotes_and_unverified_stock_codes(self):
  n=self.news();r=self.chain_result(n);macro.validate(r,[n])
  for change in ('quote','source','first','inference','unknown'):
   bad=copy.deepcopy(r);i=bad['events'][0]['impacts'][0]
   if change=='quote':i['logic_chain'][0]['quote']='Fabricated policy statement.'
   if change=='source':i['logic_chain'][0]['news_id']='unknown'
   if change=='first':i['logic_chain'].reverse()
   if change=='inference':i['logic_chain'][1]['quote']='pretend evidence'
   if change=='unknown':i['asset']='US:MADEUP'
   with self.assertRaises(ValueError,msg=change):macro.validate(bad,[n])

 def test_shared_target_deduplicates_and_retains_conflicting_causes(self):
  for key,direction in [('one','UP'),('two','DOWN')]:
   n=self.news(key,body='Federal Reserve monetary policy '+key+' announcement changes interest-rate expectations.')
   r=self.chain_result(n,direction=direction)
   macro.research(self.store,self.cfg,[n],self.at,lambda *args:r)
  v=observation.view(self.store,self.at)
  self.assertEqual(len(v['items']),0);self.assertEqual(len(v['archived_items']),1);self.assertEqual(len(v['archived_items'][0]['links']),2)
  self.assertTrue(v['archived_items'][0]['conflicting']);self.assertEqual(v['archived_items'][0]['direction'],'MIXED')
  self.assertEqual(len(v['categories']),4)

 def test_withdrawn_news_marks_observation_for_review(self):
  n=self.news();r=self.chain_result(n);macro.research(self.store,self.cfg,[n],self.at,lambda *args:r)
  self.news(body='Federal Reserve withdraws the previous policy statement and publishes a correction on interest rates.')
  item=observation.view(self.store,self.at)['archived_items'][0]
  self.assertEqual(item['status'],'NEEDS_REVIEW');self.assertEqual(item['direction'],'UNCLEAR')
  self.assertEqual(item['links'][0]['status'],'INVALIDATED')

 def test_legacy_enrichment_replaces_reaction_anchor_and_archives_prior(self):
  n=self.news();old,_=self.research(n)
  selected=macro.select_news(self.store,*dynamic.window('2026-09-14T10:30:00+08:00'),self.at)
  self.assertEqual([x['id'] for x in selected],[n['id']])
  with self.store.db:self.store.db.execute('INSERT INTO macro_observations VALUES(?,?,?,?,?)',(old['id'],'US10Y',self.at,'FORWARD','{}'))
  r=self.chain_result(n);later=normalize_time('2026-09-14T14:00:00+08:00')
  macro.research(self.store,self.cfg,[n],later,lambda *args:r)
  self.assertEqual(self.store.db.execute('SELECT count(*) FROM macro_event_revisions').fetchone()[0],1)
  self.assertEqual(self.store.db.execute('SELECT count(*) FROM macro_observations').fetchone()[0],0)
  e=self.store.db.execute('SELECT * FROM macro_events').fetchone()
  self.assertEqual(e['created_at'],later);self.assertEqual(e['basis'],'RETROSPECTIVE')
  self.assertEqual(observation.view(self.store,later)['archived_items'][0]['status'],'WATCHING')

 def test_directory_parser_filters_test_issues_and_derivatives(self):
  raw=b'Symbol|Security Name|Test Issue|ETF\nNVDA|NVIDIA Corporation - Common Stock|N|N\nTEST|Testing|Y|N\nABCW|ABC Warrants|N|N\nGLD|Gold ETF|N|Y\nFile Creation Time: 09212026|||\n'
  rows=observation.parse_directory(raw,'nasdaq','https://www.nasdaqtrader.com/test')
  self.assertEqual([r['asset'] for r in rows],['US:NVDA','US:GLD'])
  self.assertEqual(rows[1]['kind'],'ETF')
  with self.assertRaises(ValueError):observation.parse_directory(b'ticker,name\nBAD,Wrong','nasdaq','test')

 def test_48_hour_backfill_and_incremental_overlap(self):
  published=lambda hours:normalize_time((datetime.fromisoformat(self.at)-timedelta(hours=hours)).isoformat())
  def row(hours):return {'source':'新浪财经快讯','url':'https://finance.sina.com.cn/'+str(hours),'published_at':published(hours),'title':'政策变化','body':'Federal Reserve monetary policy news from '+str(hours)+' hours ago.'}
  pages={1:[row(1),row(25)],2:[row(47),row(49)],50:[row(200)]};requests=[]
  def fetch(url):
   page=int(re.search(r'page=(\d+)',url).group(1));requests.append(page);return str(page).encode()
  import re
  with patch('ashare.dynamic_sources.parse_sina',side_effect=lambda raw:pages[int(raw)]),patch('ashare.external_news.FEEDS',[]),patch('ashare.macro_sources.collect',return_value={'added':0,'window_counts':{},'failures':[]}):
   result=ds.collect(self.store,published(.5),self.at,self.at,fetch)
   self.assertTrue(result['coverage']['sina_complete']);self.assertEqual(requests[:2],[1,2])
   times=[r[0] for r in self.store.db.execute('SELECT published_at FROM dynamic_news')]
   self.assertIn(published(47),times);self.assertNotIn(published(49),times)
   requests.clear();pages[51]=[row(210)]
   result=ds.collect(self.store,published(.5),self.at,self.at,fetch)
   self.assertEqual(requests,[1,51]);self.assertTrue(result['coverage']['sina_complete'])

 def test_batch_budget_does_not_discard_unselected_news(self):
  for i in range(6):self.news(str(i),body=f'Trump proposes a new tariff policy {i} affecting global supply chains.')
  selected=macro.select_news(self.store,*dynamic.window('2026-09-14T10:30:00+08:00'),self.at)
  self.assertEqual(len(selected),4)
  result=self.chain_result(selected[0]);macro.research(self.store,self.cfg,selected,self.at,lambda *args:result)
  remaining=macro.select_news(self.store,*dynamic.window('2026-09-14T10:30:00+08:00'),self.at)
  self.assertEqual(len(remaining),2);self.assertTrue({n['id'] for n in remaining}.isdisjoint({n['id'] for n in selected}))

 def test_production_research_cannot_silently_emit_legacy_impacts(self):
  n=self.news();result=self.result(n)
  with patch('ashare.macro.run_json',return_value=result):
   with self.assertRaisesRegex(ValueError,'逐步影响逻辑链'):macro.research(self.store,self.cfg,[n],self.at)
  self.assertEqual(self.store.db.execute('SELECT count(*) FROM macro_watchlist').fetchone()[0],0)
