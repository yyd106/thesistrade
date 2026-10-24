"""Audit statistics preserve denominators, unknown history and mixed record versions."""
import copy
import hashlib
import json
import unittest

from ashare import strategy_report_audit as audit
from ashare import strategy_export as export
from ashare import strategy_export_sync
from ashare.portfolio_strategy import publication_audit
from tests import test_strategy_export as fixtures


class StrategyReportAuditTests(unittest.TestCase):
    setUp = fixtures.StrategyExportTests.setUp
    insert = fixtures.StrategyExportTests.insert
    decision = fixtures.StrategyExportTests.decision
    plan = fixtures.StrategyExportTests.plan
    archive = fixtures.StrategyExportTests.archive

    def update_payload(self, identity, change):
        payload = json.loads(self.db.execute('SELECT payload_json FROM portfolio_decisions WHERE id=?', (identity,)).fetchone()[0])
        change(payload)
        self.db.execute('UPDATE portfolio_decisions SET payload_json=? WHERE id=?', (json.dumps(payload), identity))
        self.db.commit()

    def test_round_rate_pair_rate_and_passive_rebase_have_separate_denominators(self):
        self.decision('baseline', fixtures.BEFORE, current=800, sent=True)
        self.decision('drift', fixtures.START, current=900, sent=True)
        self.decision('allow', '2026-10-09T02:00:00+08:00', action='ALLOW', current=900, target=1000, sent=True)
        self.decision('pause', fixtures.AT, action='PAUSE', current=920, sent=True)
        content, name, archive, data = self.archive()
        m = data['coverage']['report_audit']['activity']
        self.assertEqual((m['rounds'], m['item_occurrences'], m['acknowledged_changed_rounds']), (3, 3, 2))
        self.assertEqual((m['comparable_action_pairs'], m['action_changes']), (3, 2))
        self.assertEqual(m['active_target_changes'], 1)
        self.assertEqual(m['passive_target_follow_changes'], 1)
        self.assertEqual(m['passive_target_reset_changes'], 1)
        self.assertEqual((m['distinct_source_versions'], m['repeated_source_references']), (1, 2))
        overview = archive.read('总览.md').decode()
        self.assertIn('同键配对动作变化率：2 / 3', overview)
        self.assertIn('已确认发布轮次的动作或主动目标变化占比：2 / 3', overview)
        self.assertIn('不是研究转交易率', overview)
        meta = {k: data[k] for k in ('generated_at', 'start_at', 'end_at')}
        meta.update(filename=name, sha256=hashlib.sha256(content).hexdigest())
        self.assertEqual(strategy_export_sync._archive(content, meta, fixtures.AT)['coverage']['report_audit'], data['coverage']['report_audit'])

    def test_allow_eligibility_and_holding_cross_counts_do_not_infer_missing_values(self):
        self.decision('held', fixtures.START, action='ALLOW', target=1000, qty=100)
        self.decision('unheld', '2026-10-09T01:00:00+08:00', action='ALLOW', target=1000, qty=0)
        self.decision('unknown', fixtures.AT, action='ALLOW', target=1000, qty=None)
        self.update_payload('unheld', lambda p: p['decisions'][0].update(can_increase=False))
        self.update_payload('unknown', lambda p: p['decisions'][0].pop('can_increase'))
        m = self.archive()[3]['coverage']['report_audit']['activity']
        self.assertEqual(m['allow_matrix'], {'held/true': 1, 'unheld/false': 1, 'unknown/unknown': 1})
        self.assertEqual(m['action_eligibility_holding'], {'ALLOW/true/held': 1, 'ALLOW/false/unheld': 1, 'ALLOW/unknown/unknown': 1})
        self.assertEqual(m['unpaired_current_items'], 1)
        self.assertEqual(m['comparable_action_pairs'], 2)
        self.assertEqual(m['items_without_publication_audit'], 3)

    def test_frozen_records_and_source_matching_split_legacy_new_and_baseline(self):
        plan = self.plan()
        self.db.execute('CREATE TABLE portfolio_source_coverage(' + ','.join(c + ' TEXT' for c in export.COLUMNS['portfolio_source_coverage'].split()) + ')')
        self.insert('portfolio_source_coverage', version='portfolio-source-history-v1', epoch='test',
                    started_at='2026-10-08T17:00:00.866+00:00')
        self.decision('baseline', fixtures.BEFORE)
        self.decision('legacy', fixtures.START)
        self.decision('frozen', '2026-10-08T17:00:00+00:00', frozen=plan)
        self.decision('new-gap', '2026-10-08T18:00:00+00:00')
        self.decision('empty-record', fixtures.AT)
        self.insert('portfolio_history_snapshots', decision_id='empty-record', history_json={'sources': {}})
        f = self.archive()[3]['coverage']['report_audit']['frozen']
        self.assertEqual(f['window_metadata_present'], 2)
        self.assertEqual(f['window_legacy_metadata_missing'], 1)
        self.assertEqual(f['window_new_metadata_missing'], 1)
        self.assertEqual(f['baseline_metadata_missing'], 1)
        self.assertEqual(f['window_matching_sources'], 1)
        self.assertEqual(f['window_source_unmatched'], 3)
        self.assertEqual(f['window_metadata_present_source_unmatched'], 1)

    def test_first_observed_snapshot_does_not_invent_mechanism_start(self):
        self.decision('unknown-gap', fixtures.START)
        self.decision('later', fixtures.AT, frozen=self.plan())
        data = self.archive()[3]
        f = data['coverage']['report_audit']['frozen']
        self.assertIsNone(f['classification_boundary'])
        self.assertEqual(f['window_unclassified_metadata_missing'], 1)
        self.assertEqual(f['window_legacy_metadata_missing'], 0)
        data['coverage']['source_history'] = [{'version': 'unknown-version', 'started_at': fixtures.START}]
        self.assertIsNone(audit.summarize(data)['frozen']['classification_boundary'])
        data['decisions'][-1]['frozen_history']['coverage_started_at'] = fixtures.START
        self.assertEqual(audit.summarize(data)['frozen']['window_new_metadata_missing'], 1)

    def test_guidance_statistics_count_whole_retained_table_separately_from_window(self):
        for identity, status, adopted, retired in (
            ('old-active', 'ADOPTED', fixtures.BEFORE, None),
            ('old-retired', 'RETIRED', fixtures.BEFORE, fixtures.START),
            ('new-active', 'ADOPTED', fixtures.START, None),
            ('future-retirement', 'RETIRED', fixtures.BEFORE, '2026-10-23T12:00:00+00:00'),
        ):
            self.insert('strategy_guidance', id=identity, status=status, route='ALL', scope='ALL',
                        adopted_at=adopted, retired_at=retired, text='合成规则', payload_json={})
        data = self.archive()[3]
        g = data['coverage']['guidance_summary']
        self.assertEqual((g['retained_total'], g['active_at_cutoff'], g['adopted_in_window'], g['retired_in_window']), (4, 3, 1, 1))
        self.assertEqual(len(data['research']['strategy_guidance']), 2)
        self.assertFalse(audit.guidance_summary(None, fixtures.START, fixtures.AT)['available'])

    @staticmethod
    def make_audit(item):
        candidate = {**item, 'current_bps': 800, 'current_value_cents': 800000, 'max_bps': 2000,
                     'source_valid_until': fixtures.AT, 'research': {'kind': 'PAPER_TRADE', 'blockers': []}}
        packet = {'as_of': fixtures.START, 'candidates': [candidate],
                  'account': {k: 10000000 if k == 'equity_cents' else 0 for k in audit.ACCOUNT_KEYS},
                  'valuation_ready': True, 'halted': False, 'gross_cap_bps': 9000, 'symbol_cap_bps': 2000}
        fresh = copy.deepcopy(packet)
        fresh['candidates'][0].update(current_bps=900, current_value_cents=900000)
        return publication_audit(packet, fresh, [{'key': item['key'], 'action': 'HOLD', 'target_bps': 800}],
                                 {'decisions': [{'key': item['key'], 'action': 'HOLD', 'target_bps': 900}]}, fixtures.AT)

    def test_new_and_old_records_export_without_private_audit_fields_or_rewriting_reasons(self):
        plan = self.plan()
        self.decision('old', fixtures.START, frozen=plan)
        self.decision('new', fixtures.AT, current=900, reason='历史理由仍说保持8%，不改写', frozen=plan)
        def add(p):
            p['publication_audit'] = self.make_audit(p['decisions'][0])
            p['publication_audit']['raw_packet'] = 'PRIVATE_AUDIT_PACKET'
            p['publication_audit']['analysis_account']['not_allowed'] = 'PRIVATE_ACCOUNT'
            p['publication_audit']['decisions'][0]['analysis_basis']['input'] = 'PRIVATE_BASIS'
        self.update_payload('new', add)
        _, _, archive, data = self.archive()
        m = data['coverage']['report_audit']['activity']
        self.assertEqual((m['publication_audit_items'], m['items_without_publication_audit']), (1, 1))
        text = '\n'.join(archive.read(n).decode() for n in archive.namelist())
        for marker in ('PRIVATE_AUDIT_PACKET', 'PRIVATE_ACCOUNT', 'PRIVATE_BASIS'):
            self.assertNotIn(marker, text)
        report = archive.read('研究策略.md').decode()
        self.assertIn('模型原动作 / 目标：HOLD / 8%', report)
        self.assertIn('发布动作 / 目标：HOLD / 9%', report)
        self.assertIn('公式复算占比 9%', report)
        self.assertIn('历史理由仍说保持8%，不改写', report)
        self.assertEqual(data['decisions'][-1]['payload']['decisions'][0]['target_bps'], 900)

    def test_malformed_or_unknown_publication_receipts_do_not_claim_complete_coverage(self):
        self.decision('partial', fixtures.START)
        self.decision('bool-version', fixtures.AT)
        self.update_payload('partial', lambda p: p.update(publication_audit={'version': 1, 'decisions': [{'key': p['decisions'][0]['key']}]}))
        self.update_payload('bool-version', lambda p: p.update(publication_audit={'version': True, 'decisions': [{'key': p['decisions'][0]['key']}]}))
        data = self.archive()[3]
        m = data['coverage']['report_audit']['activity']
        self.assertEqual((m['publication_audit_items'], m['incomplete_publication_audit_items'], m['items_without_publication_audit']), (0, 1, 1))

    def test_global_audit_distinguishes_scaled_quantity_and_rmb_account_money(self):
        item = {'key': 'global:TEST', 'route': 'global', 'action': 'HOLD', 'target_bps': 900,
                'qty': 100_000_000, 'cost_cents': 10000, 'can_increase': True, 'source_id': 'plan'}
        receipt = self.make_audit(item)
        text = '\n'.join(audit.render_publication_item({'payload': {'publication_audit': receipt}}, item))
        self.assertIn('每100,000,000单位代表1个实际资产单位', text)
        self.assertEqual(text.count('实际资产数量：1。'), 2)
        self.assertIn('净值（人民币分）10000000', text)

    def test_malformed_adjustment_codes_and_basis_do_not_break_archive(self):
        plan = self.plan()
        self.decision('malformed', fixtures.AT, frozen=plan)
        def change(p):
            receipt = self.make_audit(p['decisions'][0])
            receipt['decisions'][0]['adjustment_codes'] = [{'unexpected': 'value'}, ['bad'], 'UNKNOWN_CODE']
            receipt['decisions'][0]['analysis_basis']['recorded_blockers'] = {'wrong': 'type'}
            p['publication_audit'] = receipt
        self.update_payload('malformed', change)
        _, _, archive, data = self.archive()
        self.assertEqual(data['coverage']['report_audit']['activity']['incomplete_publication_audit_items'], 1)
        self.assertIn('格式异常的调整码', archive.read('研究策略.md').decode())

    def test_audit_index_is_lossless_nonmutating_and_compatible_with_archive_reader(self):
        plan = self.plan()
        self.decision('baseline', fixtures.BEFORE, sent=True)
        self.decision('old', fixtures.START, frozen=plan)
        self.decision('new', fixtures.AT, current=900, frozen=plan)
        for identity in ('baseline', 'new'):
            self.update_payload(identity, lambda p: p.update(publication_audit=self.make_audit(p['decisions'][0])))
        original = export.collect(self.db, fixtures.AT)
        original['research']['strategy_reviews'] = []
        before = copy.deepcopy(original)
        projected = audit.export_projection(original)
        self.assertEqual(original, before)
        self.assertEqual(set(projected), set(original))
        self.assertEqual(set(projected['coverage']['publication_audits']), {'baseline', 'new'})
        self.assertEqual(projected['baseline']['payload']['publication_audit_ref'], 'baseline')
        self.assertEqual(projected['published_baseline']['payload']['publication_audit_ref'], 'baseline')
        self.assertNotIn('publication_audit_ref', projected['decisions'][0]['payload'])
        reloaded = json.loads(export._json(projected))
        self.assertEqual(audit.export_projection(reloaded), reloaded)
        self.assertEqual(audit.summarize(reloaded), audit.summarize(original))
        self.assertEqual(export._research(projected), export._research(original))
        self.assertEqual(export._research(reloaded), export._research(json.loads(export._json(original))))
        for old, new in zip([original['baseline'], original['published_baseline'], *original['decisions']],
                            [reloaded['baseline'], reloaded['published_baseline'], *reloaded['decisions']]):
            key = old['payload']['decisions'][0]['key']
            self.assertEqual(audit.publication_item(old, key), audit.publication_item(new, key, reloaded))
        # Reconstruct every inline receipt and compare the entire report, not
        # just rendered fields: unknown fields and nested data cannot disappear.
        restored = copy.deepcopy(reloaded)
        receipts = restored['coverage'].pop('publication_audits')
        for decision in [restored['baseline'], restored['published_baseline'], *restored['decisions']]:
            payload = decision['payload']
            if 'publication_audit_ref' in payload:
                payload['publication_audit'] = receipts[payload.pop('publication_audit_ref')]
        self.assertEqual(restored, original)
        content, name, _, data = self.archive()
        self.assertEqual(data, reloaded)
        meta = {k: data[k] for k in ('generated_at', 'start_at', 'end_at')}
        meta.update(filename=name, sha256=hashlib.sha256(content).hexdigest())
        self.assertEqual(strategy_export_sync._archive(content, meta, fixtures.AT), data)

    def test_audit_references_require_own_identity_and_matching_index(self):
        item = {'key': 'watchlist:test', 'action': 'HOLD', 'target_bps': 900, 'qty': 1,
                'cost_cents': 10000, 'can_increase': True, 'source_id': 'plan'}
        receipt = self.make_audit(item)
        decision = {'record': {'id': 'own'}, 'payload': {'publication_audit_ref': 'own', 'decisions': [item]}}
        data = {'coverage': {'publication_audits': {'own': receipt, 'other': receipt}}, 'decisions': [decision]}
        self.assertEqual(audit.publication_item(decision, item['key'], data)[0], receipt)
        for reference, message in (('other', '编号不一致'), (None, '编号不一致'), ([], '编号不一致')):
            wrong = copy.deepcopy(decision)
            wrong['payload']['publication_audit_ref'] = reference
            self.assertEqual(audit.publication_item(wrong, item['key'], data), (None, None))
            self.assertIn(message, '\n'.join(audit.render_publication_item(wrong, item, data)))
            with self.assertRaisesRegex(ValueError, '编号不一致'):
                audit.export_projection({**data, 'decisions': [wrong]})
        missing = {**data, 'coverage': {'publication_audits': {'other': receipt}}}
        self.assertEqual(audit.publication_item(decision, item['key'], missing), (None, None))
        self.assertIn('集中记录缺失', '\n'.join(audit.render_publication_item(decision, item, missing)))
        with self.assertRaisesRegex(ValueError, '集中记录缺失'):
            audit.export_projection(missing)
        inline = copy.deepcopy(decision)
        inline['payload']['publication_audit'] = copy.deepcopy(receipt)
        self.assertEqual(audit.publication_item(inline, item['key'], data)[0], receipt)
        inline['payload']['publication_audit']['version'] = True
        self.assertEqual(audit.publication_item(inline, item['key'], data), (None, None))
        self.assertIn('冲突', '\n'.join(audit.render_publication_item(inline, item, data)))
        with self.assertRaisesRegex(ValueError, '冲突'):
            audit.export_projection({**data, 'decisions': [inline]})

    def test_projection_preserves_all_receipt_fields_and_rejects_duplicate_identity_conflicts(self):
        receipt = {'version': 9, 'future_field': {'nested': [True, 1, None, '原记录']}}
        first = {'record': {'id': 'same'}, 'payload': {'publication_audit': receipt}}
        data = {'coverage': {'existing': 'unchanged'}, 'baseline': first,
                'published_baseline': copy.deepcopy(first), 'decisions': []}
        projected = audit.export_projection(data)
        self.assertEqual(projected['coverage']['publication_audits']['same'], receipt)
        self.assertEqual(data['baseline']['payload']['publication_audit'], receipt)
        data['published_baseline']['payload']['publication_audit']['future_field']['nested'][0] = 1
        before = copy.deepcopy(data)
        with self.assertRaisesRegex(ValueError, '冲突'):
            audit.export_projection(data)
        self.assertEqual(data, before)


if __name__ == '__main__':
    unittest.main()
