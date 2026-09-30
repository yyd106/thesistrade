"""Bounded public source collection, with explicit brief/fulltext distinction and item failures."""
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin,urlsplit
from . import sources,events
from .storage import now,normalize_time

FEEDS = (
    ('un_news','联合国新闻','https://news.un.org/feed/subscribe/zh/news/all/rss.xml','rss'),
    ('mofcom_news','商务部新闻','https://www.mofcom.gov.cn/xwfb/index.html','html'),
)


class BodyParser(HTMLParser):
    """Known article containers only. Never fall back to the whole navigation page."""
    def __init__(self):
        super().__init__();self.depth=0;self.active=None;self.parts=[];self.meta={};self.ignored=0

    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='meta':self.meta[(a.get('name') or a.get('property') or '').lower()]=a.get('content','')
        if tag in ('area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'):return
        self.depth+=1
        if tag in ('script','style','nav'):self.ignored+=1
        classes=a.get('class','').lower().split()
        if self.active is None and any(x in classes for x in ('vF_detail_content','vf_detail_content','art-con','trs_editor','field--name-field-text-column','views-field-field-news-story-lead')):
            self.active=self.depth

    def handle_startendtag(self,tag,attrs):
        if tag=='meta':self.handle_starttag(tag,attrs)

    def handle_endtag(self,tag):
        if tag in ('script','style','nav'):self.ignored=max(0,self.ignored-1)
        if self.active==self.depth:self.active=None
        if tag not in ('br','hr','img','meta','link','input','source'):self.depth=max(0,self.depth-1)

    def handle_data(self,text):
        if self.active is not None and not self.ignored and text.strip():self.parts.append(text.strip())


def parse_feed(raw,url,kind):
    if kind=='rss':
        if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():raise ValueError('新闻订阅包含不支持的实体声明')
        root=ET.fromstring(raw);rows=[]
        for item in root.findall('./channel/item')[:30]:
            title=item.findtext('title','').strip();link=item.findtext('link','').strip()
            description=sources.ArticleParser();description.feed(item.findtext('description',''))
            if not title or not link:continue
            sources.check_url(link)
            published=normalize_time(parsedate_to_datetime(item.findtext('pubDate','')).isoformat())
            rows.append({'title':title,'url':link,'published_at':published,'precision':'second',
                         'brief':title+'\n'+'\n'.join(description.parts)})
    else:
        page=sources.ArticleParser();page.feed(raw.decode('utf8'));rows=[];seen=set()
        for href,title in page.links:
            link=urljoin(url,href)
            if len(title)<10 or '/art/' not in link or not link.endswith('.html') or link in seen:continue
            if urlsplit(link).hostname!=urlsplit(url).hostname:continue
            sources.check_url(link);seen.add(link)
            rows.append({'title':title,'url':link,'published_at':now(),'precision':'date','brief':title})
    if not rows:raise ValueError('外部新闻目录没有解析到文章，需核查来源格式')
    return rows[:40]


def parse_article(raw,fallback):
    parser=BodyParser();parser.feed(raw.decode('utf8'))
    text='\n'.join(parser.parts)
    if len(text)<40:raise ValueError('未识别足够的文章正文，不采用页面导航充当正文')
    value=parser.meta.get('article:published_time') or parser.meta.get('pubdate')
    published=fallback['published_at'];precision=fallback['precision']
    if value:
        dt=datetime.fromisoformat(value.replace('Z','+00:00'))
        if dt.tzinfo is None:dt=dt.replace(tzinfo=sources.SH)
        published=normalize_time(dt.isoformat());precision='second' if len(value)>10 else 'date'
    return text,published,precision


def collect_article(store,run_id,row,source):
    """Collect or retry one identified article, clearing only its matching failure."""
    try:
        data=sources.fetch(row['url'],max_bytes=3000000)
        text,published,precision=parse_article(data,row)
        store.add_document(symbol='MARKET',kind='news',title=row['title'],source=source,url=row['url'],
            published_at=published,time_precision=precision,pages=[(None,text)],raw_path=store.raw(data,'.html'),
            quality='article_body',cloud_allowed=True)
        store.record_attempt('external_news_article','MARKET','OK',resource_key=row['url'],title=row['title'],run_id=run_id)
        return True
    except Exception as exc:
        store.record_attempt('external_news_article','MARKET','FAILED',str(exc),resource_key=row['url'],title=row['title'],run_id=run_id)
        return False


def collect(store,run_id,config):
    active_topics={topic for item in config['watchlist'] for topic in events.profile(config,item['symbol'])}
    for key,name,url,kind in FEEDS:
        try:
            raw=sources.fetch(url,max_bytes=3000000);raw_path=store.raw(raw,'.xml' if kind=='rss' else '.html')
            rows=parse_feed(raw,url,kind)
            relevant=[r for r in rows if active_topics.intersection(events.classify(r['brief']))]
            for r in relevant:
                store.add_document(symbol='MARKET',kind='news_brief',title=r['title'],source=key,url=r['url'],
                    published_at=r['published_at'],time_precision=r['precision'],pages=[(None,r['brief'])],raw_path=raw_path,
                    quality='source_brief_only',cloud_allowed=True)
            store.record_attempt('external_news_list','MARKET','OK',resource_key=url,title=name,run_id=run_id)
        except Exception as exc:
            store.record_attempt('external_news_list','MARKET','FAILED',str(exc),resource_key=url,title=name,run_id=run_id)
            store.check(run_id,key,'MARKET','FAILED',str(exc),track=False)
            continue
        # New/missed articles first; rotate old articles for content revision checks.
        def last_check(row):
            last=store.db.execute("SELECT checked_at FROM data_attempts WHERE source='external_news_article' AND resource_key=? ORDER BY checked_at DESC,id DESC LIMIT 1",(row['url'],)).fetchone()
            return last[0] if last else ''
        def relevance_priority(row):
            matches=[events.relevance(config,item['symbol'],row['title'],row['brief']) for item in config['watchlist']]
            return 0 if 'COMPANY' in matches else 1 if 'BUSINESS' in matches else 2
        selected=sorted(relevant,key=lambda r:(relevance_priority(r),last_check(r)))[:config.get('external_news_articles_per_source',3)]
        completed=0
        for r in selected:
            completed+=int(collect_article(store,run_id,r,key))
        store.check(run_id,key,'MARKET','PARTIAL',f'{name}：本页{len(rows)}条，主题筛选{len(relevant)}条，本轮核验{completed}篇正文；仅覆盖选定公开来源，不代表全网新闻',track=False)
