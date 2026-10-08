"""Industry candidates must reach dynamic research with foreign keys enforced."""
import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from ashare import dynamic, dynamic_sources, industry, observation, observation_pool, universe
from ashare.storage import Store, normalize_time
from test_config import load_config


class IndustryObservationReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.at = normalize_time('2026-09-25T12:00:00+08:00')
        self.config = load_config(
            Path(__file__).resolve().parents[1] / 'config.example.json',
            configure_model=False,
        )
        self.config.update(
            data_dir=self.tmp.name,
            industry_enabled=True,
            investment_policy='days_cash_v1',
            watchlist=[{'symbol': 'sh600519', 'name': '贵州茅台'}],
        )
        self.assertEqual(self.store.db.execute('PRAGMA foreign_keys').fetchone()[0], 1)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def later(self, days):
        return normalize_time((datetime.fromisoformat(self.at) + timedelta(days=days)).isoformat())

    def save_hypothesis(self, symbol='sz300499', *, waiting=False, at=None):
        at = at or self.at
        name = '合成液冷公司' + symbol
        text = name + '的客户项目预算已经落实，液冷产品采购招标启动。公司供货份额尚待核实。'
        doc_id = self.store.add_document(
            symbol=symbol, kind='company_report', title='项目采购披露', source='company',
            url='https://example.org/industry/' + symbol, published_at=at,
            first_seen_at=at, ready_at=at, pages=[(1, text)],
            raw_path=self.store.raw(text.encode(), '.txt'), quality='text', cloud_allowed=True,
        )[0]
        fact = dict(
            kind='MILESTONE', entity=name, counterparty='', product='液冷产品',
            project='项目A', owner='客户A', lot='一标段', metric='BUDGET',
            unit='元', period='2026年第四季度', value='100', effective_from='',
            effective_until='', claim_type='DISCLOSED', evidence_id=doc_id + ':0', quote=text,
        )
        proposal = dict(
            symbol=symbol, domain='ai', method='8', topic='项目A液冷产品',
            thesis='项目采购可能形成新增业务', state='ACTIVE', next_check='核实合同和份额',
            invalidation='项目取消或公司不供货', alternatives='其他供应商也可能供货',
            profit_capture='核实价格和成本', causal_chain=['客户采购启动', '公司可能取得订单'],
            counterpoints=['尚未确认收入'], missing=['供货份额未知'],
            facts=[fact] if waiting else [fact, {**fact, 'metric': 'TENDER'}], forecasts=[],
        )
        identity = dict(asset=symbol, symbol=symbol, name=name, category='CN', kind='STOCK')
        return industry.save(self.store, proposal, at, {symbol: identity})

    def rows(self, table):
        return [dict(row) for row in self.store.db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]

    def test_active_industry_candidate_reconciles_with_foreign_keys(self):
        self.save_hypothesis()
        self.assertFalse(self.rows('macro_watchlist'))
        result = observation_pool.reconcile(self.store, self.config, self.at)
        self.assertEqual([item['asset'] for item in result['items']], ['sz300499'])
        self.assertEqual(result['items'][0]['pool_tier'], 'FOCUS')
        self.assertEqual([row['asset'] for row in self.rows('macro_watchlist')], ['sz300499'])
        self.assertEqual([row['asset'] for row in self.rows('macro_watch_state')], ['sz300499'])
        self.assertFalse(list(self.store.db.execute('PRAGMA foreign_key_check')))

    def test_waiting_industry_candidate_is_persisted_without_admission(self):
        self.save_hypothesis(waiting=True)
        self.assertEqual(industry.latest(self.store, self.at)[0]['state'], 'WAITING')
        self.assertFalse(universe.permit(self.store, self.config, 'sz300499', self.at))
        result = observation_pool.reconcile(self.store, self.config, self.at)
        self.assertFalse(result['items'])
        self.assertEqual(result['archived_items'][0]['pool_tier'], 'COOLING')
        self.assertEqual(result['archived_items'][0]['links'][0]['industry_state'], 'WAITING')
        self.assertEqual([row['asset'] for row in self.rows('macro_watch_state')], ['sz300499'])
        self.assertFalse(universe.permit(self.store, self.config, 'sz300499', self.at))
        self.assertFalse(list(self.store.db.execute('PRAGMA foreign_key_check')))

    def test_already_expired_industry_candidate_can_first_reconcile_as_archived(self):
        self.save_hypothesis()
        at = self.later(31)
        self.assertFalse(self.rows('macro_watchlist'))
        self.assertFalse(universe.permit(self.store, self.config, 'sz300499', at))
        result = observation_pool.reconcile(self.store, self.config, at)
        self.assertFalse(result['items'])
        self.assertEqual(result['archived_items'][0]['pool_tier'], 'ARCHIVED')
        self.assertEqual(result['archived_items'][0]['links'][0]['industry_state'], 'ARCHIVED')
        self.assertEqual([row['asset'] for row in self.rows('macro_watch_state')], ['sz300499'])
        self.assertFalse(universe.permit(self.store, self.config, 'sz300499', at))
        self.assertFalse(list(self.store.db.execute('PRAGMA foreign_key_check')))

    def test_repeated_reconcile_keeps_evidence_deadlines_and_membership_without_duplicate_links(self):
        self.save_hypothesis()
        original_watchlist = copy.deepcopy(self.config['watchlist'])
        original_hypotheses = self.rows('industry_hypotheses')
        original_facts = self.rows('industry_facts')
        original_membership = universe.membership(self.store, self.config, self.at)
        original_items = observation.all_items(self.store, self.at)
        self.assertTrue(universe.permit(self.store, self.config, 'sz300499', self.at))

        first = observation_pool.reconcile(self.store, self.config, self.at)
        first_transitions = self.rows('macro_watch_transitions')
        first_state = self.rows('macro_watch_state')
        first_parents = self.rows('macro_watchlist')
        second = observation_pool.reconcile(self.store, self.config, self.at)

        self.assertEqual(second['counts'], first['counts'])
        self.assertEqual(self.rows('macro_watch_transitions'), first_transitions)
        self.assertEqual(self.rows('macro_watch_state'), first_state)
        self.assertEqual(self.rows('macro_watchlist'), first_parents)
        self.assertFalse(self.rows('macro_watch_links'))
        later_items = observation.all_items(self.store, self.at)
        self.assertEqual(len(later_items), 1)
        for key in ('asset', 'symbol', 'name', 'category', 'kind', 'unit', 'added_at',
                    'updated_at', 'status', 'strength', 'direction', 'conflicting', 'links'):
            self.assertEqual(later_items[0][key], original_items[0][key], key)
        self.assertEqual(len(later_items[0]['links']), 1)
        for row in first_parents:
            identity = json.loads(row['payload_json'])
            self.assertNotIn('links', identity)
            self.assertNotIn('pool_tier', identity)
            self.assertNotIn('protected', identity)

        observation_pool.reconcile(self.store, self.config, self.later(1))
        self.assertEqual(self.rows('macro_watch_transitions'), first_transitions)
        self.assertEqual(self.rows('macro_watchlist'), first_parents)
        self.assertEqual(universe.membership(self.store, self.config, self.at), original_membership)
        self.assertTrue(universe.permit(self.store, self.config, 'sz300499', self.at))
        self.assertFalse(universe.permit(self.store, self.config, 'sz300499', self.later(7)))
        observation_pool.reconcile(self.store, self.config, self.later(7))
        expired = observation.all_items(self.store, self.later(31))[0]
        self.assertEqual(len(expired['links']), 1)
        self.assertEqual(expired['links'][0]['industry_state'], 'ARCHIVED')
        self.assertFalse(expired['links'][0]['materiality']['admitted'])
        self.assertEqual(expired['links'][0]['qualification_review_at'], original_items[0]['links'][0]['qualification_review_at'])
        self.assertEqual(self.rows('industry_hypotheses'), original_hypotheses)
        self.assertEqual(self.rows('industry_facts'), original_facts)
        self.assertEqual(self.config['watchlist'], original_watchlist)

    def test_existing_parent_identity_and_timestamps_are_not_overwritten(self):
        self.save_hypothesis()
        identity = dict(
            asset='sz300499', symbol='sz300499', name='既有证券目录名称', category='CN',
            kind='STOCK', unit='元', identity_source='verified-registry', custom_metadata='keep',
        )
        with self.store.db:
            self.store.db.execute(
                'INSERT INTO macro_watchlist VALUES(?,?,?,?)',
                ('sz300499', self.later(-10), self.later(-1), json.dumps(identity, ensure_ascii=False)),
            )
        original = self.rows('macro_watchlist')
        observation_pool.reconcile(self.store, self.config, self.at)
        observation_pool.reconcile(self.store, self.config, self.later(1))
        self.assertEqual(self.rows('macro_watchlist'), original)
        item = observation.all_items(self.store, self.at)[0]
        self.assertEqual(item['name'], identity['name'])
        self.assertEqual(item['custom_metadata'], 'keep')
        self.assertEqual(item['added_at'], self.later(-10))
        self.assertEqual(item['updated_at'], self.later(-1))
        self.assertEqual(len(item['links']), 1)

    def test_failed_state_write_rolls_back_all_new_parents_states_and_transitions(self):
        self.save_hypothesis('sz300499')
        self.save_hypothesis('sz300500')
        original_hypotheses = self.rows('industry_hypotheses')
        with self.store.db:
            self.store.db.execute('''CREATE TRIGGER fail_second_watch_state
                BEFORE INSERT ON macro_watch_state WHEN NEW.asset='sz300500'
                BEGIN SELECT RAISE(ABORT, 'synthetic state write failure'); END''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'synthetic state write failure'):
            observation_pool.reconcile(self.store, self.config, self.at)
        for table in ('macro_watchlist', 'macro_watch_state', 'macro_watch_transitions'):
            self.assertFalse(self.rows(table), table)
        self.assertEqual(self.rows('industry_hypotheses'), original_hypotheses)
        self.assertFalse(list(self.store.db.execute('PRAGMA foreign_key_check')))

    def test_later_macro_event_joins_the_same_asset_without_duplicating_industry_evidence(self):
        hypothesis_id = self.save_hypothesis()
        observation_pool.reconcile(self.store, self.config, self.at)
        at = self.later(1)
        text = '公开政策发布了新的项目采购要求，可能影响液冷设备采购。'
        dynamic_sources.ingest(self.store, [{
            'title': text, 'body': text, 'source': '合成公开来源',
            'url': 'https://finance.sina.com.cn/synthetic-industry-policy.htm', 'published_at': at,
        }], at, self.store.raw(text.encode(), '.txt'))
        news = self.rows('dynamic_news')[0]
        impact = dict(
            asset='sz300499', direction='UP', strength='MEDIUM',
            logic_chain=[{'kind': 'FACT', 'statement': text, 'news_id': news['id'], 'quote': text}],
            conditions=['核实采购实施'], invalidation='采购要求撤回',
        )
        event = dict(headline='新的采购要求', horizon='DAYS', theme='POLICY', impacts=[impact])
        identity = dict(asset='sz300499', symbol='sz300499', name='目录已核实名称',
                        category='CN', kind='STOCK', unit='元', identity_source='verified-registry')
        with self.store.db:
            self.store.db.execute('INSERT INTO macro_events VALUES(?,?,?,?,?,?,?)',
                                  ('synthetic-macro-event', news['id'], at, 'FORWARD', 'POLICY',
                                   'TRACKING', json.dumps(event, ensure_ascii=False)))
            observation.sync_event(self.store, 'synthetic-macro-event', event, at, {'sz300499': identity})

        self.assertEqual(len(self.rows('macro_watchlist')), 1)
        self.assertEqual(len(self.rows('macro_watch_links')), 1)
        self.assertEqual(self.rows('macro_watchlist')[0]['added_at'], self.at)
        self.assertEqual(self.rows('macro_watchlist')[0]['updated_at'], at)
        self.assertEqual(json.loads(self.rows('macro_watchlist')[0]['payload_json']), identity)
        observation_pool.reconcile(self.store, self.config, at)
        observation_pool.reconcile(self.store, self.config, at)
        items = observation.all_items(self.store, at)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(items[0]['links']), 2)
        self.assertEqual({link['event_id'] for link in items[0]['links']},
                         {'industry:' + hypothesis_id, 'synthetic-macro-event'})
        self.assertEqual(items[0]['identity_source'], 'verified-registry')
        historical = observation.all_items(self.store, self.at)
        self.assertEqual([link['event_id'] for link in historical[0]['links']], ['industry:' + hypothesis_id])
        self.assertFalse(list(self.store.db.execute('PRAGMA foreign_key_check')))

    def test_persisting_a_later_industry_candidate_does_not_expose_it_to_earlier_snapshots(self):
        first = self.save_hypothesis()
        observation_pool.reconcile(self.store, self.config, self.at)
        second = self.save_hypothesis('sz300500', at=self.later(2))
        observation_pool.reconcile(self.store, self.config, self.later(2))
        self.assertEqual(len(self.rows('macro_watchlist')), 2)
        historical = observation.all_items(self.store, self.at)
        self.assertEqual([item['asset'] for item in historical], ['sz300499'])
        self.assertEqual([link['event_id'] for link in historical[0]['links']], ['industry:' + first])
        current = observation.all_items(self.store, self.later(2))
        self.assertEqual({item['asset'] for item in current}, {'sz300499', 'sz300500'})
        self.assertEqual({link['event_id'] for item in current for link in item['links']},
                         {'industry:' + first, 'industry:' + second})

    def test_dynamic_cycle_reaches_global_research_and_impact_review_with_industry_only_asset(self):
        self.save_hypothesis()
        self.config['model_enabled'] = True
        news = {'id': 'synthetic-news', 'status': 'ANALYZED', 'attempts': 0}
        original_watchlist = copy.deepcopy(self.config['watchlist'])
        actual_reconcile = observation_pool.reconcile

        def research_after_reconcile(*args):
            self.assertEqual([row['asset'] for row in self.rows('macro_watch_state')], ['sz300499'])
            return {'summary': 'Synthetic global research completed', 'events': []}

        with ExitStack() as patches:
            for target, value in {
                'ashare.dynamic.now': self.at,
                'ashare.connectivity.check': None,
                'ashare.macro.select_news': [news],
                'ashare.news_triage.select': ([news], {}),
                'ashare.news_triage.context': None,
                'ashare.macro_sources.refresh_markets': [],
                'ashare.macro.measure': 0,
                'ashare.impact_history.learn': {'status': 'DONE'},
                'ashare.sources.stock_catalog': [],
                'ashare.dynamic_sources.refresh_market': None,
            }.items():
                patches.enter_context(patch(target, return_value=value))
            research = patches.enter_context(patch('ashare.macro.research', side_effect=research_after_reconcile))
            review = patches.enter_context(patch('ashare.macro_impact.review', return_value={'status': 'DONE'}))
            reconcile = patches.enter_context(patch('ashare.observation_pool.reconcile', wraps=actual_reconcile))
            reevaluate = patches.enter_context(patch('ashare.dynamic.reevaluate', wraps=dynamic.reevaluate))
            result = dynamic.cycle(
                self.store, self.config, end=self.at,
                collect_fn=lambda *args: {'failures': []},
                model_fn=lambda *args: {}, impact_model_fn=lambda *args: {},
            )

        self.assertEqual(result['status'], 'SUCCEEDED')
        self.assertEqual(result['failures'], [])
        self.assertEqual(result['global_selected'], 1)
        self.assertEqual(result['global_research']['summary'], 'Synthetic global research completed')
        self.assertEqual(result['impact_review'], {'status': 'DONE'})
        research.assert_called_once()
        review.assert_called_once()
        reevaluate.assert_called_once()
        self.assertEqual(reconcile.call_count, 2)
        self.assertEqual(self.rows('dynamic_runs')[0]['status'], 'SUCCEEDED')
        self.assertEqual(len(self.rows('macro_watch_transitions')), 1)
        self.assertEqual(len(observation.all_items(self.store, self.at)[0]['links']), 1)
        self.assertEqual(self.config['watchlist'], original_watchlist)
        self.assertFalse(list(self.store.db.execute('PRAGMA foreign_key_check')))


if __name__ == '__main__':
    unittest.main()
