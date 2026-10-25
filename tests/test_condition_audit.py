"""Condition receipts are observational and never change portfolio authority."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from ashare import condition_audit as ca, portfolio_strategy as ps
import test_portfolio_strategy as portfolio_fixtures
import test_investment_policy as investment_fixtures
import test_config as defaults


AT = '2026-09-15T02:00:00+00:00'


def fixture(action='ALLOW', current=0, target=500, qty=0):
    c = {'key': 'watchlist:test', 'symbol': 'test', 'current_bps': current,
         'current_value_cents': current * 1000, 'qty': qty, 'cost_cents': current * 1000,
         'max_bps': 2000, 'can_increase': True, 'source_id': 'plan', 'source_valid_until': AT,
         'research': {'kind': 'PAPER_TRADE', 'blockers': []}}
    packet = {'as_of': AT, 'candidates': [c], 'halted': False, 'valuation_ready': True,
              'gross_cap_bps': 9000, 'symbol_cap_bps': 2000,
              'account': {k: 10000000 if k == 'equity_cents' else 0 for k in
                          ('cash_cents','equity_cents','available_cents','reserved_cents','withdrawal_reserved_cents','market_value_cents')}}
    choices = [{'key': c['key'], 'action': action, 'target_bps': target}]
    return packet, copy.deepcopy(packet), choices, {'decisions': copy.deepcopy(choices), 'risk_groups': []}


class ConditionAuditTests(unittest.TestCase):
    def test_catalog_is_versioned_by_receipt_and_explains_existing_thresholds(self):
        rows = ca.catalog()
        self.assertEqual({r['id'] for r in rows}, set(ca.CHECK_IDS + ca.RECHECK_IDS))
        self.assertEqual(len(rows), len(ca.CHECK_IDS + ca.RECHECK_IDS))
        for row in rows:
            self.assertEqual(set(row), {'id','category','actions','description','threshold_source','unit'})
            self.assertTrue(row['actions'])
            self.assertTrue(set(row['actions']) <= set(ca.ACTIONS))
        self.assertIn('100', next(r['threshold_source'] for r in rows if r['id']=='REDUCE_POSITION_TARGET'))

    def test_applicable_legal_checks_and_portfolio_groups_preserve_input(self):
        packet, fresh, choices, result = fixture()
        for action, current, target, qty in (('HOLD',300,300,100),('PAUSE',0,0,0),('REDUCE',500,300,100),('EXIT',200,0,100)):
            p, _, selected, _ = fixture(action,current,target,qty)
            p['candidates'][0].update(key='watchlist:'+action,symbol=action)
            selected[0]['key']='watchlist:'+action
            packet['candidates'] += p['candidates'];choices += selected
        fresh=copy.deepcopy(packet)
        result={'decisions':copy.deepcopy(choices),'risk_groups':[{'keys':['watchlist:test','watchlist:HOLD'],'max_bps':1000}]}
        before=copy.deepcopy((packet,fresh,choices,result))
        receipts=ca.build(packet,fresh,choices,result,AT)
        self.assertEqual((packet,fresh,choices,result),before)
        for row in receipts.values():
            self.assertTrue(ca.validate_receipt(row),row)
            self.assertEqual(row['evaluation'],'VALIDATION_REPLAY')
            self.assertEqual(row['hit_ids'],[])
            self.assertEqual(row['hit_attribution'],'NOT_RECORDED')
            self.assertEqual(row['model_trigger'],'NOT_RECORDED')
            self.assertEqual(row['model_termination'],'NOT_RECORDED')
            self.assertEqual(row['execution'],'NOT_CAPTURED')
            self.assertFalse(set(row['analysis'].values()) & {'FAIL','UNKNOWN'})
        self.assertEqual(receipts['watchlist:test']['analysis']['RISK_GROUP_CAP'],'PASS')
        self.assertEqual(receipts['watchlist:EXIT']['analysis']['RISK_GROUP_CAP'],'NOT_APPLICABLE')
        self.assertEqual(receipts['watchlist:EXIT']['analysis']['REDUCE_POSITION_TARGET'],'NOT_APPLICABLE')

    def test_failed_replay_does_not_invent_observed_branch_hits(self):
        packet,fresh,choices,result=fixture()
        fresh['candidates'][0]['can_increase']=False
        result['decisions'][0].update(action='PAUSE',target_bps=0)
        row=ca.build(packet,fresh,choices,result,AT)['watchlist:test']
        self.assertEqual(row['analysis']['ALLOW_AUTHORIZATION'],'PASS')
        self.assertEqual(row['publication']['ALLOW_AUTHORIZATION'],'NOT_APPLICABLE')
        self.assertEqual(row['publication_recheck']['PUB_ALLOW_GUARD'],'FAIL')
        self.assertEqual(row['hit_ids'],[])
        self.assertEqual(row['hit_attribution'],'NOT_RECORDED')
        observed={'watchlist:test':['PUB_ALLOW_GUARD','PUB_PASSIVE_REBASE']}
        row=ca.build(packet,fresh,choices,result,AT,observed)['watchlist:test']
        self.assertTrue(ca.validate_receipt(row))
        self.assertEqual(row['hit_ids'],observed['watchlist:test'])
        self.assertEqual(row['hit_attribution'],'PROGRAM_OBSERVED')

    def test_missing_aggregate_or_condition_data_remains_unknown(self):
        packet,fresh,choices,result=fixture()
        result.pop('risk_groups');packet['candidates'][0].pop('can_increase')
        row=ca.build(packet,fresh,choices,result,AT)['watchlist:test']
        self.assertEqual(row['analysis']['ALLOW_AUTHORIZATION'],'UNKNOWN')
        self.assertEqual(row['analysis']['RISK_GROUP_CAP'],'UNKNOWN')
        self.assertEqual(row['publication']['RISK_GROUP_CAP'],'UNKNOWN')
        self.assertEqual(row['status'],'DEGRADED')
        self.assertFalse(ca.validate_receipt(row))

    def test_replay_detects_constraints_without_changing_actions(self):
        packet,fresh,choices,result=fixture('REDUCE',500,0,100)
        row=ca.build(packet,fresh,choices,result,AT)['watchlist:test']
        self.assertEqual(row['analysis']['REDUCE_POSITION_TARGET'],'FAIL')
        self.assertEqual(row['analysis']['EXIT_POSITION_TARGET'],'NOT_APPLICABLE')
        self.assertEqual(choices[0]['action'],'REDUCE')
        self.assertEqual(choices[0]['target_bps'],0)
        packet,fresh,choices,result=fixture('ALLOW',0,2500)
        packet['gross_cap_bps']=1000
        result['risk_groups']=[{'keys':['watchlist:test'],'max_bps':200}]
        row=ca.build(packet,fresh,choices,result,AT)['watchlist:test']
        for key in ('ALLOW_AUTHORIZATION','PORTFOLIO_TOTAL_CAP','SYMBOL_TOTAL_CAP','RISK_GROUP_CAP'):
            self.assertEqual(row['analysis'][key],'FAIL')

    def test_projection_filters_nested_arbitrary_values_and_invalid_receipts(self):
        packet,fresh,choices,result=fixture()
        receipt=ca.build(packet,fresh,choices,result,AT,{'watchlist:test':[]})['watchlist:test']
        self.assertEqual(ca.project(receipt),receipt)
        dirty=copy.deepcopy(receipt)
        dirty.update(input='PRIVATE',error_code='PRIVATE')
        dirty['analysis'].update(ACTION_TARGET_BOUNDS={'body':'PRIVATE'},UNKNOWN_ID='PRIVATE')
        dirty['hit_ids']=[{'body':'PRIVATE'},'PRIVATE']
        projected=ca.project(dirty)
        self.assertNotIn('PRIVATE',json.dumps(projected))
        self.assertFalse(ca.validate_receipt(projected))
        for version in (True,2,None):
            self.assertFalse(ca.validate_receipt({**receipt,'version':version}))
        self.assertFalse(ca.validate_receipt(ca.degraded()))
        dishonest=copy.deepcopy(receipt);dishonest['hit_ids']=['PUB_ALLOW_GUARD']
        self.assertFalse(ca.validate_receipt(dishonest))
        for change in (lambda r:r.update(error_code='AUDIT_UNAVAILABLE'),
                       lambda r:r['analysis'].update(ACTION_TARGET_BOUNDS='FAIL'),
                       lambda r:r['analysis'].update(UNKNOWN_CONDITION='PASS'),
                       lambda r:r.update(hit_ids=['UNKNOWN_HIT']),
                       lambda r:r.update(catalog_version='PRIVATE_CATALOG_TEXT')):
            malformed=copy.deepcopy(receipt);change(malformed)
            self.assertFalse(ca.validate_receipt(malformed))
            filtered=ca.project(malformed)
            self.assertEqual(filtered['status'],'DEGRADED')
            self.assertFalse(ca.validate_receipt(filtered))
            self.assertNotIn('PRIVATE_CATALOG_TEXT',json.dumps(filtered))


class ConditionPublicationIntegrationTests(unittest.TestCase):
    quote=portfolio_fixtures.PortfolioStrategyTests.quote
    plan=portfolio_fixtures.PortfolioStrategyTests.plan
    later=portfolio_fixtures.PortfolioStrategyTests.later
    output=portfolio_fixtures.PortfolioStrategyTests.output
    approve=portfolio_fixtures.PortfolioStrategyTests.approve
    enter=portfolio_fixtures.PortfolioStrategyTests.enter
    fill=portfolio_fixtures.PortfolioStrategyTests.fill

    def setUp(self):
        root=Path(__file__).resolve().parents[1]
        safe=patch.object(investment_fixtures,'load_config',lambda *_a,**_k:defaults.load_config(root/'config.example.json'))
        safe.start();self.addCleanup(safe.stop)
        portfolio_fixtures.PortfolioStrategyTests.setUp(self)
        self.addCleanup(portfolio_fixtures.PortfolioStrategyTests.tearDown,self)

    def run_model(self, model, at=None, completed=None):
        at=at or self.at;completed=completed or at
        result=ps.run(self.store,self.cfg,at,model_fn=model,clock=lambda:completed)
        self.assertEqual(result['status'],'SUCCEEDED',result)
        return ps.active(self.store,completed)['payload']

    def test_actual_expiry_branch_ids_and_audit_failure_leave_authority_unchanged(self):
        self.quote();plan,_=self.plan()
        with self.store.db:self.store.db.execute('UPDATE global_plans SET valid_until=? WHERE id=?',(self.later(1),plan['id']))
        prompts=[]
        def model(prompt,*_):
            prompts.append(prompt)
            packet=json.loads(prompt.split('<UNTRUSTED_INPUT>')[1].split('</UNTRUSTED_INPUT>')[0])
            return self.output(packet,{'global:BTC':{'action':'ALLOW','target_bps':100}})
        full=self.run_model(model,completed=self.later(2))
        item=next(d for d in full['publication_audit']['decisions'] if d['key']=='global:BTC')
        self.assertEqual(item['condition_audit']['hit_ids'],['PUB_ALLOW_GUARD','PUB_PASSIVE_REBASE'])
        self.assertTrue(ca.validate_receipt(item['condition_audit']))
        self.assertEqual(item['published_action'],'PAUSE')
        for broken in (None,{},RuntimeError('PRIVATE_ERROR')):
            args={'side_effect':broken} if isinstance(broken,Exception) else {'return_value':broken}
            with patch('ashare.condition_audit.build',**args):
                limited=self.run_model(model,completed=self.later(2))
            self.assertEqual(limited['decisions'],full['decisions'])
            receipt=next(d['condition_audit'] for d in limited['publication_audit']['decisions'] if d['key']=='global:BTC')
            self.assertEqual(receipt['status'],'DEGRADED')
            self.assertEqual(receipt['hit_attribution'],'NOT_RECORDED')
            self.assertNotIn('PRIVATE_ERROR',json.dumps(limited))
        self.assertEqual(len(set(prompts)),1)
        self.assertNotIn('condition_audit',prompts[0])

    def test_actual_reduce_tightening_and_hold_rebase_without_spurious_noop_hits(self):
        _,_,instrument=self.enter();self.fill(instrument)
        at=self.later(120);self.quote(at=at)
        def moving(prompt,*_):
            packet=json.loads(prompt.split('<UNTRUSTED_INPUT>')[1].split('</UNTRUSTED_INPUT>')[0])
            current=next(c['current_bps'] for c in packet['candidates'] if c['key']=='global:BTC')
            self.quote(price=99,at=self.later(121))
            return self.output(packet,{'global:BTC':{'action':'REDUCE','target_bps':current-100}})
        payload=self.run_model(moving,at,self.later(121))
        item=next(d for d in payload['publication_audit']['decisions'] if d['key']=='global:BTC')
        self.assertEqual(item['condition_audit']['hit_ids'],['PUB_REDUCTION_GUARD','PUB_PASSIVE_REBASE'])
        self.assertTrue(ca.validate_receipt(item['condition_audit']))
        def hold(prompt,*_):
            return self.output(json.loads(prompt.split('<UNTRUSTED_INPUT>')[1].split('</UNTRUSTED_INPUT>')[0]))
        payload=self.run_model(hold,self.later(122))
        for item in payload['publication_audit']['decisions']:
            self.assertEqual(item['condition_audit']['hit_ids'],[])
            self.assertEqual(item['condition_audit']['hit_attribution'],'PROGRAM_OBSERVED')


if __name__=='__main__':
    unittest.main()
