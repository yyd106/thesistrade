"""Worker completion failures use isolated stores; no operator configuration or network."""
import json
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import Future
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ashare import scheduler
from ashare.cloud_runtime import put
from ashare.storage import Store


AT = '2026-10-11T02:00:00+00:00'


class CountingFuture(Future):
    def __init__(self):
        super().__init__()
        self.reads = 0

    def result(self, *args, **kwargs):
        self.reads += 1
        return super().result(*args, **kwargs)


class RecordingPool:
    def __init__(self):
        self.submitted = []

    def submit(self, function, job):
        self.submitted.append(job['id'])
        return Future()  # Remains running; no real task or thread is started.


class SchedulerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.config = {'data_dir': self.temp.name, 'deployment_role': 'cloud',
            'scheduler_poll_seconds': 0, 'scheduler_enabled': True}
        with self.store.db:
            put(self.store, 'execution_enabled', True)
        self.worker = scheduler.Scheduler.__new__(scheduler.Scheduler)
        self.worker.config_path = 'unused-synthetic-config'
        self.worker.futures = {}
        self.worker.dynamic_futures = {}
        self.worker.global_futures = {}
        self.worker.pending_completions = {}
        self.worker.monitor = SimpleNamespace(tick=Mock())
        self.worker.pool = RecordingPool()
        self.worker.dynamic_pool = RecordingPool()
        self.worker.global_pool = RecordingPool()
        for name in ('last_settle', 'last_maintenance', 'last_dividends'):
            setattr(self.worker, name, time.monotonic())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def job(self, jid, kind, status='RUNNING'):
        scheduler.enqueue(self.store, kind, AT, jid, status)
        return dict(self.store.db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())

    def row(self, jid):
        return self.store.db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()

    def tick(self, due=None):
        with patch.object(scheduler, 'load_config', return_value=self.config), \
             patch.object(scheduler, 'now', return_value=AT), \
             patch.object(scheduler, 'schedule_due', side_effect=due) as scheduling, \
             patch.object(scheduler, 'schedule_dynamic_due'), \
             patch.object(scheduler, 'schedule_reconnected'), \
             patch('ashare.cloud_runtime.fence'):
            self.worker.tick()
        return scheduling.call_count

    def test_each_worker_pool_failure_is_finalized_once_and_next_job_runs(self):
        cases = [('futures', 'pool', 'slot'),
                 ('global_futures', 'global_pool', 'global_slot'),
                 ('dynamic_futures', 'dynamic_pool', 'dynamic_slot')]
        for workers, pool, kind in cases:
            with self.subTest(kind=kind):
                jid = 'failed-' + kind
                self.job(jid, kind)
                future = CountingFuture()
                future.set_exception(OSError('synthetic worker setup failed'))
                getattr(self.worker, workers)[jid] = future

                def due(store, config, stamp):
                    scheduler.enqueue(store, kind, stamp, 'next-' + kind)

                with self.assertLogs('ashare.scheduler', level='ERROR'):
                    self.assertEqual(self.tick(due), 1)
                self.assertEqual(self.row(jid)['status'], 'FAILED')
                self.assertEqual(self.row(jid)['finished_at'], AT)
                self.assertIn('OSError', self.row(jid)['error'])
                self.assertNotIn(jid, getattr(self.worker, workers))
                self.assertIn('next-' + kind, getattr(self.worker, pool).submitted)
                self.assertEqual(self.tick(), 1)
                self.assertEqual(future.reads, 1)
                self.assertFalse(self.worker.pending_completions)
                # Finish the synthetic next task so it cannot gate the other pool cases.
                getattr(self.worker, workers).clear()
                with self.store.db:
                    self.store.db.execute("UPDATE jobs SET status='DONE' WHERE id=?", ('next-' + kind,))

    def test_initial_worker_store_failure_records_failed_without_execution(self):
        job = self.job('initial-store', 'global_slot')
        with patch.object(scheduler, 'load_config', return_value=self.config), \
             patch.object(scheduler, 'Store', side_effect=[OSError('synthetic open failure'), self.store]), \
             patch.object(self.store, 'close'), \
             patch.object(scheduler, 'execute') as execute:
            self.worker.work(job)
        self.assertEqual(self.row(job['id'])['status'], 'FAILED')
        self.assertIn('synthetic open failure', self.row(job['id'])['error'])
        execute.assert_not_called()

    def test_completion_store_open_failure_preserves_success_without_reexecution(self):
        job = self.job('completion-store', 'global_slot')
        result = {'status': 'SUCCEEDED', 'operation_count': 1}
        with patch.object(scheduler, 'load_config', return_value=self.config), \
             patch.object(scheduler, 'Store', side_effect=[self.store, OSError('synthetic final open failure')]), \
             patch.object(self.store, 'close'), \
             patch.object(scheduler, 'execute', return_value=result) as execute:
            with self.assertRaises(scheduler.JobCompletionPending) as failure:
                self.worker.work(job)
        future = CountingFuture()
        future.set_exception(failure.exception)
        self.worker.global_futures[job['id']] = future
        with self.assertLogs('ashare.scheduler', level='WARNING'):
            self.tick()
        self.assertEqual(self.row(job['id'])['status'], 'DONE')
        self.assertEqual(json.loads(self.row(job['id'])['result_json']), result)
        self.tick()
        self.assertEqual(future.reads, 1)
        execute.assert_called_once()
        self.assertTrue(self.store.db.execute(
            "SELECT value FROM service_state WHERE key='scheduler_worker_error'").fetchone())

    def test_failed_final_write_and_failed_error_record_retry_only_persistence(self):
        job = self.job('write-blocked', 'slot')
        with self.store.db:
            self.store.db.execute("""CREATE TRIGGER block_completion BEFORE UPDATE ON jobs
                WHEN OLD.id='write-blocked' AND NEW.status='DONE'
                BEGIN SELECT RAISE(ABORT,'synthetic completion write unavailable'); END""")
        with patch.object(scheduler, 'load_config', return_value=self.config), \
             patch.object(scheduler, 'execute', return_value={'status': 'SUCCEEDED', 'decisions': []}) as execute:
            with self.assertRaises(scheduler.JobCompletionPending) as failure:
                self.worker.work(job)
        future = CountingFuture()
        future.set_exception(failure.exception)
        self.worker.futures[job['id']] = future

        def due(store, config, stamp):
            scheduler.enqueue(store, 'slot', stamp, 'next-slot')

        with self.assertLogs('ashare.scheduler', level='WARNING'):
            self.assertEqual(self.tick(due), 1)
        self.assertEqual(self.row(job['id'])['status'], 'RUNNING')
        self.assertNotIn(job['id'], self.worker.futures)
        self.assertIn(job['id'], self.worker.pending_completions)
        self.assertIn('next-slot', self.worker.pool.submitted)
        with self.store.db:
            self.store.db.execute('DROP TRIGGER block_completion')
        self.tick()
        self.assertEqual(self.row(job['id'])['status'], 'DONE')
        self.assertFalse(self.worker.pending_completions)
        self.assertEqual(future.reads, 1)
        execute.assert_called_once()

    def test_failed_worker_record_can_retry_after_database_write_failure(self):
        job = self.job('failed-record', 'global_slot')
        with self.store.db:
            self.store.db.execute("""CREATE TRIGGER block_failure BEFORE UPDATE ON jobs
                WHEN OLD.id='failed-record' AND NEW.status='FAILED'
                BEGIN SELECT RAISE(ABORT,'synthetic failure record unavailable'); END""")
        future = CountingFuture()
        future.set_exception(RuntimeError('synthetic unexpected worker failure'))
        self.worker.global_futures[job['id']] = future
        with self.assertLogs('ashare.scheduler', level='ERROR'):
            self.tick()
        self.assertIn(job['id'], self.worker.pending_completions)
        self.assertNotIn(job['id'], self.worker.global_futures)
        with self.store.db:
            self.store.db.execute('DROP TRIGGER block_failure')
        self.tick()
        self.assertEqual(self.row(job['id'])['status'], 'FAILED')
        self.assertIn('unexpected worker failure', self.row(job['id'])['error'])
        self.assertEqual(future.reads, 1)

    def test_restart_never_requeues_interrupted_trading_jobs(self):
        for kind in ('slot', 'dynamic_slot', 'global_slot'):
            self.job('restart-' + kind, kind)
        with patch.object(scheduler, 'load_config', return_value=self.config), \
             patch('ashare.cloud_ledger.ensure_change_log'):
            self.worker.recover()
        rows = list(self.store.db.execute('SELECT id,status FROM jobs'))
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r['status'] == 'INTERRUPTED' for r in rows))

    def test_pending_error_record_does_not_block_the_same_minutes_global_worker(self):
        job = self.job('failed-watchlist', 'slot')
        self.job('waiting-global', 'global_slot', 'PENDING')
        with self.store.db:
            self.store.db.execute("""CREATE TRIGGER block_failure BEFORE UPDATE ON jobs
                WHEN OLD.id='failed-watchlist' AND NEW.status='FAILED'
                BEGIN SELECT RAISE(ABORT,'synthetic error write unavailable'); END""")
        future = CountingFuture()
        future.set_exception(OSError('synthetic watchlist worker failure'))
        self.worker.futures[job['id']] = future
        with self.assertLogs('ashare.scheduler', level='ERROR'):
            self.tick()
        self.assertIn(job['id'], self.worker.pending_completions)
        self.assertIn('waiting-global', self.worker.global_pool.submitted)
        self.assertEqual(future.reads, 1)

    def test_run_retries_startup_when_even_error_record_store_is_unavailable(self):
        self.worker.stop = Event()
        self.worker.poll_seconds = 0
        self.worker.recover = Mock(side_effect=[OSError('synthetic startup outage'), None])
        self.worker.tick = Mock(side_effect=self.worker.stop.set)
        with patch.object(scheduler, 'load_config', return_value=self.config), \
             patch.object(scheduler, 'Store', side_effect=sqlite3.OperationalError('synthetic DB outage')), \
             self.assertLogs('ashare.scheduler', level='ERROR'):
            self.worker.run()
        self.assertEqual(self.worker.recover.call_count, 2)
        self.worker.tick.assert_called_once()


if __name__ == '__main__':
    unittest.main()
