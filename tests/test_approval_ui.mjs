import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

class Element {
  constructor(tag){this.tag=tag;this.textContent='';this.children=[];this.dataset={};this.events={};this.open=false;}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  addEventListener(name,callback){this.events[name]=callback;}
  setAttribute(name,value){this[name]=value;}
  showModal(){this.open=true;}
  close(){this.open=false;}
  focus(){this.focused=true;}
}
const elements=new Map(),node=id=>{if(!elements.has(id))elements.set(id,new Element('div'));return elements.get(id);};
const context=vm.createContext({document:{addEventListener(){},createElement:tag=>new Element(tag),getElementById:node},
  fetch(){throw Error('No network allowed in approval UI fixture');},AbortSignal,URLSearchParams});
vm.runInContext(await readFile(new URL('../ashare/web/app.js',import.meta.url),'utf8'),context);
const get=name=>vm.runInContext(name,context);
const proposalHash='b'.repeat(64),oldHash='c'.repeat(64);
const fullRule='WATCH_CORE_TEST 原始规则，不得替换关键字 <script>alert(1)</script> '+ '完整原文'.repeat(50);
const oldRule={id:'G-old',route:'watchlist',scope:'sh600519',text:'旧规则全文，须完整保留',proposal_id:'old-proposal',proposal_version:{id:'old-proposal',status:'ADOPTED',hash:oldHash}};
const request={id:'AR-fixture',kind:'PROPOSAL_DECISION',subject_id:'proposal-fixture',action:'ADOPTED',
  expires_at:'2099-01-01T00:00:00Z',hash:'a'.repeat(64),
  summary:{proposal_id:'proposal-fixture',title:'原始规则',kind:'RESEARCH_GUIDANCE',target:'自选研究',from:'APPROVED',to:'ADOPTED',
    note:'按批准版本生效',proposal_hash:proposalHash,guidance:{route:'watchlist',scope:'ALL',text:fullRule},
    replaces:[oldRule],legacy_approval:false,plan:{hypothesis:'一项待验证假设',change:'本次仅调整指定研究规则',test_plan:'固定期前向验证',failure_criteria:'预先固定失败标准',rollback:'按审批撤下',evidence_summary:'已绑定原稿证据 3 项；仅显示摘要，不展开本机路径或原始资料。'}},
  snapshot:{version:'proposal-decision-v1',proposal_hash:proposalHash,status:'APPROVED',action:'ADOPTED',note:'按批准版本生效',replaces:['G-old'],rules:[oldRule],replaced_by:null,legacy_approval:false}};
const notice={id:'N-fixture',status:'OPEN',kind:'DECISION',created_at:'2026-10-05T00:00:00Z',author:'agent',
  title:'审批固定方案',body:'自由摘要不能代替被冻结方案',approval_request:request};
const text=get('approvalNoticeText')(notice);
for(const exact of [request.hash,proposalHash,oldHash,fullRule,'G-old','旧规则全文，须完整保留','sh600519','proposal-fixture','old-proposal',
                    '已批准 → 已采纳','自选股','所有标的','固定期前向验证','预先固定失败标准','证据说明：已绑定原稿证据 3 项'])assert.ok(text.includes(exact),exact);
assert.doesNotMatch(text,/"snapshot"|"proposal_hash"|"guidance"/,'Main approval preview uses readable fields rather than raw contract JSON');
assert.equal(text.split(fullRule).length-1,1,'New rule is shown once, verbatim');
assert.equal(text.split(oldRule.text).length-1,1,'Replaced rule is shown once, verbatim');
const noReplace=get('approvalNoticeText')({...notice,approval_request:{...request,summary:{...request.summary,replaces:[]},snapshot:{...request.snapshot,replaces:[],rules:[]}}});
assert.match(noReplace,/不替代任何旧规则/);
const reject=get('approvalNoticeText')({...notice,approval_request:{...request,action:'REJECTED',summary:{...request.summary,from:'READY',to:'REJECTED'}}});
assert.match(reject,/本次确认：不实施此提案/);assert.doesNotMatch(reject,/本次确认：撤回批准/);
const withdraw=get('approvalNoticeText')({...notice,approval_request:{...request,action:'REJECTED',summary:{...request.summary,from:'APPROVED',to:'REJECTED'}}});
assert.match(withdraw,/本次确认：撤回批准/);
const configRequest={id:'AR-config',kind:'CONFIG',subject_id:'config-fingerprint',action:'APPLY',expires_at:request.expires_at,hash:'d'.repeat(64),
  summary:{title:'策略设置变更',reason:'只应用本次已核对差异',changes:[{key:'model_name',before_present:true,before:'原模型',after:'固定模型'},
    {key:'watchlist',before_present:true,before:[{symbol:'sh600519',name:'原公司'}],after:[{symbol:'sz000333',name:'新公司 <img>'}]}],build_before:'build-before',build_after:'build-after'},snapshot:{}};
const configText=get('approvalNoticeText')({...notice,approval_request:configRequest});
for(const exact of ['设置变更：2 项','固定模型（model_name）','原值：原模型','新值：固定模型','固定关注名单（watchlist）','原公司','新公司 <img>','sh600519','sz000333','只应用本次已核对差异',configRequest.hash])assert.ok(configText.includes(exact),exact);
assert.doesNotMatch(configText,/"summary"|"snapshot"/);
assert.doesNotMatch(text,/自由摘要不能代替/);
assert.match(text,/批准只生成本次动作的回执/);
assert.equal(get('approvalNoticeExpired')(notice,Date.parse('2099-01-01T00:00:00Z')),true);
assert.equal(get('approvalNoticeExpired')(notice,Date.parse('2098-01-01T00:00:00Z')),false);

context.noticeFixture=notice;
vm.runInContext("currentUser={role:'ADMIN'};state={notices:[noticeFixture]};renderNotice(state)",context);
assert.equal(node('notice-body').textContent,text,'Frozen values are displayed verbatim as text');
assert.equal(node('notice-body').children.length,0,'Untrusted markup never creates DOM elements');
assert.equal(node('notice-title').focused,true);
assert.deepEqual(node('notice-actions').children.map(n=>n.textContent),['批准','不批准','稍后再看']);
vm.runInContext("globalThis.posts=[];api.post=async(path,body)=>{posts.push({path,body});return {status:'APPROVED'}};refresh=async()=>{}",context);
const action=get('noticeActions')('DECISION')[0];
await get('answerNotice')(notice,action,new Element('button'));
assert.equal(vm.runInContext('posts.length',context),0,'First click previews confirmation only');
await get('answerNotice')(notice,action,new Element('button'));
assert.equal(vm.runInContext('posts.length',context),1);
assert.equal(vm.runInContext('posts[0].body.expected_hash',context),request.hash);

const expired={...notice,id:'N-expired',approval_request:{...request,expires_at:'2020-01-01T00:00:00Z'}};
get('drawNoticeActions')(expired);
assert.deepEqual(node('notice-actions').children.filter(n=>n.tag==='button').map(n=>n.textContent),['稍后再看']);
await get('answerNotice')(expired,action,new Element('button'));
assert.equal(vm.runInContext('posts.length',context),1,'Expired requests never submit approval');

vm.runInContext("connected=true;api.post=async()=>({status:'APPROVAL_PENDING'})",context);
await get('saveSettings')(new Element('button'),{dynamic_enabled:true},'settings-feedback','设置已保存');
assert.match(node('settings-feedback').textContent,/尚未生效/);
assert.match(node('settings-feedback').textContent,/批准后由代理按回执实施/);
assert.doesNotMatch(node('settings-feedback').textContent,/设置已保存/);
console.log('Approval UI: complete verbatim contract, text-only markup, explicit confirmation/hash, expiry and pending settings passed.');
