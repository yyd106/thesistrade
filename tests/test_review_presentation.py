"""Synthetic read-only display tests; no production data or model invocations."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ashare import governance, proposal_presentation, supervision
from ashare.dashboard import status
from ashare.review_presentation import projection
from ashare.storage import Store
from test_config import load_config

AT = '2026-10-03T12:00:00+00:00'
OLD = '2026-09-01T12:00:00+00:00'


def insert_review_fixture(store, identity='display-review'):
    """Reusable isolated fixture with distinct daily/context money and cash dividend."""
    closing = {'qty': 100, 'qty_scale': 1, 'average_cost_cents': 1000, 'price_cents': 1005,
               'unrealized_cents': 500, 'cumulative_realized_cents': 300, 'valuation_complete': True,
               'quality': 'CLOSE', 'quote_at': '2026-10-02T07:00:00+00:00'}
    position = {'key': 'watchlist:sz000001', 'symbol': 'sz000001', 'name': '合成公司',
                'opening': {'late_quote': False}, 'closing': closing, 'period_profit_cents': 1200,
                'cumulative_realized_cents': 300, 'research_ids': []}
    context = {'version': 'v1', 'scope': 'ALL_POSITIONS', 'window_start': '2026-10-01T11:30:00+00:00',
               'window_end': '2026-10-03T11:30:00+00:00', 'opening': {'valuation_complete': True},
               'closing': closing, 'positions': [position], 'research': [], 'dividends': [{'amount_cents': 175}],
               'totals': {'period_profit_cents': 1200, 'cumulative_profit_cents': 800, 'holding_count': 1,
                          'reviewed_position_count': 1, 'period_fill_count': 2},
               'valuation_notice': '持仓损益不含现金分红。', 'learning_notice': '滚动窗口重叠不能相加。'}
    daily = copy.deepcopy(context)
    daily['window_start'] = '2026-10-02T11:30:00+00:00'
    daily['totals'].update(period_profit_cents=-450, period_fill_count=1)
    payload = {'facts': {'statistics': {}, 'daily_portfolio': daily, 'context_48h': context, 'portfolio': context},
               'analysis': {'summary': '模型仅提出待检验观点。', 'positions': [], 'lessons': []},
               'consistency_checks': [
                   {'check': 'CHECK_BUY_WHILE_HALTED', 'status': 'FAIL', 'checked': 1, 'failures': 1,
                    'examples': [{'symbol': 'sz000001', 'route': 'watchlist', 'fill_id': 'F-test', 'raw': 'PRIVATE_MARKER'}]},
                   {'check': 'CHECK_EXECUTION_EVIDENCE', 'status': 'INSUFFICIENT', 'checked': 1, 'failures': 0, 'missing': 1},
                   {'check': 'CHECK_DISK_SPACE', 'status': 'PASS', 'checked': 1, 'failures': 0},
                   {'check': 'CHECK_SELL_WITHOUT_REASON', 'status': 'NOT_APPLICABLE', 'checked': 0, 'failures': 0}],
               'routing': []}
    row = {'id': identity, 'window_start': daily['window_start'], 'window_end': daily['window_end'],
           'revision': 1, 'ready_at': AT, 'fingerprint': identity, 'model_status': 'SUCCEEDED'}
    with store.db:
        store.db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)',
                         tuple(row[k] for k in ('id','window_start','window_end','revision','ready_at','fingerprint','model_status')) + (json.dumps(payload),))
    return row, payload


class ReviewPresentationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cfg = load_config(Path(__file__).resolve().parents[1] / 'config.json')
        self.cfg.update(data_dir=self.tmp.name, scheduler_enabled=False, model_enabled=False)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def proposal(self, *, at=AT, source='manual', incomplete=False):
        payload = {} if incomplete else {key: '合成的完整方案：' + key for key in supervision.FIELDS}
        with self.store.db:
            return governance.draft_proposal(self.store, source=source, kind='RULE', target='合成',
                title='集中度减仓不能证明研究证伪，需补充独立验证条件' * 4, payload=payload, at=at)

    def snapshot(self):
        return {table: [tuple(row) for row in self.store.db.execute('SELECT * FROM ' + table)]
                for table in ('reviews', 'engineering_issues', 'strategy_proposals', 'lessons', 'strategy_guidance')}

    def test_daily_context_checks_are_separate_and_projection_is_read_only(self):
        row, payload = insert_review_fixture(self.store)
        before = self.snapshot()
        display = projection(self.store, row, payload, AT)
        self.assertEqual(display['daily']['totals']['period_profit_cents'], -450)
        self.assertEqual(display['context']['totals']['period_profit_cents'], 1200)
        self.assertEqual(display['daily']['dividend_cents'], 175)
        self.assertEqual(display['checks']['counts'], {'FAIL': 1, 'INSUFFICIENT': 1, 'PASS': 1, 'NOT_APPLICABLE': 1, 'UNKNOWN': 0})
        self.assertNotIn('PRIVATE_MARKER', json.dumps(display))
        self.assertEqual(before, self.snapshot())

    def test_overview_and_cloud_full_display_keep_same_evidence(self):
        insert_review_fixture(self.store)
        with patch('ashare.dashboard.now', return_value=AT):
            full = status(self.cfg, overview=False)['reviews'][0]
            compact = status(self.cfg, overview=True)['reviews'][0]
        self.assertEqual(full['presentation'], compact['presentation'])
        self.assertEqual(full['payload']['consistency_checks'], compact['payload']['consistency_checks'])
        self.assertEqual(compact['payload']['facts']['daily_accounting']['dividend_cents'], 175)
        self.assertEqual(compact['history'], {'total': 1, 'shown': 1, 'omitted': 0})
        self.assertNotIn('daily_portfolio', compact['payload']['facts'])

    def test_resolved_and_superseded_findings_leave_current_without_record_edits(self):
        row, payload = insert_review_fixture(self.store)
        with self.store.db:
            issue = governance.record_issue(self.store, 'OTHER_DATA', 'sz000001', '合成缺口', ['review:'+row['id']], OLD)
        governance.resolve_issue(self.store, issue, '合成已解决', at=AT)
        old, new = self.proposal(at=OLD), self.proposal()
        governance.decide(self.store, old, 'SUPERSEDED', decided_by=None, note='合成新版', at=AT, replaced_by=new)
        payload['analysis']['lessons'] = [{'lesson': '资料不足', 'category': 'DATA', 'applicability': '补公告'},
                                         {'lesson': '研究需验证', 'category': 'RESEARCH', 'applicability': '等到观察期结束'}]
        payload['routing'] = [{'lesson': 0, 'to': 'engineering_issue', 'id': issue}, {'lesson': 1, 'to': 'proposal_draft', 'id': old}]
        before = self.snapshot()
        items = projection(self.store, row, payload, AT)['findings']['items']
        self.assertEqual([v['status'] for v in items], ['RESOLVED', 'SUPERSEDED'])
        self.assertTrue(all(not v['current'] for v in items))
        self.assertEqual(before, self.snapshot())
        row['ready_at'] = OLD
        payload['routing'][1]['id'] = new
        expired = projection(self.store, row, payload, AT)['findings']['items'][1]
        self.assertTrue(expired['expired'])
        self.assertFalse(expired['current'])
        self.assertEqual(expired['status'], 'DRAFT')

    def test_health_failure_reasons_are_bounded_and_still_actionable(self):
        row, payload = insert_review_fixture(self.store)
        payload['consistency_checks'] = [
            {'check':'CHECK_SYNC_STALE','status':'FAIL','checked':1,'failures':1,'examples':[{'last_sync':{'at':OLD,'status':'FAILED','phase':'pull','token':'PRIVATE_MARKER'}}]},
            {'check':'CHECK_OUTBOX_BACKLOG','status':'FAIL','checked':1,'failures':1,'examples':[{'pending':7}]},
            {'check':'CHECK_RESEARCH_FAILURE_RATE','status':'FAIL','checked':20,'failures':1,'examples':[{'failed':12,'total':20}]},
            {'check':'CHECK_DISK_SPACE','status':'FAIL','checked':1,'failures':1,'examples':[{'free_gb':2.1,'db_gb':6.2,'path':'PRIVATE_MARKER'}]}]
        checks = projection(self.store, row, payload, AT)['checks']['items']
        self.assertEqual(checks[0]['examples'][0]['last_sync']['at'], OLD)
        self.assertEqual(checks[1]['examples'][0]['pending'], 7)
        self.assertEqual(checks[2]['examples'][0]['failed'], 12)
        self.assertEqual(checks[3]['examples'][0]['free_gb'], 2.1)
        self.assertNotIn('PRIVATE_MARKER', json.dumps(checks))

    def test_missing_groups_keep_route_counts_without_exporting_private_examples(self):
        from ashare import page_display
        row, payload = insert_review_fixture(self.store)
        groups = [{'check':'CHECK_BUY_OUTSIDE_PLAN_BAND', 'route':'watchlist' if i < 23 else 'dynamic',
                   'missing':'order / plan / upper band', 'count':i+1,
                   'examples':[{'path':'PRIVATE_GROUP_PATH', 'raw':'PRIVATE_GROUP_RAW'}]} for i in range(26)]
        payload['consistency_checks'] = [{'check':'CHECK_EXECUTION_EVIDENCE','status':'INSUFFICIENT',
                                         'checked':30,'failures':0,'missing_groups':groups}]
        before = copy.deepcopy(payload)
        summary = projection(self.store, row, payload, AT)['checks']['items'][0]
        public = page_display.bounded(summary, [0])
        self.assertEqual(public['missing_groups_total'], 26)
        self.assertEqual(public['missing_groups_omitted'], 2)
        self.assertEqual(len(public['missing_groups']), 24)
        self.assertEqual(public['missing_groups'][-1]['route'], 'dynamic')
        self.assertEqual(public['missing_groups'][-1]['count'], 24)
        self.assertNotIn('PRIVATE_GROUP', json.dumps(public))
        self.assertEqual(payload, before)

    def test_proposal_priority_and_14_day_history_ignore_repeated_narratives(self):
        old = self.proposal(at=OLD, source='review', incomplete=True)
        with self.store.db:
            row = self.store.db.execute('SELECT payload_json FROM strategy_proposals WHERE id=?', (old,)).fetchone()
            payload = json.loads(row[0]); payload['last_seen_at'] = AT
            self.store.db.execute('UPDATE strategy_proposals SET payload_json=? WHERE id=?', (json.dumps(payload), old))
        approved = self.proposal(at=OLD)
        governance.decide(self.store, approved, 'READY', decided_by=None, note='合成', at=OLD)
        governance.decide(self.store, approved, 'APPROVED', decided_by='Synthetic Dean', note='合成', at=OLD)
        ready = self.proposal(at=OLD)
        governance.decide(self.store, ready, 'READY', decided_by=None, note='合成', at=OLD)
        before = self.snapshot()
        view = proposal_presentation.cards(self.store, at=AT)
        self.assertEqual([r['id'] for r in view['items'][:2]], [approved, ready])
        archived = next(r for r in view['items'] if r['id']==old)
        self.assertEqual(archived['display_bucket'], 'HISTORY')
        self.assertTrue(archived['observation_only'])
        self.assertEqual(archived['status'], 'DRAFT')
        self.assertEqual(view['history_total'], 1)
        self.assertEqual(before, self.snapshot())
        for _ in range(40): self.proposal(source='review', incomplete=True)
        view = proposal_presentation.cards(self.store, at=AT)
        self.assertEqual([r['id'] for r in view['items'][:2]], [approved, ready])
        self.assertEqual(view['shown'], 20)
        self.assertEqual(view['omitted'], 23)

    def test_stale_stored_running_reviews_cannot_crowd_out_current_pending(self):
        with self.store.db:
            for i in range(31):
                subject = 'current-pending' if i==30 else 'old-running-'+str(i)
                packet = {'kind':'BATCH','subject_id':subject,'proposal':None,'batch':None,
                          'model':{'name':'test','effort':'test'},'review_version':'v'}
                self.store.db.execute('''INSERT INTO supervision_reviews
                    (id,kind,subject_id,input_hash,created_at,status,input_json) VALUES(?,?,?,?,?,?,?)''',
                    ('SR-'+str(i),'BATCH',subject,'hash',AT,'PENDING' if i==30 else 'RUNNING',json.dumps(packet)))
        before = self.store.db.total_changes
        with patch('ashare.supervision._current', side_effect=lambda store, cfg, data:data['subject_id']=='current-pending'):
            values, total = proposal_presentation._review_selection(self.store, [], AT)
        self.assertEqual(total, 31)
        self.assertEqual(values[0]['subject_id'], 'current-pending')
        self.assertEqual(values[0]['status'], 'PENDING')
        self.assertEqual(values[0]['display_bucket'], 'CURRENT')
        self.assertLessEqual(len(values), 30)
        self.assertEqual(before, self.store.db.total_changes)


if __name__ == '__main__':
    unittest.main()
