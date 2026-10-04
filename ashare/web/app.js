'use strict';

// Acquire the current session before each mutation; refresh once only if rejected
// before execution. Network errors must not silently repeat a submitted action.
class ApiError extends Error {
  constructor(message,kind,status=0){super(message);this.name='ApiError';this.kind=kind;this.status=status;}
}
class LocalApi {
  constructor(transport = (...args) => fetch(...args)) { this.transport = transport; }
  async request(path, options = {}) {
    let response;
    const signal=AbortSignal.timeout(20000);
    const transportError=error=>{
      const timeout=signal.aborted||['TimeoutError','AbortError'].includes(error?.name);
      const message=path==='/api/feedback'?'暂未确认意见是否送达。请重试提交，系统会避免重复保存。':options.method==='POST'
        ?'暂未收到操作结果。请先查看任务记录，确认是否已提交后再重试。'
        :timeout?'资料读取超过 20 秒，正在重试。':'暂时连接不上服务，正在重试。';
      return new ApiError(message,timeout?'TIMEOUT':'NETWORK');
    };
    try {
      response = await this.transport(path, {...options, cache:'no-store', credentials:'same-origin', signal});
    } catch (error) {throw transportError(error);}
    if(response.status===401){
      if(typeof location!=='undefined')location.assign('/login?next='+encodeURIComponent(location.pathname));
      throw new ApiError('登录已失效，请重新登录。','AUTH',401);
    }
    let data;
    try { data = await response.json(); }
    catch (error) {
      if(signal.aborted||['TimeoutError','AbortError'].includes(error?.name))throw transportError(error);
      throw new ApiError('本次返回的资料无法显示，请刷新重试；持续出现时请联系管理员。',response.ok?'RESPONSE':'HTTP',response.status);
    }
    return {response, data};
  }
  async get(path) {
    const {response,data} = await this.request(path);
    if (!response.ok) throw new ApiError(readableError(data.error) || '资料读取失败，请稍后重试。','HTTP',response.status);
    return data;
  }
  async post(path, body) {
    for (let attempt=0; attempt<2; attempt++) {
      const session = await this.get('/api/session');
      const {response,data} = await this.request(path, {
        method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':session.csrf_token}, body:JSON.stringify(body)
      });
      if (response.ok) return data;
      if (response.status===403 && data.code==='SESSION_EXPIRED' && attempt===0) continue;
      throw new ApiError(readableError(data.error) || '操作未完成，请稍后重试。','HTTP',response.status);
    }
  }
}

const api = new LocalApi();
const $ = id => document.getElementById(id);
const money = n => n==null ? '—' : (n/100).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const signed = n => n==null ? '—' : (n>0?'+':'')+money(n);
const percent = n => n==null ? '—' : n.toFixed(2)+'%';
const costPrice = n => n==null ? '—' : (n/100).toLocaleString('zh-CN',{minimumFractionDigits:3,maximumFractionDigits:3});
function drawPositionCost(box,h) {
  const cost=el('p',null,'position-cost');
  cost.append(el('span','成本 ','subtle'),el('strong',costPrice(h?.average_cost_cents)));
  cost.title='当前剩余持仓的平均买入成本，含已分摊买入费用；展示保留三位小数，盈亏按未舍入的总成本计算。';box.append(cost);
  if(h?.unrealized_return_pct!=null)box.append(el('p','较成本 '+(h.unrealized_return_pct>0?'+':'')+percent(h.unrealized_return_pct),'cost-return '+(h.unrealized_return_pct>0?'profit':h.unrealized_return_pct<0?'loss':'')));
}
function quoteIsStale(q,s) {
  if(!q||s.market_phase!=='CONTINUOUS')return false;
  const age=(Date.parse(s.at)-Date.parse(q.observed_at))/1000;
  return !Number.isFinite(age)||age<0||age>(s.quote_max_age_seconds??90);
}
const when = s => s ? new Date(s).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}) : '—';
const shortTime = s => s ? new Date(s).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false}) : '待安排';
function traderText(value){
  let text=String(value??'');
  const terms={CORE:'固定关注',DYNAMIC:'动态发现',FOCUS:'重点研究',COOLING:'待复核',ARCHIVED:'已归档',deferred:'等待重试',industry_v1:'第一版研究规则',industry_v2:'第二版研究规则',DELTA:'本次新增资料',FULL:'全部资料',UNREAD_DOCUMENT:'关键资料尚未读完',REVIEW_REQUIRED:'需要复核',INSUFFICIENT_DATA:'资料不足',MODEL_DEFERRED:'本次分析尚未完成',NO_ENTRY:'暂不买入',PAPER_TRADE:'满足条件后可模拟交易',ORDERS:'订单',DEMAND:'需求',LEAD_TIME:'交货周期',UNKNOWN:'待核实',MA20:'20日均价',MA60:'60日均价',bps:'基点',cninfo_industry:'巨潮资讯公告',cninfo_catalog:'公司公告目录',cninfo_pdf:'公告正文',cninfo_stock_catalog:'股票公告来源',tencent_quotes:'最新行情',tencent_daily:'历史价格',financials:'财务资料',financial_statement:'财务报表',market_comparison:'市场对照',comparison_series:'参考行情',research_analysis:'公司研究',research_pipeline:'研究结果保存',research_input:'资料整理',price_plan:'买卖参考价',review_analysis:'交易复盘',external_news:'行业与国际消息',external_news_list:'行业消息目录',external_news_article:'行业消息正文',official_news:'市场新闻',news_article:'新闻正文',report_inbox:'导入资料',report_inbox_item:'导入资料',slot_events:'盘中公告检查',ccgp:'中国政府采购网'};
  text=text.replace(/\b(?:[A-Za-z]+_\w+|CORE|DYNAMIC|FOCUS|COOLING|ARCHIVED|DELTA|FULL|ORDERS|DEMAND|UNKNOWN|MA20|MA60|bps|deferred|financials|ccgp)\b/g,word=>terms[word]||(/_/.test(word)?'相关条件待核实':word));
  for(const [before,after] of Object.entries({'固定 Watchlist':'固定关注','Dynamic List':'动态发现','心跳':'状态更新','组合授权':'组合买入安排','授权租约':'研究有效期','持仓 / 委托保护':'继续管理已有持仓和委托','资料快照':'当时使用的资料','固定资料快照':'当时使用的资料','门禁':'条件核对','契约':'判断依据','归口':'处理安排','硬性闸门':'必须满足的条件','载荷':'资料内容','RAG':'资料检索','增量研究':'根据新增资料继续研究','签名资格':'交易端已确认的名单条件','方法门禁':'研究条件核对','初步准入':'初步研究条件','业务敞口':'相关业务规模','已有敞口':'已有持仓与委托','公司实际敞口':'公司相关业务规模','前向样本':'研究完成后持续观察的样本','前向校准':'事后验证','前向观察':'研究后的观察','前向跟踪':'研究后持续跟踪','元数据':'资料标题和日期','结构化':'整理后的','动态线路':'事件交易','本线路':'此类交易','候选队列':'待研究线索','证券身份':'证券代码与公司对应关系'}))text=text.split(before).join(after);
  return text;
}
function readableError(value){
  const text=String(value||'');
  if(/timeout|timed out|TimeoutError|超时/i.test(text))return '本次处理超时，等待重试；已有结果仍可查看。';
  if(/CERTIFICATE|SSL|HTTPError|URLError|ConnectionError|ECONN|ENOTFOUND|fetch failed/i.test(text))return '暂时无法连接资料来源，等待恢复后重试。';
  if(/Traceback|KeyError|TypeError|ValueError|JSONDecode|SyntaxError|schema|stack trace/i.test(text))return '本次取得的资料未能完成处理，等待重试；已有结果仍可查看。';
  if(text==='Not found')return '未找到这项资料。';
  return traderText(text);
}
const el = (tag,text,cls) => {const e=document.createElement(tag);if(text!=null)e.textContent=tag==='blockquote'?text:traderText(text);if(cls)e.className=cls;return e;};
const names = {portfolio_strategy:'更新组合判断',global_research:'更新美股与现货研究',global_slot:'美股与现货盘面检查',dynamic_cycle:'更新新闻研究',dynamic_slot:'动态盘面检查',cycle:'更新研究',collect:'更新资料',research:'重新研究',repair:'补齐单股资料',NOT_NEEDED:'无需模型重复判断',slot:'检查盘面',review:'更新复盘',settle:'模拟撮合',
  PENDING:'等待处理',RUNNING:'正在处理',DONE:'已完成',FAILED:'未完成',INTERRUPTED:'已中断',MISSED:'已错过',SKIPPED_CATCHUP:'已跳过旧批次',
  CLOSED:'休市',CALENDAR_UNKNOWN:'交易日历待更新',DEFERRED:'等待模型可用',SUCCEEDED:'已完成',NOT_RUN:'尚未运行',
  NO_ENTRY:'暂不买入',PAPER_TRADE:'可按条件模拟交易',ACTIVE:'有效',DRAFT:'尚未完成',EXPIRED:'已过期',SUPERSEDED:'已有新版',RISK_EXIT_ONLY:'仅允许减仓',
  BUY:'买入',SELL:'卖出',HOLD:'不动',BLOCKED:'未执行',SUBMITTED:'已提交模拟委托',RECORDED:'已记录',OPEN:'等待成交',FILLED:'全部成交',
  OK:'正常',PARTIAL:'覆盖不全',REVIEW_REQUIRED:'有待核实事项',WATCH:'继续观察',INSUFFICIENT_DATA:'资料不足'};
Object.assign(names,{CORE:'固定关注',DYNAMIC:'动态发现',FOCUS:'重点研究',COOLING:'待复核',ARCHIVED:'已归档',UNKNOWN:'状态待核对',CANCELLED:'已撤销',PARTIALLY_FILLED:'部分成交',REJECTED:'未获通过',WAITING:'等待证据',TRACKING:'持续跟踪',NEEDS_INPUT:'等待补充资料',STALE:'需要更新',PAUSED:'已暂停'});
const label = s => names[s] || (s&&/^[A-Z_]+$/.test(s)?'状态待核实':s) || '—';
const blockerNames = {
  RESEARCH_VETO:'目前证据不足以支持买入',MODEL_NOT_READY:'本轮研究尚未完成',UNRESOLVED_EVENT:'公告事项尚未核验',
  CORPORATE_ACTION_UNVERIFIED:'公司行为与价格可比性待核验',FINANCIAL_BASELINE_INCOMPLETE:'公司财务底稿待补齐',MARKET_CONTEXT_INCOMPLETE:'量价与市场对照待补齐',
  UNREAD_DOCUMENT:'新增公告或报告尚未审阅完整',STALE_DAILY_BARS:'日线数据需要更新',SOURCE_GAP:'部分资料未取得',TREND_NOT_CONFIRMED:'价格趋势尚不满足买入条件',
  LOCAL_ONLY_DOCUMENTS_UNREVIEWED:'本地资料尚未获准交给模型分析',NO_COLLECTION_COVERAGE:'尚未完成本轮资料更新',UNADJUSTED_SERIES_MISSING:'缺少可计算参考价的数据',
  PRICE_DISCONTINUITY:'价格跳变需要核查',SOURCE_CHANGED_DURING_RESEARCH:'研究期间有新资料到达，需要重新核查',REDUCE_ONLY_COST_STOP:'仅按持仓成本执行止损',
  VALUATION_INCOMPLETE:'持仓报价需要更新',NO_ACTIVE_PLAN:'尚无有效研究计划',STALE_QUOTE:'报价已过时',T_PLUS_ONE_OR_NO_POSITION:'暂无可卖持仓',
  OUTSIDE_BUY_ZONE:'价格不在买入区间',INSUFFICIENT_BUDGET_OR_TARGET_REACHED:'已达到仓位上限或可用资金不足',EXISTING_OPEN_ORDER:'已有委托等待成交'
};
const blocker = s => blockerNames[String(s).split(':')[0]] || '交易条件尚未核实完整';
const decisionReason = value => String(value||'').replace(/\b[A-Z][A-Z_]{3,}\b/g, code => ({
  MODEL_DEFERRED:'本次盘面分析未完成',COST_STOP_TRIGGER:'触及持仓成本止损',PLAN_STOP_TRIGGER:'触及研究止损价',
  PLAN_EXIT_TRIGGER:'触及研究退出价',PLAN_CHANGED_OR_EXPIRED:'研究计划已更新或过期',BUY_GATES_FAILED:'买入条件尚未满足',
  EXIT_CONDITION_NOT_MET:'尚未触及退出条件',PAPER_ORDER_OPEN:'模拟委托已提交',EVENT_SOURCE_UNAVAILABLE:'公告检查未通过',
  NEW_UNREVIEWED_EVENTS:'新重要资料待研究',NO_ENTRY_PLAN:'当前研究不允许买入'
}[code]||blockerNames[code]||names[code]||'其他交易条件待核实'));
const brief = text => { const t=String(text||''); const sentence=t.match(/^.{12,180}?[。！？]/u);return sentence?sentence[0]:t.length>130?t.slice(0,130)+'…':t; };
let state=null, editing=false, refreshing=false, connected=false;
const renderingCache=new Map(), watchedJobs=new Map(), submitting=new Set();

function feedback(id,text,type='') {const box=$(id);box.textContent=readableError(text);box.hidden=!text;box.className='feedback'+(type?' '+type:'');}
function details(title,key) {const d=el('details');d.dataset.key=key;d.append(el('summary',title));return d;}
function link(title,url) {const a=el('a',title);a.href=url;if(url.startsWith('#'))a.addEventListener('click',()=>{const target=$(url.slice(1));if(target?.tagName==='DETAILS')target.open=true;});else{a.target='_blank';a.rel='noopener';}return a;}
function renderChanged(id,key,draw) {
  const signature=JSON.stringify(key);if(renderingCache.get(id)===signature)return;
  const box=$(id),opened=new Set([...box.querySelectorAll('details[open][data-key]')].map(d=>d.dataset.key));
  box.replaceChildren();draw(box);
  box.querySelectorAll('details[data-key]').forEach(d=>{if(opened.has(d.dataset.key))d.open=true;});
  renderingCache.set(id,signature);
}
function table(id,headers,rows,empty='暂无记录。') {
  renderChanged(id,rows,box=>{if(!rows.length){box.append(el('p',empty,'empty'));return;}
    const t=el('table'),head=el('tr');headers.forEach(h=>head.append(el('th',h)));t.append(head);
    rows.forEach(row=>{const tr=el('tr');row.forEach(value=>tr.append(el('td',value)));t.append(tr);});box.append(t);
  });
}
function addList(parent,title,items) {
  if(!items?.length)return;
  parent.append(el('h4',title));const ul=el('ul');items.forEach(v=>ul.append(el('li',v)));parent.append(ul);
}
function researchDecision(item,marketPhase) {
  const p=item.plan;
  const pending=openOrderSummary(item);
  if(pending)return {title:item.open_orders.some(o=>o.status==='UNKNOWN')?'委托状态待核对':'已有委托，等待成交',reason:pending,ready:false};
  if(item.archived||item.research_status==='ARCHIVED')return {title:'已归档，保留历史研究',reason:'不再据此新增买入；已有持仓和未完成委托继续管理。',ready:false};
  if(item.entry_allowed===false)return {title:'暂停新增买入',reason:item.entry_reason||'等待新的研究依据',ready:false};
  if(!p&&item.membership==='DYNAMIC')return {title:'等待完整公司研究',reason:'已发现相关线索，仍需核对公司经营、估值及交易条件。',ready:false};
  if(!p)return {title:'等待首次研究',reason:'点击“更新研究”，获取资料并生成分析。',ready:false};
  if(p.effective_status==='EXPIRED')return {title:'研究已过期，等待更新',reason:'以下保留上次研究供参考，暂不据此新增持仓。',ready:false};
  if(p.effective_status==='DRAFT')return {title:'本轮研究尚未完成',reason:'待模型分析完成后更新判断，当前不新增持仓。',ready:false};
  const reasons=[...new Set((p.payload.blockers||[]).map(blocker))];
  const event=p.trade_guidance?.groups?.find(g=>g.key.startsWith('event:'));
  if(event&&p.payload.kind==='NO_ENTRY')return {title:'待核验公告',reason:event.title+'，核验通过并更新研究后再评估买入。',ready:false};
  const r=p.research?.stocks?.find(s=>s.symbol===item.symbol);
  if(p.payload.kind==='NO_ENTRY'){
    const codes=p.payload.blockers||[],has=prefix=>codes.some(c=>c.startsWith(prefix));
    const data=codes.filter(c=>/^(SOURCE_GAP|NO_COLLECTION_COVERAGE|FINANCIAL_BASELINE_INCOMPLETE|MARKET_CONTEXT_INCOMPLETE|STALE_DAILY_BARS|UNADJUSTED_SERIES_MISSING|LOCAL_ONLY_DOCUMENTS_UNREVIEWED)/.test(c));
    if(has('UNRESOLVED_EVENT')||has('CORPORATE_ACTION_UNVERIFIED'))return {title:'待核验公告',reason:'公司事项尚未核验，等待核对公告正文和实施安排。',ready:false};
    if(data.length)return {title:'等待资料补齐',reason:[...new Set(data.map(blocker))].slice(0,2).join('；')+'，补齐后重新评估买入。',ready:false};
    if(has('UNREAD_DOCUMENT'))return {title:'等待读完关键资料',reason:'还有 '+codes.filter(c=>c.startsWith('UNREAD_DOCUMENT:')).length+' 份关键资料需要取得正文或继续研究，完成后再评估买入。',ready:false};
    if(has('MODEL_NOT_READY')||has('SOURCE_CHANGED_DURING_RESEARCH'))return {title:'等待研究更新',reason:'等待本轮研究完成，并核对新到资料后再评估买入。',ready:false};
    if(has('RESEARCH_VETO'))return {title:'研究暂不支持买入',reason:item.report?.next_checks?.length?'需先确认：'+brief(item.report.next_checks[0]):'现有证据尚不支持买入，等待经营或风险证据改善。',ready:false};
    if(has('TREND_NOT_CONFIRMED'))return {title:'等待趋势恢复',reason:'价格趋势尚未满足买入条件，需20日均价高于60日均价，且最近收盘不低于60日均价。',ready:false};
    return {title:'暂不买入',reason:reasons.slice(0,2).join('；')||'当前资料还不能确认买入条件。',ready:false};
  }
  if(p.payload.kind==='RISK_EXIT_ONLY')return {title:'仅允许减仓',reason:'现有持仓触及保护条件，能否卖出还需检查可卖数量和盘面。',ready:false};
  const l=p.payload.levels,price=item.quote?.price_cents;
  if(!l||!Number.isFinite(price))return {title:'等待更新报价',reason:'需取得有效报价和买入参考区间后，才能判断价格条件。',ready:false};
  if(price<l.buy_low_cents)return {title:'等待价格回升至买入区间',reason:'最近报价低于买入下限 '+money(l.buy_low_cents)+' 元，需回升进入区间且盘面检查通过后才评估买入。',ready:true};
  if(price>l.buy_high_cents)return {title:'等待价格回落至买入区间',reason:'最近报价高于买入上限 '+money(l.buy_high_cents)+' 元，需回落进入区间且盘面检查通过后才评估买入。',ready:true};
  return {title:'最近报价在买入区间',reason:(marketPhase==='CLOSED'?'当前休市，下个交易时段按届时有效策略检查买卖。':'价格条件满足，按现有有效策略检查买卖；进入区间无需重新研究。'),ready:true};
}
function overviewSummary(item,verdict) {
  const text=item.report?.overview;
  const prior=item.plan&&item.plan.effective_status!=='ACTIVE'?'上次研究：':'';
  const reason=verdict.reason.replace(/[。！？]+/g,'；').replace(/[；\s]+$/,'')+'。';
  return (text?prior+text+'。':'')+reason;
}
function entryBand(p){return (p?.payload?.risk_parameters?.paper_entry_band_bps??100)/100;}
function renderStocks(s) {
  renderChanged('stocks',s.watchlist,box=>s.watchlist.forEach(item=>{
    const card=el('article',null,'stock'),head=el('div',null,'stock-head'),name=el('div'),quote=el('div',null,'quote');
    name.append(el('h3',item.name),el('span',item.symbol,'stock-code'));
    quote.append(el('strong',money(item.quote?.price_cents)),el('span',' 元'),el('p',item.quote?'报价 '+shortTime(item.quote.observed_at):'报价尚未取得'));
    head.append(name,quote);card.append(head);
    const verdict=researchDecision(item);card.append(el('p',verdict.title,'verdict'+(verdict.ready?' ready':'')),el('p',verdict.reason,'reason'));
    const p=item.plan;if(!p){renderFailures(card,item);box.append(card);return;}
    const l=p.payload.levels,r=p.research?.stocks?.find(stock=>stock.symbol===item.symbol);
    renderDecisionCard(card,item.report?.decision,item.symbol);
    if(l){const levels=el('div',null,'levels');[['买入参考区间',money(l.buy_low_cents)+'–'+money(l.buy_high_cents)],['卖出参考',money(l.sell_cents)],['止损参考',money(l.stop_cents)]].forEach(([k,v])=>{const d=el('div');d.append(el('span',k),el('b',v));levels.append(d);});card.append(levels);}
    const stamp=el('div',null,'stamp');stamp.append(el('span','研究更新 '+shortTime(p.activated_at)),el('span','有效至 '+shortTime(p.valid_until)));card.append(stamp);
    renderTradeGuidance(card,item);
    renderRecovery(card,item);
    if(item.latest_study&&item.latest_study.id!==p.study_id)card.append(el('p','最新一次研究'+label(item.latest_study.model_status)+'，当前保留上一版结论。','attention'));
    const report=item.report||{},detail=details('阅读研究报告','stock:'+item.symbol),body=el('div',null,'detail-content');
    body.append(el('h4','当前判断'),el('p',report.analysis||'尚无完整分析。'));
    renderResearchChanges(body,p);
    renderDossier(body,p,item.symbol);
    addList(body,'主要风险',report.risks);addList(body,'接下来观察',report.next_checks);
    if(report.facts?.length){const evidence=details('查看原文依据（'+report.facts.length+' 条）','evidence:'+item.symbol);report.facts.forEach(f=>{const d=el('p');d.append(el('span',f.quote+' '),link('查看原文','/api/document?id='+encodeURIComponent(f.evidence_id.split(':')[0])));evidence.append(d);});body.append(evidence);}
    if(p.payload.corporate_actions?.length)body.append(el('p','参考均线已按核验通过的纯现金分红处理：从除息日前的收盘价扣减每股税前分红，原始价格另行保留。当前报价不作调整。','subtle'));
    const methodology=details('参考价位如何使用','formula:'+item.symbol);const risk=p.payload.risk_parameters;const formula=p.payload.kind==='RISK_EXIT_ONLY'?'根据持仓成本和设定的止损比例计算，只允许减少已有持仓。':risk?'以近 20 个交易日的平均收盘价为基准，上下 '+entryBand(p)+'% 为买入观察区间；下方 '+(risk.paper_stop_loss_bps/100)+'% 为止损参考，上方 '+(risk.paper_take_profit_bps/100)+'% 为卖出参考。20 日均价须高于 60 日均价，且昨日收盘价不低于 60 日均价。':'资料尚不完整，暂时无法说明参考价位。';methodology.append(el('p',formula),el('p','这些价位用于模拟观察，不代表公司合理估值；资料不齐或条件未满足时不据此买入。','subtle'));body.append(methodology);detail.append(body);card.append(detail);renderFailures(card,item);box.append(card);
  }));
}
function renderDecisionCard(card,decision,symbol) {
  if(!decision)return;
  const section=el('section',null,'decision-card');section.setAttribute('aria-label','交易判断五问');
  section.append(el('h4','当前倾向'),el('p',decision.inclination));
  section.append(el('h4','关键证据'));
  if(decision.key_evidence?.length){
    const list=el('ol');
    decision.key_evidence.slice(0,3).forEach(f=>{const row=el('li');row.append(el('span',f.implication+' '),link('依据','/api/document?id='+encodeURIComponent(f.evidence_id.split(':')[0])));list.append(row);});section.append(list);
  }else section.append(el('p','目前还没有足够的关键原文证据。','subtle'));
  [['价格反映了多少',decision.pricing],['等待什么触发',decision.trigger],['什么会推翻判断',decision.invalidation]].forEach(([title,text])=>{section.append(el('h4',title),el('p',text));});
  card.append(section);
}
function dataTable(parent,headers,rows) {
  const wrap=el('div',null,'data-table'),t=el('table'),head=el('tr');headers.forEach(h=>head.append(el('th',h)));t.append(head);
  rows.forEach(row=>{const tr=el('tr');row.forEach(v=>tr.append(el('td',v==null?'未取得':String(v))));t.append(tr);});wrap.append(t);parent.append(wrap);
}
function renderDossier(body,plan,symbol) {
  const d=plan.company_dossier,m=plan.market_context||{};
  if(!d&&!Object.keys(m).length)return;
  const section=details('公司财务底稿与量价对照','dossier:'+symbol);
  section.append(el('p','这里保留形成判断时使用的财务和行情资料；下次研究会核对新变化。','subtle'));
  if(d){
    section.append(el('h4','财务底稿'),el('p',(d['状态']||'待补齐')+(d['报告期']?' · 报告期 '+d['报告期']:'')));
    if(d['来源']){const source=el('p',d['来源']+' · ','subtle');if(d['来源链接'])source.append(link('查看来源',d['来源链接']));section.append(source);}
    if(d['最新财务'])dataTable(section,['指标','最新披露值'],Object.entries(d['最新财务']));
    if(d['累计同比（%）'])dataTable(section,['指标','累计同比'],Object.entries(d['累计同比（%）']).map(([k,v])=>[k,v==null?'基期缺失或非正，不计算百分比':v+'%']));
    if(d['近八季']?.length)dataTable(section,['报告期','收入','归母利润','扣非利润','经营现金流'],d['近八季'].map(r=>[r['报告期'],r['营业总收入（单季，亿元）'],r['归母净利润（单季，亿元）'],r['扣非归母净利润（单季，亿元）'],r['经营现金流净额（单季，亿元）']]));
    if(d['近八季']?.length)section.append(el('p','上表金额为单季、亿元；缺少相邻累计报表时不推算单季。','subtle'));
    if(d['近三年年报']?.length){const years=details('近三年年报','annual:'+symbol);dataTable(years,['报告期','营业总收入','归母净利润','经营现金流净额'],d['近三年年报'].map(r=>[r['报告期'],r['营业总收入'],r['归母净利润'],r['经营现金流净额']]));section.append(years);}
    const v=d['估值参考'];if(v){section.append(el('h4','估值参考'));dataTable(section,['指标','倍数'],[['报告期股本估算市盈率',v['报告期股本估算市盈率']],['报告期每股净资产口径市净率',v['报告期每股净资产口径市净率']]]);section.append(el('p',v['说明'],'subtle'));addList(section,'估值边界',v['缺口']);}
    if(d['单位说明'])section.append(el('p',d['单位说明'],'subtle'));
    if(d['核验范围'])section.append(el('p',d['核验范围'],'subtle'));
    addList(section,'财务缺口',d['缺口']);addList(section,'使用边界',d['限制']);
  }
  if(Object.keys(m).length){
    section.append(el('h4','量价与市场对照'));
    dataTable(section,['指标','观察值'],Object.entries(m).filter(([k,v])=>!['说明','缺口','对照说明'].includes(k)&&typeof v!=='object'));
    if(m['市场与行业对照']?.length)dataTable(section,['对照对象','20日收益（%）','20日个股超额（百分点）','60日收益（%）','60日个股超额（百分点）'],m['市场与行业对照'].map(r=>[r['名称'],r['20日']?.['收益（%）'],r['20日']?.['个股超额（百分点）'],r['60日']?.['收益（%）'],r['60日']?.['个股超额（百分点）']]));
    if(m['业务参考篮子'])dataTable(section,['业务参考篮子','数值'],Object.entries(m['业务参考篮子']));
    section.append(el('p',m['对照说明']||'','subtle'),el('p',m['说明']||'','subtle'));addList(section,'对照缺口',m['缺口']);
  }
  body.append(section);
}
function renderTradeGuidance(card,item) {
  const guide=item.plan?.trade_guidance,groups=guide?.groups||[];
  // Keep this target stable after a successful update removes the final blocker.
  const target='trade-guidance-feedback-'+item.symbol,feedbackBox=el('p',null,'feedback');
  feedbackBox.id=target;feedbackBox.setAttribute('role','status');feedbackBox.hidden=true;
  if(!groups.length){card.append(feedbackBox);return;}
  const section=details('当前交易限制（'+groups.length+' 类）','trade-guidance:'+item.symbol);
  section.className='trade-guidance';section.append(el('p',guide.summary,'subtle'));
  groups.forEach(g=>{
    const row=el('section',null,'trade-guidance-item');row.append(el('h4',g.title),el('p',g.why));
    [['在等什么',g.waiting],['你可以做什么',g.user_action],['何时解除',g.release]].forEach(([label,text])=>{
      const p=el('p');p.append(el('strong',label+'：'),el('span',text));row.append(p);
    });
    if(g.documents?.length){
      const list=details('查看具体资料（'+g.documents.length+' 份）','trade-documents:'+item.symbol+':'+g.key),ul=el('ul');
      g.documents.forEach(d=>{const li=el('li');li.append(d.url?link(d.title,d.url):el('strong',d.title),el('p',d.state,'subtle'));ul.append(li);});
      list.append(ul);row.append(list);
    }
    section.append(row);
  });
  card.append(section,feedbackBox);
}
function renderRecovery(card,item) {
  const r=item.recovery;if(!r)return;
  const section=details('资料补齐进度','recovery:'+item.symbol);
  section.append(el('p',r.why),el('p',r.next_step),el('p','今天自动补齐 '+r.automatic_attempts+' / '+r.automatic_limit+' 次；每次研究最多尝试 '+(r.research_attempts||2)+' 次。','subtle'));
  const target='recovery-feedback-'+item.symbol,box=el('p',null,'feedback');box.id=target;box.setAttribute('role','status');box.hidden=true;
  if(r.action){const b=el('button','补齐资料并重研');b.dataset.run='repair';b.dataset.symbol=item.symbol;b.dataset.feedback=target;b.dataset.idleLabel=b.textContent;b.addEventListener('click',()=>runAction(b));section.append(b,el('p','仅处理本股票，不下单；已读内容不重复整篇研究。','subtle'));}
  const help=link('查看补资料与接口设置步骤','/help/recovery');help.className='admin-only';section.append(help,box);card.append(section);
  const reviews=item.plan?.payload?.event_reviews||[];
  if(reviews.length){const checks=details('事项核验记录','event-checks:'+item.symbol);reviews.forEach(r=>{const row=el('section');row.append(el('h4',r.title),el('p',r.status==='VERIFIED'?'证据与规则核验通过；此事项不再单独阻止买入。':'尚未通过：'+r.missing.join('；')));const cash=r.facts?.cash_per_share||(r.facts?.cash_per_share_cents?money(r.facts.cash_per_share_cents):null);if(cash)row.append(el('p','每股税前现金分红 '+cash+' 元 · 登记日 '+(r.facts.record_date||'—')+' · 除息日 '+(r.facts.ex_date||'—')));if(r.facts?.follows)row.append(el('p','随分红实施发布的公告'+(r.facts.ex_date?'（除息日 '+r.facts.ex_date+'）':'')+'：本身不调整价格，随对应的实施公告一并核验。','subtle'));row.append(link('核验原文','/api/document?id='+encodeURIComponent(r.doc_id)));checks.append(row);});checks.append(el('p','通过单个事项不代表可以下单；仍需完整研究、价格、资金及盘面条件同时满足。','subtle'));card.append(checks);}
}
function drawFollowups(box,data) {
  const items=data.items||[],needs=items.filter(i=>['USER','ENGINEERING'].includes(i.owner));
  box.append(el('p','本日发生 '+(data.failed_today||0)+' 项资料处理故障，已恢复 '+(data.recovered_today||0)+' 项；当前 '+items.length+' 项待处理或等待条件，含 '+(data.carried_over||0)+' 项跨日跟进。','subtle'));
  if(data.stale)box.append(el('p','这份清单尚未更新，请检查自动运行状态；以下保留上次安排，不能据此认为问题已恢复。','attention'));
  if(!items.length&&!data.stale)box.append(el('p','当前没有待处理事项；后续发现的失败和卡点会在这里给出处理安排。'));
  function issue(i,parent) {
    const d=details(i.name+' · '+i.title+' · '+i.state_name,'followup:'+i.id);
    d.append(el('p','原因：'+i.reason),el('p','影响：'+i.impact),el('p','由谁处理：'+i.owner_name),el('p','下一步：'+i.next_action));
    if(i.escalation)d.append(el('p',i.escalation,'attention'));
    d.append(el('p',(i.next_action_at?'最早检查 / 安排时间：'+shortTime(i.next_action_at)+'。':'')+'继续条件：'+i.trigger),el('p','完成标准：'+i.completion));
    if(i.waiting_for)d.append(el('p','等待的具体内容：'+i.waiting_for));
    if(i.affected_stocks?.length)d.append(el('p','涉及股票：'+i.affected_stocks.join('、')));
    if(i.first_seen_at)d.append(el('p','首次记录 '+shortTime(i.first_seen_at)+(i.occurrences>1?' · 第 '+i.occurrences+' 次出现':''),'subtle'));
    for(const doc of i.documents||[])if(doc.url){const p=el('p');p.append(link(doc.title,doc.url));d.append(p);}
    if(i.run){const b=el('button',({repair:'补齐本股票',review:'更新复盘',research:'重新研究已有资料',collect:'只更新资料',cycle:'更新自选股研究'})[i.run]||'处理此任务');b.dataset.run=i.run;if(i.run==='repair')b.dataset.symbol=i.symbol;
      const target='followup-feedback-'+i.id;b.dataset.feedback=target;b.dataset.idleLabel=b.textContent;b.addEventListener('click',()=>runAction(b));
      const f=el('p',null,'feedback');f.id=target;f.hidden=true;f.setAttribute('role','status');d.append(b,f);}
    parent.append(d);
  }
  if(needs.length){box.append(el('p','需要处理 '+needs.length+' 项：'+needs.slice(0,3).map(i=>i.name+' · '+i.title).join('；')+(needs.length>3?'等':''),'attention'));const group=details('需要你提供材料或交给程序维护（'+needs.length+'）','followups:action');needs.forEach(i=>issue(i,group));box.append(group);}
  for(const [owners,title,key] of [[['SYSTEM'],'系统处理安排','auto'],[['DISCLOSURE','MARKET'],'等待披露或市场条件','waiting']]){
    const rows=items.filter(i=>owners.includes(i.owner));if(!rows.length)continue;
    const group=details(title+'（'+rows.length+'）','followups:'+key);rows.forEach(i=>issue(i,group));box.append(group);
  }
  if(data.today_failures?.length){const history=details('本日失败与恢复记录','followups:today');data.today_failures.forEach(f=>history.append(el('p',(f.symbol==='MARKET'?'公共任务':f.symbol)+' · '+f.title+' · '+(f.recovered?'已恢复':'未恢复')+' · '+shortTime(f.last_failed_at)),el('p',f.next_step,'subtle')));box.append(history);}
  if(data.today_jobs?.length){const jobs=details('本日未完成任务（'+data.today_jobs.length+'）','followups:jobs');data.today_jobs.forEach(j=>jobs.append(el('p',j.title+' · '+shortTime(j.at)),el('p',j.next_step,'subtle')));box.append(jobs);}
  for(const day of data.history||[]){const history=details(day.day+' 处理汇总','followups:day:'+day.day);history.append(el('p','截至 '+shortTime(day.updated_at)+' · 故障 '+day.failed_today+' 项、已恢复 '+day.recovered_today+' 项；当时尚有 '+day.items.length+' 项待处理或等待。','subtle'));day.items.forEach(i=>history.append(el('p',i.name+' · '+i.title+' → '+i.next_action)));box.append(history);}
}
function renderFollowups(s) {
  if(!s.followups)return;
  $('followup-timing').textContent=s.followups.updated_at?'核对于 '+shortTime(s.followups.updated_at):'等待首次检查';
  renderChanged('followups',s.followups,box=>drawFollowups(box,s.followups));
}
function renderResearchChanges(body,plan) {
  const learned=plan.learning;if(!learned||!Object.keys(learned).length)return;
  const text=learned.new_documents?'本次阅读 '+learned.new_documents+' 份新增、修订或待补资料。':'本次没有新增原文，结合行情和复盘复核已有判断。';
  body.append(el('p',text+(learned.mode==='DELTA'?' 已读正文不重复整篇发送，财务底稿和重要引文持续保留。':' 首次建立该股票的研究记录。'),'subtle'));
  if(learned.pending_chunks)body.append(el('p','仍有 '+learned.pending_chunks+' 段资料待后续阅读。'+(learned.required_pending_chunks!=null?' 其中关键内容 '+learned.required_pending_chunks+' 段；一般附录与参考材料待读单独提示。':'当前结论不代表已读完所有资料。'),learned.required_pending_chunks?'attention':'subtle'));
  if(plan.external_events?.length){
    const related=details('相关行业与国际消息（'+plan.external_events.length+' 条）','external:'+plan.symbol);
    related.append(el('p','以下是需要核实的关联线索；公司实际敞口和影响方向以研究正文及公司披露为准。','subtle'));
    plan.external_events.forEach(event=>{const row=el('p');row.append(link(event.title,'/api/document?id='+encodeURIComponent(event.doc_id)),el('span',' · '+event.topics.join('、')));related.append(row);});
    body.append(related);
  }
  if(plan.background_events?.length){
    const background=details('公共背景与合并报道（'+plan.background_events.length+' 条）','background:'+plan.symbol);
    background.append(el('p','以下资料不进入个股主研究，也不因其未读而阻断新增买入。','subtle'));
    plan.background_events.forEach(e=>{const p=el('p');p.append(link(e.title,'/api/document?id='+encodeURIComponent(e.doc_id)),el('span',' · '+e.reason,'subtle'));background.append(p);});body.append(background);
  }
  const optional=(plan.coverage||[]).filter(m=>!m.fulltext||m.pending_chunks);
  if(optional.length){const section=details('一般资料覆盖说明（'+optional.length+' 份）','optional:'+plan.symbol);section.append(el('p','这些资料按需补读，本身不构成新增买入的硬性限制。','subtle'));optional.forEach(m=>section.append(el('p',m.title+' · '+(m.fulltext?'正文剩余 '+m.pending_chunks+' 段':'正文待补'))));body.append(section);}
}
function renderFailures(card,item) {
  const failures=item.failures||[];if(!failures.length)return;
  const section=el('section',null,'stock-failures');section.setAttribute('aria-label',item.name+'失败项');
  const head=el('div',null,'failure-heading');head.append(el('h4','失败项'),el('span',failures.length+' 项待恢复','subtle'));section.append(head);
  const list=el('ul');
  failures.forEach(f=>{const row=el('li');row.append(el('strong',f.label+(f.title?' · '+f.title:'')),el('p',f.reason+' '+(f.trading_effect||f.impact)),el('p',(f.shared?'公共资料 · ':'')+'最近失败 '+shortTime(f.last_failed_at),'subtle'));
    if(f.next_step){const help=details('处理办法','failure-help:'+item.symbol+':'+f.key);help.append(el('p',f.recovery),el('p',f.next_step));if(f.source_url)help.append(link('打开资料原文',f.source_url));row.append(help);}list.append(row);});
  section.append(list,el('p','对应资料后续获取并处理成功后，该项会自动消失。','subtle'));card.append(section);
}
function renderAccount(s) {
  const a=s.account,p=s.portfolio;
  renderChanged('metrics',[a.equity_cents,a.available_cents,p.total_profit_cents,p.stock_weight_pct,a.valuation_complete],box=>{
    [[a.valuation_complete?'总资产（元）':'总资产估值（元）',money(a.equity_cents)],['可用现金（元）',money(a.available_cents)],['累计盈亏（含已提取）',signed(p.total_profit_cents)],['持仓比例',percent(p.stock_weight_pct)]].forEach(([title,value],i)=>{const d=el('div',null,'metric');d.append(el('span',title),el('strong',value));if(i===2)d.append(el('span','涨跌相抵，已计交易费用','metric-note'));box.append(d);});
  });
  $('position-count').textContent=p.holdings.length?p.holdings.length+' 只股票':'当前空仓';
  renderChanged('allocation',[p.holdings,p.cash_weight_pct],box=>{
    const colors=['#b5c5be','#23785e','#ba9356','#647da6'],bar=el('div',null,'allocation'),legend=el('div',null,'legend');
    [{name:'现金',weight:p.cash_weight_pct},...p.holdings.map(h=>({name:h.name,weight:h.weight_pct}))].forEach((v,i)=>{const part=el('span'),entry=el('span'),dot=el('i');part.style.width=Math.max(0,Math.min(100,v.weight||0))+'%';part.style.background=colors[i%colors.length];part.title=v.name+' '+percent(v.weight);dot.style.background=colors[i%colors.length];bar.append(part);entry.append(dot,document.createTextNode(v.name+' '+percent(v.weight)));legend.append(entry);});box.append(bar,legend);
  });
  table('holdings',['资产','市值 / 占比','浮动盈亏'],[['现金',money(a.cash_cents)+' / '+percent(p.cash_weight_pct),'—'],...p.holdings.map(h=>[h.name+(h.origin==='global'?'（现货 / 美股）':h.origin==='dynamic'?'（动态）':'（公司与资产）')+' · '+h.qty+' 股（可卖 '+h.sellable_qty+'）',money(h.market_value_cents)+' / '+percent(h.weight_pct),signed(h.unrealized_cents)+(h.valuation_basis==='COST_FALLBACK'?'（缺少报价）':'')])]);
  $('valuation').textContent=p.holdings.length?'报价时间：'+p.holdings.map(h=>h.name+' '+shortTime(h.quote_at)).join('；'):'尚未建仓。满足研究和盘面条件后，持仓会在这里更新。';
  renderChanged('effects',s.trade_effects,box=>{
    const t=s.trade_effects;
    if(!t.totals.fill_count){const empty=el('div',null,'empty');empty.append(el('strong','还没有成交'),el('span','首次模拟成交后，这里会显示买入后的浮动盈亏和卖出的实际结果。'));box.append(empty);return;}
    for(const side of ['buy','sell']){const e=t[side],d=el('div',null,'outcome');d.append(el('b',side==='buy'?'最近买入':'最近卖出'));
      if(!e){d.append(el('p',side==='buy'?'暂无买入成交。':'暂无卖出成交。','subtle'));box.append(d);continue;}
      d.append(el('p',e.name+' · '+shortTime(e.last_fill_at),'subtle'));
      const pnl=side==='sell'?e.realized_cents:e.unrealized_cents;
      if(side==='buy'&&!e.remaining_qty)d.append(el('p','本笔已全部卖出','pnl'));
      else d.append(el('p',(side==='sell'?'已实现 ':'持有部分浮动 ')+signed(pnl)+' 元','pnl '+(pnl>0?'profit':pnl<0?'loss':'')));
      d.append(el('p','成交 '+e.filled_qty+' 股 · 均价 '+money(e.average_price_cents)+' 元','subtle'));
      const detail=details('成交与费用详情','effect:'+side);detail.append(el('p','委托 '+e.requested_qty+' 股；'+(e.order_status==='PARTIAL'?'部分成交':label(e.order_status))+'；'+e.fill_count+' 笔成交合并，费用 '+money(e.fee_cents)+' 元。'));
      if(side==='sell')detail.append(el('p','已计入买入和卖出费用，成本收益率 '+percent(e.realized_return_pct)+'。'));
      else detail.append(el('p',e.remaining_qty?'本笔仍持有 '+e.remaining_qty+' 股；浮动收益率 '+percent(e.unrealized_return_pct)+'，未扣未来卖出费用。报价 '+shortTime(e.quote_at)+'。':'已实现盈亏见相应卖出成交。'));
      detail.append(el('p','当时决策：'+decisionReason(e.reason)));d.append(detail);box.append(d);
    }
  });
}
function jobMessage(job) {
  if(job.status==='PENDING')return ['任务已提交，正在等待处理。','pending'];
  if(job.status==='RUNNING')return [label(job.kind)+'进行中，完成后自动更新；可以离开本页。','pending'];
  if(job.status==='FAILED')return ['本次'+label(job.kind)+'未完成，已保留现有数据。可以重试，原因见“运行详情”。','error'];
  if(job.status==='DEFERRED'){
    if(job.kind==='portfolio_strategy')return ['本轮组合判断未完成；原退出检查继续，新增买入须等待有效的组合买入安排。','error'];
    if(job.kind==='review'){
      let result;try{result=JSON.parse(job.result_json||'null');}catch(_){result=null;}
      return ['持仓盈亏已核算，逐仓分析待补齐。'+(result?.analysis_error||'请查看复盘卡片中的原因。'),'error'];
    }
    return ['本次研究重试后仍未完成；有效旧计划保留，请查看该股票失败项中的处理办法。','error'];
  }
  if(job.status==='INTERRUPTED')return ['任务因服务重启而中断，请查看后续恢复任务。','error'];
  let r;try{r=JSON.parse(job.result_json||'null');}catch(_){r=null;}
  if(job.status!=='DONE')return [label(job.kind)+'：'+label(job.status),''];
  if(job.kind==='portfolio_strategy')return ['组合判断已更新，目标仓位与调整依据见公司与资产的组合策略。',''];
  if(job.kind==='dynamic_cycle')return [r?.research_deferred||('新闻采集与研究已结束；'+(r?.failures?.length?'部分来源待恢复，详情见新闻与事件。':'没有达标机会时继续积累样本。')),r?.failures?.length?'error':''];
  if(job.kind==='repair'){
    if(r?.status==='NEEDS_INPUT')return [r.reason||'等待新的资料或事件核验。',''];
    return ['本股票已重新研究；是否可以买入仍以更新后的交易限制为准。',''];
  }
  if(job.kind==='slot'){
    if(r?.status==='CLOSED')return ['当前休市，本次未进行交易。下一交易时段会自动检查。',''];
    if(['MISSED','EXPIRED','CALENDAR_UNKNOWN'].includes(r?.status))return ['本次未进行交易：'+label(r.status)+'。','error'];
    return [r?.operation_count===0?'盘面检查已完成，本次没有新增委托；已有委托的成交情况见记录。':r?.operation_count>0?'已提交 '+r.operation_count+' 笔模拟委托；成交情况见记录。':'盘面检查已完成，买卖操作和成交情况见下方。',''];
  }
  if(job.kind==='cycle'||job.kind==='research'){
    const studies=Array.isArray(r)?r:r?.studies||[],ok=studies.filter(x=>x.status==='SUCCEEDED').length;
    if(studies.some(x=>x.status!=='SUCCEEDED'))return [ok+' 只股票研究已更新，其他股票的模型分析尚未完成；请查看各股状态。','error'];
    if(!ok)return ['本次任务已结束，尚未生成新的研究结论，请查看资料和任务详情。','error'];
    return [ok+' 只股票的研究已更新。',''];
  }
  if(job.kind==='review'&&(r?.model_status||r?.status)==='DEFERRED')return ['持仓盈亏已核算，逐仓分析待补齐。'+(r?.analysis_error||''),'error'];
  return [label(job.kind)+'已完成。',''];
}
function actionJob(s,kind,symbol) {
  const research=['collect','research','cycle'].includes(kind);
  const jid=kind==='repair'?s?.watchlist.find(w=>w.symbol===symbol)?.recovery?.job_id:null;
  return (s?.active_jobs||[]).find(j=>kind==='repair'?j.id===jid:research?['collect','research','cycle'].includes(j.kind):j.kind===kind);
}
function updateButtons() {
  document.querySelectorAll('[data-run]').forEach(b=>{
    const kind=b.dataset.run,research=['collect','research','cycle'].includes(kind);
    const key=kind==='repair'?kind+':'+b.dataset.symbol:kind,job=actionJob(state,kind,b.dataset.symbol);
    const busy=submitting.has(key)||Boolean(job);
    b.disabled=!connected||busy||(state?.deployment_role==='cloud'&&!['slot','dynamic_slot','global_slot'].includes(kind))||(state?.deployment_role==='research'&&['slot','dynamic_slot','global_slot'].includes(kind));
    if(state?.deployment_role==='cloud'&&!['slot','dynamic_slot','global_slot'].includes(kind))b.title='由研究电脑按计划执行';
    b.textContent=job?.status==='PENDING'?'排队中…':busy?(research?'更新中…':'处理中…'):b.dataset.idleLabel;
  });
  for(const id of ['save','toggle','dynamic-toggle'])if($(id)&&!$(id).dataset.saving)$(id).disabled=!connected||state?.deployment_role==='cloud';
}
function reviewDetails(title,key) {const d=details('',key);d.children[0].textContent=title;return d;}
function reviewFreshness(r,at) {
  const hours=(Date.parse(at)-Date.parse(r?.window_end))/3600000;
  return Number.isFinite(hours)&&hours>36?'本期复盘截止于 '+when(r.window_end)+'，已超过36小时；仅供历史回看，不能作为当前状态。':'';
}
function drawReviewChecks(body,r) {
  const summary=r.presentation?.checks,items=summary?.items||r.payload.consistency_checks||[];
  const box=el('section',null,'review-checks');box.append(el('h3','程序核验'));
  if(!items.length){box.append(el('p','本条旧记录没有可展示的程序检查，不能视为核验通过。','subtle'));body.append(box);return;}
  const counts=summary?.counts||Object.fromEntries(['FAIL','INSUFFICIENT','PASS','NOT_APPLICABLE'].map(s=>[s,items.filter(c=>c.status===s).length]));
  box.append(el('p','异常 '+(counts.FAIL||0)+' 项 · 缺少依据 '+(counts.INSUFFICIENT||0)+' 项 · 通过 '+(counts.PASS||0)+' 项 · 不适用 '+(counts.NOT_APPLICABLE||0)+' 项',(counts.FAIL||counts.INSUFFICIENT)?'caution':'subtle'));
  if(counts.UNKNOWN)box.append(el('p','另有 '+counts.UNKNOWN+' 项状态待核对。','caution'));
  const titles={CHECK_BUY_OUTSIDE_PLAN_BAND:'买入价格与计划区间',CHECK_BUY_WITH_PLAN_BLOCKERS:'买入时研究限制',CHECK_BUY_WITHOUT_PORTFOLIO_ALLOW:'买入时组合安排',CHECK_BUY_WHILE_HALTED:'熔断期间买入',CHECK_SELL_WITHOUT_REASON:'卖出依据',CHECK_EXECUTION_EVIDENCE:'成交依据完整性',CHECK_LATE_QUOTE_IN_REVIEW:'复盘报价是否事后补录',CHECK_SYNC_STALE:'云端账本同步',CHECK_OUTBOX_BACKLOG:'策略发布积压',CHECK_MODEL_IDENTITY:'模型版本一致性',CHECK_RESEARCH_FAILURE_RATE:'研究成功情况',CHECK_DISK_SPACE:'磁盘与数据库空间'};
  const states={PASS:'通过',FAIL:'异常',INSUFFICIENT:'依据不足',NOT_APPLICABLE:'不适用'};
  const list=details('查看核验项目与依据','review-checks:'+r.id);
  for(const c of items.slice(0,24)){
    const row=el('div',null,'review-check-row');row.append(proposalText('p',(titles[c.check]||c.check||'检查项待核对')+'：'+(states[c.status]||'状态待核对')+' · 检查 '+(c.checked??'未知')+' 项 / 异常 '+(c.failures??'未知')+' 项'));
    if(c.detail)row.append(proposalText('p',c.detail,'subtle'));
    for(const e of (c.examples||[]).slice(0,3)){
      const parts=[e.symbol,e.position,e.route&&({watchlist:'自选股',global:'全球资产',dynamic:'动态研究'})[e.route],e.fill_id&&'成交 '+e.fill_id,e.reason,e.missing&&'缺少：'+e.missing,e.detail].filter(Boolean);
      if(e.last_sync)parts.push(Object.keys(e.last_sync).length?'上次同步 '+when(e.last_sync.at)+' · '+(e.last_sync.status==='OK'?'成功':'未成功'):'尚无成功同步记录');
      if(Number.isInteger(e.pending))parts.push('待发送 '+e.pending+' 份');
      if(Number.isInteger(e.failed))parts.push('失败 '+e.failed+' / '+(e.total??'未知')+' 次');
      if(Number.isFinite(e.free_gb))parts.push('磁盘可用 '+e.free_gb+' GB');
      if(Number.isFinite(e.db_gb))parts.push('数据库 '+e.db_gb+' GB');
      if(parts.length)row.append(proposalText('p',parts.join(' · '),'subtle'));
    }
    list.append(row);
  }
  if(summary?.omitted)list.append(el('p','另有 '+summary.omitted+' 项未显示，完整检查保留在本机复盘记录。','subtle'));
  box.append(list,el('p','程序核验核对执行与数据；模型观点另列，不代表策略效果已得到验证。','subtle'));body.append(box);
}
function drawReviewFindings(body,r) {
  const data=r.presentation?.findings;
  if(!data){const lessons=r.payload.analysis?.lessons||[];if(lessons.length){const d=details('本次发现（旧记录，去向待核对）','lessons:'+r.id);for(const l of lessons.slice(0,5))d.append(proposalText('p',l.lesson));if(lessons.length>5)d.append(el('p','仅展示前5条，完整发现保留在本机。','subtle'));body.append(d);}return;}
  if(!data.items?.length)return;
  const section=el('section',null,'review-findings');section.append(el('h3','发现与跟进'));
  if(r.presentation?.status_at)section.append(el('p','处理状态核对截至 '+when(r.presentation.status_at)+'；离线时保留最近一次记录。','subtle'));
  const states={OPEN:'待处理',RESOLVED:'已解决',WONTFIX:'已记录不修复',DRAFT:'待整理草稿',READY:'待审查与决定',APPROVED:'已批准',ADOPTED:'已上线跟踪',REJECTED:'已驳回',RETIRED:'已退役',SUPERSEDED:'已被新版替代',UNKNOWN:'去向待核对'};
  const current=data.items.filter(x=>x.current),history=data.items.filter(x=>!x.current);
  function row(item,parent){const d=reviewDetails(proposalExcerpt(item.lesson,96)+' · '+(states[item.status]||'状态待核对'),'finding:'+r.id+':'+item.ordinal);d.append(proposalText('p',item.lesson));if(item.applicability)d.append(proposalText('p','待验证条件：'+item.applicability));
    if(item.id)d.append(proposalText('p',(item.to==='engineering_issue'?'工程问题：':'改进提案：')+item.id+' · '+(states[item.status]||'状态待核对')));
    if(item.updated_at)d.append(el('p','去向最近更新 '+when(item.updated_at),'subtle'));
    if(item.expired)d.append(el('p','该发现已超过30日展示期限，仅供追溯；后续以对应问题或提案的当前状态为准。','subtle'));
    else if(item.expires_at)d.append(el('p','本条发现展示至 '+when(item.expires_at)+'，期间若已处理会移入历史。','subtle'));
    parent.append(d);}
  current.slice(0,3).forEach(x=>row(x,section));
  const rest=current.slice(3).concat(history);if(rest.length){const d=details('其他及历史发现（'+rest.length+' 条）','review-findings-history:'+r.id);rest.forEach(x=>row(x,d));section.append(d);}
  if(data.omitted)section.append(el('p','另有 '+data.omitted+' 条发现保留在本机记录。','subtle'));
  section.append(el('p','复盘发现只生成工程问题或提案草稿，不会直接进入研究规则。','subtle'));body.append(section);
}
function drawDailyReview(body,r) {
  const facts=r.payload.facts||{},analysis=r.payload.analysis||{},p=facts.portfolio;
  const daily=r.presentation?.daily||facts.daily_accounting||facts.daily_portfolio||(!facts.context_48h?p:null),context=r.presentation?.context||facts.context_48h;
  body.append(el('p','24小时核算窗口：'+when(r.window_start)+' — '+when(r.window_end),'subtle'));
  const freshness=reviewFreshness(r,state?.at||r.presentation?.status_at||new Date().toISOString());if(freshness)body.append(el('p',freshness,'caution'));
  if(p){
    const t=daily?.totals,closing=daily?.closing||p.closing||{},metrics=el('div',null,'review-metrics');
    const cumulative=daily?.totals?.cumulative_profit_cents??p.totals?.cumulative_profit_cents;
    for(const [title,value] of [['本期盈亏（24小时持仓）',t?.period_profit_cents],['截止累计盈亏（持仓口径）',cumulative],['截止持仓浮动盈亏',closing.unrealized_cents],['累计已实现（成交）',closing.cumulative_realized_cents]]){
      const metric=el('div');metric.append(el('span',title,'subtle'),el('strong',signed(value)+' 元',value>0?'profit':value<0?'loss':''));metrics.append(metric);
    }
    body.append(metrics,el('p','上述持仓损益已计成交费用，不含现金分红；不能与含分红的账户累计收益直接对照。','subtle'));
    if(!daily)body.append(el('p','这份旧记录未提供独立24小时核算；不能用48小时金额代替。','caution'));
    const dividendKnown=daily?.dividends_known??Object.prototype.hasOwnProperty.call(daily||{},'dividends');
    if(dividendKnown){const cash=daily.dividend_cents??(daily.dividends||[]).reduce((n,x)=>n+(Number.isFinite(x.amount_cents)?x.amount_cents:0),0);body.append(el('p','24小时已入账现金分红 '+signed(cash)+' 元（另计）。除息造成的价格调整须结合分红阅读。','subtle'));}
    else body.append(el('p','本条记录未展示现金分红明细，需结合账户与分红记录核对。','subtle'));
    body.append(el('p','截止持有 '+(p.totals?.holding_count??'未知')+' 个标的 · 24小时 '+(t?.period_fill_count??'未知')+' 笔成交','subtle'));
    if(!p.opening?.valuation_complete||!p.closing?.valuation_complete||daily?.opening?.valuation_complete===false||daily?.closing?.valuation_complete===false)body.append(el('p','部分报价缺失、过时或未到收盘；相关盈亏可能不完整，请结合各仓行情时间阅读。','attention'));
    if((p.positions||[]).some(x=>x.opening?.late_quote||x.closing?.late_quote))body.append(el('p','本版含事后补齐的历史行情，按行情发生时间重算；这些价格不代表当时决策已知。','subtle'));
    if(context)body.append(el('p','研究回看前48小时：'+when(context.window_start)+' — '+when(context.window_end)+' · 持仓损益 '+signed(context.totals?.period_profit_cents)+' 元。滚动窗口会重叠，不能逐日相加。','subtle'));
    drawReviewChecks(body,r);
    if(analysis.summary){const d=details('模型复盘摘要（待验证）','review-summary:'+r.id);d.append(proposalText('p',analysis.summary,'review-summary'));body.append(d);}
    const verdicts={SUPPORTED:'观点获得支持',CONTRADICTED:'观点已被反证',MIXED:'部分支持，部分反证',PENDING:'仍待验证',INSUFFICIENT:'研究依据不足'};
    const qualities={RECENT:'截止前有效报价',CLOSE:'收盘后报价',INTRADAY_LAST:'盘中末次报价',STALE:'历史旧报价',MISSING:'报价缺失',NO_POSITION:'已无持仓'};
    const positions=p.positions||[],extra=details('其余持仓回看（'+Math.max(0,Math.min(positions.length,20)-3)+' 个）','review-positions:'+r.id);
    for(const [index,position] of positions.slice(0,20).entries()){
      const c=position.closing||{},a=(analysis.positions||[]).find(x=>x.position_key===position.key),card=el('article',null,'review-position');
      const head=el('div',null,'section-head');head.append(el('h3',position.name+' · '+position.symbol),el('span',a?'模型：'+(verdicts[a.verdict]||'结论待核对'):'模型分析待补齐','badge'));card.append(head);
      card.append(el('p',(context?'48小时持仓损益 ':'本期持仓损益 ')+signed(position.period_profit_cents)+' 元 · 截止浮动 '+signed(c.unrealized_cents)+' 元'));
      if(c.qty)card.append(el('p',(qualities[c.quality]||c.quality||'行情待核对')+' '+(c.quote_at?when(c.quote_at):'')+(c.late_quote?' · 补录于 '+when(c.quote_first_seen_at):''),'subtle'));
      if(a?.next_check)card.append(proposalText('p','模型建议下次核对：'+proposalExcerpt(a.next_check,180),'review-next-check'));
      const detail=details('持仓事实与模型依据','review-position:'+r.id+':'+position.key);
      detail.append(el('p','截止 '+((c.qty||0)/(c.qty_scale||1)).toLocaleString('zh-CN',{maximumFractionDigits:8})+' 股 / 份 · 买入均价 '+costPrice(c.average_cost_cents)+' 元 · 截止报价 '+money(c.price_cents)+' 元','subtle'),el('p','累计已实现（成交） '+signed(position.cumulative_realized_cents)+' 元'));
      if(a){detail.append(proposalText('p','模型判断：'+a.reason));addList(detail,'模型认为获得支持',a.supported_points);addList(detail,'模型认为被反证',a.contradicted_points);addList(detail,'仍待验证',a.pending_points);if(a.next_check?.length>180)detail.append(proposalText('p','完整下次核对事项：'+a.next_check));}
      const records=(p.research||[]).filter(x=>(position.research_ids||[]).includes(x.id));
      if(records.length){const d=details('核对当时研究（'+records.length+' 份）','review-research:'+r.id+':'+position.key);for(const record of records.slice(0,8)){
        d.append(el('h4',(record.role==='ENTRY'?'买入依据':'截止前跟踪研究')+' · '+when(record.at)));
        const source=record.analysis||{},decision=source.decision||{};d.append(proposalText('p',proposalExcerpt(source.analysis||source.mechanism||record.plan?.thesis||'原始研究未提供文字摘要。',1200)));
        if(decision.trigger)d.append(proposalText('p','触发条件：'+decision.trigger));if(decision.invalidation)d.append(proposalText('p','反证条件：'+decision.invalidation));
      }if(records.length>8)d.append(el('p','页面保留8份研究摘要，其余记录可在本机复盘材料追溯。','subtle'));detail.append(d);}
      card.append(detail);(index<3?body:extra).append(card);
    }
    if(positions.length>3)body.append(extra);if(positions.length>20)body.append(el('p','页面展示20个持仓摘要，另有 '+(positions.length-20)+' 个保留在本机完整复盘。','subtle'));
    body.append(el('p','持仓损益 = 期内已实现 + 期末浮动 − 期初浮动，已计成交费用。短期盈亏不足以单独证明研究有效。','subtle'));
  }else{
    const stats=facts.statistics||{};body.append(el('p','旧版仅保存交易统计，不能据此还原全仓收益。','subtle'),el('p',(stats.fill_count||0)+' 笔成交 · 已实现 '+signed(stats.realized_pnl_cents)+' 元','subtle'));drawReviewChecks(body,r);
    if(analysis.summary){const d=details('旧版模型摘要','review-summary:'+r.id);d.append(proposalText('p',analysis.summary));body.append(d);}
  }
  if(r.model_status==='DEFERRED')body.append(el('p','盈亏事实已保存，逐仓分析尚未完成。'+(readableError(r.payload.analysis_error)||'研究服务未能完成本次分析。')+(r.automatic_retries_remaining?'系统会在空闲时重试，最多剩余 '+r.automatic_retries_remaining+' 次。':'可点击“更新复盘”重试最新周期。'),'attention'));
  else if(r.model_status==='NOT_RUN')body.append(el('p','盈亏核算已完成，模型分析尚未运行。','subtle'));
  drawReviewFindings(body,r);
  body.append(el('p','第 '+r.revision+' 版 · 生成于 '+when(r.ready_at),'subtle'));
}
function supervisionLabel(item) {
  const status={PENDING:'待审查',RUNNING:'审查中',DEFERRED:'待审查 · 等待重试',STALE:'材料已变化 · 旧结论仅供追溯'};
  if(item.status!=='SUCCEEDED')return status[item.status]||'待审查';
  const verdict={RECOMMEND:'建议通过（非批准）',REVISE:'退回修改建议',INSUFFICIENT:'证据不足',REJECT:'建议驳回'};
  return '已审查 · '+(verdict[item.verdict]||'已记录')+(item.approval==='WAITING_USER'?' · 等待你批准':'');
}
function drawSupervision(box,data) {
  const items=(data?.items||[]).slice(0,30),current=items.filter(x=>x.status!=='STALE'&&x.display_bucket!=='HISTORY'),history=items.filter(x=>x.status==='STALE'||x.display_bucket==='HISTORY');
  if(!items.length)box.append(el('p','评估批次或完整提案形成后自动排队审查。','subtle'));
  for(const error of (data?.discovery?.errors||[]).slice(0,3))box.append(el('p','材料待核实：'+error,'caution'));
  if((data?.discovery?.error_count||data?.discovery?.errors?.length||0)>3)box.append(el('p','还有其他材料待核实，完整原因可在本机监督记录查看。','subtle'));
  if(items.length)box.append(el('p','页面展示 '+items.length+' / '+(data?.reviews_total??items.length)+' 条审查 · 每个对象优先最近一次。','subtle'));
  function draw(item,parent,compact=false){
    const row=el('article',null,'review supervision-card');row.append(proposalText('h3',proposalExcerpt(item.title||item.subject_id||'审查事项',100)),el('p',supervisionLabel(item),item.status==='DEFERRED'?'caution':'subtle'));
    const stale=item.status==='STALE';
    if(!stale&&item.summary)row.append(proposalText('p',proposalExcerpt(item.summary,240)));
    if(!stale&&item.error)row.append(el('p',readableError(item.error),'caution'));
    if(item.status==='DEFERRED')row.append(el('p',item.attempts>=3?'自动重试已用完，待人工核查。':'下次重试不早于 '+when(item.next_attempt_at),'subtle'));
    row.append(el('p',(({BATCH:'运行与评估',PROPOSAL:'方案审查',FOLLOWUP:'上线后复核'})[item.kind]||'审查')+' · '+(item.reviewer==='chatgpt'?'ChatGPT':item.reviewer||'审查员')+' · '+when(item.finished_at||item.created_at),'subtle'));
    const d=details(stale?'查看失效记录标识':'查看审查依据与下一步','supervision:'+item.id);d.append(proposalText('p','审查编号：'+item.id));
    if(stale)d.append(el('p','材料已变化，原建议不再作为当前行动依据。完整旧结论保留在本机供追溯。','subtle'));
    else{
      if(item.summary?.length>240)d.append(proposalText('p',item.summary));
      for(const c of (item.result?.checks||[]).slice(0,6))d.append(proposalText('p',(({evidence:'证据',version:'版本对应',attribution:'效果归因',counterexamples:'反证',validation:'检验方案',risk:'风险边界'})[c.id]||'核对')+'：'+c.reason));
      for(const c of (item.result?.counterexamples||[]).slice(0,5))d.append(proposalText('p','可能推翻结论：'+c));
      for(const step of (item.result?.next_steps||[]).slice(0,5))d.append(proposalText('p',(compact?'历史建议：':'后续建议：')+step));
      d.append(el('p','本次审查针对提交时保存的材料，后续材料变化后需重新核对。监督建议不等于用户批准。','subtle'));
    }
    row.append(d);parent.append(row);
  }
  current.slice(0,3).forEach(item=>draw(item,box));
  if(current.length>3){const d=details('其他当前审查（'+(current.length-3)+' 条）','supervision:more');current.slice(3).forEach(item=>{const entry=reviewDetails(proposalExcerpt(item.title||item.subject_id,90)+' · '+supervisionLabel(item),'supervision-entry:'+item.id);draw(item,entry);d.append(entry);});box.append(d);}
  if(history.length){const d=details('历史及失效审查（'+history.length+' 条）','supervision:history');history.forEach(item=>{const entry=reviewDetails(proposalExcerpt(item.title||item.subject_id,90)+' · '+supervisionLabel(item),'supervision-entry:'+item.id);draw(item,entry,true);d.append(entry);});box.append(d);}
  const omitted=Math.max(0,data?.reviews_omitted||0)+(Math.max(0,(data?.items?.length||0)-30));
  if(omitted)box.append(el('p','另有 '+omitted+' 条历史或未列出记录保留在本机，可按审查编号继续核对。页面不删除记录。','subtle'));
}
function proposalReviewLabel(review) {
  if(!review)return '尚未提交监督审查';
  if(review.current===false||review.status==='STALE')return '材料已变化 · 旧结论仅供追溯';
  const status={PENDING:'待审查',RUNNING:'审查中',DEFERRED:'等待重试'};
  if(review.status!=='SUCCEEDED')return status[review.status]||'审查状态待核对';
  return '已审查 · '+({RECOMMEND:'建议通过（非批准）',INSUFFICIENT:'证据不足',REVISE:'建议修改',REJECT:'建议驳回'}[review.verdict]||'结论待核对');
}
function proposalText(tag,value,cls) {
  // These are evidence and immutable identifiers: keep literal text, never HTML.
  const node=el(tag,null,cls);node.textContent=typeof value==='string'?value:'';return node;
}
function proposalField(box,title,value,empty='尚未填写，需补齐。') {
  const section=el('section',null,'proposal-field');
  section.append(el('h4',title),proposalText('p',typeof value==='string'&&value.trim()?value:empty));box.append(section);
}
function proposalExcerpt(value,limit=260) {return typeof value==='string'&&value.length>limit?value.slice(0,limit)+'…':value;}
function proposalExperimentLabel(experiment) {
  return {DESIGNED:'已登记设计 · 未运行',RUNNING:'运行中',COMPLETED:'观察已完成',INCONCLUSIVE:'已结束 · 尚不能判断',CANCELLED:'已取消',REJECTED:'已否决',INVALID:'设计材料需核对'}[experiment?.status]||'状态待核对';
}
function proposalExecutionLines(execution) {
  if(!execution||typeof execution!=='object')return [];
  const count=value=>Number.isInteger(value)&&value>=0?String(value):'待核对';
  const stamp=value=>typeof value==='string'&&Number.isFinite(Date.parse(value))?when(value):'时间待核对';
  const rate=value=>Number.isFinite(value)&&value>=0&&value<=1?(value*100).toFixed(1)+'%':'尚无完整配对结果';
  const delta=value=>Number.isFinite(value)&&Math.abs(value)<=1?(value>0?'+':'')+(value*100).toFixed(1)+' 个百分点':'尚无配对差值';
  const lines=['本轮检验研究输出是否具有可检验的结构。结果不代表收益表现，也不会自动改变当前生产策略。',
    '实际启动：'+stamp(execution.started_at)+'；预定结束：'+stamp(execution.ends_at)+'（北京时间）。'];
  for(const window of (Array.isArray(execution.windows)?execution.windows:[]).slice(0,2)){
    if(Number.isInteger(window?.index))lines.push('观察窗口 '+window.index+'：'+stamp(window.start)+' 至 '+stamp(window.end)+'（北京时间）。');
  }
  lines.push('配对观察：纳入 '+count(execution.enrolled)+' 对；完成 '+count(execution.complete)+' 对；失败 '+count(execution.failed)+' 对；等待 '+count(execution.pending)+' 对。');
  lines.push('模型调用：'+count(execution.model_calls)+' / '+count(execution.max_model_calls)+' 次；已重试 '+count(execution.retries)+' 次。失败调用也计入预算。');
  lines.push('覆盖 '+count(execution.day_clusters)+' 个日期簇；同日观察可能相关，配对数量不代表独立样本数量。');
  lines.push('可检验结构比例：基线 '+rate(execution.baseline_rate)+'；候选 '+rate(execution.candidate_rate)+'。');
  if(Number.isFinite(execution.paired_delta)&&Math.abs(execution.paired_delta)<=1)lines.push('候选相对基线：'+delta(execution.paired_delta)+'（描述统计）。');
  lines.push('总体引用有效率：基线 '+rate(execution.baseline_citation_rate)+'；候选 '+rate(execution.candidate_citation_rate)+'。');
  for(const result of (Array.isArray(execution.window_results)?execution.window_results:[]).slice(0,2)){
    if(![1,2].includes(result?.window))continue;
    const name=result.window===1?'首轮观察窗 1':'确认窗 2';
    lines.push(name+'结果：纳入 '+count(result.enrolled)+' 对；完成 '+count(result.complete)+' 对；失败 '+count(result.failed)+' 对；等待 '+count(result.pending)+' 对；覆盖 '+count(result.day_clusters)+' 个日期簇。');
    lines.push(name+'可检验结构比例：基线 '+rate(result.baseline_rate)+'；候选 '+rate(result.candidate_rate)+'；差值 '+delta(result.paired_delta)+'（描述统计）。');
    lines.push(name+'引用有效率：基线 '+rate(result.baseline_citation_rate)+'；候选 '+rate(result.candidate_citation_rate)+'。');
  }
  lines.push('分窗评估：'+({PENDING:'尚待观察；两个窗口均完成后再判断。',INSUFFICIENT:'证据不足；不能以总体比例替代确认窗的观察要求。',NOT_SUPPORTED:'不支持候选；总体改善不能抵消确认窗或引用质量未达要求。',STRUCTURE_IMPROVEMENT_ONLY:'仅观察到结构比例提高；不代表预测准确或收益改善。'}[execution.assessment]||'评估待核对。'));
  lines.push('实验结论：'+({COLLECTING:'仍在收集观察，尚不能判断。',DESCRIPTIVE_ONLY:'观察完成，仅形成结构可检验性的描述统计；不能据此认定收益改善。',INSUFFICIENT:'观察不足或实验条件发生变化，尚不能判断。',CANCELLED:'实验已取消，保留已采集的观察和失败记录。',REJECTED:'实验已否决，保留已采集的观察和失败记录。'}[execution.conclusion]||'结论待核对。'));
  if(execution.stop_reason)lines.push('停止原因：'+({WINDOW_COMPLETE:'两个观察窗口已结束',CALL_BUDGET_EXHAUSTED:'模型调用预算已用尽',IMPLEMENTATION_CHANGED:'运行版本或固定条件已变化',PROPOSAL_CLOSED:'对应提案已关闭',USER_CANCELLED:'用户取消实验',USER_REJECTED:'用户否决实验'}[execution.stop_reason]||'需在本机核对停止原因')+'。');
  if(execution.status==='RUNNING')lines.push(execution.pending>0?'当前等待：已纳入的配对完成，并继续收集后续窗口中的观察。':'当前等待：后续研究产生符合观察窗口的输入；暂未产生新配对也会保留等待状态。');
  return lines;
}
function proposalExperimentText(experiment) {
  if(!experiment)return '尚未登记实验设计。';
  const lines=[(experiment.id||'实验编号待补齐')+' · '+proposalExperimentLabel(experiment)];
  if(typeof experiment.primary_metric==='string'&&experiment.primary_metric)lines.push('主要指标：'+({verifiable_prediction_rate:experiment.execution?'可检验结构比例':'可核验预测比例',paired_net_excess_bps:'扣费后配对超额（基点）'}[experiment.primary_metric]||experiment.primary_metric));
  lines.push(...proposalExecutionLines(experiment.execution));
  const enrollment=experiment.enrollment||{},windows=[];
  if(Number.isInteger(enrollment.windows))windows.push(enrollment.windows+' 个观察窗口');
  if(Number.isInteger(enrollment.window_days))windows.push('每个 '+enrollment.window_days+' 个自然日');
  if(Number.isInteger(enrollment.embargo_days))windows.push('窗口间隔离 '+enrollment.embargo_days+' 个自然日');
  if(windows.length)lines.push('设计观察期：'+windows.join('；')+'。');
  if(Number.isInteger(enrollment.minimum_pairs))lines.push('最低观察量：'+enrollment.minimum_pairs+' 对配对观察；达到数量不代表样本相互独立。');
  if(typeof enrollment.start_after==='string'&&Number.isFinite(Date.parse(enrollment.start_after)))lines.push('设计最早可启动时间：'+when(enrollment.start_after)+'（北京时间）；这是设计下限，不代表实际启动时间。');
  if(typeof experiment.baseline_build==='string'&&experiment.baseline_build)lines.push('基线版本：'+experiment.baseline_build);
  return lines.join('\n');
}
function drawProposals(box,data) {
  if(!data){box.append(el('p','提案摘要尚未同步。服务更新并同步后会在这里显示。','empty'));return;}
  if(data.error)box.append(proposalText('p','提案摘要暂未读取成功：'+data.error,'caution'));
  const all=Array.isArray(data.items)?data.items:[],items=all.slice(0,20);
  if(!items.length){box.append(el('p',data.error?'稍后刷新查看提案摘要；暂不能确认是否存在提案。':'当前没有改进提案。复盘或评估形成候选后，会在这里显示证据、检验方案和监督进度。','empty'));return;}
  box.append(el('p','显示 '+items.length+' / '+(Number.isFinite(data.total)?data.total:all.length)+' 份提案 · 可在 ChatGPT 中引用提案编号继续讨论。','subtle'));
  const historical=x=>x.display_bucket==='HISTORY'||['REJECTED','RETIRED','SUPERSEDED'].includes(x.status);
  const current=items.filter(x=>!historical(x)&&!x.observation_only),observations=items.filter(x=>!historical(x)&&x.observation_only),history=items.filter(historical);
  function draw(item,target){
    const card=el('article',null,'proposal-card'),head=el('div',null,'proposal-head');
    const state={DRAFT:'草稿',READY:'待审查与决定',APPROVED:'已批准',ADOPTED:'已采纳',REJECTED:'已驳回',RETIRED:'已退役',SUPERSEDED:'已被新版替代'}[item.status]||'状态待核对';
    head.append(proposalText('h3',proposalExcerpt(item.title||'未命名提案',100)),el('span',state,'badge proposal-status'));
    card.append(head,proposalText('p',(item.id||'编号待补齐')+' · '+when(item.created_at),'subtle proposal-id'));
    if(item.progress_at)card.append(el('p','最近验证证据或状态进展 '+when(item.progress_at),'subtle'));
    if(item.display_reason)card.append(proposalText('p',item.display_reason,'subtle'));
    const overview=el('div',null,'proposal-overview'),evidence=item.evidence||{},review=item.supervision;
    proposalField(overview,'计划改动',proposalExcerpt(item.change,180),'尚未说明具体改动。');
    proposalField(overview,'证据摘要',proposalExcerpt(evidence.text),'尚未整理可用的证据摘要。');
    card.append(overview);
    const states=el('div',null,'proposal-states');
    states.append(el('span','证据：'+({RECORDED:'已记录 · 效果待检验',INSUFFICIENT:'尚不足以判断',MISSING:'待补充',INVALID:'格式待修正'}[evidence.status]||'待核对'),'badge'),el('span','监督：'+proposalReviewLabel(review),'badge'));
    if(item.experiment)states.append(el('span','实验：'+proposalExperimentLabel(item.experiment),'badge'));
    card.append(states);
    if(item.readiness?.status==='INCOMPLETE')card.append(proposalText('p','材料待补齐：'+(item.readiness.reason||'请查看下方检验方案与证据缺口。'),'proposal-warning'));
    const next=typeof item.next_step==='string'&&item.next_step.trim()?item.next_step:item.status==='DRAFT'?'继续整理证据与检验方案，完成后再提交审查。':'核对提案材料与监督状态，策略变更仍需明确批准。';
    card.append(proposalText('p','下一步：'+next,'proposal-next'));
    const detail=details('查看证据、检验方案与监督详情','proposal:'+item.id),body=el('div',null,'proposal-detail');
    proposalField(body,'完整标题',item.title);
    proposalField(body,'具体改动',item.change);
    proposalField(body,'要验证的判断',item.hypothesis);
    const scope=item.applicability||{},scopeParts=[];
    if(scope.route)scopeParts.push('适用路线：'+scope.route);
    if(scope.environment)scopeParts.push('适用环境：'+scope.environment);
    if(scope.build_id)scopeParts.push('程序版本：'+scope.build_id);
    proposalField(body,'适用范围',scopeParts.join('；'),'尚未说明适用范围。');
    proposalField(body,'检验方案',item.test_plan);
    proposalField(body,'失败条件',item.failure_criteria);
    proposalField(body,'回退方式',item.rollback);
    const evidenceDetail=el('section',null,'proposal-field');evidenceDetail.append(el('h4','证据与局限'));
    if(evidence.text)evidenceDetail.append(proposalText('p',evidence.text));
    const limits=(evidence.limitations||[]).filter(v=>typeof v==='string');
    for(const limitation of limits)evidenceDetail.append(proposalText('p',limitation,'proposal-warning'));
    const references=(evidence.references||[]).filter(v=>typeof v==='string');
    if(references.length){const refs=el('ul');for(const reference of references)refs.append(proposalText('li',reference));evidenceDetail.append(refs);}
    if(!references.length&&!limits.length)evidenceDetail.append(el('p','未提供进一步的证据引用或局限说明。','subtle'));
    if(evidence.additional_count>0)evidenceDetail.append(el('p','另有 '+evidence.additional_count+' 条证据记录，可按提案编号在本机核对。','subtle'));
    body.append(evidenceDetail);
    proposalField(body,'其他可能的解释',(item.counter_explanations||[]).filter(v=>typeof v==='string').join('\n'),'尚未记录反例或其他解释，仍需检查。');
    proposalField(body,'前向实验',proposalExperimentText(item.experiment));
    const supervision=el('section',null,'proposal-field');supervision.append(el('h4','独立监督'),el('p',proposalReviewLabel(review)));
    if(review?.id)supervision.append(proposalText('p','审查编号：'+review.id,'subtle'));
    if(review?.summary&&review.current!==false&&review.status!=='STALE')supervision.append(proposalText('p',review.summary));
    supervision.append(el('p','监督建议不等于批准。策略变更仍需 Dean 明确确认，之后才能实施。','subtle'));body.append(supervision);
    detail.append(body);card.append(detail);target.append(card);
  }
  function rows(list,parent){for(const item of list){const d=reviewDetails(proposalExcerpt(item.title||'未命名提案',90)+' · '+(item.id||''),'proposal-entry:'+item.id);draw(item,d);parent.append(d);}}
  current.slice(0,3).forEach(item=>draw(item,box));
  if(current.length>3){const d=details('其他当前提案（'+(current.length-3)+' 份）','proposals:more');rows(current.slice(3),d);box.append(d);}
  if(observations.length){const d=details('待整理的复盘观察（'+observations.length+' 份）','proposals:observations');d.append(el('p','这些是尚未形成完整检验方案的观察；重复叙述不算新的验证证据。','subtle'));rows(observations,d);box.append(d);}
  if(history.length){const d=details('历史提案（本页 '+history.length+' 份）','proposals:history');rows(history,d);box.append(d);}
  const omitted=Math.max(0,(data.total??all.length)-items.length);
  if(omitted)box.append(el('p','另有 '+omitted+' 份未在本页列出。完整提案、历史版本和证据保留在本机，可按提案编号继续核对。','subtle'));
  if(data.selection_notice)box.append(proposalText('p',data.selection_notice,'subtle'));
}
function diagnosticCount(value) {return Number.isInteger(value)&&value>=0?String(value):'待核对';}
function diagnosticBps(value) {return Number.isFinite(value)?(value>0?'+':'')+value.toFixed(1)+' 基点':'尚无可用统计';}
function diagnosticHasSamples(groups) {
  return Object.values(groups&&typeof groups==='object'?groups:{}).some(rows=>rows&&typeof rows==='object'&&
    Object.values(rows).some(stats=>Number.isInteger(stats?.daily?.n)&&stats.daily.n>0));
}
function drawDiagnosticGroups(box,groups) {
  if(!diagnosticHasSamples(groups)){box.append(el('p','尚无到期可评分样本；等待观察期结束并取得所需行情。','empty'));return;}
  const titles={trend_filter:'自选股 · 趋势条件',research_veto:'自选股 · 研究判断',portfolio_allow:'组合 · 增持判断',global_stance:'全球资产 · 方向判断'};
  const labels={trend_ok:'符合趋势条件',trend_fail:'未符合趋势条件',WATCH:'继续观察',NOT_WATCH:'非观察判断',ALLOW:'允许增持',NOT_ALLOW:'未允许增持',LONG:'看多',WAIT:'等待'};
  let shown=0;
  for(const [name,rows] of Object.entries(groups&&typeof groups==='object'?groups:{})){
    if(!rows||typeof rows!=='object'||Array.isArray(rows))continue;
    const group=el('section',null,'diagnostic-group');group.append(proposalText('h4',titles[name]||name));
    for(const [label,stats] of Object.entries(rows)){
      if(!stats||typeof stats!=='object')continue;
      const row=el('div',null,'diagnostic-row'),daily=stats.daily||{},non=stats.non_overlapping||{};
      row.append(proposalText('h5',labels[label]||label));
      row.append(proposalText('p','逐日去重：'+diagnosticCount(daily.n)+' 份观察'+(daily.n>0?'；平均超额 '+diagnosticBps(daily.mean_bps):''),'subtle'));
      row.append(proposalText('p','持有期不重叠：'+diagnosticCount(non.n)+' 份观察；'+diagnosticCount(non.time_clusters??(non.n===0?0:null))+' 个时间簇。'));
      if(non.n>0){
        row.append(proposalText('p','平均超额 '+diagnosticBps(non.mean_bps)+(Number.isFinite(non.hit_rate)&&non.hit_rate>=0&&non.hit_rate<=1?'；超额为正占比 '+(non.hit_rate*100).toFixed(1)+'%':'')));
        const ci=non.ci95_bps,clusters=non.time_clusters;
        if(Number.isInteger(clusters)&&clusters>=30&&clusters<=non.n&&Array.isArray(ci)&&ci.length===2&&ci.every(Number.isFinite)&&ci[0]<=ci[1])row.append(proposalText('p','95% 聚类近似区间：'+diagnosticBps(ci[0])+' 至 '+diagnosticBps(ci[1]),'subtle'));
        else row.append(el('p',Number.isInteger(clusters)&&clusters<30?'不足 30 个时间簇，不展示区间；尚不能判断效果。':'区间尚不能估计；当前仅作描述统计。','diagnostic-caution'));
      }else row.append(el('p',non.n===0?'尚无到期可评分样本。':'样本统计待核对。','subtle'));
      group.append(row);shown++;
    }
    if(group.children.length>1)box.append(group);
  }
  if(!shown)box.append(el('p','尚无到期可评分样本；等待观察期结束并取得所需行情。','empty'));
}
function diagnosticTimeRange(start,end,dateOnly=false) {
  const readable=value=>typeof value==='string'&&Number.isFinite(Date.parse(value))?(dateOnly?value.slice(0,10):when(value)):'未提供';
  return readable(start)+' 至 '+readable(end);
}
function drawDiagnosticCoverage(box,coverage) {
  box.append(el('p','统计范围：全历史累计，非最近 5 日新增样本。','diagnostic-scope'));
  if(!coverage||coverage.scope!=='ALL_HISTORY'){
    box.append(el('p','该摘要尚未提供覆盖日期，等待下一次评估更新；不能据此判断近期效果。','diagnostic-caution'));return;
  }
  box.append(el('p','登记判断：'+diagnosticTimeRange(coverage.registered_from,coverage.registered_through)+'；已评分判断：'+diagnosticTimeRange(coverage.scored_judgment_from,coverage.scored_judgment_through)+'。','subtle'));
  box.append(el('p','已评分价格观察：'+diagnosticTimeRange(coverage.observation_from,coverage.observation_through,true)+'。覆盖日期描述记录跨度，不表示期间每天都有样本。','subtle'));
  if(typeof coverage.last_scored_at==='string'&&Number.isFinite(Date.parse(coverage.last_scored_at)))box.append(el('p','最近新增到期评分：'+when(coverage.last_scored_at)+'（北京时间）。','subtle'));
}
function drawDiagnosticTrack(box,data,auxiliary) {
  const section=el('section',null,'diagnostic-track'+(auxiliary?' diagnostic-auxiliary':''));
  section.append(el('h3',auxiliary?'5 日辅助诊断':'原评分记录'),el('span',auxiliary?'仅供诊断 · 不参与批准':'保留原评分口径','badge'));
  if(!data||typeof data!=='object'){
    section.append(el('p','本项统计尚未同步，暂不能确认样本状态。','empty'));box.append(section);return;
  }
  if(auxiliary&&(data.method!=='decision-time-v2-q5'||data.horizon_days!==5||data.auxiliary_only!==true||data.approval_eligible!==false)){
    section.append(el('p','辅助诊断口径待核对，暂不展示统计结果。','caution'));box.append(section);return;
  }
  section.append(proposalText('p',auxiliary?'在 5 个交易日／观测日线窗口查看短期价格变化；不含交易费用与汇率影响，不代表策略有效。':'股票采用原定 '+diagnosticCount(data.horizon_days)+' 个交易日口径；全球资产保留各自的原观察期限。','diagnostic-scope'));
  drawDiagnosticCoverage(section,data.coverage);
  if(typeof data.notice==='string'&&data.notice)section.append(proposalText('p',data.notice,'diagnostic-notice'));
  const routes={watchlist:'自选股',portfolio:'组合',global:'全球资产',dynamic:'动态研究'},states={OPEN:'待成熟或补齐行情',SCORED:'已评分',UNSCORABLE:'暂不可评分',EXCLUDED:'已排除'};
  const counts=el('div',null,'diagnostic-counts');
  for(const [key,n] of Object.entries(data.counts&&typeof data.counts==='object'?data.counts:{})){
    const [route,state]=key.split(':');counts.append(proposalText('span',(routes[route]||route)+' · '+(states[state]||state||'状态待核对')+' '+diagnosticCount(n),'badge'));
  }
  if(counts.children.length)section.append(counts);
  if(data.sample_sources&&typeof data.sample_sources==='object'){
    const sources=el('div',null,'diagnostic-notice');
    for(const [key,title,unit] of [['scored_rows','评分条数','条'],['daily_samples','每日去重样本','份']]){
      const values=data.sample_sources[key]||{};
      sources.append(proposalText('p',title+'（来源）：判断时冻结 '+diagnosticCount(values.LIVE)+' '+unit+'；历史补登记 '+diagnosticCount(values.LEGACY)+' '+unit+'；来源未核实 '+diagnosticCount(values.UNCLASSIFIED)+' '+unit+'。'));
    }
    sources.append(el('p','判断时冻结表示记录来源，不等于已完成前向实验；每日去重后也不代表样本相互独立。'));section.append(sources);
  }
  if(data.mixed_builds)section.append(el('p','汇总包含多个程序版本，不用于直接判断改动效果。请展开下方版本分组分别查看。','diagnostic-caution'));
  if(diagnosticHasSamples(data.groups)){
    const groupDetails=details('查看分组统计','diagnostics:'+(auxiliary?'quick':'primary')+':groups');
    drawDiagnosticGroups(groupDetails,data.groups);section.append(groupDetails);
  }else drawDiagnosticGroups(section,data.groups);
  const builds=Object.entries(data.by_build&&typeof data.by_build==='object'?data.by_build:{});
  const buildTime=build=>{const time=Date.parse(data.build_times?.[build]?.last_judgment_at);return Number.isFinite(time)?time:0;};
  builds.sort(([left],[right])=>buildTime(right)-buildTime(left));
  if(builds.length){
    const versionDetails=details('按程序版本查看（'+builds.length+' 个）','diagnostics:'+(auxiliary?'quick':'primary'));
    const dated=builds.every(([build])=>Number.isFinite(Date.parse(data.build_times?.[build]?.last_judgment_at)));
    versionDetails.append(el('p',dated?'按已评分判断的最新时间排列；展示最近版本，完整历史保留在本机。':'部分旧摘要缺少版本覆盖时间，当前顺序不能代表版本新旧。','subtle'));
    for(const [build,groups] of builds){
      const version=el('section',null,'diagnostic-build');version.append(proposalText('h4','程序版本：'+build));
      const timing=data.build_times?.[build];
      if(timing)version.append(el('p','已评分判断覆盖：'+diagnosticTimeRange(timing.first_judgment_at,timing.last_judgment_at)+'。','subtle'));
      drawDiagnosticGroups(version,groups);versionDetails.append(version);
    }
    section.append(versionDetails);
  }
  box.append(section);
}
function drawScoreDiagnostics(box,data) {
  box.append(el('p','原评分与 5 日诊断独立展示，样本不能相加。辅助诊断不参与自动批准，也不会改变研究或交易规则。','diagnostic-boundary'));
  if(!data){box.append(el('p','评分诊断尚未同步。服务完成评估并同步后会在这里显示。','empty'));return;}
  if(data.status==='ERROR'){box.append(el('p','评分诊断暂未生成成功；原评分记录保留，等待后续评估与同步。','caution'));return;}
  if(data.status==='NOT_RUN'){box.append(el('p','尚未运行本轮评分诊断，等待下一次评估；暂无辅助诊断结果。','empty'));return;}
  if(data.status!=='READY'||data.version!=='quick-diagnostics-v1'){box.append(el('p','评分诊断格式待核对，暂不展示统计结果。','caution'));return;}
  const generated=typeof data.generated_at==='string'?Date.parse(data.generated_at):NaN;
  if(Number.isFinite(generated)){
    box.append(el('p','最近生成：'+when(data.generated_at)+'（北京时间）','subtle'));
    if(Date.now()-generated>48*3600000)box.append(el('p','统计摘要已超过 48 小时未更新，仅代表当时记录；待下一次评估与同步后再核对近期变化。','diagnostic-caution'));
    else if(generated-Date.now()>5*60000)box.append(el('p','摘要生成时间晚于当前时间，请核对时钟；暂不能确认统计时效。','diagnostic-caution'));
  }else box.append(el('p','摘要生成时间未提供，暂不能确认统计时效。','diagnostic-caution'));
  if(typeof data.notice==='string'&&data.notice)box.append(proposalText('p',data.notice,'diagnostic-notice'));
  const tracks=el('div',null,'diagnostic-tracks');drawDiagnosticTrack(tracks,data.primary,false);drawDiagnosticTrack(tracks,data.quick,true);box.append(tracks);
  if(Number.isInteger(data.omitted_builds)&&data.omitted_builds>0)box.append(el('p','页面另有 '+data.omitted_builds+' 个版本分组未载入；完整记录保留在本机，汇总仍覆盖全历史。','subtle'));
}
function renderActivity(s) {
  if($('supervision'))renderChanged('supervision',s.supervision||{items:[]},box=>drawSupervision(box,s.supervision));
  if($('proposals'))renderChanged('proposals',s.supervision?.proposals??null,box=>drawProposals(box,s.supervision?.proposals));
  if($('score-diagnostics'))renderChanged('score-diagnostics',{data:s.supervision?.diagnostics??null,hour:Math.floor(Date.parse(s.at)/3600000)},box=>drawScoreDiagnostics(box,s.supervision?.diagnostics));
  const market=s.market_phase==='CONTINUOUS';
  const nextSlot=s.next_runs.find(r=>r.kind==='slot');
  $('slot-timing').textContent=(market?'交易时段':'当前休市')+(nextSlot?' · 下次检查 '+shortTime(nextSlot.scheduled_at):'');
  const reviewNext=s.next_runs.find(r=>r.kind==='review');
  $('review-timing').textContent='每日核算盈亏 · 回看前48小时持仓与研究依据'+(reviewNext?' · '+shortTime(reviewNext.scheduled_at)+' 自动更新':'');
  renderChanged('decision-summary',s.decisions.slice(0,1),box=>{
    const d=s.decisions[0];if(!d){box.append(el('p','尚无买卖操作。每只股票的最近检查结果见观察名单，无操作检查不逐次保存。','empty'));return;}
    box.append(el('p',(s.watchlist.find(w=>w.symbol===d.symbol)?.name||d.symbol)+' · '+label(d.action)+' · '+label(d.status)),el('p',decisionReason(d.reason),'subtle'),el('p',shortTime(d.at),'subtle'));
  });
  table('decisions',['时间','股票 / 操作','提交结果','依据'],s.decisions.map(d=>[shortTime(d.at),d.symbol+' '+label(d.action),label(d.status),decisionReason(d.reason)]));
  table('fills',['时间','股票 / 方向','股数 / 均价','费用 / 已实现盈亏'],s.fills.map(f=>[shortTime(f.occurred_at),f.symbol+' '+label(f.side),f.qty+' / '+money(f.price_cents),money(f.fee_cents)+' / '+(f.side==='SELL'?signed(f.realized_cents):'尚未卖出')]));
  renderChanged('reviews',{items:s.reviews,hour:Math.floor(Date.parse(s.at)/3600000)},box=>{
    if(!s.reviews.length){box.append(el('p','还没有复盘记录，首次复盘完成后显示。','empty'));return;}
    s.reviews.slice(0,5).forEach((r,index)=>{const body=el('div',null,'review');drawDailyReview(body,r);
      if(!index)box.append(body);else{const det=details('较早复盘 · '+when(r.window_end),'review:'+r.id);det.append(body);box.append(det);}
    });
    const total=s.reviews[0]?.history?.total??s.reviews.length;
    box.append(el('p','展示最近 '+Math.min(s.reviews.length,5)+' / '+total+' 个复盘周期，每个周期保留最新修订。'+(total>Math.min(s.reviews.length,5)?'其余周期及修订保留在本机完整复盘记录。':''),'subtle'));
  });
}
function researchUpdate(s) {
  // Success times survive newer failures; quotes, heartbeats and page refreshes do not count.
  const rows=s.watchlist||[],times=rows.map(w=>w.last_research_at??(w.plan?.model_status==='SUCCEEDED'?w.plan.activated_at:null)).filter(t=>t&&Number.isFinite(Date.parse(t)));
  return {latest:times.reduce((a,t)=>!a||Date.parse(t)>Date.parse(a)?t:a,null),completed:times.length,total:rows.length};
}
function scheduleSummary(schedule){
  const times=(schedule.slots||[]).map(t=>Number(t.slice(0,2))*60+Number(t.slice(3))).sort((a,b)=>a-b),gaps=times.slice(1).map((t,i)=>t-times[i]);
  const interval=gaps.length?Math.min(...gaps):null;
  return '北京时间 '+schedule.collection.join('、')+' 更新资料与研究；交易时段'+(interval?'每 '+interval+' 分钟':'按配置时间')+
    (schedule.execution_mode==='RULES'?'按有效策略检查买卖':'检查盘面')+'；'+schedule.review+' 复盘。离线期间错过的研究会在后台恢复后补做最近一轮，旧交易不追单。';
}
function render(s) {
  state=s;connected=true;
  const active=s.active_jobs||[],cycle=active.find(j=>['cycle','collect','research'].includes(j.kind));
  $('service').textContent=s.scheduler_enabled?'自动运行中':'自动运行已暂停';$('service').className=s.scheduler_enabled?'':'problem';
  const next=s.next_runs.find(r=>r.kind==='cycle');
  $('next-research').textContent=cycle?'正在更新研究与资料':next?'下次研究 '+shortTime(next.scheduled_at)+' 开始':'';
  $('research-timing').textContent=cycle?'正在更新，完成后自动显示结果':s.watchlist.length+' 只自选股';
  const updates=researchUpdate(s);
  $('last-updated').textContent='上次更新时间：'+(updates.latest?when(updates.latest)+'（研究）':'尚无成功研究');
  $('update-coverage').textContent=updates.completed+' / '+updates.total+' 只已有成功研究；时间取最近完成的一只，各股报价与研究时间见列表。';
  const latestIssue=s.jobs.find(j=>j.status==='FAILED'&&!s.jobs.some(n=>n.kind===j.kind&&n.scheduled_at>j.scheduled_at&&n.status==='DONE'));
  const attention=$('attention');attention.replaceChildren();
  const staleHeartbeat=!s.state.heartbeat||(new Date(s.at)-new Date(s.state.heartbeat)>60000);
  if(staleHeartbeat){$('service').textContent='自动任务状态需要检查';$('service').className='problem';attention.append(document.createTextNode('自动任务暂未响应。现有结果仍可查看，'+(currentUser?.role==='GUEST'?'请联系管理员检查。':'请检查运行详情。')),link('查看状态','/admin/settings#diagnostics'));}
  else if(!s.scheduler_enabled)attention.append(document.createTextNode(currentUser?.role==='GUEST'?'自动运行已暂停，等待管理员恢复。':'自动运行已暂停。需要继续定时更新时，请在“固定关注与自动运行”中开启。'));
  else if(latestIssue)attention.append(document.createTextNode('最近一次'+label(latestIssue.kind)+'未完成，现有结果已保留。'),link('查看任务原因','/admin/settings#diagnostics'));
  if(s.account.risk?.halted)attention.append(el('p','账户回撤达到25%风控阈值：已停止新买入、撤销未成交买单；在市场允许时逐步平仓。恢复买入需重新确认。'));
  const risk=$('portfolio-risk');if(risk){risk.hidden=!s.account.investment_policy;risk.textContent='无杠杆模拟 · 持有周期按天 · 每小时复核'+(s.account.risk?.drawdown_bps!=null?' · 当前回撤 '+(s.account.risk.drawdown_bps/100).toFixed(2)+'%':'')+' / 风控阈值25%';}
  if(s.research_lease){const r=s.research_lease;attention.append(el('p',r.active?'组合研究已同步 · 最近完成 '+when(r.completed_at):'本地组合研究已过期或尚未就绪：暂停新买入，继续持仓风险检查。',r.active?'subtle':'caution'));}
  attention.hidden=!attention.textContent;
  renderWatchlist(s);renderAccount(s);renderActivity(s);renderFollowups(s);renderDynamic(s);renderPortfolioStrategy(s);
  if(!editing)$('watchlist').value=fixedWatchlist(s).map(w=>w.symbol+' '+w.name).join('\n');
  $('settings-summary').textContent=fixedWatchlist(s).length+' 只固定关注股票 · '+(s.scheduler_enabled?'自动运行已开启':'已暂停');
  $('schedule').textContent=scheduleSummary(s.schedule);
  $('toggle').textContent=s.scheduler_enabled?'暂停自动运行':'开启自动运行';
  $('diagnostic-summary').textContent=active.length?active.length+' 项任务处理中':'暂无进行中的任务';
  $('technical-state').textContent='服务版本 '+s.version+' · 最近正常运行 '+when(s.state.heartbeat)+' · '+s.documents+' 份资料'+(s.state.last_error?' · 最近异常：'+readableError(s.state.last_error):'');
  table('jobs',['计划时间','任务','结果'],s.jobs.map(j=>[when(j.scheduled_at),label(j.kind),readableError(j.error)||jobMessage(j)[0]]));
  table('sources',['来源','状态','详情'],[...(s.background_failures||[]).map(f=>[f.label+(f.title?' · '+f.title:''),'待恢复',f.reason+' '+f.impact]),...s.source_checks.map(c=>[traderText(c.source)+(c.symbol?' '+c.symbol:''),label(c.status),readableError(c.detail)])]);
  for(const kind of ['cycle','slot','review']){
    const target={cycle:'research-feedback',slot:'slot-feedback',review:'review-feedback'}[kind];
    if(!watchedJobs.has(target)){
      const running=active.find(j=>kind==='cycle'?['collect','research','cycle'].includes(j.kind):j.kind===kind);
      if(running)feedback(target,...jobMessage(running));else if($(target).classList.contains('pending'))feedback(target,'');
    }
  }
  renderNotice(s);
  updateButtons();
}
// Notices for Dean: only matters that are his to decide, one at a time, oldest first. Guests never see them.
const noticeKinds={DECISION:'需要你决定',VETO:'将自动上线 · 可以否决',INFO:'通知'};
const noticeAuthors={agent:'运维助手',claude:'Claude（历史审查）',program:'程序',reviewer:'外部审查员',chatgpt:'ChatGPT 审查'};
const noticeLater=new Set(),noticeAnswered=new Set();
let noticeShown=null,noticePending=null,noticeBusy=false;
function noticeActions(kind) {
  return ({DECISION:[{action:'APPROVE',label:'批准',confirm:'确认批准',primary:true},{action:'REJECT',label:'不批准',confirm:'确认不批准'}],
    VETO:[{action:'VETO',label:'否决这项改动',confirm:'确认否决',primary:true},{action:'ACK',label:'不否决'}],
    INFO:[{action:'ACK',label:'我知道了',primary:true}]})[kind]||[{action:'ACK',label:'我知道了',primary:true}];
}
function nextNotice(s,later=noticeLater,role=currentUser?.role,showing=noticeShown,answered=noticeAnswered) {
  if(role!=='ADMIN')return null;
  const open=(s?.notices||[]).filter(n=>n.status==='OPEN'&&!later.has(n.id)&&!answered.has(n.id));
  // Keep the notice on screen while it is still open, so a click never lands on a different one.
  return open.find(n=>n.id===showing)||open[0]||null;
}
function drawNoticeActions(n) {
  const box=$('notice-actions');box.replaceChildren();
  for(const a of noticeActions(n.kind)){
    const b=el('button',noticePending===a.action?a.confirm:a.label,a.primary?'primary':'');b.type='button';
    b.addEventListener('click',()=>answerNotice(n,a,b));box.append(b);
  }
  const later=el('button','稍后再看');later.type='button';
  later.addEventListener('click',()=>{noticeLater.add(n.id);noticeShown=null;$('notice-dialog').close();renderNotice(state);});
  box.append(later);
}
function renderNotice(s) {
  const dialog=$('notice-dialog');if(!dialog)return;
  const n=nextNotice(s);
  if(!n){if(dialog.open&&!noticeBusy)dialog.close();noticeShown=null;return;}
  if(noticeShown===n.id&&dialog.open)return;
  noticeShown=n.id;noticePending=null;
  $('notice-kind').textContent=noticeKinds[n.kind]||'通知';
  $('notice-title').textContent=traderText(n.title);
  $('notice-meta').textContent=when(n.created_at)+' · 来自'+(noticeAuthors[n.author]||n.author)+(n.deadline?' · 请在 '+when(n.deadline)+' 前处理':'');
  $('notice-body').textContent=traderText(n.body);
  feedback('notice-feedback','');drawNoticeActions(n);
  if(!dialog.open){if(dialog.showModal)dialog.showModal();else dialog.setAttribute('open','');}
  // Focus the title, not a button: a key pressed while typing elsewhere must not answer an unread notice.
  $('notice-title').focus();
}
async function answerNotice(n,a,button) {
  if(noticeBusy)return;
  if(a.confirm&&noticePending!==a.action){
    noticePending=a.action;drawNoticeActions(n);
    feedback('notice-feedback','再点一次“'+a.confirm+'”确认；点其他按钮可以改主意。','pending');return;
  }
  noticeBusy=true;button.disabled=true;feedback('notice-feedback','正在提交…','pending');
  try{
    await api.post('/api/notices/decide',{id:n.id,action:a.action});
    noticeAnswered.add(n.id);noticeBusy=false;noticeShown=null;$('notice-dialog').close();renderNotice(state);await refresh();
  }catch(error){feedback('notice-feedback',error.message,'error');}
  finally{noticeBusy=false;button.disabled=false;}
}
function refreshProblem(error,stage='request') {
  if(error.kind==='AUTH')return {title:'登录已失效，请重新登录',detail:'后台仍可连接。重新登录后即可继续查看最新结果。',action:'login'};
  if(error.kind==='TIMEOUT')return {title:'后台读取较慢，正在重试',detail:'读取超过 20 秒。页面保留已显示的结果，稍后自动重试；这不表示外网断开。',action:'retry'};
  if(error.kind==='NETWORK')return {title:'暂时连接不上本机后台，正在重试',detail:'本页连接的是这台 Mac 上的工作台。请确认后台已启动；外网正常不代表本机后台可以连接。',action:'retry'};
  if(stage==='render')return {title:'数据已收到，页面显示失败',detail:'请刷新页面加载最新版本；若仍出现，请将这条提示反馈给管理员。',action:'reload'};
  return {title:'后台结果暂未读取成功，正在重试',detail:error.message||'后台返回异常，请稍后重试；持续出现时请检查后台错误日志。',action:'retry'};
}
function showRefreshProblem(error,stage='request') {
  const problem=refreshProblem(error,stage);connected=false;
  $('service').textContent=problem.title;$('service').className='problem';
  const box=$('attention');box.hidden=false;box.replaceChildren(el('span',problem.detail+' '));
  if(problem.action==='login')box.append(pageLink('重新登录','/login?next='+encodeURIComponent(typeof location==='undefined'?'/':location.pathname)));
  else if(problem.action==='reload')box.append(pageLink('刷新页面',typeof location==='undefined'?'/':location.pathname));
  if(!state){$('last-updated').textContent='上次更新时间：暂未读取';$('stocks').textContent=problem.action==='login'?'请重新登录后查看自选股。':'自选股暂未读取成功，恢复后自动显示。';}
  updateButtons();
}
async function refresh() {
  if(refreshing)return;refreshing=true;
  let stage='request';
  try {
    const s=await api.get('/api/status');stage='render';render(s);stage='secondary';
    if(selectedSymbol){try{await refreshStock();}catch(error){if(error.kind==='AUTH')throw error;if(!stockData)$('stock-content').replaceChildren(el('p',error.message,'panel attention'));else{$('attention').hidden=false;$('attention').textContent='个股资料暂未更新，保留上次读取结果。';}}}
    for(const [target,entry] of watchedJobs){
      try {
        const job=s.jobs.find(j=>j.id===entry.id)||s.active_jobs.find(j=>j.id===entry.id)||await api.get('/api/job?id='+encodeURIComponent(entry.id));
        feedback(target,...jobMessage(job));
        if(!['PENDING','RUNNING'].includes(job.status))watchedJobs.delete(target);
      } catch(error){
        if(error.kind==='AUTH')throw error;
        feedback(target,'任务进度暂未读取成功，下次自动重试；请勿重复提交。','error');
      }
    }
  } catch(error) {
    console.error('dashboard_refresh',error);
    showRefreshProblem(error,stage);
  } finally {refreshing=false;}
}
async function runAction(button) {
  const kind=button.dataset.run,target=button.dataset.feedback;
  const key=kind==='repair'?kind+':'+button.dataset.symbol:kind;
  if(submitting.has(key))return;
  submitting.add(key);watchedJobs.delete(target);updateButtons();feedback(target,'正在提交…','pending');
  try {
    const result=await api.post('/api/run',{kind,...(button.dataset.symbol?{symbol:button.dataset.symbol}:{})});watchedJobs.set(target,{id:result.job_id});
    feedback(target,result.reused?'已有相同任务在处理，正在跟踪进度。':'任务已提交，正在等待处理。','pending');await refresh();
  } catch(error){feedback(target,error.message,'error');}
  finally{submitting.delete(key);updateButtons();}
}
let pendingWatchlist=null;
function watchlistDiff(before,after){const old=new Map(before.map(w=>[w.symbol,w.name])),next=new Map(after.map(w=>[w.symbol,w.name]));return [...after.filter(w=>!old.has(w.symbol)).map(w=>'新增：'+w.name+' '+w.symbol),...before.filter(w=>!next.has(w.symbol)).map(w=>'移除：'+w.name+' '+w.symbol),...after.filter(w=>old.has(w.symbol)&&old.get(w.symbol)!==w.name).map(w=>'名称调整：'+old.get(w.symbol)+' → '+w.name+' '+w.symbol)];}
async function saveSettings(button,body,target,success) {
  button.dataset.saving='true';button.disabled=true;feedback(target,'正在保存…','pending');
  try{await api.post('/api/settings',body);if(body.watchlist)editing=false;feedback(target,success);await refresh();}
  catch(error){feedback(target,error.message,'error');}
  finally{delete button.dataset.saving;button.disabled=!connected;}
}
const boards=['watchlist','holdings','activity','dynamic'];
let currentBoard='watchlist';
function watchlistHash(){
  const params=new URLSearchParams({list:researchListFilter,status:researchStatusFilter,market:observationCategory});
  if(watchlistQuery)params.set('q',watchlistQuery);
  return '#watchlist?'+params;
}
function rememberWatchlist(){if(typeof location!=='undefined'&&location.pathname==='/')history.replaceState(history.state,'',watchlistHash());}
function stockPath(symbol){return '/stocks/'+encodeURIComponent(symbol)+'?return='+encodeURIComponent(watchlistHash());}
function parseWorkspaceHash(hash){
  const [raw,query='']=hash.replace(/^#/,'').split('?'),params=new URLSearchParams(query);
  const board=['fill-history','score-diagnostics-section','proposal-section','supervision-section'].includes(raw)?'activity':raw==='portfolio-section'||raw==='watchlist-performance'?'holdings':raw.replace(/-section$/,'');
  return {board:boards.includes(board)?board:'watchlist',list:['CORE','DYNAMIC','FIXED','ALL'].includes(params.get('list'))?params.get('list'):'CORE',status:['ALL',...Object.keys(researchStates)].includes(params.get('status'))?params.get('status'):'ALL',market:observationCategories[params.get('market')]?params.get('market'):'CN',query:(params.get('q')||'').slice(0,80)};
}
function selectBoard(board,{focus=false,remember=false}={}) {
  currentBoard=boards.includes(board)?board:'watchlist';
  for(const name of boards) {
    const selected=name===currentBoard,tab=$(name+'-tab');
    tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;
    $(name+'-section').hidden=!selected;
    if(selected&&focus)tab.focus();
  }
  if(remember)history.replaceState(history.state,'',currentBoard==='watchlist'?watchlistHash():'#'+currentBoard);
}
function initBoardTabs() {
  if(location.pathname!=='/'||!$('watchlist-tab'))return;
  const fromHash=()=>{
    const view=parseWorkspaceHash(location.hash);
    if(view.board==='watchlist'){
      researchListFilter=view.list;researchStatusFilter=view.status;observationCategory=view.market;watchlistQuery=view.query;
      $('watchlist-search').value=watchlistQuery;if(state)renderWatchlist(state);
    }
    selectBoard(view.board);if(location.hash==='#fill-history')$('fill-history').open=true;
  };
  fromHash();window.addEventListener('hashchange',fromHash);
  boards.forEach((board,index)=>{
    const tab=$(board+'-tab');
    tab.addEventListener('click',()=>selectBoard(board,{remember:true}));
    tab.addEventListener('keydown',event=>{
      const next=event.key==='ArrowRight'?(index+1)%boards.length:event.key==='ArrowLeft'?(index+boards.length-1)%boards.length:event.key==='Home'?0:event.key==='End'?boards.length-1:null;
      if(next===null)return;
      event.preventDefault();selectBoard(boards[next],{focus:true,remember:true});
    });
  });
  $('watchlist-search').addEventListener('input',event=>{watchlistQuery=event.target.value;renderWatchlist(state);rememberWatchlist();});
  $('research-status-filter').addEventListener('change',event=>{researchStatusFilter=event.target.value;renderWatchlist(state);rememberWatchlist();});
  $('watchlist-reset').addEventListener('click',()=>resetWatchlist());
}
function resetWatchlist(){
  researchListFilter='CORE';researchStatusFilter='ALL';observationCategory='CN';watchlistQuery='';
  $('watchlist-search').value='';if(state)renderWatchlist(state);rememberWatchlist();$('watchlist-search').focus();
}
async function boot() {
  try{const session=await api.get('/api/session');if(!session.authenticated){location.replace('/login?next='+encodeURIComponent(location.pathname));return;}currentUser=session.user;document.body.dataset.role=currentUser.role;initPages();initBoardTabs();}catch(e){showRefreshProblem(e);if(e.kind!=='AUTH')setTimeout(boot,10000);return;}
  document.querySelectorAll('a[href^="#"]').forEach(a=>a.addEventListener('click',()=>{const target=$(a.getAttribute('href').slice(1));if(target?.tagName==='DETAILS')target.open=true;else if(target?.id==='settings-section')target.querySelector('details').open=true;}));
  document.querySelectorAll('[data-run]').forEach(b=>{b.dataset.idleLabel=b.textContent;b.addEventListener('click',()=>runAction(b));});
  $('watchlist').addEventListener('input',()=>{editing=true;pendingWatchlist=null;$('watchlist-changes').hidden=true;$('save').textContent='检查名单变更';});
  $('notice-dialog')?.addEventListener('cancel',()=>{if(noticeShown)noticeLater.add(noticeShown);noticeShown=null;});
  $('save').addEventListener('click',()=>{
    const watchlist=$('watchlist').value.trim().split('\n').filter(v=>v.trim()).map(v=>{const [symbol,...name]=v.trim().split(/\s+/);return {symbol,name:name.join(' ')||symbol};});
    if(!watchlist.length){feedback('settings-feedback','请至少填写一只自选股。','error');return;}
    const before=fixedWatchlist(state),diff=watchlistDiff(before,watchlist),signature=JSON.stringify([before,watchlist]);
    if(!diff.length){feedback('settings-feedback','固定关注名单没有变化。');return;}
    if(pendingWatchlist!==signature){const box=$('watchlist-changes');box.replaceChildren(el('h4','请核对本次变更'));diff.forEach(line=>box.append(el('p',line)));box.append(el('p','动态发现的公司只有在此处明确新增后，才会成为固定关注。移除公司仍保留持仓管理和历史。','subtle'));box.hidden=false;pendingWatchlist=signature;$('save').textContent='确认并保存以上变更';return;}
    saveSettings($('save'),{watchlist},'settings-feedback','固定关注名单已保存，变更已记录。').then(()=>{pendingWatchlist=null;$('watchlist-changes').hidden=true;$('save').textContent='检查名单变更';});
  });
  $('dynamic-toggle').addEventListener('click',()=>{if(state?.dynamic)saveSettings($('dynamic-toggle'),{dynamic_enabled:!state.dynamic.enabled},'dynamic-feedback',state.dynamic.enabled?'新闻跟踪已暂停，已有相关持仓继续管理。':'已开启新闻跟踪。');});
  $('toggle').addEventListener('click',()=>{if(state)saveSettings($('toggle'),{scheduler_enabled:!state.scheduler_enabled},'schedule-feedback',state.scheduler_enabled?'已暂停新任务，现有任务会继续完成。':'已开启自动运行。');});
  $('search-form').addEventListener('submit',async event=>{
    event.preventDefault();const q=$('query').value.trim();if(!q)return;
    $('search').disabled=true;feedback('search-feedback','正在查找…','pending');
    try{const hits=await api.get('/api/search?q='+encodeURIComponent(q));$('hits').replaceChildren();
      feedback('search-feedback',hits.length?'找到 '+hits.length+' 段相关原文。':'没有找到相关原文，可以换一个关键词。');
      hits.forEach(h=>{const d=el('div',null,'review');d.append(link(h.title,'/api/document?id='+encodeURIComponent(h.doc_id)),el('p',h.text),el('p',h.symbol+' · '+(h.page?'第 '+h.page+' 页':'网页')+' · '+shortTime(h.available_at),'subtle'));$('hits').append(d);});
    }catch(error){feedback('search-feedback',error.message,'error');}finally{$('search').disabled=false;}
  });
  updateButtons();await refresh();if(location.pathname==='/admin/feedback')await loadInbox();setInterval(refresh,10000);
}
document.addEventListener('DOMContentLoaded',boot);

let currentUser=null,stockData=null,historyOffset=0,inboxOffset=0,inboxTotal=0,opinionAttempt=null;
const selectedSymbol=typeof location!=='undefined'?location.pathname.match(/^\/stocks\/((?:sh|sz)\d{6})$/)?.[1]:null;
const inboxLabels={NEW:'待查看',REVIEWING:'处理中',ADOPTED:'已采纳',DISMISSED:'暂不采纳'};
const topicLabels={strategy:'交易策略',data:'研究与数据',experience:'使用体验',other:'其他建议'};
function pageLink(title,path){const a=el('a',title);a.href=path;return a;}
function openOrderSummary(item){
  return (item.open_orders||[]).map(o=>o.status==='UNKNOWN'?'已有委托状态待核对，核对前不重复下单。':
    '已有'+label(o.side)+'委托 '+o.qty+' 股，已成交 '+o.filled_qty+' 股，剩余 '+Math.max(0,o.qty-o.filled_qty)+' 股等待成交；期间不重复下单。').join(' ');
}
function executionSummary(item,nextSlot){
  const d=item.last_decision;
  const pending=openOrderSummary(item);
  const codes=[...new Set([...(d?.reason?.match(/\b[A-Z][A-Z_]{3,}\b/g)||[]),...(d?.buy_blockers||[])])];
  // Execution gates follow the proposal in stored reasons: show them first, before shortening prose.
  const reason=codes.length?codes.map(decisionReason).join('；'):decisionReason(d?.reason);
  const last=pending?(d?'上次检查 '+shortTime(d.at)+'：':'')+pending:
    d?'上次检查 '+shortTime(d.at)+'：'+(d.status==='BLOCKED'?'未新增委托':label(d.action)+' · '+label(d.status))+(reason?'。'+brief(reason):''):'尚无盘面检查记录。';
  return last+(nextSlot?' 下次检查 '+shortTime(nextSlot.scheduled_at)+'。':'');
}
const observationCategories={CN:'A股',US:'美股',COMMODITY:'商品与数字资产'};
const impactStrengths={HIGH:'潜在强影响',MEDIUM:'中等影响',LOW:'有限影响',UNKNOWN:'强度待补充'};
let observationCategory='CN',observationArchiveOffset=0,observationArchiveData=null;
const poolTierLabels={CORE:'原自选',FOCUS:'重点研究',ACTIVE:'持续观察',COOLING:'候补 · 待复核',ARCHIVED:'已归档'};
function drawPoolStatus(parent,target){
  if(!target.pool_tier)return;
  const line=el('p',null,'observation-pool-state');line.append(el('span',target.fixed?'固定观察':poolTierLabels[target.pool_tier]||target.pool_tier,'badge '+(target.pool_tier==='FOCUS'?'focus-badge':'')));
  if(target.protected)line.append(el('span','持仓 / 委托保护','badge'));
  parent.append(line,el('p',target.pool_reason,'subtle'));
  if(target.review_due_at)parent.append(el('p','下次复核期限 '+shortTime(target.review_due_at),'subtle'));
}
function drawObservationCard(parent,target){
  const card=el('article',null,'observation-card');card.id='observed-'+target.asset;card.tabIndex=-1;
  const heading=el('div',null,'section-head');heading.append(el('h3',target.name),el('span',target.fixed?'现货模拟':target.status==='NEEDS_REVIEW'?'待复核':impactStrengths[target.strength]||impactStrengths.UNKNOWN,'badge'));
  card.append(heading,el('p',(target.symbol||target.asset)+' · '+(observationCategories[target.category]||'全球宏观背景')+' · '+macroDirections[target.direction],'subtle'));drawPoolStatus(card,target);
  if(target.indicator)card.append(el('p','指标日期 '+target.indicator.date+(target.indicator_status==='FAILED'?' · 最近更新未成功':''),'subtle'));
  card.append(el('p','加入观察 '+shortTime(target.added_at)+' · 最近研究 '+shortTime(target.trade_plan?.created_at||target.updated_at),'subtle'));
  drawObservationCauses(card,target);parent.append(card);
}
function renderObservationArchive(data){
  if(!data)return;
  $('observation-archive').hidden=!data.archive_count;
  $('observation-archive-title').textContent='候补与归档（'+(data.archive_count||0)+'）';
  renderChanged('observation-archive-items',[data.archived_items,data.archive_offset],box=>(data.archived_items||[]).forEach(t=>drawObservationCard(box,t)));
  const offset=data.archive_offset||0,total=data.archive_count||0;
  $('observation-archive-page').textContent='第 '+(Math.floor(offset/25)+1)+' / '+Math.max(1,Math.ceil(total/25))+' 页';
  $('observation-archive-prev').disabled=offset===0;$('observation-archive-next').disabled=offset+25>=total;
  $('observation-archive-prev').onclick=()=>loadObservationArchive(Math.max(0,offset-25));
  $('observation-archive-next').onclick=()=>loadObservationArchive(offset+25);
}
async function loadObservationArchive(offset=0,asset=null){
  try{const data=await api.get('/api/observations?offset='+offset+(asset?'&asset='+encodeURIComponent(asset):''));
    observationArchiveOffset=data.archive_offset||0;observationArchiveData=data;renderObservationArchive(data);$('observation-archive').open=true;$('observation-archive-feedback').hidden=true;return data;
  }catch(e){feedback('observation-archive-feedback',e.message,'error');return null;}
}

async function showObservation(asset,category){
  researchListFilter='ALL';researchStatusFilter='ALL';watchlistQuery='';if($('watchlist-search'))$('watchlist-search').value='';observationCategory=observationCategories[category]?category:'CN';selectBoard('watchlist',{remember:true});if(state)renderWatchlist(state);
  const member=state?.observation?.membership?.[asset];
  if(member&&['COOLING','ARCHIVED'].includes(member.tier)){if(!await loadObservationArchive(0,asset))return;}
  const target=$('observed-'+asset);if(target){target.scrollIntoView({block:'center',behavior:'smooth'});target.focus({preventScroll:true});}
}
function showMacroEvent(id){
  selectBoard('dynamic',{remember:true});
  const g=state?.dynamic?.global;
  if(g){
    const groups={current:(g.items||[]).filter(macroProminent),followup:g.followup_items||[],history:g.history_items||[...(g.items||[]).filter(e=>!macroProminent(e)),...(g.archived_items||[])]};
    for(const [kind,events] of Object.entries(groups)){
      const index=events.findIndex(e=>e.id===id);if(index<0)continue;
      macroPages[kind]=Math.floor(index/5);renderingCache.delete({current:'macro-cases',followup:'macro-followup',history:'macro-archive'}[kind]);
      renderGlobalMacro(state.dynamic);break;
    }
  }
  const target=$('macro-event-'+id);
  if(target){const history=target.closest('#macro-history');if(history)history.open=true;target.scrollIntoView({block:'start',behavior:'smooth'});target.focus({preventScroll:true});}
}
function drawLogicChain(parent,impact,key){
  if(!impact.logic_chain?.length){parent.append(el('p',impact.mechanism,'subtle'),el('p','早期研究，逐步逻辑链待补充。','subtle'));return;}
  const chain=el('ol',null,'logic-chain');chain.setAttribute('aria-label','影响逻辑链');
  impact.logic_chain.forEach((step,index)=>{
    const row=el('li'),badge=el('span',step.kind==='FACT'?'新闻事实':'推断','chain-kind '+(step.kind==='FACT'?'fact':'inference'));
    row.append(badge,el('span',step.statement));
    if(step.kind==='FACT'&&step.quote){const citation=details('新闻依据',key+':step:'+index);citation.append(el('blockquote',step.quote));row.append(citation);}
    chain.append(row);
  });parent.append(chain);
  if(impact.strength_basis)parent.append(el('p','影响依据：'+impact.strength_basis,'impact-condition'));
  if(impact.conditions)parent.append(el('p','成立条件：'+impact.conditions,'impact-condition'));
  if(impact.invalidation)parent.append(el('p','失效条件：'+impact.invalidation,'impact-condition'));
}
function drawImpactAssessment(parent,value,key,options={}){
  const v=value||{state:'PENDING',admitted:false,reason:'等待独立影响评估，暂不占用活跃名额'};
  if(v.method_gate){parent.append(el('p',v.admitted?'产业研究初步证据通过；买入前仍需核对公司、价格与资金条件':'产业研究仍待补充证据或复核','subtle'));return;}
  const block=el('section',null,'impact-assessment');block.setAttribute('aria-label','独立影响评估');
  const labels={PENDING:'待评估',BACKGROUND:'背景资料',NEEDS_EVIDENCE:'待补证据',HISTORICALLY_WEAK:'历史反应偏弱',ADMITTED:'通过影响评估'};
  block.append(el('p',(options.historical?'当时评估：':'')+(labels[v.state]||'待评估'),'impact-assessment-title'),el('p',v.reason,'subtle'));
  const a=v.assessment,h=v.history;
  if(a){
    if(a.direction)block.append(el('p',(options.historical?'当时独立评估方向：':'独立评估方向：')+(macroDirections[a.direction]||'方向待确认')));
    block.append(el('p',a.basis));
    if(a.impact_basis&&a.impact_basis!=='ECONOMIC'){
      const x=a.expectation_test;
      block.append(el('p','预期变化：'+x.probability_logic),el('p','反证与复核：'+x.counter_evidence,'subtle'));
      if(x.market_confirmation==='NOT_PROVIDED')block.append(el('p','尚无已核实的盘中市场反应；当前日线样本用于后续比较。','subtle'));
    }
    const more=details('影响规模与历史依据',key+':materiality');
    const magnitudes={HIGH:'较大',MEDIUM:'中等',LOW:'有限',UNKNOWN:'尚不能确定'},novelty={INCREMENTAL:'有新增冲击证据',IMPLEMENTATION:'既有安排执行',REPEAT:'重复信息',UNKNOWN:'增量待核实'};
    more.append(el('p','产业影响：'+magnitudes[a.magnitude]+' · '+novelty[a.novelty]),el('p','规模依据：'+a.scale.explanation),el('p','传导核验：'+a.transmission));
    if(a.scale.kind==='NUMERIC')more.append(el('p','可比规模：'+a.scale.numerator.value+' / '+a.scale.denominator.value+' '+a.scale.numerator.unit+'（'+a.scale.numerator.period+'），约 '+(a.scale.numerator.value/a.scale.denominator.value*100).toFixed(2)+'%。'));
    const historical={INSUFFICIENT:'样本不足，尚未验证',SUPPORTED:'后续样本支持较强市场反应',WEAK:'后续样本中的明显反应偏少',INCONCLUSIVE:'历史结果未形成明确支持'};
    if(h){more.append(el('p','同类独立前向样本 '+h.sample_count+' / '+h.required_count+' · '+historical[h.state]));
      if(h.sample_count>=h.required_count&&h.median_absolute_change!==null)more.append(el('p','同类样本变动幅度中位数 '+h.median_absolute_change.toFixed(2)+h.change_unit+'；日级相关反应，不等于产业因果或可交易收益。','subtle'));
    }
    if(v.measurement_basis==='LATE_REVIEW')more.append(el('p','本条在事件后补充评估，仅供回看，不计入前向校准样本。','subtle'));
    if(v.measurement_proxy)more.append(el('p','历史行情口径：'+v.measurement_proxy,'subtle'));
    if(v.measurement_gap)more.append(el('p','数据缺口：'+v.measurement_gap,'subtle'));
    more.append(el('p','改变判断所需证据：'+a.missing_evidence));
    (v.sources||[]).forEach(source=>more.append(link(source.title,source.url)));
    (a.citations||[]).forEach(c=>more.append(el('blockquote',c.quote)));
    block.append(more);
  }
  parent.append(block);
}
function drawObservationCauses(parent,target){
  const causes=details('跟踪依据 · '+target.links.length+' 条研究','observed:'+target.asset);
  if(target.links.length&&target.status==='NEEDS_REVIEW')causes.append(el('p','相关原文或结论已修订，原影响判断需要复核。','caution'));
  if(target.conflicting)causes.append(el('p','不同事件给出相反方向，请分别查看成立条件。','caution'));
  for(const cause of target.links){
    const article=el('section',null,'observation-cause');article.append(el('h4',cause.headline),el('p',shortTime(cause.published_at)+' · '+(cause.status==='INVALIDATED'?'原判断已失效':macroDirections[cause.impact.direction]),'subtle'));
    if(cause.review_state==='DUE'||cause.review_state==='EXPIRED')article.append(el('p',cause.review_state==='DUE'?'此事件已到复核期限，等待新证据。':'此事件的观察期限已结束。','caution'));
    drawImpactAssessment(article,cause.materiality,'observed:'+target.asset+':'+cause.event_id);
    drawLogicChain(article,cause.impact,'observed:'+target.asset+':'+cause.event_id);
    article.append(link('查看新闻原文',cause.url));
    if([...(state?.dynamic?.global?.items||[]),...(state?.dynamic?.global?.followup_items||[]),...(state?.dynamic?.global?.history_items||[]),...(state?.dynamic?.global?.archived_items||[])].some(e=>e.id===cause.event_id)){
      const jump=el('button','查看动态','text-button');jump.type='button';jump.addEventListener('click',()=>showMacroEvent(cause.event_id));article.append(jump);
    }
    causes.append(article);
  }parent.append(causes);
}
function indicatorText(target){return target.indicator?target.indicator.value.toLocaleString('zh-CN',{maximumFractionDigits:4})+' '+target.unit:'行情暂未接入';}
function watchlistTable(){
  const table=el('table',null,'watchlist-table'),head=el('tr'),heading=el('thead');
  ['资产 / 持仓','当前持仓','当前价 / 成本价','买入参考区间','卖出 / 止损参考','当前建议'].forEach((title,index)=>{
    const th=el('th',title);th.scope='col';if(index===1){th.setAttribute('aria-sort','descending');th.append(el('span',' ↓','sort-indicator'));}head.append(th);
  });heading.append(head);table.append(heading);return table;
}
const nativePrice=(micros,digits=2)=>Number.isFinite(micros)?(micros/1e6).toLocaleString('zh-CN',{minimumFractionDigits:digits,maximumFractionDigits:digits}):'—';
function drawGlobalWatchlistRow(table,target,s){
  const h=target.position,q=target.spot_quote,p=target.trade_plan,pp=p?.payload,l=pp?.levels;
  const unit=target.unit||(target.category==='US'?'美元/股':'美元/单位'),quantityUnit=unit.includes('/')?unit.split('/')[1]:'单位';
  const expired=p&&((p.status&&p.status!=='ACTIVE')||Date.parse(p.valid_until)<=Date.parse(s.at));
  const row=el('tr',null,'watchlist-values'),name=el('td'),holding=el('td',null,'holding-value'),quote=el('td'),buy=el('td'),sell=el('td'),decision=el('td');
  name.append(el('strong',target.name),el('p',target.symbol||target.asset,'subtle'),el('p',h?(h.qty/(h.qty_scale||1)).toLocaleString('zh-CN',{maximumFractionDigits:8})+' '+quantityUnit:'未持仓','subtle'));
  if(target.fixed||target.pool_tier)name.append(el('span',target.fixed?'固定观察':poolTierLabels[target.pool_tier]||'动态发现','badge'));
  holding.append(el('strong',money(h?.market_value_cents??0)+' 元'));
  if(h){
    const pnl=h.market_value_cents-h.cost_cents;
    holding.append(el('p',(pnl>0?'浮盈 ':pnl<0?'浮亏 ':'浮动盈亏 ')+signed(pnl)+' 元','holding-pnl '+(pnl>0?'profit':pnl<0?'loss':'')));
    if(!q)holding.append(el('p','暂无报价 · 按成本估算','subtle'));
    else if(h.valuation_complete===false)holding.append(el('p','估值待更新','caution'));
  }
  const current=el('p',null,'position-price'),cost=el('p',null,'position-cost');
  current.append(el('span','现价 ','subtle'),el('strong',nativePrice(q?.price_micros)));
  cost.append(el('span','成本 ','subtle'),el('strong',nativePrice(h?.average_native_cost_micros,3)));
  cost.title='当前剩余持仓的平均买入成本，含买入费用；按成交时汇率还原原币成本。';
  quote.append(current,cost,el('p',unit,'subtle'));
  if(h?.average_native_cost_micros>0&&Number.isFinite(q?.price_micros)){
    const change=(q.price_micros/h.average_native_cost_micros-1)*100;
    quote.append(el('p','较成本 '+(change>0?'+':'')+percent(change)+'（原币）','cost-return '+(change>0?'profit':change<0?'loss':'')));
  }
  quote.append(el('p',q?'最近报价 · '+shortTime(q.observed_at):'报价待获取','subtle'));
  buy.append(el('strong',l?nativePrice(l.buy_low_micros)+' – '+nativePrice(l.buy_high_micros):'暂未形成'));
  sell.append(el('p',l?nativePrice(l.sell_micros)+' / '+nativePrice(l.stop_micros):'暂未形成'));
  if(l){buy.append(el('p',unit,'subtle'));sell.append(el('p',unit,'subtle'));}
  if(expired)buy.append(el('p','研究已过期 · 仅供回看','caution'));
  const blockers=target.recheck?.payload?.blockers?.length?target.recheck.payload.blockers:pp?.blockers||[];
  const title=target.entry_allowed===false?target.entry_reason||'暂停新增买入':expired?'研究已过期，等待更新':!p?'等待研究形成交易计划':blockers.length?'等待：'+blockers.slice(0,2).join('；'):pp.kind==='PAPER_TRADE'?(h?'按策略持续检查':'等待价格与资金条件'):'当前研究暂不买入';
  decision.append(el('strong',title,'decision-status caution'),el('p',p?'研究 '+shortTime(p.created_at):'等待首次研究','subtle'),el('p','组合买卖安排更新：'+(target.last_strategy_updated_at?shortTime(target.last_strategy_updated_at):'尚未发布'),'subtle'));
  [name,holding,quote,buy,sell,decision].forEach((cell,i)=>cell.dataset.label=['股票 / 资产','当前持仓','当前价 / 成本价','买入参考区间','卖出 / 止损参考','当前状态'][i]);row.append(name,holding,quote,buy,sell,decision);
  const summaryRow=el('tr',null,'watchlist-summary-row'),cell=el('td'),research=details('研究逻辑与动态影响','watchlist-research:'+target.asset);cell.colSpan=6;
  if(pp){research.append(el('p',pp.thesis,'watchlist-status-summary'),el('p','持有参考 '+pp.holding_days+' 天 · 每小时复核 · 研究有效至 '+shortTime(p.valid_until),'subtle'));}
  else research.append(el('p','等待本轮研究形成交易计划。','subtle'));
  if(target.recheck)research.append(el('p','最近复核 '+shortTime(target.recheck.checked_at)+(blockers.length?' · '+blockers.join('；'):''),'subtle'));
  if(q)research.append(el('p','美元兑人民币 '+(q.fx_micros/1e6).toFixed(4)+' · '+shortTime(q.fx_at),'subtle'));
  if(target.indicator)research.append(el('p','历史指标 '+indicatorText(target)+' · '+target.indicator.date,'subtle'));
  drawPoolStatus(research,target);drawObservationCauses(research,target);
  const industry=s.industry;if(industry?.members?.some(m=>m.symbol===target.asset)){const d=details('公司产业研究与完整历史','global-industry:'+target.asset);drawIndustry(d,{...industry,members:industry.members.filter(m=>m.symbol===target.asset),hypotheses:(industry.hypotheses||[]).filter(h=>h.symbol===target.asset),forecasts:(industry.forecasts||[]).filter(f=>f.symbol===target.asset),history:(industry.history||[]).filter(h=>h.symbol===target.asset)});research.append(d);}
  cell.append(research);summaryRow.append(cell);
  const group=el('tbody');group.id='observed-'+target.asset;group.tabIndex=-1;group.setAttribute('aria-label',target.name);group.append(row,summaryRow);table.append(group);
}
let researchListFilter='CORE',researchStatusFilter='ALL',watchlistQuery='';
const researchLists={CORE:'固定关注',DYNAMIC:'动态发现',FIXED:'固定资产',ALL:'全部标的'};
const researchStates={TRACKING:'活跃跟踪',LEAD:'研究线索',REVIEW:'待复核',ARCHIVED:'已归档'};
const hypothesisStates={ACTIVE:'依据有效',WAITING:'待补证据',REVIEW:'待复核',ARCHIVED:'已归档',INVALIDATED:'判断已失效',REALIZED:'已兑现'};
const industryMethods={'1':'供应链需求传导','2':'供应瓶颈与盈利机会','3':'投资建设的先后顺序','8':'政策到实际采购'};
const industryTerms={ORDERS:'订单',DEMAND:'需求',CAPACITY:'产能',OUTPUT:'产量',LEAD_TIME:'交货周期',INVENTORY:'库存',SUPPLY_CONSTRAINT:'供应约束',ALTERNATIVE_SUPPLY:'替代供应',PROFIT_CAPTURE:'公司取得利润的依据',DEMAND_EXCEEDS_SUPPLY:'需求超过可供数量',CAPACITY_FULL:'产能已满',LEAD_TIME_RISING:'交货周期持续延长',INVENTORY_DEPLETING:'库存持续下降',BUDGET:'预算',FUNDING:'资金落实',TENDER:'招标',AWARD:'中标',CONTRACT:'合同',DELIVERY:'交付',ACCEPTANCE:'验收',PAYMENT:'回款',CANCELLED:'项目取消',BUSINESS:'相关业务',INCREMENTAL_UNITS:'新增设备数量',CONTENT_PER_UNIT:'单台设备用量',UNIT_PRICE:'产品单价',SUPPLIER_SHARE:'供货份额',incremental_units:'新增设备数量',content_per_unit:'单台设备用量',unit_price:'产品单价',supplier_share:'供货份额',UNKNOWN:'未披露'};
const industryTerm=value=>industryTerms[value]||(/^[A-Z][A-Z_]{3,}$/.test(value||'')?'其他经营指标':value||'未披露');
function fixedWatchlist(s){return s.fixed_watchlist||s.watchlist.filter(w=>!w.membership||w.membership==='CORE');}
function researchRows(s){
  const rows=new Map(),members=new Map((s.industry?.members||[]).map(m=>[m.symbol,m]));
  for(const t of s.observation?.items||[])rows.set(t.asset,{...t,discovery:t,symbol:t.asset,membership:t.fixed?'FIXED':'DYNAMIC',research_status:['COOLING','ARCHIVED'].includes(t.pool_tier)?t.pool_tier==='COOLING'?'REVIEW':'ARCHIVED':'TRACKING'});
  for(const w of s.watchlist||[])rows.set(w.symbol,{...rows.get(w.symbol),...w,category:'CN',membership:w.membership||'CORE',research_status:'TRACKING',discovery:undefined});
  for(const m of members.values())rows.set(m.symbol,{...rows.get(m.symbol),...m,category:m.category||(m.symbol.startsWith('US:')?'US':'CN'),research_status:m.research_status||(m.buy_eligible?'TRACKING':m.tier==='ARCHIVED'?'ARCHIVED':'REVIEW')});
  return [...rows.values()];
}
function filteredResearchRows(s,list=researchListFilter,status=researchStatusFilter){
  return researchRows(s).filter(r=>(!s.industry?.enabled||list==='ALL'||r.membership===list)&&(!s.industry?.enabled||status==='ALL'||r.research_status===status));
}
function matchesWatchlistQuery(row,query=watchlistQuery){
  const text=[row.name,row.symbol,row.asset].filter(Boolean).join(' ').toLocaleLowerCase();
  return query.trim().toLocaleLowerCase().split(/\s+/).every(term=>text.includes(term));
}
function drawWatchlistEmpty(box){
  const empty=el('div',null,'watchlist-empty');empty.append(el('strong',watchlistQuery?'没有找到匹配的股票或资产':'当前筛选下暂无标的'),el('p','可以调整名单、市场或研究状态，也可以回到固定关注查看全部股票。','subtle'));
  const reset=el('button','返回固定关注');reset.type='button';reset.addEventListener('click',resetWatchlist);empty.append(reset);box.append(empty);
}
function drawIndustryEvidence(box,p,key){
  const evidence=details('查看原文与数据口径',key+':evidence');
  for(const f of p.facts||[]){
    const row=el('div',null,'industry-evidence');
    row.append(el('p',[f.entity,f.product,industryTerm(f.metric),industryTerm(f.value)+(f.unit?' '+f.unit:''),industryTerm(f.period)].filter(Boolean).join(' · ')));
    if(f.project||f.counterparty)row.append(el('p',[f.project&&'项目：'+f.project,f.lot&&'标段：'+f.lot,f.counterparty&&'交易对方：'+f.counterparty].filter(Boolean).join(' · '),'subtle'));
    row.append(el('blockquote',f.quote),el('p','公开时间 '+shortTime(f.published_at)+' · 取得时间 '+shortTime(f.acquired_at||f.ready_at)+' · 纳入研究 '+shortTime(f.ready_at)+' · '+({DISCLOSED:'原文披露',GUIDANCE:'公司指引，尚未兑现',ESTIMATE:'研究估计，尚未证实'}[f.claim_type]||'待核实'),'subtle'));
    if(f.effective_from||f.effective_until)row.append(el('p','业务适用时间：'+(f.effective_from?shortTime(f.effective_from):'未说明起点')+' 至 '+(f.effective_until?shortTime(f.effective_until):'未说明终点'),'subtle'));
    if(/^https:\/\//.test(f.url)){const a=el('a','查看公开原文');a.href=f.url;a.target='_blank';a.rel='noopener noreferrer';row.append(a);}evidence.append(row);
  }box.append(evidence);
}
function drawIndustryThesis(box,p,key){
  box.append(el('p',p.thesis),el('p','影响路径：'+(p.causal_chain||[]).join(' → ')),el('p','下一步核实：'+(p.next_check||'等待补充研究')),el('p','推翻判断的条件：'+(p.invalidation||'等待补充')));
  if(p.alternatives)box.append(el('p','替代供应：'+p.alternatives));if(p.profit_capture)box.append(el('p','公司能否取得利润：'+p.profit_capture));
  if(p.revenue_scenario){const r=p.revenue_scenario;box.append(el('p',r.status==='SCENARIO'?'满足假定条件时的新增收入范围：'+r.low+'–'+r.high+' '+r.unit+'；尚非已确认收入。':'新增收入尚不能估算，还缺：'+(r.missing||[]).map(industryTerm).join('、'),'subtle'));}
  addList(box,'相反证据与风险',p.counterpoints);addList(box,'还缺什么证据',p.missing);drawIndustryEvidence(box,p,key);
}
function drawIndustry(box,data){
  const members=new Map((data.members||[]).map(m=>[m.symbol,m])),rows=data.hypotheses||[];
  if(!rows.length)box.append(el('p','这家公司尚无已核实的产业研究依据。取得资料后会展示判断、原文及待核实事项。','subtle'));
  for(const h of rows){
    const p=h.payload,m=members.get(h.symbol),d=details((p.name||m?.name||h.symbol)+' · '+industryMethods[h.method]+' · '+(hypothesisStates[h.effective_state||h.state]||'待核实'),'industry:'+h.id);
    d.append(el('p',(data.domains?.[h.domain]?.name||'相关行业')+' · '+(m?.membership==='CORE'?'固定关注':'动态发现')+' · '+(m?.entry_reason||(m?.buy_eligible?'仍需公司研究、组合安排与价格条件全部通过':'暂停新增买入；继续管理已有持仓和委托')),'subtle'));
    d.append(el('p','研究更新 '+shortTime(h.created_at)+' · 复核期限 '+shortTime(h.review_at),'subtle'));
    drawIndustryThesis(d,p,'industry:'+h.id);
    if(h.versions?.length){const versions=details('历次研究与当时依据（'+h.versions.length+' 版）','industry-versions:'+h.id);for(const [i,v] of h.versions.entries()){const item=details(shortTime(v.at)+' · '+(hypothesisStates[v.state]||'待核实'),'industry-version:'+v.id);if(v.payload)drawIndustryThesis(item,v.payload,'version:'+v.id);else item.append(el('p',v.thesis));item.append(el('p',v.payload?.rule_version==='industry_v2'?'研究规则：第二版（需核对供需口径、替代供应及利润依据）':'研究规则：第一版','subtle'));versions.append(item);}d.append(versions);}
    box.append(d);
  }
  if(data.forecasts?.length){const section=details('经营预测兑现情况','industry-forecasts');for(const f of data.forecasts){const p=f.payload,d=details(industryTerm(p.metric)+' · '+p.period,'forecast:'+f.id);d.append(el('p','预测前基准 '+p.baseline+' '+p.unit+' · 预计 '+p.low+'–'+p.high+' '+p.unit+' · 核对日期 '+shortTime(f.due_at)),el('p',({UNKNOWN:'尚未取得可比披露，暂不能判断',CONFLICT:'不同披露存在冲突，等待核实',IN_RANGE:'实际披露落在预测范围内',OUTSIDE_RANGE:'实际披露超出预测范围'}[f.outcome?.status]||'尚未到核对日期')));for(const o of f.outcome?.payload?.observations||[]){d.append(el('p','已披露 '+o.value+' '+p.unit),el('blockquote',o.quote));if(/^https:\/\//.test(o.url))d.append(link('核对实际披露',o.url));}section.append(d);}box.append(section);}
  if(data.history?.length){const history=details('加入、复核与退出记录','industry-history');for(const m of data.history){history.append(el('p',shortTime(m.at)+' · '+(m.name||m.symbol)+' · '+(m.membership==='CORE'?'固定关注':'动态发现')+' · '+(researchStates[m.research_status]||poolTierLabels[m.tier]||'待核实')),el('p',m.reason||'早期记录未保存具体原因','subtle'),el('p',(m.buy_eligible?'当时已满足名单条件':'当时暂停新增买入')+(m.protected?'；继续管理已有持仓和委托':'')+' · '+(m.rule_version==='industry_v2'?'按第二版研究规则':'按第一版研究规则'),'subtle'));}box.append(history);}
}
function renderResearchLists(s){
  const data=s.industry,box=$('research-list-tabs');if(!box)return;
  box.hidden=!data?.enabled;$('industry-research-panel').hidden=!data?.enabled;
  if($('research-status-tabs'))$('research-status-tabs').hidden=!data?.enabled;
  if(!data?.enabled)return;
  renderChanged('research-list-tabs',[researchRows(s),researchListFilter],target=>{
    for(const [key,title] of Object.entries(researchLists)){
      const count=filteredResearchRows(s,key,'ALL').length,b=el('button',null,'list-choice');b.type='button';b.id='list-'+key;b.setAttribute('aria-pressed',String(researchListFilter===key));
      b.append(el('span',title),el('span',String(count),'list-count'));
      b.addEventListener('click',()=>{
        researchListFilter=key;researchStatusFilter='ALL';watchlistQuery='';$('watchlist-search').value='';
        const rows=filteredResearchRows(state||s,key,'ALL');
        observationCategory=rows.some(r=>r.category==='CN')?'CN':rows[0]?.category||'CN';
        renderWatchlist(state||s);rememberWatchlist();$('list-'+key).focus();
      });target.append(b);
    }
  });
  renderChanged('research-status-filter',[researchRows(s),researchListFilter,observationCategory,watchlistQuery],target=>{
    for(const [key,title] of Object.entries({ALL:'全部状态',...researchStates})){
      const count=filteredResearchRows(s,researchListFilter,key).filter(r=>r.category===observationCategory&&matchesWatchlistQuery(r)).length;
      const option=el('option',title+' · '+count);option.value=key;target.append(option);
    }
  });
  $('research-status-filter').value=researchStatusFilter;
  $('industry-summary').textContent='跟踪领域：'+Object.values(data.domains||{}).map(d=>d.name).join('、')+'。先核对需求、供给、预算和项目进度，再判断哪些公司值得研究。入选名单不等于可以买入。';
  renderChanged('industry-hypotheses',[data.checks,data.coverage],target=>{
    for(const c of data.checks||[]){const d=details((data.domains?.[c.step]?.name||'相关行业')+' · '+(c.status==='DONE'?'本轮已检查':'尚待完成'),'industry-check:'+c.step);for(const [method,reason] of Object.entries(c.payload?.methods||{}))d.append(el('p',industryMethods[method]+'：'+reason));target.append(d);}
    const coverage=details('资料取得情况与缺口','industry-coverage');for(const c of data.coverage||[])coverage.append(el('p',(data.domains?.[c.domain]?.name||'相关行业')+' · '+({cninfo_industry:'巨潮资讯公司公告',ccgp:'中国政府采购网'}[c.source]||'公开资料')+' · '+(c.status==='FAILED'?'本次未取得资料':'资料仍在补齐')+'：'+readableError(c.detail),'subtle'));target.append(coverage);
  });
}
function renderWatchlist(s){
  if(!s)return;
  renderResearchLists(s);
  if(!observationCategories[observationCategory])observationCategory='CN';
  const observed=s.observation?.items||[],manual=new Set(s.watchlist.map(w=>w.symbol)),visible=filteredResearchRows(s).filter(r=>matchesWatchlistQuery(r));
  renderChanged('observation-categories',[visible,observationCategory,researchListFilter,researchStatusFilter],box=>{
    Object.entries(observationCategories).forEach(([category,title],index)=>{
      const count=visible.filter(t=>t.category===category).length;
      const b=el('button',title+' '+count);b.type='button';b.id='category-'+category;b.setAttribute('role','tab');b.setAttribute('aria-selected',String(category===observationCategory));b.setAttribute('aria-controls','stocks');b.tabIndex=category===observationCategory?0:-1;
      b.addEventListener('click',()=>{observationCategory=category;renderWatchlist(state||s);rememberWatchlist();$('category-'+category).focus();});
      b.addEventListener('keydown',event=>{const keys=Object.keys(observationCategories),next=event.key==='ArrowRight'?(index+1)%keys.length:event.key==='ArrowLeft'?(index+keys.length-1)%keys.length:event.key==='Home'?0:event.key==='End'?keys.length-1:null;if(next!==null){event.preventDefault();observationCategory=keys[next];renderWatchlist(state||s);rememberWatchlist();$('category-'+keys[next]).focus();}});box.append(b);
    });
  });
  const pool=s.observation?.counts,limits=s.observation?.policy;
  $('observation-pool-summary').textContent=pool&&pool.total!=null?'研究名额已用 '+pool.total+' / '+(limits.total_limit||40)+'：固定关注 '+pool.original+' + 固定现货 '+pool.fixed+' + 动态占用 '+Math.max(0,pool.total-pool.original-pool.fixed)+'。待补证据且无持仓的线索不占名额。'+(pool.protected_overflow?'已有持仓与委托继续管理，暂停增加其他标的。':''):pool?'动态活跃 '+pool.active+' / '+limits.active_limit+' · 重点研究 '+pool.focus+' / '+limits.focus_limit:'';
  renderObservationArchive(observationArchiveOffset&&observationArchiveData?observationArchiveData:s.observation);
  $('stocks').setAttribute('aria-labelledby','category-'+observationCategory);
  $('last-updated').hidden=observationCategory!=='CN';$('update-coverage').hidden=observationCategory!=='CN';
  for(const id of ['followup-section'])if($(id))$(id).hidden=observationCategory!=='CN';
  $('watchlist-refresh-button').hidden=false;
  $('watchlist-refresh-button').dataset.run=observationCategory==='CN'?'cycle':'global_research';
  $('watchlist-note').hidden=false;
  $('watchlist-note').textContent=observationCategory==='CN'?'单位：元。成本价为当前持仓的平均买入成本（含买入费用）。盈亏按已取得的最近报价计算；参考区间用于观察。':'持仓价值与浮动盈亏以人民币显示；现价、成本价及买卖区间使用标注的原币单位。较成本变动按原币计算，人民币盈亏还受汇率影响。';
  const update=researchUpdate({watchlist:visible.filter(r=>r.category==='CN')});
  $('last-updated').textContent='最近完成公司研究：'+(update.latest?when(update.latest):'尚无成功研究');
  $('update-coverage').textContent='当前筛选的 A股公司中，'+update.completed+' / '+update.total+' 只已完成公司研究。产业线索与公司研究分别展示。';
  const count=visible.filter(r=>r.category===observationCategory).length,total=filteredResearchRows(s,researchListFilter,'ALL').length;
  $('research-timing').textContent='查看当前持仓、最近价格与研究结论；点开公司名称，继续阅读完整依据。';
  $('watchlist-scope').textContent=({CORE:'你固定关注的股票。默认包含全部研究状态，待复核的公司也会显示。',DYNAMIC:'研究过程中发现的公司与资产。加入观察不代表已满足买入条件。',FIXED:'持续观察的固定资产，价格与买卖区间按各自原币显示。',ALL:'固定关注、动态发现与固定资产，按市场和研究状态查找。'}[s.industry?.enabled?researchListFilter:'ALL']);
  $('watchlist-result').textContent='显示 '+count+' / '+total+' 个标的 · '+observationCategories[observationCategory]+' · '+(researchStates[researchStatusFilter]||'全部状态')+(watchlistQuery?' · 搜索“'+watchlistQuery+'”':'');

  renderChanged('stocks',[s.watchlist,s.portfolio.holdings,s.market_phase,s.next_runs,observed,visible,s.industry,observationCategory,researchListFilter,researchStatusFilter,watchlistQuery,[...s.watchlist,...observed].map(item=>quoteIsStale(item.quote,s))],box=>{
    const holdings=new Map(s.portfolio.holdings.filter(h=>h.origin!=='dynamic').map(h=>[h.symbol,h]));
    const discoveries=new Map(observed.map(t=>[t.asset,t]));
    if(observationCategory!=='CN'){
      const targets=visible.filter(t=>t.category===observationCategory).map(t=>({...t,asset:t.asset||t.symbol,links:t.links||t.discovery?.links||[]})).sort((a,b)=>(b.position?.market_value_cents||0)-(a.position?.market_value_cents||0));
      const table=watchlistTable();
      if(!targets.length){drawWatchlistEmpty(box);return;}
      targets.forEach(target=>drawGlobalWatchlistRow(table,target,s));box.append(table);return;
    }
    const extra=observed.filter(t=>t.category==='CN'&&!manual.has(t.asset)).map(t=>({symbol:t.asset,name:t.name,quote:t.quote,discovery:t}));
    for(const item of extra){const h=s.portfolio.holdings.find(h=>h.origin==='dynamic'&&h.symbol===item.symbol);if(h)holdings.set(item.symbol,h);}
    const value=item=>holdings.get(item.symbol)?.market_value_cents??0;
    const ordered=visible.filter(w=>w.category==='CN').sort((a,b)=>value(b)-value(a));
    const t=watchlistTable();
    if(!ordered.length){drawWatchlistEmpty(box);return;}
    for(const item of ordered){const row=el('tr'),name=el('td'),holding=el('td',null,'holding-value'),quote=el('td'),buy=el('td'),sell=el('td'),decision=el('td');
      const h=holdings.get(item.symbol),p=item.plan,l=p?.payload?.levels,discovery=discoveries.get(item.symbol),v=item.entry_allowed===false?{title:item.entry_reason||'暂停新增买入',reason:item.reason||'等待补充证据',ready:false}:item.research_status==='LEAD'?{title:'研究线索，尚不能买入',reason:item.reason,ready:false}:item.discovery?{title:discovery.status==='NEEDS_REVIEW'?'影响待复核':macroDirections[discovery.direction],reason:'动态发现标的，持续观察事件条件与后续证据。',ready:false}:researchDecision(item,s.market_phase);
      name.append(item.discovery&&!s.industry?.enabled?el('strong',item.name):pageLink(item.name,stockPath(item.symbol)),el('p',item.symbol,'subtle'),el('p',h?h.qty+' 股 · '+percent(h.weight_pct):'未持仓','subtle'));
      if(item.discovery)name.append(el('span',poolTierLabels[discovery.pool_tier]||'动态发现','badge'));
      if(item.research_status)name.append(el('p',researchStates[item.research_status],'subtle'));
      if(item.membership)name.append(el('span',item.membership==='CORE'?'固定关注':'动态发现','badge'));
      holding.append(el('strong',money(value(item))+' 元'));
      if(h?.unrealized_cents!=null)holding.append(el('p',(h.unrealized_cents>0?'浮盈 ':h.unrealized_cents<0?'浮亏 ':'浮动盈亏 ')+signed(h.unrealized_cents)+' 元','holding-pnl '+(h.unrealized_cents>0?'profit':h.unrealized_cents<0?'loss':'')));
      if(h?.valuation_basis==='COST_FALLBACK')holding.append(el('p','暂无报价 · 按成本估算','subtle'));
      const current=el('p',null,'position-price');current.append(el('span','现价 ','subtle'),el('strong',item.discovery&&!item.quote?indicatorText(discovery):money(item.quote?.price_cents)));quote.append(current);
      drawPositionCost(quote,h);
      const stale=quoteIsStale(item.quote,s);
      quote.append(el('p',item.quote?(stale?'报价过时 · ':s.market_phase==='CLOSED'?'最近报价 · ':'报价 ')+shortTime(item.quote.observed_at):'待获取',stale?'quote-stale caution':'subtle'));
      buy.append(el('strong',l?money(l.buy_low_cents)+' – '+money(l.buy_high_cents):'暂未形成'));
      sell.append(el('p',l?money(l.sell_cents)+' / '+money(l.stop_cents):'暂未形成'));
      if(p&&p.effective_status!=='ACTIVE')buy.append(el('p',label(p.effective_status)+' · 仅供回看','caution'));
      decision.append(el('strong',v.title,'decision-status '+(v.ready?'ready':'caution')),el('p',brief(v.reason),'decision-reason'),el('p',p?'公司研究 '+shortTime(p.activated_at):item.membership==='DYNAMIC'?'公司研究尚未完成':item.discovery?'事件研究 '+shortTime(discovery.updated_at):'公司研究尚未开始','subtle'));
      decision.append(el('p','组合买卖安排更新：'+(item.last_strategy_updated_at||discovery?.last_strategy_updated_at?shortTime(item.last_strategy_updated_at||discovery.last_strategy_updated_at):'尚未发布'),'subtle'));
      row.className='watchlist-values';[name,holding,quote,buy,sell,decision].forEach((cell,i)=>cell.dataset.label=['股票 / 资产','当前持仓','当前价 / 成本价','买入参考区间','卖出 / 止损参考','当前状态'][i]);row.append(name,holding,quote,buy,sell,decision);
      const summaryRow=el('tr',null,'watchlist-summary-row'),summaryCell=el('td');summaryCell.colSpan=6;
      const research=details('跟踪理由与研究摘要','watchlist-research:'+item.symbol);
      if(item.membership)research.append(el('p',[(item.domains||[]).join('、'),item.reason||'固定关注'].filter(Boolean).join(' · ')+(item.review_at?' · 下次复核 '+shortTime(item.review_at):''),'subtle'));
      const thesis=(s.industry?.hypotheses||[]).filter(h=>h.symbol===item.symbol);
      for(const h of thesis)research.append(el('p',industryMethods[h.method]+'：'+h.payload.thesis),el('p','下一步核实：'+h.payload.next_check,'subtle'));
      const change=(s.industry?.history||[]).find(h=>h.symbol===item.symbol);if(change)research.append(el('p','最近名单变更 '+shortTime(change.at)+'：'+(change.reason||'状态已更新'),'subtle'));
      research.append(pageLink('查看公司全部研究与历史 →',stockPath(item.symbol)));
      research.append(el('p',overviewSummary(item,v),'watchlist-status-summary'));summaryRow.append(summaryCell);
      if(!item.discovery)research.append(el('p',executionSummary(item,s.next_runs?.find(r=>r.kind==='slot')),'subtle'));
      if(discovery){drawPoolStatus(research,discovery);drawObservationCauses(research,discovery);}summaryCell.append(research);
      const group=el('tbody');group.id='observed-'+item.symbol;group.tabIndex=-1;group.setAttribute('aria-label',item.name);group.append(row,summaryRow);t.append(group);
    }box.append(t);
  });
}
async function refreshStock(){
  const data=await api.get('/api/stock?symbol='+encodeURIComponent(selectedSymbol)+'&offset='+historyOffset);stockData=data;
  $('opinion-context').textContent='关于 '+data.stock.name+' · '+(data.stock.plan?'研究 '+shortTime(data.stock.plan.activated_at):'尚无研究');
  renderChanged('stock-content',[data.stock,data.position,data.dimensions,data.followups,data.trade_statistics,data.industry],box=>{
    const item={...data.stock,...(data.industry?.members?.[0]||{})},p=item.plan,l=p?.payload?.levels,report=item.report||{},verdict=researchDecision(item),h=data.position;
    document.title=item.name+' · ThesisTrade';
    const top=el('section',null,'panel stock-summary'),heading=el('div',null,'stock-head'),name=el('div'),q=el('div',null,'quote');name.append(el('h1',item.name),el('span',item.symbol,'subtle'));q.append(el('strong',money(item.quote?.price_cents)+' 元'),el('p',item.quote?'报价 '+shortTime(item.quote.observed_at):'报价尚未取得'));heading.append(name,q);
    top.append(heading,el('p',verdict.title,'verdict'+(verdict.ready?' ready':'')),el('p',verdict.reason));
    if(l){const levels=el('div',null,'levels');[['买入参考区间',money(l.buy_low_cents)+' – '+money(l.buy_high_cents)],['卖出参考',money(l.sell_cents)],['止损参考',money(l.stop_cents)]].forEach(([title,value])=>{const d=el('div');d.append(el('span',title),el('b',value));levels.append(d);});top.append(levels);}
    top.append(el('p',p?'研究更新 '+shortTime(p.activated_at)+' · 有效至 '+shortTime(p.valid_until)+' · '+label(p.effective_status):'尚无完整公司研究','subtle'));
    top.append(el('p','组合买卖安排更新：'+(item.last_strategy_updated_at?when(item.last_strategy_updated_at):'尚未发布'),'subtle'));
    top.append(el('p',executionSummary(item,null),'subtle'));
    if(item.latest_study&&item.latest_study.id!==p?.study_id)top.append(el('p','最新尝试'+label(item.latest_study.model_status)+'。以下保留上次形成的结论，价位是否有效以当前状态为准。','attention'));
    const stats=el('div',null,'position-summary');stats.append(el('p',h?'持仓 '+h.qty+' 股 · 可卖 '+h.sellable_qty+' 股 · 仓位 '+percent(h.weight_pct):'当前未持仓'));if(h){drawPositionCost(stats,h);stats.append(el('p','成本含已分摊买入费用；盈亏按当前剩余持仓计算。','subtle'));}stats.append(el('p',h?'持仓市值 '+money(h.market_value_cents)+' 元 · 浮动盈亏 '+signed(h.unrealized_cents)+' 元':'当前没有持仓，历史成交仍可查看。'),el('p','累计 '+data.trade_statistics.fill_count+' 笔成交 · 已实现 '+signed(data.trade_statistics.realized_cents)+' 元 · 交易费用 '+money(data.trade_statistics.fee_cents)+' 元','subtle'));top.append(stats);box.append(top);
    if(data.industry?.members?.length||data.industry?.hypotheses?.length){const section=el('section',null,'panel');section.append(el('h2','为什么跟踪这家公司'),el('p',item.reason||'固定关注'),el('p',item.entry_reason||'买入前仍需完整研究与交易条件通过','subtle'));drawIndustry(section,data.industry);box.append(section);}
    const research=el('section',null,'panel');research.append(el('h2',item.archived?'归档前的公司研究':'最新公司研究结论'),el('p',report.analysis||(item.archived?'归档前尚未形成完整公司研究；已有产业依据和变更记录保留在上方。':'尚未形成完整公司研究，暂不能判断买卖条件。'),'research-summary'));renderDecisionCard(research,report.decision,item.symbol);
    if(p&&data.dimensions.some(d=>d.origin==='EXISTING_REPORT'))research.append(el('p',item.archived?'以下是归档时保存的研究与财务资料。':'以下分项根据当前研究及财务资料整理，随公司研究更新。','subtle'));
    for(const d of p?data.dimensions:[]){const section=details(d.title,'dimension:'+d.id);section.className='dimension';section.append(el('p',d.summary));if(d.uncertainty)section.append(el('p','待确认 / 判断边界：'+d.uncertainty,'subtle'));for(const evidence of d.evidence_ids||[]){const fact=report.facts?.find(f=>f.evidence_id===evidence);const line=el('p');if(fact)line.append(el('span',fact.quote+' '));line.append(link('查看依据','/api/document?id='+encodeURIComponent(evidence.split(':')[0])));section.append(line);}research.append(section);}
    if(p){renderResearchChanges(research,p);renderDossier(research,p,item.symbol);const method=details('参考价位的计算与用途','method');method.append(el('p',p.payload.kind==='RISK_EXIT_ONLY'?'按现有持仓成本及止损设置计算，用于减仓保护。':'买入区间以近 20 日均价上下 '+entryBand(p)+'% 计算，另按策略比例计算止损与卖出参考。买入还要求 20 日均价高于 60 日均价、昨日收盘不低于 60 日均价。'),el('p','这些参考价用于模拟交易条件，不是公司合理估值。','subtle'));research.append(method);}box.append(research);
    const tasks=el('section',null,'panel');tasks.append(el('h2','还缺什么与下一步'));renderTradeGuidance(tasks,item);renderRecovery(tasks,item);drawFollowups(tasks,data.followups);renderFailures(tasks,item);box.append(tasks);
  });
  renderChanged('stock-history',data.history,box=>{for(const [key,title] of [['fills','成交记录'],['orders','委托记录'],['decisions','买卖依据']]){const info=data.history[key],section=details(title+'（共 '+info.total+' 条）','history:'+key);if(key==='fills')section.open=true;
    const rows=info.items;if(!rows.length)section.append(el('p',info.total?'本页没有此类记录。':'暂无'+title+'。','empty'));
    else if(key==='fills')dataTable(section,['时间 / 方向','股数 / 成交价','费用 / 已实现盈亏'],rows.map(r=>[shortTime(r.occurred_at)+' · '+label(r.side),r.qty+' 股 / '+money(r.price_cents),money(r.fee_cents)+' / '+(r.side==='SELL'?signed(r.realized_cents):'尚未卖出')]));
    else if(key==='orders')dataTable(section,['提交时间 / 方向','委托数量 / 限价','状态'],rows.map(r=>[shortTime(r.created_at)+' · '+label(r.side),r.qty+' 股 / '+money(r.limit_cents),label(r.status)]));
    else dataTable(section,['时间 / 判断','结果','原因'],rows.map(r=>[shortTime(r.at)+' · '+label(r.action),label(r.status),decisionReason(r.reason)]));box.append(section);}});
  const max=Math.max(...Object.values(data.history).map(h=>h.total));$('history-page').textContent='第 '+(historyOffset/30+1)+' / '+Math.max(1,Math.ceil(max/30))+' 页';$('history-prev').disabled=historyOffset===0;$('history-next').disabled=historyOffset+30>=max;updateButtons();
}
async function loadInbox(){
  try{const result=await api.get('/api/feedback?offset='+inboxOffset+'&status='+encodeURIComponent($('inbox-status').value)+'&symbol='+encodeURIComponent($('inbox-symbol').value));inboxTotal=result.total;const box=$('inbox-items');box.replaceChildren();if(!result.items.length)box.append(el('p','当前筛选下没有意见。','panel empty'));
    for(const item of result.items){const card=el('article',null,'panel'),head=el('div',null,'section-head');head.append(el('h3',(item.nickname||item.username)+' · '+topicLabels[item.topic]),el('span',shortTime(item.created_at),'subtle'));card.append(head);const context=el('p',null,'subtle');context.append(item.symbol?pageLink(state?.watchlist.find(w=>w.symbol===item.symbol)?.name||item.symbol,'/stocks/'+item.symbol):el('span','首页'));context.append(el('span',' · '+item.username));card.append(context,el('p',item.body,'opinion-text'));
      if(item.study_id){const ref=details('提交时的研究版本','feedback-version:'+item.id);ref.append(el('p','研究时间 '+shortTime(item.study_at),'subtle'),el('p',item.study_analysis||'该版本尚无完整研究分析。'));card.append(ref);}
      const form=el('form',null,'inbox-update'),select=el('select'),selectLabel=el('label','处理状态'),note=el('textarea'),noteLabel=el('label','管理员备注'),button=el('button','保存处理记录'),status=el('p',null,'feedback');select.id='status-'+item.id;selectLabel.htmlFor=select.id;note.id='note-'+item.id;noteLabel.htmlFor=note.id;
      for(const [value,title] of Object.entries(inboxLabels)){const option=el('option',title);option.value=value;select.append(option);}select.value=item.status;note.value=item.admin_note;note.maxLength=4000;note.rows=2;status.hidden=true;status.setAttribute('role','status');form.append(selectLabel,select,noteLabel,note,button,status);
      form.addEventListener('submit',async e=>{e.preventDefault();button.disabled=true;try{await api.post('/api/feedback/update',{id:item.id,status:select.value,admin_note:note.value});status.textContent='处理记录已保存。';status.className='feedback';}catch(e){status.textContent=e.message;status.className='feedback error';}finally{status.hidden=false;button.disabled=false;}});card.append(form);box.append(card);
    }$('inbox-page').textContent='共 '+result.total+' 条 · 第 '+(inboxOffset/50+1)+' 页';$('inbox-prev').disabled=!inboxOffset;$('inbox-next').disabled=inboxOffset+50>=result.total;
  }catch(e){feedback('inbox-feedback',e.message,'error');}
}
function initPages(){
  const route=location.pathname,id=selectedSymbol?'stock-page':route==='/admin/feedback'?'admin-feedback-page':route==='/admin/settings'?'admin-settings-page':'home-page';$(id).hidden=false;$('opinion-board').hidden=route.startsWith('/admin/');if(route.startsWith('/admin/'))document.querySelector('.app-layout').classList.add('single-column');$('current-user').textContent=currentUser.role==='ADMIN'?'管理员':'访客';
  if(selectedSymbol){const back=new URLSearchParams(location.search).get('return');if(back&&back.startsWith('#watchlist'))$('stock-back').href='/'+back;}
  $('logout').addEventListener('click',async()=>{try{await api.post('/api/logout',{});location.replace('/login');}catch(e){$('attention').textContent=e.message;$('attention').hidden=false;}});
  for(const [button,delta] of [['history-prev',-30],['history-next',30]])$(button).addEventListener('click',async()=>{historyOffset=Math.max(0,historyOffset+delta);try{await refreshStock();}catch(e){$('attention').hidden=false;$('attention').textContent=e.message;}});
  $('opinion-form').addEventListener('submit',async e=>{e.preventDefault();const b=e.currentTarget.querySelector('button');b.disabled=true;const body={page:selectedSymbol?'stock':'home',symbol:selectedSymbol||null,study_id:stockData?.stock.plan?.study_id||null,nickname:$('opinion-name').value,topic:$('opinion-topic').value,body:$('opinion-body').value};const signature=JSON.stringify([body.page,body.symbol,body.nickname,body.topic,body.body]);if(opinionAttempt?.signature!==signature)opinionAttempt={signature,id:crypto.randomUUID(),body};feedback('opinion-feedback','正在提交…','pending');try{await api.post('/api/feedback',{...opinionAttempt.body,request_id:opinionAttempt.id});$('opinion-body').value='';opinionAttempt=null;feedback('opinion-feedback','意见已送达，仅管理员可见。');}catch(e){feedback('opinion-feedback',e.message+' 再次提交不会重复保存同一条意见。','error');}finally{b.disabled=false;}});
  $('feedback-filter').addEventListener('submit',e=>{e.preventDefault();inboxOffset=0;loadInbox();});
  for(const [id,delta] of [['inbox-prev',-50],['inbox-next',50]])$(id).addEventListener('click',()=>{inboxOffset=Math.max(0,inboxOffset+delta);loadInbox();});
  if(route==='/admin/feedback')api.get('/api/status').then(s=>{for(const w of s.watchlist){const o=el('option',w.name);o.value=w.symbol;$('inbox-symbol').append(o);}}).catch(e=>feedback('inbox-feedback',e.message,'error'));
  $('password-form').addEventListener('submit',async e=>{e.preventDefault();const b=e.currentTarget.querySelector('button');b.disabled=true;try{const result=await api.post('/api/password',{username:$('password-user').value,password:$('new-password').value});$('new-password').value='';if(result.reauthenticate)location.replace('/login');else feedback('password-feedback','密码已更新，该账号需要重新登录。');}catch(e){feedback('password-feedback',e.message,'error');}finally{b.disabled=false;}});
}


const dynamicStates={RESEARCH:'研究中',READY:'等待入场',HOLDING:'持仓中',CLOSED:'已退出',EXPIRED:'已过期',INVALIDATED:'事件已失效'};
function drawDynamicCase(box,c){
  const card=el('article',null,'dynamic-card'),head=el('div',null,'section-head');
  head.append(el('strong',c.name+' · '+c.symbol),el('span',dynamicStates[c.status]||c.status,'badge'));
  card.append(head,link(c.title,c.url),el('p',c.analysis.impact),el('p',c.analysis.business_link,'subtle'));
  const blockers=c.plan.blockers||[];
  card.append(el('p',c.position.qty?'动态持仓 '+c.position.qty+' 股，可卖 '+c.position.sellable_qty+' 股':blockers.length?'等待：'+blockers.slice(0,2).join('；'):c.status==='READY'?'计划已验证，等待价格与资金条件':'等待研究与历史验证','dynamic-wait'));
  if(c.check)card.append(el('p','最近检查：'+c.check.reason,'subtle'));
  if(c.orders?.length)card.append(el('p',c.orders.map(o=>label(o.side)+' '+o.filled_qty+'/'+o.qty+' 股 · '+label(o.status)).join('；'),'subtle'));
  card.append(el('p',c.source+' · 发布 '+shortTime(c.published_at)+' · 研究 '+shortTime(c.created_at),'subtle'));
  const detail=details('证据、交易条件与历史验证','dynamic:'+c.id);
  detail.append(el('p','价格反映程度：'+c.analysis.pricing),el('p','推翻条件：'+c.analysis.invalidation));
  if(c.plan.buy_low_cents)detail.append(el('p','入场区间 '+money(c.plan.buy_low_cents)+'–'+money(c.plan.buy_high_cents)+' 元；有效至 '+shortTime(c.expires_at)+'。'));
  detail.append(el('p','此类交易单次仓位上限 '+(c.plan.max_position_pct||5)+'%；持有期最多3个交易日，并检查止损和止盈。'));
  for(const e of c.analysis.evidence||[])detail.append(el('blockquote',e.quote));
  addList(detail,'当前等待条件',blockers);
  const h=c.plan.history;
  if(h)detail.append(el('p','独立前向样本 '+h.count+'/'+h.required+'；历史补采案例 '+h.retrospective_count+'。'+(h.passed?'验证门槛已通过。':'尚未通过自动模拟入场门槛。')));
  if(c.observation){const o=c.observation,r=o.result;detail.append(el('p',(o.basis==='FORWARD'?'前向观察':'历史补采观察')+'：'+shortTime(o.entry_at)+' 至 '+shortTime(o.exit_at)+'；市场变动 '+(r.market_return_bps/100).toFixed(2)+'%。'+(r.status==='MEASURED'?'':'价格不连续，不纳入验证。')),el('p',r.note,'subtle'));}
  if(c.basis==='RETROSPECTIVE')detail.append(el('p','此案例在新闻发生后补采，存在回看偏差，仅供研究，不计入自动交易验证。','subtle'));
  card.append(detail);box.append(card);
}
function renderDynamic(s){
  const d=s.dynamic;if(!d)return;
  const active=d.items.filter(c=>['READY','RESEARCH','HOLDING'].includes(c.status)||c.position.qty);
  $('dynamic-count').textContent='近48小时 · '+(d.global?.library?.current_total??d.global?.items.filter(macroProminent).length??0)+' 个事件';
  renderGlobalMacro(d);
  $('dynamic-toggle').textContent=d.enabled?'暂停新闻跟踪':'开启新闻跟踪';
  const job=s.active_jobs?.find(j=>j.kind==='dynamic_cycle')||d.last_run?.status==='RUNNING';
  $('dynamic-timing').textContent=(job?'正在更新 · ':!d.enabled?'新闻跟踪已暂停 · ':!s.scheduler_enabled?'自动调度已暂停 · ':'每30分钟更新 · ')+((d.last_finished_at||d.last_run?.finished_at)?'上次完成 '+shortTime(d.last_finished_at||d.last_run.finished_at):'等待首次采集完成')+(d.next_at?' · 下次 '+shortTime(d.next_at):'');
  renderChanged('dynamic-health',[d.last_run?.details?.failures,d.last_run?.details?.identity_failures,d.last_run?.details?.coverage,d.last_run?.details?.research_deferred,d.last_run?.details?.global_research?.summary,d.state.quote_error],box=>{
    if(d.last_run?.details?.global_research?.summary)box.append(el('p',d.last_run.details.global_research.summary,'subtle'));
    const failures=d.last_run?.details?.failures||[];
    if(failures.length){const summary=details('部分新闻来源待恢复（'+failures.length+'）','dynamic-failures');failures.forEach(f=>summary.append(el('p',f.includes('CERTIFICATE_VERIFY_FAILED')?f.split('：')[0]+'：暂时无法连接资料来源，稍后重试。':readableError(f),'subtle')));box.append(summary);}
    if(d.last_run?.details?.coverage?.sina_complete===false)box.append(el('p','48小时新闻窗口尚未完整回补，后续继续采集。','caution'));
    for(const failure of d.last_run?.details?.identity_failures||[])box.append(el('p',failure+'；未核验标的暂不加入观察。','subtle'));
    if(d.last_run?.details?.research_deferred)box.append(el('p',readableError(d.last_run.details.research_deferred),'subtle'));
    if(d.state.quote_error)box.append(el('p','行情暂未更新：'+readableError(d.state.quote_error),'caution'));
  });
  renderChanged('dynamic-budget',d.balance,box=>{
    box.append(el('span','事件交易持仓市值 '+money(d.balance.market_value_cents)+' 元'),el('span','事件交易委托占用 '+money(d.balance.reserved_cents)+' 元'));
  });
  renderChanged('dynamic-cases',active,box=>{
    if(!active.length){box.append(el('p',d.last_run?'目前没有通过全部执行条件的计划。全球研究结论会持续跟踪，具备适配和证据的标的才进入模拟交易。':'暂无模拟执行计划。','empty'));return;}
    active.forEach(c=>drawDynamicCase(box,c));
  });
  renderChanged('dynamic-news',d.recent_news,box=>{d.recent_news.forEach(n=>{const row=el('p',null,'dynamic-news-row');row.append(link(n.title,n.url),el('span',n.source+' · '+shortTime(n.published_at),'subtle'));box.append(row);});});
  table('dynamic-fills',['时间','标的','操作','成交','费用'],d.fills.map(f=>[shortTime(f.occurred_at),f.symbol,label(f.side),f.qty+' 股 × '+money(f.price_cents),money(f.fee_cents)]),'事件交易暂无成交。');
  const archived=d.items.filter(c=>!active.includes(c));
  renderChanged('dynamic-archive',archived,box=>{if(!archived.length)box.append(el('p','暂无已结束事件。','empty'));else archived.forEach(c=>drawDynamicCase(box,c));});
}

const macroRegions={GLOBAL:'全球',US:'美国',EUROPE:'欧洲',JAPAN:'日本',CHINA:'中国',ASIA:'亚洲',MIDDLE_EAST:'中东',OCEANIA:'大洋洲',LATIN_AMERICA:'拉丁美洲',AFRICA:'非洲',EMERGING:'新兴市场'};
const macroDirections={UP:'上行压力',DOWN:'下行压力',MIXED:'双向影响',UNCLEAR:'方向待确认'};
function macroDisplayLifecycle(event){
  const life=event.lifecycle;if(!life)return null;
  const current=Date.now();
  if(life.actionable&&Number.isFinite(Date.parse(life.expires_at))&&current>=Date.parse(life.expires_at)){
    return {...life,state:'ENDED_UNCONFIRMED',label:'观察已到期 · 待同步核对',stale:true,
      reason:'本页按当前时间发现观察期限已过；缓存中的旧评估不能作为当前依据。',
      next_step:'等待本机同步最新状态；旧判断及缺失结果仅供追溯，不计成功或失败。'};
  }
  if(life.actionable&&life.state!=='REVIEW_DUE'&&Number.isFinite(Date.parse(life.review_due_at))&&current>=Date.parse(life.review_due_at)){
    return {...life,state:'REVIEW_DUE_UNCONFIRMED',label:'已到复核期 · 待同步核对',stale:true,
      reason:'当前时间已到复核期限，缓存中的旧评估不能作为当前依据。',
      next_step:'核实新的证据，并等待本机同步最新复核状态。'};
  }
  return life;
}
function macroDirectionSummary(impacts,assets){
  return impacts.map(i=>(assets[i.asset]?.name||i.asset)+'：'+(macroDirections[i.direction]||'方向待确认')).join('；');
}
function drawMacroRevisions(parent,event,assets){
  const revision=event.revisions;if(!revision||(!revision.total&&!revision.source_replacements?.length))return;
  const box=details('修订经过与替代来源','macro-revisions:'+event.id);
  if(revision.total)box.append(el('p','研究共修订 '+revision.total+' 次，展示最近 '+revision.items.length+' 次。','subtle'));
  for(const change of revision.items||[]){
    box.append(el('p',shortTime(change.at)+' · '+(change.changed.length?change.changed.join('、'):'研究重新生成，主要结论字段未变')));
    if(change.before_headline!==change.after_headline)box.append(el('p','主题：'+change.before_headline+' → '+change.after_headline,'subtle'));
    const before=macroDirectionSummary(change.before_directions,assets),after=macroDirectionSummary(change.after_directions,assets);
    if(before!==after)box.append(el('p','此前：'+before),el('p','修订后：'+after));
  }
  if(revision.source_replacement_total)box.append(el('p','原文共有 '+revision.source_replacement_total+' 条后续修订，展示最近 '+revision.source_replacements.length+' 条，最新版本优先。','subtle'));
  for(const source of revision.source_replacements||[]){
    box.append(link((source.source_status==='REVISED'?'历史中间修订：':'最新修订来源：')+source.title,source.url),el('p','发现修订 '+shortTime(source.first_seen_at),'subtle'));
    const all=[...(state?.dynamic?.global?.items||[]),...(state?.dynamic?.global?.followup_items||[]),...(state?.dynamic?.global?.history_items||[])];
    if(source.event_id&&all.some(e=>e.id===source.event_id)){
      const button=el('button','查看后续研究','text-button');button.type='button';button.addEventListener('click',()=>showMacroEvent(source.event_id));box.append(button);
    }
  }
  parent.append(box);
}
function drawMacroEvent(box,event,assets,options={}){
  const a=event.analysis,life=macroDisplayLifecycle(event),compact=!!options.compact;
  const historical=options.historical||event.status==='INVALIDATED'||life?.stale||['ENDED','BACKGROUND'].includes(life?.state);
  const card=el('article',null,'macro-card'+(compact?' macro-library-card':'')),head=el('div',null,'section-head');
  card.id='macro-event-'+event.id;card.tabIndex=-1;
  head.append(el('h3',a.headline),el('span',life?.label||(event.status==='INVALIDATED'?'原判断已失效':event.theme_label),'badge'));card.append(head);
  card.append(el('p',a.regions.map(r=>macroRegions[r]||r).join(' · ')+' · '+(historical?'当时研究期限：':'研究期限：')+({DAYS:'数日',MONTHS:'数月',UNCERTAIN:'待确认'}[a.horizon])+' · 新闻发布 '+shortTime(event.published_at),'subtle'));
  if(life){
    card.append(el('p',life.reason,'macro-lifecycle'),el('p','复核期限 '+shortTime(life.review_due_at)+' · 观察结束 '+shortTime(life.expires_at),'subtle'));
    card.append(el('p',life.next_step,'macro-next-step'));
  }else if(historical)card.append(el('p','历史判断仅供回看，当前状态以最新研究为准。','macro-lifecycle'));
  if(compact){
    card.append(el('p',brief(a.facts),'macro-library-summary'));
    card.append(el('p',(historical?'当时推演：':'初步推演：')+macroDirectionSummary(a.impacts,assets),'subtle'));
    const reviewed=a.impacts.filter(i=>i.materiality?.assessment?.direction).map(i=>({asset:i.asset,direction:i.materiality.assessment.direction}));
    if(reviewed.length)card.append(el('p',(historical?'当时独立评估：':'独立评估：')+macroDirectionSummary(reviewed,assets),'subtle'));
    if(a.impacts.some(i=>i.materiality?.assessment?.direction&&i.materiality.assessment.direction!==i.direction))card.append(el('p','初步推演与独立评估存在方向分歧，请分别核对依据。','caution'));
    const outcomes=(event.reactions||[]).filter(r=>r.observation),recorded=outcomes.filter(r=>!r.observation.diagnostic_only),diagnostics=outcomes.filter(r=>r.observation.diagnostic_only);
    if(outcomes.length){
      for(const [title,rows] of [['已记录观察：',recorded],['页面辅助观察：',diagnostics]]){
        if(rows.length)card.append(el('p',title+rows.slice(0,3).map(r=>r.name+' '+(r.observation.change>0?'+':'')+r.observation.change+r.observation.change_unit).join('；'),'macro-library-summary'));
      }
      if(diagnostics.length)card.append(el('p','页面辅助观察仅供回看，不写入研究历史或交易决策。','subtle'));
      card.append(el('p','日级相关变化，不等于因果验证或交易收益。','subtle'));
    }else if(event.reactions?.length)card.append(el('p','观察缺口：'+event.reactions[0].waiting,'subtle'));
    if(event.revisions?.total||event.revisions?.source_replacement_total)card.append(el('p','研究修订 '+(event.revisions.total||0)+' 次 · 原文后续修订 '+(event.revisions.source_replacement_total||0)+' 条，详情可追溯。','subtle'));
  }
  const body=compact?details('查看完整判断、证据与修订','macro-body:'+event.id):card;
  if(!compact)body.append(el('p',a.facts,'macro-facts'));
  else body.append(el('p',a.facts));
  body.append(el('h4',historical?'当时的标的影响判断':'受影响标的与方向','macro-impact-heading'));
  const impacts=el('ul',null,'macro-impacts');impacts.setAttribute('aria-label','受影响标的与方向');
  for(const impact of a.impacts){
    const row=el('li',null,'macro-impact'),title=el('div',null,'section-head'),direction=el('span',(historical?'当时推演：':'初步推演：')+macroDirections[impact.direction],'macro-direction');direction.dataset.direction=impact.direction;
    title.append(el('strong',assets[impact.asset]?.name||impact.asset),direction);row.append(title,el('p','初步影响规模：'+(impactStrengths[impact.strength]||impactStrengths.UNKNOWN),'impact-strength'));
    drawImpactAssessment(row,impact.materiality,'macro:'+event.id+':'+impact.asset,{historical});
    if(impact.materiality?.assessment?.direction&&impact.materiality.assessment.direction!==impact.direction)row.append(el('p','两次判断方向不同，评估通过不代表支持初稿方向。','caution'));
    drawLogicChain(row,impact,'macro:'+event.id+':'+impact.asset);
    const observed=state?.observation?.items?.find(t=>t.asset===impact.asset)||state?.observation?.membership?.[impact.asset];
    if(observed){const tier=observed.pool_tier||observed.tier,jump=el('button',['COOLING','ARCHIVED'].includes(tier)?'查看候补 / 归档记录':tier==='FOCUS'?'重点研究 · 查看公司与资产':'已加入公司与资产 · 查看','text-button');jump.type='button';jump.addEventListener('click',()=>showObservation(impact.asset,observed.category));row.append(jump);}
    impacts.append(row);
  }
  body.append(impacts,el('p','不确定性：'+a.uncertainty,'macro-uncertainty'));
  const detail=details(historical?'原分析依据与已记录观察':'分析依据与后续观察','macro:'+event.id);
  detail.append(el('p','传导机制：'+a.transmission),el('p','预期差：'+a.expectation_basis),el('p','反证：'+a.invalidation));
  addList(detail,historical?'当时拟观察的条件':'后续观察',a.impacts.map(i=>(assets[i.asset]?.name||i.asset)+'：'+i.watch));
  for(const report of event.related_reports||[])detail.append(link('同一事件报道：'+report.source+' · '+report.title,report.url));
  for(const cite of event.citations){detail.append(link(cite.source+' · '+cite.title,cite.url),el('blockquote',cite.quote));}
  detail.append(el('p',event.basis==='RETROSPECTIVE'?'历史补采研究：形成判断时事件已经发生，存在回看偏差。':'前向口径：从该次研究完成后观察对应指标。','subtle'));
  for(const r of event.reactions){const o=r.observation;if(o){detail.append(el('p',(o.diagnostic_only?'页面辅助观察 · ':'')+r.name+' · '+o.baseline.date+' → '+o.end.date+' · '+(o.change>0?'+':'')+o.change+o.change_unit),link('查看指标来源',o.url),el('p',o.method,'subtle'));}else detail.append(el('p',r.name+'：'+r.waiting,'subtle'));}
  detail.append(el('p','研究更新 '+shortTime(event.created_at)+'；市场方向是条件判断，不代表已提交交易。','subtle'));body.append(detail);
  drawMacroRevisions(body,event,assets);
  if(compact)card.append(body);box.append(card);
}
const macroPages={current:0,followup:0,history:0};
function renderMacroPage(box,events,kind,assets,total=events.length,asOf=null){
  const size=5,pages=Math.max(1,Math.ceil(events.length/size));macroPages[kind]=Math.min(macroPages[kind]||0,pages-1);
  const draw=()=>{
    box.replaceChildren();const page=macroPages[kind],start=page*size;
    const caption=total>events.length?'共 '+total+' 条，本次仅载入最近 '+events.length+' 条；其余记录继续保留，本页未载入。':'共 '+total+' 条，每页最多 '+size+' 条。';
    box.append(el('p',caption+(asOf?' 状态核对截至 '+shortTime(asOf)+'；到期提醒按当前时间更新。':''),'subtle'));
    if(!events.length){box.append(el('p',total>0?'本次摘要未载入这些记录，请在本机追溯完整内容。':kind==='followup'?'暂无仍在观察期内的待跟进事项。':'暂无记录。','empty'));return;}
    events.slice(start,start+size).forEach(e=>drawMacroEvent(box,e,assets,{compact:true,historical:kind==='history'}));
    if(pages>1){
      const nav=el('nav',null,'macro-pagination');nav.setAttribute('aria-label',kind==='current'?'重要动态分页':kind==='followup'?'待跟进分页':'历史研究分页');
      const prev=el('button','上一页'),next=el('button','下一页');prev.type=next.type='button';prev.disabled=page===0;next.disabled=page===pages-1;
      prev.addEventListener('click',()=>{macroPages[kind]=Math.max(0,macroPages[kind]-1);draw();});next.addEventListener('click',()=>{macroPages[kind]=Math.min(pages-1,macroPages[kind]+1);draw();});
      nav.append(prev,el('span','第 '+(page+1)+' / '+pages+' 页'),next);box.append(nav);
    }
  };draw();
}
function renderNewsScreening(box,screening){
  if(!screening)return;
  const c=screening.counts||{};
  box.append(el('p','近48小时新闻筛选：深入研究 '+(c.DEEP||0)+' 条 · 等待关键证据 '+(c.WATCH||0)+' 条 · 背景资料 '+(c.BACKGROUND||0)+' 条。','subtle'));
  if(!screening.recent?.length)return;
  const details=el('details',null,'dynamic-library');details.append(el('summary','查看近期筛选依据'));
  const decisions={DEEP:'进入深入研究',WATCH:'等待关键证据',BACKGROUND:'保留为背景'};
  screening.recent.forEach(n=>{
    const row=el('article',null,'news-screening-item');row.append(link(n.title,n.url),el('p',decisions[n.decision]+' · '+n.source+' · '+shortTime(n.at),'subtle'));
    row.append(el('p',n.reason),el('p','传导路径：'+n.channel,'subtle'),el('p','规模依据：'+n.scale_basis,'subtle'));
    if(n.expectation_signal?.channel!=='NONE'&&n.expectation_signal){
      const x=n.expectation_signal;
      row.append(el('p','预期重定价：'+x.repricing_logic),el('p','反证：'+x.counter_evidence,'subtle'));
      if(x.baseline_quote)row.append(el('p','此前依据：'+x.baseline_quote,'subtle'));
      else row.append(el('p','此前可比表态尚待核实，未断言出现新增冲击。','subtle'));
    }
    if(n.content_basis)row.append(el('p','资料范围：'+n.content_basis,'subtle'));
    if(n.decision!=='BACKGROUND')row.append(el('p','待核实：'+n.next_evidence,'subtle'));
    details.append(row);
  });
  box.append(details);
}
function macroProminent(event){
  if(event.screening===undefined)return true;
  const impacts=event.analysis?.impacts||[];
  if(impacts.some(i=>i.materiality?.admitted))return true;
  return event.screening?.decision==='DEEP'&&!impacts.every(i=>i.materiality?.state==='BACKGROUND');
}
function renderGlobalMacro(d){
  const g=d.global||{items:[],assets:{},markets:[],sources:[],news_counts:{},observation_counts:{},event_count:0};
  renderChanged('macro-screening',g.news_screening,box=>renderNewsScreening(box,g.news_screening));
  const prominent=g.items.filter(macroProminent),asOf=g.library?.as_of||g.window_end,clock=[asOf,Math.floor(Date.now()/3600000)];
  renderChanged('macro-cases',[g.items,g.assets,state?.observation?.membership,g.library?.current_total,clock],box=>{
    if(!(g.library?.current_total??prominent.length)){box.append(el('p','近48小时暂无通过筛选且已完成研究的重要动态。新闻每30分钟更新，仍有跟进价值的事项和历史记录分别在下方查看。','empty'));return;}
    renderMacroPage(box,prominent,'current',g.assets,g.library?.current_total??prominent.length,asOf);
  });
  const legacy=[...g.items.filter(e=>!macroProminent(e)),...(g.archived_items||[])];
  const followup=g.followup_items||[],archived=g.history_items||legacy,library=g.library||{};
  if($('macro-followup-section')){
    $('macro-followup-section').hidden=!(library.followup_total??followup.length);
    if($('macro-followup-title'))$('macro-followup-title').textContent='当前待跟进（'+(library.followup_total??followup.length)+'）';
    renderChanged('macro-followup',[followup,g.assets,state?.observation?.membership,library.followup_total,clock],box=>renderMacroPage(box,followup,'followup',g.assets,library.followup_total??followup.length,asOf));
  }
  $('macro-history').hidden=!(library.history_total??archived.length);
  $('macro-history-title').textContent='历史研究与修订（'+(library.history_total??archived.length)+'）';
  renderChanged('macro-archive',[archived,g.assets,state?.observation?.membership,library.history_total,clock],box=>renderMacroPage(box,archived,'history',g.assets,library.history_total??archived.length,asOf));
  $('macro-execution-note').textContent=g.execution||'全球市场结论用于研究；当前账户仅接入普通沪深主板模拟交易，执行计划单独核验。';
  renderChanged('dynamic-learning',[g.event_count,g.observation_counts,g.news_counts,g.impact_learning],box=>{
    const impact=g.impact_learning;
    if(impact){box.append(el('p','独立影响评估 '+impact.assessed+' 项 · 待评估 '+impact.pending+' 项 · 背景资料 '+impact.background+' 项。'));
      box.append(el('p','历史效果检验：'+impact.forward_samples+' 个可用前向样本，'+impact.calibrated_cohorts+' 组达到检验数量。每组至少20例训练及后续10例检验。'+(!impact.calibrated_cohorts?'目前尚未完成历史验证。':''),'subtle'));}

    box.append(el('p','全球宏观事件 '+g.event_count+' 个；已完成前向指标观察 '+(g.observation_counts.FORWARD||0)+' 条，历史事件观察 '+(g.observation_counts.RETROSPECTIVE||0)+' 条。'));
    box.append(el('p','比较事件前最近观测与之后第3个观测日。国债收益率按基点变化，其余已接入指标按百分比变化；数据有发布延迟，不是交易收益或因果证明。','subtle'));
    box.append(el('p','优先候选 '+(g.news_counts.PENDING||0)+' 条 · 已合并 '+(g.news_counts.MERGED||0)+' 条重复报道 · 暂存 '+(g.news_counts.DORMANT||0)+' 条 · 过期归档 '+(g.news_counts.EXPIRED||0)+' 条。','subtle'));
    if(g.news_counts.FAILED)box.append(el('p',g.news_counts.FAILED+' 条宏观新闻分析待重试。','caution'));
  });
  renderChanged('macro-markets',g.markets,box=>{
    box.append(el('h4','历史指标覆盖'));
    g.markets.forEach(m=>{const row=el('p',null,'macro-source');row.append(el('strong',m.name),el('span',m.latest?m.latest.date+' · '+m.latest.value+' '+m.unit:m.status==='QUALITATIVE'?'定性研究 · 历史序列未接入':'等待取得历史序列','subtle'));if(m.status==='FAILED')row.append(el('span','最近更新未成功；'+(m.latest?'显示上次数据':'暂不测量变化'),'caution'));if(m.url)row.append(link('来源',m.url));box.append(row);});
  });
  renderChanged('macro-sources',[g.sources,g.source_coverage],box=>{
    const coverage=g.source_coverage;
    box.append(el('h4','新闻来源'));
    if(coverage)box.append(el('p','当前可用 '+coverage.available+' / '+coverage.configured+' 个渠道。'+coverage.note,'subtle'));
    const labels={OK:'已检查',FAILED:'获取失败',PENDING:'等待首次检查',STALE:'订阅内容较旧',OVERDUE:'检查已延迟'};
    const groups={PRIMARY:'政策与产业一手来源',MEDIA:'国际主流媒体',WIRE:'财经快讯'};
    const sources=coverage?.sources||g.sources;
    Object.entries(groups).forEach(([family,title])=>{
      const values=sources.filter(s=>(s.family||'WIRE')===family);if(!values.length)return;
      box.append(el('h4',title));
      values.forEach(s=>{const row=el('p',null,'macro-source');row.append(el('strong',s.source),el('span',(labels[s.status]||'待检查')+(s.checked_at?' · '+shortTime(s.checked_at):'')+(s.coverage?' · '+s.coverage:''),'subtle'));
        if(s.latest_at)row.append(el('span','最新发布 '+shortTime(s.latest_at),'subtle'));
        if(s.status==='FAILED')row.append(el('span',s.detail?.includes('CERTIFICATE_VERIFY_FAILED')?'来源证书暂未通过验证，稍后重试。':s.detail?.includes('403')?'来源暂时拒绝访问，当前覆盖存在缺口。':'本次获取未成功，稍后重试。','caution'));
        if(s.status==='STALE')row.append(el('span','可连接，但近期新闻覆盖不足。','caution'));
        if(s.url)row.append(link('公开订阅',s.url));box.append(row);
      });
    });
    coverage?.gaps?.forEach(g=>box.append(el('p',g.source+'：'+g.reason,'subtle')));
  });}


function portfolioText(value) {return String(value||'').replace(/(\d+(?:\.\d+)?)\s*bps\b/gi,(_,n)=>(Number(n)/100).toFixed(2)+'%');}
function renderPortfolioStrategy(s) {
  const panel=$('portfolio-strategy-panel'),p=s.portfolio_strategy;
  if(!panel)return;
  panel.hidden=!p?.enabled;if(panel.hidden)return;
  $('portfolio-strategy-time').textContent=p.created_at?' · '+shortTime(p.created_at):' · 待生成';
  $('portfolio-strategy-summary').textContent=traderText(portfolioText(p.summary))||'等待综合各路研究、已有持仓和可用资金。';
  const busy=(s.active_jobs||[]).some(j=>j.kind==='portfolio_strategy');
  $('portfolio-strategy-state').textContent=(busy?'正在更新组合判断。 ':p.last_run?.status==='DEFERRED'?'本轮未完成，'+(readableError(p.last_run.error)||'等待重试')+'。 ':'')+(p.status==='ACTIVE'?'有效至 '+shortTime(p.valid_until)+'；买卖仍需满足原策略和风控条件。':'组合买入安排尚未形成或已过期，暂停新增买入；原止损与退出继续。');
  const labels={ALLOW:'允许买入',HOLD:'保持持仓',PAUSE:'暂停买入',REDUCE:'减仓',EXIT:'退出'};
  table('portfolio-strategy-items',['标的','组合决定','研究时仓位 → 目标','调整依据'],[...(p.decisions||[])].sort((a,b)=>b.current_bps-a.current_bps).map(d=>[d.name,labels[d.action]+(d.current_authorization?'':'（待复核）'),(d.current_bps/100).toFixed(2)+'% → '+(d.target_bps/100).toFixed(2)+'%',portfolioText(d.reason)]));
  const groups=$('portfolio-strategy-groups');groups.replaceChildren();
  for(const g of p.risk_groups||[])groups.append(el('p',g.name+' · 合计目标上限 '+(g.max_bps/100).toFixed(2)+'% · '+portfolioText(g.reason),'subtle'));
}
