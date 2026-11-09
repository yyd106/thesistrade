"""Board-aware price-discontinuity thresholds keyed on verified listing dates; real public
bars trimmed to the needed windows. Tests inject listing tables and never read the shipped file."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from daily_bar_fixtures import S301563_LAST90, S301563_FIRST10, S301682_LAST90, S301682_FIRST10, S301563_CA_EVENT
from ashare import research


def bars(raw):
    return [b.split(',') for b in raw]


def flat(dates, closes):
    return [[d, c, c, c, c, '1'] for d, c in zip(dates, closes)]


def days(first, n):
    from datetime import date, timedelta
    d = date.fromisoformat(first)
    return [(d + timedelta(days=i)).isoformat() for i in range(n)]


# Injected listing tables; the shipped file is checked only by the loader test.
L563 = {'sz301563': {'listed_on': '2025-09-30', 'price_limit_from': '2025-10-15'}}
L682 = {'sz301682': {'listed_on': '2026-03-25', 'price_limit_from': '2026-04-01'}}
OLD = {'listed_on': '2020-01-02', 'price_limit_from': '2020-01-10'}


class PriceDiscontinuityTests(unittest.TestCase):
    def test_chinext_limit_move_not_flagged(self):
        # sz301563 云汉芯城: last 90 bars contain only 2026-10-22 +16.2%.
        self.assertFalse(research.price_discontinuity('sz301563', bars(S301563_LAST90), L563))
        # sz301682 宏明电子: 2026-10-27 +20.0% and 2026-10-28 +15.6% are legal 20%-board moves.
        self.assertFalse(research.price_discontinuity('sz301682', bars(S301682_LAST90), L682))

    def test_corporate_action_still_flagged(self):
        # Real unadjusted bars: sz301563 fell 242.61 -> 174.07 (-28.25%) on its 2026-05-29
        # ex-rights day — beyond the 20% ChiNext limit, so flagged under the board rule.
        self.assertTrue(research.price_discontinuity('sz301563', bars(S301563_CA_EVENT), L563))
        # A 28% jump inside an ordinary window flags regardless of board.
        b = [['2026-01-%02d' % d, '100', '100', '100', '100', '1'] for d in range(1, 31)]
        b += [['2026-02-02', '100', '129', '130', '99', '1']]  # +29%
        self.assertTrue(research.price_discontinuity('sz301563', b, L563))
        self.assertTrue(research.price_discontinuity('sh600519', b, {}))
        self.assertTrue(research.price_discontinuity('sh688001', b, {'sh688001': OLD}))

    def test_main_board_still_uses_strict_threshold(self):
        b = [['2026-01-%02d' % d, '100', '100', '100', '100', '1'] for d in range(1, 31)]
        b += [['2026-02-02', '100', '116', '116', '100', '1']]  # +16%
        self.assertTrue(research.price_discontinuity('sh600519', b, {}))
        self.assertFalse(research.price_discontinuity('sz301563', b, L563))

    def test_real_listing_days_one_to_five_strict_day_six_board(self):
        # Real IPO dates: 09-30, 10-09, 10-10, 10-13, 10-14 are the five no-limit days;
        # 10-15 is day six, the first day the 20% limit applies.
        ipo_days = ['2025-09-30', '2025-10-09', '2025-10-10', '2025-10-13', '2025-10-14']
        for i, d in enumerate(ipo_days[1:], start=1):  # transitions ending on days 2-5
            b = flat([ipo_days[i - 1], d], ['100', '118'])  # +18%
            self.assertTrue(research.price_discontinuity('sz301563', b, L563), d)
        b = flat(['2025-10-14', '2025-10-15'], ['100', '118'])  # day5 -> day6: board limit
        self.assertFalse(research.price_discontinuity('sz301563', b, L563))
        # The real series: day 2 closed +40.9% (no limit yet) — flagged under the strict rule.
        self.assertTrue(research.price_discontinuity('sz301563', bars(S301563_FIRST10[:5]), L563))
        self.assertTrue(research.price_discontinuity('sz301563', bars(S301563_FIRST10 + S301563_LAST90), L563))
        # sz301682's real days 1-5 moved little; a synthetic +18% inside them still flags.
        b = flat(['2026-03-30', '2026-03-31'], ['100', '118'])
        self.assertTrue(research.price_discontinuity('sz301682', b, L682))

    def test_listing_series_flagged_at_any_length(self):
        # The reviewer series: first row is the IPO day, day 2 +18%, later unchanged.
        # Flagged at 60/84/85/86/89/90 bars — the count never decides the window.
        listing = {'sz301999': {'listed_on': '2026-05-01', 'price_limit_from': '2026-05-12'}}
        for n in (60, 84, 85, 86, 89, 90):
            b = flat(days('2026-05-01', n), ['100', '118'] + ['118'] * (n - 2))
            self.assertTrue(research.price_discontinuity('sz301999', b, listing), n)

    def test_mature_stock_with_short_series_not_flagged(self):
        # Old verified listing, only 60 returned bars, first transition +18%: board limit.
        listing = {'sz301999': OLD}
        b = flat(days('2026-01-01', 60), ['100', '118'] + ['118'] * 58)
        self.assertFalse(research.price_discontinuity('sz301999', b, listing))
        # STAR board, same shape.
        self.assertFalse(research.price_discontinuity('sh688999', b, {'sh688999': OLD}))

    def test_window_left_edge_cutting_day_one_stays_strict(self):
        # The window begins at listing day 2; days 2-5 still have no price limit.
        b = flat(['2025-10-09', '2025-10-10', '2025-10-13', '2025-10-14'], ['100', '118', '118', '118'])
        self.assertTrue(research.price_discontinuity('sz301563', b, L563))
        # Days 2-6: the strict part flags, the day-6 transition itself would not at +18%.
        b = flat(['2025-10-13', '2025-10-14', '2025-10-15'], ['100', '100', '118'])
        self.assertFalse(research.price_discontinuity('sz301563', b, L563))
        b = flat(['2025-10-13', '2025-10-14', '2025-10-15'], ['100', '118', '118'])
        self.assertTrue(research.price_discontinuity('sz301563', b, L563))

    def test_unknown_listing_uses_strict_threshold(self):
        # ChiNext +18% with no listing entry: the explicit conservative state.
        b = flat(days('2026-01-01', 60), ['100', '118'] + ['118'] * 58)
        self.assertTrue(research.price_discontinuity('sz301999', b, {}))
        self.assertTrue(research.price_discontinuity('sh688999', b, {}))

    def test_exact_boundaries_both_directions(self):
        listing = {'sz301999': OLD, 'sh688999': OLD}
        for symbol in ('sz301999', 'sh688999'):
            for close, expected in (('121', False), ('121.01', True), ('79', False), ('78.99', True)):
                b = flat(['2026-01-01', '2026-01-02'], ['100', close])
                self.assertEqual(research.price_discontinuity(symbol, b, listing), expected, (symbol, close))
        for close, expected in (('115', False), ('115.01', True), ('85', False), ('84.99', True)):
            b = flat(['2026-01-01', '2026-01-02'], ['100', close])
            self.assertEqual(research.price_discontinuity('sh600519', b, {}), expected, close)

    def test_listing_dates_loader_validates_entries(self):
        raw = {'_说明': 'x',
               'ok': {'listed_on': '2025-09-30', 'price_limit_from': '2025-10-15', 'source': 'https://a'},
               'bad_iso': {'listed_on': 'x', 'price_limit_from': '2025-10-15', 'source': 'https://a'},
               'reversed': {'listed_on': '2025-10-15', 'price_limit_from': '2025-09-30', 'source': 'https://a'},
               'insecure': {'listed_on': '2025-09-30', 'price_limit_from': '2025-10-15', 'source': 'http://a'},
               'bare': '2025-09-30'}
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(raw, f)
        loaded = research.listing_dates(f.name)
        self.assertEqual(set(loaded), {'ok'})
        self.assertEqual(loaded['ok'], {'listed_on': '2025-09-30', 'price_limit_from': '2025-10-15'})
        self.assertEqual(research.listing_dates(f.name + '.missing'), {})
        self.assertTrue(research.listing_dates())  # the shipped file has verified entries

    def test_price_plan_gap_uses_board_rule(self):
        # 301682's +20% day was a false PRICE_DISCONTINUITY under the flat 0.15 rule.
        packet = {'symbol': 'sz301682', 'as_of': '2026-11-06T10:00:00+08:00',
                  'source_checks': [{'source': 'daily_bars', 'status': 'OK'}], 'mandatory_coverage': [], 'local_only_required_count': 0, 'local_only_document_count': 0,
                  'stocks': [{'features': {'unadjusted': {'basis': 'UNADJUSTED', 'last_complete_date': '2026-11-06',
                    'ma20_cents': 100, 'ma60_cents': 90, 'close_cents': 100, 'bars': bars(S301682_LAST90)}}}]}
        cfg = {'paper_stop_loss_bps': 600, 'paper_take_profit_bps': 1000, 'strategy_version': 'test', 'paper_max_stock_pct': 20}
        with patch('ashare.research.listing_dates', return_value=L682):
            plan = research.price_plan(packet, cfg, {'action': 'WATCH'}, 'SUCCEEDED')
            self.assertNotIn('PRICE_DISCONTINUITY', plan['blockers'])
            bad = json.loads(json.dumps(packet))
            bad['stocks'][0]['features']['unadjusted']['bars'][-1][2] = '50.00'
            plan = research.price_plan(bad, cfg, {'action': 'WATCH'}, 'SUCCEEDED')
            self.assertIn('PRICE_DISCONTINUITY', plan['blockers'])
        # Without a listing entry the plan takes the strict conservative rule.
        with patch('ashare.research.listing_dates', return_value={}):
            plan = research.price_plan(packet, cfg, {'action': 'WATCH'}, 'SUCCEEDED')
            self.assertIn('PRICE_DISCONTINUITY', plan['blockers'])


if __name__ == '__main__':
    unittest.main()
