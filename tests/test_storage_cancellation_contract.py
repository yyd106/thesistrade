"""Cancellation must cover every streaming verification, not only compression."""
import gzip
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from ashare import maintenance, scheduler
from ashare.storage import Store

AT = '2026-11-10T00:00:00+00:00'


class CancellationContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hourly = self.root / 'backups/hourly'
        self.hourly.mkdir(parents=True)
        self.daily = self.root / 'backups/daily'

    def backup(self, day='20261107', size=3 << 20):
        raw = self.hourly / (day + 'T010000Z.sqlite3')
        raw.write_bytes(b'x' * size)
        result = maintenance.compress_daily_backups(self.root, 7, AT)
        self.assertFalse(result['errors'])
        return raw, self.daily / (day + '.sqlite3.gz')

    def test_pre_cancelled_valid_copy_does_not_open_archive_or_prune(self):
        raw, target = self.backup()
        before = hashlib.sha256(raw.read_bytes()).hexdigest()
        cancel = threading.Event(); cancel.set()
        with patch.object(gzip, 'open', side_effect=AssertionError('read after stop')):
            result = maintenance.compress_daily_backups(self.root, 7, AT, max_days=1, cancel=cancel)
        self.assertTrue(result['cancelled'])
        self.assertFalse(result['compressed'] or result['removed'] or result['errors'])
        self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), before)
        self.assertTrue(maintenance.verified_daily_backup(target, raw))

    def test_cancel_while_verifying_gzip_or_source_stops_after_current_chunk(self):
        raw, target = self.backup()
        original_gzip = gzip.open
        original_path = Path.open
        for stage in ('gzip', 'source'):
            with self.subTest(stage=stage):
                cancel = threading.Event(); reads = []
                class Reader:
                    def __init__(self, stream): self.stream = stream
                    def read(self, *args):
                        if reads:
                            raise AssertionError('read continued after cancellation')
                        data = self.stream.read(*args)
                        reads.append(len(data)); cancel.set()
                        return data
                    def __enter__(self): return self
                    def __exit__(self, *args): self.stream.close()
                    def __getattr__(self, key): return getattr(self.stream, key)
                def path_open(path, *args, **kwargs):
                    stream = original_path(path, *args, **kwargs)
                    return Reader(stream) if path == raw and args and args[0] == 'rb' else stream
                use = (patch.object(gzip, 'open', lambda *a, **k: Reader(original_gzip(*a, **k)))
                       if stage == 'gzip' else patch.object(Path, 'open', path_open))
                with use:
                    result = maintenance.compress_daily_backups(self.root, 7, AT, max_days=1, cancel=cancel)
                self.assertTrue(result['cancelled'])
                self.assertEqual(reads, [1 << 20])
                self.assertEqual(result['errors'], [])
                self.assertTrue(maintenance.verified_daily_backup(target, raw))

    def test_cancel_during_retention_validation_preserves_all_archives(self):
        for day in ('20261105', '20261106', '20261107'):
            self.backup(day, 128)
        for raw in self.hourly.glob('*.sqlite3'):
            raw.unlink()
        before = {p.name: p.read_bytes() for p in self.daily.iterdir()}
        cancel = threading.Event(); original = gzip.open
        def cancel_on_open(*args, **kwargs):
            stream = original(*args, **kwargs); cancel.set(); return stream
        with patch.object(gzip, 'open', cancel_on_open):
            result = maintenance.compress_daily_backups(self.root, 1, AT, max_days=1, cancel=cancel)
        self.assertTrue(result['cancelled'])
        self.assertFalse(result['removed'] or result['errors'])
        self.assertEqual({p.name: p.read_bytes() for p in self.daily.iterdir()}, before)

    def test_cancel_during_new_archive_verification_keeps_source_and_retry(self):
        raw = self.hourly / '20261107T010000Z.sqlite3'
        raw.write_bytes(b'x' * (3 << 20)); before = hashlib.sha256(raw.read_bytes()).hexdigest()
        cancel = threading.Event(); original = gzip.open; calls = []
        class Reader:
            def __init__(self, stream): self.stream = stream
            def read(self, *args):
                if calls: raise AssertionError('new gzip read continued after stop')
                data = self.stream.read(*args); calls.append(len(data)); cancel.set(); return data
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def __getattr__(self, key): return getattr(self.stream, key)
        with patch.object(gzip, 'open', lambda *a, **k: Reader(original(*a, **k))):
            result = maintenance.compress_daily_backups(self.root, 7, AT, max_days=1, cancel=cancel)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['compressed'], [])
        self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(), before)
        self.assertEqual(list(self.daily.glob('*.tmp')), [])
        target = self.daily / '20261107.sqlite3.gz'
        # A complete, already-published archive stays; a later full verification is required.
        self.assertTrue(maintenance.verified_daily_backup(target, raw))
        retry = maintenance.compress_daily_backups(self.root, 7, AT, max_days=1)
        self.assertFalse(retry['cancelled'] or retry['errors'])

    def test_stop_after_manifest_publication_does_not_claim_verified_completion(self):
        raw = self.hourly / '20261107T010000Z.sqlite3'; raw.write_bytes(b'x' * 128)
        cancel = threading.Event(); original = maintenance.os.replace
        def publish_then_stop(src, dst):
            original(src, dst)
            if str(dst).endswith('20261107.json'): cancel.set()
        with patch.object(maintenance.os, 'replace', publish_then_stop):
            result = maintenance.compress_daily_backups(self.root, 7, AT, max_days=1, cancel=cancel)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['compressed'], [])
        self.assertEqual(raw.read_bytes(), b'x' * 128)
        self.assertEqual(list(self.daily.glob('*.tmp')), [])

    def test_sidecar_publish_interruption_cleans_temp_and_retries(self):
        raw = self.hourly / '20261107T010000Z.sqlite3'; raw.write_bytes(b'x' * 128)
        cancel = threading.Event(); original = Path.write_text
        def side_written(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path.name == '20261107.json.tmp': cancel.set()
            return result
        with patch.object(Path, 'write_text', side_written):
            result = maintenance.compress_daily_backups(self.root, 7, AT, max_days=1, cancel=cancel)
        self.assertTrue(result['cancelled'])
        self.assertEqual(list(self.daily.glob('*.tmp')), [])
        target = self.daily / '20261107.sqlite3.gz'
        self.assertFalse(maintenance.verified_daily_backup(target, raw))
        retry = maintenance.compress_daily_backups(self.root, 7, AT, max_days=1)
        self.assertEqual(retry['compressed'], ['20261107'])
        self.assertTrue(maintenance.verified_daily_backup(target, raw))

    def test_already_cancelled_inventory_does_not_scan_or_write(self):
        store = Store(str(self.root)); self.addCleanup(store.close)
        cancel = threading.Event(); cancel.set()
        with patch.object(maintenance.os, 'walk', side_effect=AssertionError('walk after stop')):
            result = maintenance.storage_inventory(store, {}, AT, tables=False, cancel=cancel)
        self.assertIsNone(result)
        self.assertIsNone(store.db.execute("SELECT value FROM service_state WHERE key='storage_inventory'").fetchone())

    def test_service_stop_reaches_storage_worker_before_close(self):
        worker_started = threading.Event()
        w = scheduler.Scheduler('unused-config-no-tick')
        self.addCleanup(w.close)
        def waiting_worker():
            worker_started.set()
            return w.storage_cancel.wait(2)
        future = w.storage_pool.submit(waiting_worker)
        self.assertTrue(worker_started.wait(2))
        # This is what the real SIGTERM handler does, before its 15s thread join.
        w.stop.set()
        self.assertTrue(future.result(timeout=3))
