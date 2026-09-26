"""Explain actual plan blockers without changing decisions or pretending they are resolved."""
import re
from .presentation import SOURCE_NAMES,trader_paragraph
from .sources import check_url


def public_url(value):
    try:check_url(value)
    except (TypeError,ValueError):return None
    return value


def trade_guidance(store,config,symbol,plan,packet,at):
    if not plan:return {'groups':[]}
    codes=plan['payload'].get('blockers',[])
    coverage={m['doc_id']:m for m in packet.get('mandatory_coverage',[])}
    current=[d for scope in (symbol,'MARKET') for d in store.documents_as_of(at,scope)]
    full_by_url={d['url']:d for d in current if d['cloud_allowed'] and d['kind'] not in ('announcement_metadata','news_brief','news_index')}
    scheduled='、'.join(config['collection_times'])
    automatic=('每天 '+scheduled+' 自动抓取并研究。' if config['scheduler_enabled'] else '自动运行已暂停，需要手动更新研究，或在设置中恢复自动运行。')
    groups=[];buckets={}
    def add(key,title,why,waiting,user_action,release,*,documents=None,run=None):
        item={'key':key,'title':title,'why':why,'waiting':waiting,'user_action':user_action,'release':release,
              'documents':documents or [],'run':run}
        groups.append(item);return item
    def document(did):
        row=store.db.execute('SELECT * FROM documents WHERE id=?',(did,)).fetchone()
        if not row:return {'id':did,'title':'原计划引用的资料','url':None,'state':'原资料记录暂不可用'},None
        d=dict(row);m=coverage.get(did,{})
        newer=full_by_url.get(d['url'])
        if newer and newer['id']!=did:state='正文已取得，等待新研究核对'
        elif m.get('fulltext'):state='正文已读，公告事项仍待核验' if m.get('all_chunks_accounted',m.get('all_chunks_in_packet')) else '正文已取得，尚有内容待分批研究'
        else:state='当前研究只读到标题或摘要，尚未取得正文'
        return {'id':did,'title':d['title'],'url':public_url(d['url']),'state':state},d
    for code in dict.fromkeys(codes):
        key,_,value=code.partition(':')
        if key in ('UNRESOLVED_EVENT','CORPORATE_ACTION_UNVERIFIED','UNREAD_DOCUMENT'):
            doc,d=document(value)
            if key in ('UNRESOLVED_EVENT','CORPORATE_ACTION_UNVERIFIED'):
                from .research import RISK_WORDS
                matched='、'.join(dict.fromkeys(re.findall(RISK_WORDS,doc['title'])))
                dividend=bool(re.search(r'权益分派|分红|除权|除息',doc['title']))
                title='分红或除权事项尚未核验' if dividend else '公告中的风险线索尚未核验'
                why=('公告标题涉及权益分派、分红或除权，触发了价格可比性的核对要求；这不代表公司出现重大利空。' if dividend else
                     '公告标题含有风险筛选词，触发事件核对；这是待核验线索，并非已确认的重大风险。')
                if matched:why+=' 本条触发词：'+matched+'。'
                waiting=('需要核对实施日期、每股分配方案，以及除权前后价格是否可比。' if dividend else
                         '需要核对公告全文、事件进展、影响范围及是否存在后续结论。')
                review=next((r for r in packet.get('event_reviews',[]) if r['doc_id']==value),None)
                waiting='；'.join(review.get('missing',[])) if review and review.get('missing') else waiting
                action=('请打开下方实施公告核对每10股现金分红、是否送转、股权登记日及除息日。已有正文无需再下载；点击“补齐资料并重研”会重新核验。' if dividend else
                        '请提供该事项最新进展或正式结论的公告链接，以及对应案件或事项编号；需要核对影响是否仍存在，不是只确认“读过了”。')
                release=('纯现金分红的正文、日期、价格调整及登记日策略持仓检查全部通过后，新研究自动解除这一事项；送转、配股、差异化分红或分红账务未支持时会明确保留缺口；随分红发布的回购调价等公告随实施公告一并解除。' if dividend else
                         '正式结论与影响核验通过后才能解除；通用诉讼、处罚等尚不能自动判断风险已消除，需补充个案核验规则。')
                add('event:'+value,title,why,waiting,action,release,documents=[doc],run='research')
            else:
                m=coverage.get(value,{})
                group='updated' if d and d['url'] in full_by_url and full_by_url[d['url']]['id']!=value else 'reading' if m.get('fulltext') else 'missing'
                buckets.setdefault(group,[]).append(doc)
            continue
        if key=='SOURCE_GAP':
            source=SOURCE_NAMES.get(value,'相关资料')
            add(code,source+'尚未完整取得','这份研究计划没有通过该来源的完整性检查。',automatic,
                '通常无需操作；可点击“更新资料并研究”重试。具体失败原因见该股票底部的失败项。',
                '该来源重新取得并处理成功，且新研究通过此项检查。',run='cycle')
        elif key=='RESEARCH_VETO':
            stock=next((s for s in plan.get('research',{}).get('stocks',[]) if s['symbol']==symbol),{})
            checks=[trader_paragraph(x) for x in stock.get('next_checks',[]) if trader_paragraph(x)]
            add(key,'研究判断暂不支持买入','研究认为证据不足，或仍有需要核实的经营问题。',
                '；'.join(checks) or '等待新的公司披露或可靠证据，使研究能够形成可观察的判断。',
                '通常等待相关披露后更新研究；“接下来观察”列出了具体指标。无需为了消除提示购买数据服务。',
                '新证据经研究后不再否决买入；即使这一项解除，仍须通过其余交易条件。',run='cycle')
        elif key in ('FINANCIAL_BASELINE_INCOMPLETE','MARKET_CONTEXT_INCOMPLETE'):
            financial=key=='FINANCIAL_BASELINE_INCOMPLETE'
            gaps=packet.get('company_dossier',{}).get('gaps',[]) if financial else ((packet.get('stocks') or [{}])[0].get('features') or {}).get('market_context',{}).get('缺口',[])
            add(key,'公司财务底稿待补齐' if financial else '量价及市场对照待补齐',
                '；'.join(gaps) or '关键字段尚未通过核验。',automatic,
                '可以更新资料并研究；缺少的数值不会按零处理，也不会以旧数据冒充本轮已核验。',
                '本轮关键字段和时间口径核验通过，并生成新的研究计划。',run='cycle')
        elif key=='TREND_NOT_CONFIRMED':
            basis=plan['payload'].get('basis',{})
            def price(k):
                n=basis.get(k);return f'{n/100:,.2f}元' if type(n) is int else '待更新'
            add(key,'价格趋势尚未满足条件',f"研究采用的20日均价为{price('ma20_cents')}，60日均价为{price('ma60_cents')}，昨日收盘为{price('close_cents')}。",
                '等待20日均价高于60日均价，并且昨日收盘不低于60日均价。',
                '无需补资料或调整设置；等后续交易日的价格变化。跌到买入区间以下本身不能解除趋势限制。',
                '日线更新后，新研究确认两个趋势条件均满足。')
        elif key=='LOCAL_ONLY_DOCUMENTS_UNREVIEWED':
            docs=[{'id':d['id'],'title':d['title'],'url':None,'state':'未获准交给联网模型处理'} for d in current if not d['cloud_allowed'] and d['id'] in {m['version_id'] for m in packet.get('document_manifest',[])}]
            add(key,'部分本地资料尚未取得处理许可','这份计划涉及不能交给联网模型的本地资料。',
                '等待确认具体文件的来源许可与允许的使用范围。',
                '请先确认资料提供方是否允许云端处理，再告知哪些文件可以使用；不用提供账号密码，也不会因点击更新而自动授权。',
                '许可被明确记录，相关资料经合法处理并重新研究后才能解除。',documents=docs)
        elif key in ('MODEL_NOT_READY','NO_ACTIVE_PLAN'):
            add(key,'本轮研究尚未完成','当前缺少成功完成的研究判断。',automatic,
                '先看股票底部的失败项：若提示需要登录，请恢复已有订阅登录；若额度不足，等待额度恢复。其他情况可重新研究已有资料。',
                '模型完成并通过证据校验，生成新的有效研究计划。',run='research')
        elif key in ('STALE_DAILY_BARS','UNADJUSTED_SERIES_MISSING','NO_COLLECTION_COVERAGE','SOURCE_CHANGED_DURING_RESEARCH'):
            titles={'STALE_DAILY_BARS':'历史价格需要更新','UNADJUSTED_SERIES_MISSING':'计算参考价的价格资料不齐',
                'NO_COLLECTION_COVERAGE':'尚无完整的资料采集记录','SOURCE_CHANGED_DURING_RESEARCH':'研究时又有新资料到达'}
            add(key,titles[key],'现有研究所依据的资料尚不能满足这项检查。',automatic,
                '通常无需手工补充；可以更新资料并研究。若仍失败，按股票底部的具体失败项处理。',
                '资料更新完成，新研究重新通过检查。',run='cycle')
        elif key=='PRICE_DISCONTINUITY':
            add(key,'历史价格出现较大跳变','价格序列出现较大变化，需要排查除权、停牌或来源差错。',
                '等待公告与价格记录相互核对；当前系统不会自动判定跳变已解释。',
                '核对公司分红、除权或复牌公告；若来源价格有误，需取得更正数据。单纯等待价格反弹不能证明异常已解决。',
                '价格数据更正后重新研究，或补齐经验证的公司行为处理流程。')
        elif key=='LOT_EXCEEDS_CAP':
            add(key,'单手金额超过单股仓位上限','按当前账户净值，最小申报数量的金额已超过单股上限，这只股票只研究、不交易。',
                '等待账户净值增长或股价下降，使最小申报数量的金额回到单股上限以内。',
                '无需操作。按已确认的规则，不追加模拟本金、不放宽单股上限。',
                '新研究计算的最小申报金额不超过单股上限后自动恢复买入评估。')
        elif key=='UNSUPPORTED_BOARD':
            add(key,'该板块尚无模拟交易规则','这只证券所在的板块不在模拟执行器支持的范围内，只研究不交易。',
                '需要程序补齐该板块的申报数量、价格限制等规则并测试。',
                '可以把股票名称和本栏提示发给我评估；重复研究不能解决。',
                '程序支持该板块并通过测试后，新研究自动解除此项。')
        elif key=='REDUCE_ONLY_COST_STOP':
            add(key,'当前计划只允许降低已有持仓','这是一份按持仓成本保护风险的计划，不允许新增买入。',automatic,
                '如需恢复买入评估，先更新完整研究。卖出还需满足可卖数量和盘面条件。',
                '生成允许评估买入的完整研究计划，并通过其余交易条件。',run='cycle')
        else:
            add(key,'交易条件仍需核对','现有计划包含尚未解释完整的限制。',
                '等待核对该项规则和本次研究记录，不能猜测已经解除。',
                '可以把股票名称和本栏提示发给我检查；暂时无需补交凭据。',
                '找到确切原因并通过对应检查后，才能恢复新增买入。')
    for key,docs in buckets.items():
        if key=='missing':
            add(key,f'有 {len(docs)} 份关键资料尚未取得正文','当前只掌握这些重要资料的标题或摘要，不能据此确认已完成研究。',
                automatic+f" 每轮每股最多补取 {config['pdf_downloads_per_stock']} 份，优先关键未取文件；缓存文件另行复查。网络失败时不保证一轮补齐。",
                '可从下列原文链接查阅；点击更新会继续按重要性补取，不需要逐篇手工提交。',
                '对应正文成功取得、关键内容经研究核对后，此项才会在新计划中消失。',documents=docs,run='cycle')
        elif key=='reading':
            add(key,f'有 {len(docs)} 份资料的关键内容待读','文件已取得，重要经营、财务或风险章节仍需分批核对；一般附录待读单独提示。',automatic,
                '无需重新下载或再次提供文件。可以等待自动研究，也可点击“继续研究已有资料”提前处理下一批。',
                '关键章节研究成功，并由新计划确认；每次只推进一批，不能承诺点击一次就完成。',documents=docs,run='research')
        else:
            add(key,f'有 {len(docs)} 份资料已补齐，等待研究确认','正文或修订版本已经取得，当前显示的计划仍依据此前资料。',automatic,
                '无需重复提交文件；继续研究已有资料即可让新计划核对。',
                '新研究确认已取得并读完对应版本；其他限制仍单独判断。',documents=docs,run='research')
    if plan.get('effective_status') in ('EXPIRED','DRAFT','SUPERSEDED'):
        add('plan_status','研究计划已过期或尚未生效','以下限制说明来自上次研究记录，当前不能直接据此买入。',automatic,
            '更新资料并研究，查看新的更新时间与有效期。',
            '新的有效研究计划生成，且所有买入检查通过。',run='cycle')
    return {'groups':groups,'summary':'这些事项会拦截新增买入。普通风险提示和失败项并不都直接构成买入限制；盘中还会另查价格、资金与交易条件。',
            'as_of':plan.get('activated_at')}
