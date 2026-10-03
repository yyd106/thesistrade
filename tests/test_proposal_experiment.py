import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from ashare import evaluation, experiments, governance, judgments, selfcheck
from ashare.proposal_experiment import summary
from ashare.storage import Store, digest

AT = '2026-09-30T12:00:00+00:00'


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
                with self.assertRaises(ValueError):
                    summary(self.store, pid)

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
