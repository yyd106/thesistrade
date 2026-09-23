import copy,json,tempfile,unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
from impact_fixtures import approve_pending
from ashare.storage import Store,normalize_time,digest
from test_config import load_config
from ashare import observation,observation_pool as pool,macro,macro_queue,dynamic,dynamic_sources as ds

class PoolTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
  self.cfg=load_config(Path(__file__).resolve().parents[1]/'config.json');self.cfg['data_dir']=self.tmp.name
  self.at=normalize_time('2026-09-22T12:00:00+08:00')
 def tearDown(self):self.store.close();self.tmp.cleanup()
 def when(self,hours):return normalize_time((datetime.fromisoformat(self.at)+timedelta(hours=hours)).isoformat())
 def target(self,asset,strength='HIGH',age=0,horizon='DAYS',source='美联储'):
  spec={'asset':asset,'name':asset,'category':'US','kind':'STOCK','unit':'美元'}
  impact={'asset':asset,'direction':'UP','strength':strength,'strength_basis':'产业成本可能变化','conditions':'新政策实施','invalidation':'政策撤回','mechanism':'成本变化传入利润','watch':'实际政策',
   'logic_chain':[{'kind':'FACT','statement':'新的政策事实','news_id':'news','quote':'An exact new policy statement.'},{'kind':'INFERENCE','statement':'可能影响利润','news_id':'','quote':''}]}
  return {**spec,'added_at':self.when(-age),'updated_at':self.at,'links':[{'event_id':asset,'headline':'公开政策变化','published_at':self.when(-age),'source':source,'status':'TRACKING','horizon':horizon,'theme':'MONETARY','impact':impact,'materiality':{'admitted':True,'state':'ADMITTED','reason':'Explicit approved reviewer fixture'}}],'status':'WATCHING','strength':strength,'direction':'UP'}
 def persist(self,items):
  with self.store.db:
   for item in items:
    n=self.news(item['asset'],body='Federal Reserve monetary policy change '+item['asset'],published=item['links'][0]['published_at'])
    impact=copy.deepcopy(item['links'][0]['impact']);impact['logic_chain'][0]['news_id']=n['id'];impact['logic_chain'][0]['quote']=n['body']
    event={'headline':item['asset']+'政策变化','horizon':item['links'][0]['horizon'],'theme':'MONETARY','impacts':[impact]}
    eid=digest(item['asset']+n['id'])
    self.store.db.execute('INSERT INTO macro_events VALUES(?,?,?,?,?,?,?)',(eid,n['id'],self.at,'FORWARD','MONETARY','TRACKING',json.dumps(event)))
    observation.sync_event(self.store,eid,event,self.at,{item['asset']:item})
  approve_pending(self.store,self.at)
 def news(self,key,body=None,published=None,source='新浪财经快讯'):
  body=body or ('Trump announces tariff policy '+key+' with global trade effects.')
  ds.ingest(self.store,[{'title':body[:95],'body':body,'source':source,'url':'https://finance.sina.com.cn/'+key+'.htm','published_at':published or self.at}],self.at,'test')
  return dict(self.store.db.execute('SELECT * FROM dynamic_news ORDER BY rowid DESC LIMIT 1').fetchone())
 def select(self):return macro.select_news(self.store,*dynamic.window(self.at),self.at,self.cfg)
 def test_capacity_and_core_identity_are_separate(self):
  items=[self.target('US:T'+str(i)) for i in range(30)]
  items+=[self.target(w['symbol']) for w in self.cfg['watchlist']]
  original=copy.deepcopy(self.cfg['watchlist']);r=pool.allocate(self.store,items,self.at,self.cfg)
  self.assertEqual(r['counts']['active'],18);self.assertEqual(r['counts']['focus'],8);self.assertEqual(len(r['items']),40)
  self.assertEqual(self.cfg['watchlist'],original);self.assertEqual(len(r['archived_items']),12)
 def test_hysteresis_prevents_equal_score_churn_but_admits_stronger_evidence(self):
  self.cfg['dynamic_observation_policy']={**pool.DEFAULTS,'active_limit':2,'focus_limit':1}
  with self.store.db:
   for asset in ('US:Z','US:Y'):
    self.store.db.execute('INSERT INTO macro_watchlist VALUES(?,?,?,?)',(asset,self.at,self.at,'{}'))
    self.store.db.execute('INSERT INTO macro_watch_state VALUES(?,?,?)',(asset,self.at,json.dumps({'pool_tier':'ACTIVE'})))
  first=pool.allocate(self.store,[self.target('US:Z',strength='MEDIUM'),self.target('US:Y',strength='MEDIUM'),self.target('US:A',strength='MEDIUM')],self.at,self.cfg)
  self.assertEqual({i['asset'] for i in first['items']},{'US:Z','US:Y'})
  stronger=pool.allocate(self.store,[self.target('US:Z',strength='MEDIUM'),self.target('US:Y',strength='MEDIUM'),self.target('US:A',strength='HIGH')],self.at,self.cfg)
  self.assertIn('US:A',{i['asset'] for i in stronger['items']})
 def test_short_and_long_expiry_use_news_time_not_reanalysis_time(self):
  values=[self.target('US:SHORT',age=72),self.target('US:OLD',age=168),self.target('US:LONG',age=100,horizon='MONTHS'),self.target('US:DUE',age=168,horizon='MONTHS'),self.target('US:END',age=720,horizon='MONTHS')]
  r=pool.allocate(self.store,values,self.at,self.cfg);tiers={i['asset']:i['pool_tier'] for i in r['items']+r['archived_items']}
  self.assertEqual(tiers['US:SHORT'],'COOLING');self.assertEqual(tiers['US:OLD'],'ARCHIVED');self.assertEqual(tiers['US:LONG'],'FOCUS');self.assertEqual(tiers['US:DUE'],'COOLING');self.assertEqual(tiers['US:END'],'ARCHIVED')
 def test_all_protected_positions_survive_overflow_and_block_new_admission(self):
  values=[self.target('US:T'+str(i),age=200) for i in range(20)]+[self.target('US:NEW')]
  with patch('ashare.observation_pool.protected_assets',return_value={i['asset'] for i in values[:-1]}):r=pool.allocate(self.store,values,self.at,self.cfg)
  self.assertEqual(r['counts']['active'],20);self.assertEqual(r['counts']['protected_overflow'],2);self.assertEqual(r['archived_items'][0]['asset'],'US:NEW')
 def test_protection_reads_both_order_and_holding_lines_without_mutating_them(self):
  self.store.db.execute('PRAGMA foreign_keys=OFF')
  with self.store.db:
   self.store.db.execute('INSERT INTO paper_lots VALUES(?,?,?,?,?)',('lot','sh600000',100,1000,'2026-09-21'))
   self.store.db.execute('INSERT INTO dynamic_lots VALUES(?,?,?,?,?,?)',('dlot','case','sh600001',100,1000,'2026-09-21'))
   self.store.db.execute('INSERT INTO paper_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('order','decision','plan','sh600002','BUY',100,0,100,10000,self.at,self.when(2),'OPEN'))
  # Paper order fixture uses its actual column layout to ensure query coverage.
  self.assertTrue({'sh600000','sh600001','sh600002'}<=pool.protected_assets(self.store))
 def test_reconcile_is_idempotent_and_archive_is_bounded_and_addressable(self):
  self.persist([self.target('US:T'+str(i)) for i in range(50)])
  pool.reconcile(self.store,self.cfg,self.at);count=self.store.db.execute('SELECT count(*) FROM macro_watch_transitions').fetchone()[0]
  pool.reconcile(self.store,self.cfg,self.at);self.assertEqual(self.store.db.execute('SELECT count(*) FROM macro_watch_transitions').fetchone()[0],count)
  r=observation.view(self.store,self.at,self.cfg);self.assertEqual(r['archive_count'],32);self.assertEqual(len(r['archived_items']),25)
  second=observation.view(self.store,self.at,self.cfg,25);self.assertEqual(len(second['archived_items']),7)
  located=observation.view(self.store,self.at,self.cfg,asset=second['archived_items'][-1]['asset']);self.assertEqual(located['archive_offset'],25)
 def test_invalidated_evidence_exits_unprotected_pool(self):
  item=self.target('US:GONE');item['links'][0]['status']='INVALIDATED';r=pool.allocate(self.store,[item],self.at,self.cfg)
  self.assertFalse(r['items']);self.assertEqual(r['archived_items'][0]['pool_tier'],'ARCHIVED')
 def test_new_fact_reactivates_archived_target(self):
  old=self.target('US:BACK',age=200);first=pool.allocate(self.store,[old],self.at,self.cfg);self.assertFalse(first['items'])
  old['links']+=self.target('US:BACK')['links'];second=pool.allocate(self.store,[old],self.at,self.cfg);self.assertEqual(second['items'][0]['pool_tier'],'FOCUS')
 def test_queue_bounded_and_expired_candidates_do_not_consume_budget(self):
  for i in range(90):self.news('n'+str(i))
  old=self.news('old',published=self.when(-49));self.store.db.execute("INSERT INTO macro_news(news_id,status) VALUES(?,'PENDING')",(old['id'],));self.store.db.commit()
  self.assertEqual(len(self.select()),4)
  counts=dict(self.store.db.execute('SELECT status,count(*) FROM macro_news GROUP BY status'))
  self.assertEqual(counts['PENDING'],64);self.assertEqual(counts['DORMANT'],26);self.assertEqual(counts['EXPIRED'],1)
 def test_syndicated_reporting_groups_but_numeric_and_negation_changes_do_not(self):
  body='Trump announces tariff policy on imported goods. Officials confirmed the new measure will affect global supply chains and production costs.'
  a=self.news('one',body=body);b=self.news('two',body=body+' More coverage.')
  c=self.news('three',body=body.replace('new measure','new 25% measure'));d=self.news('four',body=body.replace('confirmed','denied'))
  selected=self.select();self.assertEqual(len(selected),3)
  self.assertIn(c['id'],{n['id'] for n in selected});self.assertIn(d['id'],{n['id'] for n in selected})
  self.assertEqual(self.store.db.execute("SELECT count(*) FROM macro_news WHERE status='MERGED'").fetchone()[0],1)
 def test_completed_reprint_does_not_consume_another_model_slot(self):
  body='Trump announces tariff policy on imported goods. Officials confirmed the new measure will affect global supply chains and production costs.'
  old=self.news('old',body=body,published=self.when(-1));self.store.db.execute("INSERT INTO macro_news(news_id,status) VALUES(?,'DONE')",(old['id'],));self.store.db.commit()
  self.news('new',body=body+' More coverage.');self.assertFalse(self.select())
 def test_focus_priority_leaves_room_for_new_themes(self):
  self.persist([self.target('US:FOCUS')]);pool.reconcile(self.store,self.cfg,self.at)
  for i in range(6):self.news('focus'+str(i),body='Federal Reserve rate hike policy '+str(i)+' affects the dollar.',source='美联储')
  for i in range(3):self.news('health'+str(i),body='FDA approves new vaccine policy '+str(i)+' for public health.')
  selected=self.select();self.assertEqual(len(selected),4)
  self.assertEqual(sum('FDA' in n['body'] for n in selected),2)
 def test_policy_rejects_invalid_capacity_relationships(self):
  with self.assertRaises(ValueError):pool.policy({'dynamic_observation_policy':{'active_limit':5,'focus_limit':8}})
  with self.assertRaises(ValueError):pool.policy({'dynamic_observation_policy':{'cooldown_hours':999}})
 def test_priority_context_is_bounded_public_research_without_extra_model_calls(self):
  self.persist([self.target('US:T'+str(i)) for i in range(20)]);pool.reconcile(self.store,self.cfg,self.at)
  n=self.news('new');self.select();calls=[]
  def model(prompt,*args):calls.append(prompt);return {'summary':'本批没有新的可确认传导','events':[]}
  macro.research(self.store,self.cfg,[n],self.at,model)
  self.assertEqual(len(calls),1)
  packet=json.loads(calls[0].split('<UNTRUSTED_WORLD_NEWS>')[1].split('</UNTRUSTED_WORLD_NEWS>')[0]);self.assertEqual(len(packet['priority_research_assets']),8)
  self.assertTrue(all(len(i['prior_public_causes'])==1 for i in packet['priority_research_assets']))
  for private in ('protected','paper_accounts','portfolio','priority_score','cash_cents'):self.assertNotIn(private,json.dumps(packet))
 def test_policy_implementation_and_country_changes_are_not_merged(self):
  body='Trump proposes tariff policy on imported goods. Officials confirmed the new measure will affect global supply chains and production costs in China.'
  self.news('proposal',body=body);self.news('implemented',body=body.replace('proposes','implements'));self.news('country',body=body.replace('China','Japan'))
  self.assertEqual(len(self.select()),3)
 def test_copied_reporting_cannot_extend_catalyst_deadline(self):
  item=self.target('US:TTL',age=71);item['links'][0]['catalyst_at']=self.when(-73)
  r=pool.allocate(self.store,[item],self.at,self.cfg);self.assertFalse(r['items']);self.assertEqual(r['archived_items'][0]['pool_tier'],'COOLING')
