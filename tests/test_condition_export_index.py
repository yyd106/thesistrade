"""Content-addressed condition receipts retain every byte of logical JSON data."""
import copy
import json
import unittest

from ashare import portfolio_strategy as portfolio, strategy_export as export
from ashare import strategy_report_audit as audit, strategy_condition_report as report
from test_condition_audit import fixture, AT


def dataset():
    packet, fresh, choices, result = fixture()
    second = copy.deepcopy(packet['candidates'][0])
    second.update(key='watchlist:second', symbol='second')
    packet['candidates'].append(second)
    fresh = copy.deepcopy(packet)
    choices.append({'key':second['key'],'action':'ALLOW','target_bps':500})
    result['decisions'] = copy.deepcopy(choices)
    receipt = portfolio.publication_audit(packet,fresh,choices,result,AT,
                                         observed_hits={c['key']:[] for c in choices})
    items = [{**c,**d,'reason':'目标仓位为5%'} for c,d in zip(fresh['candidates'],choices)]
    decision = {'record':{'id':'one','created_at':AT},'payload':{'decisions':items,'publication_audit':receipt},
                'frozen_history':None,'source_refs':{}}
    second_decision = copy.deepcopy(decision)
    second_decision['record']['id']='two'
    return {'coverage':{},'baseline':copy.deepcopy(decision),'published_baseline':copy.deepcopy(decision),
            'decisions':[decision,second_decision],'frozen_sources':{}}


class ConditionExportIndexTests(unittest.TestCase):
    def test_deduplicates_and_fully_restores_without_mutating_input(self):
        original=dataset();before=copy.deepcopy(original)
        projected=audit.export_projection(original)
        self.assertEqual(original,before)
        self.assertEqual(set(projected),set(original))
        self.assertEqual(len(projected['coverage']['condition_receipts']),1)
        index=projected['coverage']['condition_receipts']
        for reference,receipt in index.items():
            self.assertEqual(reference,audit._condition_digest(receipt))
        for receipt in projected['coverage']['publication_audits'].values():
            for row in receipt['decisions']:
                self.assertNotIn('condition_audit',row)
                self.assertIn(row['condition_audit_ref'],index)
        reloaded=json.loads(export._json(projected))
        self.assertEqual(audit.export_projection(reloaded),reloaded)
        self.assertEqual(audit.summarize(original),audit.summarize(reloaded))
        restored=copy.deepcopy(reloaded)
        for key in ('baseline','published_baseline'):
            expanded,problem=audit._publication_audit(restored[key],restored)
            self.assertIsNone(problem)
            payload=restored[key]['payload'];payload.pop('publication_audit_ref');payload['publication_audit']=expanded
        for decision in restored['decisions']:
            expanded,problem=audit._publication_audit(decision,restored)
            self.assertIsNone(problem)
            payload=decision['payload'];payload.pop('publication_audit_ref');payload['publication_audit']=expanded
        restored['coverage'].pop('publication_audits');restored['coverage'].pop('condition_receipts')
        self.assertEqual(restored,original)

    def test_reader_returns_expanded_parent_and_the_same_row_for_numeric_matching(self):
        original=dataset();projected=audit.export_projection(original)
        before=copy.deepcopy(projected)
        for old,new in zip(original['decisions'],projected['decisions']):
            for item in old['payload']['decisions']:
                old_audit,old_row=audit.publication_item(old,item['key'],original)
                parent,row=audit.publication_item(new,item['key'],projected)
                self.assertEqual((parent,row),(old_audit,old_row))
                self.assertIs(next(d for d in parent['decisions'] if d['key']==item['key']),row)
                self.assertTrue(report.inspect_item(new,item,projected)[1])
                self.assertEqual(report.inspect_item(old,item,original),report.inspect_item(new,item,projected))
                self.assertEqual(report.render_item(old,item,original),report.render_item(new,item,projected))
        self.assertEqual(projected,before)

    def test_unknown_future_receipt_fields_and_orphans_are_lossless(self):
        original=dataset()
        future={'version':999,'unknown':{'nested':[True,1,None,{'condition_audit_ref':'literal future data'}]},
                'large_integer':12345678901234567890}
        for decision in [original['baseline'],original['published_baseline'],*original['decisions']]:
            decision['payload']['publication_audit']['decisions'][0]['condition_audit']=copy.deepcopy(future)
        orphan={'version':'future','text':'未引用但必须保留'}
        orphan_id=audit._condition_digest(orphan)
        original['coverage']['condition_receipts']={orphan_id:orphan}
        projected=audit.export_projection(original)
        self.assertEqual(projected['coverage']['condition_receipts'][orphan_id],orphan)
        self.assertEqual(projected['coverage']['condition_receipts'][audit._condition_digest(future)],future)
        parent,row=audit.publication_item(projected['decisions'][0],'watchlist:test',projected)
        self.assertEqual(row['condition_audit'],future)
        self.assertEqual(audit.export_projection(projected),projected)

    def test_missing_wrong_hash_and_invalid_reference_never_claim_complete(self):
        clean=audit.export_projection(dataset())
        for damage in ('missing','hash','invalid'):
            with self.subTest(damage=damage):
                data=copy.deepcopy(clean)
                row=data['coverage']['publication_audits']['one']['decisions'][0]
                reference=row['condition_audit_ref']
                if damage=='missing':data['coverage']['condition_receipts'].pop(reference)
                elif damage=='hash':data['coverage']['condition_receipts'][reference]['model_trigger']='changed'
                else:row['condition_audit_ref']=None
                before=copy.deepcopy(data);decision=data['decisions'][0];item=decision['payload']['decisions'][0]
                self.assertEqual(audit.publication_item(decision,item['key'],data),(None,None))
                self.assertFalse(report.inspect_item(decision,item,data)[1])
                with self.assertRaisesRegex(ValueError,'条件审计'):
                    audit.export_projection(data)
                self.assertEqual(data,before)

    def test_dual_condition_representations_require_equal_content(self):
        data=audit.export_projection(dataset())
        row=data['coverage']['publication_audits']['one']['decisions'][0]
        row['condition_audit']=copy.deepcopy(data['coverage']['condition_receipts'][row['condition_audit_ref']])
        self.assertIsNotNone(audit.publication_item(data['decisions'][0],row['key'],data)[1])
        compact=audit.export_projection(data)
        self.assertNotIn('condition_audit',compact['coverage']['publication_audits']['one']['decisions'][0])
        row['condition_audit']['version']=True
        before=copy.deepcopy(data)
        self.assertEqual(audit.publication_item(data['decisions'][0],row['key'],data),(None,None))
        with self.assertRaisesRegex(ValueError,'冲突'):
            audit.export_projection(data)
        self.assertEqual(data,before)

    def test_inline_publication_and_indexed_publication_compare_expanded_content(self):
        original=dataset();data=audit.export_projection(original)
        data['decisions'][0]['payload']['publication_audit']=copy.deepcopy(original['decisions'][0]['payload']['publication_audit'])
        self.assertIsNotNone(audit.publication_item(data['decisions'][0],'watchlist:test',data)[1])
        self.assertEqual(audit.export_projection(data),audit.export_projection(original))
        data['decisions'][0]['payload']['publication_audit']['decisions'][0]['condition_audit']['version']=2
        with self.assertRaisesRegex(ValueError,'冲突'):
            audit.export_projection(data)

    def test_old_inline_without_condition_receipts_remains_compatible(self):
        data=dataset()
        for decision in [data['baseline'],data['published_baseline'],*data['decisions']]:
            for row in decision['payload']['publication_audit']['decisions']:
                row.pop('condition_audit')
        projected=audit.export_projection(data)
        self.assertNotIn('condition_receipts',projected['coverage'])
        self.assertEqual(audit.publication_item(data['decisions'][0],'watchlist:test',data),
                         audit.publication_item(projected['decisions'][0],'watchlist:test',projected))


if __name__=='__main__':
    unittest.main()
