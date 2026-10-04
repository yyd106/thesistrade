from approval_fixture import decide as confirmed_decide
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ashare import governance
from ashare.cli import _store_command
from ashare.review import route_lessons
from ashare.storage import Store


BEFORE = '2026-09-01T00:00:00+00:00'
ADOPTED = '2026-09-02T00:00:00+00:00'
DURING = '2026-09-03T00:00:00+00:00'
RETIRED = '2026-09-04T00:00:00+00:00'
AFTER = '2026-09-05T00:00:00+00:00'


class GovernanceCorrectnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.arguments = {'source': 'synthetic', 'kind': 'RESEARCH_GUIDANCE', 'target': 'watchlist',
            'title': '合成研究规则', 'payload': {'guidance': {'route': 'watchlist', 'scope': 'sh600000',
                'text': '研究时明确区分事实与尚未核验的假设。'}, 'hypothesis': '合成假设',
                'observations': [{'detail': '初始合成依据'}]}, 'at': BEFORE, 'dedupe_key': 'synthetic-rule'}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def draft(self, **changes):
        with self.store.db:
            return governance.draft_proposal(self.store, **(copy.deepcopy(self.arguments) | changes))

    def decide(self, pid, status, at=ADOPTED, **kwargs):
        return confirmed_decide(self.store, pid, status, decided_by='SYNTHETIC_USER',
                                 note='仅用于合成测试', at=at, **kwargs)

    def to_state(self, pid, status):
        if status == 'REJECTED':
            self.decide(pid, status)
        elif status == 'SUPERSEDED':
            replacement = self.draft(dedupe_key=None)
            self.decide(pid, status, replaced_by=replacement)
        elif status != 'DRAFT':
            sequence = ['READY', 'APPROVED', 'ADOPTED', 'RETIRED']
            for step in sequence[:sequence.index(status) + 1]:
                self.decide(pid, step, RETIRED if step == 'RETIRED' else ADOPTED)

    def guidance(self, at, route='watchlist', symbol='sh600000'):
        return governance.guidance(self.store, route, symbol, at)

    def test_retirement_preserves_historical_rules_and_exact_time_boundaries(self):
        pid = self.draft()
        self.assertEqual(self.guidance(DURING), [])
        self.to_state(pid, 'ADOPTED')
        self.assertEqual(self.guidance(BEFORE), [])
        expected = self.guidance(ADOPTED)
        self.assertEqual([r['proposal_id'] for r in expected], [pid])
        self.assertEqual(self.guidance('2026-09-02T08:00:00+08:00'), expected)
        self.assertEqual(self.guidance(DURING), expected)
        self.decide(pid, 'RETIRED', RETIRED)
        self.assertEqual(self.guidance(BEFORE), [])
        self.assertEqual(self.guidance(ADOPTED), expected)
        self.assertEqual(self.guidance(DURING), expected)
        self.assertEqual(self.guidance('2026-09-04T08:00:00+08:00'), [])
        self.assertEqual(self.guidance(AFTER), [])
        self.assertEqual(self.guidance(DURING, symbol='sh600001'), [])
        self.assertEqual(self.guidance(DURING, route='global'), [])

    def test_all_scopes_and_complementary_rules_still_coexist(self):
        specific = self.draft()
        self.to_state(specific, 'ADOPTED')
        payload = {'guidance': {'route': 'ALL', 'scope': 'ALL', 'text': '研究时记录每项判断引用的原始证据。'}}
        broad = self.draft(dedupe_key='all-routes', payload=payload)
        self.to_state(broad, 'ADOPTED')
        self.assertEqual({g['proposal_id'] for g in self.guidance(DURING)}, {specific, broad})
        self.assertEqual([g['proposal_id'] for g in self.guidance(DURING, route='global', symbol='BTC')], [broad])
        self.decide(specific, 'RETIRED', RETIRED)
        self.assertEqual({g['proposal_id'] for g in self.guidance(DURING)}, {specific, broad})
        self.assertEqual([g['proposal_id'] for g in self.guidance(AFTER)], [broad])

    def test_missing_retirement_time_does_not_reactivate_a_retired_rule(self):
        pid = self.draft()
        self.to_state(pid, 'ADOPTED')
        with self.store.db:
            self.store.db.execute("UPDATE strategy_guidance SET status='RETIRED',retired_at=NULL WHERE proposal_id=?", (pid,))
        self.assertEqual(self.guidance(DURING), [])
        self.assertEqual(self.guidance(AFTER), [])

    def test_identical_duplicate_is_read_only_and_reports_every_lifecycle_state(self):
        for state in ('DRAFT', 'READY', 'APPROVED', 'ADOPTED', 'REJECTED', 'RETIRED', 'SUPERSEDED'):
            with self.subTest(state=state):
                key = 'state-' + state
                created = self.draft(dedupe_key=key, return_receipt=True)
                self.assertEqual((created['action'], created['status']), ('CREATED', 'DRAFT'))
                self.to_state(created['id'], state)
                before = [tuple(r) for r in self.store.db.execute('SELECT * FROM strategy_proposals ORDER BY id')]
                total_changes = self.store.db.total_changes
                found = self.draft(dedupe_key=key, at=AFTER, return_receipt=True)
                self.assertEqual(found, {'id': created['id'], 'status': state, 'action': 'EXISTS'})
                self.assertEqual(self.draft(dedupe_key=key, at=AFTER), created['id'])
                self.assertEqual(self.store.db.total_changes, total_changes)
                self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT * FROM strategy_proposals ORDER BY id')], before)

    def test_conflicting_key_rejects_all_changed_content_without_writes(self):
        pid = self.draft()
        self.to_state(pid, 'REJECTED')
        before = tuple(self.store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (pid,)).fetchone())
        original = self.arguments['payload']
        changes = [{'source': 'different'}, {'kind': 'RULE'}, {'target': 'global'}, {'title': '另一个标题'},
                   {'payload': {**original, 'hypothesis': '另一个假设'}},
                   {'payload': {**original, 'observations': [{'detail': '新观察须单独登记'}]}}]
        for change in changes:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, '内容不一致'):
                self.draft(**change)
            self.assertEqual(tuple(self.store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (pid,)).fetchone()), before)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM strategy_proposals').fetchone()[0], 1)

    def test_200_character_title_and_target_are_preserved_and_idempotent(self):
        fields = {'target': '标' * 200, 'title': '题' * 200}
        created = self.draft(**fields, return_receipt=True)
        stored = self.store.db.execute('SELECT target,title FROM strategy_proposals WHERE id=?', (created['id'],)).fetchone()
        self.assertEqual(dict(stored), fields)
        changes = self.store.db.total_changes
        self.assertEqual(self.draft(**fields, at=AFTER, return_receipt=True),
                         {'id': created['id'], 'status': 'DRAFT', 'action': 'EXISTS'})
        self.assertEqual(self.store.db.total_changes, changes)

    def test_overlong_or_nonstring_title_and_target_reject_before_creating(self):
        for field in ('title', 'target'):
            for value in ('字' * 201, None, 42, True, [], {}):
                with self.subTest(field=field, value=value):
                    changes = self.store.db.total_changes
                    with self.assertRaisesRegex(ValueError, field + ' 须为不超过200字的字符串'):
                        self.draft(**{field: value}, return_receipt=True)
                    self.assertEqual(self.store.db.total_changes, changes)
                    self.assertEqual(self.store.db.execute('SELECT count(*) FROM strategy_proposals').fetchone()[0], 0)

    def test_matching_200_character_prefix_does_not_hide_a_new_tail(self):
        fields = {'title': '题' * 200, 'target': '标' * 200}
        created = self.draft(**fields, return_receipt=True)
        for field in fields:
            with self.subTest(field=field):
                changes = self.store.db.total_changes
                with self.assertRaisesRegex(ValueError, field + ' 须为不超过200字的字符串'):
                    self.draft(**(fields | {field: fields[field] + '尾'}), return_receipt=True)
                self.assertEqual(self.store.db.total_changes, changes)
        self.assertEqual(self.draft(**fields), created['id'])

    def test_unique_insert_collision_never_returns_a_created_receipt(self):
        # Simulate a competing writer winning after our lookup, before our insert.
        original = self.store.db
        arguments = copy.deepcopy(self.arguments)
        class CompetingInsert:
            def execute(inner, sql, parameters=()):
                if sql.startswith('INSERT INTO strategy_proposals'):
                    original.execute(sql, ('competing-writer',) + parameters[1:])
                return original.execute(sql, parameters)
        with patch.object(self.store, 'db', CompetingInsert()):
            with self.assertRaises(sqlite3.IntegrityError):
                governance.draft_proposal(self.store, **arguments, return_receipt=True)
        found = self.draft(return_receipt=True)
        self.assertEqual(found, {'id': 'competing-writer', 'status': 'DRAFT', 'action': 'EXISTS'})

    def test_cli_repeat_reports_closed_status_and_changed_spec_is_rejected(self):
        spec = {'source': 'synthetic', 'kind': 'RULE', 'target': 'watchlist', 'title': '合成规则',
                'hypothesis': '假设', 'change': '调整', 'evidence': '证据', 'test_plan': '检验',
                'failure_criteria': '失败标准', 'rollback': '撤回', 'dedupe_key': 'cli-synthetic'}
        path = Path(self.tmp.name) / 'proposal.json'
        path.write_text(json.dumps(spec), encoding='utf-8')
        args = SimpleNamespace(command='proposals', action='new', file=str(path), supersedes=None, note=None)
        first = _store_command(args, {}, self.store)
        self.assertEqual((first['action'], first['status']), ('CREATED', 'DRAFT'))
        self.to_state(first['id'], 'REJECTED')
        row = tuple(self.store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (first['id'],)).fetchone())
        repeated = _store_command(args, {}, self.store)
        self.assertEqual(repeated, {'id': first['id'], 'action': 'EXISTS', 'status': 'REJECTED', 'supersedes': []})
        path.write_text(json.dumps({**spec, 'change': '新的调整'}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '内容不一致'):
            _store_command(args, {}, self.store)
        self.assertEqual(tuple(self.store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (first['id'],)).fetchone()), row)

    def test_review_evidence_reports_closed_proposal_without_rewriting_it(self):
        lesson = {'symbol': 'GOLD', 'category': 'RESEARCH', 'lesson': '须核对反向证据。',
                  'applicability': '高波动', 'decision_ids': [], 'fill_ids': []}
        with self.store.db:
            created = route_lessons(self.store, 'r1', [lesson], BEFORE, 'build-synthetic')[0]
        self.assertEqual((created['action'], created['status'], created['observation_action']), ('CREATED', 'DRAFT', 'RECORDED'))
        self.to_state(created['id'], 'REJECTED')
        frozen = tuple(self.store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (created['id'],)).fetchone())
        with self.store.db:
            later = route_lessons(self.store, 'r2', [{**lesson, 'lesson': '须核对反向证据！'}], DURING, 'build-synthetic')[0]
            duplicate = route_lessons(self.store, 'r2', [lesson], AFTER, 'build-synthetic')[0]
        self.assertEqual((later['id'], later['action'], later['status'], later['observation_action']),
                         (created['id'], 'EXISTS', 'REJECTED', 'RECORDED'))
        self.assertEqual((duplicate['action'], duplicate['status'], duplicate['observation_action']),
                         ('EXISTS', 'REJECTED', 'ALREADY_RECORDED'))
        self.assertEqual(tuple(self.store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (created['id'],)).fetchone()), frozen)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM strategy_proposals').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM review_observations').fetchone()[0], 2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM strategy_guidance').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
