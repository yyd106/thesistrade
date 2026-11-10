import json
import unittest
from datetime import datetime, timedelta
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
        self.cfg['watchlist']=[{'symbol':SYMBOL,'name':c['name']}]
        dynamic_fixtures.DynamicTests.prepare_execution(self,c,self.at)
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
        payload=ps.active(self.store,self.later(121))['payload'];audit=payload['publication_audit']
        item=next(x for x in audit['decisions'] if x['key']=='global:BTC')
        self.assertEqual((audit['analysis_as_of'],audit['published_at']),(at,self.later(121)))
        self.assertEqual(payload['account'],audit['analysis_account'])
        self.assertNotEqual(audit['analysis_account']['equity_cents'],audit['publication_account']['equity_cents'])
        self.assertEqual(item['model_action'],'HOLD')
        self.assertEqual(item['model_target_bps'],item['analysis_basis']['current_bps'])
        self.assertEqual(item['published_target_bps'],d['target_bps'])
        self.assertNotEqual(item['model_target_bps'],item['published_target_bps'])
        self.assertEqual(item['adjustment_codes'],['HOLD_PAUSE_CURRENT_REBASE'])
        for stage in ('analysis','publication'):
            basis=item[stage+'_basis'];equity=max(1,audit[stage+'_account']['equity_cents'])
            self.assertEqual(basis['current_bps'],(basis['current_value_cents']*10000+equity-1)//equity)

    def test_publication_audit_retains_model_choice_without_changing_model_contract(self):
        self.quote();self.plan();captured={}
        def model(prompt,schema,*args):
            packet=json.loads(prompt.split('<UNTRUSTED_INPUT>')[1].split('</UNTRUSTED_INPUT>')[0])
            self.assertEqual(prompt,ps.PROMPT+'\n<UNTRUSTED_INPUT>'+ps.encoded(packet)+'</UNTRUSTED_INPUT>')
            self.assertEqual(schema,ps.SCHEMA)
            self.assertNotIn('publication_audit',packet)
            captured.update(packet=packet)
            return self.output(packet,{'global:BTC':{'action':'ALLOW','target_bps':100}})
        result=ps.run(self.store,self.cfg,self.at,model_fn=model,clock=lambda:self.at)
        self.assertEqual(result['status'],'SUCCEEDED',result)
        payload=ps.active(self.store,self.at)['payload'];audit=payload['publication_audit']
        self.assertEqual(audit['version'],1)
        self.assertEqual(audit['target_method'],'MODEL_DISCRETION_WITH_HARD_CONSTRAINTS')
        self.assertEqual(audit['validation'],{'analysis':'PASSED','publication':'PASSED'})
        self.assertEqual(audit['analysis_account'],captured['packet']['account'])
        self.assertEqual(audit['analysis_account'],audit['publication_account'])
        item=next(x for x in audit['decisions'] if x['key']=='global:BTC')
        self.assertEqual((item['model_action'],item['published_action']),('ALLOW','ALLOW'))
        self.assertEqual((item['model_target_bps'],item['published_target_bps']),(100,100))
        self.assertEqual(item['analysis_basis'],item['publication_basis'])
        self.assertEqual(item['adjustment_codes'],[])
        self.assertEqual(ps.decision(self.store,self.cfg,'global','BTC',self.at)['target_bps'],100)
        for account in (audit['analysis_account'],audit['publication_account']):
            self.assertEqual(set(account),{'cash_cents','equity_cents','available_cents','reserved_cents',
                'withdrawal_reserved_cents','market_value_cents'})
            self.assertTrue(all(type(v) is int for v in account.values()))
        def check_keys(value):
            if isinstance(value,dict):
                self.assertFalse(set(value)&{'input_guard','guard','source_token','membership_token','orders','research','prompt'})
                for v in value.values():check_keys(v)
            elif isinstance(value,list):
                for v in value:check_keys(v)
        check_keys(audit)

    def test_audit_distinguishes_model_allow_from_expired_source_publication_pause(self):
        self.quote();plan,_=self.plan()
        with self.store.db:self.store.db.execute('UPDATE global_plans SET valid_until=? WHERE id=?',(self.later(1),plan['id']))
        result=ps.run(self.store,self.cfg,self.at,
            model_fn=lambda prompt,*args:self.output(json.loads(prompt.split('<UNTRUSTED_INPUT>')[1].split('</UNTRUSTED_INPUT>')[0]),
                {'global:BTC':{'action':'ALLOW','target_bps':100}}),clock=lambda:self.later(2))
        self.assertEqual(result['status'],'SUCCEEDED',result)
        payload=ps.active(self.store,self.later(2))['payload']
        item=next(x for x in payload['publication_audit']['decisions'] if x['key']=='global:BTC')
        decision=next(x for x in payload['decisions'] if x['key']=='global:BTC')
        self.assertEqual((item['model_action'],item['model_target_bps']),('ALLOW',100))
        self.assertEqual((item['published_action'],item['published_target_bps']),('PAUSE',0))
        self.assertTrue(item['analysis_basis']['can_increase'])
        self.assertFalse(item['publication_basis']['can_increase'])
        self.assertEqual(item['publication_basis']['recorded_blockers'],[])
        self.assertEqual(item['adjustment_codes'],['ALLOW_ELIGIBILITY_LOST','HOLD_PAUSE_CURRENT_REBASE'])
        self.assertEqual((decision['action'],decision['target_bps']),('PAUSE',0))
        self.assertEqual(decision['reason'],'现有证据保持观察，不把重复驱动视为独立机会；发布时条件变化，暂停新增买入')

    def test_audit_preserves_model_reduce_when_price_move_removes_minimum_gap(self):
        _,_,instrument=self.enter();self.fill(instrument)
        at=self.later(120);self.quote(at=at)
        def moving(prompt,*args):
            packet=json.loads(prompt.split('<UNTRUSTED_INPUT>')[1].split('</UNTRUSTED_INPUT>')[0])
            current=next(x['current_bps'] for x in packet['candidates'] if x['key']=='global:BTC')
            self.quote(price=99,at=self.later(121))
            return self.output(packet,{'global:BTC':{'action':'REDUCE','target_bps':current-100}})
        result=ps.run(self.store,self.cfg,at,model_fn=moving,clock=lambda:self.later(121))
        self.assertEqual(result['status'],'SUCCEEDED',result)
        payload=ps.active(self.store,self.later(121))['payload']
        item=next(x for x in payload['publication_audit']['decisions'] if x['key']=='global:BTC')
        self.assertEqual((item['model_action'],item['published_action']),('REDUCE','HOLD'))
        self.assertEqual(item['analysis_basis']['current_bps']-item['model_target_bps'],100)
        self.assertLess(item['publication_basis']['current_bps']-item['model_target_bps'],100)
        self.assertEqual(item['published_target_bps'],item['publication_basis']['current_bps'])
        self.assertEqual(item['adjustment_codes'],['REDUCE_MINIMUM_GAP_LOST','HOLD_PAUSE_CURRENT_REBASE'])

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


class MaterialTriggerTests(unittest.TestCase):
    """source_material comparisons and forced re-runs; synthetic rows only, no network."""
    SYMBOL='sz000333'
    later=investment_fixtures.InvestmentTests.later
    quote=investment_fixtures.InvestmentTests.quote
    plan=investment_fixtures.InvestmentTests.plan
    output=PortfolioStrategyTests.output
    approve=PortfolioStrategyTests.approve

    def setUp(self):
        PortfolioStrategyTests.setUp(self)
        self.cfg.update(watchlist=[{'symbol':self.SYMBOL,'name':'合成'}],scheduler_enabled=True)

    def tearDown(self):investment_fixtures.InvestmentTests.tearDown(self)

    def wplan(self,symbol=None,study='study-1',plan='plan-1',at=None,status='ACTIVE',payload=None):
        symbol=symbol or self.SYMBOL;at=at or self.at
        payload=payload if payload is not None else dict(self.original)
        expiry=normalize_time((datetime.fromisoformat(at)+timedelta(hours=24)).isoformat())
        with self.store.db:
            self.store.db.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?,?,?)',(study,None,symbol,at,at,'fixture','{}'))
            self.store.db.execute('INSERT OR IGNORE INTO studies VALUES(?,?,?,?,?,?)',(study,study,symbol,at,'SUCCEEDED','{}'))
            self.store.db.execute("UPDATE plans SET status='SUPERSEDED' WHERE symbol=? AND status='ACTIVE'",(symbol,))
            self.store.db.execute('INSERT INTO plans VALUES(?,?,?,?,?,?,?,?)',(plan,study,symbol,at,expiry,status,'synthetic',json.dumps(payload)))
            self.store.db.execute('INSERT INTO plan_events(plan_id,at,status,reason) VALUES(?,?,?,?)',(plan,at,status,'synthetic'))
        return dict(self.store.db.execute('SELECT * FROM plans WHERE id=?',(plan,)).fetchone())

    original={'kind':'PAPER_TRADE','blockers':[],
        'levels':{'buy_low_cents':990,'buy_high_cents':1010,'stop_cents':940,'sell_cents':1100},
        'max_stock_pct':20,'thesis':'synthetic'}

    def renew(self,plan='plan-2',study='study-1',mutate=None,status='ACTIVE'):
        payload=json.loads(self.store.db.execute('SELECT payload_json FROM plans WHERE id=?',('plan-1',)).fetchone()[0])
        if mutate:mutate(payload)
        return self.wplan(study=study,plan=plan,status=status,payload=payload)

    def hold(self,qty=100,cost=94000):
        self.store.db.execute('PRAGMA foreign_keys=OFF')
        with self.store.db:
            self.store.db.execute('INSERT INTO paper_lots VALUES(?,?,?,?,?)',('lot-1',self.SYMBOL,qty,cost,'2026-09-10'))
        self.store.db.execute('PRAGMA foreign_keys=ON')

    def aquote(self,price=1000,at=None):
        at=at or self.at;qid=ps.digest(json.dumps({'s':self.SYMBOL,'p':price,'at':at},sort_keys=True))[:24]
        with self.store.db:
            self.store.db.execute('INSERT OR IGNORE INTO quotes VALUES(?,?,?,?,?,?,?,?,?)',
                (qid,self.SYMBOL,'合成',price,1000,at,at,'SYNTHETIC','synthetic'))

    def strip_material(self):
        payload=ps.active(self.store,self.at)['payload']
        for d in payload['decisions']:d.pop('source_material',None)
        with self.store.db:
            self.store.db.execute("UPDATE portfolio_decisions SET payload_json=? WHERE status='ACTIVE'",(json.dumps(payload),))

    def test_materially_changed_boundaries(self):
        m=lambda **kw:{'study_id':'s','status':'ACTIVE','strategy_version':'v','kind':'PAPER_TRADE',
            'blockers':[],'levels':{'buy_low_cents':1000,'buy_high_cents':1100,'stop_cents':900},
            'membership_token':None,'max_stock_pct':20,**kw}
        base=m()
        self.assertFalse(ps.materially_changed(base,dict(base)))
        self.assertFalse(ps.materially_changed(None,None))
        self.assertTrue(ps.materially_changed(base,None))
        self.assertTrue(ps.materially_changed(None,base))
        moved=m(levels={'buy_low_cents':1005,'buy_high_cents':1100,'stop_cents':900})  # exactly 0.5%: not material
        self.assertFalse(ps.materially_changed(base,moved))
        moved['levels']['buy_low_cents']=1006
        self.assertTrue(ps.materially_changed(base,moved))
        gone=m(levels={'buy_low_cents':None,'buy_high_cents':1100,'stop_cents':900})
        self.assertTrue(ps.materially_changed(base,gone))
        for field,value in (('study_id','s2'),('status','EXPIRED'),('strategy_version','v2'),('kind','NO_ENTRY'),
                            ('blockers',['X']),('membership_token','t'),('max_stock_pct',10)):
            self.assertTrue(ps.materially_changed(base,m(**{field:value})),field)

    def test_renewal_keeps_authorization_and_changed_keys(self):
        self.wplan();self.approve()
        decided=next(d for d in ps.active(self.store,self.at)['payload']['decisions'] if d['key']=='watchlist:'+self.SYMBOL)
        self.assertIsNotNone(decided['source_material']);self.assertIn('near_stop',decided)
        self.renew(mutate=lambda p:p['levels'].update(buy_low_cents=992))  # +0.20%, inside tolerance
        self.assertEqual(ps.changed_keys(self.store,self.cfg,self.at),[])
        self.assertIsNotNone(ps.decision(self.store,self.cfg,'watchlist',self.SYMBOL,self.at))

    def test_material_changes_invalidate(self):
        self.wplan();self.approve()
        for mutate,why in ((lambda p:p['blockers'].append('X'),'blockers'),(lambda p:p.update(kind='NO_ENTRY'),'kind'),
                           (lambda p:p['levels'].update(stop_cents=950),'level-move'),(lambda p:p.update(research_membership_token='t'),'membership')):
            self.renew(plan='changed-'+why,mutate=mutate)
            self.assertIn('watchlist:'+self.SYMBOL,ps.changed_keys(self.store,self.cfg,self.at),why)
            self.assertIsNone(ps.decision(self.store,self.cfg,'watchlist',self.SYMBOL,self.at),why)
            self.renew(plan='restored-'+why)  # back to an immaterial copy of the original
            self.assertEqual(ps.changed_keys(self.store,self.cfg,self.at),[],why)
        self.renew(plan='other-study',study='study-2')
        self.assertIn('watchlist:'+self.SYMBOL,ps.changed_keys(self.store,self.cfg,self.at))
        self.renew(plan='expired',status='EXPIRED')
        self.assertIn('watchlist:'+self.SYMBOL,ps.changed_keys(self.store,self.cfg,self.at))

    def test_legacy_decision_without_material_uses_row_token(self):
        self.wplan();self.approve();self.strip_material()
        self.renew(mutate=lambda p:p['levels'].update(buy_low_cents=992))
        self.assertIn('watchlist:'+self.SYMBOL,ps.changed_keys(self.store,self.cfg,self.at))
        self.assertIsNone(ps.decision(self.store,self.cfg,'watchlist',self.SYMBOL,self.at))

    def test_request_forced_by_position_change(self):
        self.wplan();self.approve()
        self.assertIsNone(ps.request(self.store,self.cfg,self.later(30)))
        self.hold()
        jid=ps.request(self.store,self.cfg,self.later(95))  # forced check throttled to once per 60s
        self.assertTrue(jid)
        self.assertTrue(self.store.db.execute("SELECT 1 FROM service_state WHERE key='portfolio_dirty'").fetchone())
        reasons=json.loads(self.store.db.execute("SELECT value FROM service_state WHERE key='portfolio_forced_reasons'").fetchone()[0])
        self.assertEqual(reasons,['POSITION_CHANGED:watchlist:'+self.SYMBOL,'COST_CHANGED:watchlist:'+self.SYMBOL])
        self.assertTrue(self.store.db.execute("SELECT 1 FROM jobs WHERE kind='portfolio_strategy' AND status='PENDING'").fetchone())

    def test_request_forced_by_near_stop_only(self):
        self.hold();self.aquote(1000);self.wplan();self.approve()
        self.aquote(950,at=self.later(30))  # inside 2% above the 940 effective stop
        jid=ps.request(self.store,self.cfg,self.later(95))
        self.assertTrue(jid)
        reasons=json.loads(self.store.db.execute("SELECT value FROM service_state WHERE key='portfolio_forced_reasons'").fetchone()[0])
        self.assertEqual(reasons,['NEAR_STOP:watchlist:'+self.SYMBOL])

    def test_request_unchanged_and_throttled(self):
        self.wplan();self.approve()
        calls=[]
        with patch('ashare.portfolio_strategy.forced_reasons',side_effect=lambda *a:calls.append(1) or []):
            self.assertIsNone(ps.request(self.store,self.cfg,self.later(10)))
            self.assertIsNone(ps.request(self.store,self.cfg,self.later(40)))  # second call inside 60s: no re-check
            self.assertIsNone(ps.request(self.store,self.cfg,self.later(70)))  # re-checks, still no reasons
            self.assertIsNone(ps.request(self.store,self.cfg,self.later(80)))
        self.assertEqual(len(calls),2)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM service_state WHERE key='portfolio_dirty'").fetchone())
        self.assertFalse(self.store.db.execute("SELECT 1 FROM jobs WHERE kind='portfolio_strategy'").fetchone())

    def test_request_forced_by_cash_change(self):
        self.wplan();self.approve()
        self.assertIsNone(ps.request(self.store,self.cfg,self.later(30)))
        with self.store.db:self.store.db.execute('UPDATE paper_accounts SET cash_cents=cash_cents-100000')
        self.assertEqual(ps.forced_reasons(self.store,self.cfg,self.later(95)),['CASH_CHANGED'])
        jid=ps.request(self.store,self.cfg,self.later(95))
        self.assertTrue(jid)
        self.assertTrue(self.store.db.execute("SELECT 1 FROM service_state WHERE key='portfolio_dirty'").fetchone())
        reasons=json.loads(self.store.db.execute("SELECT value FROM service_state WHERE key='portfolio_forced_reasons'").fetchone()[0])
        self.assertEqual(reasons,['CASH_CHANGED'])
        self.assertTrue(self.store.db.execute("SELECT 1 FROM jobs WHERE kind='portfolio_strategy' AND status='PENDING'").fetchone())

    def test_request_forced_by_withdrawal_reservation(self):
        with self.store.db:self.store.db.execute('UPDATE paper_accounts SET cash_cents=20000000')
        self.wplan();self.approve()
        from ashare import withdrawals
        view=withdrawals.refresh(self.store,self.cfg,self.later(95))  # real reservation entry
        self.assertGreater(view['reserved_cents'],0,view)
        self.assertEqual(ps.forced_reasons(self.store,self.cfg,self.later(95)),['WITHDRAWAL_CHANGED'])

    def record_fill(self,qty=100,price=1000,day='2026-09-10'):
        """A held position entitled to a dividend needs ledger facts at the record date."""
        self.store.db.execute('PRAGMA foreign_keys=OFF')
        with self.store.db:
            self.store.db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?)',
                ('q-fill',self.SYMBOL,'合成',price,1000,day+'T15:00:00+08:00',day+'T15:00:00+08:00','SYNTHETIC','synthetic'))
            self.store.db.execute('INSERT INTO paper_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                ('o-fill','d-fill','plan-1',self.SYMBOL,'BUY',qty,qty,price,0,day+'T10:00:00+08:00',day+'T15:00:00+08:00','FILLED'))
            self.store.db.execute('INSERT INTO paper_fills VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                ('f-fill','o-fill','q-fill',self.SYMBOL,'BUY',qty,price,0,0,day+'T10:00:00+08:00',day+'T10:00:00+08:00'))
        self.store.db.execute('PRAGMA foreign_keys=ON')

    def test_request_forced_by_dividend_credit(self):
        self.hold();self.record_fill()
        action={'cash_per_share':'0.5','record_date':'2026-09-14','ex_date':'2026-09-15','doc_id':'doc'}
        self.wplan(payload={**self.original,'corporate_actions':[action]});self.approve()
        from ashare import dividends
        done=dividends.credit(self.store,self.cfg,self.later(95))['credited']  # real posting entry
        self.assertEqual(len(done),1)
        self.assertEqual(done[0]['amount_cents'],5000)
        self.assertEqual(ps.forced_reasons(self.store,self.cfg,self.later(95)),['CASH_CHANGED'])

    def test_request_forced_by_cost_and_halt(self):
        self.hold();self.wplan();self.approve()
        with self.store.db:
            self.store.db.execute('UPDATE paper_lots SET cost_cents=cost_cents+1')
            self.store.db.execute("INSERT OR REPLACE INTO portfolio_risk VALUES('DEMO_PAPER',?,?)",
                (self.later(30),json.dumps({'halted':True})))
        self.assertEqual(ps.forced_reasons(self.store,self.cfg,self.later(95)),
            ['COST_CHANGED:watchlist:'+self.SYMBOL,'HALT_CHANGED'])

    def test_buy_order_reservation_does_not_force_rerun(self):
        p,i=self.plan('BTC')
        self.wplan();self.approve({'global:BTC':{'action':'ALLOW','target_bps':100}})
        q=self.quote('BTC',at=self.later(95))
        order=global_paper.submit(self.store,self.cfg,'BTC','BUY',q,p,i,self.later(95))
        self.assertIn('id',order,order);self.assertGreater(order['reserved_cents'],0)
        self.assertEqual(ps.forced_reasons(self.store,self.cfg,self.later(95)),[])
        self.assertIsNone(ps.request(self.store,self.cfg,self.later(95)))
        self.assertFalse(self.store.db.execute("SELECT 1 FROM service_state WHERE key='portfolio_dirty'").fetchone())

    def test_legacy_payload_missing_account_and_audit_skips_new_reasons(self):
        self.wplan();self.approve()
        payload=ps.active(self.store,self.at)['payload']
        payload.pop('account',None);payload.pop('publication_audit',None)
        for d in payload['decisions']:d.pop('cost_cents',None)
        with self.store.db:
            self.store.db.execute("UPDATE portfolio_decisions SET payload_json=? WHERE status='ACTIVE'",(json.dumps(payload),))
            self.store.db.execute('UPDATE paper_accounts SET cash_cents=cash_cents-100000')
            self.store.db.execute("INSERT OR REPLACE INTO portfolio_risk VALUES('DEMO_PAPER',?,?)",
                (self.later(30),json.dumps({'halted':True})))
        self.assertEqual(ps.forced_reasons(self.store,self.cfg,self.later(95)),[])

    def write(self,sql,args=()):
        with self.store.db:self.store.db.execute(sql,args)

    def model_with(self,mutate):
        def model(prompt,*args):
            packet=json.loads(prompt.split('<UNTRUSTED_INPUT>')[1].split('</UNTRUSTED_INPUT>')[0])
            mutate()
            return self.output(packet)
        return model

    def test_immaterial_renewal_during_model_publishes_on_the_fresh_row(self):
        self.wplan();self.approve()
        result=ps.run(self.store,self.cfg,self.later(60),model_fn=self.model_with(self.renew),clock=lambda:self.later(90))
        self.assertEqual(result['status'],'SUCCEEDED',result)
        payload=ps.active(self.store,self.later(90))['payload']
        decision=next(d for d in payload['decisions'] if d['key']=='watchlist:'+self.SYMBOL)
        self.assertEqual(decision['source_id'],'plan-2')
        self.assertEqual(ps.changed_keys(self.store,self.cfg,self.later(90)),[])
        self.assertIsNotNone(ps.decision(self.store,self.cfg,'watchlist',self.SYMBOL,self.later(90)))

    def test_material_change_during_model_defers_with_reason(self):
        self.wplan();self.approve()
        cases={'blocker':lambda p:p['blockers'].append('X'),'level':lambda p:p['levels'].update(stop_cents=960)}
        for name,mutate in cases.items():
            with self.subTest(name=name):
                result=ps.run(self.store,self.cfg,self.later(60),
                    model_fn=self.model_with(lambda:self.renew(plan='plan-'+name,mutate=mutate)),clock=lambda:self.later(90))
                self.assertEqual(result['status'],'DEFERRED',result)
                self.assertIn('分析期间研究、持仓或挂单变化，等待重新判断',result['error'])
                self.assertIn('SOURCE_MATERIAL_CHANGED:watchlist:'+self.SYMBOL,result['error'])

    def test_state_change_during_model_defers_with_reason(self):
        self.wplan();self.approve()
        cases={'POSITION_CHANGED':lambda:self.hold(qty=200),
               'CASH_CHANGED':lambda:self.write('UPDATE paper_accounts SET cash_cents=cash_cents-1'),
               'ORDERS_CHANGED':lambda:self.write(
                   "INSERT INTO global_orders VALUES('o-pub','i-pub','p-pub','BTC','BUY',1,0,1,1,?,?,'OPEN','{}')",
                   (self.later(60),self.later(3600))),
               'HALT_CHANGED':lambda:self.write(
                   "INSERT OR REPLACE INTO portfolio_risk VALUES('DEMO_PAPER',?,?)",(self.later(60),json.dumps({'halted':True})))}
        for code,mutate in cases.items():
            with self.subTest(code=code):
                result=ps.run(self.store,self.cfg,self.later(60),model_fn=self.model_with(mutate),clock=lambda:self.later(90))
                self.assertEqual(result['status'],'DEFERRED',result)
                self.assertIn(code,result['error'])

    def test_source_token_change_during_model_defers_with_reason(self):
        self.quote('BTC');p,i=self.plan('BTC')
        self.wplan();self.approve()
        def mutate():
            self.write("UPDATE global_plans SET valid_until=? WHERE id=?",(self.later(7200),p['id']))
        result=ps.run(self.store,self.cfg,self.later(60),model_fn=self.model_with(mutate),clock=lambda:self.later(90))
        self.assertEqual(result['status'],'DEFERRED',result)
        self.assertIn('SOURCE_CHANGED:global:BTC',result['error'])


class ActionabilityGatingTests(MaterialTriggerTests):
    """r3: a non-actionable candidate (qty=0 and can_increase=False) cannot force a re-run."""
    KEY = 'watchlist:sz000333'

    def blocked(self):
        return dict(self.original, blockers=['SOURCE_GAP:tencent_daily'])

    def hold_y(self, symbol):
        self.write('PRAGMA foreign_keys=OFF')
        self.write("INSERT OR REPLACE INTO paper_lots VALUES(?,?,?,?,?)",
                   ('lot-' + symbol, symbol, 100, 94000, '2026-09-10'))
        self.write('INSERT OR IGNORE INTO quotes VALUES(?,?,?,?,?,?,?,?,?)',
                   (symbol + '-q', symbol, '乙', 1000, 1000, self.at, self.at, 'SYNTHETIC', 'synthetic'))

    def test_non_actionable_changes_do_not_trigger(self):
        self.wplan(payload=self.blocked()); self.approve()
        self.renew(plan='plan-b2', mutate=lambda p: p.update(blockers=['STALE_DAILY_BARS']))
        self.assertEqual(ps.changed_keys(self.store, self.cfg, self.at), [])
        self.renew(plan='plan-s2', study='study-2')
        self.assertEqual(ps.changed_keys(self.store, self.cfg, self.at), [])
        self.assertIsNone(ps.request(self.store, self.cfg, self.later(95)))
        self.assertFalse(self.store.db.execute(
            "SELECT 1 FROM jobs WHERE kind='portfolio_strategy' AND status='PENDING'").fetchone())

    def test_actionable_candidates_still_trigger(self):
        # can_increase candidate: same blocker flip counts.
        self.wplan(); self.approve()
        self.renew(plan='plan-c', mutate=lambda p: p['blockers'].append('X'))
        self.assertEqual(ps.changed_keys(self.store, self.cfg, self.at), [self.KEY])
        # held candidate: a new study_id counts too.
        self.hold(); self.aquote(1000); self.wplan(plan='plan-h'); self.approve()
        self.renew(plan='plan-h2', study='study-2')
        self.assertEqual(ps.changed_keys(self.store, self.cfg, self.at), [self.KEY])

    def test_can_increase_flip_triggers(self):
        self.wplan(payload=self.blocked()); self.approve()
        self.renew(plan='plan-clear', mutate=lambda p: p.update(blockers=[]))
        self.assertEqual(ps.changed_keys(self.store, self.cfg, self.at), [self.KEY])

    def test_new_candidate_gating(self):
        self.wplan(); self.approve()
        # A new non-actionable dynamic case (RESEARCH, not yet ready) is ignored.
        self.write('PRAGMA foreign_keys=OFF')
        self.write("INSERT INTO dynamic_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ('dc-1', 'n-1', 'sz000999', '动态', 't', 'e', 'LONG', self.at,
                    self.later(7200), 'b', 'RESEARCH', '{}', '{}'))
        self.assertEqual(ps.changed_keys(self.store, self.cfg, self.at), [])
        # A new actionable watchlist candidate with a source counts.
        self.cfg['watchlist'].append({'symbol': 'sz000555', 'name': '新'})
        self.wplan(symbol='sz000555', plan='plan-new')
        self.assertEqual(ps.changed_keys(self.store, self.cfg, self.at), ['watchlist:sz000555'])

    def test_disappearing_candidate_gating(self):
        self.wplan(payload=self.blocked()); self.approve()
        self.write("UPDATE plans SET status='EXPIRED' WHERE id='plan-1'")
        self.assertEqual(ps.changed_keys(self.store, self.cfg, self.at), [])
        self.hold(); self.aquote(1000); self.wplan(plan='plan-h'); self.approve()
        self.write("UPDATE plans SET status='EXPIRED' WHERE id='plan-h'")
        self.assertEqual(ps.changed_keys(self.store, self.cfg, self.at), [self.KEY])

    def test_decision_dependency_gated_by_actionability(self):
        self.wplan()  # X: actionable
        self.cfg['watchlist'].append({'symbol': 'sz000555', 'name': '乙'})
        self.wplan(symbol='sz000555', plan='plan-y', payload=self.blocked())  # Y: non-actionable
        self.approve({self.KEY: {'action': 'ALLOW', 'target_bps': 500, 'related_keys': ['watchlist:sz000555']}})
        self.assertIsNotNone(ps.decision(self.store, self.cfg, 'watchlist', self.SYMBOL, self.at))
        # A material reissue of non-actionable Y must not lapse X's ALLOW.
        self.wplan(symbol='sz000555', plan='plan-y2', payload=dict(self.original, blockers=['X', 'Y']))
        self.assertIsNotNone(ps.decision(self.store, self.cfg, 'watchlist', self.SYMBOL, self.at))
    def test_decision_dependency_lapses_when_dependency_was_actionable(self):
        # Y held at decision time: its material reissue lapses X's ALLOW as before.
        self.wplan()
        self.cfg['watchlist'].append({'symbol': 'sz000555', 'name': '乙'})
        self.hold_y('sz000555')
        self.wplan(symbol='sz000555', plan='plan-y3')
        self.approve({self.KEY: {'action': 'ALLOW', 'target_bps': 500, 'related_keys': ['watchlist:sz000555']}})
        self.wplan(symbol='sz000555', plan='plan-y4', payload=dict(self.original, blockers=['Z']))
        self.assertIsNone(ps.decision(self.store, self.cfg, 'watchlist', self.SYMBOL, self.at))

    def test_publication_gated_by_relevance_and_can_increase(self):
        # Non-actionable source change during inference publishes (relevance tolerated).
        self.wplan(payload=self.blocked()); self.approve()
        result = ps.run(self.store, self.cfg, self.later(60),
            model_fn=self.model_with(lambda: self.renew(plan='plan-na', mutate=lambda p: p.update(blockers=['Y']))),
            clock=lambda: self.later(90))
        self.assertEqual(result['status'], 'SUCCEEDED', result)
        # The same change on an actionable candidate defers with the material reason.
        self.wplan(plan='plan-a'); self.approve()
        result = ps.run(self.store, self.cfg, self.later(120),
            model_fn=self.model_with(lambda: self.renew(plan='plan-ma', mutate=lambda p: p['blockers'].append('Z'))),
            clock=lambda: self.later(150))
        self.assertEqual(result['status'], 'DEFERRED', result)
        self.assertIn('SOURCE_MATERIAL_CHANGED:' + self.KEY, result['error'])
        # A can_increase flip during inference defers with the new code.
        self.wplan(plan='plan-flip', payload=self.blocked()); self.approve()
        result = ps.run(self.store, self.cfg, self.later(180),
            model_fn=self.model_with(lambda: self.renew(plan='plan-clr', mutate=lambda p: p.update(blockers=[]))),
            clock=lambda: self.later(210))
        self.assertEqual(result['status'], 'DEFERRED', result)
        self.assertIn('CAN_INCREASE_CHANGED:' + self.KEY, result['error'])

    def test_can_increase_true_to_false_during_model_publishes_paused(self):
        # True->False is tolerated: the publish layer tightens ALLOW to PAUSE and audits it.
        # Expiring a watchlist plan flips can_increase without touching source_material.
        self.wplan(); self.approve()
        def model(prompt, *a):
            packet = json.loads(prompt.split('<UNTRUSTED_INPUT>')[1].split('</UNTRUSTED_INPUT>')[0])
            self.write('UPDATE plans SET valid_until=? WHERE id=?', (self.later(61), 'plan-1'))
            return self.output(packet, {self.KEY: {'action': 'ALLOW', 'target_bps': 100}})
        result = ps.run(self.store, self.cfg, self.later(60), model_fn=model, clock=lambda: self.later(90))
        self.assertEqual(result['status'], 'SUCCEEDED', result)
        payload = ps.active(self.store, self.later(90))['payload']
        item = next(x for x in payload['publication_audit']['decisions'] if x['key'] == self.KEY)
        self.assertEqual((item['published_action'], item['published_target_bps']), ('PAUSE', 0))
        self.assertFalse(item['publication_basis']['can_increase'])


class PublicationSetChangeTests(ActionabilityGatingTests):
    """r4: a non-actionable candidate appearing or disappearing during inference no
    longer defers publication — additions get a synthetic PAUSE, removals are
    stripped from decisions/risk_groups/related_keys before re-validation."""

    def dcase(self):
        self.write('PRAGMA foreign_keys=OFF')
        self.write("INSERT INTO dynamic_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ('dc-1', 'n-1', 'sz000999', '动态', 't', 'e', 'LONG', self.at,
                    self.later(7200), 'b', 'RESEARCH', '{}', '{}'))

    def test_non_actionable_add_gets_synthetic_pause(self):
        self.wplan(); self.approve()
        r = ps.run(self.store, self.cfg, self.later(60),
                   model_fn=self.model_with(self.dcase), clock=lambda: self.later(90))
        self.assertEqual(r['status'], 'SUCCEEDED', r)
        payload = ps.active(self.store, self.later(90))['payload']
        d = next(x for x in payload['decisions'] if x['key'] == 'dynamic:dc-1')
        self.assertEqual(d['action'], 'PAUSE')
        self.assertEqual(d['target_bps'], 0)
        self.assertIn('新出现的候选', d['reason'])
        self.assertEqual(d['related_keys'], [])

    def test_non_actionable_remove_is_stripped_and_revalidated(self):
        self.wplan(); self.dcase()
        self.approve({self.KEY: {'action': 'PAUSE', 'target_bps': 0,
                                 'related_keys': ['dynamic:dc-1']}},
                     groups=[{'name': 'g1', 'keys': [self.KEY, 'dynamic:dc-1'],
                              'max_bps': 6000, 'reason': 'r'}])
        r = ps.run(self.store, self.cfg, self.later(60),
                   model_fn=self.model_with(
                       lambda: self.write("UPDATE dynamic_cases SET status='EXPIRED' WHERE id='dc-1'")),
                   clock=lambda: self.later(90))
        self.assertEqual(r['status'], 'SUCCEEDED', r)
        payload = ps.active(self.store, self.later(90))['payload']
        self.assertNotIn('dynamic:dc-1', [d['key'] for d in payload['decisions']])
        for d in payload['decisions']:
            self.assertNotIn('dynamic:dc-1', d['related_keys'])
        self.assertTrue(all(len(g['keys']) >= 2 for g in payload['risk_groups']))

    def test_actionable_add_still_defers(self):
        self.wplan(); self.approve()
        def add_buyable():
            self.cfg['watchlist'].append({'symbol': 'sz000555', 'name': '乙'})
            self.wplan(symbol='sz000555', plan='plan-new')
        r = ps.run(self.store, self.cfg, self.later(60),
                   model_fn=self.model_with(add_buyable), clock=lambda: self.later(90))
        self.assertEqual(r['status'], 'DEFERRED', r)

    def test_actionable_remove_still_defers(self):
        self.hold(); self.aquote(1000); self.wplan(plan='plan-h'); self.approve()
        r = ps.run(self.store, self.cfg, self.later(60),
                   model_fn=self.model_with(
                       lambda: self.write("UPDATE plans SET status='EXPIRED' WHERE id='plan-h'")),
                   clock=lambda: self.later(90))
        self.assertEqual(r['status'], 'DEFERRED', r)
