import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

class Element {
  constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.attrs={};this._text='';this.hidden=false;}
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
const root=new Element('section'),online=new Element('section');online.textContent='本机在线记录保持独立';
const context=vm.createContext({document:{addEventListener(){},createElement:tag=>new Element(tag),getElementById:id=>id==='strategy-freshness'?root:online},
  fetch(){throw Error('No network in rendering tests');},AbortSignal,URLSearchParams});
const source=await readFile(new URL('../ashare/web/app.js',import.meta.url),'utf8');
vm.runInContext(source,context,{filename:'app.js'});
const render=vm.runInContext('renderStrategyFreshness',context);
const at='2026-10-15T12:00:00+00:00';
const age=(mean,max,unknown=0)=>({known:1,unknown,mean_seconds:mean,max_seconds:max});
const zero={count:0,classification_unknown_count:0,plan_age:age(null,null),research_age:age(null,null)};
const window={coverage_complete:true,observed_seconds:86400,unknown_seconds:0,interruption_count:0,interruption_seconds:0,longest_interruption_seconds:0,ongoing_interruption:false};
const plan={time_valid_count:1,expired_active_count:2,future_active_count:0,unknown_validity_count:0,protective_count:0,ready_count:null,
  max_age_threshold_seconds:43200,over_max_age_count:2,plan_age:age(3600,3600),research_age:age(72000,72000),min_remaining_seconds:7200};
const summary={as_of:at,heartbeat:{completed_at:at,received_at:at,observed_until:at,windows:{'24h':{...window},'7d':{...window,coverage_complete:false,unknown_seconds:518400}}},
  plans:{status:'CURRENT',routes:{watchlist:{...plan},global:{...plan,max_age_threshold_seconds:null},dynamic:{...plan,ready_count:1,max_age_threshold_seconds:null}}},
  fills:{window_start:'2026-10-08T12:00:00+00:00',window_end:at,coverage_state:'CURRENT',ledger_observed_until:at,
    routes:Object.fromEntries(['watchlist','global','dynamic'].map(route=>[route,Object.fromEntries(['BUY','SELL','PROTECTIVE_EXIT','UNKNOWN'].map(group=>[group,{...zero}]))]))}};
summary.fills.routes.watchlist.BUY={count:1,classification_unknown_count:0,plan_age:age(3600,3600),research_age:age(72000,72000)};
summary.fills.routes.watchlist.SELL={count:1,classification_unknown_count:1,plan_age:age(7200,7200),research_age:age(108000,108000)};
summary.fills.routes.watchlist.PROTECTIVE_EXIT={count:1,classification_unknown_count:0,plan_age:age(60,60),research_age:{known:0,unknown:1,mean_seconds:null,max_seconds:null}};
const view={status:'OK',as_of:at,stale:false,summary,current_lease:{initialized:true,execution_enabled:true,active:true,remaining_seconds:3600}};
render({strategy_freshness:view});
assert.equal(root.hidden,false);
for(const expected of ['还剩 1 小时','最近研究完成','云端接收','续期记录覆盖截至','过去 24 小时：中断 0 次','过去 7 天：整窗未知，覆盖不足','计划签发龄','原研究龄','20 小时','仍标为活跃但已过期 2 份','自选股','全球资产','动态事件','买入 1 笔','普通卖出 1 笔','保护性退出 1 笔','退出原因待核对','本期无成交'])assert.ok(root.textContent.includes(expected),expected);
assert.equal(root.children[0].tagName,'DETAILS');assert.ok(!root.children[0].open,'initial card is compact');
root.children[0].open=true;
render({strategy_freshness:{...view,stale:true,current_lease:{...view.current_lease,active:false,remaining_seconds:0}}});
assert.ok(root.children[0].open,'refresh preserves expanded state');
assert.match(root.textContent,/云端研究已过期，暂停新买入/);
assert.match(root.textContent,/统计摘要尚未更新/);
assert.match(root.textContent,/上次快照中的计划状态/);
assert.doesNotMatch(root.textContent,/还剩 1 小时/);

const unknown=structuredClone(view);unknown.summary.heartbeat.windows['24h']={...window,coverage_complete:false,observed_seconds:0,unknown_seconds:86400,interruption_count:null,interruption_seconds:null,longest_interruption_seconds:null};
unknown.summary.plans.status='UNKNOWN_HISTORICAL_STATUS';unknown.summary.fills.coverage_state='UNKNOWN';unknown.summary.fills.ledger_observed_until=null;
render({strategy_freshness:unknown});
assert.match(root.textContent,/历史时点的计划状态不可回放/);
assert.match(root.textContent,/成交情况未知/);
assert.doesNotMatch(root.textContent,/过去 24 小时：中断 0 次|本期无成交/);
render({strategy_freshness:{status:'AWAITING_SYNC',summary:null,current_lease:view.current_lease,stale:true}});
assert.match(root.textContent,/等待本机同步/);assert.match(root.textContent,/还剩 1 小时/);
render({strategy_freshness:{status:'UNAVAILABLE',summary:null,stale:false}});
assert.match(root.textContent,/暂不可用/);assert.doesNotMatch(root.textContent,/中断 0 次|本期无成交/);
render({strategy_freshness:null});assert.equal(root.hidden,true);assert.equal(root.children.length,0);
assert.equal(online.textContent,'本机在线记录保持独立');
const html=await readFile(new URL('../ashare/web/index.html',import.meta.url),'utf8');
assert.match(html,/id="strategy-freshness"[^>]*hidden/);
console.log('Strategy freshness UI: lease authority, coverage gaps, separate ages/routes/exits, historical/stale states, folding and disabled state passed.');
