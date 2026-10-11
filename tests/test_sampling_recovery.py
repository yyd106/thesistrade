"""Calendar-cohort diagnostics use all known history without inventing independence."""
import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ashare import evaluation, governance, judgments, proposal_evidence, quick_diagnostics, selfcheck
from ashare.storage import Store, digest


def sessions(start, count):
    """Synthetic weekday sessions; these tests do not rescore exchange prices."""
    result, day = [], date.fromisoformat(start)
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day.isoformat())
        day += timedelta(days=1)
    return result


def sample(identity, entry, exit_, *, symbol='A', route='watchlist', days=20, value=100):
    return {'id': identity, 'symbol': symbol, 'route': route, 'horizon_days': days,
            'created_at': entry + 'T00:00:00+00:00', 'build_id': 'b1',
            'judgment': {'trend_ok': True, 'model_action': 'WATCH', 'stance': 'LONG'},
            'score': {'entry_date': entry, 'exit_date': exit_, 'excess_bps': value,
                      'return_bps': value, 'method': evaluation.SCORE_METHOD}}


class DateCohortTests(unittest.TestCase):
    def test_whole_day_keeps_all_symbols_and_uses_latest_exit(self):
        rows = [sample('short', '2026-01-01', '2026-01-03', symbol='A'),
                sample('long', '2026-01-01', '2026-01-05', symbol='B'),
                sample('staggered', '2026-01-04', '2026-01-08', symbol='C'),
                sample('boundary', '2026-01-05', '2026-01-09', symbol='D'),
                sample('next-a', '2026-01-06', '2026-01-10', symbol='A'),
                sample('next-b', '2026-01-06', '2026-01-10', symbol='B')]
        selected = evaluation.time_clusters(evaluation.non_overlapping(rows, 20))
        self.assertEqual({r['id'] for r in selected}, {'short', 'long', 'next-a', 'next-b'})
        stats = evaluation.describe(selected)
        self.assertEqual((stats['n'], stats['time_clusters']), (4, 2))
        self.assertIsNone(stats['ci95_bps'])

    def test_selection_does_not_depend_on_values_builds_labels_or_input_order(self):
        rows = [sample(str(i), f'2026-01-{i + 1:02d}', f'2026-01-{i + 5:02d}', symbol=str(i % 2))
                for i in range(16)]
        expected = {r['id'] for r in evaluation.non_overlapping(evaluation.daily_samples(rows), 20)}
        altered = copy.deepcopy(rows[::-1])
        for i, row in enumerate(altered):
            row['build_id'] = 'build-' + str(i)
            row['environment'] = 'different'
            row['judgment'].update(trend_ok=False, model_action='NOT_WATCH', stance='WAIT')
            row['score'].update(return_bps=-99999 + i, excess_bps=99999 - i)
        self.assertEqual({r['id'] for r in evaluation.non_overlapping(evaluation.daily_samples(altered), 20)}, expected)

    def test_horizons_and_routes_are_selected_separately_but_q5_has_one_horizon(self):
        rows = [sample('h5', '2026-01-01', '2026-01-05', days=5),
                sample('h20', '2026-01-01', '2026-01-20', days=20),
                sample('next5', '2026-01-06', '2026-01-10', days=5),
                sample('other-route', '2026-01-02', '2026-01-21', route='global')]
        self.assertEqual(len(evaluation.daily_samples(rows)), 4)
        self.assertEqual(len(evaluation.non_overlapping(evaluation.daily_samples(rows), 20)), 4)
        rows[0]['score']['method'] = rows[1]['score']['method'] = evaluation.QUICK_SCORE_METHOD
        rows[1]['created_at'] = '2026-01-01T01:00:00+00:00'
        self.assertEqual(evaluation.sample_horizon(rows[1]), 5)
        self.assertEqual({r['id'] for r in evaluation.daily_samples(rows[:2])}, {'h20'})

    def test_tied_daily_judgments_use_id_not_input_order_or_return(self):
        rows = [sample('a', '2026-01-01', '2026-01-05', value=999),
                sample('z', '2026-01-01', '2026-01-05', value=-999)]
        self.assertEqual([r['id'] for r in evaluation.daily_samples(rows)], ['z'])
        self.assertEqual([r['id'] for r in evaluation.daily_samples(rows[::-1])], ['z'])


class SamplingHistoryTests(unittest.TestCase):
    at = '2027-01-01T12:00:00+00:00'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cfg = {'data_dir': self.tmp.name, 'deployment_role': 'research', 'evaluation_horizon_days': 20}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, identity, entry, exit_, *, days=20, symbol='A', route='watchlist', method=None,
            created_at=None, scored_at=None, frozen_at=None, value=100, build='b1'):
        method = method or evaluation.SCORE_METHOD
        created_at = created_at or ((date.fromisoformat(entry) - timedelta(days=1)).isoformat() + 'T00:00:00+00:00')
        judgment = {'trend_ok': True, 'model_action': 'WATCH', 'stance': 'LONG', 'plan_kind': 'PAPER_TRADE'}
        with self.store.db:
            evaluation._insert(self.store, identity, route, symbol, identity, created_at, created_at[:10], build, days,
                               None if route == 'global' else evaluation.BENCHMARK, judgment,
                               research={'thesis': 'synthetic'}, historical=frozen_at is not None)
            if frozen_at is not None:
                judgments.freeze(self.store, identity=identity, route=route, symbol=symbol, source_id=identity,
                    at=created_at, build_id=build, days=days, benchmark=evaluation.BENCHMARK,
                    judgment=judgment, frozen_at=frozen_at)
            self.store.db.execute('INSERT INTO signal_scores VALUES(?,?,?,?,?)',
                (identity, method, 'SCORED', judgments.encode({'method': method, 'entry_date': entry, 'exit_date': exit_,
                 'return_bps': value, 'excess_bps': value}), scored_at or self.at))

    def freeze_report(self, content, at=None):
        fingerprint = digest(judgments.encode(content))
        identity = 'SC-' + fingerprint[:20]
        report = {**content, 'id': identity, 'created_at': at or self.at}
        with self.store.db:
            self.store.db.execute('INSERT INTO selfcheck_runs VALUES(?,?,?,?)',
                (identity, report['created_at'], fingerprint, judgments.encode(report)))
        return report

    def test_rolling_scores_recluster_after_selection_and_keep_30_cluster_gate(self):
        days = sessions('2025-01-02', 155)
        for i in range(145):
            self.add('q' + str(i), days[i], days[i + 4], method=evaluation.QUICK_SCORE_METHOD,
                     symbol='A' if i % 2 == 0 else 'B', value=i)
        before = evaluation.quick_comparisons(self.store, self.cfg, at=self.at)['groups']['trend_filter']['trend_ok']
        self.assertEqual(before['daily']['time_clusters'], 1)
        self.assertEqual(before['non_overlapping']['time_clusters'], 29)
        self.assertIsNone(before['non_overlapping']['ci95_bps'])
        self.add('q145', days[145], days[149], method=evaluation.QUICK_SCORE_METHOD, symbol='B', value=145)
        after = evaluation.quick_comparisons(self.store, self.cfg, at=self.at)
        self.assertEqual(after['method'], evaluation.QUICK_SCORE_METHOD)
        self.assertEqual(after['sampling_method'], evaluation.SAMPLING_METHOD)
        stats = after['groups']['trend_filter']['trend_ok']
        self.assertEqual(stats['daily']['time_clusters'], 1)
        self.assertIsNone(stats['daily']['ci95_bps'])
        self.assertEqual(stats['non_overlapping']['time_clusters'], 30)
        self.assertIsNotNone(stats['non_overlapping']['ci95_bps'])

    def test_all_history_can_accumulate_30_twenty_session_cohorts(self):
        days = sessions('2024-01-02', 600)
        for i in range(30):
            self.add('long' + str(i), days[20 * i], days[20 * i + 19], days=20, value=i * 5)
        original_scores = list(map(tuple, self.store.db.execute('SELECT * FROM signal_scores ORDER BY signal_id')))
        result = selfcheck.run(self.store, self.cfg, self.at)
        report = selfcheck.status(self.store, result['id'])
        self.assertEqual(report['method'], 'selfcheck-v2')
        self.assertEqual(report['sampling_method'], evaluation.SAMPLING_METHOD)
        self.assertEqual(report['history_scope'], 'ALL_AS_OF_CUTOFF')
        self.assertIsNone(report['lookback_days'])
        self.assertEqual(report['counts'], {'watchlist:SCORED': 30})
        self.assertEqual(len(report['cohorts']), 1)
        self.assertEqual(report['cohorts'][0]['excess']['time_clusters'], 30)
        self.assertIsNotNone(report['cohorts'][0]['excess']['ci95_bps'])
        self.assertEqual(original_scores, list(map(tuple, self.store.db.execute('SELECT * FROM signal_scores ORDER BY signal_id'))))
        self.assertEqual({r['method'] for r in self.store.db.execute('SELECT method FROM signal_scores')}, {evaluation.SCORE_METHOD})
        self.assertIn('不是前向预注册', (self.store.root / result['report']).read_text())

    def test_same_day_symbols_add_rows_not_time_clusters(self):
        for i in range(50):
            self.add('same' + str(i), '2026-01-05', '2026-02-01', symbol='ASSET' + str(i))
        report, _ = selfcheck.analyze(self.store, self.at)
        stats = report['cohorts'][0]['excess']
        self.assertEqual((stats['n'], stats['time_clusters']), (50, 1))
        self.assertIsNone(stats['ci95_bps'])

    def test_pooled_horizons_do_not_count_overlapping_clusters_twice(self):
        self.add('short1', '2026-01-01', '2026-01-05', days=5)
        self.add('short2', '2026-01-06', '2026-01-10', days=5)
        self.add('long', '2026-01-03', '2026-01-22', days=20)
        result = evaluation.comparisons(self.store, self.cfg, at=self.at)
        stats = result['groups']['trend_filter']['trend_ok']['non_overlapping']
        self.assertEqual((stats['n'], stats['time_clusters']), (3, 1))
        report, _ = selfcheck.analyze(self.store, self.at)
        by_horizon = {c['horizon_days']: c['excess'] for c in report['cohorts']}
        self.assertEqual(by_horizon[5]['time_clusters'], 2)
        self.assertEqual(by_horizon[20]['time_clusters'], 1)

    def test_all_history_still_hides_future_creation_freezing_and_scores(self):
        self.add('known-old', '2024-01-02', '2024-01-29')
        self.add('future-score', '2026-01-02', '2026-01-29', scored_at='2027-01-02T00:00:00+00:00')
        self.add('future-frozen', '2026-02-02', '2026-02-27', frozen_at='2027-01-02T00:00:00+00:00')
        self.add('future-created', '2027-02-02', '2027-02-27', created_at='2027-02-01T00:00:00+00:00')
        rows = {r['id']: r for r in selfcheck.outcomes(self.store, self.at)}
        self.assertEqual(set(rows), {'known-old', 'future-score'})
        self.assertEqual(rows['known-old']['status'], 'SCORED')
        self.assertEqual(rows['future-score']['status'], 'AWAITING_SCORE')

    def test_v2_keeps_legacy_report_and_rejected_candidate_identity(self):
        self.add('first', '2026-01-02', '2026-01-29')
        content, rows = selfcheck.analyze(self.store, self.at)
        legacy = copy.deepcopy(content)
        legacy.update(method='selfcheck-v1', lookback_days=180)
        legacy.pop('sampling_method')
        legacy.pop('history_scope')
        report = self.freeze_report(legacy)
        folder = self.store.root / 'workflow' / 'selfcheck'
        folder.mkdir(parents=True)
        old_file = folder / (report['id'] + '.json')
        old_file.write_text(judgments.encode(report))
        frozen_file = old_file.read_bytes()
        frozen_row = tuple(self.store.db.execute('SELECT * FROM selfcheck_runs WHERE id=?', (report['id'],)).fetchone())
        selfcheck.propose(self.store, report, rows, self.at)
        proposal = governance.proposals(self.store)[0]
        governance.decide(self.store, proposal['id'], 'REJECTED', decided_by=None, note='synthetic rejection', at=self.at)
        original_payload = self.store.db.execute('SELECT payload_json FROM strategy_proposals WHERE id=?', (proposal['id'],)).fetchone()[0]
        self.add('second', '2026-01-02', '2026-01-29', symbol='B')
        result = selfcheck.run(self.store, self.cfg, self.at, generate=True)
        self.assertEqual(len(governance.proposals(self.store)), 1)
        self.assertEqual(result['candidates']['updates'], [{'proposal_id': proposal['id'], 'action': 'EVIDENCE_APPENDED', 'status': 'REJECTED'}])
        self.assertNotEqual(result['id'], report['id'])
        self.assertEqual(tuple(self.store.db.execute('SELECT * FROM selfcheck_runs WHERE id=?', (report['id'],)).fetchone()), frozen_row)
        self.assertEqual(old_file.read_bytes(), frozen_file)
        self.assertEqual(self.store.db.execute('SELECT payload_json FROM strategy_proposals WHERE id=?', (proposal['id'],)).fetchone()[0], original_payload)
        summary = proposal_evidence.summary(self.store, proposal, json.loads(original_payload))
        self.assertEqual(summary['status'], 'INSUFFICIENT')
        self.assertEqual(summary['additional_count'], 1)
        self.assertEqual(summary['snapshots'][-1]['report_method'], 'selfcheck-v2')
        self.assertEqual(summary['snapshots'][-1]['sampling_method'], evaluation.SAMPLING_METHOD)

    def test_v2_with_missing_or_unknown_sampling_boundary_fails_closed(self):
        self.add('one', '2026-01-02', '2026-01-29')
        content, _ = selfcheck.analyze(self.store, self.at)
        scope = {'route': 'watchlist', 'build_id': 'b1', 'environment': 'UNKNOWN'}
        for key in ('sampling_method', 'history_scope'):
            changed = copy.deepcopy(content)
            changed[key] = 'unknown-method'
            report = self.freeze_report(changed)
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, '方法不一致'):
                proposal_evidence._resolve(self.store, report['id'], ['one'], scope)

    def test_same_ids_method_upgrade_appends_without_rewriting_legacy_and_repeats_dedupe(self):
        self.add('short1', '2026-11-01', '2026-11-05', days=5)
        self.add('short2', '2026-11-06', '2026-11-10', days=5)
        self.add('long', '2026-11-03', '2026-11-22', days=20, symbol='B')
        content, rows = selfcheck.analyze(self.store, self.at)
        legacy = copy.deepcopy(content)
        legacy.update(method='selfcheck-v1', lookback_days=180)
        legacy.pop('sampling_method')
        legacy.pop('history_scope')
        # Legacy clustering happened before splitting horizons, so the long
        # observation bridged both short observations into one time cluster.
        for cohort in legacy['cohorts']:
            for name in ('price', 'excess'):
                cohort[name]['time_clusters'] = 1
        report = self.freeze_report(legacy)
        selfcheck.propose(self.store, report, rows, self.at)
        proposal = governance.proposals(self.store)[0]
        payload = proposal['payload']
        before = proposal_evidence.summary(self.store, proposal, payload)
        self.assertEqual({c['horizon_days']: c['excess']['time_clusters'] for c in before['cohorts']}, {5: 1, 20: 1})
        old_proposal = tuple(self.store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (proposal['id'],)).fetchone())
        old_report = tuple(self.store.db.execute('SELECT * FROM selfcheck_runs WHERE id=?', (report['id'],)).fetchone())
        old_link = tuple(self.store.db.execute('SELECT * FROM selfcheck_evidence WHERE proposal_id=?', (proposal['id'],)).fetchone())
        proof = [e for e in report['evidence'] if e['id'] in payload['evidence']['ids']]
        self.assertEqual(old_link[1], digest(judgments.encode(proof)))
        self.assertEqual(json.loads(old_link[4])['signatures'], sorted(digest(judgments.encode(e)) for e in proof))

        result = selfcheck.run(self.store, self.cfg, self.at, generate=True)
        self.assertEqual(result['candidates']['updates'], [{'proposal_id': proposal['id'], 'action': 'EVIDENCE_APPENDED', 'status': 'DRAFT'}])
        after = proposal_evidence.summary(self.store, proposal, payload)
        self.assertEqual(after['status'], 'INSUFFICIENT')
        self.assertEqual(after['additional_count'], 1)
        self.assertEqual({c['horizon_days']: (c['excess']['n'], c['excess']['time_clusters']) for c in after['cohorts']}, {5: (2, 2), 20: (1, 1)})
        self.assertNotEqual(before['material_hash'], after['material_hash'])
        self.assertNotEqual(after['snapshots'][0]['link_hash'], after['snapshots'][-1]['link_hash'])
        self.assertEqual(tuple(self.store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (proposal['id'],)).fetchone()), old_proposal)
        self.assertEqual(tuple(self.store.db.execute('SELECT * FROM selfcheck_runs WHERE id=?', (report['id'],)).fetchone()), old_report)
        self.assertEqual(tuple(self.store.db.execute('SELECT * FROM selfcheck_evidence WHERE proposal_id=? AND evidence_hash=?', old_link[:2]).fetchone()), old_link)

        # A different overall report with the same applicable snapshot is not
        # new evidence; operational status and report time are not its identity.
        repeated = copy.deepcopy(content)
        repeated['execution']['reason'] = 'changed operational note'
        repeated_report = self.freeze_report(repeated, '2027-01-02T12:00:00+00:00')
        self.assertNotEqual(repeated_report['id'], result['id'])
        self.assertEqual(selfcheck.propose(self.store, repeated_report, rows, repeated_report['created_at'])['updates'], [])
        self.assertEqual(proposal_evidence.summary(self.store, proposal, payload), after)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM selfcheck_evidence WHERE proposal_id=?', (proposal['id'],)).fetchone()[0], 2)

    def test_v2_subgroup_shrink_appends_complete_snapshot_instead_of_union_of_old_ids(self):
        self.add('first', '2026-11-02', '2026-11-06', days=5)
        self.add('second', '2026-11-09', '2026-11-13', days=5)
        selfcheck.run(self.store, self.cfg, self.at, generate=True)
        proposal = governance.proposals(self.store)[0]
        before = proposal_evidence.summary(self.store, proposal, proposal['payload'])
        self.assertEqual((before['cohorts'][0]['excess']['n'], before['cohorts'][0]['excess']['time_clusters']), (2, 2))
        old_link = tuple(self.store.db.execute('SELECT * FROM selfcheck_evidence WHERE proposal_id=?', (proposal['id'],)).fetchone())
        # Another build's late-arriving score shares the first entry date and
        # extends that batch's max exit. The original build now retains one row.
        self.add('other-build', '2026-11-02', '2026-11-09', days=5, symbol='B', build='b2')
        result = selfcheck.run(self.store, self.cfg, self.at, generate=True)
        self.assertIn({'proposal_id': proposal['id'], 'action': 'EVIDENCE_APPENDED', 'status': 'DRAFT'}, result['candidates']['updates'])
        after = proposal_evidence.summary(self.store, proposal, proposal['payload'])
        self.assertEqual(after['status'], 'INSUFFICIENT')
        self.assertEqual(after['additional_count'], 1)
        self.assertEqual((after['cohorts'][0]['excess']['n'], after['cohorts'][0]['excess']['time_clusters']), (1, 1))
        self.assertEqual([s['reference_count'] for s in after['snapshots']], [2, 1])
        self.assertNotEqual(before['material_hash'], after['material_hash'])
        links = list(self.store.db.execute('SELECT * FROM selfcheck_evidence WHERE proposal_id=? ORDER BY rowid', (proposal['id'],)))
        self.assertEqual(tuple(links[0]), old_link)
        self.assertEqual(json.loads(links[1]['payload_json'])['ids'], ['first'])
        linked = json.loads(links[1]['payload_json'])
        self.assertEqual(linked['signatures'], [linked['snapshot_hash']])
        self.assertEqual(linked['previous_hash'], links[0]['evidence_hash'])
        self.assertEqual(selfcheck.run(self.store, self.cfg, self.at, generate=True)['candidates']['updates'], [])
        self.assertEqual(proposal_evidence.summary(self.store, proposal, proposal['payload']), after)

    def test_existing_counterevidence_refreshes_when_sampling_shrinks_below_trigger(self):
        for i, (entry, exit_) in enumerate([
                ('2026-11-02', '2026-11-06'), ('2026-11-09', '2026-11-13'),
                ('2026-11-16', '2026-11-20'), ('2026-11-23', '2026-11-27'),
                ('2026-11-30', '2026-12-04')]):
            self.add('old-' + str(i), entry, exit_, days=5, value=-100)
        selfcheck.run(self.store, self.cfg, self.at, generate=True)
        proposal = next(p for p in governance.proposals(self.store)
                        if p['payload']['experiment_design']['change']['id'] == 'COUNTEREVIDENCE_CHECK')
        governance.decide(self.store, proposal['id'], 'REJECTED', decided_by=None,
                          note='synthetic rejection', at=self.at)
        frozen = tuple(self.store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (proposal['id'],)).fetchone())
        before = proposal_evidence.summary(self.store, proposal, proposal['payload'])
        self.assertEqual(before['cohorts'][0]['excess']['n'], 5)

        self.add('other-build', '2026-11-02', '2026-12-04', days=5, symbol='B', build='b2')
        result = selfcheck.run(self.store, self.cfg, self.at, generate=True)
        self.assertIn({'proposal_id': proposal['id'], 'action': 'EVIDENCE_APPENDED', 'status': 'REJECTED'},
                      result['candidates']['updates'])
        after = proposal_evidence.summary(self.store, proposal, proposal['payload'])
        self.assertEqual(after['cohorts'][0]['excess']['n'], 1)
        self.assertEqual(after['additional_count'], 1)
        self.assertNotEqual(before['material_hash'], after['material_hash'])
        self.assertEqual(tuple(self.store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (proposal['id'],)).fetchone()), frozen)
        self.assertEqual(selfcheck.run(self.store, self.cfg, self.at, generate=True)['candidates']['updates'], [])

    def test_existing_counterevidence_refreshes_nonnegative_mean_without_new_proposal(self):
        for i in range(5):
            self.add('negative-' + str(i), '2026-11-02', '2026-11-06', days=5,
                     symbol='A' + str(i), value=-100)
        # Even at creation, include another applicable cohort that does not
        # trigger this hypothesis. Repeating the same report must remain a noop.
        self.add('positive-long', '2026-11-02', '2026-11-27', days=20, symbol='B', value=100)
        selfcheck.run(self.store, self.cfg, self.at, generate=True)
        proposals = governance.proposals(self.store)
        proposal = next(p for p in proposals
                        if p['payload']['experiment_design']['change']['id'] == 'COUNTEREVIDENCE_CHECK')
        frozen = tuple(self.store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (proposal['id'],)).fetchone())
        before = proposal_evidence.summary(self.store, proposal, proposal['payload'])
        self.assertEqual({c['horizon_days'] for c in before['cohorts']}, {5, 20})
        self.assertEqual(selfcheck.run(self.store, self.cfg, self.at, generate=True)['candidates']['updates'], [])

        self.add('positive-short', '2026-11-02', '2026-11-06', days=5, symbol='C', value=1000)
        result = selfcheck.run(self.store, self.cfg, self.at, generate=True)
        self.assertIn({'proposal_id': proposal['id'], 'action': 'EVIDENCE_APPENDED', 'status': 'DRAFT'},
                      result['candidates']['updates'])
        self.assertTrue(all(u['action'] == 'EVIDENCE_APPENDED' for u in result['candidates']['updates']))
        self.assertEqual(len(governance.proposals(self.store)), len(proposals))
        self.assertEqual(result['candidates']['used'], len(proposals))
        after = proposal_evidence.summary(self.store, proposal, proposal['payload'])
        short = next(c for c in after['cohorts'] if c['horizon_days'] == 5)
        self.assertEqual(short['excess']['n'], 6)
        self.assertGreaterEqual(short['excess']['mean_bps'], 0)
        self.assertNotEqual(before['material_hash'], after['material_hash'])
        self.assertEqual(tuple(self.store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (proposal['id'],)).fetchone()), frozen)
        self.assertEqual(selfcheck.run(self.store, self.cfg, self.at, generate=True)['candidates']['updates'], [])

    def test_v2_link_cannot_validate_with_legacy_proof_only_fingerprint(self):
        self.add('one', '2026-11-02', '2026-11-06', days=5)
        result = selfcheck.run(self.store, self.cfg, self.at, generate=True)
        proposal = governance.proposals(self.store)[0]
        report = selfcheck.status(self.store, result['id'])
        proof = report['evidence']
        legacy_hash = digest(judgments.encode(proof))
        legacy_signatures = sorted(digest(judgments.encode(e)) for e in proof)
        with self.store.db:
            self.store.db.execute('UPDATE selfcheck_evidence SET evidence_hash=?,payload_json=? WHERE proposal_id=?',
                (legacy_hash, judgments.encode({'ids': ['one'], 'signatures': legacy_signatures}), proposal['id']))
        self.assertEqual(proposal_evidence.summary(self.store, proposal, proposal['payload'])['status'], 'INVALID')

    def test_v2_snapshot_a_b_a_follows_current_snapshot_and_preserves_append_history(self):
        self.add('first', '2026-11-02', '2026-11-06', days=5)
        self.add('second', '2026-11-16', '2026-11-20', days=5)
        selfcheck.run(self.store, self.cfg, self.at, generate=True)
        proposal = governance.proposals(self.store)[0]
        first = proposal_evidence.summary(self.store, proposal, proposal['payload'])
        self.add('later-arrival', '2026-11-12', '2026-11-18', days=5, symbol='B', build='b2')
        selfcheck.run(self.store, self.cfg, self.at, generate=True)
        middle = proposal_evidence.summary(self.store, proposal, proposal['payload'])
        self.assertEqual(middle['cohorts'][0]['excess']['n'], 1)
        frozen = [tuple(r) for r in self.store.db.execute('SELECT * FROM selfcheck_evidence WHERE proposal_id=? ORDER BY rowid', (proposal['id'],))]

        # An earlier five-session cohort blocks the late arrival, restoring A.
        self.add('earlier-arrival', '2026-11-09', '2026-11-13', days=5, symbol='C', build='b2')
        result = selfcheck.run(self.store, self.cfg, self.at, generate=True)
        after = proposal_evidence.summary(self.store, proposal, proposal['payload'])
        self.assertEqual(after['status'], 'INSUFFICIENT')
        self.assertEqual(after['cohorts'][0]['excess']['n'], 2)
        self.assertEqual(after['additional_count'], 2)
        self.assertEqual(after['snapshots'][-1]['run_id'], result['id'])
        self.assertNotEqual(after['material_hash'], first['material_hash'])
        links = list(self.store.db.execute('SELECT * FROM selfcheck_evidence WHERE proposal_id=? ORDER BY rowid', (proposal['id'],)))
        self.assertEqual([tuple(r) for r in links[:2]], frozen)
        bodies = [json.loads(r['payload_json']) for r in links]
        self.assertEqual(bodies[0]['snapshot_hash'], bodies[2]['snapshot_hash'])
        self.assertNotEqual(bodies[0]['snapshot_hash'], bodies[1]['snapshot_hash'])
        self.assertEqual(len({r['evidence_hash'] for r in links}), 3)
        self.assertEqual([p['previous_hash'] for p in bodies], [None, links[0]['evidence_hash'], links[1]['evidence_hash']])
        self.assertEqual(selfcheck.run(self.store, self.cfg, self.at, generate=True)['candidates']['updates'], [])
        self.assertEqual(proposal_evidence.summary(self.store, proposal, proposal['payload']), after)

        # Every node, including metadata and the original mirror, is verified.
        for damage in ('head_missing', 'middle_missing', 'wrong_previous', 'changed_timestamp'):
            with self.subTest(damage=damage):
                self.store.db.execute('SAVEPOINT corrupt_link')
                try:
                    if damage in ('head_missing', 'middle_missing'):
                        victim = links[0 if damage == 'head_missing' else 1]
                        self.store.db.execute('DELETE FROM selfcheck_evidence WHERE proposal_id=? AND evidence_hash=?',
                                              (proposal['id'], victim['evidence_hash']))
                    elif damage == 'wrong_previous':
                        bad = {**bodies[2], 'previous_hash': links[0]['evidence_hash']}
                        self.store.db.execute('UPDATE selfcheck_evidence SET payload_json=? WHERE proposal_id=? AND evidence_hash=?',
                                              (judgments.encode(bad), proposal['id'], links[2]['evidence_hash']))
                    else:
                        self.store.db.execute('UPDATE selfcheck_evidence SET created_at=? WHERE proposal_id=? AND evidence_hash=?',
                                              ('2027-02-01T12:00:00+00:00', proposal['id'], links[1]['evidence_hash']))
                    self.assertEqual(proposal_evidence.summary(self.store, proposal, proposal['payload'])['status'], 'INVALID')
                finally:
                    self.store.db.execute('ROLLBACK TO corrupt_link')
                    self.store.db.execute('RELEASE corrupt_link')

    def test_v2_return_to_original_report_is_not_skipped_as_original_mirror(self):
        self.add('first', '2026-11-02', '2026-11-06', days=5)
        self.add('second', '2026-11-16', '2026-11-20', days=5)
        result = selfcheck.run(self.store, self.cfg, self.at, generate=True)
        original = selfcheck.status(self.store, result['id'])
        original_rows = selfcheck.outcomes(self.store, self.at)
        proposal = governance.proposals(self.store)[0]
        self.add('blocker', '2026-11-12', '2026-11-18', days=5, symbol='B', build='b2')
        selfcheck.run(self.store, self.cfg, self.at, generate=True)
        # A deliberately earlier caller timestamp must not reorder append nodes.
        at = '2026-12-31T12:00:00+00:00'
        selfcheck.propose(self.store, original, original_rows, at)
        after = proposal_evidence.summary(self.store, proposal, proposal['payload'])
        self.assertEqual(after['status'], 'INSUFFICIENT')
        self.assertEqual(after['cohorts'][0]['excess']['n'], 2)
        self.assertEqual(after['additional_count'], 2)
        self.assertEqual(after['snapshots'][-1]['run_id'], original['id'])
        self.assertEqual(selfcheck.propose(self.store, original, original_rows, at)['updates'], [])

    def test_v2_empty_scope_replaces_old_statistics_and_can_recover(self):
        self.add('only', '2026-11-16', '2026-11-20', days=5)
        selfcheck.run(self.store, self.cfg, self.at, generate=True)
        proposal = governance.proposals(self.store)[0]
        before = proposal_evidence.summary(self.store, proposal, proposal['payload'])
        self.add('blocker', '2026-11-12', '2026-11-18', days=5, symbol='B', build='b2')
        result = selfcheck.run(self.store, self.cfg, self.at, generate=True)
        empty = proposal_evidence.summary(self.store, proposal, proposal['payload'])
        self.assertEqual(empty['status'], 'INSUFFICIENT')
        self.assertEqual(empty['cohorts'], [])
        self.assertEqual(empty['snapshots'][-1]['scope_status'], 'EMPTY_SCOPE')
        self.assertEqual(empty['snapshots'][-1]['reference_count'], 0)
        self.assertEqual(empty['snapshots'][-1]['run_id'], result['id'])
        self.assertIn('样本为0', empty['text'])
        self.assertNotEqual(empty['material_hash'], before['material_hash'])
        self.assertEqual(selfcheck.run(self.store, self.cfg, self.at, generate=True)['candidates']['updates'], [])
        self.add('unblocker', '2026-11-09', '2026-11-13', days=5, symbol='C', build='b2')
        selfcheck.run(self.store, self.cfg, self.at, generate=True)
        after = proposal_evidence.summary(self.store, proposal, proposal['payload'])
        self.assertEqual(after['cohorts'][0]['excess']['n'], 1)
        self.assertEqual(after['snapshots'][-1]['scope_status'], 'OBSERVED')
        self.assertEqual(after['additional_count'], 2)

    def test_v2_requires_full_scope_but_v1_keeps_complete_cohort_subset_contract(self):
        self.add('short', '2026-11-02', '2026-11-06', days=5)
        self.add('long', '2026-11-02', '2026-11-27', days=20, symbol='B')
        content, _ = selfcheck.analyze(self.store, self.at)
        report = self.freeze_report(content)
        scope = {'route': 'watchlist', 'build_id': 'b1', 'environment': 'UNKNOWN'}
        for ids in ([], ['short']):
            with self.subTest(ids=ids), self.assertRaisesRegex(ValueError, '完整分组'):
                proposal_evidence._resolve(self.store, report['id'], ids, scope, allow_empty=True)
        legacy = copy.deepcopy(content)
        legacy.update(method='selfcheck-v1', lookback_days=180)
        legacy.pop('sampling_method')
        legacy.pop('history_scope')
        legacy_report = self.freeze_report(legacy)
        resolved, _, _ = proposal_evidence._resolve(self.store, legacy_report['id'], ['short'], scope)
        self.assertEqual(resolved['reference_count'], 1)
        self.assertEqual(len(resolved['cohorts']), 1)
        with self.assertRaises(ValueError):
            proposal_evidence._resolve(self.store, legacy_report['id'], [], scope, allow_empty=True)

    def test_public_diagnostic_preserves_new_sampling_and_labels_old_summaries(self):
        self.add('one', '2026-01-02', '2026-01-29')
        result = quick_diagnostics.collect(self.store, self.cfg, self.at)
        self.assertEqual(result['primary']['sampling_method'], evaluation.SAMPLING_METHOD)
        self.assertEqual(result['quick']['sampling_method'], evaluation.SAMPLING_METHOD)
        self.assertEqual(quick_diagnostics.public(result), result)
        old = copy.deepcopy(result)
        for arm in ('primary', 'quick'):
            old[arm].pop('sampling_method')
        restored = quick_diagnostics.public(old)
        self.assertEqual(restored['primary']['sampling_method'], 'legacy-unversioned')
        self.assertEqual(restored['quick']['sampling_method'], 'legacy-unversioned')
        old['primary']['sampling_method'] = 'unknown-new-method'
        self.assertEqual(quick_diagnostics.public(old)['primary']['sampling_method'], 'unknown')


if __name__ == '__main__':
    unittest.main()
