"""Versioned public financial statements, with explicit units and period arithmetic.

The provider's current restated history is useful for comparison, never a backtest
of what was known in a past quarter. Raw responses and readiness are retained.
"""
import json
import math
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from .storage import now,digest
from . import sources

REPORTS = {
    'main':'RPT_F10_FINANCE_MAINFINADATA',
    'balance':'RPT_F10_FINANCE_GBALANCE',
    'cash':'RPT_DMSK_FN_CASHFLOW',
}
REPORT_LABELS = {'main':'财务指标','balance':'资产负债表','cash':'现金流量表'}
# Values are yuan, shares or percent as declared below; no provider *_RATIO is
# silently interpreted as year-on-year growth (cashflow ratios aren't growth).
FIELDS = {
    'main':{'revenue':'TOTALOPERATEREVE','net_profit':'PARENTNETPROFIT',
        'core_profit':'KCFJCXSYJLR','gross_margin_pct':'XSMLL','roe_pct':'ROEJQ',
        'bps':'BPS','shares':'TOTAL_SHARE','rd_expense':'RDEXPEND'},
    'balance':{'assets':'TOTAL_ASSETS','liabilities':'TOTAL_LIABILITIES',
        'equity':'TOTAL_EQUITY','parent_equity':'TOTAL_PARENT_EQUITY','cash':'MONETARYFUNDS',
        'inventory':'INVENTORY','receivables':'ACCOUNTS_RECE','contract_liabilities':'CONTRACT_LIAB',
        'short_debt':'SHORT_LOAN','long_debt':'LONG_LOAN','goodwill':'GOODWILL'},
    'cash':{'operating_cashflow':'NETCASH_OPERATE','capex':'CONSTRUCT_LONG_ASSET'},
}
LABELS = {'revenue':'营业总收入','net_profit':'归母净利润','core_profit':'扣非归母净利润',
    'gross_margin_pct':'毛利率','roe_pct':'加权净资产收益率','bps':'每股净资产','shares':'报告期总股本',
    'rd_expense':'研发投入','assets':'总资产','liabilities':'总负债','equity':'所有者权益',
    'parent_equity':'归母权益','cash':'货币资金','inventory':'存货','receivables':'应收账款',
    'contract_liabilities':'合同负债','short_debt':'短期借款','long_debt':'长期借款','goodwill':'商誉',
    'operating_cashflow':'经营现金流净额','capex':'购建长期资产支付现金'}
FLOWS = ('revenue','net_profit','core_profit','operating_cashflow','capex','rd_expense')
CORE = ('revenue','net_profit','core_profit','operating_cashflow','cash','assets','liabilities','equity')


def number(value):
    if value is None or value in ('','-','--'):return None
    if isinstance(value,bool):raise ValueError('财务数字为布尔值')
    try:n=Decimal(str(value))
    except InvalidOperation:raise ValueError('财务数字格式无效')
    if not n.is_finite():raise ValueError('财务数字不是有限数值')
    return int(n) if n==n.to_integral_value() else float(n)


def api_url(symbol, report):
    sec=symbol[2:]+'.'+symbol[:2].upper()
    return 'https://datacenter-web.eastmoney.com/api/data/v1/get?'+urlencode({
        'reportName':REPORTS[report],'columns':'ALL','filter':f'(SECUCODE="{sec}")',
        'pageSize':16,'pageNumber':1,'sortColumns':'REPORT_DATE','sortTypes':-1,'source':'WEB','client':'WEB'})


def parse_response(raw,symbol,group,as_of):
    obj=json.loads(raw)
    if obj.get('success') is not True or not isinstance((obj.get('result') or {}).get('data'),list):
        raise ValueError('公开财务接口没有返回有效报表')
    out={};cutoff=sources.SH
    day=datetime.fromisoformat(as_of).astimezone(cutoff).date().isoformat()
    for row in obj['result']['data']:
        if row.get('SECUCODE')!=symbol[2:]+'.'+symbol[:2].upper():raise ValueError('财务证券标识不匹配')
        if group=='main' and row.get('CURRENCY')!='CNY':raise ValueError('财务币种不是人民币')
        if row.get('REPORT_TYPE_CODE') not in (None,'001'):raise ValueError('不是合并报表口径')
        period=str(row.get('REPORT_DATE',''))[:10];notice=str(row.get('NOTICE_DATE',''))[:10]
        updated=str(row.get('UPDATE_DATE') or notice)[:10]
        for value in (period,notice,updated):date.fromisoformat(value)
        if period[5:] not in ('03-31','06-30','09-30','12-31'):raise ValueError('不是有效季度报告期')
        if notice<period:raise ValueError('财务披露时间早于报告期结束')
        if max(period,notice,updated)>day:continue
        vals={key:number(row.get(field)) for key,field in FIELDS[group].items()}
        entry={'period':period,'notice_date':notice,'updated_date':updated,'values':vals}
        if period in out and out[period]!=entry:raise ValueError('同一报告期存在冲突记录')
        out[period]=entry
    if not out:raise ValueError('没有截至当前已披露的财务记录')
    return out


def previous_period(period):
    y,m,_=map(int,period.split('-'))
    return f'{y-1}-12-31' if m==3 else f'{y}-{m-3:02d}-'+('31' if m==6 else '30')


def quarterly(periods,period,key):
    row=periods.get(period,{}).get('values',{});value=row.get(key)
    if value is None:return None
    if period.endswith('03-31'):return value
    prev=periods.get(previous_period(period),{}).get('values',{}).get(key)
    return value-prev if prev is not None else None


def trailing(periods,period,key):
    values=[]
    for _ in range(4):
        values.append(quarterly(periods,period,key));period=previous_period(period)
    return sum(values) if all(v is not None for v in values) else None


def yoy(current,prior):
    # Negative/zero bases are presented as values, not a misleading growth percentage.
    return round((current/prior-1)*100,2) if current is not None and prior is not None and prior>0 else None


def build_payload(symbol,groups,refs,stamp):
    periods={}
    for group,rows in groups.items():
        for period,row in rows.items():
            target=periods.setdefault(period,{'period':period,'values':{},'sources':{}})
            target['values'].update(row['values'])
            target['sources'][group]={'notice_date':row['notice_date'],'updated_date':row['updated_date'],
                **refs[group]}
    if not periods:raise ValueError('没有财务底稿记录')
    for row in periods.values():
        v=row['values'];a,l,e=(v.get(k) for k in ('assets','liabilities','equity'))
        if all(n is not None for n in (a,l,e)) and (a<=0 or abs(a-l-e)>max(1,a*0.000001)):
            raise ValueError('资产负债表勾稽关系不成立')
    latest=max(periods);v=periods[latest]['values'];gaps=[]
    for group in REPORTS:
        if group not in periods[latest]['sources']:gaps.append('最新报告期缺少'+REPORT_LABELS[group])
    gaps += ['缺少'+LABELS[k] for k in CORE if v.get(k) is None]
    age=(datetime.fromisoformat(stamp).astimezone(sources.SH).date()-date.fromisoformat(latest)).days
    if age>190:gaps.append('最新财务报告期距今超过190天')
    return {'symbol':symbol,'currency':'CNY','scope':'合并报表；利润为归母口径',
        'unit':'金额为元，股本为股，每股净资产为元/股，比例为百分数；利润与现金流为年初至报告期累计，余额为期末值',
        'provider':'东方财富公开财务整理','validation':'证券、币种、期间、有限数值及资产负债勾稽核验；未经逐项人工核对',
        'status':'PARTIAL' if gaps else 'READY','gaps':gaps,'latest_period':latest,
        'periods':[periods[k] for k in sorted(periods,reverse=True)],
        'field_sources':{k:{'report':REPORTS[g],'field':f} for g,m in FIELDS.items() for k,f in m.items()},
        'limitations':['当前来源可能包含历史重述，不能当作历史时点已知数据。','货币资金不等于可自由使用现金，资金受限和融资需求仍需核对公告。']}


def amount(value,key):
    if value is None:return '未取得'
    if key.endswith('_pct'):return f'{value:.2f}%'
    if key=='bps':return f'{value:.4f}元/股'
    if key=='shares':return f'{value/1e8:.4f}亿股'
    return f'{value/1e8:.4f}亿元'


def document_pages(payload):
    pages=[]
    for n,row in enumerate(payload['periods'],1):
        lines=[f"{payload['symbol']} {row['period']} 合并财务底稿（利润、现金流为年初累计；资产负债为期末余额）"]
        lines += [f'{LABELS[k]}：{amount(v,k)}' for k,v in row['values'].items()]
        notices=sorted({r['notice_date'] for r in row['sources'].values()})
        lines.append('来源：东方财富公开财务整理；披露日期：'+'、'.join(notices))
        pages.append((n,'\n'.join(lines)))
    pages.append((len(pages)+1,'底稿状态：'+payload['status']+'；缺口：'+'、'.join(payload['gaps'])+
        '\n来源及口径版本：'+digest(json.dumps(payload,ensure_ascii=False,sort_keys=True))))
    return pages


def collect(store,run_id,symbol):
    groups={};refs={};stamp=now();errors=[]
    for group in REPORTS:
        url=api_url(symbol,group)
        try:
            raw=sources.fetch(url,max_bytes=3000000);path=store.raw(raw,'.json')
            groups[group]=parse_response(raw,symbol,group,stamp)
            refs[group]={'url':url,'raw_path':path}
            store.record_attempt('financial_statement',symbol,'OK',resource_key=group,title=REPORT_LABELS[group],run_id=run_id)
        except Exception as exc:
            errors.append(group)
            store.record_attempt('financial_statement',symbol,'FAILED',str(exc),resource_key=group,title=REPORT_LABELS[group],run_id=run_id)
    if not groups:
        store.check(run_id,'financials',symbol,'FAILED','三类财务报表均未取得');return
    payload=build_payload(symbol,groups,refs,stamp)
    payload['gaps']+=['本轮未取得'+REPORT_LABELS[g] for g in errors]
    if errors:payload['status']='PARTIAL'
    raw_path=store.raw(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode(),'.json')
    published=max(r['notice_date'] for row in payload['periods'] for r in row['sources'].values())+'T00:00:00+08:00'
    did,_=store.add_document(symbol=symbol,kind='financial_data',title='公司财务底稿',source='eastmoney_public',
        url='https://data.eastmoney.com/bbsj/'+symbol[2:]+'.html',published_at=published,time_precision='date',
        pages=document_pages(payload),raw_path=raw_path,quality='typed_public_financials',cloud_allowed=True)
    # A typed version is immutable. The document body includes all displayed values.
    with store.db:store.db.execute('INSERT OR IGNORE INTO fundamental_records VALUES(?,?)',(did,json.dumps(payload,ensure_ascii=False,sort_keys=True)))
    store.check(run_id,'financials',symbol,'OK' if payload['status']=='READY' else 'PARTIAL',
        '报告期'+payload['latest_period']+'；'+('核心财务字段已核验' if not payload['gaps'] else '；'.join(payload['gaps'])))


def snapshot(store,docs,at):
    candidates=[d for d in docs if d['kind']=='financial_data' and d['cloud_allowed']]
    if not candidates:return {'status':'MISSING','gaps':['公司财务底稿尚未取得']}
    d=max(candidates,key=lambda x:x['ready_at'])
    r=store.db.execute('SELECT payload_json FROM fundamental_records WHERE doc_id=?',(d['id'],)).fetchone()
    if not r:return {'status':'MISSING','gaps':['公司财务底稿尚未核验']}
    p=json.loads(r[0]);p.update(doc_id=d['id'],ready_at=d['ready_at'],url=d['url'])
    if (datetime.fromisoformat(at).date()-date.fromisoformat(p['latest_period'])).days>190:
        p['status']='PARTIAL';p['gaps']=list(dict.fromkeys(p['gaps']+['最新财务报告期距今超过190天']))
    return p


def view(payload,quote=None):
    if not payload.get('periods'):return {'状态':'待补齐','缺口':payload.get('gaps',[])}
    periods={r['period']:r for r in payload['periods']};latest=payload['latest_period'];v=periods[latest]['values']
    prior=periods.get(str(int(latest[:4])-1)+latest[4:],{}).get('values',{})
    history=[]
    for row in payload['periods'][:8]:
        p=row['period'];vals=row['values']
        history.append({'报告期':p,**{LABELS[k]+'（单季，亿元）':round(n/1e8,4) if (n:=quarterly(periods,p,k)) is not None else None
            for k in ('revenue','net_profit','core_profit','operating_cashflow')},
            '毛利率（累计，%）':vals.get('gross_margin_pct'),'货币资金（期末，亿元）':round(vals['cash']/1e8,4) if vals.get('cash') is not None else None})
    ttm=trailing(periods,latest,'net_profit');price=(quote or {}).get('price_cents');shares=v.get('shares');bps=v.get('bps')
    valuation={'估值日期':(quote or {}).get('observed_at'),'报告期股本估算市盈率':round(price/100*shares/ttm,2) if price and shares and ttm and ttm>0 else None,
        '报告期每股净资产口径市净率':round(price/100/bps,2) if price and bps and bps>0 else None,
        '说明':'按报告期股本和近四季归母利润估算；不是核验后的实时总市值，股本变化、A/H股价差及回购可能影响可比性。亏损不计算市盈率。',
        '缺口':['未接入一致盈利预期及历史估值分位，不能仅凭当前倍数断言已充分定价或便宜。']}
    return {'状态':'核心字段已核验' if payload['status']=='READY' else '部分字段待核验','来源':payload['provider'],'来源链接':payload.get('url'),
        '报告期':latest,'资料就绪时间':payload.get('ready_at'),'口径':payload['scope'],'单位说明':payload['unit'],
        '最新财务':{LABELS[k]:amount(v.get(k),k) for group in FIELDS.values() for k in group},
        '累计同比（%）':{LABELS[k]:yoy(v.get(k),prior.get(k)) for k in ('revenue','net_profit','core_profit','operating_cashflow')},
        '近八季':history,'近三年年报':[{'报告期':r['period'],**{LABELS[k]:amount(r['values'].get(k),k) for k in ('revenue','net_profit','core_profit','operating_cashflow')}} for r in payload['periods'] if r['period'].endswith('12-31')][:3],
        '估值参考':valuation,'缺口':payload['gaps'],'核验范围':payload['validation'],'限制':payload['limitations']}
