import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import vm from 'node:vm';

const [source,html]=await Promise.all([
  readFile(new URL('../ashare/web/app.js',import.meta.url),'utf8'),
  readFile(new URL('../ashare/web/index.html',import.meta.url),'utf8'),
]);

// Keep this harness independent of test_ui.mjs: exercise the actual renderers,
// including their caches, with an intentionally unredacted API response.
class FakeElement {
  constructor(tag){
    this.tag=tag;this.tagName=tag.toUpperCase();this.children=[];this.attrs={};
    this.dataset={};this.style={};this.listeners={};this.hidden=false;this._text='';
    this.classList={contains:name=>(this.className||'').split(/\s+/).includes(name)};
  }
  set textContent(value){this._text=String(value??'');this.children=[];}
  get textContent(){return this._text+this.children.map(c=>typeof c==='string'?c:c.textContent).join('');}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this._text='';this.children=children;}
  setAttribute(key,value){this.attrs[key]=String(value);}
  addEventListener(type,handler){this.listeners[type]=handler;}
  focus(){}
  showModal(){this.open=true;}
  close(){this.open=false;}
  querySelectorAll(selector){
    const descendants=this.children.flatMap(c=>c instanceof FakeElement?[c,...c.querySelectorAll('*')]:[]);
    if(selector==='*')return descendants;
    if(selector==='details[data-key]')return descendants.filter(c=>c.tag==='details'&&c.dataset.key);
    if(selector==='details[open][data-key]')return descendants.filter(c=>c.tag==='details'&&c.dataset.key&&c.open);
    return [];
  }
}

function harness(user,pathname='/',statusPayload=dashboard,stockPayload=stockDetail){
  const nodes=new Map(),errors=[],requests=[];
  const node=id=>{if(!nodes.has(id)){const element=new FakeElement('div');element.id=id;nodes.set(id,element);}return nodes.get(id);};
  for(const match of html.matchAll(/<([a-z]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)){
    const element=new FakeElement(match[1]);element.id=match[3];
    element.className=match[2].match(/class="([^"]*)"/)?.[1]||'';
    element.hidden=/\shidden(?:\s|$)/.test(match[2]);nodes.set(element.id,element);
  }
  const context=vm.createContext({
    document:{body:new FakeElement('body'),addEventListener(){},getElementById:node,
      createElement:tag=>new FakeElement(tag),createTextNode:text=>String(text),querySelectorAll:()=>[]},
    location:{pathname,hash:'',assign(){throw Error('Unexpected authentication redirect');}},
    history:{state:null,replaceState(){}},AbortSignal,URLSearchParams,
    console:{error:(...args)=>errors.push(args)},fetch(){throw Error('Unexpected real network');},
  });
  vm.runInContext(source,context,{filename:'app.js'});
  context.testUser=user;vm.runInContext('currentUser=testUser',context);
  const evaluate=code=>vm.runInContext(code,context);
  const call=(name,...args)=>evaluate(name)(...args);
  const client=evaluate('api');
  client.transport=async path=>{
    requests.push(path);
    const payload=path.startsWith('/api/stock?')?stockPayload:path.startsWith('/api/observations?')?statusPayload.observation:statusPayload;
    assert.ok(path==='/api/status'||path.startsWith('/api/stock?')||path.startsWith('/api/observations?'),'Unexpected API request: '+path);
    return {ok:true,status:200,json:async()=>payload};
  };
  return {context,nodes,node,errors,requests,evaluate,call,
    setPayloads(status,detail){statusPayload=status;stockPayload=detail;}};
}

const at='2026-10-24T02:00:00Z',until='2099-01-01T00:00:00Z';
const secret={
  overview:'私有摘要：订单与利润兑现路径',analysis:'私有公司分析：完整研究结论',
  membership:'私有跟踪理由：需求验证',industry:'私有产业假设：供应约束',
  check:'私有产业进度：渠道调研结果',coverage:'私有资料来源：待核验线索',
  portfolio:'私有组合结论：风险集中判断',allocation:'私有组合依据：相关性推导',
  followup:'私有待办：补充一手证据',archive:'私有归档：历史筛选依据',
  global:'私有全球研究：宏观传导链',decision:'私有买卖依据：估值与触发条件',
  dimension:'私有研究维度：经营假设',evidence:'私有引文：客户预算细节',
};
const levels={buy_low_cents:9700,buy_high_cents:9900,sell_cents:10800,stop_cents:9200};
const stock={
  name:'测试公司',symbol:'sh600941',membership:'CORE',category:'CN',research_status:'TRACKING',
  reason:secret.membership,entry_reason:secret.membership,entry_allowed:true,
  quote:{price_cents:9800,observed_at:at},last_research_at:at,
  report:{overview:secret.overview,analysis:secret.analysis,facts:[{evidence_id:'doc:1',quote:secret.evidence}],
    decision:{inclination:secret.decision,key_evidence:[{evidence_id:'doc:1',implication:secret.evidence}],pricing:secret.decision,trigger:secret.decision,invalidation:secret.decision}},
  last_decision:{at,action:'BUY',status:'SUBMITTED',reason:secret.decision,buy_blockers:[]},
  plan:{effective_status:'ACTIVE',activated_at:at,valid_until:until,model_status:'SUCCEEDED',
    payload:{kind:'PAPER_TRADE',levels,blockers:[],risk_parameters:{paper_entry_band_bps:200}}},
};
const holding={symbol:stock.symbol,name:stock.name,origin:'watchlist',qty:100,sellable_qty:100,
  market_value_cents:980000,average_cost_cents:9700,unrealized_cents:10000,unrealized_return_pct:1.03,weight_pct:9.8,quote_at:at};
const globalTarget={asset:'US:TEST',symbol:'US:TEST',name:'海外测试公司',category:'US',membership:'FIXED',fixed:true,
  links:[],pool_reason:secret.global,entry_reason:secret.global,
  spot_quote:{price_micros:100000000,fx_micros:7000000,observed_at:at,fx_at:at},
  trade_plan:{status:'ACTIVE',created_at:at,valid_until:until,payload:{kind:'PAPER_TRADE',thesis:secret.global,holding_days:5,
    levels:{buy_low_micros:99000000,buy_high_micros:101000000,sell_micros:110000000,stop_micros:90000000}}},
};
const archived={...globalTarget,asset:'US:OLD',symbol:'US:OLD',name:'归档测试公司',pool_tier:'ARCHIVED',pool_reason:secret.archive};
const industry={enabled:true,domains:{technology:{name:'科技'}},
  members:[{symbol:stock.symbol,name:stock.name,category:'CN',membership:'CORE',research_status:'TRACKING',reason:secret.membership,buy_eligible:true}],
  hypotheses:[{id:'hypothesis',symbol:stock.symbol,domain:'technology',method:'DEMAND',state:'ACTIVE',
    created_at:at,review_at:until,payload:{thesis:secret.industry,next_check:secret.followup,causal_chain:[secret.industry],facts:[]}}],
  checks:[{step:'technology',status:'DONE',payload:{methods:{DEMAND:secret.check}}}],
  coverage:[{domain:'technology',source:'cninfo_industry',status:'FAILED',detail:secret.coverage}],history:[],forecasts:[],
};
const followups={updated_at:at,items:[{id:'followup',symbol:stock.symbol,name:stock.name,title:secret.followup,
  state_name:'等待补充',owner:'SYSTEM',owner_name:'系统',reason:secret.followup,impact:secret.followup,
  next_action:secret.followup,trigger:secret.followup,completion:secret.followup}]};
const fill={occurred_at:at,symbol:stock.symbol,side:'BUY',qty:100,price_cents:9800,fee_cents:100,realized_cents:0};
const decision={at,symbol:stock.symbol,action:'BUY',status:'SUBMITTED',reason:secret.decision};
const dashboard={
  at,scheduler_enabled:true,market_phase:'CLOSED',watchlist:[stock],industry,
  next_runs:[],jobs:[],active_jobs:[],state:{heartbeat:at},source_checks:[],schedule:{collection:[],slots:[],review:'19:30'},
  account:{equity_cents:10000000,available_cents:9000000,cash_cents:9000000,valuation_complete:true},
  portfolio:{holdings:[holding],total_profit_cents:10000,stock_weight_pct:9.8,cash_weight_pct:90.2,unrealized_cents:10000},
  trade_effects:{totals:{fill_count:1},buy:{name:stock.name,last_fill_at:at,remaining_qty:100,
    filled_qty:100,requested_qty:100,average_price_cents:9800,fill_count:1,fee_cents:100,
    order_status:'FILLED',unrealized_cents:10000,unrealized_return_pct:1.03,quote_at:at,reason:secret.decision}},
  decisions:[decision],fills:[fill],reviews:[],followups,
  observation:{items:[globalTarget],archive_count:1,archive_offset:0,archived_items:[archived]},
  portfolio_strategy:{enabled:true,status:'ACTIVE',created_at:at,valid_until:until,summary:secret.portfolio,
    decisions:[{name:stock.name,action:'ALLOW',current_bps:980,target_bps:1200,reason:secret.allocation,current_authorization:true}],risk_groups:[]},
  dynamic:{enabled:true,items:[],state:{},balance:{market_value_cents:0,reserved_cents:0},fills:[],
    recent_news:[{title:secret.global,url:'https://example.com/news',source:'研究资料',published_at:at}]},
};
const stockDetail={stock,position:holding,industry,followups,
  dimensions:[{id:'business',title:'经营研究',origin:'EXISTING_REPORT',summary:secret.dimension,evidence_ids:['doc:1']}],
  trade_statistics:{fill_count:1,realized_cents:0,fee_cents:100},
  history:{fills:{total:1,items:[fill]},orders:{total:1,items:[{created_at:at,side:'BUY',qty:100,limit_cents:9800,status:'FILLED'}]},decisions:{total:1,items:[decision]}},
};
const privatePanels=['industry-research-panel','portfolio-strategy-panel','followup-section','observation-archive'];
const privateContents=['industry-hypotheses','portfolio-strategy-items','portfolio-strategy-groups','portfolio-strategy-summary','followups','observation-archive-items'];
function assertPrivatePanelsHidden(h){
  for(const id of privatePanels)assert.equal(h.node(id).hidden,true,id+' must remain hidden');
  for(const id of privateContents)assert.equal(h.node(id).textContent,'',id+' must not contain research');
  for(const id of ['stocks','stock-content','stock-history','decision-summary','decisions','effects','macro-cases','dynamic-news']){
    const text=h.node(id).textContent;
    for(const value of Object.values(secret))assert.ok(!text.includes(value),id+' leaked '+value);
  }
  assert.doesNotMatch(h.node('stocks').textContent,/跟踪理由与研究摘要|研究逻辑与动态影响|查看公司全部研究与历史/);
}

// Static wrappers protect the initial document before session acquisition.
for(const id of [...privatePanels,'dynamic-tab','dynamic-section']){
  const opening=html.match(new RegExp('<[^>]+\\bid="'+id+'"[^>]*>'))?.[0];
  assert.ok(opening?.includes('admin-only'),id+' needs the initial admin-only wrapper');
}

for(const user of [null,{role:'GUEST'},{role:'VIEWER'},{role:'admin'},{}]){
  const h=harness(user);
  assert.equal(h.call('canViewResearch'),false);
  await h.call('refresh');
  assert.deepEqual(h.errors,[],'Dashboard refresh must complete for '+JSON.stringify(user));
  assertPrivatePanelsHidden(h);
  assert.match(h.node('stocks').textContent,/测试公司/);
  assert.match(h.node('stocks').textContent,/97\.00 – 99\.00/);
  assert.match(h.node('stocks').textContent,/9,800\.00 元/);
  assert.match(h.node('fills').textContent,/100 \/ 98\.00/);
  assert.equal(h.node('stocks').children[0].children[1].children.length,1,'No hidden per-stock research row');
  const calls=h.requests.length;
  assert.equal(await h.call('loadObservationArchive',25,'US:OLD'),false);
  assert.equal(h.requests.length,calls,'Guest archive paging must not make a request');
  h.call('selectBoard','dynamic');
  assert.equal(h.evaluate('currentBoard'),'watchlist','A direct dynamic tab selection must fail closed');
  for(const market of ['US','CN']){
    h.evaluate("researchListFilter='ALL';observationCategory="+JSON.stringify(market));
    h.call('renderWatchlist',dashboard);
    assertPrivatePanelsHidden(h);
    assert.match(h.node('stocks').textContent,market==='US'?/海外测试公司/:/测试公司/);
    assert.equal(h.node('stocks').children[0].children[1].children.length,1);
  }
  await h.call('refresh');
  assert.deepEqual(h.errors,[]);
  assertPrivatePanelsHidden(h);
  const detail=harness(user,'/stocks/'+stock.symbol);
  await detail.call('refresh');
  assert.deepEqual(detail.errors,[]);
  assertPrivatePanelsHidden(detail);
  assert.match(detail.node('stock-content').textContent,/测试公司.*98\.00 元/);
  assert.match(detail.node('stock-content').textContent,/买入参考区间97\.00 – 99\.00/);
  assert.match(detail.node('stock-content').textContent,/持仓 100 股/);
  assert.match(detail.node('stock-history').textContent,/成交记录.*委托记录/);
  assert.doesNotMatch(detail.node('stock-history').textContent,/买卖依据/);
  assert.doesNotMatch(detail.node('stock-content').textContent,/最新公司研究结论|经营研究|参考价位的计算与用途|还缺什么与下一步/);
}

const admin=harness({role:'ADMIN'},'/stocks/'+stock.symbol);
await admin.call('refresh');
assert.deepEqual(admin.errors,[],'Admin dashboard and stock page must still render');
assert.equal(admin.call('canViewResearch'),true);
for(const id of privatePanels)assert.equal(admin.node(id).hidden,false,id+' must remain available to admin');
assert.match(admin.node('stocks').textContent,/跟踪理由与研究摘要/);
assert.ok(admin.node('stocks').textContent.includes(secret.overview));
assert.ok(admin.node('stocks').textContent.includes(secret.industry));
assert.ok(admin.node('industry-hypotheses').textContent.includes(secret.check));
assert.ok(admin.node('industry-hypotheses').textContent.includes(secret.coverage));
assert.ok(admin.node('portfolio-strategy-summary').textContent.includes(secret.portfolio));
assert.ok(admin.node('portfolio-strategy-items').textContent.includes(secret.allocation));
assert.ok(admin.node('followups').textContent.includes(secret.followup));
assert.ok(admin.node('observation-archive-items').textContent.includes(secret.archive));
assert.ok(admin.node('dynamic-news').textContent.includes(secret.global));
assert.match(admin.node('stock-content').textContent,/最新公司研究结论|经营研究/);
for(const value of [secret.analysis,secret.dimension,secret.evidence])assert.ok(admin.node('stock-content').textContent.includes(value));
assert.match(admin.node('stock-history').textContent,/买卖依据/);
assert.ok(admin.node('stock-history').textContent.includes(secret.decision));
assert.ok(admin.node('effects').textContent.includes(secret.decision));
admin.evaluate("researchListFilter='ALL';observationCategory='US'");admin.call('renderWatchlist',dashboard);
assert.match(admin.node('stocks').textContent,/研究逻辑与动态影响/);
assert.ok(admin.node('stocks').textContent.includes(secret.global));
admin.call('selectBoard','dynamic');assert.equal(admin.evaluate('currentBoard'),'dynamic');
const before=admin.requests.length;await admin.call('loadObservationArchive',0);
assert.equal(admin.requests.length,before+1);assert.equal(admin.node('observation-archive').open,true);

// A role downgrade must invalidate stock render caches and clear already-rendered
// private panel contents even when the backend returns the identical payload.
admin.evaluate("currentUser={role:'GUEST'};observationCategory='CN'");
await admin.call('refresh');
assert.deepEqual(admin.errors,[]);assertPrivatePanelsHidden(admin);
assert.doesNotMatch(admin.node('stock-history').textContent,/买卖依据/);
admin.call('selectBoard','dynamic');assert.equal(admin.evaluate('currentBoard'),'watchlist');

// Render the real server allowlists as well as the unredacted fixtures. This
// catches missing fields in the result-only API contract without touching data.
const projected=JSON.parse(execFileSync(process.env.PYTHON||'python3',['-c',
  'import json, runpy, sys; p=runpy.run_path(sys.argv[1]); v=json.load(sys.stdin); print(json.dumps({"status":p["guest_status"](v["status"]),"stock":p["guest_stock"](v["stock"])}))',
  fileURLToPath(new URL('../ashare/privacy.py',import.meta.url)),
],{input:JSON.stringify({status:dashboard,stock:stockDetail}),encoding:'utf8'}));
for(const value of Object.values(secret))assert.ok(!JSON.stringify(projected).includes(value),'Server projection leaked '+value);
const guest=harness({role:'GUEST'},'/stocks/'+stock.symbol,projected.status,projected.stock);
await guest.call('refresh');
assert.deepEqual(guest.errors,[],'Real guest_status and guest_stock projections must render successfully');
assertPrivatePanelsHidden(guest);
assert.match(guest.node('stocks').textContent,/测试公司.*97\.00 – 99\.00/);
assert.match(guest.node('stock-content').textContent,/持仓 100 股/);
assert.match(guest.node('stock-history').textContent,/成交记录.*委托记录/);
guest.evaluate("researchListFilter='ALL';observationCategory='US'");guest.call('renderWatchlist',projected.status);
assert.match(guest.node('stocks').textContent,/海外测试公司/);assertPrivatePanelsHidden(guest);

// An open ADMIN page must accept a server-side revocation when another tab
// changes the shared login, without relying on a new boot/session response.
const switched=harness({role:'ADMIN'},'/stocks/'+stock.symbol);
switched.call('initStrategyDownload');await switched.call('refresh');
assert.equal(switched.node('strategy-download').hidden,false);
assert.ok(switched.node('stock-content').textContent.includes(secret.analysis));
switched.setPayloads(projected.status,projected.stock);
await switched.call('refresh');
assert.deepEqual(switched.errors,[]);
assert.equal(switched.evaluate('currentUser.role'),'GUEST');
assert.equal(switched.context.document.body.dataset.role,'GUEST');
assert.equal(switched.node('strategy-download').hidden,true);
assertPrivatePanelsHidden(switched);
assert.match(switched.node('stock-content').textContent,/持仓 100 股/);
await switched.call('refresh');assertPrivatePanelsHidden(switched);

// A stock endpoint can be the first response after the cookie changes. It must
// revoke access even if the dashboard has not refreshed yet.
const stockSwitched=harness({role:'ADMIN'},'/stocks/'+stock.symbol);
stockSwitched.call('initStrategyDownload');await stockSwitched.call('refreshStock');
assert.ok(stockSwitched.node('stock-content').textContent.includes(secret.analysis));
assert.ok(stockSwitched.node('stock-history').textContent.includes(secret.decision));
stockSwitched.setPayloads(dashboard,projected.stock);
await stockSwitched.call('refreshStock');
assert.equal(stockSwitched.evaluate('currentUser.role'),'GUEST');
assert.equal(stockSwitched.context.document.body.dataset.role,'GUEST');
assert.equal(stockSwitched.node('strategy-download').hidden,true);
assertPrivatePanelsHidden(stockSwitched);
assert.match(stockSwitched.node('stock-content').textContent,/持仓 100 股/);
assert.doesNotMatch(stockSwitched.node('stock-history').textContent,/买卖依据/);

// A pending approval must not keep its private modal on screen after another
// tab changes the login. Detached buttons and late responses remain inert.
const privateNotice={id:'private-notice',kind:'DECISION',status:'OPEN',author:'agent',created_at:at,
  title:secret.industry,body:secret.analysis};
const noticeFields=['notice-body','notice-title','notice-meta','notice-actions','notice-kind','notice-feedback'];
function assertNoticeCleared(h){
  assert.equal(h.node('notice-dialog').open,false,'A revoked approval modal must close even while busy');
  for(const id of noticeFields)assert.equal(h.node(id).textContent,'',id+' must clear on revocation');
  assert.equal(h.node('notice-feedback').hidden,true);
  assert.equal(h.evaluate('noticeShown'),null);assert.equal(h.evaluate('noticePending'),null);
}
for(const [endpoint,outcome] of [['status','resolve'],['stock','reject']]){
  const privateStatus={...dashboard,notices:[privateNotice]};
  const h=harness({role:'ADMIN'},'/stocks/'+stock.symbol,privateStatus);
  await h.call('refresh');
  assert.equal(h.node('notice-dialog').open,true);
  assert.equal(h.node('notice-body').textContent,secret.analysis);
  let finish,fail,posts=0;
  h.evaluate('api').post=()=>{posts++;return new Promise((resolve,reject)=>{finish=resolve;fail=reject;});};
  await h.node('notice-actions').children[0].listeners.click();
  assert.equal(h.evaluate('noticePending'),'APPROVE');
  const staleButton=h.node('notice-actions').children[0];
  const pending=staleButton.listeners.click();
  assert.equal(posts,1);assert.equal(h.evaluate('noticeBusy'),true);
  h.setPayloads(projected.status,projected.stock);
  await h.call(endpoint==='status'?'refresh':'refreshStock');
  assertNoticeCleared(h);
  await staleButton.listeners.click();
  h.call('drawNoticeActions',privateNotice);h.call('renderNotice',privateStatus);
  assertNoticeCleared(h);assert.equal(posts,1,'A stale approval button cannot send another request');
  const requests=h.requests.length;
  if(outcome==='resolve')finish({status:'APPROVED'});else fail(Error(secret.decision));
  await pending;
  assert.equal(h.evaluate('noticeBusy'),false);assertNoticeCleared(h);
  assert.equal(h.requests.length,requests,'A revoked completion cannot refresh or redraw a private notice');
  assert.deepEqual(h.errors,[]);
}

console.log('Research privacy UI checks passed: default-deny roles, full-data and real guest API dashboard/stock rendering, archive request guard, market and tab switching, cross-tab revocation including pending notices, trade effects, and unchanged admin research.');
