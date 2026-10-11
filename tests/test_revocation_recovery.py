"""Durable revocations across outages and concurrent research/sync workers."""
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

from ashare import cloud_runtime as runtime, cloud_sync as sync
from ashare.storage import Store


class RevocationRecoveryTests(unittest.TestCase):
    at = '2026-10-11T01:00:00+00:00'
    later = '2026-10-11T01:06:00+00:00'
    first_key = 'watchlist:sh600519'
    second_key = 'global:BTC'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'research')
        self.cloud = Store(self.root / 'cloud')
        # Read only the checked-in example; never load operator configuration.
        self.cfg = json.loads((Path(__file__).resolve().parents[1] / 'config.example.json').read_text())
        self.cfg.update(data_dir=str(self.store.root), deployment_role='research', industry_enabled=False)
        self.cloud_cfg = {**self.cfg, 'data_dir': str(self.cloud.root), 'deployment_role': 'cloud'}
        with self.store.db:
            runtime.put(self.store, 'remote_features', ['targeted_invalidation'])

    def tearDown(self):
        self.store.close()
        self.cloud.close()
        self.tmp.cleanup()

    def receive(self, body, at=None):
        self.cloud.db.execute('BEGIN IMMEDIATE')
        try:
            result = sync.handle(self.cloud, self.cloud_cfg, '/api/sync/invalidate', body, at or self.later)
            self.cloud.db.commit()
            return result
        except BaseException:
            self.cloud.db.rollback()
            raise

    def test_old_event_applies_without_renewing_research_lease(self):
        completed = '2026-10-11T00:59:00+00:00'
        with self.cloud.db:
            runtime.put(self.cloud, 'research_completed_at', completed)
        answer = self.receive({'changed_at': self.at, 'keys': [self.first_key]})
        self.assertEqual(answer['outcome'], 'APPLIED')
        self.assertEqual(runtime.value(self.cloud, 'invalidated_keys'), {self.first_key: self.at})
        self.assertEqual(runtime.value(self.cloud, 'research_completed_at'), completed)

    def test_covered_old_event_is_acknowledged_without_revoking_newer_research(self):
        with self.cloud.db:
            runtime.put(self.cloud, 'research_completed_at', self.later)
        answer = self.receive({'changed_at': self.at, 'keys': [self.first_key]})
        self.assertEqual(answer, {'status': 'ACCEPTED', 'scope': 'KEYS', 'outcome': 'COVERED'})
        self.assertIsNone(runtime.value(self.cloud, 'invalidated_keys'))
        self.assertIsNone(runtime.value(self.cloud, 'invalidated_at'))

    def test_old_event_still_requires_valid_keys(self):
        with self.cloud.db:
            runtime.put(self.cloud, 'research_completed_at', self.later)
        invalid = [None, {}, self.first_key, [], [1], ['x'], ['x' * 121],
                   ['*', 1], [self.first_key, self.first_key], [f'key:{i}' for i in range(201)]]
        for keys in invalid:
            with self.subTest(keys=keys), self.assertRaisesRegex(ValueError, '失效标的格式无效'):
                self.receive({'changed_at': self.at, 'keys': keys})

    def test_future_event_limit_remains_and_global_watermark_never_moves_back(self):
        with self.assertRaisesRegex(ValueError, '失效通知时间无效'):
            self.receive({'changed_at': '2026-10-11T01:11:01+00:00'})
        self.receive({'changed_at': '2026-10-11T01:11:00+00:00'})
        self.receive({'changed_at': self.at})
        self.assertEqual(runtime.value(self.cloud, 'invalidated_at'), '2026-10-11T01:11:00+00:00')

    def test_outage_recovery_reaches_publication_and_notification_delivery(self):
        sync.queue_invalidation(self.store, [self.first_key], self.at)

        def request(config, path, body):
            self.assertEqual(path, '/api/sync/invalidate')
            return self.receive(body)

        with patch.object(sync, 'pull', return_value={'ledger_version': 'synthetic-ledger'}), \
                patch.object(sync, 'now', return_value=self.later), \
                patch.object(sync, 'request', side_effect=request), \
                patch.object(sync, 'flush', return_value=None) as publication, \
                patch.object(sync, 'deliver_notices', return_value=None) as notices:
            self.assertEqual(sync.sync_once(self.cfg), {'status': 'SYNCED'})
        publication.assert_called_once()
        notices.assert_called_once()
        self.assertIsNone(runtime.value(self.store, 'pending_invalidation'))
        self.assertEqual(runtime.value(self.store, 'last_sync')['status'], 'OK')

    def test_failed_delivery_keeps_queue_and_legacy_queue_can_be_acknowledged(self):
        old = {'changed_at': self.at, 'keys': [self.first_key]}
        with self.store.db:
            runtime.put(self.store, 'pending_invalidation', old)
        with patch.object(sync, 'request', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                sync.deliver_invalidation(self.store, self.cfg)
        self.assertEqual(runtime.value(self.store, 'pending_invalidation'), old)
        with patch.object(sync, 'request', return_value={'status': 'ACCEPTED'}):
            sync.deliver_invalidation(self.store, self.cfg)
        self.assertIsNone(runtime.value(self.store, 'pending_invalidation'))

    def test_same_key_added_during_request_survives_old_ack_even_in_same_second(self):
        other = Store(self.store.root)
        try:
            for timestamp in (self.later, self.at):
                with self.subTest(timestamp=timestamp):
                    with self.store.db:
                        runtime.put(self.store, 'pending_invalidation', None)
                    sync.queue_invalidation(self.store, [self.first_key], self.at)
                    old = runtime.value(self.store, 'pending_invalidation')

                    def request(*args):
                        sync.queue_invalidation(other, [self.first_key], timestamp)
                        return {'status': 'ACCEPTED'}

                    with patch.object(sync, 'request', side_effect=request):
                        sync.deliver_invalidation(self.store, self.cfg)
                    current = runtime.value(self.store, 'pending_invalidation')
                    self.assertEqual(current['keys'], [self.first_key])
                    self.assertEqual(current['changed_at'], timestamp)
                    self.assertNotEqual(current['revision'], old['revision'])
                    with patch.object(sync, 'request', return_value={'status': 'ACCEPTED'}):
                        sync.deliver_invalidation(self.store, self.cfg)
                    self.assertIsNone(runtime.value(self.store, 'pending_invalidation'))
        finally:
            other.close()

    def test_concurrent_queue_merges_both_workers(self):
        ready = {name: Event() for name in ('first', 'second')}
        start = {name: Event() for name in ready}
        first_read, release, second_begin, second_read = Event(), Event(), Event(), Event()
        original = sync.value

        def value(store, key, default=None):
            result = original(store, key, default)
            if key == 'pending_invalidation':
                if getattr(store, 'test_worker', None) == 'first':
                    first_read.set()
                    if not release.wait(5):
                        raise TimeoutError('first queue was not released')
                elif getattr(store, 'test_worker', None) == 'second':
                    second_read.set()
            return result

        def worker(name, key):
            store = Store(self.store.root)
            store.test_worker = name
            if name == 'second':
                store.db.set_trace_callback(lambda sql: second_begin.set() if sql == 'BEGIN IMMEDIATE' else None)
            ready[name].set()
            try:
                if not start[name].wait(5):
                    raise TimeoutError('queue worker was not started')
                sync.queue_invalidation(store, [key], self.at)
            finally:
                store.close()

        with patch.object(sync, 'value', side_effect=value), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, 'first', self.first_key), pool.submit(worker, 'second', self.second_key)]
            try:
                self.assertTrue(all(event.wait(5) for event in ready.values()))
                start['first'].set()
                self.assertTrue(first_read.wait(5))
                start['second'].set()
                self.assertTrue(second_begin.wait(5))
                self.assertFalse(second_read.is_set(), 'second worker read before acquiring the write lock')
            finally:
                release.set()
                for event in start.values():
                    event.set()
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(set(runtime.value(self.store, 'pending_invalidation')['keys']),
                         {self.first_key, self.second_key})

    def test_queue_waiting_behind_ack_is_not_cleared(self):
        sync.queue_invalidation(self.store, [self.first_key], self.at)
        ready = {name: Event() for name in ('ack', 'queue')}
        start = {name: Event() for name in ready}
        ack_read, release, queue_begin = Event(), Event(), Event()
        reads = 0
        original = sync.value

        def value(store, key, default=None):
            nonlocal reads
            result = original(store, key, default)
            if key == 'pending_invalidation' and getattr(store, 'test_worker', None) == 'ack':
                reads += 1
                if reads == 2:
                    ack_read.set()
                    if not release.wait(5):
                        raise TimeoutError('ack was not released')
            return result

        def worker(name):
            store = Store(self.store.root)
            store.test_worker = name
            if name == 'queue':
                store.db.set_trace_callback(lambda sql: queue_begin.set() if sql == 'BEGIN IMMEDIATE' else None)
            ready[name].set()
            try:
                if not start[name].wait(5):
                    raise TimeoutError('worker was not started')
                if name == 'ack':
                    sync.deliver_invalidation(store, self.cfg)
                else:
                    sync.queue_invalidation(store, [self.second_key], self.later)
            finally:
                store.close()

        with patch.object(sync, 'value', side_effect=value), \
                patch.object(sync, 'request', return_value={'status': 'ACCEPTED'}), \
                ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, name) for name in ready]
            try:
                self.assertTrue(all(event.wait(5) for event in ready.values()))
                start['ack'].set()
                self.assertTrue(ack_read.wait(5))
                start['queue'].set()
                self.assertTrue(queue_begin.wait(5))
            finally:
                release.set()
                for event in start.values():
                    event.set()
            for future in futures:
                future.result(timeout=5)
        pending = runtime.value(self.store, 'pending_invalidation')
        self.assertEqual(pending['keys'], [self.second_key])
        self.assertEqual(pending['changed_at'], self.later)

    def test_queue_and_ack_preserve_outer_transaction_rollback(self):
        other = Store(self.store.root)
        try:
            self.store.db.execute('BEGIN')
            runtime.put(self.store, 'unrelated', 'not-committed')
            sync.queue_invalidation(self.store, [self.first_key], self.at)
            self.assertTrue(self.store.db.in_transaction)
            self.assertIsNone(runtime.value(other, 'pending_invalidation'))
            self.store.db.rollback()
            self.assertIsNone(runtime.value(self.store, 'unrelated'))
            self.assertIsNone(runtime.value(self.store, 'pending_invalidation'))

            sync.queue_invalidation(self.store, [self.first_key], self.at)
            original = runtime.value(self.store, 'pending_invalidation')
            self.store.db.execute('BEGIN')
            with patch.object(sync, 'request', return_value={'status': 'ACCEPTED'}):
                sync.deliver_invalidation(self.store, self.cfg)
            self.assertTrue(self.store.db.in_transaction)
            self.assertEqual(runtime.value(other, 'pending_invalidation'), original)
            self.store.db.rollback()
            self.assertEqual(runtime.value(self.store, 'pending_invalidation'), original)
            self.assertIsNone(runtime.value(self.store, 'last_invalidation'))
        finally:
            self.store.db.rollback()
            other.close()

    def test_stale_outer_read_snapshot_cannot_overwrite_new_revocation(self):
        other = Store(self.store.root)
        try:
            self.store.db.execute('BEGIN')
            self.assertIsNone(runtime.value(self.store, 'pending_invalidation'))
            sync.queue_invalidation(other, [self.second_key], self.later)
            with self.assertRaises(sqlite3.OperationalError):
                sync.queue_invalidation(self.store, [self.first_key], self.at)
            self.assertTrue(self.store.db.in_transaction)
            self.store.db.rollback()
            self.assertEqual(runtime.value(self.store, 'pending_invalidation')['keys'], [self.second_key])
            sync.queue_invalidation(self.store, [self.first_key], self.at)
            self.assertEqual(set(runtime.value(self.store, 'pending_invalidation')['keys']),
                             {self.first_key, self.second_key})
        finally:
            self.store.db.rollback()
            other.close()


if __name__ == '__main__':
    unittest.main()
