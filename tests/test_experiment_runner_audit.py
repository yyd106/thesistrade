"""Independent synthetic regressions for interruption and invalid model output."""
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from ashare import experiment_runner as runner
from ashare import experiment_measurement as measurement
from ashare.judgments import encode
from ashare.settings import DEFAULTS
from ashare.storage import Store, digest
from tests import test_experiment_measurement as measurement_fixtures


AT = '2026-10-03T08:00:00+00:00'


class ExperimentRunnerAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(self.tmp.name)
        self.cfg = {**DEFAULTS, 'data_dir': self.tmp.name, 'deployment_role': 'standalone',
                    'model_enabled': True, 'model_name': 'gpt-6-astra', 'model_reasoning_effort': 'medium',
                    'watchlist': [{'symbol': 'sh600000', 'name': '合成公司'}]}
        self.identity = runner.prepare(self.s, self.cfg, AT)['id']
        runner.start(self.s, self.cfg, self.identity, '合成实验恢复测试', AT)
        fixture = measurement_fixtures.ExperimentMeasurementTests()
        fixture.setUp()
        self.packet = fixture.packet
        self.output = fixture.unknown()
        self.pid = 'EP-synthetic-audit'
        raw = encode(self.packet)
        pair = {'id': self.pid, 'experiment_id': self.identity, 'window_index': 1,
                'enrolled_at': AT, 'as_of': AT, 'symbol': 'sh600000', 'snapshot_id': 'audit-snapshot',
                'input_hash': digest(raw), 'input_json': raw, 'baseline_prompt': 'baseline',
                'candidate_prompt': 'candidate', 'event_key': 'audit-event', 'day_key': '2026-10-03'}
        # The live-enrollment tests own the evolving target-event representation.
        # Keep this recovery fixture forward-compatible with an added frozen field.
        columns = {r['name'] for r in self.s.db.execute('PRAGMA table_info(experiment_pairs)')}
        if 'target_json' in columns:
            pair['target_json'] = encode(['E1'])
        with self.s.db:
            self.s.db.execute('INSERT INTO experiment_pairs(' + ','.join(pair) + ') VALUES(' +
                              ','.join('?' for _ in pair) + ')', tuple(pair.values()))

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def fake_model(self, *args, **kwargs):
        return copy.deepcopy(self.output)

    def test_orphan_reservation_is_counted_and_recovered_without_overwriting(self):
        pair = dict(self.s.db.execute('SELECT * FROM experiment_pairs WHERE id=?', (self.pid,)).fetchone())
        orphan = runner._reserve(self.s, pair, 'baseline', AT)
        result = runner.run_pair(self.s, self.cfg, self.pid, model_fn=self.fake_model, clock=lambda: AT)
        self.assertEqual(result['status'], 'COMPLETE')
        records = self.s.db.execute('SELECT * FROM experiment_results WHERE attempt_id=?', (orphan,)).fetchall()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['status'], 'INTERRUPTED')
        self.assertEqual(runner.summary(self.s, self.identity)['model_calls'], 3)
        runner.run_pair(self.s, self.cfg, self.pid, model_fn=self.fake_model, clock=lambda: AT)
        self.assertEqual(runner.summary(self.s, self.identity)['model_calls'], 3)

    def test_repeated_single_arm_failure_stops_at_three_and_keeps_denominator(self):
        def fail_baseline(prompt, *args, **kwargs):
            if prompt == 'baseline':
                raise RuntimeError('PRIVATE_RAW_DETAIL_MUST_NOT_REACH_SUMMARY')
            return copy.deepcopy(self.output)
        for _ in range(5):
            runner.run_pair(self.s, self.cfg, self.pid, model_fn=fail_baseline, clock=lambda: AT)
        summary = runner.summary(self.s, self.identity)
        self.assertEqual((summary['enrolled'], summary['complete'], summary['failed']), (1, 0, 1))
        self.assertEqual(summary['model_calls'], 4)
        self.assertEqual(summary['retries'], 2)
        self.assertNotIn('PRIVATE_RAW_DETAIL', json.dumps(summary))
        errors = {r[0] for r in self.s.db.execute('SELECT error_code FROM experiment_results WHERE status=?', ('FAILED',))}
        self.assertEqual(errors, {'RuntimeError'})

    def test_expired_output_is_a_persisted_failure_not_a_summary_exception(self):
        clock = [AT]
        def finish_late(*args, **kwargs):
            clock[0] = '2026-10-03T15:00:00+00:00'
            return copy.deepcopy(self.output)
        result = runner.run_pair(self.s, self.cfg, self.pid, model_fn=finish_late, clock=lambda: clock[0])
        self.assertEqual(result['status'], 'FAILED')
        summary = runner.summary(self.s, self.identity)
        self.assertEqual((summary['enrolled'], summary['failed']), (1, 1))
        self.assertEqual(self.s.db.execute('SELECT error_code FROM experiment_results').fetchone()[0], 'OUTPUT_EXPIRED')

    def test_nonfinite_raw_output_is_persisted_invalid_and_does_not_orphan_call(self):
        def not_json(*args, **kwargs):
            output = copy.deepcopy(self.output)
            output['stocks'][0]['hypothesis_test']['threshold'] = float('nan')
            return output
        result = runner.run_pair(self.s, self.cfg, self.pid, model_fn=not_json, clock=lambda: AT)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM experiment_attempts').fetchone()[0], 2)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM experiment_results').fetchone()[0], 2)
        self.assertEqual(runner.summary(self.s, self.identity)['failed'], 1)

    def test_false_model_identity_is_retained_and_never_counted_complete(self):
        with patch('ashare.model.pinned', return_value={'name': self.cfg['model_name'],
                                                      'effort': self.cfg['model_reasoning_effort']}), \
             patch('ashare.model.run_json', side_effect=self.fake_model), \
             patch('ashare.model.call_meta', return_value={'actual_model': 'different-model'}):
            result = runner.run_pair(self.s, self.cfg, self.pid, clock=lambda: AT)
        self.assertEqual(result['status'], 'FAILED')
        summary = runner.summary(self.s, self.identity)
        self.assertEqual((summary['model_calls'], summary['enrolled'], summary['failed']), (2, 1, 1))
        self.assertNotIn('raw_output', summary)


if __name__ == '__main__':
    unittest.main()
