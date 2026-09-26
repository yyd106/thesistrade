"""Cash dividends on held shares: credited once by the executing ledger, counted by the cost stop.
Synthetic stock and amounts (ashare.demo); no collected data."""
import json
import unittest
from unittest.mock import patch
import test_workflow as workflow_fixtures
from ashare import digest, dividends, review_portfolio
from ashare.demo import SYMBOL, put_quote
from ashare.paper import account, positions, settle
from ashare.scheduler import Scheduler
from ashare.slots import hard_reason
from ashare.storage import normalize_time

BOUGHT = '2026-09-15T10:00:10+08:00'  # 1,900 shares at 10.00, cost 19,005.70 with fees
ACTION = {'cash_per_share': '0.5', 'cash_per_share_cents': 50, 'record_date': '2026-09-15', 'ex_date': '2026-09-16', 'doc_id': 'doc'}


class DividendTests(unittest.TestCase):
    setUp = workflow_fixtures.WorkflowTests.setUp
    tearDown = workflow_fixtures.WorkflowTests.tearDown
    plan = workflow_fixtures.WorkflowTests.plan
    slot = workflow_fixtures.WorkflowTests.slot

    def hold(self):
        self.plan();put_quote(self.store, self.at);self.slot()
        put_quote(self.store, BOUGHT);settle(self.store, self.cfg, BOUGHT)
        self.assertEqual(positions(self.store, normalize_time(BOUGHT))[SYMBOL]['qty'], 1900)

    def publish(self, *actions, kind='NO_ENTRY'):
        """The dividend as research publishes it: in the stock's plan."""
        with self.store.db:
            row = self.store.db.execute('SELECT * FROM plans ORDER BY rowid DESC LIMIT 1').fetchone()
            payload = {**json.loads(row['payload_json']), 'kind': kind, 'corporate_actions': list(actions)}
            self.store.db.execute('UPDATE plans SET payload_json=? WHERE id=?', (json.dumps(payload), row['id']))

    def cash(self):
        return self.store.db.execute("SELECT cash_cents FROM paper_accounts WHERE id='DEMO_PAPER'").fetchone()[0]

    def test_credited_once_after_the_ex_date_for_shares_held_at_the_record_date(self):
        self.hold();self.publish(ACTION);before = self.cash()
        self.assertEqual(dividends.credit(self.store, self.cfg, '2026-09-15T20:00:00+08:00')['credited'], [])  # before the ex-date
        done = dividends.credit(self.store, self.cfg, '2026-09-16T16:00:00+08:00')['credited']
        self.assertEqual([(d['qty'], d['amount_cents']) for d in done], [(1900, 95000)])
        self.assertEqual(self.cash(), before + 95000)
        self.assertEqual(dividends.credit(self.store, self.cfg, '2026-09-17T16:00:00+08:00')['credited'], [])  # once only
        self.assertEqual(self.cash(), before + 95000)
        self.assertEqual(dividends.history(self.store)[0]['cash_per_share'], '0.5')
        held = positions(self.store, normalize_time('2026-09-17T10:00:00+08:00'))[SYMBOL]
        self.assertEqual((held['cost_cents'], held['dividend_cents']), (1900570, 95000))  # lots keep their cost

    def test_cost_stop_counts_the_dividend_received(self):
        self.hold()
        p = positions(self.store, normalize_time('2026-09-16T10:00:00+08:00'))[SYMBOL]
        quote = {'price_cents': 935}
        self.assertEqual(hard_reason(quote, p, {}, self.cfg), 'COST_STOP_TRIGGER')  # 6.5% below cost without it
        self.publish(ACTION);dividends.credit(self.store, self.cfg, '2026-09-16T16:00:00+08:00')
        p = positions(self.store, normalize_time('2026-09-17T10:00:00+08:00'))[SYMBOL]
        self.assertIsNone(hard_reason(quote, p, {}, self.cfg))  # 9.35 plus 0.50 received is 1.8% below cost
        self.assertEqual(hard_reason({'price_cents': 890}, p, {}, self.cfg), 'COST_STOP_TRIGGER')  # a real loss still stops

    def test_no_credit_without_entitlement_or_with_doubtful_facts(self):
        self.hold()
        bought_after = {**ACTION, 'record_date': '2026-09-14', 'ex_date': '2026-09-15'}
        bad = [{**ACTION, 'cash_per_share': '0'}, {**ACTION, 'ex_date': '2026-10-16'}, {**ACTION, 'record_date': 'x'},
               {**ACTION, 'ex_date': '2026-09-15'}, {'ex_date': '2026-09-16'}, 'text']
        self.publish(bought_after, *bad)
        self.assertEqual(dividends.credit(self.store, self.cfg, '2026-09-18T16:00:00+08:00'), {'credited': [], 'conflicts': []})
        # Two amounts for one ex-date: nothing is credited and the conflict is reported.
        self.publish(ACTION, {**ACTION, 'cash_per_share': '0.6'})
        self.assertEqual(dividends.credit(self.store, self.cfg, '2026-09-18T16:00:00+08:00'), {'credited': [], 'conflicts': [SYMBOL + ':2026-09-16']})
        # A protective plan written by the cloud is not research and carries no dividends.
        self.publish(ACTION, kind='RISK_EXIT_ONLY')
        self.assertEqual(dividends.credit(self.store, self.cfg, '2026-09-18T16:00:00+08:00')['credited'], [])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM paper_flows WHERE kind='CASH_DIVIDEND'").fetchone()[0], 0)

    def test_sold_shares_keep_their_dividend_and_new_lots_get_none(self):
        lots = {SYMBOL: [{'qty': 700, 'acquired_day': '2026-09-15'}, {'qty': 500, 'acquired_day': '2026-09-17'}]}
        self.hold();self.publish(ACTION);dividends.credit(self.store, self.cfg, '2026-09-16T16:00:00+08:00')
        # 1,200 of the 1,900 entitled shares sold later, 500 bought after the record date.
        self.assertEqual(dividends.received(self.store, lots), {SYMBOL: 35000})
        self.assertEqual(dividends.amount_cents(3, '0.055'), 16)  # rounded down to the cent

    def test_scheduler_records_a_failure_and_keeps_going(self):
        self.hold()
        with patch('ashare.dividends.credit', side_effect=RuntimeError('boom')):
            self.assertIsNone(Scheduler.credit_dividends(None, self.store, self.cfg, normalize_time('2026-09-16T16:00:00+08:00')))
        state = dict(self.store.db.execute('SELECT key,value FROM service_state').fetchall())
        self.assertIn('RuntimeError: boom', state['dividend_error'])
        self.publish(ACTION)
        result = Scheduler.credit_dividends(None, self.store, self.cfg, normalize_time('2026-09-16T16:00:00+08:00'))
        self.assertEqual(len(result['credited']), 1)
        self.assertNotIn('dividend_error', dict(self.store.db.execute('SELECT key,value FROM service_state').fetchall()))

    def test_reports_show_the_credit_and_the_review_keeps_balancing(self):
        self.hold();self.publish(ACTION);dividends.credit(self.store, self.cfg, '2026-09-16T16:00:00+08:00')
        a, b = digest.day_window('2026-09-16')
        section = digest.execution(self.store, self.cfg, a, b)
        self.assertEqual([(d['qty'], d['amount_cents']) for d in section['dividends']], [(1900, 95000)])
        text = digest.markdown({**digest.build(self.store, self.cfg, '2026-09-16')})
        self.assertIn('分红入账：合成测试 1900 股 × 每股 0.5 元 = 950.00 元（税前，除息日 2026-09-16）', text)
        # The whole-account review rebuilds cash from flows and fills; the credit is one more flow.
        with self.store.db:  # the fixture opened the account at the real clock; date it before the trades
            self.store.db.execute("UPDATE paper_flows SET created_at=? WHERE kind='SIMULATED_INITIAL'", (normalize_time('2026-09-14T09:00:00+08:00'),))
        start, end = normalize_time('2026-09-15T00:00:00+08:00'), normalize_time('2026-09-17T00:00:00+08:00')
        facts = review_portfolio.build(self.store, self.cfg, start, end, normalize_time('2026-09-17T00:00:00+08:00'))
        self.assertEqual(facts['closing']['cash_cents'], account(self.store, end)['cash_cents'])
        self.assertEqual(facts['positions'][0]['closing']['cost_cents'], 1900570)
        self.assertEqual([(d['symbol'], d['amount_cents']) for d in facts['dividends']], [(SYMBOL, 95000)])
        self.assertEqual(review_portfolio.model_view(facts)['dividends'], facts['dividends'])  # the review model sees it

    def test_due_shows_a_verified_dividend_the_ledger_has_not_credited(self):
        self.hold();self.publish(ACTION)
        stamp = normalize_time('2026-09-16T16:00:00+08:00')
        self.assertEqual([(d['amount_cents'], d['credited']) for d in dividends.due(self.store, stamp)['due']], [(95000, False)])
        dividends.credit(self.store, self.cfg, stamp)
        self.assertEqual([d['credited'] for d in dividends.due(self.store, stamp)['due']], [True])
        self.assertEqual(dividends.due(self.store, normalize_time('2026-09-15T16:00:00+08:00'))['due'], [])  # not yet ex


if __name__ == '__main__':
    unittest.main()
