import json
import unittest
from unittest.mock import patch
import test_workflow as workflow_fixtures
from ashare import digest, governance
from ashare.demo import SYMBOL
from ashare.storage import normalize_time

DAY = '2026-09-15'


def utc(local_time):
    return normalize_time(f'{DAY}T{local_time}:00+08:00')


class DigestTests(unittest.TestCase):
    setUp = workflow_fixtures.WorkflowTests.setUp
    tearDown = workflow_fixtures.WorkflowTests.tearDown
    plan = workflow_fixtures.WorkflowTests.plan

    def seed_day(self):
        self.plan()
        db = self.store.db
        for n in range(20):
            self.store.record_attempt('tencent_quotes', SYMBOL, 'OK', '', at=utc('10:00'))
        for n in range(6):
            self.store.record_attempt('cninfo_catalog', SYMBOL, 'FAILED', 'URLError: <urlopen error [Errno 8] nodename nor servname provided, or not known>', at=utc('18:05'))
        decision = {'summary': '维持现有持仓', 'decisions': [{'key': 'watchlist:' + SYMBOL, 'name': '合成测试', 'symbol': SYMBOL, 'action': 'ALLOW', 'target_bps': 1000}]}
        earlier = {'decisions': [{'key': 'watchlist:' + SYMBOL, 'name': '合成测试', 'symbol': SYMBOL, 'action': 'PAUSE', 'target_bps': 0}]}
        with db:
            db.execute('INSERT INTO portfolio_decisions VALUES(?,?,?,?,?,?)', ('d0', normalize_time('2026-09-14T20:00:00+08:00'), utc('23:00'), 'SUPERSEDED', 1, json.dumps(earlier)))
            db.execute('INSERT INTO portfolio_decisions VALUES(?,?,?,?,?,?)', ('d1', utc('09:40'), utc('21:40'), 'ACTIVE', 2, json.dumps(decision)))
            db.execute('INSERT INTO portfolio_runs VALUES(?,?,?,?,?,?)', ('r1', utc('09:38'), utc('09:40'), 'SUCCEEDED', None, '{}'))
            db.execute('INSERT INTO cloud_outbox VALUES(?,?,?,?,?)', ('d0', normalize_time('2026-09-14T20:00:30+08:00'), 'SENT', json.dumps({'pruned': True, 'sha256': 'x', 'bytes': 5000000}, separators=(',', ':')), None))
            db.execute('INSERT INTO cloud_outbox VALUES(?,?,?,?,?)', ('d1', utc('09:41'), 'SENT', json.dumps({'pruned': True, 'sha256': 'y', 'bytes': 800000}, separators=(',', ':')), None))
            db.execute('INSERT INTO jobs(id,kind,scheduled_at,started_at,finished_at,status,attempts,result_json,error) VALUES(?,?,?,?,?,?,?,?,?)',
                       ('cycle:x', 'cycle', utc('18:00'), utc('18:00'), utc('22:19'), 'FAILED', 1, None, 'URLError'))
            db.execute('INSERT INTO jobs(id,kind,scheduled_at,started_at,finished_at,status,attempts,result_json,error) VALUES(?,?,?,?,?,?,?,?,?)',
                       ('review:x', 'review', utc('19:30'), utc('19:30'), utc('19:31'), 'DONE', 1, None, None))
            account = {'positions': {SYMBOL: {'qty': 1000, 'market_value_cents': 1000000, 'cost_cents': 990000}}, 'global_positions': {}, 'dynamic_positions': {}}
            db.execute('INSERT INTO equity_marks VALUES(?,?,?,?,?,?,?,?)', ('m0', normalize_time('2026-09-14T15:00:00+08:00'), utc('00:00'), 9000000, 10000000, 0, 1, json.dumps(account)))
            db.execute('INSERT INTO equity_marks VALUES(?,?,?,?,?,?,?,?)', ('m1', utc('15:00'), utc('15:01'), 9000000, 10031500, 0, 1, json.dumps(account)))
            review = {'analysis': {'summary': '48小时损益为正，程序检查全部通过。'}, 'routing': [{'lesson': 0, 'to': 'proposal_draft', 'id': 'p1'}],
                      'consistency_checks': [{'check': 'CHECK_SYNC_STALE', 'status': 'PASS'}, {'check': 'CHECK_BUY_OUTSIDE_PLAN_BAND', 'status': 'FAIL'}]}
            db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)', ('rv1', normalize_time('2026-09-14T19:30:00+08:00'), utc('19:30'), 1, utc('19:32'), 'f', 'SUCCEEDED', json.dumps(review)))
        governance.draft_proposal(self.store, source='review:rv1', kind='RULE', target='自选股 · 退出规则', title='趋势失效后退出',
                                  payload={'observations': []}, dedupe_key='t1', at=utc('19:32'))
        log = self.store.root / 'workflow' / 'changes'
        log.mkdir(parents=True, exist_ok=True)
        (log / 'config-changes.jsonl').write_text(json.dumps({'key': 'model_name', 'class': 'STRATEGY', 'before': None, 'after': 'gpt-6-astra',
                                                            'reason': '固定模型', 'approved_by': 'Dean', 'at': utc('15:52')}, ensure_ascii=False) + '\n', encoding='utf-8')

    def build(self):
        with patch('ashare.digest.now', return_value=normalize_time('2026-09-15T23:50:00+08:00')):
            return digest.build(self.store, self.cfg, DAY)

    def test_day_summary_covers_every_stage_from_primary_records(self):
        self.seed_day()
        d = self.build()
        self.assertEqual((d['collection']['failed'], d['collection']['network_errors']), (6, 6))
        self.assertGreaterEqual(d['collection']['attempts'], 26)
        stock = d['research']['stocks'][0]
        self.assertEqual((stock['research'], stock['plan']), ('WATCH', 'PAPER_TRADE'))
        self.assertTrue(stock['source'].startswith('研究 '))
        p = d['portfolio']
        self.assertEqual(p['latest_actions'], {'ALLOW': 1})
        self.assertEqual(p['changes'][0]['from'], 'PAUSE');self.assertEqual(p['changes'][0]['to'], 'ALLOW')
        self.assertEqual(p['publications'], {'SENT': 1});self.assertEqual(p['publication_bytes']['last'], 800000)
        self.assertEqual(d['execution']['equity_end_cents'] - d['execution']['equity_start_cents'], 31500)
        self.assertEqual(d['execution']['positions'][0]['name'], '合成测试')
        self.assertEqual((d['review']['checks_passed'], d['review']['checks_total']), (1, 2))
        self.assertEqual(d['review']['routing'], {'proposal_draft': 1})
        self.assertEqual(d['governance']['config_changes'][0]['key'], 'model_name')
        self.assertEqual(len(d['governance']['proposals_new']), 1)

    def test_flags_point_at_failures_gaps_and_strategy_changes(self):
        self.seed_day()
        flags = '\n'.join(self.build()['flags'])
        self.assertIn('抓取失败率', flags);self.assertIn('网络类错误 6 次', flags)
        self.assertIn('资料研究任务未完成 1 次', flags)
        self.assertIn('组合策略发布最长间隔', flags)  # 09:41 until the day's end
        self.assertIn('复盘程序检查未通过：买入价超出计划区间', flags)
        self.assertIn('策略类设置被修改：model_name', flags)

    def test_markdown_and_rollup_are_written_without_model_calls(self):
        self.seed_day()
        with patch('ashare.digest.now', return_value=normalize_time('2026-09-16T23:50:00+08:00')):
            result = digest.write(self.store, self.cfg, DAY)
            text = (self.store.root / result['report']).read_text(encoding='utf-8')
            for heading in ('## 需要关注', '## 抓取', '## 研究', '## 组合策略', '## 云端执行', '## 复盘', '## 调整与治理', '## 任务运行'):
                self.assertIn(heading, text)
            self.assertIn('| 合成测试 | 可观察 | 可开仓 |', text)
            self.assertNotIn('**', text)
            summary = digest.rollup(self.store, self.cfg, '2026-09-14', '2026-09-16')
        self.assertEqual(summary['days'], 3)
        rolled = (self.store.root / summary['report']).read_text(encoding='utf-8')
        self.assertIn('| 2026-09-15 | 6/', rolled)
        self.assertIn('2026-09-15 设置 model_name', rolled)
        self.assertIn('2026-09-15 新提案', rolled)

    def test_scheduled_on_the_research_node_and_written_by_the_job(self):
        from ashare.scheduler import schedule_due
        from ashare.workflow import execute
        scan = normalize_time('2026-09-15T23:00:00+08:00')
        with self.store.db:self.store.db.execute("INSERT OR REPLACE INTO service_state VALUES('last_scan',?)", (scan,))
        items = schedule_due(self.store, {**self.cfg, 'deployment_role': 'research'}, normalize_time('2026-09-15T23:55:00+08:00'))
        self.assertIn(('digest', normalize_time('2026-09-15T23:50:00+08:00')), items)
        with self.store.db:self.store.db.execute("INSERT OR REPLACE INTO service_state VALUES('last_scan',?)", (scan,))
        self.assertNotIn('digest', {k for k, _ in schedule_due(self.store, {**self.cfg, 'deployment_role': 'cloud'}, normalize_time('2026-09-15T23:55:00+08:00'))})
        result = execute(self.cfg, 'digest', use_model=False, end=normalize_time('2026-09-15T23:50:00+08:00'))
        self.assertEqual(result['day'], DAY)
        self.assertTrue((self.store.root / 'workflow' / 'digests' / (DAY + '.md')).exists())

    def test_a_broken_section_is_reported_not_hidden(self):
        self.seed_day()
        with patch('ashare.digest.execution', side_effect=RuntimeError('equity_marks unreadable')), \
             patch('ashare.digest.now', return_value=normalize_time('2026-09-15T23:50:00+08:00')):
            d = digest.build(self.store, self.cfg, DAY)
            text = digest.markdown(d)
        self.assertIn('“云端执行”一节生成失败', d['flags'][0])
        self.assertIn('本节生成失败（RuntimeError: equity_marks unreadable）', text)
        self.assertIn('## 复盘', text)

    def test_empty_database_still_produces_a_page(self):
        with patch('ashare.digest.now', return_value=normalize_time('2026-09-15T23:50:00+08:00')):
            text = digest.markdown(digest.build(self.store, self.cfg, DAY))
        self.assertIn('当天没有成交', text);self.assertIn('当天没有复盘记录', text)


if __name__ == '__main__':
    unittest.main()
