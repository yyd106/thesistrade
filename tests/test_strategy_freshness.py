import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from ashare import cloud_runtime, strategy_freshness as sf
from ashare.storage import Store


AT = '2026-10-15T12:00:00+00:00'


def hours(offset):
    return (datetime.fromisoformat(AT) + timedelta(hours=offset)).isoformat()


class StrategyFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        # Synthetic orphan rows exercise honest unknown-age handling for partial
        # historical replicas, without constructing unrelated execution fixtures.
        self.store.db.execute('PRAGMA foreign_keys=OFF')
        self.store.db.execute('CREATE TABLE IF NOT EXISTS freshness_receipts(sequence INTEGER PRIMARY KEY,received_at TEXT NOT NULL,completed_at TEXT NOT NULL)')
        self.cfg = {'deployment_role': 'cloud', 'plan_max_age_hours': 12}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def result(self, at=AT):
        with patch('ashare.strategy_freshness.now', return_value=AT):
            return sf.summary(self.store, self.cfg, at)

    def receipt(self, seq, received, completed=None, local=False):
        with self.store.db:
            if local:
                self.store.db.execute('INSERT INTO freshness_receipts VALUES(?,?,?)', (seq, received, completed or received))
            else:
                self.store.db.execute('INSERT INTO cloud_receipts VALUES(?,?,?,?,?)',
                                      (seq, 'b' + str(seq), received, completed or received, 'h' + str(seq)))

    def state(self, key, value):
        with self.store.db:
            cloud_runtime.put(self.store, key, value)

    def plan(self, pid, issued, research=None, expiry=None, *, route='watchlist', kind='PAPER_TRADE', status='ACTIVE', model='SUCCEEDED'):
        expiry = expiry or hours(6)
        with self.store.db:
            if route == 'watchlist':
                if research is not None:
                    self.store.db.execute('INSERT INTO studies VALUES(?,?,?,?,?,?)',
                                          ('s' + pid, 'snapshot' + pid, pid, research, model, '{}'))
                self.store.db.execute('INSERT INTO plans VALUES(?,?,?,?,?,?,?,?)',
                                      (pid, 's' + pid, pid, issued, expiry, status, 'v1', json.dumps({'kind': kind, 'private': 'PRIVATE_TEXT'})))
            elif route == 'global':
                self.store.db.execute('INSERT INTO global_plans VALUES(?,?,?,?,?,?,?)',
                                      (pid, pid, issued, expiry, 'hash', status, json.dumps({'model_status': model})))
            else:
                self.store.db.execute('INSERT INTO dynamic_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                      (pid, 'n' + pid, pid, 'name', 'theme', 'APPROVAL', 'BULLISH', issued, expiry,
                                       'FORWARD', status, '{}', '{}'))

    def fill(self, identity, pid, when, *, route='watchlist', side='BUY', code=None, recorded=None):
        order = 'o' + identity
        with self.store.db:
            if route == 'watchlist':
                self.store.db.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?)',
                                      ('d' + identity, 'slot' + identity, 'sym', pid, when, side, 'FILLED', code or '', json.dumps({'risk_trigger': code})))
                self.store.db.execute('INSERT INTO paper_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                      (order, 'd' + identity, pid, 'sym', side, 100, 100, 100, 0, when, AT, 'FILLED'))
            elif route == 'global':
                self.store.db.execute('INSERT INTO global_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                      (order, identity, pid, 'sym', side, 100, 100, 100, 0, when, AT, 'FILLED', json.dumps({'reason': code})))
            else:
                self.store.db.execute('INSERT INTO dynamic_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                      (order, identity, pid, 'sym', side, 100, 100, 100, 0, when, AT, 'FILLED', code or '', '{}'))
            if route == 'global':
                self.store.db.execute('INSERT INTO global_fills VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                      (identity, order, 'quote', 'sym', side, 100, 100, 100, 100, 1, 0, when, recorded or when))
            else:
                table = 'paper_fills' if route == 'watchlist' else 'dynamic_fills'
                self.store.db.execute('INSERT INTO ' + table + ' VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                      (identity, order, 'quote', 'sym', side, 100, 100, 1, 0, when, recorded or when))

    def test_receipt_union_uses_completion_expiry_and_clips_windows(self):
        self.receipt(1, hours(-30))
        self.receipt(2, hours(-20))
        self.receipt(3, hours(-4), hours(-6))
        result = self.result()['heartbeat']
        day = result['windows']['24h']
        self.assertEqual(day['interruption_count'], 1)
        self.assertEqual(day['interruption_seconds'], 4 * 3600)
        self.assertEqual(day['longest_interruption_seconds'], 4 * 3600)
        self.assertTrue(day['coverage_complete'])
        self.assertFalse(day['ongoing_interruption'])
        self.assertEqual(result['state'], 'ACTIVE')
        self.assertEqual(result['windows']['7d']['unknown_seconds'], 138 * 3600)

    def test_no_receipts_is_unknown_not_healthy_zero(self):
        result = self.result()
        self.assertEqual(result['heartbeat']['state'], 'UNKNOWN')
        for item in result['heartbeat']['windows'].values():
            self.assertIsNone(item['interruption_count'])
            self.assertIsNone(item['longest_interruption_seconds'])
        self.assertEqual(result['fills']['routes']['watchlist']['BUY']['count'], 0)
        self.assertIsNone(result['fills']['routes']['watchlist']['BUY']['plan_age']['mean_seconds'])

    def test_continuous_receipts_have_zero_interruptions(self):
        for index, offset in enumerate((-30, -20, -10), 1):
            self.receipt(index, hours(offset))
        item = self.result()['heartbeat']['windows']['24h']
        self.assertEqual(item['interruption_count'], 0)
        self.assertEqual(item['interruption_seconds'], 0)

    def test_delayed_delivery_and_retry_do_not_extend_completed_time(self):
        self.receipt(1, hours(-24), hours(-25))
        self.receipt(2, hours(-14), hours(-25))
        item = self.result()['heartbeat']['windows']['24h']
        self.assertEqual(item['interruption_seconds'], 13 * 3600)
        self.assertTrue(item['ongoing_interruption'])
        self.assertEqual(self.result()['heartbeat']['state'], 'EXPIRED')

    def test_replica_coverage_does_not_invent_outage_during_sync_gap(self):
        self.cfg['deployment_role'] = 'research'
        self.receipt(1, hours(-30), local=True)
        self.state('freshness_history', {'observed_from': hours(-24), 'observed_until': hours(-2),
                                        'initialized': True, 'execution_enabled': False})
        result = self.result()['heartbeat']
        item = result['windows']['24h']
        self.assertEqual(item['unknown_seconds'], 2 * 3600)
        self.assertEqual(item['interruption_seconds'], 16 * 3600)
        self.assertEqual(result['state'], 'UNKNOWN')
        self.assertFalse(result['execution_enabled'])
        self.assertFalse(item['coverage_complete'])

    def test_replica_receipts_without_coverage_remain_unknown(self):
        self.cfg['deployment_role'] = 'research'
        self.receipt(1, hours(-1), local=True)
        result = self.result()['heartbeat']
        self.assertEqual(result['state'], 'UNKNOWN')
        self.assertIsNone(result['windows']['24h']['interruption_seconds'])

    def test_expiry_boundary_and_timezone_offsets_follow_elapsed_time(self):
        self.receipt(1, '2026-10-15T08:00:00+08:00')
        result = self.result()['heartbeat']
        self.assertEqual(result['state'], 'EXPIRED')
        self.assertEqual(result['windows']['24h']['interruption_seconds'], 0)
        self.assertEqual(result['windows']['24h']['observed_seconds'], 12 * 3600)

    def test_invalid_or_future_receipts_do_not_fabricate_coverage(self):
        self.receipt(1, hours(1))
        self.assertIsNone(self.result()['heartbeat']['observed_from'])
        self.receipt(2, hours(-4), hours(-3))
        result = self.result()['heartbeat']
        self.assertEqual(result['invalid_receipt_count'], 1)
        self.assertIsNone(result['windows']['24h']['interruption_count'])

    def test_renewed_plan_preserves_old_research_age_and_protection_is_separate(self):
        self.plan('renewed', hours(-2), hours(-48))
        self.fill('buy', 'renewed', hours(-1))
        self.plan('risk', hours(-.5), hours(-96), kind='RISK_EXIT_ONLY')
        self.fill('exit', 'risk', hours(-.25), side='SELL', code='COST_STOP_TRIGGER')
        rows = self.result()['fills']['routes']['watchlist']
        self.assertEqual(rows['BUY']['plan_age']['mean_seconds'], 3600)
        self.assertEqual(rows['BUY']['research_age']['mean_seconds'], 47 * 3600)
        self.assertEqual(rows['PROTECTIVE_EXIT']['count'], 1)
        self.assertEqual(rows['PROTECTIVE_EXIT']['research_age']['max_seconds'], 95.75 * 3600)
        self.assertEqual(rows['SELL']['count'], 0)

    def test_global_dynamic_and_missing_sell_links(self):
        self.plan('global', hours(-12), route='global')
        self.fill('gb', 'global', hours(-1), route='global')
        self.fill('gs', None, hours(-1), route='global', side='SELL', code='COST_STOP_TRIGGER')
        self.plan('dynamic', hours(-4), route='dynamic', status='READY')
        self.fill('ds', 'dynamic', hours(-2), route='dynamic', side='SELL', code='PORTFOLIO_REDUCE')
        rows = self.result()['fills']['routes']
        self.assertEqual(rows['global']['BUY']['research_age']['max_seconds'], 11 * 3600)
        self.assertEqual(rows['global']['PROTECTIVE_EXIT']['plan_age']['unknown'], 1)
        self.assertIsNone(rows['global']['PROTECTIVE_EXIT']['plan_age']['max_seconds'])
        self.assertEqual(rows['dynamic']['SELL']['research_age']['max_seconds'], 2 * 3600)

    def test_global_unperformed_or_unverified_research_stays_unknown(self):
        for index, status in enumerate(('NOT_NEEDED', 'DEFERRED', None)):
            pid = 'unresearched' + str(index)
            self.plan(pid, hours(-2), route='global', model=status)
            if status is None:
                with self.store.db:
                    self.store.db.execute('UPDATE global_plans SET payload_json=? WHERE id=?', ('{}', pid))
            self.fill('unresearched-fill' + str(index), pid, hours(-1), route='global')
        result = self.result()
        fills = result['fills']['routes']['global']['BUY']
        self.assertEqual(fills['plan_age']['known'], 3)
        self.assertEqual(fills['research_age']['known'], 0)
        self.assertEqual(fills['research_age']['unknown'], 3)
        self.assertIsNone(fills['research_age']['mean_seconds'])
        current = result['plans']['routes']['global']
        self.assertEqual(current['time_valid_count'], 3)
        self.assertEqual(current['research_age']['unknown'], 3)

    def test_plan_query_reads_only_active_or_eligible_fill_references(self):
        for route in sf.ROUTES:
            active = 'READY' if route == 'dynamic' else 'ACTIVE'
            for name, status in (('active', active), ('recent', 'SUPERSEDED'), ('old', 'SUPERSEDED'),
                                 ('late', 'SUPERSEDED'), ('future', 'SUPERSEDED'), ('unrelated', 'SUPERSEDED')):
                self.plan(route + name, hours(-300), hours(-300), route=route, status=status)
            self.fill(route + 'recent-fill', route + 'recent', hours(-1), route=route)
            self.fill(route + 'old-fill', route + 'old', hours(-169), route=route)
            self.fill(route + 'late-fill', route + 'late', hours(-1), route=route, recorded=hours(1))
            self.fill(route + 'future-fill', route + 'future', hours(1), route=route)
            # Test the SQL result itself: merely discarding archived payloads
            # after loading them would not satisfy this bounded-read regression.
            selected = list(sf._plan_rows(self.store, route, datetime.fromisoformat(AT)))
            self.assertEqual({row['id'] for row in selected}, {route + 'active', route + 'recent'})
            historical = list(sf._plan_rows(self.store, route, datetime.fromisoformat(hours(-168))))
            self.assertEqual({row['id'] for row in historical}, {route + 'active', route + 'old'})
        result = self.result()['fills']['routes']
        for route in sf.ROUTES:
            self.assertEqual(result[route]['BUY']['count'], 1)
            self.assertEqual(result[route]['BUY']['plan_age']['max_seconds'], 299 * 3600)

    def test_fill_age_does_not_use_future_research_and_excludes_late_recorded_rows(self):
        self.plan('future', hours(1), hours(2))
        self.fill('past', 'future', hours(-1))
        self.fill('late', 'future', hours(-2), recorded=hours(1))
        self.fill('future-fill', 'future', hours(1))
        result = self.result()['fills']['routes']['watchlist']['BUY']
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['plan_age']['unknown'], 1)
        self.assertEqual(result['research_age']['unknown'], 1)

    def test_current_validity_expired_rows_and_stale_research_are_distinct(self):
        self.plan('valid', hours(-2), hours(-48))
        self.plan('expired', hours(-24), hours(-24), expiry=hours(-12))
        self.plan('future', hours(1), hours(1))
        self.plan('draft', hours(-2), hours(-2), status='DRAFT')
        result = self.result()['plans']['routes']['watchlist']
        self.assertEqual(result['status_rows'], 3)
        self.assertEqual(result['time_valid_count'], 1)
        self.assertEqual(result['expired_active_count'], 1)
        self.assertEqual(result['future_active_count'], 1)
        self.assertEqual(result['over_max_age_count'], 1)
        self.assertEqual(result['plan_age']['max_seconds'], 2 * 3600)
        self.assertEqual(result['research_age']['max_seconds'], 48 * 3600)
        self.assertEqual(result['min_remaining_seconds'], 6 * 3600)
        self.assertIsNone(self.result()['plans']['routes']['global']['max_age_threshold_seconds'])

    def test_historical_snapshot_does_not_reuse_present_active_status(self):
        self.plan('new', hours(-1), hours(-1))
        result = self.result(hours(-24))['plans']
        self.assertEqual(result['status'], 'UNKNOWN_HISTORICAL_STATUS')
        for route in result['routes'].values():
            self.assertIsNone(route['status_rows'])
            self.assertIsNone(route['time_valid_count'])
            self.assertIsNone(route['plan_age']['known'])

    def test_summary_is_read_only_bounded_and_never_calls_execution(self):
        self.plan('private', hours(-2), hours(-48))
        self.receipt(1, hours(-1))
        before = self.store.db.total_changes
        self.store.db.execute('PRAGMA query_only=ON')
        with patch('ashare.cloud_runtime.lease', side_effect=AssertionError('execution path')), \
             patch('ashare.dynamic.eligibility', side_effect=AssertionError('execution path')):
            result = self.result()
        self.assertEqual(before, self.store.db.total_changes)
        self.assertNotIn('PRIVATE_TEXT', json.dumps(result))
        self.assertLess(len(json.dumps(result).encode()), sf.MAX_BYTES)
        self.assertEqual(sf.validate_public_summary(result), result)
        for change in ('extra', 'text', 'nan', 'enum', 'fractional_count', 'missing_time'):
            packet = copy.deepcopy(result)
            if change == 'extra': packet['raw_research'] = 'PRIVATE_TEXT'
            if change == 'text': packet['plans']['routes']['global']['status_rows'] = 'PRIVATE_TEXT'
            if change == 'nan': packet['plans']['routes']['global']['status_rows'] = float('nan')
            if change == 'enum': packet['heartbeat']['state'] = 'PRIVATE_TEXT'
            if change == 'fractional_count': packet['plans']['routes']['global']['status_rows'] = .5
            if change == 'missing_time': packet['as_of'] = None
            with self.assertRaises(ValueError): sf.validate_public_summary(packet)

    def test_feature_off_reads_nothing(self):
        self.cfg['strategy_freshness_enabled'] = False
        self.assertIsNone(sf.summary(None, self.cfg, AT))


if __name__ == '__main__':
    unittest.main()
