"""Daily evaluation job and the weekly evaluation report. Deterministic and model-free: the report is
the evidence a person or a desktop agent reads before proposing any strategy change."""
import json
from datetime import datetime, timedelta
from .storage import now, normalize_time, json_write
from .calendar import local, next_year_warning


def run_daily(store, config, at=None):
    """Score matured registry rows and advance the shadow books. Idempotent."""
    from .evaluation import score, registry_counts
    from .shadow import run as run_shadow
    at = normalize_time(at or now())
    result = {'at': at, 'registry': score(store, config, at), 'shadow': run_shadow(store, config, at), 'counts': registry_counts(store)}
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO service_state VALUES('evaluation_last',?)", (json.dumps(result, ensure_ascii=False),))
    json_write(store.root / 'workflow' / 'evaluation' / 'daily' / (local(at).date().isoformat() + '.json'), result)
    return result


def _pct(bps):
    return '—' if bps is None else f'{bps / 100:+.2f}%'


def collect(store, config, at=None, days=7):
    from .evaluation import comparisons, registry_counts
    from .shadow import summary
    from .governance import issues, proposals
    from .maintenance import disk_status
    at = normalize_time(at or now())
    since = normalize_time((datetime.fromisoformat(at) - timedelta(days=days)).isoformat())
    week_start = local(since).date().isoformat()
    builds = [json.loads(r[0]) | {'first_seen_at': r[1]} for r in store.db.execute('SELECT payload_json,first_seen_at FROM builds WHERE first_seen_at>=? ORDER BY first_seen_at', (since,))]
    attempts = {f"{r['source']}:{r['status']}": r['n'] for r in store.db.execute(
        "SELECT source,status,count(*) n FROM data_attempts WHERE checked_at>=? AND source IN ('research_analysis','review_analysis') GROUP BY source,status", (since,))}
    portfolio_runs = {r['status']: r['n'] for r in store.db.execute('SELECT status,count(*) n FROM portfolio_runs WHERE started_at>=? GROUP BY status', (since,))}
    renewed = store.db.execute("SELECT count(*) FROM plan_events WHERE at>=? AND reason LIKE 'RENEWED:%'", (since,)).fetchone()[0]
    return {'generated_at': at, 'window': {'from': since, 'to': at}, 'horizon_days': config.get('evaluation_horizon_days', 20),
            'registry': {'counts': registry_counts(store), 'all_time': comparisons(store, config), 'scored_this_week': store.db.execute(
                "SELECT count(*) FROM signal_registry WHERE status='SCORED' AND scored_at>=?", (since,)).fetchone()[0]},
            'shadow': {'all_time': summary(store, config), 'this_week': summary(store, config, week_start)},
            'builds_this_week': builds,
            'model_usage': {'attempts': attempts, 'portfolio_runs': portfolio_runs, 'research_renewals_without_model': renewed},
            'engineering_issues': [{k: i[k] for k in ('id', 'issue_key', 'category', 'symbol', 'title', 'occurrences', 'first_seen_at', 'last_seen_at')} for i in issues(store, 'OPEN')][:30],
            'proposals': {s: [{k: p[k] for k in ('id', 'kind', 'target', 'title', 'created_at')} for p in proposals(store, s)][:30]
                          for s in ('DRAFT', 'READY', 'APPROVED', 'ADOPTED')},
            'proposals_closed': closed_proposals(store),
            'disk': disk_status(store, config), 'calendar_warning': next_year_warning(at)}


def closed_proposals(store):
    """Closed proposals are counted, and a superseded one names its replacement, so that
    "rejected" is not read as "the hypothesis failed"."""
    from .governance import proposals
    count = lambda status: store.db.execute('SELECT count(*) FROM strategy_proposals WHERE status=?', (status,)).fetchone()[0]
    return {'REJECTED': count('REJECTED'), 'SUPERSEDED': count('SUPERSEDED'),
            'replacements': [{'id': p['id'], 'title': p['title'], 'replaced_by': p['payload'].get('superseded_by')}
                             for p in proposals(store, 'SUPERSEDED')][:30]}


def markdown(report):
    lines = [f"# 周度评估报告（{report['window']['from'][:10]} 至 {report['window']['to'][:10]}）", '',
             '本报告由程序生成，不含模型判断。结论前先看样本量：持有期不重叠的独立样本少于30个时，差异只能当线索，不能作为改动依据。', '']
    lines += ['## 结论注册表', '', f"评估期限：{report['horizon_days']}个交易日；本周新完成打分 {report['registry']['scored_this_week']} 条。", '',
              '| 比较项 | 分组 | 每日样本数 | 平均超额 | 独立样本数 | 独立样本平均超额 | 95%区间（独立） |', '|---|---|---|---|---|---|---|']
    names = {'trend_filter': '趋势过滤是否有效', 'research_veto': '研究否决是否有效（仅趋势成立样本）',
             'portfolio_allow': '组合放行是否有效（有选择余地的样本）', 'global_stance': '全球资产做多判断是否有效'}
    for key, groups in report['registry']['all_time']['groups'].items():
        for label, stats in groups.items():
            d, i = stats['daily'], stats['independent']
            ci = i.get('ci95_bps')
            lines.append(f"| {names.get(key, key)} | {label} | {d.get('n', 0)} | {_pct(d.get('mean_bps'))} | {i.get('n', 0)} | {_pct(i.get('mean_bps'))} | "
                         + (f"{_pct(ci[0])} ~ {_pct(ci[1])}" if ci else '样本不足') + ' |')
    lines += ['', '## 对照账本', '', '四本账共用同一套日线价格与费用；“lot”按整手和当前账户规模，“frac”允许零股，用来剔除整手取整的影响。', '',
              '| 账本 | 起止 | 期末净值（元） | 累计收益 | 最大回撤 | 平均仓位 | 已平仓笔数 | 胜率 | 平均持有天数 |', '|---|---|---|---|---|---|---|---|---|']
    labels = {'A': 'A 纯规则', 'B': 'B 规则+研究资格', 'C': 'C 规则+研究+组合决策', 'D': 'D 规则仓位（模型只否决或下调）'}
    for name, s in report['shadow']['all_time'].items():
        book, variant = name.split('-')
        lines.append(f"| {labels[book]}（{variant}） | {s['from']}~{s['to']} | {s['equity_cents'] / 100:,.2f} | {s['return_pct']:+.2f}% | "
                     f"{s['max_drawdown_pct']}% | {s['avg_exposure_pct']}% | {s['closed_trades']} | {s['win_rate'] if s['win_rate'] is not None else '—'} | "
                     f"{s['avg_held_days'] if s['avg_held_days'] is not None else '—'} |")
    lines += ['', '## 版本变化', '']
    lines += [f"- {b['first_seen_at']}：build {b['build_id']}（代码 {b['code']}，配置 {b['config']}，模型 {b['model']}，采纳规则 {b['guidance']}）" for b in report['builds_this_week']] or ['- 本周没有新版本上线。']
    usage = report['model_usage']
    lines += ['', '## 模型调用', '', f"- 研究：{json.dumps(usage['attempts'], ensure_ascii=False)}",
              f"- 组合决策运行：{json.dumps(usage['portfolio_runs'], ensure_ascii=False)}",
              f"- 资料未变化、沿用结论而未调用模型的研究：{usage['research_renewals_without_model']} 次"]
    lines += ['', '## 未关闭的工程问题', '']
    lines += [f"- [{i['category']}] {i['title']}（{i['symbol']}，出现{i['occurrences']}次，最近 {i['last_seen_at']}，编号 {i['id']}）" for i in report['engineering_issues']] or ['- 无。']
    lines += ['', '## 变更提案', '']
    for status, items in report['proposals'].items():
        lines.append(f"- {status}：{len(items)} 条" + ('' if not items else '；' + '；'.join(f"{p['id']} {p['title']}" for p in items[:8])))
    closed = report.get('proposals_closed')
    if closed:
        replaced = closed['replacements']
        lines.append(f"- REJECTED（驳回，累计）：{closed['REJECTED']} 条")
        lines.append(f"- SUPERSEDED（被新版替代，累计）：{closed['SUPERSEDED']} 条" + ('' if not replaced else '；旧→新 ' + '；'.join(f"{p['id']}→{p['replaced_by']}" for p in replaced[:8])))
    disk = report['disk']
    lines += ['', '## 运行健康', '', f"- 磁盘可用 {disk['free_gb']}GB，数据库 {disk['db_gb']}GB，备份 {disk['backups_gb']}GB" + ('（超过警戒线）' if disk['warning'] else '')]
    if report['calendar_warning']:
        lines.append(f"- {report['calendar_warning']['year']}年交易日历尚未录入，距年底 {report['calendar_warning']['days_left']} 天。")
    return '\n'.join(lines) + '\n'


def weekly_report(store, config, at=None):
    at = normalize_time(at or now())
    report = collect(store, config, at)
    year, week, _ = local(at).isocalendar()
    folder = store.root / 'workflow' / 'evaluation' / 'weekly'
    stem = f'{year}-W{week:02d}'
    json_write(folder / (stem + '.json'), report)
    (folder / (stem + '.md')).write_text(markdown(report), encoding='utf-8')
    return {'status': 'SUCCEEDED', 'report': str((folder / (stem + '.md')).relative_to(store.root)), 'week': stem}
