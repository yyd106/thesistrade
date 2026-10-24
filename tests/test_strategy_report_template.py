"""Readable research-to-trade cards use synthetic retained evidence only."""
import json
import unittest

from ashare import strategy_export as export
from tests import test_strategy_export as fixtures


class StrategyReportTemplateTests(unittest.TestCase):
    setUp = fixtures.StrategyExportTests.setUp
    insert = fixtures.StrategyExportTests.insert
    decision = fixtures.StrategyExportTests.decision
    plan = fixtures.StrategyExportTests.plan
    archive = fixtures.StrategyExportTests.archive

    def test_frozen_stock_card_connects_evidence_rules_and_portfolio_versions(self):
        self.insert('studies', id='study1', symbol='sh600000', created_at=fixtures.BEFORE,
                    model_status='SUCCEEDED', result_json={'stocks': [{
                        'symbol': 'sh600000', 'action': 'WATCH', 'analysis': '订单先变成收入，再验证现金回收',
                        'facts': [{'evidence_id': 'DOC:1', 'quote': '合成证据原文'}],
                        'hypothesis_test': {'claim': '收入增长是否转化为经营现金流', 'deadline': '2026-11-01'},
                        'decision': {'inclination': '等待验证现金流', 'pricing': '估值尚待复核',
                                     'key_evidence': [{'evidence_id': 'DOC:1', 'implication': '订单不能直接当作利润'}],
                                     'trigger': '后续披露验证回款', 'invalidation': '应收增长超过收入'},
                        'counterpoints': ['回款可能延期'], 'next_checks': ['下次财报验证回款'],
                        'missing_fields': ['缺少分部现金流']}]})
        plan = self.plan(thesis='冻结的研究依据')
        payload = json.loads(plan['payload_json'])
        payload.update(levels={'buy_low_cents': 1000, 'buy_high_cents': 1200,
                               'stop_cents': 900, 'sell_cents': 1400}, max_stock_pct=20,
                       formula='保留的原始程序公式', holding_days=5, recheck_hours=1,
                       risk_parameters={'paper_stop_loss_bps': 600})
        plan['payload_json'] = json.dumps(payload)
        self.decision('first', fixtures.START, frozen=plan, sent=True, action='ALLOW', target=1000)
        self.decision('second', fixtures.AT, frozen=plan, action='HOLD', reason='继续观察回款')
        _, _, archive, _ = self.archive()
        report = archive.read('研究策略.md').decode().split('## 原始研究记录')[0]
        overview = archive.read('总览.md').decode()
        self.assertEqual(report.count('### 1. 测试公司（sh600000）'), 1)
        for value in ('收入增长是否转化为经营现金流', '合成证据原文', '订单不能直接当作利润',
                      '估值尚待复核', '应收增长超过收入', '下次财报验证回款', '缺少分部现金流',
                      '入场价格区间：10 元 — 12 元', '止损价 / 止盈价：9 元 — 14 元',
                      '计划仓位上限：20%', 'first', 'second', '保持 / 8%', '继续观察回款'):
            self.assertIn(value, report)
        self.assertIn('统一研究依据截止时间 | 未记录，不能补推', report)
        self.assertIn('未纳入模拟成交明细', report)
        self.assertIn('引用 2 次；此处展开最后一次', report)
        self.assertIn('阅读入口', overview)
        self.assertIn('sh600000', overview)
        self.assertIn('窗口内最后一次生成决定', overview)
        self.assertIn('测试公司（sh600000）', archive.read('交易策略变更.md').decode())

    def test_missing_history_does_not_fill_historical_card_from_current_plan(self):
        self.plan(thesis='不应补入历史的当前研究')
        self.decision('unfrozen', fixtures.START)
        _, _, archive, _ = self.archive()
        report = archive.read('研究策略.md').decode()
        first = report.split('\n### 1.', 1)[1].split('## 原始研究记录', 1)[0]
        self.assertIn('历史研究缺口', first)
        self.assertNotIn('不应补入历史的当前研究', first)
        self.assertIn('入场价格区间：未记录 — 未记录', first)
        self.assertIn('研究失效条件 | 未记录，不能补推', first)
        self.assertIn('不应补入历史的当前研究', report.split('## 原始研究记录')[1])

    def test_frozen_identity_and_source_time_must_match_decision(self):
        plan = self.plan(at=fixtures.AT)
        self.decision('earlier', fixtures.START, frozen=plan)
        data = self.archive()[3]
        self.assertIsNone(export._frozen_source(data, data['decisions'][0],
                                              data['decisions'][0]['payload']['decisions'][0])[0])
        plan['activated_at'] = fixtures.START
        plan['symbol'] = 'sz000001'
        self.decision('wrong-stock', fixtures.AT, frozen=plan)
        data = self.archive()[3]
        card = next(c for c in export._report_cards(data) if any(d[0]['record']['id'] == 'wrong-stock' for d in c['decisions']))
        self.assertEqual(card['origin'], 'missing')

    def test_same_time_decisions_choose_last_source_order_for_detailed_card(self):
        earlier = self.plan('earlier-plan', thesis='较早版本的研究')
        later = self.plan('later-plan', thesis='同秒最后决定使用的研究', at='2026-10-09T01:00:00+08:00')
        self.decision('earlier-decision', fixtures.AT, source='earlier-plan', frozen=earlier)
        self.decision('later-decision', fixtures.AT, source='later-plan', frozen=later)
        _, _, archive, data = self.archive()
        self.assertLess(data['decisions'][0]['source_order'], data['decisions'][1]['source_order'])
        card = archive.read('研究策略.md').decode().split('\n### 1.', 1)[1].split('## 原始研究记录', 1)[0]
        self.assertIn('同秒最后决定使用的研究', card)
        self.assertIn('later-decision', card)
        self.assertNotIn('较早版本的研究', card)
        self.assertNotIn('earlier-decision', card)

    def test_later_or_different_stock_study_is_not_used_for_past_plan(self):
        self.insert('studies', id='study1', symbol='sh600000', created_at=fixtures.AT,
                    model_status='SUCCEEDED', result_json={'stocks': [
                        {'symbol': 'sh600000', 'analysis': '未来研究不得回填'},
                        {'symbol': 'sz000001', 'analysis': '其他股票不得混用'}]})
        plan = self.plan(thesis='当时计划中已有的依据')
        self.decision('first', fixtures.START, frozen=plan)
        _, _, archive, _ = self.archive()
        card = archive.read('研究策略.md').decode().split('\n### 1.', 1)[1].split('\n### 2.', 1)[0].split('## 原始研究记录', 1)[0]
        self.assertIn('关联研究未留存或晚于计划形成时间', card)
        self.assertIn('当时计划中已有的依据', card)
        self.assertNotIn('未来研究不得回填', card)
        self.assertNotIn('其他股票不得混用', card)

    def test_global_and_event_cards_preserve_price_units_and_news_metadata(self):
        self.insert('global_plans', id='global1', symbol='GOLD', created_at=fixtures.START,
                    valid_until=fixtures.AT, fingerprint='fp', status='ACTIVE',
                    payload_json={'analysis_as_of': fixtures.START, 'analysis': {'stance': 'WAIT', 'thesis': '合成黄金研究'},
                                  'levels': {'buy_low_micros': 2_000_000_000, 'buy_high_micros': 2_020_000_000,
                                             'stop_micros': 1_900_000_000, 'sell_micros': 2_200_000_000}})
        self.insert('dynamic_news', id='news1', source='合成来源', title='合成新闻标题',
                    url='https://example.test/news', published_at=fixtures.BEFORE, first_seen_at=fixtures.START)
        self.insert('dynamic_cases', id='case1', symbol='sz000001', name='事件公司', news_id='news1',
                    created_at=fixtures.START, expires_at=fixtures.AT, status='RESEARCH',
                    analysis_json={'impact': '合成冲击', 'business_link': '合成传导', 'invalidation': '合成反证'},
                    plan_json={'buy_low_cents': 990, 'buy_high_cents': 1010, 'stop_bps': 600, 'take_profit_bps': 1000})
        report = self.archive()[2].read('研究策略.md').decode().split('## 原始研究记录')[0]
        for value in ('2000 美元 — 2020 美元', '1900 美元 — 2200 美元', '9.9 元 — 10.1 元',
                      '成本止损基点：600', '合成传导', '合成新闻标题', 'https://example.test/news'):
            self.assertIn(value, report)
        self.assertIn('本窗口未找到引用此版本的匹配冻结组合', report)
        self.assertEqual(export._price_rule({'buy_low_micros': 123456789012, 'buy_high_micros': 123456789013},
                                          'buy_low_micros', 'buy_high_micros', 1_000_000, '美元'),
                         '123456.789012 美元 — 123456.789013 美元')

    def test_standalone_research_and_untrusted_text_are_visible_but_escaped(self):
        self.insert('studies', id='standalone', symbol='sz000001', created_at=fixtures.START,
                    model_status='SUCCEEDED', result_json={'stocks': [
                        {'symbol': 'sz000001', 'analysis': '<script>unsafe</script> [link](bad)',
                         'missing_fields': ['未进入组合']} ]})
        report = self.archive()[2].read('研究策略.md').decode().split('## 原始研究记录')[0]
        self.assertIn('名称未记录（sz000001）', report)
        self.assertIn('未进入组合', report)
        self.assertNotIn('<script>', report)
        self.assertIn('&lt;script&gt;', report)
        self.assertIn(r'\[link\]\(bad\)', report)


if __name__ == '__main__':
    unittest.main()
