"""Behavioral regression for the R1-only trend waiver, through study and real reuse."""
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from ashare import market_regime, research
from ashare.cloud_runtime import put
from ashare.demo import SYMBOL, research_model, seed
from ashare.pipeline import load_config
from ashare.storage import Store


class TrendGatePathsTests(unittest.TestCase):
    @contextmanager
    def fixture(self, mode='D14', features=('exposure_caps_v2',), policy='days_cash_v1',
                levels=(900, 1000, 950)):
        with tempfile.TemporaryDirectory(prefix='d14-trend-path-') as folder:
            cfg=load_config(Path(__file__).resolve().parents[1]/'config.example.json',configure_model=False)
            cfg.update(data_dir=folder,watchlist=[{'symbol':SYMBOL,'name':'Synthetic'}],
                       deployment_role='research',exposure_regime=mode,investment_policy=policy,
                       portfolio_strategy=None,industry_enabled=False,model_enabled=True,
                       research_reuse_hours=6,cost_budget=None)
            store=Store(folder)
            try:
                seed(store,cfg)
                # Persist synthetic features first, so make_snapshot/reusable see the same
                # real stored input instead of a caller-mutated packet or fake reuse record.
                row=store.db.execute('SELECT rowid,payload FROM market_features LIMIT 1').fetchone()
                f=json.loads(row['payload'])
                f['unadjusted'].update(ma20_cents=levels[0],ma60_cents=levels[1],close_cents=levels[2])
                with store.db:
                    store.db.execute('UPDATE market_features SET payload=? WHERE rowid=?',(json.dumps(f),row['rowid']))
                    put(store,'remote_features',list(features))
                packet=research.make_snapshot(store,cfg,SYMBOL,at='2026-09-15T09:01:00+08:00')
                model=Mock(side_effect=research_model(packet))
                with patch('ashare.research.run_json',side_effect=AssertionError('real model forbidden')):
                    result=research.study(store,cfg,packet,model_fn=model,at='2026-09-15T09:02:00+08:00')
                self.assertEqual(result['status'],'SUCCEEDED',result)
                self.assertEqual(model.call_count,1)
                yield store,cfg,result,model
            finally:
                store.close()

    def plan(self,store,pid):
        row=store.db.execute('SELECT * FROM plans WHERE id=?',(pid,)).fetchone()
        self.assertIsNotNone(row)
        return json.loads(row['payload_json'])

    def renew(self,store,cfg,first,model):
        packet=research.make_snapshot(store,cfg,SYMBOL,at='2026-09-15T09:03:00+08:00')
        reuse=research.reusable(store,cfg,packet)
        self.assertIsNotNone(reuse,'fixture must reach the genuine reuse eligibility path')
        studies=store.db.execute('SELECT count(*) FROM studies').fetchone()[0]
        with patch('ashare.research.run_json',side_effect=AssertionError('renew called model')):
            second=research.renew(store,cfg,packet,reuse,at='2026-09-15T09:04:00+08:00')
        self.assertEqual(second['status'],'REUSED')
        self.assertEqual(second['study_id'],first['study_id'])
        self.assertNotEqual(second['plan_id'],first['plan_id'])
        self.assertEqual(store.db.execute('SELECT count(*) FROM studies').fetchone()[0],studies)
        self.assertEqual(store.db.execute('SELECT status FROM plans WHERE id=?',(first['plan_id'],)).fetchone()[0],'SUPERSEDED')
        self.assertEqual(model.call_count,1)
        plan=self.plan(store,second['plan_id'])
        self.assertEqual(plan['research_reuse']['previous_plan_id'],first['plan_id'])
        return plan

    def assert_trend(self,plan,required,trend_ok=False):
        self.assertIs(plan['trend_ok'],trend_ok)
        self.assertEqual('TREND_NOT_CONFIRMED' in plan['blockers'],required,plan['blockers'])
        if required:self.assertEqual(plan['kind'],'NO_ENTRY')
        else:self.assertEqual(plan['kind'],'PAPER_TRADE',plan['blockers'])

    def test_d14_study_keeps_failed_trend_in_days_cash_and_legacy(self):
        for policy in ('days_cash_v1',None):
            with self.subTest(policy=policy),self.fixture(policy=policy) as (s,cfg,first,model):
                self.assertTrue(market_regime.regime_active(s,cfg))
                self.assert_trend(self.plan(s,first['plan_id']),True)

    def test_d14_real_renew_recomputes_failed_trend_in_days_cash_and_legacy(self):
        for policy in ('days_cash_v1',None):
            with self.subTest(policy=policy),self.fixture(policy=policy) as (s,cfg,first,model):
                self.assert_trend(self.renew(s,cfg,first,model),True)

    def test_other_modes_and_pairing_reach_both_research_paths(self):
        cases=[('R1',['exposure_caps_v1'],False),('R1',[],True),
               ('R1',['exposure_caps_v2'],True),('D14',[],True),
               ('D14',['exposure_caps_v1'],True),('OFF',['exposure_caps_v1','exposure_caps_v2'],True),
               (None,[],True)]
        for mode,features,required in cases:
            with self.subTest(mode=mode,features=features),self.fixture(mode,features) as (s,cfg,first,model):
                self.assert_trend(self.plan(s,first['plan_id']),required)
                self.assert_trend(self.renew(s,cfg,first,model),required)

    def test_d14_trend_boundaries_reach_both_research_paths(self):
        for levels,required in (((1000,1000,1100),True),((1100,1000,999),True),
                                ((1100,1000,1000),False),((1100,1000,1100),False)):
            with self.subTest(levels=levels),self.fixture(levels=levels) as (s,cfg,first,model):
                self.assert_trend(self.plan(s,first['plan_id']),required,not required)
                self.assert_trend(self.renew(s,cfg,first,model),required,not required)

    def test_cloud_waiver_uses_received_mode_not_local_config(self):
        with tempfile.TemporaryDirectory(prefix='d14-cloud-predicate-') as folder:
            store=Store(folder)
            try:
                cfg={'deployment_role':'cloud','exposure_regime':'R1'}
                self.assertFalse(market_regime.trend_gate_replaced(store,cfg))
                for mode,replaced in (('OFF',False),('D14',False),('R1',True)):
                    for cap in (10,35,90):
                        with self.subTest(mode=mode,cap=cap),store.db:
                            put(store,'exposure_caps',{'mode':mode,'regime':{'status':'OK','cap_pct':cap}})
                            self.assertIs(market_regime.trend_gate_replaced(store,cfg),replaced)
                            self.assertIs(market_regime.regime_active(store,cfg),mode!='OFF')
            finally:store.close()


if __name__=='__main__':
    unittest.main()
