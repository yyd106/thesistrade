from __future__ import annotations

import html
import io
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from .finance import cents
from .storage import now, digest, normalize_time

SH = ZoneInfo("Asia/Shanghai")
ALLOWED_HOSTS = {"qt.gtimg.cn", "web.ifzq.gtimg.cn", "www.cninfo.com.cn", "static.cninfo.com.cn", "www.csrc.gov.cn", "www.sse.com.cn",
                 "www.sec.gov", "data.sec.gov", "search.ccgp.gov.cn", "www.ccgp.gov.cn", "news.un.org", "www.mofcom.gov.cn", "datacenter-web.eastmoney.com", "data.eastmoney.com",
                 "push2his.eastmoney.com", "money.finance.sina.com.cn"}


def check_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or p.hostname not in ALLOWED_HOSTS or p.username or p.password or p.port not in (None, 443):
        raise ValueError("只允许已配置的公开 HTTPS 数据源")


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        from .sec_source import HOSTS
        if urllib.parse.urlsplit(newurl).hostname in HOSTS:
            raise ValueError('SEC请求必须使用独立受控客户端')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, form=None, max_bytes=20_000_000):
    check_url(url)
    from .sec_source import HOSTS
    if urllib.parse.urlsplit(url).hostname in HOSTS:
        raise ValueError('SEC请求必须使用独立受控客户端')
    payload = urllib.parse.urlencode(form).encode() if form is not None else None
    headers = {"User-Agent": "Mozilla/5.0 (personal research prototype)", "Referer": "https://www.cninfo.com.cn/"}
    req = urllib.request.Request(url, data=payload, headers=headers)
    opener = urllib.request.build_opener(SafeRedirect())
    last = None
    for attempt in range(2):
        try:
            with opener.open(req, timeout=12) as r:
                check_url(r.url)
                if int(r.headers.get("Content-Length", "0")) > max_bytes:
                    raise ValueError("响应超过容量限制")
                chunks, total = [], 0
                deadline = time.monotonic() + 30
                while True:
                    if time.monotonic() > deadline:
                        raise TimeoutError("下载超时")
                    b = r.read(min(65536, max_bytes + 1 - total))
                    if not b:
                        break
                    chunks.append(b)
                    total += len(b)
                    if total > max_bytes:
                        raise ValueError("响应超过容量限制")
                return b"".join(chunks)
        except (OSError, TimeoutError) as e:
            last = e
            if attempt == 0:
                time.sleep(1)
    raise last


def parse_quotes(raw, expected):
    from .quote_health import quote_fields
    rows = []
    for symbol, body in re.findall(r'v_((?:sh|sz)\d{6})="([^"]*)"', raw.decode("gb18030")):
        if symbol not in expected:
            continue
        rows.append(quote_fields(symbol, body.split("~")))
    if {r["symbol"] for r in rows} != set(expected):
        raise ValueError("行情缺少请求证券")
    return rows


def _store_quote(store, run_id, row, source, path):
    with store.db:
        store.db.execute("INSERT OR IGNORE INTO quotes VALUES(?,?,?,?,?,?,?,?,?)",
                         (digest(json.dumps(row, sort_keys=True))[:24], row["symbol"], row["name"], row["price_cents"], row["prev_close_cents"], row["observed_at"], now(), source, path))
    store.record_attempt('tencent_quotes', row["symbol"], 'OK', run_id=run_id)


def collect_quotes(store, run_id, symbols, config=None, fetch_fn=None):
    """One batch request to qt.gtimg.cn; its quotes are stored at once. With a config that allows it, a
    symbol the batch cannot supply is then fetched from Tencent's minute endpoint: at most once a minute per
    symbol, a few at a time, within 20 seconds overall. In settled continuous trading, when even the newest
    quote of an exchange's stocks (of the whole batch, when either exchange has fewer than three) is older
    than the usable age, that feed has stopped updating and all of its stocks count as missing; one stale
    stock among live ones is only an inactive stock and is stored as before. Every outage is recorded by
    quote_health on the executing node. Raises when a symbol is left without a usable quote."""
    from . import quote_health as qh
    fetch_fn = fetch_fn or fetch
    at = now()
    live = bool(config) and qh.live(at)
    max_age = (config or {}).get('quote_max_age_seconds', 90)
    backup = bool(config) and config.get('quote_fallback_enabled', True)
    raw = path = primary_error = None
    try:
        raw = fetch_fn("https://qt.gtimg.cn/q=" + ",".join(symbols), max_bytes=100000)
        path = store.raw(raw, ".txt")
    except Exception as exc:
        primary_error = exc
    parsed, reasons, unpriced = {}, {s: primary_error for s in symbols}, set()
    for symbol in symbols if raw is not None else ():
        try:
            parsed[symbol] = parse_quotes(raw, [symbol])[0]
        except qh.PriceUnavailable as exc:
            reasons[symbol] = exc
            unpriced.add(symbol)  # answered without a price (suspended): not an outage, no backup request
        except Exception as exc:
            reasons[symbol] = exc
    boards = {b: {s: r for s, r in parsed.items() if s.startswith(b)} for b in ('sh', 'sz')}
    # Per exchange when each has a few stocks, so a feed that stops for one exchange is still caught;
    # otherwise the whole batch, so a single quiet stock is never mistaken for a stopped feed.
    groups = boards if all(len(g) >= 3 for g in boards.values()) else {'': dict(parsed)}
    for name, group in groups.items() if live else ():
        if group and min(qh.age(r, at) for r in group.values()) > max_age:
            newest = max(r['observed_at'] for r in group.values())
            for symbol in group:
                reasons[symbol] = ValueError(f'主接口{name.upper()}报价整体停止更新（最新 {newest}）')
                del parsed[symbol]
    for symbol in symbols:
        if symbol in parsed:
            _store_quote(store, run_id, parsed[symbol], "tencent_public_research", path)
    primary_failed = [s for s in symbols if s not in parsed and s not in unpriced]
    missing, used = list(primary_failed), 0
    if primary_failed and backup:
        due, covered, waiting = qh.reserve_fallback(store, primary_failed, at)
        results = {}
        for symbol, body in qh.fetch_many(due, fetch_fn).items():
            try:
                if isinstance(body, Exception):
                    raise body
                row, _ = qh.parse_minute(body, symbol)
                if live and qh.age(row, at) > max_age:
                    raise ValueError(f"报价已过时（{row['observed_at']}）")
                _store_quote(store, run_id, row, "tencent_minute_fallback", store.raw(body, ".json"))
                results[symbol] = True
                used += 1
            except Exception as exc:
                reasons[symbol] = ValueError(f'{reasons[symbol]}；备用分时接口：{type(exc).__name__}: {str(exc)[:160]}')
                results[symbol] = False
        qh.mark_fallback(store, results, at)
        for symbol in waiting:
            reasons[symbol] = ValueError(f'{reasons[symbol]}；备用分时接口一分钟内已经问过，还没有可用报价')
        # A symbol whose backup quote from under a minute ago is still usable is not missing.
        missing = [s for s in primary_failed if s not in covered and not results.get(s)]
    errors = [s for s in symbols if s in unpriced or s in missing]
    for symbol in errors:
        store.record_attempt('tencent_quotes', symbol, 'FAILED', str(reasons[symbol])[:300], run_id=run_id)
    if config:
        cause = f'{type(primary_error).__name__}: {primary_error}' if primary_error else ''
        qh.observe(store, config, at, primary_failed, [s for s in errors if s not in unpriced],
                   detail=(cause + '；' if cause else '') + ('备用分时接口已开启' if backup else '备用分时接口已关闭'))
    if errors:
        if primary_error is not None and len(errors) == len(symbols):
            raise primary_error
        raise ValueError('部分股票行情无法读取：'+','.join(errors))
    note = f"；{used}只改用分时接口" if used else ''
    store.check(run_id, "tencent_quotes", None, "OK", f"{len(symbols)}只；公开研究行情，延迟与商用授权未验证，不能用于实盘执行{note}")


def history_summary(raw, symbol, cutoff_date):
    obj = json.loads(raw)
    series = obj["data"][symbol]
    bars = series.get('qfqday') or series.get('day')
    if not isinstance(bars,list):raise ValueError('历史价格格式无法读取')
    complete = sorted((b for b in bars if b[0] < cutoff_date), key=lambda b: b[0])
    if len(complete) < 60 or len({b[0] for b in complete}) != len(complete):
        raise ValueError("不足60个完整交易日或出现重复日期")
    close = [Decimal(b[2]) for b in complete]
    if any(x <= 0 or not x.is_finite() for x in close):
        raise ValueError("日线价格无效")
    ma20 = sum(close[-20:]) / 20
    ma60 = sum(close[-60:]) / 60
    return {"symbol": symbol, "last_complete_date": complete[-1][0], "adjustment": "qfq_current_snapshot" if series.get('qfqday') else 'provider_unadjusted_fallback',
            "close": str(close[-1]), "ma20": str(ma20), "ma60": str(ma60),
            "return_20d_pct": str((close[-1] / close[-21] - 1) * 100),
            "trend_partial_points": (10 if close[-1] > ma20 else 0) + (10 if ma20 > ma60 else 0),
            "missing": ["行业相对收益", "财务", "估值分位", "成交额与交易规则完整验证"],
            "notice": "当前前复权快照；不可冒充历史时点已经知道的复权数据；非完整策略评分" if series.get('qfqday') else "来源返回原始历史价格，未声明为前复权序列；非完整策略评分"}


def _eastmoney_raw(symbol, fqt=0):
    """(vendor raw bytes, bars) [date,open,close,high,low,volume(手)] from Eastmoney's kline API."""
    secid = ('1' if symbol.startswith('sh') else '0') + '.' + symbol[2:]
    raw = fetch(f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}&klt=101&fqt={fqt}"
                "&lmt=90&end=20500101&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56", max_bytes=2000000)
    klines = (json.loads(raw).get('data') or {}).get('klines')
    if not isinstance(klines, list) or not klines:
        raise ValueError('东方财富日线结构变化')
    bars = [b.split(',')[:6] for b in klines]
    if any(len(b) < 6 for b in bars):
        raise ValueError('东方财富日线字段缺失')
    return raw, bars[-90:]


def eastmoney_daily(symbol, fqt=0):
    """Daily bars [date,open,close,high,low,volume(手)] from Eastmoney's public kline API."""
    return _eastmoney_raw(symbol, fqt)[1]


def _sina_raw(symbol):
    """(vendor raw bytes, bars) in the same layout; Sina volume is shares, rounded to 手 here."""
    raw = fetch(f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
                f"?symbol={symbol}&scale=240&ma=no&datalen=90", max_bytes=2000000)
    rows = json.loads(raw)
    if not isinstance(rows, list) or not rows:
        raise ValueError('新浪日线结构变化')
    bars = sorted([[r['day'], str(r['open']), str(r['close']), str(r['high']), str(r['low']),
                   str((Decimal(str(r['volume'])) / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))]
                  for r in rows], key=lambda b: b[0])
    return raw, bars[-90:]


def sina_daily(symbol):
    """Daily bars in the same layout; Sina reports volume in shares, converted to 手 here."""
    return _sina_raw(symbol)[1]


def tencent_wrap(symbol, key, bars):
    """Tencent-shaped JSON bytes so downstream code never learns which source answered."""
    return json.dumps({'data': {symbol: {key: bars}}}, ensure_ascii=False).encode()


def closes_agreement(a, b):
    """(shared date count, max close difference in cents) between two bar lists."""
    ca, cb = {x[0]: cents(x[2]) for x in a}, {x[0]: cents(x[2]) for x in b}
    shared = sorted(set(ca) & set(cb))
    return len(shared), (max(abs(ca[d] - cb[d]) for d in shared) if shared else None)


def closes_match(a, b, min_overlap=20):
    """Every shared date's close within one cent, over at least min_overlap dates."""
    overlap, max_diff = closes_agreement(a, b)
    return overlap >= min_overlap and max_diff is not None and max_diff <= 1


def _bars_problem(bars, cutoff, expected):
    """Rejection reason or None: the pre-selection validity gate for every candidate.

    Parseable ISO dates, strictly ascending and unique, at least 60 completed bars with
    positive OHLC, and the newest completed bar reaching the last completed trading day —
    the same definition research.py uses for STALE_DAILY_BARS."""
    if not bars:
        return '空序列'
    days = []
    for b in bars:
        try:
            days.append(date.fromisoformat(b[0]))
        except (ValueError, TypeError, IndexError):
            return '日期无法解析'
    if len(set(days)) != len(days) or days != sorted(days):
        return '日期重复或乱序'
    for b in bars:
        try:
            ohlc = [Decimal(str(b[i])) for i in (1, 2, 3, 4)]
            if not all(v.is_finite() for v in ohlc):
                return '价格非有限数值'
            if any(v <= 0 for v in ohlc):
                return '价格非正'
        except Exception:
            return '价格无法解析'
    completed = [b for b in bars if b[0] < cutoff]
    if len(completed) < 60:
        return '完整交易日不足60根'
    if expected and completed[-1][0] < expected:
        return '最新完整交易日早于' + expected
    return None


def _attempt_detail(params, **fields):
    return json.dumps({'params': params, **{k: v for k, v in fields.items() if v is not None}}, ensure_ascii=False)


def saved_unadjusted(store, symbol):
    row = store.db.execute("SELECT payload FROM market_features WHERE symbol=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                           (symbol,)).fetchone() if store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='market_features'").fetchone() else None
    return (json.loads(row[0]).get('unadjusted') or {}).get('bars') if row else None


def unadjusted_daily(store, run_id, symbol):
    """(raw_bytes, source, tencent_error, vendor_raw): the plain series plus its origin bytes.

    Every candidate is validated before selection (ISO dates, ascending unique, >=60
    completed bars, positive OHLC, newest completed bar reaching the last completed day).
    With a saved reference sharing >=20 dates a mirror must reproduce it within 1 cent —
    Eastmoney first, Sina only if Eastmoney fails or is rejected. With no sufficient saved
    reference both mirrors are fetched and must reproduce each other."""
    from .calendar import completed_bar_cutoff, last_completed_day
    cutoff, expected = completed_bar_cutoff(now()), last_completed_day(now())
    try:
        raw = fetch("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=" + symbol + ",day,,,90,", max_bytes=2000000)
        bars = json.loads(raw)['data'][symbol]['day']
        if not isinstance(bars, list):
            raise ValueError('腾讯日线结构变化')
        problem = _bars_problem(bars, cutoff, expected)
        if problem:
            raise ValueError('腾讯未复权日线不合格：' + problem)
        return raw, 'tencent_daily', None, raw
    except Exception as exc:
        tencent_error = exc
    saved = saved_unadjusted(store, symbol)
    mirrors = (('eastmoney_daily', lambda: _eastmoney_raw(symbol, 0), 'klt=101&fqt=0&lmt=90'),
               ('sina_daily', lambda: _sina_raw(symbol), 'scale=240&ma=no&datalen=90'))
    held = []
    for idx, (name, fetcher, params) in enumerate(mirrors):
        try:
            vendor_raw, bars = fetcher()
        except Exception as exc:
            store.record_attempt(name, symbol, 'FETCH_FAILED', _attempt_detail(params, reason=str(exc)[:200]), run_id=run_id)
            continue
        try:
            reason = _bars_problem(bars, cutoff, expected)
        except Exception as exc:
            reason = '序列校验异常：' + str(exc)[:100]
        if reason:
            store.record_attempt(name, symbol, 'REJECTED', _attempt_detail(params, bars=len(bars), reason=reason), run_id=run_id)
            continue
        overlap = max_diff = None
        if saved is not None:
            overlap, max_diff = closes_agreement(bars, saved)
            if overlap >= 20:
                if max_diff <= 1:
                    for hname, hparams, hbars, _hv, hover, hdiff in held:
                        store.record_attempt(hname, symbol, 'REJECTED',
                            _attempt_detail(hparams, bars=len(hbars), overlap=hover, max_diff_cents=hdiff,
                                            reason='已请求但因参照重叠不足未采用，后由其他备用源通过'), run_id=run_id)
                    for later_name, _, later_params in mirrors[idx + 1:]:
                        store.record_attempt(later_name, symbol, 'SKIPPED', _attempt_detail(later_params, reason='前序备用源已通过，不再请求'), run_id=run_id)
                    store.record_attempt(name, symbol, 'ACCEPTED', _attempt_detail(params, bars=len(bars), overlap=overlap, max_diff_cents=max_diff), run_id=run_id)
                    return tencent_wrap(symbol, 'day', bars), name, tencent_error, vendor_raw
                store.record_attempt(name, symbol, 'REJECTED', _attempt_detail(params, bars=len(bars), overlap=overlap, max_diff_cents=max_diff, reason='与已保存序列不一致'), run_id=run_id)
                continue
        held.append((name, params, bars, vendor_raw, overlap, max_diff))
    # No sufficient saved reference: the mirrors must reproduce each other.
    if len(held) >= 2:
        overlap, max_diff = closes_agreement(held[0][2], held[1][2])
        agree = overlap >= 20 and max_diff is not None and max_diff <= 1
        for name, params, bars, vendor_raw, _ov, _md in held:
            store.record_attempt(name, symbol, 'ACCEPTED' if agree else 'REJECTED',
                _attempt_detail(params, bars=len(bars), overlap=overlap, max_diff_cents=max_diff,
                                reason='镜像互验' + ('一致' if agree else '不一致')), run_id=run_id)
        if agree:
            return tencent_wrap(symbol, 'day', held[0][2]), held[0][0], tencent_error, held[0][3]
    else:
        for name, params, bars, vendor_raw, _ov, _md in held:
            store.record_attempt(name, symbol, 'REJECTED', _attempt_detail(params, bars=len(bars), reason='无足够参照且镜像不足，无法互验'), run_id=run_id)
    return None, None, tencent_error, None


def qfq_daily(store, run_id, symbol, cutoff):
    """(raw_bytes, source): Tencent's qfq series, or Eastmoney's fqt=1 as a last resort."""
    try:
        raw = fetch("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=" + symbol + ",day,,,90,qfq", max_bytes=2000000)
        history_summary(raw, symbol, cutoff)
        return raw, 'tencent_daily', None
    except Exception as exc:
        tencent_error = exc
    try:
        bars = eastmoney_daily(symbol, fqt=1)
        store.record_attempt('eastmoney_daily', symbol, 'OK', run_id=run_id)
    except Exception as exc:
        store.record_attempt('eastmoney_daily', symbol, 'FAILED', str(exc)[:300], run_id=run_id)
        return None, None, tencent_error
    return tencent_wrap(symbol, 'qfqday', bars), 'eastmoney_daily', tencent_error


def collect_history(store, run_id, symbol,config=None):
    from .calendar import completed_bar_cutoff
    cutoff=completed_bar_cutoff(now())
    raw, qfq_source, qfq_error = qfq_daily(store, run_id, symbol, cutoff)
    plain_raw, daily_source, daily_error, vendor_raw = unadjusted_daily(store, run_id, symbol)
    # One monitoring row per stock per round, whichever sources served the bars: ChatGPT's
    # Tencent failure statistics count a single tencent_daily attempt per collect_history call.
    if qfq_error is None and daily_error is None:
        store.record_attempt('tencent_daily', symbol, 'OK', run_id=run_id)
    else:
        which = '、'.join(name for name, err in (('复权', qfq_error), ('未复权', daily_error)) if err)
        err = qfq_error or daily_error
        store.record_attempt('tencent_daily', symbol, 'FAILED', f'{which}请求失败：{err}', run_id=run_id)
    if raw is None:
        raise qfq_error
    if plain_raw is None:
        raise daily_error
    path = store.raw(raw, ".json")
    summary = history_summary(raw, symbol, cutoff)
    # Executable price plans use a separate unadjusted sequence; never mix qfq prices into orders.
    plain_path = store.raw(plain_raw, '.json')
    bars = json.loads(plain_raw)['data'][symbol]['day']
    completed = sorted([b for b in bars if b[0] < cutoff], key=lambda b:b[0])
    if len(completed) < 60 or len({b[0] for b in completed}) != len(completed):
        raise ValueError('未复权日线不足或重复')
    prices = [cents(b[2]) for b in completed]
    if any(p <= 0 for p in prices):
        raise ValueError('未复权价格无效')
    # The vendor's original response bytes are stored apart from the normalized series;
    # series_hash/vendor_hash link the two for audit.
    vendor_path = plain_path if vendor_raw == plain_raw else store.raw(vendor_raw, '.json')
    summary['unadjusted'] = {'last_complete_date': completed[-1][0], 'close_cents': prices[-1],
        'ma20_cents': sum(prices[-20:]) // 20, 'ma60_cents': sum(prices[-60:]) // 60,
        'raw_path': plain_path, 'bars': completed, 'basis': 'UNADJUSTED',
        'series_hash': digest(plain_raw), 'vendor_path': vendor_path,
        'vendor_hash': digest(vendor_raw), 'source': daily_source}
    summary['qfq_source'] = qfq_source
    from . import market_context
    try:
        summary['market_context']=market_context.assemble(store,config or {},run_id,symbol,raw,cutoff)
        store.check(run_id,'market_comparison',symbol,'PARTIAL' if summary['market_context']['缺口'] else 'OK',
            '；'.join(summary['market_context']['缺口']) or '量价、宽基指数与业务参考股对照已核验')
    except Exception as exc:
        summary['market_context']={'缺口':['量价及市场对照未完成核验']}
        store.check(run_id,'market_comparison',symbol,'FAILED',str(exc))
    store.db.execute("CREATE TABLE IF NOT EXISTS market_features(run_id TEXT, symbol TEXT, created_at TEXT, raw_path TEXT, payload TEXT, PRIMARY KEY(run_id,symbol))")
    store.db.execute("INSERT OR REPLACE INTO market_features VALUES(?,?,?,?,?)", (run_id, symbol, now(), path, json.dumps(summary, ensure_ascii=False)))
    store.db.commit()
    store.check(run_id, "daily_bars", symbol, "OK",
                f"历史日线与原始价格均已核对（复权序列：{qfq_source}，未复权序列：{daily_source}）；排除当日未完成K线；非执行行情")


def stock_catalog(store):
    cache = store.root / "cache" / "cninfo-stock-list.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
        return json.loads(cache.read_bytes())["stockList"]
    raw = fetch("https://www.cninfo.com.cn/new/data/szse_stock.json", max_bytes=5000000)
    obj = json.loads(raw)
    if not isinstance(obj.get("stockList"), list):
        raise ValueError("股票目录结构变化")
    from .storage import atomic_write
    atomic_write(cache, raw)
    store.raw(raw, ".json")
    return obj["stockList"]


def pdf_pages(raw):
    if not raw.startswith(b"%PDF"):
        raise ValueError("响应并非PDF")
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw))
    if len(reader.pages) > 500:
        raise ValueError("PDF超过500页，需分批导入")
    pages = [(i + 1, p.extract_text() or "") for i, p in enumerate(reader.pages)]
    if sum(len(t.strip()) for _, t in pages) < 80:
        raise ValueError("PDF文字不足，需OCR；未将扫描件视为已读")
    return pages


def collect_announcements(store, run_id, item, config, catalog):
    symbol = item["symbol"]
    code = symbol[2:]
    matches = [s for s in catalog if s.get("code") == code]
    if len(matches) != 1 or not matches[0].get("orgId"):
        raise ValueError("无法唯一确认巨潮股票orgId")
    end = datetime.now(SH).date()
    start = end - timedelta(days=config["announcement_lookback_days"])
    announcements = []
    complete = False
    for page in range(1, config["max_announcement_pages"] + 1):
        form = {"pageNum": page, "pageSize": 30, "column": "sse" if symbol.startswith("sh") else "szse",
                "tabName": "fulltext", "stock": code + "," + matches[0]["orgId"], "searchkey": "",
                "secid": "", "plate": "", "category": "", "trade": "", "seDate": f"{start}~{end}",
                "sortName": "time", "sortType": "desc", "isHLtitle": "false"}
        raw = fetch("https://www.cninfo.com.cn/new/hisAnnouncement/query", form=form, max_bytes=3000000)
        store.raw(raw, ".json")
        data = json.loads(raw)
        if "announcements" not in data or "hasMore" not in data:
            raise ValueError("公告查询结构变化")
        batch = data["announcements"] or []
        for a in batch:
            if a.get("secCode") != code:
                raise ValueError("公告返回了其他证券，拒绝关联")
        announcements.extend(batch)
        if not data["hasMore"]:
            complete = True
            break
        time.sleep(0.3)
    # 元数据清单完整入库；正文只限额下载，不能声称已检查所有公告正文。
    for a in announcements:
        title = html.unescape(re.sub(r"<[^>]*>", "", a["announcementTitle"]))
        url = urllib.parse.urljoin("https://static.cninfo.com.cn/", a["adjunctUrl"])
        check_url(url)
        published = datetime.fromtimestamp(a["announcementTime"] / 1000, timezone.utc).isoformat()
        raw_path = store.raw(json.dumps(a, ensure_ascii=False).encode(), ".json")
        store.add_document(symbol=symbol, kind="announcement_metadata", title=title, source="cninfo", url=url,
                           published_at=published, time_precision="date", pages=[(None, title)], raw_path=raw_path,
                           quality="metadata_only", cloud_allowed=True)
    store.check(run_id, "cninfo_catalog", symbol, "OK" if complete else "PARTIAL",
                f"{start}至{end}共{len(announcements)}条元数据；正文仅选取限额；分页{'完成' if complete else '未完成'}",track=not config.get('_intraday'))
    downloaded, errors = 0, []
    selected = select_announcements(store, announcements, symbol, config)
    for a in selected:
        url = urllib.parse.urljoin("https://static.cninfo.com.cn/", a["adjunctUrl"])
        try:
            raw = fetch(url)
            path = store.raw(raw, ".pdf")
            # Revalidate bytes each collection, but skip expensive extraction if unchanged.
            existing = store.db.execute("SELECT 1 FROM documents WHERE url=? AND kind='company_report' AND raw_path=?", (url, path)).fetchone()
            if existing:
                store.record_attempt('cninfo_pdf',symbol,'OK',resource_key=url,title=a['announcementTitle'],run_id=run_id)
                continue
            pages = pdf_pages(raw)
            published = datetime.fromtimestamp(a["announcementTime"] / 1000, timezone.utc).isoformat()
            store.add_document(symbol=symbol, kind="company_report", title=html.unescape(re.sub(r"<[^>]*>", "", a["announcementTitle"])),
                               source="cninfo", url=url, published_at=published, time_precision="date", pages=pages,
                               raw_path=path, quality="pdf_text_layout_unverified", cloud_allowed=True)
            downloaded += 1
            store.record_attempt('cninfo_pdf',symbol,'OK',resource_key=url,title=a['announcementTitle'],run_id=run_id)
        except Exception as e:
            errors.append(type(e).__name__ + ": " + str(e)[:160])
            store.record_attempt('cninfo_pdf',symbol,'FAILED',str(e),resource_key=url,title=a['announcementTitle'],run_id=run_id)
    store.check(run_id, "cninfo_pdf", symbol, "PARTIAL" if errors else "OK",
                f"本次新增{downloaded}份正文；按重要性补取{len(selected)}份，已取得正文不占补取名额；PDF表格布局未人工校验；" + "; ".join(errors),track=False)
    # A zero-download intraday catalog refresh must not clear a full-text failure.
    if selected and not errors:
        store.record_attempt('cninfo_pdf',symbol,'OK',run_id=run_id)


def select_announcements(store, announcements, symbol, config):
    from .materiality import classify
    limit=config['pdf_downloads_per_stock']
    if not limit:return []
    stamp=datetime.fromisoformat(now());missing=[];recheck=[];failed=[]
    for a in announcements:
        url=urllib.parse.urljoin('https://static.cninfo.com.cn/',a['adjunctUrl'])
        body=store.db.execute("SELECT 1 FROM documents WHERE symbol=? AND url=? AND kind='company_report'",(symbol,url)).fetchone()
        last=store.db.execute("SELECT status,checked_at FROM data_attempts WHERE source='cninfo_pdf' AND symbol=? AND resource_key=? ORDER BY checked_at DESC,id DESC LIMIT 1",(symbol,url)).fetchone()
        entry=(a,url,last)
        if not body:missing.append(entry)
        elif last and last['status']!='OK':failed.append(entry)
        elif not last or (stamp-datetime.fromisoformat(last['checked_at'])).total_seconds()>=config.get('document_recheck_hours',24)*3600:recheck.append(entry)
    def priority(e):
        a,_,last=e
        # At equal importance, unattempted items precede retries so one failed URL cannot starve the queue.
        return (classify(a['announcementTitle'])['priority'],bool(last and last['status']!='OK'),-a['announcementTime'])
    selected=sorted(missing,key=priority)[:limit]
    selected+=sorted(failed,key=priority)[:max(0,limit-len(selected))]
    # Separate revision budget guarantees revision checks even while a backlog remains.
    revisions=sorted(recheck,key=lambda e:e[2]['checked_at'] if e[2] else '')[:config.get('pdf_revision_checks_per_stock',1)]
    return [e[0] for e in selected+revisions]


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ignore = 0
        self.parts = []
        self.links = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.ignore += 1
        if tag == "a":
            self.current = [dict(attrs).get("href", ""), ""]

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.ignore = max(0, self.ignore - 1)
        if tag == "a" and self.current:
            self.links.append(tuple(self.current))
            self.current = None

    def handle_data(self, text):
        if self.ignore:
            return
        if text.strip():
            self.parts.append(text.strip())
        if self.current:
            self.current[1] += text.strip()


def collect_news(store, run_id, url, fulltext_limit=3):
    raw = fetch(url, max_bytes=3000000)
    path = store.raw(raw, ".html")
    parser = ArticleParser()
    parser.feed(raw.decode("utf-8"))
    links = [(urllib.parse.urljoin(url, h), t) for h, t in parser.links if len(t) > 12 and re.search(r"/c_\d+|/content_|/\d{8}/|/\d{6}/", h)]
    links = list(dict.fromkeys(links))[:20]
    if not links:
        raise ValueError("新闻列表未解析到有效条目；页面可能动态渲染或结构变化")
    text = "\n".join(t + "\n" + u for u, t in links)
    store.add_document(symbol="MARKET", kind="news_index", title="监管与交易所新闻列表快照", source="official_news",
                       url=url, published_at=now(), pages=[(None, text)], raw_path=path,
                       quality="headlines_only", cloud_allowed=True)
    store.record_attempt('official_news','MARKET','OK',run_id=run_id)
    count, errors = 0, []
    for article_url, title in links[:fulltext_limit]:
        try:
            article_raw = fetch(article_url, max_bytes=3000000)
            article = ArticleParser()
            article.feed(article_raw.decode('utf-8'))
            article_text = '\n'.join(article.parts)
            if len(article_text) < 100:
                raise ValueError('正文太短或动态加载')
            dates = re.findall(r'20\d{2}-\d{2}-\d{2}', article_text)
            pub = datetime.fromisoformat(dates[0]).replace(tzinfo=SH).isoformat() if dates else now()
            store.add_document(symbol='MARKET', kind='news', title=title, source='official_news',
                url=article_url, published_at=pub, time_precision='date' if dates else 'second',
                pages=[(None, article_text)], raw_path=store.raw(article_raw,'.html'),
                quality='html_text_navigation_unverified', cloud_allowed=True)
            count += 1
            store.record_attempt('news_article','MARKET','OK',resource_key=article_url,title=title,run_id=run_id)
        except Exception as exc:
            errors.append(type(exc).__name__)
            store.record_attempt('news_article','MARKET','FAILED',str(exc),resource_key=article_url,title=title,run_id=run_id)
    store.check(run_id, "official_news", "MARKET", "PARTIAL", f"{len(links)}条标题，已取得{count}篇网页全文；只覆盖指定官方栏目，含导航未核验；错误{errors}",track=False)
