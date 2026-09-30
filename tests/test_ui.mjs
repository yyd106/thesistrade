import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
const source=await readFile(new URL('../ashare/web/app.js',import.meta.url),'utf8');
const context=vm.createContext({document:{addEventListener(){}},fetch(){throw Error('Unexpected real network');},AbortSignal});
vm.runInContext(source,context,{filename:'app.js'});
const LocalApi=vm.runInContext('LocalApi',context);
const reply=(status,data)=>({ok:status>=200&&status<300,status,json:async()=>data});
let calls=[],count=0;
const reconnect=new LocalApi(async(path,opts)=>{
  calls.push({path,opts});
  if(path==='/api/session')return reply(200,{csrf_token:++count===1?'expired':'renewed'});
  if(opts.headers['X-CSRF-Token']==='expired')return reply(403,{code:'SESSION_EXPIRED'});
  return reply(202,{job_id:'one-job'});
});
assert.equal((await reconnect.post('/api/run',{kind:'cycle'})).job_id,'one-job');
assert.equal(calls.length,4);
assert.equal(calls[3].opts.headers['X-CSRF-Token'],'renewed');
let mutations=0;
const foreign=new LocalApi(async path=>path==='/api/session'?reply(200,{csrf_token:'fresh'}):(++mutations,reply(403,{code:'ORIGIN_FORBIDDEN',error:'页面来源不匹配'})));
await assert.rejects(()=>foreign.post('/api/run',{kind:'cycle'}),/页面来源不匹配/);
assert.equal(mutations,1);
let sends=0;
const uncertain=new LocalApi(async path=>{if(path==='/api/session')return reply(200,{csrf_token:'fresh'});sends++;throw Error('lost response');});
await assert.rejects(()=>uncertain.post('/api/run',{kind:'cycle'}),/确认是否已提交/);
assert.equal(sends,1);
const decision=vm.runInContext('researchDecision',context);
assert.equal(decision({plan:{effective_status:'EXPIRED'}}).title,'研究已过期，等待更新');
assert.equal(decision({plan:{effective_status:'DRAFT'}}).title,'本轮研究尚未完成');
const message=vm.runInContext('jobMessage',context);
assert.match(message({kind:'slot',status:'DONE',result_json:'{"status":"CLOSED"}'})[0],/未进行交易/);
assert.match(message({kind:'cycle',status:'DONE',result_json:'{"studies":[{"status":"DEFERRED"}]}'})[0],/尚未完成/);
console.log('UI checks passed: session recovery, origin rejection, no repeat on uncertain result, expired research, deferred result, closed market.');
// Browser fetch rejects being invoked as a method of an arbitrary client object.
let defaultCalls=0;
context.fetch=function(path){
  assert.notEqual(this?.constructor?.name,'LocalApi');
  defaultCalls++;
  return Promise.resolve(path==='/api/session'?reply(200,{csrf_token:'default'}):reply(202,{job_id:'default-job'}));
};
assert.equal((await new LocalApi().post('/api/run',{kind:'slot'})).job_id,'default-job');
assert.equal(defaultCalls,2);
console.log('Default browser transport binding passed.');
// Failure rows are stock-scoped, readable, and omitted as soon as the API resolves them.
class FakeElement {
  constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.attrs={};this.dataset={};}
  append(...children){this.children.push(...children);}
  setAttribute(key,value){this.attrs[key]=value;}
  addEventListener(){}
}
context.document.createElement=tag=>new FakeElement(tag);
const showFailures=vm.runInContext('renderFailures',context);
const supervisionLabel=vm.runInContext('supervisionLabel',context);
assert.match(supervisionLabel({status:'SUCCEEDED',verdict:'RECOMMEND',approval:'WAITING_USER'}),/建议通过.*等待你批准/);
assert.match(supervisionLabel({status:'SUCCEEDED',verdict:'INSUFFICIENT'}),/证据不足/);
assert.doesNotMatch(supervisionLabel({status:'STALE',verdict:'RECOMMEND',approval:'WAITING_USER'}),/建议通过|等待你批准/);
assert.match(supervisionLabel({status:'DEFERRED'}),/等待重试/);
const drawSupervision=vm.runInContext('drawSupervision',context);
const supervisorCard=new FakeElement('div');
drawSupervision(supervisorCard,{items:[{id:'SR-test',kind:'PROPOSAL',title:'<script>untrusted</script>',status:'SUCCEEDED',verdict:'INSUFFICIENT',reviewer:'chatgpt',created_at:'2026-09-28T08:00:00Z',input_hash:'hash',review_version:'v1',result:{counterexamples:['可能来自市场上涨'],checks:[],next_steps:[]}}]});
assert.match(JSON.stringify(supervisorCard),/证据不足/);
assert.match(JSON.stringify(supervisorCard),/可能来自市场上涨/);
const item={name:'迈威生物',failures:[{label:'历史价格与均线',title:'',reason:'收到的数据格式无法正确读取。',impact:'暂时不能更新参考价。',last_failed_at:'2026-09-17T03:47:27Z'}]};
let card=new FakeElement('article');showFailures(card,item);
assert.equal(card.children[0].attrs['aria-label'],'迈威生物失败项');
assert.match(JSON.stringify(card),/收到的数据格式无法正确读取/);
assert.doesNotMatch(JSON.stringify(card),/KeyError|qfqday/);
card=new FakeElement('article');showFailures(card,{...item,failures:[]});assert.equal(card.children.length,0);
console.log('Stock-scoped failure rendering and recovery checks passed.');
const showChanges=vm.runInContext('renderResearchChanges',context);
card=new FakeElement('article');
showChanges(card,{symbol:'sz000333',learning:{mode:'DELTA',new_documents:2,pending_chunks:3},external_events:[{doc_id:'one',title:'红海航运变化',topics:['地缘与运输风险']}]});
assert.match(JSON.stringify(card),/本次阅读 2 份/);
assert.match(JSON.stringify(card),/仍有 3 段/);
assert.match(JSON.stringify(card),/相关行业与国际消息/);
assert.match(JSON.stringify(card),/公司实际敞口/);
card=new FakeElement('article');showChanges(card,{learning:{mode:'DELTA',new_documents:0,pending_chunks:0}});
assert.match(JSON.stringify(card),/本次没有新增原文/);
assert.doesNotMatch(JSON.stringify(card),/相关行业与国际消息/);
console.log('Incremental research, pending reading, and external evidence rendering passed.');
const showGuidance=vm.runInContext('renderTradeGuidance',context);
card=new FakeElement('article');
showGuidance(card,{symbol:'sz002415',plan:{trade_guidance:{summary:'这些事项拦截新增买入',groups:[{key:'event:one',title:'分红或除权事项尚未核验',why:'标题触发核对',waiting:'等待核对实施日期',user_action:'查看原公告',release:'核验完成后重新研究',run:'research',documents:[{title:'权益分派公告',url:'https://static.cninfo.com.cn/test.pdf',state:'正文已取得'}]}]}}});
assert.match(JSON.stringify(card),/当前交易限制/);
assert.match(JSON.stringify(card),/你可以做什么/);
assert.match(JSON.stringify(card),/何时解除/);
assert.match(JSON.stringify(card),/static.cninfo.com.cn/);
const showRecovery=vm.runInContext('renderRecovery',context);
showRecovery(card,{symbol:'sz002415',recovery:{action:'research',why:'继续读取关键章节',next_step:'等待自动处理',automatic_attempts:1,automatic_limit:2}});
assert.match(JSON.stringify(card),/补齐资料并重研/);
assert.match(JSON.stringify(card),/"symbol":"sz002415"/);
card=new FakeElement('article');showGuidance(card,{symbol:'sz002415',plan:{trade_guidance:{groups:[]}}});
assert.equal(card.children.length,1);assert.equal(card.children[0].hidden,true);
assert.equal(card.children[0].id,'trade-guidance-feedback-sz002415');
assert.match(decision({plan:{effective_status:'ACTIVE',payload:{kind:'NO_ENTRY',blockers:['UNRESOLVED_EVENT:one']},trade_guidance:{groups:[{key:'event:one',title:'分红或除权事项尚未核验'}]}}}).reason,/分红或除权/);
card=new FakeElement('article');
showRecovery(card,{symbol:'sz000651',recovery:{action:null,why:'等待',next_step:'等待',automatic_attempts:0,automatic_limit:2},plan:{payload:{event_reviews:[
  {doc_id:'m',title:'2026年半年度权益分派实施公告',status:'VERIFIED',missing:[],facts:{cash_per_share:'0.055',cash_per_share_cents:null,record_date:'2026-09-22',ex_date:'2026-09-23'}},
  {doc_id:'f',title:'关于权益分派实施后调整回购股份价格上限的公告',status:'NEEDS_EVIDENCE',missing:['随除息日2026-09-23的实施公告一并核验'],facts:{follows:'DIVIDEND',record_date:'2026-09-22',ex_date:'2026-09-23'}},
  {doc_id:'o',title:'规则v1的核验记录',status:'VERIFIED',missing:[],facts:{cash_per_share_cents:55,record_date:'2026-08-18',ex_date:'2026-08-19'}}]}}});
assert.match(JSON.stringify(card),/每股税前现金分红 0.055 元/);
assert.match(JSON.stringify(card),/每股税前现金分红 0.55 元/);
assert.match(JSON.stringify(card),/随分红实施发布的公告（除息日 2026-09-23）/);
console.log('Actionable trading limits, source links, and stable feedback target passed; sub-cent dividends and follow-on announcements render exactly.');
const actionJob=vm.runInContext('actionJob',context);
const active={watchlist:[{symbol:'one',recovery:{job_id:'one-job'}},{symbol:'two',recovery:{job_id:null}}],active_jobs:[{id:'one-job',kind:'repair',status:'RUNNING'}]};
assert.equal(actionJob(active,'repair','one').id,'one-job');
assert.equal(actionJob(active,'repair','two'),undefined);
assert.equal(actionJob(null,'repair','one'),undefined);
assert.equal(vm.runInContext('decisionReason("MODEL_DEFERRED: 模型分析超时；STALE_QUOTE")',context),'本次盘面分析未完成: 模型分析超时；报价已过时');
console.log('Per-stock busy state and readable decision failure reasons passed.');
const drawFollowups=vm.runInContext('drawFollowups',context);
card=new FakeElement('section');
drawFollowups(card,{failed_today:2,recovered_today:1,carried_over:1,items:[{id:'case',name:'海康威视',symbol:'sz002415',title:'关键正文缺失',state_name:'已转后续处理',owner:'SYSTEM',owner_name:'系统',reason:'文件未取得',impact:'暂停新增买入',next_action:'20:00定时研究继续补取',trigger:'新正文取得',completion:'正文已研究',next_action_at:'2026-09-18T12:00:00Z',escalation:'今日额外补齐已用完',documents:[{title:'原文',url:'https://static.cninfo.com.cn/test.pdf'}],run:'repair'}],today_failures:[{symbol:'sz002415',title:'公告处理',last_failed_at:'2026-09-18T03:00:00Z',recovered:true,next_step:'已成功'}]});
for(const text of ['跨日跟进','由谁处理','下一步','继续条件','完成标准','今日额外补齐已用完','本日失败与恢复记录','补齐本股票'])assert.ok(JSON.stringify(card).includes(text));
assert.match(JSON.stringify(card),/"symbol":"sz002415"/);
card=new FakeElement('section');drawFollowups(card,{items:[],stale:true});
assert.doesNotMatch(JSON.stringify(card),/当前没有待处理事项/);assert.match(JSON.stringify(card),/尚未更新/);
console.log('Unified ownership, next steps, daily recovery, and stale followup rendering passed.');
const showDecision=vm.runInContext('renderDecisionCard',context);
card=new FakeElement('article');
showDecision(card,{inclination:'等待现金流改善',key_evidence:[{evidence_id:'doc:1',implication:'收入增长但回款放缓'}],pricing:'缺少一致预期，无法确认定价程度',trigger:'下一期经营现金流转正',invalidation:'核心客户订单取消'},'sz000333');
const decisionText=JSON.stringify(card);
for(const title of ['当前倾向','关键证据','价格反映了多少','等待什么触发','什么会推翻判断'])assert.match(decisionText,new RegExp(title));
assert.match(decisionText,/api\/document/);assert.doesNotMatch(decisionText,/innerHTML/);
const showDossier=vm.runInContext('renderDossier',context);
card=new FakeElement('article');showDossier(card,{company_dossier:{'状态':'部分字段待核验','最新财务':{'经营现金流':'未取得'},'近八季':[{'报告期':'2026-06-30'}],'缺口':['现金流待补齐']},market_context:{'缺口':['同行数据过时']}},'sz000333');
assert.match(JSON.stringify(card),/现金流待补齐/);assert.match(JSON.stringify(card),/同行数据过时/);assert.match(JSON.stringify(card),/未取得/);
console.log('Decision five questions, citations, and financial/market gaps passed.');
const researchUpdate=vm.runInContext('researchUpdate',context);
let timing=researchUpdate({at:'2026-09-19T10:00:00Z',state:{heartbeat:'2026-09-19T10:00:00Z'},watchlist:[
  {last_research_at:'2026-09-19T01:00:00Z',latest_study:{created_at:'2026-09-19T09:00:00Z',model_status:'DEFERRED'},quote:{observed_at:'2026-09-19T09:59:59Z'}},
  {last_research_at:'2026-09-19T09:30:00+08:00'},
  {last_research_at:null,plan:{model_status:'DEFERRED',activated_at:'2026-09-19T10:00:00Z'}}]});
assert.equal(timing.latest,'2026-09-19T09:30:00+08:00');assert.equal(timing.completed,2);assert.equal(timing.total,3);
assert.equal(researchUpdate({watchlist:[]}).latest,null);
assert.equal(researchUpdate({watchlist:[{last_research_at:'invalid'}]}).completed,0);
console.log('Research update time excludes failed studies, quotes, heartbeat and page refresh; partial coverage remains explicit.');

const blockedStock=codes=>({symbol:'sz000333',plan:{effective_status:'ACTIVE',payload:{kind:'NO_ENTRY',blockers:codes}}});
assert.equal(decision(blockedStock(['MARKET_CONTEXT_INCOMPLETE'])).title,'等待资料补齐');
assert.equal(decision(blockedStock(['UNREAD_DOCUMENT:one'])).title,'等待读完关键资料');
assert.equal(decision(blockedStock(['TREND_NOT_CONFIRMED'])).title,'等待趋势恢复');
assert.equal(decision(blockedStock(['RESEARCH_VETO'])).title,'研究暂不支持买入');
assert.equal(decision(blockedStock(['CORPORATE_ACTION_UNVERIFIED:one'])).title,'待核验公告');
const readyStock={name:'中国移动',symbol:'sh600941',quote:{price_cents:9800,observed_at:'2026-09-18T07:00:00Z'},
  report:{overview:'经营现金流提供支持，但仍需跟踪盈利变化'},
  plan:{effective_status:'ACTIVE',activated_at:'2026-09-20T13:00:00Z',payload:{kind:'PAPER_TRADE',blockers:[],levels:{buy_low_cents:9700,buy_high_cents:9900,sell_cents:10800,stop_cents:9200},risk_parameters:{paper_entry_band_bps:200}}}};
assert.match(decision(readyStock,'CLOSED').title,/最近报价在买入区间/);
assert.match(decision(readyStock,'CLOSED').reason,/休市/);
assert.match(decision({...readyStock,quote:{price_cents:9699}}).title,/回升/);
assert.match(decision({...readyStock,quote:{price_cents:9901}}).title,/回落/);
for(const price of [9700,9900])assert.match(decision({...readyStock,quote:{price_cents:price}}).title,/最近报价在买入区间/);
assert.match(decision({...readyStock,quote:null}).title,/更新报价/);
const summary=vm.runInContext('overviewSummary',context);
assert.match(summary(readyStock,decision(readyStock,'CLOSED')),/经营现金流.*休市/);
const expired={...readyStock,plan:{...readyStock.plan,effective_status:'EXPIRED'}};
assert.match(summary(expired,decision(expired)),/^上次研究：/);
const band=vm.runInContext('entryBand',context);
assert.equal(band(readyStock.plan),2);assert.equal(band({payload:{risk_parameters:{}}}),1);
FakeElement.prototype.querySelectorAll=()=>[];
FakeElement.prototype.replaceChildren=function(...c){this.children=c;};
const home=new FakeElement('section');context.document.getElementById=()=>home;
const renderWatchlist=vm.runInContext('renderWatchlist',context);
renderWatchlist({watchlist:[readyStock],portfolio:{holdings:[]},market_phase:'CLOSED'});
const table=home.children[0],group=table.children[1];
assert.equal(group.tag,'tbody');assert.equal(group.attrs['aria-label'],'中国移动');
assert.equal(group.children.length,2);assert.equal(group.children[1].children[0].colSpan,6);
assert.match(JSON.stringify(home),/经营现金流.*休市/);
renderWatchlist({watchlist:[readyStock],portfolio:{holdings:[]},market_phase:'CONTINUOUS'});
assert.doesNotMatch(JSON.stringify(home),/当前休市/);
console.log('Distinct wait states, inclusive entry prices, closed market, per-stock summaries and historical plan bands passed.');

// Rank the watchlist by its own current value, never share count or dynamic lots.
const rankedWatchlist=[{...readyStock,symbol:'zero',name:'未持仓'},{...readyStock,symbol:'low',name:'较小持仓'},{...readyStock,symbol:'high',name:'较大持仓'},{...readyStock,symbol:'tie',name:'同额持仓'}];
const holdings=[{symbol:'zero',origin:'dynamic',market_value_cents:9999999,qty:999},{symbol:'low',origin:'watchlist',market_value_cents:123456,qty:900,weight_pct:1.23,valuation_basis:'COST_FALLBACK'},{symbol:'high',origin:'watchlist',market_value_cents:987654,qty:100,weight_pct:9.87},{symbol:'tie',market_value_cents:123456,qty:200,weight_pct:1.23}];
const orderBefore=rankedWatchlist.map(s=>s.symbol);
renderWatchlist({watchlist:rankedWatchlist,portfolio:{holdings},market_phase:'CLOSED'});
assert.deepEqual(home.children[0].children.slice(1).map(g=>g.attrs['aria-label']),['较大持仓','较小持仓','同额持仓','未持仓']);
assert.deepEqual(rankedWatchlist.map(s=>s.symbol),orderBefore);
assert.equal(home.children[0].children[0].children[0].children[1].attrs['aria-sort'],'descending');
assert.equal(home.children[0].children[1].children[0].children[1].children[0].textContent,'9,876.54 元');
assert.match(JSON.stringify(home),/1,234.56 元/);assert.match(JSON.stringify(home),/暂无报价 · 按成本估算/);
assert.equal(home.children[0].children[4].children[0].children[1].children[0].textContent,'0.00 元');
holdings[1].market_value_cents=1000000;
renderWatchlist({watchlist:rankedWatchlist,portfolio:{holdings},market_phase:'CLOSED'});
assert.equal(home.children[0].children[1].attrs['aria-label'],'较小持仓');
console.log('Holding value ranking, stable ties, refresh reordering, cost fallback and dynamic ownership isolation passed.');

// Display remaining fee-inclusive cost beside the price without rounding P&L.
const costHolding={symbol:readyStock.symbol,origin:'watchlist',qty:700,market_value_cents:1990800,average_cost_cents:1966600/700,unrealized_cents:24200,unrealized_return_pct:1.23};
const costState={watchlist:[{...readyStock,quote:{price_cents:2844,observed_at:'2026-09-22T01:30:00Z'}}],portfolio:{holdings:[costHolding]},market_phase:'CONTINUOUS',at:'2026-09-22T01:31:30Z',quote_max_age_seconds:90};
renderWatchlist(costState);
assert.match(JSON.stringify(home),/当前价 \/ 成本价/);assert.match(JSON.stringify(home),/28.094/);assert.match(JSON.stringify(home),/28.44/);
assert.match(JSON.stringify(home),/较成本 \+1.23%/);assert.match(JSON.stringify(home),/浮盈 \+242.00 元/);
assert.doesNotMatch(JSON.stringify(home),/报价过时/);
renderWatchlist({...costState,at:'2026-09-22T01:31:31Z'});assert.match(JSON.stringify(home),/报价过时/);
renderWatchlist({...costState,market_phase:'CLOSED',at:'2026-09-23T00:00:00Z'});assert.doesNotMatch(JSON.stringify(home),/报价过时/);assert.match(JSON.stringify(home),/最近报价/);
const drawCost=vm.runInContext('drawPositionCost',context);
card=new FakeElement('td');drawCost(card,null);assert.match(JSON.stringify(card),/—/);assert.doesNotMatch(JSON.stringify(card),/0.000|较成本/);
card=new FakeElement('td');drawCost(card,{average_cost_cents:1002.5,unrealized_return_pct:null});assert.match(JSON.stringify(card),/10.025/);assert.doesNotMatch(JSON.stringify(card),/较成本/);
card=new FakeElement('td');drawCost(card,{average_cost_cents:1002.5,unrealized_return_pct:-0.25});assert.match(JSON.stringify(card),/较成本 -0.25%/);assert.match(JSON.stringify(card),/loss/);
console.log('Fee-inclusive cost, exact-basis returns, missing positions/quotes and stale quote transitions passed.');

// HTTP responses, auth expiry and body-read timeouts must not be reported as offline.
const ApiError=vm.runInContext('ApiError',context),problem=vm.runInContext('refreshProblem',context);
const offline=new LocalApi(async()=>{throw new TypeError('Failed to fetch');});
await assert.rejects(()=>offline.get('/api/status'),e=>e.kind==='NETWORK'&&problem(e).title.includes('本机后台'));
for(const bodyOnly of [false,true]){
  const slow=new LocalApi(async()=>{
    const e=new Error('deadline');e.name='TimeoutError';
    if(bodyOnly)return {ok:true,status:200,json:async()=>{throw e;}};
    throw e;
  });
  await assert.rejects(()=>slow.get('/api/status'),e=>e.kind==='TIMEOUT'&&problem(e).title.includes('读取较慢'));
}
const rejected=new LocalApi(async()=>reply(503,{error:'数据库暂时繁忙'}));
await assert.rejects(()=>rejected.get('/api/status'),e=>e.status===503&&problem(e).detail==='数据库暂时繁忙');
const unreadable=new LocalApi(async()=>({ok:true,status:200,json:async()=>{throw new SyntaxError('bad JSON');}}));
await assert.rejects(()=>unreadable.get('/api/status'),e=>e.kind==='RESPONSE'&&!problem(e).title.includes('连接'));
let redirected='';context.location={pathname:'/stocks/sz000333',assign:path=>{redirected=path;}};
const loggedOut=new LocalApi(async()=>({ok:false,status:401,json:async()=>{throw new SyntaxError('irrelevant body');}}));
await assert.rejects(()=>loggedOut.get('/api/status'),e=>e.kind==='AUTH'&&problem(e).action==='login');
assert.equal(redirected,'/login?next=%2Fstocks%2Fsz000333');
assert.equal(problem(new TypeError('render failed'),'render').action,'reload');
delete context.location;
// Exercise the whole refresh boundary: keep rendered research on failure and retry reads only.
const macroNodes=new Map();context.document.getElementById=id=>{if(!macroNodes.has(id))macroNodes.set(id,new FakeElement('div'));return macroNodes.get(id);};
context.document.querySelectorAll=()=>[];
context.console={error(){}};
vm.runInContext(`render=s=>{state=s;connected=true;$('last-updated').textContent='上次更新时间：已读取';$('service').textContent='自动运行中';$('attention').hidden=true;};`,context);
const refresh=vm.runInContext('refresh',context),client=vm.runInContext('api',context);
client.transport=async()=>{throw Error('disconnected');};
await refresh();assert.match(macroNodes.get('last-updated').textContent,/暂未读取/);
assert.equal(vm.runInContext('refreshing',context),false);
client.transport=async()=>reply(200,{jobs:[],active_jobs:[]});
await refresh();assert.equal(vm.runInContext('connected',context),true);
client.transport=async()=>reply(503,{error:'数据库暂时繁忙'});
await refresh();assert.match(macroNodes.get('last-updated').textContent,/已读取/);
assert.doesNotMatch(macroNodes.get('service').textContent,/无法连接|连接不上/);
// A task progress failure cannot turn a successfully loaded dashboard into an outage.
vm.runInContext("watchedJobs.set('task-progress',{id:'old-task'});",context);
client.transport=async path=>path==='/api/status'?reply(200,{jobs:[],active_jobs:[]}):reply(404,{error:'task missing'});
await refresh();assert.equal(vm.runInContext('connected',context),true);
assert.match(macroNodes.get('task-progress').textContent,/任务进度暂未读取成功/);
vm.runInContext("watchedJobs.clear();render=()=>{throw new TypeError('broken renderer');};",context);
await refresh();assert.match(macroNodes.get('service').textContent,/数据已收到，页面显示失败/);
console.log('Connection, timeout (including body read), auth expiry, server and rendering errors are distinct; last results survive and secondary failures stay local.');

const executionSummary=vm.runInContext('executionSummary',context);
const lastCheck={last_decision:{at:'2026-09-21T01:30:06Z',action:'HOLD',status:'BLOCKED',reason:'generic',buy_blockers:['STALE_QUOTE','EVENT_SOURCE_UNAVAILABLE']}};
const lastText=executionSummary(lastCheck,{scheduled_at:'2026-09-21T02:00:00Z'});
assert.match(lastText,/上次检查.*09:30/);assert.match(lastText,/报价已过时.*公告检查未通过/);assert.match(lastText,/下次检查.*10:00/);
assert.doesNotMatch(lastText,/STALE_QUOTE|EVENT_SOURCE_UNAVAILABLE|generic/);
assert.match(executionSummary({},null),/尚无盘面检查/);
assert.match(executionSummary({last_decision:{at:'2026-09-21T02:00:00Z',action:'BUY',status:'SUBMITTED',reason:'PAPER_ORDER_OPEN',buy_blockers:[]}},null),/买入.*已提交模拟委托/);
console.log('Per-stock last decision shows actual blockers, submitted orders and the next scheduled check.');

const scheduleSummary=vm.runInContext('scheduleSummary',context);
const cadence=scheduleSummary({collection:['00:00','06:00','12:00','18:00'],slots:['09:30','09:31','13:00'],review:'19:30',execution_mode:'RULES'});
assert.match(cadence,/00:00、06:00、12:00、18:00/);assert.match(cadence,/每 1 分钟按有效策略检查买卖/);
assert.match(cadence,/恢复后补做最近一轮/);assert.doesNotMatch(cadence,/每 30 分钟/);
console.log('Schedule text follows configured cadence and explains offline catch-up.');

const pendingStock={last_decision:{at:'2026-09-21T02:29:00Z',action:'BUY',status:'BLOCKED',buy_blockers:[],reason:'有效策略允许买入，报价进入参考区间；提交前复核行情、公告、仓位和可用资金。；EXISTING_OPEN_ORDER'},open_orders:[{side:'BUY',qty:700,filled_qty:200,status:'PARTIAL'}]};
const pendingText=executionSummary(pendingStock,null);
assert.match(pendingText,/买入委托 700 股，已成交 200 股，剩余 500 股/);
assert.doesNotMatch(pendingText,/未执行|复核行情|重新研究|EXISTING_OPEN_ORDER/);
const rejection=executionSummary({...pendingStock,open_orders:[]},null);
assert.match(rejection,/已有委托等待成交/);assert.doesNotMatch(rejection,/提交前复核/);
assert.equal(decision(pendingStock).title,'已有委托，等待成交');
const inZone=decision({plan:{effective_status:'ACTIVE',payload:{kind:'PAPER_TRADE',levels:{buy_low_cents:2770,buy_high_cents:2883}}},quote:{price_cents:2813}},'CONTINUOUS');
assert.match(inZone.reason,/现有有效策略/);assert.match(inZone.reason,/无需重新研究/);
console.log('Existing order progress takes priority; the true rejection reason survives shortening; reentry does not require research.');
const drawDynamic=vm.runInContext('drawDynamicCase',context);
const dynamicCard=new FakeElement('section');
drawDynamic(dynamicCard,{id:'event',name:'动态测试',symbol:'sh600547',status:'RESEARCH',url:'https://finance.sina.com.cn/test',title:'公开新闻标题',source:'公开来源',published_at:'2026-09-21T02:00:00Z',created_at:'2026-09-21T02:30:00Z',expires_at:'2026-09-21T14:30:00Z',basis:'RETROSPECTIVE',analysis:{impact:'<img src=x onerror=alert(1)>',business_link:'关联待核验',pricing:'不能排除价格已经反映',invalidation:'事件撤回',evidence:[{quote:'真实来源中的引文'}]},plan:{blockers:['前向历史验证未达标'],history:{count:0,required:30,retrospective_count:2,passed:false}},position:{qty:0,sellable_qty:0},orders:[]});
const dynamicText=JSON.stringify(dynamicCard);
for(const phrase of ['研究中','前向历史验证未达标','存在回看偏差','真实来源中的引文'])assert.ok(dynamicText.includes(phrase));
assert.doesNotMatch(dynamicText,/innerHTML/);
console.log('Dynamic evidence, cold-start gates, retrospective caveat, and text-safe rendering passed.');
const drawMacro=vm.runInContext('drawMacroEvent',context);
const macroCard=new FakeElement('section');
drawMacro(macroCard,{id:'macro-one',status:'TRACKING',theme_label:'货币政策与利率',published_at:'2026-09-21T02:00:00Z',created_at:'2026-09-21T02:10:00Z',basis:'FORWARD',analysis:{headline:'海外央行与全球资产',regions:['US','EUROPE'],horizon:'DAYS',facts:'<img src=x onerror=alert(1)>',transmission:'利率通过资金成本影响市场',uncertainty:'缺少一致预期',expectation_basis:'尚未核验预期差',invalidation:'政策撤回',impacts:[{asset:'US10Y',direction:'UP',mechanism:'收益率上行不等于债券价格上涨',watch:'通胀数据'},{asset:'GOLD',direction:'MIXED',mechanism:'避险与利率共同作用',watch:'实际利率'}]},citations:[{source:'美联储',title:'政策声明',url:'https://www.federalreserve.gov/test.htm',quote:'Monetary policy statement'}],reactions:[{asset:'US10Y',name:'美国10年期国债收益率',observation:{baseline:{date:'2026-09-10'},end:{date:'2026-09-15'},change:25,change_unit:'bp',url:'https://fred.stlouisfed.org/series/DGS10',method:'实际指标变化，不是交易收益'}},{asset:'GOLD',name:'黄金',observation:null,waiting:'历史序列尚未接入'}]}, {US10Y:{name:'美国10年期国债收益率'},GOLD:{name:'黄金'}});
const macroText=JSON.stringify(macroCard);
for(const phrase of ['美国 · 欧洲','黄金','上行压力','+25bp','历史序列尚未接入','尚未核验预期差','不代表已提交交易'])assert.ok(macroText.includes(phrase));
assert.doesNotMatch(macroText,/innerHTML/);
const impacts=macroCard.children[0].children.find(c=>c.className==='macro-impacts');
assert.equal(impacts.tag,'ul');assert.equal(impacts.attrs['aria-label'],'受影响标的与方向');
assert.equal(impacts.children.length,2);assert.ok(impacts.children.every(c=>c.tag==='li'));
assert.equal(impacts.children[0].children[0].children[1].dataset.direction,'UP');
assert.equal(impacts.children[1].children[0].children[1].dataset.direction,'MIXED');
console.log('Global macro event rendering preserves cross-market directions, yield units, missing-data state and safe text.');

macroNodes.clear();context.document.getElementById=id=>{if(!macroNodes.has(id))macroNodes.set(id,new FakeElement('div'));return macroNodes.get(id);};
const renderMacro=vm.runInContext('renderGlobalMacro',context);
const g={items:[],archived_items:[],assets:{},markets:[],sources:[],news_counts:{},observation_counts:{},event_count:0};
renderMacro({global:g});
assert.match(JSON.stringify(macroNodes.get('macro-cases')),/近48小时暂无/);assert.equal(macroNodes.get('macro-history').hidden,true);
const archivedEvent={id:'older',status:'TRACKING',theme_label:'货币政策与利率',published_at:'2026-09-19T02:00:00Z',created_at:'2026-09-21T02:00:00Z',basis:'RETROSPECTIVE',analysis:{headline:'历史央行事件',regions:['US'],horizon:'DAYS',facts:'历史事实',transmission:'利率传导',uncertainty:'方向未知',expectation_basis:'待核验',invalidation:'撤回',impacts:[{asset:'GOLD',direction:'UNCLEAR',mechanism:'待确认',watch:'后续数据'}]},citations:[],reactions:[]};
renderMacro({global:{...g,archived_items:[archivedEvent],assets:{GOLD:{name:'黄金'}},event_count:1}});
assert.doesNotMatch(JSON.stringify(macroNodes.get('macro-cases')),/历史央行事件/);
assert.match(JSON.stringify(macroNodes.get('macro-archive')),/历史央行事件/);assert.equal(macroNodes.get('macro-history').hidden,false);
assert.match(macroNodes.get('macro-history-title').textContent,/（1）/);
console.log('Recent-event empty state and separate historical-event children passed.');

// Fact/inference chains remain visible, and category switches preserve latest state.
FakeElement.prototype.addEventListener=function(name,fn){(this.listeners??={})[name]=fn;};
FakeElement.prototype.focus=function(){this.focused=true;};
FakeElement.prototype.scrollIntoView=function(){this.scrolled=true;};
context.history={state:null,replaceState(){}};
const chainedImpact={asset:'GOLD',direction:'UP',strength:'HIGH',mechanism:'政策经成本与需求传导',watch:'政策落地',strength_basis:'可能影响行业成本',conditions:'政策须落地',invalidation:'政策撤回',logic_chain:[{statement:'公开政策表态',kind:'FACT',news_id:'one',quote:'An exact public policy statement.'},{statement:'<img src=x> 若政策实施则改变需求',kind:'INFERENCE',news_id:'',quote:''},{statement:'供需变化可能支持金价',kind:'INFERENCE',news_id:'',quote:''}]};
const drawChain=vm.runInContext('drawLogicChain',context),chainBox=new FakeElement('div');drawChain(chainBox,chainedImpact,'test');
assert.equal(chainBox.children[0].tag,'ol');assert.equal(chainBox.children[0].children.length,3);
assert.match(JSON.stringify(chainBox),/新闻事实/);assert.match(JSON.stringify(chainBox),/推断/);
assert.match(JSON.stringify(chainBox),/成立条件/);assert.match(JSON.stringify(chainBox),/失效条件/);
assert.doesNotMatch(JSON.stringify(chainBox),/innerHTML/);
const observationTarget=(asset,name,category)=>({asset,name,category,kind:'BENCHMARK',strength:'HIGH',direction:'UP',status:'WATCHING',added_at:'2026-09-21T02:00:00Z',updated_at:'2026-09-21T02:10:00Z',links:[{event_id:'discovery-event',headline:'公开政策变化影响产业',published_at:'2026-09-21T01:00:00Z',url:'https://finance.sina.com.cn/test',status:'TRACKING',impact:{...chainedImpact,asset}}]});
const observationState={watchlist:[readyStock],portfolio:{holdings:[]},market_phase:'CLOSED',observation:{items:[observationTarget('GOLD','黄金','COMMODITY'),observationTarget('US:NVDA','NVIDIA Corporation','US'),observationTarget(readyStock.symbol,readyStock.name,'CN'),observationTarget('sh600547','山东黄金','CN')]}};
context.observationFixture=observationState;vm.runInContext('state=observationFixture;observationCategory="CN";',context);
renderWatchlist(observationState);
assert.equal(macroNodes.get('observation-categories').children.length,3);
assert.equal(macroNodes.get('observation-categories').children[0].textContent,'A股 2');
assert.equal(macroNodes.get('stocks').children[0].children.length,3); // Header plus 2 distinct targets; no duplicate original stock.
assert.match(JSON.stringify(macroNodes.get('stocks')),/动态影响/);
macroNodes.get('observation-categories').children[1].listeners.click();
assert.match(JSON.stringify(macroNodes.get('stocks')),/NVIDIA Corporation/);
assert.doesNotMatch(JSON.stringify(macroNodes.get('stocks')),/山东黄金/);
assert.equal(macroNodes.get('last-updated').hidden,true);
vm.runInContext('showObservation("GOLD","COMMODITY")',context);
assert.equal(vm.runInContext('observationCategory',context),'COMMODITY');
assert.match(JSON.stringify(macroNodes.get('stocks')),/黄金/);
assert.equal(macroNodes.get('watchlist-section').hidden,false);assert.equal(macroNodes.get('dynamic-section').hidden,true);
const refreshed=structuredClone(observationState);refreshed.observation.items[1].name='NVIDIA updated';context.observationFixture=refreshed;vm.runInContext('state=observationFixture',context);
renderWatchlist(refreshed);macroNodes.get('observation-categories').children[1].listeners.click();
assert.match(JSON.stringify(macroNodes.get('stocks')),/NVIDIA updated/);
console.log('Fact/inference chains, three categories, deduplicated discoveries, cross-board navigation and fresh-state switching passed.');

// Archived targets stay outside live categories and retain a navigable, paged record.
const archivedTarget={...observationTarget('US:OLD','Archived company','US'),pool_tier:'ARCHIVED',pool_reason:'事件观察期限已结束',review_due_at:null,priority_score:0};
const focusTarget={...observationTarget('US:NVDA','Focus company','US'),pool_tier:'FOCUS',pool_reason:'优先研究相关新证据',review_due_at:'2026-09-24T00:00:00Z',priority_score:85};
const managedState={...observationState,observation:{items:[focusTarget],archived_items:[archivedTarget],archive_count:26,archive_offset:0,counts:{active:1,focus:1,protected:0,protected_overflow:0},policy:{active_limit:18,focus_limit:8},membership:{'US:NVDA':{tier:'FOCUS',category:'US'},'US:OLD':{tier:'ARCHIVED',category:'US'}}}};
context.managedFixture=managedState;vm.runInContext('state=managedFixture;observationCategory="US";observationArchiveOffset=0;',context);renderWatchlist(managedState);
assert.match(macroNodes.get('observation-pool-summary').textContent,/动态活跃 1 \/ 18 · 重点研究 1 \/ 8/);
assert.match(JSON.stringify(macroNodes.get('stocks')),/重点研究/);assert.doesNotMatch(JSON.stringify(macroNodes.get('stocks')),/Archived company/);
assert.match(JSON.stringify(macroNodes.get('observation-archive-items')),/Archived company/);assert.match(JSON.stringify(macroNodes.get('observation-archive-items')),/事件观察期限已结束/);
assert.equal(macroNodes.get('observation-archive-prev').disabled,true);assert.equal(macroNodes.get('observation-archive-next').disabled,false);
const archiveRenderer=vm.runInContext('renderObservationArchive',context);archiveRenderer({...managedState.observation,archive_offset:25,archived_items:[archivedTarget]});
assert.equal(macroNodes.get('observation-archive-prev').disabled,false);assert.equal(macroNodes.get('observation-archive-next').disabled,true);
const archiveMacro=new FakeElement('section');drawMacro(archiveMacro,{...archivedEvent,analysis:{...archivedEvent.analysis,impacts:[{...chainedImpact,asset:'US:OLD'}]}},{'US:OLD':{name:'Archived company'}});
assert.match(JSON.stringify(archiveMacro),/查看候补 \/ 归档记录/);assert.doesNotMatch(JSON.stringify(archiveMacro),/已加入观察栏/);
client.transport=async()=>reply(200,{...managedState.observation,archive_offset:25,archived_items:[archivedTarget]});
await vm.runInContext('showObservation("US:OLD","US")',context);
assert.equal(macroNodes.get('observation-archive').open,true);assert.equal(vm.runInContext('observationArchiveOffset',context),25);
assert.equal(macroNodes.get('observed-US:OLD').focused,true);
console.log('Pool limits, focus state, archive isolation, paging and event-to-archive navigation passed.');
// Independent impact judgement remains visible in both entry points without inventing a win rate.
const drawImpact=vm.runInContext('drawImpactAssessment',context),assessmentBox=new FakeElement('section');
drawImpact(assessmentBox,{state:'BACKGROUND',admitted:false,reason:'范围不匹配，不占用活跃名额',assessment:{magnitude:'UNKNOWN',novelty:'IMPLEMENTATION',basis:'既有安排，尚未证明新增需求',scale:{kind:'UNKNOWN',explanation:'缺少产业分母'},transmission:'地方补贴尚不能推及全国股市',missing_evidence:'具体受益金额和产业占比',citations:[{quote:'<script>not executable</script>'}]},history:{state:'INSUFFICIENT',sample_count:0,required_count:30,median_absolute_change:null},sources:[{title:'官方通知',url:'https://fgw.sh.gov.cn/test'}]},'test');
assert.match(JSON.stringify(assessmentBox),/背景资料/);assert.match(JSON.stringify(assessmentBox),/0 \/ 30/);assert.match(JSON.stringify(assessmentBox),/样本不足/);assert.doesNotMatch(JSON.stringify(assessmentBox),/胜率|NaN|undefined/);
assert.equal(assessmentBox.children[0].attrs['aria-label'],'独立影响评估');
const pendingImpactBox=new FakeElement('section');drawImpact(pendingImpactBox,null,'pending');assert.match(JSON.stringify(pendingImpactBox),/暂不占用活跃名额/);
console.log('Independent impact admission, sources and honest cold-start state passed.');
// Semantic screening is inspectable, bounded and uses text nodes for external content.
const drawScreening=vm.runInContext('renderNewsScreening',context),screenBox=new FakeElement('section');
drawScreening(screenBox,{counts:{DEEP:2,WATCH:3,BACKGROUND:4},recent:[{decision:'WATCH',title:'<img src=x onerror=alert(1)>',url:'https://www.ft.com/test',source:'Financial Times',at:'2026-09-22T02:00:00Z',reason:'等待政策范围确认',channel:'供给约束可能向成本传导',scale_basis:'缺少产业分母',next_evidence:'正式实施范围'}]});
assert.match(JSON.stringify(screenBox),/深研 2 条/);assert.match(JSON.stringify(screenBox),/等待政策范围确认/);assert.match(JSON.stringify(screenBox),/传导路径/);assert.equal(screenBox.children[1].tag,'details');
assert.doesNotMatch(JSON.stringify(screenBox),/undefined|innerHTML/);
console.log('News screening decisions, economic rationale and safe collapsed detail passed.');
// Older unfiltered research remains accessible without flooding the main important-news list.
const prominent=vm.runInContext('macroProminent',context);
assert.equal(prominent({...archivedEvent,screening:null}),false);
assert.equal(prominent({...archivedEvent,screening:{decision:'DEEP'}}),true);
assert.equal(prominent({...archivedEvent,screening:{decision:'DEEP'},analysis:{impacts:[{materiality:{state:'BACKGROUND'}}]}}),false);
assert.equal(prominent({...archivedEvent,screening:null,analysis:{impacts:[{materiality:{admitted:true}}]}}),true);
renderMacro({global:{...g,items:[{...archivedEvent,screening:null}],assets:{GOLD:{name:'黄金'}}}});
assert.doesNotMatch(JSON.stringify(macroNodes.get('macro-cases')),/历史央行事件/);
assert.match(JSON.stringify(macroNodes.get('macro-archive')),/历史央行事件/);
console.log('Important-news selection preserves earlier evidence in collapsed, navigable records.');
const drawReview=vm.runInContext('drawDailyReview',context);
const closing={qty:100,average_cost_cents:1005,price_cents:1010,unrealized_cents:500,quality:'CLOSE',quote_at:'2026-09-22T07:00:00Z',quote_first_seen_at:'2026-09-23T00:00:00Z',late_quote:true,valuation_complete:true,cumulative_realized_cents:0};
const review={id:'r',revision:2,model_status:'SUCCEEDED',ready_at:'2026-09-23T01:00:00Z',window_start:'2026-09-21T11:30:00Z',window_end:'2026-09-22T11:30:00Z',payload:{facts:{portfolio:{totals:{period_profit_cents:1000,cumulative_profit_cents:500,holding_count:1,reviewed_position_count:1,period_fill_count:0},opening:{valuation_complete:true},closing,positions:[{key:'w:a',name:'合成持仓',symbol:'a',opening:{},closing,research_ids:[],period_profit_cents:1000,cumulative_realized_cents:0}],research:[]}},analysis:{summary:'没有成交也核对持仓',positions:[{position_key:'w:a',verdict:'PENDING',reason:'两天不足以验证',supported_points:[],contradicted_points:[],pending_points:['等待季度结果'],next_check:'检查下一季利润'}]}}};
card=new FakeElement('section');drawReview(card,review);
for(const text of ['本期盈亏','截止累计盈亏','合成持仓','仍待验证','事后补齐','等待季度结果','第 2 版'])assert.ok(JSON.stringify(card).includes(text));
assert.doesNotMatch(JSON.stringify(card),/暂无交易可复盘|分析尚未完成/);
card=new FakeElement('section');drawReview(card,{...review,model_status:'DEFERRED',automatic_retries_remaining:1,payload:{...review.payload,analysis:{},analysis_error:'网络域名解析失败'}});
assert.match(JSON.stringify(card),/网络域名解析失败/);assert.match(JSON.stringify(card),/最多剩余 1 次/);
assert.match(message({kind:'review',status:'DONE',result_json:'{"status":"DEFERRED"}'})[0],/待补齐/);
console.log('Whole-position reviews, no-trade coverage, historical quote labels and actionable failures passed.');
assert.match(message({kind:'review',status:'DEFERRED',result_json:'{"analysis_error":"网络域名解析失败"}'})[0],/网络域名解析失败/);
// The removed category falls back safely; all live categories use the same table.
context.observationFixture=observationState;vm.runInContext('state=observationFixture;observationCategory="RATES_FX"',context);
renderWatchlist(observationState);
assert.equal(macroNodes.get('observation-categories').children.length,3);
assert.equal(vm.runInContext('observationCategory',context),'CN');
assert.doesNotMatch(JSON.stringify(macroNodes.get('observation-categories')),/债券|外汇/);
assert.doesNotMatch(JSON.stringify(macroNodes.get('stocks')),/Coming soon/);
const spotTable=new FakeElement('table'),drawGlobalRow=vm.runInContext('drawGlobalWatchlistRow',context);
const spotTarget={...observationTarget('BTC','比特币','COMMODITY'),fixed:true,position:{qty:10000000,qty_scale:100000000,market_value_cents:140000,cost_cents:130000,average_cost_cents:1300000,average_native_cost_micros:2000000000},spot_quote:{price_micros:2100000000,fx_micros:7000000,fx_at:'2026-09-23T01:00:00Z',observed_at:'2026-09-23T01:00:00Z'},unit:'美元/枚'};
drawGlobalRow(spotTable,spotTarget,observationState);
assert.match(JSON.stringify(spotTable),/固定观察/);assert.match(JSON.stringify(spotTable),/2,000.000/);assert.match(JSON.stringify(spotTable),/0.1 枚/);
assert.match(JSON.stringify(spotTable),/1,400.00 元/);assert.match(JSON.stringify(spotTable),/2,100.00/);assert.match(JSON.stringify(spotTable),/5.00%（原币）/);
assert.equal(spotTable.children[0].children[0].children.length,6);
assert.equal(spotTable.children[0].children[1].children[0].children[0].tag,'details');
assert.ok(!spotTable.children[0].children[1].children[0].children[0].open);
const foreignState={...observationState,at:'2026-09-23T02:00:00Z',observation:{items:[{...spotTarget,asset:'ETH',name:'以太坊',position:null},spotTarget,{...spotTarget,asset:'GOLD',name:'黄金',unit:'美元/金衡盎司',position:{...spotTarget.position,market_value_cents:200000}}]}};
context.foreignFixture=foreignState;vm.runInContext('state=foreignFixture;observationCategory="COMMODITY"',context);renderWatchlist(foreignState);
assert.deepEqual(macroNodes.get('stocks').children[0].children.slice(1).map(g=>g.attrs['aria-label']),['黄金','比特币','以太坊']);
assert.equal(macroNodes.get('stocks').children[0].children[0].children[0].children[0].textContent,'资产 / 持仓');
assert.match(JSON.stringify(macroNodes.get('stocks')),/美元\/金衡盎司/);
const missingRow=new FakeElement('table');drawGlobalRow(missingRow,{...spotTarget,position:null,spot_quote:null},foreignState);
assert.match(JSON.stringify(missingRow),/未持仓|报价待获取|暂未形成/);assert.doesNotMatch(JSON.stringify(missingRow),/NaN|undefined/);
const expiredRow=new FakeElement('table');drawGlobalRow(expiredRow,{...spotTarget,trade_plan:{status:'ACTIVE',created_at:'2026-09-22T00:00:00Z',valid_until:'2026-09-23T01:00:00Z',payload:{kind:'PAPER_TRADE',thesis:'依据供给变化',holding_days:5,levels:{buy_low_micros:1900000000,buy_high_micros:2000000000,sell_micros:2200000000,stop_micros:1800000000}}}},foreignState);
assert.match(JSON.stringify(expiredRow),/仅供回看/);assert.match(JSON.stringify(expiredRow),/1,900.00 – 2,000.00/);
vm.runInContext('observationCategory="US"',context);renderWatchlist(foreignState);
assert.equal(macroNodes.get('stocks').children[0].tag,'table');assert.match(JSON.stringify(macroNodes.get('stocks')),/暂未发现需要跟踪的美股标的/);
console.log('Three unified tables, native currency costs, fractional quantities, holding order, collapsed research and empty/expired states passed.');
const showPortfolioStrategy=vm.runInContext('renderPortfolioStrategy',context);
macroNodes.clear();
showPortfolioStrategy({portfolio_strategy:{enabled:false}});
assert.equal(macroNodes.get('portfolio-strategy-panel').hidden,true);
showPortfolioStrategy({active_jobs:[],portfolio_strategy:{enabled:true,status:'ACTIVE',created_at:'2026-09-23T08:00:00Z',valid_until:'2026-09-23T09:00:00Z',summary:'跨标的组合判断',decisions:[{name:'示例资产',action:'REDUCE',current_bps:2000,target_bps:1800,current_authorization:true,reason:'<script>unexpected()</script> 相同风险集中'}],risk_groups:[{name:'共同驱动',max_bps:2000,reason:'相关性尚待验证'}]}});
assert.equal(macroNodes.get('portfolio-strategy-panel').hidden,false);
assert.match(JSON.stringify(macroNodes.get('portfolio-strategy-items')),/20.00% → 18.00%/);
assert.match(JSON.stringify(macroNodes.get('portfolio-strategy-items')),/相同风险集中/);
assert.doesNotMatch(JSON.stringify(macroNodes.get('portfolio-strategy-items')),/"tag":"script"/);
showPortfolioStrategy({active_jobs:[],portfolio_strategy:{enabled:true,status:'EXPIRED',last_run:{status:'DEFERRED',error:'模型等待'}}});
assert.match(macroNodes.get('portfolio-strategy-state').textContent,/暂停新增买入.*原止损与退出继续/);
console.log('Portfolio targets, current authorization, safe text and failure state passed.');
const strategyCard=new FakeElement('article');
drawGlobalRow(strategyCard,{...observationTarget('BTC','比特币','COMMODITY'),last_strategy_updated_at:'2026-09-23T01:23:00Z'},foreignState);
assert.match(JSON.stringify(strategyCard),/上一次交易策略更新时间/);
assert.doesNotMatch(JSON.stringify(strategyCard),/尚未发布/);
const emptyStrategyCard=new FakeElement('article');
drawGlobalRow(emptyStrategyCard,observationTarget('ETH','以太坊','COMMODITY'),foreignState);
assert.match(JSON.stringify(emptyStrategyCard),/上一次交易策略更新时间：尚未发布/);
console.log('Per-asset strategy timestamp and missing-publication state passed.');
// Notices: only an admin sees them, oldest open first, one deferred with "稍后再看" stays hidden until reload.
const nextNotice=vm.runInContext('nextNotice',context),noticeActions=vm.runInContext('noticeActions',context);
const noticeState={notices:[{id:'N-1',status:'OPEN',kind:'DECISION'},{id:'N-2',status:'OPEN',kind:'INFO'}]};
assert.equal(nextNotice(noticeState,new Set(),'ADMIN',null,new Set()).id,'N-1');
assert.equal(nextNotice(noticeState,new Set(['N-1']),'ADMIN',null,new Set()).id,'N-2');
// The one on screen stays while open, even when an older one arrives; an answered one never comes back.
assert.equal(nextNotice(noticeState,new Set(),'ADMIN','N-2',new Set()).id,'N-2');
assert.equal(nextNotice(noticeState,new Set(),'ADMIN','N-9',new Set(['N-1'])).id,'N-2');
assert.equal(nextNotice(noticeState,new Set(),'GUEST'),null);
assert.equal(nextNotice({notices:[{id:'N-3',status:'ACKED',kind:'INFO'}]},new Set(),'ADMIN'),null);
assert.equal(nextNotice({},new Set(),'ADMIN'),null);
assert.deepEqual([...noticeActions('DECISION')].map(a=>a.action),['APPROVE','REJECT']);
assert.ok(noticeActions('DECISION').every(a=>a.confirm));
assert.deepEqual([...noticeActions('VETO')].map(a=>a.action),['VETO','ACK']);
assert.deepEqual([...noticeActions('INFO')].map(a=>a.action),['ACK']);
console.log('Notice selection, roles and per-kind answers passed.');
// Industry cards use text nodes and show missing evidence without implying trade permission.
const drawIndustry=vm.runInContext('drawIndustry',context);
const industryBox=new FakeElement('div');
drawIndustry(industryBox,{domains:{ai:{name:'AI 算力基础设施'}},methods:{'1':'二级供应链传导'},members:[{symbol:'sz300499',membership:'DYNAMIC',buy_eligible:false}],hypotheses:[{id:'h',symbol:'sz300499',domain:'ai',method:'1',effective_state:'REVIEW',review_at:'2026-09-30T12:00:00Z',payload:{name:'高澜股份',thesis:'<script>不可信原文</script>',causal_chain:['终端订单','上游供应'],next_check:'核实实际采购份额',invalidation:'客户否认供货',missing:['客户收入占比未知'],counterpoints:['认证不等于量产'],facts:[{entity:'供应商',product:'部件',metric:'订单',value:'未知',unit:'元',period:'本季度',quote:'原始披露文字',published_at:'2026-09-29T12:00:00Z',ready_at:'2026-09-30T12:00:00Z',claim_type:'DISCLOSED',url:'javascript:alert(1)'}]}}]});
assert.match(JSON.stringify(industryBox),/待复核/);assert.match(JSON.stringify(industryBox),/暂停新增买入/);assert.match(JSON.stringify(industryBox),/收入占比未知/);
assert.doesNotMatch(JSON.stringify(industryBox),/javascript:/);
const methodBox=new FakeElement('div');vm.runInContext('drawImpactAssessment',context)(methodBox,{method_gate:true,admitted:true},'new-method');
assert.match(JSON.stringify(methodBox),/初步证据通过/);assert.doesNotMatch(JSON.stringify(methodBox),/通过影响评估/);
console.log('Industry evidence, uncertainty, method gate and safe source-link rendering passed.');
