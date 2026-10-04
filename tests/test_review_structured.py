"""Execution audits use recorded codes; fixtures never place orders or call a model."""
import json
import tempfile
import unittest
from unittest.mock import patch

from ashare import governance, review_checks
from ashare.storage import Store


class StructuredExitTests(unittest.TestCase):
    start = '2026-10-01T00:00:00+00:00'
    at = '2026-10-01T12:00:00+00:00'
    end = '2026-10-02T00:00:00+00:00'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        # Deliberately incomplete historical snapshots are the subject of these checks.
        self.store.db.execute('PRAGMA foreign_keys=OFF')
        self.number = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def insert(self, table, row):
        keys = list(row)
        self.store.db.execute(f'INSERT INTO {table} ({",".join(keys)}) VALUES ({",".join("?" for _ in keys)})',
                              tuple(row[k] for k in keys))

    def fill(self, route='watchlist', *, reason='COST_STOP_TRIGGER', payload=None, side='SELL', order=True):
        self.number += 1
        key = str(self.number)
        prefix = {'watchlist': 'paper', 'global': 'global', 'dynamic': 'dynamic'}[route]
        symbol = {'watchlist': 'sh600000', 'global': 'BTC', 'dynamic': 'sz000001'}[route]
        row = {'id': 'o' + key, 'symbol': symbol, 'side': side, 'qty': 1, 'filled_qty': 1,
               'reserved_cents': 0, 'created_at': self.at, 'expires_at': self.end, 'status': 'FILLED'}
        row['limit_micros' if route == 'global' else 'limit_cents'] = 100
        if route == 'watchlist':
            row.update(decision_id='d' + key, plan_id='p' + key)
            decision_payload = {'risk_trigger': 'COST_STOP_TRIGGER'} if payload is None else payload
            self.insert('decisions', {'id': 'd' + key, 'slot_id': 's' + key, 'symbol': symbol,
                'at': self.at, 'action': side, 'status': 'SUBMITTED', 'reason': reason,
                'payload_json': json.dumps(decision_payload)})
        elif route == 'global':
            row.update(intent_key='i' + key, plan_id=None,
                       payload_json=json.dumps({'reason': reason} if payload is None else payload))
        else:
            row.update(intent_key='i' + key, case_id='c' + key, reason=reason, terms_json='{}')
        if order:
            self.insert(prefix + '_orders', row)
        fill = {'id': 'f' + key, 'order_id': 'o' + key, 'quote_id': 'q' + key, 'symbol': symbol,
                'side': side, 'qty': 1, 'fee_cents': 0, 'realized_cents': 0,
                'occurred_at': self.at, 'recorded_at': self.at}
        fill['price_micros' if route == 'global' else 'price_cents'] = 100
        if route == 'global':
            fill.update(fx_micros=1_000_000, gross_cents=100)
        self.insert(prefix + '_fills', fill)
        self.store.db.commit()
        return key

    def checks(self):
        return {c['check']: c for c in review_checks.execution(self.store, {}, self.start, self.end, self.end)}

    def sell_check(self):
        return self.checks()['CHECK_SELL_WITHOUT_REASON']

    def test_watchlist_uses_structured_code_even_if_explanation_changes(self):
        self.fill(reason='本条说明可以重写，不能决定退出授权', payload={'risk_trigger': 'COST_STOP_TRIGGER'})
        check = self.sell_check()
        self.assertEqual(check['status'], 'PASS')
        self.assertEqual(check['evidence_sources'], {'DECISION_RISK_TRIGGER': 1})
        self.assertEqual(check['legacy_checked'], 0)

    def test_unknown_structured_code_cannot_fall_back_to_valid_reason(self):
        for value in ('未触发 COST_STOP_TRIGGER', 'X' * 10000, ['COST_STOP_TRIGGER'], False):
            self.fill(payload={'risk_trigger': value})
        check = self.sell_check()
        self.assertEqual((check['status'], check['failures']), ('FAIL', 4))
        self.assertTrue(all(len(e['exit_code']) <= 80 for e in check['examples']))

    def test_explicitly_missing_code_cannot_fall_back_to_valid_reason(self):
        self.fill(payload={'risk_trigger': None})
        self.fill(payload={'risk_trigger': ''})
        check = self.sell_check()
        self.assertEqual((check['status'], check['missing'], check['checked']), ('INSUFFICIENT', 2, 0))

    def test_legacy_exact_code_or_program_prefix_is_labelled(self):
        for reason in ('COST_STOP_TRIGGER', 'PLAN_STOP_TRIGGER；研究说明；PAPER_ORDER_OPEN'):
            self.fill(reason=reason, payload={})
        check = self.sell_check()
        self.assertEqual((check['status'], check['legacy_checked']), ('PASS', 2))
        self.assertIn('2笔旧记录', check['detail'])

    def test_legacy_negated_or_embedded_code_is_never_pass(self):
        for reason in ('未触发 COST_STOP_TRIGGER', '说明；COST_STOP_TRIGGER',
                       'COST_STOP_TRIGGER 并未触发', 'COST_STOP_TRIGGER:未触发'):
            self.fill(reason=reason, payload={})
        check = self.sell_check()
        self.assertEqual((check['status'], check['missing'], check['legacy_checked']), ('INSUFFICIENT', 4, 0))

    def test_corrupt_decision_payload_does_not_enable_legacy_fallback(self):
        key = self.fill(payload={})
        with self.store.db:
            self.store.db.execute('UPDATE decisions SET payload_json=? WHERE id=?', ('{broken', 'd' + key))
        self.assertEqual(self.sell_check()['status'], 'INSUFFICIENT')

    def test_global_exact_codes_and_missing_or_unknown_codes(self):
        for code in review_checks.GLOBAL_EXIT_CODES:
            self.fill('global', reason=code)
        self.assertEqual(self.sell_check()['status'], 'PASS')
        self.fill('global', reason='未触发 COST_STOP_TRIGGER')
        self.fill('global', reason='COST_STOP_TRIGGER；说明')
        check = self.sell_check()
        self.assertEqual((check['status'], check['failures']), ('FAIL', 2))
        self.fill('global', payload={})
        self.assertEqual(self.sell_check()['missing'], 1)

    def test_dynamic_exact_fixed_reasons_and_missing_or_unknown_reasons(self):
        for reason in review_checks.DYNAMIC_EXIT_CODES:
            self.fill('dynamic', reason=reason)
        self.assertEqual(self.sell_check()['status'], 'PASS')
        self.fill('dynamic', reason='未触发 动态持仓成本止损')
        self.fill('dynamic', reason='动态持仓止盈；未触发')
        check = self.sell_check()
        self.assertEqual((check['status'], check['failures']), ('FAIL', 2))
        self.fill('dynamic', reason='')
        self.assertEqual(self.sell_check()['missing'], 1)

    def test_missing_order_or_decision_needs_evidence(self):
        self.fill(order=False)
        key = self.fill()
        with self.store.db:
            self.store.db.execute('DELETE FROM decisions WHERE id=?', ('d' + key,))
        self.assertEqual((self.sell_check()['status'], self.sell_check()['missing']), ('INSUFFICIENT', 2))

    def test_missing_groups_keep_all_routes_and_counts_with_bounded_examples(self):
        for route in ('watchlist', 'dynamic', 'global'):
            for _ in range(6):
                self.fill(route, side='BUY', order=False)
        checks = self.checks()
        evidence = checks['CHECK_EXECUTION_EVIDENCE']
        self.assertEqual((evidence['status'], evidence['missing']), ('INSUFFICIENT', 54))
        self.assertEqual(len(evidence['missing_groups']), 9)
        self.assertTrue(all(g['count'] == 6 and len(g['examples']) == 2 for g in evidence['missing_groups']))
        self.assertEqual({g['route'] for g in evidence['missing_groups']}, {'watchlist', 'dynamic', 'global'})
        for check in ('CHECK_BUY_OUTSIDE_PLAN_BAND', 'CHECK_BUY_WITH_PLAN_BLOCKERS', 'CHECK_BUY_WITHOUT_PORTFOLIO_ALLOW'):
            self.assertTrue(all(r['missing'] == 6 for r in checks[check]['by_route'].values()))

    def test_issue_keeps_all_group_counts_without_one_issue_per_fill(self):
        for route in ('watchlist', 'dynamic', 'global'):
            for _ in range(3):
                self.fill(route, side='BUY', order=False)
        with patch('ashare.review_checks.health', return_value=[]):
            review_checks.run(self.store, {}, self.start, self.end, self.end)
            review_checks.run(self.store, {}, self.start, self.end, self.end)
        issues = governance.issues(self.store)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['issue_key'], 'CHECK_EXECUTION_EVIDENCE')
        groups = json.loads(issues[0]['payload']['latest']['evidence'][0])['missing_groups']
        self.assertEqual(len(groups), 9)
        self.assertTrue(all(g['count'] == 3 for g in groups))

    def test_execution_check_is_read_only(self):
        self.fill(payload={'risk_trigger': 'COST_STOP_TRIGGER'})
        before = list(self.store.db.iterdump())
        self.checks()
        self.assertEqual(before, list(self.store.db.iterdump()))


if __name__ == '__main__':
    unittest.main()
