import copy
import tempfile
import unittest
from unittest.mock import patch

from ashare import digest, settings, strategy_freshness
from ashare.storage import Store


AT = '2026-10-15T12:00:00+00:00'


class StrategyFreshnessDigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.config = {**settings.DEFAULTS, 'data_dir': self.tmp.name, 'watchlist': [],
                       'deployment_role': 'cloud', 'mode': 'paper', 'model_enabled': False}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def summary(self):
        with patch('ashare.strategy_freshness.now', return_value=AT):
            return strategy_freshness.summary(self.store, self.config, AT)

    def build(self, day):
        with patch('ashare.digest.now', return_value=AT), patch('ashare.strategy_freshness.now', return_value=AT):
            return digest.build(self.store, self.config, day)

    def test_historical_digest_stops_at_day_end_and_does_not_replay_current_plans(self):
        result = self.build('2026-10-14')
        self.assertEqual(result['strategy_freshness']['as_of'], '2026-10-14T16:00:00+00:00')
        self.assertEqual(result['strategy_freshness']['summary']['plans']['status'], 'UNKNOWN_HISTORICAL_STATUS')
        text = digest.markdown(result)
        self.assertIn('## 本机在线', text)
        self.assertIn('## 策略新鲜度', text)
        self.assertIn('统计截至 2026-10-15 00:00', text)
        self.assertIn('历史时点的计划状态不可回放', text)

    def test_today_stops_at_generation_time_and_disabled_hides_section(self):
        result = self.build('2026-10-15')
        self.assertEqual(result['strategy_freshness']['as_of'], AT)
        self.config['strategy_freshness_enabled'] = False
        result = self.build('2026-10-15')
        self.assertIsNone(result['strategy_freshness'])
        text = digest.markdown(result)
        self.assertNotIn('## 策略新鲜度', text)
        self.assertIn('## 本机在线', text)

    def test_unknown_coverage_is_not_zero_interruptions_or_zero_fills(self):
        self.config['deployment_role'] = 'research'
        data = self.summary()
        text = '\n'.join(digest.freshness_markdown({'status': 'OK', 'summary': data}))
        self.assertIn('整窗未知，覆盖不足', text)
        self.assertIn('成交覆盖未知', text)
        self.assertIn('成交情况未知', text)
        self.assertNotIn('中断 0 次', text)
        self.assertNotIn('本期无成交', text)

    def test_separate_ages_routes_exit_groups_and_incomplete_window(self):
        data = self.summary()
        data['heartbeat']['windows']['24h'].update(coverage_complete=True, observed_seconds=86400, unknown_seconds=0,
            interruption_count=0, interruption_seconds=0, longest_interruption_seconds=0)
        data['heartbeat']['windows']['7d'].update(observed_seconds=86400, unknown_seconds=518400,
            interruption_count=0, interruption_seconds=0, longest_interruption_seconds=0)
        plan = data['plans']['routes']['watchlist']
        plan.update(time_valid_count=1, expired_active_count=2, over_max_age_count=2, min_remaining_seconds=3600)
        plan['plan_age'].update(known=1, mean_seconds=3600, max_seconds=3600)
        plan['research_age'].update(known=1, mean_seconds=72000, max_seconds=72000)
        for group, seconds in [('BUY', 3600), ('SELL', 7200), ('PROTECTIVE_EXIT', 60)]:
            row = data['fills']['routes']['watchlist'][group]
            row.update(count=1)
            row['plan_age'].update(known=1, mean_seconds=seconds, max_seconds=seconds)
            row['research_age'].update(known=1, mean_seconds=72000, max_seconds=72000)
        text = '\n'.join(digest.freshness_markdown({'status': 'OK', 'summary': data}))
        for phrase in ('过去 24 小时：中断 0 次', '过去 7 天：整窗未知', '已覆盖 24.0 小时内中断 0 次',
                       '计划签发龄', '原研究龄', '20.0 小时', '自选股 / 买入', '自选股 / 普通卖出',
                       '自选股 / 保护性退出', '全球资产', '动态事件', '仍标活跃但已过期', '本期无成交'):
            self.assertIn(phrase, text)
        # Extra notes follow the complete three-route plan table, not its middle.
        self.assertLess(text.index('| 动态事件 |'), text.index('超过计划签发龄上限'))
        before = copy.deepcopy(data)
        digest.freshness_markdown({'status': 'OK', 'summary': data})
        self.assertEqual(data, before)

    def test_statistics_failure_does_not_abort_other_digest_sections(self):
        with patch('ashare.strategy_freshness.summary', side_effect=ValueError('synthetic')):
            result = self.build('2026-10-15')
        self.assertEqual(result['strategy_freshness']['status'], 'UNAVAILABLE')
        text = digest.markdown(result)
        self.assertIn('统计暂不可用', text)
        self.assertIn('## 本机在线', text)
        self.assertIn('## 任务运行', text)


if __name__ == '__main__':
    unittest.main()
