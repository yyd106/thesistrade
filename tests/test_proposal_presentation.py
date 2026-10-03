import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ashare import governance, supervision, proposal_presentation as presentation
from ashare.cli import _store_command
from ashare.cloud_protocol import canonical
from ashare.cloud_sync import handle
from ashare.storage import Store


AT = '2026-10-03T01:00:00+00:00'


class ProposalPresentationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def proposal(self, at=AT, **updates):
        payload = {k: '合成的完整提案摘要：' + k for k in supervision.FIELDS}
        payload.update(updates)
        with self.store.db:
            identity = governance.draft_proposal(self.store, source='test', kind='RULE', target='合成测试',
                title='改进方案', payload=payload, at=at)
        return governance.proposal(self.store, identity)

    def test_cards_are_read_only_allowlisted_and_explain_approval(self):
        row = self.proposal(private_packet={'raw': 'RAW_PRIVATE_MARKER'},
            observations=[{'raw': 'RAW_PRIVATE_MARKER'}], counter_explanations=['市场共同变化'])
        before = self.store.db.total_changes
        card = presentation.card(self.store, row)
        self.assertEqual(card['status'], 'DRAFT')
        self.assertEqual(card['readiness']['status'], 'READY')
        self.assertNotIn('等待你批准', card['next_step'])
        self.assertNotIn('RAW_PRIVATE_MARKER', json.dumps(card))
        self.assertEqual(before, self.store.db.total_changes)
        governance.decide(self.store, row['id'], 'READY', decided_by=None, note='合成测试', at=AT)
        review = {'id': 'SR-test', 'status': 'SUCCEEDED', 'verdict': 'RECOMMEND', 'summary': '监督建议通过'}
        with patch.object(supervision, 'listing', return_value=[review]) as query:
            card = presentation.card(self.store, governance.proposal(self.store, row['id']))
        query.assert_called_once_with(self.store, limit=1, subject_id=row['id'])
        self.assertEqual(card['status'], 'READY')
        self.assertIn('仍需 Dean 明确批准', card['next_step'])
        with patch.object(supervision, 'listing', return_value=[{**review, 'status': 'STALE'}]):
            stale = presentation.card(self.store, governance.proposal(self.store, row['id']))
        self.assertFalse(stale['supervision']['current'])
        self.assertNotIn('建议通过', stale['next_step'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM strategy_guidance').fetchone()[0], 0)

    def test_incomplete_evidence_is_visible_and_closed_proposal_does_not_reopen(self):
        for absent in (None, '', '  '):
            missing = presentation.card(self.store, self.proposal(evidence=absent))
            self.assertEqual(missing['evidence']['status'], 'MISSING')
            self.assertIn('尚未整理', missing['evidence']['text'])
            self.assertEqual(missing['readiness']['status'], 'INCOMPLETE')
        row = self.proposal(evidence={})
        card = presentation.card(self.store, row)
        self.assertEqual(card['readiness']['status'], 'INCOMPLETE')
        self.assertIn(card['evidence']['status'], ('INVALID', 'MISSING'))
        governance.decide(self.store, row['id'], 'REJECTED', decided_by=None, note='无完整证据', at=AT)
        card = presentation.card(self.store, governance.proposal(self.store, row['id']))
        self.assertEqual(card['status'], 'REJECTED')
        self.assertIn('已驳回', card['next_step'])

    def test_old_proposal_is_accessible_by_cli_beyond_recent_list_limit(self):
        old = self.proposal(at='2026-09-01T00:00:00+00:00')
        for _ in range(201):
            self.proposal()
        self.assertNotIn(old['id'], {p['id'] for p in governance.proposals(self.store)})
        result = _store_command(SimpleNamespace(command='proposals', action='show', id=old['id']), {}, self.store)
        self.assertEqual(result['id'], old['id'])
        self.assertEqual(result['review_card']['id'], old['id'])

    def test_combined_display_budget_and_cloud_sync_do_not_renew_lease(self):
        for _ in range(25):
            self.proposal(**{k: '汉字' * 5000 for k in supervision.FIELDS})
        reviews = [{'id': 'SR-' + str(i), 'summary': '汉字' * 7000} for i in range(30)]
        display = presentation.attach(self.store, {'items': reviews,
            'discovery': {'at': AT, 'errors': ['汉字' * 5000] * 50}})
        self.assertLess(len(canonical(display)), 1_000_000)
        self.assertGreater(len(display['proposals']['items']), 0)
        self.assertEqual(display['proposals']['total'], 25)
        self.assertEqual(display['proposals']['shown'], len(display['proposals']['items']))
        self.assertGreater(display['reviews_omitted'], 0)
        with self.store.db:
            self.store.db.execute("INSERT INTO cloud_state VALUES('research_completed_at',?)", (json.dumps(AT),))
        before = self.store.db.execute('SELECT id,status,payload_json FROM strategy_proposals ORDER BY id').fetchall()
        result = handle(self.store, {}, '/api/sync/reviews', {'supervision': display}, AT)
        self.assertEqual(result['status'], 'ACCEPTED')
        self.assertEqual(json.loads(self.store.db.execute("SELECT value FROM cloud_state WHERE key='research_completed_at'").fetchone()[0]), AT)
        self.assertEqual(self.store.db.execute('SELECT id,status,payload_json FROM strategy_proposals ORDER BY id').fetchall(), before)
        received = json.loads(self.store.db.execute("SELECT value FROM cloud_state WHERE key='display_supervision'").fetchone()[0])
        self.assertEqual(received['proposals'], display['proposals'])

    def test_empty_cards_and_ready_priority(self):
        self.assertEqual(presentation.cards(self.store), {'items': [], 'total': 0, 'shown': 0})
        ready = self.proposal(at='2026-09-01T00:00:00+00:00')
        governance.decide(self.store, ready['id'], 'READY', decided_by=None, note='合成测试', at=AT)
        for _ in range(22):
            self.proposal()
        shown = presentation.cards(self.store)
        self.assertEqual(shown['items'][0]['id'], ready['id'])
        self.assertLessEqual(shown['shown'], 20)

    def test_active_experiment_remains_visible_above_twenty_newer_drafts(self):
        active = self.proposal(at='2026-09-01T00:00:00+00:00')
        ready = self.proposal(at='2026-09-02T00:00:00+00:00')
        governance.decide(self.store, ready['id'], 'READY', decided_by=None, note='合成测试', at=AT)
        for _ in range(22):
            self.proposal()
        with self.store.db:
            self.store.db.execute('INSERT INTO experiment_designs VALUES(?,?,?,?,?)', ('EX-test', active['id'], AT, 'test', '{}'))
            self.store.db.execute('INSERT INTO experiment_events(experiment_id,at,status,note) VALUES(?,?,?,?)',
                                  ('EX-test', AT, 'RUNNING', '合成测试'))
        with patch.object(presentation, 'card', side_effect=lambda store, row: {'id': row['id']}):
            shown = presentation.cards(self.store)
            self.assertEqual(shown['items'][0]['id'], active['id'])
            self.assertEqual(shown['items'][1]['id'], ready['id'])
            with self.store.db:
                self.store.db.execute('INSERT INTO experiment_events(experiment_id,at,status,note) VALUES(?,?,?,?)',
                                      ('EX-test', AT, 'CANCELLED', '合成测试'))
            self.assertEqual(presentation.cards(self.store)['items'][0]['id'], ready['id'])

    def test_experiment_progress_does_not_approve_or_promote_proposal(self):
        row = self.proposal()
        for state, expectation in [('RUNNING', '正在采集配对观察'), ('COMPLETED', '仅提供结构可检验性'),
                                   ('INCONCLUSIVE', '尚不能判断')]:
            with self.subTest(state=state):
                experiment = {'id': 'EX-test', 'status': state, 'execution': {'status': state}}
                with patch.object(presentation, 'experiment_summary', return_value=experiment):
                    card = presentation.card(self.store, row)
                self.assertEqual(card['status'], 'DRAFT')
                self.assertIn(expectation, card['next_step'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM strategy_guidance').fetchone()[0], 0)

    def test_confirmation_window_failure_is_visible_in_next_step(self):
        row = self.proposal()
        experiment = {'id': 'EX-test', 'status': 'COMPLETED',
                      'execution': {'status': 'COMPLETED', 'assessment': 'NOT_SUPPORTED'}}
        with patch.object(presentation, 'experiment_summary', return_value=experiment):
            card = presentation.card(self.store, row)
        self.assertEqual(card['status'], 'DRAFT')
        self.assertIn('分窗结果不支持候选', card['next_step'])
        self.assertNotIn('提交监督审查', card['next_step'])


if __name__ == '__main__':
    unittest.main()
