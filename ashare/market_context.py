"""Comparable dated price/volume features; peer basket is explicitly not an index."""
import json
import math
import statistics
from datetime import datetime
from . import sources
from .storage import now, normalize_time

BENCHMARK = {'symbol':'sh000300','name':'沪深300'}
PEERS = {
    'sh600519':[('sz000858','五粮液'),('sz000568','泸州老窖')],
    'sz000333':[('sz000651','格力电器'),('sh600690','海尔智家')],
    'sz002415':[('sz002236','大华股份')],
    'sz002957':[('sz002031','巨轮智能'),('sz300450','先导智能')],
    'sh688062':[('sh688180','君实生物'),('sh688235','百济神州')],
    'sh605016':[('sz002286','保龄宝'),('sh605077','华康股份')],
    'sz002028':[('sh600406','国电南瑞'),('sh600312','平高电气')],
}


def peers(config,symbol):
    value=config.get('comparison_peers',{}).get(symbol)
    return [(r['symbol'],r['name']) for r in value] if value is not None else PEERS.get(symbol,[])


def parse(raw,symbol,day,index=False):
    obj=json.loads(raw);series=obj['data'][symbol]
    basis='QFQ_SNAPSHOT' if series.get('qfqday') else 'INDEX_PRICE' if index else 'UNADJUSTED'
    bars=series.get('qfqday') or series.get('day')
    if not isinstance(bars,list):raise ValueError('量价序列格式错误')
    out=[]
    for b in bars:
        if b[0]>=day:continue
        datetime.strptime(b[0],'%Y-%m-%d')
        vals=[float(v) for v in b[1:6]]
        if len(vals)!=5 or any(not math.isfinite(v) for v in vals):raise ValueError('量价字段缺失')
        o,c,h,l,v=vals
        if min(o,c,h,l)<=0 or v<0 or not l<=min(o,c)<=max(o,c)<=h:raise ValueError('量价数据关系异常')
        out.append({'date':b[0],'open':o,'close':c,'high':h,'low':l,'volume':v})
    out.sort(key=lambda b:b['date'])
    if len(out)<61 or len({b['date'] for b in out})!=len(out):raise ValueError('量价序列不足61日或日期重复')
    return {'symbol':symbol,'basis':basis,'bars':out}


def features(series):
    b=series['bars'];c=[v['close'] for v in b];last=c[-1]
    ret=lambda n:round((last/c[-n-1]-1)*100,2)
    ranges=[max(b[i]['high']-b[i]['low'],abs(b[i]['high']-c[i-1]),abs(b[i]['low']-c[i-1])) for i in range(len(b)-14,len(b))]
    vols=[v['volume'] for v in b];avg=sum(vols[-21:-1])/20
    logs=[math.log(c[i]/c[i-1]) for i in range(len(c)-20,len(c))]
    return {'截至交易日':b[-1]['date'],'价格口径':{'QFQ_SNAPSHOT':'当前前复权快照','INDEX_PRICE':'指数价格','UNADJUSTED':'未复权，跨除权比较受限'}[series['basis']],
        '近1日涨跌幅（%）':ret(1),'近5日涨跌幅（%）':ret(5),'近20日涨跌幅（%）':ret(20),'近60日涨跌幅（%）':ret(60),
        '较20日均价偏离（%）':round((last/(sum(c[-20:])/20)-1)*100,2),
        '当日成交量相对前20日均量（倍）':round(vols[-1]/avg,2) if avg>0 else None,
        '近5日均量相对之前20日均量（倍）':round((sum(vols[-5:])/5)/(sum(vols[-25:-5])/20),2) if sum(vols[-25:-5])>0 else None,
        '14日平均真实波幅占收盘价（%）':round(sum(ranges)/14/last*100,2),
        '20日收益波动率年化（%）':round(statistics.stdev(logs)*math.sqrt(244)*100,2),
        '距60日最高收盘回撤（%）':round((last/max(c[-60:])-1)*100,2),
        '说明':'成交量只计算同来源序列的比值；波动按244交易日年化。前复权数值仅供当前研究，不用于历史回测或委托价格。'}


def collect_comparisons(store,run_id,config):
    from .calendar import completed_bar_cutoff
    cutoff=completed_bar_cutoff(now())
    requested={BENCHMARK['symbol']:BENCHMARK['name']}
    from .universe import company_targets
    for item in company_targets(store,config):requested.update(dict(peers(config,item['symbol'])))
    for symbol,name in requested.items():
        try:
            raw=sources.fetch('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param='+symbol+',day,,,120,qfq',max_bytes=2000000)
            path=store.raw(raw,'.json');series=parse(raw,symbol,cutoff,symbol==BENCHMARK['symbol'])
            series.update(name=name,raw_path=path)
            with store.db:store.db.execute('INSERT OR REPLACE INTO comparison_series VALUES(?,?,?,?)',(run_id,symbol,now(),json.dumps(series,ensure_ascii=False)))
            store.record_attempt('comparison_series','MARKET','OK',resource_key=symbol,title=name,run_id=run_id)
        except Exception as exc:
            store.record_attempt('comparison_series','MARKET','FAILED',str(exc),resource_key=symbol,title=name,run_id=run_id)


def comparison(own,other,period):
    if own['basis']=='UNADJUSTED' or other['basis']=='UNADJUSTED':return None
    start,end=own['bars'][-period-1]['date'],own['bars'][-1]['date']
    by={b['date']:b['close'] for b in other['bars']}
    if other['bars'][-1]['date']!=end or start not in by:return None
    result=(by[end]/by[start]-1)*100
    own_ret=(own['bars'][-1]['close']/own['bars'][-period-1]['close']-1)*100
    return {'起始日':start,'截止日':end,'收益（%）':round(result,2),'个股超额（百分点）':round(own_ret-result,2)}


def assemble(store,config,run_id,symbol,raw,day,at=None):
    own=parse(raw,symbol,day);result=features(own);gaps=[];rows={}
    stamp=normalize_time(at or now())
    for row in store.db.execute('SELECT * FROM comparison_series WHERE run_id=? AND ready_at<=?',(run_id,stamp)):
        rows[row['symbol']]=dict(row)
    comparisons=[]
    for code,name in [(BENCHMARK['symbol'],BENCHMARK['name']),*peers(config,symbol)]:
        stored=rows.get(code)
        if stored is None:
            # A transient fetch failure does not erase a completed trading day's
            # already collected prices. Only consider the latest known revision;
            # never search backwards for an older, more convenient comparison.
            cached=store.db.execute('SELECT * FROM comparison_series WHERE symbol=? AND ready_at<=? ORDER BY ready_at DESC,rowid DESC LIMIT 1',(code,stamp)).fetchone()
            stored=dict(cached) if cached else None
        row=json.loads(stored['payload_json']) if stored else None
        if row and row.get('symbol')!=code:row=None
        returns={str(n):comparison(own,row,n) if row else None for n in (20,60)}
        if not all(returns.values()):gaps.append(name+'缺少同区间、可比口径行情')
        comparisons.append({'symbol':code,'名称':name,'类型':'宽基指数' if code==BENCHMARK['symbol'] else '业务参考股',
            '20日':returns['20'],'60日':returns['60'],'原始资料':row.get('raw_path') if row else None,
            '获取时间':stored['ready_at'] if stored else None,
            '使用方式':('本轮获取' if stored['run_id']==run_id else '复用同交易日历史资料') if stored and all(returns.values()) else '待补齐',
            '来源批次':stored['run_id'] if stored else None})
    peer_count=len(peers(config,symbol));basket={}
    for n in (20,60):
        vals=[r[f'{n}日'] for r in comparisons if r['类型']=='业务参考股']
        if vals and all(vals):
            basket[f'{n}日收益（%）']=round(sum(v['收益（%）'] for v in vals)/len(vals),2)
            basket[f'{n}日个股超额（百分点）']=round(sum(v['个股超额（百分点）'] for v in vals)/len(vals),2)
    if not peer_count:gaps.append('尚未配置业务参考股')
    result.update(市场与行业对照=comparisons,业务参考篮子=basket,
        对照说明='预设业务参考股的等权期间收益，不是官方行业指数；样本有限，不代表完整行业。仅在所有指定样本同区间可比时计算篮子。',缺口=gaps)
    return result
