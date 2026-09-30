"""Regression cases from the 2026-09-30 audit; all data is synthetic."""
import json
import unittest
from unittest.mock import patch
from ashare import evaluation, shadow, review_checks, global_research, backtest
from ashare.storage import normalize_time as n
from ashare.strategy_math import global_trend
from ashare.demo import SYMBOL, research_model
from ashare.research import study
import test_evaluation as fx
import test_dynamic as dyn
import test_investment_policy as glob


class ScoreAuditTests(fx.Fixture):
    def test_entry_is_strictly_after_completion_before_during_after_session_and_holiday(self):
        days = fx.trading_days('2026-09-24', 6)
        bars = {d: (10, 11, 12, 9) for d in days}
        row = {'as_of_day': '2026-09-23', 'horizon_days': 1, 'benchmark': None}
        for at, expected in [('2026-09-24T09:29:59+08:00', '2026-09-24'),
                             ('2026-09-24T09:30:00+08:00', '2026-09-28'),
                             ('2026-09-24T12:00:00+08:00', '2026-09-28'),
                             ('2026-09-24T17:00:00+08:00', '2026-09-28'),
                             ('2026-09-26T10:00:00+08:00', '2026-09-28')]:
            with self.subTest(at=at):
                score = evaluation.score_row({**row, 'created_at': n(at)}, bars, {})
                self.assertEqual(score['entry_date'], expected)
                self.assertGreater(score['entry_at'], n(at))

    def test_missing_benchmark_remains_retryable_and_legacy_score_is_unchanged(self):
        study(self.store, self.cfg, self.packet, model_fn=research_model(self.packet), at='2026-09-15T12:00:00+08:00')
        days = fx.trading_days('2026-09-10', 12)
        self.features([(d, 10, 11, 12, 9) for d in days])
        with self.store.db:
            self.store.db.execute("UPDATE signal_registry SET status='SCORED',score_json='{}',scored_at='legacy'")
        self.assertEqual(evaluation.score(self.store, self.cfg, n('2026-09-30T20:00:00+08:00'))['pending'], 1)
        self.benchmark(days, [4000]*len(days))
        self.assertEqual(evaluation.score(self.store, self.cfg, n('2026-09-30T20:00:00+08:00'))['scored'], 1)
        self.assertEqual(tuple(self.store.db.execute('SELECT status,score_json,scored_at FROM signal_registry').fetchone()), ('SCORED','{}','legacy'))
        self.assertEqual(evaluation.load(self.store, 'watchlist')[0]['score']['entry_date'], '2026-09-16')

    def test_missing_entry_day_does_not_shift_the_window(self):
        row={'as_of_day':'2026-09-23','horizon_days':1,'benchmark':None,'created_at':n('2026-09-24T09:00:00+08:00')}
        self.assertIsNone(evaluation.score_row(row,{'2026-09-28':(10,11,12,9)},{}))

    def test_opposite_groups_do_not_each_keep_overlapping_window_and_builds_are_separate(self):
        with self.store.db:
            for i, (entry, exit_, action, build) in enumerate([
                ('2026-09-01','2026-09-08','WATCH','a'),
                ('2026-09-02','2026-09-09','WAIT','b'),
                ('2026-09-10','2026-09-17','WAIT','b')]):
                rid = str(i)
                evaluation._insert(self.store, rid, 'watchlist', SYMBOL, rid, entry, entry, build, 5, None,
                                   {'trend_ok': True, 'model_action': action})
                self.store.db.execute('INSERT INTO signal_scores VALUES(?,?,?,?,?)',
                    (rid,evaluation.SCORE_METHOD,'SCORED',json.dumps({'entry_date':entry,'exit_date':exit_,'excess_bps':100}),exit_))
        result = evaluation.comparisons(self.store, self.cfg)
        groups = result['groups']['research_veto']
        self.assertEqual(groups['NOT_WATCH']['daily']['n'], 2)
        self.assertEqual(groups['NOT_WATCH']['non_overlapping']['n'], 1)
        self.assertEqual(groups['WATCH']['non_overlapping']['n'], 1)
        self.assertTrue(result['mixed_builds']);self.assertEqual(set(result['by_build']), {'a','b'})
        self.assertIsNone(groups['WATCH']['non_overlapping']['ci95_bps'])

    def test_correlated_symbols_do_not_inflate_confidence(self):
        samples = [{'symbol':str(i),'score':{'entry_date':'2026-09-01','exit_date':'2026-09-30','excess_bps':i}} for i in range(100)]
        result = evaluation.describe(evaluation.time_clusters(samples))
        self.assertEqual(result['n'], 100);self.assertEqual(result['time_clusters'], 1)
        self.assertIsNone(result['ci95_bps'])


class ShadowAuditTests(fx.Fixture):
    def setUp(self):
        super().setUp()
        self.days = fx.trading_days('2026-05-06', 67)
        self.entry, self.second, self.last = self.days[-3:]
        self.bars = [(d, 10+i*.01, 10+i*.01, 10.1+i*.01, 9.9+i*.01) for i,d in enumerate(self.days)]
        self.cfg['shadow_start_date'] = self.entry

    def collect(self, bars, key='v1'):
        payload = {'unadjusted':{'bars':[[d,str(o),str(c),str(h),str(l),'1000'] for d,o,c,h,l in bars]}}
        with self.store.db:
            self.store.db.execute('INSERT INTO market_features VALUES(?,?,?,?,?)',
                                 (key,SYMBOL,'2026-09-30T00:00:00+00:00',None,json.dumps(payload)))

    def plan(self, hour):
        r = study(self.store, self.cfg, self.packet, model_fn=research_model(self.packet), at='2026-09-15T09:01:00+08:00')
        sig = shadow.indicators(shadow.bars_for(self.store,SYMBOL), self.entry, self.cfg)
        payload = {'kind':'PAPER_TRADE','blockers':[],'levels':sig['levels']}
        with self.store.db:
            self.store.db.execute('UPDATE plans SET activated_at=?,valid_until=?,payload_json=? WHERE id=?',
                (n(self.entry+'T'+hour+':00+08:00'),n(self.last+'T20:00:00+08:00'),json.dumps(payload),r['plan_id']))
        return r['plan_id']

    def test_midmorning_plan_cannot_fill_at_open(self):
        self.collect(self.bars);self.plan('09:45')
        r = shadow.run(self.store,self.cfg,n(self.entry+'T20:00:00+08:00'))
        buys = list(self.store.db.execute("SELECT book FROM shadow_trades_v2 WHERE run_id=? AND side='BUY'",(r['run_id'],)))
        self.assertIn('A-lot',[b[0] for b in buys]);self.assertNotIn('B-lot',[b[0] for b in buys])

    def test_midmorning_portfolio_authorization_cannot_enable_opening_entry(self):
        self.collect(self.bars);self.plan('08:00')
        with self.store.db:
            self.store.db.execute('INSERT INTO portfolio_decisions VALUES(?,?,?,?,?,?)',
                ('late',n(self.entry+'T09:45:00+08:00'),n(self.last+'T20:00:00+08:00'),'ACTIVE','test',
                 json.dumps({'decisions':[{'key':'watchlist:'+SYMBOL,'action':'ALLOW','target_bps':1500}]})))
        r=shadow.run(self.store,self.cfg,n(self.entry+'T20:00:00+08:00'))
        books={r[0] for r in self.store.db.execute("SELECT book FROM shadow_trades_v2 WHERE run_id=? AND side='BUY'",(r['run_id'],))}
        self.assertIn('B-lot',books);self.assertNotIn('C-lot',books)

    def test_expiry_before_close_does_not_allow_unknown_intraday_touch(self):
        self.bars[-3]=(self.entry,11.5,11,11.5,10.3)
        self.collect(self.bars);pid=self.plan('08:00')
        with self.store.db:self.store.db.execute('UPDATE plans SET valid_until=? WHERE id=?',(n(self.entry+'T11:00:00+08:00'),pid))
        r=shadow.run(self.store,self.cfg,n(self.entry+'T20:00:00+08:00'))
        books={r[0] for r in self.store.db.execute("SELECT book FROM shadow_trades_v2 WHERE run_id=? AND side='BUY'",(r['run_id'],))}
        self.assertIn('A-lot',books);self.assertNotIn('B-lot',books)

    def test_missing_middle_day_blocks_later_days_and_late_data_creates_new_batch(self):
        self.collect([b for b in self.bars if b[0]!=self.second])
        at = n(self.last+'T20:00:00+08:00')
        first = shadow.run(self.store,self.cfg,at)
        self.assertEqual(first['status'],'PARTIAL');self.assertEqual(first['through'],self.entry)
        before = [tuple(r) for r in self.store.db.execute('SELECT * FROM shadow_days_v2 WHERE run_id=?',(first['run_id'],))]
        self.collect([b for b in self.bars if b[0]==self.second], 'late')
        later = shadow.run(self.store,self.cfg,at)
        self.assertNotEqual(first['run_id'],later['run_id']);self.assertEqual(later['status'],'SUCCEEDED')
        self.assertEqual(later['days'],[self.entry,self.second,self.last])
        self.assertEqual(before,[tuple(r) for r in self.store.db.execute('SELECT * FROM shadow_days_v2 WHERE run_id=?',(first['run_id'],))])
        self.assertEqual(shadow.summary(self.store,self.cfg)['A-lot']['run_id'],later['run_id'])
        self.assertEqual(shadow.run(self.store,self.cfg,at)['days'],[])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM shadow_book_days').fetchone()[0],0)

    def test_missing_symbol_is_waiting_not_stale_mark_and_failed_replay_is_atomic(self):
        self.collect(self.bars)
        self.cfg['watchlist'].append({'symbol':'sh600000'})
        result = shadow.run(self.store,self.cfg,n(self.entry+'T20:00:00+08:00'))
        self.assertEqual(result['status'],'WAITING_DATA');self.assertEqual(shadow.summary(self.store,self.cfg),{})
        self.cfg['watchlist'].pop()
        original = shadow.run_day
        def fail(*args):
            original(*args)
            raise RuntimeError('synthetic interruption')
        with patch('ashare.shadow.run_day',side_effect=fail),self.assertRaises(RuntimeError):
            shadow.run(self.store,self.cfg,n(self.entry+'T20:00:00+08:00'))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM shadow_days_v2').fetchone()[0],0)
        self.assertEqual(shadow.status(self.store)['run_id'],result['run_id'])

    def test_afternoon_exit_does_not_finance_entry_or_same_day_reentry(self):
        self.collect(self.bars)
        data = shadow.bars_for(self.store,SYMBOL)
        data[self.entry]=(1050,950,1060,900)
        new = 'sh600000';other = dict(data);other[self.entry]=(1050,1050,1060,1040)
        state={'cash':1_200_000,'equity':10_000_000,'peak':10_000_000,
               'positions':{SYMBOL:{'qty':7000,'cost_cents':7_000_000,'last':1000,'entry_day':self.days[-4]}}}
        with self.store.db:
            self.store.db.execute('INSERT INTO shadow_days_v2 VALUES(?,?,?,?)',('test','A-lot',self.days[-4],json.dumps(state)))
            shadow.run_day(self.store,self.cfg,self.entry,[new,SYMBOL],{new:other,SYMBOL:data},'test')
        trades=[dict(r) for r in self.store.db.execute("SELECT * FROM shadow_trades_v2 WHERE run_id='test' AND book='A-lot'")]
        self.assertTrue(any(t['side']=='SELL' for t in trades))
        buys=[t for t in trades if t['side']=='BUY']
        self.assertTrue(buys);self.assertFalse(any(t['symbol']==SYMBOL for t in buys))
        self.assertLessEqual(sum(t['qty']*t['price_cents']+t['fee_cents'] for t in buys),1_200_000)


class GlobalAuditTests(unittest.TestCase):
    setUp = glob.InvestmentTests.setUp
    tearDown = glob.InvestmentTests.tearDown
    later = glob.InvestmentTests.later
    quote = glob.InvestmentTests.quote
    plan = glob.InvestmentTests.plan
    buy = glob.InvestmentTests.buy

    def test_backtest_uses_exact_production_history_indices(self):
        # Old code compares the latest close with index -6, incorrectly allowing this.
        prices=[90]*16+[110,90,90,90,100]
        self.assertFalse(global_trend([p*1_000_000 for p in prices]))
        bars=[(f'2026-01-{i+1:02d}',p,p,p,p) for i,p in enumerate(prices+[100])]
        self.assertEqual(backtest.simulate_global(bars,{'max_hold':5}),[])
        prices=[90]*19+[100]
        self.assertTrue(global_trend([p*1_000_000 for p in prices]))
        bars=[(f'2026-01-{i+1:02d}',p,p,p,p) for i,p in enumerate(prices+[100])]
        self.assertEqual(backtest.simulate_global(bars,{'max_hold':5})[0]['entry_date'],'2026-01-21')

    def test_reuse_invalidated_by_build_and_adopted_guidance(self):
        bars=[{'date':f'2026-08-{i+1:02d}','price_micros':(100+i)*1_000_000} for i in range(20)]
        with self.store.db:self.store.db.execute('INSERT INTO global_market VALUES(?,?,?)',('BTC',self.at,json.dumps({'status':'OK','bars':bars})))
        calls=[]
        def model(*args):
            calls.append(args[0])
            return {'stance':'LONG','thesis':'synthetic','counterpoints':['uncertain'],'holding_days':5,'evidence_ids':['PRICE_HISTORY'],'next_checks':[]}
        with patch('ashare.global_research.targets',return_value={'BTC':{'asset':'BTC','name':'test','links':[]}}):
            global_research.run(self.store,self.cfg,self.at,model_fn=model,fetch_quotes=False)
            r=global_research.run(self.store,self.cfg,self.later(1),model_fn=model,fetch_quotes=False)
            self.assertEqual(r['plans'][0]['status'],'REUSED');self.assertEqual(len(calls),1)
            cfg={**self.cfg,'model_name':'synthetic-model'}
            global_research.run(self.store,cfg,self.later(2),model_fn=model,fetch_quotes=False)
            self.assertEqual(len(calls),2)
            with self.store.db:self.store.db.execute("INSERT INTO strategy_guidance VALUES('test','global','BTC','test adopted constraint','ADOPTED',NULL,?,NULL,'fixture','{}')",(self.at,))
            global_research.run(self.store,cfg,self.later(3),model_fn=model,fetch_quotes=False)
            self.assertEqual(len(calls),3);self.assertIn('test adopted constraint',calls[-1])

    def test_global_fills_are_checked_and_pre_halt_order_cannot_escape_fill_time_check(self):
        from ashare import global_paper
        o,plan,item=self.buy();self.quote(at=self.later(60))
        global_paper.settle(self.store,self.cfg,self.later(60),{'BTC':item})
        def checks():return {r['check']:r for r in review_checks.execution(self.store,self.cfg,self.later(-1),self.later(120),self.later(120))}
        self.assertEqual(checks()['CHECK_BUY_OUTSIDE_PLAN_BAND']['by_route']['global']['checked'],1)
        with self.store.db:
            self.store.db.execute('UPDATE global_fills SET price_micros=102000000')
            self.store.db.execute('UPDATE portfolio_risk SET payload_json=?',(json.dumps({'halted':True,'triggered_at':self.later(30)}),))
        self.assertEqual(checks()['CHECK_BUY_OUTSIDE_PLAN_BAND']['status'],'FAIL')
        self.assertEqual(checks()['CHECK_BUY_WHILE_HALTED']['status'],'FAIL')


class DynamicAuditTests(unittest.TestCase):
    setUp = dyn.DynamicTests.setUp
    tearDown = dyn.DynamicTests.tearDown
    news = dyn.DynamicTests.news
    case = dyn.DynamicTests.case
    quote = dyn.DynamicTests.quote
    buy = dyn.DynamicTests.buy
    fill = dyn.DynamicTests.fill

    def checks(self):
        return {r['check']:r for r in review_checks.execution(self.store,self.cfg,n('2026-09-15T00:00:00+08:00'),n('2026-09-16T00:00:00+08:00'),n('2026-09-16T00:00:00+08:00'))}

    def test_empty_is_not_applicable_and_dynamic_uses_immutable_entry(self):
        self.assertTrue(all(r['status']=='NOT_APPLICABLE' for r in self.checks().values()))
        case=self.case();self.buy(case);self.fill()
        with self.store.db:self.store.db.execute("UPDATE dynamic_cases SET analysis_json='{}',plan_json='{}',status='INVALIDATED'")
        from ashare.review_portfolio import research_record
        record=research_record(self.store,'dynamic',case['id'],n('2026-09-16T00:00:00+08:00'))
        self.assertEqual(record['basis'],'ORDER_SNAPSHOT');self.assertEqual(record['plan']['buy_high_cents'],1010)
        self.assertEqual(record['analysis'],json.loads(case['analysis_json']))
        self.assertEqual(self.checks()['CHECK_BUY_WITH_PLAN_BLOCKERS']['status'],'PASS')
        with self.store.db:self.store.db.execute('UPDATE dynamic_fills SET price_cents=1100')
        self.assertEqual(self.checks()['CHECK_BUY_OUTSIDE_PLAN_BAND']['status'],'FAIL')

    def test_missing_original_snapshot_is_insufficient(self):
        case=self.case();self.buy(case);self.fill()
        with self.store.db:self.store.db.execute("UPDATE dynamic_orders SET terms_json='{}'")
        result=self.checks()
        self.assertEqual(result['CHECK_EXECUTION_EVIDENCE']['status'],'INSUFFICIENT')
        self.assertEqual(result['CHECK_BUY_OUTSIDE_PLAN_BAND']['status'],'INSUFFICIENT')
        self.assertEqual(result['CHECK_BUY_OUTSIDE_PLAN_BAND']['checked'],0)

    def test_orphan_fill_is_detected_instead_of_dropped(self):
        case=self.case();self.buy(case);self.fill()
        self.store.db.execute('PRAGMA foreign_keys=OFF')
        with self.store.db:self.store.db.execute("UPDATE dynamic_fills SET order_id='missing'")
        result=self.checks()
        self.assertEqual(result['CHECK_EXECUTION_EVIDENCE']['status'],'INSUFFICIENT')
        self.assertEqual(result['CHECK_EXECUTION_EVIDENCE']['by_route']['dynamic']['fills'],1)
