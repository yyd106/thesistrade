"""D14/P06 safety boundaries on isolated ledgers; no market requests or production data."""
import json
import math
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from contextlib import closing
from unittest.mock import patch

from ashare import exposure, cost_budget, market_regime, portfolio_risk, paper, global_paper
from ashare.cloud_runtime import put, value
from ashare.storage import Store, normalize_time
import test_buy_preview as buys
import test_investment_policy as investment
import test_dynamic as dynamic
import test_withdrawals as withdrawals
import test_withdrawal_http as http
from test_exposure_bundle import r1


class D14Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.addCleanup(self.store.close)
        self.at=normalize_time('2026-11-09T10:00:00+08:00')
        self.cfg={'deployment_role':'cloud','paper_max_stock_pct':20,'paper_max_gross_pct':90}

    def bundle(self, cap=10, status='OK', day='2026-11-06'):
        b=exposure.make_bundle('D14',r1(cap,day,status))
        with self.store.db:put(self.store,'exposure_caps',b)
        return b

    def test_all_states_and_stale_only_limit_buys(self):
        for state, expected in [(10,(50,10,37)),(50,(90,20,None)),(90,(90,20,None))]:
            b=self.bundle(state);exposure.validate_bundle(b)
            self.assertEqual((exposure.gross_cap_pct(self.store,self.cfg,self.at)[0],exposure.symbol_cap_pct(self.store,self.cfg,self.at),b['vol20_max_pct']),expected)
        for status in ('STALE','MISSING'):
            self.bundle(10,status)
            self.assertEqual(exposure.gross_cap_pct(self.store,self.cfg,self.at)[0],50)
            self.assertEqual(exposure._cap_state(self.store,self.cfg,self.at),(None,{}))
        self.bundle(90,day='2026-11-05')
        self.assertEqual(exposure.gross_cap_pct(self.store,self.cfg,self.at)[0],50)
        self.assertEqual(exposure._cap_state(self.store,self.cfg,self.at),(None,{}))

    def test_closed_day_defers_reduction_until_next_open(self):
        self.bundle()
        for stamp in ('2026-11-06T22:00:00+08:00','2026-11-07T10:00:00+08:00','2026-11-09T09:29:59+08:00'):
            self.assertIsNone(exposure._cap_state(self.store,self.cfg,normalize_time(stamp))[0])
        self.assertEqual(exposure._cap_state(self.store,self.cfg,self.at)[0],50)

    def test_pairing_and_r1_d14_off_transitions(self):
        research={**self.cfg,'deployment_role':'research','exposure_regime':'D14'}
        with self.store.db:put(self.store,'remote_features',['exposure_caps_v1'])
        self.assertFalse(market_regime.regime_active(self.store,research))
        self.assertEqual(exposure.gross_cap_pct(self.store,research,self.at)[0],90)
        with self.store.db:put(self.store,'remote_features',['exposure_caps_v2'])
        self.assertTrue(market_regime.regime_active(self.store,research))
        self.assertEqual(exposure.gross_cap_pct(self.store,research,self.at)[0],50)
        self.bundle();self.assertEqual(exposure.gross_cap_pct(self.store,self.cfg,self.at)[0],50)
        with self.store.db:put(self.store,'exposure_caps',exposure.make_bundle('R1',r1(10)))
        self.assertEqual(exposure.gross_cap_pct(self.store,self.cfg,self.at)[0],10)
        with self.store.db:put(self.store,'exposure_caps',exposure.make_bundle('OFF',None))
        self.assertFalse(market_regime.regime_active(self.store,self.cfg))
        self.assertEqual(exposure.gross_cap_pct(self.store,self.cfg,self.at)[0],90)
        self.assertEqual(exposure.symbol_cap_pct(self.store,self.cfg,self.at),20)

    def test_volatility_rejects_missing_nonfinite_bool_and_high(self):
        self.bundle()
        for x in (None,True,float('nan'),float('inf'),-1,'20'):
            self.assertEqual(exposure.buy_blocker(self.store,self.cfg,self.at,{'vol20_annual_pct':x}),'BEAR_VOLATILITY_MISSING')
        self.assertEqual(exposure.buy_blocker(self.store,self.cfg,self.at,{'vol20_annual_pct':37.00001}),'BEAR_HIGH_VOLATILITY')
        self.assertIsNone(exposure.buy_blocker(self.store,self.cfg,self.at,{'vol20_annual_pct':37}))
        self.bundle(90);self.assertIsNone(exposure.buy_blocker(self.store,self.cfg,self.at,{}))

    def test_same_sample_log_return_convention(self):
        prices=[100*math.exp(.01*i+(.01 if i%2 else 0)) for i in range(21)]
        import statistics
        expected=statistics.stdev([math.log(b/a) for a,b in zip(prices,prices[1:])])*math.sqrt(244)*100
        self.assertAlmostEqual(exposure.volatility(prices),expected,12)
        self.assertIsNone(exposure.volatility(prices[:-1]))
        self.assertIsNone(exposure.volatility([0]*21))


class LiveBuyTests(unittest.TestCase):
    def fixture(self):
        t=buys.BuyPreviewTests();t.setUp();self.addCleanup(t.tearDown);t.prepare(target=1500)
        return t

    def series(self,t,falling):
        from ashare.calendar import last_completed_day,trading_day
        d=date.fromisoformat(last_completed_day(t.at));days=[]
        while len(days)<90:
            if trading_day(d):days.append(d.isoformat())
            d-=timedelta(days=1)
        rows=[{'date':d,'close':500-i if falling else 100+i} for i,d in enumerate(sorted(days))]
        with t.store.db:
            t.store.db.execute("INSERT OR IGNORE INTO runs VALUES('d14','d14','manual',?,NULL,NULL,'SUCCEEDED',NULL,NULL,NULL)",(t.at,))
            t.store.db.execute('INSERT OR REPLACE INTO comparison_series VALUES(?,?,?,?)',('d14','sh000300',t.at,json.dumps({'bars':rows})))
            put(t.store,'remote_features',['exposure_caps_v2'])
        t.cfg['exposure_regime']='D14'

    def set_vol(self,t,x):
        p=json.loads(t.p['payload_json']);p['vol20_annual_pct']=x
        with t.store.db:t.store.db.execute('UPDATE plans SET payload_json=? WHERE id=?',(json.dumps(p),t.p['id']))
        t.p=dict(t.store.db.execute('SELECT * FROM plans WHERE id=?',(t.p['id'],)).fetchone())

    def test_bull_plan_turns_bear_rechecks_preview_and_real_submit(self):
        t=self.fixture();self.series(t,False);self.set_vol(t,80)
        t.approve({'watchlist:'+t.p['symbol']:{'action':'ALLOW','target_bps':1500}})
        self.assertEqual(t.preview()['status'],'READY')
        self.series(t,True)
        self.assertEqual(t.preview()['reason'],'BEAR_HIGH_VOLATILITY')
        self.assertEqual(paper.submit(t.store,t.cfg,'high-vol',t.p,'BUY',t.q,t.at)['reason'],'BEAR_HIGH_VOLATILITY')
        self.assertEqual(t.view()['action'],'WATCH')

    def test_bear_lowvol_sizes_at_ten_percent_and_existing_above_cap_no_add(self):
        t=self.fixture();self.set_vol(t,20);self.series(t,False)
        t.approve({'watchlist:'+t.p['symbol']:{'action':'ALLOW','target_bps':1500}})
        self.series(t,True)
        preview=t.preview();self.assertEqual(preview['status'],'READY',preview)
        self.assertLessEqual(preview['reserved_cents'],1_000_000)
        from ashare.portfolio_strategy import snapshot
        snap=snapshot(t.store,t.cfg,t.at)
        self.assertEqual(snap['symbol_cap_bps'],1000)
        self.assertTrue(snap['candidates'][0]['can_increase'])

    def test_standing_order_is_cancelled_when_regime_changes(self):
        t=self.fixture();self.set_vol(t,80);self.series(t,False)
        t.approve({'watchlist:'+t.p['symbol']:{'action':'ALLOW','target_bps':1500}})
        self.assertEqual(t.slot()['operation_count'],1)
        self.series(t,True)
        from ashare.demo import put_quote
        stamp=t.later(60);put_quote(t.store,stamp)
        self.assertEqual(paper.settle(t.store,t.cfg,stamp),[])
        self.assertEqual(t.store.db.execute('SELECT status FROM paper_orders').fetchone()[0],'CANCELLED')


class CostTests(unittest.TestCase):
    def fixture(self):
        t=investment.InvestmentTests();t.setUp();self.addCleanup(lambda:t.tearDown())
        t.cfg['cost_budget']='P06'
        with t.store.db:put(t.store,'remote_features',['cost_budget_v1'])
        return t

    def test_formula_daily_lock_and_opening_gap(self):
        self.assertEqual(cost_budget.cap_bps('1','1'),9000)
        self.assertEqual(cost_budget.cap_bps('1.0262041','1'),9000)
        self.assertEqual(cost_budget.cap_bps('.95','1'),5263)
        self.assertEqual(cost_budget.cap_bps('.9','1'),0)
        previous={};risk={'unit_nav':'.909','valuation_complete':True}
        midday=cost_budget.updated(previous,risk,{},normalize_time('2026-11-11T10:00:00+08:00'))
        self.assertFalse(midday['cost_locked'])
        close=cost_budget.updated(previous,risk,{},normalize_time('2026-11-11T15:05:00+08:00'))
        self.assertTrue(close['cost_locked']);self.assertLessEqual(close['cost_cap_bps'],1000)
        rebound=cost_budget.updated(close,{'unit_nav':'1.05'}, {},normalize_time('2026-11-12T10:00:00+08:00'))
        self.assertTrue(rebound['cost_locked']);self.assertEqual(rebound['cost_cap_bps'],close['cost_cap_bps'])
        gap=cost_budget.updated({}, {'unit_nav':'.89'}, {},normalize_time('2026-11-12T09:30:00+08:00'))
        self.assertTrue(gap['cost_locked']);self.assertEqual(gap['cost_cap_bps'],0)

    def test_gap_cancels_pending_buy_and_persists_across_restart_rebound(self):
        t=self.fixture();order,p,i=t.buy()
        with t.store.db:t.store.db.execute('UPDATE paper_accounts SET cash_cents=8900000')
        risk=portfolio_risk.refresh(t.store,t.cfg,t.later(1))
        self.assertTrue(risk['cost_locked']);self.assertFalse(risk['halted'])
        self.assertEqual(t.store.db.execute('SELECT status,reserved_cents FROM global_orders').fetchone()[:],('CANCELLED',0))
        t.store.close();t.store=Store(t.tmp.name)
        with t.store.db:t.store.db.execute('UPDATE paper_accounts SET cash_cents=10500000')
        risk=portfolio_risk.refresh(t.store,t.cfg,t.later(2));self.assertTrue(risk['cost_locked']);self.assertEqual(risk['cost_cap_bps'],0)
        q=t.quote('ETH',at=t.later(3));p,i=t.plan('ETH',at=t.later(3))
        self.assertEqual(global_paper.submit(t.store,t.cfg,'ETH','BUY',q,p,i,t.later(3))['reason'],'COST_BUDGET_LOCKED')

    def test_withdrawal_preserves_nav_and_incomplete_preserves_cost_state(self):
        t=self.fixture();before=portfolio_risk.refresh(t.store,t.cfg,t.at)
        t.store.db.execute('BEGIN IMMEDIATE')
        with t.store.db:
            portfolio_risk.adjust_withdrawal_inside(t.store,t.at,2_000_000)
            t.store.db.execute('UPDATE paper_accounts SET cash_cents=8000000,withdrawn_cents=2000000')
        after=portfolio_risk.refresh(t.store,t.cfg,t.later(1))
        self.assertEqual(before['unit_nav'],after['unit_nav']);self.assertEqual(before['cost_cap_bps'],after['cost_cap_bps'])
        with t.store.db:t.store.db.execute('UPDATE paper_accounts SET cash_cents=5000000')
        with patch('ashare.portfolio_risk.valuation_ready',return_value=False):
            incomplete=portfolio_risk.refresh(t.store,t.cfg,t.later(2))
        for k in before:
            if k.startswith('cost_'):self.assertEqual(incomplete[k],after[k])

    def test_combined_cap_and_disabled_lock_retention(self):
        t=self.fixture();t.cfg['exposure_regime']='D14'
        with t.store.db:
            put(t.store,'remote_features',['cost_budget_v1','exposure_caps_v2'])
            t.store.db.execute('UPDATE paper_accounts SET cash_cents=9400000')
        portfolio_risk.refresh(t.store,t.cfg,t.at)
        cap,detail=exposure.gross_cap_pct(t.store,t.cfg,t.at)
        self.assertEqual(cap,42.55);self.assertEqual(detail['regime']['cap_pct'],50)
        t.cfg['cost_budget']=None
        self.assertEqual(exposure.gross_cap_pct(t.store,t.cfg,t.at)[0],50)
        t.cfg['exposure_regime']=None
        self.assertEqual(exposure.gross_cap_pct(t.store,t.cfg,t.at)[0],90)

    def test_watchlist_and_dynamic_buy_reject_cost_lock(self):
        t=buys.BuyPreviewTests();t.setUp();self.addCleanup(t.tearDown);t.prepare()
        t.cfg['cost_budget']='P06'
        with t.store.db:
            put(t.store,'remote_features',['cost_budget_v1'])
            t.store.db.execute('UPDATE paper_accounts SET cash_cents=8900000')
        self.assertEqual(paper.submit(t.store,t.cfg,'locked',t.p,'BUY',t.q,t.at)['reason'],'COST_BUDGET_LOCKED')
        d=dynamic.DynamicTests();d.setUp();self.addCleanup(d.tearDown)
        d.cfg.update(cost_budget='P06',investment_policy='days_cash_v1')
        c=d.case();d.prepare_execution(c,d.at);q=d.quote()
        with d.store.db:
            put(d.store,'remote_features',['cost_budget_v1'])
            d.store.db.execute("UPDATE paper_flows SET created_at='2026-09-01T00:00:00+00:00'")
            d.store.db.execute('UPDATE paper_accounts SET cash_cents=8900000')
        from ashare.dynamic_paper import submit
        self.assertEqual(submit(d.store,d.cfg,c,'BUY',q,d.at,'test')['reason'],'COST_BUDGET_LOCKED')


class CostHttpTests(unittest.TestCase):
    def setUp(self):
        self.t=http.WithdrawalHttpTests();self.t.setUp();self.addCleanup(self.t.tearDown)
        with closing(Store(self.t.data)) as s:
            with s.db:put(s,'cost_budget_mode','P06')

    def preview(self):
        code,value=self.t.post('/api/cost-budget/request',{})
        self.assertEqual(code,200,value);return value['approval']

    def execute(self,a,headers=None):
        return self.t.post('/api/cost-budget/execute',{'approval_id':a['id'],'expected_hash':a['hash']},headers)

    def test_request_no_effect_then_execution_time_nav_and_replay_once(self):
        a=self.preview()
        with closing(Store(self.t.data)) as s:
            self.assertIsNone(__import__('ashare.approvals',fromlist=['receipt_state']).receipt_state(s,a['id']))
            with s.db:s.db.execute('UPDATE paper_accounts SET cash_cents=17000000')
        code,result=self.execute(a);self.assertEqual(code,200,result)
        self.assertEqual(result['new_basis'],'1.7');self.assertEqual(result['old_basis'],'1')
        self.assertEqual(self.execute(a)[1]['status'],'ALREADY_EXECUTED')
        with closing(Store(self.t.data)) as s:
            self.assertEqual(s.db.execute('SELECT count(*) FROM approval_consumptions').fetchone()[0],1)
            self.assertEqual(portfolio_risk.state(s)['cost_basis_revision'],1)
        code,raw,_=self.t.request('GET','/api/cost-budget/result?approval_id='+a['id'])
        self.assertEqual(code,200);self.assertEqual(json.loads(raw)['result'],result)

    def test_guest_csrf_missing_and_expired_rejected(self):
        self.assertEqual(self.t.post('/api/cost-budget/request',{},self.t.guest_headers)[0],403)
        a=self.preview()
        self.assertEqual(self.execute(a,self.t.guest_headers)[0],403)
        self.assertEqual(self.execute({**a,'hash':'x'*64})[0],400)
        from ashare.storage import now
        from datetime import datetime
        future=normalize_time((datetime.fromisoformat(now())+timedelta(days=2)).isoformat())
        with patch('ashare.dashboard.now',return_value=future):
            self.assertNotEqual(self.execute(a)[0],200)

    def test_reset_preserves_hard_halt_and_no_receipt_function_rejects(self):
        with closing(Store(self.t.data)) as s:
            portfolio_risk.refresh(s,self.t.cfg,__import__('ashare.storage',fromlist=['now']).now())
            r=portfolio_risk.state(s);r.update(halted=True,cost_locked=True,cost_cap_bps=0)
            with s.db:s.db.execute('UPDATE portfolio_risk SET payload_json=?',(json.dumps(r),))
        a=self.preview();code,r=self.execute(a)
        self.assertEqual(code,200,r);self.assertTrue(r['hard_halted'])
        with closing(Store(self.t.data)) as s:
            self.assertTrue(portfolio_risk.state(s)['halted']);self.assertFalse(portfolio_risk.state(s)['cost_locked'])


class ReductionProgressTests(unittest.TestCase):
    def fixture(self):
        from test_market_regime import ReduceToCapTests
        t=ReduceToCapTests();t.setUp();self.addCleanup(t.tearDown)
        t.cfg.update(exposure_regime='D14',paper_max_fill_qty=600,paper_slippage_bps=0,paper_sell_tax_bps=0,paper_commission_bps=0,paper_min_fee_cents=0)
        with t.store.db:
            put(t.store,'remote_features',['exposure_caps_v2'])
            t.store.db.execute('UPDATE paper_accounts SET cash_cents=4000000,initial_cents=10000000')
            t.store.db.execute('UPDATE paper_flows SET amount_cents=10000000')
        t.hold('sz000001',6000,price=1000)
        return t

    def quote(self,t,seconds):
        from datetime import datetime
        stamp=normalize_time((datetime.fromisoformat(t.at)+timedelta(seconds=seconds)).isoformat())
        with t.store.db:
            t.store.db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?)',('next-'+str(seconds),'sz000001','合',1000,1000,stamp,stamp,'SYN',''))
        return stamp

    def test_d14_started_episode_does_not_stop_at_54_percent(self):
        t=self.fixture()
        r=exposure.reduce_to_cap(t.store,t.cfg,t.at)
        self.assertEqual(len(r['orders']),1,r);self.assertEqual(r['orders'][0]['qty'],1000)
        stamp=self.quote(t,60)
        fills=paper.settle(t.store,t.cfg,stamp)
        self.assertEqual(fills[0]['qty'],600)
        account=paper.account(t.store,stamp)
        self.assertEqual(account['market_value_cents']*100//account['equity_cents'],54)
        exposure.reduce_to_cap(t.store,t.cfg,stamp)
        order=t.store.db.execute('SELECT * FROM paper_orders').fetchone()
        self.assertEqual(order['status'],'PARTIAL')
        stamp=self.quote(t,120);fills=paper.settle(t.store,t.cfg,stamp)
        self.assertEqual(fills[0]['qty'],400)
        account=paper.account(t.store,stamp)
        self.assertEqual(account['market_value_cents']*100//account['equity_cents'],50)
        exposure.reduce_to_cap(t.store,t.cfg,stamp)
        self.assertIsNone(exposure._episode(t.store))
        self.assertEqual(t.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],1)

    def test_p06_small_overage_and_zero_cap_do_not_get_five_point_allowance(self):
        self.assertEqual(exposure._threshold_bps(0,{'cost_cap_pct':0}),0)
        self.assertEqual(exposure._threshold_bps(50,{'cost_cap_pct':50,'mode':'D14'}),5000)
        t=self.fixture();t.cfg.update(cost_budget='P06',investment_policy='days_cash_v1',exposure_regime=None)
        with t.store.db:
            put(t.store,'remote_features',['cost_budget_v1'])
            # Synthetic gap: basis 1, true unit NAV .89 and 4% exposure.
            t.store.db.execute('UPDATE paper_accounts SET cash_cents=8544000')
            t.store.db.execute('UPDATE paper_lots SET qty=356,cost_cents=356000')
        risk=portfolio_risk.refresh(t.store,t.cfg,t.at)
        self.assertTrue(risk['cost_locked']);self.assertEqual(risk['cost_cap_bps'],0)
        r=exposure.reduce_to_cap(t.store,t.cfg,t.at)
        self.assertTrue(r['triggered']);self.assertEqual(r['cap_pct'],0)
        self.assertEqual(r['orders'][0]['qty'],356)


class CloudD14Tests(unittest.TestCase):
    def fixture(self, features, vol=20):
        from test_cloud_sync import CloudSyncTests
        t=CloudSyncTests();t.setUp();self.addCleanup(t.tearDown)
        t.cfg['exposure_regime']='D14'
        with t.store.db:
            put(t.store,'remote_features',features)
            p=t.store.db.execute('SELECT * FROM global_plans WHERE symbol=?',('BTC',)).fetchone()
            content=json.loads(p['payload_json']);content['vol20_annual_pct']=vol
            t.store.db.execute('UPDATE global_plans SET payload_json=? WHERE id=?',(json.dumps(content),p['id']))
        return t

    def test_new_research_old_cloud_stays_off(self):
        t=self.fixture(['exposure_caps_v1'],80)
        body=t.publication(overrides={'global:BTC':{'action':'ALLOW','target_bps':100}})
        self.assertEqual(body['exposure_caps']['mode'],'OFF')
        self.assertEqual(t.receive(body)['status'],'ACCEPTED')
        self.assertFalse(market_regime.regime_active(t.cloud,t.cloud_cfg))

    def test_paired_bundle_and_actual_cloud_submit(self):
        t=self.fixture(['exposure_caps_v1','exposure_caps_v2'])
        from ashare.calendar import last_completed_day
        reg=r1(10,day:=last_completed_day(t.at))
        with patch('ashare.market_regime.r1',return_value=reg),patch('ashare.exposure.r1',return_value=reg):
            body=t.publication(overrides={'global:BTC':{'action':'ALLOW','target_bps':100}})
        self.assertEqual(body['exposure_caps']['mode'],'D14')
        self.assertEqual(t.receive(body)['status'],'ACCEPTED')
        from ashare.global_research import active_plan
        from ashare.global_market import latest,targets
        q=latest(t.cloud,'BTC',t.at);plan=active_plan(t.cloud,'BTC',t.at)
        item=targets(t.cloud,t.cloud_cfg,t.at)['BTC']
        self.assertEqual(global_paper.submit(t.cloud,t.cloud_cfg,'BTC','BUY',q,plan,item,t.at)['status'],'OPEN')

    def test_cloud_independently_rejects_bull_plan_when_incoming_bear(self):
        t=self.fixture(['exposure_caps_v1','exposure_caps_v2'],80)
        from ashare.calendar import last_completed_day
        reg=r1(90,last_completed_day(t.at))
        with patch('ashare.market_regime.r1',return_value=reg),patch('ashare.exposure.r1',return_value=reg):
            body=t.publication(overrides={'global:BTC':{'action':'ALLOW','target_bps':100}})
        body['exposure_caps']=exposure.make_bundle('D14',r1(10,last_completed_day(t.at)))
        with self.assertRaisesRegex(ValueError,'BEAR_HIGH_VOLATILITY'):t.receive(body)
        self.assertIsNone(value(t.cloud,'exposure_caps'))
