"""Periodic withdrawal discovery without orders, models, network, or live config."""
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ashare import scheduler, withdrawals
from ashare.cloud_runtime import put
from ashare.finance import PaperLedger
from ashare.settings import DEFAULTS
from ashare.storage import Store


AT = '2026-10-11T02:00:00+00:00'


class WithdrawalSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.config = {**DEFAULTS, 'data_dir': self.tmp.name, 'deployment_role': 'cloud',
                       'mode': 'paper', 'investment_policy': 'days_cash_v1',
                       'portfolio_strategy': None, 'watchlist': [], 'scheduler_enabled': False}
        self.worker = scheduler.Scheduler.__new__(scheduler.Scheduler)
        self.worker.config_path = 'unused-synthetic-config'
        self.worker.last_withdrawal_check = None
        self.worker.futures = {}
        self.worker.dynamic_futures = {}
        self.worker.global_futures = {}
        self.worker.pending_completions = {}
        self.worker.monitor = SimpleNamespace(tick=Mock())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def initialize(self, cash=16_000_000):
        PaperLedger(self.store).initialize()
        with self.store.db:
            self.store.db.execute('UPDATE paper_accounts SET cash_cents=?', (cash,))
            put(self.store, 'initialized', True)

    def error(self):
        row = self.store.db.execute("SELECT value FROM service_state WHERE key='withdrawal_error'").fetchone()
        return row[0] if row else None

    def tick(self, monotonic=100):
        with patch.object(scheduler, 'load_config', return_value=self.config), \
             patch.object(scheduler, 'now', return_value=AT), \
             patch.object(scheduler.time, 'monotonic', return_value=monotonic):
            self.worker.tick()

    def test_no_orders_still_create_cycle_before_disabled_execution_early_return(self):
        self.initialize()
        with self.store.db:
            put(self.store, 'execution_enabled', False)
        def monitor_first(config):
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM withdrawal_cycles').fetchone()[0], 0)
        self.worker.monitor.tick.side_effect = monitor_first
        self.tick()
        self.worker.monitor.tick.assert_called_once_with(self.config)
        result = withdrawals.status(self.store, self.config, AT)
        self.assertEqual(result['status'], 'READY', result)
        self.assertEqual(result['reserved_cents'], 4_000_000)
        self.assertTrue(result['pending'])
        self.assertEqual(result['withdrawn_cents'], 0)
        self.assertEqual(result['current_stage'], 1)
        for table in ('paper_orders', 'dynamic_orders', 'global_orders', 'withdrawal_transfers'):
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM '+table).fetchone()[0], 0)
        self.assertIsNotNone(self.store.db.execute("SELECT value FROM service_state WHERE key='heartbeat'").fetchone())

    def test_research_modes_and_uninitialized_cloud_never_initialize_or_write_ledger(self):
        configurations = ({'deployment_role': 'research', 'mode': 'paper'},
                          {'deployment_role': 'standalone', 'mode': 'research'},
                          {'deployment_role': 'cloud', 'mode': 'paper'})
        with patch('ashare.withdrawals.refresh') as refresh:
            for selected in configurations:
                with self.subTest(config=selected):
                    self.worker.withdrawal_tick(self.store, {**self.config, **selected}, AT)
                    for table in ('paper_accounts', 'paper_flows', 'portfolio_risk', 'withdrawal_cycles'):
                        self.assertEqual(self.store.db.execute('SELECT count(*) FROM '+table).fetchone()[0], 0)
                    self.assertIsNone(self.worker.last_withdrawal_check)
        refresh.assert_not_called()

    def test_standalone_paper_discovers_without_cloud_initialization(self):
        self.initialize()
        with self.store.db:
            put(self.store, 'initialized', False)
        self.worker.withdrawal_tick(self.store, {**self.config, 'deployment_role': 'standalone'}, AT)
        self.assertEqual(self.store.db.execute('SELECT status FROM withdrawal_cycles').fetchone()[0], 'READY')

    def test_sixty_second_throttle_rechecks_new_crossing_at_boundary(self):
        self.initialize(14_000_000)
        with patch('ashare.withdrawals.refresh', wraps=withdrawals.refresh) as refresh:
            with patch.object(scheduler.time, 'monotonic', return_value=100):
                self.worker.withdrawal_tick(self.store, self.config, AT)
            with self.store.db:
                self.store.db.execute('UPDATE paper_accounts SET cash_cents=16000000')
            with patch.object(scheduler.time, 'monotonic', return_value=159.999):
                self.worker.withdrawal_tick(self.store, self.config, AT)
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM withdrawal_cycles').fetchone()[0], 0)
            with patch.object(scheduler.time, 'monotonic', return_value=160):
                self.worker.withdrawal_tick(self.store, self.config, AT)
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM withdrawal_cycles').fetchone()[0], 1)

    def test_failure_records_error_keeps_tick_alive_then_retries_and_clears_error(self):
        self.initialize()
        with patch('ashare.withdrawals.refresh', side_effect=OSError('synthetic refresh failure')) as refresh:
            with self.assertLogs('ashare.scheduler', level='ERROR'):
                self.tick(100)
            self.tick(159)
        self.assertEqual(refresh.call_count, 1)
        self.assertIn('synthetic refresh failure', self.error())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM withdrawal_cycles').fetchone()[0], 0)
        self.assertIsNotNone(self.store.db.execute("SELECT value FROM service_state WHERE key='heartbeat'").fetchone())
        self.tick(160)
        self.assertIsNone(self.error())
        self.assertEqual(self.store.db.execute('SELECT status FROM withdrawal_cycles').fetchone()[0], 'READY')

    def test_error_record_failure_does_not_interrupt_other_tick_writes(self):
        self.initialize()
        with self.store.db:
            self.store.db.execute("""CREATE TRIGGER synthetic_withdrawal_error_insert BEFORE INSERT ON service_state
                WHEN NEW.key='withdrawal_error' BEGIN SELECT RAISE(ABORT,'synthetic error record failure'); END""")
        with patch('ashare.withdrawals.refresh', side_effect=OSError('synthetic refresh failure')):
            with self.assertLogs('ashare.scheduler', level='ERROR'):
                self.tick(100)
        self.assertIsNotNone(self.store.db.execute("SELECT value FROM service_state WHERE key='heartbeat'").fetchone())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM withdrawal_cycles').fetchone()[0], 0)
        with self.store.db:
            self.store.db.execute('DROP TRIGGER synthetic_withdrawal_error_insert')
        self.tick(160)
        self.assertEqual(self.store.db.execute('SELECT status FROM withdrawal_cycles').fetchone()[0], 'READY')

    def test_error_clear_failure_retains_committed_cycle_and_can_retry_cleanup(self):
        self.initialize()
        with self.store.db:
            self.store.db.execute("INSERT INTO service_state VALUES('withdrawal_error','earlier synthetic failure')")
            self.store.db.execute("""CREATE TRIGGER synthetic_withdrawal_error_delete BEFORE DELETE ON service_state
                WHEN OLD.key='withdrawal_error' BEGIN SELECT RAISE(ABORT,'synthetic error clear failure'); END""")
        with self.assertLogs('ashare.scheduler', level='ERROR'):
            self.tick(100)
        self.assertEqual(self.store.db.execute('SELECT status FROM withdrawal_cycles').fetchone()[0], 'READY')
        self.assertEqual(self.error(), 'earlier synthetic failure')
        self.assertIsNotNone(self.store.db.execute("SELECT value FROM service_state WHERE key='heartbeat'").fetchone())
        with self.store.db:
            self.store.db.execute('DROP TRIGGER synthetic_withdrawal_error_delete')
        self.tick(160)
        self.assertIsNone(self.error())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM withdrawal_cycles').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
