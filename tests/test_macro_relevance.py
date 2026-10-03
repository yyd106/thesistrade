"""Event display usefulness over time, using only synthetic local fixtures."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from ashare import macro
from ashare.storage import Store
from ashare.macro_presentation import lifecycle, observation_waiting

AT = '2026-10-03T10:00:00+00:00'


def event_fixture(key='one', published=AT, *, status='TRACKING', horizon='DAYS', direction='UP'):
    """Small public-shaped synthetic event reusable by browser preview fixtures."""
    analysis = {'news_id': 'n-' + key, 'headline': '合成事件 ' + key, 'theme': 'MONETARY',
        'regions': ['US'], 'horizon': horizon, 'facts': '合成公开政策事实，仍需要验证实际产业变化。',
        'transmission': '政策可能改变资金成本', 'uncertainty': '缺少实际执行证据',
        'invalidation': '政策撤回', 'expectations': 'UNKNOWN', 'expectation_basis': '没有一致预期数据',
        'impacts': [{'asset': 'US10Y', 'direction': direction, 'mechanism': '影响利率预期',
            'watch': '核实正式政策实施日期', 'strength': 'HIGH'}],
        'evidence': [{'news_id': 'n-' + key, 'quote': 'Synthetic public policy source.'}]}
    return {'id': 'e-' + key, 'news_id': 'n-' + key, 'created_at': published, 'basis': 'FORWARD',
            'theme': 'MONETARY', 'status': status, 'published_at': published, 'analysis': analysis}


class MacroRelevanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def event(self, key='one', published=AT, **kwargs):
        e = event_fixture(key, published, **kwargs)
        self.store.db.execute('''INSERT INTO dynamic_news
            (id,source,url,published_at,first_seen_at,title,body,cluster_id,revision_of,status,raw_path)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (e['news_id'], 'Synthetic source', 'https://example.com/' + key,
            published, published, e['analysis']['headline'], 'Synthetic public policy source.', key, None, 'NEW', 'synthetic'))
        self.store.db.execute('INSERT INTO macro_events VALUES(?,?,?,?,?,?,?)',
            (e['id'], e['news_id'], published, e['basis'], e['theme'], e['status'], json.dumps(e['analysis'])))
        self.store.db.commit()
        return e

    def at(self, days=0, hours=0):
        return (datetime.fromisoformat(AT) + timedelta(days=days, hours=hours)).isoformat()

    def test_lifecycle_has_existing_policy_boundaries_without_changing_records(self):
        e = self.event()
        e['horizon'] = 'DAYS'
        self.assertEqual(lifecycle(e, self.at(hours=71))['state'], 'PENDING_REVIEW')
        self.assertEqual(lifecycle(e, self.at(hours=72))['state'], 'REVIEW_DUE')
        self.assertEqual(lifecycle(e, self.at(days=7))['state'], 'ENDED')
        e['horizon'] = 'MONTHS'
        self.assertEqual(lifecycle(e, self.at(days=6))['state'], 'PENDING_REVIEW')
        self.assertEqual(lifecycle(e, self.at(days=7))['state'], 'REVIEW_DUE')
        self.assertEqual(lifecycle(e, self.at(days=30))['state'], 'ENDED')
        self.assertEqual(self.store.db.execute('SELECT status FROM macro_events').fetchone()[0], 'TRACKING')

    def test_custom_policy_and_earliest_cluster_anchor_match_observation_policy(self):
        e = self.event()
        e['horizon'] = 'DAYS'
        e['catalyst_at'] = self.at(days=-2)
        config = {'dynamic_observation_policy': {'cooldown_hours': 48, 'archive_days': 5}}
        life = lifecycle(e, AT, config)
        self.assertEqual(life['state'], 'REVIEW_DUE')
        self.assertEqual(life['expires_at'], self.at(days=3))
        self.assertEqual(lifecycle(e, self.at(days=3), config)['state'], 'ENDED')

    def test_current_and_historical_views_share_representative_deduplication(self):
        first = self.event('first')
        second = self.event('second')
        for e in (first, second):
            self.store.db.execute('INSERT INTO macro_news_queue VALUES(?,?,?,?,?)',
                (e['news_id'], first['news_id'], 20, 'Same synthetic event', AT))
        self.store.db.commit()
        current = macro.view(self.store, AT)
        older = macro.view(self.store, self.at(days=17))
        self.assertEqual([e['id'] for e in current['items']], [first['id']])
        self.assertEqual([e['id'] for e in older['archived_items']], [first['id']])
        self.assertEqual(older['library']['history_total'], 1)

    def test_future_representative_cannot_hide_current_event(self):
        first = self.event('first')
        second = self.event('future', self.at(hours=1))
        self.store.db.execute('INSERT INTO macro_news_queue VALUES(?,?,?,?,?)',
            (first['news_id'], second['news_id'], 20, 'Future representative', AT))
        self.store.db.commit()
        result = macro.view(self.store, AT)
        self.assertEqual([e['id'] for e in result['items']], [first['id']])

    def test_followups_and_expired_history_are_separate_with_truthful_bounded_counts(self):
        for i in range(47):
            self.event('active-' + str(i))
            self.event('old-' + str(i), self.at(days=-17))
        changes = self.store.db.total_changes
        result = macro.view(self.store, AT)
        self.assertEqual(result['library']['followup_total'], 47)
        self.assertEqual(result['library']['history_total'], 47)
        self.assertEqual(len(result['followup_items']), 40)
        self.assertEqual(len(result['history_items']), 40)
        self.assertTrue(all(e['lifecycle']['actionable'] for e in result['followup_items']))
        self.assertTrue(all(e['lifecycle']['state'] == 'ENDED' for e in result['history_items']))
        self.assertEqual(self.store.db.total_changes, changes)

    def test_background_is_history_and_original_direction_survives_independent_disagreement(self):
        e = self.event()
        assessment = {'state': 'BACKGROUND', 'admitted': False, 'assessment': {'direction': 'DOWN', 'missing_evidence': '实际采购合同'}}
        with patch('ashare.macro_impact.context', return_value={(e['id'], 'US10Y'): assessment}):
            result = macro.view(self.store, AT)
        self.assertFalse(result['followup_items'])
        event = result['history_items'][0]
        self.assertEqual(event['lifecycle']['state'], 'BACKGROUND')
        self.assertEqual(event['analysis']['impacts'][0]['direction'], 'UP')
        self.assertEqual(event['analysis']['impacts'][0]['materiality']['assessment']['direction'], 'DOWN')

    def test_followup_prioritizes_explicit_missing_evidence_and_preserves_draft(self):
        e = self.event()
        assessment = {'state': 'NEEDS_EVIDENCE', 'admitted': False, 'assessment': {'direction': 'DOWN', 'missing_evidence': '实际采购合同'}}
        with patch('ashare.macro_impact.context', return_value={(e['id'], 'US10Y'): assessment}):
            result = macro.view(self.store, AT)
        self.assertIn('实际采购合同', result['followup_items'][0]['lifecycle']['next_step'])
        self.assertEqual(json.loads(self.store.db.execute('SELECT payload_json FROM macro_events').fetchone()[0]), e['analysis'])

    def test_expired_or_invalidated_events_do_not_wait_for_future_windows_forever(self):
        e = self.event('old', self.at(days=-17))
        result = macro.view(self.store, AT)['history_items'][0]
        self.assertEqual(result['lifecycle']['state'], 'ENDED')
        self.assertIn('已超过可测量窗口', result['reactions'][0]['waiting'])
        self.assertNotIn('等待事件后', result['reactions'][0]['waiting'])
        e['status'] = 'INVALIDATED'
        self.assertIn('原判断已失效', observation_waiting({'series': 'DGS10'}, e, AT))
        self.assertIn('不会自动形成价格验证', observation_waiting({}, e, AT))

    def test_revisions_trace_actual_changed_fields_and_exclude_future_replacements(self):
        e = self.event()
        prior = dict(self.store.db.execute('SELECT * FROM macro_events').fetchone())
        changed = copy.deepcopy(e['analysis'])
        changed['headline'] = '修订后的合成判断'
        changed['impacts'][0]['direction'] = 'DOWN'
        self.store.db.execute('INSERT INTO macro_event_revisions(event_id,replaced_at,previous_json) VALUES(?,?,?)',
            (e['id'], AT, json.dumps(prior)))
        self.store.db.execute('UPDATE macro_events SET payload_json=? WHERE id=?', (json.dumps(changed), e['id']))
        future = self.event('future', self.at(hours=1))
        self.store.db.execute('UPDATE dynamic_news SET revision_of=? WHERE id=?', (e['news_id'], future['news_id']))
        self.store.db.commit()
        result = macro.view(self.store, AT)['items'][0]['revisions']
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['changed'], ['研究主题', '标的、方向或推演'])
        self.assertEqual(result['items'][0]['before_directions'][0]['direction'], 'UP')
        self.assertEqual(result['items'][0]['after_directions'][0]['direction'], 'DOWN')
        self.assertFalse(result['source_replacements'])

    def test_same_analysis_revision_does_not_invent_materiality_changes(self):
        e = self.event()
        prior = dict(self.store.db.execute('SELECT * FROM macro_events').fetchone())
        self.store.db.execute('INSERT INTO macro_event_revisions(event_id,replaced_at,previous_json) VALUES(?,?,?)', (e['id'], AT, json.dumps(prior)))
        self.store.db.commit()
        self.assertEqual(macro.view(self.store, AT)['items'][0]['revisions']['items'][0]['changed'], [])

    def test_recursive_source_corrections_show_current_version_and_true_count(self):
        old = self.event('original', self.at(days=-6))
        previous = old['news_id']
        for i in range(5):
            newer = self.event('revision-' + str(i), self.at(days=-5 + i))
            self.store.db.execute('UPDATE dynamic_news SET revision_of=? WHERE id=?', (previous, newer['news_id']))
            self.store.db.execute("UPDATE dynamic_news SET status='REVISED' WHERE id=?", (previous,))
            previous = newer['news_id']
        self.store.db.execute("UPDATE macro_events SET status='INVALIDATED' WHERE id=?", (old['id'],))
        self.store.db.commit()
        result = next(e for e in macro.view(self.store, AT)['history_items'] if e['id'] == old['id'])['revisions']
        self.assertEqual(result['source_replacement_total'], 5)
        self.assertEqual(len(result['source_replacements']), 3)
        self.assertEqual(result['source_replacements'][0]['id'], 'n-revision-4')
        self.assertEqual(result['source_replacements'][0]['source_status'], 'NEW')
        self.assertEqual(result['source_replacements'][0]['event_id'], 'e-revision-4')
        self.assertTrue(all(r['source_status'] == 'REVISED' for r in result['source_replacements'][1:]))

    def market(self):
        points = [{'date': date, 'value': value} for date, value in
                  [('2026-10-02', 4), ('2026-10-04', 4.1), ('2026-10-05', 4.2), ('2026-10-06', 4.3)]]
        payload = {'points': points, 'url': 'https://example.com/series', 'series': 'DGS10', 'raw_path': 'synthetic'}
        self.store.db.execute('INSERT INTO macro_markets VALUES(?,?,?,?,?)', ('US10Y', self.at(days=5), 'OK', json.dumps(payload), None))
        self.store.db.commit()

    def test_more_than_600_completed_events_do_not_block_a_new_observation(self):
        for i in range(601):
            e = self.event(str(i))
            if i < 600:
                self.store.db.execute('INSERT INTO macro_observations VALUES(?,?,?,?,?)', (e['id'], 'US10Y', self.at(days=4), 'FORWARD', '{"unchanged":true}'))
        self.market()
        before = list(self.store.db.execute('SELECT event_id,payload_json FROM macro_observations ORDER BY event_id'))
        self.assertEqual(macro.measure(self.store, self.at(days=6)), 1)
        new = json.loads(self.store.db.execute("SELECT payload_json FROM macro_observations WHERE event_id='e-600'").fetchone()[0])
        self.assertEqual(new['change'], 30)
        self.assertEqual(new['change_unit'], 'bp')
        self.assertEqual(macro.measure(self.store, self.at(days=6)), 0)
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in self.store.db.execute("SELECT event_id,payload_json FROM macro_observations WHERE event_id!='e-600' ORDER BY event_id")])

    def test_incomplete_old_events_do_not_block_new_missing_outcomes(self):
        for i in range(601):
            self.event('stale-' + str(i), self.at(days=-90))
        self.event('new')
        self.market()
        self.assertEqual(macro.measure(self.store, self.at(days=6)), 1)
        self.assertEqual(self.store.db.execute('SELECT event_id FROM macro_observations').fetchone()[0], 'e-new')


if __name__ == '__main__':
    unittest.main()
