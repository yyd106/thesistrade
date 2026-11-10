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
const nodes=new Map(),element=id=>{
  if(!nodes.has(id))nodes.set(id,new Element('section'));
  return nodes.get(id);
};
const context=vm.createContext({document:{addEventListener(){},createElement:tag=>new Element(tag),getElementById:element,querySelectorAll(){return [];}},fetch(){throw Error('No network in synthetic UI test');},AbortSignal,URLSearchParams});
vm.runInContext(await readFile(new URL('../ashare/web/app.js',import.meta.url),'utf8'),context,{filename:'app.js'});
const decide=vm.runInContext('researchDecision',context),guest=vm.runInContext('resultDecision',context);
for(const symbol of ['sh600519','sh603444']){
  const levels={buy_low_cents:990,buy_high_cents:1010,sell_cents:1100,stop_cents:940};
  for(const item of [
    {symbol,research_only:true,entry_allowed:false,plan:{payload:{kind:'PAPER_TRADE',levels},effective_status:'ACTIVE'}},
    {symbol,entry_allowed:false,plan:{payload:{kind:'RESEARCH_ONLY',levels},effective_status:'EXPIRED'}},
  ]){
    assert.equal(decide(item).title,'参考区间，只研究不交易');assert.equal(decide(item).ready,false);
    assert.equal(guest(item).title,'参考区间，只研究不交易');assert.equal(guest(item).ready,false);
    assert.deepEqual(item.plan.payload.levels,levels);
  }
}
console.log('Research-only UI: both symbols stay clearly non-trading for admin/guest, old plans and expired references; levels remain available.');
