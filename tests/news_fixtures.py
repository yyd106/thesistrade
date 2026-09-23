"""Explicit synthetic model for deterministic screening integration tests only."""
import json
from ashare.news_triage import EMPTY_SIGNAL

def screen_deep(prompt,*args):
 packet=json.loads(prompt.split('<UNTRUSTED_NEWS>')[1].split('</UNTRUSTED_NEWS>')[0])
 return {'items':[{'news_id':n['id'],'decision':'DEEP','potential':'HIGH','novelty':'NEW','evidence':'CONFIRMED','stage':'ANNOUNCED','channel':'测试中明确给定的政策改变利率与融资成本，须继续独立审查。','scale_basis':'测试示例中的全国政策范围。','reason':'显式模型测试数据，不作为正式分析。','next_evidence':'需要进一步核实产业敞口。','pricing':'UNKNOWN','expectation_quote':'','quote':n['body'][:80],'expectation_signal':EMPTY_SIGNAL.copy()} for n in packet['news']]}
