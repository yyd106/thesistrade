import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
class Element {
  constructor(tag){this.tag=tag;this.textContent='';this.children=[];this.events={};this.dataset={};}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  addEventListener(name,callback){this.events[name]=callback;}
  setAttribute(name,value){this[name]=value;}
}
const elements=new Map(),node=id=>{if(!elements.has(id))elements.set(id,new Element('div'));return elements.get(id);};
const stored=new Map();
const context=vm.createContext({document:{addEventListener(){},createElement:tag=>new Element(tag),getElementById:node},
  fetch(){throw Error('No network in cost reset UI tests');},AbortSignal,URLSearchParams,
  sessionStorage:{getItem:key=>stored.get(key)||null,setItem:(key,value)=>stored.set(key,value),removeItem:key=>stored.delete(key)}});
vm.runInContext(await readFile(new URL('../ashare/web/app.js',import.meta.url),'utf8'),context);
const get=name=>vm.runInContext(name,context),text=()=>JSON.stringify(node('cost-budget'));
context.approval={id:'AR-test',kind:'COST_BASIS_RESET',hash:'a'.repeat(64),expires_at:'2099-01-01T00:00:00Z',snapshot:{old_basis:'1',unit_nav_at_request:'.9',hard_halted:true}};
vm.runInContext("state={deployment_role:'cloud',exposure:{cost_enabled:true},account:{risk:{cost_locked:true,cost_cap_bps:0,unit_nav:'.9'}}};currentUser={role:'ADMIN'};globalThis.posts=[];api.post=async(path,body)=>{posts.push({path,body});return {status:'PREVIEW_READY',approval}}",context);
get('renderCostBudget')(get('state'));
assert.match(text(),/累计相对最初投入的亏损仍可能超过10%/);
await get('requestCostReset')();
assert.equal(get('posts.length'),1);assert.equal(get('posts[0].path'),'/api/cost-budget/request');
assert.match(text(),/确认重置成本基准/);assert.match(text(),/执行时的单位净值/);
assert.doesNotMatch(text(),/approval_id|"snapshot"|aaaaaaaa/);
vm.runInContext("api.post=async(path,body)=>{posts.push({path,body});return new Promise(resolve=>globalThis.finish=resolve)}",context);
const first=get('confirmCostReset')(get('approval'));await get('confirmCostReset')(get('approval'));
assert.equal(get('posts.length'),2);assert.equal(stored.get('cost-reset-pending'),'AR-test');
get('finish')({status:'RESET',approval_id:'AR-test',new_basis:'.91'});await first;
assert.equal(stored.size,0);assert.match(text(),/已重置为 .91/);
await get('confirmCostReset')(get('approval'));assert.equal(get('posts.length'),2);
vm.runInContext("costPreview=approval;api.post=async(path,body)=>{posts.push({path,body});throw Error('lost')}",context);
await get('confirmCostReset')(get('approval'));
assert.equal(get('costUnknown'),'AR-test');await get('requestCostReset')();assert.equal(get('posts.length'),3);
vm.runInContext("api.get=async()=>({status:'CONSUMED',request_id:'AR-test',result:{status:'RESET',new_basis:'.91'}})",context);
await get('checkCostReset')();assert.equal(get('posts.length'),3);assert.equal(stored.size,0);
vm.runInContext("currentUser={role:'GUEST'}",context);get('renderCostBudget')(get('state'));
assert.doesNotMatch(text(),/生成成本基准重置预览/);await get('requestCostReset')();assert.equal(get('posts.length'),3);
console.log('Cost reset UI: preview, explicit confirmation, duplicate clicks, unknown result, guest guard passed');
