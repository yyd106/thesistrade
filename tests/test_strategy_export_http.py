"""Administrator-only archives use existing sessions, never a research job."""
import io
import json
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import test_dashboard as fixtures
from ashare import auth
from ashare.dashboard import make_handler
from ashare.storage import Store


class StrategyExportHttpTests(unittest.TestCase):
    setUp = fixtures.DashboardTests.setUp
    tearDown = fixtures.DashboardTests.tearDown
    request = fixtures.DashboardTests.request

    def test_admin_receives_zip_and_no_job_or_strategy_is_created(self):
        with self._store() as store:
            before = self._counts(store)
        code, raw, headers = self.request('GET', '/api/strategy-export')
        self.assertEqual(code, 200, raw[:300])
        self.assertIn(b'Content-Type: application/zip', headers)
        self.assertIn(b'Content-Disposition: attachment; filename="thesistrade-strategies-', headers)
        self.assertIn(b'Cache-Control: no-store', headers)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertIn('交易策略变更.md', archive.namelist())
            self.assertIn('研究策略.md', archive.namelist())
        with self._store() as store:
            self.assertEqual(before, self._counts(store))

    def _store(self):
        from contextlib import contextmanager
        @contextmanager
        def opened():
            store = Store(self.data)
            try:yield store
            finally:store.close()
        return opened()

    @staticmethod
    def _counts(store):
        return {table: store.db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                for table in ('jobs', 'studies', 'portfolio_decisions', 'paper_orders', 'cloud_receipts')}

    def test_guest_anonymous_and_cross_origin_cannot_generate_or_download(self):
        with self._store() as store:
            token, _ = auth.login(store, 'guest', 'test-guest-password', 'download-guest')
        checks = [({'Cookie': ''}, 401),
                  ({'Cookie': auth.COOKIE + '=' + token}, 403),
                  ({'Origin': 'https://foreign.example'}, 403),
                  ({'Sec-Fetch-Site': 'cross-site'}, 403)]
        with patch('ashare.strategy_export.build_archive', side_effect=AssertionError('not authorized')):
            for headers, expected in checks:
                code, raw, head = self.request('GET', '/api/strategy-export', headers=headers)
                self.assertEqual(code, expected)
                self.assertNotIn(b'Content-Disposition', head)
                self.assertIn('error', json.loads(raw))

    def test_range_and_path_parameters_cannot_widen_export(self):
        with patch('ashare.strategy_export.build_archive', side_effect=AssertionError('bad request')):
            for query in ('?days=365', '?path=../../config.json', '?start=2020-01-01'):
                self.assertEqual(self.request('GET', '/api/strategy-export' + query)[0], 400)

    def cloud_handler(self):
        cfg = json.loads(self.path.read_text())
        cfg['deployment_role'] = 'cloud'
        self.path.write_text(json.dumps(cfg))
        return make_handler(self.path, 'test-cloud-session', 8765)

    def test_cloud_only_serves_verified_cache_and_missing_cache_is_readable(self):
        handler = self.cloud_handler()
        with patch('ashare.strategy_export_sync.read_cache', return_value=(b'PK-verified', 'test.zip')) as read, \
             patch('ashare.strategy_export.build_archive', side_effect=AssertionError('cloud must not reconstruct incomplete history')):
            code, raw, headers = self.request('GET', '/api/strategy-export', handler=handler)
            self.assertEqual((code, raw), (200, b'PK-verified'))
            self.assertEqual(Path(read.call_args.args[0]).resolve(), Path(self.data).resolve())
            self.assertIn(b'filename="test.zip"', headers)
        with patch('ashare.strategy_export_sync.read_cache', side_effect=ValueError('研究端尚未准备今天的策略档案，请稍后重试。')):
            code, raw, headers = self.request('GET', '/api/strategy-export', handler=handler)
            self.assertEqual(code, 503)
            self.assertIn('尚未准备', json.loads(raw)['error'])
            self.assertNotIn(b'Content-Disposition', headers)


if __name__ == '__main__':
    unittest.main()
