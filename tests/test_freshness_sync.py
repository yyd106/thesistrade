"""Freshness transport cannot renew research or mutate the authoritative ledger."""
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from ashare import freshness_sync as freshness, cloud_ledger as ledger, cloud_sync as sync
from ashare.cloud_runtime import put, value, lease
from ashare.storage import Store

AT = '2026-10-15T12:00:00+00:00'
OLD = '2026-10-15T00:00:00+00:00'
LATER = '2026-10-15T12:04:00+00:00'


class FreshnessSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cloud_tmp = tempfile.TemporaryDirectory()
        self.store, self.cloud = Store(self.tmp.name), Store(self.cloud_tmp.name)
        self.config = {'data_dir': self.tmp.name, 'deployment_role': 'research', 'strategy_freshness_enabled': True}
        self.cloud_config = dict(self.config, data_dir=self.cloud_tmp.name, deployment_role='cloud')
        with self.cloud.db:
            put(self.cloud, 'initialized', True)
            put(self.cloud, 'execution_enabled', True)
            put(self.cloud, 'research_completed_at', OLD)
            put(self.cloud, 'research_received_at', OLD)
            self.cloud.db.execute('INSERT INTO cloud_receipts VALUES(1,?,?,?,?)', ('b1', OLD, OLD, 'h1'))
        with self.store.db:
            put(self.store, 'remote_features', [freshness.FEATURE])

    def tearDown(self):
        self.store.close(); self.cloud.close()
        self.tmp.cleanup(); self.cloud_tmp.cleanup()

    def packet(self, at=AT):
        view = freshness.local_view(self.store, self.config, at)
        packet = {'version': freshness.VERSION, 'generated_at': at, 'enabled': True,
                  'status': view['status'], 'summary': view['summary']}
        packet['content_hash'] = freshness.packet_hash(packet)
        return packet

    def test_history_is_read_only_and_contains_only_receipt_times(self):
        changes = self.cloud.db.total_changes
        with patch('pathlib.Path.read_text', side_effect=AssertionError('no files')):
            result = freshness.history(self.cloud, AT)
        self.assertEqual(self.cloud.db.total_changes, changes)
        self.assertEqual(set(result['rows'][0]), {'sequence', 'received_at', 'completed_at'})
        self.assertNotIn('b1', json.dumps(result)); self.assertNotIn('h1', json.dumps(result))
        with self.store.db: freshness.mirror_history(self.store, result)
        self.assertEqual(value(self.store, 'freshness_history')['observed_from'], OLD)
        self.assertEqual(value(self.store, 'freshness_history')['observed_until'], AT)
        self.assertIsNone(value(self.store, 'research_completed_at'))

    def test_capped_history_never_claims_dropped_coverage(self):
        with self.cloud.db:
            self.cloud.db.execute('UPDATE cloud_receipts SET received_at=?,completed_at=?', ('2026-10-01T00:00:00+00:00',)*2)
            for seq, stamp in enumerate(('2026-10-10T00:00:00+00:00','2026-10-11T00:00:00+00:00','2026-10-12T00:00:00+00:00'), 2):
                self.cloud.db.execute('INSERT INTO cloud_receipts VALUES(?,?,?,?,?)', (seq, 'b'+str(seq), stamp, stamp, 'h'))
        with patch.object(freshness, 'HISTORY_LIMIT', 3): packet = freshness.history(self.cloud, AT)
        self.assertEqual(len(packet['rows']), 3)
        self.assertEqual(packet['observed_from'], '2026-10-10T00:00:00+00:00')

    def test_bad_extra_rolls_back_only_its_own_writes(self):
        packet = freshness.history(self.cloud, AT)
        packet['rows'].append({'sequence': 2, 'received_at': AT, 'completed_at': '2026-10-16T00:00:00+00:00'})
        with self.store.db:
            put(self.store, 'test_ledger_marker', True)
            ledger.import_extras(self.store, self.config, {'freshness': packet})
        self.assertTrue(value(self.store, 'test_ledger_marker'))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM freshness_receipts').fetchone()[0], 0)
        self.assertIsNotNone(value(self.store, 'extras_error_freshness'))
        self.assertIsNone(value(self.store, 'freshness_history'))

    def test_mirror_replays_and_conflicts_are_immutable(self):
        packet = freshness.history(self.cloud, AT)
        with self.store.db: freshness.mirror_history(self.store, packet)
        with self.store.db: freshness.mirror_history(self.store, packet)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM freshness_receipts').fetchone()[0], 1)
        conflict = copy.deepcopy(packet)
        conflict['rows'][0]['completed_at'] = '2026-10-14T23:59:00+00:00'
        conflict['completed_at'] = conflict['rows'][0]['completed_at']
        with self.assertRaisesRegex(ValueError, '冲突'), self.store.db:
            freshness.mirror_history(self.store, conflict)
        self.assertEqual(value(self.store, 'freshness_history')['completed_at'], OLD)

    def test_independent_display_does_not_change_lease_or_ledger(self):
        before = lease(self.cloud, self.cloud_config, AT)
        version = ledger.version(self.cloud)
        packet = self.packet()
        with self.cloud.db: answer = sync.handle(self.cloud, self.cloud_config, '/api/sync/freshness', packet, AT)
        self.assertEqual(answer['freshness']['status'], 'UPDATED')
        self.assertEqual(before, lease(self.cloud, self.cloud_config, AT))
        self.assertEqual(version, ledger.version(self.cloud))
        self.assertFalse(before['active'])
        self.assertEqual(value(self.cloud, 'research_received_at'), OLD)
        self.assertEqual(self.cloud.db.execute('SELECT count(*) FROM cloud_receipts').fetchone()[0], 1)

    def test_strict_projection_rejects_private_or_strategy_fields(self):
        packet = self.packet()
        for mutate in (lambda p: p.update(plans=[]), lambda p: p['summary'].update(raw='PRIVATE_MARKER'),
                       lambda p: p['summary']['heartbeat'].update(execution_enabled='yes')):
            bad = copy.deepcopy(packet); mutate(bad); bad['content_hash'] = freshness.packet_hash(bad)
            with self.assertRaises(ValueError), self.cloud.db: freshness.receive(self.cloud, bad, AT)
        self.assertIsNone(value(self.cloud, freshness.STATE))
        with patch('ashare.strategy_freshness.summary', return_value={'raw': 'PRIVATE_MARKER'}):
            self.assertEqual(freshness.local_view(self.store, self.config, AT)['status'], 'UNAVAILABLE')

    def test_idempotence_stale_and_conflicting_generation(self):
        packet = self.packet()
        with self.cloud.db: freshness.receive(self.cloud, packet, AT)
        changes = self.cloud.db.total_changes
        with self.cloud.db: result = freshness.receive(self.cloud, packet, AT)
        self.assertEqual(result['freshness']['status'], 'UNCHANGED')
        self.assertEqual(changes, self.cloud.db.total_changes)
        with self.cloud.db: result = freshness.receive(self.cloud, self.packet(OLD), AT)
        self.assertEqual(result['freshness']['status'], 'IGNORED_STALE')
        bad = copy.deepcopy(packet); bad['summary']['heartbeat']['invalid_receipt_count'] = 10
        bad['content_hash'] = freshness.packet_hash(bad)
        with self.assertRaisesRegex(ValueError, '冲突'), self.cloud.db: freshness.receive(self.cloud, bad, AT)

    def test_feature_gate_and_disable_tombstone(self):
        with self.store.db: put(self.store, 'remote_features', [])
        with patch('ashare.cloud_sync.request', side_effect=AssertionError('old cloud')):
            self.assertIsNone(freshness.deliver(self.store, self.config))
        with self.store.db: put(self.store, 'remote_features', [freshness.FEATURE])
        def transport(config, path, packet):
            self.assertEqual(path, '/api/sync/freshness')
            with self.cloud.db: return freshness.receive(self.cloud, packet, packet['generated_at'])
        with patch('ashare.cloud_sync.request', side_effect=transport) as send, patch('ashare.freshness_sync.now', return_value=AT):
            freshness.deliver(self.store, dict(self.config, strategy_freshness_enabled=False))
            freshness.deliver(self.store, dict(self.config, strategy_freshness_enabled=False))
        self.assertEqual(send.call_count, 1)
        self.assertIsNone(freshness.cloud_view(self.cloud, self.cloud_config, AT))
        self.assertIsNone(freshness.local_view(self.store, dict(self.config, strategy_freshness_enabled=False), AT))

    def test_cloud_marks_stale_snapshot_and_reads_lease_live(self):
        with self.cloud.db: freshness.receive(self.cloud, self.packet(), AT)
        shown = freshness.cloud_view(self.cloud, self.cloud_config, LATER)
        self.assertTrue(shown['stale']); self.assertEqual(shown['as_of'], AT)
        self.assertFalse(shown['current_lease']['active'])
        self.assertEqual(shown['current_lease']['remaining_seconds'], 0)

    def test_disable_retries_after_lost_enable_response_and_stale_ack(self):
        disabled = dict(self.config, strategy_freshness_enabled=False)
        calls = []
        def transport(config, path, packet):
            calls.append(packet['status'])
            with self.cloud.db: result = freshness.receive(self.cloud, packet, packet['generated_at'])
            if packet['enabled']: raise TimeoutError('response lost after acceptance')
            return result
        with patch('ashare.cloud_sync.request', side_effect=transport), patch('ashare.freshness_sync.now', return_value=OLD):
            freshness.deliver(self.store, disabled)
        with patch('ashare.cloud_sync.request', side_effect=transport), patch('ashare.freshness_sync.now', return_value=AT):
            with self.assertRaises(TimeoutError): freshness.deliver(self.store, self.config)
        # A stale tombstone must not suppress the next attempt.
        with patch('ashare.cloud_sync.request', side_effect=transport), patch('ashare.freshness_sync.now', return_value=OLD):
            freshness.deliver(self.store, disabled)
        self.assertFalse(value(self.store, 'freshness_sent_disabled'))
        with patch('ashare.cloud_sync.request', side_effect=transport), patch('ashare.freshness_sync.now', return_value=LATER):
            freshness.deliver(self.store, disabled)
        self.assertEqual(calls, ['DISABLED', 'OK', 'DISABLED', 'DISABLED'])
        self.assertFalse(value(self.cloud, freshness.STATE)['enabled'])

    def test_ledger_compatibility_and_extra_failure_isolation(self):
        with self.cloud.db:
            old = ledger.export_ledger_v2(self.cloud, {'protocol': 2}, AT, config=self.cloud_config)
        self.assertNotIn('extras', old)
        with patch('ashare.freshness_sync.history', side_effect=ValueError('synthetic failure')), self.cloud.db:
            new = ledger.export_ledger_v2(self.cloud, {'protocol': 2, 'extras': {'freshness': True}}, AT, config=self.cloud_config)
        self.assertEqual(old['tables'], new['tables'])
        self.assertIn('freshness_error', new['extras'])

    def test_worker_keeps_review_delivery_when_freshness_fails(self):
        from ashare.page_display import FEATURE
        with self.store.db: put(self.store, 'remote_features', [FEATURE, freshness.FEATURE])
        with patch('ashare.freshness_sync.deliver', side_effect=ValueError('synthetic failure')), \
             patch('ashare.cloud_sync.deliver_page_display', return_value={'status': 'REVIEW_OK'}) as review:
            result = sync.page_display_once(self.config)
        review.assert_called_once()
        self.assertEqual(result['status'], 'REVIEW_OK')
        self.assertEqual(value(self.store, 'freshness_sync')['status'], 'FAILED')
        self.assertIsNone(value(self.store, 'last_sync'))

    def test_diagnostic_switch_does_not_change_research_identity(self):
        from ashare import build, config_ops
        from ashare.settings import DEFAULTS
        self.assertTrue(DEFAULTS['strategy_freshness_enabled'])
        self.assertEqual(config_ops.classify('strategy_freshness_enabled'), 'OPERATIONAL')
        disabled = dict(self.config, strategy_freshness_enabled=False)
        self.assertEqual(build.config_fingerprint(self.config), build.config_fingerprint(disabled))
        with patch('ashare.build.behavior_runtime_fingerprint', return_value='synthetic-runtime'):
            self.assertEqual(build.behavior_info(self.config, self.store), build.behavior_info(disabled, self.store))
        self.assertNotIn('cloud_sync.py', build.DISPLAY_ONLY_MODULES)
        self.assertNotIn('scheduler.py', build.DISPLAY_ONLY_MODULES)


if __name__ == '__main__': unittest.main()
