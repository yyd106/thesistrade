import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const source=await readFile(new URL('../ashare/web/app.js',import.meta.url),'utf8');
let clock=Date.parse('2030-01-01T10:00:00Z');
class PageDate extends Date {
  constructor(...args){super(...(args.length?args:[clock]));}
  static now(){return clock;}
}
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.textContent='';this.attrs={};this.dataset={};this.listeners={};}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  setAttribute(name,value){this.attrs[name]=value;}
  addEventListener(name,fn){this.listeners[name]=fn;}
  querySelectorAll(selector){return descendants(this).filter(n=>n!==this&&n.tag==='details'&&n.dataset.key&&(!selector.includes('[open]')||n.open));}
}
function descendants(node){return [node,...node.children.flatMap(descendants)];}
function visibleText(node){return [node.textContent,...(node.tag==='details'&&!node.open?node.children.slice(0,1):node.children).map(visibleText)].join(' ');}
const nodes=new Map();
const document={addEventListener(){},createElement:tag=>new Element(tag),getElementById:id=>{if(!nodes.has(id))nodes.set(id,new Element('div'));return nodes.get(id);}};
const context=vm.createContext({document,Date:PageDate,fetch(){throw Error('No network in synthetic UI checks');},AbortSignal,URLSearchParams});
vm.runInContext(source,context,{filename:'app.js'});
const get=name=>vm.runInContext(name,context);
const assets={US10Y:{name:'美国10年期国债收益率'}};

export function macroEventFixture(id,{history=false,invalid=false}={}){
  const materiality={state:'ADMITTED',admitted:true,reason:'影响证据通过，仍需观察',assessment:{direction:'DOWN',basis:'独立核对后认为压力方向相反',magnitude:'HIGH',novelty:'INCREMENTAL',scale:{kind:'UNKNOWN',explanation:'规模尚未量化'},transmission:'独立传导路径',missing_evidence:'仍需实际采购合同',citations:[]},history:{state:'INSUFFICIENT',sample_count:0,required_count:30}};
  return {id,status:invalid?'INVALIDATED':'TRACKING',theme_label:'货币政策与利率',published_at:'2030-01-01T08:00:00Z',created_at:'2030-01-01T09:00:00Z',basis:'FORWARD',screening:{decision:'DEEP'},
    lifecycle:{state:invalid?'INVALIDATED':history?'ENDED':'TRACKING',label:invalid?'原判断已失效':history?'观察已结束':'跟踪观察',reason:history?'旧判断仅供回看':'影响评估通过，仍需核实新证据',next_step:history?'查看已经记录的结果和缺口':'下一步核实：实际采购合同',actionable:!history&&!invalid,review_due_at:'2030-01-04T08:00:00Z',expires_at:'2030-01-08T08:00:00Z'},
    analysis:{headline:'合成事件 '+id,regions:['US'],horizon:'DAYS',facts:'合成事件的公开事实；<img src=x onerror=alert(1)>',transmission:'政策可能改变资金成本',uncertainty:'执行效果未知',expectation_basis:'尚无一致预期',invalidation:'政策撤回',impacts:[{asset:'US10Y',direction:'UP',strength:'HIGH',mechanism:'初步路径',watch:'实际采购合同',materiality}]},
    citations:[{source:'合成来源',title:'公开政策原文',url:'https://example.com/source',quote:'Synthetic exact quotation'}],reactions:[],
    revisions:{total:1,items:[{at:'2030-01-01T09:00:00Z',changed:['标的、方向或推演'],before_headline:'合成判断',after_headline:'合成判断',before_directions:[{asset:'US10Y',direction:'UP'}],after_directions:[{asset:'US10Y',direction:'DOWN'}]}],source_replacement_total:2,source_replacements:[{title:'最新修订原文',url:'https://example.com/current',first_seen_at:'2030-01-01T08:00:00Z',source_status:'NEW'},{title:'历史中间修订原文',url:'https://example.com/old',first_seen_at:'2030-01-01T07:00:00Z',source_status:'REVISED'}]}};
}
export function macroRelevanceFixture(){
  return {items:Array.from({length:8},(_,i)=>macroEventFixture('current-'+i)),archived_items:[],
    followup_items:Array.from({length:7},(_,i)=>macroEventFixture('followup-'+i)),
    history_items:Array.from({length:9},(_,i)=>macroEventFixture('history-'+i,{history:true})),
    library:{as_of:'2030-01-01T10:00:00Z',limit:40,followup_total:47,history_total:89},
    assets,markets:[],sources:[],news_counts:{},observation_counts:{},event_count:144,window_end:'2030-01-01T10:00:00Z'};
}

const drawMacro=get('drawMacroEvent'),card=new Element('div');
drawMacro(card,macroEventFixture('conflict'),assets,{compact:true});
const visible=visibleText(card),all=descendants(card).map(n=>n.textContent).join(' ');
assert.match(visible,/下一步核实：实际采购合同/);
assert.match(visible,/复核期限.*观察结束/);
assert.match(visible,/初步推演：.*上行压力/);
assert.match(visible,/独立评估：.*下行压力/);
assert.match(visible,/方向分歧/);
assert.doesNotMatch(visible,/Synthetic exact quotation|独立传导路径/);
assert.match(all,/评估通过不代表支持初稿方向/);
assert.match(all,/修订后：.*下行压力/);
assert.match(all,/最新修订来源：最新修订原文/);
assert.match(all,/历史中间修订：历史中间修订原文/);
assert.ok(descendants(card).every(n=>!['img','script'].includes(n.tag)));
assert.ok(descendants(card).filter(n=>n.tag==='details').every(n=>!n.open));

const measured=new Element('div'),measuredEvent=macroEventFixture('measured',{history:true});
measuredEvent.reactions=[{name:'美国10年期国债收益率',observation:{baseline:{date:'2029-12-24'},end:{date:'2029-12-27'},change:30,change_unit:'bp',url:'https://example.com/series',method:'日级指标变化'}}];
drawMacro(measured,measuredEvent,assets,{compact:true,historical:true});
assert.match(visibleText(measured),/已记录观察：美国10年期国债收益率 \+30bp/);
assert.match(visibleText(measured),/不等于因果验证或交易收益/);

const historical=new Element('div');
drawMacro(historical,macroEventFixture('invalid',{invalid:true}),assets,{compact:true,historical:true});
assert.match(visibleText(historical),/原判断已失效/);
assert.match(visibleText(historical),/当时推演/);
assert.match(descendants(historical).map(n=>n.textContent).join(' '),/当时评估：通过影响评估/);

const render=get('renderGlobalMacro'),fixture=macroRelevanceFixture();
context.fixtureState={dynamic:{global:fixture},observation:{items:[],membership:{}}};vm.runInContext('state=fixtureState;',context);
render({global:fixture});
const cards=id=>nodes.get(id).children.filter(n=>n.tag==='article');
for(const id of ['macro-cases','macro-followup','macro-archive'])assert.equal(cards(id).length,5);
assert.match(visibleText(nodes.get('macro-followup')),/共 47 条，本次仅载入最近 7 条/);
assert.match(visibleText(nodes.get('macro-archive')),/共 89 条，本次仅载入最近 9 条/);
assert.match(visibleText(nodes.get('macro-archive')),/状态核对截至/);
assert.equal(nodes.get('macro-history-title').textContent,'历史研究与修订（89）');
assert.equal(nodes.get('macro-followup-title').textContent,'当前待跟进（47）');
const nav=nodes.get('macro-cases').children.find(n=>n.tag==='nav');
nav.children.at(-1).listeners.click();
assert.equal(cards('macro-cases').length,3);
assert.equal(cards('macro-cases')[0].id,'macro-event-current-5');
render({global:{...fixture,library:{...fixture.library,as_of:'2030-01-01T11:00:00Z'}}});
assert.equal(cards('macro-cases')[0].id,'macro-event-current-5');

// The cloud can retain an old signed snapshot while the local machine is offline.
// Device time must retire its authority even when data and server snapshot do not change.
clock=Date.parse('2030-01-04T09:00:00Z');
render({global:fixture});
assert.match(visibleText(nodes.get('macro-cases')),/已到复核期 · 待同步核对/);
assert.match(visibleText(nodes.get('macro-cases')),/旧评估不能作为当前依据/);
clock=Date.parse('2030-01-08T09:00:00Z');
render({global:fixture});
assert.match(visibleText(nodes.get('macro-cases')),/观察已到期 · 待同步核对/);
assert.match(visibleText(nodes.get('macro-cases')),/当时独立评估/);
assert.equal(fixture.items[0].lifecycle.state,'TRACKING');

console.log('News relevance UI passed: bounded pages, actionable summaries, truthful totals, preserved page state, historical authority, source revisions, independent disagreement, safe text and offline deadlines.');
