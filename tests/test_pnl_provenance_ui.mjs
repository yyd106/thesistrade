import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

class Element {
  constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.attrs={};this.dataset={};this.style={};}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  setAttribute(key,value){this.attrs[key]=value;}
  querySelectorAll(){return [];}
  addEventListener(){}
}
const elements=new Map(),element=id=>{
  if(!elements.has(id))elements.set(id,new Element('section'));
  return elements.get(id);
};
const context=vm.createContext({document:{addEventListener(){},createElement:tag=>new Element(tag),createTextNode:text=>Object.assign(new Element('#text'),{textContent:text}),getElementById:element,querySelectorAll(){return [];}},fetch(){throw Error('No real network in synthetic UI test');},AbortSignal,URLSearchParams});
vm.runInContext(await readFile(new URL('../ashare/web/app.js',import.meta.url),'utf8'),context,{filename:'app.js'});
const get=name=>vm.runInContext(name,context),full=node=>[node.textContent,...node.children.map(full)].join(' ');
const setState=value=>{context.fixtureState=value;vm.runInContext('state=fixtureState',context);};
const closing={qty:100,cost_cents:100000,market_value_cents:134100,average_cost_cents:1000,price_cents:1341,unrealized_cents:34100,cumulative_realized_cents:-26179,equity_cents:10033021,quality:'CLOSE',quote_at:'2026-09-30T08:14:58Z',valuation_complete:true};
const position={key:'watchlist:TEST',symbol:'TEST',origin:'watchlist',name:'合成持仓',opening:{},closing,period_profit_cents:0,cumulative_realized_cents:-26179,research_ids:[]};
const provenance={key:position.key,closing:{quote_at:closing.quote_at,quote_source:'tencent_public_research',quote_source_label:'腾讯公开行情',quote_first_seen_at:'2026-09-30T08:15:03Z',quote_time_notice:'报价时间取自腾讯接口返回的行情时间字段；首次采集时间另列。15:00后的时间戳仅表示供应商盘后快照时间，不能据此认定发生盘后成交或已核验为交易所收盘价。'}};
const portfolio={window_start:'2026-09-29T11:30:00Z',window_end:'2026-10-01T11:30:00Z',opening:{valuation_complete:true},closing,positions:[position],research:[],totals:{period_profit_cents:0,cumulative_profit_cents:7921,holding_count:1,period_fill_count:0}};
const daily={...portfolio,window_start:'2026-09-30T11:30:00Z',dividends_known:true,dividend_cents:0,cumulative_dividend_cents:25100,account_cumulative_profit_cents:33021,reconciliation_difference_cents:0,accounting_basis:'FROZEN_EQUITY_WITH_LEDGER_FLOWS',quote_provenance:[provenance]};
const record={id:'synthetic-pnl',revision:1,model_status:'SUCCEEDED',window_start:daily.window_start,window_end:daily.window_end,ready_at:'2026-10-01T11:40:00Z',payload:{facts:{portfolio,context_48h:portfolio},analysis:{},consistency_checks:[]},presentation:{daily,context:{...portfolio,quote_provenance:[provenance]}}};
const state={at:'2026-10-01T12:00:00Z',account:{equity_cents:10032721,available_cents:9898921,cash_cents:9898921,valuation_complete:true},portfolio:{total_profit_cents:32721,unrealized_cents:33800,cumulative_realized_cents:-26179,cumulative_dividend_cents:25100,cash_weight_pct:98.6,stock_weight_pct:1.4,holdings:[{symbol:'TEST',name:'合成持仓',origin:'watchlist',qty:100,sellable_qty:100,cost_cents:100000,market_value_cents:133800,unrealized_cents:33800,weight_pct:1.4,quote_at:'2026-09-30T06:56:00Z',quote_source:'tencent_public_research'}]},trade_effects:{totals:{fill_count:0}},reviews:[record],next_runs:[],decisions:[],fills:[],watchlist:[]};
const draw=(r=record,s=state)=>{setState(s);const box=new Element('section');get('drawDailyReview')(box,r);return full(box);};
const before=JSON.stringify(record);
let text=draw();
for(const value of ['成交与持仓累计 +79.21 元 + 累计现金分红 +251.00 元 = 账户累计 +330.21 元','报价快照差 -3.00','当前账户累计 +327.21 元','24小时已入账现金分红 0.00 元','行情源盘后时间戳','16:14:58','腾讯公开行情','系统首次取得','16:15:03','供应商盘后快照时间'])assert.ok(text.includes(value),value);
assert.equal(JSON.stringify(record),before,'The historical review remains immutable');

// A changed position or realized result cannot be described as just a quote change.
for(const change of [s=>{s.portfolio.holdings[0].qty=50;},s=>{s.portfolio.holdings[0].cost_cents=99999;},s=>{s.portfolio.cumulative_realized_cents+=100;}]){
  const changed=structuredClone(state);change(changed);text=draw(record,changed);
  assert.doesNotMatch(text,/报价快照差/);assert.match(text,/持仓浮动变化/);
}
const missingCosts=structuredClone(record),missingHoldingCost=structuredClone(state);
delete missingCosts.payload.facts.portfolio.positions[0].closing.cost_cents;delete missingHoldingCost.portfolio.holdings[0].cost_cents;
assert.doesNotMatch(draw(missingCosts,missingHoldingCost),/报价快照差/,'Missing costs are not proof of identical holdings');
const laterPortfolio=structuredClone(record);laterPortfolio.payload.facts.portfolio.window_end='2026-10-02T11:30:00Z';
assert.doesNotMatch(draw(laterPortfolio),/报价快照差/,'Different cutoff positions cannot establish price-only attribution');

// Unknown lifetime values stay unknown even when the 24-hour dividend is zero.
for(const key of ['cumulative_dividend_cents','account_cumulative_profit_cents']){
  const missing=structuredClone(record);delete missing.presentation.daily[key];text=draw(missing);
  assert.match(text,/旧记录尚无截止累计分红对账/);
  assert.doesNotMatch(text,/复盘截止对账：|与账户概览对照/);
  if(key==='account_cumulative_profit_cents')assert.match(text,/截止账户累计盈亏（含分红） — 元/);
}
const noCurrent=structuredClone(state);delete noCurrent.portfolio.cumulative_dividend_cents;
assert.doesNotMatch(draw(record,noCurrent),/与账户概览对照/);
for(const at of ['invalid','2026-10-01T11:29:59Z'])assert.doesNotMatch(draw(record,{...state,at}),/与账户概览对照/);
const mismatch=structuredClone(record);mismatch.presentation.daily.account_cumulative_profit_cents+=100;
text=draw(mismatch);assert.match(text,/分项合计 \+330\.21 元/);assert.match(text,/截止账户权益计算值为 \+331\.21 元/);assert.match(text,/仍差 \+1\.00 元/);
const estimated=structuredClone(record);estimated.presentation.daily.accounting_basis='TRADE_PNL_PLUS_LEDGER_DIVIDENDS';estimated.presentation.daily.reconciliation_difference_cents=null;
assert.match(draw(estimated),/未提供可独立核对的历史账户权益/);

// Provenance follows the 48-hour position's exact quote, with daily fallback.
const nested=structuredClone(record);nested.presentation.daily.quote_provenance=structuredClone(nested.presentation.daily.quote_provenance);nested.presentation.daily.quote_provenance[0].closing.quote_source_label='不同日窗口来源';
text=draw(nested);assert.match(text,/来源：腾讯公开行情/);assert.doesNotMatch(text,/不同日窗口来源/);
delete nested.presentation.context.quote_provenance;
assert.match(draw(nested),/不同日窗口来源/);
const unmatched=structuredClone(record);unmatched.presentation.context.quote_provenance[0].closing.quote_at='2026-09-29T08:14:58Z';
text=draw(unmatched);assert.match(text,/来源：来源未记录/);assert.doesNotMatch(text,/系统首次取得/);

// Account view states both its lifetime dividend and the current quote timestamp.
setState(state);get('renderAccount')(state);text=full(element('metrics'))+' '+full(element('valuation'));
for(const value of ['327.21','累计现金分红 251.00 元','当前持仓浮动合计 +338.00 元','14:56:00','腾讯公开行情','每日复盘保留研究端截止时的报价快照'])assert.ok(text.includes(value),value);

// The unchanged historical record must re-render when only current prices change.
setState(state);get('renderActivity')(state);assert.match(full(element('reviews')),/当前账户累计 \+327\.21 元/);
const refreshed=structuredClone(state);refreshed.portfolio.total_profit_cents+=100;refreshed.portfolio.unrealized_cents+=100;
setState(refreshed);get('renderActivity')(refreshed);text=full(element('reviews'));
assert.match(text,/当前账户累计 \+328\.21 元/);assert.match(text,/报价快照差 -2\.00/);assert.doesNotMatch(text,/当前账户累计 \+327\.21 元/);
console.log('P&L reconciliation: lifetime vs daily dividends, historical vs current quotes, conservative attribution, missing values, provenance and same-hour refresh passed.');
