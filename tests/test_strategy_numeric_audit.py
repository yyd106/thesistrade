"""Numbers are checked against recorded stages, not guessed trading intent."""
import copy
import json
import unittest

from ashare.strategy_numeric_audit import check


class StrategyNumericAuditTests(unittest.TestCase):
    @staticmethod
    def item(reason, current=1450, target=1153, action='REDUCE'):
        return {'key': 'watchlist:test', 'reason': reason, 'current_bps': current,
                'target_bps': target, 'action': action}

    @staticmethod
    def receipt(item, analysis_current=1453, model_target=1153, model_action='REDUCE', codes=None):
        def basis(current):
            return {'current_bps': current, 'current_value_cents': current * 1000,
                    'qty': 100, 'cost_cents': 100000, 'max_bps': 2000, 'can_increase': True,
                    'source_id': 'plan', 'source_valid_until': '2026-10-26T00:00:00+00:00',
                    'source_kind': 'PAPER_TRADE', 'recorded_blockers': []}
        row = {'key': item['key'], 'model_action': model_action, 'model_target_bps': model_target,
               'analysis_basis': basis(analysis_current), 'published_action': item['action'],
               'published_target_bps': item['target_bps'], 'publication_basis': basis(item['current_bps']),
               'adjustment_codes': [] if codes is None else codes}
        account = {'cash_cents': 5000000, 'equity_cents': 10000000, 'available_cents': 5000000,
                   'reserved_cents': 0, 'withdrawal_reserved_cents': 0, 'market_value_cents': 5000000}
        constraints = {'valuation_ready': True, 'halted': False, 'gross_cap_bps': 9000, 'symbol_cap_bps': 2000}
        audit = {'version': 1, 'target_method': 'MODEL_DISCRETION_WITH_HARD_CONSTRAINTS',
                 'analysis_as_of': '2026-10-25T01:00:00+00:00', 'published_at': '2026-10-25T01:01:00+00:00',
                 'analysis_account': account, 'publication_account': dict(account),
                 'analysis_constraints': constraints, 'publication_constraints': dict(constraints),
                 'validation': {'analysis': 'PASSED', 'publication': 'PASSED'}, 'decisions': [row]}
        return audit, row

    def test_legacy_from_to_is_only_suspected_and_preserves_the_original(self):
        item = self.item('将仓位从14.53%减至11.53%。')
        before = copy.deepcopy(item)
        result = check(item)
        self.assertEqual(result['mode'], 'POSTHOC_DIAGNOSTIC')
        self.assertEqual(result['basis'], 'FINAL_ONLY')
        self.assertEqual(result['status'], 'SUSPECTED_MISMATCH')
        self.assertEqual([c['status'] for c in result['comparisons']], ['SUSPECTED_MISMATCH', 'CONSISTENT'])
        self.assertEqual([c['role'] for c in result['comparisons']], ['CURRENT', 'TARGET'])
        self.assertEqual(result['comparisons'][0]['ref_role'], 'FINAL_CURRENT')
        self.assertEqual(item, before)

    def test_model_analysis_stage_not_publication_stage_checks_original_reason(self):
        item = self.item('当前仓位14.53%，目标仓位11.53%。')
        audit, row = self.receipt(item)
        before = copy.deepcopy((item, audit, row))
        result = check(item, audit, row)
        self.assertEqual(result['status'], 'CONSISTENT')
        self.assertEqual(result['basis'], 'MODEL_ANALYSIS')
        first = result['comparisons'][0]
        self.assertEqual((first['reference_bps'], first['final_bps'], first['final_matches']), (1453, 1450, False))
        self.assertEqual(first['ref_role'], 'ANALYSIS_CURRENT')
        self.assertEqual(result['comparisons'][1]['ref_role'], 'MODEL_TARGET')
        self.assertEqual((item, audit, row), before)
        json.dumps(result, allow_nan=False)

    def test_exact_model_stage_difference_is_numeric_inconsistency(self):
        item = self.item('现有仓位14.50%，目标11.53%。')
        audit, row = self.receipt(item)
        result = check(item, audit, row)
        self.assertEqual(result['status'], 'INCONSISTENT')
        self.assertEqual(result['comparisons'][0]['status'], 'INCONSISTENT')
        self.assertTrue(result['comparisons'][0]['final_matches'])

    def test_display_precision_rounds_with_decimal_not_a_fixed_epsilon(self):
        cases = [('当前仓位14.53%', 1450, 'SUSPECTED_MISMATCH', '0.50'),
                 ('当前仓位14.5%', 1454, 'CONSISTENT', '5.0'),
                 ('当前仓位14.5%', 1455, 'SUSPECTED_MISMATCH', '5.0'),
                 ('目标8%', 849, 'CONSISTENT', '50'),
                 ('目标8%', 850, 'SUSPECTED_MISMATCH', '50'),
                 ('目标8.690%', 869, 'CONSISTENT', '0.050')]
        for reason, value, status, tolerance in cases:
            with self.subTest(reason=reason, value=value):
                result = check(self.item(reason, current=value, target=value))
                self.assertEqual(result['status'], status)
                self.assertEqual(result['comparisons'][0]['tolerance_bps'], tolerance)

    def test_hold_rebase_requires_matching_recorded_stages_and_adjustment(self):
        item = self.item('当前仓位8.69%，保持8.69%。', current=868, target=868, action='HOLD')
        audit, row = self.receipt(item, analysis_current=869, model_target=869, model_action='HOLD',
                                  codes=['HOLD_PAUSE_CURRENT_REBASE'])
        result = check(item, audit, row)
        self.assertEqual(result['status'], 'EXPLAINED_REBASE')
        self.assertEqual([c['status'] for c in result['comparisons']], ['CONSISTENT', 'EXPLAINED_REBASE'])
        row['adjustment_codes'] = []
        result = check(item, audit, row)
        self.assertEqual(result['status'], 'CONSISTENT')
        self.assertFalse(result['comparisons'][1]['final_matches'])
        self.assertEqual(check(item)['status'], 'SUSPECTED_MISMATCH')

    def test_reduce_to_hold_is_not_mislabeled_as_same_action_rebase(self):
        item = self.item('目标8.00%。', current=868, target=868, action='HOLD')
        audit, row = self.receipt(item, analysis_current=900, model_target=800, model_action='REDUCE',
                                  codes=['REDUCE_MINIMUM_GAP_LOST', 'HOLD_PAUSE_CURRENT_REBASE'])
        result = check(item, audit, row)
        self.assertEqual(result['status'], 'CONSISTENT')
        self.assertFalse(result['comparisons'][0]['final_matches'])

    def test_percentage_point_reduction_is_distinct_from_relative_percentage(self):
        item = self.item('从14.53%减至11.53%，减仓3个百分点。')
        audit, row = self.receipt(item)
        result = check(item, audit, row)
        self.assertEqual(result['status'], 'CONSISTENT')
        delta = result['comparisons'][-1]
        self.assertEqual((delta['role'], delta['unit'], delta['reference_bps']), ('REDUCTION_PP', 'PERCENTAGE_POINTS', 300))
        self.assertEqual(delta['ref_role'], 'ANALYSIS_REDUCTION')
        self.assertEqual(check(self.item('减仓3%。'))['status'], 'UNKNOWN')
        self.assertEqual(check(self.item('将仓位减少3.00个百分点。'))['comparisons'][0]['role'], 'REDUCTION_PP')

    def test_non_position_percentages_are_not_position_assertions(self):
        examples = ['当前毛利率14.53%，净利率目标11.53%。', '收益率目标8%。',
                    '股价从14.53%降至11.53%。', '风险上限20%，单股上限20%。',
                    '置信度从85%降至80%。', '订单转化率从14%降至12%。',
                    '毛利润同比从14%降至12%。', '成功率目标80%。',
                    '订单转化率保持14%。', '利润增长目标8%。',
                    '总仓位目标90%。', '现金占比目标20%。', '价格目标8%。']
        for reason in examples:
            with self.subTest(reason=reason):
                result = check(self.item(reason))
                self.assertEqual(result['status'], 'UNKNOWN')
                self.assertEqual(result['comparisons'], [])

    def test_ranges_bounds_hypotheses_negations_and_past_values_remain_unknown(self):
        examples = ['目标仓位8%-10%。', '目标8%至10%。', '目标仓位8%以内。',
                    '目标仓位8%左右。', '目标8%或10%。', '目标仓位不超过8%。',
                    '假设目标仓位8%。', '如果行情回落，目标仓位8%。', '若仓位从14%减至8%。',
                    '不保持8%。', '不将仓位从14%减至8%。', '此前当前仓位14%。',
                    '上轮目标8%。', '预计当前仓位8%。', '例如目标8%。',
                    '目标8%并非本次决定。', '当前仓位14%是错误说法。',
                    '从14%减至8%只是示例。', '目标8%仅在条件满足后执行。']
        for reason in examples:
            with self.subTest(reason=reason):
                self.assertEqual(check(self.item(reason))['comparisons'], [])

    def test_separate_sentence_allows_explicit_current_statement_after_hypothesis(self):
        result = check(self.item('假设目标8%。当前仓位14.50%，目标11.53%。'))
        self.assertEqual(result['status'], 'CONSISTENT')
        self.assertEqual(len(result['comparisons']), 2)
        self.assertEqual(check(self.item('保持8%不变。', current=800, target=800))['status'], 'CONSISTENT')

    def test_same_mention_is_not_counted_as_both_current_and_target(self):
        result = check(self.item('保持当前仓位8.69%。', current=869, target=869, action='HOLD'))
        self.assertEqual(result['status'], 'CONSISTENT')
        self.assertEqual([c['role'] for c in result['comparisons']], ['TARGET'])

    def test_full_width_percent_and_exact_spans_remain_json_safe(self):
        item = self.item('当前仓位为14.50％，目标仓位为11.53％。')
        result = check(item)
        self.assertEqual(result['status'], 'CONSISTENT')
        for comparison in result['comparisons']:
            a, b = comparison['span']
            self.assertEqual(item['reason'][a:b], comparison['mention'])
        json.dumps(result, allow_nan=False)

    def test_invalid_item_numbers_are_unknown_not_coerced(self):
        for value in (True, 1450.0, '1450', None, -1, 10001, float('nan')):
            with self.subTest(value=repr(value)):
                result = check(self.item('当前仓位14.50%。', current=value))
                self.assertEqual(result['status'], 'UNKNOWN')
                self.assertIsNone(result['comparisons'][0]['reference_bps'])
        for item in (None, [], {}, {'reason': None}, {'reason': 123}):
            self.assertEqual(check(item)['status'], 'UNKNOWN')

    def test_corrupt_or_unsupported_receipts_never_establish_a_model_mismatch(self):
        item = self.item('当前仓位1%，目标1%。')
        audit, row = self.receipt(item)
        corrupt = [None, [], {'version': True}, {'version': 2},
                   {'version': 1, 'decisions': [{'key': item['key']}]},
                   {**audit, 'analysis_as_of': 'invalid'},
                   {**audit, 'analysis_as_of': '2026-10-25T01:00:00'},
                   {**audit, 'published_at': '2026-10-24T01:00:00+00:00'},
                   {**audit, 'decisions': [row, copy.deepcopy(row)]}]
        for bad in corrupt:
            with self.subTest(audit=bad):
                result = check(item, bad, row)
                self.assertEqual(result['status'], 'UNKNOWN')
                self.assertEqual(result['basis'], 'UNAVAILABLE')
                self.assertTrue(result['issues'])
        damaged = copy.deepcopy(audit)
        damaged['decisions'][0]['analysis_basis']['current_bps'] = True
        self.assertEqual(check(item, damaged)['status'], 'UNKNOWN')

    def test_audit_key_and_final_values_must_match_before_trusting_it(self):
        item = self.item('当前仓位1%。')
        audit, row = self.receipt(item)
        mismatched = copy.deepcopy(row)
        mismatched['key'] = 'watchlist:other'
        self.assertEqual(check(item, audit, mismatched)['status'], 'UNKNOWN')
        for field, value in (('action', 'HOLD'), ('target_bps', 1100), ('current_bps', 1451)):
            with self.subTest(field=field):
                result = check({**item, field: value}, audit, row)
                self.assertEqual(result['status'], 'UNKNOWN')
                self.assertIn('AUDIT_FINAL_VALUE_MISMATCH', result['issues'])
        self.assertEqual(check(item, {**audit, 'decisions': [mismatched]})['status'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
