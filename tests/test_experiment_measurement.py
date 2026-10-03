import copy
import unittest
from datetime import datetime, timedelta, timezone

from ashare import experiment_measurement as measurement
from ashare.model import DIMENSIONS, TRADER_SCHEMA


class ExperimentMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.at = '2026-10-03T08:00:00+00:00'
        self.packet = {'schema_version': '0.2', 'as_of': self.at,
            'stocks': [{'symbol': 'sh600000'}],
            'evidence': [{'evidence_id': 'E1', 'symbol': 'sh600000',
                          'text': '合成资料：公司本季度收入增长百分之十。下一季以公司正式披露核验。'},
                         {'evidence_id': 'E2', 'symbol': 'sh600001', 'text': '另一家公司的收入增长。'}]}
        self.output = {'summary': '合成研究', 'stocks': [{
            'symbol': 'sh600000', 'action': 'WATCH', 'analysis': '增长仍需后续验证。',
            'facts': [{'evidence_id': 'E1', 'quote': '公司本季度收入增长百分之十'}],
            'counterpoints': ['增长可能减弱'], 'missing_fields': [], 'next_checks': ['核对下期披露'],
            'decision': {'inclination': '继续观察', 'key_evidence': [{'evidence_id': 'E1', 'implication': '已披露增长'}],
                         'pricing': '估值尚不能判断', 'trigger': '下期披露', 'invalidation': '增长消失'},
            'dimensions': [{'id': d, 'summary': '尚待验证', 'uncertainty': '有证据缺口', 'evidence_ids': []}
                           for d in DIMENSIONS],
            'hypothesis_test': {'status': 'TESTABLE', 'claim': '公司下一季收入维持增长',
                'metric': '公司下一季度收入同比百分比', 'operator': '>', 'threshold': 0,
                'deadline': '2026-12-01T16:00:00+08:00', 'source_ids': ['E1'],
                'invalidation': '正式披露的下一季度收入同比小于或等于零'},
        }]}

    def unknown(self):
        result = copy.deepcopy(self.output)
        result['stocks'][0]['hypothesis_test'] = {'status': 'UNKNOWN', 'claim': '', 'metric': '',
            'operator': 'UNKNOWN', 'threshold': None, 'deadline': None, 'source_ids': [], 'invalidation': ''}
        return result

    def pair(self, identity, at=None, baseline=None, candidate=None, status='COMPLETE', window=1):
        return {'id': identity, 'as_of': at or self.at, 'window': window, 'status': status,
                'baseline': baseline or measurement.measure(self.unknown(), self.packet, self.at),
                'candidate': candidate or measurement.measure(self.output, self.packet, self.at)}

    def test_same_schema_isolated_and_measurement_does_not_mutate(self):
        original = copy.deepcopy(self.output)
        packet = copy.deepcopy(self.packet)
        result = measurement.measure(self.output, self.packet, self.at)
        self.assertEqual((result['status'], result['verifiable']), ('TESTABLE', 1))
        self.assertEqual((result['citations_total'], result['citations_valid'], result['citation_rate']), (1, 1, 1))
        self.assertEqual(self.output, original)
        self.assertEqual(self.packet, packet)
        self.assertNotIn('hypothesis_test', TRADER_SCHEMA['properties']['stocks']['items']['properties'])
        self.assertIn('hypothesis_test', measurement.SCHEMA['properties']['stocks']['items']['required'])
        self.assertTrue(all(not isinstance(v, (dict, list)) for v in result.values()))

    def test_unknown_is_valid_but_not_verifiable(self):
        result = measurement.measure(self.unknown(), self.packet, self.at)
        self.assertEqual((result['status'], result['valid'], result['verifiable']), ('UNKNOWN', 1, 0))
        forged = self.unknown()
        forged['stocks'][0]['hypothesis_test']['threshold'] = 42
        self.assertEqual(measurement.measure(forged, self.packet, self.at)['valid'], 0)

    def test_future_deadline_finite_threshold_and_operator_are_required(self):
        for key, value in [('deadline', self.at), ('deadline', '2026-10-02T08:00:00+00:00'),
                           ('deadline', '2026-12-01'), ('deadline', '2026-12-01T08:00:00'),
                           ('threshold', True), ('threshold', float('nan')),
                           ('threshold', float('inf')), ('threshold', '10'),
                           ('operator', 'approximately'), ('claim', 'UNKNOWN'),
                           ('metric', '未知'), ('invalidation', '')]:
            with self.subTest(key=key, value=value):
                result = copy.deepcopy(self.output)
                result['stocks'][0]['hypothesis_test'][key] = value
                stats = measurement.measure(result, self.packet, self.at)
                self.assertEqual((stats['status'], stats['verifiable']), ('INVALID', 0))

    def test_exact_citations_and_security_scope_are_checked(self):
        variants = [([], ['E1']),
                    ([{'evidence_id': 'E1', 'quote': '公司收入增长百分之二十'}], ['E1']),
                    ([{'evidence_id': 'E2', 'quote': '另一家公司的收入增长'}], ['E2']),
                    ([{'evidence_id': 'E1', 'quote': '公司本季度收入增长百分之十'}], ['missing']),
                    ([{'evidence_id': 'E1', 'quote': '公司本季度收入增长百分之十'}], []),
                    ([{'evidence_id': 'E1', 'quote': '公司本季度收入增长百分之十'}], ['E1', 'E1'])]
        for facts, ids in variants:
            with self.subTest(facts=facts, ids=ids):
                result = copy.deepcopy(self.output)
                result['stocks'][0]['facts'] = facts
                result['stocks'][0]['hypothesis_test']['source_ids'] = ids
                self.assertEqual(measurement.measure(result, self.packet, self.at)['verifiable'], 0)
        invalid = copy.deepcopy(self.output)
        invalid['stocks'][0]['facts'].append({'evidence_id': 'missing', 'quote': '伪造的引用文本'})
        stats = measurement.measure(invalid, self.packet, self.at)
        self.assertEqual((stats['citations_total'], stats['citations_valid'], stats['citation_rate']), (2, 1, .5))

    def test_malformed_outputs_and_future_packets_return_safe_failure(self):
        for result in (None, [], {}, {'summary': 'bad', 'stocks': []}, {'stocks': [None]}):
            self.assertEqual(measurement.measure(result, self.packet, self.at)['status'], 'INVALID')
        packet = {**self.packet, 'as_of': '2026-10-04T08:00:00+00:00'}
        self.assertEqual(measurement.measure(self.output, packet, self.at)['valid'], 0)
        missing_field = copy.deepcopy(self.output)
        del missing_field['stocks'][0]['dimensions']
        self.assertEqual(measurement.measure(missing_field, self.packet, self.at)['valid'], 0)

    def test_deadline_must_still_be_future_when_measurement_completes(self):
        result = copy.deepcopy(self.output)
        result['stocks'][0]['hypothesis_test']['deadline'] = '2026-10-03T08:01:00+00:00'
        stats = measurement.measure(result, self.packet, '2026-10-03T08:02:00+00:00')
        self.assertEqual(stats['verifiable'], 0)

    def test_failure_and_unknown_remain_in_denominator(self):
        first = self.pair('ok')
        failure = self.pair('failed', status='FAILED')
        failure['candidate'] = measurement.measure(None, self.packet, self.at)
        failure['baseline'] = measurement.measure(self.output, self.packet, self.at)
        pending = self.pair('pending', status='PENDING')
        pending['candidate'] = None
        pending['baseline'] = measurement.measure(self.output, self.packet, self.at)
        result = measurement.summarize([first, failure, pending], 30)
        self.assertEqual((result['enrolled'], result['complete'], result['failed'], result['pending']), (3, 1, 1, 1))
        self.assertEqual(result['baseline_rate'], .333333)
        self.assertEqual(result['candidate_rate'], .333333)
        self.assertEqual(result['paired_delta'], 0)
        self.assertEqual(result['conclusion'], 'INSUFFICIENT')
        self.assertIsNone(result['ci95'])
        self.assertEqual(result['baseline_citations_total'], 3)
        self.assertEqual(result['baseline_unknown'], 1)
        self.assertEqual(result['candidate_invalid'], 1)

    def test_same_day_many_stocks_are_one_cluster_and_windows_separate(self):
        pairs = [self.pair(str(i), window=1 if i < 50 else 2) for i in range(100)]
        result = measurement.summarize(pairs, 30)
        self.assertEqual((result['enrolled'], result['day_clusters']), (100, 1))
        self.assertIsNone(result['ci95'])
        self.assertEqual([w['enrolled'] for w in result['windows']], [50, 50])
        # UTC midnight is not a new research day in Shanghai until local midnight.
        pairs = [self.pair('a', '2026-10-03T15:59:00+00:00'),
                 self.pair('b', '2026-10-03T16:00:00+00:00')]
        self.assertEqual(measurement.summarize(pairs, 30)['day_clusters'], 2)

    def test_thirty_days_can_show_descriptive_interval_but_never_pass(self):
        start = datetime(2026, 10, 3, 8, tzinfo=timezone.utc)
        pairs = [self.pair(str(i), (start + timedelta(days=i)).isoformat(), window=1 if i < 15 else 2)
                 for i in range(30)]
        result = measurement.summarize(pairs, 30)
        self.assertEqual(result['ci95'], [1, 1])
        self.assertEqual(result['conclusion'], 'DESCRIPTIVE_ONLY')
        pairs[-1]['status'] = 'PENDING'
        self.assertIsNone(measurement.summarize(pairs, 30)['ci95'])

    def test_duplicates_invalid_rows_and_false_success_rejected(self):
        pair = self.pair('same')
        with self.assertRaises(ValueError):
            measurement.summarize([pair, pair], 30)
        for key, value in [('window', True), ('window', 3), ('as_of', '2026-10-03'),
                           ('status', 'PASSED'), ('candidate', {'verifiable': 1, 'valid': 0})]:
            altered = {**pair, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                measurement.summarize([altered], 30)
        with self.assertRaises(ValueError):
            measurement.summarize([], True)
        pair['candidate'] = None
        with self.assertRaises(ValueError):
            measurement.summarize([pair], 30)

    def test_empty_trial_has_no_invented_rates(self):
        result = measurement.summarize([], 30)
        self.assertEqual(result['enrolled'], 0)
        self.assertIsNone(result['paired_delta'])
        self.assertIsNone(result['baseline_rate'])
        self.assertEqual(result['conclusion'], 'INSUFFICIENT')


if __name__ == '__main__':
    unittest.main()
