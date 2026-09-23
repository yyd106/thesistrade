"""Bounded observation allocation; no order, position or original watchlist writes."""
from __future__ import annotations
import json
from datetime import datetime,timedelta
from .storage import normalize_time

DEFAULTS={'active_limit':18,'focus_limit':8,'cooldown_hours':72,'archive_days':7,'long_review_days':7,'long_archive_days':30,'replacement_margin':10,'news_queue_limit':64}

def policy(config=None):
 p={**DEFAULTS,**(config or {}).get('dynamic_observation_policy',{})}
 if any(type(p.get(k)) is not int or p[k]<1 for k in DEFAULTS):raise ValueError('动态观察额度与期限必须为正整数')
 if p['focus_limit']>p['active_limit'] or p['active_limit']>50 or p['news_queue_limit']>200:raise ValueError('动态观察额度超出范围')
 if p['cooldown_hours']>=p['archive_days']*24 or p['long_review_days']>=p['long_archive_days']:raise ValueError('动态观察复核期限必须早于归档期限')
 return p

def protected_assets(store):
 result=set()
 for table in ('paper_lots','dynamic_lots','global_lots'):
  result.update(r[0] for r in store.db.execute('SELECT DISTINCT symbol FROM '+table+' WHERE qty>0'))
 for table in ('paper_orders','dynamic_orders','global_orders'):
  result.update(r[0] for r in store.db.execute("SELECT DISTINCT symbol FROM "+table+" WHERE status IN ('OPEN','PARTIAL','UNKNOWN')"))
 return result

def allocate(store,items,at,config=None):
 p=policy(config);at=normalize_time(at);clock=datetime.fromisoformat(at)
 original={w['symbol'] for w in (config or {}).get('watchlist',[])};protected=protected_assets(store)
 from .investment_policy import enabled,FIXED,MAX_ASSETS,admitted_kind
 bounded=enabled(config or {});fixed=set(FIXED) if bounded else set()
 if bounded:p={**p,'active_limit':max(0,min(p['active_limit'],MAX_ASSETS-len(original|fixed))), 'total_limit':MAX_ASSETS,'fixed_count':len(fixed)}
 previous={r['asset']:json.loads(r['payload_json']) for r in store.db.execute('SELECT * FROM macro_watch_state WHERE changed_at<=?',(at,))}
 official={'美联储','欧洲央行','英国央行','日本央行','EIA能源资讯','联合国新闻','商务部新闻'}
 candidates=[];forced=[];core=[]
 for item in items:
  asset=item['asset'];item['protected']=asset in protected
  valid=[x for x in item['links'] if x['status']=='TRACKING']
  fresh=[];rejected=[];score=0;due=None;expires=None;latest=None
  for cause in valid:
   stamp=cause.get('catalyst_at',cause['published_at']);age=max(0,(clock-datetime.fromisoformat(stamp)).total_seconds()/3600)
   long=cause.get('horizon')=='MONTHS';review_hours=p['long_review_days']*24 if long else p['cooldown_hours'];expiry_hours=(p['long_archive_days'] if long else p['archive_days'])*24
   cause['review_due_at']=normalize_time((datetime.fromisoformat(stamp)+timedelta(hours=review_hours)).isoformat())
   cause['expires_at']=normalize_time((datetime.fromisoformat(stamp)+timedelta(hours=expiry_hours)).isoformat())
   cause['review_state']='EXPIRED' if age>=expiry_hours else 'DUE' if age>=review_hours else 'CURRENT'
   expires=max(expires or '',cause['expires_at']);latest=max(latest or '',stamp)
   if age>=review_hours:continue
   assessment=cause.get('materiality') or {'admitted':False,'state':'PENDING','reason':'等待独立影响评估，暂不占用活跃名额'}
   if not assessment['admitted']:
    rejected.append(assessment);continue
   fresh.append(cause);due=min(due or cause['review_due_at'],cause['review_due_at'])
   impact=cause['impact'];chain=impact.get('logic_chain',[])
   value={'HIGH':30,'MEDIUM':20,'LOW':5}.get((assessment.get('assessment') or {}).get('magnitude',impact.get('strength')),0)
   value+=15 if cause.get('source') in official else 5
   value+=15 if len(chain)>=2 and chain[0].get('kind')=='FACT' else 0
   value+=5 if impact.get('conditions') and impact.get('invalidation') else 0
   value+=5 if impact['direction'] in ('UP','DOWN') else 0
   value+=round(20*(1-age/review_hours))
   if assessment.get('history',{}).get('state')=='SUPPORTED':value+=10
   score=max(score,value)
  if fresh:
   directions={(c.get('materiality',{}).get('assessment') or {}).get('direction',c['impact']['direction']) for c in fresh}
   item['conflicting']='UP' in directions and 'DOWN' in directions
   item['direction']='MIXED' if item['conflicting'] or 'MIXED' in directions else 'UNCLEAR' if 'UNCLEAR' in directions else next(iter(directions))
   item['strength']=max(((c.get('materiality',{}).get('assessment') or {}).get('magnitude',c['impact'].get('strength','UNKNOWN')) for c in fresh),key=lambda x:{'HIGH':3,'MEDIUM':2,'LOW':1}.get(x,0))
  if item.get('conflicting'):score=max(0,score-10)
  item.update(priority_score=score,last_catalyst_at=latest,review_due_at=due,expires_at=expires,pool_tier='ARCHIVED',pool_reason='暂无仍有效的事件依据',focus_eligible=bool(fresh and any(c['impact'].get('logic_chain') for c in fresh)))
  if valid and not fresh:
   cooling=bool(expires and expires>at)
   item['pool_tier']='COOLING' if cooling else 'ARCHIVED'
   item['pool_reason']='事件已到复核期限，等待新证据' if cooling else '事件观察期限已结束'
  if rejected and not fresh:
   pending=any(a['state']!='BACKGROUND' for a in rejected)
   item.update(pool_tier='COOLING' if pending else 'ARCHIVED',pool_reason=next((a['reason'] for a in rejected if a['state']!='BACKGROUND'),rejected[0]['reason']))
  if asset in original:
   item.update(pool_tier='CORE',pool_reason='原自选股，沿用既有研究流程');core.append(item)
  elif asset in fixed:
   item.update(pool_tier='CORE',pool_reason='固定现货观察，持有周期按天');core.append(item)
  elif asset in protected:
   item.update(pool_tier='ACTIVE',pool_reason='有持仓或未完成委托，保留监控');forced.append(item)
  elif bounded and not admitted_kind(item):
   item.update(pool_tier='ARCHIVED',pool_reason='保留宏观传导背景，不占当前投资范围的观察名额')
  elif fresh:
   item.update(pool_tier='ARCHIVED',pool_reason='活跃名额已满，保留历史研究');candidates.append(item)
 # Keep comparable incumbents; new candidates must beat the weakest by a margin.
 rank=lambda i:(-i['priority_score'],i['asset'])
 protected_count=len(protected-original-fixed) if bounded else len(forced)
 free=max(0,p['active_limit']-protected_count);kept=sorted([i for i in candidates if previous.get(i['asset'],{}).get('pool_tier') in ('ACTIVE','FOCUS')],key=rank)[:free]
 challengers=sorted([i for i in candidates if i not in kept],key=rank)
 for item in challengers:
  if len(kept)<free:kept.append(item)
  elif kept:
   weakest=sorted(kept,key=rank)[-1]
   if item['priority_score']>=weakest['priority_score']+p['replacement_margin']:kept.remove(weakest);kept.append(item)
 for item in kept:item.update(pool_tier='ACTIVE',pool_reason='独立影响评估通过，持续观察')
 active=forced+kept
 focus=sorted([i for i in active if i['focus_eligible']],key=lambda i:(not i['protected'],-i['priority_score'],i['asset']))[:p['focus_limit']]
 for item in focus:item.update(pool_tier='FOCUS',pool_reason='优先研究相关新证据'+('；有持仓或未完成委托' if item['protected'] else ''))
 current=sorted(core+active,key=lambda i:(i['pool_tier']!='CORE',i['pool_tier']!='FOCUS',not i['protected'],-i['priority_score'],i['asset']))
 archive=sorted([i for i in items if i not in current],key=lambda i:(i['pool_tier']!='COOLING',-i['priority_score'],i['asset']))
 return {'items':current,'archived_items':archive,'policy':p,'counts':{'active':len(active),'focus':len(focus),'protected':len(forced),'protected_overflow':max(0,len(forced)-p['active_limit']),'cooling':sum(i['pool_tier']=='COOLING' for i in archive),'archived':sum(i['pool_tier']=='ARCHIVED' for i in archive),'original':len(original),'fixed':len(fixed),'total':len(original|fixed|protected|{i['asset'] for i in active})}}

def reconcile(store,config,at):
 from .observation import all_items
 allocation=allocate(store,all_items(store,at),at,config)
 with store.db:
  for item in allocation['items']+allocation['archived_items']:
   data={k:item[k] for k in ('pool_tier','pool_reason','priority_score','protected','last_catalyst_at','review_due_at','expires_at')}
   old=store.db.execute('SELECT payload_json FROM macro_watch_state WHERE asset=?',(item['asset'],)).fetchone();prior=json.loads(old[0]) if old else {}
   if (prior.get('pool_tier'),prior.get('protected'))!=(data['pool_tier'],data['protected']):
    store.db.execute('INSERT INTO macro_watch_transitions(asset,at,previous_tier,tier,reason) VALUES(?,?,?,?,?)',(item['asset'],at,prior.get('pool_tier'),data['pool_tier'],data['pool_reason']))
   store.db.execute('INSERT OR REPLACE INTO macro_watch_state VALUES(?,?,?)',(item['asset'],at,json.dumps(data,ensure_ascii=False)))
 return allocation
