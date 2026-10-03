"""End-to-end local forward trials, using only synthetic snapshots and answers."""
import copy
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier, Event
from unittest.mock import patch

from ashare import experiment_measurement as measurement, experiment_runner as runner
from ashare import experiments, model, research
from ashare.judgments import encode
from ashare.settings import DEFAULTS
from ashare.storage import Store, digest, normalize_time


AT = '2026-10-03T08:00:00+00:00'
SYMBOL = 'sh600000'
SYMBOLS = (SYMBOL, 'sh600001', 'sh600002', 'sz000333')


def later(stamp=AT, *, days=0, seconds=0):
    return normalize_time((datetime.fromisoformat(stamp) + timedelta(days=days, seconds=seconds)).isoformat())


class ExperimentRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cfg = {**copy.deepcopy(DEFAULTS), 'data_dir': self.tmp.name, 'deployment_role': 'research',
                    'mode': 'paper', 'model_enabled': True, 'model_timeout_seconds': 30,
                    'model_name': 'synthetic-fixed-model', 'model_reasoning_effort': 'high',
                    'watchlist': [{'symbol': s, 'name': '合成公司'} for s in SYMBOLS],
                    'live_execution_enabled': False, 'paid_api_fallback': False}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def start(self):
        prepared = runner.prepare(self.store, self.cfg, AT)
        runner.start(self.store, self.cfg, prepared['id'], '合成授权：只运行隔离实验', AT)
        return prepared

    def packet(self, identity='one', stamp=AT, symbol=SYMBOL, source=None):
        eid = 'E-' + identity
        packet = {'schema_version': '0.2', 'snapshot_id': 'SN-' + identity, 'batch_id': None,
            'symbol': symbol, 'as_of': stamp, 'stocks': [{'symbol': symbol}],
            'learning': {'new_chunk_ids': [eid]},
            'document_manifest': [],
            'evidence': [{'evidence_id': eid, 'doc_id': source or 'DOC-' + identity,
                'symbol': symbol, 'ready_at': stamp,
                'text': '合成资料：公司本季度收入增长百分之十。下一季度以公司正式披露核验。'}]}
        research.persist_snapshot(self.store, packet)
        return packet

    def enroll(self, packet, at=None, real_prompt=False):
        if real_prompt:
            return runner.enroll(self.store, self.cfg, packet, at or packet['as_of'])
        with patch('ashare.research.build_prompt', side_effect=lambda p, c:
                   '合成共同研究说明。<UNTRUSTED_PACKET>' + encode(p) + '</UNTRUSTED_PACKET>'):
            return runner.enroll(self.store, self.cfg, packet, at or packet['as_of'])

    def output(self, packet, unknown=False):
        evidence = packet['evidence'][0]
        # The literal citation is taken from the synthetic source for both arms.
        quote = evidence['text'][:min(40, len(evidence['text']))]
        result = {'summary': '合成研究', 'stocks': [{
            'symbol': packet['symbol'], 'action': 'WATCH', 'analysis': '合成资料中的增长仍需后续验证。',
            'facts': [{'evidence_id': evidence['evidence_id'], 'quote': quote}],
            'counterpoints': ['增长可能减弱'], 'missing_fields': [], 'next_checks': ['核对下期正式披露'],
            'decision': {'inclination': '继续观察', 'key_evidence': [{'evidence_id': evidence['evidence_id'], 'implication': '已披露经营资料'}],
                         'pricing': '估值尚不能判断', 'trigger': '下期披露', 'invalidation': '增长消失'},
            'dimensions': [{'id': d, 'summary': '尚待验证', 'uncertainty': '有证据缺口', 'evidence_ids': []}
                           for d in model.DIMENSIONS],
            'hypothesis_test': {'status': 'TESTABLE', 'claim': '公司下一季收入维持增长',
                'metric': '公司下一季度收入同比百分比', 'operator': '>', 'threshold': 0,
                'deadline': later(packet['as_of'], days=100), 'source_ids': [evidence['evidence_id']],
                'invalidation': '正式披露的下一季度收入同比小于或等于零'},
        }]}
        if unknown:
            result['stocks'][0]['hypothesis_test'] = {'status': 'UNKNOWN', 'claim': '', 'metric': '',
                'operator': 'UNKNOWN', 'threshold': None, 'deadline': None, 'source_ids': [], 'invalidation': ''}
        return result

    def model(self, packet, calls=None, both_unknown=False):
        def invoke(prompt, schema, folder, timeout, **kwargs):
            if calls is not None:
                calls.append({'prompt': prompt, 'schema': schema, 'folder': folder})
            candidate = measurement.CANDIDATE_INSTRUCTION in prompt
            return self.output(packet, unknown=both_unknown or not candidate)
        return invoke

    def protected_rows(self):
        tables = ('strategy_proposals', 'strategy_guidance', 'studies', 'plans', 'plan_events', 'learned_chunks',
                  'signal_registry', 'judgment_contracts', 'paper_orders', 'paper_flows', 'portfolio_decisions',
                  'cloud_state', 'cloud_outbox', 'cloud_receipts')
        return {name: [tuple(r) for r in self.store.db.execute('SELECT * FROM ' + name)] for name in tables}

    def populated_trial(self, counts=(15, 15), *, two_per_day=False, confirmation_unknown=False):
        prepared = self.start()
        for window, (first_day, count) in enumerate(zip((0, 90), counts), 1):
            for index in range(count):
                day = index // 2 if two_per_day else index
                symbol = SYMBOLS[index % 2] if two_per_day else SYMBOL
                packet = self.packet(f'endpoint-{window}-{index}', later(days=first_day + day), symbol)
                enrolled = self.enroll(packet)
                self.assertEqual(enrolled['status'], 'ENROLLED')
                result = runner.run_pair(self.store, self.cfg, enrolled['id'],
                    model_fn=self.model(packet, both_unknown=confirmation_unknown and window == 2),
                    clock=lambda: packet['as_of'])
                self.assertEqual(result['status'], 'COMPLETE')
        return prepared['id']

    def test_prepare_start_enroll_run_summary_and_close_do_not_write_production(self):
        prepared = self.start()
        packet = self.packet()
        protected = self.protected_rows()
        enrolled = self.enroll(packet)
        self.assertEqual(enrolled['status'], 'ENROLLED')
        calls = []
        result = runner.run_pair(self.store, self.cfg, enrolled['id'], model_fn=self.model(packet, calls), clock=lambda: later(seconds=1))
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(len(calls), 2)
        row = self.store.db.execute('SELECT * FROM experiment_pairs WHERE id=?', (enrolled['id'],)).fetchone()
        self.assertEqual(json.loads(row['input_json']), packet)
        self.assertEqual(row['input_hash'], digest(encode(packet)))
        self.assertEqual(row['candidate_prompt'].replace(measurement.CANDIDATE_INSTRUCTION, '', 1), row['baseline_prompt'])
        self.assertIs(calls[0]['schema'], calls[1]['schema'])
        self.assertNotEqual(calls[0]['folder'], calls[1]['folder'])
        summary = runner.summary(self.store, prepared['id'])
        self.assertEqual((summary['enrolled'], summary['complete'], summary['model_calls']), (1, 1, 2))
        self.assertEqual((summary['baseline_rate'], summary['candidate_rate']), (0, 1))
        self.assertEqual(summary['conclusion'], 'COLLECTING')
        self.assertFalse(summary['production_changes'])
        self.assertFalse(summary['profit_evidence'])
        self.assertNotIn('收入增长', encode(summary))
        runner.close(self.store, prepared['id'], 'CANCELLED', '合成测试结束', later(seconds=2))
        closed = runner.summary(self.store, prepared['id'])
        self.assertEqual(closed['status'], 'CANCELLED')
        runner.advance(self.store, self.cfg, later(days=180))
        self.assertEqual(runner.summary(self.store, prepared['id']), closed)
        self.assertEqual(self.protected_rows(), protected)
        with self.assertRaises(ValueError):
            runner.start(self.store, self.cfg, prepared['id'], '不允许重开', later(seconds=3))

    def test_real_research_prompt_and_snapshot_use_same_experimental_inputs(self):
        from ashare.demo import seed
        prepared = self.start()
        packet = seed(self.store, self.cfg, AT)
        production = research.build_prompt(packet, self.cfg)
        baseline_tables = self.protected_rows()
        enrolled = self.enroll(packet, real_prompt=True)
        self.assertEqual(enrolled['status'], 'ENROLLED')
        row = self.store.db.execute('SELECT baseline_prompt,candidate_prompt FROM experiment_pairs WHERE id=?', (enrolled['id'],)).fetchone()
        original_prefix, original_data = production.split('<UNTRUSTED_PACKET>', 1)
        baseline_prefix, baseline_data = row[0].split('<UNTRUSTED_PACKET>', 1)
        self.assertTrue(baseline_prefix.startswith(original_prefix))
        self.assertIn(measurement.COMMON_INSTRUCTION, baseline_prefix)
        self.assertEqual(baseline_data, original_data)
        self.assertEqual(row[1].replace(measurement.CANDIDATE_INSTRUCTION, '', 1), row[0])
        result = runner.run_pair(self.store, self.cfg, enrolled['id'], model_fn=self.model(packet), clock=lambda: later(seconds=1))
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(runner.summary(self.store, prepared['id'])['model_calls'], 2)
        self.assertEqual(self.protected_rows(), baseline_tables)

    def test_preparation_is_idempotent_and_activation_requires_current_pinned_build(self):
        prepared = runner.prepare(self.store, self.cfg, AT)
        self.assertEqual(runner.prepare(self.store, self.cfg, AT), prepared)
        self.assertIsNone(runner.summary(self.store, prepared['id']))
        for changed in ({**self.cfg, 'model_enabled': False}, {**self.cfg, 'model_name': None},
                        {**self.cfg, 'paper_entry_band_bps': 200}):
            with self.assertRaises(ValueError):
                runner.start(self.store, changed, prepared['id'], '合成测试', AT)
        self.assertEqual(experiments.view(self.store, prepared['id'])['status'], 'DESIGNED')

    def test_concurrent_starts_allow_only_one_active_candidate(self):
        first = runner.prepare(self.store, self.cfg, AT)
        other_cfg = {**self.cfg, 'model_name': 'another-synthetic-model'}
        second = runner.prepare(self.store, other_cfg, AT)
        barrier = Barrier(2)
        def start_one(config, identity):
            store = Store(self.tmp.name)
            try:
                barrier.wait(timeout=10)
                try:
                    return runner.start(store, config, identity, '合成并发启动测试', AT)['status']
                except ValueError:
                    return 'REJECTED_START'
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(start_one, self.cfg, first['id']), pool.submit(start_one, other_cfg, second['id'])]
            results = [future.result(timeout=20) for future in futures]
        self.assertCountEqual(results, ['RUNNING', 'REJECTED_START'])
        self.assertEqual(len(runner._running(self.store)), 1)

    def test_unknown_is_final_and_completed_pair_never_calls_again(self):
        prepared = self.start()
        packet = self.packet()
        pid = self.enroll(packet)['id']
        calls = []
        for _ in range(3):
            runner.run_pair(self.store, self.cfg, pid, model_fn=self.model(packet, calls, both_unknown=True), clock=lambda: AT)
        self.assertEqual(len(calls), 2)
        summary = runner.summary(self.store, prepared['id'])
        self.assertEqual((summary['complete'], summary['model_calls'], summary['retries']), (1, 2, 0))
        self.assertEqual((summary['baseline_rate'], summary['candidate_rate']), (0, 0))

    def test_call_failures_allow_only_two_retries_and_remain_in_denominator(self):
        prepared = self.start()
        packet = self.packet()
        pid = self.enroll(packet)['id']
        calls = []
        def fail(*args, **kwargs):
            calls.append(1)
            raise RuntimeError('PRIVATE_INPUT_MARKER')
        for _ in range(5):
            runner.run_pair(self.store, self.cfg, pid, model_fn=fail, clock=lambda: AT)
        self.assertEqual(len(calls), 6)
        counts = [tuple(r) for r in self.store.db.execute('SELECT arm,count(*) FROM experiment_attempts GROUP BY arm ORDER BY arm')]
        self.assertEqual(counts, [('baseline', 3), ('candidate', 3)])
        summary = runner.summary(self.store, prepared['id'])
        self.assertEqual((summary['enrolled'], summary['failed'], summary['model_calls'], summary['retries']), (1, 1, 6, 4))
        self.assertEqual((summary['baseline_rate'], summary['candidate_rate']), (0, 0))
        self.assertEqual({r[0] for r in self.store.db.execute('SELECT error_code FROM experiment_results')}, {'RuntimeError'})

    def test_orphan_reservation_survives_restart_and_consumes_call_budget(self):
        prepared = self.start()
        packet = self.packet()
        pid = self.enroll(packet)['id']
        pair = dict(self.store.db.execute('SELECT * FROM experiment_pairs WHERE id=?', (pid,)).fetchone())
        aid = runner._reserve(self.store, pair, 'baseline', AT)
        self.assertIsNotNone(aid)
        self.store.close()
        self.store = Store(self.tmp.name)
        calls = []
        result = runner.run_pair(self.store, self.cfg, pid, model_fn=self.model(packet, calls), clock=lambda: later(seconds=1))
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(len(calls), 2)
        interrupted = self.store.db.execute('SELECT status,error_code FROM experiment_results WHERE attempt_id=?', (aid,)).fetchone()
        self.assertEqual(tuple(interrupted), ('INTERRUPTED', 'WORKER_INTERRUPTED'))
        summary = runner.summary(self.store, prepared['id'])
        self.assertEqual((summary['model_calls'], summary['retries']), (3, 1))

    def test_cancelled_worker_never_launches_a_new_call_and_yield_is_recorded(self):
        prepared = self.start()
        packet = self.packet()
        pid = self.enroll(packet)['id']
        cancel = Event()
        cancel.set()
        runner.run_pair(self.store, self.cfg, pid, cancel, model_fn=lambda *a, **k: self.fail('Cancelled call launched'), clock=lambda: AT)
        self.assertEqual(runner.summary(self.store, prepared['id'])['model_calls'], 0)
        cancel.clear()
        def yield_call(*args, **kwargs):
            cancel.set()
            raise model.ModelYield('优先生产研究')
        runner.run_pair(self.store, self.cfg, pid, cancel, model_fn=yield_call, clock=lambda: AT)
        self.assertEqual(runner.summary(self.store, prepared['id'])['model_calls'], 1)
        self.assertEqual(self.store.db.execute('SELECT status FROM experiment_results').fetchone()[0], 'YIELDED')

    def test_historical_researched_stale_and_future_input_cannot_enroll(self):
        self.start()
        historical = self.packet('history', later(seconds=-1))
        self.assertEqual(self.enroll(historical, AT)['status'], 'OUTSIDE_WINDOW')
        stale = self.packet('stale')
        self.assertEqual(self.enroll(stale, later(seconds=61))['status'], 'NOT_LIVE_INPUT')
        researched = self.packet('researched')
        with self.store.db:
            self.store.db.execute('INSERT INTO studies VALUES(?,?,?,?,?,?)', ('prior-study', researched['snapshot_id'], SYMBOL, AT, 'SUCCEEDED', '{}'))
        self.assertEqual(self.enroll(researched)['status'], 'ALREADY_RESEARCHED')
        future = self.packet('future')
        future['evidence'][0]['ready_at'] = later(seconds=1)
        self.assertEqual(self.enroll(future)['status'], 'FUTURE_EVIDENCE')
        mismatch = self.packet('mismatch')
        mismatch['evidence'][0]['text'] += '未冻结的新增文本'
        self.assertEqual(self.enroll(mismatch)['status'], 'SNAPSHOT_MISMATCH')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM experiment_pairs').fetchone()[0], 0)

    def test_windows_embargo_and_expired_input_are_never_backfilled(self):
        prepared = self.start()
        late_first = later(days=50, seconds=-10)
        packet = self.packet('late-first', late_first)
        pid = self.enroll(packet)['id']
        runner.run_pair(self.store, self.cfg, pid, model_fn=lambda *a, **k: self.fail('Old input was called'), clock=lambda: later(days=50))
        self.assertEqual(runner.summary(self.store, prepared['id'])['failed'], 1)
        embargo = self.packet('embargo', later(days=60))
        self.assertEqual(self.enroll(embargo)['status'], 'OUTSIDE_WINDOW')
        self.assertIsNone(runner.next_pending(self.store, self.cfg, later(days=90)))
        second = self.packet('second', later(days=90))
        new_id = self.enroll(second)['id']
        self.assertEqual(self.store.db.execute('SELECT window_index FROM experiment_pairs WHERE id=?', (new_id,)).fetchone()[0], 2)
        self.assertEqual(self.enroll(packet, later(days=90))['status'], 'OUTSIDE_WINDOW')
        runner.advance(self.store, self.cfg, later(days=140))
        final = runner.summary(self.store, prepared['id'])
        self.assertEqual((final['status'], final['conclusion'], final['pending']), ('INCONCLUSIVE', 'INSUFFICIENT', 0))

    def test_input_age_limit_prevents_delayed_model_calls(self):
        prepared = self.start()
        packet = self.packet()
        pid = self.enroll(packet)['id']
        runner.run_pair(self.store, self.cfg, pid, model_fn=lambda *a, **k: self.fail('Expired input was called'), clock=lambda: later(seconds=runner.MAX_INPUT_AGE_SECONDS + 1))
        summary = runner.summary(self.store, prepared['id'])
        self.assertEqual((summary['failed'], summary['model_calls']), (1, 0))

    def test_build_drift_stops_trial_and_preserves_all_enrollment(self):
        prepared = self.start()
        packet = self.packet()
        pid = self.enroll(packet)['id']
        changed = {**self.cfg, 'paper_entry_band_bps': self.cfg['paper_entry_band_bps'] + 1}
        result = runner.run_pair(self.store, changed, pid, model_fn=lambda *a, **k: self.fail('Changed implementation called'), clock=lambda: AT)
        self.assertEqual(result['status'], 'IMPLEMENTATION_CHANGED')
        summary = runner.summary(self.store, prepared['id'])
        self.assertEqual((summary['status'], summary['stop_reason']), ('INCONCLUSIVE', 'IMPLEMENTATION_CHANGED'))
        self.assertEqual((summary['enrolled'], summary['failed'], summary['model_calls']), (1, 1, 0))
        self.assertEqual(experiments.view(self.store, prepared['id'])['status'], 'INCONCLUSIVE')

    def test_daily_limit_and_source_event_dedup_preserve_first_enrollment(self):
        self.start()
        one = self.packet('one', source='shared-source')
        self.assertEqual(self.enroll(one)['status'], 'ENROLLED')
        duplicate = self.packet('duplicate', symbol=SYMBOLS[1], source='shared-source')
        self.assertEqual(self.enroll(duplicate)['status'], 'NO_NEW_EVENT')
        two = self.packet('two', symbol=SYMBOLS[1])
        self.assertEqual(self.enroll(two)['status'], 'ENROLLED')
        three = self.packet('three', symbol=SYMBOLS[2])
        self.assertEqual(self.enroll(three)['status'], 'BUDGET_LIMIT')

    def test_cloud_and_paid_or_live_execution_are_rejected(self):
        prepared = self.start()
        packet = self.packet()
        pid = self.enroll(packet)['id']
        for changed in ({**self.cfg, 'deployment_role': 'cloud'}, {**self.cfg, 'paid_api_fallback': True},
                        {**self.cfg, 'live_execution_enabled': True}):
            calls = (lambda: runner.prepare(self.store, changed, AT),
                     lambda: runner.start(self.store, changed, prepared['id'], '不可运行', AT),
                     lambda: runner.enroll(self.store, changed, packet, AT),
                     lambda: runner.advance(self.store, changed, AT),
                     lambda: runner.next_pending(self.store, changed, AT),
                     lambda: runner.run_pair(self.store, changed, pid, model_fn=self.model(packet), clock=lambda: AT))
            for call in calls:
                with self.assertRaises(ValueError):
                    call()
        self.assertEqual(runner.summary(self.store, prepared['id'])['model_calls'], 0)

    def test_new_target_excludes_prior_event_and_old_reference_cannot_score(self):
        prepared = self.start()
        original = self.packet('original', source='prior-document')
        self.enroll(original)
        second = copy.deepcopy(original)
        second.update(snapshot_id='SN-new-target', as_of=later(days=90))
        second['evidence'].append({'evidence_id': 'E-new', 'doc_id': 'new-document', 'symbol': SYMBOL,
            'ready_at': second['as_of'], 'text': '新合成披露：下一季度收入仍需正式核验。'})
        second['learning']['new_chunk_ids'] = ['E-original', 'E-new']
        research.persist_snapshot(self.store, second)
        enrolled = self.enroll(second)
        self.assertEqual(enrolled['status'], 'ENROLLED')
        row = self.store.db.execute('SELECT target_json FROM experiment_pairs WHERE id=?', (enrolled['id'],)).fetchone()
        self.assertEqual(json.loads(row[0]), ['E-new'])
        # The valid old citation remains usable as context, but cannot validate
        # the new target that defines this experimental observation.
        calls = []
        result = runner.run_pair(self.store, self.cfg, enrolled['id'], model_fn=self.model(second, calls), clock=lambda: second['as_of'])
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(len(calls), 2)
        candidate = self.store.db.execute("SELECT r.measurement_json FROM experiment_results r JOIN experiment_attempts a ON a.id=r.attempt_id WHERE a.pair_id=? AND a.arm='candidate'", (enrolled['id'],)).fetchone()
        stats = json.loads(candidate[0])
        self.assertEqual((stats['status'], stats['verifiable']), ('OFF_TARGET', 0))
        self.assertEqual(runner.summary(self.store, prepared['id'])['candidate_rate'], 0)

    def test_revision_of_previously_enrolled_document_family_is_not_new_event(self):
        self.start()
        shared = dict(symbol=SYMBOL, kind='financial_data', title='合成披露', source='SYNTHETIC',
                      url='https://example.test/same-disclosure', published_at=AT, raw_path='synthetic', cloud_allowed=True)
        first, _ = self.store.add_document(**shared, first_seen_at=AT, ready_at=AT, pages=[(1, '合成公司第一版披露内容。')])
        self.assertEqual(self.enroll(self.packet('first-version', source=first))['status'], 'ENROLLED')
        next_day = later(days=1)
        revision, _ = self.store.add_document(**shared, first_seen_at=next_day, ready_at=next_day, pages=[(1, '合成公司第二版修订内容。')])
        self.assertNotEqual(first, revision)
        revised_packet = self.packet('revised-version', next_day, source=revision)
        self.assertEqual(self.enroll(revised_packet)['status'], 'NO_NEW_EVENT')

    def test_each_window_has_sixty_calls_and_total_budget_is_one_hundred_twenty(self):
        prepared = self.start()
        for window, first_day in ((1, 0), (2, 90)):
            for index in range(30):
                packet = self.packet(f'w{window}-{index}', later(days=first_day + index))
                enrolled = self.enroll(packet)
                self.assertEqual(enrolled['status'], 'ENROLLED')
                result = runner.run_pair(self.store, self.cfg, enrolled['id'], model_fn=self.model(packet), clock=lambda: packet['as_of'])
                self.assertEqual(result['status'], 'COMPLETE')
            extra = self.packet(f'w{window}-extra', later(days=first_day + 31))
            self.assertEqual(self.enroll(extra)['status'], 'BUDGET_LIMIT')
            self.assertEqual(runner.summary(self.store, prepared['id'])['model_calls'], window * 60)
        runner.advance(self.store, self.cfg, later(days=140))
        summary = runner.summary(self.store, prepared['id'])
        self.assertEqual((summary['status'], summary['model_calls'], summary['complete'], summary['retries']),
                         ('COMPLETED', 120, 60, 0))
        self.assertEqual(summary['conclusion'], 'DESCRIPTIVE_ONLY')
        self.assertEqual(summary['assessment'], 'STRUCTURE_IMPROVEMENT_ONLY')
        self.assertFalse(summary['profit_evidence'])

    def test_thirty_real_pairs_wait_for_fixed_endpoint_and_report_structure_only(self):
        identity = self.populated_trial()
        runner.advance(self.store, self.cfg, later(days=140, seconds=-1))
        before = runner.summary(self.store, identity)
        self.assertEqual((before['status'], before['assessment'], before['complete'], before['day_clusters']),
                         ('RUNNING', 'PENDING', 30, 30))
        runner.advance(self.store, self.cfg, later(days=140))
        final = runner.summary(self.store, identity)
        self.assertEqual((final['status'], final['assessment']), ('COMPLETED', 'STRUCTURE_IMPROVEMENT_ONLY'))
        self.assertEqual([w['complete'] for w in final['window_results']], [15, 15])
        self.assertEqual((final['model_calls'], final['complete'], final['day_clusters']), (60, 30, 30))
        self.assertFalse(final['profit_evidence'])
        self.assertFalse(final['production_changes'])
        self.assertEqual(self.store.db.execute('SELECT status FROM strategy_proposals').fetchone()[0], 'DRAFT')

    def test_confirmation_window_not_improved_is_unsupported_despite_positive_total(self):
        identity = self.populated_trial(confirmation_unknown=True)
        runner.advance(self.store, self.cfg, later(days=140))
        final = runner.summary(self.store, identity)
        self.assertEqual((final['status'], final['assessment']), ('COMPLETED', 'NOT_SUPPORTED'))
        self.assertGreater(final['paired_delta'], 0)
        self.assertEqual(final['window_results'][1]['paired_delta'], 0)

    def test_thirty_pairs_across_too_few_day_clusters_remain_inconclusive(self):
        identity = self.populated_trial(two_per_day=True)
        runner.advance(self.store, self.cfg, later(days=140))
        final = runner.summary(self.store, identity)
        self.assertEqual((final['complete'], final['day_clusters']), (30, 16))
        self.assertEqual((final['status'], final['assessment']), ('INCONCLUSIVE', 'INSUFFICIENT'))

    def test_thirty_pairs_cannot_substitute_for_fifteen_in_each_window(self):
        identity = self.populated_trial(counts=(16, 14))
        runner.advance(self.store, self.cfg, later(days=140))
        final = runner.summary(self.store, identity)
        self.assertEqual((final['complete'], final['day_clusters']), (30, 30))
        self.assertEqual([w['complete'] for w in final['window_results']], [16, 14])
        self.assertEqual((final['status'], final['assessment']), ('INCONCLUSIVE', 'INSUFFICIENT'))

    def test_fewer_than_thirty_completed_pairs_remain_inconclusive(self):
        identity = self.populated_trial(counts=(15, 14))
        runner.advance(self.store, self.cfg, later(days=140))
        final = runner.summary(self.store, identity)
        self.assertEqual(final['complete'], 29)
        self.assertEqual((final['status'], final['assessment']), ('INCONCLUSIVE', 'INSUFFICIENT'))

    def test_invalid_nonfinite_output_is_terminal_and_does_not_strand_reservation(self):
        prepared = self.start()
        packet = self.packet()
        pid = self.enroll(packet)['id']
        malformed = self.output(packet)
        malformed['stocks'][0]['hypothesis_test']['threshold'] = float('nan')
        calls = []
        def fail_format(*args, **kwargs):
            calls.append(1)
            return copy.deepcopy(malformed)
        for _ in range(2):
            result = runner.run_pair(self.store, self.cfg, pid, model_fn=fail_format, clock=lambda: AT)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(len(calls), 2)
        self.assertEqual(runner.summary(self.store, prepared['id'])['failed'], 1)
        rows = [tuple(r) for r in self.store.db.execute('SELECT status,error_code,result_json FROM experiment_results')]
        self.assertEqual(rows, [('INVALID', 'NON_JSON_OUTPUT', '{}')] * 2)

    def test_actual_model_identity_mismatch_is_not_counted_as_valid_result(self):
        prepared = self.start()
        packet = self.packet()
        pid = self.enroll(packet)['id']
        with patch('ashare.model.pinned', return_value={'name': self.cfg['model_name'], 'effort': self.cfg['model_reasoning_effort']}), \
             patch('ashare.model.run_json', side_effect=self.model(packet)) as call, \
             patch('ashare.model.call_meta', return_value={'actual_model': 'wrong-model', 'actual_effort': 'high'}):
            result = runner.run_pair(self.store, self.cfg, pid, clock=lambda: AT)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(call.call_count, 2)
        self.assertEqual({r[0] for r in self.store.db.execute('SELECT error_code FROM experiment_results')}, {'MODEL_IDENTITY_MISMATCH'})
        summary = runner.summary(self.store, prepared['id'])
        self.assertEqual((summary['baseline_rate'], summary['candidate_rate'], summary['failed']), (0, 0, 1))

    def test_experiment_audit_rows_are_append_only(self):
        prepared = self.start()
        packet = self.packet()
        pid = self.enroll(packet)['id']
        runner.run_pair(self.store, self.cfg, pid, model_fn=self.model(packet), clock=lambda: AT)
        runner.close(self.store, prepared['id'], 'CANCELLED', '合成结束', AT)
        for table in ('experiment_runs', 'experiment_pairs', 'experiment_attempts', 'experiment_results', 'experiment_conclusions'):
            with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                with self.store.db:
                    self.store.db.execute('DELETE FROM ' + table)


if __name__ == '__main__':
    unittest.main()
