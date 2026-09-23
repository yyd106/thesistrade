"""Candidate event-to-stock routing. A keyword match is not confirmed economic exposure."""
import json
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher

TOPICS = {
    'geopolitics': ('地缘与运输风险', r'战争|战事|冲突|军事|袭击|停火|中东|红海|霍尔木兹|乌克兰|航运|海峡|地缘',
                    '事件变化 → 能源、运输或风险偏好 → 公司成本、订单或估值；实际敞口待核实'),
    'liquidity': ('宏观与资金环境', r'降息|加息|利率|通胀|汇率|货币政策|流动性|经济衰退',
                  '利率、汇率或需求变化 → 融资成本、终端需求和估值；方向与程度待核实'),
    'trade': ('贸易与出口规则', r'关税|制裁|出口管制|贸易限制|反倾销|自贸|国际贸易|对外贸易',
              '贸易规则变化 → 出口、采购或竞争格局；涉及地区及收入占比需公司披露验证'),
    'consumer': ('消费与家电', r'消费|家电|家居|以旧换新|白酒|零售', '消费政策或需求变化 → 销量、价格和利润率'),
    'technology': ('科技与设备', r'半导体|芯片|人工智能|机器人|自动化|工业设备|制造业|安防', '技术与设备政策 → 客户投资、采购限制和订单'),
    'healthcare': ('医药与医疗', r'医药|药品|生物制药|临床|医保|医疗|药物', '研发、审评和支付政策 → 产品进度、市场空间和现金需求'),
    'food': ('食品与原料', r'食品|代糖|糖醇|淀粉|玉米|粮食|农产品|营养', '原料与食品需求变化 → 成本、销量和毛利率'),
    'power': ('电网与能源', r'电网|输配电|变压器|电力|新能源|能源|原油|成品油|天然气', '能源价格或电力投资变化 → 原料成本、投资计划和设备需求'),
}
DEFAULT_PROFILES = {
    'sh600519': ['consumer','food'], 'sz000333': ['consumer','technology','power'],
    'sz002415': ['technology'], 'sz002957': ['technology'], 'sh688062': ['healthcare'],
    'sh605016': ['food','consumer'], 'sz002028': ['power','technology'],
}
GLOBAL_TOPICS = ['geopolitics','liquidity','trade']
BUSINESS_TERMS = {
    'sh600519':r'茅台|白酒|酒类|酒精税|消费税',
    'sz000333':r'美的集团|家电|空调|白电|智能家居|以旧换新|压缩机',
    'sz002415':r'海康威视|安防|视频监控|机器视觉|智能物联',
    'sz002957':r'科瑞技术|工业自动化|锂电设备|消费电子|自动化设备|智能制造装备',
    'sh688062':r'迈威生物|创新药|生物制药|药物临床|医保药品|药品审评|药品支付',
    'sh605016':r'百龙创园|代糖|功能糖|阿洛酮糖|糖醇|膳食纤维|淀粉|玉米',
    'sz002028':r'思源电气|输配电|变压器|电网|特高压|开关设备|铜价|铝价',
}


def profile(config,symbol):
    sector=config.get('research_topics',{}).get(symbol,DEFAULT_PROFILES.get(symbol,[]))
    return list(dict.fromkeys(GLOBAL_TOPICS+sector))


def classify(text):
    return [key for key,(_,pattern,_) in TOPICS.items() if re.search(pattern,text)]


def relevance(config,symbol,title,text=''):
    names=[i['name'] for i in config.get('watchlist',[]) if i['symbol']==symbol and len(i.get('name',''))>=3]
    terms=config.get('business_keywords',{}).get(symbol)
    pattern='|'.join(re.escape(t) for t in terms) if terms is not None else BUSINESS_TERMS.get(symbol,'')
    candidate=title+'\n'+text[:1800]
    if any(name in candidate for name in names) or re.search(r'(?<!\d)'+symbol[2:]+r'(?!\d)',candidate):return 'COMPANY'
    if pattern and re.search(pattern,candidate):return 'BUSINESS'
    return 'BACKGROUND'


def routed(store,config,symbol,at):
    """Keep background news out of the company prompt and deterministic entry gates."""
    own=store.documents_as_of(at,symbol)
    topics=profile(config,symbol)
    cutoff=(datetime.fromisoformat(at)-timedelta(days=config.get('external_news_lookback_days',30))).isoformat()
    shared=[];links=[];background=[];clusters=[]
    for d in store.documents_as_of(at,'MARKET'):
        if d['kind']=='news_index' or d['published_at']<cutoff or not d['cloud_allowed']:
            continue
        text='\n'.join(r[0] for r in store.db.execute('SELECT text FROM chunks WHERE doc_id=? ORDER BY ordinal',(d['id'],)))
        matched=[t for t in classify(d['title']+'\n'+text) if t in topics]
        # Business terms are an explicit, editable profile. Global-topic matches alone never qualify.
        match=relevance(config,symbol,d['title'],text)
        direct=match=='COMPANY';business=match=='BUSINESS'
        if not direct and not business:
            background.append({'doc_id':d['id'],'title':d['title'],'url':d['url'],
                'reason':'未匹配公司或具体业务，仅作公共背景，不进入个股结论及交易阻断'})
            continue
        normalized=re.sub(r'【[^】]*】|\s+','',d['title'])
        duplicate=next((x for x in clusters if x['url']!=d['url'] and SequenceMatcher(None,x['title'],normalized).ratio()>=.92),None)
        if duplicate:
            background.append({'doc_id':d['id'],'title':d['title'],'url':d['url'],'reason':'同事件近似报道，主研究保留一个来源；原文可展开核对'})
            continue
        clusters.append({'url':d['url'],'title':normalized})
        from .materiality import classify as importance
        material=direct and importance(d['title'])['required']
        shared.append({**d,'research_required':material})
        links.append({'doc_id':d['id'],'title':d['title'],'url':d['url'],
            'published_at':d['published_at'],'ready_at':d['ready_at'],
            'topics':[TOPICS[t][0] for t in matched] or ['公司事项'],
            'relevance':'公司直接提及' if direct else '具体业务匹配',
            'required':material,'possible_channels':[TOPICS[t][2] for t in matched],
            'exposure_status':'公司实际敞口仍需披露验证；业务匹配只用于选材，不能据此断言股价方向'})
    return own+shared,links,background


def related(store,config,symbol,at):
    docs,links,_=routed(store,config,symbol,at)
    return docs,links
