import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from ashare import evaluation, experiments, governance, judgments, selfcheck
from ashare.proposal_experiment import summary
from ashare.storage import Store, digest

AT = '2026-09-30T12:00:00+00:00'


def execution_fixture(status='RUNNING'):
    return {'version': 'forward-runner-v1', 'manifest_hash': 'a' * 64,
            'baseline_build': 'test-build', 'started_at': '2026-10-03T12:00:00+00:00',
            'ends_at': '2026-10-08T12:00:00+00:00', 'status': status,
            'windows': [{'index': 1, 'start': '2026-10-03T12:00:00+00:00', 'end': '2026-10-05T12:00:00+00:00'},
                        {'index': 2, 'start': '2026-10-06T12:00:00+00:00', 'end': '2026-10-08T12:00:00+00:00'}],
            'primary_metric': 'verifiable_prediction_rate', 'metric_label': '可检验结构比例',
            'model_calls': 8, 'max_model_calls': 120, 'retries': 1, 'minimum_pairs': 5,
            'enrolled': 4, 'complete': 2, 'failed': 1, 'pending': 1, 'baseline_rate': .5,
            'candidate_rate': 1., 'paired_delta': .5, 'day_clusters': 2,
            'baseline_citation_rate': .5, 'candidate_citation_rate': 1., 'assessment': 'PENDING',
            'window_results': [{'window': 1, 'enrolled': 4, 'complete': 2, 'failed': 1, 'pending': 1,
                                'baseline_rate': .5, 'candidate_rate': 1., 'paired_delta': .5, 'day_clusters': 2,
                                'baseline_citation_rate': .5, 'candidate_citation_rate': 1.},
                               {'window': 2, 'enrolled': 0, 'complete': 0, 'failed': 0, 'pending': 0,
                                'baseline_rate': None, 'candidate_rate': None, 'paired_delta': None, 'day_clusters': 0,
                                'baseline_citation_rate': None, 'candidate_citation_rate': None}],
            'conclusion': 'COLLECTING', 'stop_reason': None,
            'production_changes': False, 'profit_evidence': False}


class ProposalExperimentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def generated(self):
        with self.store.db:
            evaluation._insert(self.store, 'g1', 'global', 'GOLD', 'g1',
                '2026-09-15T12:00:00+00:00', '2026-09-15', 'test-build', 3, None,
                {'stance': 'LONG'}, research={'thesis': 'RAW_PRIVATE_MARKER', 'next_checks': []})
            self.store.db.execute('INSERT INTO signal_scores VALUES(?,?,?,?,?)',
                ('g1', evaluation.SCORE_METHOD, 'SCORED', judgments.encode({
                    'method': evaluation.SCORE_METHOD, 'entry_date': '2026-09-16',
                    'exit_date': '2026-09-18', 'return_bps': -100, 'excess_bps': -100}), AT))
        selfcheck.run(self.store, {'data_dir': self.tmp.name, 'deployment_role': 'research'}, AT, generate=True)
        proposal = governance.proposals(self.store)[0]
        return proposal['id'], experiments.view(self.store)[0]

    def stored(self, spec, *, hashed=None, state='DESIGNED'):
        with self.store.db:
            pid = governance.draft_proposal(self.store, source='test', kind='RULE',
                target='global', title='合成实验设计', payload={}, at=AT)
            fingerprint = hashed or digest(judgments.encode(spec))
            identity = 'EX-' + digest(pid + fingerprint)[:20]
            self.store.db.execute('INSERT INTO experiment_designs VALUES(?,?,?,?,?)',
                (identity, pid, AT, fingerprint, judgments.encode(spec)))
            if state:
                self.store.db.execute('INSERT INTO experiment_events(experiment_id,at,status,note) VALUES(?,?,?,?)',
                    (identity, AT, state, 'RAW_EVENT_NOTE_MARKER'))
        return pid

    def test_missing_design_returns_none_without_writes(self):
        before = self.store.db.total_changes
        self.assertIsNone(summary(self.store, 'missing'))
        self.assertEqual(self.store.db.total_changes, before)

    def test_real_selfcheck_design_has_exact_allowlist_and_is_read_only(self):
        pid, design = self.generated()
        before = self.store.db.total_changes
        with patch('pathlib.Path.read_text', side_effect=AssertionError('No file reads')):
            result = summary(self.store, pid)
        self.assertEqual(self.store.db.total_changes, before)
        self.assertEqual(set(result), {'id', 'status', 'version', 'baseline_build', 'route',
            'environment', 'primary_metric', 'enrollment', 'budget', 'controls', 'spec_hash'})
        self.assertEqual(result['id'], design['id'])
        self.assertEqual(result['status'], 'DESIGNED')
        self.assertEqual(result['enrollment'], design['spec']['enrollment'])
        self.assertEqual(result['primary_metric'], design['spec']['primary_metric'])
        self.assertEqual(result['spec_hash'], digest(judgments.encode(design['spec'])))
        self.assertNotIn('RAW_PRIVATE_MARKER', json.dumps(result))
        self.assertNotIn('started_at', result)

    def test_closed_design_preserves_frozen_spec_and_current_status(self):
        pid, design = self.generated()
        before = summary(self.store, pid)
        experiments.close(self.store, design['id'], 'REJECTED', 'RAW_EVENT_NOTE_MARKER', at=AT)
        after = summary(self.store, pid)
        self.assertEqual(after['status'], 'REJECTED')
        self.assertEqual(after['spec_hash'], before['spec_hash'])
        self.assertNotIn('RAW_EVENT_NOTE_MARKER', json.dumps(after))

    def test_hash_mismatch_and_invalid_stored_designs_fail_closed(self):
        _, design = self.generated()
        spec = design['spec']
        pid = self.stored(spec, hashed='0' * 64)
        with self.assertRaisesRegex(ValueError, '冻结实验设计'):
            summary(self.store, pid)
        cases = []
        for path, value in [(('primary_metric',), '/tmp/RAW_PRIVATE_MARKER'),
                            (('environment',), 'x' * 201),
                            (('enrollment', 'window_days'), True),
                            (('budget', 'major_changes'), True),
                            (('controls',), ['READ_RAW_SOURCE']),
                            (('private_payload',), {'raw': 'RAW_PRIVATE_MARKER'})]:
            invalid = copy.deepcopy(spec)
            target = invalid if len(path) == 1 else invalid[path[0]]
            target[path[-1]] = value
            cases.append(invalid)
        for invalid in cases:
            with self.subTest(invalid=invalid):
                pid = self.stored(invalid)
                with self.assertRaisesRegex(ValueError, '冻结实验设计') as failure:
                    summary(self.store, pid)
                self.assertNotIn('RAW_PRIVATE_MARKER', str(failure.exception))

    def test_missing_or_unknown_status_is_not_reported_as_designed(self):
        _, design = self.generated()
        for state in (None, 'RUNNING', 'APPROVED'):
            with self.subTest(state=state):
                pid = self.stored(design['spec'], state=state)
                with patch('ashare.proposal_experiment._runner_summary', return_value=None), self.assertRaises(ValueError):
                    summary(self.store, pid)

    def test_running_summary_is_allowlisted_stable_and_read_only(self):
        _, design = self.generated()
        pid = self.stored(design['spec'], state='RUNNING')
        raw = {**execution_fixture(), 'raw_input': 'RAW_PRIVATE_MARKER',
               'output': {'raw': 'RAW_PRIVATE_MARKER'}, 'updated_at': AT}
        raw['windows'][0]['private'] = 'RAW_PRIVATE_MARKER'
        raw['window_results'][0]['private'] = 'RAW_PRIVATE_MARKER'
        raw['metric_label'] = 'RAW_PRIVATE_MARKER'
        before = self.store.db.total_changes
        with patch('ashare.proposal_experiment._runner_summary', return_value=raw), \
                patch('pathlib.Path.read_text', side_effect=AssertionError('No file reads')):
            first = summary(self.store, pid)
            raw['updated_at'] = '2026-10-04T12:00:00+00:00'
            second = summary(self.store, pid)
        self.assertEqual(self.store.db.total_changes, before)
        self.assertEqual(first, second)
        self.assertEqual(set(first['execution']), set(execution_fixture()))
        self.assertNotIn('RAW_PRIVATE_MARKER', json.dumps(first))
        self.assertNotIn('updated_at', first['execution'])
        self.assertEqual(first['execution']['metric_label'], '可检验结构比例')
        self.assertEqual(first['execution']['model_calls'], 8)

    def test_running_summary_rejects_unsafe_or_inconsistent_statistics(self):
        _, design = self.generated()
        pid = self.stored(design['spec'], state='RUNNING')
        values = [('status', 'COMPLETED'), ('version', 'unknown'),
                  ('baseline_build', '/tmp/RAW_PRIVATE_MARKER'), ('manifest_hash', 'RAW_PRIVATE_MARKER'),
                  ('enrolled', True), ('failed', -1), ('model_calls', 121),
                  ('baseline_rate', float('nan')), ('candidate_rate', 1.1), ('paired_delta', -1.1),
                  ('baseline_citation_rate', True), ('candidate_citation_rate', -1),
                  ('assessment', 'PROFIT_PROVEN'), ('window_results', []),
                  ('conclusion', 'PROFIT_PROVEN'), ('stop_reason', '/tmp/RAW_PRIVATE_MARKER'),
                  ('production_changes', True), ('profit_evidence', True),
                  ('started_at', '2026-10-03'), ('windows', [])]
        for key, value in values:
            with self.subTest(key=key):
                execution = {**execution_fixture(), key: value}
                with patch('ashare.proposal_experiment._runner_summary', return_value=execution), self.assertRaises(ValueError) as failure:
                    summary(self.store, pid)
                self.assertNotIn('RAW_PRIVATE_MARKER', str(failure.exception))
        execution = execution_fixture()
        execution['windows'][1]['start'] = execution['windows'][0]['start']
        with patch('ashare.proposal_experiment._runner_summary', return_value=execution), self.assertRaises(ValueError):
            summary(self.store, pid)
        for key, value in [('window', 1), ('enrolled', True), ('candidate_citation_rate', float('nan'))]:
            execution = execution_fixture()
            execution['window_results'][1][key] = value
            with patch('ashare.proposal_experiment._runner_summary', return_value=execution), self.assertRaises(ValueError):
                summary(self.store, pid)

    def test_finished_execution_does_not_lose_its_statistics(self):
        _, design = self.generated()
        for state, conclusion in [('COMPLETED', 'DESCRIPTIVE_ONLY'), ('INCONCLUSIVE', 'INSUFFICIENT'),
                                  ('CANCELLED', 'CANCELLED'), ('REJECTED', 'REJECTED')]:
            with self.subTest(state=state):
                pid = self.stored(design['spec'], state=state)
                raw = {**execution_fixture(state), 'conclusion': conclusion, 'stop_reason': 'WINDOW_COMPLETE'}
                with patch('ashare.proposal_experiment._runner_summary', return_value=raw):
                    result = summary(self.store, pid)
                self.assertEqual(result['status'], state)
                self.assertEqual(result['execution']['conclusion'], conclusion)
                self.assertFalse(result['execution']['profit_evidence'])

    def test_body_and_event_note_are_not_exposed(self):
        _, design = self.generated()
        spec = copy.deepcopy(design['spec'])
        for key in ('failure_criteria', 'rollback'):
            spec[key] = '/tmp/RAW_PRIVATE_MARKER'
        spec['change']['text'] = 'RAW_PRIVATE_MARKER'
        pid = self.stored(spec)
        with patch('pathlib.Path.read_text', side_effect=AssertionError('No path following')):
            result = summary(self.store, pid)
        self.assertNotIn('RAW_PRIVATE_MARKER', json.dumps(result))
        self.assertNotIn('RAW_EVENT_NOTE_MARKER', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
