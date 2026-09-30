"""Public SEC fundamentals and filing text for dynamically discovered US companies."""
import json
import re
from datetime import datetime, timedelta
from . import sources
from .storage import now, normalize_time, atomic_write

TAGS={'revenue':('RevenueFromContractWithCustomerExcludingAssessedTax','Revenues','SalesRevenueNet'),
      'profit':('NetIncomeLoss','ProfitLoss'),'assets':('Assets',),'liabilities':('Liabilities',),
      'cash':('CashAndCashEquivalentsAtCarryingValue',),'operating_cashflow':('NetCashProvidedByUsedInOperatingActivities',),
      'capex':('PaymentsToAcquirePropertyPlantAndEquipment',)}


def collect(store,symbol,at,fetch_fn=None):
    fetch=fetch_fn or sources.fetch
    if not re.fullmatch(r'US:[A-Z][A-Z0-9.-]{0,9}',symbol):raise ValueError('美股身份错误')
    cache=store.root/'cache/sec-tickers.json'
    if cache.exists() and datetime.fromtimestamp(cache.stat().st_mtime).date()==datetime.fromisoformat(at).date():raw=cache.read_bytes()
    else:
        raw=fetch('https://www.sec.gov/files/company_tickers.json',max_bytes=5000000);atomic_write(cache,raw)
    matches=[v for v in json.loads(raw).values() if v['ticker']==symbol[3:]]
    if len(matches)!=1:raise ValueError('SEC主体无法唯一确认')
    cik=int(matches[0]['cik_str']);url=f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json'
    raw=fetch(url,max_bytes=20000000);data=json.loads(raw)
    if data['cik']!=cik:raise ValueError('SEC主体不匹配')
    facts=data.get('facts',{}).get('us-gaap',{});metrics={}
    for key,tags in TAGS.items():
        observations=[]
        for tag in tags:
            for unit,values in facts.get(tag,{}).get('units',{}).items():
                if unit!='USD':continue
                observations += [{**v,'unit':unit,'tag':tag} for v in values if v.get('filed','9999')<=at[:10] and v.get('end','9999')<=at[:10] and v.get('form') in ('10-Q','10-K','20-F')]
            if observations:break
        # Never subtract quarterly and cumulative periods or different taxonomy tags.
        unique={}
        for v in sorted(observations,key=lambda v:v['filed']):unique[(v.get('start'),v['end'],v['unit'],v['tag'])]=v
        metrics[key]=sorted(unique.values(),key=lambda v:(v['end'],v['filed']),reverse=True)[:6]
    suburl=f'https://data.sec.gov/submissions/CIK{cik:010d}.json';subraw=fetch(suburl,max_bytes=5000000);sub=json.loads(subraw)
    if symbol[3:] not in sub.get('tickers',[]):raise ValueError('SEC证券映射变化')
    recent=sub['filings']['recent'];filings=[];texts=[]
    for i,form in enumerate(recent['form']):
        if form not in ('10-Q','10-K','20-F','8-K','6-K') or recent['filingDate'][i]>at[:10]:continue
        accession=recent['accessionNumber'][i].replace('-','');doc=recent['primaryDocument'][i]
        if not re.fullmatch(r'\d{18}',accession) or not re.fullmatch(r'[A-Za-z0-9_.-]+',doc):continue
        item={'form':form,'filed':recent['filingDate'][i],'accession':accession,'url':f'https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}'};filings.append(item)
        if len(filings)>=2:break
    for f in filings:
        body=fetch(f['url'],max_bytes=12000000);parser=sources.ArticleParser();parser.feed(body.decode('utf8'))
        text='\n'.join(parser.parts)
        if len(text)<200:raise ValueError('SEC正文未解析')
        published=f['filed']+'T00:00:00+00:00'
        doc_id,_=store.add_document(symbol=symbol,kind='company_report',title=sub['name']+' '+f['form'],source='SEC',url=f['url'],published_at=published,time_precision='date',pages=[(None,text)],raw_path=store.raw(body,'.html'),quality='html_text_layout_unverified',cloud_allowed=True)
        texts.append(doc_id)
    gaps=[k for k in ('revenue','profit','assets','cash','operating_cashflow') if not metrics[k]]
    if not filings:gaps.append('recent_filings')
    if metrics['revenue'] and (datetime.fromisoformat(at).date()-datetime.fromisoformat(metrics['revenue'][0]['end']).date()).days>180:gaps.append('stale_financial_period')
    packet={'symbol':symbol,'cik':cik,'name':sub['name'],'metrics':metrics,'filings':filings,'documents':texts,'gaps':gaps,'status':'PARTIAL' if gaps else 'READY','scope':'合并口径；原始期间与币种；未推算分部、单季和一致预期'}
    published=max((v['filed'] for vv in metrics.values() for v in vv),default=at[:10])+'T00:00:00+00:00'
    store.add_document(symbol=symbol,kind='us_financial_data',title=sub['name']+' SEC财务底稿',source='SEC',url=url,published_at=published,time_precision='date',pages=[(None,json.dumps(packet,ensure_ascii=False))],raw_path=store.raw(raw,'.json'),quality='structured_source',cloud_allowed=True)
    store.raw(subraw,'.json')
    return packet


def dossier(store,symbol,at,refresh=False):
    docs=[d for d in store.documents_as_of(at,symbol) if d['kind']=='us_financial_data']
    if refresh and (not docs or (datetime.fromisoformat(at)-datetime.fromisoformat(docs[0]['ready_at'])).total_seconds()>86400):
        try:return collect(store,symbol,at)
        except Exception as exc:
            store.record_attempt('sec_company',symbol,'FAILED',str(exc),at=at)
            return {'status':'PARTIAL','gaps':['SEC资料未取得'],'metrics':{},'documents':[]}
    if not docs:return {'status':'MISSING','gaps':['SEC公司底稿尚未取得'],'metrics':{},'documents':[]}
    from .storage import CHUNK_SIZE,CHUNK_STEP
    chunks=list(store.db.execute('SELECT text FROM chunks WHERE doc_id=? ORDER BY ordinal',(docs[0]['id'],)))
    body=''.join(r['text'][:CHUNK_STEP] if i<len(chunks)-1 else r['text'] for i,r in enumerate(chunks))
    return {**json.loads(body),'doc_id':docs[0]['id'],'ready_at':docs[0]['ready_at']}
