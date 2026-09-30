import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ashare.storage import Store, normalize_time
from ashare import evaluation, judgments, selfcheck, experiments, governance, weekly
from ashare.review import route_lessons


class SelfcheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(self.tmp.name)
        self.cfg = {'data_dir': self.tmp.name, 'deployment_role': 'research'}
        self.at = '2026-09-30T12:00:00+00:00'

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def signal(self, identity='g1', route='global', build='b1', stamp='2026-09-15T12:00:00+00:00',
               stance='LONG', scored=True, historical=False, symbol='GOLD', entry='2026-09-16', exit='2026-09-18', value=-100):
        judgment = {'stance': stance, 'plan_kind': stance, 'action': stance}
        with self.s.db:
            evaluation._insert(self.s, identity, route, symbol, identity, stamp, stamp[:10], build, 3, None,
                               judgment, research={'thesis': '合成判断', 'next_checks': ['核对后续公告']}, historical=historical)
            if scored:
                self.s.db.execute('INSERT INTO signal_scores VALUES(?,?,?,?,?)', (identity, evaluation.SCORE_METHOD, 'SCORED',
                    judgments.encode({'method': evaluation.SCORE_METHOD, 'entry_date': entry, 'exit_date': exit,
                                      'return_bps': value, 'excess_bps': value}), self.at))
        return identity

    def test_environment_uses_only_available_completed_bars_and_never_relabels(self):
        bars = [{'date': f'2026-08-{i:02d}', 'price_micros': 100 + i} for i in range(1, 29)]
        bars += [{'date': '2026-09-15', 'price_micros': 99999}, {'date': '2026-09-16', 'price_micros': 1}]
        # Use a fresh 21-day sequence ending Sep 14, known before the decision.
        bars = [{'date': (datetime(2026, 8, 25) + timedelta(days=i)).date().isoformat(), 'price_micros': 100 + i} for i in range(21)] + bars[-2:]
        with self.s.db:
            self.s.db.execute('INSERT INTO global_market VALUES(?,?,?)', ('GOLD', '2026-09-15T10:00:00+00:00', judgments.encode({'bars': bars})))
        self.signal(scored=False)
        p = json.loads(self.s.db.execute('SELECT payload_json FROM judgment_contracts').fetchone()[0])
        self.assertEqual(p['market']['last_day'], '2026-09-14')
        self.assertEqual(p['market']['label'], 'UP:NORMAL_VOL')
        self.assertEqual(p['business_test']['status'], 'UNSTRUCTURED')
        with self.s.db:
            self.s.db.execute("UPDATE global_market SET checked_at='2026-09-16T12:00:00+00:00',payload_json='{}'")
        self.signal(identity='g2', scored=False)
        self.assertEqual(json.loads(self.s.db.execute("SELECT payload_json FROM judgment_contracts WHERE id='g2'").fetchone()[0])['market']['status'], 'MISSING')
        self.assertEqual(json.loads(self.s.db.execute("SELECT payload_json FROM judgment_contracts WHERE id='g1'").fetchone()[0]), p)

    def test_immutable_contract_and_backfill_do_not_invent_forward_evidence(self):
        self.signal(historical=True)
        selfcheck.run(self.s, self.cfg, self.at, generate=True)
        c = self.s.db.execute('SELECT * FROM judgment_contracts').fetchone()
        self.assertEqual(c['provenance'], 'LEGACY')
        self.assertEqual(json.loads(c['payload_json'])['market']['label'], 'UNKNOWN')
        self.assertFalse(self.s.db.execute('SELECT 1 FROM strategy_proposals').fetchone())
        with self.assertRaises(sqlite3.IntegrityError), self.s.db:
            self.s.db.execute("UPDATE judgment_contracts SET build_id='fake'")
        self.assertEqual(judgments.backfill(self.s, self.cfg, self.at), 0)

    def test_future_outcome_is_not_visible_at_earlier_check(self):
        self.signal()
        before = selfcheck.outcomes(self.s, '2026-09-29T12:00:00+00:00')
        self.assertEqual(before[0]['status'], 'AWAITING_SCORE')
        r = selfcheck.run(self.s, self.cfg, '2026-09-29T12:00:00+00:00', generate=True)
        self.assertEqual(r['candidates']['updates'], [])

    def test_complete_draft_design_and_idempotent_evidence(self):
        self.signal()
        first = selfcheck.run(self.s, self.cfg, self.at, generate=True)
        self.assertEqual(len(first['candidates']['updates']), 1)
        p = governance.proposals(self.s)[0]
        self.assertEqual(p['status'], 'DRAFT')
        self.assertTrue({'hypothesis', 'change', 'evidence', 'test_plan', 'failure_criteria', 'rollback', 'metrics', 'observation_period'} <= set(p['payload']))
        exp = experiments.view(self.s)[0]
        self.assertEqual(exp['status'], 'DESIGNED')
        self.assertGreater(exp['spec']['enrollment']['start_after'], self.at)
        repeat = selfcheck.run(self.s, self.cfg, self.at, generate=True)
        self.assertEqual(first['id'], repeat['id'])
        self.assertFalse(repeat['candidates']['updates'])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM selfcheck_evidence').fetchone()[0], 1)
        self.assertFalse(self.s.db.execute('SELECT 1 FROM strategy_guidance').fetchone())
        self.assertFalse(self.s.db.execute('SELECT 1 FROM global_orders').fetchone())

    def test_new_pending_judgment_does_not_duplicate_proposal_or_evidence(self):
        self.signal()
        selfcheck.run(self.s, self.cfg, self.at, generate=True)
        self.signal('pending', scored=False, symbol='SILVER')
        later = selfcheck.run(self.s, self.cfg, self.at, generate=True)
        self.assertFalse(later['candidates']['updates'])
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM strategy_proposals').fetchone()[0], 1)

    def test_rejected_candidate_remains_rejected_when_new_evidence_arrives(self):
        self.signal()
        selfcheck.run(self.s, self.cfg, self.at, generate=True)
        p = governance.proposals(self.s)[0]
        governance.decide(self.s, p['id'], 'REJECTED', decided_by=None, note='合成实验否决', at=self.at)
        frozen = self.s.db.execute('SELECT payload_json FROM strategy_proposals').fetchone()[0]
        self.signal('g2', symbol='SILVER')
        result = selfcheck.run(self.s, self.cfg, self.at, generate=True)
        self.assertEqual(result['candidates']['updates'][0]['status'], 'REJECTED')
        self.assertEqual(self.s.db.execute('SELECT payload_json FROM strategy_proposals').fetchone()[0], frozen)
        self.assertEqual(len(experiments.view(self.s)), 1)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM selfcheck_evidence').fetchone()[0], 2)

    def test_weekly_budget_survives_restart(self):
        for i in range(5):self.signal('g' + str(i), build='b' + str(i), symbol='ASSET' + str(i))
        first = selfcheck.run(self.s, self.cfg, self.at, generate=True)
        self.assertEqual(first['candidates']['used'], 3)
        self.s.close();self.s = Store(self.tmp.name)
        self.assertFalse(selfcheck.run(self.s, self.cfg, self.at, generate=True)['candidates']['updates'])
        later = selfcheck.run(self.s, self.cfg, '2026-10-06T12:00:00+00:00', generate=True)
        self.assertEqual(later['candidates']['used'], 2)

    def test_versions_actions_and_time_clusters_are_separate(self):
        self.signal('g1', build='b1', symbol='GOLD')
        self.signal('g2', build='b2', symbol='SILVER', stance='WAIT')
        result = selfcheck.run(self.s, self.cfg, self.at)
        r = selfcheck.status(self.s, result['id'])
        self.assertEqual({c['build_id'] for c in r['cohorts']}, {'b1', 'b2'})
        self.assertEqual({c['stance'] for c in r['cohorts']}, {'LONG', 'WAIT'})
        self.assertTrue(all(c['excess']['ci95_bps'] is None for c in r['cohorts']))
        self.assertTrue(all(c['excess']['time_clusters'] == 1 for c in r['cohorts']))
        self.assertIn('观望不解释为预测下跌', r['layers']['price_prediction'])

    def test_last_judgment_sampling_precedes_grouping(self):
        self.signal('old', build='old', value=1000)
        self.signal('new', build='new', value=-100, stamp='2026-09-15T13:00:00+00:00')
        report, _ = selfcheck.analyze(self.s, self.at)
        self.assertEqual(len(report['cohorts']), 1)
        self.assertEqual(report['cohorts'][0]['build_id'], 'new')

    def test_report_files_recover_without_recomputing_the_frozen_report(self):
        self.signal()
        r = selfcheck.run(self.s, self.cfg, self.at)
        path = self.s.root / r['report']
        original = path.read_text()
        path.unlink()  # Disposable synthetic fixture only.
        repeat = selfcheck.run(self.s, self.cfg, self.at)
        self.assertEqual(repeat['id'], r['id'])
        self.assertEqual(path.read_text(), original)

    def test_empty_and_cloud_are_explicit(self):
        r = selfcheck.run(self.s, self.cfg, self.at, generate=True)
        self.assertEqual(r['counts'], {})
        self.assertFalse(r['candidates']['updates'])
        n = self.s.db.total_changes
        cloud = selfcheck.run(self.s, {'deployment_role': 'cloud'}, self.at, generate=True)
        self.assertEqual(cloud['status'], 'NOT_APPLICABLE')
        self.assertEqual(self.s.db.total_changes, n)

    def test_experiment_changes_and_execution_states_are_rejected(self):
        self.signal()
        selfcheck.run(self.s, self.cfg, self.at, generate=True)
        exp = experiments.view(self.s)[0]
        altered = copy.deepcopy(exp['spec']);altered['primary_metric'] = 'changed'
        with self.assertRaises(ValueError), self.s.db:
            experiments.register(self.s, exp['proposal_id'], altered, self.at)
        for state in ('RUNNING', 'PASSED', 'ADOPTED'):
            with self.assertRaises(ValueError):experiments.close(self.s, exp['id'], state, '不可跳过实验', self.at)
        experiments.close(self.s, exp['id'], 'REJECTED', '保留失败设计', self.at)
        self.assertEqual(experiments.view(self.s, exp['id'])['status'], 'REJECTED')
        with self.assertRaises(ValueError):experiments.close(self.s, exp['id'], 'CANCELLED', '重复', self.at)
        self.assertEqual(len(experiments.view(self.s, exp['id'])['history']), 2)

    def test_invalid_experiment_bounds(self):
        self.signal();selfcheck.run(self.s, self.cfg, self.at, generate=True)
        spec = experiments.view(self.s)[0]['spec']
        for section, key, value in [('enrollment', 'start_after', '2026-09-01T00:00:00+00:00'),
                                     ('enrollment', 'embargo_days', 0), ('enrollment', 'window_days', True),
                                     ('budget', 'arms', 3)]:
            changed = copy.deepcopy(spec);changed[section][key] = value
            with self.assertRaises(ValueError):experiments.validate(changed, self.at)

    def test_cross_day_review_dedup_does_not_reopen_closed_proposal(self):
        lesson = {'symbol': 'GOLD', 'category': 'RESEARCH', 'lesson': '须核对反向证据。',
                  'applicability': '高波动', 'decision_ids': [], 'fill_ids': []}
        with self.s.db:a = route_lessons(self.s, 'r1', [lesson], self.at, 'b1')
        governance.decide(self.s, a[0]['id'], 'REJECTED', decided_by=None, note='重复观察', at=self.at)
        with self.s.db:
            b = route_lessons(self.s, 'r2', [{**lesson, 'lesson': '须核对反向证据！'}], self.at, 'b1')
            route_lessons(self.s, 'r2', [lesson], self.at, 'b1')
        self.assertEqual(a[0]['id'], b[0]['id'])
        p = governance.proposals(self.s)[0]
        self.assertEqual(p['status'], 'REJECTED')
        self.assertEqual(len(p['review_observations']), 2)
        with self.s.db:c = route_lessons(self.s, 'r3', [lesson], self.at, 'b2')
        self.assertNotEqual(a[0]['id'], c[0]['id'])

    def dynamic_case(self, identity, basis='FORWARD'):
        with self.s.db:
            self.s.db.execute('INSERT INTO dynamic_news(id,source,url,published_at,first_seen_at,title,body,cluster_id,raw_path) VALUES(?,?,?,?,?,?,?,?,?)',
                (identity, '合成', 'https://example.test', '2026-09-15T00:00:00+00:00', '2026-09-15T00:00:00+00:00', '合成新闻', '测试', identity, 'fixture'))
            self.s.db.execute('INSERT INTO dynamic_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (identity, identity, 'sh600000', '合成', 'test', 'POLICY', 'BEARISH', '2026-09-15T01:00:00+00:00',
                 '2026-09-15T13:00:00+00:00', basis, 'RESEARCH', judgments.encode({'invalidation': '反证', 'evidence': [{'news_id': identity}]}),
                 judgments.encode({'holding_days': 3, 'buy_high_cents': 1000})))
            judgments.dynamic(self.s, self.cfg, identity)
            self.s.db.execute('INSERT INTO dynamic_observations VALUES(?,?,?,?,?,?)',
                (identity, '2026-09-16T01:30:00+00:00', '2026-09-21T01:30:00+00:00', self.at, basis,
                 judgments.encode({'status': 'MEASURED', 'market_return_bps': -100, 'net_return_bps': 50, 'excess_return_bps': 25, 'cost_bps': 50})))

    def test_dynamic_original_terms_and_retrospective_isolation(self):
        self.dynamic_case('d1')
        self.dynamic_case('d2', 'RETROSPECTIVE')
        with self.s.db:
            self.s.db.execute("UPDATE dynamic_cases SET plan_json='{}'")
        result = selfcheck.run(self.s, self.cfg, self.at)
        p = json.loads(self.s.db.execute("SELECT payload_json FROM judgment_contracts WHERE id='dynamic:d1'").fetchone()[0])
        self.assertEqual(p['judgment']['plan']['buy_high_cents'], 1000)
        c = selfcheck.status(self.s, result['id'])['cohorts']
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]['net_direction']['mean_bps'], 50)
        self.assertEqual(c[0]['evidence_ids'], ['dynamic:d1'])

    def test_daily_and_weekly_hooks_do_not_call_models(self):
        with patch('ashare.evaluation.score', return_value={}), patch('ashare.evaluation.registry_counts', return_value={}), \
             patch('ashare.shadow.run', return_value={}), patch('ashare.selfcheck.run', return_value={'status': 'SUCCEEDED'}) as hook:
            result = weekly.run_daily(self.s, self.cfg, self.at)
            self.assertEqual(result['selfcheck']['status'], 'SUCCEEDED')
            hook.assert_called_once_with(self.s, self.cfg, self.at)
        with patch('ashare.weekly.collect', return_value={}), patch('ashare.weekly.markdown', return_value='synthetic'), \
             patch('ashare.selfcheck.run', return_value={}) as hook:
            weekly.weekly_report(self.s, self.cfg, self.at)
            hook.assert_called_once_with(self.s, self.cfg, self.at, generate=True)


if __name__ == '__main__':
    unittest.main()
