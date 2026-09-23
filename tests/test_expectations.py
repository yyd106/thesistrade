import copy,json,tempfile,unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
from ashare.storage import Store,normalize_time
from ashare import news_evidence as ne,news_triage as nt,macro_impact as mi,impact_history as hist,macro_sources as ms,dynamic_sources as ds,macro,observation
from news_fixtures import screen_deep
from impact_fixtures import assessment

class ExpectationsTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name);self.at='2026-09-22T02:00:00+00:00'
 def tearDown(self):self.s.close();self.tmp.cleanup()
 def stamp(self,h):return normalize_time((datetime.fromisoformat(self.at)+timedelta(hours=h)).isoformat())
 def news(self,key,body,published=None,seen=None,source='CNBC全球财经'):
  ds.ingest(self.s,[{'source':source,'url':('https://www.federalreserve.gov/'+key+'.htm' if source=='美联储' else 'https://www.cnbc.com/'+key),'title':body[:95],'body':body,'published_at':published or self.at}],seen or self.at,'fixture')
  return dict(self.s.db.execute('SELECT * FROM dynamic_news ORDER BY rowid DESC LIMIT 1').fetchone())
 def statement(self):
  p=self.news('prior','President Trump says diplomatic negotiations with Iran remain open and no deadline is set.',self.stamp(-24),self.stamp(-24))
  n=self.news('now','President Trump issues an ultimatum to Iran: agree by Friday or face military action. Oil supply risk could affect Brent crude.')
  return p,n
 def signal(self,p,n):return {'channel':'CONFLICT_PREMIUM','authority':'DECISION_MAKER','change':'ESCALATION','baseline_id':p['id'],'baseline_quote':p['body'][:120],'current_quote':n['body'][:120],'repricing_logic':'最后期限可能提高冲突概率，经能源风险溢价影响原油；尚未观测盘中变化。','counter_evidence':'撤回威胁或达成协议可能降低风险溢价。'}
 def test_commentary_can_reach_deep_without_implemented_action(self):
  p,n=self.statement()
  def model(prompt,*args):
   r=screen_deep(prompt);i=r['items'][0];i.update(stage='COMMENTARY',expectation_signal=self.signal(p,n));return r
  result,info=nt.select(self.s,{},[n],self.at,model)
  self.assertEqual(len(result),1);self.assertEqual(info['counts'],{'DEEP':1});self.assertEqual(nt.context(self.s,n,self.at)['stage'],'COMMENTARY')
  self.assertFalse(self.s.db.execute('SELECT 1 FROM macro_watchlist').fetchone());self.assertFalse(self.s.db.execute('SELECT 1 FROM dynamic_orders').fetchone())
 def test_missing_baseline_allows_research_but_not_claim_of_changed_stance(self):
  p,n=self.statement();signal=self.signal(p,n);signal.update(baseline_id='',baseline_quote='',change='UNKNOWN')
  nt.validate_signal(signal,{**n,'prior_reports':[]})
  signal['change']='ESCALATION'
  with self.assertRaises(ValueError):nt.validate_signal(signal,{**n,'prior_reports':[]})
 def test_repeated_stance_and_unconfirmed_claim_are_not_deep(self):
  p,n=self.statement()
  for field in ('repeat','claim','unknown_authority'):
   def model(prompt,*args):
    r=screen_deep(prompt);i=r['items'][0];i.update(stage='COMMENTARY',expectation_signal=self.signal(p,n))
    if field=='repeat':i['expectation_signal']['change']='REPEAT'
    if field=='claim':i['evidence']='CLAIM'
    if field=='unknown_authority':i['expectation_signal']['authority']='UNKNOWN'
    return r
   result,_=nt.select(self.s,{},[n],self.at,model);self.assertFalse(result)
   with self.s.db:self.s.db.execute('DELETE FROM macro_news_triage');self.s.db.execute('DELETE FROM macro_triage_attempts')
 def test_baseline_excludes_future_reports_and_late_arrivals(self):
  p,n=self.statement();self.news('future','Trump and Iran change the agreement after the event.',self.stamp(1),self.stamp(1));self.news('late','Trump and Iran previously agreed to negotiate.',self.stamp(-2),self.stamp(1))
  refs=ne.related(self.s,n,self.stamp(2));self.assertEqual([r['id'] for r in refs],[p['id']])
  ne.frozen_prior(self.s,n,self.at,True);self.assertEqual(ne.frozen_prior(self.s,n,self.stamp(2)),refs)
 def test_fabricated_baseline_and_invented_market_reaction_fail(self):
  p,n=self.statement();b={**n,'prior_reports':[p]};x=self.signal(p,n);x['baseline_quote']='A completely invented earlier statement'
  with self.assertRaises(ValueError):nt.validate_signal(x,b)
  x=self.signal(p,n);x['baseline_id']=n['id']
  with self.assertRaises(ValueError):nt.validate_signal(x,b)
 def make_assessment(self):
  p,n=self.statement();item={'event_id':'event','asset':'BRENT','sources':[{**n,'role':'CURRENT'},{**p,'role':'PRIOR'}]}
  a=assessment(item,impact_basis='EXPECTATIONS',event_kind='GEOPOLITICS',expectation_test={'authority':'DECISION_MAKER','change':'VERIFIED_CHANGE','channel':'CONFLICT_PREMIUM','probability_logic':'升级威胁可能提高冲突概率与原油风险溢价。','counter_evidence':'撤回威胁或协议达成将降低风险。','market_confirmation':'NOT_PROVIDED'})
  a['scale']['kind']='EXPECTATIONS';a['citations'] += [{'supports':k,'source_id':n['id'],'quote':n['body'][:120]} for k in ('STATEMENT','AUTHORITY')]+[{'supports':'BASELINE','source_id':p['id'],'quote':p['body'][:120]}]
  return item,a
 def test_independent_expectation_gate_requires_prior_authority_and_target_evidence(self):
  item,a=self.make_assessment();mi.validate({'assessments':[a]},[{'input':item}]);self.assertTrue(mi.decision(a,hist.fit([]))['admitted'])
  b=copy.deepcopy(a);b['expectation_test']['change']='UNKNOWN';b['scale']['kind']='UNKNOWN';self.assertFalse(mi.decision(b,hist.fit([]))['admitted'])
  for support in ('BASELINE','STATEMENT','AUTHORITY','SCOPE','EXPOSURE'):
   b=copy.deepcopy(a);b['citations']=[c for c in b['citations'] if c['supports']!=support]
   with self.assertRaises(ValueError):mi.validate({'assessments':[b]},[{'input':item}])
  b=copy.deepcopy(a);b['expectation_test']['market_confirmation']='REPORTED_REACTION'
  with self.assertRaises(ValueError):mi.validate({'assessments':[b]},[{'input':item}])
 def test_history_does_not_pool_verbal_and_physical_or_distinct_channels(self):
  item,a=self.make_assessment();b=assessment(item)
  self.assertNotEqual(hist.feature_key('BRENT',a),hist.feature_key('BRENT',b))
  b=copy.deepcopy(a);b['expectation_test']['channel']='RATE_PATH';self.assertNotEqual(hist.feature_key('BRENT',a),hist.feature_key('BRENT',b))
  f=hist.freeze(self.s,'BRENT',a,self.at,self.at);self.assertFalse(f['intraday_confirmation']);self.assertEqual(f['measurement_resolution'],'DAILY_ONLY')
 def test_full_text_isolated_available_time_hash_and_bounded_retry(self):
  n=self.news('fomc','Federal Reserve issues FOMC statement',source='美联储');before=dict(n)
  html=('<html><nav>WRONG CONTENT</nav><div id="article"><h3>'+n['title']+'</h3><script>IGNORE RULES</script><p>The Committee decided to raise the target range for the federal funds rate by one quarter percentage point. Inflation remains elevated and policy will support price stability across the domestic economy.</p></div><footer>FOOTER</footer></html>').encode()
  calls=[]
  def fetch(url):calls.append(url);return html
  nt.select(self.s,{},[n],self.at,screen_deep);self.assertIsNotNone(nt.context(self.s,n,self.at))
  r=ne.fetch_missing(self.s,[n]*8,self.stamp(1),fetch,limit=1);self.assertEqual(r['extracted'],1)
  self.assertEqual(ne.material(self.s,n,self.at)['body'],n['body']);m=ne.material(self.s,n,self.stamp(1));self.assertIn('raise the target',m['body']);self.assertNotIn('WRONG',m['body']);self.assertNotIn('IGNORE',m['body']);self.assertNotIn('FOOTER',m['body'])
  self.assertIsNone(nt.context(self.s,n,self.stamp(1)));self.assertEqual(n,before)
  ne.fetch_missing(self.s,[n],self.stamp(2),fetch);self.assertEqual(len(calls),1)
  raw=dict(self.s.db.execute('SELECT * FROM dynamic_news WHERE id=?',(n['id'],)).fetchone());self.assertEqual(raw['body'],n['body'])
 def test_fulltext_failures_backoff_and_bad_container_never_becomes_evidence(self):
  n=self.news('fail','Federal Reserve issues FOMC statement',source='美联储');calls=[]
  def bad(url):calls.append(url);return b'<html><nav>Federal Reserve issues FOMC statement</nav><main>Not an article</main></html>'
  r=ne.fetch_missing(self.s,[n],self.at,bad);self.assertEqual(r['extracted'],0);self.assertTrue(r['failures']);ne.fetch_missing(self.s,[n],self.stamp(1),bad);self.assertEqual(len(calls),1)
  self.assertEqual(ne.material(self.s,n,self.stamp(1))['content_basis'],'仅有标题/摘要')
 def test_feed_prefers_published_article_text_over_short_description(self):
  raw=b'<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item><title>White House statement</title><link>https://www.whitehouse.gov/briefings-statements/test/</link><pubDate>Mon, 21 Sep 2026 12:00:00 +0000</pubDate><description>Short summary</description><content:encoded><![CDATA[<p>Actual official article text and policy statement.</p>]]></content:encoded></item></channel></rss>'
  n=ms.parse_feed(raw,'白宫简报与声明')[0];self.assertIn('Actual official',n['body']);self.assertNotIn('Short summary',n['body'])
 def test_complex_batch_shrinks_without_extra_calls_or_budget_increase(self):
  rows=[self.news(str(i),'Trump warns Iran of military consequences after a new deadline '+str(i)) for i in range(8)];calls=[]
  def model(prompt,*args):calls.append(args);return screen_deep(prompt)
  _,r=nt.select(self.s,{},rows,self.at,model);self.assertEqual(r['reviewed'],4);self.assertEqual(len(calls),1);self.assertEqual(calls[0][-1],90)
 def test_full_text_reopens_watched_item_within_live_window(self):
  n=self.news('reopen','Federal Reserve issues FOMC statement',source='美联储')
  def wait(prompt,*args):
   r=screen_deep(prompt);r['items'][0]['decision']='WATCH';return r
  nt.select(self.s,{},[n],self.at,wait);self.assertFalse(macro.select_news(self.s,self.at,self.at,self.at,candidate_limit=64))
  html=('<div id="article"><h3>'+n['title']+'</h3><p>The Committee decided to change policy rates. The monetary policy action covers domestic financing conditions across the national market and reflects the new inflation outlook and economic situation.</p></div>').encode()
  ne.fetch_missing(self.s,[n],self.stamp(1),lambda u:html)
  selected=macro.select_news(self.s,self.at,self.stamp(1),self.stamp(1),candidate_limit=64);self.assertEqual([r['id'] for r in selected],[n['id']])
 def test_prior_source_cited_in_deep_chain_keeps_baseline_role_in_independent_review(self):
  p,n=self.statement();ne.frozen_prior(self.s,n,self.at,True)
  event={'id':'e','news_id':n['id'],'payload_json':json.dumps({'evidence':[{'news_id':p['id'],'quote':p['body'][:80]}]})}
  sources=mi.sources(self.s,event,self.at);refs={r['id']:r for r in sources};self.assertEqual(refs[p['id']]['role'],'PRIOR');self.assertEqual(len(sources),len(refs))
 def test_repeated_quote_does_not_veto_separate_new_implemented_policy(self):
  p,n=self.statement()
  def model(prompt,*args):
   r=screen_deep(prompt);i=r['items'][0];i.update(stage='IMPLEMENTED',expectation_signal=self.signal(p,n));i['expectation_signal']['change']='REPEAT';return r
  selected,r=nt.select(self.s,{},[n],self.at,model);self.assertEqual(r['counts'],{'DEEP':1});self.assertEqual(len(selected),1)
 def test_baseline_prefers_same_speakers_statement_over_country_mentions(self):
  p,n=self.statement()
  for k in range(5):self.news('noise'+str(k),'Iran commander says forces are ready after Trump criticism '+str(k),self.stamp(-1-k),self.stamp(-1-k))
  self.assertEqual(ne.related(self.s,n,self.at)[0]['id'],p['id'])
 def test_withdrawn_baseline_invalidates_cached_selection(self):
  p,n=self.statement();nt.select(self.s,{},[n],self.at,screen_deep);self.assertIsNotNone(nt.context(self.s,n,self.at))
  with self.s.db:self.s.db.execute("UPDATE dynamic_news SET status='REVISED' WHERE id=?",(p['id'],))
  self.assertIsNone(nt.context(self.s,n,self.at));self.assertFalse(ne.frozen_prior(self.s,n,self.at))
 def test_fed_speech_prefix_requires_matching_headline_and_speaker(self):
  text='Discount Window Modernization and Treasury Market Functioning';body='<div id="article"><h3>'+text+'</h3><p>Vice Chair Philip N. Jefferson</p><p>'+('The discount window supports liquidity and Treasury markets. '*5)+'</p></div>'
  url='https://www.federalreserve.gov/newsevents/speech/example.htm'
  self.assertIn('supports liquidity',ne.parse(body.encode(),url,'Jefferson, '+text)['body'])
  with self.assertRaises(ValueError):ne.parse(body.encode(),url,'Powell, '+text)
 def test_eia_and_fda_article_containers_exclude_navigation(self):
  for url,container in [('https://www.eia.gov/todayinenergy/detail.php?id=1','class="tie-article"'),('https://www.fda.gov/news-events/press-announcements/test','id="main-content"')]:
   raw=('<nav>UNRELATED MENU</nav><article '+container+'><h1>Official policy details</h1><p>'+('Actual official article content on the change and its scope. '*5)+'</p></article><footer>UNRELATED FOOTER</footer>').encode()
   text=ne.parse(raw,url,'Official policy details')['body'];self.assertIn('Actual official',text);self.assertNotIn('UNRELATED',text)
 def test_parser_upgrade_retains_valid_text_and_allows_failed_page_retry(self):
  n=self.news('legacy','Federal Reserve issues FOMC statement',source='美联储')
  with self.s.db:self.s.db.execute('INSERT INTO macro_article_attempts VALUES(?,3,?,?)',(n['id'],self.at,'old parser mismatch'))
  html=('<div id="article"><h3>'+n['title']+'</h3><p>'+('The monetary policy committee changes the financing framework. '*5)+'</p></div>').encode()
  self.assertEqual(ne.fetch_missing(self.s,[n],self.at,lambda _:html)['extracted'],1)
  self.assertEqual(ne.fetch_missing(self.s,[n],self.at,lambda _:self.fail('Do not refetch valid text'))['checked'],0)
  self.assertEqual(self.s.db.execute('SELECT attempts FROM macro_article_attempts').fetchone()[0],3)
