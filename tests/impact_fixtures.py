"""Explicit test-only reviewer output; never loaded by the application."""
import json
from ashare import macro_impact

def assessment(item,**changes):
 source=item['sources'][0];quote=(source['body'] or source['title'])[:100]
 empty={'value':None,'unit':'','period':'','source_id':'','quote':''}
 a={'event_id':item['event_id'],'asset':item['asset'],'event_kind':'MONETARY','scope':'GLOBAL','novelty':'INCREMENTAL',
  'exposure':'VERIFIED_DIRECT','target_fit':'MATCH','magnitude':'HIGH','direction':'UP',
  'scale':{'kind':'SYSTEMIC','numerator':empty.copy(),'denominator':empty.copy(),'explanation':'Test-only system-wide policy fixture'},
  'impact_basis':'ECONOMIC','expectation_test':macro_impact.EMPTY_EXPECTATION.copy(),
  'basis':'Test-only independently verified broad policy change','transmission':'Test-only rate transmission','missing_evidence':'Subsequent public confirmation',
  'citations':[{'supports':field,'source_id':source['id'],'quote':quote} for field in ('NOVELTY','EXPOSURE','SCOPE','SCALE')]}
 return {**a,**changes}

def reviewer(prompt,*args):
 items=json.loads(prompt.split('<UNTRUSTED_IMPACT_INPUT>')[1].split('</UNTRUSTED_IMPACT_INPUT>')[0])
 return {'assessments':[assessment(item) for item in items]}

def approve_pending(store,at):
 while macro_impact.candidates(store,at):
  r=macro_impact.review(store,{},at,reviewer)
  if not r['reviewed']:raise AssertionError('Test review unexpectedly deferred')
