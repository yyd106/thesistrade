"""Research boundaries prioritize a pending decision without parallel source writes."""
import tempfile
import time
import unittest
from concurrent.futures import Future
from contextlib import ExitStack
from threading import Event
from unittest.mock import Mock, patch

from ashare import scheduler
from ashare.storage import Store


AT = '2026-10-28T14:00:00+00:00'


class RecordingPool:
    def __init__(self):
        self.jobs = []

    def submit(self, function, job):
        self.jobs.append(job['id'])
        return Future()


class PortfolioPriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.config = {'data_dir': self.temp.name, 'deployment_role': 'research',
            'scheduler_poll_seconds': 0, 'scheduler_enabled': True, 'model_enabled': True}
        self.worker = scheduler.Scheduler.__new__(scheduler.Scheduler)
        self.worker.config_path = 'synthetic-config'
        for name in ('futures', 'dynamic_futures', 'global_futures', 'pending_completions'):
            setattr(self.worker, name, {})
        for name in ('pool', 'dynamic_pool', 'global_pool'):
            setattr(self.worker, name, RecordingPool())
        for name in ('last_sync', 'last_followups', 'last_maintenance'):
            setattr(self.worker, name, time.monotonic())
        for name in ('withdrawal_tick', 'page_display_tick', 'strategy_export_tick',
                     'supervision_tick', 'experiment_tick'):
            setattr(self.worker, name, Mock())
        self.worker.sync_future = None
        self.worker.supervision_cancel = Event()
        self.worker.experiment_cancel = Event()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def enqueue(self, kind, status='PENDING', stamp=AT):
        scheduler.enqueue(self.store, kind, stamp, kind, status)

    def state(self, kind):
        return self.store.db.execute('SELECT status FROM jobs WHERE id=?', (kind,)).fetchone()[0]

    def tick(self, offline=None):
        with ExitStack() as stack:
            stack.enter_context(patch.object(scheduler, 'load_config', return_value=self.config))
            stack.enter_context(patch.object(scheduler, 'now', return_value=AT))
            stack.enter_context(patch.object(scheduler, 'offline_since', return_value=offline))
            for name in ('schedule_due', 'schedule_dynamic_due', 'schedule_reconnected'):
                stack.enter_context(patch.object(scheduler, name))
            for name in ('ashare.recovery.enqueue_recovery', 'ashare.portfolio_strategy.request',
                         'ashare.investment_policy.seed'):
                stack.enter_context(patch(name))
            stack.enter_context(patch.object(scheduler, 'schedule_review_retry'))
            self.worker.tick()

    def test_pending_portfolio_runs_before_new_source_writers_then_research_resumes(self):
        for kind in ('portfolio_strategy', 'cycle', 'review', 'dynamic_cycle', 'global_research', 'digest'):
            self.enqueue(kind)
        self.tick()
        self.assertEqual(self.worker.global_pool.jobs, ['portfolio_strategy'])
        for kind in ('cycle', 'review', 'dynamic_cycle', 'global_research'):
            self.assertEqual(self.state(kind), 'PENDING')
        self.assertEqual(self.state('digest'), 'RUNNING')
        self.assertTrue(self.worker.strategy_export_tick.called)
        self.assertTrue(self.worker.withdrawal_tick.called)
        # Completion frees the boundary; a queued batch is not starved.
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET status='DONE',finished_at=? WHERE id='portfolio_strategy'", (AT,))
        self.worker.global_futures['portfolio_strategy'].set_result(None)
        self.tick()
        self.assertEqual(self.state('cycle'), 'RUNNING')
        self.assertEqual(self.state('dynamic_cycle'), 'RUNNING')

    def test_running_cycle_is_not_interrupted_and_portfolio_starts_on_next_boundary(self):
        self.enqueue('cycle', 'RUNNING')
        self.enqueue('portfolio_strategy')
        self.tick()
        self.assertEqual(self.state('portfolio_strategy'), 'PENDING')
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET status='DONE',finished_at=? WHERE id='cycle'", (AT,))
        self.tick()
        self.assertEqual(self.state('portfolio_strategy'), 'RUNNING')

    def test_already_running_portfolio_keeps_new_research_and_news_pending(self):
        self.enqueue('portfolio_strategy', 'RUNNING')
        for kind in ('research', 'repair', 'review', 'dynamic_cycle'):
            self.enqueue(kind)
        self.tick()
        self.assertFalse(self.worker.pool.jobs)
        self.assertFalse(self.worker.dynamic_pool.jobs)

    def test_offline_and_future_requests_are_not_started(self):
        self.enqueue('portfolio_strategy')
        self.tick(offline=AT)
        self.assertEqual(self.state('portfolio_strategy'), 'PENDING')
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET scheduled_at='2026-10-29T00:00:00+00:00' WHERE id='portfolio_strategy'")
        self.assertFalse(self.worker.start_waiting_portfolio(self.store, self.config, AT, set(), None))

    def test_busy_global_pool_does_not_reserve_an_unusable_boundary(self):
        self.enqueue('portfolio_strategy')
        self.enqueue('cycle')
        self.worker.global_futures = {'one': Future(), 'two': Future()}
        self.tick()
        self.assertEqual(self.state('portfolio_strategy'), 'PENDING')
        self.assertEqual(self.state('cycle'), 'RUNNING')

    def test_cloud_and_standalone_dispatch_are_unchanged(self):
        self.enqueue('portfolio_strategy')
        for role in ('cloud', 'standalone'):
            self.assertFalse(self.worker.start_waiting_portfolio(
                self.store, {**self.config, 'deployment_role': role}, AT, set(), None))
        self.assertEqual(self.state('portfolio_strategy'), 'PENDING')


if __name__ == '__main__':
    unittest.main()
