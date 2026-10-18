"""Local news routing hints, never evidence of exposure or permission to trade.

Only current research identities activate these public vocabulary hints. Account
values and the resulting local rank are not part of the model's news packet.
"""
import re

# Public aliases and business vocabulary supplement operator-supplied Chinese
# keywords. This is a vocabulary, not another watchlist or an exposure model.
COMPANIES = {
    'sh600031': ('Sany Heavy Industry|三一重工', '工程机械|挖掘机|construction machinery|excavator'),
    'sh600309': ('Wanhua Chemical|万华化学', '聚氨酯|MDI|TDI|polyurethane'),
    'sh600519': ('Kweichow Moutai|贵州茅台|茅台', '白酒|baijiu|liquor tax'),
    'sh600900': ('China Yangtze Power|长江电力', '水电|水力发电|hydropower|hydroelectric'),
    'sh600941': ('China Mobile|中国移动', '移动通信|通信资本开支|mobile network|telecom capital expenditure'),
    'sh600989': ('Ningxia Baofeng Energy|Baofeng Energy|宝丰能源', '煤化工|煤制烯烃|coal to olefins|methanol|polyethylene|polypropylene'),
    'sh601179': ('China XD Electric|中国西电', '输配电|特高压|变压器|power grid|power transformer|switchgear'),
    'sh601888': ('China Tourism Group Duty Free|China Duty Free|中国中免', '免税|duty free|duty-free'),
    'sh601899': ('Zijin Mining|紫金矿业', '铜矿|铜价|金价|copper mine|copper price|gold mining'),
    'sh603217': ('Yuanli Chemical|元利科技', '精细化工|二元酸酯|dibasic ester|fine chemical'),
    'sh605016': ('Bailong Chuangyuan|百龙创园', '代糖|阿洛酮糖|膳食纤维|allulose|dietary fiber|dietary fibre'),
    'sh688062': ('Mabwell|迈威生物', '创新药|生物制药|临床|clinical trial|biopharma|drug approval'),
    'sz000333': ('Midea Group|美的集团', '家电|空调|压缩机|home appliance|air conditioner|compressor'),
    'sz000651': ('Gree Electric|格力电器', '家电|空调|制冷设备|home appliance|air conditioner|refrigeration'),
    'sz002028': ('Sieyuan Electric|思源电气', '输配电|特高压|变压器|power grid|power transformer|switchgear'),
    'sz002415': ('Hikvision|海康威视', '安防|视频监控|机器视觉|video surveillance|machine vision'),
    'sz002957': ('Colibri Technologies|科瑞技术', '工业自动化|锂电设备|industrial automation|battery manufacturing equipment'),
    'sz300499': ('Goaland|高澜股份', '液冷|数据中心冷却|liquid cooling|data center cooling|data centre cooling'),
    'sz300760': ('Mindray|迈瑞医疗', '医疗器械|医院采购|体外诊断|medical device|hospital procurement|in vitro diagnostic'),
    'US:NVDA': ('NVIDIA|英伟达', 'GPU|graphics processor|data center|data centre|人工智能|算力'),
    'US:TSLA': ('Tesla|特斯拉', 'electric vehicle|battery storage|电动汽车|储能'),
}
ASSETS = {
    'GOLD': ('黄金|金价|gold|XAU', '贵金属|precious metal|central bank gold'),
    'SILVER': ('白银|银价|silver|XAG', '贵金属|precious metal|solar demand'),
    'BTC': ('比特币|bitcoin|BTC', '加密资产|加密货币|cryptocurrency|crypto regulation|digital asset'),
    'ETH': ('以太坊|ethereum|ether|ETH', '加密资产|加密货币|cryptocurrency|crypto regulation|digital asset'),
}
SECTORS = {
    'consumer': '家电|消费|白酒|零售|home appliance|retail sales',
    'technology': '半导体|机器人|自动化|液冷|数据中心|semiconductor|robotics|industrial automation|liquid cooling|data center|data centre',
    'healthcare': '医疗器械|医院采购|药品审评|临床|medical device|hospital procurement|clinical trial|drug approval',
    'food': '食品|代糖|糖醇|膳食纤维|allulose|dietary fiber|food ingredient',
    'power': '电网|变压器|特高压|输配电|power grid|power transformer|switchgear',
    'AI 算力基础设施': '数据中心|算力|液冷|data center|data centre|GPU|liquid cooling',
    '机器人与工业自动化': '机器人|工业自动化|robotics|industrial automation',
    '半导体设备材料与先进封装': '半导体|先进封装|semiconductor|advanced packaging',
}
MACRO_ASSET_THEMES = {'MONETARY', 'INFLATION', 'GROWTH', 'GEOPOLITICS', 'TRADE', 'ENERGY'}


def phrase(term):
    """ASCII names/terms must be whole tokens; Chinese names can adjoin prose."""
    term = term.strip()
    if not term:
        return None
    left = r'(?<![A-Za-z0-9_])' if term[0].isascii() and term[0].isalnum() else ''
    right = r'(?![A-Za-z0-9_])' if term[-1].isascii() and term[-1].isalnum() else ''
    return re.compile(left + re.escape(term) + right, re.I)


def ticker(symbol):
    if re.fullmatch(r'(sh|sz)\d{6}', symbol):
        return re.compile(r'(?<![A-Za-z0-9_])(?:' + symbol[:2] + r')?' + symbol[2:] + r'(?![A-Za-z0-9_])', re.I)
    if re.fullmatch(r'US:[A-Z][A-Z0-9.-]{0,9}', symbol):
        code = re.escape(symbol[3:])
        # Short tickers are also ordinary words (ON, IT, CAT). Require an
        # explicit market/cashtag marker instead of treating prose as a symbol.
        prefix = r'(?:\$|US:|NASDAQ:\s*|NYSE:\s*)'
        if len(symbol[3:]) >= 4:
            prefix = '(?:' + prefix + ')?'
        return re.compile(r'(?<![A-Za-z0-9_])' + prefix + code + r'(?![A-Za-z0-9_])')
    return None


def profiles(store, config, at):
    """Resolve existing membership APIs, retaining identities, never balances."""
    from .universe import membership
    from .global_market import targets
    from .observation import view
    from .investment_policy import FIXED, enabled
    from .events import BUSINESS_TERMS, DEFAULT_PROFILES
    config = {**(config or {}), 'watchlist': (config or {}).get('watchlist', [])}
    current = {m['symbol']: m for m in membership(store, config, at)
               if m['membership'] == 'CORE' or m.get('buy_eligible') or m.get('protected')}
    for symbol, item in targets(store, config, at).items():
        current[symbol] = {**current.get(symbol, {}), **item}
    # Membership records carry domains but not their live causal links. Retain
    # current admitted themes for CN as well as US targets, without reviving
    # expired hypotheses merely because a historical domain is still listed.
    for item in view(store, at, config)['items']:
        if item['asset'] in current:
            current[item['asset']] = {**current[item['asset']], 'links': item.get('links', [])}
    if enabled(config):
        for symbol, item in FIXED.items():
            current.setdefault(symbol, item)
    result = []
    for symbol, item in sorted(current.items()):
        defaults = COMPANIES.get(symbol, ASSETS.get(symbol, ('', '')))
        name = item.get('name', symbol)
        names = defaults[0].split('|') + ([name] if name != symbol and len(name) >= 2 else [])
        business = config.get('business_keywords', {}).get(symbol)
        business = business if business is not None else BUSINESS_TERMS.get(symbol, '').split('|')
        terms = list(business) + defaults[1].split('|')
        sectors = config.get('research_topics', {}).get(symbol, DEFAULT_PROFILES.get(symbol, []))
        for sector in list(sectors) + item.get('domains', []):
            terms += SECTORS.get(sector, '').split('|')
        themes = {l.get('theme') for l in item.get('links', [])
                  if l.get('status') == 'TRACKING' and l.get('review_state') == 'CURRENT'
                  and (l.get('materiality') or {}).get('admitted')}
        result.append({'symbol': symbol, 'name': name, 'ticker': ticker(symbol),
                       'names': [phrase(t) for t in dict.fromkeys(names) if t],
                       'terms': [phrase(t) for t in dict.fromkeys(terms) if len(t) >= 2],
                       'themes': themes, 'macro_themes': MACRO_ASSET_THEMES if symbol in ASSETS else set()})
    return result


def assess(news, current, topics=()):
    text = news['title'] + '\n' + news['body']
    direct, business, linked, thematic = [], [], [], []
    for item in current:
        match = next((p.search(text) for p in item['names'] if p.search(text)), None)
        if not match and item['ticker']:
            match = item['ticker'].search(text)
        if match:
            direct.append((item['symbol'], match.group(0)))
        else:
            match = next((p.search(text) for p in item['terms'] if p.search(text)), None)
            if match:
                business.append((item['symbol'], match.group(0)))
            elif item['themes'].intersection(topics):
                linked.append((item['symbol'], sorted(item['themes'].intersection(topics))[0]))
            elif item['macro_themes'].intersection(topics):
                thematic.append((item['symbol'], sorted(item['macro_themes'].intersection(topics))[0]))
    companies = [m for m in direct if m[0] not in ASSETS]
    kind, score, matches = ('COMPANY', 60, companies) if companies else ('ASSET', 40, direct) if direct else ('BUSINESS', 24, business) if business else ('LINKED_THEME', 12, linked) if linked else ('THEME', 12, thematic) if thematic else ('DISCOVERY', 0, [])
    labels = {'COMPANY': '当前公司直接提及', 'ASSET': '当前资产直接提及', 'BUSINESS': '当前研究业务关键词', 'LINKED_THEME': '当前已接纳因果主题', 'THEME': '当前资产宏观背景', 'DISCOVERY': '新事件候选'}
    reason = labels[kind] + ('：' + '；'.join(symbol + ' / ' + word for symbol, word in matches[:3]) if matches else '')
    return {'kind': kind, 'score': score, 'related': bool(matches),
            'priority_related': kind not in ('THEME', 'DISCOVERY'), 'reason': reason}
