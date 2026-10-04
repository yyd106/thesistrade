"""A stalled display transport cannot occupy the strategy/ledger worker or lock."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ashare import cloud_sync as sync, cloud_runtime as runtime, page_display as pages
from ashare.scheduler import Scheduler
from ashare.storage import Store
from ashare.workflow import task_lock

AT = '2026-10-04T10:00:00+00:00'


class PageDisplayIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.config = {'data_dir': self.tmp.name, 'deployment_role': 'research', 'industry_enabled': False}
        with self.store.db:
            runtime.put(self.store, 'remote_features', [pages.FEATURE])
        self.packet = {'version': pages.VERSION, 'generated_at': AT, 'reviews': [],
                       'macro': {k: [] for k in ('items', 'archived_items', 'followup_items', 'history_items')}}
        self.packet['content_hash'] = pages.content_hash(self.packet)
        self.scheduler = Scheduler('unused-synthetic-config-path')

    def tearDown(self):
        self.scheduler.close()
        self.store.close()
        self.tmp.cleanup()

    def ack(self, _config, path, body):
        self.assertEqual(path, '/api/sync/reviews')
        self.assertEqual(set(body), {'page_display'})
        return {'status': 'ACCEPTED', 'page_display': {'status': 'UPDATED',
                'generated_at': body['page_display']['generated_at'],
                'content_hash': body['page_display']['content_hash']}}

    def test_stalled_display_leaves_strategy_lock_and_successive_ledger_syncs_available(self):
        entered, release = threading.Event(), threading.Event()
        worker_threads = []

        def slow_transport(*args):
            worker_threads.append(threading.current_thread())
            entered.set()
            if not release.wait(5):
                raise AssertionError('synthetic transport was not released')
            return self.ack(*args)

        with patch('ashare.page_display.collect', return_value=copy.deepcopy(self.packet)) as collect, \
             patch('ashare.cloud_sync.request', side_effect=slow_transport) as transport, \
             patch('ashare.cloud_sync.pull', return_value={'ledger_version': 'synthetic'}) as pull, \
             patch('ashare.cloud_sync.flush', return_value=None) as flush, \
             patch('ashare.cloud_sync.deliver_invalidation') as invalidation, \
             patch('ashare.cloud_sync.deliver_notices'), patch('ashare.cloud_sync.deliver_supervision'):
            try:
                self.scheduler.page_display_tick(self.store, self.config)
                self.assertTrue(entered.wait(3))
                self.assertTrue(worker_threads[0].name.startswith('page-display'))
                with task_lock(Path(self.tmp.name), 'cloud-sync'):
                    pass
                # A blocked HTTPS send has neither the strategy file lock nor a
                # SQLite write transaction, and it cannot queue work in sync_pool.
                self.store.db.execute('BEGIN IMMEDIATE')
                self.store.db.rollback()
                for _ in range(2):
                    result = self.scheduler.sync_pool.submit(sync.sync_once, self.config).result(timeout=2)
                    self.assertEqual(result['status'], 'SYNCED')
                self.assertEqual(pull.call_count, 2)
                self.assertEqual(flush.call_count, 2)
                self.assertEqual(invalidation.call_count, 2)
                self.assertEqual(runtime.value(self.store, 'last_sync')['status'], 'OK')
                self.assertIsNone(runtime.value(self.store, 'research_completed_at'))
                # A second process/CLI call uses the display lock, not the
                # strategy lock, and never starts a duplicate network request.
                self.assertEqual(sync.page_display_once(self.config)['status'], 'BUSY')
                self.scheduler.last_page_display -= 120
                self.scheduler.page_display_tick(self.store, self.config)
                self.assertEqual(collect.call_count, 1)
                self.assertEqual(transport.call_count, 1)
            finally:
                release.set()
                if self.scheduler.page_display_future is not None:
                    self.scheduler.page_display_future.result(timeout=3)
        self.assertEqual(runtime.value(self.store, 'page_display_sync')['status'], 'OK')

    def test_failure_is_only_recorded_in_page_display_state(self):
        last_sync = {'at': AT, 'status': 'OK', 'ledger_version': 'unchanged'}
        with self.store.db:
            runtime.put(self.store, 'last_sync', last_sync)
            runtime.put(self.store, 'research_completed_at', AT)
        tables = ('paper_accounts', 'paper_flows', 'paper_orders', 'paper_fills', 'portfolio_decisions',
                  'cloud_contracts', 'cloud_receipts', 'reviews', 'lessons', 'strategy_guidance')
        before = {t: [tuple(r) for r in self.store.db.execute('SELECT * FROM ' + t)] for t in tables}
        with patch('ashare.page_display.collect', side_effect=ValueError('synthetic collection failure')):
            result = sync.page_display_once(self.config)
        self.assertEqual(result['status'], 'FAILED')
        self.assertEqual(runtime.value(self.store, 'page_display_sync')['status'], 'FAILED')
        self.assertEqual(runtime.value(self.store, 'last_sync'), last_sync)
        self.assertEqual(runtime.value(self.store, 'research_completed_at'), AT)
        self.assertEqual(before, {t: [tuple(r) for r in self.store.db.execute('SELECT * FROM ' + t)] for t in tables})
        self.assertIsNone(runtime.value(self.store, 'page_display_sent_hash'))

    def test_feature_gate_and_role_skip_collection_before_submit(self):
        for features, role in (([], 'research'), (['review_news_display_v1'], 'research'),
                               ([pages.FEATURE], 'cloud')):
            with self.subTest(features=features, role=role):
                with self.store.db:
                    runtime.put(self.store, 'remote_features', features)
                config = {**self.config, 'deployment_role': role}
                with patch('ashare.page_display.collect', side_effect=AssertionError('unsupported collection')):
                    self.scheduler.page_display_tick(self.store, config)
                    self.assertIsNone(self.scheduler.page_display_future)
                    self.assertIn(sync.page_display_once(config)['status'], ('UNSUPPORTED', 'SKIPPED'))

    def test_refresh_cadence_is_independent_and_unchanged_hash_skips_transport(self):
        with patch('ashare.page_display.collect', return_value=copy.deepcopy(self.packet)) as collect, \
             patch('ashare.cloud_sync.request', side_effect=self.ack) as transport:
            self.scheduler.page_display_tick(self.store, self.config)
            self.scheduler.page_display_future.result(timeout=3)
            self.scheduler.page_display_tick(self.store, self.config)
            self.assertIsNone(self.scheduler.page_display_future)
            self.assertEqual(collect.call_count, 1)
            self.scheduler.last_page_display -= 61
            self.scheduler.page_display_tick(self.store, self.config)
            self.scheduler.page_display_future.result(timeout=3)
            self.assertEqual(collect.call_count, 2)
            self.assertEqual(transport.call_count, 1)

    def test_cancelled_worker_never_sends_and_close_joins_the_owned_thread(self):
        cancel = threading.Event()

        def finish_collect(*args):
            cancel.set()
            return copy.deepcopy(self.packet)

        with patch('ashare.page_display.collect', side_effect=finish_collect), \
             patch('ashare.cloud_sync.request', side_effect=AssertionError('cancelled transport')):
            self.assertEqual(sync.page_display_once(self.config, cancel)['status'], 'CANCELLED')
        self.assertIsNone(runtime.value(self.store, 'page_display_sent_hash'))

        entered, release = threading.Event(), threading.Event()
        workers = []

        def transport(*args):
            workers.append(threading.current_thread())
            entered.set()
            if not release.wait(5):
                raise AssertionError('synthetic transport was not released')
            return self.ack(*args)

        with patch('ashare.page_display.collect', return_value=copy.deepcopy(self.packet)), \
             patch('ashare.cloud_sync.request', side_effect=transport):
            self.scheduler.page_display_tick(self.store, self.config)
            self.assertTrue(entered.wait(3))
            closer = threading.Thread(target=self.scheduler.close, name='synthetic-scheduler-close')
            closer.start()
            try:
                self.assertTrue(self.scheduler.stop.wait(2))
                self.assertTrue(closer.is_alive())
                release.set()
                closer.join(3)
                self.assertFalse(closer.is_alive())
                self.assertTrue(all(not worker.is_alive() for worker in workers))
                self.assertTrue(self.scheduler.page_display_future.done())
            finally:
                release.set()
                closer.join(3)
        self.scheduler.page_display_tick(self.store, self.config)
        self.assertIsNone(self.scheduler.page_display_future)


if __name__ == '__main__':
    unittest.main()
