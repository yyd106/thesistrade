import json
import tempfile
import unittest
from datetime import date, timedelta

from ashare import evaluation, quick_diagnostics as qd
from ashare.storage import Store


AT = '2026-03-01T10:00:00+00:00'


class DiagnosticFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def insert(self, identity, created, build, *, methods=None, scored=AT, route='watchlist'):
        day = date.fromisoformat(created[:10])
        with self.store.db:
            evaluation._insert(self.store, identity, route, identity, 'source', created,
                day.isoformat(), build, 20, evaluation.BENCHMARK,
                {'trend_ok': True, 'model_action': 'WATCH', 'private': 'RAW_PRIVATE_MARKER'}, historical=True)
            for method in (methods or []):
                score = {'entry_date': (day + timedelta(days=1)).isoformat(),
                         'exit_date': (day + timedelta(days=5)).isoformat(),
                         'excess_bps': 100, 'private': 'RAW_PRIVATE_MARKER'}
                self.store.db.execute('INSERT INTO signal_scores VALUES(?,?,?,?,?)',
                    (identity, method, 'SCORED', json.dumps(score), scored))

    def snapshot(self):
        return {name: [tuple(r) for r in self.store.db.execute('SELECT * FROM ' + name)]
                for name in ('signal_registry', 'signal_scores', 'judgment_contracts', 'service_state')}

    def test_coverage_distinguishes_registered_judgments_scored_judgments_and_price_windows(self):
        self.insert('early', '2026-01-01T09:00:00+00:00', 'old',
                    methods=[evaluation.SCORE_METHOD, evaluation.QUICK_SCORE_METHOD])
        self.insert('later', '2026-02-01T09:00:00+00:00', 'new', methods=[evaluation.QUICK_SCORE_METHOD])
        self.insert('pending', '2026-02-15T09:00:00+00:00', 'pending')
        self.insert('future-score', '2026-02-20T09:00:00+00:00', 'future-score',
                    methods=[evaluation.SCORE_METHOD], scored='2026-03-02T10:00:00+00:00')
        self.insert('future-judgment', '2026-03-02T09:00:00+00:00', 'future')
        self.insert('other-route', '2025-01-01T09:00:00+00:00', 'other', route='dynamic')
        before = self.snapshot()
        total_changes = self.store.db.total_changes
        result = qd.collect(self.store, {'evaluation_horizon_days': 20}, AT)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.store.db.total_changes, total_changes)
        primary, quick = result['primary']['coverage'], result['quick']['coverage']
        for coverage in (primary, quick):
            self.assertEqual(coverage['scope'], 'ALL_HISTORY')
            self.assertEqual(coverage['registered_from'], '2026-01-01T09:00:00+00:00')
            self.assertEqual(coverage['registered_through'], '2026-02-20T09:00:00+00:00')
            self.assertEqual(coverage['observation_from'], '2026-01-02')
            self.assertEqual(coverage['last_scored_at'], AT)
        self.assertEqual(primary['scored_judgment_through'], '2026-01-01T09:00:00+00:00')
        self.assertEqual(primary['observation_through'], '2026-01-06')
        self.assertEqual(quick['scored_judgment_through'], '2026-02-01T09:00:00+00:00')
        self.assertEqual(quick['observation_through'], '2026-02-06')
        self.assertNotIn('future-score', result['primary']['by_build'])
        self.assertNotIn('RAW_PRIVATE_MARKER', json.dumps(result))
        self.assertIn('全历史累计，非最近5日新增样本', qd.markdown(result))

    def test_latest_judgment_keeps_recent_builds_even_when_hashes_sort_in_reverse(self):
        count = 32
        for i in range(count):
            day = date(2026, 1, 1) + timedelta(days=i)
            self.insert(str(i), day.isoformat() + 'T09:00:00+00:00', f'build-{count - i:02d}',
                        methods=[evaluation.SCORE_METHOD, evaluation.QUICK_SCORE_METHOD])
        result = qd.collect(self.store, {}, AT)
        for arm in ('primary', 'quick'):
            builds = list(result[arm]['by_build'])
            self.assertEqual(builds[0], 'build-01')
            self.assertNotIn('build-32', builds)
            self.assertLessEqual(len(builds), qd.MAX_BUILDS)
            self.assertEqual(result[arm]['omitted_builds'], count - len(builds))
            self.assertEqual(set(result[arm]['build_times']), set(builds))
            dates = [result[arm]['build_times'][build]['last_judgment_at'] for build in builds]
            self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), qd.MAX_BYTES)
        self.assertEqual(qd.public(result), result)

    def test_backfilled_old_build_does_not_become_the_latest_version(self):
        self.insert('old', '2026-01-01T09:00:00+00:00', 'z-old',
                    methods=[evaluation.QUICK_SCORE_METHOD], scored=AT)
        self.insert('new', '2026-02-01T09:00:00+00:00', 'a-new',
                    methods=[evaluation.QUICK_SCORE_METHOD], scored='2026-02-20T09:00:00+00:00')
        result = qd.collect(self.store, {}, AT)
        self.assertEqual(list(result['quick']['by_build']), ['a-new', 'z-old'])

    def test_legacy_dates_are_unknown_and_date_allowlist_rejects_raw_fields(self):
        noisy = {'coverage': {'scope': 'SECRET', 'registered_from': 'RAW_PRIVATE_MARKER',
                             'observation_from': '2026-02-30', 'observation_through': '2026-02-01',
                             'raw': 'RAW_PRIVATE_MARKER'},
                 'by_build': {'z-first': {}, 'a-second': {}},
                 'build_times': {'z-first': {'first_judgment_at': 'RAW_PRIVATE_MARKER',
                                            'raw': 'RAW_PRIVATE_MARKER'}}, 'raw': 'RAW_PRIVATE_MARKER'}
        result = qd.public({'version': qd.VERSION, 'status': 'READY', 'generated_at': AT,
                            'primary': noisy, 'quick': noisy})
        self.assertEqual(list(result['quick']['by_build']), ['z-first', 'a-second'])
        self.assertIsNone(result['quick']['coverage']['registered_from'])
        self.assertIsNone(result['quick']['coverage']['observation_from'])
        self.assertEqual(result['quick']['coverage']['observation_through'], '2026-02-01')
        self.assertNotIn('RAW_PRIVATE_MARKER', json.dumps(result))
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(qd.public(result), result)

    def test_empty_database_reports_unknown_coverage_without_fabricating_freshness(self):
        result = qd.collect(self.store, {}, AT)
        self.assertEqual(result['primary']['coverage'], {'scope': 'ALL_HISTORY',
            'registered_from': None, 'registered_through': None,
            'scored_judgment_from': None, 'scored_judgment_through': None,
            'last_scored_at': None, 'observation_from': None, 'observation_through': None})
        self.assertEqual(result['quick']['build_times'], {})


if __name__ == '__main__':
    unittest.main()
