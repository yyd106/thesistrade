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
const context=vm.createContext({document:{addEventListener(){},createElement:tag=>new Element(tag),createTextNode:text=>Object.assign(new Element('#text'),{textContent:text}),getElementById:element,querySelectorAll(){return [];}},fetch(){throw Error('No network in synthetic test');},AbortSignal,URLSearchParams});
vm.runInContext(await readFile(new URL('../ashare/web/app.js',import.meta.url),'utf8'),context,{filename:'app.js'});
const get=name=>vm.runInContext(name,context),full=node=>[node.textContent,...node.children.map(full)].join(' ');
const setState=value=>{context.fixtureState=value;vm.runInContext('state=fixtureState',context);};
const daily={window_start:'2026-10-11T11:30:00Z',window_end:'2026-10-12T11:30:00Z',
  opening:{valuation_complete:true},closing:{valuation_complete:true,unrealized_cents:0,cumulative_realized_cents:0},positions:[],research:[],
  totals:{period_profit_cents:0,cumulative_profit_cents:0,holding_count:0,period_fill_count:0},
  dividends_known:true,dividend_cents:0,cumulative_dividend_cents:25100,period_dividend_tax_cents:5020,
  cumulative_dividend_tax_cents:5020,period_net_dividend_cents:-5020,cumulative_net_dividend_cents:20080,
  account_period_profit_cents:-5020,account_cumulative_profit_cents:20080,reconciliation_difference_cents:0,
  dividend_accounting_version:'CASH_DIVIDEND_TAX_V1',accounting_basis:'FROZEN_EQUITY_WITH_LEDGER_FLOWS'};
const record={id:'synthetic-tax',revision:1,model_status:'SUCCEEDED',window_start:daily.window_start,window_end:daily.window_end,ready_at:'2026-10-12T11:40:00Z',payload:{facts:{portfolio:daily},analysis:{},consistency_checks:[]},presentation:{daily}};
const state={at:'2026-10-12T12:00:00Z',account:{equity_cents:10020080,available_cents:10020080,cash_cents:10020080,valuation_complete:true},
  portfolio:{total_profit_cents:20080,unrealized_cents:0,cumulative_realized_cents:0,cumulative_dividend_cents:25100,cumulative_dividend_tax_cents:5020,cumulative_net_dividend_cents:20080,cash_weight_pct:100,stock_weight_pct:0,holdings:[]},
  trade_effects:{totals:{fill_count:0}},reviews:[record],next_runs:[],decisions:[],fills:[],watchlist:[]};
const draw=(r=record,s=state)=>{setState(s);const box=new Element('section');get('drawDailyReview')(box,r);return full(box);};
const before=JSON.stringify(record);
let text=draw();
for(const expected of ['本期账户盈亏（24小时） -50.20 元','分红已扣税 50.20 元','（税前） − 分红已扣税 50.20 元 = 账户累计 +200.80 元','分红净入账 -50.20 元'])assert.ok(text.includes(expected),expected);
assert.doesNotMatch(text,/其他待核对差额|仍差/);
assert.equal(JSON.stringify(record),before,'Rendering never rewrites a frozen report');

const historical=structuredClone(record);
Object.assign(historical.presentation.daily,{period_dividend_tax_cents:0,cumulative_dividend_tax_cents:0,period_net_dividend_cents:0,cumulative_net_dividend_cents:25100,account_period_profit_cents:0,account_cumulative_profit_cents:25100});
text=draw(historical);
assert.match(text,/后续分红税 -50\.20/);
assert.match(text,/当前账户累计 \+200\.80 元/);
assert.doesNotMatch(text,/其他待核对差额|仍差/);

const unrelated=structuredClone(state);unrelated.portfolio.total_profit_cents+=100;
assert.match(draw(historical,unrelated),/其他待核对差额 \+1\.00/,'Tax does not absorb an unrelated discrepancy');
const unknown=structuredClone(historical);delete unknown.presentation.daily.cumulative_dividend_tax_cents;
text=draw(unknown);assert.match(text,/历史与当前分红税口径尚未齐全/);assert.doesNotMatch(text,/后续分红税|其他待核对差额/);

setState(state);get('renderAccount')(state);text=full(element('metrics'));
for(const expected of ['累计现金分红 251.00 元（税前）','分红已扣税 50.20 元','净入账 200.80 元'])assert.ok(text.includes(expected),expected);
const refreshed=structuredClone(state);refreshed.portfolio.cumulative_dividend_tax_cents=5120;refreshed.portfolio.cumulative_net_dividend_cents=19980;
setState(refreshed);get('renderAccount')(refreshed);assert.match(full(element('metrics')),/分红已扣税 51\.20 元/);
console.log('Dividend tax UI: net account P&L, posted catch-up attribution, unknown history and field refresh passed.');
