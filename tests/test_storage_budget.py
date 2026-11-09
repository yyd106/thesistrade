"""Compressed daily backups, storage inventory, and the 30GB program budget."""
import fcntl
import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_config
from ashare import maintenance, research
from ashare.storage import Store, normalize_time

CFG_FILE = Path(__file__).resolve().parents[1] / 'config.json'
AT = '2026-11-07T10:00:00+00:00'  # Saturday UTC; anything dated before 20261107 is a completed day


def hourly(root, names, content=b'x' * 128):
    folder = Path(root) / 'backups' / 'hourly'
    folder.mkdir(parents=True, exist_ok=True)
    for n in names:
        (folder / n).write_bytes(content)
    return folder


class RetentionTests(unittest.TestCase):
    def test_legacy_mode_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = hourly(tmp, [f'202611{d:02d}T{h:02d}0000Z.sqlite3' for d in range(1, 9) for h in (0, 12)])
            removed = maintenance.backup_retention(folder, hourly_keep=3, daily_keep=3)
            kept = sorted(p.name for p in folder.glob('*.sqlite3'))
            self.assertEqual({k[:8] for k in kept}, {'20261106', '20261107', '20261108'})
            # last 3 hourlies (07T12,08T00,08T12) plus the day-06 representative
            self.assertEqual(len(kept), 4)
            self.assertEqual(len(removed), 16 - 4)

    def test_compressed_days_drop_their_representative(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = hourly(tmp, [f'202611{d:02d}T{h:02d}0000Z.sqlite3' for d in range(1, 9) for h in (0, 12)])
            compressed = {'20261106', '20261107'}
            maintenance.backup_retention(folder, hourly_keep=3, daily_keep=3, compressed_days=compressed)
            kept = sorted(p.name for p in folder.glob('*.sqlite3'))
            # 06's representative is gone; 07 survives only inside the hourly window, not as a day rep.
            self.assertEqual({k[:8] for k in kept}, {'20261107', '20261108'})
            self.assertEqual(len(kept), 3)
            self.assertNotIn('20261106', {k[:8] for k in kept})


class CompressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def put(self, at):
        return self.store.periodic_backup(at)

    def test_real_sqlite_round_trip(self):
        src = self.put('2026-11-05T10:00:00+00:00')
        self.assertTrue(src.exists())
        result = maintenance.compress_daily_backups(self.store.root, 7, AT)
        self.assertEqual(result['compressed'], ['20261105'])
        self.assertFalse(result['errors'])
        gz = self.store.root / 'backups' / 'daily' / '20261105.sqlite3.gz'
        side = json.loads((self.store.root / 'backups' / 'daily' / '20261105.json').read_text())
        body = gzip.decompress(gz.read_bytes())
        # The trailer must be on disk: the stored .gz fully decompresses by itself.
        with gzip.open(gz, 'rb') as gzfile:
            self.assertEqual(gzfile.read(), body)
        self.assertEqual(hashlib.sha256(body).hexdigest(), side['source_sha256'])
        self.assertEqual(side['source'], '20261105T100000Z.sqlite3')
        self.assertEqual(side['method'], 'gzip-6')
        self.assertEqual(side['source_bytes'], len(body))
        self.assertEqual(side['gz_bytes'], gz.stat().st_size)
        out = Path(self.tmp.name) / 'restore.sqlite3'
        out.write_bytes(body)
        self.assertEqual(sqlite3.connect(out).execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        # Idempotent: second run compresses nothing and keeps the file.
        again = maintenance.compress_daily_backups(self.store.root, 7, AT)
        self.assertEqual(again['compressed'], [])
        self.assertTrue(gz.exists())

    def test_never_compresses_the_current_day(self):
        self.put('2026-11-07T01:00:00+00:00')
        result = maintenance.compress_daily_backups(self.store.root, 7, AT)
        self.assertEqual(result['compressed'], [])
        self.assertEqual(list((self.store.root / 'backups' / 'daily').glob('*.sqlite3.gz')), [])

    def test_daily_keep_prunes_oldest_gz(self):
        (self.store.root / 'backups' / 'hourly').mkdir(parents=True)
        daily = self.store.root / 'backups' / 'daily'
        daily.mkdir(parents=True)
        for d in range(1, 10):
            body = f'backup-{d}'.encode()
            gz = daily / f'202611{d:02d}.sqlite3.gz'
            gz.write_bytes(gzip.compress(body))
            (daily / f'202611{d:02d}.json').write_text(json.dumps({
                'source': f'202611{d:02d}T100000Z.sqlite3', 'source_bytes': len(body),
                'source_sha256': hashlib.sha256(body).hexdigest(), 'gz_bytes': gz.stat().st_size,
                'method': 'gzip-6'}))
        result = maintenance.compress_daily_backups(self.store.root, 7, AT)
        self.assertEqual(sorted(p.name for p in daily.glob('*.sqlite3.gz')),
                         [f'202611{d:02d}.sqlite3.gz' for d in range(3, 10)])
        self.assertEqual(result['removed'], ['20261101.sqlite3.gz', '20261102.sqlite3.gz'])
        self.assertFalse((daily / '20261101.json').exists())

    def test_failure_leaves_no_final_file_and_continues(self):
        hourly(self.store.root, ['20261104T100000Z.sqlite3', '20261105T100000Z.sqlite3'])
        real_open = gzip.GzipFile
        calls = []
        def flaky(*a, **kw):
            calls.append(a)
            if len(calls) == 1:  # first day in sorted order is 20261104
                raise OSError('injected')
            return real_open(*a, **kw)
        with patch('gzip.GzipFile', flaky):
            result = maintenance.compress_daily_backups(self.store.root, 7, AT)
        daily = self.store.root / 'backups' / 'daily'
        self.assertEqual(result['errors'], [{'day': '20261104', 'error': 'injected'}])
        self.assertEqual(result['compressed'], ['20261105'])
        self.assertFalse((daily / '20261104.sqlite3.gz').exists())
        self.assertEqual(list(daily.glob('*.tmp')), [])

    def test_busy_lock_skips_the_run(self):
        folder = hourly(self.store.root, ['20261105T100000Z.sqlite3'])
        with (folder / '.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = maintenance.compress_daily_backups(self.store.root, 7, AT)
        self.assertTrue(result['skipped_locked'])
        self.assertEqual(result['compressed'], [])

    def test_compressed_retention_via_periodic_backup(self):
        # Day 1 compressed already; its hourly representative is removed on the next backup.
        self.put('2026-11-05T10:00:00+00:00')
        self.put('2026-11-06T10:00:00+00:00')
        maintenance.compress_daily_backups(self.store.root, 7, '2026-11-07T00:00:00+00:00')
        self.put('2026-11-07T01:00:00+00:00')
        self.store.periodic_backup('2026-11-08T01:00:00+00:00', hourly_keep=1, daily_keep=7, compressed=True)
        names = {p.name[:8] for p in (self.store.root / 'backups' / 'hourly').glob('*.sqlite3')}
        self.assertNotIn('20261105', names)
        # Uncompressed mode keeps the day representative regardless of the gz copy.
        self.store.periodic_backup('2026-11-08T02:00:00+00:00', hourly_keep=1, daily_keep=7, compressed=False)
        names = {p.name[:8] for p in (self.store.root / 'backups' / 'hourly').glob('*.sqlite3')}
        self.assertIn('20261108', names)


class BackupReplacementSafetyTests(unittest.TestCase):
    setUp = CompressTests.setUp
    tearDown = CompressTests.tearDown
    put = CompressTests.put

    def test_corrupt_gzip_keeps_raw_until_repaired(self):
        source = self.put('2026-11-05T10:00:00+00:00')
        expected = source.read_bytes()
        daily = self.store.root / 'backups' / 'daily'
        daily.mkdir(parents=True)
        gz = daily / '20261105.sqlite3.gz'
        gz.write_bytes(b'bad')
        self.store.periodic_backup('2026-11-07T01:00:00+00:00', hourly_keep=1, compressed=True)
        self.assertTrue(source.exists())
        result = maintenance.compress_daily_backups(self.store.root, 7, AT)
        self.assertFalse(result['errors'])
        self.assertEqual(gzip.decompress(gz.read_bytes()), expected)
        self.store.periodic_backup('2026-11-08T01:00:00+00:00', hourly_keep=1, compressed=True)
        self.assertFalse(source.exists())

    def test_missing_manifest_after_failure_keeps_raw_and_repairs_on_retry(self):
        source = self.put('2026-11-05T10:00:00+00:00')
        original = Path.write_text
        def fail_sidecar(path, *args, **kwargs):
            if path.name == '20261105.json.tmp':
                raise OSError('injected sidecar failure')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'write_text', fail_sidecar):
            first = maintenance.compress_daily_backups(self.store.root, 7, AT)
        self.assertTrue(first['errors'])
        self.store.periodic_backup('2026-11-07T01:00:00+00:00', hourly_keep=1, compressed=True)
        self.assertTrue(source.exists())
        retry = maintenance.compress_daily_backups(self.store.root, 7, AT)
        self.assertFalse(retry['errors'])
        self.assertIn('20261105', retry['compressed'])
        target = self.store.root / 'backups/daily/20261105.sqlite3.gz'
        self.assertTrue(maintenance.verified_daily_backup(target, source))

    def test_earlier_same_day_backup_does_not_replace_latest_representative(self):
        self.put('2026-11-05T10:00:00+00:00')
        maintenance.compress_daily_backups(self.store.root, 7, AT)
        newest = self.put('2026-11-05T11:00:00+00:00')
        self.store.periodic_backup('2026-11-07T01:00:00+00:00', hourly_keep=1, compressed=True)
        self.assertTrue(newest.exists())
        maintenance.compress_daily_backups(self.store.root, 7, AT)
        target = self.store.root / 'backups/daily/20261105.sqlite3.gz'
        self.assertTrue(maintenance.verified_daily_backup(target, newest))

    def test_manifest_or_stream_corruption_cannot_release_source(self):
        source = self.put('2026-11-05T10:00:00+00:00')
        maintenance.compress_daily_backups(self.store.root, 7, AT)
        target = self.store.root / 'backups/daily/20261105.sqlite3.gz'
        side = target.parent / '20261105.json'
        valid_body, valid_meta = target.read_bytes(), side.read_text()
        mutations = [None, [], {'source_bytes': 1}, {'source_sha256': '0' * 64},
                     {'source': '20261104T100000Z.sqlite3'}]
        for value in mutations:
            with self.subTest(manifest=value):
                obj = json.loads(valid_meta)
                obj = {**obj, **value} if isinstance(value, dict) else value
                side.write_text(json.dumps(obj))
                self.assertFalse(maintenance.verified_daily_backup(target, source))
        side.write_text(valid_meta)
        target.write_bytes(valid_body[:-8])
        self.assertFalse(maintenance.verified_daily_backup(target, source))
        target.write_bytes(valid_body)
        source.write_bytes(b'!' + source.read_bytes()[1:])
        self.assertFalse(maintenance.verified_daily_backup(target, source))

    def test_invalid_newer_archives_do_not_evict_valid_old_day(self):
        self.put('2026-11-01T10:00:00+00:00')
        maintenance.compress_daily_backups(self.store.root, 1, AT)
        daily = self.store.root / 'backups/daily'
        for day in ('20261102', '20261103'):
            (daily / (day + '.sqlite3.gz')).write_bytes(b'bad')
        result = maintenance.compress_daily_backups(self.store.root, 1, AT)
        self.assertEqual(len(result['errors']), 2)
        self.assertTrue(maintenance.verified_daily_backup(daily / '20261101.sqlite3.gz'))


class MaintenanceRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cfg = test_config.load_config(CFG_FILE)
        self.cfg.update(data_dir=self.tmp.name, scheduler_enabled=False, model_enabled=False)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_non_cloud_run_compresses_and_caches_inventory(self):
        hourly(self.store.root, ['20261105T100000Z.sqlite3'])
        result = maintenance.run(self.store, self.cfg, AT)
        self.assertEqual(result['backups_compressed']['compressed'], ['20261105'])
        self.assertTrue(result['inventory_refreshed'])
        stored = json.loads(self.store.db.execute(
            "SELECT value FROM service_state WHERE key='storage_inventory'").fetchone()[0])
        self.assertEqual(stored['generated_at'], normalize_time(AT))
        self.assertIn('agent.sqlite3', stored['top'])
        self.assertIn('backups', stored)
        # A census generated moments ago is reused, not recomputed.
        from ashare.storage import now
        stored['generated_at'] = now()
        self.store.db.execute("UPDATE service_state SET value=? WHERE key='storage_inventory'",
                              (json.dumps(stored),))
        result = maintenance.run(self.store, self.cfg, AT)
        self.assertFalse(result['inventory_refreshed'])
        # Older than 20h: refreshed again.
        stored['generated_at'] = '2026-11-05T00:00:00+00:00'
        self.store.db.execute("UPDATE service_state SET value=? WHERE key='storage_inventory'",
                              (json.dumps(stored),))
        result = maintenance.run(self.store, self.cfg, AT)
        self.assertTrue(result['inventory_refreshed'])

    def test_cloud_run_keeps_legacy_retention(self):
        cfg = {**self.cfg, 'deployment_role': 'cloud'}
        result = maintenance.run(self.store, cfg, AT)
        self.assertNotIn('backups_compressed', result)
        self.assertNotIn('inventory_refreshed', result)
        self.assertIn('ledger_changes_pruned', result)

    def test_inventory_structure(self):
        (self.store.root / 'raw' / 'ab').mkdir(parents=True)
        (self.store.root / 'raw' / 'ab' / 'file.bin').write_bytes(b'x' * 64)
        inv = maintenance.storage_inventory(self.store, self.cfg, AT)
        self.assertGreater(inv['program_bytes'], 0)
        self.assertEqual(inv['raw']['files'], 1)
        self.assertEqual(inv['raw']['bytes'], 64)
        self.assertIn('page_size', inv['db'])
        self.assertTrue(inv['db'].get('tables') or inv['db'].get('dbstat_error'))
        self.assertIn('hourly', inv['backups'])
        self.assertIn('manual', inv['backups'])


class DiskStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cfg = test_config.load_config(CFG_FILE)
        self.cfg.update(data_dir=self.tmp.name, storage_budget_gb=30)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_counts_gz_and_breakdowns(self):
        (self.store.root / 'backups' / 'hourly').mkdir(parents=True)
        (self.store.root / 'backups' / 'hourly' / '20261105T100000Z.sqlite3').write_bytes(b'x' * 100)
        (self.store.root / 'backups' / 'daily').mkdir(parents=True)
        (self.store.root / 'backups' / 'daily' / '20261105.sqlite3.gz').write_bytes(b'x' * 50)
        (self.store.root / 'backups' / 'manual.sqlite3').write_bytes(b'x' * 25)
        d = maintenance.disk_status(self.store, self.cfg)
        self.assertAlmostEqual(d['backups_gb'], 175 / 1e9, places=6)
        self.assertAlmostEqual(d['backups_hourly_gb'], 100 / 1e9, places=6)
        self.assertAlmostEqual(d['backups_daily_gb'], 50 / 1e9, places=6)
        self.assertAlmostEqual(d['backups_manual_gb'], 25 / 1e9, places=6)
        self.assertIn('program_gb', d)
        self.assertEqual(d['budget_gb'], 30)

    def test_budget_levels_and_warning(self):
        for pct, level, warning in ((69.9, 'OK', False), (70.0, 'NOTICE', False), (90.0, 'WARNING', True)):
            inv = {'generated_at': __import__('ashare.storage', fromlist=['now']).now(),
                   'program_bytes': int(30e9 * pct / 100)}
            self.store.db.execute("INSERT OR REPLACE INTO service_state VALUES('storage_inventory',?)",
                                  (json.dumps(inv),))
            d = maintenance.disk_status(self.store, self.cfg)
            self.assertEqual(d['budget_level'], level, pct)
            self.assertEqual(d['warning'], warning, pct)
            self.assertEqual(d['budget_pct'], pct)


class SettingsAndSnapshotTests(unittest.TestCase):
    def test_storage_budget_gb_defaults_and_validates(self):
        from ashare.settings import validate_settings
        from ashare.config_ops import classify
        cfg = test_config.load_config(CFG_FILE)
        validate_settings(cfg)
        self.assertEqual(cfg['storage_budget_gb'], 30)
        for bad in (4, 1001, '30'):
            broken = {**cfg, 'storage_budget_gb': bad}
            with self.assertRaises(ValueError):
                validate_settings(broken)
        self.assertEqual(classify('storage_budget_gb'), 'OPERATIONAL')

    def test_persist_snapshot_keeps_db_only(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(tmp.name)
        self.addCleanup(store.close)
        cfg = test_config.load_config(CFG_FILE)
        cfg.update(data_dir=tmp.name, scheduler_enabled=False, model_enabled=False)
        from ashare import demo
        packet = demo.seed(store, cfg)  # seed persists via research.persist_snapshot
        row = store.db.execute('SELECT packet_json FROM snapshots WHERE id=?',
                               (packet['snapshot_id'],)).fetchone()
        self.assertEqual(json.loads(row[0]), packet)
        self.assertFalse((store.root / 'workflow' / 'snapshots'
                          / (packet['snapshot_id'] + '.json')).exists())

    def test_weekly_health_line_mentions_budget(self):
        from ashare import weekly
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(tmp.name)
        self.addCleanup(store.close)
        cfg = test_config.load_config(CFG_FILE)
        cfg.update(data_dir=tmp.name, scheduler_enabled=False, model_enabled=False)
        report = weekly.collect(store, cfg, '2026-11-07T02:00:00+00:00')
        text = weekly.markdown(report)
        self.assertIn('预算', text)
        self.assertIn('程序合计', text)


if __name__ == '__main__':
    unittest.main()
