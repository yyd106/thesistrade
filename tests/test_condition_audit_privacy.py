"""Independent condition-receipt boundaries using temporary synthetic fixtures.

No operator configuration, production database, signing key or model input is
read. The existing sync fixture creates disposable test keys in its temp tree.
"""
import copy
import hashlib
import json
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ashare import build, cloud_ledger, cloud_protocol, portfolio_strategy as portfolio, privacy
from ashare.dashboard import make_handler
from ashare.paper import account
from ashare.storage import Store
import test_portfolio_audit_privacy as audit_fixtures
import test_research_privacy as privacy_fixtures
import test_strategy_export as export_fixtures


PRIVATE = 'PRIVATE_CONDITION_AND_NUMERIC_AUDIT_SENTINEL'


def publication_fixture(item, observed_hits=None):
    """A fully specified frozen HOLD replay, with independently supplied hits."""
    from ashare.strategy_report_audit import ACCOUNT_KEYS
    candidate = {**item, 'current_bps': 800, 'current_value_cents': 800000,
                 'qty': 100, 'cost_cents': 10000, 'max_bps': 2000,
                 'source_id': 'synthetic-plan', 'source_valid_until': export_fixtures.AT,
                 'can_increase': True, 'research': {'kind': 'PAPER_TRADE', 'blockers': []}}
    packet = {'as_of': export_fixtures.START, 'candidates': [candidate],
              'account': {k: 10000000 if k == 'equity_cents' else 0 for k in ACCOUNT_KEYS},
              'valuation_ready': True, 'halted': False, 'gross_cap_bps': 9000, 'symbol_cap_bps': 2000}
    fresh = copy.deepcopy(packet)
    fresh['candidates'][0].update(current_bps=900, current_value_cents=900000)
    return portfolio.publication_audit(packet, fresh,
        [{'key': item['key'], 'action': 'HOLD', 'target_bps': 800}],
        {'decisions': [{'key': item['key'], 'action': 'HOLD', 'target_bps': 900}], 'risk_groups': []},
        export_fixtures.AT, observed_hits=observed_hits)


def private_status():
    value = audit_fixtures.private_status()
    receipt = {'version': 1, 'status': 'RECORDED', 'condition_catalog': [PRIVATE],
               'analysis': {'EXTRA': PRIVATE}, 'hit_ids': [PRIVATE],
               'numeric_audit': {'issues': [PRIVATE]}}
    # Check duplicate and future placements, not only today's canonical path.
    value['condition_catalog'] = receipt
    value['numeric_audit'] = receipt
    value['portfolio_strategy']['publication_audit']['decisions'][0]['condition_audit'] = receipt
    value['coverage'] = {'publication_audits': {'decision': receipt}}
    value['coverage']['condition_receipts'] = {'synthetic-hash': receipt}
    for item in (value['account'], value['state'], value['watchlist'][0],
                 value['watchlist'][0]['plan']['payload'], value['decisions'][0]):
        item.update(condition_audit=receipt, numeric_audit=receipt)
    return value


class ConditionAuditHttpTests(unittest.TestCase):
    setUp = privacy_fixtures.ResearchPrivacyHttpTests.setUp
    tearDown = privacy_fixtures.ResearchPrivacyHttpTests.tearDown
    request = privacy_fixtures.ResearchPrivacyHttpTests.request

    def assert_private(self, result):
        raw = json.dumps(result)
        for term in (PRIVATE, 'condition_audit', 'condition_catalog', 'numeric_audit', 'publication_audits', 'condition_receipts'):
            self.assertNotIn(term, raw)

    def test_local_and_cloud_http_do_not_expose_nested_or_indexed_receipts(self):
        full = private_status()
        detail = {'at': privacy_fixtures.AT, 'stock': full['watchlist'][0],
                  'position': full['portfolio']['holdings'][0], 'condition_audit': full['condition_catalog'],
                  'numeric_audit': full['numeric_audit'], 'history': {
                      'decisions': {'total': 1, 'items': full['decisions'], 'condition_audit': PRIVATE},
                      'fills': {'total': 1, 'items': full['fills']}}}
        original, original_detail = copy.deepcopy(full), copy.deepcopy(detail)
        self.assert_private(privacy.guest_status(full))
        self.assert_private(privacy.guest_stock(detail))
        for role in ('GUEST', 'READER'):
            with closing(Store(self.cfg['data_dir'])) as store, store.db:
                store.db.execute("UPDATE app_users SET role=? WHERE username='guest'", (role,))
            for deployment in ('standalone', 'cloud'):
                self.cfg.update(deployment_role=deployment, public_origin='https://privacy.example')
                self.path.write_text(json.dumps(self.cfg))
                self.handler = make_handler(self.path, 'page-token', 8765)
                status_path = 'ashare.cloud_dashboard.status' if deployment == 'cloud' else 'ashare.dashboard.status'
                with self.subTest(role=role, deployment=deployment), \
                     patch(status_path, return_value=full), patch('ashare.stock_detail.detail', return_value=detail):
                    for endpoint, admin in (('/api/status', full), ('/api/stock?symbol=sh600519', detail)):
                        code, raw, _ = self.request('GET', endpoint, headers=self.guest)
                        self.assertEqual(code, 200, raw)
                        public = json.loads(raw)
                        self.assert_private(public)
                        self.assertFalse(public['research_access'])
                        code, raw, _ = self.request('GET', endpoint)
                        self.assertEqual(code, 200, raw)
                        self.assertEqual(json.loads(raw), admin)
                    public = privacy.guest_status(full)
                    self.assertEqual(public['watchlist'][0]['quote']['price_cents'], 1050)
                    self.assertEqual(public['fills'][0]['qty'], 100)
        self.assertEqual(full, original)
        self.assertEqual(detail, original_detail)


class ConditionAuditSyncTests(unittest.TestCase):
    setUp = audit_fixtures.PortfolioAuditSignedSyncTests.setUp
    tearDown = audit_fixtures.PortfolioAuditSignedSyncTests.tearDown
    quote = audit_fixtures.PortfolioAuditSignedSyncTests.quote
    plan = audit_fixtures.PortfolioAuditSignedSyncTests.plan
    later = audit_fixtures.PortfolioAuditSignedSyncTests.later
    output = audit_fixtures.PortfolioAuditSignedSyncTests.output
    transact = audit_fixtures.PortfolioAuditSignedSyncTests.transact
    publication = audit_fixtures.PortfolioAuditSignedSyncTests.publication
    receive = audit_fixtures.PortfolioAuditSignedSyncTests.receive
    signed_body = audit_fixtures.PortfolioAuditSignedSyncTests.signed_body

    def test_new_receipt_is_signed_preserved_and_never_grants_buy_authority(self):
        from ashare import condition_audit
        body = self.publication()
        payload = json.loads(body['decision']['payload_json'])
        rows = payload['publication_audit']['decisions']
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(condition_audit.validate_receipt(row['condition_audit']))
        before = cloud_ledger.version(self.cloud)
        received, _, headers = self.signed_body(body)
        self.assertEqual(self.receive(received)['status'], 'ACCEPTED')
        self.assertEqual(portfolio.active(self.cloud, self.at)['payload'], payload)
        self.assertEqual(cloud_ledger.version(self.cloud), before)
        self.assertEqual(self.receive(received)['status'], 'ALREADY_RECEIVED')
        self.assertEqual(portfolio.decision(self.cloud, self.cloud_cfg, 'global', 'BTC', self.at)['action'], 'HOLD')
        with self.assertRaisesRegex(ValueError, '未授权新增买入'):
            portfolio.buy_budget(self.cloud, self.cloud_cfg, 'global', 'BTC', self.at, account(self.cloud, self.at))
        changed = copy.deepcopy(received)
        altered = json.loads(changed['decision']['payload_json'])
        altered['publication_audit']['decisions'][0]['condition_audit']['hit_ids'] = ['FORGED_ALLOW']
        changed['decision']['payload_json'] = json.dumps(altered)
        with self.assertRaisesRegex(ValueError, '签名验证失败'):
            cloud_protocol.verify(self.cloud_cfg, '/api/sync/strategy', cloud_protocol.packed(changed), headers, self.at)
        with self.assertRaisesRegex(ValueError, '发布序号冲突'):
            self.receive(changed)
        self.assertEqual(portfolio.active(self.cloud, self.at)['payload'], payload)

    def test_old_stage_audit_without_condition_receipt_is_still_accepted(self):
        body = self.publication()
        payload = json.loads(body['decision']['payload_json'])
        for row in payload['publication_audit']['decisions']:
            row.pop('condition_audit', None)
        body['decision']['payload_json'] = json.dumps(payload)
        received, _, _ = self.signed_body(body)
        self.assertEqual(self.receive(received)['status'], 'ACCEPTED')
        self.assertEqual(portfolio.active(self.cloud, self.at)['payload'], payload)

    def test_even_signed_but_invalid_audit_metadata_cannot_override_decision(self):
        body = self.publication()
        payload = json.loads(body['decision']['payload_json'])
        for row in payload['publication_audit']['decisions']:
            row['condition_audit'] = {'version': True, 'status': 'RECORDED',
                                      'hit_ids': ['ALLOW'], 'authorization': True}
            row['numeric_audit'] = {'status': 'CONSISTENT', 'action': 'ALLOW', 'target_bps': 10000}
        body['decision']['payload_json'] = json.dumps(payload)
        received, _, _ = self.signed_body(body)
        self.assertEqual(self.receive(received)['status'], 'ACCEPTED')
        self.assertEqual(portfolio.active(self.cloud, self.at)['payload'], payload)
        with self.assertRaisesRegex(ValueError, '未授权新增买入'):
            portfolio.buy_budget(self.cloud, self.cloud_cfg, 'global', 'BTC', self.at, account(self.cloud, self.at))

    def test_audit_failure_or_absence_preserves_model_input_and_final_choices(self):
        modes = ('normal', 'absent', 'exception')
        observed = []
        for mode in modes:
            captured = {}
            def model(prompt, schema, *_args):
                packet = json.loads(prompt.split('<UNTRUSTED_INPUT>')[1].split('</UNTRUSTED_INPUT>')[0])
                captured.update(prompt=prompt, schema=copy.deepcopy(schema))
                self.assertEqual(prompt, portfolio.PROMPT + '\n<UNTRUSTED_INPUT>' + portfolio.encoded(packet) + '</UNTRUSTED_INPUT>')
                for private in ('condition_audit', 'condition_catalog', 'numeric_audit', 'publication_audit'):
                    self.assertNotIn(private, prompt)
                return self.output(packet, {'global:BTC': {'action': 'ALLOW', 'target_bps': 100}})
            from contextlib import nullcontext
            context = (nullcontext() if mode == 'normal' else
                       patch('ashare.condition_audit.build', return_value={}) if mode == 'absent' else
                       patch('ashare.condition_audit.build', side_effect=RuntimeError(PRIVATE)))
            before = cloud_ledger.version(self.store)
            with context:
                result = portfolio.run(self.store, self.cfg, self.at, model_fn=model, clock=lambda: self.at)
            self.assertEqual(result['status'], 'SUCCEEDED', result)
            payload = portfolio.active(self.store, self.at)['payload']
            self.assertNotIn(PRIVATE, json.dumps(payload))
            self.assertEqual(cloud_ledger.version(self.store), before)
            observed.append((captured, payload['summary'], payload['decisions'], payload['risk_groups']))
            if mode == 'exception':
                receipts = [r['condition_audit'] for r in payload['publication_audit']['decisions']]
                self.assertTrue(receipts)
                self.assertTrue(all(r['status'] == 'DEGRADED' for r in receipts))
        self.assertEqual(observed[0], observed[1])
        self.assertEqual(observed[0], observed[2])


class ConditionAuditFingerprintTests(unittest.TestCase):
    def test_new_audit_modules_remain_inside_conservative_behavior_boundary(self):
        # Audit-only semantics do not imply permission to relax the frozen
        # experiment identity. Exercise real source hashing on a tiny temp tree.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'ashare'
            root.mkdir()
            (root / '__init__.py').write_text('__version__ = "0.0.0"\n')
            for module in ('condition_audit.py', 'strategy_numeric_audit.py', 'portfolio_strategy.py'):
                self.assertNotIn(module, build.DISPLAY_ONLY_MODULES)
                prior = build.behavior_code_fingerprint(root)
                path = root / module
                path.write_text('VALUE = "first synthetic implementation"\n')
                self.assertNotEqual(build.behavior_code_fingerprint(root), prior)
                prior = build.behavior_code_fingerprint(root)
                path.write_text('VALUE = "second synthetic implementation"\n')
                self.assertNotEqual(build.behavior_code_fingerprint(root), prior)


class ConditionAuditReceiptTests(unittest.TestCase):
    def test_non_position_changes_do_not_become_numeric_weight_claims(self):
        from ashare import strategy_numeric_audit
        for reason in ('置信度从85%降至80%。', '订单转化率从14%降至12%。',
                       '毛利润同比从14%降至12%。'):
            item = {'reason': reason, 'current_bps': 1400, 'target_bps': 1200}
            original = copy.deepcopy(item)
            with self.subTest(reason=reason):
                numeric = strategy_numeric_audit.check(item)
                self.assertEqual(numeric['status'], 'UNKNOWN')
                self.assertEqual(numeric['comparisons'], [])
                self.assertEqual(item, original)
        explicit = strategy_numeric_audit.check({'reason': '将仓位从14%降至12%。',
                                                 'current_bps': 1400, 'target_bps': 1200})
        self.assertEqual(explicit['status'], 'CONSISTENT')
        self.assertEqual([c['role'] for c in explicit['comparisons']], ['CURRENT', 'TARGET'])

    def test_replayed_pass_is_not_an_observed_program_hit_or_economic_trigger(self):
        from ashare import condition_audit
        item = {'key': 'watchlist:sh600000', 'symbol': 'sh600000'}
        replay = publication_fixture(item)['decisions'][0]['condition_audit']
        self.assertTrue(condition_audit.validate_receipt(replay))
        self.assertEqual(replay['evaluation'], 'VALIDATION_REPLAY')
        self.assertEqual(replay['publication_recheck']['PUB_PASSIVE_REBASE'], 'PASS')
        self.assertEqual(replay['hit_ids'], [])
        self.assertEqual(replay['hit_attribution'], 'NOT_RECORDED')
        actual = publication_fixture(item, {item['key']: ['PUB_PASSIVE_REBASE']})['decisions'][0]['condition_audit']
        self.assertTrue(condition_audit.validate_receipt(actual))
        self.assertEqual(actual['hit_ids'], ['PUB_PASSIVE_REBASE'])
        self.assertEqual(actual['hit_attribution'], 'PROGRAM_OBSERVED')
        for receipt in (replay, actual):
            self.assertEqual(receipt['model_trigger'], 'NOT_RECORDED')
            self.assertEqual(receipt['model_termination'], 'NOT_RECORDED')
            self.assertEqual(receipt['execution'], 'NOT_CAPTURED')

    def test_projection_cannot_repair_malformed_receipt_into_complete_evidence(self):
        from ashare import condition_audit
        receipt = publication_fixture({'key': 'watchlist:sh600000', 'symbol': 'sh600000'})['decisions'][0]['condition_audit']
        self.assertTrue(condition_audit.validate_receipt(receipt))
        corruptions = (
            lambda r: r.update(version=True),
            lambda r: r.update(hit_ids=[PRIVATE]),
            lambda r: r['analysis'].update({PRIVATE: 'PASS'}),
            lambda r: r['analysis'].update(ACTION_TARGET_BOUNDS='UNKNOWN'),
            lambda r: r.update(hit_attribution='NOT_RECORDED', hit_ids=['PUB_PASSIVE_REBASE']),
        )
        for corrupt in corruptions:
            damaged = copy.deepcopy(receipt)
            corrupt(damaged)
            self.assertFalse(condition_audit.validate_receipt(damaged))
            projected = condition_audit.project(damaged)
            self.assertNotIn(PRIVATE, json.dumps(projected))
            self.assertFalse(condition_audit.validate_receipt(projected), projected)


class ConditionAuditReportTests(unittest.TestCase):
    setUp = export_fixtures.StrategyExportTests.setUp
    insert = export_fixtures.StrategyExportTests.insert
    decision = export_fixtures.StrategyExportTests.decision
    archive = export_fixtures.StrategyExportTests.archive

    def test_malformed_receipts_export_without_crashing_or_claiming_complete(self):
        from ashare import strategy_condition_report as report
        corruptions = {
            'bool-version': lambda r: r.update(version=True),
            'unknown-hit': lambda r: r.update(hit_ids=[PRIVATE]),
            'extra-condition': lambda r: r['analysis'].update({PRIVATE: 'PASS'}),
            'missing-stage': lambda r: r.pop('publication'),
            'wrong-type': lambda r: r.update(publication_recheck=[PRIVATE]),
        }
        for identity, corrupt in corruptions.items():
            self.decision(identity, export_fixtures.START, current=900, reason='保持当前仓位8%。')
            payload = json.loads(self.db.execute('SELECT payload_json FROM portfolio_decisions WHERE id=?', (identity,)).fetchone()[0])
            audit = publication_fixture(payload['decisions'][0])
            corrupt(audit['decisions'][0]['condition_audit'])
            audit['decisions'][0]['condition_audit']['raw_input'] = PRIVATE
            payload['publication_audit'] = audit
            self.db.execute('UPDATE portfolio_decisions SET payload_json=? WHERE id=?', (json.dumps(payload), identity))
        self.db.commit()
        before = list(self.db.execute('SELECT id,payload_json FROM portfolio_decisions ORDER BY id'))
        _, _, archive, data = self.archive()
        summary = data['coverage']['condition_audit']
        self.assertEqual(summary['counts']['incomplete'], len(corruptions))
        self.assertEqual(summary['counts'].get('recorded', 0), 0)
        self.assertEqual(summary['program_hit_counts'], {})
        self.assertEqual(report.summarize(data), summary)
        for name in archive.namelist():
            self.assertNotIn(PRIVATE, archive.read(name).decode())
        self.assertEqual(list(self.db.execute('SELECT id,payload_json FROM portfolio_decisions ORDER BY id')), before)

    def test_damaged_index_and_foreign_row_do_not_borrow_complete_evidence(self):
        from ashare import strategy_condition_report as report
        item = {'key': 'watchlist:sh600000', 'symbol': 'sh600000', 'action': 'HOLD',
                'target_bps': 900, 'current_bps': 900, 'reason': '保持当前仓位8%。'}
        audit = publication_fixture(item)
        decision = {'record': {'id': 'own'}, 'payload': {'decisions': [item], 'publication_audit_ref': 'own'}}
        data = {'decisions': [decision], 'coverage': {'publication_audits': {'own': audit}}}
        self.assertTrue(report.inspect_item(decision, item, data)[1])
        for damage in ('missing-index', 'foreign-reference', 'foreign-row', 'bool-version', 'changed-final', 'changed-current'):
            altered = copy.deepcopy(data)
            changed = altered['decisions'][0]
            if damage == 'missing-index':
                altered['coverage']['publication_audits'].clear()
            elif damage == 'foreign-reference':
                changed['payload']['publication_audit_ref'] = 'other'
            elif damage == 'foreign-row':
                altered['coverage']['publication_audits']['own']['decisions'][0]['key'] = 'watchlist:other'
            elif damage == 'bool-version':
                altered['coverage']['publication_audits']['own']['version'] = True
            elif damage == 'changed-final':
                changed['payload']['decisions'][0]['target_bps'] = 999
            else:
                changed['payload']['decisions'][0]['current_bps'] = 999
            with self.subTest(damage=damage):
                receipt, valid, numeric = report.inspect_item(changed, changed['payload']['decisions'][0], altered)
                self.assertFalse(valid)
                self.assertNotEqual(numeric['status'], 'INCONSISTENT')
                self.assertEqual(report.summarize(altered)['counts'].get('recorded', 0), 0)
                self.assertIn('不完整/不匹配', '\n'.join(report.render_item(changed, changed['payload']['decisions'][0], altered)))


class ConditionAuditIndexBoundaryTests(unittest.TestCase):
    @staticmethod
    def data():
        item = {'key': 'watchlist:sh600000', 'symbol': 'sh600000', 'action': 'HOLD',
                'target_bps': 900, 'current_bps': 900, 'reason': '保持当前仓位8%。'}
        audit = publication_fixture(item)
        sibling = {**item, 'key': 'watchlist:sh600001', 'symbol': 'sh600001'}
        audit['decisions'].append({**copy.deepcopy(audit['decisions'][0]), 'key': sibling['key']})
        decision = {'record': {'id': 'own'}, 'payload': {'decisions': [item, sibling], 'publication_audit': audit}}
        return {'coverage': {}, 'decisions': [decision]}

    def test_content_hash_keeps_json_types_unknown_fields_and_orphans_exact(self):
        from ashare import strategy_report_audit as report_audit
        data = self.data()
        original_audit = data['decisions'][0]['payload']['publication_audit']
        template = original_audit['decisions'][0]
        original_audit['future'] = {'literal': {'condition_audit_ref': 'not-a-real-reference'}}
        original_audit['decisions'] = [{**copy.deepcopy(template), 'key': str(n), 'condition_audit': value}
                                      for n, value in enumerate((None, True, 1, False, 0))]
        orphan = {'future_version': 999, 'nested': [True, 1, None], 'verbatim': '完整保留'}
        digest = lambda value: hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                                        separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        data['coverage']['condition_receipts'] = {digest(orphan): orphan}
        before = copy.deepcopy(data)
        projected = report_audit.export_projection(data)
        index = projected['coverage']['condition_receipts']
        self.assertEqual(len(index), 6, 'JSON true/1 and false/0 must not share content identities')
        self.assertEqual(index[digest(orphan)], orphan)
        for reference, value in index.items():
            self.assertEqual(reference, digest(value))
        for n in range(5):
            parent, row = report_audit.publication_item(projected['decisions'][0], str(n), projected)
            self.assertEqual(parent, original_audit)
            self.assertIs(parent['decisions'][n], row)
        self.assertEqual(report_audit.export_projection(json.loads(json.dumps(projected))), projected)
        self.assertEqual(data, before)

    def test_valid_condition_hash_cannot_bypass_parent_identity_or_broken_sibling(self):
        from ashare import strategy_report_audit as report_audit
        from ashare import strategy_condition_report as report
        clean = report_audit.export_projection(self.data())
        decision = clean['decisions'][0]
        item = decision['payload']['decisions'][0]
        self.assertTrue(report.inspect_item(decision, item, clean)[1])
        for corruption in ('foreign-parent', 'broken-sibling'):
            data = copy.deepcopy(clean)
            current = data['decisions'][0]
            if corruption == 'foreign-parent':
                data['coverage']['publication_audits']['other'] = copy.deepcopy(data['coverage']['publication_audits']['own'])
                current['payload']['publication_audit_ref'] = 'other'
            else:
                sibling = data['coverage']['publication_audits']['own']['decisions'][1]
                sibling['condition_audit_ref'] = '0' * 64
            before = copy.deepcopy(data)
            with self.subTest(corruption=corruption):
                self.assertEqual(report_audit.publication_item(current, item['key'], data), (None, None))
                self.assertFalse(report.inspect_item(current, item, data)[1])
                self.assertEqual(report.inspect_item(current, item, data)[2]['status'], 'UNKNOWN')
                with self.assertRaises(ValueError):
                    report_audit.export_projection(data)
                self.assertEqual(data, before)


if __name__ == '__main__':
    unittest.main()
