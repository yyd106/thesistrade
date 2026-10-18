import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ashare import dynamic_sources, macro, news_relevance as relevance, news_triage
from ashare.storage import Store, normalize_time
from news_fixtures import screen_deep
from test_config import load_config


class NewsRelevanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.at = normalize_time('2026-10-18T10:00:00+08:00')
        self.cfg = load_config(Path(__file__).resolve().parents[1] / 'config.example.json')
        self.cfg.update(data_dir=self.tmp.name, industry_enabled=True, watchlist=[
            {'symbol': 'sz300499', 'name': '高澜股份'},
            {'symbol': 'sz000333', 'name': '美的集团'},
        ])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def assess(self, text, current=None):
        news = {'title': text, 'body': text, 'source': '新浪财经快讯'}
        return relevance.assess(news, current if current is not None else relevance.profiles(self.store, self.cfg, self.at), macro.topics(news))

    def news(self, key, body, source='新浪财经快讯'):
        dynamic_sources.ingest(self.store, [{'title': body[:95], 'body': body,
            'url': 'https://finance.sina.com.cn/' + key, 'source': source,
            'published_at': self.at}], self.at, 'fixture')
        return dict(self.store.db.execute('SELECT * FROM dynamic_news ORDER BY rowid DESC LIMIT 1').fetchone())

    def queue(self, limit=64):
        return macro.select_news(self.store, self.at, self.at, self.at, self.cfg, candidate_limit=limit)

    def test_core_identity_works_without_any_dynamic_focus_or_active_targets(self):
        current = relevance.profiles(self.store, self.cfg, self.at)
        self.assertEqual({p['symbol'] for p in current}, {'sz300499', 'sz000333'})
        self.assertEqual(self.assess('高澜股份公布年度报告。', current)['kind'], 'COMPANY')
        self.assertEqual(self.assess('Midea Group announces annual results.', current)['kind'], 'COMPANY')

    def test_company_without_macro_topic_enters_queue_with_explanation(self):
        n = self.news('annual', '高澜股份公布年度报告，董事会披露新的业绩数据。')
        self.assertFalse(macro.topics(n))
        self.assertEqual([x['id'] for x in self.queue()], [n['id']])
        reason = self.store.db.execute('SELECT reason FROM macro_news_queue WHERE news_id=?', (n['id'],)).fetchone()[0]
        self.assertIn('当前公司直接提及', reason)
        self.assertIn('sz300499', reason)

    def test_industry_keywords_work_in_both_languages_without_macro_topic(self):
        for text in ('液冷设备新增订单已签约。', 'Liquid cooling equipment procurement increases.'):
            with self.subTest(text=text):
                self.assertEqual(self.assess(text)['kind'], 'BUSINESS')
        n = self.news('cooling', 'Liquid cooling equipment procurement increases.')
        self.assertFalse(macro.topics(n))
        self.assertIn(n['id'], {x['id'] for x in self.queue()})

    def test_inactive_company_aliases_do_not_activate_global_watchlist(self):
        self.assertEqual(self.assess('NVIDIA announces quarterly results.')['kind'], 'DISCOVERY')
        self.cfg['watchlist'] = [{'symbol': 'US:NVDA', 'name': 'NVIDIA'}]
        self.assertEqual(self.assess('NVIDIA announces quarterly results.')['kind'], 'COMPANY')

    def test_numeric_symbols_require_boundaries(self):
        for text in ('sz300499报告', '300499公布报告', '(300499) results'):
            self.assertEqual(self.assess(text)['kind'], 'COMPANY', text)
        for text in ('13004990 records', 'ABC300499X records', 'item_300499 records'):
            self.assertEqual(self.assess(text)['kind'], 'DISCOVERY', text)

    def test_us_tickers_and_ascii_names_do_not_match_substrings_or_common_words(self):
        self.cfg['watchlist'] = [{'symbol': 'US:CAT', 'name': 'Caterpillar'},
                                 {'symbol': 'US:NVDA', 'name': 'NVIDIA'}]
        for text in ('The cat is sleeping.', 'CAT scan equipment arrives.', 'NVIDIAish brand', 'NVDA2 earnings'):
            self.assertEqual(self.assess(text)['kind'], 'DISCOVERY', text)
        for text in ('$CAT releases results.', 'NYSE: CAT releases results.', 'NVDA releases results.', 'NVIDIA releases results.'):
            self.assertEqual(self.assess(text)['kind'], 'COMPANY', text)

    def test_fixed_assets_include_cross_asset_macro_without_dynamic_links(self):
        self.cfg['investment_policy'] = 'days_cash_v1'
        current = relevance.profiles(self.store, self.cfg, self.at)
        self.assertTrue({'BTC', 'ETH', 'GOLD', 'SILVER'} <= {p['symbol'] for p in current})
        self.assertEqual(self.assess('Federal Reserve interest rate decision.', current)['kind'], 'THEME')
        self.assertFalse(self.assess('Federal Reserve interest rate decision.', current)['priority_related'])
        self.assertEqual(self.assess('Bitcoin network upgraded successfully.', current)['kind'], 'ASSET')
        self.assertEqual(self.assess('Whether we meet together tomorrow.', current)['kind'], 'DISCOVERY')

    def test_power_equipment_vocabulary_does_not_claim_ai_transformer_exposure(self):
        self.cfg['watchlist'] = [{'symbol': 'sh601179', 'name': '中国西电'}]
        self.assertEqual(self.assess('AI Transformer architecture improves model accuracy.')['kind'], 'DISCOVERY')
        self.assertEqual(self.assess('Power transformer procurement increases.')['kind'], 'BUSINESS')

    def test_resolver_keeps_identity_only_and_excludes_archived_members(self):
        members = [{'symbol': 'US:NVDA', 'name': 'NVIDIA', 'membership': 'DYNAMIC', 'tier': 'ACTIVE', 'buy_eligible': True, 'cash_cents': 999},
                   {'symbol': 'US:TSLA', 'name': 'Tesla', 'membership': 'DYNAMIC', 'tier': 'ARCHIVED', 'buy_eligible': False}]
        with patch('ashare.universe.membership', return_value=members), patch('ashare.global_market.targets', return_value={}):
            current = relevance.profiles(self.store, self.cfg, self.at)
        self.assertEqual({p['symbol'] for p in current}, {'US:NVDA'})
        self.assertNotIn('cash_cents', str(current))

    def test_dynamic_cn_causal_theme_requires_current_admitted_link(self):
        member = {'symbol': 'sz300001', 'name': '测试公司', 'membership': 'DYNAMIC',
                  'tier': 'FOCUS', 'buy_eligible': True, 'domains': ['MONETARY']}
        link = {'theme': 'MONETARY', 'status': 'TRACKING', 'review_state': 'CURRENT', 'materiality': {'admitted': True}}
        item = {'asset': member['symbol'], 'links': [link]}
        with patch('ashare.universe.membership', return_value=[member]), patch('ashare.global_market.targets', return_value={}), patch('ashare.observation.view', return_value={'items': [item]}):
            current = relevance.profiles(self.store, self.cfg, self.at)
            self.assertEqual(self.assess('Federal Reserve interest rate decision.', current)['kind'], 'LINKED_THEME')
            link['review_state'] = 'DUE'
            current = relevance.profiles(self.store, self.cfg, self.at)
            self.assertEqual(self.assess('Federal Reserve interest rate decision.', current)['kind'], 'DISCOVERY')

    def test_company_priority_survives_high_volume_wire_and_discovery_reserve(self):
        direct = [self.news('company' + str(i), '高澜股份披露第' + str(i) + '项季度经营数据。') for i in range(8)]
        discoveries = [self.news('discovery' + str(i), 'FDA approves new therapy for disease ' + str(i) + '.', 'FDA药品与医疗监管') for i in range(3)]
        selected = self.queue(4)
        self.assertEqual(len(selected), 4)
        self.assertIn(selected[0]['id'], {n['id'] for n in direct})
        self.assertEqual(sum(n['id'] in {x['id'] for x in discoveries} for n in selected), 2)
        self.assertEqual(len({n['source'] for n in selected}), 2)

    def test_global_shocks_keep_discovery_slots_among_cross_source_company_news(self):
        self.cfg['investment_policy'] = 'days_cash_v1'
        sources = ('新浪财经快讯', 'Financial Times', '美联储', 'FDA药品与医疗监管')
        for i in range(12):
            self.news('company' + str(i), '高澜股份披露第' + str(i) + '项季度经营数据。', sources[i % len(sources)])
        shocks = [self.news('rates', 'Federal Reserve cuts rates by 25 basis points in a new interest rate decision.', '美联储'),
                  self.news('shutdown', 'Key oil shipping strait shutdown cuts global supply 20 percent after military conflict.')]
        self.news('routine', 'Public health meeting welcomes visitors to the annual conference.', 'WHO全球卫生')
        candidates = self.queue()
        shock_ids = {n['id'] for n in shocks}
        for n in candidates:
            if n['id'] in shock_ids:
                self.assertEqual(n['_news_relevance'], 'THEME')
                self.assertFalse(n['_news_related'])
        calls = []
        def model(prompt, *args):
            calls.append(prompt)
            return screen_deep(prompt)
        selected, info = news_triage.select(self.store, self.cfg, candidates, self.at, model)
        packet = json.loads(calls[0].split('<UNTRUSTED_NEWS>')[1].split('</UNTRUSTED_NEWS>')[0])
        self.assertEqual(info['reviewed'], 8)
        self.assertTrue(shock_ids <= {n['id'] for n in packet['news']})
        self.assertTrue(shock_ids <= {n['id'] for n in selected})
        self.assertEqual(len(selected), 4)

    def test_discovery_survives_queue_truncation_with_sixteen_company_sources(self):
        self.cfg['investment_policy'] = 'days_cash_v1'
        for i in range(80):
            self.news('company' + str(i), '高澜股份披露第' + str(i) + '项季度经营数据。', 'Fixture wire ' + str(i % 16))
        shocks = [self.news('rates', 'Federal Reserve cuts interest rates by 25 basis points.', 'Fixture wire 0'),
                  self.news('shutdown', 'Key oil shipping strait shutdown cuts global supply 20 percent after military conflict.', 'Fixture wire 1')]
        shock_ids = {n['id'] for n in shocks}
        candidates = self.queue()
        self.assertEqual(len(candidates), 64)
        self.assertTrue(shock_ids <= {n['id'] for n in candidates})
        status = dict(self.store.db.execute('SELECT news_id,status FROM macro_news'))
        self.assertTrue(all(status[ident] == 'PENDING' for ident in shock_ids))
        calls = []
        def model(prompt, *args):
            calls.append(prompt)
            return screen_deep(prompt)
        selected, info = news_triage.select(self.store, self.cfg, candidates, self.at, model)
        packet = json.loads(calls[0].split('<UNTRUSTED_NEWS>')[1].split('</UNTRUSTED_NEWS>')[0])
        self.assertEqual(info['reviewed'], 8)
        self.assertTrue(shock_ids <= {n['id'] for n in packet['news']})
        self.assertTrue(shock_ids <= {n['id'] for n in selected})
        self.assertEqual(len(selected), 4)

    def test_diversity_preserves_priority_after_one_discovery_instead_of_round_robin(self):
        def item(i, source, score, related):
            return {'id': str(i), 'title': 'Report ' + str(i), 'body': 'Report ' + str(i), 'source': source,
                    '_news_priority': score, '_news_related': related}
        rows = [item(1, 'same', 100, True), item(2, 'same', 99, True),
                item(3, 'other', 10, False), item(4, 'third', 9, False)]
        selected = news_triage.diverse(rows, 4)
        self.assertEqual([n['id'] for n in selected], ['1', '3', '2', '4'])

    def test_discoveries_survive_existing_complex_statement_cost_limit(self):
        for i in range(8):
            self.news('statement' + str(i), '高澜股份对第' + str(i) + '项季度经营数据作出表态。')
        discoveries = [self.news('health' + str(i), 'FDA approves new therapy ' + str(i) + ' for patients.', 'FDA药品与医疗监管') for i in range(2)]
        calls = []
        def model(prompt, *args):
            calls.append(prompt)
            return screen_deep(prompt)
        _, info = news_triage.select(self.store, self.cfg, self.queue(), self.at, model)
        self.assertEqual(len(calls), 1)
        self.assertEqual(info['reviewed'], 5)  # Three double-cost statements, two discoveries.
        packet = json.loads(calls[0].split('<UNTRUSTED_NEWS>')[1].split('</UNTRUSTED_NEWS>')[0])
        self.assertTrue({n['id'] for n in discoveries} <= {n['id'] for n in packet['news']})

    def test_official_source_or_celebrity_does_not_override_deep_evidence_gate(self):
        n = self.news('celebrity', 'Trump announces another general public speech on monetary matters.', '美联储')
        self.queue()
        def model(prompt, *args):
            result = screen_deep(prompt)
            result['items'][0].update(evidence='CLAIM', stage='COMMENTARY')
            return result
        selected, info = news_triage.select(self.store, self.cfg, self.queue(), self.at, model)
        self.assertFalse(selected)
        self.assertEqual(info['reviewed'], 1)
        self.assertEqual(news_triage.context(self.store, n, self.at)['decision'], 'WATCH')

    def test_local_relevance_never_enters_prompt_or_increases_call_budget(self):
        self.cfg['business_keywords']['sz300499'] = ['PRIVATE_PROFILE_MARKER']
        for i in range(10):
            self.news('direct' + str(i), '高澜股份披露季度报告第' + str(i) + '项经营数据。')
        for i in range(3):
            self.news('new' + str(i), 'FDA approves a new therapy ' + str(i) + ' for patients.', 'FDA药品与医疗监管')
        candidates = self.queue()
        calls = []
        def model(prompt, *args):
            calls.append((prompt, args))
            return screen_deep(prompt)
        selected, info = news_triage.select(self.store, self.cfg, candidates, self.at, model)
        self.assertEqual(len(calls), 1)
        self.assertEqual(info['reviewed'], 8)
        self.assertEqual(len(selected), 4)
        self.assertEqual(calls[0][1][-1], 90)
        for private in ('PRIVATE_PROFILE_MARKER', '_news_priority', '_news_related', '_news_relevance', 'watchlist', 'cash_cents'):
            self.assertNotIn(private, calls[0][0])
        packet = json.loads(calls[0][0].split('<UNTRUSTED_NEWS>')[1].split('</UNTRUSTED_NEWS>')[0])
        self.assertTrue(any(n['source'] == 'FDA药品与医疗监管' for n in packet['news']))
        for table in ('plans', 'paper_orders', 'dynamic_orders', 'macro_watchlist'):
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
