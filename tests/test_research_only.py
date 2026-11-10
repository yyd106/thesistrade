"""Synthetic no-order, publication, recovery and research-only regressions."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ashare import (build, cloud_ledger, cloud_runtime, config_journal, config_ops,
                    dynamic_paper, evaluation, paper, portfolio_strategy, research,
                    research_only, universe)
from ashare.finance import PaperLedger
from ashare.pipeline import load_config
from ashare.settings import validate_settings
from ashare.storage import Store
from approval_fixture import grant
import test_execution_safety as execution_fixture
import test_cloud_sync as cloud_fixture
import test_evaluation as evaluation_fixture

SYMBOL = execution_fixture.SYMBOL

ROOT = Path(__file__).resolve().parents[1]


class ResearchOnlyExecutionTests(execution_fixture.ExecutionSafetyTests):
    def lock(self):
        self.store.db.execute('BEGIN IMMEDIATE')
        try:
            research_only.activate_inside(self.store,{SYMBOL},'synthetic-approved-change')
            self.store.db.commit()
        except BaseException:
            self.store.db.rollback();raise

    def test_old_plan_authorization_and_old_config_cannot_create_any_order(self):
        plan=self.watchlist_plan();case=self.case();q=self.quote();dq=self.quote(route='dynamic')
        self.lock()  # Deliberately keep cfg without the new field (stale process).
        for side in ('BUY','SELL'):
            self.assertEqual(paper.submit(self.store,self.cfg,'late-'+side,plan,side,q,self.at)['reason'],'RESEARCH_ONLY')
            self.assertEqual(dynamic_paper.submit(self.store,self.cfg,case,side,dq,self.at,'synthetic')['reason'],'RESEARCH_ONLY')
        for table in ('paper_orders','dynamic_orders','global_orders'):
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
        self.assertIsNone(portfolio_strategy.decision(self.store,self.cfg,'dynamic',case['id'],self.at))
        self.assertFalse(universe.permit(self.store,self.cfg,SYMBOL,self.at))
        self.assertNotIn(SYMBOL,{x['symbol'] for x in portfolio_strategy.snapshot(self.store,self.cfg,self.at)['candidates']})

    def test_standing_fixed_order_is_cancelled_before_quote_fill(self):
        _,order=self.watchlist_buy()
        with self.assertRaisesRegex(ValueError,'OPEN_ORDER'):self.lock()
        self.assertEqual(research_only.locks(self.store),{})
        # Fault injection: an out-of-band flag must still never fill an old order.
        self.cfg['watchlist'][0]['research_only']=True
        self.quote(60)
        self.assertEqual(paper.settle(self.store,self.cfg,self.later(60)),[])
        self.assert_cancelled('paper_orders',order)

    def test_dynamic_old_order_and_unknown_status_block_activation(self):
        _,order=self.dynamic_buy()
        for status in ('UNKNOWN','UNRECOGNIZED_STATUS'):
            with self.store.db:self.store.db.execute('UPDATE dynamic_orders SET status=?',(status,))
            with self.assertRaisesRegex(ValueError,'OPEN_ORDER'):self.lock()
        with self.store.db:self.store.db.execute("UPDATE dynamic_orders SET status='OPEN'")
        self.cfg['watchlist'][0]['research_only']=True
        self.quote(60,route='dynamic')
        self.assertEqual(dynamic_paper.settle(self.store,self.cfg,self.later(60)),[])
        self.assert_cancelled('dynamic_orders',order)

    def test_holding_refuses_enable_and_original_hard_exit_remains_available(self):
        _,order=self.watchlist_buy();self.quote(60)
        self.assertEqual(len(paper.settle(self.store,self.cfg,self.later(60))),1)
        with self.assertRaisesRegex(ValueError,'EXPOSURE'):self.lock()
        self.assertFalse(research_only.contains(self.store,self.cfg,SYMBOL))
        # Existing full hard-exit/T+1 regression inherited below remains unchanged.
        self.assertGreater(paper.positions(self.store,self.later(60))[SYMBOL]['qty'],0)

    def test_atomic_lock_beats_other_connection_stale_config(self):
        other=Store(self.tmp.name)
        try:
            plan=self.watchlist_plan();q=self.quote();self.lock()
            self.assertEqual(paper.submit(other,self.cfg,'old-worker',plan,'BUY',q,self.at)['reason'],'RESEARCH_ONLY')
        finally:other.close()

    def test_config_lock_serializes_with_waiting_old_worker(self):
        plan=self.watchlist_plan();q=self.quote();ready=threading.Event();proceed=threading.Event();done=threading.Event();answers=[]
        def worker():
            other=Store(self.tmp.name);ready.set();proceed.wait(5)
            try:answers.append(paper.submit(other,self.cfg,'racing-worker',plan,'BUY',q,self.at))
            finally:other.close();done.set()
        thread=threading.Thread(target=worker);thread.start()
        self.assertTrue(ready.wait(5))
        self.store.db.execute('BEGIN IMMEDIATE')
        research_only.activate_inside(self.store,{SYMBOL},'synthetic-approved-concurrent')
        proceed.set()
        self.store.db.commit()
        self.assertTrue(done.wait(5));thread.join()
        self.assertEqual(answers[0]['reason'],'RESEARCH_ONLY')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],0)


class ResearchOnlyResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.addCleanup(self.store.close)
        self.cfg=load_config(ROOT/'config.example.json',configure_model=False)
        self.cfg.update(data_dir=self.tmp.name,watchlist=[{'symbol':SYMBOL,'name':'合成公司','research_only':True}],
                        deployment_role='standalone',model_enabled=True)
        from ashare.demo import seed
        self.packet=seed(self.store,self.cfg)

    def test_two_requested_symbols_both_membership_modes_and_field_types(self):
        for symbol in ('sh600519','sh603444'):
            for industry in (False,True):
                cfg={**self.cfg,'industry_enabled':industry,'watchlist':[{'symbol':symbol,'name':'合成只研究','research_only':True}]}
                m=universe.company_targets(self.store,cfg,self.packet['as_of'])[0]
                self.assertTrue(m['research_only']);self.assertFalse(m['buy_eligible'])
                self.assertFalse(universe.permit(self.store,cfg,symbol,self.packet['as_of']))
                packet={**self.packet,'symbol':symbol,'research_membership':m}
                packet['sizing']={**packet['sizing'],'equity_cents':1}
                p=research.price_plan(packet,cfg,{'action':'WATCH'},'SUCCEEDED')
                self.assertEqual(p['kind'],'RESEARCH_ONLY');self.assertTrue(p['levels'])
                self.assertEqual(p['blockers'],[])
                self.assertNotIn('LOT_EXCEEDS_CAP',p['research_gaps'])
                self.assertNotIn('RESEARCH_MEMBERSHIP_REVIEW',p['research_gaps'])
        for invalid in ('true',1,None,[],{}):
            cfg=copy.deepcopy(self.cfg);cfg['watchlist'][0]['research_only']=invalid
            with self.assertRaisesRegex(ValueError,'research_only'):validate_settings(cfg)
        plain=copy.deepcopy(self.cfg);plain['watchlist'][0].pop('research_only')
        self.assertNotEqual(build.config_fingerprint(plain),build.config_fingerprint(self.cfg))

    def test_research_renewal_keeps_reference_levels_and_registry(self):
        from ashare.demo import research_model
        first=research.study(self.store,self.cfg,self.packet,model_fn=research_model(self.packet),at='2026-09-15T09:01:00+08:00')
        self.assertEqual(first['plan_kind'],'RESEARCH_ONLY')
        prior=dict(self.store.db.execute('SELECT * FROM studies WHERE id=?',(first['study_id'],)).fetchone())
        old=dict(self.store.db.execute('SELECT * FROM plans WHERE id=?',(first['plan_id'],)).fetchone())
        packet=research.make_snapshot(self.store,self.cfg,SYMBOL,at='2026-09-15T09:02:00+08:00')
        result=research.renew(self.store,self.cfg,packet,{'study':prior,'plan':old},at='2026-09-15T09:03:00+08:00')
        self.assertEqual(result['plan_kind'],'RESEARCH_ONLY')
        rows=[json.loads(r[0]) for r in self.store.db.execute('SELECT judgment_json FROM signal_registry')]
        self.assertEqual(len(rows),2)
        self.assertTrue(all(p['research_only'] and p['levels'] for p in rows))
        g=evaluation.trading_gate_counts(self.store,'2026-09-14','2026-09-16')
        self.assertEqual((g['plans_total'],g['research_only_excluded'],g['trading_plan_count']),(2,2,0))
        self.assertEqual(g['blocker_counts'],{})
        self.assertEqual(evaluation.registry_counts(self.store)['watchlist:OPEN'],2)

    def test_missing_prices_and_data_change_remain_research_gaps(self):
        p=copy.deepcopy(self.packet);p['stocks'][0]['features']['unadjusted']={}
        plan=research.price_plan(p,self.cfg,{'action':'WATCH'},'SUCCEEDED')
        self.assertIsNone(plan['levels']);self.assertIn('UNADJUSTED_SERIES_MISSING',plan['research_gaps'])
        plan['kind']='NO_ENTRY';plan['blockers']=['SOURCE_CHANGED_DURING_RESEARCH']
        research_only.enforce_plan(self.store,self.cfg,SYMBOL,plan)
        self.assertEqual(plan['kind'],'RESEARCH_ONLY');self.assertEqual(plan['blockers'],[])
        self.assertIn('SOURCE_CHANGED_DURING_RESEARCH',plan['research_gaps'])

    def test_old_historical_gate_sample_is_not_reclassified(self):
        p=research.price_plan(self.packet,self.cfg,{'action':'WATCH'},'SUCCEEDED')
        p.update(kind='NO_ENTRY',research_only=False,blockers=['LOT_EXCEEDS_CAP'])
        with self.store.db:
            self.store.db.execute('INSERT INTO studies VALUES(?,?,?,?,?,?)',('old-study',self.packet['snapshot_id'],SYMBOL,self.packet['as_of'],'SUCCEEDED','{}'))
            self.store.db.execute('INSERT INTO plans VALUES(?,?,?,?,?,?,?,?)',('old-plan','old-study',SYMBOL,self.packet['as_of'],'2026-09-16','SUPERSEDED','synthetic',json.dumps(p)))
        g=evaluation.trading_gate_counts(self.store,'2026-09-14','2026-09-16')
        self.assertEqual(g['trading_plan_count'],1);self.assertEqual(g['blocker_counts'],{'LOT_EXCEEDS_CAP':1})

    def test_signed_membership_cannot_drop_change_or_relabel_flag(self):
        at=self.packet['as_of'];until='2026-09-15T12:00:00+00:00'
        for industry in (False,True):
            cfg={**self.cfg,'industry_enabled':industry}
            body=universe.publication(self.store,cfg,at,until)
            universe.validate_publication(self.store,cfg,body,[],at,until)
            mutations=[]
            b=copy.deepcopy(body);b['members'][0].pop('research_only');mutations.append(b)
            b=copy.deepcopy(body);b['members'][0]['buy_eligible']=True;mutations.append(b)
            b=copy.deepcopy(body);b['research_only']=[];mutations.append(b)
            b=copy.deepcopy(body);b['version']='industry_v1';mutations.append(b)
            for bad in mutations:
                with self.assertRaises(ValueError):universe.validate_publication(self.store,cfg,bad,[],at,until)

    def test_effective_behavior_identity_keeps_durable_lock_after_flag_removed(self):
        plain=copy.deepcopy(self.cfg);plain['watchlist'][0].pop('research_only')
        before=build.behavior_info(plain,self.store)
        self.store.db.execute('BEGIN IMMEDIATE')
        research_only.activate_inside(self.store,{SYMBOL},'synthetic-approved')
        self.store.db.commit()
        after=build.behavior_info(plain,self.store)
        self.assertNotEqual(before['config_hash'],after['config_hash'])
        self.assertFalse(build.behavior_matches(before,plain,self.store))


class ResearchOnlyConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'config.json';self.data=Path(self.tmp.name)/'data'
        raw=json.loads((ROOT/'config.example.json').read_text())
        raw.update(data_dir=str(self.data),deployment_role='standalone',watchlist=[{'symbol':SYMBOL,'name':'合成公司'}])
        self.path.write_text(json.dumps(raw))
        Store(self.data).close()

    def initialize(self):
        store=Store(self.data)
        try:PaperLedger(store).initialize()
        finally:store.close()

    def approve(self):
        changes={'watchlist':[{'symbol':SYMBOL,'name':'合成公司','research_only':True}]}
        request=config_ops.request_change(self.path,changes,reason='合成只研究测试')
        store=Store(self.data)
        try:grant(store,request)
        finally:store.close()
        return request['id'],changes

    def apply(self,identity,changes):
        return config_ops.apply(self.path,changes,reason='合成只研究测试',approval_id=identity)

    def test_unknown_account_refuses_enable_without_file_change(self):
        identity,changes=self.approve();before=self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'ACCOUNT_UNKNOWN'):self.apply(identity,changes)
        self.assertEqual(self.path.read_bytes(),before)

    def test_failed_publish_and_recovery_keep_lock_and_old_config_denied(self):
        self.initialize();identity,changes=self.approve();before=self.path.read_bytes()
        with patch.object(config_journal,'_publish',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.apply(identity,changes)
        self.assertEqual(self.path.read_bytes(),before)
        store=Store(self.data)
        try:self.assertIn(SYMBOL,research_only.symbols(store,{'watchlist':[]}))
        finally:store.close()
        config_ops.recover(self.path)
        self.assertTrue(json.loads(self.path.read_text())['watchlist'][0]['research_only'])
        store=Store(self.data)
        try:
            self.assertIn(SYMBOL,research_only.symbols(store,{'watchlist':[{'symbol':SYMBOL,'research_only':False}]}))
        finally:store.close()

    def test_handled_write_failure_never_releases_no_order_lock(self):
        self.initialize();identity,changes=self.approve()
        with patch.object(config_journal,'_publish',side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):self.apply(identity,changes)
        store=Store(self.data)
        try:
            self.assertIn(SYMBOL,research_only.locks(store))
            self.assertFalse(universe.permit(store,load_config(self.path,configure_model=False),SYMBOL,'2026-09-15T09:00:00+00:00'))
        finally:store.close()


class ResearchOnlyCloudTests(unittest.TestCase):
    quote=cloud_fixture.CloudSyncTests.quote
    plan=cloud_fixture.CloudSyncTests.plan
    later=cloud_fixture.CloudSyncTests.later
    output=cloud_fixture.CloudSyncTests.output
    transact=cloud_fixture.CloudSyncTests.transact
    publication=cloud_fixture.CloudSyncTests.publication
    receive=cloud_fixture.CloudSyncTests.receive
    tearDown=cloud_fixture.CloudSyncTests.tearDown

    def setUp(self):
        # Existing fixture requests an operator config; substitute the versioned
        # example in memory, never create/read a production configuration file.
        original=Path.read_text;example=original(ROOT/'config.example.json')
        def read(path,*a,**k):
            return example if path==ROOT/'config.json' else original(path,*a,**k)
        with patch.object(Path,'read_text',read):cloud_fixture.CloudSyncTests.setUp(self)
        for store,cfg in ((self.store,self.cfg),(self.cloud,self.cloud_cfg)):
            cfg['watchlist']=[{'symbol':'sh600519','name':'测试股','research_only':True}]
            store.db.execute('BEGIN IMMEDIATE')
            research_only.activate_inside(store,{'sh600519'},'synthetic-approved')
            store.db.commit()
        with self.store.db:cloud_runtime.put(self.store,'remote_features',list(cloud_ledger.FEATURES))

    def test_cloud_accepts_certificate_but_never_receives_research_only_candidate(self):
        for cfg in (self.cfg,self.cloud_cfg):cfg['industry_enabled']=False
        body=self.publication()
        member=next(m for m in body['research_membership']['members'] if m['symbol']=='sh600519')
        self.assertTrue(member['research_only']);self.assertFalse(member['buy_eligible'])
        self.assertNotIn('sh600519',{d['symbol'] for d in json.loads(body['decision']['payload_json'])['decisions']})
        self.assertEqual(self.receive(body)['status'],'ACCEPTED')

    def test_missing_changed_and_old_version_certificate_rejected_atomically(self):
        body=self.publication()
        bad=[]
        b=copy.deepcopy(body);b.pop('research_membership');bad.append(b)
        b=copy.deepcopy(body);b['research_membership']['members'][0]['research_only']=False;bad.append(b)
        b=copy.deepcopy(body);b['research_membership']['version']='industry_v1';bad.append(b)
        for value in bad:
            with self.assertRaises(ValueError):self.receive(value)
            self.assertIsNone(cloud_runtime.value(self.cloud,'research_completed_at'))
            self.assertEqual(self.cloud.db.execute('SELECT count(*) FROM cloud_receipts').fetchone()[0],0)

    def test_old_cloud_feature_does_not_allow_publishing_ro_bundle(self):
        with self.store.db:cloud_runtime.put(self.store,'remote_features',['industry_lists_v1'])
        with self.assertRaisesRegex(ValueError,'云端尚不支持'):self.publication()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM cloud_outbox').fetchone()[0],0)

    def test_ordinary_ledger_import_keeps_local_durable_lock(self):
        packet=self.transact(lambda:cloud_ledger.export_ledger(self.cloud,{},self.at))
        cloud_ledger.import_ledger(self.store,self.cfg,packet)
        old={**self.cfg,'watchlist':[{'symbol':'sh600519','name':'测试股'}]}
        self.assertTrue(research_only.contains(self.store,old,'sh600519'))
        self.assertFalse(universe.permit(self.store,old,'sh600519',self.at))

    def test_research_activation_requires_fresh_cloud_lock_and_complete_matching_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            fresh=Store(directory)
            try:
                cfg={**self.cfg,'data_dir':directory}
                PaperLedger(fresh).initialize()
                def activate():
                    fresh.db.execute('BEGIN IMMEDIATE')
                    try:
                        research_only.activate_inside(fresh,{'sh600519'},'synthetic-apply',config=cfg)
                        fresh.db.commit()
                    except BaseException:fresh.db.rollback();raise
                with self.assertRaisesRegex(ValueError,'CLOUD_PREFLIGHT'):activate()
                packet=self.transact(lambda:cloud_ledger.export_ledger(self.cloud,{},self.at))
                cloud_ledger.import_ledger(fresh,cfg,packet)
                with patch.object(research_only,'now',return_value=self.later(121)):
                    with self.assertRaisesRegex(ValueError,'CLOUD_PREFLIGHT'):activate()
                with patch.object(research_only,'now',return_value=self.at):activate()
                self.assertIn('sh600519',research_only.locks(fresh))
                # Once durably activated, config recovery cannot deadlock on an
                # expired remote preflight or erase its already-authorized lock.
                with patch.object(research_only,'now',return_value=self.later(999)):activate()
            finally:fresh.close()


class ResearchOnlyShadowTests(unittest.TestCase):
    features=evaluation_fixture.Fixture.features
    tearDown=evaluation_fixture.Fixture.tearDown

    def setUp(self):
        original=Path.read_text;example=original(ROOT/'config.example.json')
        def read(path,*a,**k):return example if path==ROOT/'config.json' else original(path,*a,**k)
        # The fixture's setup uses super(), so run its two concrete setup parts
        # on a fixture instance and retain its isolated data for this assertion.
        self.fixture=evaluation_fixture.ShadowBookTests()
        with patch.object(Path,'read_text',read):self.fixture.setUp()
        self.__dict__.update(self.fixture.__dict__)

    def test_counterfactual_a_retained_bcd_no_entry_and_no_order_entrypoint_called(self):
        from ashare import shadow
        from ashare.demo import research_model
        from ashare.storage import normalize_time
        self.cfg['watchlist'][0]['research_only']=True
        result=research.study(self.store,self.cfg,self.packet,model_fn=research_model(self.packet),at='2026-09-15T09:01:00+08:00')
        with self.store.db:
            self.store.db.execute('UPDATE plans SET activated_at=?,valid_until=? WHERE id=?',
                (normalize_time(self.entry_day+'T08:00:00+08:00'),normalize_time(self.exit_day+'T20:00:00+08:00'),result['plan_id']))
            self.store.db.execute('UPDATE plan_events SET at=? WHERE plan_id=?',
                (normalize_time(self.entry_day+'T08:00:00+08:00'),result['plan_id']))
        with patch('ashare.paper.submit',side_effect=AssertionError('shadow must not submit orders')), \
             patch('ashare.dynamic_paper.submit',side_effect=AssertionError('shadow must not submit orders')), \
             patch('ashare.global_paper.submit',side_effect=AssertionError('shadow must not submit orders')):
            shadow.run(self.store,self.cfg,normalize_time(self.exit_day+'T20:00:00+08:00'))
        buys={r[0] for r in self.store.db.execute("SELECT book FROM shadow_trades_v2 WHERE side='BUY'")}
        self.assertEqual(buys,{'A-lot','A-frac'})
        for table in ('paper_orders','dynamic_orders','global_orders'):
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
