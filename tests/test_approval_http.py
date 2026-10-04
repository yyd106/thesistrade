"""Authenticated approval transport with isolated stores and in-memory HTTP sockets."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import test_dashboard as fixtures
from ashare import approvals, auth, notices
from ashare.storage import Store, now


class ApprovalHttpTests(unittest.TestCase):
    setUp = fixtures.DashboardTests.setUp
    tearDown = fixtures.DashboardTests.tearDown
    request = fixtures.DashboardTests.request

    def prepared(self, store=None, at=None):
        own = store is None
        store = store or Store(self.data)
        try:
            return approvals.create_request(
                store, kind='PROPOSAL_DECISION', subject_id='proposal-fixture' + ('-expired' if at else ''), action='ADOPTED',
                snapshot={'proposal': {'id': 'proposal-fixture', 'status': 'APPROVED',
                              'guidance': {'route': 'watchlist', 'scope': 'ALL',
                                           'text': '仅在原始披露事实变化时调整研究结论'}},
                 'replaces': []}, summary={'title': '采纳指定研究规则；本次没有替代旧规则。'}, at=at)
        finally:
            if own:
                store.close()

    def answer(self, request, action='APPROVE', headers=None, **extra):
        return self.request('POST', '/api/notices/decide',
                            {'id': request['notice_id'], 'action': action,
                             'expected_hash': request['hash'], **extra},
                            {'X-CSRF-Token': self.csrf, **(headers or {})})

    def receipt(self, request):
        store = Store(self.data)
        try:
            return approvals.receipt_state(store, request['id'])
        finally:
            store.close()

    def test_admin_confirmation_returns_one_bound_receipt_without_adopting(self):
        request = self.prepared()
        self.assertEqual(self.answer(request)[0], 200)
        first = self.receipt(request)
        self.assertEqual((first['request_id'], first['request_hash'], first['actor'], first['authority']),
                         (request['id'], request['hash'], 'admin', 'LOCAL_ADMIN'))
        self.assertEqual(self.answer(request)[0], 200)
        self.assertEqual(self.receipt(request), first)
        self.assertEqual(self.answer(request, 'REJECT')[0], 400)
        store = Store(self.data)
        try:
            self.assertEqual(store.db.execute('SELECT count(*) FROM strategy_guidance').fetchone()[0], 0)
        finally:
            store.close()

    def test_stale_missing_hash_invalid_action_and_auth_boundaries_do_not_approve(self):
        request = self.prepared()
        self.assertEqual(self.answer(request, expected_hash=None)[0], 400)
        self.assertEqual(self.answer(request, expected_hash='f' * 64)[0], 400)
        self.assertEqual(self.answer(request, action='ACK')[0], 400)
        for headers, code in [({'Cookie': ''}, 401), ({'Origin': 'https://foreign.example'}, 403),
                              ({'X-CSRF-Token': 'stale'}, 403)]:
            self.assertEqual(self.answer(request, headers=headers)[0], code)
        store = Store(self.data)
        try:
            token, guest = auth.login(store, 'guest', 'test-guest-password', 'approval-guest')
        finally:
            store.close()
        self.assertEqual(self.answer(request, headers={'Cookie': auth.COOKIE + '=' + token,
                         'X-CSRF-Token': guest['csrf_token']})[0], 403)
        self.assertIsNone(self.receipt(request))

    def test_plain_username_cannot_approve_and_expired_preview_cannot_be_signed(self):
        request = self.prepared()
        store = Store(self.data)
        try:
            for user in ('admin', {'username': 'admin', 'role': 'ADMIN'}):
                with self.assertRaises(ValueError):
                    notices.decide(store, request['notice_id'], 'APPROVE', user,
                                   expected_hash=request['hash'], authority='LOCAL_ADMIN')
            self.assertIsNone(approvals.receipt_state(store, request['id']))
        finally:
            store.close()
        expired = self.prepared(at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat())
        self.assertEqual(self.answer(expired)[0], 400)
        self.assertIsNone(self.receipt(expired))
        store = Store(self.data)
        try:
            self.assertNotIn(expired['notice_id'], [n['id'] for n in notices.open_for_display(store)])
            self.assertIsNotNone(notices.get(store, expired['notice_id']))
        finally:
            store.close()

    def test_notice_failure_rolls_back_the_receipt_too(self):
        request = self.prepared()
        store = Store(self.data)
        try:
            with store.db:
                store.db.execute("CREATE TRIGGER reject_notice BEFORE UPDATE ON notices BEGIN SELECT RAISE(ABORT,'injected notice failure'); END")
        finally:
            store.close()
        self.assertEqual(self.answer(request)[0], 400)
        self.assertIsNone(self.receipt(request))

    def test_nested_notice_failure_does_not_leave_an_approval_to_commit(self):
        request = self.prepared()
        store = Store(self.data)
        try:
            user = auth.session(store, self.cookie)
            with store.db:
                store.db.execute("CREATE TRIGGER reject_notice BEFORE UPDATE ON notices BEGIN SELECT RAISE(ABORT,'injected failure'); END")
            store.db.execute('BEGIN IMMEDIATE')
            with self.assertRaises(Exception):
                notices.decide(store, request['notice_id'], 'APPROVE', user,
                               expected_hash=request['hash'], authority='LOCAL_ADMIN')
            store.db.commit()
            self.assertIsNone(approvals.receipt_state(store, request['id']))
        finally:
            store.close()

    def test_old_cloud_never_receives_strategy_approval_requests(self):
        from ashare import cloud_runtime, cloud_sync
        request = self.prepared()
        store = Store(self.data)
        try:
            ordinary = notices.create(store, title='普通待办', body='这是一项只记录决定的普通通知。')
            with store.db:
                cloud_runtime.put(store, 'remote_features', ['notices'])
            def send(config, path, body):
                self.assertEqual([n['id'] for n in body['notices']], [ordinary['id']])
                return {'states': {ordinary['id']: {'status': 'OPEN', 'acked_at': None, 'decided_by': None}}}
            with patch('ashare.cloud_sync.request', side_effect=send):
                cloud_sync.deliver_notices(store, {'deployment_role': 'research'})
            self.assertIsNone(notices.get(store, request['notice_id'])['delivered_at'])
            with patch('ashare.cloud_sync.request') as send_again:
                self.assertIsNone(cloud_sync.deliver_notices(store, {'deployment_role': 'research'}))
                send_again.assert_not_called()
        finally:
            store.close()

    def test_current_approval_bypasses_expired_backlog_and_history_rotates_read_only(self):
        store = Store(self.data)
        at = '2030-01-03T00:00:00+00:00'
        old_at = '2030-01-01T00:00:00+00:00'
        try:
            old = []
            with approvals.atomic(store):
                for number in range(205):
                    request = approvals.create_request(store, kind='PROPOSAL_DECISION',
                        subject_id='old-' + str(number), action='APPROVED',
                        snapshot={'number': number}, summary={'title': '过期待办'}, at=old_at)
                    old.append(request['notice_id'])
                current = approvals.create_request(store, kind='PROPOSAL_DECISION',
                    subject_id='current', action='APPROVED', snapshot={}, summary={'title': '当前待审批'}, at=at)
                notices.mark_delivered(store, old + [current['notice_id']], at)
            before = store.db.total_changes
            with patch('ashare.notices.now', return_value=at):
                first = notices.unresolved(store)
            with patch('ashare.notices.now', return_value='2030-01-03T00:01:00+00:00'):
                second = notices.unresolved(store)
            self.assertEqual(first[0], current['notice_id'])
            self.assertEqual(second[0], current['notice_id'])
            self.assertEqual(len(first), 200)
            self.assertEqual(len(second), 200)
            self.assertEqual(len(set(first)), 200)
            self.assertTrue(set(old).issubset(set(first) | set(second)))
            self.assertEqual(store.db.total_changes, before)
            self.assertFalse(store.db.in_transaction)
            self.assertEqual(store.db.execute('SELECT count(*) FROM notices').fetchone()[0], 206)
        finally:
            store.close()

    def test_strategy_settings_create_a_request_while_operational_settings_apply(self):
        before = self.path.read_bytes()
        code, raw, _ = self.request('POST', '/api/settings', {'dynamic_enabled': True},
                                    {'X-CSRF-Token': self.csrf})
        self.assertEqual(code, 202, raw)
        pending = json.loads(raw)
        self.assertEqual(pending['status'], 'APPROVAL_PENDING')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.answer(pending['approval_request'])[0], 200)
        self.assertEqual(self.path.read_bytes(), before, 'Approving never silently executes settings')
        code, raw, _ = self.request('POST', '/api/settings', {'scheduler_enabled': True},
                                    {'X-CSRF-Token': self.csrf})
        self.assertEqual(code, 200, raw)
        self.assertTrue(json.loads(self.path.read_text())['scheduler_enabled'])

    def test_display_contains_frozen_contract_and_never_only_free_text(self):
        request = self.prepared()
        store = Store(self.data)
        try:
            with store.db:
                store.db.execute('UPDATE notices SET body=? WHERE id=?', ('不可信摘要：这是另一个操作。', request['notice_id']))
            shown = notices.open_for_display(store)[0]
            self.assertEqual(shown['approval_request']['hash'], request['hash'])
            self.assertIn('原始披露事实变化', json.dumps(shown['approval_request'], ensure_ascii=False))
            self.assertNotIn('不可信摘要', shown['body'])
        finally:
            store.close()

    def test_cloud_roundtrip_requires_receipt_and_refuses_inbound_authority(self):
        request = self.prepared()
        local = Store(self.data)
        with tempfile.TemporaryDirectory() as cloud_dir:
            cloud = Store(cloud_dir)
            try:
                outgoing = notices.outgoing(notices.get(local, request['notice_id']))
                with cloud.db:
                    notices.receive(cloud, {'notices': [outgoing]}, now())
                for extra in ({'decided_by': 'admin'}, {'approval_receipt': {'actor': 'admin'}}, {'authority': 'CLOUD_ADMIN'}):
                    forged = {**outgoing, 'payload_json': json.dumps({**json.loads(outgoing['payload_json']), **extra})}
                    with self.assertRaises(ValueError), cloud.db:
                        notices.receive(cloud, {'notices': [forged]}, now())
                changed = copy.deepcopy(request)
                changed['snapshot']['replaces'] = ['another-rule']
                forged = {**outgoing, 'payload_json': json.dumps({'approval_request': changed})}
                with self.assertRaises(ValueError), cloud.db:
                    notices.receive(cloud, {'notices': [forged]}, now())
                with local.db:
                    notices.apply_states(local, {request['notice_id']: {'status': 'APPROVED', 'acked_at': now(), 'decided_by': 'admin'}})
                self.assertEqual(notices.get(local, request['notice_id'])['status'], 'OPEN')
                auth.setup(cloud, 'cloud-admin-password', 'cloud-guest-password')
                token, _ = auth.login(cloud, 'admin', 'cloud-admin-password', 'cloud-admin')
                user = auth.session(cloud, auth.COOKIE + '=' + token)
                notices.decide(cloud, request['notice_id'], 'APPROVE', user,
                               expected_hash=request['hash'], authority='CLOUD_ADMIN')
                state = notices.for_replica(cloud, {'known': [request['notice_id']]})['states']
                self.assertEqual(state[request['notice_id']]['approval_receipt']['authority'], 'CLOUD_ADMIN')
                with local.db:
                    notices.apply_states(local, state)
                self.assertEqual(notices.get(local, request['notice_id'])['status'], 'APPROVED')
                self.assertEqual(approvals.receipt_state(local, request['id'])['authority'], 'CLOUD_ADMIN')
                notices.apply_states(local, {request['notice_id']: {'status': 'OPEN', 'acked_at': None, 'decided_by': None}})
                self.assertEqual(notices.get(local, request['notice_id'])['status'], 'APPROVED')
            finally:
                cloud.close()
                local.close()

    def test_ordinary_notice_cannot_import_an_approval_receipt(self):
        store = Store(self.data)
        try:
            ordinary = notices.create(store, title='普通通知', body='仅为通知，不绑定任何策略批准动作。')
            with self.assertRaises(ValueError), store.db:
                notices.apply_states(store, {ordinary['id']: {'status': 'APPROVED',
                                     'approval_receipt': {'actor': 'admin'}}})
            self.assertEqual(notices.get(store, ordinary['id'])['status'], 'OPEN')
        finally:
            store.close()
