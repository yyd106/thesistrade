"""Guest HTTP responses expose results, never research or its alternate routes."""
import copy
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ashare import auth
from ashare.dashboard import make_handler
from ashare.storage import Store
import test_dashboard as fixtures


PRIVATE = 'PRIVATE_RESEARCH_SENTINEL'
AT = '2026-10-24T00:00:00+00:00'


def research_fixture():
    plan = {'id': 'p1', 'study_id': 's1', 'activated_at': AT, 'valid_until': AT,
            'effective_status': 'ACTIVE', 'research': PRIVATE, 'trade_guidance': PRIVATE,
            'payload': {'kind': 'PAPER_TRADE', 'levels': {'buy_low_cents': 1000, 'buy_high_cents': 1100,
                        'sell_cents': 1200, 'stop_cents': 900, 'calculation': PRIVATE},
                        'thesis': PRIVATE, 'blockers': [PRIVATE], 'research_rules': PRIVATE}}
    item = {'symbol': 'sh600519', 'name': '测试股票', 'membership': 'CORE', 'research_status': 'TRACKING',
            'reason': PRIVATE, 'entry_reason': PRIVATE, 'plan': plan, 'report': {'overview': PRIVATE},
            'quote': {'price_cents': 1050, 'observed_at': AT, 'raw_path': PRIVATE, 'payload_json': PRIVATE},
            'last_decision': {'action': 'HOLD', 'status': 'BLOCKED', 'at': AT, 'reason': PRIVATE,
                              'buy_blockers': [PRIVATE], 'payload_json': PRIVATE},
            'open_orders': [{'side': 'BUY', 'qty': 100, 'status': 'OPEN', 'reason': PRIVATE}]}
    archived = {**item, 'symbol': 'sz000001', 'name': PRIVATE, 'research_status': 'ARCHIVED'}
    observed = {'asset': 'US:AAPL', 'name': 'Apple', 'category': 'US', 'pool_tier': 'FOCUS',
                'trade_plan': plan, 'links': [PRIVATE], 'pool_reason': PRIVATE, 'recheck': PRIVATE,
                'spot_quote': {'price_micros': 123000000, 'payload_json': PRIVATE}, 'entry_reason': PRIVATE}
    holding = {'symbol': 'sh600519', 'name': '测试股票', 'qty': 100, 'market_value_cents': 105000,
               'cost_cents': 100000, 'research': PRIVATE}
    return {'at': AT, 'mode': 'paper', 'market_phase': 'CLOSED', 'deployment_role': 'standalone',
            'watchlist': [item, archived], 'fixed_watchlist': [{'symbol': 'sh600519', 'name': '测试股票'}],
            'industry': {'enabled': True, 'members': [item, archived], 'hypotheses': [PRIVATE], 'history': [PRIVATE]},
            'observation': {'items': [observed, {**observed, 'asset': PRIVATE, 'pool_tier': 'COOLING'}],
                            'archived_items': [PRIVATE], 'archive_count': 1, 'membership': {PRIVATE: PRIVATE}},
            'portfolio_strategy': PRIVATE, 'followups': PRIVATE, 'reviews': [PRIVATE], 'supervision': PRIVATE,
            'strategy_freshness': PRIVATE, 'documents': 4, 'snapshots': 3, 'notices': [PRIVATE],
            'account': {'equity_cents': 10100000, 'positions': {'sh600519': holding}, 'research': PRIVATE,
                        'orders': [{'side': 'BUY', 'qty': 100, 'reason': PRIVATE}],
                        'risk': {'halted': False, 'drawdown_bps': 0, 'payload': PRIVATE}, 'investment_policy': PRIVATE},
            'portfolio': {'holdings': [holding], 'total_profit_cents': 100000, 'research': PRIVATE},
            'trade_effects': {'buy': {'symbol': 'sh600519', 'filled_qty': 100, 'reason': PRIVATE},
                              'sell': None, 'totals': {'fill_count': 1, 'reason': PRIVATE}},
            'decisions': [{'action': 'BUY', 'status': 'SUBMITTED', 'at': AT, 'reason': PRIVATE, 'payload_json': PRIVATE}],
            'fills': [{'symbol': 'sh600519', 'qty': 100, 'price_cents': 1000, 'payload_json': PRIVATE}],
            'dynamic': {'enabled': True, 'items': [PRIVATE], 'global': {'items': [PRIVATE]}, 'validation': PRIVATE,
                        'fills': [{'symbol': 'sz000333', 'qty': 100, 'reason': PRIVATE}]},
            'state': {'heartbeat': AT, 'last_error': PRIVATE}, 'unknown_future_section': PRIVATE}


class ResearchPrivacyHttpTests(unittest.TestCase):
    request = fixtures.DashboardTests.request

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'config.json'
        self.cfg = json.loads((Path(__file__).resolve().parents[1] / 'config.example.json').read_text())
        self.cfg.update(data_dir=str(Path(self.tmp.name) / 'data'), scheduler_enabled=False, model_enabled=False)
        self.path.write_text(json.dumps(self.cfg))
        with closing(Store(self.cfg['data_dir'])) as store:
            auth.setup(store, 'test-admin-password', 'test-guest-password')
            token, _ = auth.login(store, 'admin', 'test-admin-password', 'privacy-admin')
            self.cookie = auth.COOKIE + '=' + token
            token, _ = auth.login(store, 'guest', 'test-guest-password', 'privacy-guest')
            self.guest = {'Cookie': auth.COOKIE + '=' + token}
        self.handler = make_handler(self.path, 'page-token', 8765)

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_allowlist_retains_results_and_removes_every_duplicate_research_path(self):
        original = research_fixture()
        before = copy.deepcopy(original)
        with patch('ashare.dashboard.status', return_value=original):
            code, raw, _ = self.request('GET', '/api/status?role=ADMIN', headers=self.guest)
            self.assertEqual(code, 200, raw)
            result = json.loads(raw)
            self.assertNotIn(PRIVATE, raw.decode())
            self.assertFalse(result['research_access'])
            self.assertEqual([r['symbol'] for r in result['watchlist']], ['sh600519'])
            stock = result['watchlist'][0]
            self.assertEqual(stock['plan']['payload']['levels']['buy_low_cents'], 1000)
            self.assertEqual(stock['quote']['price_cents'], 1050)
            self.assertEqual(stock['last_decision'], {'action': 'HOLD', 'status': 'BLOCKED', 'at': AT})
            self.assertEqual(result['portfolio']['holdings'][0]['qty'], 100)
            self.assertEqual(result['fills'][0]['price_cents'], 1000)
            self.assertEqual([r['asset'] for r in result['observation']['items']], ['US:AAPL'])
            self.assertEqual(result['observation']['archive_count'], 0)
            self.assertNotIn('hypotheses', result['industry'])
            self.assertIsNone(result['portfolio_strategy'])
            self.assertIsNone(result['followups'])
            self.assertEqual(original, before)
            code, raw, _ = self.request('GET', '/api/status')
            self.assertEqual((code, json.loads(raw)), (200, before))

    def test_cloud_cache_uses_same_projection_and_unknown_roles_fail_closed(self):
        self.cfg.update(deployment_role='cloud', public_origin='https://privacy.example')
        self.path.write_text(json.dumps(self.cfg))
        self.handler = make_handler(self.path, 'page-token', 8765)
        with closing(Store(self.cfg['data_dir'])) as store, store.db:
            store.db.execute("UPDATE app_users SET role='READER' WHERE username='guest'")
        with patch('ashare.cloud_dashboard.status', return_value=research_fixture()):
            code, raw, _ = self.request('GET', '/api/status', headers=self.guest)
        self.assertEqual(code, 200, raw)
        self.assertNotIn(PRIVATE, raw.decode())
        self.assertFalse(json.loads(raw)['research_access'])

    def test_real_synthetic_research_keeps_quote_levels_and_account_values(self):
        from ashare.demo import SYMBOL, seed, research_model
        from ashare.pipeline import load_config
        from ashare.research import study
        cfg = load_config(self.path)
        cfg['watchlist'] = [{'symbol': SYMBOL, 'name': '合成测试股票'}]
        self.path.write_text(json.dumps(cfg))
        with closing(Store(cfg['data_dir'])) as store:
            packet = seed(store, cfg)
            study(store, cfg, packet, model_fn=research_model(packet), at='2026-09-15T09:01:00+08:00')
        code, raw, _ = self.request('GET', '/api/status')
        self.assertEqual(code, 200, raw)
        full = json.loads(raw)
        code, raw, _ = self.request('GET', '/api/status', headers=self.guest)
        self.assertEqual(code, 200, raw)
        public = json.loads(raw)
        self.assertEqual(public['watchlist'][0]['quote']['price_cents'], 1000)
        self.assertEqual(public['watchlist'][0]['plan']['payload']['levels'], full['watchlist'][0]['plan']['payload']['levels'])
        for key in ('initial_cents', 'cash_cents', 'available_cents', 'equity_cents', 'withdrawn_cents'):
            self.assertEqual(public['account'][key], full['account'][key])
        for key in ('total_profit_cents', 'stock_weight_pct', 'cash_weight_pct'):
            self.assertEqual(public['portfolio'][key], full['portfolio'][key])
        for key in ('threshold_cents', 'retain_cents', 'candidate_cents'):
            self.assertEqual(public['withdrawals'][key], full['withdrawals'][key])
        code, raw, _ = self.request('GET', '/api/stock?symbol=' + SYMBOL, headers=self.guest)
        self.assertEqual(code, 200, raw)
        self.assertEqual(json.loads(raw)['stock']['plan']['payload']['levels'], public['watchlist'][0]['plan']['payload']['levels'])

    def test_stock_and_paginated_trade_history_expose_results_without_research(self):
        source = research_fixture()
        value = {'at': AT, 'stock': source['watchlist'][0], 'industry': source['industry'],
                 'dimensions': [PRIVATE], 'followups': PRIVATE, 'position': source['portfolio']['holdings'][0],
                 'trade_statistics': {'fill_count': 1, 'fee_cents': 500, 'realized_cents': 0},
                 'history': {key: {'items': rows, 'total': 80, 'offset': 30, 'limit': 30, 'research': PRIVATE}
                             for key, rows in [('fills', source['fills']), ('orders', source['account']['orders']),
                                               ('decisions', source['decisions'])]}}
        before = copy.deepcopy(value)
        with patch('ashare.stock_detail.detail', return_value=value) as detail:
            code, raw, _ = self.request('GET', '/api/stock?symbol=sh600519&offset=30', headers=self.guest)
            self.assertEqual(code, 200, raw)
            self.assertEqual(detail.call_args.args[1:], ('sh600519', 30))
            self.assertNotIn(PRIVATE, raw.decode())
            result = json.loads(raw)
            self.assertEqual(result['history']['fills']['total'], 80)
            self.assertEqual(result['history']['fills']['items'][0]['qty'], 100)
            self.assertEqual(result['dimensions'], [])
            self.assertIsNone(result['industry'])
            self.assertEqual(value, before)
            self.assertEqual(json.loads(self.request('GET', '/api/stock?symbol=sh600519')[1]), before)
            value['stock'] = source['watchlist'][1]
            self.assertEqual(self.request('GET', '/api/stock?symbol=sz000001', headers=self.guest)[0], 403)
            self.assertEqual(self.request('GET', '/api/stock?symbol=sz000001')[0], 200)

    def test_research_routes_reject_guests_before_any_research_read(self):
        self.cfg.update(deployment_role='cloud', public_origin='https://privacy.example')
        self.path.write_text(json.dumps(self.cfg))
        self.handler = make_handler(self.path, 'page-token', 8765)
        paths = ['/api/document?id=known-preview', '/api/observations?offset=25&asset=US:AAPL',
                 '/api/strategy-export', '/api/snapshot?id=known', '/api/search?q=known', '/api/job?id=known']
        with patch('ashare.cloud_runtime.value', return_value={'document_previews': {'known-preview': PRIVATE}}) as read_cache:
            for path in paths:
                with self.subTest(path=path):
                    code, raw, _ = self.request('GET', path, headers=self.guest)
                    self.assertEqual((code, json.loads(raw)['code']), (403, 'ADMIN_REQUIRED'))
                    self.assertEqual(self.request('GET', path, headers={'Cookie': ''})[0], 401)
            read_cache.assert_not_called()
            code, raw, _ = self.request('GET', '/api/document?id=known-preview')
            self.assertEqual((code, json.loads(raw)), (200, PRIVATE))

    def test_withdrawal_alternate_endpoint_does_not_leak_plan_or_portfolio_constraints(self):
        value = {'current_stage': 1, 'status': 'READY', 'candidate_cents': 4000000, 'can_transfer': True,
                 'blockers': [PRIVATE], 'cycle': {'stage': 1, 'reserved_cents': 4000000, 'constraints': PRIVATE}}
        with patch('ashare.withdrawals.status', return_value=value):
            code, raw, _ = self.request('GET', '/api/withdrawals', headers=self.guest)
            self.assertEqual(code, 200, raw)
            self.assertNotIn(PRIVATE, raw.decode())
            self.assertEqual(json.loads(raw)['candidate_cents'], 4000000)
            self.assertFalse(json.loads(raw)['can_transfer'])
            self.assertEqual(json.loads(self.request('GET', '/api/withdrawals')[1]), value)


if __name__ == '__main__':
    unittest.main()
