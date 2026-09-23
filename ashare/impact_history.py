"""Prospective, volatility-scaled event calibration. No trading or causal claims."""
from __future__ import annotations
import json,math,statistics
from datetime import datetime
from .storage import digest
from .macro_sources import ASSETS

VERSION='impact-history-1'
MIN_TRAIN=20
MIN_VALIDATION=10
MATERIAL_Z=1.5  # Engineering hypothesis, not a validated trading threshold.

def encode(value):return json.dumps(value,ensure_ascii=False,sort_keys=True)
def days(a,b):return (datetime.fromisoformat(a[:10])-datetime.fromisoformat(b[:10])).days

def market(store,asset,at):
 if asset=='CN_EQUITY':
  row=store.db.execute('SELECT * FROM dynamic_market WHERE symbol=? AND updated_at<=?',('sh000300',at)).fetchone()
  if not row:return None
  data=json.loads(row['payload_json'])
  return {'points':[{'date':b[0],'value':float(b[2])} for b in data.get('bars',[]) if b[0]<at[:10]],'checked_at':row['updated_at'],'raw_path':data.get('raw_path'),
   'url':'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000300,day,,,320,','proxy':'沪深300宽基代理，不能代表单一产业或全部A股'}
 row=store.db.execute("SELECT * FROM macro_markets WHERE asset=? AND status='OK' AND checked_at<=?",(asset,at)).fetchone()
 if not row:return None
 p=json.loads(row['payload_json']);p['checked_at']=row['checked_at']
 p['points']=sorted([x for x in p.get('points',[]) if x['date']<at[:10]],key=lambda x:x['date'])
 return p

def delta(a,b,unit):
 if unit=='bp':return (b-a)*100
 return (b/a-1)*100 if a>0 and b>0 else None

def feature_key(asset,a):
 key=[asset,a['event_kind'],a['scope'],a['novelty'],a['magnitude'],a['scale']['kind']]
 # Preserve prior economic cohorts; never pool verbal repricing with physical actions.
 if a.get('impact_basis','ECONOMIC')!='ECONOMIC':key.extend([a['impact_basis'],a['expectation_test']['channel'],a['expectation_test']['authority']])
 return encode(key)

def freeze(store,asset,a,published,at):
 """Freeze all prediction features before the outcome, even for rejected news."""
 key=feature_key(asset,a);spec=ASSETS.get(asset,{})
 f={'measurement_resolution':'DAILY_ONLY','intraday_confirmation':False,'key':key,'published_at':published,'direction':a['direction'],'basis':'FORWARD' if 0<=(datetime.fromisoformat(at)-datetime.fromisoformat(published)).total_seconds()<=7200 else 'LATE_REVIEW','version':VERSION,'measurable':False}
 if not spec.get('series') and asset!='CN_EQUITY':return {**f,'gap':'该标的尚无可用历史行情序列'}
 p=market(store,asset,at)
 if not p:return {**f,'gap':'评估时未取得可用行情版本'}
 unit=spec.get('change_unit','%');points=[x for x in p['points'] if x['date']<published[:10]][-61:]
 benchmark='SP500' if asset=='NASDAQ' else None
 bp=market(store,benchmark,at) if benchmark else None
 if benchmark:
  by_date={x['date']:x for x in (bp or {}).get('points',[])}
  points=[{**x,'benchmark_value':by_date[x['date']]['value']} for x in points if x['date'] in by_date]
 changes=[]
 for first,last in zip(points,points[1:]):
  d=delta(first['value'],last['value'],unit)
  if d is None:continue
  if benchmark:
   bd=delta(first['benchmark_value'],last['benchmark_value'],'%')
   if bd is None:continue
   d-=bd
  changes.append(d)
 if len(changes)<20:return {**f,'gap':'评估前有效日变化不足20个，无法估计常态波动'}
 if days(published,points[-1]['date'])>4:return {**f,'gap':'评估前基准行情过旧，不能用于事件校准'}
 vol=statistics.stdev(changes)
 if vol<=1e-9:return {**f,'gap':'历史波动不足以归一化'}
 return {**f,'measurable':True,'baseline':points[-1],'daily_volatility':vol,'volatility_points':len(changes),'change_unit':unit,'benchmark':benchmark,
  'market_url':p['url'],'proxy':p.get('proxy'),'market_raw_path':p.get('raw_path'),'market_available_at':p['checked_at'],'benchmark_raw_path':(bp or {}).get('raw_path'),
  'method':'NASDAQ相对标普500的日变化' if benchmark else (p.get('proxy') or '该指标自身日变化')+'；未扣除其他共同因素'}

def measure(store,at):
 count=0
 rows=store.db.execute('''SELECT a.* FROM macro_impact_assessments a WHERE a.created_at<=? AND json_extract(a.features_json,'$.measurable')=1 AND NOT EXISTS
  (SELECT 1 FROM macro_impact_outcomes o WHERE o.assessment_id=a.id) ORDER BY a.created_at DESC LIMIT 2000''',(at,)).fetchall()
 for r in rows:
  f=json.loads(r['features_json'])
  if not f.get('measurable'):continue
  p=market(store,r['asset'],at)
  if not p:continue
  after=[x for x in p['points'] if x['date']>f['published_at'][:10]]
  if len(after)<3:continue
  end=after[2];base=f['baseline']
  if days(end['date'],f['published_at'])>14:continue
  change=delta(base['value'],end['value'],f['change_unit'])
  if change is None:continue
  benchmark_result=None
  if f['benchmark']:
   bp=market(store,f['benchmark'],at);b=next((x for x in (bp or {}).get('points',[]) if x['date']==end['date']),None)
   if not b:continue
   benchmark_result=delta(base['benchmark_value'],b['value'],'%')
   if benchmark_result is None:continue
   change-=benchmark_result
  # Baseline to t+3 also contains the event-day change (when an observation exists).
  steps=len([x for x in p['points'] if base['date']<x['date']<=end['date']])
  z=abs(change)/(f['daily_volatility']*math.sqrt(max(1,steps)))
  o={'start':base['date'],'end':end['date'],'change':round(change,5),'change_unit':f['change_unit'],'normalized_move':round(z,5),'material':z>=MATERIAL_Z,
   'direction_correct':(change>0 and f['direction']=='UP') or (change<0 and f['direction']=='DOWN') if f['direction'] in ('UP','DOWN') else None,
   'basis':f['basis'],'market_url':p['url'],'proxy':p.get('proxy'),'market_raw_path':p.get('raw_path'),'market_available_at':p['checked_at'],'benchmark_change':benchmark_result,
   'method':(f.get('proxy')+'；' if f.get('proxy') else '')+'事件前最近已知观测至事件后第3个观测日；常态波动按实际观测步数缩放。日级相关反应，不能证明产业因果或交易收益。'}
  with store.db:store.db.execute('INSERT INTO macro_impact_outcomes VALUES(?,?,?)',(r['id'],at,encode(o)))
  count+=1
 return count

def wilson(hits,n):
 if not n:return [0.,1.]
 z=1.96;p=hits/n;den=1+z*z/n;mid=(p+z*z/(2*n))/den;half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
 return [max(0.,mid-half),min(1.,mid+half)]

def fit(samples):
 """Chronological holdout; Beta(1,1) shrinkage, confidence bounds, no magic win rate."""
 samples=sorted(samples,key=lambda x:(x['created_at'],x['id']));n=len(samples);split=max(MIN_TRAIN,n*2//3)
 train=samples[:split];validation=samples[split:]
 def stats(rows):
  hits=sum(s['outcome']['material'] for s in rows);size=len(rows)
  return {'n':size,'material_count':hits,'probability':(hits+1)/(size+2) if size else None,'interval':wilson(hits,size)}
 t,v=stats(train),stats(validation);state='INSUFFICIENT'
 if t['n']>=MIN_TRAIN and v['n']>=MIN_VALIDATION:
  state='SUPPORTED' if min(t['interval'][0],v['interval'][0])>.5 else 'WEAK' if max(t['interval'][1],v['interval'][1])<.5 else 'INCONCLUSIVE'
 changes=[abs(s['outcome']['change']) for s in samples]
 return {'version':VERSION,'state':state,'sample_count':n,'required_count':MIN_TRAIN+MIN_VALIDATION,'train':t,'validation':v,
  'median_absolute_change':statistics.median(changes) if changes else None,'change_unit':samples[0]['outcome']['change_unit'] if samples else None,
  'material_z_threshold':MATERIAL_Z,'sample_ids':[s['id'] for s in samples],
  'limitation':'同标的、事件类型、地域、增量状态、预判强度和规模依据的前向样本；去重且窗口不重叠。日级市场反应不等于产业因果。'}

def models(store,at):
 groups={};ends={};seen=set()
 # Collapse repeated assessments and event groups; exclude revised/retracted inputs.
 rows=store.db.execute('''SELECT a.*,o.ready_at,o.payload_json AS outcome_json,e.news_id,coalesce(q.representative_id,e.news_id) AS cluster
 FROM macro_impact_assessments a JOIN macro_impact_outcomes o ON o.assessment_id=a.id
 JOIN macro_events e ON e.id=a.event_id JOIN dynamic_news n ON n.id=e.news_id
 LEFT JOIN macro_news_queue q ON q.news_id=e.news_id
 WHERE o.ready_at<=? AND a.created_at<=? AND e.status!='INVALIDATED' AND n.status!='REVISED'
 ORDER BY a.created_at,a.id''',(at,at)).fetchall()
 for r in rows:
  f=json.loads(r['features_json']);o=json.loads(r['outcome_json']);identity=(r['asset'],r['cluster'])
  if f['basis']!='FORWARD' or f.get('version')!=VERSION or identity in seen:continue
  # Non-overlap across ALL categories of the same asset, before stratification.
  if o['start']<=ends.get(r['asset'],''):continue
  ends[r['asset']]=o['end'];seen.add(identity)
  groups.setdefault(f['key'],[]).append({'id':r['id'],'created_at':r['created_at'],'outcome':o})
 return {key:fit(samples) for key,samples in groups.items()}

def learn(store,at):
 measured=measure(store,at);calibrations=models(store,at)
 with store.db:
  for key,m in calibrations.items():
   identifier=digest(VERSION+key+encode(m))[:24]
   store.db.execute('INSERT OR IGNORE INTO macro_impact_calibrations VALUES(?,?,?,?)',(identifier,at,key,encode(m)))
 return {'measured':measured,'cohorts':len(calibrations),'forward_samples':sum(m['sample_count'] for m in calibrations.values()),'calibrated_cohorts':sum(m['state']!='INSUFFICIENT' for m in calibrations.values())}
