"""Current proposal agenda before the first Saturday; never makes governance decisions."""
from datetime import date, timedelta
from collections import Counter
import html

from .calendar import local

FIRST_DECISION_DAY = date(2026, 12, 5)


def decision_day(report_day):
    candidate = date.fromisoformat(report_day) + timedelta(days=1)
    return candidate if candidate >= FIRST_DECISION_DAY and candidate.weekday() == 5 and candidate.day <= 7 else None


def collect(store, report_day, generated_at):
    target = decision_day(report_day)
    if target is None:
        return None
    result = {'decision_day': target.isoformat(), 'as_of': generated_at, 'items': None}
    # Current lifecycle fields cannot reconstruct the queue on a previous date.
    if local(generated_at).date().isoformat() != report_day:
        return {**result, 'status': 'HISTORICAL_QUEUE_UNKNOWN'}
    try:
        rows = store.db.execute("""SELECT id,kind,title,status,created_at FROM strategy_proposals
            WHERE status IN ('DRAFT','READY','APPROVED') AND created_at<=?
            ORDER BY CASE status WHEN 'READY' THEN 0 WHEN 'APPROVED' THEN 1 ELSE 2 END,created_at,id""",
            (generated_at,)).fetchall()
        counts = dict(Counter(row['status'] for row in rows))
        return {**result, 'status': 'OK', 'counts': counts,
                'items': [dict(row) for row in rows if row['status'] == 'READY']}
    except Exception:
        return {**result, 'status': 'UNAVAILABLE'}


def _cell(value):
    return html.escape(str(value), quote=False).replace('\r', ' ').replace('\n', ' ').replace('|', '\\|').replace('`', '\\`')


def markdown(agenda):
    if not agenda:
        return []
    lines = ['', '## 明日月度决策', '', f"- 决策日：{agenda['decision_day']}（每月第一个周六）。"]
    if agenda['status'] == 'HISTORICAL_QUEUE_UNKNOWN':
        return lines + ['- 本页为事后补生成，无法由当前状态重建当时待决清单；未将今天的提案倒填到历史。']
    if agenda['status'] != 'OK':
        return lines + ['- 待决清单读取失败，数量未知，请在管理员页面核对。']
    counts = agenda.get('counts', {})
    lines += [f"- 当前待决READY {counts.get('READY', 0)}条，待完善DRAFT {counts.get('DRAFT', 0)}条，已批准待落实APPROVED {counts.get('APPROVED', 0)}条。",
              '- 下表列出本页生成时全部READY提案，请逐条决定批准、驳回或暂缓。',
              '- 暂缓需另记理由，保留现有状态；本清单不批准或实施任何变更。']
    if not agenda['items']:
        return lines + ['- 当前没有READY提案；其余待办数量见上。']
    lines += ['', '| 编号 | 状态 | 类型 | 待决事项 |', '|---|---|---|---|']
    for row in agenda['items']:
        lines.append('| ' + ' | '.join(_cell(row[k]) for k in ('id', 'status', 'kind', 'title')) + ' |')
    return lines
