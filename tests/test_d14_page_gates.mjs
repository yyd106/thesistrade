import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.textContent='';}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  addEventListener(){}
  setAttribute(key,value){this[key]=value;}
}
const context=vm.createContext({document:{addEventListener(){},createElement:tag=>new Element(tag),getElementById(){return null;}},
  fetch(){throw Error('No network in page gate tests');},AbortSignal,URLSearchParams});
vm.runInContext(await readFile(new URL('../ashare/web/app.js',import.meta.url),'utf8'),context);
const get=name=>vm.runInContext(name,context), text=node=>JSON.stringify(node);
const payload={kind:'PAPER_TRADE',blockers:[],levels:{buy_low_cents:100,buy_high_cents:200}};
const item={symbol:'sh600000',asset:'US:TEST',name:'test',plan:{status:'ACTIVE',effective_status:'ACTIVE',payload},
  quote:{price_cents:150},current_buy_blocker:'BEAR_HIGH_VOLATILITY',current_buy_reason:'偏空时波动超过37%，暂停新增买入。 当前波动 80.00%。'};
for(const role of ['ADMIN','GUEST']){
  vm.runInContext(`currentUser={role:'${role}'}`,context);
  for(const fn of ['researchDecision','resultDecision']){
    const result=get(fn)(item,'CONTINUOUS');assert.equal(result.title,'等待');assert.equal(result.ready,false);assert.match(result.reason,/80.00%/);
  }
}
const globalTable=new Element('table');
get('drawGlobalWatchlistRow')(globalTable,{...item,category:'US',trade_plan:item.plan},{at:'2026-11-12T00:00:00Z'});
assert.match(text(globalTable),/等待/);assert.match(text(globalTable),/80.00%/);assert.doesNotMatch(text(globalTable),/等待价格与资金条件/);
const dynamicBox=new Element('div');
get('drawDynamicCase')(dynamicBox,{...item,id:'case',title:'case',url:'https://example.com',status:'READY',plan:payload,analysis:{},position:{qty:0}});
assert.match(text(dynamicBox),/80.00%/);assert.doesNotMatch(text(dynamicBox),/计划已验证，等待价格与资金条件/);
assert.equal(get('researchDecision')({...item,current_buy_blocker:null},'CONTINUOUS').ready,true);
console.log('D14/P06 page gates: A-share list/detail, guest view, global asset list and dynamic cards agree');
