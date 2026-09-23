from __future__ import annotations

import argparse
import json
import platform
import sqlite3
from datetime import datetime
from pathlib import Path

from .pipeline import load_config, run
from .sources import pdf_pages
from .storage import Store, now


def main():
    parser = argparse.ArgumentParser(description="Dean A股本地研究与模拟交易闭环（无实盘功能）")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    once = sub.add_parser("run", help="采集并生成一轮研究报告")
    once.add_argument("--without-model", action="store_true")
    once.add_argument("--cache-only", action="store_true")
    once.add_argument("--job-key", help="重复任务去重键；同一键已完成时不重复运行")
    once.add_argument("--kind", choices=["manual", "hourly", "daily_research", "daily_review"], default="manual")
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("backup")
    for name in ('collect','cycle','research','slot','review','settle'):
        p=sub.add_parser(name)
        p.add_argument('--without-model',action='store_true')
        p.add_argument('--job-key')
        if name=='research':
            p.add_argument('--batch-id');p.add_argument('--symbol')
        if name=='review':p.add_argument('--end',help='已结束窗口的截止时间ISO8601；回看此前24小时')
    server=sub.add_parser('serve',help='启动本地控制台与持久化定时调度')
    server.add_argument('--port',type=int)
    service=sub.add_parser('service',help='管理本项目的macOS登录自启动服务')
    service.add_argument('action',choices=['install','status','restart','uninstall'])
    sub.add_parser('demo',help='在独立目录用明确标注的合成数据验证完整闭环')
    password=sub.add_parser('reset-password',help='在本机重设共享账号密码并撤销其已登录会话')
    password.add_argument('username',choices=['admin','guest'])
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--symbol")
    search.add_argument("--as-of", default=None, help="ISO8601，必须有时区")
    search.add_argument("--limit", type=int, default=5)
    imp = sub.add_parser("import", help="导入有权使用的本地研报/新闻；默认不发给云端模型")
    imp.add_argument("file")
    imp.add_argument("--symbol", required=True)
    imp.add_argument("--title", required=True)
    imp.add_argument("--published-at", required=True)
    imp.add_argument("--kind", choices=["broker_report", "news", "company_report"], default="broker_report")
    imp.add_argument("--allow-cloud", action="store_true", help="仅在资料许可允许云端处理时指定")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command=='reset-password':
        import getpass
        from .auth import reset_password
        first=getpass.getpass('输入新密码（至少12个字符，输入不显示）：')
        if first!=getpass.getpass('再次输入：'):parser.error('两次密码不一致，未修改。')
        store=Store(config['data_dir'])
        try:reset_password(store,args.username,first)
        finally:store.close()
        print('密码已更新，该账号所有设备需要重新登录。');return
    if args.command=='serve':
        from .dashboard import serve
        serve(config['config_path'],args.port);return
    if args.command=='service':
        from .service import manage
        print(json.dumps(manage(config,args.action),ensure_ascii=False,indent=2));return
    if args.command=='demo':
        from .demo import run_demo
        result=run_demo(config)
    elif args.command in ('collect','cycle','research','slot','review','settle'):
        from .workflow import execute
        result=execute(config,args.command,use_model=not args.without_model,key=args.job_key,
            batch_id=getattr(args,'batch_id',None),symbol=getattr(args,'symbol',None),end=getattr(args,'end',None))
    elif args.command == "doctor":
        from .model import auth_status,codex_executable
        from .calendar import CALENDAR_VERSION,trading_day,local
        result = {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                  "config": config["config_path"], "data_dir": config["data_dir"], "mode": config['mode'], "real_broker": "NOT_CONNECTED",
                  'codex_auth':auth_status(),'api_key_required':False,'calendar':CALENDAR_VERSION,
                  'calendar_current_year_supported':trading_day(local(now()).date()) is not None,
                  'scheduler_enabled_in_config':config['scheduler_enabled'],'ui_url':'http://127.0.0.1:'+str(config['ui_port'])}
    elif args.command == "run":
        result = run(config, kind=args.kind, job_key=args.job_key, use_model=not args.without_model, collect_data=not args.cache_only)
    else:
        store = Store(config["data_dir"])
        try:
            if args.command == "status":
                from .dashboard import status
                result=status(config)
            elif args.command == "search":
                result = store.search(args.query, args.as_of or now(), args.symbol, limit=min(max(args.limit, 1), 30))
            elif args.command == "backup":
                result = {"backup": str(store.backup())}
            else:
                file = Path(args.file).resolve()
                if file.stat().st_size > 20_000_000:
                    raise ValueError("文件超过20MB，需拆分")
                raw = file.read_bytes()
                if file.suffix.lower() == ".pdf":
                    pages = pdf_pages(raw)
                    quality = "pdf_text_layout_unverified"
                elif file.suffix.lower() in (".txt", ".md"):
                    pages = [(None, raw.decode("utf-8"))]
                    quality = "user_supplied_text"
                else:
                    raise ValueError("仅支持PDF、TXT、Markdown；扫描PDF需先OCR")
                if args.symbol not in [x["symbol"] for x in config["watchlist"]] + ["MARKET"]:
                    raise ValueError("证券不在自选股内")
                path = store.raw(raw, file.suffix.lower())
                doc_id, created = store.add_document(symbol=args.symbol, kind=args.kind, title=args.title, source="user_import",
                                                     url=file.as_uri(), published_at=args.published_at, pages=pages,
                                                     raw_path=path, quality=quality, cloud_allowed=args.allow_cloud)
                result = {"document_id": doc_id, "new": created, "cloud_allowed": args.allow_cloud}
        finally:
            store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
