"""Execution/presentation contract, including read-only forecasts and negative gates."""
import copy
import json
import sqlite3
import unittest
import test_portfolio_strategy as fixtures
from ashare import paper, trader_view
from ashare import portfolio_strategy as ps
from ashare.demo import seed, put_quote, research_model, SYMBOL
from ashare.research import study
from ashare.slots import run_slot
from ashare.calendar import phase


class BuyPreviewTests(unittest.TestCase):
    setUp=fixtures.PortfolioStrategyTests.setUp
    tearDown=fixtures.PortfolioStrategyTests.tearDown
    output=fixtures.PortfolioStrategyTests.output
    approve=fixtures.PortfolioStrategyTests.approve
    later=fixtures.PortfolioStrategyTests.later

    def prepare(self,target=1000):
        self.cfg.update(watchlist=[{'symbol':SYMBOL,'name':'Synthetic'}],slot_execution_mode='RULES',paper_max_fill_qty=10000)
        packet=seed(self.store,self.cfg)
        study(self.store,self.cfg,packet,model_fn=research_model(packet),at='2026-09-15T09:01:00+08:00')
        put_quote(self.store,self.at)
        self.approve({'watchlist:'+SYMBOL:{'action':'ALLOW','target_bps':target}})
        self.p=dict(self.store.db.execute("SELECT * FROM plans WHERE symbol=? AND status='ACTIVE'",(SYMBOL,)).fetchone())
        self.q=self.store.latest_quote(SYMBOL,self.at)

    def preview(self,at=None,q=None,cfg=None):
        # An attempted INSERT/UPDATE/DELETE/PRAGMA/transaction is denied, not rolled back.
        before=self.store.db.total_changes
        self.store.db.set_authorizer(lambda action,*args: sqlite3.SQLITE_OK if action in (
            sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_RECURSIVE) else sqlite3.SQLITE_DENY)
        try:r=paper.preview_buy(self.store,cfg or self.cfg,self.p,q or self.q,at or self.at)
        finally:self.store.db.set_authorizer(None)
        self.assertEqual(self.store.db.total_changes,before)
        return r

    def view(self,at=None,q=None):
        at=at or self.at;q=q or self.q;a=paper.account(self.store,at)
        s={'at':at,'market_phase':phase(at),'quote_max_age_seconds':90,'account':a,
           'portfolio':{'holdings':[]},'portfolio_strategy':ps.view(self.store,self.cfg,at),
           'watchlist':[{'symbol':SYMBOL,'plan':{**self.p,'payload':json.loads(self.p['payload_json'])},'quote':q}],
           'research_lease':{'active':True}}
        return trader_view.build(s,self.cfg,buy_check=lambda p,q,t:self.preview(at=t,q=q))['rows'][0]

    def slot(self):
        return run_slot(self.store,self.cfg,self.at,refresh_fn=lambda *a:{'event_status':{SYMBOL:'OK'},'quote_status':'OK'},clock=lambda:self.at)

    def test_ready_is_read_only_and_exactly_matches_real_order_terms(self):
        self.prepare();r=self.preview();self.assertEqual(r['status'],'READY',r)
        self.assertEqual(self.view()['action'],'BUY')
        self.assertEqual(self.slot()['operation_count'],1)
        o=dict(self.store.db.execute('SELECT * FROM paper_orders').fetchone())
        for key in ('qty','limit_cents','reserved_cents'):self.assertEqual(r[key],o[key],key)

    def test_target_below_minimum_lot_not_buy_and_execution_agrees(self):
        self.prepare(target=1)
        self.assertEqual(self.preview()['reason'],'INSUFFICIENT_BUDGET_OR_TARGET_REACHED')
        self.assertNotEqual(self.view()['action'],'BUY')
        actual=paper.submit(self.store,self.cfg,'rejected',self.p,'BUY',self.q,self.at)
        self.assertEqual(actual['reason'],self.preview()['reason'])

    def test_existing_order_is_waiting_and_no_preview_write(self):
        self.prepare();self.assertEqual(self.slot()['operation_count'],1)
        self.assertEqual(self.preview()['reason'],'EXISTING_OPEN_ORDER')
        self.assertEqual(self.view()['code'],'EXISTING_OPEN_ORDER')
        self.assertEqual(paper.submit(self.store,self.cfg,'another',self.p,'BUY',self.q,self.at)['reason'],'EXISTING_OPEN_ORDER')

    def test_price_guard_blocks_display_and_real_execution(self):
        self.prepare();q={**self.q,'prev_close_cents':800}
        self.assertEqual(self.preview(q=q)['reason'],'NEAR_PRICE_LIMIT_OR_CORPORATE_ACTION')
        self.assertNotEqual(self.view(q=q)['action'],'BUY')
        self.assertEqual(paper.submit(self.store,self.cfg,'another',self.p,'BUY',q,self.at)['reason'],self.preview(q=q)['reason'])

    def test_fresh_authorization_cannot_buy_closed_market_or_stale_quote(self):
        self.at='2026-09-15T03:29:00+00:00';self.prepare()
        closed=self.later(120)
        self.assertTrue(ps.view(self.store,self.cfg,closed)['decisions'][0]['current_authorization'])
        self.assertEqual(self.preview(at=closed)['reason'],'MARKET_CLOSED')
        self.assertNotEqual(self.view(at=closed)['action'],'BUY')
        self.assertEqual(self.preview(at=self.later(91))['reason'],'MARKET_CLOSED')
        self.assertEqual(self.preview(at=self.later(59),q={**self.q,'observed_at':self.later(-120)})['reason'],'STALE_QUOTE')

    def test_cash_gross_and_commission_are_shared_budget_gates(self):
        self.prepare();a=paper._account_without_initialize(self.store,self.at)
        for changed in ({'available_cents':0},{'market_value_cents':a['equity_cents']*9//10}):
            with self.subTest(changed=changed),self.assertRaisesRegex(ValueError,'INSUFFICIENT_BUDGET_OR_TARGET_REACHED'):
                paper.buy_order_terms(self.store,self.cfg,self.p,self.q,self.at,account={**a,**changed})
        smallest=100*self.q['price_cents']
        with self.assertRaisesRegex(ValueError,'INSUFFICIENT_BUDGET_OR_TARGET_REACHED'):
            paper.buy_order_terms(self.store,self.cfg,self.p,self.q,self.at,account={**a,'available_cents':smallest})
        r=paper.buy_order_terms(self.store,self.cfg,self.p,self.q,self.at,account={**a,'available_cents':smallest+paper.fee(self.cfg,'BUY',smallest)})
        self.assertEqual(r['qty'],100)

    def test_new_drawdown_and_withdrawal_forecast_do_not_latch_or_create_cycles(self):
        self.prepare()
        with self.store.db:self.store.db.execute('UPDATE paper_accounts SET cash_cents=7500000')
        self.assertEqual(self.preview()['reason'],'ACCOUNT_DRAWDOWN_HALT')
        from ashare.portfolio_risk import state
        self.assertFalse(state(self.store).get('halted'))
        with self.store.db:self.store.db.execute('UPDATE paper_accounts SET cash_cents=20000000')
        self.assertEqual(self.preview()['reason'],'WITHDRAWAL_PENDING')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM withdrawal_cycles').fetchone()[0],0)

    def test_cloud_lease_and_research_role_are_conservative(self):
        self.prepare()
        from ashare.cloud_runtime import put
        with self.store.db:
            put(self.store,'initialized',True);put(self.store,'execution_enabled',True)
            put(self.store,'research_completed_at',self.later(-43201))
        r=self.preview(cfg={**self.cfg,'deployment_role':'cloud'})
        self.assertIn('RESEARCH_HEARTBEAT_EXPIRED',r['reason'])
        self.assertIn('LOCAL_RESEARCH_ONLY',self.preview(cfg={**self.cfg,'deployment_role':'research'})['reason'])

    def test_status_without_execution_checker_never_labels_buy(self):
        import test_trader_view as f
        s=f.status(watchlist=[f.item(plan=f.plan(),quote=f.quote(1550))],portfolio_strategy=f.ps(f.decision()))
        self.assertEqual(trader_view.build(s,f.CONFIG)['rows'][0]['code'],'ENTRY_UNVERIFIED')
