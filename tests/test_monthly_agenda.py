import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ashare import digest, governance, monthly_agenda
import test_workflow as workflow_fixtures


class MonthlyAgendaTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE strategy_proposals(id,kind,title,status,created_at)')
        self.store = SimpleNamespace(db=self.db)

    def tearDown(self):
        self.db.close()

    def test_first_saturday_eve_handles_month_year_and_leap_boundaries(self):
        expected = {'2026-11-06': None, '2026-12-03': None, '2026-12-04': '2026-12-05',
                    '2026-12-05': None, '2027-01-01': '2027-01-02', '2027-04-30': '2027-05-01',
                    '2028-03-03': '2028-03-04', '2028-09-30': None, '2027-12-31': '2028-01-01'}
        for day, answer in expected.items():
            value = monthly_agenda.decision_day(day)
            self.assertEqual(value.isoformat() if value else None, answer, day)

    def test_current_queue_is_complete_and_read_only(self):
        rows = [('r', 'RULE', '待审', 'READY', '2026-11-01T00:00:00+00:00'),
                ('a', 'RULE', '实施', 'APPROVED', '2026-11-01T00:00:00+00:00'),
                ('d', 'OTHER', '补全', 'DRAFT', '2026-11-01T00:00:00+00:00'),
                ('x', 'RULE', '历史', 'SUPERSEDED', '2026-11-01T00:00:00+00:00'),
                ('f', 'RULE', '将来', 'READY', '2026-12-05T00:00:00+00:00')]
        self.db.executemany('INSERT INTO strategy_proposals VALUES(?,?,?,?,?)', rows)
        before = self.db.total_changes
        agenda = monthly_agenda.collect(self.store, '2026-12-04', '2026-12-04T15:50:00+00:00')
        self.assertEqual([r['id'] for r in agenda['items']], ['r'])
        self.assertEqual(agenda['counts'], {'READY': 1, 'APPROVED': 1, 'DRAFT': 1})
        self.assertEqual(self.db.total_changes, before)
        self.assertIn('2026-12-05', '\n'.join(monthly_agenda.markdown(agenda)))

    def test_historical_report_does_not_backfill_current_queue(self):
        agenda = monthly_agenda.collect(self.store, '2026-12-04', '2026-12-05T00:00:00+00:00')
        self.assertEqual(agenda['status'], 'HISTORICAL_QUEUE_UNKNOWN')
        self.assertIsNone(agenda['items'])
        self.assertIn('无法', '\n'.join(monthly_agenda.markdown(agenda)))

    def test_failure_is_unknown_not_empty_queue(self):
        self.db.execute('DROP TABLE strategy_proposals')
        agenda = monthly_agenda.collect(self.store, '2026-12-04', '2026-12-04T15:50:00+00:00')
        rendered = '\n'.join(monthly_agenda.markdown(agenda))
        self.assertEqual(agenda['status'], 'UNAVAILABLE')
        self.assertIn('数量未知', rendered)
        self.assertNotIn('当前没有', rendered)

    def test_title_cannot_break_table_or_inject_html(self):
        agenda = {'status': 'OK', 'decision_day': '2026-12-05', 'items': [
            {'id': 'x', 'kind': 'RULE', 'status': 'READY', 'title': '<script>|x\ny'}]}
        rendered = '\n'.join(monthly_agenda.markdown(agenda))
        self.assertNotIn('<script>', rendered)
        self.assertIn('&lt;script&gt;\\|x y', rendered)


class MonthlyDigestIntegrationTests(unittest.TestCase):
    setUp = workflow_fixtures.WorkflowTests.setUp
    tearDown = workflow_fixtures.WorkflowTests.tearDown

    def test_agenda_precedes_normal_attention_section_without_model(self):
        with self.store.db:
            pid = governance.draft_proposal(self.store, source='agent', kind='RULE', target='test',
                title='月度待决事项', payload={}, at='2026-11-07T00:00:00+00:00')
        governance.decide(self.store, pid, 'READY', note='合成测试待决项', at='2026-11-07T01:00:00+00:00')
        with patch('ashare.digest.now', return_value='2026-12-04T15:50:00+00:00'), \
                patch('ashare.model.run_json', side_effect=AssertionError('No model allowed')):
            d = digest.build(self.store, self.cfg, '2026-12-04')
            text = digest.markdown(d)
        self.assertLess(text.index('## 明日月度决策'), text.index('## 需要关注'))
        self.assertIn(pid, text)
        self.assertIn('2026-12-05', text)


if __name__ == '__main__':
    unittest.main()
