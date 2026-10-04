import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from ashare import approvals, governance
from ashare.cli import _store_command
from ashare.storage import Store
from approval_fixture import grant


class ApprovalCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.store = Store(self.tmp.name)
        self.spec = {'kind': 'RESEARCH_GUIDANCE', 'target': 'watchlist', 'title': '合成审批链路',
                     'guidance': {'route': 'watchlist', 'scope': 'ALL', 'text': '研究中应清楚区分已核验事实与尚待证实的假设。'},
                     'hypothesis': '合成', 'change': '合成', 'evidence': '合成统计摘要', 'test_plan': '合成',
                     'failure_criteria': '合成', 'rollback': '合成'}
        self.path = Path(self.tmp.name) / 'proposal.json'
        self.path.write_text(json.dumps(self.spec))

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def command(self, action, **kw):
        args = SimpleNamespace(**dict(command='proposals', action=action, id=None, file=str(self.path),
                                      note='合成说明', approved_by=None, to=None) | kw)
        return _store_command(args, {}, self.store)

    def test_cli_request_confirm_consume_chain_and_old_name_cannot_bypass(self):
        pid = self.command('new')['id']
        self.command('decide', id=pid, to='READY')
        with self.assertRaises(ValueError):
            self.command('decide', id=pid, to='APPROVED', approved_by='Dean')
        request = self.command('request', id=pid, to='APPROVED', replace_none=True)
        grant(self.store, request)
        self.command('decide', id=pid, to='APPROVED', replace_none=True, approval_id=request['id'])
        self.assertEqual(approvals.request_info(self.store, request['id'])['status'], 'CONSUMED')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM strategy_guidance').fetchone()[0], 0)
        request = self.command('request', id=pid, to='ADOPTED', replace_none=True)
        grant(self.store, request)
        self.command('decide', id=pid, to='ADOPTED', replace_none=True, approval_id=request['id'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM strategy_guidance').fetchone()[0], 1)

    def test_multiple_draft_supersessions_rollback_together(self):
        first, second = self.command('new')['id'], self.command('new')['id']
        before = [tuple(r) for r in self.store.db.execute('SELECT * FROM strategy_proposals ORDER BY id')]
        original = governance.decide
        def fail_second(store, pid, *args, **kw):
            if pid == second: raise ValueError('synthetic second transition failed')
            return original(store, pid, *args, **kw)
        with patch('ashare.governance.decide', side_effect=fail_second), self.assertRaises(ValueError):
            self.command('new', supersedes=[first, second])
        self.assertEqual([tuple(r) for r in self.store.db.execute('SELECT * FROM strategy_proposals ORDER BY id')], before)

    def test_concurrent_adoption_consumes_once_and_writes_one_rule(self):
        pid = self.command('new')['id']
        self.command('decide', id=pid, to='READY')
        request = self.command('request', id=pid, to='APPROVED', replace_none=True)
        grant(self.store, request)
        self.command('decide', id=pid, to='APPROVED', replace_none=True, approval_id=request['id'])
        request = self.command('request', id=pid, to='ADOPTED', replace_none=True)
        grant(self.store, request)
        def attempt(_):
            store = Store(self.tmp.name)
            try:
                governance.decide(store, pid, 'ADOPTED', note='合成说明', approval_id=request['id'], replaces=[])
                return 'APPLIED'
            except ValueError:
                return 'REJECTED'
            finally: store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertCountEqual(results, ['APPLIED', 'REJECTED'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM strategy_guidance').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM approval_consumptions WHERE request_id=?', (request['id'],)).fetchone()[0], 1)
