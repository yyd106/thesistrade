"""Synthetic integration checks for forward enrollment and background priority."""
import hashlib
import tempfile
import unittest
from concurrent.futures import Future
from threading import Event
from unittest.mock import MagicMock, patch

from ashare import research, workflow
from ashare.scheduler import Scheduler
from ashare.settings import DEFAULTS
from ashare.storage import Store


AT = '2026-10-03T08:00:00+00:00'
SYMBOL = 'sh600000'


class Pool:
    def __init__(self):
        self.calls = []

    def submit(self, *args):
        self.calls.append(args)
        return Future()


class ExperimentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cfg = {**DEFAULTS, 'data_dir': self.tmp.name, 'deployment_role': 'standalone',
                    'mode': 'paper', 'model_enabled': True, 'model_timeout_seconds': 60,
                    'watchlist': [{'symbol': SYMBOL, 'name': '合成公司'}],
                    'live_execution_enabled': False, 'paid_api_fallback': False}
        self.packet = {'snapshot_id': 'synthetic-snapshot', 'symbol': SYMBOL, 'as_of': AT}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def scheduler(self):
        scheduler = object.__new__(Scheduler)
        scheduler.experiment_future = None
        scheduler.experiment_cancel = Event()
        scheduler.last_experiment_scan = None
        scheduler.experiment_scan_ok = True
        scheduler.experiment_pool = Pool()
        scheduler.supervision_future = None
        return scheduler

    def execute_research(self, *, reuse=None, use_model=True, enroll_error=None):
        order = []

        def enroll(*args):
            order.append('enroll')
            if enroll_error:
                raise enroll_error
            self.assertEqual(args[2], self.packet)
            return {'status': 'ENROLLED'}

        def study(*args):
            order.append('study')
            return {'status': 'SUCCEEDED'}

        with patch('ashare.workflow.make_snapshot', return_value=self.packet), \
             patch('ashare.research.reusable', return_value=reuse), \
             patch('ashare.research.renew', return_value={'status': 'REUSED'}) as renew, \
             patch('ashare.research.persist_snapshot', side_effect=lambda *a: order.append('persist')), \
             patch('ashare.experiment_runner.enroll', side_effect=enroll), \
             patch('ashare.workflow.study', side_effect=study), \
             patch('ashare.connectivity.check'), \
             patch('ashare.portfolio_strategy.request'), \
             patch('ashare.storage.Store.periodic_backup'):
            result = workflow.execute(self.cfg, 'research', use_model=use_model)
        return order, result, renew.call_count

    def test_production_prompt_is_byte_identical_after_extraction(self):
        # Golden digest of the pre-runner production prompt with this fixed packet.
        # A deliberate production prompt change must explicitly replace this value.
        packet = {'symbol': SYMBOL, 'as_of': AT}
        with patch('ashare.research.model_packet', return_value=packet):
            prompt = research.build_prompt(packet, {})
        self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(),
                         'bf2c3cba9805abcb9d69a5bbf724b2d1171e3ce0d379a203f5541df598ab5fd6')

    def test_new_input_is_frozen_and_enrolled_before_production_study(self):
        order, result, renewals = self.execute_research()
        self.assertEqual(order, ['persist', 'enroll', 'study'])
        self.assertEqual(result[0]['status'], 'SUCCEEDED')
        self.assertEqual(renewals, 0)

    def test_renewals_and_model_disabled_runs_do_not_enroll(self):
        order, result, renewals = self.execute_research(reuse={'study': 'existing'})
        self.assertEqual(order, [])
        self.assertEqual(result[0]['status'], 'REUSED')
        self.assertEqual(renewals, 1)
        order, _, _ = self.execute_research(use_model=False)
        self.assertEqual(order, ['persist', 'study'])
        self.cfg['model_enabled'] = False
        order, _, _ = self.execute_research()
        self.assertEqual(order, ['persist', 'study'])

    def test_enrollment_failure_does_not_block_research_or_log_input_text(self):
        order, result, _ = self.execute_research(enroll_error=ValueError('PRIVATE_INPUT_MARKER'))
        self.assertEqual(order, ['persist', 'enroll', 'study'])
        self.assertEqual(result[0]['status'], 'SUCCEEDED')
        row = self.store.db.execute("SELECT status,detail FROM data_attempts WHERE source='experiment_enrollment'").fetchone()
        self.assertEqual(tuple(row), ('FAILED', 'ValueError'))

    def test_scheduler_yields_to_research_and_supervision(self):
        scheduler = self.scheduler()
        with patch('ashare.experiment_runner.advance'), \
             patch('ashare.experiment_runner.next_pending', return_value={'id': 'pair-one'}) as pending, \
             patch('ashare.supervision.busy', return_value=True) as busy, \
             patch('ashare.supervision.next_pending', return_value=None) as review:
            scheduler.experiment_tick(self.store, self.cfg, AT)
            self.assertTrue(scheduler.experiment_cancel.is_set())
            busy.return_value = False
            review.return_value = {'id': 'review-one'}
            scheduler.experiment_tick(self.store, self.cfg, AT)
            review.return_value = None
            scheduler.supervision_future = Future()
            scheduler.experiment_tick(self.store, self.cfg, AT)
            pending.assert_not_called()
            self.assertEqual(scheduler.experiment_pool.calls, [])
            scheduler.supervision_future = None
            scheduler.experiment_tick(self.store, self.cfg, AT)
            self.assertFalse(scheduler.experiment_cancel.is_set())
            self.assertEqual(len(scheduler.experiment_pool.calls), 1)
            self.assertEqual(scheduler.experiment_pool.calls[0][2], 'pair-one')
            scheduler.experiment_tick(self.store, self.cfg, AT)
            self.assertEqual(len(scheduler.experiment_pool.calls), 1)

    def test_active_worker_is_cancelled_on_offline_or_disabled_models(self):
        scheduler = self.scheduler()
        scheduler.experiment_future = Future()
        with patch('ashare.experiment_runner.advance'), \
             patch('ashare.experiment_runner.next_pending') as pending:
            for config, offline in ((self.cfg, True), ({**self.cfg, 'scheduler_enabled': False}, None),
                                    ({**self.cfg, 'model_enabled': False}, None)):
                scheduler.experiment_cancel.clear()
                scheduler.experiment_tick(self.store, config, AT, offline=offline)
                self.assertTrue(scheduler.experiment_cancel.is_set())
            pending.assert_not_called()

    def test_deadline_advance_is_model_free_and_throttled_even_offline(self):
        scheduler = self.scheduler()
        with patch('ashare.experiment_runner.advance') as advance, \
             patch('ashare.experiment_runner.next_pending') as pending, \
             patch('ashare.scheduler.time.monotonic', return_value=100) as clock:
            scheduler.experiment_tick(self.store, self.cfg, AT, offline=True)
            clock.return_value = 159
            scheduler.experiment_tick(self.store, self.cfg, AT, offline=True)
            advance.assert_called_once_with(self.store, self.cfg, AT)
            clock.return_value = 160
            scheduler.experiment_tick(self.store, self.cfg, AT, offline=True)
            self.assertEqual(advance.call_count, 2)
            pending.assert_not_called()
            self.assertEqual(scheduler.experiment_pool.calls, [])

    def test_cloud_never_advances_or_starts_experiment_work(self):
        scheduler = self.scheduler()
        with patch('ashare.experiment_runner.advance') as advance, \
             patch('ashare.experiment_runner.next_pending') as pending:
            scheduler.experiment_tick(self.store, {**self.cfg, 'deployment_role': 'cloud'}, AT)
            advance.assert_not_called()
            pending.assert_not_called()
        self.assertTrue(scheduler.experiment_cancel.is_set())
        self.assertEqual(scheduler.experiment_pool.calls, [])

    def test_failed_deadline_scan_blocks_work_until_a_successful_scan(self):
        scheduler = self.scheduler()
        with patch('ashare.experiment_runner.advance', side_effect=ValueError('PRIVATE_INPUT_MARKER')) as advance, \
             patch('ashare.experiment_runner.next_pending', return_value={'id': 'pair-one'}) as pending, \
             patch('ashare.supervision.busy', return_value=False), \
             patch('ashare.supervision.next_pending', return_value=None), \
             patch('ashare.scheduler.time.monotonic', return_value=100) as clock:
            scheduler.experiment_tick(self.store, self.cfg, AT)
            clock.return_value = 159
            scheduler.experiment_tick(self.store, self.cfg, AT)
            self.assertTrue(scheduler.experiment_cancel.is_set())
            self.assertEqual(advance.call_count, 1)
            pending.assert_not_called()
            advance.side_effect = None
            clock.return_value = 160
            scheduler.experiment_tick(self.store, self.cfg, AT)
            self.assertEqual(len(scheduler.experiment_pool.calls), 1)

    def test_worker_errors_are_contained_and_report_only_exception_type(self):
        scheduler = self.scheduler()
        future = Future()
        future.set_exception(ValueError('PRIVATE_INPUT_MARKER'))
        scheduler.experiment_future = future
        with patch('ashare.experiment_runner.advance'), \
             patch('ashare.experiment_runner.next_pending', side_effect=RuntimeError('RAW_PATH_MARKER')), \
             patch('ashare.supervision.busy', return_value=False), \
             patch('ashare.supervision.next_pending', return_value=None):
            scheduler.experiment_tick(self.store, self.cfg, AT)
        self.assertIsNone(scheduler.experiment_future)
        saved = self.store.db.execute("SELECT value FROM service_state WHERE key='experiment_worker_error'").fetchone()[0]
        self.assertEqual(saved, 'RuntimeError')

    def test_shutdown_cancels_experiment_before_waiting_for_its_pool(self):
        scheduler = self.scheduler()
        scheduler.stop = Event()
        scheduler.supervision_cancel = Event()
        for name in ('supervision_pool', 'pool', 'dynamic_pool', 'global_pool', 'monitor', 'sync_pool', 'reports_pool', 'page_display_pool'):
            setattr(scheduler, name, MagicMock())
        scheduler.experiment_pool = MagicMock()
        scheduler.experiment_pool.shutdown.side_effect = lambda **kw: self.assertTrue(scheduler.experiment_cancel.is_set())
        scheduler.close()
        scheduler.experiment_pool.shutdown.assert_called_once_with(wait=True, cancel_futures=True)


if __name__ == '__main__':
    unittest.main()
