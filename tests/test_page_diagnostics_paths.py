"""Page-only observations never ride the traditional strategy display path."""
import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from ashare import cloud_sync, cloud_runtime, dashboard, dynamic, macro, page_display
from ashare.finance import PaperLedger
from ashare.storage import Store
from test_macro_relevance import event_fixture

AT = '2026-10-09T10:00:00+00:00'


class PageDiagnosticsPathsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        PaperLedger(self.store).initialize()
        self.config = {'data_dir': self.tmp.name, 'deployment_role': 'research', 'mode': 'paper',
                       'scheduler_enabled': False, 'model_enabled': False, 'watchlist': [],
                       'investment_policy': None, 'industry_enabled': False, 'portfolio_strategy': None,
                       'quote_max_age_seconds': 180, 'collection_times': [], 'slot_times': [],
                       'review_time': '19:30', 'slot_execution_mode': 'RULES'}
        event = event_fixture('page-only', published='2026-10-03T10:00:00+00:00')
        with self.store.db:
            self.store.db.execute('''INSERT INTO dynamic_news
                (id,source,url,published_at,first_seen_at,title,body,cluster_id,revision_of,status,raw_path)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (event['news_id'], 'Synthetic', 'https://example.com/news',
                    event['published_at'], event['published_at'], 'Synthetic policy', 'Synthetic public policy source.',
                    'synthetic', None, 'NEW', 'synthetic'))
            self.store.db.execute('INSERT INTO macro_events VALUES(?,?,?,?,?,?,?)',
                (event['id'], event['news_id'], event['created_at'], event['basis'], event['theme'], event['status'], json.dumps(event['analysis'])))
            market = {'points': [{'date': d, 'value': v} for d, v in [('2026-10-02', 4), ('2026-10-04', 4.1), ('2026-10-05', 4.2), ('2026-10-06', 4.3)]],
                      'url': 'https://example.com/series', 'series': 'DGS10', 'raw_path': 'synthetic'}
            self.store.db.execute('INSERT INTO macro_markets VALUES(?,?,?,?,?)', ('US10Y', '2026-10-08T10:00:00+00:00', 'OK', json.dumps(market), None))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def observation(self, result):
        return result['followup_items'][0]['reactions'][0]['observation']

    def test_legacy_strategy_display_skips_diagnostic_calculation_end_to_end(self):
        with patch('ashare.dashboard.now', return_value=AT), \
             patch('ashare.macro_presentation.diagnostic_observation', side_effect=AssertionError('legacy display must not calculate page observations')):
            legacy = cloud_sync.display_packet(self.config)
        self.assertIsNone(self.observation(legacy['dynamic']['global']))
        self.assertNotIn('diagnostic_only', json.dumps(legacy))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM macro_observations').fetchone()[0], 0)
        # The default local page and the independently gated v2 channel still get
        # the same explicitly labelled calculation without registering evidence.
        with patch('ashare.dashboard.now', return_value=AT):
            local = dashboard.status(self.config)
        independent = page_display.collect(self.store, self.config, AT)
        self.assertEqual(independent['version'], 'review-news-display-v2')
        self.assertTrue(self.observation(local['dynamic']['global'])['diagnostic_only'])
        self.assertEqual(self.observation(local['dynamic']['global']), self.observation(independent['macro']))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM macro_observations').fetchone()[0], 0)

    def test_existing_registered_observation_is_identical_in_both_display_paths(self):
        self.assertEqual(macro.measure(self.store, AT), 1)
        before = list(self.store.db.iterdump())
        enabled = dynamic.view(self.store, self.config, AT)
        with patch('ashare.macro_presentation.diagnostic_observation', side_effect=AssertionError('registered result wins')):
            disabled = dynamic.view(self.store, self.config, AT, page_diagnostics=False)
        self.assertEqual(enabled, disabled)
        self.assertNotIn('diagnostic_only', json.dumps(disabled))
        self.assertEqual(before, list(self.store.db.iterdump()))

    def test_disabling_diagnostics_preserves_all_other_macro_fields(self):
        enabled = macro.view(self.store, AT, self.config)
        disabled = macro.view(self.store, AT, self.config, page_diagnostics=False)
        expected = copy.deepcopy(enabled)
        for key in ('items', 'archived_items', 'followup_items', 'history_items'):
            for event in expected[key]:
                for reaction in event['reactions']:
                    if (reaction.get('observation') or {}).get('diagnostic_only'):
                        reaction['observation'] = None
        self.assertEqual(disabled, expected)

    def enable_display(self, bundle_id=None):
        with self.store.db:
            cloud_runtime.put(self.store, 'remote_features', [page_display.FEATURE])
            if bundle_id is not None:
                cloud_runtime.put(self.store, 'last_upload', {'bundle_id': bundle_id, 'at': AT})

    @staticmethod
    def display_ack(_config, path, body):
        assert path == '/api/sync/reviews'
        packet = body['page_display']
        return {'status': 'ACCEPTED', 'page_display': {'status': 'UPDATED',
                'generated_at': packet['generated_at'], 'content_hash': packet['content_hash']}}

    def test_new_strategy_upload_resends_same_content_and_restores_independent_page(self):
        self.enable_display()
        later = '2026-10-09T10:01:00+00:00'
        refreshed = '2026-10-09T10:02:00+00:00'
        with tempfile.TemporaryDirectory() as cloud_dir:
            cloud = Store(cloud_dir)
            sent = []
            def transport(_config, path, body):
                self.assertEqual(path, '/api/sync/reviews')
                packet = body['page_display'];sent.append(copy.deepcopy(packet))
                with cloud.db:
                    return page_display.receive(cloud, packet, packet['generated_at'])
            try:
                with patch('ashare.cloud_sync.request', side_effect=transport):
                    with patch('ashare.page_display.now', return_value=AT):
                        cloud_sync.deliver_page_display(self.store, self.config)
                    with patch('ashare.page_display.now', return_value=later):
                        self.assertIsNone(cloud_sync.deliver_page_display(self.store, self.config))
                    self.assertEqual(len(sent), 1)  # A clock tick alone still never sends.
                    legacy = {'dynamic': {'global': macro.view(self.store, later, self.config, page_diagnostics=False)}}
                    self.assertIsNone(self.observation(page_display.apply(cloud, copy.deepcopy(legacy))['dynamic']['global']))
                    upload = {'bundle_id': 'synthetic-strategy-1', 'at': later}
                    with self.store.db:
                        cloud_runtime.put(self.store, 'last_upload', upload)
                    with patch('ashare.page_display.now', return_value=refreshed):
                        cloud_sync.deliver_page_display(self.store, self.config)
                    self.assertEqual(len(sent), 2)
                    self.assertEqual(sent[0]['content_hash'], sent[1]['content_hash'])
                    self.assertEqual(cloud_runtime.value(self.store, 'page_display_sent_upload'), upload['bundle_id'])
                    self.assertEqual(cloud_runtime.value(self.store, 'last_upload'), upload)
                    shown = page_display.apply(cloud, copy.deepcopy(legacy))
                    self.assertTrue(self.observation(shown['dynamic']['global'])['diagnostic_only'])
                    with patch('ashare.page_display.now', return_value='2026-10-09T10:03:00+00:00'):
                        self.assertIsNone(cloud_sync.deliver_page_display(self.store, self.config))
                    self.assertEqual(len(sent), 2)
            finally:
                cloud.close()

    def test_upload_during_collection_remains_pending_for_the_next_display_send(self):
        self.enable_display('before-collection')
        collect = page_display.collect
        def concurrent_collect(store, config):
            packet = collect(store, config, AT)
            with store.db:
                cloud_runtime.put(store, 'last_upload', {'bundle_id': 'during-collection', 'at': AT})
            return packet
        with patch('ashare.cloud_sync.request', side_effect=self.display_ack) as request:
            with patch('ashare.page_display.collect', side_effect=concurrent_collect):
                cloud_sync.deliver_page_display(self.store, self.config)
            self.assertEqual(cloud_runtime.value(self.store, 'page_display_sent_upload'), 'before-collection')
            with patch('ashare.page_display.now', return_value='2026-10-09T10:01:00+00:00'):
                cloud_sync.deliver_page_display(self.store, self.config)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(cloud_runtime.value(self.store, 'page_display_sent_upload'), 'during-collection')

    def test_mismatched_ack_does_not_mark_the_upload_stamp(self):
        self.enable_display('new-upload')
        with self.store.db:
            cloud_runtime.put(self.store, 'page_display_sent_hash', 'previous-hash')
            cloud_runtime.put(self.store, 'page_display_sent_upload', 'previous-upload')
        def mismatch(config, path, body):
            result = self.display_ack(config, path, body)
            result['page_display']['content_hash'] = 'wrong-ack-hash'
            return result
        with patch('ashare.page_display.now', return_value=AT), patch('ashare.cloud_sync.request', side_effect=mismatch):
            with self.assertRaisesRegex(ValueError, '回执与本次发送不一致'):
                cloud_sync.deliver_page_display(self.store, self.config)
        self.assertEqual(cloud_runtime.value(self.store, 'page_display_sent_upload'), 'previous-upload')
        self.assertEqual(cloud_runtime.value(self.store, 'page_display_sent_hash'), 'previous-hash')
        self.assertEqual(cloud_runtime.value(self.store, 'last_upload')['bundle_id'], 'new-upload')


if __name__ == '__main__':
    unittest.main()
