import json
import copy
import tempfile
import unittest
from unittest.mock import patch

from ashare import digest
from ashare.settings import DEFAULTS
from ashare.storage import Store, normalize_time


DAY = '2026-09-15'


def stamp(clock, day=DAY):
    return normalize_time(f'{day}T{clock}:00+08:00')


class ResearchPerformanceDigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cfg = {**copy.deepcopy(DEFAULTS), 'data_dir': self.tmp.name, 'watchlist': []}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def job(self, key, scheduled, started, finished, status, kind='portfolio_strategy'):
        with self.store.db:
            self.store.db.execute('INSERT INTO jobs(id,kind,scheduled_at,started_at,finished_at,status,attempts) '
                                  'VALUES(?,?,?,?,?,?,1)', (key, kind, scheduled, started, finished, status))

    def step(self, key, status, payload, at=None, domain='AI'):
        with self.store.db:
            self.store.db.execute('INSERT INTO industry_steps VALUES(?,?,?,?,?)',
                                  (key, domain, status, at or stamp('10:00'), json.dumps(payload)))

    def build(self, cutoff='12:00'):
        with patch('ashare.digest.now', return_value=stamp(cutoff)):
            return digest.build(self.store, self.cfg, DAY)

    def test_queue_completed_and_ongoing_durations_have_separate_denominators(self):
        self.job('complete', stamp('09:00'), stamp('09:30'), stamp('09:32'), 'DONE')
        self.job('running', stamp('10:00'), stamp('10:05'), None, 'RUNNING')
        self.job('pending', stamp('11:00'), None, None, 'PENDING')
        self.job('future-finish', stamp('11:00'), stamp('11:10'), stamp('13:10'), 'DONE')
        self.job('future-start', stamp('11:30'), stamp('12:30'), stamp('12:35'), 'DONE')
        self.job('future-schedule', stamp('13:00'), None, None, 'PENDING')
        self.job('terminal-without-finish', stamp('09:00'), stamp('09:05'), None, 'FAILED')
        self.job('stale-finish-on-running', stamp('09:00'), stamp('09:05'), stamp('09:10'), 'RUNNING')
        result = self.build()['jobs']
        timing = result['timing_by_kind']['portfolio_strategy']
        self.assertEqual(result['as_of'], stamp('12:00'))
        self.assertEqual((timing['queue_count'], timing['queue_max_minutes']), (5, 30))
        self.assertEqual((timing['completed_count'], timing['execution_max_minutes']), (1, 2))
        self.assertEqual((timing['running_count'], timing['running_max_minutes']), (2, 115))
        self.assertEqual((timing['pending_count'], timing['pending_max_minutes']), (2, 60))
        self.assertEqual(result['longest_minutes'], {'portfolio_strategy': 2})
        text = digest.markdown(self.build())
        self.assertIn('已开始排队：次数 / 最长分钟', text)
        self.assertIn('已结束执行：次数 / 最长分钟', text)
        self.assertIn('存储状态，历史状态无法回放', text)
        self.assertIn('组合决策截至时点未开始 2 次', text)
        self.assertIn('组合决策截至时点未结束 2 次', text)

    def test_historical_day_keeps_scheduled_window_and_caps_durations_at_midnight(self):
        self.job('late-finish', stamp('23:00'), stamp('23:10'), stamp('02:00', '2026-09-16'), 'DONE', 'cycle')
        self.job('previous-schedule', stamp('23:30', '2026-09-14'), stamp('00:10'), stamp('00:15'), 'DONE')
        self.job('next-schedule', stamp('00:00', '2026-09-16'), stamp('00:30', '2026-09-16'), None, 'RUNNING')
        with patch('ashare.digest.now', return_value=stamp('12:00', '2026-09-16')):
            result = digest.jobs(self.store, *digest.day_window(DAY))
        self.assertEqual(result['as_of'], stamp('00:00', '2026-09-16'))
        self.assertEqual(result['by_kind'], {'cycle': {'DONE': 1}})
        self.assertEqual(result['timing_by_kind']['cycle']['running_max_minutes'], 50)
        self.assertEqual(result['timing_by_kind']['cycle']['completed_count'], 0)
        self.assertEqual(result['longest_minutes'], {})
        self.assertEqual(result['long_cycles'], [])

    def test_internal_errors_remain_visible_under_done_job_and_partial_results_are_counted(self):
        self.job('outer', stamp('09:00'), stamp('09:00'), stamp('11:00'), 'DONE', 'research')
        self.step('old-partial', 'DEFERRED', {'hypotheses': ['h1', 'h2'],
                  'rejected': ['证据条数错误 private-evidence'], 'methods': {'1': 'private-method'}})
        self.step('new-complete', 'DONE', {'status': 'COMPLETE', 'hypotheses': ['h1'], 'rejected': [],
                  'research_gaps': [{'missing': 'private-gap', 'next_check': 'private-plan'}], 'failure_kind': ''})
        self.step('new-partial', 'DEFERRED', {'status': 'PARTIAL', 'hypotheses': ['h2'], 'rejected': [],
                  'failure_kind': 'MODEL_TIMEOUT', 'reason': 'private-timeout'})
        self.step('old-timeout', 'DEFERRED', {'reason': 'TimeoutError: timed out private-prompt'})
        self.step('new-validation', 'DEFERRED', {'status': 'VALIDATION_FAILED', 'hypotheses': [],
                  'rejected': ['引用超出本轮快照 private-reference'], 'failure_kind': 'VALIDATION'})
        self.step('future', 'DEFERRED', {'reason': 'timed out'}, at=stamp('13:00'))
        result = self.build()
        summary = result['research']['industry']
        self.assertEqual(summary['steps'], 5)
        self.assertEqual(summary['by_status'], {'DEFERRED': 4, 'DONE': 1})
        self.assertEqual((summary['hypothesis_references'], summary['distinct_hypotheses']), (4, 2))
        self.assertEqual((summary['partial_steps'], summary['research_gap_count'], summary['rejected_count']), (2, 1, 2))
        self.assertEqual(summary['failure_kinds'], {'VALIDATION': 2, 'MODEL_TIMEOUT': 2})
        self.assertEqual(summary['rejection_kinds'], {'FACT_COUNT': 1, 'REFERENCE': 1})
        self.assertEqual(summary['technical_failure_steps'], 4)
        self.assertEqual(result['jobs']['by_kind']['research'], {'DONE': 1})
        self.assertIn('行业内部步骤有技术错误 4 次', '\n'.join(result['flags']))
        self.assertIn('2 次保留了部分成果', '\n'.join(result['flags']))
        text = digest.markdown(result)
        self.assertIn('行业假设引用 4 条，去重 2 条', text)
        self.assertIn('证据条数 1', text)
        self.assertNotIn('private-', json.dumps(summary) + text)

    def test_expected_evidence_gaps_and_resource_deferrals_are_not_technical_failures(self):
        self.step('no-evidence', 'DONE', {'status': 'NO_EVIDENCE'})
        self.step('no-findings', 'DONE', {'status': 'NO_FINDINGS', 'hypotheses': []})
        self.step('gaps', 'DONE', {'status': 'EVIDENCE_GAPS', 'research_gaps': [{'missing': ['x']}]})
        self.step('reused', 'DONE', {'status': 'COMPLETE', 'hypotheses': ['h1'], 'reused': True})
        self.step('budget', 'DEFERRED', {'reason': '模型未启用或预算不足'})
        self.step('stopped', 'DEFERRED', {'failure_kind': 'STOPPED'})
        self.step('disabled', 'DEFERRED', {'failure_kind': 'MODEL_DISABLED'})
        result = self.build()
        summary = result['research']['industry']
        self.assertEqual(summary['technical_failure_steps'], 0)
        self.assertEqual(summary['no_evidence_steps'], 1)
        self.assertEqual(summary['research_gap_count'], 1)
        self.assertEqual(summary['reused_steps'], 1)
        self.assertEqual(summary['failure_kinds'], {'MODEL_DISABLED_OR_BUDGET': 1, 'STOPPED': 1, 'MODEL_DISABLED': 1})
        self.assertNotIn('行业内部步骤有技术错误', '\n'.join(result['flags']))
        self.assertIn('资料缺口不计技术错误', digest.markdown(result))

    def test_industry_day_boundary_and_missing_legacy_table(self):
        self.step('before', 'DONE', {}, at=stamp('23:59', '2026-09-14'))
        self.step('inside', 'DONE', {}, at=stamp('00:00'))
        self.step('after', 'DONE', {}, at=stamp('00:00', '2026-09-16'))
        with patch('ashare.digest.now', return_value=stamp('12:00', '2026-09-16')):
            summary = digest.industry_research(self.store, *digest.day_window(DAY))
        self.assertEqual(summary['steps'], 1)
        with self.store.db:
            self.store.db.execute('DROP TABLE industry_steps')
        result = self.build()
        self.assertFalse(result['research']['industry']['available'])
        self.assertNotIn('error', result['research'])
        self.assertIn('历史数据库没有行业步骤表', digest.markdown(result))


if __name__ == '__main__':
    unittest.main()
