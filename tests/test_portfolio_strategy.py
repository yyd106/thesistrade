import json
import unittest
from unittest.mock import patch
import test_investment_policy as investment_fixtures
import test_dynamic as dynamic_fixtures
from ashare import portfolio_strategy as ps, global_paper
from ashare.storage import normalize_time


class PortfolioStrategyTests(unittest.TestCase):
    later=investment_fixtures.InvestmentTests.later
    quote=investment_fixtures.InvestmentTests.quote
    plan=investment_fixtures.InvestmentTests.plan

    def setUp(self):
        investment_fixtures.InvestmentTests.setUp(self)
        self.at=normalize_time('2026-09-15T10:00:00+08:00')
        self.cfg['portfolio_strategy']=ps.VERSION
        with self.store.db:self.store.db.execute('DELETE FROM macro_watchlist')
        from ashare.investment_policy import seed
        seed(self.store,self.at)
        with self.store.db:self.store.db.execute("UPDATE paper_flows SET created_at='2026-09-01T00:00:00+00:00'")
    def tearDown(self):investment_fixtures.InvestmentTests.tearDown(self)

    def output(self,packet,overrides=None,groups=None):
        decisions=[]
        for c in packet['candidates']:
            d={'key':c['key'],'action':'HOLD','target_bps':c['current_bps'],'reason':'现有证据保持观察，不把重复驱动视为独立机会',
                'evidence_ids':[c['reference_id']],'related_keys':[]}
            d.update((overrides or {}).get(c['key'],{}));decisions.append(d)
        return {'summary':'综合已有持仓与共同驱动后控制新增资金','decisions':decisions,'risk_groups':groups or []}

    def approve(self,overrides=None,at=None,groups=None):
        at=at or self.at
        result=ps.run(self.store,self.cfg,at,model_fn=lambda prompt,*args:self.output(json.loads(prompt.split('<UNTRUSTED_INPUT>')[1].split('</UNTRUSTED_INPUT>')[0]),overrides,groups),clock=lambda:at)
        self.assertEqual(result['status'],'SUCCEEDED',result)
        return result

    def enter(self,asset='BTC',target=500):
        q=self.quote(asset)
        from ashare.global_research import active_plan
        p=active_plan(self.store,asset,self.at)
        if p:i={'asset':asset,'links':[]}
        else:p,i=self.plan(asset)
        self.approve({'global:'+asset:{'action':'ALLOW','target_bps':target}})
        order=global_paper.submit(self.store,self.cfg,asset,'BUY',q,p,i,self.at)
        self.assertIn('id',order,order)
        return order,p,i

    def fill(self,i,asset='BTC',second=60):
        at=self.later(second);self.quote(asset,at=at)
        return global_paper.settle(self.store,self.cfg,at,{asset:i})

    def test_model_allocation_reaches_submission_and_fill_with_exact_cash(self):
        o,p,i=self.enter(target=100)
        self.assertLessEqual(o['reserved_cents'],100000)
        self.assertEqual(global_paper.settle(self.store,self.cfg,self.at,{'BTC':i}),[])
        self.assertEqual(len(self.fill(i)),1)
        from ashare.paper import account
        a=account(self.store,self.later(60));held=a['global_positions']['BTC']
        self.assertEqual(a['cash_cents']+held['cost_cents'],10_000_000)
        self.assertLessEqual(held['market_value_cents'],100000)
        self.assertEqual(global_paper.settle(self.store,self.cfg,self.later(60),{'BTC':i}),[])
        self.assertTrue(json.loads(o['payload_json'])['portfolio_decision']['portfolio_id'])
        from ashare.review_portfolio import build
        facts=build(self.store,self.cfg,self.later(-1),self.later(120),self.later(120))
        self.assertEqual(facts['fills'][0]['portfolio_decision']['portfolio_id'],json.loads(o['payload_json'])['portfolio_decision']['portfolio_id'])

    def test_missing_expired_or_changed_authorization_blocks_but_native_exit_survives(self):
        q=self.quote();p,i=self.plan()
        self.assertEqual(global_paper.submit(self.store,self.cfg,'BTC','BUY',q,p,i,self.at)['status'],'BLOCKED')
        o,p,i=self.enter();self.fill(i)
        at=self.later(3601);q=self.quote(price=90,at=at)
        self.assertIsNone(ps.decision(self.store,self.cfg,'global','BTC',at))
        result=global_paper.submit(self.store,self.cfg,'BTC','SELL',q,p,i,at)
        self.assertIn('id',result,result)
        self.assertEqual(json.loads(result['payload_json'])['reason'],'COST_STOP_TRIGGER')

    def test_new_decision_cancels_pending_buy_and_can_retry_without_double_fill(self):
        o,p,i=self.enter()
        self.approve(at=self.later(1))
        self.assertEqual(self.store.db.execute('SELECT status FROM global_orders WHERE id=?',(o['id'],)).fetchone()[0],'CANCELLED')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM portfolio_order_events').fetchone()[0],1)
        self.approve({'global:BTC':{'action':'ALLOW','target_bps':100}},at=self.later(2))
        q=self.quote(at=self.later(2))
        second=global_paper.submit(self.store,self.cfg,'BTC','BUY',q,p,i,self.later(2))
        self.assertIn('id',second,second);self.assertNotEqual(o['id'],second['id'])
        self.assertEqual(len(self.fill(i)),1)

    def test_cross_source_dependency_invalidates_prior_buy_permission(self):
        self.quote('BTC');self.quote('ETH');p,i=self.plan('BTC');other,_=self.plan('ETH')
        self.approve({'global:BTC':{'action':'ALLOW','target_bps':100,'related_keys':['global:ETH']}})
        with self.store.db:self.store.db.execute("UPDATE global_plans SET status='SUPERSEDED' WHERE id=?",(other['id'],))
        self.assertIsNone(ps.decision(self.store,self.cfg,'global','BTC',self.at))
        with self.assertRaises(ValueError):
            from ashare.paper import account
            ps.buy_budget(self.store,self.cfg,'global','BTC',self.at,account(self.store,self.at))

    def test_partial_reduce_executes_to_target_and_preserves_fractional_accounting(self):
        o,p,i=self.enter();self.fill(i)
        before=global_paper.balance(self.store,self.later(60))['positions']['BTC']['qty']
        at=self.later(120);q=self.quote(at=at)
        self.approve({'global:BTC':{'action':'REDUCE','target_bps':200}},at=at)
        sell=global_paper.submit(self.store,self.cfg,'BTC','SELL',q,p,i,at)
        self.assertIn('id',sell,sell);self.assertLess(sell['qty'],before)
        self.assertTrue(json.loads(sell['payload_json'])['portfolio_exit'])
        self.quote(at=self.later(180));self.assertEqual(len(global_paper.settle(self.store,self.cfg,self.later(180),{'BTC':i})),1)
        from ashare.paper import account
        a=account(self.store,self.later(180));held=a['global_positions']['BTC']
        self.assertGreater(held['qty'],0)
        self.assertLessEqual(held['market_value_cents'],a['equity_cents']*200//10000+10)

    def test_reduction_order_is_cancelled_if_portfolio_revoked_before_fill(self):
        o,p,i=self.enter();self.fill(i)
        at=self.later(120);q=self.quote(at=at)
        self.approve({'global:BTC':{'action':'EXIT','target_bps':0}},at=at)
        sell=global_paper.submit(self.store,self.cfg,'BTC','SELL',q,p,i,at);self.assertIn('id',sell)
        self.approve(at=self.later(121));self.quote(at=self.later(180))
        self.assertEqual(global_paper.settle(self.store,self.cfg,self.later(180),{'BTC':i}),[])
        self.assertEqual(self.store.db.execute('SELECT status FROM global_orders WHERE id=?',(sell['id'],)).fetchone()[0],'CANCELLED')

    def test_reject_ungrounded_overbudget_ineligible_and_incomplete_results(self):
        self.quote();self.plan();packet=ps.snapshot(self.store,self.cfg,self.at)
        for mutate in (lambda r:r['decisions'].pop(),lambda r:r['decisions'][0].update(evidence_ids=['made-up']),
                       lambda r:next(d for d in r['decisions'] if d['key']=='global:BTC').update(action='ALLOW',target_bps=600),
                       lambda r:next(d for d in r['decisions'] if d['key']=='global:GOLD').update(action='ALLOW',target_bps=100)):
            raw=self.output(packet);mutate(raw)
            with self.assertRaises(ValueError):ps.validate(raw,packet)

    def test_group_limits_and_reservations_are_enforced_for_two_assets(self):
        q1=self.quote('BTC');p1,i1=self.plan('BTC');q2=self.quote('ETH');p2,i2=self.plan('ETH')
        group={'name':'合成共同驱动','keys':['global:BTC','global:ETH'],'max_bps':500,'reason':'测试两项配置共享预算，并非测得相关系数'}
        self.approve({'global:BTC':{'action':'ALLOW','target_bps':300},'global:ETH':{'action':'ALLOW','target_bps':200}},groups=[group])
        a=global_paper.submit(self.store,self.cfg,'BTC','BUY',q1,p1,i1,self.at)
        b=global_paper.submit(self.store,self.cfg,'ETH','BUY',q2,p2,i2,self.at)
        self.assertIn('id',a);self.assertIn('id',b);self.assertLessEqual(a['reserved_cents']+b['reserved_cents'],500000)
        raw=self.output(ps.snapshot(self.store,self.cfg,self.at),{'global:BTC':{'action':'ALLOW','target_bps':300},'global:ETH':{'action':'ALLOW','target_bps':300}},[group])
        with self.assertRaises(ValueError):ps.validate(raw,ps.snapshot(self.store,self.cfg,self.at))

    def test_model_failure_and_concurrent_mutation_cannot_publish(self):
        self.approve();old=ps.active(self.store,self.at)['id']
        result=ps.run(self.store,self.cfg,self.at,model_fn=lambda *a:(_ for _ in ()).throw(RuntimeError('test unavailable')),clock=lambda:self.at)
        self.assertEqual(result['status'],'DEFERRED');self.assertEqual(ps.active(self.store,self.at)['id'],old)
        def mutate(prompt,*args):
            packet=json.loads(prompt.split('<UNTRUSTED_INPUT>')[1].split('</UNTRUSTED_INPUT>')[0])
            with self.store.db:self.store.db.execute('UPDATE paper_accounts SET cash_cents=cash_cents-1')
            return self.output(packet)
        result=ps.run(self.store,self.cfg,self.at,model_fn=mutate,clock=lambda:self.at)
        self.assertEqual(result['status'],'DEFERRED');self.assertEqual(ps.active(self.store,self.at)['id'],old)
        self.assertTrue(list((self.store.root/'workflow'/'portfolio-strategy').glob('*/input.json')))
        self.assertTrue(list((self.store.root/'workflow'/'portfolio-strategy').glob('*/failure.json')))

    def test_unknown_orders_are_not_cancelled_by_new_decision(self):
        o,p,i=self.enter()
        with self.store.db:self.store.db.execute("UPDATE global_orders SET status='UNKNOWN' WHERE id=?",(o['id'],))
        self.approve(at=self.later(1))
        self.assertEqual(self.store.db.execute('SELECT status FROM global_orders').fetchone()[0],'UNKNOWN')

    def test_original_a_share_buy_reduction_and_t_plus_one(self):
        from ashare.demo import seed,put_quote,research_model,SYMBOL
        from ashare.research import study
        from ashare.slots import run_slot
        from ashare.paper import settle,account
        self.cfg.update(watchlist=[{'symbol':SYMBOL,'name':'合成'}],slot_execution_mode='RULES',paper_max_fill_qty=10000)
        packet=seed(self.store,self.cfg);study(self.store,self.cfg,packet,model_fn=research_model(packet),at='2026-09-15T09:01:00+08:00')
        put_quote(self.store,self.at)
        self.approve({'watchlist:'+SYMBOL:{'action':'ALLOW','target_bps':1000}})
        def slot(at):
            put_quote(self.store,at)
            return run_slot(self.store,self.cfg,at,refresh_fn=lambda *a:{'event_status':{SYMBOL:'OK'},'quote_status':'OK'},clock=lambda:at)
        result=slot(self.at);self.assertEqual(result['operation_count'],1,result)
        put_quote(self.store,self.later(60));settle(self.store,self.cfg,self.later(60))
        at=self.later(120);put_quote(self.store,at)
        self.approve({'watchlist:'+SYMBOL:{'action':'REDUCE','target_bps':400}},at=at)
        q=self.store.latest_quote(SYMBOL,at);self.assertEqual(ps.exit_quantity(self.store,self.cfg,'watchlist',SYMBOL,at,q),0)
        tomorrow=normalize_time('2026-09-16T10:00:00+08:00');put_quote(self.store,tomorrow)
        self.approve({'watchlist:'+SYMBOL:{'action':'REDUCE','target_bps':400}},at=tomorrow)
        result=slot(tomorrow);self.assertEqual(result['operation_count'],1,result)
        sell=dict(self.store.db.execute("SELECT * FROM paper_orders WHERE side='SELL'").fetchone())
        self.assertGreater(sell['qty'],0);self.assertLess(sell['qty'],account(self.store,tomorrow)['positions'][SYMBOL]['qty'])
        later=normalize_time('2026-09-16T10:01:00+08:00');put_quote(self.store,later);self.assertEqual(len(settle(self.store,self.cfg,later)),1)

    def test_dynamic_gate_and_reduction_follow_same_portfolio(self):
        from ashare import dynamic_paper as dp
        from ashare.demo import SYMBOL
        self.cfg['paper_max_fill_qty']=10000
        self.news=dynamic_fixtures.DynamicTests.news.__get__(self);self.case=dynamic_fixtures.DynamicTests.case.__get__(self)
        c=self.case();q=dynamic_fixtures.DynamicTests.quote(self)
        with patch('ashare.dynamic.eligibility',return_value=([],{'passed':True})):
            self.approve({'dynamic:'+c['id']:{'action':'ALLOW','target_bps':500}})
            order=dp.submit(self.store,self.cfg,c,'BUY',q,self.at,'test')
        self.assertIn('id',order,order);self.assertLessEqual(order['reserved_cents'],500000)
        dynamic_fixtures.DynamicTests.quote(self,at=self.later(60));self.assertEqual(len(dp.settle(self.store,self.cfg,self.later(60))),1)
        at=normalize_time('2026-09-16T10:00:00+08:00');q=dynamic_fixtures.DynamicTests.quote(self,at=at)
        self.approve({'dynamic:'+c['id']:{'action':'REDUCE','target_bps':200}},at=at)
        order=dp.submit(self.store,self.cfg,c,'SELL',q,at,'PORTFOLIO_REDUCE')
        self.assertIn('id',order,order)
        q=dynamic_fixtures.DynamicTests.quote(self,at=normalize_time('2026-09-16T10:01:00+08:00'))
        self.assertEqual(len(dp.settle(self.store,self.cfg,q['observed_at'])),1)
        self.assertGreater(dp.positions(self.store,q['observed_at'])[c['symbol']]['qty'],0)

    def test_quote_moves_during_model_rebase_hold_without_overriding_target(self):
        o,p,i=self.enter();self.fill(i)
        at=self.later(120);self.quote(at=at)
        def moving(prompt,*args):
            packet=json.loads(prompt.split('<UNTRUSTED_INPUT>')[1].split('</UNTRUSTED_INPUT>')[0])
            self.quote(price=102,at=self.later(121))
            return self.output(packet)
        result=ps.run(self.store,self.cfg,at,model_fn=moving,clock=lambda:self.later(121))
        self.assertEqual(result['status'],'SUCCEEDED',result)
        d=ps.decision(self.store,self.cfg,'global','BTC',self.later(121))
        self.assertEqual(d['action'],'HOLD');self.assertEqual(d['target_bps'],d['current_bps'])

    def test_scheduler_coalesces_sources_and_retains_dirty_request(self):
        self.cfg['scheduler_enabled']=True
        jid=ps.request(self.store,self.cfg,self.at,changed=True)
        self.assertTrue(jid);self.assertIsNone(ps.request(self.store,self.cfg,self.at,changed=True))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM jobs WHERE kind='portfolio_strategy'").fetchone()[0],1)
        self.assertTrue(self.store.db.execute("SELECT 1 FROM service_state WHERE key='portfolio_dirty'").fetchone())
        with self.store.db:self.store.db.execute("UPDATE jobs SET status='DONE'")
        self.approve()
        self.assertIsNone(ps.request(self.store,self.cfg,self.later(30),changed=True))
        self.assertTrue(ps.request(self.store,self.cfg,self.later(61)))

    def test_permission_changed_before_matching_prevents_buy_fill(self):
        o,p,i=self.enter()
        with self.store.db:self.store.db.execute("UPDATE portfolio_decisions SET valid_until=?",(self.later(1),))
        self.assertEqual(self.fill(i),[])
        self.assertEqual(self.store.db.execute('SELECT status FROM global_orders WHERE id=?',(o['id'],)).fetchone()[0],'CANCELLED')
