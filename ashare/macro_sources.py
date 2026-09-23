"""Public global macro feeds and actual indicator observations; never broker quotes."""
from __future__ import annotations
import csv,io,json,math,re,xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit,urlunsplit
from .storage import now,normalize_time
from . import dynamic_sources as data

from .news_catalog import FEEDS, article_hosts

# Research targets are asset classes/benchmarks, not executable security identifiers.
# Yield UP means yield rises; EURUSD UP means the euro appreciates against USD.
ASSETS={
 'BTC':{'name':'比特币','group':'商品','unit':'美元/枚'},
 'ETH':{'name':'以太坊','group':'商品','unit':'美元/枚'},
 'GOLD':{'name':'黄金','group':'贵金属','unit':'美元/盎司'},
 'SILVER':{'name':'白银','group':'贵金属','unit':'美元/盎司'},
 'PLATINUM':{'name':'铂金','group':'贵金属','unit':'美元/盎司'},
 'PALLADIUM':{'name':'钯金','group':'贵金属','unit':'美元/盎司'},
 'WTI':{'name':'WTI原油现货','group':'能源','unit':'美元/桶','series':'DCOILWTICO'},
 'BRENT':{'name':'布伦特原油现货','group':'能源','unit':'美元/桶','series':'DCOILBRENTEU'},
 'NATGAS':{'name':'美国天然气现货','group':'能源','unit':'美元/百万英热单位','series':'DHHNGSP'},
 'COPPER':{'name':'铜','group':'工业金属','unit':'价格'},
 'AGRICULTURE':{'name':'农产品','group':'商品','unit':'价格'},
 'US10Y':{'name':'美国10年期国债收益率','group':'利率','unit':'%','series':'DGS10','change_unit':'bp'},
 'USD':{'name':'广义贸易加权美元指数','group':'汇率','unit':'指数','series':'DTWEXBGS'},
 'EURUSD':{'name':'欧元兑美元','group':'汇率','unit':'美元/欧元','series':'DEXUSEU'},
 'USDJPY':{'name':'美元兑日元','group':'汇率','unit':'日元/美元','series':'DEXJPUS'},
 'GBPUSD':{'name':'英镑兑美元','group':'汇率','unit':'美元/英镑','series':'DEXUSUK'},
 'AUDUSD':{'name':'澳元兑美元','group':'汇率','unit':'美元/澳元','series':'DEXUSAL'},
 'NZDUSD':{'name':'新西兰元兑美元','group':'汇率','unit':'美元/新西兰元','series':'DEXUSNZ'},
 'USDCNY':{'name':'美元兑人民币','group':'汇率','unit':'人民币/美元','series':'DEXCHUS'},
 'EU10Y':{'name':'欧元区国债收益率','group':'利率','unit':'%'},
 'JP10Y':{'name':'日本国债收益率','group':'利率','unit':'%'},
 'SP500':{'name':'美国标普500','group':'股票市场','unit':'指数','series':'SP500'},
 'NASDAQ':{'name':'纳斯达克综合指数','group':'科技','unit':'指数','series':'NASDAQCOM'},
 'EU_EQUITY':{'name':'欧洲股票市场','group':'股票市场','unit':'市场'},
 'JP_EQUITY':{'name':'日本股票市场','group':'股票市场','unit':'市场'},
 'HK_EQUITY':{'name':'香港股票市场','group':'股票市场','unit':'市场'},
 'CN_EQUITY':{'name':'中国内地股票市场','group':'股票市场','unit':'市场'},
 'EM_EQUITY':{'name':'新兴市场股票','group':'股票市场','unit':'市场'},
 'GLOBAL_TECH':{'name':'全球科技行业','group':'科技','unit':'行业'},
 'GLOBAL_HEALTH':{'name':'全球医疗行业','group':'医疗','unit':'行业'},
}

def parse_feed(raw,source):
 if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():raise ValueError('不支持XML实体声明')
 root=ET.fromstring(raw);rows=[];errors=[]
 for item in [e for e in root.iter() if e.tag.split('}')[-1] in ('item','entry')][:100]:
  fields={child.tag.split('}')[-1]:child for child in item}
  def value(key):return ''.join(fields[key].itertext()).strip() if key in fields else ''
  title=data.plain(value('title'));url=value('link')
  if not url:
   url=next((x.attrib.get('href','') for x in item if x.tag.split('}')[-1]=='link' and x.attrib.get('rel','alternate')=='alternate'),'')
  if not title or not url:continue
  try:
   p=urlsplit(url)
   if p.scheme=='http' and p.hostname in data.NEWS_HOSTS:url=urlunsplit(p._replace(scheme='https'))
   data.check_url(url)
   allowed=article_hosts(source)
   if allowed and urlsplit(url).hostname not in allowed:raise ValueError('文章域名与标注的新闻来源不符')
   stamp=value('pubDate') or value('published') or value('date')
   # A feed-level update time (or HTTP fetch time) is not publication time.
   try:stamp=datetime.fromisoformat(stamp.replace('Z','+00:00'))
   except ValueError:stamp=parsedate_to_datetime(stamp)
   if stamp.tzinfo is None:raise ValueError('新闻发布时间缺少时区')
   body=title+'\n'+data.plain(value('encoded') or value('content') or value('description') or value('summary'))
   rows.append({'source':source,'url':url,'published_at':normalize_time(stamp.isoformat()),'title':title,'body':body[:8000]})
  except (ValueError,TypeError,OverflowError) as exc:errors.append(str(exc))
 if not rows:raise ValueError(errors[0] if errors else '订阅没有可解析且带发布时间的条目')
 return rows

def collect(store,start,end,at,fetch_fn=None):
 fetch_fn=fetch_fn or data.fetch;result={'added':0,'window_counts':{},'failures':[]}
 def read(feed):
  name,url=feed
  try:
   raw=fetch_fn(url);return name,raw,parse_feed(raw,name),None
  except Exception as exc:return name,None,[],str(exc)[:180]
 with ThreadPoolExecutor(max_workers=5,thread_name_prefix='macro-feed') as pool:
  for name,raw,rows,error in pool.map(read,FEEDS):
   available=[r for r in rows if r['published_at']<=at]
   count=sum(start<=r['published_at']<end for r in available)
   if not error and not available:error='订阅仅含未来时间，未作为当前新闻入库'
   if error:result['failures'].append(name+'：'+error)
   else:
    result['added']+=data.ingest(store,available,at,store.raw(raw,'.xml'))
    result['window_counts'][name]=count
   with store.db:
    store.db.execute('INSERT OR REPLACE INTO dynamic_feed_checks VALUES(?,?,?,?,?)',
     (name,at,'FAILED' if error else 'OK',count,error or '公开订阅标题与摘要；未声称阅读全文或完整回补48小时'))
    if not error:
     store.db.execute('INSERT OR REPLACE INTO macro_feed_health VALUES(?,?,?,?,?)',
      (name,at,max(r['published_at'] for r in available),min(r['published_at'] for r in available),len(available)))
 return result

def parse_series(raw,series,at):
 reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
 if reader.fieldnames not in (['observation_date',series],['DATE',series]):raise ValueError('指标CSV结构不符')
 date_field=reader.fieldnames[0];today=normalize_time(at)[:10];values={}
 for row in reader:
  day=row[date_field];datetime.strptime(day,'%Y-%m-%d')
  if day>=today or row[series] in ('','.'):continue
  v=float(row[series])
  if not math.isfinite(v):raise ValueError('指标数值无效')
  if day in values:raise ValueError('重复指标日期')
  values[day]=v
 if not values:raise ValueError('没有已发布的历史观测')
 return [{'date':k,'value':v} for k,v in sorted(values.items())]

def refresh_markets(store,at=None,fetch_fn=None,priority_assets=None):
 at=normalize_time(at or now());fetch_fn=fetch_fn or data.fetch
 cached={r['asset']:dict(r) for r in store.db.execute('SELECT * FROM macro_markets')}
 requested=set()
 for r in store.db.execute("SELECT payload_json FROM macro_events WHERE status!='INVALIDATED' ORDER BY created_at DESC LIMIT 100"):
  requested.update(x['asset'] for x in json.loads(r[0]).get('impacts',[]))
 targets=[a for a,m in ASSETS.items() if m.get('series') and (a not in cached or (datetime.fromisoformat(at)-datetime.fromisoformat(cached[a]['checked_at'])).total_seconds()>=21600)]
 baseline=['US10Y','USD','WTI','SP500','EURUSD','BRENT','NATGAS','NASDAQ','USDJPY','GBPUSD','AUDUSD','NZDUSD','USDCNY']
 targets=sorted(targets,key=lambda a:(a not in (priority_assets or []),a not in requested,cached.get(a,{}).get('checked_at',''),baseline.index(a)))[:5]
 start=(datetime.fromisoformat(at)-timedelta(days=370)).date().isoformat()
 def read(asset):
  m=ASSETS[asset];url='https://fred.stlouisfed.org/graph/fredgraph.csv?id='+m['series']+'&cosd='+start
  try:
   raw=fetch_fn(url);points=parse_series(raw,m['series'],at)
   return asset,raw,{'series':m['series'],'url':'https://fred.stlouisfed.org/series/'+m['series'],'points':points,'as_of':at,'unit':m['unit']},None
  except Exception as exc:return asset,None,None,str(exc)[:180]
 failures=[]
 with ThreadPoolExecutor(max_workers=3,thread_name_prefix='macro-history') as pool:
  for asset,raw,payload,error in pool.map(read,targets):
   if error:
    failures.append(ASSETS[asset]['name']+'：'+error)
    payload=json.loads(cached[asset]['payload_json']) if asset in cached else {}
   else:payload['raw_path']=store.raw(raw,'.csv')
   with store.db:store.db.execute('INSERT OR REPLACE INTO macro_markets VALUES(?,?,?,?,?)',(asset,at,'FAILED' if error else 'OK',json.dumps(payload,ensure_ascii=False),error))
 return failures
