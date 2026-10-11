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
  fetch(){throw Error('No network in simulated withdrawal UI tests');},AbortSignal,URLSearchParams,
  sessionStorage:{getItem:key=>stored.get(key)||null,setItem:(key,value)=>stored.set(key,value),removeItem:key=>stored.delete(key)}});
vm.runInContext(await readFile(new URL('../ashare/web/app.js',import.meta.url),'utf8'),context);
const get=name=>vm.runInContext(name,context),flatten=n=>[n,...n.children.flatMap(flatten)],text=()=>JSON.stringify(node('withdrawals'));
const status={version:'simulated-withdrawal-v1',authority:'cloud',current_stage:1,status:'READY',
  threshold_cents:15000000,retain_cents:12000000,equity_cents:16000000,cash_cents:10000000,
  candidate_cents:4000000,reserved_cents:4000000,can_transfer:true,can_cancel:true,blockers:[]};
const approval={id:'AR-synthetic',kind:'WITHDRAWAL',action:'TRANSFER',hash:'a'.repeat(64),expires_at:'2099-01-01T00:00:00Z',
  snapshot:{action:'TRANSFER',stage:1,cycle_id:'cycle-synthetic',cycle_revision:2,threshold_cents:15000000,
    retain_cents:12000000,equity_cents:16000000,cash_cents:10000000,amount_cents:4000000,
    post_equity_cents:12000000,state_hash:'PRIVATE_IMPLEMENTATION_HASH'}};
context.statusFixture=status;context.approvalFixture=approval;
vm.runInContext("state={deployment_role:'cloud',withdrawals:statusFixture};currentUser={role:'ADMIN'};globalThis.posts=[];api.post=async(path,body)=>{posts.push({path,body});return {status:'PREVIEW_READY',approval:approvalFixture,withdrawal:statusFixture}}",context);
get('renderWithdrawals')(get('state'));
for(const value of ['模拟账户资金','不会转出真实资金','严格超过','150,000.00','120,000.00','160,000.00','40,000.00'])assert.ok(text().includes(value),value);
assert.equal(get('parseWorkspaceHash')('#withdrawal-section').board,'holdings');
for(const [value,label] of [['IDLE','等待达到本档阈值'],['READY','可生成提取预览'],['WAITING','等待满足提取条件'],['PAUSED_THRESHOLD','等待重新超过本档阈值']]){
  get('renderWithdrawals')({...get('state'),withdrawals:{...status,status:value}});assert.ok(text().includes(label));
}
await get('requestWithdrawal')('TRANSFER');
assert.equal(get('posts.length'),1);assert.equal(get('posts[0].path'),'/api/withdrawals/request');
assert.match(text(),/确认模拟提取 40,000\.00 元/);
assert.match(text(),/第 2 版计划/);
assert.doesNotMatch(text(),/PRIVATE_IMPLEMENTATION_HASH|state_hash|approval_id|"snapshot"/);
assert.equal(get('posts.some(p=>p.path.endsWith("execute"))'),false,'Preview generation never executes a transfer');

// Two clicks while a request is in flight result in exactly one submitted action.
vm.runInContext("api.post=async(path,body)=>{posts.push({path,body});return new Promise(resolve=>globalThis.finishTransfer=resolve)}",context);
const first=get('confirmWithdrawal')(approval),second=get('confirmWithdrawal')(approval);
await second;
assert.equal(get('posts.length'),2);
assert.deepEqual(JSON.parse(JSON.stringify(get('posts[1].body'))),{approval_id:approval.id,expected_hash:approval.hash});
assert.equal(stored.get('withdrawal-pending'),approval.id);
get('finishTransfer')({status:'COMPLETED',request_id:approval.id,withdrawal:{...status,current_stage:2,status:'IDLE',can_transfer:false,can_cancel:false}});
await first;
assert.match(text(),/已完成并对账/);assert.equal(stored.has('withdrawal-pending'),false);
await get('confirmWithdrawal')(approval);assert.equal(get('posts.length'),2,'A completed preview is removed');

// Unknown results remain locked until a read; an undelivered request requires another explicit confirmation.
vm.runInContext("withdrawalPreview=approvalFixture;api.post=async(path,body)=>{posts.push({path,body});throw new ApiError('lost response','NETWORK')}",context);
await get('confirmWithdrawal')(approval);
assert.equal(get('withdrawalUnknown'),approval.id);assert.equal(stored.get('withdrawal-pending'),approval.id);
assert.match(text(),/先核对/);
assert.deepEqual(flatten(node('withdrawals')).filter(n=>n.tag==='button').map(n=>n.textContent),['核对上次操作结果']);
await get('requestWithdrawal')('CANCEL');await get('confirmWithdrawal')(approval);
assert.equal(get('posts.length'),3);
vm.runInContext("globalThis.reads=[];api.get=async path=>{reads.push(path);return {status:'PENDING',request_id:approvalFixture.id,approval:approvalFixture,result:null}}",context);
await get('checkWithdrawalResult')();
assert.equal(get('posts.length'),3,'Reading PENDING does not resend a transfer');assert.equal(get('withdrawalUnknown'),null);
assert.equal(get('withdrawalPreview.id'),approval.id);assert.equal(stored.size,0);
assert.match(text(),/未查到完成记录.*再次明确确认/);assert.match(text(),/确认模拟提取 40,000\.00 元/);
vm.runInContext("api.post=async(path,body)=>{posts.push({path,body});return {status:'COMPLETED',request_id:approvalFixture.id,withdrawal:statusFixture}}",context);
await get('confirmWithdrawal')(get('withdrawalPreview'));
assert.equal(get('posts.length'),4);assert.equal(get('posts[3].body.approval_id'),approval.id);

// A committed action whose response was lost is read back without any new POST.
vm.runInContext("withdrawalPreview=approvalFixture;api.post=async(path,body)=>{posts.push({path,body});throw new ApiError('lost committed response','NETWORK')}",context);
await get('confirmWithdrawal')(approval);
vm.runInContext("api.get=async path=>{reads.push(path);return {status:'CONSUMED',request_id:approvalFixture.id,result:{status:'COMPLETED',request_id:approvalFixture.id,withdrawal:statusFixture}}}",context);
await get('checkWithdrawalResult')();
assert.match(text(),/已核对.*没有重复提取/);assert.equal(get('withdrawalUnknown'),null);assert.equal(stored.size,0);
assert.equal(get('posts.length'),5);

// Expiration after a lost request safely returns to preview generation without releasing reservations.
vm.runInContext("withdrawalUnknown=approvalFixture.id;withdrawalPending(approvalFixture.id);api.get=async()=>({status:'EXPIRED',request_id:approvalFixture.id,result:null})",context);
await get('checkWithdrawalResult')();
assert.equal(get('withdrawalUnknown'),null);assert.equal(get('withdrawalPreview'),null);assert.equal(stored.size,0);
assert.match(text(),/原预览已失效.*重新生成预览/);assert.equal(get('posts.length'),5);
assert.equal(get('state.withdrawals.reserved_cents'),status.reserved_cents);

// A server rejection of an outdated amount removes the old confirmation.
vm.runInContext("withdrawalPreview=approvalFixture;api.post=async()=>{const e=new ApiError('资产或计划已变化，请重新预览。','HTTP',409);e.payload={status:'STALE',withdrawal:statusFixture};throw e}",context);
await get('confirmWithdrawal')(approval);
assert.equal(get('withdrawalPreview'),null);assert.equal(get('withdrawalUnknown'),null);
assert.match(text(),/资产或计划已变化/);assert.doesNotMatch(text(),/确认模拟提取 40,000/);

const expired={...approval,id:'AR-expired',expires_at:'2000-01-01T00:00:00Z'};
context.expiredFixture=expired;vm.runInContext('withdrawalPreview=expiredFixture',context);
await get('confirmWithdrawal')(expired);
assert.equal(get('posts.length'),5);assert.match(text(),/预览已过期/);

const cancel={...approval,action:'CANCEL',snapshot:{...approval.snapshot,action:'CANCEL',amount_cents:0,release_cents:4000000}};
context.cancelFixture=cancel;vm.runInContext('withdrawalPreview=cancelFixture;renderWithdrawals(state)',context);
assert.match(text(),/释放预留 40,000\.00 元/);assert.match(text(),/档位不前进/);
assert.ok(flatten(node('withdrawals')).filter(n=>n.tag==='button').every(n=>!n.textContent.startsWith('确认模拟提取')));

// Guests and the research replica cannot reach writing actions, even by invoking handlers.
for(const [role,deployment] of [['GUEST','cloud'],['ADMIN','research'],['ADMIN','standalone']]){
  context.testRole=role;context.testDeployment=deployment;
  vm.runInContext('currentUser={role:testRole};state={deployment_role:testDeployment,withdrawals:statusFixture};renderWithdrawals(state)',context);
  assert.equal(flatten(node('withdrawals')).filter(n=>n.tag==='button').length,0);
  await get('requestWithdrawal')('TRANSFER');await get('confirmWithdrawal')(cancel);await get('checkWithdrawalResult')();
  assert.equal(get('posts.length'),5);
  if(deployment!=='cloud')assert.match(text(),/本页仅显示云端同步结果/);
}
context.blockedFixture={...status,can_transfer:false,can_cancel:false,blockers:[{code:'TEST',message:'等待行情 <img src=x onerror=alert(1)>'}]};
vm.runInContext("currentUser={role:'ADMIN'};withdrawalPreview=null;state={deployment_role:'cloud',withdrawals:blockedFixture};renderWithdrawals(state)",context);
assert.match(text(),/等待行情/);
assert.ok(flatten(node('withdrawals')).filter(n=>n.tag==='button').every(n=>n.disabled));
assert.ok(flatten(node('withdrawals')).every(n=>!['img','script'].includes(n.tag)&&!n.innerHTML));

// An incomplete or mismatched success body remains uncertain, including after a page reload.
vm.runInContext("state={deployment_role:'cloud',withdrawals:statusFixture};withdrawalPreview=approvalFixture;api.post=async(path,body)=>{posts.push({path,body});return {status:'COMPLETED',request_id:'another-request'}}",context);
await get('confirmWithdrawal')(approval);
assert.equal(get('withdrawalUnknown'),approval.id);assert.equal(get('posts.length'),6);
vm.runInContext('withdrawalUnknown=withdrawalPending()',context);
assert.equal(get('withdrawalUnknown'),approval.id);
vm.runInContext("api.get=async()=>({status:'CONSUMED',request_id:'another-request',result:{status:'COMPLETED',request_id:'another-request'}})",context);
await get('checkWithdrawalResult')();
assert.equal(get('withdrawalUnknown'),approval.id);assert.equal(get('posts.length'),6);
console.log('Simulated withdrawal UI: exact preview confirmation, no duplicate clicks, unknown-result reconciliation, expired/stale plans and read-only replica/guest access passed.');
