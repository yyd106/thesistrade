from __future__ import annotations
from .universe import company_targets

import fcntl
import html
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import sources
from .finance import PaperLedger, withdrawal_tier
from .model import run_codex
from .storage import Store, now, digest, json_write, atomic_write


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    if config.get("mode") not in ("research", "paper") or config.get("live_execution_enabled") is not False or config.get("paid_api_fallback") is not False:
        raise ValueError("此版本仅支持research/paper；不包含实盘或付费API路径")
    symbols = [i["symbol"] for i in config["watchlist"]]
    if not symbols or len(symbols) > 50 or len(set(symbols)) != len(symbols) or any(not re.fullmatch(r"(?:sh|sz)\d{6}", s) for s in symbols):
        raise ValueError("自选股格式/数量不合法")
    config["data_dir"] = str((path.parent / config["data_dir"]).resolve())
    config["config_path"] = str(path)
    import os
    for env,key in (("THESISTRADE_ROLE","deployment_role"),("THESISTRADE_DATA_DIR","data_dir"),("THESISTRADE_PUBLIC_ORIGIN","public_origin"),("THESISTRADE_SYNC_KEY_ID","sync_key_id"),("THESISTRADE_SYNC_PUBLIC_KEY","sync_public_key")):
        if os.environ.get(env):config[key]=os.environ[env]
    if config.get('deployment_role')=='cloud' and os.environ.get('RAILWAY_PUBLIC_DOMAIN') and not config.get('public_origin'):
        config['public_origin']='https://'+os.environ['RAILWAY_PUBLIC_DOMAIN']
    if config.get('deployment_role')=='cloud':
        config.update(model_enabled=False,slot_execution_mode='RULES',background_market_enabled=True)
        config['portfolio_strategy']='cross_research_v1'
        config['investment_policy']='days_cash_v1'
    if config.get('deployment_role') in ('cloud','research'):
        config['portfolio_authorization_hours']=12
    from .settings import validate_settings
    validate_settings(config)
    from .model import configure
    configure(config)
    return config


def collect(store, run_id, config, on_ready=None):
    def attempt(source, symbol, function):
        print("采集 " + source + (" " + symbol if symbol else ""), flush=True)
        try:
            return function()
        except Exception as e:
            detail = type(e).__name__ + ": " + str(e)[:350]
            store.check(run_id, source, symbol, "FAILED", detail,track=source!='tencent_quotes')
            print("来源异常 " + source + ": " + detail, flush=True)
            return None
    from .inbox import import_inbox
    attempt('report_inbox',None,lambda:import_inbox(store,config,run_id))
    attempt("tencent_quotes", None, lambda: sources.collect_quotes(store, run_id, [i["symbol"] for i in company_targets(store,config)], config))
    catalog = attempt("cninfo_stock_catalog", None, lambda: sources.stock_catalog(store))
    if catalog is not None:store.check(run_id,'cninfo_stock_catalog',None,'OK','股票目录已读取')
    attempt("official_news", "MARKET", lambda: sources.collect_news(store, run_id, config["news_url"]))
    if config.get('external_news_enabled',True):
        from .external_news import collect as collect_external
        attempt('external_news','MARKET',lambda:collect_external(store,run_id,config))
    from . import market_context, fundamentals
    attempt('market_comparison','MARKET',lambda:market_context.collect_comparisons(store,run_id,config))
    from .connectivity import check as online_or_stop
    import time
    targets=company_targets(store,config)
    if config.get('industry_enabled'):
        targets=sorted(targets,key=lambda i:(not i.get('protected'),store.db.execute("SELECT coalesce(max(ready_at),'') FROM batch_stocks WHERE symbol=?",(i['symbol'],)).fetchone()[0],i['symbol']))
    for item in targets:
        if config.get('_deadline') and time.monotonic()>=config['_deadline']:
            store.check(run_id,'research_budget',item['symbol'],'PARTIAL','本轮预算结束；已完成公司保存，下轮优先补齐')
            break
        # Stop before the next stock once the machine has been offline for a while; research is redone after reconnect.
        online_or_stop(store)
        symbol = item["symbol"]
        attempt("tencent_daily", symbol, lambda: sources.collect_history(store, run_id, symbol,config))
        attempt('financials',symbol,lambda:fundamentals.collect(store,run_id,symbol))
        if catalog is not None:
            attempt("cninfo_catalog", symbol, lambda: sources.collect_announcements(store, run_id, item, config, catalog))
        else:
            store.check(run_id, "cninfo_catalog", symbol, "FAILED", "股票目录不可用，不能确认公告范围")
        if on_ready:
            on_ready(item)


def build_packet(store, run_id, config, as_of):
    stocks, evidence = [], {}
    for item in company_targets(store,config,as_of):
        symbol = item["symbol"]
        quote = store.latest_quote(symbol, as_of)
        if quote:
            age = (datetime.fromisoformat(as_of) - datetime.fromisoformat(quote["observed_at"])).total_seconds()
            quote["age_seconds"] = round(age)
            quote["freshness"] = "RECENT" if 0 <= age <= 600 else "STALE"
        docs = store.documents_as_of(as_of, symbol)
        # 公告清单独立于Top K；这里是已抓取范围，不宣称全市场/全部历史覆盖。
        metadata = [d for d in docs if d["kind"] == "announcement_metadata"]
        hits = store.search("营业收入 净利润 经营活动现金流 风险 诉讼 回购 减持 分红 业绩 负债", as_of, symbol, limit=60, cloud_only=True)
        selected, counts = [], {}
        for hit in hits:
            key = hit["doc_id"]
            if hit["extraction_quality"] == "metadata_only" or counts.get(key, 0) >= 3:
                continue
            selected.append(hit)
            counts[key] = counts.get(key, 0) + 1
            if len(selected) >= 9:
                break
        # 无关键词命中的最新原文和标题也能作为有限证据，避免错误报告“没有新公告”。
        for d in docs[:4]:
            if not d["cloud_allowed"]:
                continue
            chunk = store.db.execute("SELECT * FROM chunks WHERE doc_id=? ORDER BY ordinal LIMIT 1", (d["id"],)).fetchone()
            if chunk:
                selected.append({"evidence_id": chunk["id"], "doc_id": d["id"], "page": chunk["page"], "text": chunk["text"],
                                 **{k: d[k] for k in ("symbol", "kind", "title", "url", "published_at", "first_seen_at", "available_at", "extraction_quality")}})
        for e in selected:
            evidence[e["evidence_id"]] = e
        features = None
        if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='market_features'").fetchone():
            f = store.db.execute("SELECT * FROM market_features WHERE symbol=? AND created_at<=? ORDER BY created_at DESC LIMIT 1", (symbol, as_of)).fetchone()
            if f:
                features = json.loads(f["payload"])
                features["first_seen_at"] = f["created_at"]
                features["raw_path"] = f["raw_path"]
        stocks.append({"symbol": symbol, "name": item["name"], "quote": quote, "features": features,
                       "complete_strategy_score": None,
                       "announcement_catalog": [{k: d[k] for k in ("id", "title", "url", "published_at", "extraction_quality")} for d in metadata[:100]],
                       "catalog_count": len(metadata), "catalog_truncated": len(metadata) > 100})
    checks = [dict(r) for r in store.db.execute("SELECT source,symbol,status,detail,checked_at FROM source_checks WHERE run_id=? ORDER BY id", (run_id,))]
    if any(c["source"] == "cache_only" for c in checks):
        cached = store.db.execute("SELECT id FROM runs WHERE id!=? AND as_of<=? AND EXISTS(SELECT 1 FROM source_checks s WHERE s.run_id=runs.id AND s.source='tencent_quotes') ORDER BY as_of DESC LIMIT 1", (run_id, as_of)).fetchone()
        if cached:
            checks.extend({**dict(r), "cached_from_run": cached["id"]} for r in store.db.execute("SELECT source,symbol,status,detail,checked_at FROM source_checks WHERE run_id=?", (cached["id"],)))
    account = store.db.execute("SELECT * FROM paper_accounts WHERE id='DEMO_PAPER'").fetchone()
    stage = account["current_stage"] if account else 1
    threshold, retain = withdrawal_tier(stage)
    prior = store.db.execute("SELECT r.as_of,s.result_json FROM research s JOIN runs r ON r.id=s.run_id WHERE s.model_status='SUCCEEDED' AND r.as_of<=? ORDER BY r.as_of DESC LIMIT 1", (as_of,)).fetchone()
    previous = None
    if prior:
        p = json.loads(prior["result_json"])
        previous = {"as_of": prior["as_of"], "summary": p["summary"], "next_checks": [{"symbol": s["symbol"], "next_checks": s["next_checks"]} for s in p["stocks"]], "evidence_status": "历史模型观点，只作研究线索，不是独立事实来源"}
    task = store.db.execute("SELECT kind FROM runs WHERE id=?", (run_id,)).fetchone()
    return {"schema_version": "0.1", "run_id": run_id, "kind": task["kind"] if task else "manual", "as_of": as_of, "mode": "research", "watchlist_kind": config["watchlist_kind"],
            "previous_research": previous,
            "account": {"actual_broker_account": "NOT_CONNECTED", "paper_initial_cny": account["initial_cents"] / 100 if account else None, "paper_label": "独立演示模拟现金，非实际入金", "stage": stage, "threshold_cny": threshold / 100, "retain_cny": retain / 100},
            "retrieval": "cn-bigram-v1 / SQLite FTS5；尚未启用语义向量与OCR", "source_checks": checks,
            "stocks": stocks, "evidence": list(evidence.values()),
            "limitations": ["公开研究行情延迟与授权范围未验证", "公告正文仅限额下载，清单扫描不等于全文审阅", "PDF表格布局尚未人工核对", "券商付费研报未接入", "结构化财务、估值和实盘规则未接齐", "当前复权快照不能用于无未来信息的历史回测"]}


def fallback(packet, reason):
    return {"summary": "本轮完成资料整理，模型分析尚未完成：" + reason,
            "stocks": [{"symbol": s["symbol"], "action": "INSUFFICIENT_DATA", "analysis": "等待模型研究和数据补齐。此处为程序生成的状态说明。",
                        "facts": [], "counterpoints": ["未完成完整策略验证"], "missing_fields": packet["limitations"],
                        "next_checks": ["查看来源健康状态", "补齐结构化财务与估值"]} for s in packet["stocks"]]}


def report(store, folder, packet, result, model_status):
    def clean(s):
        return str(s).replace("|", "／").replace("\n", " ")
    title = {"manual": "研究报告", "hourly": "小时研究检查", "daily_research": "每日研究", "daily_review": "每日研究复盘"}.get(packet.get("kind", "manual"), "研究报告")
    local_as_of = datetime.fromisoformat(packet["as_of"]).astimezone(sources.SH).strftime("%Y-%m-%d %H:%M:%S")
    lines = ["# A股 Agent " + title, "", f"截止时间：{local_as_of}（北京时间）  ", f"运行编号：{packet['run_id']}  ",
             f"模式：研究 · 演示自选股 · 模型状态：{model_status}", "", "这两只股票仅用于验证系统流程，不代表投资推荐。东方财富证券账户未连接；模拟本金10万元为虚拟现金，无真实下单或转账。", "", "## 本轮结论", "", result["summary"], "", "## 行情与数据状态", "",
             "| 股票 | 最新价（元） | 较昨收 | 行情时间（北京时间） | 新鲜度 | 公告清单 |", "| --- | --- | --- | --- | --- | --- |"]
    for stock in packet["stocks"]:
        q = stock["quote"]
        if q:
            stamp = datetime.fromisoformat(q["observed_at"]).astimezone(sources.SH).strftime("%Y-%m-%d %H:%M:%S")
            pct = (q["price_cents"] / q["prev_close_cents"] - 1) * 100
            lines.append(f"| {stock['name']} {stock['symbol']} | {q['price_cents']/100:.2f} | {pct:+.2f}% | {stamp} | {q['freshness']} | {stock['catalog_count']}条 |")
        else:
            lines.append(f"| {stock['name']} {stock['symbol']} | 缺失 | — | — | UNKNOWN | {stock['catalog_count']}条 |")
    evidence = {e["evidence_id"]: e for e in packet["evidence"]}
    names = {s["symbol"]: s["name"] for s in packet["stocks"]}
    for stock in result["stocks"]:
        lines += ["", "## " + names[stock["symbol"]] + " · " + stock["action"], "", stock["analysis"], "", "证据摘录：", ""]
        for fact in stock["facts"]:
            e = evidence[fact["evidence_id"]]
            lines += ["> " + fact["quote"].replace("\n", "\n> "), "", f"来源：[{clean(e['title'])}]({e['url']})，页码：{e['page'] or '标题/网页'}，证据ID：`{e['evidence_id']}`", ""]
        if not stock["facts"]:
            lines.append("本轮无通过校验的模型事实摘录。")
        lines += ["", "反证与不确定性：", ""] + ["- " + x for x in stock["counterpoints"]]
        lines += ["", "缺失数据：", ""] + ["- " + x for x in stock["missing_fields"]]
        lines += ["", "下一步检查：", ""] + ["- " + x for x in stock["next_checks"]]
    lines += ["", "## 利润提取规则", "", "初始预算10万元，盈利可再投资。阈值／留存：15／12、20／15、30／20、40／30、50／40……万元。严格超过阈值才触发，按最新资产提取至留存值；实际转出并对账后才推进档位。", "", "当前实际账户净资产未知，不产生真实提取计划。", "", "## 来源覆盖与失败", "", "| 来源 | 股票 | 状态 | 说明 |", "| --- | --- | --- | --- |"]
    lines += [f"| {c['source']} | {c['symbol'] or '全部'} | {c['status']} | {clean(c['detail'])}；检查于{c['checked_at']} {'（历史缓存）' if c.get('cached_from_run') else ''} |" for c in packet["source_checks"]]
    lines += ["", "## 实现边界", ""] + ["- " + x for x in packet["limitations"]]
    lines += ["- 检索当前使用中文双字词法索引；语义向量检索、OCR和完整交易模拟撮合尚未实现。", "- 模型引文已校验为给定原文中的连续文字；分析和推测仍需人工判断。", "", "## 本次保存的公告清单", ""]
    for stock in packet["stocks"]:
        lines += ["### " + stock["name"], ""]
        lines += [f"- [{clean(d['title'])}]({d['url']}) · {datetime.fromisoformat(d['published_at']).astimezone(sources.SH).date().isoformat()}" for d in stock["announcement_catalog"]]
    text = "\n".join(lines) + "\n"
    atomic_write(folder / "report.md", text)
    atomic_write(store.root / "reports" / "latest.md", text)
    # 自包含HTML只显示转义文字，不允许来源正文注入HTML或脚本。
    page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>A股研究报告</title><style>body{margin:0;background:#f4f6f8;color:#192330;font:16px/1.8 system-ui}main{max-width:1000px;margin:32px auto;padding:36px;background:white;border-radius:16px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}h1{margin:0;color:#174b48}.tag{color:#667;font-size:14px}</style><main><h1>Dean · A股研究 Agent</h1><p class="tag">本机研究原型 · 演示数据范围 · 无实盘连接</p><pre>' + html.escape(text) + '</pre></main></html>'
    atomic_write(folder / "report.html", page)
    atomic_write(store.root / "reports" / "latest.html", page)
    return str(folder / "report.md")


def run(config, *, kind="manual", job_key=None, use_model=True, collect_data=True):
    store = Store(config["data_dir"])
    lock = (store.root / "run.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        store.close()
        lock.close()
        return {"status": "BUSY", "detail": "已有任务运行，没有启动重复任务"}
    run_id = uuid.uuid4().hex
    job_key = job_key or "manual:" + run_id
    try:
        prior = store.db.execute("SELECT * FROM runs WHERE job_key=?", (job_key,)).fetchone()
        if prior and prior["status"] in ("SUCCEEDED", "PARTIAL"):
            return {"status": "ALREADY_DONE", "run_id": prior["id"], "report": prior["report_path"]}
        # 文件锁已取得，遗留RUNNING来自中断；保留记录并允许同任务重新执行。
        store.db.execute("UPDATE runs SET status='INTERRUPTED',error='检测到上轮中断',finished_at=? WHERE status='RUNNING'", (now(),))
        if prior:
            store.db.execute("UPDATE runs SET job_key=? WHERE id=?", (job_key + ":attempt:" + prior["id"], prior["id"]))
        store.db.execute("INSERT INTO runs(id,job_key,kind,started_at,status) VALUES(?,?,?,?,'RUNNING')", (run_id, job_key, kind, now()))
        store.db.commit()
        PaperLedger(store).initialize()
        folder = store.root / "reports" / run_id
        folder.mkdir(parents=True)
        if collect_data:
            collect(store, run_id, config)
        else:
            store.check(run_id, "cache_only", None, "PARTIAL", "仅使用已保存资料；本轮未刷新外部来源")
        as_of = now()
        packet = build_packet(store, run_id, config, as_of)
        json_write(folder / "packet.json", packet)
        json_write(folder / "config-snapshot.json", config)
        if use_model and config["model_enabled"]:
            print("资料包已保存，使用ChatGPT订阅运行Codex分析", flush=True)
            try:
                result = run_codex(packet, folder / "model", config["model_timeout_seconds"])
                model_status = "SUCCEEDED"
            except Exception as e:
                model_status = "DEFERRED"
                result = fallback(packet, type(e).__name__ + ": " + str(e))
                print(result["summary"], flush=True)
        else:
            model_status = "NOT_RUN"
            result = fallback(packet, "本次未启用模型")
        json_write(folder / "research.json", result)
        report_path = report(store, folder, packet, result, model_status)
        source_gaps = any(c["status"] != "OK" for c in packet["source_checks"])
        status = "PARTIAL" if source_gaps or model_status != "SUCCEEDED" else "SUCCEEDED"
        with store.db:
            store.db.execute("INSERT INTO research VALUES(?,?,?,?,?)", (run_id, now(), digest(json.dumps(packet, ensure_ascii=False, sort_keys=True)), json.dumps(result, ensure_ascii=False), model_status))
            store.db.execute("UPDATE runs SET as_of=?,finished_at=?,status=?,model_status=?,report_path=? WHERE id=?", (as_of, now(), status, model_status, report_path, run_id))
        store.backup()
        return {"status": status, "run_id": run_id, "model_status": model_status, "report": report_path,
                "documents": store.db.execute("SELECT count(*) FROM documents").fetchone()[0],
                "chunks": store.db.execute("SELECT count(*) FROM chunks").fetchone()[0]}
    except BaseException as e:
        store.db.rollback()
        store.db.execute("UPDATE runs SET status='FAILED',finished_at=?,error=? WHERE id=?", (now(), type(e).__name__ + ": " + str(e)[:500], run_id))
        store.db.commit()
        raise
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
        store.close()
