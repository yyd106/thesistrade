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
    sub.add_parser('model-check', help='用最小请求验证订阅登录、固定模型与推理强度，并记录实际运行的模型')
    ev=sub.add_parser('evaluate', help='给到期的结论打分并推进对照账本（不调用模型）')
    ev.add_argument('--backfill', action='store_true', help='先把注册表上线前的历史研究、组合与全球判断补登记（按原时间）')
    sub.add_parser('weekly-report', help='生成周度评估报告（不调用模型）')
    sub.add_parser('schedule', help='显示周期任务与下一次运行时间')
    prop = sub.add_parser('proposals', help='变更提案：list/show/new/decide')
    prop.add_argument('action', choices=['list', 'show', 'new', 'decide'])
    prop.add_argument('id', nargs='?')
    prop.add_argument('--status');prop.add_argument('--file', help='new：提案JSON文件')
    prop.add_argument('--supersedes', action='append', help='new：被这份新提案替代的旧提案编号，可重复；旧提案标为SUPERSEDED')
    prop.add_argument('--to', help='decide：READY/APPROVED/REJECTED/ADOPTED/RETIRED/SUPERSEDED')
    prop.add_argument('--replaced-by', help='decide --to SUPERSEDED 时必填：替代它的新版提案编号')
    prop.add_argument('--approved-by', help='批准、上线、撤下时必填：用户本人确认的记录')
    prop.add_argument('--note', help='决定理由或实施说明')
    iss = sub.add_parser('issues', help='工程问题：list/new/resolve/retitle')
    iss.add_argument('action', choices=['list', 'new', 'resolve', 'retitle']);iss.add_argument('id', nargs='?')
    iss.add_argument('--status', default='OPEN');iss.add_argument('--note');iss.add_argument('--wontfix', action='store_true')
    iss.add_argument('--key', help='new：问题类型，如 OTHER_DATA、OTHER_SYSTEM、OTHER_EXECUTION、MISSING_DAILY_BARS')
    iss.add_argument('--symbol', help='new：相关代码，全市场问题不填')
    iss.add_argument('--title', help='new：一句话标题，同类型、同代码、同标题的再次登记只累加次数；retitle：新标题')
    iss.add_argument('--detail', help='new：现象与依据');iss.add_argument('--evidence', action='append', help='new：证据位置，可重复')
    sub.add_parser('guidance', help='列出已上线的研究规则')
    evc = sub.add_parser('event-check', help='按当前规则重放各股的公司行为核验：哪些公告仍阻挡买入、原因是什么（只读，不调用模型）')
    evc.add_argument('--symbol', action='append', help='只查指定代码，可重复；默认全部自选股')
    dg = sub.add_parser('digest', help='运行日报：一页汇总当天的抓取、研究、组合策略、执行、复盘与调整（程序生成，不调用模型）')
    dg.add_argument('--date', help='某一天（YYYY-MM-DD，北京时间），默认今天')
    dg.add_argument('--since', help='汇总区间起点（YYYY-MM-DD），生成多日汇总')
    dg.add_argument('--until', help='汇总区间终点（YYYY-MM-DD），默认今天')
    maint = sub.add_parser('maintenance', help='存储维护：清理已发送的发布包正文、磁盘状态、可选整理数据库')
    maint.add_argument('--vacuum', action='store_true', help='整理数据库文件（需先停止服务）')
    maint.add_argument('--list-publications', action='store_true', help='列出旧版本留下的 publication.json 副本')
    maint.add_argument('--remove-publications', action='store_true', help='删除旧 publication.json 副本（保留哈希清单）')
    maint.add_argument('--older-than-days', type=int, default=1)
    bt = sub.add_parser('backtest', help='机械规则离线回测：fetch/run/fetch-global/run-global')
    bt.add_argument('action', choices=['fetch', 'run', 'fetch-global', 'run-global'])
    bt.add_argument('--universe', help='CSV: symbol,start,end（当时的成分股区间）')
    bt.add_argument('--start', default='2015-01-01');bt.add_argument('--end', default=None)
    bt.add_argument('--variant', action='append', help='只跑指定变体，可重复')
    cfgp = sub.add_parser('config', help='查看或修改设置（按类别校验并留痕）')
    cfgp.add_argument('action', choices=['show', 'set'])
    cfgp.add_argument('pairs', nargs='*', help='set：key=value，value 按JSON解析')
    cfgp.add_argument('--reason');cfgp.add_argument('--approved-by')
    args = parser.parse_args()
    config = load_config(args.config)
    handled = extended(args, config)
    if handled is not None:
        print(json.dumps(handled, ensure_ascii=False, indent=2));return
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
        import sys,os
        from .model import auth_status,codex_executable,pinned
        from .calendar import CALENDAR_VERSION,trading_day,local,next_year_warning,supported_years
        from .build import info
        executable=os.path.realpath(sys.executable)
        result = {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                  'python_executable':executable,
                  'python_warning':'运行环境来自ChatGPT应用自带的Python，应用升级可能移走它；建议按运维手册改用独立安装的Python重建.venv' if 'codex-runtimes' in executable else None,
                  "config": config["config_path"], "data_dir": config["data_dir"], "mode": config['mode'], "real_broker": "NOT_CONNECTED",
                  'codex_auth':auth_status(),'api_key_required':False,'model_pinned':pinned(),'build':info(config),
                  'calendar':CALENDAR_VERSION,'calendar_years':supported_years(),
                  'calendar_current_year_supported':trading_day(local(now()).date()) is not None,
                  'calendar_next_year_warning':next_year_warning(now()),
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


def extended(args, config):
    """Operator and desktop-agent commands. Returns None for commands handled by main()."""
    command = args.command
    if command == 'model-check':
        from .model import check
        from datetime import datetime, timezone
        folder = Path(config['data_dir']) / 'workflow' / 'model-check' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        try:
            return check(folder, min(180, config.get('model_timeout_seconds', 240)))
        except Exception as exc:
            from .model import call_meta, pinned
            return {'status': 'FAILED', 'error': str(exc)[:500], 'pinned': pinned(), 'meta': call_meta(folder)}
    if command in ('evaluate', 'weekly-report'):
        from .workflow import execute
        extra = {}
        if command == 'evaluate' and args.backfill:
            from .evaluation import backfill
            store = Store(config['data_dir'])
            try:extra['backfill'] = backfill(store, config)
            finally:store.close()
        result = execute(config, 'evaluate' if command == 'evaluate' else 'weekly_report', use_model=False)
        return {**extra, **result} if extra else result
    if command == 'digest':
        from .digest import write, rollup
        store = Store(config['data_dir'])
        try:
            return rollup(store, config, args.since, args.until) if args.since else write(store, config, args.date)
        finally:store.close()
    if command == 'schedule':
        from .reporting import next_runs
        keys = ('collection_times', 'review_time', 'slot_times', 'evaluation_time', 'weekly_report_weekday', 'weekly_report_time', 'digest_time',
                'plan_max_age_hours', 'research_reuse_hours', 'portfolio_refresh_minutes', 'dynamic_enabled', 'scheduler_enabled')
        settings = {k: config.get(k) for k in keys}
        slots = sorted(config.get('slot_times', []))
        settings['slot_times'] = f"{len(slots)}个（{slots[0]}–{slots[-1]}，云端执行）" if slots else []
        return {'settings': settings, 'next_runs': next_runs(config, now()),
                'fixed': {'cloud_sync_seconds': 60, 'dynamic_cycle_minutes': 30, 'maintenance_minutes': 60, 'followups_seconds': 60}}
    if command == 'event-check':
        from .event_review import check
        store = Store(config['data_dir'])
        try:
            return check(store, config, args.symbol)
        finally:store.close()
    if command in ('proposals', 'issues', 'guidance', 'maintenance', 'backtest'):
        store = Store(config['data_dir'])
        try:
            return _store_command(args, config, store)
        finally:
            store.close()
    if command == 'config':
        from .config_ops import apply, classify
        if args.action == 'show':
            raw = json.loads(Path(config['config_path']).read_text())
            return {k: {'value': v, 'class': classify(k)} for k, v in sorted(raw.items()) if k != 'sync_key_file'}
        from .config_ops import parse_value
        changes = {}
        for pair in args.pairs:
            key, sep, value = pair.partition('=')
            if not sep:
                raise ValueError('请使用 key=value 形式')
            changes[key.strip()] = parse_value(value)
        return apply(config['config_path'], changes, reason=args.reason, approved_by=args.approved_by)
    return None


def _store_command(args, config, store):
    from . import governance
    command = args.command
    if command == 'guidance':
        return [dict(r) for r in store.db.execute("SELECT * FROM strategy_guidance ORDER BY adopted_at")]
    if command == 'issues':
        if args.action == 'list':
            return governance.issues(store, None if args.status == 'ALL' else args.status)
        if args.action == 'new':
            key = getattr(args, 'key', None) or 'OTHER_SYSTEM'
            if key not in governance.REVIEW_ISSUE_KEYS:
                raise ValueError('问题类型只能是：' + '、'.join(governance.REVIEW_ISSUE_KEYS) + '（程序检查类由复盘自动登记）')
            title, detail = (getattr(args, 'title', None) or '').strip(), (getattr(args, 'detail', None) or '').strip()
            if not title or not detail:
                raise ValueError('登记工程问题需要 --title 和 --detail')
            with store.db:
                iid = governance.record_issue(store, key, getattr(args, 'symbol', None), detail, ['source:agent'] + list(getattr(args, 'evidence', None) or []),
                                              now(), title=title[:120], dedupe=title[:120])
            row = store.db.execute('SELECT status,occurrences FROM engineering_issues WHERE id=?', (iid,)).fetchone()
            return {'status': row['status'], 'id': iid, 'occurrences': row['occurrences']}
        if not args.id:
            raise ValueError('需要工程问题编号')
        if args.action == 'retitle':
            return governance.retitle_issue(store, args.id, getattr(args, 'title', None), args.note)
        governance.resolve_issue(store, args.id, args.note, status='WONTFIX' if args.wontfix else 'RESOLVED')
        return {'status': 'UPDATED', 'id': args.id}
    if command == 'proposals':
        if args.action == 'list':
            return [{k: p[k] for k in ('id', 'status', 'kind', 'target', 'title', 'created_at', 'decided_at', 'decided_by')} for p in governance.proposals(store, args.status)]
        if args.action == 'show':
            row = next((p for p in governance.proposals(store) if p['id'] == args.id), None)
            if not row:
                raise ValueError('未找到该提案')
            return row
        if args.action == 'new':
            spec = json.loads(Path(args.file).read_text(encoding='utf-8'))
            required = {'kind', 'target', 'title', 'hypothesis', 'change', 'evidence', 'test_plan', 'failure_criteria', 'rollback'}
            missing = required - set(spec)
            if missing:
                raise ValueError('提案缺少字段：' + '、'.join(sorted(missing)))
            old = list(dict.fromkeys(getattr(args, 'supersedes', None) or []))
            note = (args.note or '').strip() or '被完整新版替代'
            same = store.db.execute('SELECT id FROM strategy_proposals WHERE dedupe_key=?', (spec['dedupe_key'],)).fetchone() if spec.get('dedupe_key') else None
            for oid in old:  # check every old proposal before writing anything; the new id does not exist yet
                row = store.db.execute('SELECT status,decided_by FROM strategy_proposals WHERE id=?', (oid,)).fetchone()
                if not row:
                    raise ValueError(f'未找到提案 {oid}')
                if 'SUPERSEDED' not in governance.TRANSITIONS.get(row['status'], ()):
                    raise ValueError(f"提案 {oid} 的状态为{row['status']}，不能标为被新版替代")
                if row['status'] == 'REJECTED' and row['decided_by']:
                    raise ValueError(f"提案 {oid} 由 {row['decided_by']} 驳回，改标请用 decide --to SUPERSEDED 并写明批准人")
                if same and same['id'] == oid:
                    raise ValueError(f'提案文件的 dedupe_key 指向 {oid} 本身，不能替代它自己')
            if old and same:
                raise ValueError(f"提案文件的 dedupe_key 已被提案 {same['id']} 使用；用 --supersedes 登记新版时请换一个 dedupe_key 或去掉它")
            with store.db:
                pid = governance.draft_proposal(store, source=spec.get('source', 'agent'), kind=spec['kind'], target=spec['target'],
                                                title=spec['title'], payload={k: v for k, v in spec.items() if k not in ('kind', 'target', 'title', 'source')},
                                                at=now(), dedupe_key=spec.get('dedupe_key'))
            for oid in old:
                governance.decide(store, oid, 'SUPERSEDED', decided_by=None, note=f'{note}（新版 {pid}）', replaced_by=pid)
            return {'status': 'DRAFT', 'id': pid, 'supersedes': old}
        return governance.decide(store, args.id, args.to, decided_by=args.approved_by, note=args.note,
                                 replaced_by=getattr(args, 'replaced_by', None))
    if command == 'maintenance':
        from . import maintenance
        result = {'outbox_pruned': maintenance.prune_outbox(store), 'disk': maintenance.disk_status(store, config)}
        if args.list_publications or args.remove_publications:
            files = maintenance.publication_files(store, args.older_than_days)
            result['publications'] = {'count': len(files), 'bytes': sum(f.stat().st_size for f in files),
                                      'examples': [str(f.relative_to(store.root)) for f in files[:5]]}
        if args.remove_publications:
            result['removed'] = len(maintenance.remove_publication_files(store, args.older_than_days))
        if args.vacuum:
            result['vacuum'] = maintenance.vacuum(store)
        return result
    if command == 'backtest':
        from . import backtest
        from datetime import date
        end = args.end or date.today().isoformat()
        root = config['data_dir']
        if args.action == 'fetch':
            universe = backtest.load_universe(args.universe)
            done = {'sh000300': backtest.fetch_a_share(root, 'sh000300', args.start, end, index=True)}
            for member in universe:
                if member['symbol'] not in done:
                    try:done[member['symbol']] = backtest.fetch_a_share(root, member['symbol'], args.start, end)
                    except Exception as exc:done[member['symbol']] = 'FAILED: ' + str(exc)[:120]
            return {'fetched': done}
        if args.action == 'fetch-global':
            return {'fetched': {asset: backtest.fetch_global(root, asset) for asset in backtest.GLOBAL}}
        if args.action == 'run':
            result = backtest.run_ma(root, config, backtest.load_universe(args.universe), args.start, end, args.variant)
        else:
            result = backtest.run_global(root, config, args.start, end, args.variant)
        folder = backtest.save(root, result)
        return {'report': str(folder / 'report.md'), 'summary': {k: {'net': v['net'], 'excess': v['excess']} for k, v in result['variants'].items()},
                'missing_data': result.get('missing_data')}
    return None


if __name__ == "__main__":
    main()
