import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

class Element {
  constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.attrs={};this._text='';this.hidden=false;this.className='';}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(c=>c.textContent??String(c)).join(' ');}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this._text='';this.children=children;}
  setAttribute(key,value){this.attrs[key]=value;}
  addEventListener(){}
  querySelectorAll(selector){
    const out=[];
    for(const child of this.children){
      if(child.tagName==='DETAILS'&&child.dataset.key&&(!selector.includes('[open]')||child.open))out.push(child);
      if(child.querySelectorAll)out.push(...child.querySelectorAll(selector));
    }
    return out;
  }
}
function walk(node,out=[]){for(const child of node.children||[]){out.push(child);walk(child,out);}return out;}
function cells(node,label){return walk(node).filter(e=>e.dataset&&e.dataset.label===label);}

const section=new Element('section');
const context=vm.createContext({document:{addEventListener(){},createElement:tag=>new Element(tag),getElementById:id=>id==='trader-view'?section:null},
  fetch(){throw Error('No network in rendering tests');},AbortSignal,URLSearchParams});
const source=await readFile(new URL('../ashare/web/app.js',import.meta.url),'utf8');
vm.runInContext(source,context,{filename:'app.js'});
const render=vm.runInContext('renderTraderView',context);
const setRole=role=>vm.runInContext(`currentUser={username:'tester',role:'${role}'}`,context);

const FRESH='2026-11-06T01:59:00+00:00';
const row=(symbol,name,extra)=>({symbol,name,action:'WATCH',code:'OTHER',headline:'观望',reason:'存在尚未解释的交易限制。',
  price_cents:1550,quote_at:FRESH,quote_stale:false,buy_low_cents:null,buy_high_cents:null,stop_cents:null,
  target_cents:null,cost_stop_cents:null,zone_actionable:false,suggested_pct:null,current_pct:null,
  held_qty:0,sellable_qty:0,distance_pct:null,condition:null,plan_valid_until:'2026-11-09T07:00:00+00:00',
  research_at:'2026-11-05T20:00:00+00:00',portfolio_reason:null,open_order:null,blocker_count:0,...extra});
const view={
  version:'trader-view-v1',at:'2026-11-06T02:00:00+00:00',
  regime:{source_status:'OK',as_of_day:'2026-11-05',close:4054.28,ma20:4112.9,ma60:4404.8,
    raw_cap_pct:10,confirmed_cap_pct:10,cap_pct:10,stale:false,label:'偏空',stance:'DEFENSIVE',
    conditions:{close_above_ma60:false,ma20_above_ma60:false},pending_change:null,
    summary:'偏空：保守，总仓位不超过 10%，只做研究和组合都通过的标的。',enforced:false,
    enforced_note:'模拟账户总仓位上限仍按现有规则执行；本页市场状态仅为人工参考，待组合仓位规则上线后才会自动生效。'},
  account:{equity_cents:10000000,gross_pct:62.0,cash_pct:38.0,halted:false,drawdown_bps:300,suggested_max_pct:10,over_cap:true},
  freshness:{research_at:'2026-11-05T20:00:00+00:00',portfolio_at:'2026-11-05T12:00:00+00:00',
    portfolio_valid_until:'2026-11-07T12:00:00+00:00',portfolio_status:'ACTIVE',stale:true,
    notes:['最近一次成功研究距今超过 8 小时，结论可能已过期。']},
  counts:{SELL:1,REDUCE:0,BUY:1,WAIT_PRICE:1,HOLD:1,WATCH:2},
  notice:'页面价位是模拟策略的机械参考，仓位百分比均按账户总资金计算。',
  rows:[
    row('sh600001','止损股',{action:'SELL',code:'COST_STOP_TRIGGER',headline:'卖出',reason:'价格已触及成本止损线，系统将按规则卖出。',
      price_cents:1400,stop_cents:1504,target_cents:1800,cost_stop_cents:1504,current_pct:9.0,held_qty:100,sellable_qty:100}),
    row('sh600002','买入股',{action:'BUY',code:'IN_ZONE',headline:'买入',reason:'现价 15.50 元在计划买入区间内，可按组合目标仓位买入。',
      buy_low_cents:1500,buy_high_cents:1600,stop_cents:1400,target_cents:1800,zone_actionable:true,suggested_pct:12}),
    row('sh600003','等点股',{action:'WAIT_PRICE',code:'ABOVE_ZONE',headline:'等买点',reason:'现价高于买入区间上沿，等待回落至区间内再买入。',
      price_cents:1700,buy_low_cents:1500,buy_high_cents:1600,stop_cents:1400,target_cents:1800,
      zone_actionable:true,suggested_pct:12,distance_pct:6.25}),
    row('sh600004','持有股',{action:'HOLD',code:'HELD',headline:'持有',reason:'持仓正常，未触发退出条件。',
      held_qty:200,sellable_qty:200,current_pct:8.0,cost_stop_cents:1400,stop_cents:1400}),
    row('sh600005','观望股',{code:'RESEARCH_VETO',reason:'研究判断暂不支持买入。接下来观察：收入增速',blocker_count:3}),
    row('sh600006','趋势股',{code:'TREND',reason:'价格趋势尚未满足买入条件，等待趋势确认。',
      condition:'需收盘站上60日线（15.50 元），且20日线（16.00 元）高于60日线；最近收盘 15.40 元。',
      buy_low_cents:1500,buy_high_cents:1600}),
  ],
};

setRole('ADMIN');
render({trader_view:view});
assert.equal(section.hidden,false);
const text=section.textContent;
for(const expected of ['今日操作','偏空','当前模拟仓位 62%','高于当前有效总仓上限',
  '只做研究和组合都通过的标的','沪深300 收盘 4054.28','60日线 4404.80','截至 2026-11-05',
  '模拟账户总仓位上限仍按现有规则执行','研究更新','组合判断','有效至','价格仅供参考',
  '卖出 1 · 减仓 0 · 买入 1 · 等买点 1 · 持有 1 · 观望 2',
  '止损股','买入股','等点股','持有股','成本止损 15.04','今天没有需要操作的股票。']) {
  if(expected==='今天没有需要操作的股票。')assert.ok(!text.includes(expected),'has actionable rows');
  else assert.ok(text.includes(expected),expected);
}
// Regime badge carries the stance class.
const badge=walk(section).find(e=>e.className&&e.className.includes('regime-badge'));
assert.ok(badge.className.includes('regime-defensive'),badge.className);
// Actionable table: zone, suggested size and distance are visible.
const mainTable=walk(section).find(e=>e.tagName==='TABLE');
const zoneCells=cells(mainTable,'买入价').map(c=>c.textContent);
assert.ok(zoneCells.some(t=>t.includes('15.00–16.00')),'BUY zone visible');
assert.ok(zoneCells.some(t=>t.includes('距区间 +6.25%')),'WAIT_PRICE distance visible');
assert.ok(cells(mainTable,'建议仓位').some(c=>c.textContent.includes('12.00%')),'suggested pct visible');
assert.ok(cells(mainTable,'股票').some(c=>c.textContent.includes('100 股')&&c.textContent.includes('9.00%')),'held row shows qty and pct');
assert.ok(cells(mainTable,'理由').some(c=>c.textContent.includes('成本止损线')),'reason visible');
// WATCH rows fold into the 观望 details; their buy-zone column stays a dash and conditions show.
const fold=walk(section).find(e=>e.tagName==='DETAILS');
assert.ok(fold,'watch details exists');
assert.ok(fold.textContent.includes('观望（2 只）'),fold.textContent);
assert.ok(fold.textContent.includes('需收盘站上60日线'),'trend condition visible');
const watchZone=cells(fold,'买入价');
assert.equal(watchZone.length,2);
assert.ok(watchZone.every(c=>c.textContent.trim()==='—'),'WATCH buy zone is always a dash');
// Guest sees nothing; the section is emptied.
setRole('GUEST');
render({trader_view:view});
assert.equal(section.hidden,true);
assert.equal(section.children.length,0);
assert.equal(section.textContent,'');
// Missing view stays hidden and empty for admins too.
setRole('ADMIN');
render({});
assert.equal(section.hidden,true);
assert.equal(section.children.length,0);
// Fresh (non-stale) data: no caution, and an empty actionable set prints the empty line.
const freshView={...view,freshness:{...view.freshness,stale:false,notes:[]},rows:[...view.rows.slice(4)],counts:{SELL:0,REDUCE:0,BUY:0,WAIT_PRICE:0,HOLD:0,WATCH:2}};
render({trader_view:freshView});
assert.equal(section.hidden,false);
assert.ok(!section.textContent.includes('价格仅供参考'),'fresh view has no stale caution');
assert.ok(section.textContent.includes('今天没有需要操作的股票。'),'empty actionable line');
const html=await readFile(new URL('../ashare/web/index.html',import.meta.url),'utf8');
assert.match(html,/id="trader-view"[^>]*hidden/);
assert.match(html,/id="trader-view"[^>]*admin-only/);
console.log('Trader view UI: regime banner, freshness caution, action table, watch folding, guest/admin gating passed.');
