"""storage_worker keeps heavy storage maintenance off the scheduler tick."""
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ashare import maintenance, scheduler
from ashare.storage import Store
from test_storage_budget import hourly

AT = '2026-11-08T02:00:00+00:00'


def cfg_for(tmp, **kw):
    from ashare.settings import DEFAULTS
    return {**DEFAULTS, 'deployment_role': 'standalone', 'data_dir': str(tmp),
            'backup_daily_keep': 7, 'scheduler_enabled': True, 'model_enabled': False, **kw}


def scheduler_mock():
    w = scheduler.Scheduler.__new__(scheduler.Scheduler)
    w.config_path = 'x'
    for n in ('futures', 'dynamic_futures', 'global_futures', 'pending_completions'):
        setattr(w, n, {})
    for n in ('pool', 'dynamic_pool', 'global_pool', 'sync_pool', 'page_display_pool',
              'strategy_export_pool', 'supervision_pool', 'reports_pool', 'experiment_pool'):
        setattr(w, n, Mock())
    w.storage_pool = ThreadPoolExecutor(max_workers=1)
    for n in ('sync_future', 'page_display_future', 'strategy_export_future',
              'supervision_future', 'reports_future', 'experiment_future', 'storage_future'):
        setattr(w, n, None)
    for n in ('experiment_cancel', 'supervision_cancel', 'storage_cancel', 'stop'):
        setattr(w, n, threading.Event())
    for n in ('last_settle', 'last_followups', 'last_maintenance', 'last_dividends',
              'last_sync', 'last_page_display', 'last_strategy_export',
              'last_supervision_scan', 'last_reports', 'last_withdrawal_check',
              'last_experiment_scan', 'last_reduce_check', 'last_storage'):
        setattr(w, n, 0)
    w.experiment_scan_ok = False; w.poll_seconds = 20
    w.monitor = SimpleNamespace(tick=Mock(), close=Mock())
    return w


class StorageWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cfg = cfg_for(self.tmp.name)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def test_run_no_longer_compresses_or_inventories(self):
        with patch.object(maintenance, 'compress_daily_backups', side_effect=AssertionError('called')), \
             patch.object(maintenance, 'storage_inventory', side_effect=AssertionError('called')):
            result = maintenance.run(self.store, self.cfg, AT)
        self.assertNotIn('backups_compressed', result)
        self.assertNotIn('inventory_refreshed', result)
        self.assertIn('disk', result)

    def test_one_backup_day_per_call_oldest_first(self):
        hourly(self.store.root, ['20261105T010000Z.sqlite3', '20261106T010000Z.sqlite3',
                               '20261107T010000Z.sqlite3'])
        compressed = []
        for _ in range(3):
            compressed += maintenance.storage_worker(self.cfg, AT)['compressed']
        self.assertEqual(compressed, ['20261105', '20261106', '20261107'])
        daily = self.store.root / 'backups' / 'daily'
        for day in compressed:
            src = sorted((self.store.root / 'backups' / 'hourly').glob(day + '*.sqlite3'))[-1]
            self.assertTrue(maintenance.verified_daily_backup(daily / (day + '.sqlite3.gz'), src))
        self.assertEqual(maintenance.storage_worker(self.cfg, AT)['compressed'], [])

    def test_cancel_mid_compression_leaves_no_partial_output(self):
        src_bytes = b'x' * (3 << 20)
        hourly(self.store.root, ['20261105T010000Z.sqlite3'], content=src_bytes)
        src = sorted((self.store.root / 'backups' / 'hourly').glob('*.sqlite3'))[-1]
        before_sha = hashlib.sha256(src.read_bytes()).hexdigest()
        cancel = threading.Event()
        real_open = Path.open

        class Wrapped:
            def __init__(self, f): self.f = f
            def read(self, *a):
                data = self.f.read(*a)
                if data: cancel.set()
                return data
            def __enter__(self): return self
            def __exit__(self, *a): self.f.close()
            def __getattr__(self, k): return getattr(self.f, k)

        def fake_open(self, *a, **kw):
            f = real_open(self, *a, **kw)
            mode = a[0] if a else kw.get('mode', 'r')
            if self.name.endswith('.sqlite3') and 'b' in mode:
                return Wrapped(f)
            return f
        with patch.object(Path, 'open', fake_open):
            result = maintenance.storage_worker(self.cfg, AT, cancel=cancel)
        self.assertTrue(result['cancelled'])
        daily = self.store.root / 'backups' / 'daily'
        self.assertEqual(list(daily.glob('*.gz')), [])
        self.assertEqual(list(daily.glob('*.tmp')), [])
        self.assertEqual(src.read_bytes(), src_bytes)
        self.assertEqual(hashlib.sha256(src.read_bytes()).hexdigest(), before_sha)
        follow = maintenance.storage_worker(self.cfg, AT, cancel=threading.Event())
        self.assertEqual(follow['compressed'], ['20261105'])

    def test_inventory_tables_false_skips_dbstat(self):
        statements = []
        self.store.db.set_trace_callback(lambda s: statements.append(s))
        inv = maintenance.storage_inventory(self.store, self.cfg, AT, tables=False)
        self.store.db.set_trace_callback(None)
        self.assertIn('page_count', inv['db'])
        self.assertNotIn('tables', inv['db'])
        self.assertFalse(any('dbstat' in s for s in statements))

    def test_tick_never_waits_for_storage_worker(self):
        w = scheduler_mock()
        block = threading.Event(); submitted = []

        def blocking_worker(config, at, cancel):
            submitted.append(at); block.wait(10)
            raise RuntimeError('worker blew up')
        with patch.object(scheduler, 'load_config', return_value=self.cfg), \
             patch.object(scheduler, 'now', return_value=AT), \
             patch.object(scheduler.time, 'monotonic', return_value=4000), \
             patch.object(scheduler, 'schedule_due'), \
             patch.object(scheduler, 'schedule_dynamic_due'), \
             patch.object(scheduler, 'schedule_reconnected'), \
             patch.object(maintenance, 'storage_worker', side_effect=blocking_worker):
            t0 = time.perf_counter(); w.tick(); t1 = time.perf_counter()
            self.assertLess(t1 - t0, 2)
            w.tick(); t2 = time.perf_counter()
            self.assertLess(t2 - t1, 2)
            self.assertEqual(len(submitted), 1)
            self.assertIsNotNone(self.store.db.execute(
                "SELECT value FROM service_state WHERE key='heartbeat'").fetchone())
            block.set()
            deadline = time.perf_counter() + 10
            while not w.storage_future.done() and time.perf_counter() < deadline:
                time.sleep(0.05)
            w.last_storage = 0
            w.tick()
        self.assertIsNotNone(self.store.db.execute(
            "SELECT value FROM service_state WHERE key='storage_worker_error'").fetchone())
        w.close()

    def test_close_signals_storage_cancel(self):
        w = scheduler_mock()
        w.close()
        self.assertTrue(w.storage_cancel.is_set())

    def test_max_days_caps_attempts_not_successes(self):
        hourly(self.store.root, ['20261105T010000Z.sqlite3', '20261106T010000Z.sqlite3'])
        # The first attempt fails once: that call must not move on to day two.
        bombed = {'done': False}
        real_write = __import__('gzip').GzipFile.write
        def flaky_write(self, data):
            if not bombed['done']:
                bombed['done'] = True
                raise IOError('boom')
            return real_write(self, data)
        with patch.object(__import__('gzip').GzipFile, 'write', flaky_write):
            r = maintenance.compress_daily_backups(self.store.root, 7, AT, max_days=1)
        self.assertEqual(r['compressed'], [])
        self.assertEqual(len(r['errors']), 1)
        self.assertEqual(r['errors'][0]['day'], '20261105')
        daily = self.store.root / 'backups' / 'daily'
        self.assertEqual(list(daily.glob('*20261106*')), [])
        r2 = maintenance.compress_daily_backups(self.store.root, 7, AT, max_days=1)
        self.assertEqual(r2['compressed'], ['20261105'])
        self.assertEqual(list(daily.glob('*20261106*')), [])
        r3 = maintenance.compress_daily_backups(self.store.root, 7, AT, max_days=1)
        self.assertEqual(r3['compressed'], ['20261106'])

    def test_inventory_cancel_returns_none_and_worker_reports_cancelled(self):
        cancel = threading.Event()
        row_before = self.store.db.execute(
            "SELECT value FROM service_state WHERE key='storage_inventory'").fetchone()
        seen = []

        real_walk = maintenance.os.walk
        hourly(self.store.root, ['20261105T010000Z.sqlite3'])

        def fake_walk(path, **kw):
            out = real_walk(path, **kw)
            first = next(out, None)
            cancel.set()
            if first is not None:
                yield first
            yield from out
        with patch.object(maintenance.os, 'walk', fake_walk):
            inv = maintenance.storage_inventory(self.store, self.cfg, AT, cancel=cancel)
        self.assertIsNone(inv)
        self.assertEqual(self.store.db.execute(
            "SELECT value FROM service_state WHERE key='storage_inventory'").fetchone(), row_before)
        cancel2 = threading.Event()

        def fake_walk2(path, **kw):
            out = real_walk(path, **kw)
            first = next(out, None)
            cancel2.set()
            if first is not None:
                yield first
            yield from out
        with patch.object(maintenance.os, 'walk', fake_walk2):
            result = maintenance.storage_worker(self.cfg, AT, cancel=cancel2)
        self.assertTrue(result['cancelled'])
        self.assertFalse(result['inventory_refreshed'])

    def test_close_cancels_storage_first_and_shuts_down_last(self):
        w = scheduler_mock()
        order = []
        for name in ('experiment_pool', 'supervision_pool', 'pool', 'dynamic_pool',
                     'global_pool', 'sync_pool', 'reports_pool', 'page_display_pool',
                     'strategy_export_pool', 'storage_pool'):
            pool = getattr(w, name)
            pool.shutdown = Mock(
                side_effect=lambda n=name, **kw: order.append((n, w.storage_cancel.is_set())))
        w.close()
        names = [n for n, _ in order]
        self.assertTrue(order[names.index('experiment_pool')][1])
        self.assertEqual(names[-1], 'storage_pool')
        self.assertLess(names.index('page_display_pool'), names.index('storage_pool'))
        self.assertLess(names.index('strategy_export_pool'), names.index('storage_pool'))
