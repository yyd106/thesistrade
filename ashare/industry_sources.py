"""Bounded official discovery outside the fixed stock list, with explicit coverage gaps."""
import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlencode
from . import sources
from .storage import now, normalize_time
from .industry import DOMAINS


def coverage(store,cycle,domain,source,start,status,detail):
    with store.db:
        store.db.execute('INSERT INTO industry_coverage(cycle_id,domain,source,started_at,finished_at,status,detail) VALUES(?,?,?,?,?,?,?)',(cycle,domain,source,start,now(),status,detail[:1200]))


def collect(store,config,cycle,domain,deadline,fetch_fn=None):
    from .industry_research import policy
    limits=policy(config)
    fetch=fetch_fn or sources.fetch;start=now();end=datetime.fromisoformat(start).date();begin=end-timedelta(days=limits['lookback_days'])
    terms=DOMAINS[domain]['terms'];term=terms[end.toordinal()%len(terms)];count=0;errors=[]
    try:
        # One page is intentionally not represented as full-market coverage.
        form={'pageNum':1,'pageSize':limits['page_size'],'column':'szse','tabName':'fulltext','stock':'','searchkey':term,'secid':'','plate':'','category':'','trade':'','seDate':f'{begin}~{end}','sortName':'time','sortType':'desc','isHLtitle':'false'}
        raw=fetch('https://www.cninfo.com.cn/new/hisAnnouncement/query',form=form,max_bytes=3000000)
        store.raw(raw,'.json');data=json.loads(raw)
        if not isinstance(data.get('announcements'),(list,type(None))) or 'hasMore' not in data:raise ValueError('产业公告目录结构变化')
        downloaded=0
        for a in (data['announcements'] or [])[:limits['page_size']]:
            if time.monotonic()>=deadline:errors.append('本轮采集预算结束');break
            code=a.get('secCode','')
            if not re.fullmatch(r'(60|68|00|30)\d{4}',code):continue
            symbol=('sh' if code.startswith('6') else 'sz')+code
            url=urljoin('https://static.cninfo.com.cn/',a['adjunctUrl']);sources.check_url(url)
            title=html.unescape(re.sub('<[^>]+>','',a['announcementTitle']))
            published=normalize_time(datetime.fromtimestamp(a['announcementTime']/1000,timezone.utc).isoformat())
            store.add_document(symbol=symbol,kind='announcement_metadata',title=title,source='cninfo',url=url,published_at=published,time_precision='date',pages=[(None,title)],raw_path=store.raw(json.dumps(a,ensure_ascii=False).encode(),'.json'),quality='metadata_only',cloud_allowed=True)
            if downloaded>=limits['pdf_limit']:continue
            downloaded+=1
            try:
                body=fetch(url,max_bytes=12000000);path=store.raw(body,'.pdf')
                if store.db.execute("SELECT 1 FROM documents WHERE url=? AND raw_path=? AND kind='company_report'",(url,path)).fetchone():continue
                store.add_document(symbol=symbol,kind='company_report',title=title,source='cninfo',url=url,published_at=published,time_precision='date',pages=sources.pdf_pages(body),raw_path=path,quality='pdf_text_layout_unverified',cloud_allowed=True);count+=1
            except Exception as exc:errors.append(title+': '+str(exc)[:140])
        coverage(store,cycle,domain,'cninfo_industry',start,'PARTIAL',f"{begin}至{end}；关键词{term}，最多{limits['page_size']}条目录、{limits['pdf_limit']}份正文，本次新增{count}份；"+('存在后续页；' if data['hasMore'] else '')+'；'.join(errors))
    except Exception as exc:coverage(store,cycle,domain,'cninfo_industry',start,'FAILED',str(exc))
    if time.monotonic()>=deadline:return
    # Procurement pages are public sources. Failure/captcha is a coverage gap,
    # never evidence that the industry has no procurement activity.
    start=now()
    from .cloud_runtime import value,put
    retry=value(store,'ccgp_retry_after')
    if retry and start<retry:
        coverage(store,cycle,domain,'ccgp',start,'FAILED','采购网限制访问，等待冷却重试；公司采购公告仍可作为证据');return
    try:
        url='https://search.ccgp.gov.cn/bxsearch?'+urlencode({'searchtype':1,'page_index':1,'kw':term,'start_time':begin.strftime('%Y:%m:%d'),'end_time':end.strftime('%Y:%m:%d'),'timeType':6,'dbselect':'bidx','bidType':0,'pinMu':0})
        raw=fetch(url,max_bytes=3000000);store.raw(raw,'.html')
        if '访问过于频繁' in raw.decode('utf8',errors='replace'):
            with store.db:put(store,'ccgp_retry_after',normalize_time((datetime.fromisoformat(start)+timedelta(hours=6)).isoformat()))
            raise ValueError('采购网限制访问，已设置6小时冷却；不绕过来源限制')
        parser=sources.ArticleParser();parser.feed(raw.decode('utf8'))
        links=[]
        for href,title in parser.links:
            target=urljoin(url,href).replace('http://www.ccgp.gov.cn/','https://www.ccgp.gov.cn/')
            if target.startswith('https://www.ccgp.gov.cn/cggg/') and len(title)>8 and target not in [x[0] for x in links]:links.append((target,title))
        if not links:raise ValueError('未识别采购目录或来源限制访问')
        for target,title in links[:2]:
            if time.monotonic()>=deadline:break
            from .external_news import BodyParser
            body=fetch(target,max_bytes=3000000);parser=BodyParser();parser.feed(body.decode('utf8'))
            text='\n'.join(parser.parts)
            date=re.search(r'/t(\d{8})_',target)
            if not date or len(text)<80:raise ValueError('采购正文或公开日期未核验')
            published=normalize_time(datetime.strptime(date[1],'%Y%m%d').replace(tzinfo=sources.SH).isoformat())
            store.add_document(symbol='MARKET',kind='procurement',title=title,source='ccgp',url=target,published_at=published,time_precision='date',pages=[(None,text)],raw_path=store.raw(body,'.html'),quality='html_text',cloud_allowed=True)
        coverage(store,cycle,domain,'ccgp',start,'PARTIAL',f'{begin}至{end}关键词{term}；一页目录、最多2份正文；非完整采购覆盖')
    except Exception as exc:coverage(store,cycle,domain,'ccgp',start,'FAILED',str(exc))
