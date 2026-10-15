"""Synthetic-only source, time and research integration tests for W3."""
import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from ashare import global_research, macro_sources, metal_context
from ashare.settings import DEFAULTS
from ashare.storage import Store, normalize_time


AT = normalize_time('2026-10-15T09:00:00Z')


def later(stamp, seconds):
    return normalize_time((datetime.fromisoformat(stamp)+timedelta(seconds=seconds)).isoformat())


def observations(end, count=25, start=1.0):
    days, day = [], date.fromisoformat(end)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    return [{'date': d, 'value': round(start+i*.01, 6)} for i, d in enumerate(reversed(days))]


class MetalContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.cfg = {**DEFAULTS,
                    **json.loads((Path(__file__).resolve().parents[1]/'config.example.json').read_text()),
                    'data_dir': self.tmp.name, 'investment_policy': 'days_cash_v1',
                    'model_enabled': True, 'industry_enabled': False}
        self.series = {
            'DGS10': observations('2026-10-13', start=4),
            'DFII10': observations('2026-10-13', start=-.3),
            'DTWEXBGS': observations('2026-10-09', start=100),
        }

    def seed(self, asset, *, points=None, stamp=AT, status='OK'):
        spec = macro_sources.ASSETS[asset]
        payload = {'series': spec['series'], 'unit': spec['unit'], 'url': 'https://fred.stlouisfed.org/series/'+spec['series'],
                   'points': copy.deepcopy(points if points is not None else self.series[spec['series']]),
                   'as_of': stamp, 'fetched_at': stamp, 'available_at': stamp, 'raw_path': 'synthetic.csv'}
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO macro_markets VALUES(?,?,?,?,?)',
                                  (asset, stamp, status, json.dumps(payload), None if status == 'OK' else 'synthetic failure'))
        return payload

    def seed_all(self, **kwargs):
        for asset in metal_context.FACTORS:
            self.seed(asset, **kwargs)

    def fetch(self, url):
        series = parse_qs(urlsplit(url).query)['id'][0]
        values = self.series.get(series) or observations('2026-10-13', start=20)
        return ('observation_date,'+series+'\n'+'\n'.join(p['date']+','+str(p['value']) for p in values)+'\n').encode()

    def refresh(self, at=AT, fetch=None, receipt=None, assets=metal_context.FACTORS):
        return macro_sources.refresh_markets(self.store, at, fetch_fn=fetch or self.fetch,
            priority_assets=metal_context.FACTORS, assets=assets, clock=lambda: receipt or at)

    def factors(self, at=AT):
        return {f['asset']: f for f in metal_context.context(self.store, at)['factors']}

    def run_research(self, symbols=('GOLD',), at=AT, *, fetch_quotes=False, config=None, pick_evidence=None):
        selected = {s: {'asset': s, 'name': s, 'links': []} for s in symbols}
        bars = [{'date': (date(2026, 8, 1)+timedelta(days=i)).isoformat(),
                 'price_micros': (100+i)*1_000_000} for i in range(30)]
        with self.store.db:
            for symbol in symbols:
                self.store.db.execute('INSERT OR REPLACE INTO global_market VALUES(?,?,?)',
                    (symbol, at, json.dumps({'status': 'OK', 'bars': bars})))
        packets = []
        def model(prompt, *_args):
            packet = json.loads(prompt.split('<DATA>', 1)[1].split('</DATA>', 1)[0])
            packets.append(packet)
            evidence = pick_evidence(packet) if pick_evidence else ['PRICE_HISTORY']
            return {'stance': 'LONG', 'thesis': '合成数据研究，非收益证明', 'counterpoints': ['条件可能反转'],
                    'holding_days': 5, 'evidence_ids': evidence, 'next_checks': ['跟踪已知因素变化']}
        with patch('ashare.global_research.targets', return_value=selected), \
             patch('ashare.global_research.refresh', return_value={'status': 'SUCCEEDED'}), \
             patch('ashare.global_research.now', return_value=at), \
             patch('ashare.macro_sources.now', return_value=at), \
             patch('ashare.macro_sources.data.fetch', side_effect=self.fetch), \
             patch('ashare.research.record_build', return_value={'build_id': 'synthetic-w3'}):
            result = global_research.run(self.store, config or self.cfg, at, model_fn=model, fetch_quotes=fetch_quotes)
        return result, packets

    def test_new_series_subset_budget_and_six_hour_cache(self):
        calls = []
        def fetch(url):
            calls.append(parse_qs(urlsplit(url).query)['id'][0])
            return self.fetch(url)
        self.assertEqual(self.refresh(fetch=fetch), [])
        self.assertCountEqual(calls, ('DGS10', 'DFII10', 'DTWEXBGS'))
        calls.clear()
        self.refresh(later(AT, 6*3600-1), fetch=fetch)
        self.assertEqual(calls, [])
        self.refresh(later(AT, 6*3600), fetch=fetch)
        self.assertEqual(len(calls), 3)
        calls.clear()
        self.refresh(later(AT, 12*3600), fetch=fetch, assets=None)
        self.assertEqual(len(calls), macro_sources.MARKET_REFRESH_LIMIT)

    def test_actual_receipt_is_not_backdated_and_snapshot_is_not_visible_before_it(self):
        receipt = later(AT, 3)
        self.refresh(receipt=receipt)
        self.assertTrue(all(f['reason'] == 'NOT_AVAILABLE_AT_RESEARCH_TIME' for f in self.factors().values()))
        for factor in self.factors(receipt).values():
            self.assertEqual(factor['status'], 'READY')
            self.assertEqual(factor['available_at'], receipt)
            self.assertEqual(factor['fetched_at'], receipt)
            self.assertEqual(factor['availability_basis'], 'LOCAL_RECEIPT')
            self.assertLess(factor['observation_date'], receipt[:10])

    def test_twenty_periods_require_twenty_one_points_and_use_correct_units(self):
        self.seed_all()
        factors = self.factors()
        for asset in ('US10Y', 'US10Y_REAL'):
            factor = factors[asset]
            self.assertEqual(factor['status'], 'READY')
            self.assertEqual(factor['change_20']['observation_count'], 21)
            self.assertEqual(factor['change_20']['unit'], 'bp')
            self.assertEqual(factor['change_20']['value'], 20)
        self.assertLess(factors['US10Y_REAL']['value'], 0)  # Negative real yields are valid.
        self.assertEqual(factors['USD']['change_20']['unit'], '%')
        self.assertAlmostEqual(factors['USD']['change_20']['value'], (100.24/100.04-1)*100, places=6)
        self.seed('US10Y', points=self.series['DGS10'][-20:])
        short = self.factors()['US10Y']
        self.assertEqual(short['status'], 'READY')
        self.assertEqual(short['change_20']['reason'], 'INSUFFICIENT_OBSERVATIONS')
        self.assertIsNone(short['change_20']['value'])

    def test_weekly_dollar_release_holiday_and_fred_grace(self):
        # Monday Oct 12 is a federal holiday: Tuesday release, Wednesday grace.
        before = metal_context.freshness_policy('USD', '2026-10-14T20:14:59Z')
        after = metal_context.freshness_policy('USD', '2026-10-14T20:15:00Z')
        self.assertEqual(before['minimum_observation_date'], '2026-10-02')
        self.assertEqual(after['minimum_observation_date'], '2026-10-09')
        self.assertEqual(after['cadence'], 'H10_WEEKLY')
        self.seed('USD')
        self.assertEqual(self.factors()['USD']['status'], 'READY')

    def test_daily_rate_cycle_and_weekend_are_distinct_from_weekly_dollar(self):
        self.assertEqual(metal_context.freshness_policy('US10Y', '2026-10-15T20:14:59Z')['minimum_observation_date'], '2026-10-09')
        self.assertEqual(metal_context.freshness_policy('US10Y', '2026-10-15T20:15:00Z')['minimum_observation_date'], '2026-10-13')
        self.assertEqual(metal_context.freshness_policy('US10Y', '2026-10-17T12:00:00Z')['minimum_observation_date'], '2026-10-14')
        # Standard holiday handling includes an observed New Year and Good Friday.
        self.assertFalse(metal_context._business(date(2027, 12, 31)))
        self.assertFalse(metal_context._business(date(2026, 4, 3), observation=True))

    def test_fresh_fetch_of_old_observations_remains_unknown(self):
        self.seed_all(points=observations('2026-09-01'))
        for factor in self.factors().values():
            self.assertEqual(factor['reason'], 'STALE_OBSERVATION')
            self.assertIsNone(factor['value'])
            self.assertIsNone(factor['evidence_id'])
            self.assertEqual(factor['observations'], [])

    def test_fetch_failure_keeps_audit_cache_but_does_not_expose_its_values(self):
        self.seed_all()
        def fail(_url):
            raise OSError('synthetic FRED outage')
        failures = self.refresh(later(AT, 6*3600), fetch=fail)
        self.assertEqual(len(failures), 3)
        for factor in self.factors(later(AT, 6*3600)).values():
            self.assertEqual(factor['reason'], 'SOURCE_FAILED')
            self.assertIsNone(factor['value'])
            self.assertEqual(factor['observations'], [])
        cached = json.loads(self.store.db.execute("SELECT payload_json FROM macro_markets WHERE asset='US10Y'").fetchone()[0])
        self.assertEqual(cached['points'], self.series['DGS10'])

    def test_missing_malformed_future_duplicate_or_invalid_series_is_unknown(self):
        self.assertTrue(all(f['reason'] == 'MISSING' for f in self.factors().values()))
        for points in ([{'date': AT[:10], 'value': 4}],
                       [{'date': '2026-10-13', 'value': float('nan')}],
                       [{'date': '2026-10-13', 'value': True}],
                       [{'date': '2026-10-13', 'value': 4}]*2,
                       [{'date': 'bad', 'value': 4}]):
            with self.subTest(points=points):
                self.seed('US10Y', points=points)
                self.assertEqual(self.factors()['US10Y']['reason'], 'INVALID_DATA')
        self.seed('USD', points=[{'date': '2026-10-09', 'value': 0}])
        self.assertEqual(self.factors()['USD']['reason'], 'INVALID_DATA')
        with self.store.db:
            self.store.db.execute("UPDATE macro_markets SET payload_json='not JSON' WHERE asset='US10Y'")
        self.assertEqual(self.factors()['US10Y']['reason'], 'INVALID_DATA')

    def test_legacy_cache_availability_is_read_conservatively_and_future_receipt_is_rejected(self):
        payload = self.seed('US10Y')
        payload.pop('fetched_at')
        payload.pop('available_at')
        with self.store.db:
            self.store.db.execute("UPDATE macro_markets SET payload_json=? WHERE asset='US10Y'", (json.dumps(payload),))
        self.assertEqual(self.factors()['US10Y']['status'], 'READY')
        self.assertEqual(self.factors()['US10Y']['availability_basis'], 'LEGACY_COLLECTION_TIME')
        payload['fetched_at'] = later(AT, 1)
        with self.store.db:
            self.store.db.execute("UPDATE macro_markets SET payload_json=? WHERE asset='US10Y'", (json.dumps(payload),))
        self.assertEqual(self.factors()['US10Y']['reason'], 'NOT_AVAILABLE_AT_RESEARCH_TIME')

    def test_cache_structure_and_future_availability_are_rejected_without_raising(self):
        payload = self.seed('US10Y')
        malformed = [None, [], 1, {**payload, 'points': {}},
                     {**payload, 'points': [None]}, {**payload, 'fetched_at': 123}]
        for value in malformed:
            with self.subTest(value=value), self.store.db:
                self.store.db.execute("UPDATE macro_markets SET payload_json=? WHERE asset='US10Y'", (json.dumps(value),))
            self.assertEqual(self.factors()['US10Y']['reason'], 'INVALID_DATA')
        payload['available_at'] = later(AT, 1)
        with self.store.db:
            self.store.db.execute("UPDATE macro_markets SET payload_json=? WHERE asset='US10Y'", (json.dumps(payload),))
        self.assertEqual(self.factors()['US10Y']['reason'], 'NOT_AVAILABLE_AT_RESEARCH_TIME')

    def test_nonobject_factor_cache_does_not_abort_mixed_gold_and_btc_research(self):
        self.seed_all()
        with self.store.db:
            self.store.db.execute("UPDATE macro_markets SET payload_json='null' WHERE asset='US10Y'")
        result, packets = self.run_research(('GOLD', 'BTC'))
        self.assertEqual({p['symbol']: p['status'] for p in result['plans']}, {'GOLD': 'SUCCEEDED', 'BTC': 'SUCCEEDED'})
        gold = next(p for p in packets if p['symbol'] == 'GOLD')
        self.assertEqual(gold['metal_macro_context']['factors'][0]['reason'], 'INVALID_DATA')
        self.assertNotIn('metal_macro_context', next(p for p in packets if p['symbol'] == 'BTC'))

    def test_bad_cache_check_time_is_refreshed_without_aborting_other_assets(self):
        self.seed_all()
        with self.store.db:
            self.store.db.execute("UPDATE macro_markets SET checked_at='bad-time' WHERE asset='US10Y'")
        result, packets = self.run_research(('GOLD', 'BTC'), fetch_quotes=True)
        self.assertEqual(result['macro_market_failures'], [])
        self.assertEqual({p['symbol']: p['status'] for p in result['plans']}, {'GOLD': 'SUCCEEDED', 'BTC': 'SUCCEEDED'})
        gold = next(p for p in packets if p['symbol'] == 'GOLD')
        self.assertTrue(all(f['status'] == 'READY' for f in gold['metal_macro_context']['factors']))
        self.assertEqual(self.store.db.execute("SELECT checked_at FROM macro_markets WHERE asset='US10Y'").fetchone()[0], AT)

    def test_collector_exception_marks_factors_unknown_without_aborting_other_assets(self):
        self.seed_all()
        with patch('ashare.macro_sources.refresh_markets', side_effect=ValueError('synthetic invalid collection metadata')):
            result, packets = self.run_research(('GOLD', 'BTC'), fetch_quotes=True)
        self.assertEqual(len(result['macro_market_failures']), 1)
        self.assertIn('synthetic invalid collection metadata', result['macro_market_failures'][0])
        self.assertEqual({p['symbol']: p['status'] for p in result['plans']}, {'GOLD': 'SUCCEEDED', 'BTC': 'SUCCEEDED'})
        gold = next(p for p in packets if p['symbol'] == 'GOLD')
        for factor in gold['metal_macro_context']['factors']:
            self.assertEqual(factor['reason'], 'REFRESH_FAILED')
            self.assertIsNone(factor['value'])
            self.assertIsNone(factor['evidence_id'])
        self.assertNotIn('metal_macro_context', next(p for p in packets if p['symbol'] == 'BTC'))

    def test_unchanged_fetch_preserves_first_availability_and_content_identity(self):
        self.refresh()
        first = metal_context.context(self.store, AT)
        self.refresh(later(AT, 6*3600))
        second = metal_context.context(self.store, later(AT, 6*3600))
        self.assertEqual(metal_context.content_identity(first), metal_context.content_identity(second))
        self.assertEqual(second['factors'][0]['available_at'], AT)
        self.assertEqual(second['factors'][0]['fetched_at'], later(AT, 6*3600))

    def test_gold_and_silver_receive_frozen_context_and_can_cite_macro_evidence(self):
        def cite(packet):
            return [packet['metal_macro_context']['factors'][1]['evidence_id']]
        result, packets = self.run_research(('GOLD', 'SILVER'), fetch_quotes=True, pick_evidence=cite)
        self.assertEqual(result['model_calls'], 2)
        self.assertEqual(result['macro_market_failures'], [])
        for item, packet in zip(result['plans'], packets):
            self.assertEqual(item['status'], 'SUCCEEDED')
            self.assertEqual(item['kind'], 'PAPER_TRADE')
            context = packet['metal_macro_context']
            self.assertEqual([f['asset'] for f in context['factors']], list(metal_context.FACTORS))
            frozen = self.store.root/'workflow'/'global-research'/item['plan_id']/'input.json'
            self.assertEqual(json.loads(frozen.read_text()), packet)
            method = json.loads(self.store.db.execute('SELECT payload_json FROM research_methods WHERE id=?', (packet['method_id'],)).fetchone()[0])
            self.assertEqual(method['metal_macro_context'], metal_context.VERSION)

    def test_macro_change_researches_again_but_polling_and_unseen_old_revision_do_not(self):
        self.refresh()
        first, _ = self.run_research()
        frozen = self.store.root/'workflow'/'global-research'/first['plans'][0]['plan_id']/'input.json'
        original = frozen.read_bytes()
        second_at = later(AT, 6*3600)
        self.refresh(second_at)
        second, packets = self.run_research(at=second_at)
        self.assertEqual(second['plans'][0]['status'], 'REUSED')
        self.assertEqual(packets, [])
        # Revise a point outside the 21-point input: no visible content changed.
        self.series['DGS10'][0]['value'] += .1
        self.seed('US10Y', stamp=later(second_at, 1))
        third, _ = self.run_research(at=later(second_at, 1))
        self.assertEqual(third['plans'][0]['status'], 'REUSED')
        # A revision inside the actual input must invalidate reuse.
        self.series['DGS10'][-1]['value'] += .1
        self.seed('US10Y', stamp=later(second_at, 2))
        fourth, packets = self.run_research(at=later(second_at, 2))
        self.assertEqual(fourth['model_calls'], 1)
        self.assertEqual(fourth['plans'][0]['status'], 'SUCCEEDED')
        self.assertEqual(frozen.read_bytes(), original)

    def test_freshness_transition_invalidates_reuse_without_new_observations(self):
        before = normalize_time('2026-10-15T20:14:59Z')
        self.seed_all(stamp=before, points=observations('2026-10-09'))
        first, _ = self.run_research(at=before)
        self.assertEqual(first['model_calls'], 1)
        second, packets = self.run_research(at=later(before, 1))
        self.assertEqual(second['model_calls'], 1)
        self.assertEqual(packets[0]['metal_macro_context']['factors'][0]['reason'], 'STALE_OBSERVATION')

    def test_missing_and_failed_factors_do_not_block_price_grounded_research(self):
        result, packets = self.run_research()
        self.assertEqual(result['plans'][0]['status'], 'SUCCEEDED')
        self.assertTrue(all(f['status'] == 'UNKNOWN' for f in packets[0]['metal_macro_context']['factors']))
        with patch.object(self, 'fetch', side_effect=OSError('synthetic disconnected FRED')):
            result, packets = self.run_research(at=later(AT, 1), fetch_quotes=True)
        self.assertEqual(result['plans'][0]['status'], 'SUCCEEDED')
        self.assertEqual(len(result['macro_market_failures']), 3)
        self.assertTrue(all(f['reason'] == 'SOURCE_FAILED' for f in packets[0]['metal_macro_context']['factors']))

    def test_invented_or_unknown_macro_evidence_is_rejected(self):
        result, _ = self.run_research(pick_evidence=lambda packet: ['MACRO:DFII10:invented'])
        self.assertEqual(result['plans'][0]['status'], 'DEFERRED')
        self.seed_all()
        valid = self.factors()['US10Y_REAL']['evidence_id']
        self.seed('US10Y_REAL', status='FAILED')
        result, _ = self.run_research(at=later(AT, 1), pick_evidence=lambda packet: [valid])
        self.assertEqual(result['plans'][0]['status'], 'DEFERRED')

    def test_disabled_and_nonmetal_inputs_preserve_original_prompt_and_do_not_fetch_factors(self):
        for symbols, config in ((('BTC', 'ETH'), self.cfg),
                                (('GOLD', 'SILVER'), {**self.cfg, 'global_macro_context_enabled': False})):
            with self.subTest(symbols=symbols), patch('ashare.macro_sources.refresh_markets') as refresh:
                result, packets = self.run_research(symbols, fetch_quotes=True, config=config)
                refresh.assert_not_called()
            self.assertNotIn('macro_market_failures', result)
            self.assertEqual(result['model_calls'], 2)
            for packet in packets:
                self.assertNotIn('metal_macro_context', packet)
                self.assertNotIn('metal_macro_context', global_research.build_prompt(packet))


if __name__ == '__main__':
    unittest.main()
