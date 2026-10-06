from approval_fixture import decide as confirmed_decide
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timedelta
from unittest.mock import patch
from ashare.storage import Store,normalize_time
from test_config import load_config
from ashare.demo import seed,put_quote,research_model,decision_model,run_demo,SYMBOL
from ashare.research import make_snapshot,study,model_packet
from ashare import governance
from ashare.slots import run_slot
from ashare.paper import settle,account
from ashare.review import run_review
from ashare.calendar import trading_day,phase
from ashare.scheduler import schedule_due


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.cfg=load_config(Path(__file__).resolve().parents[1]/'config.json')
        # Legacy model-mode coverage must not depend on the operator's live cadence.
        from ashare.settings import DEFAULTS
        for key in ('collection_times','slot_times','slot_deadline_seconds','slot_execution_mode'):
            self.cfg[key]=DEFAULTS[key]
        self.cfg.update(data_dir=self.tmp.name,watchlist=[{'symbol':SYMBOL,'name':'合成测试'}],paper_slippage_bps=0,paper_max_fill_qty=10000)
        self.at='2026-09-15T10:00:00+08:00'

    def tearDown(self):self.store.close();self.tmp.cleanup()

    def plan(self):
        p=seed(self.store,self.cfg)
        result=study(self.store,self.cfg,p,model_fn=research_model(p),at='2026-09-15T09:01:00+08:00')
        self.assertEqual(result['plan_kind'],'PAPER_TRADE')
        return p,result

    def slot(self,action='BUY',at=None):
        stamp=at or self.at
        return run_slot(self.store,self.cfg,stamp,refresh_fn=lambda *a:{'event_status':{SYMBOL:'OK'},'quote_status':'OK'},
            model_fn=decision_model(action),clock=lambda:stamp)

    def test_opening_slot_waits_for_quote_and_announcement_before_deciding(self):
        self.plan();stamp=[datetime.fromisoformat(normalize_time('2026-09-15T09:30:00+08:00'))]
        start=stamp[0].isoformat();pauses=[]
        run=self.store.db.execute('SELECT id FROM runs LIMIT 1').fetchone()[0]
        def wait(seconds):
            pauses.append(seconds);stamp[0]+=timedelta(seconds=seconds)
            if len(pauses)==2:put_quote(self.store,stamp[0].isoformat())
            if len(pauses)==3:
                with self.store.db:self.store.db.execute('INSERT INTO source_checks(run_id,source,symbol,status,detail,checked_at) VALUES(?,?,?,?,?,?)',
                    (run,'slot_events',SYMBOL,'OK','opening complete',stamp[0].isoformat()))
        with patch('ashare.slots.time.sleep',side_effect=wait),patch('ashare.monitor.now',side_effect=lambda:stamp[0].isoformat()):
            result=run_slot(self.store,self.cfg,start,model_fn=decision_model('BUY'),clock=lambda:stamp[0].isoformat())
        self.assertEqual(len(pauses),3)
        self.assertEqual(result['decisions'][0]['status'],'SUBMITTED')
        packet=json.loads(self.store.db.execute('SELECT input_json FROM slots WHERE id=?',(result['slot_id'],)).fetchone()[0])
        self.assertEqual(packet['stocks'][0]['buy_blockers'],[])
        self.assertEqual(packet['input_as_of'],stamp[0].isoformat())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],1)

    def test_missing_opening_data_stays_blocked_after_bounded_wait(self):
        self.plan();stamp=[datetime.fromisoformat(normalize_time('2026-09-15T09:30:00+08:00'))]
        start=stamp[0].isoformat();pauses=[]
        def wait(seconds):pauses.append(seconds);stamp[0]+=timedelta(seconds=seconds)
        with patch('ashare.slots.time.sleep',side_effect=wait),patch('ashare.monitor.now',side_effect=lambda:stamp[0].isoformat()),patch('ashare.slots.run_json') as model:
            result=run_slot(self.store,self.cfg,start,clock=lambda:stamp[0].isoformat())
        self.assertEqual(sum(pauses),30);model.assert_not_called()
        self.assertEqual(result['decisions'][0]['status'],'BLOCKED')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],0)

    def test_market_wait_preserves_model_deadline_and_prioritizes_risk_exit(self):
        from ashare.slots import wait_for_market_inputs
        self.plan();at=normalize_time(self.at)
        with patch('ashare.monitor.now',return_value=at),patch('ashare.slots.time.sleep') as sleep:
            deadline=normalize_time((datetime.fromisoformat(at)+timedelta(seconds=self.cfg['slot_model_timeout_seconds']+10)).isoformat())
            wait_for_market_inputs(self.store,self.cfg,deadline,clock=lambda:at)
            sleep.assert_not_called()
            put_quote(self.store,at,900)
            with patch('ashare.slots.positions',return_value={SYMBOL:{'qty':100,'cost_cents':100000}}):
                wait_for_market_inputs(self.store,self.cfg,normalize_time('2026-09-15T10:04:00+08:00'),clock=lambda:at)
            sleep.assert_not_called()

    def test_ready_time_and_revision_and_snapshot_immutability(self):
        p,_=self.plan();old=json.loads(self.store.db.execute('SELECT packet_json FROM snapshots WHERE id=?',(p['snapshot_id'],)).fetchone()[0])
        args=dict(symbol=SYMBOL,kind='news',title='现金流',source='fixture',url='https://example.test/revision',
            published_at='2026-09-15T09:00:00+08:00',first_seen_at='2026-09-15T09:01:00+08:00',raw_path='synthetic',cloud_allowed=True)
        first,_=self.store.add_document(**args,pages=[(1,'现金流增加')],ready_at='2026-09-15T10:04:00+08:00')
        self.assertFalse(self.store.search('现金流',self.at,SYMBOL)[-1]['doc_id']==first if self.store.search('现金流',self.at,SYMBOL) else False)
        second,_=self.store.add_document(**args,pages=[(1,'现金流修订下降')],ready_at='2026-09-15T11:00:00+08:00')
        before=self.store.documents_as_of('2026-09-15T10:30:00+08:00',SYMBOL)
        after=self.store.documents_as_of('2026-09-15T11:30:00+08:00',SYMBOL)
        self.assertIn(first,{d['id'] for d in before});self.assertNotIn(second,{d['id'] for d in before})
        self.assertIn(second,{d['id'] for d in after});self.assertNotIn(first,{d['id'] for d in after})
        self.assertEqual(old,json.loads(self.store.db.execute('SELECT packet_json FROM snapshots WHERE id=?',(p['snapshot_id'],)).fetchone()[0]))
        with self.assertRaises(Exception):
            with self.store.db:self.store.db.execute("UPDATE snapshots SET packet_json='{}'")

    def test_late_quote_partial_fill_dedup_target_and_t_plus_one(self):
        self.cfg['paper_max_fill_qty']=100
        self.plan();put_quote(self.store,self.at)
        first=self.slot();self.assertEqual(first['decisions'][0]['status'],'SUBMITTED')
        self.assertFalse(settle(self.store,self.cfg,self.at))
        self.assertEqual(self.slot()['status'],'ALREADY_DONE')
        put_quote(self.store,'2026-09-15T10:00:10+08:00')
        self.assertEqual(len(settle(self.store,self.cfg,'2026-09-15T10:00:10+08:00')),1)
        self.assertFalse(settle(self.store,self.cfg,'2026-09-15T10:00:10+08:00'))
        a=account(self.store,normalize_time('2026-09-15T10:00:10+08:00'))
        self.assertEqual(a['positions'][SYMBOL]['sellable_qty'],0);self.assertGreater(a['reserved_cents'],0)
        settle(self.store,self.cfg,'2026-09-15T10:05:00+08:00')
        self.assertEqual(account(self.store,normalize_time('2026-09-15T10:05:00+08:00'))['reserved_cents'],0)

    def test_second_slot_does_not_buy_target_again(self):
        self.plan();put_quote(self.store,self.at);self.slot()
        put_quote(self.store,'2026-09-15T10:00:10+08:00');settle(self.store,self.cfg,'2026-09-15T10:00:10+08:00')
        put_quote(self.store,'2026-09-15T10:30:00+08:00')
        second=self.slot(at='2026-09-15T10:30:00+08:00')
        self.assertEqual(second['decisions'][0]['status'],'BLOCKED')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],1)

    def test_new_event_blocks_buy(self):
        self.plan();put_quote(self.store,self.at)
        self.store.add_document(symbol=SYMBOL,kind='news',title='新风险',source='fixture',url='https://example.test/event',
            published_at=self.at,first_seen_at=self.at,ready_at=self.at,pages=[(1,'新出现的风险公告')],raw_path='synthetic',cloud_allowed=True)
        result=self.slot();self.assertEqual(result['decisions'][0]['status'],'BLOCKED')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],0)

    def test_timeout_cannot_place_order(self):
        self.plan();put_quote(self.store,self.at)
        times=iter([self.at,self.at,self.at,'2026-09-15T10:06:00+08:00'])
        result=run_slot(self.store,self.cfg,self.at,refresh_fn=lambda *a:{'event_status':{SYMBOL:'OK'}},model_fn=decision_model('BUY'),clock=lambda:next(times))
        self.assertEqual(result['status'],'EXPIRED');self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],0)

    def test_review_window_feedback_ready_and_no_duplicates(self):
        self.plan();put_quote(self.store,self.at);r=self.slot(action='BUY')
        did=self.store.db.execute('SELECT id FROM decisions').fetchone()[0]
        model=lambda *a:{'summary':'测试复盘','lessons':[{'symbol':SYMBOL,'category':'OBSERVATION','lesson':'等待明确条件',
            'decision_ids':[did],'fill_ids':[],'applicability':'下一次条件相同且证据仍有效时'}]}
        review=run_review(self.store,self.cfg,end='2026-09-15T19:30:00+08:00',model_fn=model,clock=lambda:'2026-09-15T20:05:00+08:00')
        self.assertEqual(review['statistics']['actions']['BUY'],1)
        before=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T20:00:00+08:00')
        after=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T20:06:00+08:00')
        # Review lessons are unvalidated hypotheses: they never enter research input.
        self.assertFalse(before['internal_lessons']);self.assertFalse(after['internal_lessons']);self.assertFalse(after['adopted_guidance'])
        self.assertNotIn('等待明确条件',json.dumps(model_packet(after,self.cfg),ensure_ascii=False))
        # The strategy observation is kept as a DRAFT change proposal instead.
        drafts=governance.proposals(self.store,'DRAFT');self.assertEqual(len(drafts),1)
        self.assertIn('等待明确条件',json.dumps(drafts[0]['payload'],ensure_ascii=False))
        # Only guidance adopted from a user-approved proposal reaches research, from its adoption time on.
        with self.store.db:
            pid=governance.draft_proposal(self.store,source='agent',kind='RESEARCH_GUIDANCE',target='watchlist',title='等待明确条件',
                payload={'guidance':{'route':'watchlist','scope':'ALL','text':'只有公司披露明确的经营变化时才改变结论'}},at='2026-09-15T20:07:00+08:00')
        governance.decide(self.store,pid,'READY',decided_by=None,note='整理完成',at='2026-09-15T20:07:00+08:00')
        with self.assertRaises(ValueError):governance.decide(self.store,pid,'APPROVED',decided_by='',note='代理不能自行批准',at='2026-09-15T20:07:00+08:00')
        confirmed_decide(self.store,pid,'APPROVED',decided_by='Dean',note='同意试行',at='2026-09-15T20:08:00+08:00')
        confirmed_decide(self.store,pid,'ADOPTED',decided_by='Dean',note='已上线',at='2026-09-15T20:09:00+08:00')
        adopted=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T20:10:00+08:00')
        self.assertEqual([g['text'] for g in adopted['adopted_guidance']],['只有公司披露明确的经营变化时才改变结论'])
        self.assertEqual(model_packet(adopted,self.cfg)['已采纳研究规则'][0]['id'],'G-'+pid)
        again=run_review(self.store,self.cfg,end='2026-09-15T19:30:00+08:00',model_fn=model,clock=lambda:'2026-09-15T20:06:00+08:00')
        self.assertEqual(again['status'],'ALREADY_DONE')

    def test_expired_plan_and_unknown_calendar(self):
        self.plan();put_quote(self.store,'2026-09-16T10:00:00+08:00')
        r=self.slot(at='2026-09-16T10:00:00+08:00')
        self.assertEqual(r['decisions'][0]['status'],'BLOCKED')
        self.assertIsNone(trading_day('2027-01-04'));self.assertEqual(phase('2027-01-04T10:00:00+08:00'),'CALENDAR_UNKNOWN')
        self.assertFalse(trading_day('2026-09-25'));self.assertFalse(trading_day('2026-09-20'))

    def test_restart_marks_late_slot_missed(self):
        with self.store.db:self.store.db.execute("INSERT INTO service_state VALUES('last_scan',?)",(normalize_time('2026-09-15T09:59:00+08:00'),))
        schedule_due(self.store,self.cfg,'2026-09-15T10:10:00+08:00')
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs WHERE kind='slot'").fetchone()[0],'MISSED')
        schedule_due(self.store,self.cfg,'2026-09-15T10:11:00+08:00')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)

    def test_deferred_research_does_not_replace_active_plan(self):
        _,r=self.plan();packet=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:05:00+08:00')
        second=study(self.store,self.cfg,packet,use_model=False,at='2026-09-15T09:06:00+08:00')
        self.assertEqual(second['status'],'DEFERRED')
        self.assertEqual(self.store.db.execute("SELECT id FROM plans WHERE status='ACTIVE'").fetchone()[0],r['plan_id'])

    def test_cost_stop_survives_expired_research(self):
        self.plan();put_quote(self.store,self.at);self.slot()
        put_quote(self.store,'2026-09-15T10:00:10+08:00');settle(self.store,self.cfg,'2026-09-15T10:00:10+08:00')
        put_quote(self.store,'2026-09-16T10:00:00+08:00',935)
        r=self.slot('HOLD',at='2026-09-16T10:00:00+08:00')
        self.assertEqual(r['decisions'][0]['action'],'SELL');self.assertEqual(r['decisions'][0]['status'],'SUBMITTED')
        self.assertEqual(json.loads(self.store.db.execute("SELECT payload_json FROM plans WHERE status='ACTIVE'").fetchone()[0])['kind'],'RISK_EXIT_ONLY')

    def test_commission_policy_frozen_when_order_is_created(self):
        self.plan();put_quote(self.store,self.at);self.slot()
        self.cfg['paper_commission_bps']=100
        put_quote(self.store,'2026-09-15T10:00:10+08:00');settle(self.store,self.cfg,'2026-09-15T10:00:10+08:00')
        self.assertEqual(self.store.db.execute('SELECT fee_cents FROM paper_fills').fetchone()[0],570)

    def test_retry_deferred_review_without_changing_old_revision(self):
        self.plan()
        r=run_review(self.store,self.cfg,end='2026-09-15T19:30:00+08:00',use_model=False,clock=lambda:'2026-09-15T20:00:00+08:00')
        newer=run_review(self.store,self.cfg,end='2026-09-15T19:30:00+08:00',model_fn=lambda *a:{'summary':'完成','lessons':[]},clock=lambda:'2026-09-15T20:10:00+08:00')
        self.assertEqual(newer['revision'],2);self.assertEqual(self.store.db.execute('SELECT count(*) FROM reviews').fetchone()[0],2)

    def test_shared_cash_reservation_across_independent_connections(self):
        from concurrent.futures import ThreadPoolExecutor
        from ashare.paper import submit
        from ashare.research import encode
        import copy
        packet,_=self.plan();put_quote(self.store,self.at)
        self.cfg['paper_max_gross_pct']=25
        jobs=[]
        for n,sym in enumerate([SYMBOL,'sh600000']):
            p=copy.deepcopy(packet);p['snapshot_id']='concurrent-snapshot-'+str(n);p['symbol']=sym;p['stocks'][0]['symbol']=sym
            for e in p['evidence']:e['symbol']=sym
            with self.store.db:
                self.store.db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)',(p['snapshot_id'],None,sym,p['as_of'],p['as_of'],'fixture',encode(p)))
                if sym==SYMBOL:
                    self.store.db.execute('INSERT INTO snapshot_members SELECT ?,doc_id FROM snapshot_members WHERE snapshot_id=?',(p['snapshot_id'],packet['snapshot_id']))
            model=lambda *a,sym=sym:{'summary':'fixture','stocks':[{'symbol':sym,'action':'WATCH','analysis':'fixture','facts':[], 'counterpoints':[],'missing_fields':[],'next_checks':[]}]}
            plan_result=study(self.store,self.cfg,p,model_fn=model,at='2026-09-15T09:02:00+08:00')
            plan=dict(self.store.db.execute('SELECT * FROM plans WHERE id=?',(plan_result['plan_id'],)).fetchone())
            at=normalize_time(self.at);qid='concurrent-q-'+str(n)
            with self.store.db:
                self.store.db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?)',(qid,sym,'合成股票',1000,1000,at,at,'fixture','fixture'))
                self.store.db.execute('INSERT INTO slots VALUES(?,?,?,?,?,?,?)',('concurrent-slot-'+str(n),normalize_time('2026-09-15T10:0'+str(n)+':00+08:00'),at,at,'SUCCEEDED','{}','SUCCEEDED'))
                did='concurrent-decision-'+str(n)
                self.store.db.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?)',(did,'concurrent-slot-'+str(n),sym,plan['id'],at,'BUY','RECORDED','fixture','{}'))
            jobs.append((did,plan,dict(self.store.db.execute('SELECT * FROM quotes WHERE id=?',(qid,)).fetchone())))
        def send(args):
            other=Store(self.tmp.name)
            try:return submit(other,self.cfg,args[0],args[1],'BUY',args[2],self.at)
            finally:other.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(send,jobs))
        self.assertEqual(len([r for r in results if 'id' in r]),2)
        a=account(self.store,normalize_time(self.at))
        self.assertLessEqual(a['reserved_cents'],2500000)
        self.assertGreaterEqual(a['available_cents'],7500000)

    def test_late_execution_report_creates_review_revision(self):
        self.plan();put_quote(self.store,self.at);self.slot()
        put_quote(self.store,'2026-09-15T10:00:10+08:00');settle(self.store,self.cfg,'2026-09-15T10:00:10+08:00')
        with self.store.db:self.store.db.execute('UPDATE paper_fills SET recorded_at=?',(normalize_time('2026-09-15T20:05:00+08:00'),))
        first=run_review(self.store,self.cfg,end='2026-09-15T19:30:00+08:00',use_model=False,clock=lambda:'2026-09-15T20:00:00+08:00')
        second=run_review(self.store,self.cfg,end='2026-09-15T19:30:00+08:00',use_model=False,clock=lambda:'2026-09-15T20:10:00+08:00')
        self.assertEqual(first['statistics']['fill_count'],0);self.assertEqual(second['statistics']['fill_count'],1)
        self.assertEqual(second['revision'],2)

    def test_local_only_content_never_enters_model_packet(self):
        seed(self.store,self.cfg)
        self.store.add_document(symbol=SYMBOL,kind='broker_report',title='内部机密报告标题',source='fixture',url='file:///private-report.pdf',
            published_at=self.at,first_seen_at=self.at,ready_at=self.at,pages=[(1,'SENSITIVE_PRIVATE_CONTENT')],raw_path='private',cloud_allowed=False)
        packet=make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T10:01:00+08:00')
        text=json.dumps(packet,ensure_ascii=False)
        self.assertNotIn('SENSITIVE_PRIVATE_CONTENT',text);self.assertNotIn('内部机密报告标题',text)
        self.assertEqual(packet['local_only_document_count'],1)

    def test_synthetic_complete_cycle(self):
        result=run_demo(self.cfg)
        self.assertEqual([f['side'] for f in result['fills']],['BUY','SELL'])
        self.assertEqual(result['t_plus_one_slot']['decisions'][0]['status'],'BLOCKED')
        self.assertFalse(result['account']['positions'])
        self.assertEqual(result['review']['statistics']['fill_count'],1)

    def test_dashboard_empty_account_and_next_scheduled_runs(self):
        from ashare.reporting import portfolio,trade_effects,next_runs
        a=account(self.store,self.at)
        p=portfolio(self.store,self.cfg,a,self.at)
        self.assertEqual(p['cash_weight_pct'],100)
        self.assertEqual(p['holdings'],[])
        self.assertIsNone(trade_effects(self.store,self.cfg,self.at)['buy'])
        self.assertIsNone(trade_effects(self.store,self.cfg,self.at)['sell'])
        runs=next_runs(self.cfg,'2026-09-17T02:00:00+08:00')
        self.assertEqual(runs[0],{'kind':'cycle','scheduled_at':normalize_time('2026-09-17T08:00:00+08:00')})
        self.assertEqual(runs[1]['scheduled_at'],normalize_time('2026-09-17T09:30:00+08:00'))
        self.assertEqual(next_runs({**self.cfg,'scheduler_enabled':False},self.at),[])
        self.assertNotIn('slot',[r['kind'] for r in next_runs(self.cfg,'2027-01-01T02:00:00+08:00')])

    def test_dashboard_partial_fills_aggregate_and_mark_unrealized(self):
        from ashare.reporting import portfolio,trade_effects
        self.cfg['paper_max_fill_qty']=100
        self.plan();put_quote(self.store,self.at);self.slot()
        put_quote(self.store,'2026-09-15T10:00:10+08:00');settle(self.store,self.cfg,'2026-09-15T10:00:10+08:00')
        put_quote(self.store,'2026-09-15T10:00:20+08:00');settle(self.store,self.cfg,'2026-09-15T10:00:20+08:00')
        at='2026-09-15T10:00:30+08:00';put_quote(self.store,at,1050)
        t=trade_effects(self.store,self.cfg,at)
        self.assertEqual(t['buy']['filled_qty'],200)
        self.assertEqual(t['buy']['fill_count'],2)
        self.assertEqual(t['buy']['fee_cents'],500)
        self.assertEqual(t['buy']['unrealized_cents'],9500)
        self.assertEqual(t['buy']['order_status'],'PARTIAL')
        self.assertIsNone(t['sell'])
        a=account(self.store,at);p=portfolio(self.store,self.cfg,a,at)
        self.assertEqual(p['holdings'][0]['sellable_qty'],0)
        self.assertEqual(p['holdings'][0]['average_cost_cents'],1002.5)
        self.assertEqual(p['holdings'][0]['unrealized_return_pct'],4.74)
        self.assertEqual(p['holdings'][0]['quote_source'],'SYNTHETIC_FIXTURE')
        self.assertEqual(p['holdings'][0]['quote_first_seen_at'],normalize_time(at))
        self.assertEqual(p['cumulative_realized_cents'],0)
        self.assertEqual(p['cumulative_dividend_cents'],0)
        self.assertEqual(p['unrealized_cents'],9500)
        self.assertAlmostEqual(p['cash_weight_pct']+p['stock_weight_pct'],100,places=2)
        # A missing quote is unknown P&L, never a fictitious zero return.
        with patch.object(self.store,'latest_quote',return_value=None):
            missing=portfolio(self.store,self.cfg,account(self.store,at),at)
            self.assertIsNone(missing['unrealized_cents'])
            self.assertEqual(missing['holdings'][0]['valuation_basis'],'COST_FALLBACK')
            self.assertEqual(missing['holdings'][0]['average_cost_cents'],1002.5)
            self.assertIsNone(missing['holdings'][0]['unrealized_return_pct'])
            self.assertIsNone(missing['holdings'][0]['quote_source'])
            self.assertIsNone(missing['holdings'][0]['quote_first_seen_at'])
            self.assertIsNone(trade_effects(self.store,self.cfg,at)['buy']['unrealized_cents'])

    def test_dashboard_cumulative_components_include_closed_routes_and_prior_dividends(self):
        import sqlite3
        from types import SimpleNamespace
        from ashare.reporting import portfolio
        # Minimal isolated accounting fixture: no open positions, but every route
        # has historical realized P&L. No execution engine or model is involved.
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE paper_flows(account_id TEXT,kind TEXT,amount_cents INT,created_at TEXT)')
        at=normalize_time(self.at);prior=normalize_time('2026-09-14T10:00:00+08:00');future=normalize_time('2026-09-16T10:00:00+08:00')
        for table,profit in (('paper_fills',1000),('dynamic_fills',-200),('global_fills',300)):
            db.execute('CREATE TABLE '+table+'(realized_cents INT,occurred_at TEXT)')
            db.executemany('INSERT INTO '+table+' VALUES(?,?)',[(profit,prior),(9999,future)])
        db.executemany('INSERT INTO paper_flows VALUES(?,?,?,?)',[
            ('DEMO_PAPER','SIMULATED_INITIAL',10000000,prior),
            ('DEMO_PAPER','CASH_DIVIDEND',25100,prior),
            ('DEMO_PAPER','CASH_DIVIDEND',54321,future),
            ('OTHER_ACCOUNT','CASH_DIVIDEND',45600,prior)])
        account_view={'positions':{},'cash_cents':10026200,'equity_cents':10026200,
                      'market_value_cents':0,'initial_cents':10000000,'withdrawn_cents':0}
        changes=db.total_changes
        result=portfolio(SimpleNamespace(db=db),self.cfg,account_view,at)
        self.assertEqual(result['holdings'],[])
        self.assertEqual(result['cumulative_realized_cents'],1100)
        self.assertEqual(result['cumulative_dividend_cents'],25100)
        self.assertEqual(result['total_profit_cents'],26200)
        self.assertEqual(result['unrealized_cents'],0)
        self.assertEqual(db.total_changes,changes)

    def test_cost_display_uses_remaining_lots_and_keeps_dynamic_basis_separate(self):
        from ashare.reporting import portfolio
        self.plan();put_quote(self.store,self.at);self.slot()
        stamp='2026-09-15T10:00:10+08:00';put_quote(self.store,stamp);settle(self.store,self.cfg,stamp)
        with self.store.db:
            # Residual fee basis after a partial sale need not be whole cents/share.
            self.store.db.execute('UPDATE paper_lots SET qty=300,cost_cents=303001')
        stamp='2026-09-15T10:00:20+08:00';put_quote(self.store,stamp,1010)
        a=account(self.store,stamp)
        a['dynamic_positions']={SYMBOL:{'qty':100,'cost_cents':110500,'market_value_cents':101000,'mark_cents':1010,'quote_at':normalize_time(stamp)}}
        before=dict(self.store.db.execute('SELECT * FROM paper_lots').fetchone())
        with patch('ashare.dynamic_sources.latest_quote',return_value=self.store.latest_quote(SYMBOL,stamp)):
            h={r['origin']:r for r in portfolio(self.store,self.cfg,a,stamp)['holdings']}
        self.assertAlmostEqual(h['watchlist']['average_cost_cents'],1010.0033333333333)
        self.assertEqual(h['watchlist']['unrealized_cents'],-1)
        self.assertEqual(h['dynamic']['average_cost_cents'],1105)
        self.assertEqual(h['dynamic']['unrealized_return_pct'],-8.60)
        self.assertEqual(dict(self.store.db.execute('SELECT * FROM paper_lots').fetchone()),before)

    def test_dashboard_closed_trade_uses_realized_fifo_cost_and_fees(self):
        from ashare.reporting import trade_effects,portfolio
        result=run_demo(self.cfg);other=Store(result['data_dir']);at='2026-09-16T19:31:00+08:00'
        try:
            t=trade_effects(other,self.cfg,at)
            self.assertEqual(t['buy']['remaining_qty'],0)
            self.assertIsNone(t['buy']['unrealized_cents'])
            sell=t['sell'];buy=t['buy']
            expected=sell['gross_cents']-sell['fee_cents']-buy['gross_cents']-buy['fee_cents']
            self.assertLess(expected,0)
            self.assertEqual(sell['realized_cents'],expected)
            self.assertEqual(sell['cost_basis_cents'],buy['gross_cents']+buy['fee_cents'])
            self.assertEqual(t['totals']['realized_cents'],expected)
            a=account(other,at)
            self.assertEqual(portfolio(other,self.cfg,a,at)['total_profit_cents'],expected)
        finally:other.close()

    def test_morning_schedule_is_enqueued_once_after_overnight(self):
        with self.store.db:self.store.db.execute("INSERT INTO service_state VALUES('last_scan',?)",(normalize_time('2026-09-17T02:00:00+08:00'),))
        schedule_due(self.store,self.cfg,'2026-09-17T08:00:05+08:00')
        schedule_due(self.store,self.cfg,'2026-09-17T08:00:15+08:00')
        jobs=list(self.store.db.execute('SELECT kind,status,scheduled_at FROM jobs'))
        self.assertEqual(len(jobs),1)
        self.assertEqual(tuple(jobs[0]),('cycle','PENDING',normalize_time('2026-09-17T08:00:00+08:00')))


if __name__=='__main__':unittest.main()
