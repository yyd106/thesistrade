import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ashare import evaluation, quick_diagnostics as qd, weekly, selfcheck, supervision
from ashare import evaluation_batches as batches
from ashare.cli import extended
from ashare.cloud_sync import handle
from ashare.cloud_protocol import canonical
from test_evaluation import Fixture, trading_days, SYMBOL

AT = '2026-10-30T10:00:00+00:00'


class QuickIntegrationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.cfg.update(evaluation_horizon_days=20, deployment_role='research')
        with self.store.db:
            evaluation._insert(self.store, 'quick-fixture', 'watchlist', SYMBOL, 'source',
                '2026-09-14T08:00:00+00:00', '2026-09-14', 'test-build', 20, evaluation.BENCHMARK,
                {'trend_ok': True, 'model_action': 'WATCH', 'private': 'RAW_PRIVATE_MARKER'},
                research={'thesis': 'RAW_PRIVATE_MARKER'})
        days = trading_days('2026-09-15', 23)
        self.features([(d, 10, 11, 12, 9) for d in days])
        self.benchmark(days, [4000] * len(days))

    def unchanged_tables(self):
        tables = ('signal_registry', 'judgment_contracts', 'strategy_proposals', 'strategy_guidance',
                  'experiment_designs', 'experiment_events', 'paper_orders', 'paper_flows', 'plans', 'studies')
        return {table: [tuple(row) for row in self.store.db.execute('SELECT * FROM ' + table)] for table in tables}

    def test_run_keeps_primary_and_legacy_records_byte_identical_and_is_idempotent(self):
        evaluation.score(self.store, self.cfg, AT)
        with self.store.db:
            self.store.db.execute('INSERT INTO signal_scores VALUES(?,?,?,?,?)',
                ('quick-fixture', 'legacy-fixture', 'SCORED', '{"untouched":true}', AT))
        original = [tuple(row) for row in self.store.db.execute('SELECT * FROM signal_scores')]
        before = self.unchanged_tables()
        outcomes = selfcheck.analyze(self.store, AT)
        first = qd.run(self.store, self.cfg, AT)
        again = qd.run(self.store, self.cfg, AT)
        self.assertEqual(first['new_scores']['scored'], 1)
        self.assertEqual(again['new_scores']['scored'], 0)
        self.assertEqual(original, [tuple(row) for row in self.store.db.execute(
            'SELECT * FROM signal_scores WHERE method!=?', (evaluation.QUICK_SCORE_METHOD,))])
        self.assertEqual(before, self.unchanged_tables())
        self.assertEqual(outcomes, selfcheck.analyze(self.store, AT))
        self.assertTrue((self.store.root / first['report']).is_file())
        self.assertNotIn('RAW_PRIVATE_MARKER', json.dumps(qd.view(self.store)))
        self.assertEqual(first['primary']['method'], evaluation.SCORE_METHOD)
        self.assertEqual(first['quick']['horizon_days'], 5)
        self.assertFalse(first['quick']['approval_eligible'])

    def test_daily_job_and_weekly_report_include_separate_diagnostic_without_model(self):
        with patch('ashare.model.run_json', side_effect=AssertionError('No model calls')):
            result = weekly.run_daily(self.store, self.cfg, AT)
        self.assertEqual(result['quick_diagnostics']['status'], 'READY')
        report = weekly.collect(self.store, self.cfg, AT)
        self.assertEqual(report['registry']['all_time']['method'], evaluation.SCORE_METHOD)
        self.assertEqual(report['quick_diagnostics']['quick']['method'], evaluation.QUICK_SCORE_METHOD)
        markdown = weekly.markdown(report)
        self.assertIn('5 日辅助诊断（不参与批准）', markdown)
        self.assertIn('结论注册表', markdown)
        self.assertIn('不进入自动候选', markdown)

    def test_frozen_supervision_facts_exclude_quick_results_and_old_input_stays_identical(self):
        original_batch = batches.start(self.store, self.cfg, at=AT)['id']
        original = supervision.batch_input(self.store, original_batch)
        qd.run(self.store, self.cfg, AT)
        self.assertEqual(original, supervision.batch_input(self.store, original_batch))
        later = batches.start(self.store, self.cfg, at='2026-10-31T10:00:00+00:00', force=True)['id']
        packet = supervision.batch_input(self.store, later)
        self.assertNotIn('quick_diagnostics', packet['facts'])
        self.assertNotIn(evaluation.QUICK_SCORE_METHOD, json.dumps(packet['facts']))
        report = json.loads((batches.folder(self.store, later) / 'report.json').read_text())
        self.assertEqual(report['quick_diagnostics']['quick']['method'], evaluation.QUICK_SCORE_METHOD)

    def test_signed_display_transfers_only_summary_without_renewing_lease(self):
        qd.run(self.store, self.cfg, AT)
        before = self.unchanged_tables()
        display = supervision.view(self.store)
        self.assertLess(len(canonical(display)), 1_000_000)
        with self.store.db:
            self.store.db.execute('INSERT INTO cloud_state VALUES(?,?)', ('research_completed_at', json.dumps(AT)))
        result = handle(self.store, {}, '/api/sync/reviews', {'supervision': display}, AT)
        self.assertEqual(result['status'], 'ACCEPTED')
        synced = json.loads(self.store.db.execute("SELECT value FROM cloud_state WHERE key='display_supervision'").fetchone()[0])
        self.assertEqual(synced['diagnostics'], qd.view(self.store))
        self.assertEqual(json.loads(self.store.db.execute("SELECT value FROM cloud_state WHERE key='research_completed_at'").fetchone()[0]), AT)
        self.assertEqual(before, self.unchanged_tables())

    def test_cli_run_and_show_are_separate_from_evaluate(self):
        result = extended(SimpleNamespace(command='diagnostics', action='run'), self.cfg)
        self.assertEqual(result['status'], 'READY')
        displayed = extended(SimpleNamespace(command='diagnostics', action='show'), self.cfg)
        self.assertEqual(displayed['quick']['method'], evaluation.QUICK_SCORE_METHOD)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM signal_scores WHERE method=?', (evaluation.SCORE_METHOD,)).fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, '本机'):
            qd.run(self.store, {**self.cfg, 'deployment_role': 'cloud'}, AT)

    def test_auxiliary_failure_does_not_block_original_evaluation(self):
        with patch.object(qd, 'run', side_effect=ValueError('RAW_PRIVATE_ERROR')):
            result = weekly.run_daily(self.store, self.cfg, AT)
        self.assertEqual(result['registry']['scored'], 1)
        self.assertIn('selfcheck', result)
        self.assertEqual(result['quick_diagnostics']['status'], 'ERROR')
        self.assertNotIn('RAW_PRIVATE_ERROR', json.dumps(qd.view(self.store)))

    def test_empty_corrupt_and_read_only_display(self):
        before = self.store.db.total_changes
        self.assertEqual(qd.view(self.store)['status'], 'NOT_RUN')
        self.assertEqual(self.store.db.total_changes, before)
        with self.store.db:
            self.store.db.execute('INSERT INTO service_state VALUES(?,?)', (qd.STATE_KEY, 'broken-json'))
        self.assertEqual(qd.view(self.store)['status'], 'ERROR')

    def test_summary_budget_allowlist_and_small_cluster_interval_suppression(self):
        noisy_stats = {'n': 100, 'time_clusters': 1, 'mean_bps': 9, 'ci95_bps': [1, 10], 'raw': 'SECRET'}
        groups = {key: {label: {'daily': noisy_stats, 'non_overlapping': noisy_stats}
                       for label in labels} for key, labels in qd.GROUPS.items()}
        value = {'groups': groups, 'by_build': {f'build-{i:04d}': groups for i in range(300)},
                 'counts': {'watchlist:SCORED': 100, 'raw': 'SECRET'}, 'horizon_days': 20, 'raw': 'SECRET'}
        result = qd.public({'version': qd.VERSION, 'status': 'READY', 'generated_at': AT, 'primary': value, 'quick': value})
        self.assertLess(len(canonical(result)), qd.MAX_BYTES)
        self.assertGreater(result['omitted_builds'], 0)
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertIsNone(result['quick']['groups']['trend_filter']['trend_ok']['non_overlapping']['ci95_bps'])
        self.assertEqual(qd.public(result), result)


if __name__ == '__main__':
    unittest.main()
