"""Synthetic tests for the isolated fixed five-day diagnostic."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from unittest.mock import patch

from ashare import evaluation, judgments, selfcheck
from ashare.calendar import trading_day
from ashare.storage import Store, normalize_time


def trading_days(start, count):
    current, days = date.fromisoformat(start), []
    while len(days) < count:
        if trading_day(current) is True:
            days.append(current.isoformat())
        current += timedelta(days=1)
    return days


class QuickEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cfg = {'evaluation_horizon_days': 20, 'data_dir': self.tmp.name, 'deployment_role': 'research'}
        with self.store.db:
            self.store.db.execute('CREATE TABLE market_features(run_id TEXT,symbol TEXT,created_at TEXT,raw_path TEXT,payload TEXT,PRIMARY KEY(run_id,symbol))')
            self.store.db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('fixture', 'fixture', 'SYNTHETIC', '2026-01-01T00:00:00+00:00', None, None, 'SUCCEEDED', None, None, None))
        self.days = trading_days('2026-09-14', 30)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def signal(self, identity='s1', *, symbol='sh600001', route='watchlist',
               created_at='2026-09-14T08:00:00+08:00', as_of_day='2026-09-11',
               days=20, build='b1', historical=False, judgment=None):
        with self.store.db:
            evaluation._insert(self.store, identity, route, symbol, identity, normalize_time(created_at),
                as_of_day, build, days, None if route == 'global' else evaluation.BENCHMARK,
                judgment or {'trend_ok': True, 'model_action': 'WATCH', 'stance': 'LONG'}, historical=historical)
        return identity

    def prices(self, days=None, *, symbol='sh600001', missing=(), multiplier=1):
        days = self.days if days is None else days
        bars = [[d, str(10 * multiplier), str((10 + i) * multiplier), str((11 + i) * multiplier),
                 str(9 * multiplier), '1000'] for i, d in enumerate(days) if d not in missing]
        path = 'raw/' + symbol + '.json'
        (self.store.root / 'raw').mkdir(exist_ok=True)
        (self.store.root / path).write_text(json.dumps({'data': {symbol: {'qfqday': bars}}}))
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO market_features VALUES(?,?,?,?,?)',
                                 ('fixture', symbol, '2026-12-31T10:00:00+00:00', path, '{}'))

    def benchmark(self, days=None, *, missing=()):
        days = self.days if days is None else days
        bars = [{'date': d, 'open': 100, 'close': 101, 'high': 102, 'low': 99}
                for d in days if d not in missing]
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO comparison_series VALUES(?,?,?,?)',
                ('fixture', evaluation.BENCHMARK, '2026-12-31T10:00:00+00:00', json.dumps({'bars': bars})))

    def global_prices(self, symbol='GOLD'):
        days = [(date(2026, 9, 15) + timedelta(days=i)).isoformat() for i in range(8)]
        with self.store.db:
            self.store.db.execute('INSERT INTO global_market VALUES(?,?,?)',
                (symbol, '2026-09-23T10:00:00+00:00', json.dumps({'bars': [
                    {'date': d, 'price_micros': 100 + 10 * i} for i, d in enumerate(days)]})))
        return days

    def score(self, identity='s1', method=evaluation.QUICK_SCORE_METHOD):
        row = self.store.db.execute('SELECT * FROM signal_scores WHERE signal_id=? AND method=?',
                                    (identity, method)).fetchone()
        return {**dict(row), 'score': json.loads(row['score_json'])} if row else None

    def snapshot(self, table):
        return [tuple(r) for r in self.store.db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]

    def add_scored(self, identity, entry, exit_, *, symbol='sh600001', build='b1',
                   created_at='2026-01-04T08:00:00+08:00', value=100, method=evaluation.QUICK_SCORE_METHOD,
                   judgment=None, historical=False):
        self.signal(identity, symbol=symbol, build=build, created_at=created_at, judgment=judgment, historical=historical)
        with self.store.db:
            self.store.db.execute('INSERT INTO signal_scores VALUES(?,?,?,?,?)',
                (identity, method, 'SCORED', json.dumps({'method': method, 'entry_date': entry,
                 'exit_date': exit_, 'return_bps': value, 'excess_bps': value}), '2026-12-31T12:00:00+00:00'))

    def test_five_trading_days_includes_entry_and_ignores_configured_horizon(self):
        self.signal(days=20)
        self.prices()
        self.benchmark()
        at = self.days[4] + 'T15:05:00+08:00'
        self.assertEqual(evaluation.score_quick(self.store, {**self.cfg, 'evaluation_horizon_days': 1}, at),
                         {'scored': 1, 'pending': 0, 'unscorable': 0})
        result = self.score()['score']
        self.assertEqual((result['entry_date'], result['exit_date']), (self.days[0], self.days[4]))
        self.assertEqual(result['return_bps'], 4000)
        self.assertEqual(result['excess_bps'], 3900)
        self.assertEqual((result['method'], result['horizon_days']), ('decision-time-v2-q5', 5))
        self.assertTrue(result['auxiliary_only'])
        self.assertFalse(result['approval_eligible'])
        self.assertEqual(evaluation.score(self.store, self.cfg, at), {'scored': 0, 'pending': 1, 'unscorable': 0})
        self.assertEqual(evaluation.registry_counts(self.store), {'watchlist:OPEN': 1})
        self.assertEqual(evaluation.registry_counts(self.store, method=evaluation.QUICK_SCORE_METHOD), {'watchlist:SCORED': 1})

    def test_quick_keeps_original_registry_contract_and_score_bytes(self):
        self.signal()
        self.prices()
        self.benchmark()
        at = self.days[24] + 'T20:00:00+08:00'
        self.assertEqual(evaluation.score(self.store, self.cfg, at)['scored'], 1)
        original_score = self.score(method=evaluation.SCORE_METHOD)
        with self.store.db:
            self.store.db.execute('INSERT INTO signal_scores VALUES(?,?,?,?,?)',
                ('s1', 'legacy-v1', 'SCORED', '{"legacy": "unchanged"}', '2026-09-01T00:00:00+00:00'))
        legacy_score = self.score(method='legacy-v1')
        original_comparisons = evaluation.comparisons(self.store, self.cfg)
        tables = ('signal_registry', 'judgment_contracts', 'strategy_proposals', 'strategy_guidance', 'plans')
        before = {table: self.snapshot(table) for table in tables}
        evaluation.score_quick(self.store, self.cfg, at)
        self.assertEqual(self.score(method=evaluation.SCORE_METHOD), original_score)
        self.assertEqual(self.score(method='legacy-v1'), legacy_score)
        self.assertEqual(evaluation.comparisons(self.store, self.cfg), original_comparisons)
        self.assertEqual({table: self.snapshot(table) for table in tables}, before)
        self.assertNotEqual(self.score()['score']['exit_date'], original_score['score']['exit_date'])
        self.assertEqual(self.score()['score']['exit_date'], self.days[4])

    def test_idempotent_after_late_price_revision(self):
        self.signal()
        self.prices()
        self.benchmark()
        at = self.days[4] + 'T20:00:00+08:00'
        evaluation.score_quick(self.store, self.cfg, at)
        before = self.snapshot('signal_scores')
        self.prices(multiplier=10)
        self.assertEqual(evaluation.score_quick(self.store, self.cfg, self.days[10] + 'T20:00:00+08:00'),
                         {'scored': 0, 'pending': 0, 'unscorable': 0})
        self.assertEqual(self.snapshot('signal_scores'), before)

    def test_judgment_at_open_waits_for_next_open(self):
        self.signal('before', created_at='2026-09-14T09:29:59+08:00')
        self.signal('at-open', created_at='2026-09-14T09:30:00+08:00')
        self.signal('after', created_at='2026-09-14T09:31:00+08:00')
        self.prices()
        self.benchmark()
        evaluation.score_quick(self.store, self.cfg, self.days[6] + 'T20:00:00+08:00')
        self.assertEqual(self.score('before')['score']['entry_date'], self.days[0])
        for identity in ('at-open', 'after'):
            self.assertEqual(self.score(identity)['score']['entry_date'], self.days[1])
            self.assertEqual(self.score(identity)['score']['exit_date'], self.days[5])

    def test_source_data_day_can_delay_entry_beyond_judgment_day(self):
        self.signal(as_of_day=self.days[1])
        self.prices()
        self.benchmark()
        evaluation.score_quick(self.store, self.cfg, self.days[7] + 'T20:00:00+08:00')
        self.assertEqual(self.score()['score']['entry_date'], self.days[2])
        self.assertEqual(self.score()['score']['exit_date'], self.days[6])

    def test_weekends_and_exchange_holiday_are_not_observation_days(self):
        self.signal(created_at='2026-09-18T10:00:00+08:00', as_of_day='2026-09-17')
        self.prices()
        self.benchmark()
        expected = trading_days('2026-09-21', 5)
        evaluation.score_quick(self.store, self.cfg, expected[-1] + 'T20:00:00+08:00')
        result = self.score()['score']
        self.assertEqual((result['entry_date'], result['exit_date']), (expected[0], expected[-1]))
        self.assertGreater((date.fromisoformat(expected[-1]) - date.fromisoformat(expected[0])).days, 4)

    def test_unclosed_and_future_prices_never_mature_diagnostic_early(self):
        self.signal()
        # The latest arrived file intentionally includes future daily bars.
        self.prices()
        self.benchmark()
        for clock in ('10:00:00', '15:04:59'):
            result = evaluation.score_quick(self.store, self.cfg, self.days[4] + 'T' + clock + '+08:00')
            self.assertEqual(result, {'scored': 0, 'pending': 1, 'unscorable': 0})
            self.assertIsNone(self.score())
        self.assertEqual(evaluation.score_quick(self.store, self.cfg, self.days[4] + 'T15:05:00+08:00')['scored'], 1)

    def test_missing_middle_stock_bar_does_not_shift_window(self):
        self.signal()
        self.prices(missing=(self.days[2],))
        self.benchmark()
        at = self.days[9] + 'T20:00:00+08:00'
        self.assertEqual(evaluation.score_quick(self.store, self.cfg, at)['pending'], 1)
        self.prices()
        self.assertEqual(evaluation.score_quick(self.store, self.cfg, at)['scored'], 1)
        self.assertEqual(self.score()['score']['exit_date'], self.days[4])

    def test_missing_benchmark_waits_and_retries_when_it_arrives(self):
        self.signal()
        self.prices()
        at = self.days[9] + 'T20:00:00+08:00'
        for missing in (self.days, (self.days[0],), (self.days[4],)):
            self.benchmark(missing=missing)
            self.assertEqual(evaluation.score_quick(self.store, self.cfg, at)['pending'], 1)
            self.assertIsNone(self.score())
        self.benchmark()
        self.assertEqual(evaluation.score_quick(self.store, self.cfg, at)['scored'], 1)

    def test_missing_data_becomes_unscorable_only_after_120_days(self):
        self.signal(created_at='2026-05-17T08:00:00+00:00', as_of_day='2026-05-15')
        self.assertEqual(evaluation.score_quick(self.store, self.cfg, '2026-09-14T08:00:00+00:00')['pending'], 1)
        result = evaluation.score_quick(self.store, self.cfg, '2026-09-14T08:00:01+00:00')
        self.assertEqual(result, {'scored': 0, 'pending': 0, 'unscorable': 1})
        self.assertEqual(self.score()['status'], 'UNSCORABLE')
        self.assertIsNone(self.score(method=evaluation.SCORE_METHOD))

    def test_global_retains_five_close_to_close_periods_and_completed_cutoff(self):
        self.signal(route='global', symbol='GOLD', as_of_day='2026-09-14', days=30)
        days = self.global_prices()
        self.assertEqual(evaluation.score_quick(self.store, self.cfg, days[5] + 'T23:59:59+00:00')['pending'], 1)
        self.assertEqual(evaluation.score_quick(self.store, self.cfg, days[6] + 'T00:00:01+00:00')['scored'], 1)
        result = self.score()['score']
        self.assertEqual((result['entry_date'], result['exit_date']), (days[0], days[5]))
        self.assertEqual(result['return_bps'], 5000)
        self.assertIn('未计汇率和费用', result['basis'])
        summary = evaluation.quick_comparisons(self.store, self.cfg)
        self.assertEqual(summary['groups']['global_stance']['LONG']['daily']['n'], 1)
        self.assertIn('6个已完成收盘点之间的5期', summary['notice'])

    def test_quick_only_results_do_not_feed_selfcheck_outcomes_or_candidates(self):
        self.signal(route='global', symbol='GOLD', as_of_day='2026-09-14')
        self.global_prices()
        evaluation.score_quick(self.store, self.cfg, '2026-09-23T12:00:00+00:00')
        self.assertEqual(selfcheck.outcomes(self.store, '2026-09-23T12:00:00+00:00')[0]['status'], 'AWAITING_SCORE')
        result = selfcheck.run(self.store, self.cfg, '2026-09-23T12:00:00+00:00', generate=True)
        self.assertEqual(result['candidates']['updates'], [])
        self.assertEqual(self.snapshot('strategy_proposals'), [])
        self.assertEqual(self.snapshot('strategy_guidance'), [])

    def test_quick_comparisons_empty_metadata_and_method_isolation(self):
        self.add_scored('main', '2026-01-05', '2026-02-05', method=evaluation.SCORE_METHOD, value=999)
        before = evaluation.comparisons(self.store, self.cfg)
        quick = evaluation.quick_comparisons(self.store, self.cfg)
        self.assertEqual(quick['method'], evaluation.QUICK_SCORE_METHOD)
        self.assertEqual(quick['horizon_days'], 5)
        self.assertTrue(quick['auxiliary_only'])
        self.assertFalse(quick['approval_eligible'])
        self.assertEqual(quick['groups']['trend_filter']['trend_ok']['daily']['n'], 0)
        self.assertEqual(quick['by_build'], {})
        self.assertEqual(quick['sample_sources']['scored_rows'], {'LIVE': 0, 'LEGACY': 0, 'UNCLASSIFIED': 0})
        self.add_scored('quick', '2026-01-05', '2026-01-09', value=-555)
        self.assertEqual(evaluation.comparisons(self.store, self.cfg), before)
        self.assertEqual(evaluation.quick_comparisons(self.store, self.cfg)['groups']['trend_filter']['trend_ok']['daily']['mean_bps'], -555)

    def test_daily_deduplication_precedes_group_and_build_split(self):
        self.add_scored('old', '2026-01-05', '2026-01-09', build='old', value=100,
                        judgment={'trend_ok': False, 'model_action': 'NOT_WATCH'})
        self.add_scored('new', '2026-01-05', '2026-01-09', build='new', value=-50,
                        created_at='2026-01-04T09:00:00+08:00')
        result = evaluation.quick_comparisons(self.store, self.cfg)
        self.assertEqual(set(result['by_build']), {'new'})
        self.assertEqual(result['groups']['trend_filter']['trend_fail']['daily']['n'], 0)
        self.assertEqual(result['groups']['trend_filter']['trend_ok']['daily']['mean_bps'], -50)
        self.assertEqual(result['sample_sources']['scored_rows']['LIVE'], 2)
        self.assertEqual(result['sample_sources']['daily_samples']['LIVE'], 1)

    def test_non_overlap_uses_quick_window_before_group_and_build_splits(self):
        self.add_scored('first', '2026-01-05', '2026-01-09', build='b1')
        self.add_scored('overlap', '2026-01-08', '2026-01-14', build='b2',
                        judgment={'trend_ok': True, 'model_action': 'NOT_WATCH'})
        self.add_scored('next', '2026-01-12', '2026-01-16', build='b2')
        result = evaluation.quick_comparisons(self.store, {**self.cfg, 'evaluation_horizon_days': 60})
        self.assertEqual(result['groups']['trend_filter']['trend_ok']['daily']['n'], 3)
        self.assertEqual(result['groups']['trend_filter']['trend_ok']['non_overlapping']['n'], 2)
        self.assertEqual(result['groups']['research_veto']['NOT_WATCH']['non_overlapping']['n'], 0)
        self.assertEqual(result['by_build']['b2']['trend_filter']['trend_ok']['non_overlapping']['n'], 1)

    def test_many_correlated_symbols_are_one_time_cluster_without_ci(self):
        for i in range(50):
            self.add_scored('s' + str(i), '2026-01-05', '2026-01-09', symbol='symbol' + str(i), value=i)
        result = evaluation.quick_comparisons(self.store, self.cfg)['groups']['trend_filter']['trend_ok']
        for choice in ('daily', 'non_overlapping'):
            self.assertEqual(result[choice]['n'], 50)
            self.assertEqual(result[choice]['time_clusters'], 1)
            self.assertIsNone(result[choice]['ci95_bps'])

    def test_ci_requires_30_distinct_time_clusters(self):
        days = trading_days('2026-01-05', 150)
        for i in range(29):
            self.add_scored('s' + str(i), days[5 * i], days[5 * i + 4], value=10 * i)
        before = evaluation.quick_comparisons(self.store, self.cfg)['groups']['trend_filter']['trend_ok']['daily']
        self.assertEqual(before['time_clusters'], 29)
        self.assertIsNone(before['ci95_bps'])
        self.add_scored('s29', days[145], days[149], value=290)
        after = evaluation.quick_comparisons(self.store, self.cfg)['groups']['trend_filter']['trend_ok']['daily']
        self.assertEqual(after['time_clusters'], 30)
        self.assertIsNotNone(after['ci95_bps'])

    def test_source_counts_expose_history_without_claiming_forward_experiment(self):
        self.add_scored('live', '2026-01-05', '2026-01-09')
        self.add_scored('legacy', '2026-01-12', '2026-01-16', historical=True)
        judgments.backfill(self.store, self.cfg, '2026-09-30T12:00:00+00:00')
        self.add_scored('unknown', '2026-01-19', '2026-01-23', historical=True)
        result = evaluation.quick_comparisons(self.store, self.cfg)
        self.assertEqual(result['sample_sources']['daily_samples'], {'LIVE': 1, 'LEGACY': 1, 'UNCLASSIFIED': 1})
        self.assertIn('不等于前向实验', result['sample_sources']['notice'])

    def test_since_filters_quick_records_before_statistics(self):
        self.add_scored('old', '2026-01-05', '2026-01-09', created_at='2026-01-04T08:00:00+08:00', value=100)
        self.add_scored('new', '2026-01-12', '2026-01-16', created_at='2026-01-11T08:00:00+08:00', value=-100)
        result = evaluation.quick_comparisons(self.store, self.cfg, since='2026-01-10T00:00:00+00:00')
        self.assertEqual(result['groups']['trend_filter']['trend_ok']['daily']['n'], 1)
        self.assertEqual(result['groups']['trend_filter']['trend_ok']['daily']['mean_bps'], -100)
        self.assertEqual(result['sample_sources']['scored_rows']['LIVE'], 1)

    def test_explicit_historical_cutoff_hides_future_scores_and_judgments(self):
        self.add_scored('known', '2026-01-05', '2026-01-09')
        self.add_scored('future-score', '2026-01-12', '2026-01-16')
        self.add_scored('future-judgment', '2026-01-19', '2026-01-23',
                        created_at='2026-10-01T08:00:00+08:00')
        for identity in ('known', 'future-score', 'future-judgment'):
            with self.store.db:
                row = self.score(identity)
                self.store.db.execute('INSERT INTO signal_scores VALUES(?,?,?,?,?)',
                    (identity, evaluation.SCORE_METHOD, row['status'], row['score_json'], row['scored_at']))
        # Synthetic archived records allow separate checks of the two time predicates.
        with self.store.db:
            self.store.db.execute("UPDATE signal_scores SET scored_at='2026-09-01T00:00:00+00:00' WHERE signal_id IN ('known','future-judgment')")
        cutoff = '2026-09-15T12:00:00+00:00'
        for method, summarize in ((evaluation.QUICK_SCORE_METHOD, evaluation.quick_comparisons),
                                  (evaluation.SCORE_METHOD, evaluation.comparisons)):
            result = summarize(self.store, self.cfg, at=cutoff)
            self.assertEqual(result['groups']['trend_filter']['trend_ok']['daily']['n'], 1)
            self.assertEqual(evaluation.registry_counts(self.store, method=method, at=cutoff),
                             {'watchlist:OPEN': 1, 'watchlist:SCORED': 1})
            self.assertEqual([r['id'] for r in evaluation.load(self.store, 'watchlist', method=method, at=cutoff)], ['known'])
        self.assertEqual(evaluation.quick_comparisons(self.store, self.cfg, at=cutoff)['sample_sources']['daily_samples']['LIVE'], 1)
        self.assertEqual(evaluation.comparisons(self.store, self.cfg)['groups']['trend_filter']['trend_ok']['daily']['n'], 3)

    def test_historical_source_counts_do_not_backdate_later_contracts(self):
        self.add_scored('legacy', '2026-01-05', '2026-01-09', historical=True)
        with self.store.db:
            self.store.db.execute("UPDATE signal_scores SET scored_at='2026-09-01T00:00:00+00:00'")
        judgments.backfill(self.store, self.cfg, '2026-09-30T12:00:00+00:00')
        result = evaluation.quick_comparisons(self.store, self.cfg, at='2026-09-15T12:00:00+00:00')
        self.assertEqual(result['sample_sources']['scored_rows'], {'LIVE': 0, 'LEGACY': 0, 'UNCLASSIFIED': 1})

    def test_concurrent_quick_evaluators_report_only_rows_actually_inserted(self):
        self.signal()
        self.prices()
        self.benchmark()
        barrier = threading.Barrier(2)
        score_row = evaluation.score_row

        def after_both_selected(*args):
            barrier.wait(timeout=5)
            return score_row(*args)

        def run():
            store = Store(self.tmp.name)
            try:
                return evaluation.score_quick(store, self.cfg, self.days[4] + 'T20:00:00+08:00')
            finally:
                store.close()

        with patch.object(evaluation, 'score_row', side_effect=after_both_selected), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(r['scored'] for r in results), 1)
        self.assertEqual(len(self.snapshot('signal_scores')), 1)


if __name__ == '__main__':
    unittest.main()
