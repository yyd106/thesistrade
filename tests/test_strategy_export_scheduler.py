"""Archive work stays isolated from ledger, research and page workers."""
import tempfile
import unittest
from concurrent.futures import Future
from threading import Event
from unittest.mock import Mock, patch

from ashare.cloud_runtime import put, value
from ashare.scheduler import Scheduler
from ashare.storage import Store


class StrategyExportSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.config = {'data_dir': self.tmp.name, 'deployment_role': 'research'}
        self.scheduler = Scheduler.__new__(Scheduler)
        self.scheduler.stop = Event()
        self.scheduler.strategy_export_pool = Mock()
        self.scheduler.strategy_export_future = None
        self.scheduler.last_strategy_export = None
        with self.store.db:
            put(self.store, 'remote_features', ['strategy_export_v1'])
            put(self.store, 'research_completed_at', '2026-10-22T01:00:00+00:00')
            put(self.store, 'last_sync', {'status': 'OK'})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_one_independent_worker_every_five_minutes(self):
        future = Future()
        self.scheduler.strategy_export_pool.submit.return_value = future
        with patch('ashare.scheduler.time.monotonic', return_value=1000):
            self.scheduler.strategy_export_tick(self.store, self.config)
            self.scheduler.strategy_export_tick(self.store, self.config)
        self.scheduler.strategy_export_pool.submit.assert_called_once()
        self.assertIs(self.scheduler.strategy_export_future, future)
        future.set_result({'status': 'OK'})
        with patch('ashare.scheduler.time.monotonic', return_value=1299):
            self.scheduler.strategy_export_tick(self.store, self.config)
        self.assertEqual(self.scheduler.strategy_export_pool.submit.call_count, 1)
        with patch('ashare.scheduler.time.monotonic', return_value=1300):
            self.scheduler.strategy_export_tick(self.store, self.config)
        self.assertEqual(self.scheduler.strategy_export_pool.submit.call_count, 2)

    def test_failure_does_not_fail_scheduler_or_touch_authorization(self):
        future = Future()
        future.set_exception(RuntimeError('archive unavailable'))
        self.scheduler.strategy_export_future = future
        self.scheduler.last_strategy_export = 1000
        with patch('ashare.scheduler.time.monotonic', return_value=1010):
            self.scheduler.strategy_export_tick(self.store, self.config)
        self.assertEqual(value(self.store, 'strategy_export_sync')['status'], 'FAILED')
        self.assertEqual(value(self.store, 'last_sync'), {'status': 'OK'})
        self.assertEqual(value(self.store, 'research_completed_at'), '2026-10-22T01:00:00+00:00')
        self.scheduler.strategy_export_pool.submit.assert_not_called()

    def test_cloud_old_peers_offline_and_shutdown_do_not_start(self):
        self.scheduler.strategy_export_tick(self.store, {**self.config, 'deployment_role': 'cloud'})
        self.scheduler.strategy_export_tick(self.store, self.config, offline='since')
        with self.store.db:put(self.store, 'remote_features', [])
        self.scheduler.strategy_export_tick(self.store, self.config)
        with self.store.db:put(self.store, 'remote_features', ['strategy_export_v1'])
        self.scheduler.stop.set()
        self.scheduler.strategy_export_tick(self.store, self.config)
        self.scheduler.strategy_export_pool.submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
