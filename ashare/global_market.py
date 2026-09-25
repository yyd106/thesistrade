"""Public read-only spot/US quotes. Native USD and FX observations are immutable."""
import json
import re
import urllib.request
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo
from urllib.parse import quote as escape
from .storage import now, normalize_time, digest
from .investment_policy import FIXED

SCALE = 100_000_000  # indivisible ledger units per ounce, coin or share
MICRO = 1_000_000


def micros(value):
    d = Decimal(str(value))
    if not d.is_finite() or d <= 0:
        raise ValueError('无效价格或汇率')
    return int((d*MICRO).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def notional(qty, price, fx):
    return (qty*price*fx*100 + SCALE*MICRO*MICRO//2)//(SCALE*MICRO*MICRO)


def stamp(unix):
    return normalize_time(datetime.fromtimestamp(unix, timezone.utc).isoformat())


def get_json(url):
    # URLs constructed exclusively from fixed hosts and validated ticker identifiers.
    req = urllib.request.Request(url, headers={'User-Agent': 'ThesisTrade/0.12 public-research', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('行情响应超出上限')
    return json.loads(raw)


def chart(symbol, fetch=get_json, history=False):
    if not re.fullmatch(r'[A-Z0-9.^=-]{1,15}', symbol):
        raise ValueError('无效行情标识')
    data = fetch('https://query1.finance.yahoo.com/v8/finance/chart/'+escape(symbol, safe='')+'?interval=1d&range='+('3mo' if history else '5d'))
    data = data['chart']['result'][0]
    if data['meta']['symbol'] != symbol:
        raise ValueError('行情标识不匹配')
    return data


def session_open(symbol, at, payload):
    t = datetime.fromisoformat(normalize_time(at))
    if symbol in ('BTC', 'ETH'):
        return True
    if symbol in ('GOLD', 'SILVER'):
        ny = t.astimezone(ZoneInfo('America/New_York'))
        # Conservative weekday window with a daily metals maintenance break.
        return ny.weekday() < 5 and not 17 <= ny.hour < 18
    if symbol.startswith('US:'):
        period = payload.get('session') or {}
        return period.get('start', 10**20) <= t.timestamp() < period.get('end', 0)
    return False


# Buys need a current CNY rate. Sells (exits and stops) accept the last rate for up to four days,
# because the FX market is closed at weekends while BTC/ETH still trade and must stay protected.
FX_BUY_MAX_AGE = 3600
FX_SELL_MAX_AGE = 4 * 86400


def fresh(q, at, max_age=90, fx_max_age=FX_BUY_MAX_AGE):
    if not q:
        return False
    t = datetime.fromisoformat(normalize_time(at))
    return (q['first_seen_at'] <= normalize_time(at)
            and 0 <= (t-datetime.fromisoformat(q['observed_at'])).total_seconds() <= max_age
            and 0 <= (t-datetime.fromisoformat(q['fx_at'])).total_seconds() <= fx_max_age)


def fx_age(q, at):
    return (datetime.fromisoformat(normalize_time(at))-datetime.fromisoformat(q['fx_at'])).total_seconds() if q else None


def latest(store, symbol, at):
    r = store.db.execute('SELECT * FROM global_quotes WHERE symbol=? AND observed_at<=? AND first_seen_at<=? ORDER BY observed_at DESC,first_seen_at DESC LIMIT 1', (symbol, at, at)).fetchone()
    return dict(r) if r else None


def targets(store, config, at):
    if config.get('deployment_role')=='cloud':
        from .cloud_runtime import contract
        selected={}
        for row in store.db.execute("SELECT key FROM cloud_contracts WHERE key LIKE 'global:%'"):
            c=contract(store,row[0])
            if c.get('item'):selected[c['symbol']]=c['item']
        for r in store.db.execute("SELECT symbol FROM global_lots WHERE qty>0 UNION SELECT symbol FROM global_orders WHERE status IN ('OPEN','PARTIAL','UNKNOWN')"):
            selected.setdefault(r[0],{'asset':r[0],'name':r[0],'protected':True,'links':[]})
        return selected
    from .observation import all_items
    from .observation_pool import allocate
    items = allocate(store, all_items(store, at), at, config)['items']
    selected = {i['asset']: i for i in items if i['asset'] in FIXED or i['category'] == 'US' and i['kind'] == 'STOCK'}
    for r in store.db.execute("SELECT symbol FROM global_lots WHERE qty>0 UNION SELECT symbol FROM global_orders WHERE status IN ('OPEN','PARTIAL','UNKNOWN')"):
        selected.setdefault(r[0], {'asset': r[0], 'name': r[0], 'protected': True, 'links': []})
    return selected


def refresh(store, symbols, at=None, fetch=get_json, history=False):
    """Network workers never share a SQLite connection; write only after joining."""
    at = normalize_time(at or now())
    fx = None
    fx_error = None
    try:
        data = chart('CNY=X', fetch)
        if data['meta'].get('currency') != 'CNY':
            raise ValueError('人民币汇率单位错误')
        fx = {'micros': micros(data['meta']['regularMarketPrice']), 'at': stamp(data['meta']['regularMarketTime'])}
    except Exception as exc:
        # Never invent a rate or relabel an old one with the fetch time. Reuse the last observed rate
        # with its own timestamp, so prices keep updating; buys still require a current rate.
        last = store.db.execute('SELECT fx_micros,fx_at FROM global_quotes WHERE fx_at<=? ORDER BY fx_at DESC LIMIT 1', (at,)).fetchone()
        if not last:
            return {'status': 'DEFERRED', 'error': '汇率不可用：'+str(exc)[:180], 'updated': 0}
        fx = {'micros': last['fx_micros'], 'at': last['fx_at']}
        fx_error = '汇率刷新失败，沿用最近一次汇率（'+last['fx_at']+'）：'+str(exc)[:120]

    def read(symbol):
        try:
            bars = []
            if symbol in ('GOLD', 'SILVER'):
                url = 'https://api.gold-api.com/price/'+FIXED[symbol]['provider_symbol']
                raw = fetch(url)
                if raw['symbol'] != FIXED[symbol]['provider_symbol'] or raw.get('currency', 'USD') != 'USD':
                    raise ValueError('现货单位或标识不匹配')
                price, observed, provider, session = micros(raw['price']), normalize_time(raw['updatedAt']), url, {}
            elif symbol in ('BTC', 'ETH'):
                url = 'https://api.exchange.coinbase.com/products/'+FIXED[symbol]['provider_symbol']
                raw = fetch(url+'/ticker')
                price, observed, provider, session = micros(raw['price']), normalize_time(raw['time']), url+'/ticker', {}
                if history:
                    for row in fetch(url+'/candles?granularity=86400'):
                        if row[0]+86400 <= datetime.fromisoformat(at).timestamp():
                            bars.append({'date': stamp(row[0])[:10], 'price_micros': micros(row[4])})
            elif symbol.startswith('US:'):
                raw = chart(symbol[3:], fetch, history)
                meta = raw['meta']
                if meta.get('currency') != 'USD' or meta.get('instrumentType') != 'EQUITY':
                    raise ValueError('仅接入美元普通股，不接杠杆产品/衍生品')
                price, observed, provider = micros(meta['regularMarketPrice']), stamp(meta['regularMarketTime']), 'Yahoo Finance chart'
                session = meta.get('currentTradingPeriod', {}).get('regular', {})
                today = datetime.fromisoformat(at).astimezone(ZoneInfo('America/New_York')).date().isoformat()
                for ts, close in zip(raw.get('timestamp', []), raw['indicators']['quote'][0].get('close', [])):
                    date = datetime.fromtimestamp(ts, ZoneInfo('America/New_York')).date().isoformat()
                    if close and date < today:
                        bars.append({'date': date, 'price_micros': micros(close)})
            else:
                raise ValueError('资产不在现货模拟范围')
            if observed > at or fx['at'] > at:
                # at may precede network response; caller normally uses observation receipt as cutoff.
                receipt = normalize_time(now())
                if observed > receipt or fx['at'] > receipt:
                    raise ValueError('提供方报价时间在未来')
            return symbol, {'price_micros': price, 'observed_at': observed, 'fx_micros': fx['micros'], 'fx_at': fx['at'],
                            'provider': provider, 'currency': 'USD', 'session': session, 'bars': sorted(bars, key=lambda b: b['date'])[-60:]}, raw, None
        except Exception as exc:
            return symbol, None, None, str(exc)[:200]

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='spot-data') as pool:
        results = list(pool.map(read, symbols))
    updated = 0
    for symbol, data, raw, error in results:
        receipt = normalize_time(now())
        old = store.db.execute('SELECT payload_json FROM global_market WHERE symbol=?', (symbol,)).fetchone()
        previous = json.loads(old[0]) if old else {}
        if data:
            if not history or not data['bars']:
                data['bars'] = previous.get('bars', [])
            if symbol in ('GOLD', 'SILVER'):
                # Local daily last observations, never marketed as exchange closes.
                daily = {r['observed_at'][:10]: {'date': r['observed_at'][:10], 'price_micros': r['price_micros']}
                         for r in store.db.execute('SELECT observed_at,price_micros FROM global_quotes WHERE symbol=? AND observed_at<? ORDER BY observed_at', (symbol, at[:10]))}
                data['bars'] = sorted(daily.values(), key=lambda b: b['date'])[-60:]
                data['history_basis'] = '本地每日最后现货样本；非交易所日线'
            payload = {**data, 'status': 'OK', 'error': None}
            path = store.raw(json.dumps(raw, ensure_ascii=False).encode(), '.json')
            qid = digest(json.dumps([symbol, data['observed_at'], data['price_micros'], fx]))[:24]
            with store.db:
                store.db.execute('INSERT OR IGNORE INTO global_quotes VALUES(?,?,?,?,?,?,?,?)',
                                 (qid, symbol, data['observed_at'], receipt, data['price_micros'], data['fx_micros'], data['fx_at'], json.dumps({**data, 'raw_path': str(path)})))
            updated += 1
        else:
            payload = {**previous, 'status': 'FAILED', 'error': error}
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO global_market VALUES(?,?,?)', (symbol, receipt, json.dumps(payload, ensure_ascii=False)))
    return {'status': 'SUCCEEDED' if updated == len(results) and not fx_error else 'PARTIAL', 'updated': updated,
            'errors': {s: error for s, _, _, error in results if error}, 'fx_error': fx_error}
