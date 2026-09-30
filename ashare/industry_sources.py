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
    from .cloud_runtime import value,put
    cursor_key='industry_catalog:'+domain+':'+term
    cursor=value(store,cursor_key) or {'begin':str(begin),'end':str(end),'page':1}
    directory_ok=False
    try:
        form={'pageNum':cursor['page'],'pageSize':limits['page_size'],'column':'szse','tabName':'fulltext','stock':'','searchkey':term,'secid':'','plate':'','category':'','trade':'','seDate':cursor['begin']+'~'+cursor['end'],'sortName':'time','sortType':'desc','isHLtitle':'false'}
        raw=fetch('https://www.cninfo.com.cn/new/hisAnnouncement/query',form=form,max_bytes=3000000)
        store.raw(raw,'.json');data=json.loads(raw)
        if not isinstance(data.get('announcements'),(list,type(None))) or 'hasMore' not in data:raise ValueError('公告目录暂时无法读取')
        # Advance a page only after its entire bounded catalog is durably queued.
        for a in (data['announcements'] or [])[:limits['page_size']]:
            code=a.get('secCode','')
            if not re.fullmatch(r'(60|68|00|30)\d{4}',code):continue
            symbol=('sh' if code.startswith('6') else 'sz')+code
            url=urljoin('https://static.cninfo.com.cn/',a['adjunctUrl']);sources.check_url(url)
            title=html.unescape(re.sub('<[^>]+>','',a['announcementTitle']))
            published=normalize_time(datetime.fromtimestamp(a['announcementTime']/1000,timezone.utc).isoformat())
            store.add_document(symbol=symbol,kind='announcement_metadata',title=title,source='cninfo',url=url,published_at=published,time_precision='date',pages=[(None,title)],raw_path=store.raw(json.dumps(a,ensure_ascii=False).encode(),'.json'),quality='metadata_only',cloud_allowed=True)
            with store.db:
                store.db.execute("INSERT OR IGNORE INTO industry_source_queue(url,domain,symbol,title,published_at,discovered_at,status) VALUES(?,?,?,?,?,?,'PENDING')",(url,domain,symbol,title,published,start))
        with store.db:put(store,cursor_key,{**cursor,'page':cursor['page']+1} if data['hasMore'] else None)
        directory_ok=True
    except Exception as exc:errors.append('公告目录：'+str(exc)[:180])
    # Existing successful bodies do not spend the new-body allowance. Failed
    # items remain queued and move behind untried items so one bad PDF cannot starve them.
    pending=list(store.db.execute("SELECT * FROM industry_source_queue WHERE domain=? AND status!='DONE' ORDER BY attempted_at IS NOT NULL,attempted_at,discovered_at,url",(domain,)))
    attempts=0
    for item in pending:
        if time.monotonic()>=deadline:break
        existing=store.db.execute("SELECT 1 FROM documents WHERE url=? AND kind='company_report' AND extraction_quality NOT IN ('metadata_only','ocr_required')",(item['url'],)).fetchone()
        if existing:
            with store.db:store.db.execute("UPDATE industry_source_queue SET status='DONE',completed_at=?,error='' WHERE url=?",(now(),item['url']))
            continue
        if attempts>=limits['pdf_limit']:break
        attempts+=1
        try:
            body=fetch(item['url'],max_bytes=12000000);path=store.raw(body,'.pdf');pages=sources.pdf_pages(body)
            if not pages or not any(text.strip() for _,text in pages):raise ValueError('正文暂未识别，需要重新取得或核对')
            store.add_document(symbol=item['symbol'],kind='company_report',title=item['title'],source='cninfo',url=item['url'],published_at=item['published_at'],time_precision='date',pages=pages,raw_path=path,quality='pdf_text_layout_unverified',cloud_allowed=True)
            with store.db:store.db.execute("UPDATE industry_source_queue SET status='DONE',attempted_at=?,completed_at=?,error='' WHERE url=?",(now(),now(),item['url']))
            count+=1
        except Exception as exc:
            errors.append(item['title']+'：'+str(exc)[:140])
            with store.db:store.db.execute("UPDATE industry_source_queue SET status='PENDING',attempted_at=?,error=? WHERE url=?",(now(),str(exc)[:300],item['url']))
    # A separate bounded revision check cannot consume the new-body allowance.
    cutoff=normalize_time((datetime.fromisoformat(start)-timedelta(hours=config.get('document_recheck_hours',24))).isoformat())
    revisions=store.db.execute("SELECT * FROM industry_source_queue WHERE domain=? AND status='DONE' AND coalesce(attempted_at,completed_at)<? ORDER BY coalesce(attempted_at,completed_at) LIMIT 1",(domain,cutoff)).fetchall()
    for item in revisions:
        if time.monotonic()>=deadline:break
        try:
            body=fetch(item['url'],max_bytes=12000000);path=store.raw(body,'.pdf')
            if not store.db.execute("SELECT 1 FROM documents WHERE url=? AND raw_path=? AND kind='company_report'",(item['url'],path)).fetchone():
                pages=sources.pdf_pages(body)
                if not pages or not any(text.strip() for _,text in pages):raise ValueError('修订后的正文尚不能读取')
                store.add_document(symbol=item['symbol'],kind='company_report',title=item['title'],source='cninfo',url=item['url'],published_at=item['published_at'],time_precision='date',pages=pages,raw_path=path,quality='pdf_text_layout_unverified',cloud_allowed=True)
            with store.db:store.db.execute("UPDATE industry_source_queue SET attempted_at=?,error='' WHERE url=?",(now(),item['url']))
        except Exception as exc:errors.append('正文修订检查：'+str(exc)[:140])
    remaining=store.db.execute("SELECT count(*) FROM industry_source_queue WHERE domain=? AND status!='DONE'",(domain,)).fetchone()[0]
    coverage(store,cycle,domain,'cninfo_industry',start,'PARTIAL' if directory_ok or count else 'FAILED',
             cursor['begin']+'至'+cursor['end']+'；关键词'+term+'，目录第'+str(cursor['page'])+'页；本次取得'+str(count)+'份正文，待补'+str(remaining)+'份；'+('；'.join(errors) or '继续按资料取得进度补齐，不代表完整覆盖'))
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
