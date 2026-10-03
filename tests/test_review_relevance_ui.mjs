import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
const source=await readFile(new URL('../ashare/web/app.js',import.meta.url),'utf8');
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.attrs={};this.dataset={};}
  append(...nodes){this.children.push(...nodes);}
  setAttribute(key,value){this.attrs[key]=value;}
  addEventListener(){}
}
const context=vm.createContext({document:{addEventListener(){},createElement:tag=>new Element(tag)},fetch(){throw Error('No real network in synthetic UI test');},AbortSignal,URLSearchParams});
vm.runInContext(source,context,{filename:'app.js'});
vm.runInContext("state={at:'2026-10-03T12:00:00Z'}",context);
const get=name=>vm.runInContext(name,context),nodes=n=>[n,...n.children.flatMap(nodes)];
const visible=n=>[n.textContent,...(n.tag==='details'&&!n.open?n.children.slice(0,1):n.children).map(visible)].join(' ');
const full=n=>nodes(n).map(x=>x.textContent).join(' ');
const close={qty:100,qty_scale:1,average_cost_cents:1000,price_cents:1005,unrealized_cents:500,cumulative_realized_cents:300,quality:'CLOSE',quote_at:'2026-09-30T07:00:00Z',valuation_complete:true};
const p={opening:{valuation_complete:true},closing:close,window_start:'2026-10-01T11:30:00Z',window_end:'2026-10-03T11:30:00Z',positions:[{key:'w:test',name:'合成公司',symbol:'TEST',opening:{},closing:close,period_profit_cents:1200,cumulative_realized_cents:300,research_ids:[]}],research:[],totals:{period_profit_cents:1200,cumulative_profit_cents:800,period_fill_count:2,holding_count:1,reviewed_position_count:1}};
const daily={...p,window_start:'2026-10-02T11:30:00Z',totals:{...p.totals,period_profit_cents:-450,period_fill_count:1},dividends:[{amount_cents:175}]};
const checks=[{check:'CHECK_SYNC_STALE',status:'FAIL',checked:1,failures:1,examples:[{last_sync:{at:'2026-10-01T00:00:00Z',status:'FAILED',phase:'pull'}}]},{check:'CHECK_EXECUTION_EVIDENCE',status:'INSUFFICIENT',checked:1,failures:0},{check:'CHECK_MODEL_IDENTITY',status:'PASS',checked:1,failures:0},{check:'CHECK_SELL_WITHOUT_REASON',status:'NOT_APPLICABLE',checked:0,failures:0}];
const record={id:'review-synthetic',revision:2,model_status:'SUCCEEDED',ready_at:'2026-10-03T11:40:00Z',window_start:'2026-10-02T11:30:00Z',window_end:'2026-10-03T11:30:00Z',payload:{facts:{portfolio:p,context_48h:p,daily_portfolio:daily},consistency_checks:checks,analysis:{summary:'MODEL_FULL_SUMMARY <script>original facts</script>',positions:[{position_key:'w:test',verdict:'PENDING',reason:'MODEL_DETAIL_REASON',pending_points:['等到约定经营数据披露'],supported_points:[],contradicted_points:[],next_check:'检查下一季经营数据是否达到预设条件'}],lessons:[]}}};
const before=JSON.stringify(record);
let box=new Element('section');get('drawDailyReview')(box,record);
let output=visible(box),all=full(box);
for(const text of ['24小时核算窗口','本期盈亏（24小时持仓）','-4.50 元','研究回看前48小时','+12.00 元','24小时已入账现金分红 +1.75 元','不含现金分红','24小时 1 笔成交','程序核验','异常 1 项','缺少依据 1 项','通过 1 项','不适用 1 项','模型：仍待验证','模型建议下次核对'])assert.ok(output.includes(text),text);
assert.doesNotMatch(output,/MODEL_FULL_SUMMARY|MODEL_DETAIL_REASON/);
assert.ok(all.includes('MODEL_FULL_SUMMARY'));assert.ok(all.includes('MODEL_DETAIL_REASON'));
assert.ok(all.includes('上次同步'));assert.ok(all.includes('未成功'));
assert.equal(JSON.stringify(record),before);
assert.ok(nodes(box).every(n=>!['script','img','iframe'].includes(n.tag)&&!n.innerHTML));
// Local overview and old cloud full packets keep the same 24-hour amount.
box=new Element('section');const compact=structuredClone(record);delete compact.payload.facts.daily_portfolio;compact.payload.facts.daily_accounting={...daily,dividend_cents:175,dividends_known:true};get('drawDailyReview')(box,compact);
assert.match(visible(box),/-4\.50 元/);assert.match(visible(box),/已入账现金分红 \+1\.75 元/);
const missing=structuredClone(record);delete missing.payload.facts.daily_portfolio;box=new Element('section');get('drawDailyReview')(box,missing);
assert.match(visible(box),/未提供独立24小时核算/);assert.match(visible(box),/不能用48小时金额代替/);
assert.match(get('reviewFreshness')(record,'2026-10-05T00:00:00Z'),/已超过36小时/);
assert.match(get('reviewFreshness')(record,'2026-10-05T00:00:00Z'),/本期复盘.*仅供历史回看/);
assert.doesNotMatch(get('reviewFreshness')(record,'2026-10-05T00:00:00Z'),/最近复盘|等待下一轮/);
assert.equal(get('reviewFreshness')(record,'2026-10-03T12:00:00Z'),'');
// Superseded and expired findings stay in closed history, while identifiers survive verbatim.
const findingsRecord=structuredClone(record);findingsRecord.presentation={findings:{items:[{ordinal:0,lesson:'当前资料缺口',category:'DATA',to:'engineering_issue',id:'ISSUE_unchanged_1',status:'OPEN',current:true,expires_at:'2026-11-02T12:00:00Z'},{ordinal:1,lesson:'历史研究发现',to:'proposal_draft',id:'CP_unchanged_1',status:'SUPERSEDED',current:false,expired:true}],total:2,omitted:0}};
box=new Element('section');get('drawDailyReview')(box,findingsRecord);
assert.match(visible(box),/当前资料缺口/);assert.doesNotMatch(visible(box),/历史研究发现/);
assert.match(full(box),/ISSUE_unchanged_1|CP_unchanged_1/);assert.match(full(box),/已超过30日展示期限/);
// Only three current cards are expanded; stale advice is never rendered as action text.
const reviewItems=Array.from({length:8},(_,i)=>({id:'SR-'+i,subject_id:'subject-'+i,title:'当前审查 '+i,status:'PENDING',kind:'BATCH',created_at:'2026-10-03T00:00:00Z',reviewer:'chatgpt',summary:'当前摘要 '+i,result:{next_steps:['继续检验']}}));
reviewItems.push({id:'SR-stale',title:'失效审查',status:'STALE',kind:'PROPOSAL',display_bucket:'HISTORY',summary:'STALE_RECOMMEND_MARKER',result:{next_steps:['STALE_ACTION_MARKER']}});
box=new Element('section');get('drawSupervision')(box,{items:reviewItems,reviews_total:45,reviews_omitted:36});
assert.equal(box.children.filter(x=>x.tag==='article').length,3);
assert.match(visible(box),/其他当前审查（5 条）/);assert.match(visible(box),/历史及失效审查（1 条）/);assert.match(visible(box),/另有 36 条/);
assert.doesNotMatch(full(box),/STALE_RECOMMEND_MARKER|STALE_ACTION_MARKER/);
// Repeated incomplete observations are one collapsed collection and never flood first screen.
const base={kind:'RULE',status:'DRAFT',title:'集中度减仓不能证明研究证伪'.repeat(15),created_at:'2026-10-03T00:00:00Z',change:'独立核验一个候选',hypothesis:'需要前向观察',test_plan:'固定观察窗口',failure_criteria:'未达到预定标准',rollback:'不采纳',evidence:{status:'INSUFFICIENT',text:'尚无独立证据'},readiness:{status:'INCOMPLETE',reason:'需补全'},next_step:'补齐证据'};
const proposals=[{...base,id:'ACTIVE',status:'READY',observation_only:false,active_experiment:true},...Array.from({length:35},(_,i)=>({...base,id:'DRAFT_'+i,observation_only:true,display_bucket:i<10?'CURRENT':'HISTORY'}))];
box=new Element('section');get('drawProposals')(box,{items:proposals,total:100});
assert.equal(box.children.filter(x=>x.tag==='article').length,1);
assert.match(visible(box),/待整理的复盘观察（10 份）/);assert.match(visible(box),/历史提案（本页 9 份）/);assert.match(visible(box),/显示 20 \/ 100/);assert.match(visible(box),/另有 80 份/);
assert.ok(visible(box).length<1400,'Default proposal content stays bounded');
assert.ok(nodes(box).filter(n=>n.tag==='details').every(n=>!n.open));
console.log('Review relevance: daily/context/dividend scopes, fact/model separation, closed findings, stale supervision and bounded proposal history passed.');
