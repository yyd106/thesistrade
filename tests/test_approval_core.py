import copy
import json
import tempfile
import unittest
from ashare import approvals, auth
from ashare.storage import Store
from approval_fixture import grant, admin_session

AT = '2026-10-05T00:00:00+00:00'
LATER = '2026-10-05T00:01:00+00:00'
EXPIRED = '2026-10-06T00:00:00+00:00'


class ApprovalCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.snapshot = {'before': 200, 'after': 100, 'version': 'synthetic'}
        self.request = approvals.create_request(self.store, kind='CONFIG', subject_id='synthetic-config',
                                                action='APPLY', snapshot=self.snapshot,
                                                summary={'title': '合成设置变更'}, at=AT)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def check(self, identity=None, **kw):
        return approvals.check_receipt(self.store, identity or self.request['id'],
            **{'kind': 'CONFIG', 'subject_id': 'synthetic-config', 'action': 'APPLY',
               'snapshot': self.snapshot, 'at': LATER, **kw})

    def test_request_is_not_permission_and_name_or_role_is_not_identity(self):
        for user in ('Dean', {'username': 'admin', 'role': 'ADMIN'}):
            with self.assertRaisesRegex(ValueError, '已登录管理员'):
                approvals.approve(self.store, self.request['id'], self.request['hash'], user, at=AT)
        with self.assertRaises(ValueError): self.check()
        self.assertEqual(approvals.request_info(self.store, self.request['id'], at=AT)['status'], 'PENDING')

    def test_guest_and_expired_sessions_cannot_issue_receipt(self):
        admin_session(self.store, AT)
        token, _ = auth.login(self.store, 'guest', 'synthetic-guest-password', 'test', at=AT)
        guest = auth.session(self.store, auth.COOKIE + '=' + token, at=AT)
        with self.assertRaises(ValueError):
            approvals.approve(self.store, self.request['id'], self.request['hash'], guest, at=AT)
        admin = admin_session(self.store, AT)
        with self.assertRaises(ValueError):
            approvals.approve(self.store, self.request['id'], self.request['hash'], admin, at=EXPIRED)

    def test_exact_action_version_and_subject_are_required(self):
        grant(self.store, self.request, AT)
        for changed in ({'action': 'RETIRED'}, {'subject_id': 'elsewhere'}, {'kind': 'PROPOSAL_DECISION'},
                        {'snapshot': {**self.snapshot, 'after': 101}}):
            with self.assertRaises(ValueError): self.check(**changed)
        self.assertEqual(self.check()['actor'], 'admin')

    def test_consume_is_one_use_with_result_retained(self):
        grant(self.store, self.request, AT)
        approvals.consume(self.store, self.request['id'], {'changed': True}, at=LATER)
        with self.assertRaisesRegex(ValueError, '已经使用'): self.check()
        with self.assertRaises(ValueError):
            approvals.consume(self.store, self.request['id'], {'changed': False}, at=LATER)
        saved = approvals.request_info(self.store, self.request['id'], at=LATER)
        self.assertEqual(saved['consumption']['result'], {'changed': True})

    def test_consumption_rolls_back_with_caller(self):
        grant(self.store, self.request, AT)
        with self.assertRaises(RuntimeError):
            with approvals.atomic(self.store):
                approvals.consume(self.store, self.request['id'], {'changed': True}, at=LATER)
                raise RuntimeError('synthetic failure after consume')
        self.assertEqual(self.check()['decision'], 'APPROVE')

    def test_dedupe_keeps_frozen_request_and_rejection_is_not_overwritten(self):
        same = approvals.create_request(self.store, kind='CONFIG', subject_id='synthetic-config', action='APPLY',
            snapshot=dict(reversed(list(self.snapshot.items()))), summary={'title': '合成设置变更'}, at=LATER)
        self.assertEqual(same['id'], self.request['id'])
        user = admin_session(self.store, AT)
        approvals.approve(self.store, self.request['id'], self.request['hash'], user, decision='REJECT', at=AT)
        with self.assertRaises(ValueError):
            approvals.approve(self.store, self.request['id'], self.request['hash'], user, at=AT)
        with self.assertRaises(ValueError): self.check()

    def test_changed_page_hash_expiry_and_backdated_execution_fail(self):
        user = admin_session(self.store, AT)
        with self.assertRaises(ValueError):
            approvals.approve(self.store, self.request['id'], '0' * 64, user, at=AT)
        grant(self.store, self.request, LATER)
        with self.assertRaises(ValueError): self.check(at=AT)
        with self.assertRaises(ValueError): self.check(at=EXPIRED)

    def test_reservation_prevents_borrowing_and_can_finish_after_expiry(self):
        grant(self.store, self.request, AT)
        approvals.reserve(self.store, self.request['id'], 'operation-a', at=LATER)
        with self.assertRaises(ValueError): self.check()
        with self.assertRaises(ValueError): self.check(operation_id='operation-b')
        self.check(operation_id='operation-a', at=EXPIRED)
        approvals.consume(self.store, self.request['id'], {'recovered': True}, operation_id='operation-a', at=EXPIRED)
        self.assertEqual(approvals.request_info(self.store, self.request['id'])['status'], 'CONSUMED')

    def test_release_keeps_history_and_restores_unexpired_permission(self):
        grant(self.store, self.request, AT)
        approvals.reserve(self.store, self.request['id'], 'operation-a', at=AT)
        with self.assertRaises(ValueError): approvals.release(self.store, self.request['id'], 'operation-b', at=LATER)
        approvals.release(self.store, self.request['id'], 'operation-a', at=LATER)
        self.check()
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM approval_reservations').fetchone()[0], 1)

    def test_request_import_rejects_tampering_and_authority_fields(self):
        value = approvals.envelope(self.store, self.request['id'])
        changed = copy.deepcopy(value); changed['snapshot']['after'] = 10000
        for invalid in (changed, {**value, 'receipt': {'actor': 'admin'}}):
            with self.assertRaises(ValueError): approvals.receive_request(self.store, invalid)
        with self.assertRaises(ValueError):
            approvals.mirror_receipt(self.store, {'actor': 'admin', 'decision': 'APPROVE'})

    def test_cloud_receipt_is_bound_to_local_request_and_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            cloud = Store(tmp)
            try:
                approvals.receive_request(cloud, approvals.envelope(self.store, self.request['id']))
                user = admin_session(cloud, AT)
                receipt = approvals.approve(cloud, self.request['id'], self.request['hash'], user, authority='CLOUD_ADMIN', at=AT)
                with self.assertRaises(ValueError): approvals.mirror_receipt(self.store, {**receipt, 'request_hash': '0' * 64})
                approvals.mirror_receipt(self.store, receipt)
                approvals.mirror_receipt(self.store, receipt)
                with self.assertRaises(ValueError): approvals.mirror_receipt(self.store, {**receipt, 'decision': 'REJECT'})
                self.check()
            finally: cloud.close()

    def test_envelope_and_receipt_do_not_contain_session_credentials(self):
        receipt = grant(self.store, self.request, AT)
        serialized = json.dumps(approvals.request_info(self.store, self.request['id']), ensure_ascii=False)
        for field in ('token_hash', 'csrf_token', 'password'):
            self.assertNotIn(field, serialized)
        self.assertEqual(set(receipt), approvals.RECEIPT_FIELDS)
