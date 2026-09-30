"""Change governance: engineering issues, change proposals and adopted research guidance.

Review output never reaches a research prompt directly. Program and data defects become
engineering issues. Strategy observations become DRAFT proposals. Only guidance adopted from a
proposal the user approved is given to research, and adopting it changes the build id.
"""
import json
import re
import uuid
from .storage import now, normalize_time, digest

# Known defect classes; free-text lessons are mapped onto these so repeats collapse into one issue.
ISSUE_KEYS = {
    'EXECUTION_IGNORES_RESEARCH_TRIGGER': ('EXECUTION', '研究写的入场条件执行端不检查'),
    'QUOTE_RECORDED_AFTER_CUTOFF': ('DATA', '行情入库时间晚于复盘截止'),
    'MISSING_OR_STALE_QUOTE': ('DATA', '持仓报价缺失或过期'),
    'MISSING_DAILY_BARS': ('DATA', '日线缺失或未更新'),
    'UNSUPPORTED_SECURITY_RULE': ('EXECUTION', '证券或板块交易规则不支持'),
    'PORTFOLIO_DECISION_CONFLICT': ('EXECUTION', '组合决策与研究或执行冲突'),
    'SYNC_OR_LEDGER_GAP': ('SYSTEM', '云端同步或账本缺口'),
    'MODEL_FAILURE': ('SYSTEM', '模型调用失败、超时或身份不一致'),
    'OTHER_DATA': ('DATA', '其他数据问题'),
    'OTHER_SYSTEM': ('SYSTEM', '其他系统问题'),
    'OTHER_EXECUTION': ('EXECUTION', '其他执行问题'),
    # Deterministic daily consistency checks (review_checks.py).
    'CHECK_BUY_OUTSIDE_PLAN_BAND': ('EXECUTION', '买入成交价超出计划买入区间'),
    'CHECK_BUY_WITH_PLAN_BLOCKERS': ('EXECUTION', '计划有未解除限制时仍发生买入'),
    'CHECK_BUY_WITHOUT_PORTFOLIO_ALLOW': ('EXECUTION', '没有组合授权时发生买入'),
    'CHECK_BUY_WHILE_HALTED': ('EXECUTION', '回撤熔断期间发生买入'),
    'CHECK_SELL_WITHOUT_REASON': ('EXECUTION', '卖出缺少退出依据'),
    'CHECK_EXECUTION_EVIDENCE': ('EXECUTION', '成交核验缺少原始依据'),
    'CHECK_LATE_QUOTE_IN_REVIEW': ('DATA', '复盘期内持仓收盘报价事后补齐'),
    'CHECK_SYNC_STALE': ('SYSTEM', '云端账本同步超过10分钟未成功'),
    'CHECK_OUTBOX_BACKLOG': ('SYSTEM', '策略发布积压'),
    'CHECK_MODEL_IDENTITY': ('SYSTEM', '模型实际运行配置与固定配置不一致'),
    'CHECK_RESEARCH_FAILURE_RATE': ('SYSTEM', '研究模型失败率偏高'),
    'CHECK_DISK_SPACE': ('SYSTEM', '磁盘空间或数据库体积超过警戒线'),
}
REVIEW_ISSUE_KEYS = [k for k in ISSUE_KEYS if not k.startswith('CHECK_')]
PROPOSAL_KINDS = ('RESEARCH_GUIDANCE', 'PARAMETER', 'PROMPT', 'RULE', 'SCHEDULE', 'DATA_SOURCE', 'OTHER')
TRANSITIONS = {'DRAFT': ('READY', 'REJECTED', 'SUPERSEDED'), 'READY': ('APPROVED', 'REJECTED', 'DRAFT', 'SUPERSEDED'),
               'APPROVED': ('ADOPTED', 'REJECTED'), 'ADOPTED': ('RETIRED',),
               # A proposal rejected only because a complete new version replaced it can be relabelled.
               'REJECTED': ('SUPERSEDED',), 'RETIRED': (), 'SUPERSEDED': ()}
# States a replacing proposal may be in; a proposal can only be superseded by one still in play.
LIVE = ('DRAFT', 'READY', 'APPROVED', 'ADOPTED')
RESERVED_PAYLOAD = ('history', 'superseded_by', 'supersedes')
# A decision that changes production must name who approved it; agents may only prepare.
USER_DECISIONS = ('APPROVED', 'ADOPTED', 'RETIRED')


def guidance(store, route, symbol, at):
    """Adopted research rules for this route and symbol, as of `at`. Never includes drafts."""
    rows = store.db.execute('''SELECT * FROM strategy_guidance WHERE status='ADOPTED' AND route IN (?,'ALL')
        AND scope IN (?,'ALL') AND adopted_at<=? ORDER BY adopted_at,id''', (route, symbol, normalize_time(at)))
    return [{'id': r['id'], 'text': r['text'], 'adopted_at': r['adopted_at'], 'proposal_id': r['proposal_id'],
             'claim_type': 'ADOPTED_RESEARCH_RULE'} for r in rows]


def record_issue(store, issue_key, symbol, detail, evidence, at, *, title=None, dedupe=None):
    """Upsert one engineering issue per (issue key, symbol), or per (key, symbol, dedupe) for issues an
    operator records by hand, so distinct manual reports under a generic key do not merge. Caller owns the transaction."""
    if issue_key not in ISSUE_KEYS:
        issue_key = 'OTHER_SYSTEM'
    category, default_title = ISSUE_KEYS[issue_key]
    iid = digest(issue_key + '|' + (symbol or 'MARKET') + ('|' + dedupe if dedupe else ''))[:24]
    at = normalize_time(at)
    row = retitled_issue(store, issue_key, symbol or 'MARKET', title or default_title, iid) if dedupe else None
    row = row or store.db.execute('SELECT * FROM engineering_issues WHERE id=?', (iid,)).fetchone()
    iid = row['id'] if row else iid
    item = {'detail': str(detail)[:600], 'evidence': [str(e) for e in evidence][:10], 'at': at}
    if not row:
        store.db.execute('INSERT INTO engineering_issues VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                         (iid, issue_key, category, symbol or 'MARKET', 'OPEN', title or default_title, at, at, 1,
                          json.dumps({'latest': item, 'history': [item]}, ensure_ascii=False), None, None))
        return iid
    payload = json.loads(row['payload_json'])
    payload['latest'] = item
    payload['history'] = (payload.get('history', []) + [item])[-10:]
    # A resolved issue that is observed again reopens instead of silently accumulating.
    store.db.execute('UPDATE engineering_issues SET status=?,last_seen_at=?,occurrences=occurrences+1,payload_json=?,resolved_at=CASE WHEN ?=1 THEN NULL ELSE resolved_at END WHERE id=?',
                     ('OPEN' if row['status'] in ('OPEN', 'RESOLVED') else row['status'], max(at, row['last_seen_at']),
                      json.dumps(payload, ensure_ascii=False), int(row['status'] == 'RESOLVED'), iid))
    return iid


def resolve_issue(store, issue_id, resolution, at=None, status='RESOLVED'):
    if status not in ('RESOLVED', 'WONTFIX'):
        raise ValueError('只能标记为RESOLVED或WONTFIX')
    if not resolution or not resolution.strip():
        raise ValueError('需要说明处理方式')
    with store.db:
        n = store.db.execute('UPDATE engineering_issues SET status=?,resolved_at=?,resolution=? WHERE id=?',
                             (status, normalize_time(at or now()), resolution.strip()[:600], issue_id)).rowcount
    if not n:
        raise ValueError('未找到该工程问题')


def retitled_issue(store, issue_key, symbol, title, iid):
    """The retitled issue a hand-recorded report under `title` belongs to: one that now carries the
    title, else the issue first recorded under it (its id comes from that title), else one that
    carried it before. Only retitled issues are searched, so nothing else merges."""
    rows = [r for r in store.db.execute('SELECT * FROM engineering_issues WHERE issue_key=? AND symbol=?', (issue_key, symbol))
            if json.loads(r['payload_json']).get('titles')]
    current = next((r for r in rows if r['title'] == title), None)
    if current:
        return current
    if store.db.execute('SELECT 1 FROM engineering_issues WHERE id=?', (iid,)).fetchone():
        return None  # the issue first recorded under this title
    return next((r for r in rows if any(t.get('from') == title for t in json.loads(r['payload_json'])['titles'])), None)


def retitle_issue(store, issue_id, title, note, at=None):
    """Correct an issue's title in place. The id, occurrences and history stay; the old title is kept."""
    title = (title or '').strip()
    if not title or len(title) > 120:
        raise ValueError('新标题须为1到120字')
    if not note or not note.strip():
        raise ValueError('需要说明改标题的理由')
    row = store.db.execute('SELECT * FROM engineering_issues WHERE id=?', (issue_id,)).fetchone()
    if not row:
        raise ValueError('未找到该工程问题')
    if row['title'] == title:
        raise ValueError('新标题与当前标题相同')
    clash = store.db.execute('SELECT id FROM engineering_issues WHERE issue_key=? AND symbol=? AND title=? AND id<>?',
                             (row['issue_key'], row['symbol'], title, issue_id)).fetchone()
    if clash:
        raise ValueError(f"已有同类型、同代码、同标题的工程问题 {clash['id']}；若是同一问题，保留一条，另一条用 resolve --wontfix 注明合并")
    payload = json.loads(row['payload_json'])
    payload['titles'] = payload.get('titles', []) + [{'from': row['title'], 'to': title, 'at': normalize_time(at or now()), 'note': note.strip()[:300]}]
    with store.db:
        store.db.execute('UPDATE engineering_issues SET title=?,payload_json=? WHERE id=?', (title, json.dumps(payload, ensure_ascii=False), issue_id))
    return {'status': 'RETITLED', 'id': issue_id, 'from': row['title'], 'to': title}


def draft_proposal(store, *, source, kind, target, title, payload, at, dedupe_key=None):
    """Create a DRAFT change proposal. Repeated observations append evidence to the same draft."""
    if kind not in PROPOSAL_KINDS:
        raise ValueError('未知提案类型')
    at = normalize_time(at)
    # Lifecycle fields are written only by decide().
    payload = {k: v for k, v in payload.items() if k not in RESERVED_PAYLOAD}
    if dedupe_key:
        row = store.db.execute('SELECT * FROM strategy_proposals WHERE dedupe_key=?', (dedupe_key,)).fetchone()
        if row:
            old = json.loads(row['payload_json'])
            old['observations'] = (old.get('observations', []) + payload.get('observations', []))[-20:]
            old['last_seen_at'] = at
            store.db.execute('UPDATE strategy_proposals SET payload_json=? WHERE id=?', (json.dumps(old, ensure_ascii=False), row['id']))
            return row['id']
    pid = uuid.uuid4().hex[:16]
    store.db.execute('INSERT INTO strategy_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                     (pid, at, source, kind, target[:200], 'DRAFT', title[:200], json.dumps({**payload, 'last_seen_at': at}, ensure_ascii=False),
                      None, None, None, dedupe_key))
    return pid


def validate_guidance(value):
    if not isinstance(value, dict) or set(value) != {'route', 'scope', 'text'}:
        raise ValueError('研究规则须包含route、scope、text')
    if value['route'] not in ('watchlist', 'global', 'ALL'):
        raise ValueError('研究规则线路须为watchlist、global或ALL')
    if value['scope'] != 'ALL' and not re.fullmatch(r'(?:sh|sz)\d{6}|[A-Z]{2,5}|US:[A-Z0-9.-]{1,10}', value['scope']):
        raise ValueError('研究规则适用范围须为ALL或单个标的代码')
    text = value['text'].strip() if isinstance(value['text'], str) else ''
    if not 8 <= len(text) <= 300:
        raise ValueError('研究规则文字须为8到300字')
    return {**value, 'text': text}


def check_decision(store, proposal_id, status, *, decided_by, note, replaced_by=None):
    """Validate a transition without writing; returns the proposal row."""
    row = store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (proposal_id,)).fetchone()
    if not row:
        raise ValueError(f'未找到提案 {proposal_id}')
    if status not in TRANSITIONS.get(row['status'], ()):
        raise ValueError(f"提案 {proposal_id} 的状态不能从{row['status']}变为{status}")
    if status in USER_DECISIONS and (not decided_by or not decided_by.strip()):
        raise ValueError('批准、上线或撤下须写明批准人（用户本人确认），代理不能自行批准')
    if row['status'] == 'REJECTED' and row['decided_by'] and (not decided_by or not decided_by.strip()):
        raise ValueError(f"该提案由 {row['decided_by']} 驳回，改标须写明批准人")
    if row['status'] == 'APPROVED' and (not decided_by or not decided_by.strip()):
        raise ValueError('用户已批准的提案，改变状态须写明批准人')
    if not note or not note.strip():
        raise ValueError('需要记录决定理由或实施说明')
    if status == 'SUPERSEDED':
        if not replaced_by:
            raise ValueError('标记为被新版替代须写明新版提案编号（--replaced-by）')
        if replaced_by == proposal_id:
            raise ValueError('新版提案不能是它自己')
        new = store.db.execute('SELECT status FROM strategy_proposals WHERE id=?', (replaced_by,)).fetchone()
        if not new:
            raise ValueError(f'未找到新版提案 {replaced_by}')
        if new['status'] not in LIVE:
            raise ValueError(f"新版提案 {replaced_by} 的状态为{new['status']}，只能由仍有效的提案替代")
    elif replaced_by:
        raise ValueError('只有标记为SUPERSEDED时才写新版提案编号')
    return row


def decide(store, proposal_id, status, *, decided_by, note, at=None, replaced_by=None):
    """Move a proposal through its lifecycle. Production-changing states require the approver's name.
    Every transition is kept in the payload's history, so a relabel does not erase the earlier decision."""
    at = normalize_time(at or now())
    row = check_decision(store, proposal_id, status, decided_by=decided_by, note=note, replaced_by=replaced_by)
    payload = json.loads(row['payload_json'])
    history = [h for h in payload.get('history') or [] if isinstance(h, dict)]
    if row['decided_at'] and not any(h.get('at') == row['decided_at'] for h in history):
        # A decision recorded before the history existed (0.15.2 and earlier) is kept first.
        history = [{'from': None, 'to': row['status'], 'at': row['decided_at'], 'by': row['decided_by'],
                    'note': (row['decision_note'] or '')[:300]}] + history
    payload['history'] = history + [{'from': row['status'], 'to': status, 'at': at,
                                     'by': (decided_by or '').strip()[:120] or None, 'note': note.strip()[:300]}]
    if status == 'SUPERSEDED':
        payload['superseded_by'] = replaced_by
    with store.db:
        if status == 'SUPERSEDED':
            new = json.loads(store.db.execute('SELECT payload_json FROM strategy_proposals WHERE id=?', (replaced_by,)).fetchone()[0])
            new['supersedes'] = sorted(set(new.get('supersedes', [])) | {proposal_id})
            store.db.execute('UPDATE strategy_proposals SET payload_json=? WHERE id=?', (json.dumps(new, ensure_ascii=False), replaced_by))
        store.db.execute('UPDATE strategy_proposals SET payload_json=? WHERE id=?', (json.dumps(payload, ensure_ascii=False), proposal_id))
        if status == 'ADOPTED' and row['kind'] == 'RESEARCH_GUIDANCE':
            g = validate_guidance(payload.get('guidance'))
            store.db.execute('INSERT INTO strategy_guidance VALUES(?,?,?,?,?,?,?,?,?,?)',
                             ('G-' + proposal_id, g['route'], g['scope'], g['text'], 'ADOPTED', proposal_id, at, None,
                              decided_by.strip()[:120], json.dumps({'note': note.strip()[:600]}, ensure_ascii=False)))
        if status == 'RETIRED':
            store.db.execute("UPDATE strategy_guidance SET status='RETIRED',retired_at=? WHERE proposal_id=? AND status='ADOPTED'", (at, proposal_id))
        store.db.execute('UPDATE strategy_proposals SET status=?,decided_at=?,decided_by=?,decision_note=? WHERE id=?',
                         (status, at, (decided_by or '').strip()[:120] or None, note.strip()[:1000], proposal_id))
    return dict(store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (proposal_id,)).fetchone())


def proposals(store, status=None):
    sql = 'SELECT * FROM strategy_proposals' + (' WHERE status=?' if status else '') + ' ORDER BY created_at DESC LIMIT 200'
    return [{**dict(r), 'payload': json.loads(r['payload_json'])} for r in store.db.execute(sql, (status,) if status else ())]


def issues(store, status='OPEN'):
    sql = 'SELECT * FROM engineering_issues' + (' WHERE status=?' if status else '') + ' ORDER BY last_seen_at DESC LIMIT 200'
    return [{**dict(r), 'payload': json.loads(r['payload_json'])} for r in store.db.execute(sql, (status,) if status else ())]
