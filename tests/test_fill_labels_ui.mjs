import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

class Element {
  constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.attrs={};this.dataset={};}
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
const context=vm.createContext({document:{addEventListener(){},createElement:tag=>new Element(tag),getElementById:element,querySelectorAll(){return [];}},fetch(){throw Error('No real network in synthetic UI test');},AbortSignal,URLSearchParams});
vm.runInContext(await readFile(new URL('../ashare/web/app.js',import.meta.url),'utf8'),context,{filename:'app.js'});
const full=node=>[node.textContent,...node.children.map(full)].join(' ');
const fills=[
  {symbol:'EXITED',side:'BUY',qty:100,price_cents:1000,fee_cents:500,realized_cents:0,occurred_at:'2026-09-29T02:00:00Z'},
  {symbol:'EXITED',side:'SELL',qty:100,price_cents:1100,fee_cents:600,realized_cents:8900,occurred_at:'2026-09-30T02:00:00Z'},
  {symbol:'PARTIAL',side:'BUY',qty:200,price_cents:2000,fee_cents:500,realized_cents:0,occurred_at:'2026-09-29T03:00:00Z'},
  {symbol:'PARTIAL',side:'SELL',qty:100,price_cents:1900,fee_cents:600,realized_cents:-10850,occurred_at:'2026-09-30T03:00:00Z'},
];
// Exercise the real homepage history renderer, including fully and partly exited buys.
vm.runInContext('renderActivity',context)({at:'2026-09-30T12:00:00Z',next_runs:[],decisions:[],fills,reviews:[],watchlist:[]});
let text=full(element('fills'));
assert.doesNotMatch(text,/尚未卖出/);
assert.equal(text.match(/买入不结算已实现盈亏/g)?.length,2);
assert.match(text,/6\.00 \/ \+89\.00/);
assert.match(text,/6\.00 \/ -108\.50/);

// Test the stock-history path with no current holding. Skip unrelated research cards.
const stockResponse={stock:{name:'已清仓的合成公司'},history:{fills:{items:fills.slice(0,2),total:2},orders:{items:[],total:0},decisions:{items:[],total:0}}};
context.stockResponse=stockResponse;
vm.runInContext('api.get=async()=>stockResponse; renderChanged=(original=>function(id,key,draw){if(id!=="stock-content")original(id,key,draw);})(renderChanged)',context);
await vm.runInContext('refreshStock()',context);
text=full(element('stock-history'));
assert.doesNotMatch(text,/尚未卖出/);
assert.equal(text.match(/买入不结算已实现盈亏/g)?.length,1);
assert.match(text,/6\.00 \/ \+89\.00/);
console.log('Fill history: buy rows never imply a remaining lot; realized gain/loss remains on sell rows in home and stock views.');
