import copy
import json
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
from ashare.storage import Store,normalize_time,digest
from test_config import load_config
from ashare.finance import PaperLedger
from ashare import dynamic,dynamic_sources as ds,dynamic_paper as dp
from ashare.paper import account,settle
from ashare.demo import seed,study,research_model,put_quote,SYMBOL
from ashare.slots import run_slot
from ashare.scheduler import schedule_dynamic_due,schedule_due

class DynamicTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
  self.cfg=load_config(Path(__file__).resolve().parents[1]/'config.json')
  self.cfg.update(data_dir=self.tmp.name,watchlist=[{'symbol':SYMBOL,'name':'海康威视'}],dynamic_enabled=True,slot_execution_mode='RULES',paper_slippage_bps=0,paper_max_fill_qty=10000)
  PaperLedger(self.store).initialize()
  self.at=normalize_time('2026-09-15T10:00:00+08:00')
 def tearDown(self):self.store.close();self.tmp.cleanup()
 def news(self,key='news',at=None,body='海康威视获得新产品审批，订单预计增长，仍需核对具体经营影响。'):
  at=at or self.at
  row={'source':'新浪财经快讯','url':'https://finance.sina.com.cn/'+key,'published_at':at,'title':body[:20],'body':body}
  ds.ingest(self.store,[row],at,'synthetic')
  return dict(self.store.db.execute('SELECT * FROM dynamic_news WHERE url=? ORDER BY rowid DESC LIMIT 1',(row['url'],)).fetchone())
 def case(self,key='news',at=None,basis='FORWARD',symbol=SYMBOL):
  at=at or self.at;n=self.news(key,at)
  cid=digest(key)[:24]
  analysis={'news_id':n['id'],'symbol':symbol,'theme':'technology','event_type':'APPROVAL','direction':'BULLISH','novelty':'NEW','impact':'审批可能影响订单','business_link':'直接公司事项','pricing':'尚未完全反映','priced_in':'NO','invalidation':'审批撤回','evidence':[{'news_id':n['id'],'quote':n['body']}],'direct_company_evidence':True}
  plan={'holding_days':3,'max_position_pct':5,'stop_bps':600,'take_profit_bps':1000,'buy_low_cents':990,'buy_high_cents':1010}
  with self.store.db:
   self.store.db.execute('INSERT INTO dynamic_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(cid,n['id'],symbol,'海康威视','technology','APPROVAL','BULLISH',at,normalize_time((datetime.fromisoformat(at)+timedelta(hours=12)).isoformat()),basis,'READY',json.dumps(analysis),json.dumps(plan)))
  return dict(self.store.db.execute('SELECT * FROM dynamic_cases WHERE id=?',(cid,)).fetchone())
 def quote(self,at=None,price=1000,symbol=SYMBOL):
  at=at or self.at;identity=digest(symbol+at+str(price))[:24]
  with self.store.db:self.store.db.execute('INSERT OR IGNORE INTO dynamic_quotes VALUES(?,?,?,?,?,?,?,?)',(identity,symbol,'海康威视',price,1000,at,at,'synthetic'))
  return ds.latest_quote(self.store,symbol,at)
 def buy(self,c,at=None):
  at=at or self.at;q=self.quote(at)
  with patch('ashare.dynamic.eligibility',return_value=([],{'passed':True})):
   return dp.submit(self.store,self.cfg,c,'BUY',q,at,'synthetic eligibility')
 def fill(self,at='2026-09-15T10:00:10+08:00',price=1000):
  at=normalize_time(at);self.quote(at,price);return dp.settle(self.store,self.cfg,at)
 def original_plan(self):
  packet=seed(self.store,self.cfg)
  result=study(self.store,self.cfg,packet,model_fn=research_model(packet),at='2026-09-15T09:01:00+08:00')
  r=self.store.db.execute('SELECT * FROM plans WHERE id=?',(result['plan_id'],)).fetchone()
  plan=json.loads(r['payload_json']);plan['max_stock_pct']=5
  with self.store.db:self.store.db.execute('UPDATE plans SET payload_json=? WHERE id=?',(json.dumps(plan),r['id']))
  return r['id']
 def original_slot(self,at=None):
  at=at or self.at;put_quote(self.store,at)
  return run_slot(self.store,self.cfg,at,clock=lambda:at,refresh_fn=lambda *args:{'event_status':{SYMBOL:'OK'}})
 def test_whole_account_review_includes_both_paths_and_closed_dynamic_position(self):
  from ashare.review_portfolio import build
  self.original_plan();self.original_slot();t=normalize_time('2026-09-15T10:00:05+08:00');put_quote(self.store,t);settle(self.store,self.cfg,t)
  c=self.case();self.buy(c);self.fill()
  put_quote(self.store,'2026-09-15T15:00:00+08:00',1000)
  start=normalize_time('2026-09-15T19:30:00+08:00');end=normalize_time('2026-09-16T19:30:00+08:00')
  q=self.quote(normalize_time('2026-09-16T10:00:00+08:00'),935)
  self.assertIn('id',dp.submit(self.store,self.cfg,c,'SELL',q,q['observed_at'],'synthetic close'))
  self.fill('2026-09-16T10:00:10+08:00',935);put_quote(self.store,'2026-09-16T15:00:00+08:00',1020)
  p=build(self.store,self.cfg,start,end,normalize_time('2026-09-17T09:00:00+08:00'))
  rows={r['origin']:r for r in p['positions']}
  self.assertEqual(set(rows),{'watchlist','dynamic'});self.assertEqual(rows['dynamic']['closing']['qty'],0)
  self.assertLess(rows['dynamic']['period_realized_cents'],0);self.assertEqual(p['totals']['holding_count'],1)
  self.assertEqual(rows['dynamic']['research_ids'],[c['id']]);self.assertTrue(rows['watchlist']['entry_research_ids'])

 def test_news_window_deduplication_revision_and_no_watchlist_evidence(self):
  start,end=dynamic.window('2026-09-15T10:37:58+08:00')
  self.assertEqual(start,normalize_time('2026-09-15T10:00:00+08:00'));self.assertEqual(end,normalize_time('2026-09-15T10:30:00+08:00'))
  first=self.news();self.news();self.assertEqual(self.store.db.execute('SELECT count(*) FROM dynamic_news').fetchone()[0],1)
  second=self.news(body='海康威视说明前述审批并未通过，原公告已经更正。')
  self.assertEqual(second['revision_of'],first['id'])
  self.assertEqual(self.store.db.execute('SELECT count(*) FROM documents').fetchone()[0],0)
  self.assertEqual(self.store.db.execute('SELECT count(*) FROM source_checks').fetchone()[0],0)
 def test_revision_invalidates_only_dynamic_plan(self):
  pid=self.original_plan();c=self.case();self.news(body='海康威视审批结果有更正，前述审批消息已撤回。')
  self.assertEqual(self.store.db.execute('SELECT status FROM dynamic_cases WHERE id=?',(c['id'],)).fetchone()[0],'INVALIDATED')
  self.assertEqual(self.store.db.execute('SELECT status FROM plans WHERE id=?',(pid,)).fetchone()[0],'ACTIVE')
 def test_candidates_require_catalog_and_supported_board(self):
  n=self.news(body='山东黄金与迈威生物受到市场关注，黄金价格变化。')
  catalog=[{'code':'600547','category':'A股','zwjc':'山东黄金'},{'code':'688062','category':'A股','zwjc':'迈威生物'}]
  self.assertEqual([x['symbol'] for x in ds.candidates(n,catalog)],['sh600547'])
  self.assertFalse(ds.candidates(n,[]))
  self.assertEqual([x['symbol'] for x in ds.candidates({'title':'公司动态','body':'600547发布新消息'},catalog)],['sh600547'])
  self.assertFalse(ds.candidates({'title':'编号','body':'16005470不是证券代码'},catalog))
 def test_source_parser_excludes_comments_and_future_news(self):
  raw=json.dumps({'result':{'data':{'feed':{'list':[{'id':1,'rich_text':'【公司消息】海康威视产品审批完成，需要继续核对。','create_time':'2026-09-15 10:00:00','ext':'{}','comment_list':{'private':'do not include'}}]}}}}).encode()
  rows=ds.parse_sina(raw);self.assertNotIn('private',json.dumps(rows))
  self.assertEqual(ds.ingest(self.store,rows,normalize_time('2026-09-15T09:59:59+08:00'),'synthetic'),0)
 def test_model_rejects_invented_ticker_and_quote(self):
  c=self.case();n=dict(self.store.db.execute('SELECT * FROM dynamic_news').fetchone());n['candidates']=[{'symbol':SYMBOL,'name':'海康威视'}]
  item=json.loads(c['analysis_json']);item.pop('direct_company_evidence');packet={'news':[n]}
  self.assertEqual(dynamic.validate({'summary':'研究','opportunities':[item]},packet)['opportunities'][0]['symbol'],SYMBOL)
  bad=copy.deepcopy(item);bad['symbol']='sh999999'
  with self.assertRaises(ValueError):dynamic.validate({'summary':'研究','opportunities':[bad]},packet)
  bad=copy.deepcopy(item);bad['evidence'][0]['quote']='并没有出现在新闻中的虚构引文'
  with self.assertRaises(ValueError):dynamic.validate({'summary':'研究','opportunities':[bad]},packet)
 def test_shared_cash_reserved_once_and_visible_to_watchlist(self):
  c=self.case();o=self.buy(c);self.assertIn('id',o)
  a=account(self.store,self.at);self.assertEqual(a['reserved_cents'],o['reserved_cents'])
  self.assertEqual(a['available_cents'],a['cash_cents']-o['reserved_cents'])
  again=self.buy(c);self.assertEqual(o['id'],again['id']);self.assertEqual(account(self.store,self.at)['reserved_cents'],o['reserved_cents'])
  self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],0)
 def test_dynamic_same_symbol_order_blocks_watchlist_only_at_account_gate(self):
  self.original_plan();self.buy(self.case())
  result=self.original_slot();self.assertEqual(result['decisions'][0]['status'],'BLOCKED')
  self.assertIn('EXISTING_OPEN_ORDER',result['decisions'][0]['reason'])
 def test_watchlist_order_blocks_dynamic_submission(self):
  self.original_plan();self.original_slot();o=self.buy(self.case())
  self.assertNotIn('id',o);self.assertIn('已有挂单',o['reason'])
 def test_dynamic_fill_is_not_visible_as_watchlist_position(self):
  c=self.case();o=self.buy(c);self.assertEqual(dp.settle(self.store,self.cfg,self.at),[])
  self.fill();a=account(self.store,normalize_time('2026-09-15T10:00:10+08:00'))
  self.assertFalse(a['positions']);self.assertEqual(a['dynamic_positions'][SYMBOL]['qty'],o['qty'])
  self.assertEqual(a['market_value_cents'],o['qty']*1000)
  self.assertEqual(a['cash_cents'],a['initial_cents']-o['qty']*1000-500)
  self.assertEqual(a['reserved_cents'],0)
  self.assertEqual(self.fill(),[])
 def test_t_plus_one_and_dynamic_sell_never_touches_watchlist_lots(self):
  self.original_plan();self.original_slot();at=normalize_time('2026-09-15T10:00:05+08:00');put_quote(self.store,at);settle(self.store,self.cfg,at)
  original_qty=account(self.store,at)['positions'][SYMBOL]['qty'];c=self.case();o=self.buy(c);self.assertIn('id',o);self.fill()
  low=normalize_time('2026-09-15T10:01:00+08:00');q=self.quote(low,935)
  out=dp.submit(self.store,self.cfg,c,'SELL',q,low,'stop');self.assertIn('T+1',out['reason'])
  later=normalize_time('2026-09-16T10:00:00+08:00');q=self.quote(later,935);out=dp.submit(self.store,self.cfg,c,'SELL',q,later,'stop')
  self.assertIn('id',out);self.assertEqual(out['qty'],o['qty'])
  self.fill('2026-09-16T10:00:10+08:00',935)
  a=account(self.store,normalize_time('2026-09-16T10:00:10+08:00'));self.assertEqual(a['positions'][SYMBOL]['qty'],original_qty);self.assertFalse(a['dynamic_positions'])
 def test_dynamic_holdings_never_enter_original_slot_scope(self):
  c=self.case(symbol='sh600547');q=self.quote(symbol='sh600547')
  with patch('ashare.dynamic.eligibility',return_value=([],{})):o=dp.submit(self.store,self.cfg,c,'BUY',q,self.at,'test')
  later=normalize_time('2026-09-15T10:00:10+08:00');self.quote(later,symbol='sh600547');dp.settle(self.store,self.cfg,later)
  self.original_plan();result=self.original_slot('2026-09-15T10:01:00+08:00')
  self.assertEqual({d['symbol'] for d in result['decisions']},{SYMBOL})
 def test_insufficient_pool_never_overspends(self):
  with self.store.db:self.store.db.execute("UPDATE paper_accounts SET cash_cents=1000 WHERE id='DEMO_PAPER'")
  result=self.buy(self.case());self.assertNotIn('id',result);self.assertIn('额度不足',result['reason'])
  self.assertEqual(account(self.store,self.at)['cash_cents'],1000)
 def test_expired_buy_releases_reserved_funds(self):
  o=self.buy(self.case());expiry=normalize_time('2026-09-15T10:05:00+08:00');dp.settle(self.store,self.cfg,expiry)
  self.assertEqual(account(self.store,expiry)['reserved_cents'],0)
  self.assertEqual(self.store.db.execute('SELECT status FROM dynamic_orders WHERE id=?',(o['id'],)).fetchone()[0],'EXPIRED')
 def test_original_result_identical_when_dynamic_has_no_exposure(self):
  pid=self.original_plan();before=dict(self.store.db.execute('SELECT * FROM plans WHERE id=?',(pid,)).fetchone())
  self.case(symbol='sh600547');r=self.original_slot()
  self.assertEqual(r['decisions'][0]['status'],'SUBMITTED')
  self.assertEqual(before,dict(self.store.db.execute('SELECT * FROM plans WHERE id=?',(pid,)).fetchone()))
  self.assertEqual(self.store.db.execute('SELECT count(*) FROM dynamic_orders').fetchone()[0],0)
 def test_observations_require_completed_future_bars_and_preserve_bias(self):
  c=self.case(basis='RETROSPECTIVE')
  def data_until(days):
   bars=[['2026-09-'+str(day).zfill(2),'10','10.1','10.3','9.9','1000'] for day in days]
   for sym in (SYMBOL,'sh000300'):
    with self.store.db:self.store.db.execute('INSERT OR REPLACE INTO dynamic_market VALUES(?,?,?)',(sym,self.at,json.dumps({'bars':bars,'raw_path':'synthetic'})))
  data_until([14,15,16,17]);self.assertEqual(dynamic.measure(self.store,self.cfg,'2026-09-18T08:00:00+00:00'),0)
  data_until([14,15,16,17,18,21]);self.assertEqual(dynamic.measure(self.store,self.cfg,'2026-09-21T08:00:00+00:00'),1)
  o=self.store.db.execute('SELECT * FROM dynamic_observations').fetchone();self.assertEqual(o['basis'],'RETROSPECTIVE');self.assertGreater(o['entry_at'],c['created_at'])
  self.assertEqual(dynamic.history_stats(self.store,SYMBOL,'APPROVAL','BULLISH','2026-09-22T08:00:00+00:00')['count'],0)
 def test_history_calibration_excludes_future_retrospective_and_overlap(self):
  base=datetime.fromisoformat(normalize_time('2026-01-01T09:00:00+08:00'))
  for i in range(32):
   at=(base+timedelta(days=i*7)).isoformat();c=self.case('sample'+str(i),at,basis='RETROSPECTIVE' if i==31 else 'FORWARD')
   entry=(base+timedelta(days=i*7,hours=1)).isoformat();exit=(base+timedelta(days=i*7+3)).isoformat()
   ready='2027-01-01T00:00:00+00:00' if i==30 else exit
   with self.store.db:self.store.db.execute('INSERT INTO dynamic_observations VALUES(?,?,?,?,?,?)',(c['id'],entry,exit,ready,c['basis'],json.dumps({'status':'MEASURED','net_return_bps':150,'excess_return_bps':100})))
  s=dynamic.history_stats(self.store,SYMBOL,'APPROVAL','BULLISH',self.at)
  self.assertEqual(s['count'],30);self.assertTrue(s['passed']);self.assertEqual(s['train']['count'],20);self.assertEqual(s['holdout']['count'],10)
  overlap=self.case('overlap',(base+timedelta(days=1)).isoformat())
  with self.store.db:self.store.db.execute('INSERT INTO dynamic_observations VALUES(?,?,?,?,?,?)',(overlap['id'],(base+timedelta(days=1)).isoformat(),(base+timedelta(days=3)).isoformat(),(base+timedelta(days=4)).isoformat(),'FORWARD',json.dumps({'status':'MEASURED','net_return_bps':999,'excess_return_bps':999})))
  self.assertEqual(dynamic.history_stats(self.store,SYMBOL,'APPROVAL','BULLISH',self.at)['count'],30)
 def test_no_model_confidence_can_bypass_history_and_data_gates(self):
  c=self.case();blocks,stats=dynamic.eligibility(self.store,self.cfg,c,self.at)
  self.assertFalse(stats['passed']);self.assertTrue(any('前向历史' in s for s in blocks))
  q=self.quote();o=dp.submit(self.store,self.cfg,c,'BUY',q,self.at,'模型确信')
  self.assertNotIn('id',o);self.assertEqual(self.store.db.execute('SELECT count(*) FROM dynamic_orders').fetchone()[0],0)
 def test_scheduler_is_separate_deduplicated_and_skips_old_execution(self):
  schedule_due(self.store,self.cfg,self.at)
  original=[tuple(r) for r in self.store.db.execute("SELECT id,kind,scheduled_at,status FROM jobs WHERE kind NOT LIKE 'dynamic_%'")]
  schedule_dynamic_due(self.store,self.cfg,self.at);schedule_dynamic_due(self.store,self.cfg,self.at)
  self.assertEqual(original,[tuple(r) for r in self.store.db.execute("SELECT id,kind,scheduled_at,status FROM jobs WHERE kind NOT LIKE 'dynamic_%'")])
  self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE kind='dynamic_cycle'").fetchone()[0],1)
  schedule_dynamic_due(self.store,self.cfg,normalize_time('2026-09-15T13:00:00+08:00'))
  self.assertEqual(self.store.db.execute("SELECT status FROM jobs WHERE kind='dynamic_slot' AND scheduled_at=?",(self.at,)).fetchone()[0],'MISSED')
 def test_paused_dynamic_discovery_still_schedules_owned_position_management(self):
  c=self.case();self.buy(c);self.fill();self.cfg['dynamic_enabled']=False
  schedule_dynamic_due(self.store,self.cfg,normalize_time('2026-09-15T10:01:00+08:00'))
  self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE kind='dynamic_cycle'").fetchone()[0],0)
  self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE kind='dynamic_slot'").fetchone()[0],1)
 def test_view_keeps_original_check_state_and_quotes_separate(self):
  pid=self.original_plan();before=self.store.latest_quote(SYMBOL,self.at);self.case();self.quote(price=1020)
  v=dynamic.view(self.store,self.cfg,self.at)
  self.assertEqual(v['case_count'],1);self.assertEqual(v['items'][0]['quote']['price_cents'],1020)
  self.assertEqual(self.store.latest_quote(SYMBOL,self.at),before)
  self.assertEqual(self.store.db.execute('SELECT count(*) FROM latest_trade_checks').fetchone()[0],0)
 def test_dynamic_profit_is_excluded_from_watchlist_review_attribution(self):
  from ashare.paper import mark_equity
  self.buy(self.case());self.fill()
  later=normalize_time('2026-09-15T10:01:00+08:00');self.quote(later,1050)
  a=mark_equity(self.store,later)
  self.assertGreater(a['equity_cents'],a['initial_cents'])
  self.assertEqual(a['watchlist_equity_cents'],a['initial_cents'])
 def test_late_dynamic_slot_never_replays_a_trade(self):
  self.case()
  with patch('ashare.dynamic_paper.now',return_value=normalize_time('2026-09-15T10:10:00+08:00')),patch('ashare.dynamic_sources.refresh_quotes') as refresh:
   result=dp.run_slot(self.store,self.cfg,self.at)
   self.assertEqual(result['status'],'MISSED');refresh.assert_not_called()
 def test_ingest_revision_invalidates_owned_dynamic_position(self):
  c=self.case();self.buy(c);self.fill()
  self.news(body='海康威视正式更正此前消息，产品审批已经撤销，需重新核验。')
  self.assertEqual(self.store.db.execute('SELECT status FROM dynamic_cases WHERE id=?',(c['id'],)).fetchone()[0],'INVALIDATED')
 def test_partial_fill_fees_and_reservations(self):
  self.cfg['paper_max_fill_qty']=100;c=self.case();o=self.buy(c)
  self.fill();a=account(self.store,normalize_time('2026-09-15T10:00:10+08:00'))
  self.assertEqual(a['dynamic_positions'][SYMBOL]['qty'],100);self.assertGreater(a['reserved_cents'],0)
  for second in (20,30,40):self.fill('2026-09-15T10:00:'+str(second)+'+08:00')
  self.assertEqual(self.store.db.execute('SELECT sum(fee_cents) FROM dynamic_fills').fetchone()[0],500)
  self.assertEqual(account(self.store,normalize_time('2026-09-15T10:00:40+08:00'))['reserved_cents'],0)

if __name__=='__main__':unittest.main()
