"""Publication receipts cross signed sync but never the guest HTTP boundary.

All accounts, keys and model output in these tests are temporary fixtures.
The operator's config, signing keys and model files are never read.
"""
import copy
import json
from contextlib import closing
from pathlib import Path
import unittest
from unittest.mock import patch

from ashare import cloud_ledger as ledger, cloud_protocol as protocol, portfolio_strategy as portfolio, privacy
from ashare.dashboard import make_handler
from ashare.storage import Store
import test_cloud_sync as cloud_fixtures
import test_config as test_defaults
import test_investment_policy as investment_fixtures
import test_research_privacy as privacy_fixtures


PRIVATE = 'PRIVATE_PUBLICATION_AUDIT_SENTINEL'
ROOT = Path(__file__).resolve().parents[1]


def audit_fixture():
    return {'version': 1, 'target_method': 'MODEL_DISCRETION_WITH_HARD_CONSTRAINTS',
            'analysis_as_of': privacy_fixtures.AT, 'published_at': privacy_fixtures.AT,
            'analysis_account': {'equity_cents': 10000000, 'note': PRIVATE},
            'publication_account': {'equity_cents': 10001000, 'note': PRIVATE},
            'analysis_constraints': {'valuation_ready': True}, 'publication_constraints': {'valuation_ready': True},
            'validation': {'analysis': 'PASSED', 'publication': 'PASSED'},
            'decisions': [{'key': 'watchlist:sh600519', 'model_action': 'HOLD', 'model_target_bps': 869,
                           'analysis_basis': {'current_bps': 869, 'recorded_blockers': [PRIVATE]},
                           'published_action': 'HOLD', 'published_target_bps': 868,
                           'publication_basis': {'current_bps': 868},
                           'adjustment_codes': ['HOLD_PAUSE_CURRENT_REBASE']}]}


def private_status():
    value = privacy_fixtures.research_fixture()
    audit = audit_fixture()
    value['publication_audit'] = audit
    value['portfolio_strategy'] = {'enabled': True, 'publication_audit': audit}
    value['account'].update(publication_audit=audit, analysis_account=audit['analysis_account'],
                            publication_account=audit['publication_account'])
    value['state']['publication_audit'] = audit
    value['watchlist'][0]['publication_audit'] = audit
    value['watchlist'][0]['plan']['payload']['publication_audit'] = audit
    value['decisions'][0]['publication_audit'] = audit
    return value


class PortfolioAuditPrivacyTests(unittest.TestCase):
    setUp = privacy_fixtures.ResearchPrivacyHttpTests.setUp
    tearDown = privacy_fixtures.ResearchPrivacyHttpTests.tearDown
    request = privacy_fixtures.ResearchPrivacyHttpTests.request

    def assert_private(self, value):
        text = json.dumps(value)
        self.assertNotIn(PRIVATE, text)
        self.assertNotIn('publication_audit', text)
        self.assertNotIn('analysis_account', text)
        self.assertNotIn('publication_account', text)

    def test_local_and_cloud_status_hide_receipts_for_every_non_admin_role(self):
        for deployment in ('standalone', 'cloud'):
            self.cfg.update(deployment_role=deployment, public_origin='https://privacy.example')
            self.path.write_text(json.dumps(self.cfg))
            self.handler = make_handler(self.path, 'page-token', 8765)
            full = private_status()
            before = copy.deepcopy(full)
            public = privacy.guest_status(full)
            self.assert_private(public)
            self.assertEqual(public['account']['equity_cents'], full['account']['equity_cents'])
            self.assertIsNone(public['portfolio_strategy'])
            target = 'ashare.cloud_dashboard.status' if deployment == 'cloud' else 'ashare.dashboard.status'
            with patch(target, return_value=full):
                for role in ('GUEST', 'READER'):
                    with self.subTest(deployment=deployment, role=role):
                        with closing(Store(self.cfg['data_dir'])) as store, store.db:
                            store.db.execute("UPDATE app_users SET role=? WHERE username='guest'", (role,))
                        code, raw, _ = self.request('GET', '/api/status', headers=self.guest)
                        self.assertEqual(code, 200, raw)
                        self.assert_private(json.loads(raw))
                        self.assertFalse(json.loads(raw)['research_access'])
                code, raw, _ = self.request('GET', '/api/status')
                self.assertEqual((code, json.loads(raw)), (200, before))
                self.assertIn(PRIVATE, raw.decode())
            self.assertEqual(full, before, 'Guest projection cannot mutate the shared administrator data')

    def test_stock_history_and_plan_audit_do_not_escape_through_an_alternate_view(self):
        full = private_status()
        detail = {'at': privacy_fixtures.AT, 'publication_audit': audit_fixture(),
                  'stock': full['watchlist'][0], 'position': full['portfolio']['holdings'][0],
                  'trade_statistics': {'fill_count': 1, 'fee_cents': 1, 'realized_cents': 0},
                  'history': {key: {'total': len(rows), 'items': rows, 'publication_audit': audit_fixture()}
                              for key, rows in [('fills', full['fills']), ('orders', full['account']['orders']),
                                                ('decisions', full['decisions'])]}}
        before = copy.deepcopy(detail)
        self.assert_private(privacy.guest_stock(detail))
        for deployment in ('standalone', 'cloud'):
            self.cfg['deployment_role'] = deployment
            self.path.write_text(json.dumps(self.cfg))
            self.handler = make_handler(self.path, 'page-token', 8765)
            with self.subTest(deployment=deployment), patch('ashare.stock_detail.detail', return_value=detail):
                code, raw, _ = self.request('GET', '/api/stock?symbol=sh600519', headers=self.guest)
                self.assertEqual(code, 200, raw)
                public = json.loads(raw)
                self.assert_private(public)
                self.assertEqual(public['history']['fills']['items'][0]['qty'], 100)
                self.assertEqual(public['stock']['plan']['payload']['levels']['buy_low_cents'], 1000)
                self.assertEqual(json.loads(self.request('GET', '/api/stock?symbol=sh600519')[1]), before)
        self.assertEqual(detail, before)


class PortfolioAuditSignedSyncTests(unittest.TestCase):
    quote = cloud_fixtures.CloudSyncTests.quote
    plan = cloud_fixtures.CloudSyncTests.plan
    later = cloud_fixtures.CloudSyncTests.later
    output = cloud_fixtures.CloudSyncTests.output
    transact = cloud_fixtures.CloudSyncTests.transact
    publication = cloud_fixtures.CloudSyncTests.publication
    receive = cloud_fixtures.CloudSyncTests.receive
    tearDown = cloud_fixtures.CloudSyncTests.tearDown

    def setUp(self):
        # Existing helpers normally load config.json; route this isolated fixture
        # explicitly to public defaults before creating its temporary accounts.
        safe_config = patch.object(investment_fixtures, 'load_config',
                                   lambda *_args, **_kwargs: test_defaults.load_config(ROOT / 'config.example.json'))
        safe_config.start()
        self.addCleanup(safe_config.stop)
        cloud_fixtures.CloudSyncTests.setUp(self)

    def signed_body(self, body):
        raw = protocol.packed(body)
        headers = protocol.signed_headers(self.cfg, '/api/sync/strategy', raw, self.at)
        protocol.verify(self.cloud_cfg, '/api/sync/strategy', raw, headers, self.at)
        return protocol.unpack(raw), raw, headers

    def test_new_audit_is_signed_preserved_and_does_not_change_execution_authority(self):
        body = self.publication()
        original = json.loads(body['decision']['payload_json'])
        audit = original['publication_audit']
        self.assertEqual(audit['version'], 1)
        before = ledger.version(self.cloud)
        received, raw, headers = self.signed_body(body)
        self.assertEqual(self.receive(received)['status'], 'ACCEPTED')
        saved = portfolio.active(self.cloud, self.at)['payload']
        self.assertEqual(saved, original)
        self.assertEqual(portfolio.view(self.cloud, self.cloud_cfg, self.at)['publication_audit'], audit)
        self.assertEqual(ledger.version(self.cloud), before)
        self.assertEqual(self.receive(received)['status'], 'ALREADY_RECEIVED')
        changed = copy.deepcopy(received)
        altered = json.loads(changed['decision']['payload_json'])
        altered['publication_audit']['publication_account']['equity_cents'] += 1
        changed['decision']['payload_json'] = json.dumps(altered)
        with self.assertRaisesRegex(ValueError, '签名验证失败'):
            protocol.verify(self.cloud_cfg, '/api/sync/strategy', protocol.packed(changed), headers, self.at)
        with self.assertRaisesRegex(ValueError, '发布序号冲突'):
            self.receive(changed)
        self.assertEqual(portfolio.active(self.cloud, self.at)['payload'], original)

    def test_old_signed_decision_without_audit_remains_compatible(self):
        body = self.publication()
        original = json.loads(body['decision']['payload_json'])
        original.pop('publication_audit')
        body['decision']['payload_json'] = json.dumps(original)
        received, _, _ = self.signed_body(body)
        self.assertEqual(self.receive(received)['status'], 'ACCEPTED')
        saved = portfolio.active(self.cloud, self.at)['payload']
        self.assertEqual(saved, original)
        self.assertNotIn('publication_audit', portfolio.view(self.cloud, self.cloud_cfg, self.at))


if __name__ == '__main__':
    unittest.main()
