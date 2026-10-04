"""Config approvals exercise real temporary files, SQLite and crash recovery, never production data."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from ashare import config_ops, config_journal, approvals, model
from ashare.storage import Store
from approval_fixture import grant

ROOT = Path(__file__).resolve().parents[1]


class ConfigApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'config.json'
        self.data = Path(self.tmp.name) / 'data'
        raw = json.loads((ROOT / 'config.json').read_text())
        raw.update(data_dir=str(self.data), deployment_role='standalone',
                   sync_key_file='DO-NOT-EXPORT-PRIVATE-KEY-PATH', uneditable_secret='DO-NOT-EXPORT-SECRET')
        self.path.write_text(json.dumps(raw, ensure_ascii=False))
        self.changes = {'paper_entry_band_bps': 100}
        self.reason = '经具体差异确认的测试变更'

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, changes=None, reason=None):
        return config_ops.request_change(self.path, changes or self.changes, reason=reason or self.reason)

    def authorize(self, request=None):
        request = request or self.request()
        store = Store(self.data)
        try:
            grant(store, request)
        finally:
            store.close()
        return request['id']

    def apply(self, approval_id, changes=None, reason=None):
        return config_ops.apply(self.path, changes or self.changes, reason=reason or self.reason, approval_id=approval_id)

    def rows(self):
        log = self.data / 'workflow/changes/config-changes.jsonl'
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def test_name_alone_has_no_authority(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, '审批收据'):
            config_ops.apply(self.path, self.changes, reason=self.reason, approved_by='Dean approved')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.rows(), [])

    def test_request_is_read_only_and_never_exports_private_configuration(self):
        before = self.path.read_bytes()
        pinned = model.pinned()
        request = self.request({'model_name': 'gpt-5.5'})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(model.pinned(), pinned)
        self.assertEqual(self.rows(), [])
        store = Store(self.data)
        try:
            rendered = json.dumps(request) + '\n'.join(store.db.iterdump())
        finally:
            store.close()
        self.assertNotIn('DO-NOT-EXPORT', rendered)
        self.assertNotIn('uneditable_secret', rendered)
        self.assertNotIn('sync_key_file', rendered)

    def test_exact_receipt_applies_once_and_logs_authenticated_actor(self):
        approval_id = self.authorize()
        result = self.apply(approval_id)
        self.assertEqual(json.loads(self.path.read_text())['paper_entry_band_bps'], 100)
        self.assertNotEqual(result['build_before'], result['build_after'])
        self.assertEqual(self.rows()[0]['approval_id'], approval_id)
        self.assertTrue(self.rows()[0]['approved_by'])
        with self.assertRaises(ValueError):
            self.apply(approval_id)
        self.assertEqual(len(self.rows()), 1)

    def test_changed_diff_reason_and_state_cannot_borrow_receipt(self):
        approval_id = self.authorize()
        before = self.path.read_bytes()
        for changes, reason in [({'paper_entry_band_bps': 120}, None), (None, '另一项理由')]:
            with self.assertRaises(ValueError):
                self.apply(approval_id, changes, reason)
        self.assertEqual(self.path.read_bytes(), before)
        config_ops.apply(self.path, {'research_reuse_hours': 12}, reason='测试运行类修改')
        with self.assertRaises(ValueError):
            self.apply(approval_id)
        self.assertNotEqual(json.loads(self.path.read_text())['paper_entry_band_bps'], 100)

    def test_receipt_does_not_authorize_another_config_path(self):
        approval_id = self.authorize()
        other = self.path.with_name('other.json')
        other.write_bytes(self.path.read_bytes())
        with self.assertRaises(ValueError):
            config_ops.apply(other, self.changes, reason=self.reason, approval_id=approval_id)
        self.assertEqual(other.read_bytes(), self.path.read_bytes())

    def test_normal_publish_failure_releases_reservation_without_consumption(self):
        approval_id = self.authorize()
        before = self.path.read_bytes()
        with patch.object(config_journal, '_publish', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.apply(approval_id)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.rows(), [])
        self.apply(approval_id)
        self.assertEqual(len(self.rows()), 1)

    def test_crash_after_file_replacement_recovers_same_intent(self):
        approval_id = self.authorize()
        with patch.object(config_journal, '_append_log', side_effect=OSError('interrupted audit append')):
            with self.assertRaises(OSError):
                self.apply(approval_id)
        self.assertEqual(json.loads(self.path.read_text())['paper_entry_band_bps'], 100)
        self.assertEqual(self.rows(), [])
        recovered = config_ops.recover(self.path)
        self.assertEqual(recovered[0]['approval_id'], approval_id)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(config_ops.recover(self.path), [])

    def test_crash_after_audit_append_never_duplicates_complete_log_rows(self):
        approval_id = self.authorize()
        with patch.object(approvals, 'consume', side_effect=RuntimeError('interrupted SQLite transaction')):
            with self.assertRaises(RuntimeError):
                self.apply(approval_id)
        first = self.rows()
        self.assertEqual(len(first), 1)
        config_ops.recover(self.path)
        self.assertEqual(self.rows(), first)

    def test_interruption_before_replace_keeps_authorized_recovery_possible(self):
        approval_id = self.authorize()
        before = self.path.read_bytes()
        # Simulate process loss, not a handled filesystem failure.
        with patch.object(config_journal, '_publish', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(approval_id)
        self.assertEqual(self.path.read_bytes(), before)
        config_ops.recover(self.path)
        self.assertEqual(json.loads(self.path.read_text())['paper_entry_band_bps'], 100)

    def test_recovery_never_overwrites_out_of_band_edits(self):
        approval_id = self.authorize()
        with patch.object(config_journal, '_append_log', side_effect=OSError('interrupted audit append')):
            with self.assertRaises(OSError):
                self.apply(approval_id)
        raw = json.loads(self.path.read_text())
        raw['research_reuse_hours'] = 12
        self.path.write_text(json.dumps(raw, ensure_ascii=False))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, '未覆盖'):
            config_ops.recover(self.path)
        with self.assertRaisesRegex(ValueError, '未覆盖'):
            config_ops.apply(self.path, {'research_reuse_hours': 10}, reason='并发运行类修改')
        self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_consumers_apply_once(self):
        approval_id = self.authorize()
        def worker(_):
            try:
                return self.apply(approval_id)
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_concurrent_operational_writers_do_not_lose_other_keys(self):
        changes = [{'research_reuse_hours': 12}, {'pdf_downloads_per_stock': 6}]
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda c: config_ops.apply(self.path, c, reason='并发运行类修改'), changes))
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw['research_reuse_hours'], 12)
        self.assertEqual(raw['pdf_downloads_per_stock'], 6)
        self.assertEqual(len(self.rows()), 2)

    def test_mixed_change_consumes_only_exact_full_difference(self):
        changes = {**self.changes, 'research_reuse_hours': 12}
        approval_id = self.authorize(self.request(changes))
        with self.assertRaises(ValueError):
            self.apply(approval_id)
        self.apply(approval_id, changes)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len({r['operation_id'] for r in self.rows()}), 1)

    def test_forbidden_keys_never_become_approval_requests(self):
        before = self.path.read_bytes()
        for key in ('sync_key_file', 'paper_max_stock_pct', 'unknown_key'):
            with self.assertRaisesRegex(ValueError, '不允许'):
                self.request({key: 'blocked'})
        self.assertEqual(self.path.read_bytes(), before)

    def test_expired_unreserved_receipt_cannot_start_a_change(self):
        request = self.request()
        approval_id = self.authorize(request)
        before = self.path.read_bytes()
        with patch.object(approvals, 'now', return_value=request['expires_at']):
            with self.assertRaisesRegex(ValueError, '过期'):
                self.apply(approval_id)
        self.assertEqual(self.path.read_bytes(), before)

    def test_reserved_operation_can_finish_after_approval_expiry(self):
        request = self.request()
        approval_id = self.authorize(request)
        with patch.object(config_journal, '_publish', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(approval_id)
        future = (datetime.fromisoformat(request['expires_at']) + timedelta(hours=1)).isoformat()
        with patch.object(approvals, 'now', return_value=future):
            result = config_ops.recover(self.path)
        self.assertEqual(result[0]['approval_id'], approval_id)
        self.assertEqual(len(self.rows()), 1)

    def test_resume_after_partial_multikey_log_appends_only_missing_key(self):
        changes = {**self.changes, 'research_reuse_hours': 12}
        approval_id = self.authorize(self.request(changes))
        append = config_journal._append_log
        def partial(log, payload, operation_id):
            append(log, {**payload, 'entries': payload['entries'][:1]}, operation_id)
            raise OSError('interrupted between entries')
        with patch.object(config_journal, '_append_log', side_effect=partial):
            with self.assertRaises(OSError):
                self.apply(approval_id, changes)
        first = self.rows()[0]
        config_ops.recover(self.path)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.rows()[0], first)
        self.assertEqual({r['key'] for r in self.rows()}, set(changes))

    def test_before_publish_recovery_cancels_stale_intent_and_releases_startup(self):
        approval_id = self.authorize()
        before = self.path.read_bytes()
        with patch.object(config_journal, '_publish', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(approval_id)
        with patch('ashare.build.code_fingerprint', return_value='different-code'):
            result = config_ops.recover(self.path)
            self.assertEqual(result[0]['status'], 'ABORTED')
            self.assertTrue(result[0]['requires_new_approval'])
            self.assertEqual(config_ops.recover(self.path), [])
            with self.assertRaises(ValueError):
                self.apply(approval_id)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.rows(), [])
        store = Store(self.data)
        try:
            row = store.db.execute('SELECT status,abort_reason FROM config_apply_journal').fetchone()
            self.assertEqual(tuple(row), ('ABORTED', 'STATE_CHANGED_BEFORE_PUBLICATION'))
            self.assertIsNone(store.db.execute('SELECT 1 FROM approval_consumptions WHERE request_id=?', (approval_id,)).fetchone())
            self.assertIsNone(store.db.execute('SELECT 1 FROM approval_reservations WHERE request_id=? AND released_at IS NULL', (approval_id,)).fetchone())
        finally:
            store.close()

    def test_publication_evidence_prevents_automatic_stale_intent_cancellation(self):
        approval_id = self.authorize()
        before = self.path.read_bytes()
        with patch.object(approvals, 'consume', side_effect=RuntimeError('interrupted completion')):
            with self.assertRaises(RuntimeError):
                self.apply(approval_id)
        # An out-of-band restoration to original bytes is not proof that the operation never ran.
        self.path.write_bytes(before)
        with patch('ashare.build.code_fingerprint', return_value='different-code'):
            with self.assertRaisesRegex(ValueError, '程序版本已变化'):
                config_ops.recover(self.path)
        store = Store(self.data)
        try:
            self.assertEqual(store.db.execute('SELECT status FROM config_apply_journal').fetchone()[0], 'APPLYING')
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
