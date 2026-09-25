"""Durable, rule-based ownership of failures and blockers. Never changes trading gates."""
import json
import re
from datetime import datetime,timedelta
from .calendar import local,phase,trading_day
from .storage import normalize_time,digest,json_write
from .reporting import next_runs
from .presentation import failure_help,failure_reason,SOURCE_NAMES
from .recovery import recovery_status
from .guidance import trade_guidance

OWNER_NAMES={'SYSTEM':'系统','USER':'你','ENGINEERING':'程序维护','DISCLOSURE':'系统跟踪披露','MARKET':'系统跟踪行情'}
STATE_NAMES={'AUTO':'自动处理','RUNNING':'正在处理','ESCALATED':'已转后续处理','WAITING':'等待条件','ACTION':'需要处理'}
JOB_NAMES={'slot':'盘面判断','research':'研究','cycle':'资料研究','collect':'资料采集','repair':'单股补齐','review':'复盘','settle':'模拟撮合','evaluate':'评估打分','weekly_report':'周度评估报告'}


def job_failed(j):
    if j['status'] in ('FAILED','INTERRUPTED','DEFERRED','MISSED'):return True
    result=json.loads(j['result_json'] or 'null')
    if isinstance(result,dict):
        if result.get('model_status')=='DEFERRED' or result.get('status') in ('DEFERRED','ERROR','MISSED','EXPIRED'):return True
        result=result.get('studies',[])
    return isinstance(result,list) and any(r.get('status')=='DEFERRED' for r in result)


def stamp_after(at,seconds):
    return normalize_time((datetime.fromisoformat(at)+timedelta(seconds=seconds)).isoformat())


def next_market(at,seconds=0):
    candidate=stamp_after(at,seconds)
    if phase(candidate)=='CONTINUOUS':return candidate
    d=local(candidate)
    for n in range(16):
        day=d+timedelta(days=n)
        if trading_day(day.date()) is not True:continue
        for h in (9,13):
            t=day.replace(hour=h,minute=30 if h==9 else 0,second=0,microsecond=0)
            if t>=d:return normalize_time(t.isoformat())
    return None


def recovery_time(store,config,at,exhausted):
    d=local(at)
    row=store.db.execute("SELECT value FROM service_state WHERE key='last_recovery'").fetchone()
    earliest=max(at,stamp_after(row[0],config['recovery_interval_seconds'])) if row else at
    for n in range(16):
        day=d+timedelta(days=n)
        if exhausted and n==0:continue
        if trading_day(day.date()) is not True:continue
        start=normalize_time(day.replace(hour=8,minute=0,second=0,microsecond=0).isoformat())
        end=normalize_time(day.replace(hour=22,minute=0,second=0,microsecond=0).isoformat())
        proposed=max(earliest,start)
        if proposed<end:return proposed
    return None


def build(store,config,at):
    """Only read current evidence; unresolved items never expire because the date changed."""
    schedules={r['kind']:r['scheduled_at'] for r in next_runs(config,at)}
    names={w['symbol']:w['name'] for w in config['watchlist']}
    symbols=set(names)|{r[0] for r in store.db.execute('SELECT symbol FROM paper_lots WHERE qty>0')}
    recoveries={s:recovery_status(store,config,s,at) for s in symbols}
    items=[]
    def add(key,symbol,title,reason,*,owner='SYSTEM',state='AUTO',action,trigger,done,
            next_at=None,impact='暂停新增买入；减仓仍按退出条件检查',documents=None,run=None,evidence=None):
        item={'key':key,'symbol':symbol,'name':names.get(symbol,symbol if symbol!='MARKET' else '公共任务'),
              'title':title,'reason':reason,'owner':owner,'owner_name':OWNER_NAMES[owner],
              'state':state,'state_name':STATE_NAMES[state],'next_action':action,'trigger':trigger,
              'next_action_at':next_at,'completion':done,'impact':impact,
              'documents':documents or [],'run':run,'evidence':evidence or [],'escalation':None}
        if not config['scheduler_enabled'] and owner in ('SYSTEM','DISCLOSURE','MARKET'):
            item.update(owner='USER',owner_name='你',state='ACTION',state_name='需要处理',next_action_at=None,
                next_action='先在设置中恢复自动运行，或手动执行对应更新。原计划：'+action,
                trigger='你恢复自动运行或手动提交任务后继续')
        items.append(item);return item
    def research_route(symbol):
        r=recoveries.get(symbol)
        if not r:return {'next_at':schedules.get('cycle'),'trigger':'下一次资料更新后复核','run':'cycle'}
        t=recovery_time(store,config,at,r['state']=='LIMIT_REACHED')
        candidates=[x for x in (t,schedules.get('cycle')) if x]
        return {'next_at':None if r['state']=='RUNNING' else min(candidates,default=None),
                'trigger':'本股票任务完成后核对' if r['state']=='RUNNING' else '不早于所列时间，按现有队列安排；新公告到达也会进入下一轮研究',
                'run':'repair' if symbol in names and r['action'] else 'research'}
    def escalate(item,symbol):
        r=recoveries.get(symbol)
        if r and r['action'] is None and re.search(r'登录|额度',r['why']) and item['owner']=='SYSTEM':
            item.update(owner='USER',owner_name='你',state='ACTION',state_name='需要处理',next_action_at=None,
                next_action='先恢复订阅登录或等待额度恢复，再手动继续研究。',trigger='登录或额度恢复后继续；自动重复研究已暂停')
        elif r and r['state']=='LIMIT_REACHED' and item['owner']=='SYSTEM':
            item.update(state='ESCALATED',state_name='已转后续处理',
                escalation=f"今日额外补齐已用 {r['automatic_attempts']}/{r['automatic_limit']} 次；原有定时研究仍继续。",
                next_action='停止本日额外循环重试。先按本项原文和失败原因核对缺口；仍缺数据可手工补充，下一次定时研究继续验证。'+item['next_action'])
        elif r and r['state']=='RUNNING' and item['owner']=='SYSTEM':item.update(state='RUNNING',state_name='正在处理')
        return item
    for symbol in sorted(symbols):
        from .paper import market_guard
        if market_guard(symbol,{'name':'普通股票','price_cents':100,'prev_close_cents':100})=='UNSUPPORTED_BOARD':
            add('capability:'+symbol,symbol,'该板块的模拟执行规则尚未实现','该股票可以研究，但当前模拟执行器仅支持部分沪深主板规则。',owner='ENGINEERING',state='ACTION',
                action='需要补充该板块的申报数量、价格限制和交易规则并测试，再启用对应模拟执行；下载更多报告不能解决。',
                trigger='程序补齐并验证对应执行规则后继续',done='模拟执行器明确支持本股票所在板块',impact='阻止该板块模拟委托；研究可继续')
        row=store.db.execute("SELECT p.*,s.snapshot_id,s.result_json FROM plans p JOIN studies s ON s.id=p.study_id WHERE p.symbol=? AND p.activated_at<=? ORDER BY CASE p.status WHEN 'ACTIVE' THEN 0 ELSE 1 END,p.activated_at DESC,p.rowid DESC LIMIT 1",(symbol,at)).fetchone()
        if not row:
            escalate(add('plan:'+symbol+':missing',symbol,'尚无有效研究','需要建立研究计划。',
                action='补取本股票资料并研究；失败时按下方失败原因处理。',done='出现有效研究计划',**research_route(symbol)),symbol)
            continue
        plan=dict(row);plan['payload']=json.loads(plan['payload_json']);plan['research']=json.loads(plan['result_json'])
        plan['effective_status']='EXPIRED' if plan['valid_until']<=at else plan['status']
        packet=json.loads(store.db.execute('SELECT packet_json FROM snapshots WHERE id=?',(plan['snapshot_id'],)).fetchone()[0])
        guide=trade_guidance(store,config,symbol,plan,packet,at)
        for g in guide['groups']:
            route=research_route(symbol);owner='SYSTEM';state='AUTO';action='补取或继续增量研究，并核对本项是否解除。'
            key=g['key']
            if key=='TREND_NOT_CONFIRMED':
                owner='MARKET';state='WAITING';action='等待新的完整日线，再复核均线和收盘条件；不反复重读旧报告。'
                route={'next_at':schedules.get('cycle'),'trigger':g['waiting'],'run':None}
            elif key=='RESEARCH_VETO':
                owner='DISCLOSURE';state='WAITING';action='在定时采集时检查新的公司披露；有实质变化后重研。'
                route={'next_at':schedules.get('cycle'),'trigger':g['waiting'],'run':None}
            elif key=='LOT_EXCEEDS_CAP':
                owner='MARKET';state='WAITING';action='只研究不交易；每轮研究按最新净值与价格重新核对，不追加本金、不放宽上限。'
                route={'next_at':schedules.get('cycle'),'trigger':g['waiting'],'run':None}
            elif key=='UNSUPPORTED_BOARD':
                continue  # Already reported once as a capability item above.
            elif key=='LOCAL_ONLY_DOCUMENTS_UNREVIEWED':
                owner='USER';state='ACTION';action=g['user_action']
                route={'next_at':None,'trigger':'你明确具体文件的合法处理许可后重研','run':None}
            elif g['title']=='交易条件仍需核对':
                owner='ENGINEERING';state='ACTION';action='核对本条关联计划中的具体规则，补齐原因解释与处理流程；不能以重复研究冒充修复。'
                route={'next_at':None,'trigger':'程序维护确认规则原因并验证处理后继续','run':None}
            elif key.startswith('event:') or key=='PRICE_DISCONTINUITY':
                review=next((r for r in packet.get('event_reviews',[]) if 'event:'+r['doc_id']==key),None)
                missing='；'.join(review.get('missing',[])) if review else g['waiting']
                if review and re.search(r'尚未实现|未支持|处理规则|账务|通用风险事项',missing) or key=='PRICE_DISCONTINUITY':
                    owner='ENGINEERING';state='ACTION';action='需要补充该事项的核验或账务处理规则，并用原文验证。可将本条交给我继续开发；当前没有后台自动修代码的任务。'
                    route={'next_at':None,'trigger':'程序修复并验证后重新研究；单纯重复抓取不能解决','run':None}
                elif review and re.search(r'尚未到达|等待除息日',missing):
                    owner='DISCLOSURE';state='WAITING';action='等待实施日期到达、完整日线取得后再次核验。'
                    route={'next_at':schedules.get('cycle'),'trigger':missing,'run':None}
                elif review:
                    owner='USER';state='ACTION';action=g['user_action']+' 需要的具体内容：'+missing
                    route={'next_at':schedules.get('cycle'),'trigger':'补充对应正式原文后重新核验；后台也继续检查新披露','run':None}
            item=add('plan:'+symbol+':'+key,symbol,g['title'],g['why'],owner=owner,state=state,
                action=action,done=g['release'],documents=g['documents'],evidence=[plan['id']],**route)
            item['waiting_for']=g['waiting'];escalate(item,symbol)
        # Prices outside a valid plan are normal waiting conditions, not data failures.
        if plan['effective_status']=='ACTIVE' and plan['payload'].get('kind')=='PAPER_TRADE':
            q=store.latest_quote(symbol,at);levels=plan['payload'].get('levels') or {}
            if q and not levels.get('buy_low_cents',0)<=q['price_cents']<=levels.get('buy_high_cents',0):
                add('price:'+symbol,symbol,'价格未进入买入区间','最近取得的价格不满足该研究计划的买入区间。',owner='MARKET',state='WAITING',
                    action='按下一次盘面检查重新判断价格，同时复核研究、公告和资金；低于区间不自动抄底。',
                    trigger='价格进入区间且其他交易条件通过',next_at=schedules.get('slot'),done='新盘面确认该项条件满足',run=None)
    # Exact-resource records, including public failures only once. No stock-count multiplication.
    latest=list(store.db.execute('''SELECT a.* FROM data_attempts a JOIN (
        SELECT id,row_number() OVER(PARTITION BY symbol,source,resource_key ORDER BY checked_at DESC,id DESC) rank
        FROM data_attempts WHERE checked_at<=?) latest ON latest.id=a.id WHERE latest.rank=1''',(at,)))
    for f in latest:
        if f['status']=='OK' or f['status']=='PARTIAL' and f['source'] in ('financials','market_comparison'):continue
        symbol=f['symbol'];source=f['source'];help=failure_help(source,f['detail'],f['title'],f['resource_key'],config)
        route=research_route(symbol);owner='SYSTEM';state='AUTO';action=help['next_step'];text=f['detail']
        if re.search(r'登录|认证|额度|quota|usage.?limit|rate.?limit|unauthorized',text,re.I):
            owner='USER';state='ACTION';route={'next_at':None,'trigger':'恢复登录或订阅额度后手动重新研究','run':'research'}
        elif re.search(r'OCR|扫描|文字不足',text,re.I):
            owner='USER';state='ACTION';route={'next_at':None,'trigger':'补入可读取且允许处理的对应正文后重研','run':None}
        elif source in ('tencent_quotes','slot_events','cninfo_stock_catalog'):
            secs=config['quote_poll_seconds'] if source=='tencent_quotes' else config['announcement_poll_seconds']
            route={'next_at':next_market(at,secs),'trigger':'下一个有效采集时段；请求未结束时不叠加','run':None}
            if not config['background_market_enabled']:
                owner='USER';state='ACTION';route.update(next_at=None,trigger='恢复后台行情采集设置后继续')
                action='后台行情采集已关闭；请按操作说明恢复设置，或手动检查盘面。'
        elif source=='review_analysis':
            route={'next_at':schedules.get('review'),'trigger':'下一次每日复盘；也可手动更新复盘','run':'review'}
            action='在下次复盘时重新生成分析；原交易统计保留。需要提前处理可点击“更新复盘”。'
        elif symbol=='MARKET':
            route={'next_at':schedules.get('cycle'),'trigger':'下一次定时采集；对应来源成功才清除此项','run':'collect'}
            action='在下次定时采集重新获取该来源；持续失败时核对下方原文或按网络错误排查，不因此暂停个股交易。'
        item=add('failure:'+symbol+':'+source+':'+f['resource_key'],symbol,
            SOURCE_NAMES.get(source,'资料处理')+'失败'+(' · '+f['title'] if f['title'] else ''),failure_reason(text),
            owner=owner,state=state,action=action,done='同一股票、同一步骤、同一资料成功获取并处理',
            impact=help['trading_effect'],documents=[{'title':'来源原文','url':help['source_url']}] if help['source_url'] else [],evidence=['data_attempt:'+str(f['id'])],**route)
        escalate(item,symbol)
        prior_ok=store.db.execute("SELECT max(checked_at) FROM data_attempts WHERE symbol=? AND source=? AND resource_key=? AND status='OK' AND checked_at<=?",(symbol,source,f['resource_key'],at)).fetchone()[0]
        first=store.db.execute("SELECT min(checked_at) FROM data_attempts WHERE symbol=? AND source=? AND resource_key=? AND status!='OK' AND checked_at>=? AND checked_at<=?",(symbol,source,f['resource_key'],prior_ok or '0000',at)).fetchone()[0]
        item['observed_at']=first or f['checked_at']
    # Recent terminal job states may fail before a stock-level attempt can be written.
    latest_jobs={};running_jobs={}
    for row in store.db.execute("SELECT j.*,i.payload_json FROM jobs j LEFT JOIN job_inputs i ON i.job_id=j.id WHERE j.scheduled_at<=? ORDER BY j.scheduled_at DESC,j.rowid DESC",(at,)):
        scope=json.loads(row['payload_json'] or '{}').get('symbol','MARKET')
        if row['status'] in ('PENDING','RUNNING'):
            running_jobs.setdefault((row['kind'],scope),row);continue
        latest_jobs.setdefault((row['kind'],scope),row)
    for (kind,scope),j in latest_jobs.items():
        if not job_failed(j):continue
        related=next((i for i in items if i['key']=='failure:MARKET:review_analysis:'),None) if kind=='review' else None
        if related:
            related['evidence'].append(j['id']);continue
        name=JOB_NAMES.get(kind,'后台任务')
        route=research_route(scope) if kind in ('repair','research','cycle','collect') else {'next_at':schedules.get('slot' if kind in ('slot','settle') else 'review'),'trigger':'下一次正常任务重新判断；不补执行过去的买卖','run':'review' if kind=='review' else None}
        item=add('job:'+kind+':'+scope,scope,name+'任务未完成',failure_reason(j['error'] or '任务未完成'),
            action='查看相应股票的失败处理方案；下次任务使用新资料重新执行。' if kind!='slot' else '保留当时结果，下一Slot重新检查；反复超时需检查订阅状态和精简模型输入，不能延长旧决策有效期。',
            done='同范围后续任务成功；历史未成交记录保留',impact='不直接解除或增加原有交易限制',evidence=[j['id']],**route)
        if (kind,scope) in running_jobs:
            item.update(state='RUNNING',state_name='正在处理',next_action_at=None,trigger='等待本轮任务结束并核对结果；仅排入队列不算恢复')
    # Slot-only conditions have their own next step even when no fetch failed.
    decision_routes={
        'T_PLUS_ONE_OR_NO_POSITION':('暂无可卖持仓','等待下一个交易日检查可卖数量；没有持仓则无需卖出。','可卖数量与退出条件同时满足','MARKET'),
        'INSUFFICIENT_BUDGET_OR_TARGET_REACHED':('仓位或可用资金已达限制','等待成交、资金变化或卖出后复核；不自动追加本金或抬高仓位上限。','可用资金与仓位条件满足','MARKET'),
        'EXISTING_OPEN_ORDER':('已有委托等待处理','等待既有模拟委托成交或到期，再检查是否需要新委托。','原委托结束并通过新的决策检查','SYSTEM'),
        'STALE_QUOTE':('盘面报价需要恢复','继续采集新报价；若反复失败，按来源失败项排查网络。','取得90秒内有效报价','SYSTEM'),
        'EVENT_SOURCE_UNAVAILABLE':('盘中公告检查需要恢复','继续检查公告目录；发现重要新公告后补正文并研究。','取得完整且未过期的公告检查','SYSTEM'),
        'NEW_UNREVIEWED_EVENTS':('盘中发现重要新资料','补取新增原文并增量研究，再复核买入条件。','新重要资料已研究并通过对应检查','SYSTEM'),
        'MODEL_DEFERRED':('盘面分析未完成','下一Slot重新判断；如连续失败，检查模型登录、额度及超时记录。','新的盘面分析在本Slot时限内成功','SYSTEM')}
    decision_routes.update({
        'UNSUPPORTED_BOARD':('该板块的模拟执行规则尚未实现','需要补充该板块的申报数量、价格限制和交易规则并验证；重复研究不能解决。','对应执行规则实现且通过测试','ENGINEERING'),
        'LOT_EXCEEDS_CAP':('单手金额超过单股仓位上限','按账户规模只研究不交易；不追加本金、不放宽单股上限。账户净值增长或价格下降后自动恢复评估。','最小申报数量的金额不超过单股上限','MARKET'),
        'SPECIAL_SECURITY':('证券特殊状态需要核对','核对风险警示、上市阶段或退市状态；当前执行规则不支持时需先补充程序。','证券状态与对应执行规则已核验','ENGINEERING'),
        'NEAR_PRICE_LIMIT_OR_CORPORATE_ACTION':('价格接近限制或需核对公司行为','核对当日交易状态及公司行为公告，等待可执行的价格与规则条件。','价格和公司行为检查通过','DISCLOSURE'),
        'RESEARCH_MODE':('当前仅启用研究模式','若要运行独立模拟，需要将本地模式明确设置为paper；该操作不会连接实盘。','你确认模拟模式后重新检查','USER')})
    decision_routes.update({
        'UNKNOWN_ORDER_RECONCILE_REQUIRED':('有委托状态尚未确认','先核对本地委托与成交记录，查明未知状态来源；不能重复下单绕过。','委托和成交已逐笔对账','ENGINEERING'),
        'WITHDRAWAL_PENDING':('模拟利润提取尚未完成','核对利润提取记录、预留资金和完成状态；该项目没有真实资金转出接口。','模拟提取完成且资金记录一致','ENGINEERING'),
        'VALUATION_INCOMPLETE':('持仓估值所需报价缺失','补取所有持仓的有效报价后再计算仓位及可用资金。','持仓估值完整','SYSTEM'),
        'PLAN_CHANGED_OR_EXPIRED':('决策期间研究已更新或过期','使用最新有效研究在下一Slot重新判断，不能沿用旧计划下单。','有效研究与决策版本一致','SYSTEM'),
        'PLAN_INACTIVE':('原研究计划已失效','更新研究，再在新的Slot检查交易条件。','出现新的有效研究计划','SYSTEM'),
        'EXIT_CONDITION_NOT_MET':('尚未满足卖出条件','继续按既定止损或退出条件观察；没有触发时不卖出。','可卖持仓、退出条件与盘面检查通过','MARKET'),
        'OUTSIDE_BUY_ZONE':('决策时价格已离开买入区间','等待新的盘面检查，重新确认价格和计划。','价格及其余买入条件满足','MARKET'),
        'MARKET_CLOSED':('当前交易时段已结束','等待下一交易时段按新报价重新判断，不追补旧单。','新的有效交易Slot到达','MARKET')})
    for symbol in sorted(symbols):
        d=store.latest_trade_check(symbol,at)
        if not d or d['status'] not in ('BLOCKED','EXPIRED'):continue
        payload=json.loads(d['payload_json']);reasons=d['reason']+' '+' '.join(payload.get('input_buy_blockers',[]))
        for code,(title,action,done,owner) in decision_routes.items():
            if code not in reasons:continue
            shared=next((i for i in items if i['key']=='job:slot:MARKET'),None) if code=='MODEL_DEFERRED' else None
            if shared:
                shared.setdefault('affected_stocks',[]).append(names.get(symbol,symbol));shared['evidence'].append(d['id']);continue
            # Live recovery is authoritative for quotes/announcements/new research.
            if code=='STALE_QUOTE':
                from .paper import quote_ok
                if quote_ok(store.latest_quote(symbol,at),config,at) or phase(at)!='CONTINUOUS':continue
            if code=='EVENT_SOURCE_UNAVAILABLE':
                from .monitor import cached_checks
                if phase(at)!='CONTINUOUS' or cached_checks(store,config)['event_status'].get(symbol)=='OK':continue
            if code=='NEW_UNREVIEWED_EVENTS':
                from .slots import active_plan,unreviewed_events
                p=active_plan(store,symbol,at)
                if p and not unreviewed_events(store,p,at,config):continue
            add('decision:'+symbol+':'+code,symbol,title,'最近一次盘面检查记录了该条件；不代表已发生买卖。',owner=owner,state='ACTION' if owner in ('USER','ENGINEERING') else 'WAITING',
                action=action,trigger=done,next_at=None if owner in ('USER','ENGINEERING') else schedules.get('slot'),done='下一次盘面核对通过此项；当前状态随检查更新',
                impact='以最新盘面复核为准，不能据此补执行旧委托',evidence=[d['id']])
    if not config['scheduler_enabled']:
        add('service:paused','MARKET','自动运行已暂停','不会安排新的定时采集和研究。',owner='USER',state='ACTION',
            action='在“自选股与自动运行”中开启自动运行；也可按需手动执行。',trigger='你开启自动运行后继续',done='自动运行设置已开启',impact='自动更新暂停，已有任务按原规则完成')
    if trading_day(local(at).date()) is None:
        add('service:calendar','MARKET','交易日历需要更新','当前年份尚无核验过的交易日历。',owner='ENGINEERING',state='ACTION',
            action='根据交易所当年休市通知更新日历并测试，再恢复盘面定时任务。',trigger='日历更新通过验证后继续',done='当前年份交易日期可明确判断',impact='交易Slot暂停，资料研究可继续')
    from .calendar import next_year_warning
    upcoming=next_year_warning(at)
    if upcoming:
        add('service:calendar-next-year','MARKET',f"{upcoming['year']}年交易日历尚未录入",
            f"距离年底还有{upcoming['days_left']}天。未录入的年份不会执行任何交易。",owner='ENGINEERING',state='ACTION',
            action='交易所发布次年休市安排后（通常在12月），按《年度交易日历更新》流程录入、测试并部署到本地和云端。',
            trigger='次年休市安排发布后',done=f"{upcoming['year']}年日历已录入并通过测试",impact='现在不影响交易；若1月1日前未完成，新年起全部交易暂停')
    disk=json.loads((store.db.execute("SELECT value FROM service_state WHERE key='maintenance'").fetchone() or ['{}'])[0]).get('disk')
    if disk and disk.get('warning'):
        add('service:disk','MARKET','磁盘空间或数据库体积超过警戒线',
            f"可用空间{disk['free_gb']}GB，数据库{disk['db_gb']}GB，备份{disk['backups_gb']}GB。",owner='USER',state='ACTION',
            action='按运维手册“磁盘空间”一节处理：确认备份保留设置、清理旧发布包副本，必要时停服务后整理数据库。',
            trigger='空间释放后自动消失',done='可用空间和数据库体积回到警戒线以内',impact='空间耗尽会导致研究、复盘与备份失败')
    return items,latest


def reconcile(store,config,at):
    at=normalize_time(at);items,attempts=build(store,config,at);seen=set()
    with store.db:
        for item in items:
            iid=digest(item['key'])[:24];seen.add(iid)
            row=store.db.execute('SELECT * FROM followup_items WHERE id=?',(iid,)).fetchone()
            if row:
                observed=item.get('observed_at',at)
                opened=at if row['status']!='OPEN' else min(row['opened_at'],observed) if row['occurrences']==1 else row['opened_at']
                store.db.execute("UPDATE followup_items SET status='OPEN',first_seen_at=?,opened_at=?,last_seen_at=?,resolved_at=NULL,occurrences=?,payload_json=? WHERE id=?",
                    (min(row['first_seen_at'],observed),opened,at,row['occurrences']+int(row['status']!='OPEN'),json.dumps(item,ensure_ascii=False),iid))
            else:
                observed=item.get('observed_at',at)
                store.db.execute('INSERT INTO followup_items VALUES(?,?,?,?,?,?,?,?)',(iid,'OPEN',observed,observed,at,None,1,json.dumps(item,ensure_ascii=False)))
        for row in store.db.execute("SELECT id FROM followup_items WHERE status='OPEN'").fetchall():
            if row['id'] not in seen:store.db.execute("UPDATE followup_items SET status='CLEARED',resolved_at=? WHERE id=?",(at,row['id']))
        day=local(at).date().isoformat();start=normalize_time(day+'T00:00:00+08:00')
        failed={}
        for r in store.db.execute("SELECT * FROM data_attempts WHERE status!='OK' AND checked_at>=? AND checked_at<=? ORDER BY id",(start,at)):
            if r['status']=='PARTIAL' and r['source'] in ('financials','market_comparison'):continue
            key=(r['symbol'],r['source'],r['resource_key']);failed[key]=dict(r)
        latest_by_key={(r['symbol'],r['source'],r['resource_key']):r for r in attempts}
        today_failures=[{'symbol':key[0],'title':SOURCE_NAMES.get(key[1],'资料处理')+(' · '+r['title'] if r['title'] else ''),
            'last_failed_at':r['checked_at'],'recovered':latest_by_key[key]['status']=='OK',
            'next_step':'对应资料处理成功，继续原有观察。' if latest_by_key[key]['status']=='OK' else '按统一处理清单中的责任方和下一步继续；未解决项跨日保留。'} for key,r in failed.items()]
        today_jobs=[{'id':j['id'],'title':JOB_NAMES.get(j['kind'],'后台任务'),'at':j['finished_at'] or j['scheduled_at'],
            'next_step':'不会删除当时的失败记录；后续处理安排见当前清单。'} for j in store.db.execute('SELECT * FROM jobs WHERE coalesce(finished_at,scheduled_at)>=? AND coalesce(finished_at,scheduled_at)<=?',(start,at)) if job_failed(j)]
        open_items=[{**json.loads(r['payload_json']),'id':r['id'],'first_seen_at':r['first_seen_at'],'opened_at':r['opened_at'],'occurrences':r['occurrences']} for r in store.db.execute("SELECT * FROM followup_items WHERE status='OPEN'")]
        summary={'day':day,'updated_at':at,'items':open_items,'today_failures':today_failures,
            'failed_today':len(today_failures),'recovered_today':sum(f['recovered'] for f in today_failures),'today_jobs':today_jobs,
            'carried_over':sum(local(x['opened_at']).date().isoformat()<day for x in open_items),
            'owners':{owner:sum(x['owner']==owner for x in open_items) for owner in OWNER_NAMES}}
        store.db.execute('INSERT OR REPLACE INTO followup_days VALUES(?,?,?)',(day,at,json.dumps(summary,ensure_ascii=False)))
        store.db.execute("INSERT OR REPLACE INTO service_state VALUES('followups_checked_at',?)",(at,))
    json_write(store.root/'workflow'/'followups'/(day+'.json'),summary)
    return summary


def view(store,at):
    row=store.db.execute('SELECT payload_json FROM followup_days ORDER BY day DESC LIMIT 1').fetchone()
    result=json.loads(row[0]) if row else {'items':[],'updated_at':None,'today_failures':[],'owners':{},'failed_today':0,'recovered_today':0,'carried_over':0}
    result['stale']=not result['updated_at'] or (datetime.fromisoformat(at)-datetime.fromisoformat(result['updated_at'])).total_seconds()>120
    error=store.db.execute("SELECT value FROM service_state WHERE key='followups_error'").fetchone()
    if error:result['stale']=True
    result['history']=[json.loads(r[0]) for r in store.db.execute('SELECT payload_json FROM followup_days ORDER BY day DESC LIMIT 1 OFFSET 1')]
    return result
