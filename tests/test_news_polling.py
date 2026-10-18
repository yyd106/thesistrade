import tempfile
import unittest
from unittest.mock import patch

from ashare import macro_sources, news_catalog, news_polling
from ashare.storage import Store


class NewsPollingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.at = '2026-10-18T06:00:04+00:00'

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_low_frequency_source_is_not_falsely_overdue_or_refreshed(self):
        news_polling.record(self.store, 'WHO全球卫生', self.at, 0)
        later = '2026-10-18T09:00:00+00:00'
        self.assertFalse(news_polling.due(self.store, 'WHO全球卫生', later))
        self.assertEqual(news_polling.next_check(self.store, 'WHO全球卫生'), '2026-10-18T12:00:00+00:00')
        with patch('ashare.macro_sources.FEEDS', [('WHO全球卫生', 'unused')]):
            result = macro_sources.collect(self.store, self.at, later, later, lambda _: self.fail('Not due'))
        self.assertEqual(result['skipped_sources'], ['WHO全球卫生'])
        row = next(r for r in news_catalog.coverage(self.store, later)['sources'] if r['source'] == 'WHO全球卫生')
        self.assertEqual(row['checked_at'], self.at)
        self.assertEqual(row['status'], 'OK')
        row = next(r for r in news_catalog.coverage(self.store, '2026-10-18T14:00:00+00:00')['sources'] if r['source'] == 'WHO全球卫生')
        self.assertEqual(row['status'], 'OVERDUE')

    def test_failed_source_backs_off_remains_failed_then_recovers(self):
        source = 'BLS美国劳工统计'
        raw = b'<rss><channel><item><title>CPI release</title><link>https://www.bls.gov/news.release/cpi.htm</link><pubDate>Sat, 17 Oct 2026 10:00:00 +0000</pubDate></item></channel></rss>'
        def failed(_):
            raise OSError('HTTP 403')
        with patch('ashare.macro_sources.FEEDS', [(source, 'https://www.bls.gov/feed')]):
            macro_sources.collect(self.store, self.at, self.at, self.at, failed)
            self.assertEqual(news_polling.next_check(self.store, source), '2026-10-18T07:00:04+00:00')
            result = macro_sources.collect(self.store, self.at, self.at, '2026-10-18T06:30:00+00:00', lambda _: self.fail('Backoff'))
            self.assertIn('等待重试', result['failures'][0])
            coverage = next(r for r in news_catalog.coverage(self.store, '2026-10-18T06:30:00+00:00')['sources'] if r['source'] == source)
            self.assertEqual(coverage['status'], 'FAILED')
            macro_sources.collect(self.store, self.at, self.at, '2026-10-18T07:00:04+00:00', failed)
            self.assertEqual(news_polling.next_check(self.store, source), '2026-10-18T09:00:04+00:00')
            result = macro_sources.collect(self.store, self.at, self.at, '2026-10-18T09:00:04+00:00', lambda _: raw)
            self.assertFalse(result['failures'])
            self.assertEqual(news_polling.next_check(self.store, source), '2026-10-18T09:30:00+00:00')
        self.assertEqual(self.store.db.execute('SELECT value FROM dynamic_state WHERE key=?', ('news_poll_failures:' + source,)).fetchone()[0], '0')

    def test_jitter_does_not_skip_next_half_hour_slot(self):
        news_polling.record(self.store, '美联储', self.at, 0)
        self.assertTrue(news_polling.due(self.store, '美联储', '2026-10-18T06:30:01+00:00'))

    def test_legacy_failed_source_gets_one_fresh_attempt_before_backoff(self):
        with self.store.db:
            self.store.db.execute('INSERT INTO dynamic_feed_checks VALUES(?,?,?,?,?)',
                                  ('欧洲央行', self.at, 'FAILED', 0, 'old TLS failure'))
        self.assertTrue(news_polling.due(self.store, '欧洲央行', '2026-10-18T06:30:00+00:00'))
        news_polling.record(self.store, '欧洲央行', '2026-10-18T06:30:00+00:00', 0, 'still failed')
        self.assertFalse(news_polling.due(self.store, '欧洲央行', '2026-10-18T07:00:00+00:00'))

    def test_new_official_feeds_validate_publisher_and_original_time(self):
        for source in ('中国人民银行', 'NVIDIA官方发布', 'OpenAI官方发布', 'SEC监管公告', 'Ethereum官方博客'):
            host = next(iter(news_catalog.article_hosts(source)))
            raw = f'<rss><channel><item><title>Official release</title><link>http://{host}/release</link><pubDate>Sat, 17 Oct 2026 00:00:00 +0000</pubDate></item></channel></rss>'.encode()
            row = macro_sources.parse_feed(raw, source)[0]
            self.assertEqual(row['url'], 'https://' + host + '/release')
            self.assertEqual(row['published_at'], '2026-10-17T00:00:00+00:00')
            with self.assertRaises(ValueError):
                macro_sources.parse_feed(raw.replace(host.encode(), b'www.federalreserve.gov'), source)
            with self.assertRaises(ValueError):
                macro_sources.parse_feed(raw.replace(b'pubDate', b'updated'), source)

    def test_official_relative_links_resolve_without_relaxing_host_checks(self):
        raw = b'<rss><channel><item><title>Policy release</title><link>/goutongjiaoliu/policy.html</link><pubDate>Sat, 17 Oct 2026 16:00:00 GMT</pubDate></item></channel></rss>'
        row = macro_sources.parse_feed(raw, '中国人民银行')[0]
        self.assertEqual(row['url'], 'https://www.pbc.gov.cn/goutongjiaoliu/policy.html')
        with self.assertRaises(ValueError):
            macro_sources.parse_feed(raw.replace(b'/goutongjiaoliu/policy.html', b'//evil.invalid/policy.html'), '中国人民银行')
