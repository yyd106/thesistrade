import tempfile
import unittest

from ashare import approvals, cloud_ledger as ledger, cloud_runtime as runtime, withdrawals
from ashare.finance import PaperLedger
from ashare.paper import account
from ashare.settings import DEFAULTS
from ashare.storage import Store

AT = '2026-10-09T02:00:00+00:00'


class WithdrawalSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.local_tmp = tempfile.TemporaryDirectory()
        self.cloud, self.local = Store(self.tmp.name), Store(self.local_tmp.name)
        self.config = {**DEFAULTS, 'deployment_role': 'cloud', 'mode': 'paper',
                       'investment_policy': 'days_cash_v1', 'portfolio_strategy': None,
                       'watchlist': [], 'live_execution_enabled': False}
        self.local_config = {**self.config, 'deployment_role': 'research'}
        PaperLedger(self.cloud).initialize()
        with self.cloud.db:
            runtime.put(self.cloud, 'initialized', True)

    def tearDown(self):
        self.cloud.close()
        self.local.close()
        self.tmp.cleanup()
        self.local_tmp.cleanup()

    def packet(self, extras=None):
        body = {'protocol': 2, 'cursors': {}}
        if extras is not None:
            body['extras'] = extras
        with self.cloud.db:
            return ledger.export_ledger(self.cloud, {}, AT, body=body, config=self.config)

    def test_old_request_keeps_base_tables_without_new_summary(self):
        packet = self.packet()
        self.assertNotIn('extras', packet)
        self.assertEqual(set(packet['mutable']), set(ledger.MUTABLE))
        self.assertFalse(any('withdrawal_' in name for name in packet['tables']))
        ledger.import_ledger(self.local, self.local_config, packet)
        self.assertEqual(ledger.version(self.local), ledger.version(self.cloud))

    def test_opted_in_summary_is_read_only_on_research_replica(self):
        packet = self.packet({'withdrawals': True})
        summary = packet['extras']['withdrawals']
        self.assertEqual(summary['version'], 'simulated-withdrawal-v1')
        self.assertEqual(summary['authority'], 'cloud')
        ledger.import_ledger(self.local, self.local_config, packet)
        self.assertEqual(runtime.value(self.local, 'withdrawals'), summary)
        self.assertEqual(withdrawals.status(self.local, self.local_config, AT)['current_stage'], 1)
        before = self.local.db.execute('SELECT cash_cents FROM paper_accounts').fetchone()[0]
        with self.assertRaises(ValueError):
            withdrawals.request(self.local, self.local_config, AT)
        self.assertEqual(self.local.db.execute('SELECT cash_cents FROM paper_accounts').fetchone()[0], before)

    def test_corrupt_optional_summary_does_not_discard_cash_update(self):
        packet = self.packet({'withdrawals': True})
        packet['extras']['withdrawals']['reserved_cents'] = -1
        ledger.import_ledger(self.local, self.local_config, packet)
        self.assertEqual(self.local.db.execute('SELECT cash_cents FROM paper_accounts').fetchone()[0], 10000000)
        self.assertIsNone(runtime.value(self.local, 'withdrawals'))
        self.assertIsNotNone(runtime.value(self.local, 'extras_error_withdrawals'))

    def test_old_cloud_ignoring_extra_remains_importable(self):
        packet = self.packet()
        packet['features'].remove('simulated_withdrawals_v1')
        ledger.import_ledger(self.local, self.local_config, packet)
        self.assertIsNone(runtime.value(self.local, 'withdrawals'))
        self.assertEqual(account(self.local, AT)['withdrawal_reserved_cents'], 0)

    def test_withdrawal_request_does_not_enter_old_node_notice_channel(self):
        request = approvals.create_request(self.cloud, kind='WITHDRAWAL', subject_id='synthetic-cycle',
            action='TRANSFER', snapshot={'amount_cents': 4000000},
            summary={'title': '合成模拟资金操作'}, at=AT, notify=False)
        self.assertEqual(approvals.envelope(self.cloud, request['id'])['kind'], 'WITHDRAWAL')
        self.assertIsNone(self.cloud.db.execute('SELECT 1 FROM notices WHERE id=?', (request['notice_id'],)).fetchone())
        self.assertIsNone(approvals.receipt_state(self.cloud, request['id']))

    def test_withdrawal_actions_cannot_be_used_for_governance(self):
        for kind, action in [('WITHDRAWAL', 'ADOPTED'), ('CONFIG', 'TRANSFER'), ('PROPOSAL_DECISION', 'CANCEL')]:
            with self.assertRaises(ValueError):
                approvals.create_request(self.cloud, kind=kind, subject_id='synthetic-cycle',
                    action=action, snapshot={}, summary={}, at=AT, notify=False)


if __name__ == '__main__':
    unittest.main()
