"""Bounded public news and market readers, isolated from watchlist evidence/checks."""
from __future__ import annotations
import gzip
import html
import json
import re
import time
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from . import sources
from .calendar import SH, completed_bar_cutoff
from .finance import cents
from .storage import digest, now, normalize_time

NEWS_HOSTS = {'zhibo.sina.com.cn','finance.sina.com.cn','news.un.org','www.mofcom.gov.cn',
 'www.federalreserve.gov','www.ecb.europa.eu','www.bankofengland.co.uk','www.boj.or.jp',
 'www.eia.gov','fred.stlouisfed.org','www.nasdaqtrader.com'}
from .news_catalog import EXTRA_HOSTS
NEWS_HOSTS |= EXTRA_HOSTS
THEMES = {
 'gold':('黄金',r'黄金|金价|贵金属|gold\b', [('sh600547','山东黄金'),('sh600489','中金黄金')]),
 'energy':('能源与期货',r'原油|石油|天然气|OPEC|霍尔木兹|crude|oil\b', [('sh601857','中国石油'),('sh600938','中国海油')]),
 'technology':('科技',r'半导体|芯片|人工智能|算力|机器人|semiconductor|AI\b', [('sz002371','北方华创'),('sh603986','兆易创新')]),
 'healthcare':('医疗',r'医药|医疗|药品|临床|创新药|FDA|drug\b', [('sh600276','恒瑞医药'),('sz000661','长春高新')]),
 'macro':('宏观政策',r'降息|加息|关税|通胀|货币政策|美联储|央行|inflation|tariff', [('sh600547','山东黄金'),('sz002371','北方华创')]),
 'commodities':('其他商品与期货',r'期货|铜价|铝价|玉米|大豆|铁矿|煤炭', [('sh601899','紫金矿业'),('sh600362','江西铜业')]),
}

def check_url(url):
 p=urlsplit(url)
 if p.scheme!='https' or p.hostname not in NEWS_HOSTS or p.username or p.password or p.port not in (None,443):
  raise ValueError('动态新闻来源地址不在允许列表')

class Redirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,newurl):
  check_url(newurl)
  return super().redirect_request(req,fp,code,msg,headers,newurl)

def fetch(url):
 check_url(url)
 req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Accept-Encoding':'identity'})
 with urllib.request.build_opener(Redirect()).open(req,timeout=12) as r:
  check_url(r.url);data=r.read(4_000_001)
  if len(data)>4_000_000:raise ValueError('新闻响应超过容量限制')
 if data[:2]==b'\x1f\x8b':
  import io
  with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:data=f.read(4_000_001)
  if len(data)>4_000_000:raise ValueError('新闻解压超过容量限制')
 return data

def plain(value):
 return html.unescape(re.sub(r'<[^>]+>','',value)).strip()

def parse_sina(raw):
 obj=json.loads(raw)
 rows=obj['result']['data']['feed']['list']
 if not isinstance(rows,list):raise ValueError('新浪快讯结构变化')
 result=[]
 for row in rows:
  body=plain(row['rich_text'])[:8000]
  if len(body)<15 or row.get('is_delete'):continue
  stamp=normalize_time(datetime.strptime(row['create_time'],'%Y-%m-%d %H:%M:%S').replace(tzinfo=SH).isoformat())
  ext=json.loads(row.get('ext') or '{}');url=ext.get('docurl')
  if not url or urlsplit(url).hostname!='finance.sina.com.cn':url='https://finance.sina.com.cn/7x24/?id='+str(int(row['id']))
  check_url(url)
  title=body.split('】')[0]+'】' if body.startswith('【') and '】' in body else body[:95]
  result.append({'source':'新浪财经快讯','url':url,'published_at':stamp,'title':title,'body':body})
 return result

def ingest(store,rows,seen,raw_path):
 added=0
 for r in rows:
  published=normalize_time(r['published_at'])
  if published>seen:continue
  check_url(r['url'])
  # Exact copies share a cluster across publishers; article revisions retain lineage.
  normalized=re.sub(r'[\W_]+','',r['body']).lower()
  cluster=digest(normalized)[:24]
  identity=digest(r['url']+'|'+normalized)[:24]
  previous=store.db.execute('SELECT id,cluster_id FROM dynamic_news WHERE url=? ORDER BY first_seen_at DESC,rowid DESC LIMIT 1',(r['url'],)).fetchone()
  if store.db.execute('SELECT 1 FROM dynamic_news WHERE id=?',(identity,)).fetchone():continue
  duplicate=store.db.execute('SELECT id FROM dynamic_news WHERE cluster_id=?',(cluster,)).fetchone()
  status='DUPLICATE' if duplicate else 'NEW'
  with store.db:
   store.db.execute('INSERT INTO dynamic_news(id,source,url,published_at,first_seen_at,title,body,cluster_id,revision_of,status,raw_path) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
     (identity,r['source'],r['url'],published,seen,r['title'],r['body'],cluster,previous['id'] if previous else None,status,raw_path))
   if previous:
    store.db.execute("UPDATE dynamic_cases SET status='INVALIDATED' WHERE news_id=? AND status IN ('RESEARCH','READY','HOLDING')",(previous['id'],))
    store.db.execute("UPDATE dynamic_news SET status='REVISED' WHERE id=?",(previous['id'],))
    store.db.execute("UPDATE macro_events SET status='INVALIDATED' WHERE news_id=? OR EXISTS (SELECT 1 FROM json_each(macro_events.payload_json,'$.evidence') WHERE json_extract(value,'$.news_id')=?)",(previous['id'],previous['id']))
  added+=1
 return added

def collect(store,start,end,at,fetch_fn=fetch):
 counts={};failures=[];coverage={};added=0
 # Revisit the complete rolling discovery horizon; deduplication keeps this
 # idempotent and captures late reports/revisions inside the 48-hour window.
 lower=normalize_time((datetime.fromisoformat(at)-timedelta(hours=48)).isoformat())
 covered=store.db.execute("SELECT value FROM dynamic_state WHERE key='discovery_48h_covered_until'").fetchone()
 if covered and lower<covered[0]<=at:
  lower=max(lower,normalize_time((datetime.fromisoformat(covered[0])-timedelta(hours=2)).isoformat()))
 allrows=[]
 try:
  for page in range(1,121):
   raw=fetch_fn('https://zhibo.sina.com.cn/api/zhibo/feed?zhibo_id=152&page='+str(page)+'&page_size=100&dire=f&dpc=1')
   rows=parse_sina(raw);path=store.raw(raw,'.json')
   allrows+=rows;added+=ingest(store,[r for r in rows if lower<=r['published_at']<=at],at,path)
   if not rows or min(r['published_at'] for r in rows)<=lower:break
  coverage['sina_complete']=not rows or min(r['published_at'] for r in rows)<=lower
  if coverage['sina_complete']:
   with store.db:store.db.execute("INSERT OR REPLACE INTO dynamic_state VALUES('discovery_48h_covered_until',?)",(at,))
  coverage['lookback_hours']=48
  coverage['oldest_fetched_at']=min((r['published_at'] for r in allrows),default=None)
  counts['新浪财经快讯']=sum(start<=r['published_at']<end for r in allrows)
  coverage['current_window_complete']=not rows or min(r['published_at'] for r in rows)<=start
  if coverage['current_window_complete']:
   with store.db:store.db.execute("INSERT OR REPLACE INTO dynamic_state VALUES('news_watermark',?)",(end,))
  # One independent bounded older page per run, for explicitly retrospective cases.
  old=store.db.execute("SELECT value FROM dynamic_state WHERE key='backfill_page'").fetchone()
  page=int(old[0]) if old else 50
  if page<=200:
   raw=fetch_fn('https://zhibo.sina.com.cn/api/zhibo/feed?zhibo_id=152&page='+str(page)+'&page_size=100&dire=f&dpc=1')
   historical=parse_sina(raw);added+=ingest(store,historical,at,store.raw(raw,'.json'))
   with store.db:store.db.execute("INSERT OR REPLACE INTO dynamic_state VALUES('backfill_page',?)",(str(page+1),))
 except Exception as exc:failures.append('新浪财经：'+str(exc)[:180])
 # Official global and trade sources complement the finance feed. They remain
 # separate from documents/MARKET and never affect watchlist source coverage.
 from .external_news import parse_feed,parse_article
 for key,name,url,kind in __import__('ashare.external_news',fromlist=['FEEDS']).FEEDS:
  try:
   raw=fetch_fn(url);path=store.raw(raw,'.xml' if kind=='rss' else '.html')
   rows=parse_feed(raw,url,kind);n=0
   for r in rows[:(2 if kind=='html' else 8)]:
    if kind=='html':
     article=fetch_fn(r['url']);body,published,precision=parse_article(article,r)
     if precision!='second':continue
     path=store.raw(article,'.html')
    else:body=r['brief'];published=r['published_at']
    item={'source':name,'url':r['url'],'published_at':published,'title':r['title'],'body':body[:8000]}
    added+=ingest(store,[item],at,path);n+=int(start<=published<end)
   counts[name]=n
  except Exception as exc:failures.append(name+'：'+str(exc)[:180])
 from .macro_sources import collect as collect_global
 global_result=collect_global(store,start,end,at,fetch_fn)
 added+=global_result['added'];counts.update(global_result['window_counts']);failures+=global_result['failures']
 for name in ('新浪财经快讯','联合国新闻','商务部新闻'):
  error=next((f for f in failures if f.startswith(name.split('快讯')[0])),None)
  with store.db:store.db.execute('INSERT OR REPLACE INTO dynamic_feed_checks VALUES(?,?,?,?,?)',
   (name,at,'FAILED' if error else 'OK',counts.get(name,0),error or '已检查；0条表示当前窗口无新条目'))
 return {'added':added,'window_counts':counts,'coverage':coverage,'failures':failures,'catchup_start':lower}

def themes(text):
 return [k for k,v in THEMES.items() if re.search(v[1],text,re.I)]

def candidates(news,catalog):
 valid={('sh' if x['code'].startswith('6') else 'sz')+x['code']:x.get('zwjc','') for x in catalog
        if x.get('category')=='A股' and x.get('code','').startswith(('600','601','603','605','000','001','002','003'))}
 text=news['title']+'\n'+news['body'];found={}
 # Extract codes once per article. Compiling a regex for every listed company
 # thrashes Python's shared regex cache and delays the minute worker.
 mentioned_codes=set(re.findall(r'(?<!\d)\d{6}(?!\d)',text))
 for sym,name in valid.items():
  if len(name)>=3 and (name in text or sym[2:] in mentioned_codes):
   found[sym]={'symbol':sym,'name':name,'link':'新闻直接提及，经济影响仍需研究'}
 for theme in themes(text):
  for sym,name in THEMES[theme][2]:
   if valid.get(sym)==name:found.setdefault(sym,{'symbol':sym,'name':name,'link':'主题候选，业务受益关系待核验'})
 return list(found.values())[:8]

def refresh_quotes(store,symbols,at=None):
 if not symbols:return
 at=normalize_time(at or now())
 raw=sources.fetch('https://qt.gtimg.cn/q='+','.join(sorted(set(symbols))),max_bytes=200000)
 path=store.raw(raw,'.txt')
 for symbol in set(symbols):
  try:rows=sources.parse_quotes(raw,[symbol])
  except (ValueError,IndexError):continue
  with store.db:
   for q in rows:
    identity=digest(json.dumps(q,sort_keys=True))[:24]
    store.db.execute('INSERT OR IGNORE INTO dynamic_quotes VALUES(?,?,?,?,?,?,?,?)',
      (identity,symbol,q['name'],q['price_cents'],q['prev_close_cents'],q['observed_at'],at,path))

def latest_quote(store,symbol,at):
 r=store.db.execute('SELECT * FROM dynamic_quotes WHERE symbol=? AND observed_at<=? AND first_seen_at<=? ORDER BY observed_at DESC,rowid DESC LIMIT 1',(symbol,at,at)).fetchone()
 return dict(r) if r else None

def refresh_market(store,symbol,catalog,at=None):
 at=normalize_time(at or now());cutoff=completed_bar_cutoff(at)
 raw=sources.fetch('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param='+symbol+',day,,,320,',max_bytes=2000000)
 bars=json.loads(raw)['data'][symbol]['day']
 bars=sorted([b for b in bars if b[0]<cutoff])
 if len(bars)<60 or len({b[0] for b in bars})!=len(bars):raise ValueError('完整日线不足或重复')
 if any(min(cents(b[1]),cents(b[2]))<=0 for b in bars):raise ValueError('历史价格无效')
 data={'bars':bars,'basis':'UNADJUSTED','raw_path':store.raw(raw,'.json'),'announcement_status':'NOT_CHECKED','announcements':[]}
 if symbol!='sh000300':data.update(announcements(store,symbol,catalog,at))
 with store.db:store.db.execute('INSERT OR REPLACE INTO dynamic_market VALUES(?,?,?)',(symbol,at,json.dumps(data,ensure_ascii=False)))
 return data


def announcements(store,symbol,catalog,at):
 data={'announcements':[]}
 rows=[x for x in catalog if x.get('code')==symbol[2:]]
 if len(rows)!=1:raise ValueError('证券目录不能唯一匹配')
 data['name']=rows[0]['zwjc']
 day=datetime.fromisoformat(at).astimezone(SH).date()
 notices=sources.fetch('https://www.cninfo.com.cn/new/hisAnnouncement/query',form={
  'pageNum':1,'pageSize':30,'column':'sse' if symbol.startswith('sh') else 'szse','tabName':'fulltext',
  'stock':symbol[2:]+','+rows[0]['orgId'],'searchkey':'','secid':'','plate':'','category':'','trade':'',
  'seDate':str(day-timedelta(days=7))+'~'+str(day),'sortName':'time','sortType':'desc','isHLtitle':'false'},max_bytes=2000000)
 parsed=json.loads(notices)
 if 'announcements' not in parsed or 'hasMore' not in parsed:raise ValueError('公告目录结构变化')
 for n in parsed['announcements'] or []:
  if n.get('secCode')!=symbol[2:]:raise ValueError('公告证券不一致')
  data['announcements'].append({'title':plain(n['announcementTitle']),'url':'https://static.cninfo.com.cn/'+n['adjunctUrl'],'date':n['announcementTime']})
 data['announcement_status']='PARTIAL' if parsed['hasMore'] else 'OK';data['announcement_raw']=store.raw(notices,'.json')

 data['announcement_checked_at']=at
 return data

def refresh_announcements(store,symbol,catalog,at):
 row=store.db.execute('SELECT payload_json FROM dynamic_market WHERE symbol=?',(symbol,)).fetchone()
 if not row:return
 payload=json.loads(row[0]);payload.update(announcements(store,symbol,catalog,at))
 with store.db:store.db.execute('UPDATE dynamic_market SET payload_json=? WHERE symbol=?',(json.dumps(payload,ensure_ascii=False),symbol))
