"""Report diagnostics preserve historical text and distinguish their evidence."""
import copy
import json
import unittest

import test_strategy_export as fixtures
from ashare import strategy_export as export
from ashare import strategy_condition_report as report


class ConditionReportTests(unittest.TestCase):
    setUp = fixtures.StrategyExportTests.setUp
    tearDown = fixtures.StrategyExportTests.tearDown
    insert = fixtures.StrategyExportTests.insert
    decision = fixtures.StrategyExportTests.decision
    plan = fixtures.StrategyExportTests.plan
    archive = fixtures.StrategyExportTests.archive

    def change(self, identity, fn):
        raw = json.loads(self.db.execute('SELECT payload_json FROM portfolio_decisions WHERE id=?', (identity,)).fetchone()[0])
        fn(raw)
        self.db.execute('UPDATE portfolio_decisions SET payload_json=? WHERE id=?', (json.dumps(raw), identity))
        self.db.commit()

    def test_old_history_warns_without_rewriting_or_manufacturing_hits(self):
        self.decision('old', fixtures.START, current=1450, target=1153, action='REDUCE',
                      reason='将仓位从14.53%减至11.53%。', frozen=self.plan())
        before = self.db.execute('SELECT payload_json FROM portfolio_decisions').fetchone()[0]
        _, _, archive, data = self.archive()
        result = data['coverage']['condition_audit']
        self.assertEqual(result['counts']['missing'], 1)
        self.assertEqual(result['program_hit_counts'], {})
        self.assertEqual(result['numeric_counts']['SUSPECTED_MISMATCH'], 1)
        self.assertEqual(result['numeric_findings'][0]['mode'], 'POSTHOC_DIAGNOSTIC')
        self.assertIn('疑似不一致', archive.read('交易策略变更.md').decode())
        self.assertIn('14.53%', archive.read('交易策略变更.md').decode())
        self.assertIn('不是新批准的研究规则', archive.read('总览.md').decode())
        self.assertEqual(self.db.execute('SELECT payload_json FROM portfolio_decisions').fetchone()[0], before)

    def test_list_order_and_member_changes_have_different_denominators(self):
        self.decision('before', fixtures.BEFORE)
        self.decision('reordered', fixtures.START)
        self.decision('member', fixtures.AT)
        for identity, related in [('before', ['a', 'b']), ('reordered', ['b', 'a']), ('member', ['b', 'c'])]:
            self.change(identity, lambda p, r=related: p['decisions'][0].update(related_keys=r))
        data = self.archive()[3]
        f = data['coverage']['condition_audit']['field_changes']
        self.assertEqual(f['paired_items'], 2)
        self.assertEqual(f['related_keys_list_rounds'], 2)
        self.assertEqual(f['related_keys_members_rounds'], 1)
        before = copy.deepcopy(data)
        self.assertEqual(report.summarize(data), data['coverage']['condition_audit'])
        self.assertEqual(data, before)

    def test_unknown_text_is_not_reported_as_numerically_verified(self):
        self.decision('unknown', fixtures.START, reason='利润率14.53%，仍需等待。')
        data = self.archive()[3]
        self.assertEqual(data['coverage']['condition_audit']['numeric_counts'], {'UNKNOWN': 1})
        self.assertEqual(data['coverage']['condition_audit']['numeric_findings'], [])

    def test_malformed_condition_receipt_survives_as_incomplete_without_raw_fields(self):
        from test_strategy_report_audit import StrategyReportAuditTests
        self.decision('bad', fixtures.START)
        def change(p):
            receipt = StrategyReportAuditTests.make_audit(p['decisions'][0])
            receipt['decisions'][0]['condition_audit'] = {
                'version': True, 'analysis': {'PRIVATE_ID': 'PASS'}, 'raw_input': 'PRIVATE_MARKER'}
            p['publication_audit'] = receipt
        self.change('bad', change)
        _, _, archive, data = self.archive()
        self.assertEqual(data['coverage']['condition_audit']['counts']['incomplete'], 1)
        for name in archive.namelist():
            self.assertNotIn('PRIVATE_MARKER', archive.read(name).decode())
            self.assertNotIn('PRIVATE_ID', archive.read(name).decode())


if __name__ == '__main__':
    unittest.main()
