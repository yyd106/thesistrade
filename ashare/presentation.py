"""Trader-facing language and outstanding data failures; raw audit records stay intact."""
import re


SOURCE_NAMES = {
    'financials':'公司财务底稿','financial_statement':'财务报表','market_comparison':'量价与市场对照','comparison_series':'业务参考行情',
    'external_news':'外部新闻采集','external_news_list':'行业与国际消息目录','external_news_article':'行业与国际消息正文',
    'un_news':'联合国国际新闻','mofcom_news':'商务部贸易与产业消息',
    'tencent_quotes':'最新行情', 'tencent_daily':'历史价格与均线',
    'cninfo_stock_catalog':'股票与公告来源匹配', 'cninfo_catalog':'公司公告目录',
    'cninfo_pdf':'公告或报告正文', 'official_news':'市场新闻目录', 'news_article':'市场新闻正文',
    'report_inbox':'本地资料导入', 'report_inbox_item':'本地资料导入',
    'slot_events':'盘中公告检查', 'research_input':'研究资料整理',
    'research_analysis':'研究报告生成', 'price_plan':'参考价位计算',
    'research_pipeline':'研究结果保存', 'review_analysis':'交易复盘',
}
IMPACTS = {
    'financials':'关键财务资料未通过核验，本轮不能据此新增买入。','financial_statement':'对应报表尚未取得，底稿会明确保留缺口。',
    'market_comparison':'量价或市场对照不完整，暂不能确认相对强弱。','comparison_series':'相关业务参考股或指数的同期行情尚未补齐。',
    'external_news':'可能遗漏影响公司或行业的外部变化。','external_news_list':'行业、贸易或地缘事件的覆盖可能不足。',
    'external_news_article':'尚未核实这条外部消息的完整内容，对公司的影响仍不确定。',
    'tencent_quotes':'无法确认最新价格，等待行情恢复后再检查交易机会。',
    'tencent_daily':'暂时无法可靠判断价格趋势或更新买卖参考价。',
    'cninfo_stock_catalog':'无法确认该股票的公告来源，可能遗漏最新披露。',
    'cninfo_catalog':'可能遗漏新公告，本轮研究不能视为已覆盖全部披露。',
    'cninfo_pdf':'尚未读到这份资料的完整内容，不能据此确认交易判断。',
    'official_news':'最新市场消息可能不完整。', 'news_article':'这条消息尚未完成核实。',
    'report_inbox':'本地资料尚未进入研究。', 'report_inbox_item':'这份资料尚未进入研究。',
    'slot_events':'无法确认盘中是否有新公告。',
    'research_input':'新资料尚未整理完成，暂不能生成新结论。',
    'research_analysis':'本次研究尚未完成，已有报告保留原来的更新时间和有效期。',
    'price_plan':'本次买卖参考价尚未生成。', 'research_pipeline':'本轮研究结果尚未保存完成。',
    'review_analysis':'最新交易经验尚未补入后续研究。',
}


def failure_reason(detail):
    text=str(detail)
    if re.search(r'nodename|servname|name resolution|getaddrinfo|Name or service not known',text,re.I):return '数据源网址未能解析，网络连接尚未恢复。'
    if re.search(r'timeout|timed out|超时',text,re.I):return '处理超时，尚未完成。'
    if re.search(r'额度|rate.?limit|usage.?limit|quota',text,re.I):return '研究服务的可用额度暂时不足。'
    if re.search(r'登录|认证|unauthorized',text,re.I):return '研究服务需要重新登录。'
    if re.search(r'OCR|扫描|文字不足',text,re.I):return '文件中的文字暂时无法识别。'
    if re.search(r'KeyError|JSON|解析|结构|字段|格式|Decode|qfqday',text,re.I):return '收到的数据格式无法正确读取。'
    if re.search(r'不足60|日线不足|重复日期|价格无效',text):return '历史价格不完整或存在异常。'
    if re.search(r'Connection|URLError|HTTP|网络|下载|连接|SSL',text,re.I):return '暂时无法从来源取得数据。'
    if re.search(r'分页|未完成',text):return '本次处理未完成。'
    if '原文' in text:return '报告引用未通过原文核对。'
    if '术语' in text:return '报告表达未通过可读性检查，等待重新生成。'
    return '本次未能完成处理，等待后续重试。'


def failure_help(source, detail, title='', resource='', config=None):
    """Explain recovery without treating every failed fetch as a trading veto."""
    text=str(detail);cfg=config or {}
    action='可在本股票点击“补齐资料并重研”；恢复后仍会逐项检查交易条件。'
    impact='是否阻止买入以当前研究限制为准；不直接禁止已有持仓按规则退出。'
    recovery='按本股票的补取进度处理；成功后自动清除此条记录。'
    if source=='tencent_quotes':
        impact='没有90秒内的有效报价时，买入和卖出都暂停。'
        recovery='交易时段后台每'+str(cfg.get('quote_poll_seconds',30))+'秒尝试更新；未完成的请求不会重复堆积。'
    elif source in ('slot_events','cninfo_stock_catalog'):
        impact='盘中公告检查未通过时暂停新增买入；不直接阻止止损、止盈。'
        recovery='交易时段后台每'+str(cfg.get('announcement_poll_seconds',120))+'秒重新检查公告来源及各股目录。'
    elif source in ('review_analysis','official_news','news_article','external_news','external_news_list','un_news','mofcom_news'):
        impact='本身不直接阻止买卖；相关公司风险仍须单独核验。'
        action='复盘失败可点击“更新复盘”；普通背景资料可等待下一次资料采集，不必为此手工解除交易限制。'
        recovery='保留已有研究和交易规则；下次对应任务成功后清除此条。'
    elif source in ('research_analysis','research_input','research_pipeline','price_plan'):
        action='本次任务最多尝试'+str(cfg.get('research_attempts',2))+'次；仍失败时查看此处原因，也可手动“继续研究已有资料”。仍有效的旧计划会保留。'
    elif source=='cninfo_pdf':
        from .materiality import classify
        if not classify(title)['required']:impact='一般参考文件，本身不直接阻止买入或卖出。'
        action='可打开原文核对。若来源持续不可用，可下载原始PDF至本地资料收件箱并附来源信息；无需反复下载已有正文。'
    if re.search(r'nodename|servname|name resolution|getaddrinfo|Name or service not known|Connection|URLError|SSL',text,re.I):
        action='先确认浏览器能打开对应来源；可切换手机热点作对照。若网页能打开但后台仍报错，请保留本条错误供排查网络或代理；不需要重新输入证券密码。'
    elif re.search(r'登录|认证|unauthorized',text,re.I):
        action='在本机终端执行 codex login，恢复已有 ChatGPT 订阅登录后再继续研究；不需要购买 API Key。'
        recovery='自动重复研究已暂停，等待登录恢复后手动重试。'
    elif re.search(r'额度|quota|rate.?limit|usage.?limit',text,re.I):
        action='查看 Codex 的额度恢复时间，恢复后继续研究；仍有效的旧计划与程序止损检查保留。'
        recovery='自动重复研究已暂停，不切换付费 API，也不自动消耗重置额度。'
    elif re.search(r'OCR|扫描|文字不足',text,re.I):
        action='从公司或巨潮下载可选择文字的原始PDF；扫描件需要先做文字识别并核对数字，再按收件箱说明导入。'
    if cfg and not cfg.get('scheduler_enabled',True):recovery='自动运行已暂停；请手动执行对应更新，或在设置中恢复自动运行。'
    url=None
    if resource.startswith('https://'):
        from .sources import check_url
        try:check_url(resource);url=resource
        except (ValueError,TypeError):pass
    return {'trading_effect':impact,'recovery':recovery,'next_step':action,'source_url':url}


def outstanding_failures(store, symbol,config=None):
    # Latest status per exact item, never the last 20 logs or an entire-source reset.
    rows=store.db.execute('''SELECT a.* FROM data_attempts a WHERE a.symbol IN (?,'MARKET')
        AND NOT EXISTS(SELECT 1 FROM data_attempts n WHERE n.symbol=a.symbol AND n.source=a.source
        AND n.resource_key=a.resource_key AND (n.checked_at>a.checked_at OR (n.checked_at=a.checked_at AND n.id>a.id)))
        AND a.status!='OK' ORDER BY a.checked_at DESC,a.id DESC''',(symbol,))
    rows=list(rows)
    if config is not None and symbol!='MARKET':
        from .events import relevance
        from .market_context import peers,BENCHMARK
        related_codes={BENCHMARK['symbol'],*(code for code,_ in peers(config,symbol))}
        def relevant(r):
            if r['symbol']!='MARKET':return True
            if r['source']=='comparison_series':return r['resource_key'] in related_codes
            if r['source'] in ('external_news_article','news_article'):
                return relevance(config,symbol,r['title'])!='BACKGROUND'
            return r['source'] not in ('external_news','external_news_list','official_news','un_news','mofcom_news','review_analysis')
        rows=[r for r in rows if relevant(r)]
    # Coverage limits are an explicit gap, not a failed fetch. Historical PARTIAL
    # comparison entries remain in the audit log but belong in the dossier.
    rows=[r for r in rows if not (r['source'] in ('market_comparison','financials') and r['status']=='PARTIAL')]
    return [{'key':f"{r['symbol']}:{r['source']}:{r['resource_key']}",
             'label':SOURCE_NAMES.get(r['source'],'资料处理'), 'title':r['title'],
             'reason':failure_reason(r['detail']), 'impact':IMPACTS.get(r['source'],'相关资料尚不能作为判断依据。'),
             'last_failed_at':r['checked_at'], 'shared':r['symbol']=='MARKET',
             **failure_help(r['source'],r['detail'],r['title'],r['resource_key'],config)} for r in rows]


INTERNAL_WORDS = re.compile(r'paper_baseline_\w+|REVIEW_REQUIRED|INSUFFICIENT_DATA|NO_ENTRY|\bWATCH\b|'
    r'\bfeatures\b|\bsource_checks\b|\bmandatory_coverage\b|\bFTS5\b|\bRAG\b|\bJSON\b|'
    r'\b(?:KeyError|TypeError|ValueError|UNADJUSTED|qfqday|cninfo_\w+|tencent_\w+)\b', re.I)


def trader_text(value):
    """Compatibility for earlier reports, not a rewrite of their investment conclusions."""
    text=str(value or '')
    replacements={
        'paper_baseline_v1':'当前模拟交易规则', 'REVIEW_REQUIRED':'需要进一步核实',
        'INSUFFICIENT_DATA':'资料不足', 'NO_ENTRY':'暂不买入', 'WATCH':'继续观察',
        'features':'价格趋势数据', 'qfqday':'历史价格', 'FTS5':'资料检索', 'RAG':'资料检索',
        'bps':'基点', '类型化核验':'逐项核对', '技术模拟':'模拟交易',
        '技术特征':'价格走势指标', '财务估值未接入':'财务与估值资料不足',
        '复盘口径':'复盘统计方式', '元数据':'公告标题和日期', '结构化':'整理后的',
    }
    for before,after in replacements.items():text=text.replace(before,after)
    text=re.sub(r'\bMA20\b','20日均价',text);text=re.sub(r'\bMA60\b','60日均价',text)
    text=re.sub(r'\b(?:source_checks|mandatory_coverage)\b','资料核对记录',text)
    return text


def trader_paragraph(value):
    # Engineering tasks belong in failure records. Keep business risks and evidence limitations.
    parts=re.split(r'(?<=[。；])',str(value or ''))
    clean=[]
    for part in parts:
        if re.search(r'KeyError|Traceback|修复.*(?:抓取|接口|日线)|(?:来源|接口|日线).*报错|重新计算.*特征|匹配策略规则|补齐策略规则|规则.*参数|技术特征.*计算',part):continue
        part=trader_text(part).strip()
        if INTERNAL_WORDS.search(part) or re.search(r'\b[a-z]+_[a-z_]+\b',part):continue
        if part:clean.append(part)
    return ''.join(clean)


def trader_report(plan, symbol):
    stock=next((r for r in plan.get('research',{}).get('stocks',[]) if r['symbol']==symbol),{})
    analysis=trader_paragraph(stock.get('analysis') or plan['payload'].get('thesis'))
    # Reference prices have one authoritative display (the card's integer-cent plan).
    # Older writers recalculated/rounded them in prose. Keep the business argument;
    # do not publish a second, potentially conflicting set of numerical price levels.
    analysis=''.join(part for part in re.split(r'(?<=。)',analysis)
        if not (re.search(r'均价|均线|观察区间|参考价位',part) and re.search(r'\d[\d.,—–%]*元',part)))
    def items(key):
        return list(dict.fromkeys(v for x in stock.get(key,[]) if (v:=trader_paragraph(x))))
    # A short existing research judgement, not a second model-generated opinion.
    inclination=(stock.get('decision') or {}).get('inclination') or ''
    overview=(trader_paragraph(inclination) if not INTERNAL_WORDS.search(inclination) else '') or analysis
    overview='；'.join(v.strip(' ；;') for v in re.split(r'[。！？\n]',overview)[:2] if v.strip())
    if len(overview)>110:overview=overview[:109].rstrip('，、；')+'…'
    return {'analysis':analysis or '目前证据不足以形成新的交易判断，请等待资料补齐后更新研究。',
        'overview':overview,
        'risks':items('counterpoints'),'next_checks':items('next_checks'),
        'decision':stock.get('decision'),
        'dimensions':stock.get('dimensions',[]),
        'facts':stock.get('facts',[])}
