"""Synthetic read-only strategy downloads; never use production data."""
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ashare import strategy_export as export

AT = '2026-10-22T12:00:00+00:00'
START = '2026-10-08T16:00:00+00:00'
BEFORE = '2026-10-08T15:59:59+00:00'


class StrategyExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = sqlite3.connect(self.root / 'agent.sqlite3')
        self.addCleanup(self.db.close)
        for table in ('portfolio_decisions', 'plans', 'studies', 'global_plans', 'dynamic_cases',
                      'dynamic_news', 'portfolio_history_snapshots', 'portfolio_source_events',
                      'cloud_outbox', 'cloud_receipts', 'macro_events', 'macro_impact_assessments',
                      'macro_impact_sources', 'research_methods', 'strategy_guidance'):
            self.db.execute('CREATE TABLE ' + table + '(' + ','.join(k + ' TEXT' for k in export.COLUMNS[table].split()) + ')')
        self.db.commit()

    def insert(self, table, **row):
        self.db.execute('INSERT INTO ' + table + '(' + ','.join(row) + ') VALUES(' + ','.join('?' for _ in row) + ')',
                        [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in row.values()])
        self.db.commit()

    def decision(self, identity, at, *, action='HOLD', current=800, target=None, reason='原始理由',
                 summary='组合总结', source='plan1', token='token1', qty=100, sent=False, frozen=None):
        self.insert('portfolio_decisions', id=identity, created_at=at, valid_until='2026-10-23T00:00:00+00:00',
                    status='ACTIVE', version='v1', payload_json={'summary': summary, 'decisions': [{
                        'key': 'watchlist:sh600000', 'route': 'watchlist', 'symbol': 'sh600000', 'name': '测试公司',
                        'action': action, 'target_bps': current if target is None else target, 'current_bps': current,
                        'qty': qty, 'cost_cents': 10000, 'reason': reason, 'source_id': source, 'source_token': token,
                        'can_increase': True, 'evidence_ids': ['research:' + source], 'related_keys': []}],
                        'risk_groups': []})
        if sent:
            self.insert('cloud_outbox', id=identity, created_at=at, status='SENT')
        if frozen is not None:
            self.insert('portfolio_history_snapshots', decision_id=identity, history_json={
                'version': 'portfolio-source-history-v1', 'frozen_at': at,
                'sources': {'watchlist:sh600000': {'record': frozen, 'source_order': 1}}})

    def plan(self, identity='plan1', study='study1', thesis='研究全文', at=START):
        result = {'id': identity, 'study_id': study, 'symbol': 'sh600000', 'activated_at': at,
                  'valid_until': '2026-10-23T00:00:00+00:00', 'status': 'ACTIVE', 'strategy_version': 'v1',
                  'payload_json': json.dumps({'kind': 'PAPER_TRADE', 'thesis': thesis, 'levels': {'buy_low_cents': 1000}})}
        self.insert('plans', **result)
        return result

    def archive(self, at=AT):
        content, name = export.build_archive(self.root, at)
        archive = zipfile.ZipFile(io.BytesIO(content))
        return content, name, archive, json.loads(archive.read('策略数据.json'))

    def test_calendar_window_fixed_filenames_empty_database_and_read_only(self):
        before = hashlib.sha256((self.root / 'agent.sqlite3').read_bytes()).hexdigest()
        content, name, archive, data = self.archive()
        self.assertEqual(name, 'thesistrade-strategies-20261009-20261022.zip')
        self.assertTrue(name.isascii())
        self.assertEqual(tuple(archive.namelist()), export.ENTRIES)
        self.assertEqual(data['schema'], export.SCHEMA)
        self.assertEqual(data['start_at'], START)
        self.assertEqual(data['end_at'], AT)
        self.assertEqual(data['generated_at'], AT)
        self.assertEqual(data['decisions'], [])
        self.assertIn('本窗口没有可用组合决定', archive.read('交易策略变更.md').decode())
        self.assertEqual(before, hashlib.sha256((self.root / 'agent.sqlite3').read_bytes()).hexdigest())
        self.assertEqual(content, self.archive()[0])

    def test_time_offsets_boundaries_and_same_time_order(self):
        self.decision('baseline', BEFORE)
        self.decision('first', '2026-10-09T00:00:00+08:00')
        self.decision('second', START)
        self.decision('at-end', AT)
        self.decision('future', '2026-10-22T12:00:00.000001+00:00')
        _, _, _, data = self.archive()
        self.assertEqual([r['record']['id'] for r in data['decisions']], ['first', 'second', 'at-end'])
        self.assertEqual(data['baseline']['record']['id'], 'baseline')
        self.assertEqual(data['decisions'][0]['generated_change']['categories'], ['renewal_only'])
        self.assertEqual(data['decisions'][1]['previous_generated_id'], 'first')

    def test_missing_baseline_is_not_fabricated_change(self):
        self.decision('initial', START, action='ALLOW', target=1000)
        data = self.archive()[3]
        self.assertEqual(data['decisions'][0]['generated_change'], {'categories': ['missing_baseline'], 'items': []})
        self.assertEqual(data['decisions'][0]['publication']['state'], 'UNKNOWN')
        self.assertIsNone(data['decisions'][0]['acknowledged_change'])

    def test_preceding_baseline_keeps_submillisecond_window_boundary(self):
        self.decision('older', BEFORE, sent=True)
        self.decision('nearest', '2026-10-08T15:59:59.999999+00:00', action='EXIT', target=0, sent=True)
        self.decision('inserted-later-but-older', '2026-10-08T15:59:59.999998+00:00', sent=True)
        self.decision('inside', START, sent=True)
        data = self.archive()[3]
        self.assertEqual(data['baseline']['record']['id'], 'nearest')
        self.assertEqual(data['published_baseline']['record']['id'], 'nearest')
        self.assertEqual([d['record']['id'] for d in data['decisions']], ['inside'])
        self.assertIn('action_target_change', data['decisions'][0]['generated_change']['categories'])

    def test_old_baseline_submillisecond_ties_sort_by_exact_time(self):
        self.decision('nearest', '2026-10-06T15:59:59.123456+00:00', action='EXIT', target=0, sent=True)
        self.decision('inserted-later-but-older', '2026-10-06T15:59:59.123455+00:00', sent=True)
        self.decision('inside', START, sent=True)
        data = self.archive()[3]
        self.assertEqual(data['baseline']['record']['id'], 'nearest')
        self.assertEqual(data['published_baseline']['record']['id'], 'nearest')

    def test_receipt_after_cutoff_microsecond_is_not_publication_proof(self):
        self.decision('acknowledged', BEFORE, sent=True)
        self.decision('future-receipt', '2026-10-08T15:59:59.5+00:00', action='EXIT', target=0)
        self.insert('cloud_receipts', sequence=1, bundle_id='future-receipt',
                    received_at='2026-10-22T12:00:00.000001+00:00', completed_at=AT)
        self.decision('inside', START)
        self.insert('cloud_receipts', sequence=2, bundle_id='inside', received_at=AT, completed_at=AT)
        data = self.archive()[3]
        self.assertEqual(data['baseline']['record']['id'], 'future-receipt')
        self.assertEqual(data['baseline']['publication']['state'], 'UNKNOWN')
        self.assertEqual(data['published_baseline']['record']['id'], 'acknowledged')
        self.assertEqual(data['published_decision_ids'], ['inside'])
        self.assertEqual(data['decisions'][0]['previous_acknowledged_id'], 'acknowledged')

    def test_identical_comparison_routes_reference_same_complete_change(self):
        self.decision('before', BEFORE, sent=True)
        self.decision('after', START, reason='新的完整当轮理由', sent=True)
        data = self.archive()[3]
        row = data['decisions'][0]
        self.assertEqual(row['acknowledged_change']['items_ref'], 'generated_change.items')
        self.assertEqual(row['acknowledged_change']['categories'], row['generated_change']['categories'])
        delta = row['generated_change']['items'][0]
        self.assertNotIn('before', delta)
        self.assertNotIn('after', delta)
        self.assertEqual(delta['before_ref'], {'decision_id': 'before', 'key': 'watchlist:sh600000'})
        self.assertEqual(delta['after_ref'], {'decision_id': 'after', 'key': 'watchlist:sh600000'})
        records = {d['record']['id']: d for d in [data['baseline'], data['published_baseline'], *data['decisions']] if d}
        def resolve(ref):
            return next(d for d in records[ref['decision_id']]['payload']['decisions'] if d['key'] == ref['key'])
        self.assertEqual(resolve(delta['before_ref'])['reason'], '原始理由')
        self.assertEqual(resolve(delta['after_ref'])['reason'], '新的完整当轮理由')

    def test_generated_and_acknowledged_timelines_use_different_baselines(self):
        self.decision('accepted-before', BEFORE, sent=True)
        self.decision('generated-before', '2026-10-08T15:59:59.5+00:00', action='EXIT', target=0)
        self.decision('pending', START, action='EXIT', target=0)
        self.decision('accepted', '2026-10-09T01:00:00+08:00', sent=True)
        _, _, archive, data = self.archive()
        current = data['decisions'][-1]
        self.assertEqual(data['baseline']['record']['id'], 'generated-before')
        self.assertEqual(data['published_baseline']['record']['id'], 'accepted-before')
        self.assertIn('action_target_change', current['generated_change']['categories'])
        self.assertEqual(current['acknowledged_change']['categories'], ['renewal_only'])
        self.assertEqual(data['published_decision_ids'], ['accepted'])
        text = archive.read('交易策略变更.md').decode()
        self.assertIn('相对上一生成版本的变化', text)
        self.assertIn('相对上一已确认发布版本的变化', text)

    def test_acknowledged_action_change_survives_unpublished_generated_intermediate(self):
        self.decision('accepted-before', BEFORE, current=0, target=0, sent=True)
        self.decision('pending', START, action='ALLOW', current=0, target=500)
        self.decision('accepted', '2026-10-09T01:00:00+08:00', action='ALLOW', current=0, target=500, sent=True)
        _, _, archive, data = self.archive()
        current = data['decisions'][-1]
        self.assertEqual(current['generated_change']['categories'], ['renewal_only'])
        self.assertEqual(current['acknowledged_change']['categories'], ['action_target_change'])
        delta = current['acknowledged_change']['items'][0]
        self.assertEqual(delta['before_ref']['decision_id'], 'accepted-before')
        self.assertEqual(delta['after_ref']['decision_id'], 'accepted')
        text = archive.read('交易策略变更.md').decode().split('## 2026-10-09 01:00:00')[1]
        confirmed = text.split('相对上一已确认发布版本的变化', 1)[1]
        self.assertIn('保持 / 0%', confirmed)
        self.assertIn('允许新增 / 5%', confirmed)

    def test_hold_weight_follows_current_is_not_active_target_change(self):
        self.decision('before', BEFORE, current=879)
        self.decision('after', START, current=880)
        change = self.archive()[3]['decisions'][0]['generated_change']
        self.assertEqual(change['categories'], ['position_drift'])
        self.assertNotIn('action_target_change', change['categories'])

    def test_action_reason_summary_and_holding_changes_are_separate(self):
        self.decision('before', BEFORE)
        self.decision('after', START, action='REDUCE', target=400, reason='当轮减仓原文', summary='不同总结', qty=200)
        archive = self.archive()
        change = archive[3]['decisions'][0]['generated_change']
        self.assertEqual(change['categories'], ['action_target_change', 'holding_change', 'reason_update'])
        text = archive[2].read('交易策略变更.md').decode()
        self.assertIn('当轮减仓原文', text)
        self.assertIn('原始理由', text)

    def test_frozen_sources_are_exact_deduplicated_and_link_old_study(self):
        frozen = self.plan(thesis='当时完整研究')
        self.insert('studies', id='study1', symbol='sh600000', created_at=BEFORE, model_status='SUCCEEDED',
                    result_json={'summary': '原研究完整总结', 'stocks': [{'symbol': 'sh600000', 'action': 'WATCH', 'analysis': '原研究全文'}]})
        self.decision('one', START, frozen=frozen)
        self.decision('two', AT, frozen=frozen)
        with self.db:
            self.db.execute('UPDATE plans SET payload_json=?', (json.dumps({'kind': 'NO_ENTRY', 'thesis': '后来研究'}),))
        data = self.archive()[3]
        self.assertEqual(len(data['frozen_sources']), 1)
        source = next(iter(data['frozen_sources'].values()))
        self.assertEqual(json.loads(source['record']['payload_json'])['thesis'], '当时完整研究')
        study = data['research']['studies'][0]
        self.assertTrue(study['outside_window_linked'])
        self.assertEqual(json.loads(study['record']['result_json'])['summary'], '原研究完整总结')

    def test_missing_frozen_history_does_not_substitute_present_source(self):
        self.plan(thesis='当前来源')
        self.decision('unfrozen', START)
        data = self.archive()[3]
        self.assertEqual(data['decisions'][0]['source_refs'], {})
        self.assertTrue(any(i['code'] == 'FROZEN_HISTORY_UNAVAILABLE' for i in data['issues']))

    def test_source_renewal_differs_from_new_research(self):
        first = self.plan('plan1')
        second = self.plan('plan2', at='2026-10-09T01:00:00+08:00')
        self.decision('before', BEFORE, source='plan1', frozen=first)
        self.decision('after', START, source='plan2', token='newtoken', frozen=second)
        kinds = self.archive()[3]['decisions'][0]['generated_change']['categories']
        self.assertIn('source_renewal', kinds)
        self.assertNotIn('source_change', kinds)

    def test_projection_does_not_export_inputs_config_credentials_or_raw_paths(self):
        self.db.execute('CREATE TABLE snapshots(id TEXT, packet_json TEXT)')
        self.insert('snapshots', id='snapshot', packet_json={'raw': 'PRIVATE_INPUT_MARKER'})
        self.insert('studies', id='../escape', symbol='sh600000', created_at=START, model_status='SUCCEEDED',
                    result_json={'summary': '<script>UNTRUSTED</script>', 'config': {'api_key': 'PRIVATE_CONFIG_MARKER'},
                                 'stocks': [{'symbol': 'sh600000', 'analysis': '可见全文', 'api_key': 'PRIVATE_SECRET_MARKER',
                                             'raw_path': '/PRIVATE_FILE_MARKER', 'prompt': 'PRIVATE_PROMPT_MARKER',
                                             'credentials': {'value': 'PRIVATE_CREDENTIALS_MARKER'},
                                             'refresh_token': 'PRIVATE_REFRESH_MARKER',
                                             'decision': {'invalidation': '真实失效条件', 'input': {'data': 'PRIVATE_INPUT_OBJECT_MARKER'}}}]})
        self.insert('global_plans', id='global', symbol='GOLD', created_at=START, valid_until=AT, status='ACTIVE', fingerprint='fp',
                    payload_json={'analysis': {'thesis': '全球研究全文', 'input': {'data': 'PRIVATE_ANALYSIS_INPUT_MARKER'}},
                                  'model': {'actual_model': 'test', 'raw_response': 'PRIVATE_RESPONSE_MARKER'},
                                  'build': {'version': 'v1', 'other': 'PRIVATE_BUILD_MARKER'}})
        _, _, archive, data = self.archive()
        text = '\n'.join(archive.read(n).decode() for n in archive.namelist())
        for marker in ('PRIVATE_INPUT_MARKER', 'PRIVATE_CONFIG_MARKER', 'PRIVATE_SECRET_MARKER', 'PRIVATE_FILE_MARKER', 'PRIVATE_PROMPT_MARKER',
                       'PRIVATE_CREDENTIALS_MARKER', 'PRIVATE_REFRESH_MARKER', 'PRIVATE_INPUT_OBJECT_MARKER',
                       'PRIVATE_ANALYSIS_INPUT_MARKER', 'PRIVATE_RESPONSE_MARKER', 'PRIVATE_BUILD_MARKER'):
            self.assertNotIn(marker, text)
        self.assertIn('可见全文', text)
        self.assertNotIn('<script>', archive.read('研究策略.md').decode())
        self.assertEqual(len(archive.namelist()), 4)

    def test_dynamic_analysis_retains_current_production_research_contract(self):
        values = {'impact': '原始冲击', 'business_link': '业务传导', 'pricing': '价格反映', 'priced_in': 'UNKNOWN',
                  'invalidation': '可观察失效条件'}
        self.insert('dynamic_cases', id='case', news_id='news', symbol='sh600000', name='公司', theme='macro',
                    event_type='OTHER', direction='BULLISH', created_at=START, expires_at=AT,
                    basis='FORWARD', status='RESEARCH', analysis_json=values, plan_json={'history': {'count': 7}})
        row = self.archive()[3]['research']['dynamic_cases'][0]['record']
        self.assertEqual(json.loads(row['analysis_json']), values)
        self.assertEqual(json.loads(row['plan_json'])['history'], {'count': 7})

    def test_macro_assessments_keep_research_and_follow_explicit_old_event(self):
        self.insert('macro_events', id='event', news_id='news', created_at=BEFORE, basis='FORWARD', theme='GROWTH',
                    status='TRACKING', payload_json={'facts': '旧事件事实', 'transmission': '传导链'})
        self.insert('macro_impact_sources', id='citation', event_id='event', published_at=BEFORE,
                    first_seen_at=BEFORE, title='引用标题', url='https://example.test/source')
        payload = {'event_id': 'event', 'asset': 'GOLD', 'basis': '独立影响判断', 'impact_basis': 'EXPECTATIONS',
                   'expectation_test': {'change': 'VERIFIED_CHANGE', 'probability_logic': '原始概率逻辑'},
                   'citations': [{'source_id': 'citation', 'supports': 'BASELINE', 'quote': '可见引用片段'}]}
        self.insert('macro_impact_assessments', id='assessment', event_id='event', asset='GOLD', created_at=START,
                    input_hash='hash', version='v1', payload_json=payload,
                    features_json={'measurement_resolution': 'DAILY', 'baseline': {'date': '2026-10-08', 'value': 1},
                                   'market_raw_path': '/PRIVATE_MARKET_PATH', 'raw': 'PRIVATE_RAW_MARKER'})
        _, _, archive, data = self.archive()
        record = data['research']['macro_impact_assessments'][0]['record']
        self.assertEqual(json.loads(record['payload_json']), payload)
        self.assertTrue(data['research']['macro_events'][0]['outside_window_linked'])
        self.assertEqual(data['research']['macro_impact_sources'][0]['record']['title'], '引用标题')
        text = archive.read('策略数据.json').decode()
        self.assertNotIn('PRIVATE_MARKET_PATH', text)
        self.assertNotIn('PRIVATE_RAW_MARKER', text)

    def test_older_linked_method_and_current_guidance_are_explicit_baselines(self):
        self.insert('research_methods', id='method', created_at=BEFORE, status='ACTIVE',
                    payload_json={'input': 'incremental changed evidence', 'policy': 'v1'})
        self.insert('research_methods', id='bad-method', created_at=START, status='ACTIVE',
                    payload_json={'input': {'api_key': 'PRIVATE_METHOD_INPUT'}})
        self.insert('global_plans', id='global', symbol='GOLD', created_at=START, valid_until=AT, status='ACTIVE',
                    fingerprint='fp', payload_json={'method_id': 'method', 'analysis': {'stance': 'WAIT', 'thesis': '研究依据'}})
        self.insert('strategy_guidance', id='guide', route='ALL', scope='ALL', text='明确研究规则', status='ADOPTED',
                    adopted_at=BEFORE, payload_json={})
        data = self.archive()[3]
        method = next(i for i in data['research']['research_methods'] if i['record']['id'] == 'method')
        self.assertTrue(method['outside_window_linked'])
        self.assertEqual(json.loads(method['record']['payload_json'])['input'], 'incremental changed evidence')
        self.assertTrue(data['research']['strategy_guidance'][0]['current_state_baseline'])
        self.assertNotIn('PRIVATE_METHOD_INPUT', json.dumps(data))

    def test_source_events_are_complete_single_source_versions(self):
        frozen = self.plan()
        for seq, thesis in ((1, '第一原始来源'), (2, '第二原始来源')):
            row = dict(frozen, payload_json=json.dumps({'thesis': thesis, 'kind': 'PAPER_TRADE'}))
            self.insert('portfolio_source_events', seq=seq, epoch='epoch', route='watchlist', identity='sh600000',
                        source_id='plan1', source_order=1, recorded_at=START, observed_at=AT, event='UPDATE', record_json=row)
        data = self.archive()[3]
        self.assertEqual(len(data['source_events']), 2)
        self.assertEqual([json.loads(data['frozen_sources'][e['source_ref']]['record']['payload_json'])['thesis']
                          for e in data['source_events']], ['第一原始来源', '第二原始来源'])
        self.assertEqual(data['decisions'], [])

    def test_invalid_json_and_time_are_explicit_without_raw_invalid_payload(self):
        self.decision('baseline', BEFORE)
        self.insert('portfolio_decisions', id='bad', created_at=START, valid_until=AT, status='ACTIVE', version='v1', payload_json='{BROKEN_PRIVATE_TEXT')
        self.insert('studies', id='badtime', symbol='sh600000', created_at='not-a-date', model_status='SUCCEEDED', result_json={})
        _, _, archive, data = self.archive()
        self.assertEqual(data['decisions'][0]['generated_change']['categories'], ['unreadable_comparison'])
        self.assertEqual({i['code'] for i in data['issues']} & {'INVALID_TIME', 'INVALID_RESEARCH_JSON'}, {'INVALID_TIME', 'INVALID_RESEARCH_JSON'})
        self.assertNotIn('BROKEN_PRIVATE_TEXT', archive.read('策略数据.json').decode())

    def test_limits_fail_instead_of_silently_truncating(self):
        self.decision('one', START)
        with patch.object(export, 'MAX_UNCOMPRESSED', 10):
            with self.assertRaises(export.ExportTooLarge):
                self.archive()
        with patch.object(export, 'MAX_ARCHIVE', 10):
            with self.assertRaises(export.ExportTooLarge):
                self.archive()
        with patch.object(export, 'MAX_ROWS', 0):
            with self.assertRaises(export.ExportTooLarge):
                self.archive()

    def test_naive_cutoff_and_missing_database_fail_without_creating_database(self):
        with self.assertRaises(ValueError):
            self.archive('2026-10-22T12:00:00')
        missing = self.root / 'missing'
        with self.assertRaises(sqlite3.OperationalError):
            export.build_archive(missing, AT)
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
