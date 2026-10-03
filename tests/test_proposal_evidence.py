import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ashare import evaluation, governance, judgments, proposal_evidence, selfcheck, supervision
from ashare.storage import Store


class ProposalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.at = '2026-09-30T12:00:00+00:00'
        self.cfg = {'data_dir': self.tmp.name, 'deployment_role': 'research', 'model_enabled': True,
                    'model_name': 'synthetic', 'model_reasoning_effort': 'high'}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def signal(self, identity='g1', *, symbol='GOLD', route='global', build='b1', days=3):
        stamp = '2026-09-15T12:00:00+00:00'
        with self.store.db:
            evaluation._insert(self.store, identity, route, symbol, identity, stamp, stamp[:10], build, days, None,
                               {'stance': 'LONG', 'action': 'ALLOW', 'plan_kind': 'PAPER_TRADE'},
                               research={'thesis': 'DO_NOT_EXPORT_RESEARCH_BODY', 'next_checks': ['核对公告']})
            self.store.db.execute('INSERT INTO signal_scores VALUES(?,?,?,?,?)',
                (identity, evaluation.SCORE_METHOD, 'SCORED', judgments.encode({
                    'method': evaluation.SCORE_METHOD, 'entry_date': '2026-09-16', 'exit_date': '2026-09-18',
                    'return_bps': -100, 'excess_bps': -100}), self.at))

    def candidate(self):
        selfcheck.run(self.store, self.cfg, self.at, generate=True)
        p = governance.proposals(self.store)[0]
        return p, p['payload']

    @staticmethod
    def answer():
        return {'verdict': 'INSUFFICIENT', 'summary': '待验证假设，尚不能判断策略有效性。',
                'checks': [{'id': k, 'status': 'UNKNOWN', 'reason': '现有统计不足以证明候选收益。',
                            'evidence_refs': ['proposal_evidence']} for k in supervision.CHECKS],
                'counterexamples': ['价格变化可能来自市场共同变化。'], 'effectiveness': [],
                'next_steps': ['等待前向对照实验。']}

    def ready(self, pid):
        governance.decide(self.store, pid, 'READY', decided_by=None, note='合成测试完整提案', at=self.at)

    def test_actual_candidate_ready_request_and_two_sessions_without_production_changes(self):
        self.signal()
        p, payload = self.candidate()
        self.assertEqual(p['status'], 'DRAFT')
        self.ready(p['id'])
        frozen = dict(self.store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (p['id'],)).fetchone())
        rid = supervision.request(self.store, self.cfg, 'PROPOSAL', p['id'], at=self.at)
        packets = []

        def model(prompt, schema, folder, timeout):
            self.assertNotIn('DO_NOT_EXPORT_RESEARCH_BODY', prompt)
            packet = json.loads(prompt.split('<UNTRUSTED_SUMMARIES>')[1].split('</UNTRUSTED_SUMMARIES>')[0])
            packets.append(packet)
            return self.answer()

        self.assertEqual(supervision.run(self.store, self.cfg, rid, model_fn=model)['status'], 'SUCCEEDED')
        self.assertEqual([x['phase'] for x in packets], ['FACTS', 'REVIEW'])
        self.assertNotIn('proposal', packets[0])
        self.assertIn('proposal_evidence', packets[0])
        proof = packets[1]['proposal']['evidence_summary']
        self.assertEqual(proof['status'], 'INSUFFICIENT')
        self.assertEqual(proof['additional_count'], 0)
        self.assertEqual(proof['cohorts'][0]['excess']['n'], 1)
        self.assertEqual(proof['cohorts'][0]['provenance'], 'LIVE')
        self.assertEqual(frozen, dict(self.store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (p['id'],)).fetchone()))
        for table in ('paper_flows', 'paper_orders', 'global_orders', 'strategy_guidance', 'cloud_receipts'):
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
        self.assertEqual(supervision.listing(self.store, subject_id=p['id'])[0]['approval'], 'NOT_GRANTED')

    def test_legacy_text_keeps_exact_snapshot_hash_and_never_follows_paths(self):
        payload = {k: '固定检验文字 ' + k for k in supervision.FIELDS}
        payload['evidence'] = '路径仅作为说明 /private/missing/raw-source.txt'
        with self.store.db:
            pid = governance.draft_proposal(self.store, source='manual', kind='RULE', target='test',
                title='原格式', payload=payload, at=self.at)
        row = dict(self.store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (pid,)).fetchone())
        expected = {k: row[k] for k in ('id', 'kind', 'target', 'title', 'created_at')}
        expected.update({k: payload[k] for k in supervision.FIELDS})
        expected['proposal_hash'] = supervision.sha(expected)
        with patch.object(Path, 'read_text', side_effect=AssertionError('must not follow evidence paths')):
            self.assertEqual(supervision.proposal_input(self.store, pid), expected)
        self.assertEqual(proposal_evidence.summary(self.store, row, payload)['status'], 'RECORDED')

    def test_invalid_unknown_empty_and_nested_evidence_is_not_serialized(self):
        self.signal()
        p, payload = self.candidate()
        for value in (None, '', '  ', [], {}, {'body': 'UNTRUSTED_NESTED_CONTENT'},
                      {**payload['evidence'], 'instruction': {'nested': 'UNTRUSTED_NESTED_CONTENT'}},
                      {**payload['evidence'], 'ids': []}, {**payload['evidence'], 'ids': ['../raw/private.txt']}):
            with self.subTest(value=value):
                bad = {**payload, 'evidence': value}
                result = proposal_evidence.summary(self.store, p, bad)
                self.assertEqual(result['status'], 'INVALID')
                self.assertNotIn('UNTRUSTED_NESTED_CONTENT', json.dumps(result))
        bad = {**payload, 'evidence': {**payload['evidence'], 'run_id': 'SC-' + '0' * 20}}
        self.assertEqual(proposal_evidence.summary(self.store, p, bad)['status'], 'MISSING')
        with self.store.db:
            self.store.db.execute('UPDATE strategy_proposals SET payload_json=? WHERE id=?', (json.dumps(bad), p['id']))
        with self.assertRaisesRegex(ValueError, '证据不可审查'):
            supervision.request(self.store, self.cfg, 'PROPOSAL', p['id'])

    def test_cross_scope_and_partial_cohort_references_cannot_borrow_statistics(self):
        self.signal()
        self.signal('g2', symbol='SILVER')
        p, payload = self.candidate()
        partial = copy.deepcopy(payload)
        partial['evidence']['ids'] = ['g1']
        self.assertEqual(proposal_evidence.summary(self.store, p, partial)['status'], 'INVALID')
        mismatch = copy.deepcopy(payload)
        mismatch['applicability']['build_id'] = 'other-build'
        self.assertEqual(proposal_evidence.summary(self.store, p, mismatch)['status'], 'INVALID')
        unknown = copy.deepcopy(payload)
        unknown['evidence']['ids'] = ['not-in-frozen-report']
        self.assertEqual(proposal_evidence.summary(self.store, p, unknown)['status'], 'INVALID')

    def test_append_invalidates_pending_and_succeeded_without_rewriting_frozen_input(self):
        for reviewed in (False, True):
            with self.subTest(reviewed=reviewed):
                # Use separate build groups so each iteration has its own candidate.
                build = 'b' + str(reviewed)
                self.signal('g' + build, symbol='ASSET' + build, build=build)
                selfcheck.run(self.store, self.cfg, self.at, generate=True)
                p = next(x for x in governance.proposals(self.store) if x['payload'].get('applicability', {}).get('build_id') == build)
                self.ready(p['id'])
                rid = supervision.request(self.store, self.cfg, 'PROPOSAL', p['id'], at=self.at)
                if reviewed:
                    supervision.run(self.store, self.cfg, rid, model_fn=lambda *args: self.answer())
                frozen = self.store.db.execute('SELECT input_json FROM supervision_reviews WHERE id=?', (rid,)).fetchone()[0]
                before = supervision.proposal_input(self.store, p['id'])
                self.signal('more-' + build, symbol='MORE' + build, build=build)
                selfcheck.run(self.store, self.cfg, '2026-10-01T12:00:00+00:00', generate=True)
                after = supervision.proposal_input(self.store, p['id'])
                self.assertNotEqual(before['proposal_hash'], after['proposal_hash'])
                self.assertEqual(after['evidence_summary']['additional_count'], 1)
                self.assertEqual(supervision.listing(self.store, subject_id=p['id'])[0]['status'], 'STALE')
                self.assertEqual(supervision.run(self.store, self.cfg, rid,
                    model_fn=lambda *args: self.fail('stale material must not call model'))['status'], 'STALE')
                self.assertEqual(self.store.db.execute('SELECT input_json FROM supervision_reviews WHERE id=?', (rid,)).fetchone()[0], frozen)
                newer = supervision.request(self.store, self.cfg, 'PROPOSAL', p['id'])
                self.assertNotEqual(rid, newer)
                self.assertEqual(supervision.request(self.store, self.cfg, 'PROPOSAL', p['id']), newer)

    def test_summary_reads_only_frozen_report_tables_and_is_reproducible(self):
        self.signal()
        p, payload = self.candidate()
        statements = []
        self.store.db.set_trace_callback(statements.append)
        before = proposal_evidence.summary(self.store, p, payload)
        self.store.db.set_trace_callback(None)
        selects = [x.lower() for x in statements if x.lstrip().lower().startswith('select')]
        self.assertTrue(selects)
        self.assertTrue(all('selfcheck_evidence' in q or 'selfcheck_runs' in q for q in selects))
        self.signal('new-not-frozen', symbol='SILVER')
        self.assertEqual(before, proposal_evidence.summary(self.store, p, payload))
        self.store.close()
        self.store = Store(self.tmp.name)
        self.assertEqual(before, proposal_evidence.summary(self.store, p, payload))

    def test_bounded_display_keeps_full_material_identity(self):
        for i in range(25):
            self.signal('id' + str(i), symbol='ASSET' + str(i), days=1 + i % 10)
        p, payload = self.candidate()
        result = proposal_evidence.summary(self.store, p, payload)
        self.assertEqual(result['status'], 'INSUFFICIENT')
        self.assertLessEqual(len(result['cohorts']), 8)
        self.assertEqual(result['cohort_count'], 10)
        self.assertEqual(len(result['references']), 20)
        self.assertEqual(result['references_count'], 26)
        self.assertEqual(result['snapshots'][0]['reference_count'], 25)
        self.assertLess(len(json.dumps(result)), 16000)

    def test_changed_append_fingerprint_fails_closed_and_expires_review(self):
        self.signal()
        p, _ = self.candidate()
        self.ready(p['id'])
        supervision.request(self.store, self.cfg, 'PROPOSAL', p['id'])
        with self.store.db:
            self.store.db.execute("UPDATE selfcheck_evidence SET payload_json=? WHERE proposal_id=?",
                (json.dumps({'ids': ['g1'], 'signatures': ['wrong'], 'nested': 'DO_NOT_EXPORT'}), p['id']))
        self.assertEqual(supervision.listing(self.store, subject_id=p['id'])[0]['status'], 'STALE')
        with self.assertRaisesRegex(ValueError, '证据不可审查'):
            supervision.proposal_input(self.store, p['id'])

    def test_same_second_appends_keep_append_order_not_hash_order(self):
        self.signal()
        p, payload = self.candidate()
        for identity, symbol in [('g2', 'SILVER'), ('g3', 'BTC')]:
            self.signal(identity, symbol=symbol)
            latest = selfcheck.run(self.store, self.cfg, self.at, generate=True)
        result = proposal_evidence.summary(self.store, p, payload)
        self.assertEqual(result['additional_count'], 2)
        self.assertEqual(result['snapshots'][-1]['run_id'], latest['id'])
        self.assertEqual(result['cohorts'][0]['excess']['n'], 3)
        self.assertIn('样本 3', result['text'][:260])
        self.assertIn('3根观测日线', result['text'][:260])


if __name__ == '__main__':
    unittest.main()
