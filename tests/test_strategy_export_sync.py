import base64
import io
import json
import stat
import tempfile
import threading
import unittest
import warnings
import zipfile
from datetime import datetime, timedelta
from unittest.mock import patch

from ashare import cloud_runtime as runtime, strategy_export_sync as sync
from ashare.storage import Store, digest, normalize_time

AT = '2026-10-22T02:00:00+00:00'


class StrategyExportSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.config = {'data_dir': self.tmp.name, 'deployment_role': 'research'}
        with self.store.db:
            runtime.put(self.store, 'remote_features', [sync.FEATURE])
            runtime.put(self.store, 'research_completed_at', AT)
            runtime.put(self.store, 'last_sync', {'status': 'OK', 'at': AT})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def packet(self, at=AT, *, metadata_change=None, extra_entry=None, duplicate=None,
               replace_entry=None, text='原始理由'):
        generated = datetime.fromisoformat(at)
        start = generated.astimezone(sync.ZONE).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=13)
        metadata = {k: [] for k in sync.DATA_FIELDS}
        metadata.update(schema=sync.SCHEMA, generated_at=at, end_at=at,
                        start_at=normalize_time(start.isoformat()), timezone='Asia/Shanghai', scope='test')
        if metadata_change:
            metadata.update(metadata_change)
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(sync.ENTRIES):
                content = json.dumps(metadata, ensure_ascii=False) if name == '策略数据.json' else text
                archive.writestr(replace_entry if replace_entry is not None and name == '总览.md' else name, content)
            if extra_entry:
                archive.writestr(extra_entry, text)
            if duplicate:
                archive.writestr(duplicate, text)
        raw = output.getvalue()
        filename = 'thesistrade-strategies-{}-{}.zip'.format(start.strftime('%Y%m%d'), generated.astimezone(sync.ZONE).strftime('%Y%m%d'))
        return {'base64_zip': base64.b64encode(raw).decode(), 'sha256': digest(raw), 'filename': filename,
                **{k: metadata[k] for k in ('generated_at', 'start_at', 'end_at')}}

    def accept(self, body, at=AT):
        self.store.db.execute('BEGIN IMMEDIATE')
        try:
            result = sync.receive(self.store, body, at)
            self.store.db.commit()
            return result
        except BaseException:
            self.store.db.rollback()
            raise

    def cache(self):
        row = self.store.db.execute('SELECT * FROM strategy_export_cache').fetchone()
        return tuple(row) if row else None

    def trade_state(self):
        tables = ('cloud_state', 'cloud_contracts', 'cloud_receipts', 'paper_accounts', 'paper_orders',
                  'dynamic_orders', 'global_orders', 'portfolio_decisions', 'portfolio_risk')
        return {t: [tuple(r) for r in self.store.db.execute('SELECT * FROM ' + t)] for t in tables}

    def test_receive_is_idempotent_and_does_not_touch_authority_or_trading(self):
        before = self.trade_state()
        body = self.packet()
        self.assertEqual(self.accept(body)['status'], 'ACCEPTED')
        first = self.cache()
        self.assertEqual(self.accept(body, '2026-10-22T03:00:00+00:00')['status'], 'UNCHANGED')
        self.assertEqual(first, self.cache())
        self.assertEqual(before, self.trade_state())

    def test_caller_transaction_can_rollback_archive(self):
        self.store.db.execute('BEGIN IMMEDIATE')
        sync.receive(self.store, self.packet(), AT)
        self.assertTrue(self.store.db.in_transaction)
        self.assertIsNotNone(self.cache())
        self.store.db.rollback()
        self.assertIsNone(self.cache())

    def test_stale_and_conflicting_packages_preserve_newest(self):
        self.accept(self.packet())
        before = self.cache()
        old = self.packet('2026-10-22T01:59:00+00:00')
        self.assertEqual(self.accept(old)['status'], 'IGNORED_STALE')
        with self.assertRaisesRegex(ValueError, '冲突'):
            self.accept(self.packet(text='另一个理由'))
        self.assertEqual(before, self.cache())
        newer = '2026-10-22T02:05:00+00:00'
        self.assertEqual(self.accept(self.packet(newer), newer)['status'], 'ACCEPTED')
        self.assertEqual(self.cache()[1], newer)

    def test_unknown_fields_bad_base64_digest_and_filename_rejected(self):
        good = self.packet()
        changes = ({'private': 'forbidden'}, {'base64_zip': '%%%%'}, {'sha256': '0' * 64},
                   {'filename': '../../archive.zip'}, {'filename': 'a\r\nInjected: yes.zip'})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.accept({**good, **change})
        self.assertIsNone(self.cache())

    def test_exact_calendar_window_and_no_future_times(self):
        for body in (self.packet('2026-10-22T02:00:01+00:00'),
                     self.packet(metadata_change={'start_at': '2026-10-09T00:00:00+00:00'}),
                     self.packet(metadata_change={'end_at': '2026-10-22T01:00:00+00:00'}),
                     self.packet(metadata_change={'generated_at': '2026-10-22T02:00:00'})):
            with self.subTest(body=body['generated_at']), self.assertRaises(ValueError):
                self.accept(body)
        self.assertIsNone(self.cache())

    def test_zip_only_four_known_plain_regular_utf8_files(self):
        symlink = zipfile.ZipInfo('总览.md')
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        bad = [self.packet(extra_entry='raw/source.pdf'),
               self.packet(replace_entry='../总览.md'), self.packet(replace_entry=symlink),
               self.packet(metadata_change={'raw_documents': ['forbidden']}),
               self.packet(metadata_change={'timezone': 'UTC'}),
               self.packet(metadata_change={'schema': 'other'})]
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            bad.append(self.packet(duplicate='总览.md'))
        for body in bad:
            with self.subTest(sha=body['sha256']), self.assertRaises(ValueError):
                self.accept(body)
        self.assertIsNone(self.cache())

    def test_outer_and_inner_metadata_must_match(self):
        body = self.packet()
        body.update(generated_at='2026-10-22T01:59:00+00:00', end_at='2026-10-22T01:59:00+00:00')
        with self.assertRaisesRegex(ValueError, '内部'):
            self.accept(body)

    def test_zip_cannot_hide_trailing_content(self):
        body = self.packet()
        raw = base64.b64decode(body['base64_zip']) + b'undeclared source content'
        body.update(base64_zip=base64.b64encode(raw).decode(), sha256=digest(raw))
        with self.assertRaises(ValueError):
            self.accept(body)

    def test_zip_and_expansion_limits_keep_cache(self):
        body = self.packet()
        self.accept(body)
        before = self.cache()
        with patch.object(sync, 'MAX_ZIP', 30), self.assertRaises(ValueError):
            self.accept(body)
        with patch.object(sync, 'MAX_UNPACKED', 40), self.assertRaises(ValueError):
            self.accept(body)
        self.assertEqual(before, self.cache())

    def test_read_only_cache_allows_same_day_but_rejects_cross_day(self):
        with self.assertRaisesRegex(ValueError, '未准备'):
            sync.read_cache(self.tmp.name, AT)
        body = self.packet()
        self.accept(body)
        before = self.trade_state(), self.cache()
        raw, filename = sync.read_cache(self.tmp.name, '2026-10-22T15:59:59+00:00')
        self.assertEqual(raw, base64.b64decode(body['base64_zip']))
        self.assertEqual(filename, body['filename'])
        with self.assertRaisesRegex(ValueError, '今日'):
            sync.read_cache(self.tmp.name, '2026-10-22T16:00:00+00:00')
        self.assertEqual(before, (self.trade_state(), self.cache()))

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            from pathlib import Path
            missing = Path(folder) / 'missing'
            with self.assertRaisesRegex(ValueError, '未准备'):
                sync.read_cache(missing, AT)
            self.assertFalse(missing.exists())

    def build_result(self):
        body = self.packet()
        return base64.b64decode(body['base64_zip']), body['filename']

    def ack(self, config, path, body):
        self.assertEqual(path, sync.ENDPOINT)
        self.assertEqual(set(body), sync.FIELDS)
        self.assertFalse(self.store.db.in_transaction)
        return {k: body[k] for k in ('generated_at', 'sha256')} | {'status': 'ACCEPTED'}

    def test_delivery_uses_signed_transport_and_exact_ack(self):
        with patch('ashare.strategy_export.build_archive', return_value=self.build_result()), \
             patch.object(sync, 'request', side_effect=self.ack) as request:
            result = sync.deliver(self.store, self.config, at=AT)
        self.assertEqual(result['status'], 'ACCEPTED')
        self.assertEqual(request.call_count, 1)
        self.assertEqual(runtime.value(self.store, sync.STATE)['status'], 'OK')
        self.assertEqual(runtime.value(self.store, 'last_sync'), {'status': 'OK', 'at': AT})
        self.assertEqual(runtime.value(self.store, 'research_completed_at'), AT)

    def test_missing_or_mismatched_receipt_is_failure(self):
        for answer in ({'status': 'ACCEPTED'}, {'status': 'IGNORED_STALE'}, {'status': 'ACCEPTED', 'generated_at': AT, 'sha256': '0' * 64}):
            with patch('ashare.strategy_export.build_archive', return_value=self.build_result()), \
                 patch.object(sync, 'request', return_value=answer), self.assertRaisesRegex(ValueError, '未确认'):
                sync.deliver(self.store, self.config, at=AT)
        self.assertIsNone(runtime.value(self.store, sync.STATE))

    def test_transport_limits_are_checked_before_network(self):
        for limit in ('MAX_BODY', 'MAX_JSON'):
            with patch('ashare.strategy_export.build_archive', return_value=self.build_result()), \
                 patch.object(sync, limit, 100), patch.object(sync, 'request') as request, \
                 self.assertRaisesRegex(ValueError, '传输限制'):
                sync.deliver(self.store, self.config, at=AT)
            request.assert_not_called()

    def test_worker_failure_is_isolated_and_redacts_exception_text(self):
        before = self.trade_state()
        with patch('ashare.strategy_export.build_archive', side_effect=ValueError('secret private path and raw excerpt')):
            result = sync.worker(self.config)
        self.assertEqual(result['status'], 'FAILED')
        self.assertNotIn('secret', json.dumps(result))
        state = runtime.value(self.store, sync.STATE)
        self.assertEqual(state['error_type'], 'ValueError')
        after = self.trade_state()
        after['cloud_state'] = [r for r in after['cloud_state'] if r[0] != sync.STATE]
        self.assertEqual(before, after)

    def test_cancelled_unsupported_and_wrong_role_never_build_or_send(self):
        stopped = threading.Event()
        stopped.set()
        with patch('ashare.strategy_export.build_archive') as build, patch.object(sync, 'request') as request:
            self.assertEqual(sync.deliver(self.store, self.config, stopped)['status'], 'CANCELLED')
            self.assertEqual(sync.worker({**self.config, 'deployment_role': 'cloud'})['status'], 'SKIPPED')
            with self.store.db:
                runtime.put(self.store, 'remote_features', [])
            self.assertEqual(sync.deliver(self.store, self.config)['status'], 'UNSUPPORTED')
            build.assert_not_called()
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
