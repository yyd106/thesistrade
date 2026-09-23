"""One materiality policy for downloading, reading and invalidating buy plans."""
import re

POLICY_VERSION = 'decision_evidence_v1'
CORPORATE_ACTION = r'权益分派|分红派息|除权|除息|送转|配股|拆股|合并股份'
RISK = r'立案|处罚|诉讼|仲裁|退市|风险警示|违约|冻结|重大资产|重组|停牌|财务重述|更正|留置'
MATERIAL = r'业绩预告|业绩快报|减持|增持|回购|许可协议|临床|药品|关联交易|重大合同|中标|收购|募集说明书|募集资金|担保|股权转让|对外投资'
ROUTINE = r'法律意见|会议决议|股东[大会]+通知|股东会的通知|召开.*股东|会议资料|公司章程|议事规则|管理制度|登记制度|说明会.*公告|参加.*说明会|集体接待|英文版|年度.*评估报告'
KEY_SECTIONS = re.compile(r'主要会计数据|主要财务指标|营业收入|营业总收入|归属.*净利润|经营活动.*现金流|资产负债表|利润表|现金流量表|重大风险|风险因素|重要事项|收入构成|主营业务|合同负债|研发投入|临床|期中分析|主要客户|关联交易|审计意见')


def periodic(title):
    text = re.sub(r'\s+', '', title)
    return bool(re.search(r'\d{4}年(?:半年度|年度|第一季度|第三季度|一季度|三季度)报告(?:全文)?(?:[（(](?:修订|更新|更正)(?:版|稿|后)?[)）])?$', text))


def classify(title, kind='', symbol=None):
    if kind in ('financial_data',):
        return {'level':'BASELINE','priority':0,'required':False,'reason':'按报告期维护的财务底稿'}
    if re.search(CORPORATE_ACTION,title):
        return {'level':'CORPORATE_ACTION','priority':0,'required':True,'reason':'需核对公司行为及价格可比性'}
    if re.search(RISK,title):
        return {'level':'RISK','priority':0,'required':True,'reason':'需核验实质风险与后续进展'}
    if periodic(title):
        return {'level':'CORE_REPORT','priority':1,'required':True,'reason':'定期报告的核心经营、财务与风险证据'}
    # Routine attachments do not inherit the importance of the transaction they accompany.
    if re.search(ROUTINE,title) or re.search(r'(?:年度|季度)报告摘要',title):
        return {'level':'REFERENCE','priority':4,'required':False,'reason':'程序性或摘要资料，按需展开'}
    if re.search(MATERIAL,title):
        return {'level':'MATERIAL','priority':2,'required':True,'reason':'可能改变经营、资金或事件判断'}
    if symbol=='MARKET':
        return {'level':'CONTEXT','priority':3,'required':False,'reason':'行业参考，不能单独证明公司敞口'}
    # Unknown company disclosures require an initial review; don't whitelist by omission.
    return {'level':'COMPANY_UPDATE','priority':3,'required':True,'reason':'公司新增披露需先核对实质内容'}


def priority_chunks(doc, chunks):
    """Core pages first; page ordinals remain intact for receipts and quotations."""
    if classify(doc['title'],doc['kind'],doc['symbol'])['level']=='CORE_REPORT':
        return sorted(chunks,key=lambda c:(not bool(KEY_SECTIONS.search(c['text'])),c['ordinal']))
    return chunks


def document_policy(doc):
    policy=classify(doc['title'],doc['kind'],doc['symbol'])
    if doc['symbol']=='MARKET':
        policy={'level':'MATERIAL_EXTERNAL' if doc.get('research_required') else 'CONTEXT',
            'priority':2 if doc.get('research_required') else 3,'required':bool(doc.get('research_required')),
            'reason':'直接涉及公司的外部报道需核对' if doc.get('research_required') else '具体业务相关参考，不单独构成交易阻断'}
    return policy


def required_chunks(doc, chunks):
    policy=document_policy(doc)
    if not policy['required']:return []
    if policy['level']=='CORE_REPORT':
        matched=[c for c in chunks if KEY_SECTIONS.search(c['text'])]
        # No identified section never means complete: use the full report as the fallback.
        return matched or chunks
    return chunks


def material_document(doc):
    return document_policy(doc)['required']
