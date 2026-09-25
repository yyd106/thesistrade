import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from ashare.storage import Store, normalize_time, digest
from ashare import paper, calendar
from ashare.paper import market_guard, board, lot_rules, sell_quantity
import test_investment_policy as investment_fixtures


def quote(name='普通股票', price=1000, prev=1000):
    return {'name': name, 'price_cents': price, 'prev_close_cents': prev}


class BoardRuleTests(unittest.TestCase):
    def test_boards_and_price_limit_guards(self):
        self.assertEqual(board('sh600519'), 'MAIN');self.assertEqual(board('sz300499'), 'CHINEXT')
        self.assertEqual(board('sz301001'), 'CHINEXT');self.assertEqual(board('sh688062'), 'STAR')
        self.assertIsNone(board('sh900901'));self.assertEqual(market_guard('sh900901', quote()), 'UNSUPPORTED_BOARD')
        # Main board stays clear of its 10% limit; ChiNext and STAR move up to 20%.
        self.assertEqual(market_guard('sh600000', quote(price=1096)), 'NEAR_PRICE_LIMIT_OR_CORPORATE_ACTION')
        self.assertIsNone(market_guard('sz300499', quote(price=1150)))
        self.assertIsNone(market_guard('sh688062', quote(price=850)))
        self.assertEqual(market_guard('sh688062', quote(price=1196)), 'NEAR_PRICE_LIMIT_OR_CORPORATE_ACTION')

    def test_special_names_match_prefixes_not_letters_anywhere(self):
        for name in ('ST中泰', '*ST海越', 'S*ST前锋', 'N迈威', 'C迈威', '退市海润', '海润退'):
            self.assertEqual(market_guard('sh600000', quote(name)), 'SPECIAL_SECURITY', name)
        for name in ('TCL科技', 'TCL中环', 'XD福耀玻', '迈威生物-U', '中国西电', 'COSCO'):
            self.assertIsNone(market_guard('sh600000', quote(name)), name)

    def test_star_lot_and_sell_declaration_rules(self):
        self.assertEqual(lot_rules('sh688062')['min_buy'], 200);self.assertEqual(lot_rules('sz300499')['min_buy'], 100)
        # Main board: sell whatever is sellable.
        self.assertEqual(sell_quantity('sh600000', 100, 300, 300), 100)
        # STAR: each sell at least 200 shares while 200+ are held; a holding below 200 goes in one order.
        self.assertEqual(sell_quantity('sh688062', 100, 300, 300), 200)
        self.assertEqual(sell_quantity('sh688062', 100, 300, 150), 0)
        self.assertEqual(sell_quantity('sh688062', 50, 150, 150), 150)
        self.assertEqual(sell_quantity('sh688062', 50, 150, 100), 0)
        self.assertEqual(sell_quantity('sh688062', 250, 400, 400), 250)


class PlanSizingTests(unittest.TestCase):
    def setUp(self):
        from test_config import load_config
        from ashare.demo import seed
        self.tmp = tempfile.TemporaryDirectory();self.store = Store(self.tmp.name)
        self.cfg = load_config(Path(__file__).resolve().parents[1] / 'config.json')
        self.cfg.update(data_dir=self.tmp.name, model_enabled=True, watchlist=[{'symbol': 'sz000333', 'name': '测试股'}])
        self.packet = seed(self.store, self.cfg)

    def tearDown(self):
        self.store.close();self.tmp.cleanup()

    def plan(self, packet):
        from ashare.research import price_plan
        return price_plan(packet, self.cfg, {'action': 'WATCH'}, 'SUCCEEDED')

    def test_lot_larger_than_stock_cap_blocks_plan_but_research_continues(self):
        self.assertEqual(self.packet['sizing']['min_buy_qty'], 100)
        self.assertNotIn('LOT_EXCEEDS_CAP', self.plan(self.packet)['blockers'])
        expensive = json.loads(json.dumps(self.packet))
        expensive['stocks'][0]['features']['unadjusted'].update(ma20_cents=123_700, ma60_cents=100_000, close_cents=123_700)
        self.assertIn('LOT_EXCEEDS_CAP', self.plan(expensive)['blockers'])
        # A larger account would fit the same lot: the block follows account scale, not the stock.
        expensive['sizing']['equity_cents'] = 100_000_000
        self.assertNotIn('LOT_EXCEEDS_CAP', self.plan(expensive)['blockers'])

    def test_unsupported_board_blocks_plan(self):
        other = json.loads(json.dumps(self.packet));other['sizing']['board'] = None
        self.assertIn('UNSUPPORTED_BOARD', self.plan(other)['blockers'])

    def test_guidance_and_followup_explain_lot_cap(self):
        from ashare.guidance import trade_guidance
        plan = {'payload': {'blockers': ['LOT_EXCEEDS_CAP'], 'basis': {}}, 'effective_status': 'ACTIVE'}
        groups = trade_guidance(self.store, self.cfg, 'sz000333', plan, self.packet, self.packet['as_of'])['groups']
        self.assertEqual(groups[0]['title'], '单手金额超过单股仓位上限')
        self.assertIn('不追加模拟本金', groups[0]['user_action'])


class WeekendFxTests(unittest.TestCase):
    later = investment_fixtures.InvestmentTests.later
    quote = investment_fixtures.InvestmentTests.quote
    plan = investment_fixtures.InvestmentTests.plan
    buy = investment_fixtures.InvestmentTests.buy
    def setUp(self):investment_fixtures.InvestmentTests.setUp(self)
    def tearDown(self):investment_fixtures.InvestmentTests.tearDown(self)

    def test_sell_uses_last_rate_over_weekend_but_buy_needs_current_rate(self):
        o, p, i = self.buy();self.quote(at=self.later(60))
        self.assertEqual(len(global_paper_settle(self, 60, i)), 1)
        # Two days later the FX quote is still Friday's; price is fresh and the stop is hit.
        later = self.later(2 * 86400)
        q = self.quote(price=90, at=later, fx_at=self.at)
        from ashare import global_paper
        blocked = global_paper.submit(self.store, self.cfg, 'ETH', 'BUY', self.quote('ETH', at=later, fx_at=self.at), *self.plan('ETH', at=later), later)
        self.assertEqual(blocked['status'], 'BLOCKED')
        sell = global_paper.submit(self.store, self.cfg, 'BTC', 'SELL', q, p, i, later)
        self.assertIn('id', sell, sell)
        terms = json.loads(sell['payload_json'])
        self.assertEqual(terms['reason'], 'COST_STOP_TRIGGER');self.assertTrue(terms['fx_stale'])
        fill_at = normalize_time((datetime.fromisoformat(later) + timedelta(seconds=60)).isoformat())
        self.quote(price=90, at=fill_at, fx_at=self.at)
        self.assertEqual(len(global_paper.settle(self.store, self.cfg, fill_at, {'BTC': i})), 1)

    def test_refresh_reuses_last_rate_with_its_own_timestamp(self):
        from ashare.global_market import refresh
        self.quote(fx_at=self.later(-600))
        def fetch(url):
            if 'CNY=X' in url:raise OSError('network down')
            return {'price': '101', 'time': self.later(30)}
        result = refresh(self.store, ['BTC'], self.later(30), fetch=fetch)
        self.assertEqual(result['updated'], 1);self.assertIn('沿用最近一次汇率', result['fx_error'])
        row = self.store.db.execute("SELECT fx_at FROM global_quotes WHERE symbol='BTC' ORDER BY observed_at DESC LIMIT 1").fetchone()
        self.assertEqual(row[0], self.later(-600))


def global_paper_settle(case, seconds, item):
    from ashare import global_paper
    return global_paper.settle(case.store, case.cfg, case.later(seconds), {'BTC': item})


class CalendarDataTests(unittest.TestCase):
    def test_listed_years_only_and_validation(self):
        self.assertIs(calendar.trading_day('2026-09-24'), True)
        self.assertIs(calendar.trading_day('2026-09-25'), False)
        self.assertIsNone(calendar.trading_day('2027-01-04'))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cal.json'
            path.write_text(json.dumps({'2027': {'version': 'X', 'source': 'https://example.test', 'holidays': [['2027-01-01', '2026-12-31']]}}))
            with self.assertRaises(ValueError):calendar.load(path)
            path.write_text(json.dumps({'2027': {'version': 'X', 'source': 'https://example.test', 'holidays': [['2027-01-01', '2027-01-03']]}}))
            self.assertIn(2027, calendar.load(path))

    def test_next_year_warning_starts_in_november(self):
        self.assertIsNone(calendar.next_year_warning('2026-10-31T12:00:00+08:00'))
        warning = calendar.next_year_warning('2026-11-01T12:00:00+08:00')
        self.assertEqual(warning['year'], 2027);self.assertEqual(warning['days_left'], 61)
        with patch.dict(calendar.YEARS, {2027: {'version': 'X', 'source': '', 'holidays': []}}):
            self.assertIsNone(calendar.next_year_warning('2026-12-20T12:00:00+08:00'))

    def test_followup_raised_before_year_end(self):
        from test_config import load_config
        from ashare.followups import reconcile
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            try:
                cfg = load_config(Path(__file__).resolve().parents[1] / 'config.json');cfg.update(data_dir=tmp, watchlist=[{'symbol': 'sh600000', 'name': '测试'}])
                result = reconcile(store, cfg, '2026-12-01T10:00:00+08:00')
                item = next(x for x in result['items'] if x['key'] == 'service:calendar-next-year')
                self.assertEqual(item['owner'], 'ENGINEERING');self.assertIn('2027', item['title'])
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
