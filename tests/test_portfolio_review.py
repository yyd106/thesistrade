import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from ashare.storage import Store,normalize_time
from test_config import load_config
from ashare.demo import seed,study,research_model,put_quote,SYMBOL
from ashare.slots import run_slot
from ashare.paper import settle
from ashare.review import run_review
from ashare.review_portfolio import build,ledger_at,valuation
from ashare.review_analysis import validate
from ashare.scheduler import schedule_review_retry,enqueue

n=normalize_time
class PortfolioReviewTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name)
  self.cfg=load_config(Path(__file__).resolve().parents[1]/'config.json')
  self.cfg.update(data_dir=self.tmp.name,watchlist=[{'symbol':SYMBOL,'name':'合成'}],paper_slippage_bps=0,paper_max_fill_qty=10000,slot_execution_mode='MODEL')
 def tearDown(self):self.s.close();self.tmp.cleanup()
 def buy(self):
  p=seed(self.s,self.cfg);study(self.s,self.cfg,p,model_fn=research_model(p),at='2026-09-15T09:01:00+08:00')
  at='2026-09-15T10:00:00+08:00';put_quote(self.s,at)
  from ashare.demo import decision_model
  run_slot(self.s,self.cfg,at,refresh_fn=lambda *a:{'event_status':{SYMBOL:'OK'}},model_fn=decision_model('BUY'),clock=lambda:at)
  put_quote(self.s,'2026-09-15T10:00:10+08:00');settle(self.s,self.cfg,'2026-09-15T10:00:10+08:00')
 def facts(self):return build(self.s,self.cfg,n('2026-09-15T19:30:00+08:00'),n('2026-09-16T19:30:00+08:00'),n('2026-09-17T09:00:00+08:00'))
 def test_carried_position_no_trade_period_vs_cumulative_and_future_quote(self):
  self.buy();put_quote(self.s,'2026-09-15T15:00:00+08:00',1010);put_quote(self.s,'2026-09-16T15:00:00+08:00',1020)
  put_quote(self.s,'2026-09-17T08:00:00+08:00',800)
  p=self.facts();r=p['positions'][0];qty=r['closing']['qty']
  self.assertEqual(p['totals']['period_fill_count'],0);self.assertEqual(r['period_profit_cents'],qty*10)
  self.assertEqual(r['closing']['price_cents'],1020);self.assertTrue(r['entry_research_ids'])
  self.assertNotEqual(r['period_profit_cents'],r['cumulative_profit_cents'])
  self.assertEqual(p['totals']['reviewed_position_count'],1)
 def test_late_history_disclosed_and_missing_never_zero(self):
  p={'symbol':SYMBOL,'origin':'watchlist','qty':100,'cost_cents':100500}
  end=n('2026-09-16T19:30:00+08:00');later=n('2026-09-17T08:00:00+08:00')
  q=put_quote(self.s,'2026-09-16T15:00:00+08:00',1050)
  with self.s.db:self.s.db.execute('UPDATE quotes SET first_seen_at=? WHERE id=?',(later,q))
  missing=valuation(self.s,p,end,end);self.assertIsNone(missing['unrealized_cents']);self.assertEqual(missing['quality'],'MISSING')
  actual=valuation(self.s,p,end,later);self.assertTrue(actual['late_quote']);self.assertEqual(actual['unrealized_cents'],4500)
 def test_partial_sale_replays_historical_cost_and_origins_separately(self):
  common={'symbol':SYMBOL,'fee_cents':500,'recorded_at':'2026-09-15','research_id':'p'}
  fills=[{**common,'id':'a','origin':'watchlist','side':'BUY','qty':100,'price_cents':1000,'realized_cents':0,'occurred_at':n('2026-09-15T10:00:00+08:00')},
   {**common,'id':'b','origin':'dynamic','side':'BUY','qty':100,'price_cents':900,'realized_cents':0,'occurred_at':n('2026-09-15T10:00:01+08:00')},
   {**common,'id':'c','origin':'watchlist','side':'SELL','qty':50,'price_cents':1100,'realized_cents':4250,'occurred_at':n('2026-09-16T10:00:00+08:00')}]
  states=ledger_at(fills,n('2026-09-16T19:30:00+08:00'))
  self.assertEqual(states['watchlist:'+SYMBOL]['cost_cents'],50250);self.assertEqual(states['watchlist:'+SYMBOL]['qty'],50)
  self.assertEqual(states['dynamic:'+SYMBOL]['qty'],100);self.assertEqual(states['dynamic:'+SYMBOL]['cost_cents'],90500)
  before=ledger_at(fills,n('2026-09-15T19:30:00+08:00'));self.assertEqual(before['watchlist:'+SYMBOL]['qty'],100)
 def test_full_coverage_and_optional_invalid_lesson(self):
  packet={'portfolio':{'positions':[{'key':'w:a','symbol':'a','research_ids':['r']}],'fills':[{'id':'f','symbol':'a'}]},'decisions':[]}
  row={'position_key':'w:a','verdict':'PENDING','reason':'尚待验证','supported_points':[],'contradicted_points':[],'pending_points':['等待披露'],'next_check':'下次业绩','research_ids':['r']}
  value={'summary':'复盘','positions':[row],'lessons':[{'category':'RESEARCH','symbol':'a、b','lesson':'x','applicability':'x','decision_ids':[],'fill_ids':['f']}]}
  result=validate(value,packet);self.assertEqual(len(result['positions']),1);self.assertFalse(result['lessons']);self.assertTrue(result['validation_warnings'])
  for change in ({'positions':[]},{'positions':[row,row]},{'positions':[{**row,'research_ids':['future']}]},{'positions':[{**row,'research_ids':[]}]}):
   with self.assertRaises(ValueError):validate({**value,**change},packet)
 def test_empty_complete_without_model_and_no_duplicates(self):
  args=dict(end='2026-09-16T19:30:00+08:00',clock=lambda:'2026-09-16T20:00:00+08:00')
  with patch('ashare.review.run_json') as model:
   first=run_review(self.s,self.cfg,**args);second=run_review(self.s,self.cfg,**args)
  self.assertEqual(first['status'],'NOT_NEEDED');self.assertEqual(second['status'],'ALREADY_DONE');model.assert_not_called()
 def test_retries_backoff_busy_limit_and_success(self):
  self.cfg.update(scheduler_enabled=True,model_enabled=True)
  end=n('2026-09-16T19:30:00+08:00')
  with self.s.db:self.s.db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)',('r',n('2026-09-15T19:30:00+08:00'),end,1,n('2026-09-16T19:40:00+08:00'),'fp','DEFERRED','{}'))
  self.assertIsNone(schedule_review_retry(self.s,self.cfg,n('2026-09-16T20:00:00+08:00')))
  busy=enqueue(self.s,'research',end,'busy')
  with self.s.db:self.s.db.execute("UPDATE jobs SET status='RUNNING' WHERE id=?",(busy,))
  self.assertIsNone(schedule_review_retry(self.s,self.cfg,n('2026-09-16T20:10:00+08:00')))
  with self.s.db:self.s.db.execute("UPDATE jobs SET status='DONE' WHERE id=?",(busy,))
  first=schedule_review_retry(self.s,self.cfg,n('2026-09-16T20:10:00+08:00'));self.assertTrue(first)
  self.assertIsNone(schedule_review_retry(self.s,self.cfg,n('2026-09-16T20:11:00+08:00')))
  with self.s.db:self.s.db.execute("UPDATE jobs SET status='FAILED',finished_at=? WHERE id=?",(n('2026-09-16T20:12:00+08:00'),first))
  self.assertIsNone(schedule_review_retry(self.s,self.cfg,n('2026-09-16T20:40:00+08:00')))
  second=schedule_review_retry(self.s,self.cfg,n('2026-09-16T20:42:00+08:00'));self.assertTrue(second)
  with self.s.db:self.s.db.execute("UPDATE jobs SET status='FAILED',finished_at=? WHERE id=?",(n('2026-09-16T20:44:00+08:00'),second))
  self.assertIsNone(schedule_review_retry(self.s,self.cfg,n('2026-09-16T22:00:00+08:00')))

if __name__=='__main__':unittest.main()
