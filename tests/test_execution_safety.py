"""Cloud execution regressions with synthetic research certificates and isolated ledgers.

No production config, network, model, or eligibility mocks are used here.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from ashare import dynamic, dynamic_paper, global_paper, paper, portfolio_strategy, slots
from ashare.cloud_runtime import put
from ashare.finance import PaperLedger
from ashare.global_market import latest
from ashare.settings import DEFAULTS
from ashare.storage import Store, digest, normalize_time


SYMBOL = 'sz000333'
NAME = '合成公司'


class ExecutionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        example = json.loads((Path(__file__).resolve().parents[1]/'config.example.json').read_text())
        self.cfg = {**DEFAULTS, **example, 'data_dir': self.tmp.name,
                    'deployment_role': 'cloud', 'investment_policy': 'days_cash_v1',
                    'portfolio_strategy': portfolio_strategy.VERSION, 'industry_enabled': False,
                    'watchlist': [{'symbol': SYMBOL, 'name': NAME}], 'paper_slippage_bps': 0,
                    'paper_max_fill_qty': 100, 'slot_execution_mode': 'RULES'}
        self.at = normalize_time('2026-09-15T10:00:00+08:00')
        self.end = normalize_time('2026-09-15T18:00:00+08:00')
        PaperLedger(self.store).initialize()
        with self.store.db:
            for key, value in [('initialized', True), ('execution_enabled', True),
                               ('research_completed_at', self.at), ('research_received_at', self.at),
                               ('active_assets', [SYMBOL, 'BTC'])]:
                put(self.store, key, value)

    def later(self, seconds):
        return normalize_time((datetime.fromisoformat(self.at)+timedelta(seconds=seconds)).isoformat())

    def quote(self, seconds=0, price=1000, name=NAME, route='watchlist'):
        at = self.later(seconds)
        qid = digest(route+at+str(price)+name)
        with self.store.db:
            if route == 'dynamic':
                self.store.db.execute('INSERT OR IGNORE INTO dynamic_quotes VALUES(?,?,?,?,?,?,?,?)',
                                      (qid, SYMBOL, name, price, 1000, at, at, 'synthetic'))
            else:
                self.store.db.execute('INSERT OR IGNORE INTO quotes VALUES(?,?,?,?,?,?,?,?,?)',
                                      (qid, SYMBOL, name, price, 1000, at, at, 'synthetic', 'synthetic'))
        if route == 'dynamic':
            from ashare.dynamic_sources import latest_quote
            return latest_quote(self.store, SYMBOL, at)
        return self.store.latest_quote(SYMBOL, at)

    def authorize(self, route, identity, row, target=2000, symbol=SYMBOL):
        reference = 'research:'+row['id']
        d = {'key': route+':'+identity, 'route': route, 'identity': identity, 'symbol': symbol,
             'source_token': portfolio_strategy.row_token(row), 'action': 'ALLOW',
             'target_bps': target, 'related_keys': [], 'evidence_ids': [reference],
             'reference_id': reference, 'reason': 'synthetic research'}
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO portfolio_decisions VALUES(?,?,?,?,?,?)',
                                  ('portfolio', self.at, self.end, 'ACTIVE', portfolio_strategy.VERSION,
                                   json.dumps({'decisions': [d], 'risk_groups': []})))

    def watchlist_plan(self):
        payload = {'kind': 'PAPER_TRADE', 'max_stock_pct': 20,
                   'levels': {'buy_low_cents': 980, 'buy_high_cents': 1020,
                              'stop_cents': 940, 'sell_cents': 1100}}
        with self.store.db:
            self.store.db.execute('INSERT INTO snapshots VALUES(?,NULL,?,?,?,?,?)',
                                  ('snapshot', SYMBOL, self.at, self.at, 'synthetic', '{}'))
            self.store.db.execute('INSERT INTO studies VALUES(?,?,?,?,?,?)',
                                  ('study', 'snapshot', SYMBOL, self.at, 'SUCCEEDED', '{}'))
            self.store.db.execute('INSERT INTO plans VALUES(?,?,?,?,?,?,?,?)',
                                  ('plan', 'study', SYMBOL, self.at, self.end, 'ACTIVE',
                                   'paper_baseline_v1', json.dumps(payload)))
            certificate = {'source_id': 'plan', 'symbol': SYMBOL, 'reviewed_documents': []}
            self.store.db.execute('INSERT INTO cloud_contracts VALUES(?,?,?)',
                                  ('watchlist:'+SYMBOL, self.at, json.dumps(certificate)))
        plan = dict(self.store.db.execute('SELECT * FROM plans').fetchone())
        self.authorize('watchlist', SYMBOL, plan)
        return plan

    def watchlist_slot(self, seconds=0):
        at = self.later(seconds)
        return slots.run_slot(self.store, self.cfg, at, clock=lambda: at,
                              refresh_fn=lambda *args: {'event_status': {SYMBOL: 'OK'}})

    def watchlist_buy(self):
        plan = self.watchlist_plan()
        self.quote()
        result = self.watchlist_slot()
        self.assertEqual(result['decisions'][0]['status'], 'SUBMITTED', result)
        return plan, dict(self.store.db.execute("SELECT * FROM paper_orders WHERE side='BUY'").fetchone())

    def case(self):
        analysis = {'novelty': 'NEW', 'priced_in': 'NO', 'direct_company_evidence': True}
        plan = {'holding_days': 3, 'max_position_pct': 5, 'stop_bps': 600,
                'take_profit_bps': 1000, 'buy_low_cents': 990, 'buy_high_cents': 1010}
        self.market = {'updated_at': self.at, 'name': NAME, 'bars': [['2026-09-14', 1000]],
                       'announcement_checked_at': self.at, 'announcement_status': 'OK', 'announcements': []}
        with self.store.db:
            self.store.db.execute('INSERT INTO dynamic_news VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                  ('news', 'synthetic', 'https://example.test/1', self.at, self.at,
                                   '标题', NAME+'原始新闻', 'cluster', None, 'ANALYZED', 1, self.at, 'synthetic'))
            self.store.db.execute('INSERT INTO dynamic_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                  ('case', 'news', SYMBOL, NAME, 'technology', 'APPROVAL', 'BULLISH',
                                   self.at, self.end, 'FORWARD', 'READY', json.dumps(analysis), json.dumps(plan)))
            self.store.db.execute('INSERT INTO dynamic_state VALUES(?,?)', ('news_watermark', self.at))
            self.store.db.execute('INSERT INTO dynamic_market VALUES(?,?,?)',
                                  (SYMBOL, self.at, json.dumps(self.market)))
            certificate = {'source_id': 'case', 'symbol': SYMBOL, 'history': {'passed': True, 'count': 30}}
            self.store.db.execute('INSERT INTO cloud_contracts VALUES(?,?,?)',
                                  ('dynamic:case', self.at, json.dumps(certificate)))
        case = dict(self.store.db.execute('SELECT * FROM dynamic_cases').fetchone())
        self.authorize('dynamic', 'case', case, target=500)
        self.assertEqual(dynamic.eligibility(self.store, self.cfg, case, self.at)[0], [])
        return case

    def dynamic_buy(self):
        case = self.case()
        q = self.quote(route='dynamic')
        order = dynamic_paper.submit(self.store, self.cfg, case, 'BUY', q, self.at, 'synthetic')
        self.assertEqual(order['status'], 'OPEN', order)
        return case, order

    def assert_cancelled(self, table, order):
        row = self.store.db.execute('SELECT * FROM '+table+' WHERE id=?', (order['id'],)).fetchone()
        self.assertEqual(row['status'], 'CANCELLED')
        self.assertEqual(row['reserved_cents'], 0)
        self.assertFalse(self.store.db.in_transaction)
        return row

    def test_st_new_entry_and_original_non_st_protections(self):
        self.watchlist_plan()
        self.quote(name='ST'+NAME)
        result = self.watchlist_slot()
        self.assertIn('SPECIAL_SECURITY', result['decisions'][0]['reason'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0], 0)
        for name in ('ST公司', '*ST公司', 'S*ST公司'):
            q = {'name': name, 'price_cents': 1000, 'prev_close_cents': 1000}
            self.assertEqual(paper.market_guard(SYMBOL, q, 'BUY'), 'SPECIAL_SECURITY')
            self.assertIsNone(paper.market_guard(SYMBOL, q, 'SELL'))
        for name in ('ST公司退', '退市公司', 'N公司', 'C公司'):
            q = {'name': name, 'price_cents': 1000, 'prev_close_cents': 1000}
            self.assertEqual(paper.market_guard(SYMBOL, q, 'SELL'), 'SPECIAL_SECURITY')
        q = {'name': 'ST公司', 'price_cents': 904, 'prev_close_cents': 1000}
        self.assertEqual(paper.market_guard(SYMBOL, q, 'SELL'), 'NEAR_PRICE_LIMIT_OR_CORPORATE_ACTION')
        self.assertEqual(paper.market_guard('sh900001', q, 'SELL'), 'UNSUPPORTED_BOARD')

    def test_watchlist_st_stop_cancels_buy_preserves_t1_and_sells_next_day(self):
        _, buy = self.watchlist_buy()
        self.quote(10)
        self.assertEqual(len(paper.settle(self.store, self.cfg, self.later(10))), 1)
        self.quote(60, 935, name='ST'+NAME)
        same_day = self.watchlist_slot(60)
        self.assert_cancelled('paper_orders', buy)
        self.assertEqual(same_day['decisions'][0]['action'], 'SELL')
        self.assertIn('T_PLUS_ONE', same_day['decisions'][0]['reason'])
        self.assertEqual(paper.positions(self.store, self.later(60))[SYMBOL]['qty'], 100)
        self.quote(86400, 935, name='ST'+NAME)
        next_day = self.watchlist_slot(86400)
        self.assertEqual(next_day['decisions'][0]['status'], 'SUBMITTED', next_day)
        self.quote(86410, 935, name='ST'+NAME)
        fills = paper.settle(self.store, self.cfg, self.later(86410))
        self.assertEqual([(f['side'], f['qty']) for f in fills], [('SELL', 100)])
        self.assertFalse(paper.positions(self.store, self.later(86410)))

    def test_watchlist_plan_stop_cancels_before_first_fill(self):
        _, order = self.watchlist_buy()
        self.quote(60, 935)
        self.assertEqual(paper.settle(self.store, self.cfg, self.later(60)), [])
        self.assert_cancelled('paper_orders', order)
        self.assertFalse(paper.positions(self.store, self.later(60)))
        self.assertEqual(paper.account(self.store, self.later(60))['cash_cents'], 10_000_000)

    def test_watchlist_price_improvement_below_entry_band_still_fills_above_stop(self):
        _, order = self.watchlist_buy()
        self.quote(60, 970)
        fills = paper.settle(self.store, self.cfg, self.later(60))
        self.assertEqual([(f['qty'], f['price_cents']) for f in fills], [(100, 970)])
        self.assertEqual(self.store.db.execute('SELECT status FROM paper_orders WHERE id=?', (order['id'],)).fetchone()[0], 'PARTIAL')

    def test_cloud_dynamic_announcement_failure_cancels_open_buy(self):
        case, order = self.dynamic_buy()
        self.market['announcement_status'] = 'FAILED'
        with self.store.db:
            self.store.db.execute('UPDATE dynamic_market SET payload_json=? WHERE symbol=?',
                                  (json.dumps(self.market), SYMBOL))
        self.quote(180, route='dynamic')
        blockers = dynamic.eligibility(self.store, self.cfg, case, self.later(180))[0]
        self.assertIn('动态公告目录覆盖不完整', blockers)
        result = dynamic_paper.run_slot(self.store, self.cfg, self.later(180), refresh=False)
        self.assertEqual(result['fills'], 0)
        self.assert_cancelled('dynamic_orders', order)
        self.assertEqual(paper.account(self.store, self.later(180))['cash_cents'], 10_000_000)

    def test_cloud_dynamic_new_news_cancels_remaining_partial_buy(self):
        case, order = self.dynamic_buy()
        self.quote(60, route='dynamic')
        self.assertEqual(len(dynamic_paper.settle(self.store, self.cfg, self.later(60))), 1)
        cash = paper.account(self.store, self.later(60))['cash_cents']
        with self.store.db:
            self.store.db.execute('INSERT INTO dynamic_news VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                  ('new', 'synthetic', 'https://example.test/2', self.later(120), self.later(120),
                                   '新消息', NAME+'新消息尚未研究', 'cluster2', None, 'NEW', 0, None, 'synthetic'))
        self.quote(120, route='dynamic')
        self.assertIn('有新增公司新闻尚未研究', dynamic.eligibility(self.store, self.cfg, case, self.later(120))[0])
        self.assertEqual(dynamic_paper.settle(self.store, self.cfg, self.later(120)), [])
        row = self.assert_cancelled('dynamic_orders', order)
        self.assertEqual(row['filled_qty'], 100)
        self.assertEqual(dynamic_paper.case_position(self.store, case['id'], self.later(120))['qty'], 100)
        self.assertEqual(paper.account(self.store, self.later(120))['cash_cents'], cash)

    def test_cloud_dynamic_valid_partial_fills_keep_cumulative_fee(self):
        _, order = self.dynamic_buy()
        for second in (10, 20, 30, 40):
            self.quote(second, route='dynamic')
            self.assertEqual(len(dynamic_paper.settle(self.store, self.cfg, self.later(second))), 1)
        row = self.store.db.execute('SELECT * FROM dynamic_orders WHERE id=?', (order['id'],)).fetchone()
        self.assertEqual(row['status'], 'FILLED')
        self.assertEqual(row['filled_qty'], order['qty'])
        self.assertEqual(row['reserved_cents'], 0)
        self.assertEqual(self.store.db.execute('SELECT sum(fee_cents) FROM dynamic_fills').fetchone()[0], 500)

    def test_dynamic_cost_stop_cancels_remaining_buy(self):
        case, order = self.dynamic_buy()
        self.quote(10, route='dynamic')
        dynamic_paper.settle(self.store, self.cfg, self.later(10))
        self.quote(60, 935, route='dynamic')
        self.assertEqual(dynamic.eligibility(self.store, self.cfg, case, self.later(60))[0], [])
        self.assertEqual(dynamic_paper.settle(self.store, self.cfg, self.later(60)), [])
        self.assert_cancelled('dynamic_orders', order)
        p = dynamic_paper.case_position(self.store, case['id'], self.later(60))
        self.assertEqual((p['qty'], p['sellable_qty']), (100, 0))

    def test_dynamic_st_sell_ignores_expired_entry_checks(self):
        self.cfg['paper_max_fill_qty'] = 10000
        case, buy = self.dynamic_buy()
        self.quote(10, route='dynamic')
        dynamic_paper.settle(self.store, self.cfg, self.later(10))
        q = self.quote(86400, 935, name='ST'+NAME, route='dynamic')
        self.assertTrue(dynamic.eligibility(self.store, self.cfg, case, self.later(86400))[0])
        sell = dynamic_paper.submit(self.store, self.cfg, case, 'SELL', q, self.later(86400), 'cost stop')
        self.assertEqual(sell['status'], 'OPEN', sell)
        self.quote(86410, 935, name='ST'+NAME, route='dynamic')
        self.assertEqual(len(dynamic_paper.settle(self.store, self.cfg, self.later(86410))), 1)
        self.assertEqual(dynamic_paper.case_position(self.store, case['id'], self.later(86410))['qty'], 0)
        self.assertEqual(self.store.db.execute("SELECT sum(qty) FROM dynamic_fills WHERE side='SELL'").fetchone()[0], buy['qty'])

    def global_quote(self, seconds, price):
        at = self.later(seconds)
        with self.store.db:
            self.store.db.execute('INSERT INTO global_quotes VALUES(?,?,?,?,?,?,?,?)',
                                  ('global:'+at, 'BTC', at, at, price*1_000_000, 7_000_000, at, '{}'))
        return latest(self.store, 'BTC', at)

    def test_global_cost_stop_cancels_remaining_partial_buy(self):
        item = {'asset': 'BTC', 'links': []}
        payload = {'kind': 'PAPER_TRADE', 'blockers': [], 'holding_days': 5,
                   'event_fingerprint': digest(json.dumps([], ensure_ascii=False, sort_keys=True)),
                   'levels': {'buy_low_micros': 99_000_000, 'buy_high_micros': 101_000_000}}
        with self.store.db:
            # Keep gains below the first withdrawal threshold while allowing an
            # entry larger than the CNY 5,000 fill cap (5% of CNY 140,000).
            self.store.db.execute('UPDATE paper_accounts SET cash_cents=14000000')
            self.store.db.execute('INSERT INTO global_plans VALUES(?,?,?,?,?,?,?)',
                                  ('global-plan', 'BTC', self.at, self.end, 'synthetic', 'ACTIVE', json.dumps(payload)))
        plan = dict(self.store.db.execute('SELECT * FROM global_plans').fetchone())
        self.authorize('global', 'BTC', plan, target=500, symbol='BTC')
        q = self.global_quote(0, 100)
        order = global_paper.submit(self.store, self.cfg, 'BTC', 'BUY', q, plan, item, self.at)
        self.assertEqual(order['status'], 'OPEN', order)
        self.global_quote(10, 100)
        self.assertEqual(len(global_paper.settle(self.store, self.cfg, self.later(10), {'BTC': item})), 1)
        first = dict(self.store.db.execute('SELECT * FROM global_orders').fetchone())
        self.assertEqual(first['status'], 'PARTIAL')
        q = self.global_quote(60, 93)
        held = global_paper.balance(self.store, self.later(60))['positions']['BTC']
        self.assertEqual(global_paper.exit_reason(self.store, self.cfg, 'BTC', held, q, self.later(60)), 'COST_STOP_TRIGGER')
        self.assertEqual(global_paper.settle(self.store, self.cfg, self.later(60), {'BTC': item}), [])
        cancelled = self.assert_cancelled('global_orders', order)
        self.assertEqual(cancelled['filled_qty'], first['filled_qty'])
        self.assertEqual(global_paper.balance(self.store, self.later(60))['positions']['BTC']['qty'], first['filled_qty'])


if __name__ == '__main__':
    unittest.main()
