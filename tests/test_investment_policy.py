import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
from ashare.storage import Store,normalize_time,digest
from test_config import load_config
from ashare.finance import PaperLedger
from ashare.investment_policy import VERSION,seed,FIXED
from ashare.global_market import SCALE,latest,notional,fresh,session_open
from ashare import global_paper,global_research,portfolio_risk

class InvestmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.cfg=load_config(Path(__file__).resolve().parents[1]/'config.json')
        self.cfg.update(data_dir=self.tmp.name,investment_policy=VERSION,paper_slippage_bps=0,watchlist=[],model_enabled=True)
        self.at=normalize_time('2026-09-23T10:00:00+08:00')
        PaperLedger(self.store).initialize();seed(self.store,self.at)
        with self.store.db:self.store.db.execute("UPDATE paper_flows SET created_at='2026-09-01T00:00:00+00:00'")
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def later(self,n):return normalize_time((datetime.fromisoformat(self.at)+timedelta(seconds=n)).isoformat())
    def quote(self,asset='BTC',price=100,at=None,fx=7,fx_at=None):
        at=at or self.at;pid=int(price*1e6);fid=int(fx*1e6);qid=digest(asset+at+str(price)+str(fx))[:24]
        with self.store.db:self.store.db.execute('INSERT OR IGNORE INTO global_quotes VALUES(?,?,?,?,?,?,?,?)',(qid,asset,at,at,pid,fid,fx_at or at,json.dumps({'session':{}})))
        return latest(self.store,asset,at)
    def plan(self,asset='BTC',at=None):
        at=at or self.at;item={'asset':asset,'links':[]};pid='plan:'+asset+at
        payload={'kind':'PAPER_TRADE','blockers':[],'holding_days':5,'thesis':'test hypothesis','analysis':{},'event_fingerprint':digest(json.dumps([],ensure_ascii=False,sort_keys=True)),
                 'levels':{'buy_low_micros':99_000_000,'buy_high_micros':101_000_000},'max_position_pct':5}
        expiry=normalize_time((datetime.fromisoformat(at)+timedelta(hours=12)).isoformat())
        with self.store.db:self.store.db.execute('INSERT INTO global_plans VALUES(?,?,?,?,?,?,?)',(pid,asset,at,expiry,'fingerprint','ACTIVE',json.dumps(payload)))
        return global_research.active_plan(self.store,asset,at),item
    def buy(self,asset='BTC'):
        q=self.quote(asset);plan,item=self.plan(asset)
        o=global_paper.submit(self.store,self.cfg,asset,'BUY',q,plan,item,self.at)
        self.assertEqual(o['status'],'OPEN',o);return o,plan,item
    def test_fractional_fill_cash_conservation_idempotence_and_historical_review(self):
        o,plan,item=self.buy();cash=10_000_000
        self.assertEqual(global_paper.settle(self.store,self.cfg,self.at,{'BTC':item}),[])
        self.quote(at=self.later(60))
        self.assertEqual(len(global_paper.settle(self.store,self.cfg,self.later(60),{'BTC':item})),1)
        self.assertEqual(global_paper.settle(self.store,self.cfg,self.later(60),{'BTC':item}),[])
        from ashare.paper import account
        a=account(self.store,self.later(60));p=a['global_positions']['BTC']
        self.assertEqual(a['cash_cents']+p['cost_cents'],cash)
        self.assertGreater(p['qty'],0);self.assertNotEqual(p['qty']%SCALE,0)
        self.assertEqual(a['equity_cents'],a['cash_cents']+p['market_value_cents'])
        from ashare.review_portfolio import build
        review=build(self.store,self.cfg,self.later(-1),self.later(120),self.later(120))
        self.assertEqual(review['closing']['cash_cents'],a['cash_cents'])
        self.assertEqual(review['positions'][0]['closing']['cost_cents'],p['cost_cents'])
        self.assertEqual(review['positions'][0]['closing']['qty_scale'],SCALE)
        self.assertEqual(review['totals']['period_profit_cents'],-review['totals']['period_fee_cents'])
    def test_stale_fx_future_quote_and_closed_us_block(self):
        p,i=self.plan();q=self.quote(fx_at=self.later(-3601))
        self.assertFalse(fresh(q,self.at));self.assertFalse(fresh(q,self.later(-1)))
        self.assertEqual(global_paper.submit(self.store,self.cfg,'BTC','BUY',q,p,i,self.at)['status'],'BLOCKED')
        self.assertFalse(session_open('US:AAPL',self.at,{'session':{'start':1,'end':2}}))
        self.assertTrue(session_open('BTC',self.at,{}))
    def test_revised_event_and_superseded_plan_cancel_unfilled_buy(self):
        o,p,i=self.buy();self.quote(at=self.later(60))
        with self.store.db:self.store.db.execute("UPDATE global_plans SET status='SUPERSEDED' WHERE id=?",(p['id'],))
        self.assertEqual(global_paper.settle(self.store,self.cfg,self.later(60),{'BTC':i}),[])
        self.assertEqual(self.store.db.execute('SELECT status FROM global_orders').fetchone()[0],'CANCELLED')
    def test_shared_reservations_and_never_negative_cash(self):
        first,_,_=self.buy('BTC');second,_,_=self.buy('ETH')
        from ashare.paper import account
        a=account(self.store,self.at)
        self.assertEqual(a['reserved_cents'],first['reserved_cents']+second['reserved_cents'])
        self.assertEqual(a['available_cents'],a['cash_cents']-a['reserved_cents'])
        # A concurrent process cannot spend money reserved by another ledger.
        with self.store.db:self.store.db.execute("UPDATE paper_accounts SET cash_cents=?",(a['reserved_cents']-1,))
        self.quote(at=self.later(60));self.assertEqual(global_paper.settle(self.store,self.cfg,self.later(60),{'BTC':{'asset':'BTC','links':[]}}),[])
        self.assertGreaterEqual(self.store.db.execute('SELECT cash_cents FROM paper_accounts').fetchone()[0],0)
    def test_drawdown_threshold_cancels_buys_and_latches_after_restart_and_rebound(self):
        o,p,i=self.buy();portfolio_risk.refresh(self.store,self.cfg,self.at)
        with self.store.db:self.store.db.execute('UPDATE paper_accounts SET cash_cents=7500000')
        state=portfolio_risk.refresh(self.store,self.cfg,self.later(1))
        self.assertEqual(state['drawdown_bps'],2500);self.assertTrue(state['halted'])
        row=self.store.db.execute('SELECT * FROM global_orders').fetchone();self.assertEqual(row['status'],'CANCELLED');self.assertEqual(row['reserved_cents'],0)
        self.store.close();self.store=Store(self.tmp.name)
        with self.store.db:self.store.db.execute('UPDATE paper_accounts SET cash_cents=11000000')
        self.assertTrue(portfolio_risk.refresh(self.store,self.cfg,self.later(2))['halted'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM portfolio_risk_events').fetchone()[0],1)
        # Dean is told once, as information; the latch staying on raises nothing more.
        notices=[dict(r) for r in self.store.db.execute('SELECT kind,author,status,body FROM notices')]
        self.assertEqual([(n['kind'],n['author'],n['status']) for n in notices],[('INFO','program','OPEN')]);self.assertIn('25.00%',notices[0]['body'])
        q=self.quote('ETH',at=self.later(3));p,i=self.plan('ETH',self.later(3))
        self.assertEqual(global_paper.submit(self.store,self.cfg,'ETH','BUY',q,p,i,self.later(3))['reason'],'ACCOUNT_DRAWDOWN_HALT')
    def test_withdrawal_does_not_create_drawdown_and_future_loss_uses_unit_nav(self):
        portfolio_risk.refresh(self.store,self.cfg,self.at)
        self.store.db.execute('BEGIN IMMEDIATE')
        portfolio_risk.adjust_withdrawal_inside(self.store,self.at,2_000_000)
        self.store.db.execute('UPDATE paper_accounts SET cash_cents=8000000,withdrawn_cents=2000000');self.store.db.commit()
        state=portfolio_risk.refresh(self.store,self.cfg,self.later(1));self.assertEqual(state['drawdown_bps'],0)
        with self.store.db:self.store.db.execute('UPDATE paper_accounts SET cash_cents=6000000')
        self.assertTrue(portfolio_risk.refresh(self.store,self.cfg,self.later(2))['halted'])
    def test_risk_exit_and_fifo_realized_matches_cash(self):
        o,p,i=self.buy();self.quote(at=self.later(60));global_paper.settle(self.store,self.cfg,self.later(60),{'BTC':i})
        held=global_paper.balance(self.store,self.later(60))['positions']['BTC']
        with self.store.db:
            state=portfolio_risk.state(self.store);state['halted']=True
            self.store.db.execute('UPDATE portfolio_risk SET payload_json=?',(json.dumps(state),))
        q=self.quote(price=90,at=self.later(120))
        result=global_paper.submit(self.store,self.cfg,'BTC','SELL',q,None,i,self.later(120));self.assertEqual(result['status'],'OPEN',result)
        self.quote(price=90,at=self.later(180));global_paper.settle(self.store,self.cfg,self.later(180),{'BTC':i})
        self.assertEqual(global_paper.balance(self.store,self.later(180))['positions'],{})
        fills=list(self.store.db.execute('SELECT * FROM global_fills ORDER BY recorded_at'));self.assertEqual(len(fills),2)
        self.assertEqual(fills[1]['realized_cents'],fills[1]['gross_cents']-fills[1]['fee_cents']-held['cost_cents'])
    def test_hourly_recheck_never_extends_research_or_holding_duration(self):
        p,i=self.plan();self.quote()
        with self.store.db:self.assertEqual(global_research.eligibility(self.store,p,i,self.at),[])
        row=self.store.db.execute('SELECT * FROM plan_rechecks').fetchone();self.assertEqual(row['valid_until'],self.later(3600))
        self.quote(at=self.later(3601))
        with self.store.db:self.assertEqual(global_research.eligibility(self.store,p,i,self.later(3601)),[])
        self.assertEqual(global_research.active_plan(self.store,'BTC',self.at)['valid_until'],p['valid_until'])
        self.assertTrue(global_research.eligibility(self.store,p,i,self.later(43200)))
    def test_fixed_assets_and_40_budget_and_scope(self):
        from ashare.observation_pool import allocate
        from ashare.observation import all_items
        cfg={**self.cfg,'watchlist':[{'symbol':'s'+str(i)} for i in range(22)]}
        result=allocate(self.store,all_items(self.store,self.at),self.at,cfg)
        self.assertEqual({r['asset'] for r in result['items']},set(FIXED))
        self.assertEqual(result['policy']['active_limit'],14);self.assertEqual(result['counts']['total'],26)
    def test_no_history_produces_wait_plan_without_model_call(self):
        with patch('ashare.global_research.now',return_value=self.at),patch('ashare.global_research.targets',return_value={'GOLD':{'asset':'GOLD','name':'黄金','links':[]}}):
            model=lambda *a:self.fail('must not ask model to invent missing history')
            result=global_research.run(self.store,self.cfg,self.at,model_fn=model,fetch_quotes=False)
        p=global_research.active_plan(self.store,'GOLD',self.at);data=json.loads(p['payload_json'])
        self.assertEqual(data['kind'],'WAIT');self.assertEqual(result['model_calls'],0)
        self.assertTrue((Path(self.tmp.name)/'workflow'/'global-research'/p['id']/'input.json').exists())
    def test_daily_review_retains_48h_context_and_local_backup(self):
        from ashare.review import run_review
        result=run_review(self.store,self.cfg,self.at,use_model=False,clock=lambda:self.later(1))
        row=self.store.db.execute('SELECT * FROM reviews WHERE id=?',(result['review_id'],)).fetchone();facts=json.loads(row['payload_json'])['facts']
        self.assertEqual((datetime.fromisoformat(facts['context_48h']['window_end'])-datetime.fromisoformat(facts['context_48h']['window_start'])).total_seconds(),48*3600)
        self.assertEqual((datetime.fromisoformat(facts['window_end'])-datetime.fromisoformat(facts['window_start'])).total_seconds(),24*3600)
        self.assertTrue((Path(self.tmp.name)/'workflow'/'reviews'/row['id']/'facts.json').exists())
    def test_cn_risk_halt_cancels_buy_and_preserves_t_plus_one(self):
        from ashare.demo import seed as seed_cn,put_quote,research_model,SYMBOL,decision_model
        from ashare.research import study
        from ashare.slots import run_slot
        from ashare.paper import settle,account
        cfg={**self.cfg,'watchlist':[{'symbol':SYMBOL,'name':'合成'}],'paper_max_fill_qty':10000}
        packet=seed_cn(self.store,cfg)
        study(self.store,cfg,packet,model_fn=research_model(packet),at='2026-09-15T09:01:00+08:00')
        start=normalize_time('2026-09-15T10:00:00+08:00');put_quote(self.store,start)
        def slot(at):return run_slot(self.store,cfg,at,refresh_fn=lambda *a:{'event_status':{SYMBOL:'OK'},'quote_status':'OK'},model_fn=decision_model('BUY'),clock=lambda:at)
        result=slot(start);self.assertEqual(result['operation_count'],1,result)
        fill_at=normalize_time('2026-09-15T10:01:00+08:00');put_quote(self.store,fill_at);settle(self.store,cfg,fill_at)
        # Reach account threshold via a controlled cash-loss fixture; no production data.
        with self.store.db:self.store.db.execute('UPDATE paper_accounts SET cash_cents=cash_cents-2600000')
        stamp=normalize_time('2026-09-15T10:02:00+08:00');put_quote(self.store,stamp)
        result=slot(stamp);self.assertTrue(portfolio_risk.halted(self.store))
        check=result['decisions'][0];self.assertEqual(check['action'],'SELL');self.assertEqual(check['status'],'BLOCKED');self.assertIn('T_PLUS_ONE',check['reason'])
        # Expired research next day cannot disable independent account-risk exit.
        tomorrow=normalize_time('2026-09-16T10:00:00+08:00');put_quote(self.store,tomorrow)
        result=slot(tomorrow);self.assertEqual(result['decisions'][0]['status'],'SUBMITTED',result)
        self.assertEqual(result['decisions'][0]['action'],'SELL')
    def test_incomplete_valuation_cannot_raise_high_water(self):
        portfolio_risk.refresh(self.store,self.cfg,self.at)
        before=portfolio_risk.state(self.store)['peak_unit_nav']
        with self.store.db:self.store.db.execute('UPDATE paper_accounts SET cash_cents=15000000')
        with patch('ashare.portfolio_risk.valuation_ready',return_value=False):
            state=portfolio_risk.refresh(self.store,self.cfg,self.later(1))
        self.assertEqual(state['peak_unit_nav'],before);self.assertIsNone(state['drawdown_bps']);self.assertFalse(state['halted'])
    def test_schedule_enqueues_global_research_and_only_latest_window(self):
        from ashare.scheduler import schedule_due
        schedule_due(self.store,self.cfg,self.at)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE kind='global_research' AND status='PENDING'").fetchone()[0],1)
    def test_model_can_only_use_grounded_evidence_and_bounded_day_horizon(self):
        item={'asset':'BTC','name':'比特币','links':[]}
        bars=[{'date':f'2026-08-{i:02d}','price_micros':(80+i)*1_000_000} for i in range(1,25)]
        with self.store.db:self.store.db.execute('INSERT INTO global_market VALUES(?,?,?)',('BTC',self.at,json.dumps({'status':'OK','bars':bars})))
        bad={'stance':'LONG','thesis':'unverified','counterpoints':['risk'],'holding_days':0,'evidence_ids':['invented'],'next_checks':[]}
        with patch('ashare.global_research.targets',return_value={'BTC':item}):
            global_research.run(self.store,self.cfg,self.at,model_fn=lambda *a:bad,fetch_quotes=False)
        payload=json.loads(global_research.active_plan(self.store,'BTC',self.at)['payload_json'])
        self.assertEqual(payload['kind'],'WAIT');self.assertEqual(payload['model_status'],'DEFERRED')
