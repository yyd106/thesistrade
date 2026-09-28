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
        :timeout?'本机后台读取超过 20 秒，正在重试。':'暂时连接不上本机后台，正在重试。';
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
      throw new ApiError('后台返回的内容无法读取，请重试；持续出现时请检查后台错误日志。',response.ok?'RESPONSE':'HTTP',response.status);
    }
    return {response, data};
  }
  async get(path) {
    const {response,data} = await this.request(path);
    if (!response.ok) throw new ApiError(data.error || '后台读取失败，请稍后重试。','HTTP',response.status);
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
      throw new ApiError(data.error || '操作未完成，请稍后重试。','HTTP',response.status);
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
const el = (tag,text,cls) => {const e=document.createElement(tag);if(text!=null)e.textContent=text;if(cls)e.className=cls;return e;};
const names = {portfolio_strategy:'更新组合判断',global_research:'更新美股与现货研究',global_slot:'美股与现货盘面检查',dynamic_cycle:'更新动态研究',dynamic_slot:'动态盘面检查',cycle:'更新研究',collect:'更新资料',research:'重新研究',repair:'补齐单股资料',NOT_NEEDED:'无需模型重复判断',slot:'检查盘面',review:'更新复盘',settle:'模拟撮合',
  PENDING:'等待处理',RUNNING:'正在处理',DONE:'已完成',FAILED:'未完成',INTERRUPTED:'已中断',MISSED:'已错过',SKIPPED_CATCHUP:'已跳过旧批次',
  CLOSED:'休市',CALENDAR_UNKNOWN:'交易日历待更新',DEFERRED:'等待模型可用',SUCCEEDED:'已完成',NOT_RUN:'尚未运行',
  NO_ENTRY:'暂不买入',PAPER_TRADE:'可按条件模拟交易',ACTIVE:'有效',DRAFT:'尚未完成',EXPIRED:'已过期',SUPERSEDED:'已有新版',RISK_EXIT_ONLY:'仅允许减仓',
  BUY:'买入',SELL:'卖出',HOLD:'不动',BLOCKED:'未执行',SUBMITTED:'已提交模拟委托',RECORDED:'已记录',OPEN:'等待成交',FILLED:'全部成交',
  OK:'正常',PARTIAL:'覆盖不全',REVIEW_REQUIRED:'有待核实事项',WATCH:'继续观察',INSUFFICIENT_DATA:'资料不足'};
const label = s => names[s] || s || '—';
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
}[code]||blockerNames[code]||code));
const brief = text => { const t=String(text||''); const sentence=t.match(/^.{12,180}?[。！？]/u);return sentence?sentence[0]:t.length>130?t.slice(0,130)+'…':t; };
let state=null, editing=false, refreshing=false, connected=false;
const renderingCache=new Map(), watchedJobs=new Map(), submitting=new Set();

function feedback(id,text,type='') {const box=$(id);box.textContent=text;box.hidden=!text;box.className='feedback'+(type?' '+type:'');}
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
    quote.append(el('strong',money(item.quote?.price_cents)),el('span',' 元'),el('p','报价 '+shortTime(item.quote?.observed_at)));
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
  section.append(el('p','研究采用的固定资料快照；随下次资料更新重新核验。','subtle'));
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
  $('followup-timing').textContent=s.followups.updated_at?'核对于 '+shortTime(s.followups.updated_at):'等待首次归口检查';
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
  table('holdings',['资产','市值 / 占比','浮动盈亏'],[['现金',money(a.cash_cents)+' / '+percent(p.cash_weight_pct),'—'],...p.holdings.map(h=>[h.name+(h.origin==='global'?'（现货 / 美股）':h.origin==='dynamic'?'（动态）':'（观察栏）')+' · '+h.qty+' 股（可卖 '+h.sellable_qty+'）',money(h.market_value_cents)+' / '+percent(h.weight_pct),signed(h.unrealized_cents)+(h.valuation_basis==='COST_FALLBACK'?'（缺少报价）':'')])]);
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
    if(job.kind==='portfolio_strategy')return ['本轮组合判断未完成；原退出检查继续，新增买入须等待有效授权。','error'];
    if(job.kind==='review'){
      let result;try{result=JSON.parse(job.result_json||'null');}catch(_){result=null;}
      return ['持仓盈亏已核算，逐仓分析待补齐。'+(result?.analysis_error||'请查看复盘卡片中的原因。'),'error'];
    }
    return ['本次研究重试后仍未完成；有效旧计划保留，请查看该股票失败项中的处理办法。','error'];
  }
  if(job.status==='INTERRUPTED')return ['任务因服务重启而中断，请查看后续恢复任务。','error'];
  let r;try{r=JSON.parse(job.result_json||'null');}catch(_){r=null;}
  if(job.status!=='DONE')return [label(job.kind)+'：'+label(job.status),''];
  if(job.kind==='portfolio_strategy')return ['组合判断已更新，目标仓位与调整依据见观察栏的组合策略。',''];
  if(job.kind==='dynamic_cycle')return [r?.research_deferred||('动态采集与研究已结束；'+(r?.failures?.length?'部分来源待恢复，详情见动态板块。':'没有达标机会时继续积累样本。')),r?.failures?.length?'error':''];
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
    if(state?.deployment_role==='cloud'&&!['slot','dynamic_slot','global_slot'].includes(kind))b.title='由本地研究端按计划执行';
    b.textContent=job?.status==='PENDING'?'排队中…':busy?(research?'更新中…':'处理中…'):b.dataset.idleLabel;
  });
  for(const id of ['save','toggle','dynamic-toggle'])if($(id)&&!$(id).dataset.saving)$(id).disabled=!connected||state?.deployment_role==='cloud';
}
function drawDailyReview(body,r) {
  const facts=r.payload.facts,analysis=r.payload.analysis||{},p=facts.portfolio;
  body.append(el('p',shortTime(r.window_start)+' — '+shortTime(r.window_end),'subtle'));
  if(p){
    if(facts.daily_accounting)body.append(el('p','本日24小时盈亏 '+signed(facts.daily_accounting.totals.period_profit_cents)+' 元 · 以下逐仓回看前48小时。','subtle'));
    const t=p.totals,metrics=el('div',null,'review-metrics');
    for(const [title,value] of [[facts.context_48h?'48小时盈亏':'本期盈亏',t.period_profit_cents],['截止累计盈亏',t.cumulative_profit_cents],['持仓浮动盈亏',p.closing.unrealized_cents],['累计已实现',p.closing.cumulative_realized_cents]]){
      const metric=el('div');metric.append(el('span',title,'subtle'),el('strong',signed(value)+' 元',value>0?'profit':value<0?'loss':''));metrics.append(metric);
    }
    body.append(metrics,el('p','截止持有 '+t.holding_count+' 个标的 · 复盘 '+t.reviewed_position_count+' 个标的（含期间平仓） · 本期 '+t.period_fill_count+' 笔成交','subtle'));
    if(!p.opening.valuation_complete||!p.closing.valuation_complete)body.append(el('p','部分报价缺失、过时或未到收盘；相关盈亏可能不完整，请结合各仓行情时间阅读。','attention'));
    if(p.positions.some(x=>x.opening.late_quote||x.closing.late_quote))body.append(el('p','本版含事后补齐的历史行情，按行情发生时间重算；这些价格不代表当时决策已知。','subtle'));
    if(analysis.summary)body.append(el('p',analysis.summary,'review-summary'));
    const verdicts={SUPPORTED:'观点获得支持',CONTRADICTED:'观点已被反证',MIXED:'部分支持，部分反证',PENDING:'仍待验证',INSUFFICIENT:'研究依据不足'};
    const qualities={RECENT:'截止前有效报价',CLOSE:'收盘后报价',INTRADAY_LAST:'盘中末次报价',STALE:'历史旧报价',MISSING:'报价缺失',NO_POSITION:'已无持仓'};
    for(const position of p.positions){
      const c=position.closing,a=(analysis.positions||[]).find(x=>x.position_key===position.key),card=el('article',null,'review-position');
      const head=el('div',null,'section-head');head.append(el('h3',position.name+' · '+position.symbol),el('span',a?verdicts[a.verdict]:'逐仓分析待补齐','badge'));card.append(head);
      card.append(el('p','截止 '+(c.qty/(c.qty_scale||1)).toLocaleString('zh-CN',{maximumFractionDigits:8})+' 股 / 份 · 买入均价 '+costPrice(c.average_cost_cents)+' 元 · 截止报价 '+money(c.price_cents)+' 元','subtle'));
      card.append(el('p','本期 '+signed(position.period_profit_cents)+' 元 · 浮动 '+signed(c.unrealized_cents)+' 元 · 累计已实现 '+signed(position.cumulative_realized_cents)+' 元'));
      if(c.qty)card.append(el('p',(qualities[c.quality]||c.quality)+' '+(c.quote_at?shortTime(c.quote_at):'')+(c.late_quote?' · 补录于 '+shortTime(c.quote_first_seen_at):''),'subtle'));
      if(a){card.append(el('p',a.reason));addList(card,'获得支持',a.supported_points);addList(card,'被反证',a.contradicted_points);addList(card,'仍待验证',a.pending_points);card.append(el('p','下一步验证：'+a.next_check));}
      const records=(p.research||[]).filter(x=>position.research_ids.includes(x.id));
      if(records.length){const d=details('核对当时研究','review-research:'+r.id+':'+position.key);for(const record of records){
        d.append(el('h4',(record.role==='ENTRY'?'买入依据':'截止前跟踪研究')+' · '+shortTime(record.at)));
        const source=record.analysis||{},decision=source.decision||{};
        d.append(el('p',source.analysis||source.mechanism||record.plan?.thesis||'原始研究未提供文字摘要。'));
        if(decision.trigger)d.append(el('p','触发条件：'+decision.trigger));if(decision.invalidation)d.append(el('p','反证条件：'+decision.invalidation));
      }card.append(d);}body.append(card);
    }
    body.append(el('p','本期盈亏 = 本期已实现 + 期末浮动 − 期初浮动，已计成交费用。短期盈亏不足以单独证明研究有效。','subtle'));
  }else{
    const stats=facts.statistics||{};
    body.append(el('p',analysis.summary||'旧版仅保存交易统计。'),el('p',(stats.fill_count||0)+' 笔成交 · 已实现 '+signed(stats.realized_pnl_cents)+' 元','subtle'));
  }
  if(facts.context_48h){const c=facts.context_48h,d=details('前48小时持仓回看','review-48:'+r.id);d.append(el('p',shortTime(c.window_start)+' — '+shortTime(c.window_end),'subtle'),el('p','48小时盈亏 '+signed(c.totals.period_profit_cents)+' 元 · '+c.positions.length+' 个标的'));c.positions.forEach(x=>d.append(el('p',x.name+' · '+signed(x.period_profit_cents)+' 元')));d.append(el('p','滚动窗口可能重叠，每日收益按上方24小时单独统计。','subtle'));body.append(d);}
  if(r.model_status==='DEFERRED')body.append(el('p','盈亏事实已保存，逐仓分析尚未完成。'+(r.payload.analysis_error||'模型未能完成本次分析。')+(r.automatic_retries_remaining?'系统会在空闲时重试，最多剩余 '+r.automatic_retries_remaining+' 次。':'可点击“更新复盘”重试最新周期。'),'attention'));
  else if(r.model_status==='NOT_RUN')body.append(el('p','盈亏核算已完成，模型分析尚未运行。','subtle'));
  const lessons=analysis.lessons||[];if(lessons.length){const d=details('本次经验（待验证）','lessons:'+r.id);lessons.forEach(l=>d.append(el('p',l.lesson)));body.append(d);}
  body.append(el('p','第 '+r.revision+' 版 · 生成于 '+shortTime(r.ready_at),'subtle'));
}
function renderActivity(s) {
  const market=s.market_phase==='CONTINUOUS';
  const nextSlot=s.next_runs.find(r=>r.kind==='slot');
  $('slot-timing').textContent=(market?'交易时段':'当前休市')+(nextSlot?' · 下次检查 '+shortTime(nextSlot.scheduled_at):'');
  const reviewNext=s.next_runs.find(r=>r.kind==='review');
  $('review-timing').textContent='每日核算盈亏 · 回看前48小时持仓与研究依据'+(reviewNext?' · '+shortTime(reviewNext.scheduled_at)+' 自动更新':'');
  renderChanged('decision-summary',s.decisions.slice(0,1),box=>{
    const d=s.decisions[0];if(!d){box.append(el('p','尚无买卖操作。每只股票的最近检查结果见上方，无操作检查不逐次保存。','empty'));return;}
    box.append(el('p',(s.watchlist.find(w=>w.symbol===d.symbol)?.name||d.symbol)+' · '+label(d.action)+' · '+label(d.status)),el('p',decisionReason(d.reason),'subtle'),el('p',shortTime(d.at),'subtle'));
  });
  table('decisions',['时间','股票 / 操作','提交结果','依据'],s.decisions.map(d=>[shortTime(d.at),d.symbol+' '+label(d.action),label(d.status),decisionReason(d.reason)]));
  table('fills',['时间','股票 / 方向','股数 / 均价','费用 / 已实现盈亏'],s.fills.map(f=>[shortTime(f.occurred_at),f.symbol+' '+label(f.side),f.qty+' / '+money(f.price_cents),money(f.fee_cents)+' / '+(f.side==='SELL'?signed(f.realized_cents):'尚未卖出')]));
  renderChanged('reviews',s.reviews,box=>{
    if(!s.reviews.length){box.append(el('p','还没有复盘记录，首次复盘完成后显示。','empty'));return;}
    s.reviews.forEach((r,index)=>{const body=el('div',null,'review');drawDailyReview(body,r);
      if(!index)box.append(body);else{const det=details('较早复盘 · '+shortTime(r.window_end),'review:'+r.id);det.append(body);box.append(det);}
    });
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
  if(staleHeartbeat){$('service').textContent='自动任务状态需要检查';$('service').className='problem';attention.append(document.createTextNode('后台调度暂未响应。现有结果仍可查看，'+(currentUser?.role==='GUEST'?'请联系管理员检查。':'请检查运行详情。')),link('查看状态','/admin/settings#diagnostics'));}
  else if(!s.scheduler_enabled)attention.append(document.createTextNode(currentUser?.role==='GUEST'?'自动运行已暂停，等待管理员恢复。':'自动运行已暂停。需要继续定时更新时，请在“自选股与自动运行”中开启。'));
  else if(latestIssue)attention.append(document.createTextNode('最近一次'+label(latestIssue.kind)+'未完成，现有结果已保留。'),link('查看任务原因','/admin/settings#diagnostics'));
  if(s.account.risk?.halted)attention.append(el('p','账户回撤达到25%风控阈值：已停止新买入、撤销未成交买单；在市场允许时逐步平仓。恢复买入需重新确认。'));
  const risk=$('portfolio-risk');if(risk){risk.hidden=!s.account.investment_policy;risk.textContent='无杠杆模拟 · 持有周期按天 · 每小时复核'+(s.account.risk?.drawdown_bps!=null?' · 当前回撤 '+(s.account.risk.drawdown_bps/100).toFixed(2)+'%':'')+' / 风控阈值25%';}
  if(s.research_lease){const r=s.research_lease;attention.append(el('p',r.active?'本地研究已同步 · 最近完成 '+when(r.completed_at):'本地研究心跳已过期或尚未就绪：暂停新买入，继续持仓风险检查。',r.active?'subtle':'caution'));}
  attention.hidden=!attention.textContent;
  renderWatchlist(s);renderAccount(s);renderActivity(s);renderFollowups(s);renderDynamic(s);renderPortfolioStrategy(s);
  if(!editing)$('watchlist').value=s.watchlist.map(w=>w.symbol+' '+w.name).join('\n');
  $('settings-summary').textContent=s.watchlist.length+' 只股票 · '+(s.scheduler_enabled?'自动运行已开启':'已暂停');
  $('schedule').textContent=scheduleSummary(s.schedule);
  $('toggle').textContent=s.scheduler_enabled?'暂停自动运行':'开启自动运行';
  $('diagnostic-summary').textContent=active.length?active.length+' 项任务处理中':'暂无进行中的任务';
  $('technical-state').textContent='服务版本 '+s.version+' · 最近心跳 '+when(s.state.heartbeat)+' · '+s.documents+' 份资料'+(s.state.last_error?' · 最近异常：'+s.state.last_error:'');
  table('jobs',['计划时间','任务','结果'],s.jobs.map(j=>[when(j.scheduled_at),label(j.kind),j.error||jobMessage(j)[0]]));
  table('sources',['来源','状态','详情'],[...(s.background_failures||[]).map(f=>[f.label+(f.title?' · '+f.title:''),'待恢复',f.reason+' '+f.impact]),...s.source_checks.map(c=>[c.source+(c.symbol?' '+c.symbol:''),label(c.status),c.detail])]);
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
const noticeAuthors={agent:'运维助手',claude:'Claude',program:'程序'};
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
  $('notice-title').textContent=n.title;
  $('notice-meta').textContent=when(n.created_at)+' · 来自'+(noticeAuthors[n.author]||n.author)+(n.deadline?' · 请在 '+when(n.deadline)+' 前处理':'');
  $('notice-body').textContent=n.body;
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
async function saveSettings(button,body,target,success) {
  button.dataset.saving='true';button.disabled=true;feedback(target,'正在保存…','pending');
  try{await api.post('/api/settings',body);if(body.watchlist)editing=false;feedback(target,success);await refresh();}
  catch(error){feedback(target,error.message,'error');}
  finally{delete button.dataset.saving;button.disabled=!connected;}
}
function selectBoard(board,{focus=false,remember=false}={}) {
  for(const name of ['watchlist','dynamic']) {
    const selected=name===board,tab=$(name+'-tab');
    tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;
    $(name+'-section').hidden=!selected;
    if(selected&&focus)tab.focus();
  }
  if(remember)history.replaceState(history.state,'','#'+board);
}
function initBoardTabs() {
  if(location.pathname!=='/'||!$('watchlist-tab'))return;
  const boards=['watchlist','dynamic'];
  const fromHash=()=>selectBoard(['#dynamic','#dynamic-section'].includes(location.hash)?'dynamic':'watchlist');
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
}
async function boot() {
  try{const session=await api.get('/api/session');if(!session.authenticated){location.replace('/login?next='+encodeURIComponent(location.pathname));return;}currentUser=session.user;document.body.dataset.role=currentUser.role;initPages();initBoardTabs();}catch(e){showRefreshProblem(e);if(e.kind!=='AUTH')setTimeout(boot,10000);return;}
  document.querySelectorAll('a[href^="#"]').forEach(a=>a.addEventListener('click',()=>{const target=$(a.getAttribute('href').slice(1));if(target?.tagName==='DETAILS')target.open=true;else if(target?.id==='settings-section')target.querySelector('details').open=true;}));
  document.querySelectorAll('[data-run]').forEach(b=>{b.dataset.idleLabel=b.textContent;b.addEventListener('click',()=>runAction(b));});
  $('watchlist').addEventListener('input',()=>{editing=true;});
  $('notice-dialog')?.addEventListener('cancel',()=>{if(noticeShown)noticeLater.add(noticeShown);noticeShown=null;});
  $('save').addEventListener('click',()=>{
    const watchlist=$('watchlist').value.trim().split('\n').filter(v=>v.trim()).map(v=>{const [symbol,...name]=v.trim().split(/\s+/);return {symbol,name:name.join(' ')||symbol};});
    if(!watchlist.length){feedback('settings-feedback','请至少填写一只自选股。','error');return;}
    saveSettings($('save'),{watchlist},'settings-feedback','自选股已保存，下次研究使用新名单。');
  });
  $('dynamic-toggle').addEventListener('click',()=>{if(state?.dynamic)saveSettings($('dynamic-toggle'),{dynamic_enabled:!state.dynamic.enabled},'dynamic-feedback',state.dynamic.enabled?'动态发现已暂停，已有动态持仓继续管理。':'已开启动态发现。');});
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
const observationCategories={CN:'A股',US:'美股',COMMODITY:'商品与贵金属'};
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
  observationCategory=observationCategories[category]?category:'CN';selectBoard('watchlist',{remember:true});if(state)renderWatchlist(state);
  const member=state?.observation?.membership?.[asset];
  if(member&&['COOLING','ARCHIVED'].includes(member.tier)){if(!await loadObservationArchive(0,asset))return;}
  const target=$('observed-'+asset);if(target){target.scrollIntoView({block:'center',behavior:'smooth'});target.focus({preventScroll:true});}
}
function showMacroEvent(id){
  selectBoard('dynamic',{remember:true});const target=$('macro-event-'+id);
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
function drawImpactAssessment(parent,value,key){
  const v=value||{state:'PENDING',admitted:false,reason:'等待独立影响评估，暂不占用活跃名额'};
  const block=el('section',null,'impact-assessment');block.setAttribute('aria-label','独立影响评估');
  const labels={PENDING:'待评估',BACKGROUND:'背景资料',NEEDS_EVIDENCE:'待补证据',HISTORICALLY_WEAK:'历史反应偏弱',ADMITTED:'通过影响评估'};
  block.append(el('p',labels[v.state]||'待评估','impact-assessment-title'),el('p',v.reason,'subtle'));
  const a=v.assessment,h=v.history;
  if(a){
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
  const causes=details('动态影响 · '+target.links.length+' 条事件','observed:'+target.asset);
  if(target.links.length&&target.status==='NEEDS_REVIEW')causes.append(el('p','相关原文或结论已修订，原影响判断需要复核。','caution'));
  if(target.conflicting)causes.append(el('p','不同事件给出相反方向，请分别查看成立条件。','caution'));
  for(const cause of target.links){
    const article=el('section',null,'observation-cause');article.append(el('h4',cause.headline),el('p',shortTime(cause.published_at)+' · '+(cause.status==='INVALIDATED'?'原关联待复核':macroDirections[cause.impact.direction]),'subtle'));
    if(cause.review_state==='DUE'||cause.review_state==='EXPIRED')article.append(el('p',cause.review_state==='DUE'?'此事件已到复核期限，等待新证据。':'此事件的观察期限已结束。','caution'));
    drawImpactAssessment(article,cause.materiality,'observed:'+target.asset+':'+cause.event_id);
    drawLogicChain(article,cause.impact,'observed:'+target.asset+':'+cause.event_id);
    article.append(link('查看新闻原文',cause.url));
    if([...(state?.dynamic?.global?.items||[]),...(state?.dynamic?.global?.archived_items||[])].some(e=>e.id===cause.event_id)){
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
  const title=expired?'研究已过期，等待更新':!p?'等待研究形成交易计划':blockers.length?'等待：'+blockers.slice(0,2).join('；'):pp.kind==='PAPER_TRADE'?(h?'按策略持续检查':'等待价格与资金条件'):'当前研究暂不买入';
  decision.append(el('strong',title,'caution'),el('p',p?'研究 '+shortTime(p.created_at):'等待首次研究','subtle'),el('p','上一次交易策略更新时间：'+(target.last_strategy_updated_at?shortTime(target.last_strategy_updated_at):'尚未发布'),'subtle'));
  row.append(name,holding,quote,buy,sell,decision);
  const summaryRow=el('tr',null,'watchlist-summary-row'),cell=el('td'),research=details('研究逻辑与动态影响','watchlist-research:'+target.asset);cell.colSpan=6;
  if(pp){research.append(el('p',pp.thesis,'watchlist-status-summary'),el('p','持有参考 '+pp.holding_days+' 天 · 每小时复核 · 研究有效至 '+shortTime(p.valid_until),'subtle'));}
  else research.append(el('p','等待本轮研究形成交易计划。','subtle'));
  if(target.recheck)research.append(el('p','最近复核 '+shortTime(target.recheck.checked_at)+(blockers.length?' · '+blockers.join('；'):''),'subtle'));
  if(q)research.append(el('p','美元兑人民币 '+(q.fx_micros/1e6).toFixed(4)+' · '+shortTime(q.fx_at),'subtle'));
  if(target.indicator)research.append(el('p','历史指标 '+indicatorText(target)+' · '+target.indicator.date,'subtle'));
  drawPoolStatus(research,target);drawObservationCauses(research,target);cell.append(research);summaryRow.append(cell);
  const group=el('tbody');group.id='observed-'+target.asset;group.tabIndex=-1;group.setAttribute('aria-label',target.name);group.append(row,summaryRow);table.append(group);
}
function renderWatchlist(s){
  if(!observationCategories[observationCategory])observationCategory='CN';
  const observed=s.observation?.items||[],manual=new Set(s.watchlist.map(w=>w.symbol));
  renderChanged('observation-categories',[observed.map(t=>[t.asset,t.category]),s.watchlist.map(w=>w.symbol),observationCategory],box=>{
    Object.entries(observationCategories).forEach(([category,title],index)=>{
      const count=observed.filter(t=>t.category===category&&!manual.has(t.asset)).length+(category==='CN'?manual.size:0);
      const b=el('button',title+' '+count);b.type='button';b.id='category-'+category;b.setAttribute('role','tab');b.setAttribute('aria-selected',String(category===observationCategory));b.setAttribute('aria-controls','stocks');b.tabIndex=category===observationCategory?0:-1;
      b.addEventListener('click',()=>{observationCategory=category;renderWatchlist(state||s);});
      b.addEventListener('keydown',event=>{const keys=Object.keys(observationCategories),next=event.key==='ArrowRight'?(index+1)%keys.length:event.key==='ArrowLeft'?(index+keys.length-1)%keys.length:event.key==='Home'?0:event.key==='End'?keys.length-1:null;if(next!==null){event.preventDefault();observationCategory=keys[next];renderWatchlist(state||s);$('category-'+keys[next]).focus();}});box.append(b);
    });
  });
  const pool=s.observation?.counts,limits=s.observation?.policy;
  $('observation-pool-summary').textContent=pool?(limits.total_limit?'观察 '+pool.total+' / '+limits.total_limit+' · ':'')+'动态活跃 '+pool.active+' / '+limits.active_limit+' · 重点研究 '+pool.focus+' / '+limits.focus_limit+(pool.protected_overflow?' · 持仓与委托保护超额 '+pool.protected_overflow+' 个，暂停其他新增':''):'';
  renderObservationArchive(observationArchiveOffset&&observationArchiveData?observationArchiveData:s.observation);
  $('stocks').setAttribute('aria-labelledby','category-'+observationCategory);
  $('last-updated').hidden=observationCategory!=='CN';$('update-coverage').hidden=observationCategory!=='CN';
  for(const id of ['followup-section','watchlist-performance'])if($(id))$(id).hidden=observationCategory!=='CN';
  $('activity-section').hidden=false;
  $('watchlist-refresh-button').hidden=false;
  $('watchlist-refresh-button').dataset.run=observationCategory==='CN'?'cycle':'global_research';
  $('watchlist-note').hidden=false;
  $('watchlist-note').textContent=observationCategory==='CN'?'单位：元。成本价为当前持仓的平均买入成本（含买入费用）。盈亏按已取得的最近报价计算；参考区间用于观察。':'持仓价值与浮动盈亏以人民币显示；现价、成本价及买卖区间使用标注的原币单位。较成本变动按原币计算，人民币盈亏还受汇率影响。';
  $('research-timing').textContent=s.watchlist.length+' 只原自选股 · '+observed.filter(t=>!manual.has(t.asset)&&!t.fixed).length+' 个动态发现标的'+(observed.some(t=>t.fixed)?' · 4 个固定现货资产':'');
  renderChanged('stocks',[s.watchlist,s.portfolio.holdings,s.market_phase,s.next_runs,observed,observationCategory,[...s.watchlist,...observed].map(item=>quoteIsStale(item.quote,s))],box=>{
    const holdings=new Map(s.portfolio.holdings.filter(h=>h.origin!=='dynamic').map(h=>[h.symbol,h]));
    const discoveries=new Map(observed.map(t=>[t.asset,t]));
    if(observationCategory!=='CN'){
      const targets=observed.filter(t=>t.category===observationCategory).sort((a,b)=>(b.position?.market_value_cents||0)-(a.position?.market_value_cents||0)||(a.pool_tier!=='FOCUS')-(b.pool_tier!=='FOCUS')||(b.priority_score||0)-(a.priority_score||0)||(a.fixed&&b.fixed?['GOLD','SILVER','BTC','ETH'].indexOf(a.asset)-['GOLD','SILVER','BTC','ETH'].indexOf(b.asset):0));
      const table=watchlistTable();
      if(!targets.length){const body=el('tbody'),row=el('tr'),cell=el('td');cell.colSpan=6;cell.append(el('p','暂未发现需要跟踪的'+observationCategories[observationCategory]+'标的。新事件形成影响结论后会自动加入。','empty'));row.append(cell);body.append(row);table.append(body);}
      targets.forEach(target=>drawGlobalWatchlistRow(table,target,s));box.append(table);return;
    }
    const extra=observed.filter(t=>t.category==='CN'&&!manual.has(t.asset)).map(t=>({symbol:t.asset,name:t.name,quote:t.quote,discovery:t}));
    for(const item of extra){const h=s.portfolio.holdings.find(h=>h.origin==='dynamic'&&h.symbol===item.symbol);if(h)holdings.set(item.symbol,h);}
    const value=item=>holdings.get(item.symbol)?.market_value_cents??0;
    const ordered=[...s.watchlist,...extra].sort((a,b)=>value(b)-value(a));
    const t=watchlistTable();
    for(const item of ordered){const row=el('tr'),name=el('td'),holding=el('td',null,'holding-value'),quote=el('td'),buy=el('td'),sell=el('td'),decision=el('td');
      const h=holdings.get(item.symbol),p=item.plan,l=p?.payload?.levels,discovery=discoveries.get(item.symbol),v=item.discovery?{title:discovery.status==='NEEDS_REVIEW'?'影响待复核':macroDirections[discovery.direction],reason:'动态发现标的，持续观察事件条件与后续证据。',ready:false}:researchDecision(item,s.market_phase);
      name.append(item.discovery?el('strong',item.name):pageLink(item.name,'/stocks/'+item.symbol),el('p',item.symbol,'subtle'),el('p',h?h.qty+' 股 · '+percent(h.weight_pct):'未持仓','subtle'));
      if(item.discovery)name.append(el('span',poolTierLabels[discovery.pool_tier]||'动态发现','badge'));
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
      decision.append(el('strong',v.title,v.ready?'ready':'caution'),el('p',p?'研究 '+shortTime(p.activated_at):item.discovery?'研究 '+shortTime(discovery.updated_at):'等待首次研究','subtle'));
      decision.append(el('p','上一次交易策略更新时间：'+(item.last_strategy_updated_at||discovery?.last_strategy_updated_at?shortTime(item.last_strategy_updated_at||discovery.last_strategy_updated_at):'尚未发布'),'subtle'));
      row.className='watchlist-values';row.append(name,holding,quote,buy,sell,decision);
      const summaryRow=el('tr',null,'watchlist-summary-row'),summaryCell=el('td');summaryCell.colSpan=6;
      const research=details('研究逻辑'+(discovery?'与动态影响':''),'watchlist-research:'+item.symbol);
      research.append(el('p',overviewSummary(item,v),'watchlist-status-summary'));summaryRow.append(summaryCell);
      if(!item.discovery)summaryCell.append(el('p',executionSummary(item,s.next_runs?.find(r=>r.kind==='slot')),'subtle'));
      if(discovery){drawPoolStatus(research,discovery);drawObservationCauses(research,discovery);}summaryCell.append(research);
      const group=el('tbody');group.id='observed-'+item.symbol;group.tabIndex=-1;group.setAttribute('aria-label',item.name);group.append(row,summaryRow);t.append(group);
    }box.append(t);
  });
}
async function refreshStock(){
  const data=await api.get('/api/stock?symbol='+encodeURIComponent(selectedSymbol)+'&offset='+historyOffset);stockData=data;
  $('opinion-context').textContent='关于 '+data.stock.name+' · '+(data.stock.plan?'研究 '+shortTime(data.stock.plan.activated_at):'尚无研究');
  renderChanged('stock-content',[data.stock,data.position,data.dimensions,data.followups,data.trade_statistics],box=>{
    const item=data.stock,p=item.plan,l=p?.payload?.levels,report=item.report||{},verdict=researchDecision(item),h=data.position;
    document.title=item.name+' · ThesisTrade';
    const top=el('section',null,'panel stock-summary'),heading=el('div',null,'stock-head'),name=el('div'),q=el('div',null,'quote');name.append(el('h1',item.name),el('span',item.symbol,'subtle'));q.append(el('strong',money(item.quote?.price_cents)+' 元'),el('p','报价 '+shortTime(item.quote?.observed_at)));heading.append(name,q);
    top.append(heading,el('p',verdict.title,'verdict'+(verdict.ready?' ready':'')),el('p',verdict.reason));
    if(l){const levels=el('div',null,'levels');[['买入参考区间',money(l.buy_low_cents)+' – '+money(l.buy_high_cents)],['卖出参考',money(l.sell_cents)],['止损参考',money(l.stop_cents)]].forEach(([title,value])=>{const d=el('div');d.append(el('span',title),el('b',value));levels.append(d);});top.append(levels);}
    top.append(el('p',p?'研究更新 '+shortTime(p.activated_at)+' · 有效至 '+shortTime(p.valid_until)+' · '+label(p.effective_status):'尚无研究结论','subtle'));
    top.append(el('p','上一次交易策略更新时间：'+(item.last_strategy_updated_at?when(item.last_strategy_updated_at):'尚未发布'),'subtle'));
    top.append(el('p',executionSummary(item,null),'subtle'));
    if(item.latest_study&&item.latest_study.id!==p?.study_id)top.append(el('p','最新尝试'+label(item.latest_study.model_status)+'。以下保留上次形成的结论，价位是否有效以当前状态为准。','attention'));
    const stats=el('div',null,'position-summary');stats.append(el('p',h?'持仓 '+h.qty+' 股 · 可卖 '+h.sellable_qty+' 股 · 仓位 '+percent(h.weight_pct):'当前未持仓'));if(h){drawPositionCost(stats,h);stats.append(el('p','成本含已分摊买入费用；盈亏按当前剩余持仓计算。','subtle'));}stats.append(el('p',h?'持仓市值 '+money(h.market_value_cents)+' 元 · 浮动盈亏 '+signed(h.unrealized_cents)+' 元':'等待研究与盘面条件满足后建立模拟持仓。'),el('p','累计 '+data.trade_statistics.fill_count+' 笔成交 · 已实现 '+signed(data.trade_statistics.realized_cents)+' 元 · 交易费用 '+money(data.trade_statistics.fee_cents)+' 元','subtle'));top.append(stats);box.append(top);
    const research=el('section',null,'panel');research.append(el('h2','最新研究结论'),el('p',report.analysis||'尚未形成完整研究，暂不能判断买卖条件。','research-summary'));renderDecisionCard(research,report.decision,item.symbol);
    if(data.dimensions.some(d=>d.origin==='EXISTING_REPORT'))research.append(el('p','以下分项根据当前研究及财务底稿整理。独立分项分析将在下次研究后更新。','subtle'));
    for(const d of data.dimensions){const section=details(d.title,'dimension:'+d.id);section.className='dimension';section.append(el('p',d.summary));if(d.uncertainty)section.append(el('p','待确认 / 判断边界：'+d.uncertainty,'subtle'));for(const evidence of d.evidence_ids||[]){const fact=report.facts?.find(f=>f.evidence_id===evidence);const line=el('p');if(fact)line.append(el('span',fact.quote+' '));line.append(link('查看依据','/api/document?id='+encodeURIComponent(evidence.split(':')[0])));section.append(line);}research.append(section);}
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
  const route=location.pathname,id=selectedSymbol?'stock-page':route==='/admin/feedback'?'admin-feedback-page':route==='/admin/settings'?'admin-settings-page':'home-page';$(id).hidden=false;$('opinion-board').hidden=route.startsWith('/admin/');if(route.startsWith('/admin/'))document.querySelector('.app-layout').classList.add('single-column');$('current-user').textContent=currentUser.role==='ADMIN'?'Admin':'Guest';
  $('logout').addEventListener('click',async()=>{try{await api.post('/api/logout',{});location.replace('/login');}catch(e){$('attention').textContent=e.message;$('attention').hidden=false;}});
  for(const [button,delta] of [['history-prev',-30],['history-next',30]])$(button).addEventListener('click',async()=>{historyOffset=Math.max(0,historyOffset+delta);try{await refreshStock();}catch(e){$('attention').hidden=false;$('attention').textContent=e.message;}});
  $('opinion-form').addEventListener('submit',async e=>{e.preventDefault();const b=e.currentTarget.querySelector('button');b.disabled=true;const body={page:selectedSymbol?'stock':'home',symbol:selectedSymbol||null,study_id:stockData?.stock.plan?.study_id||null,nickname:$('opinion-name').value,topic:$('opinion-topic').value,body:$('opinion-body').value};const signature=JSON.stringify([body.page,body.symbol,body.nickname,body.topic,body.body]);if(opinionAttempt?.signature!==signature)opinionAttempt={signature,id:crypto.randomUUID(),body};feedback('opinion-feedback','正在提交…','pending');try{await api.post('/api/feedback',{...opinionAttempt.body,request_id:opinionAttempt.id});$('opinion-body').value='';opinionAttempt=null;feedback('opinion-feedback','意见已送达，仅管理员可见。');}catch(e){feedback('opinion-feedback',e.message+' 再次提交会使用同一标识，避免重复。','error');}finally{b.disabled=false;}});
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
  detail.append(el('p','本线路单次仓位上限 '+(c.plan.max_position_pct||5)+'%；持有期最多3个交易日，并检查止损和止盈。'));
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
  $('dynamic-count').textContent='近48小时 · '+(d.global?.items.filter(macroProminent).length||0)+' 个事件';
  renderGlobalMacro(d);
  $('dynamic-toggle').textContent=d.enabled?'暂停动态发现':'开启动态发现';
  const job=s.active_jobs?.find(j=>j.kind==='dynamic_cycle')||d.last_run?.status==='RUNNING';
  $('dynamic-timing').textContent=(job?'正在更新 · ':!d.enabled?'动态发现已暂停 · ':!s.scheduler_enabled?'自动调度已暂停 · ':'每30分钟更新 · ')+((d.last_finished_at||d.last_run?.finished_at)?'上次完成 '+shortTime(d.last_finished_at||d.last_run.finished_at):'等待首次采集完成')+(d.next_at?' · 下次 '+shortTime(d.next_at):'');
  renderChanged('dynamic-health',[d.last_run?.details?.failures,d.last_run?.details?.identity_failures,d.last_run?.details?.coverage,d.last_run?.details?.research_deferred,d.last_run?.details?.global_research?.summary,d.state.quote_error],box=>{
    if(d.last_run?.details?.global_research?.summary)box.append(el('p',d.last_run.details.global_research.summary,'subtle'));
    const failures=d.last_run?.details?.failures||[];
    if(failures.length){const summary=details('部分动态来源待恢复（'+failures.length+'）','dynamic-failures');failures.forEach(f=>summary.append(el('p',f.includes('CERTIFICATE_VERIFY_FAILED')?f.split('：')[0]+'：来源证书暂未通过验证，稍后重试。':f,'subtle')));box.append(summary);}
    if(d.last_run?.details?.coverage?.sina_complete===false)box.append(el('p','48小时新闻窗口尚未完整回补，后续继续采集。','caution'));
    for(const failure of d.last_run?.details?.identity_failures||[])box.append(el('p',failure+'；未核验标的暂不加入观察。','subtle'));
    if(d.last_run?.details?.research_deferred)box.append(el('p',d.last_run.details.research_deferred,'subtle'));
    if(d.state.quote_error)box.append(el('p','动态行情暂未更新：'+d.state.quote_error,'caution'));
  });
  renderChanged('dynamic-budget',d.balance,box=>{
    box.append(el('span','动态持仓市值 '+money(d.balance.market_value_cents)+' 元'),el('span','动态挂单占用 '+money(d.balance.reserved_cents)+' 元'));
  });
  renderChanged('dynamic-cases',active,box=>{
    if(!active.length){box.append(el('p',d.last_run?'目前没有通过全部执行条件的计划。全球研究结论会持续跟踪，具备适配和证据的标的才进入模拟交易。':'暂无模拟执行计划。','empty'));return;}
    active.forEach(c=>drawDynamicCase(box,c));
  });
  renderChanged('dynamic-news',d.recent_news,box=>{d.recent_news.forEach(n=>{const row=el('p',null,'dynamic-news-row');row.append(link(n.title,n.url),el('span',n.source+' · '+shortTime(n.published_at),'subtle'));box.append(row);});});
  table('dynamic-fills',['时间','标的','操作','成交','费用'],d.fills.map(f=>[shortTime(f.occurred_at),f.symbol,label(f.side),f.qty+' 股 × '+money(f.price_cents),money(f.fee_cents)]),'动态线路暂无成交。');
  const archived=d.items.filter(c=>!active.includes(c));
  renderChanged('dynamic-archive',archived,box=>{if(!archived.length)box.append(el('p','暂无已结束事件。','empty'));else archived.forEach(c=>drawDynamicCase(box,c));});
}

const macroRegions={GLOBAL:'全球',US:'美国',EUROPE:'欧洲',JAPAN:'日本',CHINA:'中国',ASIA:'亚洲',MIDDLE_EAST:'中东',OCEANIA:'大洋洲',LATIN_AMERICA:'拉丁美洲',AFRICA:'非洲',EMERGING:'新兴市场'};
const macroDirections={UP:'上行压力',DOWN:'下行压力',MIXED:'双向影响',UNCLEAR:'方向待确认'};
function drawMacroEvent(box,event,assets){
  const a=event.analysis,card=el('article',null,'macro-card'),head=el('div',null,'section-head');
  card.id='macro-event-'+event.id;card.tabIndex=-1;
  head.append(el('h3',a.headline),el('span',event.status==='INVALIDATED'?'原文已修订':event.theme_label,'badge'));card.append(head);
  card.append(el('p',a.regions.map(r=>macroRegions[r]||r).join(' · ')+' · '+({DAYS:'未来数日',MONTHS:'未来数月',UNCERTAIN:'期限待确认'}[a.horizon])+' · 新闻发布 '+shortTime(event.published_at),'subtle'));
  card.append(el('p',a.facts,'macro-facts'),el('h4','受影响标的与方向','macro-impact-heading'));
  const impacts=el('ul',null,'macro-impacts');impacts.setAttribute('aria-label','受影响标的与方向');
  for(const impact of a.impacts){
    const row=el('li',null,'macro-impact'),title=el('div',null,'section-head'),direction=el('span',macroDirections[impact.direction],'macro-direction');direction.dataset.direction=impact.direction;
    title.append(el('strong',assets[impact.asset]?.name||impact.asset),direction);row.append(title,el('p','初步推演：'+(impactStrengths[impact.strength]||impactStrengths.UNKNOWN),'impact-strength'));
    drawImpactAssessment(row,impact.materiality,'macro:'+event.id+':'+impact.asset);
    drawLogicChain(row,impact,'macro:'+event.id+':'+impact.asset);
    const observed=state?.observation?.items.find(t=>t.asset===impact.asset)||state?.observation?.membership?.[impact.asset];
    if(observed){const tier=observed.pool_tier||observed.tier,jump=el('button',['COOLING','ARCHIVED'].includes(tier)?'查看候补 / 归档记录':tier==='FOCUS'?'重点研究 · 查看观察栏':'已加入观察栏 · 查看','text-button');jump.type='button';jump.addEventListener('click',()=>showObservation(impact.asset,observed.category));row.append(jump);}
    impacts.append(row);
  }
  card.append(impacts,el('p','不确定性：'+a.uncertainty,'macro-uncertainty'));
  const detail=details('分析依据与后续观察','macro:'+event.id);
  detail.append(el('p','传导机制：'+a.transmission),el('p','预期差：'+a.expectation_basis),el('p','反证：'+a.invalidation));
  addList(detail,'后续观察',a.impacts.map(i=>(assets[i.asset]?.name||i.asset)+'：'+i.watch));
  for(const report of event.related_reports||[])detail.append(link('同一事件报道：'+report.source+' · '+report.title,report.url));
  for(const cite of event.citations){detail.append(link(cite.source+' · '+cite.title,cite.url),el('blockquote',cite.quote));}
  detail.append(el('p',event.basis==='RETROSPECTIVE'?'历史补采研究：形成判断时事件已经发生，存在回看偏差。':'前向跟踪：从本次研究完成后观察对应指标。','subtle'));
  for(const r of event.reactions){const o=r.observation;if(o){detail.append(el('p',r.name+' · '+o.baseline.date+' → '+o.end.date+' · '+(o.change>0?'+':'')+o.change+o.change_unit),link('查看指标来源',o.url),el('p',o.method,'subtle'));}else detail.append(el('p',r.name+'：'+r.waiting,'subtle'));}
  detail.append(el('p','研究更新 '+shortTime(event.created_at)+'；市场方向是条件判断，不代表已提交交易。','subtle'));card.append(detail);box.append(card);
}
function renderNewsScreening(box,screening){
  if(!screening)return;
  const c=screening.counts||{};
  box.append(el('p','近48小时新闻筛选：深研 '+(c.DEEP||0)+' 条 · 等待关键证据 '+(c.WATCH||0)+' 条 · 背景资料 '+(c.BACKGROUND||0)+' 条。','subtle'));
  if(!screening.recent?.length)return;
  const details=el('details',null,'dynamic-library');details.append(el('summary','查看近期筛选依据'));
  const decisions={DEEP:'进入深研',WATCH:'等待关键证据',BACKGROUND:'保留为背景'};
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
  const prominent=g.items.filter(macroProminent);
  renderChanged('macro-cases',[g.items,g.assets,state?.observation?.membership,state?.observation?.items.map(t=>[t.asset,t.pool_tier])],box=>{
    if(!prominent.length){box.append(el('p','近48小时暂无通过筛选且已完成研究的重要动态。新闻每30分钟更新，既往研究与待补证据的事件可在下方查看。','empty'));return;}
    prominent.forEach(e=>drawMacroEvent(box,e,g.assets));
  });
  const archived=[...g.items.filter(e=>!macroProminent(e)),...(g.archived_items||[])];
  $('macro-history').hidden=!archived.length;
  $('macro-history-title').textContent='既往研究、待补证据与修订记录（'+archived.length+'）';
  renderChanged('macro-archive',[archived,g.assets,state?.observation?.membership],box=>archived.forEach(e=>drawMacroEvent(box,e,g.assets)));
  $('macro-execution-note').textContent=g.execution||'全球市场结论用于研究；当前账户仅接入普通沪深主板模拟交易，执行计划单独核验。';
  renderChanged('dynamic-learning',[g.event_count,g.observation_counts,g.news_counts,g.impact_learning],box=>{
    const impact=g.impact_learning;
    if(impact){box.append(el('p','独立影响评估 '+impact.assessed+' 项 · 待评估 '+impact.pending+' 项 · 背景资料 '+impact.background+' 项。'));
      box.append(el('p','历史校准：'+impact.forward_samples+' 个可用前向样本，'+impact.calibrated_cohorts+' 组达到检验数量。每组至少20例训练及后续10例检验。'+(!impact.calibrated_cohorts?'目前尚未完成历史验证。':''),'subtle'));}

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
  $('portfolio-strategy-summary').textContent=portfolioText(p.summary)||'等待综合各路研究、已有持仓和可用资金。';
  const busy=(s.active_jobs||[]).some(j=>j.kind==='portfolio_strategy');
  $('portfolio-strategy-state').textContent=(busy?'正在更新组合判断。 ':p.last_run?.status==='DEFERRED'?'本轮未完成，'+(p.last_run.error||'等待补试')+'。 ':'')+(p.status==='ACTIVE'?'有效至 '+shortTime(p.valid_until)+'；买卖仍需满足原策略和风控条件。':'组合授权尚未形成或已过期，暂停新增买入；原止损与退出继续。');
  const labels={ALLOW:'允许买入',HOLD:'保持持仓',PAUSE:'暂停买入',REDUCE:'减仓',EXIT:'退出'};
  table('portfolio-strategy-items',['标的','组合决定','研究时仓位 → 目标','调整依据'],[...(p.decisions||[])].sort((a,b)=>b.current_bps-a.current_bps).map(d=>[d.name,labels[d.action]+(d.current_authorization?'':'（待复核）'),(d.current_bps/100).toFixed(2)+'% → '+(d.target_bps/100).toFixed(2)+'%',portfolioText(d.reason)]));
  const groups=$('portfolio-strategy-groups');groups.replaceChildren();
  for(const g of p.risk_groups||[])groups.append(el('p',g.name+' · 合计目标上限 '+(g.max_bps/100).toFixed(2)+'% · '+portfolioText(g.reason),'subtle'));
}
