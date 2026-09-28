import json
import tempfile
import unittest
from unittest.mock import patch

import test_cloud_sync as fixtures
from ashare import notices, cloud_sync as sync, cloud_ledger as ledger, cloud_runtime as runtime, quote_health as qh
from ashare.storage import Store

AT = '2026-09-28T07:40:00+00:00'
BODY = '建议购买付费行情源：两周内主接口失败累计 190 分钟，其中持仓 35 分钟没有报价。'


class NoticeRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.tmp2 = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        self.store, self.cloud = Store(self.tmp.name), Store(self.tmp2.name)

    def tearDown(self):
        self.store.close(), self.cloud.close()
        self.tmp.cleanup(), self.tmp2.cleanup()

    def test_create_checks_content_and_repeats_nothing(self):
        n = notices.create(self.store, title='购买稳定行情源', body=BODY, kind='DECISION', author='claude', at=AT,
                           payload={'deadline': '2026-10-01T12:00:00+08:00'})
        self.assertEqual((n['status'], n['kind'], n['author']), ('OPEN', 'DECISION', 'claude'))
        self.assertRegex(n['id'], r'^N-20260928-1540-[0-9a-f]{6}$')
        self.assertEqual(json.loads(n['payload_json'])['deadline'], '2026-10-01T04:00:00+00:00')
        again = notices.create(self.store, title='购买稳定行情源', body=BODY, kind='DECISION', author='claude', at=AT)
        self.assertEqual(again['id'], n['id'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM notices').fetchone()[0], 1)
        for bad in ({'title': '短'}, {'body': '太短'}, {'kind': 'ALERT'}, {'author': 'dean'}):
            with self.assertRaises(ValueError):
                notices.create(self.store, **{'title': '标题', 'body': BODY, 'kind': 'INFO', 'author': 'agent', **bad})

    def test_each_kind_takes_only_its_own_answers_once(self):
        info = notices.create(self.store, title='回撤风控', body=BODY, kind='INFO', author='program', at=AT)
        decision = notices.create(self.store, title='购买行情源', body=BODY, kind='DECISION', at=AT)
        veto = notices.create(self.store, title='止损改为7%', body=BODY, kind='VETO', at=AT)
        with self.assertRaises(ValueError):
            notices.decide(self.store, info['id'], 'APPROVE', 'dean')
        self.assertEqual(notices.decide(self.store, info['id'], 'ACK', 'dean')['status'], 'ACKED')
        self.assertEqual(notices.decide(self.store, info['id'], 'ACK', 'dean')['status'], 'ACKED')  # a retried click
        row = notices.decide(self.store, decision['id'], 'APPROVE', 'dean')
        self.assertEqual((row['status'], json.loads(row['payload_json'])['decided_by']), ('APPROVED', 'dean'))
        with self.assertRaises(ValueError):
            notices.decide(self.store, decision['id'], 'REJECT', 'dean')  # a decision is not silently reversed
        self.assertEqual(notices.decide(self.store, veto['id'], 'VETO', 'dean')['status'], 'VETOED')
        self.assertEqual(notices.open_for_display(self.store), [])

    def test_cloud_keeps_content_and_returns_answers_to_the_research_node(self):
        n = notices.create(self.store, title='购买行情源', body=BODY, kind='DECISION', at=AT, payload={'deadline': '2026-10-01T12:00:00+08:00'})
        answer = notices.receive(self.cloud, {'notices': [notices.outgoing(n)], 'known': []}, AT)
        self.assertEqual(answer['states'][n['id']]['status'], 'OPEN')
        shown = notices.open_for_display(self.cloud)
        self.assertEqual((shown[0]['title'], shown[0]['deadline']), ('购买行情源', '2026-10-01T04:00:00+00:00'))
        notices.decide(self.cloud, n['id'], 'REJECT', 'dean')
        changed = dict(notices.outgoing(n), body='完全不同的正文内容，试图覆盖已经送达的通知。')
        answer = notices.receive(self.cloud, {'notices': [changed], 'known': [n['id']]}, AT)
        self.assertEqual(notices.get(self.cloud, n['id'])['body'], BODY)
        with self.store.db:
            notices.apply_states(self.store, answer['states'])
        mirrored = notices.get(self.store, n['id'])
        self.assertEqual((mirrored['status'], json.loads(mirrored['payload_json'])['decided_by']), ('REJECTED', 'dean'))
        for bad in ({**notices.outgoing(n), 'extra': 1}, {**notices.outgoing(n), 'kind': 'ALERT'}, {**notices.outgoing(n), 'id': 'X-1'}):
            with self.assertRaises(ValueError):
                notices.receive(self.cloud, {'notices': [bad]}, AT)
        with self.assertRaises(ValueError):
            notices.receive(self.cloud, {'notices': [], 'known': ['N-x'] * 201}, AT)

    def test_front_matter_file_from_claude(self):
        n = notices.parse_markdown('---\nid: N-claude-feed-01\nkind: VETO\ntitle: 止损幅度改为 7%\ndeadline: 2026-10-02T12:00:00+08:00\n---\n正文第一行\n第二行\n')
        self.assertEqual((n['id'], n['kind'], n['title'], n['deadline'], n['body']),
                         ('N-claude-feed-01', 'VETO', '止损幅度改为 7%', '2026-10-02T12:00:00+08:00', '正文第一行\n第二行'))
        with self.assertRaises(ValueError):
            notices.parse_markdown('没有头部的正文')


class NoticeSyncTests(unittest.TestCase):
    quote = fixtures.CloudSyncTests.quote
    plan = fixtures.CloudSyncTests.plan
    later = fixtures.CloudSyncTests.later
    output = fixtures.CloudSyncTests.output
    transact = fixtures.CloudSyncTests.transact

    def setUp(self):
        fixtures.CloudSyncTests.setUp(self)

    def tearDown(self):
        fixtures.CloudSyncTests.tearDown(self)

    def cloud_request(self, cfg, path, body):
        return self.transact(lambda: sync.handle(self.cloud, self.cloud_cfg, path, body, self.at))

    def test_notices_wait_for_a_cloud_that_supports_them_then_answers_come_back(self):
        n = notices.create(self.store, title='购买行情源', body=BODY, kind='DECISION', at=self.at)
        with patch('ashare.cloud_sync.request', side_effect=self.cloud_request) as sent:
            self.assertIsNone(sync.deliver_notices(self.store, self.cfg))  # an older cloud: nothing is sent
            self.assertEqual(sent.call_count, 0)
            with self.store.db:
                runtime.put(self.store, 'remote_features', list(ledger.FEATURES))
            self.assertEqual(sync.deliver_notices(self.store, self.cfg), {'delivered': 1, 'answered': 0})
            self.assertIsNotNone(notices.get(self.store, n['id'])['delivered_at'])
            self.assertEqual(notices.get(self.cloud, n['id'])['status'], 'OPEN')
            notices.decide(self.cloud, n['id'], 'APPROVE', 'dean')
            self.assertEqual(sync.deliver_notices(self.store, self.cfg), {'delivered': 0, 'answered': 1})
            self.assertEqual(notices.get(self.store, n['id'])['status'], 'APPROVED')
            self.assertIsNone(sync.deliver_notices(self.store, self.cfg))  # nothing open or new: no request
            self.assertEqual(sent.call_count, 2)

    def test_outage_rows_travel_with_the_last_ledger_page_only_when_asked(self):
        cloud_cfg = self.cloud_cfg
        qh.observe(self.cloud, cloud_cfg, self.later(-600), ['sh600519'], [])
        qh.observe(self.cloud, cloud_cfg, self.later(-540), [], [])
        qh.observe(self.cloud, cloud_cfg, self.later(-60), ['sh600519'], ['sh600519'])
        body = {'cursors': {}, 'protocol': 2, 'change_cursor': None, 'support_since': None}
        old = self.transact(lambda: ledger.export_ledger(self.cloud, {}, self.at, body=body))
        self.assertNotIn('extras', old)  # a 0.15.5 research node never sees the new key
        paged = self.transact(lambda: ledger.export_ledger(self.cloud, {}, self.at, limit=1, body={**body, 'extras': {'quote_health': None}}))
        if paged['more']:
            self.assertNotIn('extras', paged)
        packet = self.transact(lambda: ledger.export_ledger(self.cloud, {}, self.at, body={**body, 'extras': {'quote_health': None}}))
        self.assertFalse(packet['more'])
        self.assertEqual(len(packet['extras']['quote_health']), 3)
        ledger.import_ledger(self.store, self.cfg, packet)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM quote_health').fetchone()[0], 3)
        since = runtime.value(self.store, 'quote_health_since')
        self.assertEqual(since, self.later(-60))
        again = self.transact(lambda: ledger.export_ledger(self.cloud, {}, self.at, body={**body, 'extras': {'quote_health': since}}))
        self.assertEqual({r['kind'] for r in again['extras']['quote_health']}, {'PRIMARY_DOWN', 'NO_QUOTE'})  # only the open events
        summary = qh.summary(self.store, self.later(-3600), self.at)
        self.assertEqual((summary['minutes']['PRIMARY_DOWN'], summary['open']), (1, 2))
