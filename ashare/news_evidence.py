"""Public article snapshots and point-in-time statement context; dynamic lane only."""
from __future__ import annotations
import json,re
from datetime import datetime,timedelta
from html.parser import HTMLParser
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor
from .storage import digest,normalize_time,now
from . import dynamic_sources as data

VERSION='article-text-2'
# Select only publisher article containers, never whole-page navigation or scripts.
SELECTORS={
 'www.federalreserve.gov':({'article'},set()),
 'www.whitehouse.gov':(set(),{'wp-block-post-content','entry-content'}),
 'www.bankofengland.co.uk':(set(),{'page-content','article-body'}),
 'www.eia.gov':({'article'}, {'article','tie-article'}),
 'www.bea.gov':(set(),{'field--name-body'}),
 'www.fda.gov':({'main-content'},{'field--name-body'}),
 'www.who.int':(set(),{'sf-detail-body-wrapper'}),
}
VOID={'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
class ArticleParser(HTMLParser):
 def __init__(self,host):
  super().__init__(convert_charrefs=True);self.ids,self.classes=SELECTORS[host];self.stack=[];self.parts=[];self.captured=False;self.title=[];self.modified=''
 def handle_starttag(self,tag,attrs):
  a=dict(attrs);parent=self.stack[-1] if self.stack else (False,False,False)
  chosen=a.get('id') in self.ids or bool(set(a.get('class','').split())&self.classes)
  active=parent[0] or chosen;skip=parent[1] or tag in ('script','style','nav','footer','noscript','form','aside');title=parent[2] or tag=='h1'
  if chosen:self.captured=True
  if tag=='meta' and a.get('property',a.get('name')) in ('article:modified_time','dateModified'):self.modified=a.get('content','')
  if active and tag in ('p','div','br','li','h1','h2','h3'):self.parts.append('\n')
  if tag not in VOID:self.stack.append((active,skip,title))
 def handle_endtag(self,tag):
  if tag not in VOID and self.stack:self.stack.pop()
 def handle_data(self,text):
  if not self.stack:return
  active,skip,title=self.stack[-1]
  if title and not skip:self.title.append(text)
  if active and not skip:self.parts.append(text)

def parse(raw,url,title):
 host=urlsplit(url).hostname
 if host not in SELECTORS:raise ValueError('该渠道尚无已验证正文解析器')
 p=ArticleParser(host);p.feed(raw.decode('utf-8-sig'))
 body='\n'.join(re.sub(r'\s+',' ',s).strip() for s in ''.join(p.parts).split('\n') if s.strip())
 # The expected feed headline must belong to this page, not an unrelated redirect.
 page=' '.join(p.title)+' '+body
 norm=lambda s:re.sub(r'\W+','',s).casefold()
 matched=norm(title) in norm(page)
 if not matched and host=='www.federalreserve.gov' and ', ' in title:
  speaker,headline=title.split(', ',1)
  matched=bool(re.fullmatch(r"[A-Za-z .'-]{2,50}",speaker) and norm(speaker) in norm(body) and norm(headline) in norm(page))
 if not p.captured or len(body)<180 or not matched:raise ValueError('未验证正文容器、长度或标题匹配，继续保留摘要')
 return {'body':body[:12000],'truncated':len(body)>12000,'modified_at':p.modified}

def eligible(n):
 from .news_catalog import family
 return family(n['source'])=='PRIMARY' and urlsplit(n['url']).hostname in SELECTORS

def fetch_missing(store,news,at,fetch_fn=None,limit=4):
 """At most four requests / two workers. Failure backs off; originals stay immutable."""
 at=normalize_time(at);batch=[]
 for n in news:
  if not eligible(n) or n['published_at']>at or n['first_seen_at']>at:continue
  if store.db.execute('SELECT 1 FROM macro_article_texts WHERE news_id=?',(n['id'],)).fetchone():continue
  trial=store.db.execute('SELECT * FROM macro_article_fetch_attempts WHERE news_id=? AND version=?',(n['id'],VERSION)).fetchone()
  if trial and (trial['attempts']>=3 or (datetime.fromisoformat(at)-datetime.fromisoformat(trial['last_at'])).total_seconds()<21600):continue
  batch.append(n)
  if len(batch)>=limit:break
 def read(n):
  try:
   raw=(fetch_fn or data.fetch)(n['url']);return n,raw,parse(raw,n['url'],n['title']),None
  except Exception as exc:return n,None,None,str(exc)[:160]
 result={'checked':len(batch),'extracted':0,'failures':[]}
 with ThreadPoolExecutor(max_workers=2,thread_name_prefix='article-text') as pool:
  for n,raw,content,error in pool.map(read,batch):
   available=at if fetch_fn else now()
   with store.db:
    store.db.execute('INSERT INTO macro_article_fetch_attempts VALUES(?,?,1,?,?) ON CONFLICT(news_id,version) DO UPDATE SET attempts=attempts+1,last_at=excluded.last_at,error=excluded.error',(n['id'],VERSION,available,error))
    if error:result['failures'].append({'source':n['source'],'news_id':n['id'],'reason':error});continue
    ident=digest(n['id']+VERSION+content['body'])[:24]
    store.db.execute('INSERT OR IGNORE INTO macro_article_texts VALUES(?,?,?,?,?,?,?,?)',(ident,n['id'],available,VERSION,content['body'],int(content['truncated']),content['modified_at'],store.raw(raw,'.html')))
    result['extracted']+=1
 return result

def material(store,n,at):
 n=dict(n);r=store.db.execute('SELECT * FROM macro_article_texts WHERE news_id=? AND available_at<=? ORDER BY available_at DESC,id LIMIT 1',(n['id'],at)).fetchone()
 # A newly fetched page is evidence available NOW. Never backdate it to RSS publication.
 if r:n.update(body=r['body'],article_id=r['id'],content_basis='已提取正文（有截断）' if r['truncated'] else '已提取正文',content_available_at=r['available_at'],page_modified_at=r['modified_at'])
 else:n.update(content_basis='仅有标题/摘要',content_available_at=n['first_seen_at'])
 return n

SPEAKERS={'TRUMP':r'\bTrump\b|特朗普','FED':r'\bFOMC\b|Federal Reserve|美联储','IRAN':r'Iran|伊朗'}
SUBJECTS={'MONETARY':r'利率|降息|加息|美联储|货币政策|独立性|\brate[sd]?\b|\bFed\b|monetary|central.bank|Warsh|沃什',
 'IRAN':r'Iran|伊朗|Hormuz|霍尔木兹','TRADE':r'tariff|关税|export|出口'}
def related(store,n,at):
 text=n['title']+' '+n['body'];speakers={k for k,v in SPEAKERS.items() if re.search(v,text,re.I)};subjects={k for k,v in SUBJECTS.items() if re.search(v,text,re.I)}
 if not speakers or not subjects:return []
 cutoff=normalize_time((datetime.fromisoformat(n['published_at'])-timedelta(days=30)).isoformat())
 # Stable as-of baseline: only reports already collected when this event arrived.
 rows=store.db.execute("SELECT * FROM dynamic_news WHERE published_at>=? AND published_at<? AND first_seen_at<=? AND status NOT IN ('REVISED','DUPLICATE') ORDER BY published_at DESC,id LIMIT 4000",(cutoff,n['published_at'],min(at,n['first_seen_at']))).fetchall()
 result=[];seen=set();ranked=[]
 # Sharing a country name is insufficient. Prefer the same speaker's earlier
 # statements on the same topic over incidental references in unrelated news.
 speaker=next(k for k in ('TRUMP','FED','IRAN') if k in speakers)
 subject='IRAN' if 'IRAN' in subjects else 'MONETARY' if 'MONETARY' in subjects else 'TRADE'
 speech=re.compile(r'says?|said|told|warn|threat|表态|表示|称|告诉|说|发言|讲话|承诺',re.I)
 for r in rows:
  text=r['title']+' '+r['body']
  if not re.search(SPEAKERS[speaker],text,re.I) or not re.search(SUBJECTS[subject],text,re.I):continue
  score=4*bool(re.search(SPEAKERS[speaker],r['title'],re.I))+2*bool(re.search(SUBJECTS[subject],r['title'],re.I))+2*bool(speech.search(r['title']))
  score+=4*bool(re.match(r'^(?:美国总统|President\s+|US President\s+|U.S. President\s+)?(?:'+SPEAKERS[speaker]+')',r['title'].lstrip('【'),re.I))
  ranked.append((score,r))
 ranked.sort(key=lambda pair:(-pair[0],-datetime.fromisoformat(pair[1]['published_at']).timestamp(),pair[1]['id']))
 for _,r in ranked:
  key=re.sub(r'\W','',r['body']).casefold()
  if key in seen:continue
  seen.add(key);m=material(store,r,min(at,n['first_seen_at']));result.append({k:m[k] for k in ('id','source','url','published_at','first_seen_at','title','body','content_basis','content_available_at')});result[-1]['body']=m['body'][:1200]
  if len(result)>=3:break
 return result

def briefing(store,n,at):
 m=material(store,n,at);m['prior_reports']=related(store,n,at);return m

def summary(store,at):
 r=store.db.execute('SELECT count(*) FROM macro_article_texts WHERE available_at<=?',(at,)).fetchone()[0]
 return {'extracted':r,'note':'正文按实际提取时间留存；未提取成功的消息继续使用摘要。讲话直播和社媒直连尚未覆盖。'}

def frozen_prior(store,n,at,create=False):
 r=store.db.execute('SELECT payload_json FROM macro_statement_context WHERE news_id=? AND available_at<=?',(n['id'],at)).fetchone()
 if r:
  # A withdrawn/revised baseline must invalidate a cached review, not remain
  # evidence merely because an earlier packet had once included it.
  values=json.loads(r[0]);current={x['id']:x['status'] for x in store.db.execute('SELECT id,status FROM dynamic_news WHERE id IN ('+','.join('?' for _ in values)+')',[x['id'] for x in values])} if values else {}
  return [x for x in values if current.get(x['id']) not in (None,'REVISED','DUPLICATE')]
 if not create:return []
 result=related(store,n,at)
 with store.db:store.db.execute('INSERT OR IGNORE INTO macro_statement_context VALUES(?,?,?)',(n['id'],at,json.dumps(result,ensure_ascii=False,sort_keys=True)))
 return result
