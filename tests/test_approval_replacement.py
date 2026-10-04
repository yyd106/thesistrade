"""Isolated governance cases; every proposal and receipt here is synthetic."""
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ashare import approvals, governance
from ashare.storage import Store

T0 = '2026-10-01T00:00:00+00:00'
T1 = '2026-10-02T00:00:00+00:00'
T2 = '2026-10-03T00:00:00+00:00'
T3 = '2026-10-04T00:00:00+00:00'
NOTE = '合成测试：确认当前完整版本与所列替代关系'


class ApprovalReplacementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def draft(self, *, route='watchlist', scope='ALL', replaces=None, kind='RESEARCH_GUIDANCE'):
        payload = {'hypothesis': '合成假设', 'change': '明确核验判断', 'evidence': ['synthetic:no-file-read'],
                   'test_plan': '预注册前向对照', 'failure_criteria': '不足则不采用', 'rollback': '撤下规则',
                   'guidance': {'route': route, 'scope': scope, 'text': '仅使用当时可获得的公开事实进行研究。'}}
        if replaces is not None:
            payload['replaces'] = replaces
        with self.store.db:
            return governance.draft_proposal(self.store, source='synthetic', kind=kind, target=route,
                title='合成研究规则', payload=payload, at=T0)

    def row(self, pid):
        return self.store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (pid,)).fetchone()

    def rule(self, pid):
        return self.store.db.execute('SELECT * FROM strategy_guidance WHERE proposal_id=?', (pid,)).fetchone()

    def confirm(self, request, at=T1):
        # The shared helper exercises the real confirmation/receipt storage API.
        from approval_fixture import grant
        grant(self.store, request, at=at)
        return request['id']

    def request(self, pid, status, *, at=T1, note=NOTE, **kw):
        return governance.request_decision(self.store, pid, status, note, at=at, **kw)

    def decide(self, pid, status, *, at=T1, note=NOTE, **kw):
        if governance.needs_confirmation(self.row(pid), status):
            request = self.request(pid, status, at=at, note=note, **kw)
            approval_id = self.confirm(request, at=at)
        else:
            approval_id = None
        return governance.decide(self.store, pid, status, note=note, approval_id=approval_id, at=at, **kw)

    def adopt(self, pid, at=T1):
        for status in ('READY', 'APPROVED', 'ADOPTED'):
            self.decide(pid, status, at=at)
        return 'G-' + pid

    def snapshot(self):
        return {table: [tuple(r) for r in self.store.db.execute(f'SELECT * FROM {table} ORDER BY 1')]
                for table in ('strategy_proposals', 'strategy_guidance')}

    def mutate_payload(self, pid, mutate):
        payload = json.loads(self.row(pid)['payload_json'])
        mutate(payload)
        with self.store.db:
            self.store.db.execute('UPDATE strategy_proposals SET payload_json=? WHERE id=?', (json.dumps(payload), pid))

    def test_name_is_not_approval_and_request_has_no_rule_effect(self):
        pid = self.draft()
        self.decide(pid, 'READY')
        before = self.snapshot()
        for name in ('Dean', 'user-confirmed', ''):
            with self.assertRaisesRegex(ValueError, '确认回执'):
                governance.decide(self.store, pid, 'APPROVED', decided_by=name, note=NOTE, at=T1)
        request = self.request(pid, 'APPROVED')
        self.assertTrue(request['id'])
        self.assertEqual(before, self.snapshot())

    def test_unlisted_complementary_rules_remain_active(self):
        a, b = self.draft(), self.draft()
        self.adopt(a)
        self.adopt(b, T2)
        self.assertEqual(self.rule(a)['status'], 'ADOPTED')
        self.assertEqual(self.rule(b)['status'], 'ADOPTED')
        self.assertEqual({r['proposal_id'] for r in governance.guidance(self.store, 'watchlist', 'sh600000', T3)}, {a, b})
        self.assertEqual(json.loads(self.row(b)['payload_json'])['replaces'], [])

    def test_explicit_replacement_is_atomic_and_history_has_exact_boundary(self):
        a, b, companion = self.draft(), self.draft(scope='sh600000'), self.draft()
        a_rule, b_rule = self.adopt(a), self.adopt(b)
        self.adopt(companion)
        new = self.draft(replaces=[a_rule, b_rule])
        self.adopt(new, T2)
        for old in (a, b):
            self.assertEqual(self.row(old)['status'], 'RETIRED')
            self.assertEqual(self.rule(old)['retired_at'], T2)
            old_event = json.loads(self.row(old)['payload_json'])['history'][-1]
            self.assertEqual(old_event['replaced_by_guidance'], 'G-' + new)
            self.assertTrue(old_event['approval_id'])
        self.assertEqual(self.rule(companion)['status'], 'ADOPTED')
        before = governance.guidance(self.store, 'watchlist', 'sh600000', T1)
        after = governance.guidance(self.store, 'watchlist', 'sh600000', T2)
        self.assertEqual({g['proposal_id'] for g in before}, {a, b, companion})
        self.assertEqual({g['proposal_id'] for g in after}, {new, companion})
        self.assertEqual(json.loads(self.rule(new)['payload_json'])['replaces'], sorted([a_rule, b_rule]))

    def test_invalid_replacement_lists_never_change_rules(self):
        old = self.draft()
        old_id = self.adopt(old)
        other = self.draft(route='global', scope='BTC')
        other_id = self.adopt(other)
        for selected, expected in (([old_id, old_id], '重复'), (['G-missing'], '不存在'),
                                   ([other_id], '没有线路'), ('G-text', '列表')):
            with self.subTest(selected=selected):
                pid = self.draft()
                self.decide(pid, 'READY')
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, expected):
                    self.request(pid, 'APPROVED', replaces=selected)
                self.assertEqual(before, self.snapshot())
        pid = self.draft()
        self.decide(pid, 'READY')
        with self.assertRaisesRegex(ValueError, '不存在|自身'):
            self.request(pid, 'APPROVED', replaces=['G-' + pid])

    def test_retired_rule_and_nonintersecting_symbol_are_rejected(self):
        old = self.draft(scope='sh600000')
        old_id = self.adopt(old)
        other = self.draft(scope='sh600001')
        self.decide(other, 'READY')
        with self.assertRaisesRegex(ValueError, '没有线路和适用范围交集'):
            self.request(other, 'APPROVED', replaces=[old_id])
        self.decide(old, 'RETIRED', at=T2)
        new = self.draft()
        self.decide(new, 'READY')
        with self.assertRaisesRegex(ValueError, '未生效'):
            self.request(new, 'APPROVED', replaces=[old_id], at=T3)

    def test_all_route_and_scope_intersections_are_explicitly_replaceable(self):
        old = self.draft(route='ALL', scope='ALL')
        old_id = self.adopt(old)
        new = self.draft(route='global', scope='BTC', replaces=[old_id])
        self.adopt(new, T2)
        self.assertEqual(self.rule(old)['status'], 'RETIRED')
        self.assertEqual(governance.guidance(self.store, 'watchlist', 'sh600000', T2), [])

    def test_request_receipt_binds_content_note_action_and_subject(self):
        pid = self.draft()
        self.decide(pid, 'READY')
        req = self.request(pid, 'APPROVED')
        aid = self.confirm(req)
        for note in ('另一条决定', NOTE + '。'):
            with self.assertRaises(ValueError):
                governance.decide(self.store, pid, 'APPROVED', approval_id=aid, note=note, at=T1)
        other = self.draft()
        self.decide(other, 'READY')
        with self.assertRaises(ValueError):
            governance.decide(self.store, other, 'APPROVED', approval_id=aid, note=NOTE, at=T1)
        with self.assertRaises(ValueError):
            governance.decide(self.store, pid, 'REJECTED', approval_id=aid, note=NOTE, at=T1)
        self.mutate_payload(pid, lambda p: p.update(test_plan='后来改了检验标准'))
        before = self.snapshot()
        with self.assertRaises(ValueError):
            governance.decide(self.store, pid, 'APPROVED', approval_id=aid, note=NOTE, at=T1)
        self.assertEqual(before, self.snapshot())

    def test_approved_content_cannot_be_reconfirmed_as_adopted_after_mutation(self):
        pid = self.draft()
        self.decide(pid, 'READY')
        self.decide(pid, 'APPROVED')
        self.mutate_payload(pid, lambda p: p.update(failure_criteria='看到结果后改成总能通过'))
        with self.assertRaisesRegex(ValueError, '内容版本已改变'):
            self.request(pid, 'ADOPTED')
        self.assertIsNone(self.rule(pid))

    def test_approved_replacement_list_cannot_change_at_adoption(self):
        old = self.draft()
        old_id = self.adopt(old)
        pid = self.draft()
        self.decide(pid, 'READY')
        self.decide(pid, 'APPROVED')
        with self.assertRaisesRegex(ValueError, '替代列表与已批准方案不一致'):
            self.request(pid, 'ADOPTED', replaces=[old_id])

    def test_replacement_rule_version_changed_after_confirmation_invalidates_receipt(self):
        old = self.draft()
        old_id = self.adopt(old)
        pid = self.draft(replaces=[old_id])
        self.decide(pid, 'READY')
        self.decide(pid, 'APPROVED')
        req = self.request(pid, 'ADOPTED', at=T2)
        aid = self.confirm(req, at=T2)
        with self.store.db:
            self.store.db.execute("UPDATE strategy_guidance SET text='合成测试中修改了旧规则完整文本。' WHERE id=?", (old_id,))
        before = self.snapshot()
        with self.assertRaises(ValueError):
            governance.decide(self.store, pid, 'ADOPTED', approval_id=aid, note=NOTE, at=T2)
        self.assertEqual(before, self.snapshot())

    def test_receipt_replay_changes_nothing(self):
        pid = self.draft()
        self.decide(pid, 'READY')
        req = self.request(pid, 'APPROVED')
        aid = self.confirm(req)
        governance.decide(self.store, pid, 'APPROVED', approval_id=aid, note=NOTE, at=T1)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            governance.decide(self.store, pid, 'APPROVED', approval_id=aid, note=NOTE, at=T1)
        self.assertEqual(before, self.snapshot())

    def test_failure_at_receipt_consumption_rolls_back_new_and_retired_rules(self):
        old = self.draft()
        old_id = self.adopt(old)
        new = self.draft(replaces=[old_id])
        self.decide(new, 'READY')
        self.decide(new, 'APPROVED')
        req = self.request(new, 'ADOPTED', at=T2)
        aid = self.confirm(req, at=T2)
        before = self.snapshot()
        with patch('ashare.approvals.consume', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                governance.decide(self.store, new, 'ADOPTED', approval_id=aid, note=NOTE, at=T2)
        self.assertEqual(before, self.snapshot())
        governance.decide(self.store, new, 'ADOPTED', approval_id=aid, note=NOTE, at=T2)
        self.assertEqual(self.rule(old)['status'], 'RETIRED')
        self.assertEqual(self.rule(new)['status'], 'ADOPTED')

    def test_insert_failure_after_retiring_old_rule_rolls_back_every_change(self):
        old = self.draft()
        old_id = self.adopt(old)
        new = self.draft(replaces=[old_id])
        self.decide(new, 'READY')
        self.decide(new, 'APPROVED')
        req = self.request(new, 'ADOPTED', at=T2)
        aid = self.confirm(req, at=T2)
        with self.store.db:
            self.store.db.execute("CREATE TRIGGER fail_synthetic_guidance BEFORE INSERT ON strategy_guidance BEGIN SELECT RAISE(ABORT, 'synthetic insert failure'); END")
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            governance.decide(self.store, new, 'ADOPTED', approval_id=aid, note=NOTE, at=T2)
        self.assertEqual(before, self.snapshot())

    def test_nested_transaction_keeps_callers_pending_work_and_rolls_back_with_it(self):
        pid = self.draft()
        self.decide(pid, 'READY')
        req = self.request(pid, 'APPROVED')
        aid = self.confirm(req)
        before = self.snapshot()
        try:
            with self.store.db:
                self.store.db.execute("INSERT INTO metadata VALUES('synthetic-pending','yes')")
                governance.decide(self.store, pid, 'APPROVED', approval_id=aid, note=NOTE, at=T1)
                raise RuntimeError('caller rolls back')
        except RuntimeError:
            pass
        self.assertEqual(before, self.snapshot())
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM metadata WHERE key='synthetic-pending'").fetchone())
        governance.decide(self.store, pid, 'APPROVED', approval_id=aid, note=NOTE, at=T1)

    def test_legacy_approved_requires_explicit_adoption_receipt_and_replacements(self):
        pid = self.draft()
        self.mutate_payload(pid, lambda p: p.pop('replaces'))
        with self.store.db:
            self.store.db.execute("UPDATE strategy_proposals SET status='APPROVED',decided_by='historical-user',decided_at=? WHERE id=?", (T1, pid))
        with self.assertRaisesRegex(ValueError, '明确 replaces'):
            self.request(pid, 'ADOPTED', at=T2)
        req = self.request(pid, 'ADOPTED', replaces=[], at=T2)
        aid = self.confirm(req, at=T2)
        governance.decide(self.store, pid, 'ADOPTED', replaces=[], approval_id=aid, note=NOTE, at=T2)
        self.assertEqual(self.rule(pid)['status'], 'ADOPTED')
        history = json.loads(self.row(pid)['payload_json'])['history']
        self.assertEqual(history[0]['by'], 'historical-user')
        self.assertEqual(history[-1]['approval_id'], aid)

    def test_approved_withdrawal_and_named_rejection_relabel_need_receipts(self):
        pid = self.draft()
        self.decide(pid, 'READY')
        self.decide(pid, 'APPROVED')
        with self.assertRaisesRegex(ValueError, '确认回执'):
            governance.decide(self.store, pid, 'REJECTED', decided_by='Dean', note=NOTE, at=T2)
        self.decide(pid, 'REJECTED', at=T2)
        new = self.draft()
        with self.assertRaisesRegex(ValueError, '确认回执'):
            governance.decide(self.store, pid, 'SUPERSEDED', replaced_by=new, note=NOTE, at=T3)
        self.decide(pid, 'SUPERSEDED', replaced_by=new, at=T3)
        self.assertEqual(json.loads(self.row(pid)['payload_json'])['superseded_by'], new)
        self.assertEqual(json.loads(self.row(new)['payload_json'])['supersedes'], [pid])

    def test_nonproduction_proposal_version_changes_also_invalidate_approval(self):
        pid = self.draft(kind='RULE')
        self.decide(pid, 'READY')
        self.decide(pid, 'APPROVED')
        self.mutate_payload(pid, lambda p: p.update(change='另一个交易规则'))
        with self.assertRaisesRegex(ValueError, '内容版本已改变'):
            self.request(pid, 'ADOPTED')

    def test_version_excludes_only_lifecycle_but_includes_all_material_payload(self):
        pid = self.draft()
        before = governance.proposal_version(self.row(pid))
        self.decide(pid, 'READY')
        self.decide(pid, 'APPROVED')
        self.assertEqual(before, governance.proposal_version(self.row(pid)))
        for field in ('guidance', 'evidence', 'rollback', 'unknown_future_field'):
            original = self.row(pid)['payload_json']
            self.mutate_payload(pid, lambda p: p.update({field: {'nested': 'different'}}))
            self.assertNotEqual(before, governance.proposal_version(self.row(pid)))
            with self.store.db:
                self.store.db.execute('UPDATE strategy_proposals SET payload_json=? WHERE id=?', (original, pid))

    def test_retirement_requires_receipt_and_keeps_original_rule_text(self):
        pid = self.draft()
        self.adopt(pid)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, '确认回执'):
            governance.decide(self.store, pid, 'RETIRED', decided_by='Dean', note=NOTE, at=T2)
        self.assertEqual(before, self.snapshot())
        original_text = self.rule(pid)['text']
        self.decide(pid, 'RETIRED', at=T2)
        self.assertEqual(self.rule(pid)['text'], original_text)
        self.assertEqual(self.rule(pid)['retired_at'], T2)
        self.assertEqual(len(governance.guidance(self.store, 'watchlist', 'sh600000', T1)), 1)
        self.assertEqual(governance.guidance(self.store, 'watchlist', 'sh600000', T2), [])

    def test_preview_contains_rule_text_but_never_embedded_raw_evidence(self):
        old = self.draft()
        old_id = self.adopt(old)
        with self.store.db:
            self.store.db.execute('UPDATE strategy_guidance SET payload_json=? WHERE id=?',
                (json.dumps({'note': '旧治理说明', 'legacy_embedded_raw': 'DO_NOT_EXPORT_OLD_RAW'}), old_id))
        pid = self.draft(replaces=[old_id])
        self.mutate_payload(pid, lambda p: p.update(evidence=[{'raw': 'DO_NOT_EXPORT_NEW_RAW',
            'path': '/local/private/SENSITIVE_EVIDENCE_FILE'}], observations=[{'raw': 'DO_NOT_EXPORT_OBSERVATION'}],
            test_plan={'raw': 'DO_NOT_EXPORT_STRUCTURED_PLAN'}))
        self.decide(pid, 'READY')
        req = self.request(pid, 'APPROVED')
        frozen = json.dumps({'summary': req['summary'], 'snapshot': req['snapshot']}, ensure_ascii=False)
        for secret in ('DO_NOT_EXPORT', 'SENSITIVE_EVIDENCE_FILE'):
            self.assertNotIn(secret, frozen)
        self.assertIn(self.rule(old)['text'], frozen)
        self.assertEqual(req['summary']['replaces'][0]['id'], old_id)
        self.assertEqual(req['summary']['guidance']['text'], json.loads(self.row(pid)['payload_json'])['guidance']['text'])
        self.assertEqual(req['snapshot']['proposal_hash'], governance.proposal_version(self.row(pid)))

    def test_verified_receipt_actor_is_recorded_instead_of_supplied_name(self):
        pid = self.draft()
        self.decide(pid, 'READY')
        req = self.request(pid, 'APPROVED')
        aid = self.confirm(req)
        result = governance.decide(self.store, pid, 'APPROVED', decided_by='UNTRUSTED_NAME',
                                   approval_id=aid, note=NOTE, at=T1)
        self.assertEqual(result['decided_by'], 'admin')
        self.assertEqual(json.loads(result['payload_json'])['history'][-1]['by'], 'admin')

    def test_named_rejection_can_be_confirmed_without_approving_the_proposal(self):
        pid = self.draft()
        with self.assertRaisesRegex(ValueError, '不能仅填写批准人'):
            governance.decide(self.store, pid, 'REJECTED', decided_by='Dean', note=NOTE, at=T1)
        req = self.request(pid, 'REJECTED')
        aid = self.confirm(req)
        governance.decide(self.store, pid, 'REJECTED', approval_id=aid, note=NOTE, at=T1)
        self.assertEqual(self.row(pid)['status'], 'REJECTED')
        self.assertEqual(self.row(pid)['decided_by'], 'admin')
        self.assertIsNone(self.rule(pid))


if __name__ == '__main__':
    unittest.main()
