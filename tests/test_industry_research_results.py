"""Research gaps do not create, replace, or renew evidence-backed hypotheses."""
import json
import unittest
from unittest.mock import patch

import test_industry as fixtures
from ashare import industry, industry_research, universe


class ResearchResultTests(unittest.TestCase):
    tearDown = fixtures.IndustryTests.tearDown
    document = fixtures.IndustryTests.document
    fact = fixtures.IndustryTests.fact
    proposal = fixtures.IndustryTests.proposal
    save = fixtures.IndustryTests.save
    later = fixtures.IndustryTests.later

    def setUp(self):
        fixtures.IndustryTests.setUp(self)
        with self.store.db:
            for symbol, identity in self.identities.items():
                self.store.db.execute('INSERT INTO macro_instruments VALUES(?,?,?,?)',
                                      (symbol, 'CN', self.at, json.dumps(identity)))

    def gap(self):
        return self.proposal() | {'state': 'WAITING', 'facts': []}

    def run_discovery(self, proposals=None, *, cycle='testcycle', at=None, model=None):
        result = {'hypotheses': proposals or [],
                  'method_coverage': {method: '缺少原文，待核对下一次披露' for method in industry.METHODS}}
        with patch.object(industry_research, 'DOMAINS', {'ai': industry.DOMAINS['ai']}):
            outcome = industry_research.run(self.store, self.cfg, cycle, at=at or self.at,
                                           collect_fn=False, model_fn=model or (lambda *args: result))
        row = self.store.db.execute('SELECT * FROM industry_steps WHERE cycle_id=? AND step=?',
                                    (cycle, 'ai')).fetchone()
        return outcome, row['status'], json.loads(row['payload_json'])

    def test_generation_limits_match_evidence_gate_and_configured_count(self):
        self.cfg['industry_policy'] = {'hypotheses_per_domain': 2}
        def model(prompt, schema, *args):
            facts = schema['properties']['hypotheses']['items']['properties']['facts']
            self.assertEqual((facts['minItems'], facts['maxItems']), (1, 24))
            self.assertEqual(schema['properties']['hypotheses']['maxItems'], 2)
            self.assertIn('最多2条假设', prompt)
            self.assertIn('只在method_coverage记录', prompt)
            return {'hypotheses': [], 'method_coverage': {method: '无原文' for method in industry.METHODS}}
        outcome, status, payload = self.run_discovery(model=model)
        self.assertEqual((outcome['status'], status, payload['status']), ('DONE', 'DONE', 'NO_FINDINGS'))
        self.assertEqual(payload['rejected'], [])

    def test_empty_waiting_is_only_a_gap_and_never_calls_save(self):
        with patch.object(industry_research, 'save') as save:
            outcome, status, payload = self.run_discovery([self.gap()])
        save.assert_not_called()
        self.assertEqual((outcome['status'], status, payload['status']), ('DONE', 'DONE', 'EVIDENCE_GAPS'))
        self.assertEqual(payload['hypotheses'], [])
        self.assertEqual(payload['rejected'], [])
        self.assertEqual(payload['failure_kind'], '')
        self.assertEqual(payload['research_gaps'][0]['next_check'], self.gap()['next_check'])
        self.assertEqual(industry.latest(self.store, self.at), [])
        self.assertFalse(universe.permit(self.store, self.cfg, 'sz300499', self.at))

    def test_waiting_with_valid_evidence_remains_a_saved_hypothesis(self):
        proposal = self.proposal() | {'state': 'WAITING', 'facts': [self.fact()]}
        outcome, status, payload = self.run_discovery([proposal])
        self.assertEqual((outcome['status'], status), ('DONE', 'DONE'))
        self.assertEqual(len(payload['hypotheses']), 1)
        self.assertEqual(payload['research_gaps'], [])
        self.assertEqual(industry.latest(self.store, self.at)[0]['state'], 'WAITING')
        self.assertFalse(universe.permit(self.store, self.cfg, 'sz300499', self.at))

    def test_mixed_valid_gap_and_bad_quote_keeps_success_and_reports_failure(self):
        bad = self.proposal() | {'topic': '错误引文', 'state': 'WAITING'}
        bad['facts'][0]['quote'] = '不存在于原文的断言'
        outcome, status, payload = self.run_discovery([self.proposal(), self.gap(), bad])
        self.assertEqual((outcome['status'], status, payload['status']), ('PARTIAL', 'DEFERRED', 'PARTIAL'))
        self.assertEqual((len(payload['hypotheses']), len(payload['research_gaps']), len(payload['rejected'])), (1, 1, 1))
        self.assertEqual(payload['failure_kind'], 'VALIDATION')
        self.assertIn('引用不是当时可用的原文', payload['rejected'][0])
        self.assertEqual(len(industry.latest(self.store, self.at)), 1)

    def test_other_empty_states_and_oversize_evidence_still_fail(self):
        for state in ('ACTIVE', 'INVALIDATED', 'REALIZED'):
            with self.subTest(state=state):
                outcome, status, payload = self.run_discovery([self.gap() | {'state': state}], cycle=state)
                self.assertEqual((outcome['status'], status, payload['status']), ('PARTIAL', 'DEFERRED', 'VALIDATION_FAILED'))
                self.assertEqual(payload['research_gaps'], [])
                self.assertIn('须提供有界原文证据', payload['rejected'][0])
        outcome, status, payload = self.run_discovery([self.proposal() | {'facts': [self.fact()] * 25}], cycle='too-many')
        self.assertEqual(status, 'DEFERRED')
        self.assertEqual(payload['failure_kind'], 'VALIDATION')
        self.assertEqual(industry.latest(self.store, self.at), [])

    def test_gap_cannot_hide_invalid_identity_domain_method_or_fields(self):
        mutations = [{'symbol': 'sz999999'}, {'domain': 'power'}, {'method': '9'},
                     {'next_check': ''}, {'missing': None}, {'facts': None},
                     {'forecasts': [{'metric': 'ORDERS'}]}]
        for index, mutation in enumerate(mutations):
            with self.subTest(mutation=mutation):
                outcome, status, payload = self.run_discovery([self.gap() | mutation], cycle=f'invalid-{index}')
                self.assertEqual((outcome['status'], status), ('PARTIAL', 'DEFERRED'))
                self.assertEqual(payload['research_gaps'], [])
                self.assertEqual(len(payload['rejected']), 1)
                self.assertEqual(payload['failure_kind'], 'VALIDATION')

    def test_gap_and_its_cached_reuse_do_not_renew_old_evidence(self):
        self.save()
        before = dict(self.store.db.execute('SELECT * FROM industry_hypotheses').fetchone())
        with patch.object(industry_research, 'save') as save:
            self.run_discovery([self.gap()], at=self.later(6))
            outcome, status, payload = self.run_discovery(at=self.later(8), cycle='reuse',
                                                        model=lambda *args: self.fail('unchanged gap should be reused'))
        save.assert_not_called()
        self.assertEqual((outcome['status'], status), ('DONE', 'DONE'))
        self.assertTrue(payload['reused'])
        after = dict(self.store.db.execute('SELECT * FROM industry_hypotheses').fetchone())
        self.assertEqual(before, after)
        self.assertFalse(universe.permit(self.store, self.cfg, 'sz300499', self.later(8)))
        self.assertEqual(industry.state(self.store, industry.latest(self.store, self.later(31))[0], self.later(31)), 'ARCHIVED')

    def test_retry_preserves_accepted_references_when_only_remainder_returns(self):
        bad = self.proposal() | {'topic': '错误引文'}
        bad['facts'][0]['quote'] = '不存在于原文的断言'
        _, _, first = self.run_discovery([self.proposal(), bad])
        outcome, status, payload = self.run_discovery([self.gap()])
        self.assertEqual((outcome['status'], status, payload['status']), ('DONE', 'DONE', 'COMPLETE'))
        self.assertEqual(payload['hypotheses'], first['hypotheses'])
        self.assertEqual(payload['rejected'], [])
        self.assertEqual(len(industry.latest(self.store, self.at)), 1)

    def test_model_failure_preserves_previous_results_and_is_not_a_gap(self):
        bad = self.proposal() | {'topic': '错误引文'}
        bad['facts'][0]['quote'] = '不存在于原文的断言'
        _, _, first = self.run_discovery([self.proposal(), bad])
        def timeout(*args):
            raise RuntimeError('模型分析超时，资料包已保存，未使用付费后备服务')
        outcome, status, payload = self.run_discovery(model=timeout)
        self.assertEqual((outcome['status'], status, payload['status']), ('PARTIAL', 'DEFERRED', 'PARTIAL'))
        self.assertEqual(payload['failure_kind'], 'MODEL_TIMEOUT')
        self.assertEqual(payload['hypotheses'], first['hypotheses'])
        self.assertEqual(payload['research_gaps'], [])

    def test_no_verified_identity_on_retry_preserves_saved_references_without_renewal(self):
        bad = self.proposal() | {'topic': '错误引文'}
        bad['facts'][0]['quote'] = '不存在于原文的断言'
        _, _, first = self.run_discovery([self.proposal(), bad])
        before = [dict(row) for row in self.store.db.execute('SELECT * FROM industry_hypotheses')]
        self.assertEqual((len(first['hypotheses']), len(first['rejected'])), (1, 1))
        with patch('ashare.observation.registry', return_value={}), patch.object(industry_research, 'save') as save:
            outcome, status, payload = self.run_discovery(at=self.later(8),
                                                        model=lambda *args: self.fail('no verified identities'))
        save.assert_not_called()
        self.assertEqual((outcome['status'], status, payload['status']), ('DONE', 'DONE', 'NO_EVIDENCE'))
        self.assertEqual(payload['hypotheses'], first['hypotheses'])
        self.assertEqual(payload['rejected'], [])
        self.assertEqual(payload['failure_kind'], '')
        self.assertEqual(before, [dict(row) for row in self.store.db.execute('SELECT * FROM industry_hypotheses')])
        self.assertFalse(universe.permit(self.store, self.cfg, 'sz300499', self.later(8)))


if __name__ == '__main__':
    unittest.main()
