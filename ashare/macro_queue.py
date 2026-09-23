"""Bounded news triage with conservative syndication grouping and a discovery reserve."""
from __future__ import annotations
import json,re
from collections import Counter,defaultdict
from datetime import datetime,timedelta
from .storage import normalize_time
from .observation_pool import policy
from .news_catalog import source_priority, family
from .news_triage import context,input_hash,policy as news_policy

CATALYST=re.compile(r'关税|出口.{0,4}(?:管制|限制|禁)|战争|袭击|制裁|tariff|sanction|export control|加息|降息|rate cut|rate hike|OPEC|\bwar\b|attack|blockade|shutdown|shut down|outage|capital expenditure|clinical trial|FDA approv|断供|停产|停运|封锁|产能|资本开支',re.I)
SHOCK=re.compile(r'shutdown|shut down|blockade|export (?:ban|control)|supply disruption|suspend.{0,20}(?:production|export)|cuts?.{0,20}(?:rates?|output)|关税.{0,8}(?:实施|提高)|断供|停产|停运|封锁|出口管制|降息|加息',re.I)
EXPECTATION_CATALYST=re.compile(r'ultimatum|deadline|central.bank independence|threaten.{0,70}(?:military|destroy|attack)|最后通牒|最后期限|央行独立性|威胁.{0,20}(?:军事|摧毁|袭击)',re.I)
COMMENTARY=re.compile(r'观点|评论|展望|预测|呼吁|敦促|当务之急|盘前|收盘|复盘|forecast|calls for|urges?\b|edges (?:higher|lower)|stocks (?:rise|fall)',re.I)
NEGATION=re.compile(r'不|未|否认|取消|暂停|撤销|停止|not\b|no\b|cancel\w*|deny|denied|halt\w*',re.I)
ACTION=re.compile(r'批准|提议|计划|实施|生效|确认|提高|降低|上涨|下跌|approve\w*|propos\w*|implement\w*|confirm\w*|rais\w*|cut\w*|hik\w*|withdraw\w*',re.I)
ENTITIES=re.compile(r'中国|美国|日本|欧洲|俄罗斯|伊朗|以色列|英国|法国|德国|加拿大|澳大利亚|美联储|欧洲央行|日本央行|[A-Z][a-z]{2,}|[A-Z]{2,}')
def normalized(text):
 text=re.sub(r'(?:More coverage\.|更多报道[。.]?|点击查看详情[。.]?)\s*$','',text,flags=re.I)
 return re.sub(r'[\W_]+','',text).lower()
def shingles(text):return {text[i:i+4] for i in range(max(0,len(text)-3))}
def overlap(a,b):return len(a&b)/max(1,len(a|b))
def same_event(a,b):
 # Numeric or negative/policy-status changes must receive their own assessment.
 if any(a[k]!=b[k] for k in ('numbers','negative','actions','entities')):return False
 if abs((datetime.fromisoformat(a['n']['published_at'])-datetime.fromisoformat(b['n']['published_at'])).total_seconds())>129600:return False
 if a['body']==b['body']:return True
 return len(a['body'])>=60 and len(b['body'])>=60 and overlap(a['title_bits'],b['title_bits'])>=.82 and overlap(a['body_bits'],b['body_bits'])>=.92

def prepare(store,start,end,at,config=None,candidate_limit=4):
 from .macro import topics
 from .observation import view
 limit=policy(config)['news_queue_limit'];end=normalize_time(end);at=normalize_time(at)
 screening_policy=news_policy(config);trials={r['input_hash']:dict(r) for r in store.db.execute('SELECT * FROM macro_triage_attempts')}
 cutoff=normalize_time((datetime.fromisoformat(end)-timedelta(hours=48)).isoformat())
 # Clock boundaries and article revisions are kept independent of the execution queue.
 with store.db:
  store.db.execute("UPDATE macro_news SET status='EXPIRED' WHERE status IN ('PENDING','FAILED','DORMANT','WATCHING','FILTERED','SCREENING_WAIT','FILTER_FAILED') AND news_id IN (SELECT id FROM dynamic_news WHERE published_at<?)",(cutoff,))
  store.db.execute("UPDATE macro_news SET status='EXPIRED' WHERE status IN ('PENDING','FAILED','DORMANT','WATCHING','FILTERED','SCREENING_WAIT','FILTER_FAILED') AND news_id IN (SELECT id FROM dynamic_news WHERE status IN ('DUPLICATE','REVISED'))")
  store.db.execute("UPDATE macro_news SET status='FAILED' WHERE status IN ('PENDING','DORMANT') AND attempts>=3")
 rows=[dict(r) for r in store.db.execute("SELECT n.*,m.status macro_status,m.attempts macro_attempts FROM dynamic_news n LEFT JOIN macro_news m ON m.news_id=n.id WHERE n.status NOT IN ('DUPLICATE','REVISED') AND n.published_at>=? AND n.published_at<=? AND n.first_seen_at<=? ORDER BY n.published_at DESC LIMIT 12000",(cutoff,end,at))]
 old_events={r['news_id']:json.loads(r['payload_json']) for r in store.db.execute('SELECT news_id,payload_json FROM macro_events')}
 focus=[i for i in view(store,at,config)['items'] if i['pool_tier']=='FOCUS']
 focus_themes={l['theme'] for i in focus for l in i['links'] if l['status']=='TRACKING' and l.get('review_state')=='CURRENT' and l.get('materiality',{}).get('admitted')}
 official={n['source'] for n in rows if family(n['source'])=='PRIMARY'};eligible=[];updates=[]
 for n in rows:
  classified=topics(n)
  if not classified:
   if n['macro_status']!='DONE':updates.append((n['id'],'IGNORED'))
   continue
  screening=context(store,n,at)
  if screening and screening['decision']!='DEEP':
   if n['macro_status']!='DONE':updates.append((n['id'],'FILTERED' if screening['decision']=='BACKGROUND' else 'WATCHING'))
   continue
  tried=trials.get(input_hash(n,store,at)) if not screening else None
  if tried and (tried['attempts']>=screening_policy['max_attempts'] or (datetime.fromisoformat(at)-datetime.fromisoformat(tried['last_at'])).total_seconds()<screening_policy['retry_hours']*3600):
   if n['macro_status']!='DONE':updates.append((n['id'],'FILTER_FAILED' if tried['attempts']>=screening_policy['max_attempts'] else 'SCREENING_WAIT'))
   continue
  prior=old_events.get(n['id']);legacy=n['macro_status']=='DONE' and prior and any('logic_chain' not in i for i in prior['impacts'])
  if legacy:
   with store.db:store.db.execute("UPDATE macro_news SET status='PENDING',attempts=0 WHERE news_id=?",(n['id'],))
   n['macro_status']='PENDING';n['macro_attempts']=0
  age=max(0,(datetime.fromisoformat(end)-datetime.fromisoformat(n['published_at'])).total_seconds()/3600)
  related=bool(set(classified)&focus_themes)
  score=round(20*(1-age/48))+source_priority(n['source'])+12*bool(CATALYST.search(n['title']+' '+n['body']))+25*bool(SHOCK.search(n['title']))-20*bool(COMMENTARY.search(n['title']))+10*related
  score+=12*bool(EXPECTATION_CATALYST.search(n['title']+' '+n['body']))
  if screening and screening['decision']=='DEEP':score+=50
  if legacy:score+=5
  body=normalized(n['body']);title=normalized(n['title'])
  eligible.append({'n':n,'score':score,'related':related,'body':body,'title_bits':shingles(title),'body_bits':shingles(body),
   'actions':tuple(ACTION.findall(n['body'].lower())),'entities':tuple(ENTITIES.findall(re.sub(r'More coverage\.\s*$','',n['body']))),
   'numbers':tuple(re.findall(r'\d+(?:[.,]\d+)*%?',n['body'])),'negative':tuple(NEGATION.findall(n['body'].lower()))})
 # Prefer an already studied representative; copied stories do not restart research.
 eligible.sort(key=lambda x:(x['n']['macro_status']!='DONE',x['n']['source'] not in official,x['n']['published_at'],x['n']['id']))
 representatives=[];index=defaultdict(list);exact={};mapping=[]
 for entry in eligible:
  counts=Counter(j for bit in entry['title_bits'] for j in index[bit]);choices=[j for j,n in counts.most_common(8)]
  if entry['body'] in exact:choices.insert(0,exact[entry['body']])
  match=next((j for j in dict.fromkeys(choices) if same_event(entry,representatives[j])),None)
  if match is None:
   match=len(representatives);representatives.append(entry);exact[entry['body']]=match
   for bit in entry['title_bits']:index[bit].append(match)
  rep=representatives[match];mapping.append((entry,rep))
  if entry is not rep and entry['n']['macro_status']!='DONE':updates.append((entry['n']['id'],'MERGED'))
 candidates=[e for e in representatives if e['n']['macro_status']!='DONE' and (e['n']['macro_attempts'] or 0)<3]
 candidates.sort(key=lambda e:(-e['score'],e['n']['published_at'],e['n']['id']))
 diverse_sources=[];seen_sources=set()
 for e in candidates:
  if e['n']['source'] not in seen_sources and len(diverse_sources)<min(16,limit//2):
   diverse_sources.append(e);seen_sources.add(e['n']['source'])
 discovery=diverse_sources+[e for e in candidates if not e['related'] and e not in diverse_sources][:max(0,min(16,limit//2)-len(diverse_sources))]
 queue=sorted(discovery+[e for e in candidates if e not in discovery][:limit-len(discovery)],key=lambda e:(-e['score'],e['n']['published_at'],e['n']['id']))
 for e in candidates:updates.append((e['n']['id'],'PENDING' if e in queue else 'DORMANT'))
 with store.db:
  store.db.executemany('INSERT INTO macro_news(news_id,status) VALUES(?,?) ON CONFLICT(news_id) DO UPDATE SET status=excluded.status',updates)
  store.db.executemany('INSERT OR REPLACE INTO macro_news_queue VALUES(?,?,?,?,?)',[(e['n']['id'],r['n']['id'],e['score'],'相同事件报道合并' if e is not r else '重点标的相关新证据' if e['related'] else '新事件候选',at) for e,r in mapping])
 if candidate_limit>4:return [e['n'] for e in queue[:candidate_limit]]
 # At most two focus-related slots before reserving capacity for new discoveries.
 chosen=[e for e in queue if e['related']][:2]
 def append_diverse(values):
  sources=Counter(e['n']['source'] for e in chosen)
  for e in values:
   if len(chosen)>=4:return
   if e in chosen or sources[e['n']['source']]>=3:continue
   chosen.append(e);sources[e['n']['source']]+=1
 append_diverse([e for e in queue if not e['related']]);append_diverse(queue)
 for e in queue:
  if len(chosen)>=4:break
  if e not in chosen:chosen.append(e)
 return [e['n'] for e in chosen]
