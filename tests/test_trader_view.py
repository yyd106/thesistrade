"""Trader view (今日操作清单): pure presentation over the dashboard status dict."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from ashare import trader_view
from ashare.storage import Store

# Friday 2026-11-06 10:00 Beijing = 02:00 UTC, inside CONTINUOUS trading.
AT = '2026-11-06T02:00:00+00:00'
FRESH = '2026-11-06T01:59:30+00:00'
OLD = '2026-11-06T01:50:00+00:00'
CONFIG = {'paper_stop_loss_bps': 600, 'paper_max_gross_pct': 90}
LEVELS = {'buy_low_cents': 1500, 'buy_high_cents': 1600, 'stop_cents': 1400, 'sell_cents': 1800}
BASIS = {'ma20_cents': 1600, 'ma60_cents': 1550, 'close_cents': 1540}
REGIME = {'method': 'R1-v1', 'status': 'OK', 'as_of_day': '2026-11-05', 'close': 4100.0,
          'ma20': 4050.0, 'ma60': 3990.0, 'raw_cap_pct': 90, 'confirmed_cap_pct': 90, 'cap_pct': 90}


def quote(price, observed=FRESH, symbol='sh600519'):
    return {'symbol': symbol, 'price_cents': price, 'observed_at': observed, 'first_seen_at': observed}


def plan(kind='PAPER_TRADE', *, status='ACTIVE', valid='2026-11-09T07:00:00+00:00',
         levels=None, blockers=None, basis=None):
    return {'status': status, 'effective_status': status, 'valid_until': valid,
            'activated_at': '2026-11-05T01:00:00+00:00',
            'payload': {'kind': kind, 'levels': levels if levels is not None else dict(LEVELS),
                        'blockers': blockers or [], 'basis': basis or dict(BASIS)}}


def item(symbol='sh600519', name='测试股', **kw):
    return {'symbol': symbol, 'name': name, 'plan': None, 'report': None,
            'last_research_at': '2026-11-05T20:00:00+00:00', 'open_orders': [],
            'quote': None, 'membership': 'CORE', **kw}


def holding(symbol='sh600519', qty=100, cost=160000, div=0, sellable=None, weight=10.0, **kw):
    return {'symbol': symbol, 'origin': 'watchlist', 'name': '测试股', 'qty': qty,
            'sellable_qty': qty if sellable is None else sellable, 'cost_cents': cost,
            'dividend_cents': div, 'market_value_cents': qty * 1550, 'weight_pct': weight, **kw}


def decision(symbol='sh600519', action='ALLOW', target=1200, current=0, auth=True,
             reason='组合配置该标的'):
    return {'key': 'watchlist:' + symbol, 'route': 'watchlist', 'identity': symbol,
            'symbol': symbol, 'action': action, 'target_bps': target,
            'current_bps': current, 'reason': reason, 'current_authorization': auth}


def status(**kw):
    s = {'at': AT, 'market_phase': 'CONTINUOUS', 'quote_max_age_seconds': 90,
         'watchlist': [], 'portfolio': {'holdings': [], 'stock_weight_pct': 10.0,
                                        'cash_weight_pct': 90.0},
         'portfolio_strategy': {'enabled': True, 'status': 'ACTIVE',
                                'created_at': '2026-11-05T12:00:00+00:00',
                                'valid_until': '2026-11-07T12:00:00+00:00', 'decisions': []},
         'account': {'equity_cents': 10000000, 'orders': [],
                     'risk': {'halted': False, 'drawdown_bps': 0}},
         'market_regime': dict(REGIME), 'research_lease': {'active': True}}
    s.update(kw)
    return s


def ps(*decisions, status='ACTIVE', enabled=True, **kw):
    return {'enabled': enabled, 'status': status,
            'created_at': '2026-11-05T12:00:00+00:00',
            'valid_until': '2026-11-07T12:00:00+00:00',
            'decisions': list(decisions), **kw}


def build(s):
    return trader_view.build(s, CONFIG, buy_check=lambda p,q,t: {'status':'READY','qty':100})


def row_of(view, symbol='sh600519'):
    return next(r for r in view['rows'] if r['symbol'] == symbol)


class RowRuleTests(unittest.TestCase):
    """The numbered action rules of the brief, exercised one by one."""

    def test_held_halted_is_sell(self):
        s = status(account={'equity_cents': 10000000, 'orders': [],
                            'risk': {'halted': True, 'drawdown_bps': 2600}},
                   watchlist=[item(plan=plan(), quote=quote(1550))],
                   portfolio={'holdings': [holding()], 'stock_weight_pct': 10.0, 'cash_weight_pct': 90.0})
        r = row_of(build(s))
        self.assertEqual((r['action'], r['code']), ('SELL', 'ACCOUNT_DRAWDOWN_EXIT'))

    def test_cost_stop_boundary_is_sell_and_matches_slots(self):
        from ashare.slots import hard_reason
        for price, expect in ((1504, 'COST_STOP_TRIGGER'), (1505, 'HELD')):
            s = status(watchlist=[item(plan=plan(), quote=quote(price))],
                       portfolio={'holdings': [holding()], 'stock_weight_pct': 10.0,
                                  'cash_weight_pct': 90.0})
            r = row_of(build(s))
            self.assertEqual(r['code'], expect, price)
            p = {'qty': 100, 'cost_cents': 160000, 'dividend_cents': 0}
            self.assertEqual(hard_reason(quote(price), p, plan()['payload'], CONFIG)
                             == 'COST_STOP_TRIGGER', r['code'] == 'COST_STOP_TRIGGER')
        # Cost stop per share matches quote_health.stop_levels: 160000*0.94/100 = 1504.
        s = status(watchlist=[item(plan=plan(), quote=quote(1550))],
                   portfolio={'holdings': [holding()], 'stock_weight_pct': 10.0, 'cash_weight_pct': 90.0})
        self.assertEqual(row_of(build(s))['cost_stop_cents'], 1504)

    def test_cost_stop_counts_dividends(self):
        # (price*100+1000) <= 160000*0.94=150400 → trigger at 1494, not at 1495.
        for price, expect in ((1494, 'COST_STOP_TRIGGER'), (1495, 'HELD')):
            s = status(watchlist=[item(plan=plan(), quote=quote(price))],
                       portfolio={'holdings': [holding(div=1000)], 'stock_weight_pct': 10.0,
                                  'cash_weight_pct': 90.0})
            self.assertEqual(row_of(build(s))['code'], expect, price)

    def test_plan_stop_and_plan_exit_are_sell(self):
        s = status(watchlist=[item(plan=plan(), quote=quote(1400))],
                   portfolio={'holdings': [holding(cost=120000)], 'stock_weight_pct': 10.0,
                              'cash_weight_pct': 90.0})
        self.assertEqual(row_of(build(s))['code'], 'PLAN_STOP_TRIGGER')
        s = status(watchlist=[item(plan=plan(), quote=quote(1800))],
                   portfolio={'holdings': [holding(cost=120000)], 'stock_weight_pct': 10.0,
                              'cash_weight_pct': 90.0})
        self.assertEqual(row_of(build(s))['code'], 'PLAN_EXIT_TRIGGER')

    def test_stale_or_missing_quote_skips_stop_check(self):
        # Price far below every stop but the quote is stale: execution would not see it either.
        s = status(watchlist=[item(plan=plan(), quote=quote(100, OLD))],
                   portfolio={'holdings': [holding()], 'stock_weight_pct': 10.0,
                              'cash_weight_pct': 90.0})
        r = row_of(build(s))
        self.assertEqual((r['action'], r['code']), ('HOLD', 'HELD'))
        self.assertTrue(r['quote_stale'])
        self.assertIn('报价', r['reason'])
        s = status(watchlist=[item(plan=plan(), quote=None)],
                   portfolio={'holdings': [holding()], 'stock_weight_pct': 10.0,
                              'cash_weight_pct': 90.0})
        self.assertEqual(row_of(build(s))['code'], 'HELD')

    def test_expired_plan_keeps_only_cost_stop(self):
        # Execution ignores expired plan levels: price above the old target must not exit.
        expired = plan(valid='2026-11-06T01:00:00+00:00', status='EXPIRED')
        s = status(watchlist=[item(plan=expired, quote=quote(1800))],
                   portfolio={'holdings': [holding(cost=120000)], 'stock_weight_pct': 10.0,
                              'cash_weight_pct': 90.0})
        r = row_of(build(s))
        self.assertEqual(r['code'], 'HELD')
        self.assertIsNone(r['target_cents'])
        self.assertEqual(r['stop_cents'], 1128)  # 120000*0.94/100, cost stop only

    def test_portfolio_exit_and_reduce(self):
        s = status(watchlist=[item(plan=plan(), quote=quote(1550))],
                   portfolio={'holdings': [holding()], 'stock_weight_pct': 10.0,
                              'cash_weight_pct': 90.0},
                   portfolio_strategy=ps(decision(action='EXIT')))
        self.assertEqual(row_of(build(s))['code'], 'PORTFOLIO_EXIT')
        s = status(watchlist=[item(plan=plan(), quote=quote(1550))],
                   portfolio={'holdings': [holding()], 'stock_weight_pct': 10.0,
                              'cash_weight_pct': 90.0},
                   portfolio_strategy=ps(decision(action='REDUCE', target=500, current=1000)))
        r = row_of(build(s))
        self.assertEqual((r['action'], r['code']), ('REDUCE', 'PORTFOLIO_REDUCE'))
        self.assertEqual(r['suggested_pct'], 5.0)

    def test_held_allow_add_note(self):
        s = status(watchlist=[item(plan=plan(), quote=quote(1550))],
                   portfolio={'holdings': [holding(weight=5.0)], 'stock_weight_pct': 5.0,
                              'cash_weight_pct': 95.0},
                   portfolio_strategy=ps(decision(action='ALLOW', target=1200, current=500)))
        r = row_of(build(s))
        self.assertEqual(r['code'], 'HELD')
        self.assertIn('加仓', r['reason'])
        self.assertTrue(r['zone_actionable'])
        self.assertEqual(r['suggested_pct'], 12.0)
        # Stop is max(cost stop 1504, plan stop 1400); target is the active plan's sell level.
        self.assertEqual(r['stop_cents'], 1504)
        self.assertEqual(r['target_cents'], 1800)

    def test_held_allow_at_or_below_target_is_plain_hold(self):
        # Decisions carry no current_bps; the note compares target against held weight.
        s = status(watchlist=[item(plan=plan(), quote=quote(1550))],
                   portfolio={'holdings': [holding(weight=8.5)], 'stock_weight_pct': 8.5,
                              'cash_weight_pct': 91.5},
                   portfolio_strategy=ps(decision(action='ALLOW', target=800, current=0)))
        r = row_of(build(s))
        self.assertEqual(r['code'], 'HELD')
        self.assertNotIn('加仓', r['reason'])
        self.assertFalse(r['zone_actionable'])
        self.assertIsNone(r['suggested_pct'])

    def test_unheld_halted_and_no_plan_and_expired(self):
        s = status(account={'equity_cents': 10000000, 'orders': [],
                            'risk': {'halted': True, 'drawdown_bps': 2600}},
                   watchlist=[item(plan=plan(), quote=quote(1550))],
                   portfolio_strategy=ps(decision()))
        self.assertEqual(row_of(build(s))['code'], 'HALTED')
        s = status(watchlist=[item()], portfolio_strategy=ps(decision()))
        self.assertEqual(row_of(build(s))['code'], 'NO_PLAN')
        s = status(watchlist=[item(plan=plan(valid='2026-11-06T01:00:00+00:00', status='EXPIRED'),
                                   quote=quote(1550))],
                   portfolio_strategy=ps(decision()))
        self.assertEqual(row_of(build(s))['code'], 'PLAN_EXPIRED')

    def test_entry_paused(self):
        s = status(watchlist=[item(plan=plan(), quote=quote(1550), entry_allowed=False,
                                   entry_reason='研究依据待复核，暂停新增买入')],
                   portfolio_strategy=ps(decision()))
        r = row_of(build(s))
        self.assertEqual(r['code'], 'ENTRY_PAUSED')
        self.assertEqual(r['reason'], '研究依据待复核，暂停新增买入')

    def test_blocker_categories_and_priority(self):
        cases = [(['RESEARCH_VETO'], 'RESEARCH_VETO'),
                 (['UNRESOLVED_EVENT:doc1'], 'EVENT'),
                 (['CORPORATE_ACTION_UNVERIFIED:doc2'], 'EVENT'),
                 (['TREND_NOT_CONFIRMED'], 'TREND'),
                 (['LOT_EXCEEDS_CAP'], 'LOT'),
                 (['UNSUPPORTED_BOARD'], 'BOARD'),
                 (['RESEARCH_MEMBERSHIP_REVIEW'], 'MEMBERSHIP'),
                 (['SOURCE_GAP:tencent_daily'], 'DATA'),
                 (['UNREAD_DOCUMENT:doc3'], 'DATA'),
                 (['NO_COLLECTION_COVERAGE'], 'DATA'),
                 (['FINANCIAL_BASELINE_INCOMPLETE'], 'DATA'),
                 (['MARKET_CONTEXT_INCOMPLETE'], 'DATA'),
                 (['STALE_DAILY_BARS'], 'DATA'),
                 (['UNADJUSTED_SERIES_MISSING'], 'DATA'),
                 (['LOCAL_ONLY_DOCUMENTS_UNREVIEWED'], 'DATA'),
                 (['PRICE_DISCONTINUITY'], 'DATA'),
                 (['MODEL_NOT_READY'], 'MODEL'),
                 (['SOURCE_CHANGED_DURING_RESEARCH'], 'MODEL'),
                 (['MEMBERSHIP_CHANGED_DURING_RESEARCH'], 'MODEL'),
                 (['SOMETHING_NEW'], 'OTHER')]
        for blockers, code in cases:
            s = status(watchlist=[item(plan=plan('NO_ENTRY', blockers=blockers))],
                       portfolio_strategy=ps(decision()))
            r = row_of(build(s))
            self.assertEqual((r['action'], r['code']), ('WATCH', code), blockers)
            self.assertEqual(r['blocker_count'], len(blockers))
        # Veto outranks data gates and trend; count reflects all blockers.
        s = status(watchlist=[item(plan=plan('NO_ENTRY', blockers=[
            'SOURCE_GAP:x', 'TREND_NOT_CONFIRMED', 'RESEARCH_VETO']),
            report={'next_checks': ['观察下季度收入增速']})],
            portfolio_strategy=ps(decision()))
        r = row_of(build(s))
        self.assertEqual(r['code'], 'RESEARCH_VETO')
        self.assertIn('观察下季度收入增速', r['reason'])
        self.assertEqual(r['blocker_count'], 3)

    def test_trend_condition_uses_basis_numbers(self):
        s = status(watchlist=[item(plan=plan('NO_ENTRY', blockers=['TREND_NOT_CONFIRMED']))],
                   portfolio_strategy=ps(decision()))
        r = row_of(build(s))
        self.assertEqual(r['condition'],
                         '需收盘站上60日线（15.50 元），且20日线（16.00 元）高于60日线；'
                         '最近收盘 15.40 元。')

    def test_veto_next_check_is_shortened(self):
        long_check = '观察下季度收入增速' * 30  # 270 chars, far over the line budget
        s = status(watchlist=[item(plan=plan('NO_ENTRY', blockers=['RESEARCH_VETO']),
                                   report={'next_checks': [long_check]})],
                   portfolio_strategy=ps(decision()))
        r = row_of(build(s))
        self.assertEqual(r['code'], 'RESEARCH_VETO')
        self.assertIn('接下来观察：', r['reason'])
        self.assertLessEqual(len(r['reason']), len('研究判断暂不支持买入。接下来观察：') + 80)

    def test_data_wording_blames_system_not_stock(self):
        s = status(watchlist=[item(plan=plan('NO_ENTRY', blockers=['STALE_DAILY_BARS']))],
                   portfolio_strategy=ps(decision()))
        self.assertIn('数据', row_of(build(s))['reason'])

    def test_no_portfolio_and_portfolio_pause(self):
        s = status(watchlist=[item(plan=plan(), quote=quote(1550))],
                   portfolio_strategy=ps(status='EXPIRED', valid_until='2026-11-06T00:00:00+00:00'))
        r = row_of(build(s))
        self.assertEqual(r['code'], 'NO_PORTFOLIO')
        self.assertIn('等待组合', r['reason'])
        s = status(watchlist=[item(plan=plan(), quote=quote(1550))],
                   portfolio_strategy=ps())
        self.assertEqual(row_of(build(s))['code'], 'NO_PORTFOLIO')
        s = status(watchlist=[item(plan=plan(), quote=quote(1550))],
                   portfolio_strategy=ps(decision(action='PAUSE', reason='估值数据未就绪。第二部分')))
        r = row_of(build(s))
        self.assertEqual(r['code'], 'PORTFOLIO_PAUSE')
        self.assertEqual(r['portfolio_reason'], '估值数据未就绪')
        s = status(watchlist=[item(plan=plan(), quote=quote(1550))],
                   portfolio_strategy=ps(decision(action='ALLOW', auth=False)))
        self.assertEqual(row_of(build(s))['code'], 'PORTFOLIO_PAUSE')

    def test_quote_gates_before_buy(self):
        s = status(watchlist=[item(plan=plan(), quote=None)],
                   portfolio_strategy=ps(decision()))
        r = row_of(build(s))
        self.assertEqual(r['code'], 'NO_QUOTE')
        # Waiting rows still show the plan's protective levels and target size.
        self.assertEqual((r['stop_cents'], r['target_cents'], r['suggested_pct']),
                         (1400, 1800, 12.0))
        s = status(watchlist=[item(plan=plan(), quote=quote(1550, OLD))],
                   portfolio_strategy=ps(decision()))
        r = row_of(build(s))
        self.assertEqual((r['action'], r['code']), ('WAIT_PRICE', 'STALE_QUOTE'))
        self.assertEqual((r['stop_cents'], r['target_cents'], r['suggested_pct']),
                         (1400, 1800, 12.0))

    def test_buy_zone_boundaries(self):
        for price in (1500, 1550, 1600):
            s = status(watchlist=[item(plan=plan(), quote=quote(price))],
                       portfolio_strategy=ps(decision()))
            r = row_of(build(s))
            self.assertEqual((r['action'], r['code']), ('BUY', 'IN_ZONE'), price)
            self.assertEqual(r['suggested_pct'], 12.0)
            self.assertEqual(r['stop_cents'], 1400)
            self.assertEqual(r['target_cents'], 1800)
            self.assertTrue(r['zone_actionable'])

    def test_above_and_below_zone_distance(self):
        s = status(watchlist=[item(plan=plan(), quote=quote(1680))],
                   portfolio_strategy=ps(decision()))
        r = row_of(build(s))
        self.assertEqual(r['code'], 'ABOVE_ZONE')
        self.assertEqual(r['distance_pct'], 5.0)
        s = status(watchlist=[item(plan=plan(), quote=quote(1350))],
                   portfolio_strategy=ps(decision()))
        r = row_of(build(s))
        self.assertEqual(r['code'], 'BELOW_ZONE')
        self.assertEqual(r['distance_pct'], -10.0)
        self.assertIn('回到区间内', r['reason'])

    def test_closed_market_quote_never_stale(self):
        s = status(market_phase='CLOSED',
                   watchlist=[item(plan=plan(), quote=quote(1550, '2026-11-05T08:00:00+00:00'))],
                   portfolio_strategy=ps(decision()))
        r = row_of(trader_view.build(s, CONFIG, buy_check=lambda p,q,t: {'status':'BLOCKED','reason':'MARKET_CLOSED'}))
        self.assertFalse(r['quote_stale'])
        self.assertEqual((r['action'],r['code']), ('WATCH','MARKET_CLOSED'))

    def test_open_order_attached(self):
        order = {'id': 'o1', 'symbol': 'sh600519', 'side': 'BUY', 'qty': 200,
                 'filled_qty': 100, 'limit_cents': 1550, 'status': 'PARTIAL',
                 'created_at': AT}
        s = status(watchlist=[item(plan=plan(), quote=quote(1550), open_orders=[order])],
                   portfolio_strategy=ps(decision()))
        r = row_of(trader_view.build(s, CONFIG, buy_check=lambda p,q,t: {'status':'BLOCKED','reason':'EXISTING_OPEN_ORDER'}))
        self.assertEqual(r['open_order'],
                         {'side': 'BUY', 'qty': 200, 'filled_qty': 100,
                          'limit_cents': 1550, 'status': 'PARTIAL'})
        self.assertEqual((r['action'],r['code']), ('WATCH','EXISTING_OPEN_ORDER'))

    def test_held_but_unlisted_symbol_still_gets_row(self):
        s = status(portfolio={'holdings': [holding('sz000001', weight=8.0)],
                              'stock_weight_pct': 8.0, 'cash_weight_pct': 92.0},
                   portfolio_strategy=ps(decision('sz000001', action='EXIT')))
        view = build(s)
        r = row_of(view, 'sz000001')
        self.assertEqual((r['action'], r['code']), ('SELL', 'PORTFOLIO_EXIT'))
        s = status(portfolio={'holdings': [holding('sz000001', weight=8.0)],
                              'stock_weight_pct': 8.0, 'cash_weight_pct': 92.0})
        r = row_of(build(s), 'sz000001')
        self.assertEqual(r['code'], 'HELD')
        self.assertEqual(r['cost_stop_cents'], 1504)

    def test_dynamic_and_global_holdings_ignored(self):
        s = status(portfolio={'holdings': [holding('sz000001', origin='dynamic'),
                                         holding('US:SPY', origin='global')],
                              'stock_weight_pct': 20.0, 'cash_weight_pct': 80.0})
        self.assertEqual(build(s)['rows'], [])

    def test_row_keys_complete(self):
        s = status(watchlist=[item(plan=plan(), quote=quote(1550))],
                   portfolio_strategy=ps(decision()))
        r = row_of(build(s))
        self.assertEqual(set(r), {'symbol', 'name', 'action', 'code', 'headline', 'reason',
                                  'price_cents', 'quote_at', 'quote_stale', 'buy_low_cents',
                                  'buy_high_cents', 'stop_cents', 'target_cents',
                                  'cost_stop_cents', 'zone_actionable', 'suggested_pct',
                                  'current_pct', 'held_qty', 'sellable_qty', 'distance_pct',
                                  'condition', 'plan_valid_until', 'research_at',
                                  'portfolio_reason', 'open_order', 'blocker_count'})


class SortTests(unittest.TestCase):
    def test_action_groups_and_ordering(self):
        watch = [
            item('sh600001', plan=plan(), quote=quote(1550)),                       # BUY
            item('sh600002', plan=plan(), quote=quote(1700)),                       # ABOVE_ZONE d=6.25
            item('sh600003', plan=plan(), quote=quote(1608)),                       # ABOVE_ZONE d=0.5
            item('sh600004', plan=plan('NO_ENTRY', blockers=['STALE_DAILY_BARS'])), # WATCH DATA
            item('sh600005', plan=plan('NO_ENTRY', blockers=['RESEARCH_VETO'])),    # WATCH VETO
            item('sh600006', plan=plan(), quote=quote(1550)),                       # WATCH NO_PORTFOLIO
            item('sh600007', plan=plan(), quote=quote(1550)),                       # WATCH PAUSE
        ]
        decisions = [decision('sh600001'), decision('sh600002'), decision('sh600003'),
                     decision('sh600007', action='PAUSE')]
        s = status(watchlist=watch,
                   portfolio={'holdings': [holding('sh600009', cost=100000, weight=9.0),   # stop hit → SELL
                                           holding('sh600010', cost=100000, weight=3.0)],  # HOLD
                              'stock_weight_pct': 12.0, 'cash_weight_pct': 88.0},
                   portfolio_strategy=ps(*decisions))
        s['watchlist'] += [item('sh600009', plan=plan(), quote=quote(1400)),
                           item('sh600010', plan=plan(), quote=quote(1550))]
        view = build(s)
        order = [(r['symbol'], r['action']) for r in view['rows']]
        self.assertEqual(order[0], ('sh600009', 'SELL'))
        self.assertEqual(order[1], ('sh600001', 'BUY'))
        self.assertEqual([o[0] for o in order[2:4]], ['sh600003', 'sh600002'])  # |distance| asc
        self.assertEqual(order[4], ('sh600010', 'HOLD'))
        codes = [o[0] for o in order[5:]]
        self.assertEqual(codes, ['sh600007', 'sh600006', 'sh600005', 'sh600004'])

    def test_reduce_sorted_by_current_pct(self):
        s = status(watchlist=[item('sh600001', plan=plan(), quote=quote(1550)),
                              item('sh600002', plan=plan(), quote=quote(1550))],
                   portfolio={'holdings': [holding('sh600001', weight=4.0),
                                           holding('sh600002', weight=9.0)],
                              'stock_weight_pct': 13.0, 'cash_weight_pct': 87.0},
                   portfolio_strategy=ps(decision('sh600001', action='REDUCE', target=200),
                                         decision('sh600002', action='REDUCE', target=200)))
        self.assertEqual([r['symbol'] for r in build(s)['rows']], ['sh600002', 'sh600001'])


class RegimeSectionTests(unittest.TestCase):
    def regime(self, **kw):
        s = status(market_regime={**REGIME, **kw})
        return build(s)['regime']

    def test_ok_states(self):
        for cap, label, stance in ((90, '偏多', 'AGGRESSIVE'),
                                   (50, '震荡', 'NEUTRAL'), (10, '偏空', 'DEFENSIVE')):
            r = self.regime(confirmed_cap_pct=cap, raw_cap_pct=cap)
            self.assertEqual((r['cap_pct'], r['label'], r['stance']), (cap, label, stance))
        r = self.regime()
        self.assertEqual(r['conditions'], {'close_above_ma60': True, 'ma20_above_ma60': True})
        self.assertFalse(r['enforced'])
        self.assertIsNone(r['pending_change'])

    def test_pending_change(self):
        r = self.regime(confirmed_cap_pct=10, raw_cap_pct=50)
        self.assertEqual(r['cap_pct'], 10)
        self.assertIn('50', r['pending_change'])

    def test_grace_window(self):
        # as_of == last_completed(at-6h): fresh for display even though r1 caps at 10.
        r = self.regime(as_of_day='2026-11-05', confirmed_cap_pct=50, raw_cap_pct=50)
        self.assertFalse(r['stale'])
        self.assertEqual(r['cap_pct'], 50)
        # One day older than the grace cutoff: stale, conservative 10.
        r = self.regime(as_of_day='2026-11-04', confirmed_cap_pct=90, raw_cap_pct=90)
        self.assertTrue(r['stale'])
        self.assertEqual((r['cap_pct'], r['label']), (10, '数据过期'))

    def test_missing(self):
        r = self.regime(status='MISSING', as_of_day=None, confirmed_cap_pct=None,
                        close=None, ma20=None, ma60=None)
        self.assertTrue(r['stale'])
        self.assertEqual((r['cap_pct'], r['label']), (10, '数据过期'))
        self.assertEqual(r['conditions'], {'close_above_ma60': None, 'ma20_above_ma60': None})

    def test_no_regime_key_at_all(self):
        s = status()
        s.pop('market_regime')
        r = build(s)['regime']
        self.assertEqual((r['cap_pct'], r['label']), (10, '数据过期'))


class AccountAndFreshnessTests(unittest.TestCase):
    def test_suggested_max_and_over_cap(self):
        s = status(market_regime={**REGIME, 'confirmed_cap_pct': 50, 'raw_cap_pct': 50},
                   portfolio={'holdings': [], 'stock_weight_pct': 60.0, 'cash_weight_pct': 40.0})
        a = build(s)['account']
        self.assertEqual(a['suggested_max_pct'], 50)
        self.assertTrue(a['over_cap'])
        s = status(portfolio={'holdings': [], 'stock_weight_pct': 50.0, 'cash_weight_pct': 50.0})
        a = build(s)['account']
        self.assertEqual(a['suggested_max_pct'], 90)
        self.assertFalse(a['over_cap'])

    def test_freshness_stale_after_8h_and_expired_and_lease(self):
        s = status(watchlist=[item(plan=plan(), quote=quote(1550),
                                   last_research_at='2026-11-05T10:00:00+00:00')],
                   portfolio_strategy=ps(decision()))
        f = build(s)['freshness']
        self.assertTrue(f['stale'])
        self.assertTrue(any('8' in n for n in f['notes']))
        s = status(watchlist=[item(plan=plan(), quote=quote(1550))],
                   portfolio_strategy=ps(decision(), status='EXPIRED',
                                         valid_until='2026-11-06T00:00:00+00:00'))
        self.assertTrue(build(s)['freshness']['stale'])
        s = status(watchlist=[item(plan=plan(), quote=quote(1550))],
                   portfolio_strategy=ps(decision()), research_lease={'active': False})
        f = build(s)['freshness']
        self.assertTrue(f['stale'])
        self.assertTrue(any('心跳' in n for n in f['notes']))

    def test_counts_and_notice(self):
        s = status(watchlist=[item('sh600001', plan=plan(), quote=quote(1550)),
                              item('sh600002')],
                   portfolio_strategy=ps(decision('sh600001')))
        view = build(s)
        self.assertEqual(view['counts'],
                         {'SELL': 0, 'REDUCE': 0, 'BUY': 1, 'WAIT_PRICE': 0,
                          'HOLD': 0, 'WATCH': 1})
        self.assertIn('总资金', view['notice'])

    def test_build_does_not_mutate_status(self):
        s = status(watchlist=[item(plan=plan('NO_ENTRY', blockers=['TREND_NOT_CONFIRMED']),
                                   quote=quote(1550)),
                              item('sh600002', plan=plan(), quote=quote(1550))],
                   portfolio={'holdings': [holding('sh600003')], 'stock_weight_pct': 10.0,
                              'cash_weight_pct': 90.0},
                   portfolio_strategy=ps(decision('sh600002'),
                                         decision('sh600003', action='REDUCE', target=500)))
        s['watchlist'].append(item('sh600003', plan=plan(), quote=quote(1550)))
        before = copy.deepcopy(s)
        build(s)
        self.assertEqual(s, before)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cloud_tmp = tempfile.TemporaryDirectory()
        import test_config
        self.cfg = test_config.load_config(
            Path(__file__).resolve().parents[1] / 'config.example.json')
        self.cfg.update(data_dir=self.tmp.name, scheduler_enabled=False, model_enabled=False)
        self.cloud_cfg = {**self.cfg, 'deployment_role': 'cloud', 'data_dir': self.cloud_tmp.name}

    def tearDown(self):
        self.tmp.cleanup()
        self.cloud_tmp.cleanup()

    def test_research_status_includes_regime_and_view(self):
        from ashare.dashboard import status as dashboard_status
        s = dashboard_status(self.cfg)
        self.assertIn('market_regime', s)
        self.assertIn('trader_view', s)
        self.assertEqual(s['market_regime']['status'], 'MISSING')
        self.assertEqual(s['trader_view']['version'], trader_view.VERSION)
        self.assertEqual(s['trader_view']['regime']['cap_pct'], 10)

    def test_display_packet_carries_regime_not_view(self):
        from ashare import cloud_sync
        packet = cloud_sync.display_packet(self.cfg)
        self.assertIn('market_regime', packet)
        self.assertNotIn('trader_view', packet)

    def test_cloud_status_builds_view_from_display_cache(self):
        from ashare.cloud_dashboard import status as cloud_status
        from ashare.cloud_runtime import put
        from ashare.finance import PaperLedger
        cloud = Store(self.cloud_tmp.name)
        try:
            PaperLedger(cloud).initialize()
            regime = {'method': 'R1-v1', 'status': 'OK', 'as_of_day': '2026-11-05',
                      'close': 4100.0, 'ma20': 4050.0, 'ma60': 3990.0,
                      'raw_cap_pct': 90, 'confirmed_cap_pct': 90, 'cap_pct': 90}
            with cloud.db:
                put(cloud, 'research_completed_at', '2026-11-05T00:00:00+00:00')
                put(cloud, 'research_received_at', '2026-11-05T00:00:00+00:00')
                put(cloud, 'initialized', True)
                put(cloud, 'execution_enabled', True)
                put(cloud, 'display', {'watchlist': [], 'observation': {'items': []},
                                       'reviews': [], 'market_regime': regime})
            from unittest.mock import patch
            with patch('ashare.storage.now', return_value='2026-11-06T02:00:00+00:00'), \
                 patch('ashare.cloud_dashboard.now', return_value='2026-11-06T02:00:00+00:00'):
                s = cloud_status(cloud, self.cloud_cfg)
            self.assertIn('trader_view', s)
            self.assertEqual(s['trader_view']['regime']['cap_pct'], 90)
            # Without the cached section the view degrades to the conservative MISSING cap.
            with cloud.db:
                put(cloud, 'display', {'watchlist': [], 'observation': {'items': []},
                                       'reviews': []})
            with patch('ashare.storage.now', return_value='2026-11-06T02:00:00+00:00'), \
                 patch('ashare.cloud_dashboard.now', return_value='2026-11-06T02:00:00+00:00'):
                s = cloud_status(cloud, self.cloud_cfg)
            self.assertEqual(s['trader_view']['regime']['cap_pct'], 10)
        finally:
            cloud.close()

    def test_guest_status_drops_new_keys(self):
        from ashare import privacy
        s = status()
        s['trader_view'] = build(s)
        s['trader_view']['rows'] = [{'symbol': 'sh600519', 'secret': 'x'}]
        public = privacy.guest_status(s)
        self.assertNotIn('trader_view', public)
        self.assertNotIn('market_regime', public)
        self.assertNotIn('secret', json.dumps(public))


if __name__ == '__main__':
    unittest.main()
