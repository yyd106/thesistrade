"""Persistent public research targets. Never modifies execution watchlist or orders."""
from __future__ import annotations
import csv,io,json,re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from .macro_sources import ASSETS
from .storage import normalize_time

CATEGORIES={'CN':'A股','US':'美股','COMMODITY':'商品与贵金属','RATES_FX':'债券与外汇'}
DIRECTORIES=(('nasdaq','https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt'),('other','https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt'))

def benchmark(asset):
 spec=ASSETS[asset]
 category='CN' if asset=='CN_EQUITY' else 'US' if asset in ('SP500','NASDAQ') else 'RATES_FX' if spec['group'] in ('利率','汇率') else 'COMMODITY' if spec['group'] in ('贵金属','能源','工业金属','商品') else 'WORLD'
 return {**spec,'asset':asset,'category':category,'kind':'BENCHMARK','identity_source':'公开市场指标目录'}

def registry(store):
 return {**{a:benchmark(a) for a in ASSETS},**{r['asset']:json.loads(r['payload_json']) for r in store.db.execute('SELECT * FROM macro_instruments')}}

def parse_directory(raw,kind,url):
 reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig')),delimiter='|')
 key='Symbol' if kind=='nasdaq' else 'ACT Symbol'
 if not {key,'Security Name','Test Issue','ETF'}.issubset(reader.fieldnames or []):raise ValueError('美股证券目录结构变化')
 rows=[]
 for r in reader:
  ticker=r[key]
  if not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,9}',ticker or '') or r['Test Issue']!='N':continue
  # Common shares and ETFs only; no warrants, units, preferred stock or test issues.
  name=r['Security Name'] or ''
  if re.search(r'\bwarrants?\b|\bunits?\b|preferred|depositary shares each representing',name,re.I):continue
  rows.append({'asset':'US:'+ticker,'name':name,'symbol':ticker,'category':'US','kind':'ETF' if r['ETF']=='Y' else 'STOCK','unit':'美元','identity_source':url})
 if not rows:raise ValueError('美股目录没有有效证券')
 return rows

def refresh_registry(store,at,fetch_fn=None):
 from .dynamic_sources import fetch
 from .sources import stock_catalog
 at=normalize_time(at);errors=[];fetch_fn=fetch_fn or fetch
 def save(rows):
  with store.db:store.db.executemany('INSERT OR REPLACE INTO macro_instruments VALUES(?,?,?,?)',[(r['asset'],r['category'],at,json.dumps(r,ensure_ascii=False)) for r in rows])
 def stale(category):
  stamp=store.db.execute('SELECT min(checked_at) FROM macro_instruments WHERE category=?',(category,)).fetchone()[0]
  return not stamp or (datetime.fromisoformat(at)-datetime.fromisoformat(stamp)).total_seconds()>86400
 if stale('CN'):
  try:
   rows=[]
   for s in stock_catalog(store):
    code=s.get('code','')
    if s.get('category')=='A股' and re.fullmatch(r'(?:60|68|00|30)\d{4}',code):
     symbol=('sh' if code.startswith('6') else 'sz')+code
     rows.append({'asset':symbol,'symbol':symbol,'name':s['zwjc'],'category':'CN','kind':'STOCK','unit':'元','identity_source':'https://www.cninfo.com.cn/new/data/szse_stock.json'})
   if not rows:raise ValueError('A股目录为空')
   with store.db:
    store.db.execute("DELETE FROM macro_instruments WHERE category='CN'")
    save(rows)
  except Exception as exc:errors.append('A股身份目录：'+str(exc)[:150])
 if stale('US'):
  def read(item):
   kind,url=item
   try:
    raw=fetch_fn(url);rows=parse_directory(raw,kind,url);return rows,raw,None
   except Exception as exc:return [],None,str(exc)[:150]
  results=[]
  with ThreadPoolExecutor(max_workers=2,thread_name_prefix='instrument-directory') as pool:results=list(pool.map(read,DIRECTORIES))
  # Replace a market snapshot only when both official files are valid.
  if all(not error for rows,raw,error in results):
   with store.db:
    store.db.execute("DELETE FROM macro_instruments WHERE category='US'")
    save([r for rows,raw,error in results for r in rows])
   for rows,raw,error in results:store.raw(raw,'.txt')
  else:errors+=['美股身份目录：'+error for rows,raw,error in results if error]
 return errors

def sync_event(store,event_id,analysis,at,assets):
 """Called only after validated research. One target can retain multiple causes."""
 for impact in analysis['impacts']:
  asset=impact['asset'];spec=assets.get(asset)
  if not spec:continue
  store.db.execute('INSERT INTO macro_watchlist VALUES(?,?,?,?) ON CONFLICT(asset) DO UPDATE SET updated_at=max(macro_watchlist.updated_at,excluded.updated_at),payload_json=CASE WHEN excluded.updated_at>=macro_watchlist.updated_at THEN excluded.payload_json ELSE macro_watchlist.payload_json END',
   (asset,at,at,json.dumps(spec,ensure_ascii=False)))
  store.db.execute('INSERT OR REPLACE INTO macro_watch_links VALUES(?,?,?,?)',(asset,event_id,at,json.dumps(impact,ensure_ascii=False)))

def all_items(store,at):
 from .macro_impact import context
 assessments=context(store,at)
 items=[]
 groups={r['news_id']:r['representative_id'] for r in store.db.execute('SELECT q.* FROM macro_news_queue q JOIN dynamic_news n ON n.id=q.representative_id WHERE n.status!=?',('REVISED',))}
 anchors={r[0]:r[1] for r in store.db.execute('SELECT q.representative_id,min(n.published_at) FROM macro_news_queue q JOIN dynamic_news n ON n.id=q.news_id WHERE n.status!=? GROUP BY q.representative_id',('REVISED',))}
 for row in store.db.execute('SELECT * FROM macro_watchlist WHERE added_at<=? ORDER BY updated_at DESC,asset',(at,)):
  item={**json.loads(row['payload_json']),'added_at':row['added_at'],'updated_at':row['updated_at']};links=[];seen={}
  for r in store.db.execute('''SELECT l.*,e.news_id,e.status,e.payload_json,n.title,n.published_at,n.url,n.source FROM macro_watch_links l JOIN macro_events e ON e.id=l.event_id
   JOIN dynamic_news n ON n.id=e.news_id WHERE l.asset=? AND l.linked_at<=? ORDER BY n.published_at DESC,l.event_id''',(row['asset'],at)):
   group=groups.get(r['news_id'],r['news_id'])
   if group in seen and seen[group]['status']=='TRACKING':continue
   analysis=json.loads(r['payload_json'])
   current=next((i for i in analysis['impacts'] if i['asset']==row['asset']),None)
   if group in seen:links.remove(seen[group])
   links.append({'event_id':r['event_id'],'headline':analysis['headline'],'published_at':r['published_at'],'url':r['url'],
    'catalyst_at':anchors.get(group,r['published_at']),'horizon':analysis.get('horizon','UNCERTAIN'),'theme':analysis.get('theme'),'source':r['source'],'status':'INVALIDATED' if r['status']=='INVALIDATED' or not current else 'TRACKING','impact':current or json.loads(r['impact_json']),'materiality':assessments.get((r['event_id'],row['asset']))})
   seen[group]=links[-1]
  item['links']=links;active=[l for l in links if l['status']=='TRACKING']
  item['status']='WATCHING' if active else 'NEEDS_REVIEW'
  item['strength']=max((l['impact'].get('strength','UNKNOWN') for l in active),key=lambda s:{'HIGH':3,'MEDIUM':2,'LOW':1}.get(s,0),default='UNKNOWN')
  directions={l['impact']['direction'] for l in active if l['impact']['direction'] in ('UP','DOWN')}
  all_directions={l['impact']['direction'] for l in active}
  item['direction']='MIXED' if len(directions)>1 or 'MIXED' in all_directions else 'UNCLEAR' if 'UNCLEAR' in all_directions or not directions else next(iter(directions))
  item['conflicting']=len(directions)>1
  market=store.db.execute('SELECT * FROM macro_markets WHERE asset=? AND checked_at<=?',(row['asset'],at)).fetchone()
  points=json.loads(market['payload_json']).get('points',[]) if market else []
  item['indicator']=points[-1] if points else None
  item['indicator_status']=market['status'] if market else 'NOT_CONNECTED'
  if item['category']=='CN' and item['kind']=='STOCK':
   from .dynamic_sources import latest_quote
   item['quote']=latest_quote(store,item['asset'],at)
  items.append(item)
 from .industry import links as industry_links
 by_asset={i['asset']:i for i in items}
 for asset,item in industry_links(store,at).items():
  if asset in by_asset:by_asset[asset]['links']+=item['links']
  else:items.append(item)
 return items


def view(store,at,config=None,archive_offset=0,asset=None):
 from .observation_pool import allocate
 result=allocate(store,all_items(store,at),at,config)
 from .global_market import latest
 from .global_paper import balance
 from .global_research import active_plan
 held=balance(store,at)['positions']
 for item in result['items']:
  q=latest(store,item['asset'],at)
  item['spot_quote']=q
  item['position']=held.get(item['asset'])
  plan=active_plan(store,item['asset'],at)
  item['trade_plan']={**plan,'payload':json.loads(plan['payload_json'])} if plan else None
  check=store.db.execute('SELECT * FROM plan_rechecks WHERE plan_id=?',(plan['id'],)).fetchone() if plan else None
  item['recheck']={**dict(check),'payload':json.loads(check['payload_json'])} if check else None
 archived=result['archived_items'];result['archive_count']=len(archived)
 if asset:
  index=next((i for i,t in enumerate(archived) if t['asset']==asset),None)
  if index is not None:archive_offset=index//25*25
 result['archive_offset']=archive_offset
 result['archived_items']=archived[archive_offset:archive_offset+25]
 result['categories']=[{'id':k,'name':v} for k,v in CATEGORIES.items()]
 result['membership']={i['asset']:{'tier':i['pool_tier'],'category':i['category']} for i in result['items']+archived}
 return result
