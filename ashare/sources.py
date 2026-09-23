from __future__ import annotations

import html
import io
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from .finance import cents
from .storage import now, digest, normalize_time

SH = ZoneInfo("Asia/Shanghai")
ALLOWED_HOSTS = {"qt.gtimg.cn", "web.ifzq.gtimg.cn", "www.cninfo.com.cn", "static.cninfo.com.cn", "www.csrc.gov.cn", "www.sse.com.cn",
                 "news.un.org", "www.mofcom.gov.cn", "datacenter-web.eastmoney.com", "data.eastmoney.com"}


def check_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or p.hostname not in ALLOWED_HOSTS or p.username or p.password or p.port not in (None, 443):
        raise ValueError("只允许已配置的公开 HTTPS 数据源")


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, form=None, max_bytes=20_000_000):
    check_url(url)
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
    rows = []
    for symbol, body in re.findall(r'v_((?:sh|sz)\d{6})="([^"]*)"', raw.decode("gb18030")):
        if symbol not in expected:
            continue
        v = body.split("~")
        if len(v) < 38 or v[2] != symbol[2:]:
            raise ValueError("行情字段结构或证券标识不一致")
        observed = datetime.strptime(v[30], "%Y%m%d%H%M%S").replace(tzinfo=SH)
        if observed > datetime.now(SH) + timedelta(minutes=2):
            raise ValueError("行情时间在未来")
        price, prev = cents(v[3]), cents(v[4])
        if min(price, prev) <= 0:
            raise ValueError("行情价格无效/可能停牌，需复核")
        rows.append({"symbol": symbol, "name": v[1], "price_cents": price, "prev_close_cents": prev,
                     "observed_at": normalize_time(observed.isoformat())})
    if {r["symbol"] for r in rows} != set(expected):
        raise ValueError("行情缺少请求证券")
    return rows


def collect_quotes(store, run_id, symbols):
    try:
        raw = fetch("https://qt.gtimg.cn/q=" + ",".join(symbols), max_bytes=100000)
        path = store.raw(raw, ".txt")
    except Exception as exc:
        for symbol in symbols:store.record_attempt('tencent_quotes',symbol,'FAILED',str(exc),run_id=run_id)
        raise
    errors=[]
    for symbol in symbols:
        try:
            row = parse_quotes(raw,[symbol])[0]
            with store.db:
                store.db.execute("INSERT OR IGNORE INTO quotes VALUES(?,?,?,?,?,?,?,?,?)",
                             (digest(json.dumps(row, sort_keys=True))[:24], row["symbol"], row["name"], row["price_cents"], row["prev_close_cents"], row["observed_at"], now(), "tencent_public_research", path))
            store.record_attempt('tencent_quotes',symbol,'OK',run_id=run_id)
        except Exception as exc:
            store.record_attempt('tencent_quotes',symbol,'FAILED',str(exc),run_id=run_id);errors.append(symbol)
    if errors:raise ValueError('部分股票行情无法读取：'+','.join(errors))
    store.check(run_id, "tencent_quotes", None, "OK", f"{len(symbols)}只；公开研究行情，延迟与商用授权未验证，不能用于实盘执行")


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


def collect_history(store, run_id, symbol,config=None):
    from .calendar import completed_bar_cutoff
    cutoff=completed_bar_cutoff(now())
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=" + symbol + ",day,,,90,qfq"
    raw = fetch(url, max_bytes=2000000)
    path = store.raw(raw, ".json")
    summary = history_summary(raw, symbol, cutoff)
    # Executable price plans use a separate unadjusted sequence; never mix qfq prices into orders.
    plain_raw = fetch("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=" + symbol + ",day,,,90,", max_bytes=2000000)
    plain_path = store.raw(plain_raw, '.json')
    bars = json.loads(plain_raw)['data'][symbol]['day']
    completed = sorted([b for b in bars if b[0] < cutoff], key=lambda b:b[0])
    if len(completed) < 60 or len({b[0] for b in completed}) != len(completed):
        raise ValueError('未复权日线不足或重复')
    prices = [cents(b[2]) for b in completed]
    if any(p <= 0 for p in prices):
        raise ValueError('未复权价格无效')
    summary['unadjusted'] = {'last_complete_date': completed[-1][0], 'close_cents': prices[-1],
        'ma20_cents': sum(prices[-20:]) // 20, 'ma60_cents': sum(prices[-60:]) // 60,
        'raw_path': plain_path, 'bars': completed, 'basis': 'UNADJUSTED',
        'series_hash': digest(plain_raw)}
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
    store.check(run_id, "tencent_daily", symbol, "OK", "历史日线与原始价格均已核对；排除当日未完成K线；非执行行情")


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
