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
# Production changes need a version-bound confirmation receipt; a name is never authority.
USER_DECISIONS = ('APPROVED', 'ADOPTED', 'RETIRED')


def guidance(store, route, symbol, at):
    """Adopted research rules for this route and symbol, as of `at`. Never includes drafts."""
    at = normalize_time(at)
    rows = store.db.execute('''SELECT * FROM strategy_guidance WHERE status IN ('ADOPTED','RETIRED') AND route IN (?,'ALL')
        AND scope IN (?,'ALL') AND adopted_at<=? AND (retired_at>? OR (status='ADOPTED' AND retired_at IS NULL))
        ORDER BY adopted_at,id''', (route, symbol, at, at))
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


def draft_proposal(store, *, source, kind, target, title, payload, at, dedupe_key=None, return_receipt=False):
    """Create a draft, or identify an identical existing proposal without changing it.

    Existing callers receive its id. Receipts also distinguish creation from reuse and report
    the actual lifecycle state. Additional evidence belongs in its separate append-only tables.
    """
    if kind not in PROPOSAL_KINDS:
        raise ValueError('未知提案类型')
    for field, value in (('target', target), ('title', title)):
        if not isinstance(value, str) or len(value) > 200:
            raise ValueError(f'提案 {field} 须为不超过200字的字符串')
    at = normalize_time(at)
    # Lifecycle fields are written only by decide().
    payload = {k: v for k, v in payload.items() if k not in RESERVED_PAYLOAD}
    if kind == 'RESEARCH_GUIDANCE':
        # New proposals explicitly default to an additional, complementary rule.
        payload.setdefault('replaces', [])
        _replacement_ids(payload['replaces'])
    if dedupe_key:
        row = store.db.execute('SELECT * FROM strategy_proposals WHERE dedupe_key=?', (dedupe_key,)).fetchone()
        if row:
            ignored = set(RESERVED_PAYLOAD) | {'last_seen_at', 'dedupe_key'}
            def content(value):
                value = {k: v for k, v in value.items() if k not in ignored}
                if kind == 'RESEARCH_GUIDANCE':
                    value.setdefault('replaces', [])
                return json.dumps(value, ensure_ascii=False, sort_keys=True)
            if ((row['source'], row['kind'], row['target'], row['title']) != (source, kind, target, title)
                    or content(json.loads(row['payload_json'])) != content(payload)):
                raise ValueError(f"dedupe_key 已关联提案 {row['id']}（{row['status']}），内容不一致；新增证据须单独登记，修订方案请使用新版本和新的 dedupe_key")
            receipt = {'id': row['id'], 'status': row['status'], 'action': 'EXISTS'}
            return receipt if return_receipt else row['id']
    pid = uuid.uuid4().hex[:16]
    store.db.execute('INSERT INTO strategy_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                     (pid, at, source, kind, target, 'DRAFT', title, json.dumps({**payload, 'last_seen_at': at}, ensure_ascii=False),
                      None, None, None, dedupe_key))
    return {'id': pid, 'status': 'DRAFT', 'action': 'CREATED'} if return_receipt else pid


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


def proposal_version(row):
    """Hash the complete original proposal, excluding only lifecycle bookkeeping."""
    ignored = set(RESERVED_PAYLOAD) | {'last_seen_at', 'dedupe_key'}
    payload = {k: v for k, v in json.loads(row['payload_json']).items() if k not in ignored}
    # Historical proposals without this field mean unspecified, not an implicit approval.
    content = {k: row[k] for k in ('source', 'kind', 'target', 'title')}
    content['payload'] = payload
    return digest(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False))


def _replacement_ids(value):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() or v != v.strip() for v in value):
        raise ValueError('replaces 必须明确列出旧研究规则编号；不替代时填写空列表 []')
    if len(set(value)) != len(value):
        raise ValueError('replaces 不能包含重复规则编号')
    return sorted(value)


def needs_confirmation(row, status):
    return (status in USER_DECISIONS or row['status'] == 'APPROVED'
            or (row['status'] == 'REJECTED' and bool(row['decided_by'])))


def check_decision(store, proposal_id, status, *, decided_by=None, note, replaced_by=None):
    """Check the lifecycle only. Authorization is checked atomically by decide()."""
    row = store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (proposal_id,)).fetchone()
    if not row:
        raise ValueError(f'未找到提案 {proposal_id}')
    if status not in TRANSITIONS.get(row['status'], ()):
        raise ValueError(f"提案 {proposal_id} 的状态不能从{row['status']}变为{status}")
    if not isinstance(note, str) or not note.strip():
        raise ValueError('需要记录决定理由或实施说明')
    if len(note.strip()) > 1000:
        raise ValueError('决定理由或实施说明不能超过1000字')
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


def _guidance_version(store, row):
    """Full rule content and lifecycle, plus the version of its owning proposal."""
    result = {k: row[k] for k in ('id', 'route', 'scope', 'text', 'status', 'proposal_id',
                                 'adopted_at', 'retired_at', 'approved_by')}
    payload = json.loads(row['payload_json'])
    result['payload_hash'] = digest(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False))
    owner = store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (row['proposal_id'],)).fetchone()
    result['proposal_version'] = None if owner is None else {
        'id': owner['id'], 'status': owner['status'], 'hash': proposal_version(owner)}
    return result


def _plan_summary(payload):
    """Only authored proposal summaries leave the local store, never evidence objects/paths."""
    result = {}
    for key in ('hypothesis', 'change', 'test_plan', 'failure_criteria', 'rollback'):
        value = payload.get(key)
        if isinstance(value, str):
            result[key] = value if len(value) <= 1200 else value[:1200] + '…（摘要省略；完整原稿已绑定版本）'
        elif value is not None:
            result[key] = '原稿为结构化内容；完整版本已绑定，请在本机提案详情核对。'
    evidence = payload.get('evidence')
    if evidence is not None:
        count = len(evidence) if isinstance(evidence, list) else 1
        result['evidence_summary'] = f'已绑定原稿证据 {count} 项；仅显示摘要，不展开本机路径或原始资料。'
    return result


def _decision_plan(store, row, status, note, replaces, replaced_by, at):
    payload = json.loads(row['payload_json'])
    version = proposal_version(row)
    rule, old_rules, replacement_ids = None, [], None
    if replaces is not None and (row['kind'] != 'RESEARCH_GUIDANCE' or status not in ('APPROVED', 'ADOPTED')):
        raise ValueError('replaces 仅用于批准或采纳研究规则')
    if row['kind'] == 'RESEARCH_GUIDANCE' and status in ('APPROVED', 'ADOPTED'):
        rule = validate_guidance(payload.get('guidance'))
        selected = replaces if replaces is not None else payload.get('replaces')
        if selected is None:
            raise ValueError('批准或采纳前须明确 replaces 列表；互补新增请明确填写 []')
        else:
            replacement_ids = _replacement_ids(selected)
            for gid in replacement_ids:
                old = store.db.execute('SELECT * FROM strategy_guidance WHERE id=?', (gid,)).fetchone()
                if not old or old['status'] != 'ADOPTED' or old['retired_at'] is not None or old['adopted_at'] > at:
                    raise ValueError(f'被替代规则 {gid} 不存在或当前未生效')
                if old['proposal_id'] == row['id'] or gid == 'G-' + row['id']:
                    raise ValueError('研究规则不能替代自身')
                if (rule['route'] != old['route'] and 'ALL' not in (rule['route'], old['route'])) or (
                        rule['scope'] != old['scope'] and 'ALL' not in (rule['scope'], old['scope'])):
                    raise ValueError(f'被替代规则 {gid} 与新规则没有线路和适用范围交集')
                old_version = _guidance_version(store, old)
                owner = old_version['proposal_version']
                if old['proposal_id'] and owner is None:
                    raise ValueError(f'被替代规则 {gid} 的所属提案不存在')
                if owner is not None and owner['status'] != 'ADOPTED':
                    raise ValueError(f'被替代规则 {gid} 与所属提案的状态不一致')
                old_rules.append(old_version)
    if status == 'RETIRED':
        old_rules = [_guidance_version(store, g) for g in store.db.execute(
            "SELECT * FROM strategy_guidance WHERE proposal_id=? AND status='ADOPTED' ORDER BY id", (row['id'],))]
        if any(g['retired_at'] is not None or g['adopted_at'] > at for g in old_rules):
            raise ValueError('待撤下规则的生效时间或状态不一致')
    approved = next((h for h in reversed(payload.get('history') or [])
                     if isinstance(h, dict) and h.get('to') == 'APPROVED'), None)
    legacy = row['status'] == 'APPROVED' and not (approved and approved.get('approval_id'))
    if status == 'ADOPTED' and not legacy:
        if not approved or approved.get('proposal_hash') != version:
            raise ValueError('已批准提案的内容版本已改变，请登记完整新版本并重新批准')
        if row['kind'] == 'RESEARCH_GUIDANCE' and approved.get('replaces') != replacement_ids:
            raise ValueError('采纳的替代列表与已批准方案不一致，请登记完整新版本并重新批准')
    newer = None
    if replaced_by:
        replacement = store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (replaced_by,)).fetchone()
        newer = {'id': replacement['id'], 'status': replacement['status'], 'hash': proposal_version(replacement)}
    snapshot = {'version': 'proposal-decision-v1', 'proposal_hash': version, 'status': row['status'],
                'action': status, 'note': note.strip(), 'replaces': replacement_ids, 'rules': old_rules,
                'replaced_by': newer, 'legacy_approval': legacy}
    summary = {'proposal_id': row['id'], 'title': row['title'], 'kind': row['kind'], 'target': row['target'],
               'from': row['status'], 'to': status, 'note': note.strip(), 'proposal_hash': version,
               'guidance': rule, 'replaces': old_rules, 'legacy_approval': legacy,
               'plan': _plan_summary(payload)}
    if newer:
        summary['replaced_by'] = newer
    if legacy:
        summary['approval_notice'] = '原APPROVED记录没有本版确认回执；本次确认绑定当前完整提案与本次动作，不追认旧批准。'
    return snapshot, summary


def request_decision(store, proposal_id, status, note, replaces=None, replaced_by=None, at=None):
    """Freeze the exact proposed action for a separate human confirmation. Never applies it."""
    from . import approvals
    at = normalize_time(at or now())
    with approvals.atomic(store):
        row = check_decision(store, proposal_id, status, note=note, replaced_by=replaced_by)
        if status in ('DRAFT', 'READY'):
            raise ValueError('草稿整理动作不需要用户审批，可直接记录决定')
        snapshot, summary = _decision_plan(store, row, status, note, replaces, replaced_by, at)
        return approvals.create_request(store, kind='PROPOSAL_DECISION', subject_id=proposal_id,
                                        action=status, snapshot=snapshot, summary=summary, at=at)


def _record_transition(store, row, status, actor, note, at, *, approval_id=None, version=None,
                       replaces=None, replaced_by=None, replacing_guidance=None):
    payload = json.loads(row['payload_json'])
    history = [h for h in payload.get('history') or [] if isinstance(h, dict)]
    if row['decided_at'] and not any(h.get('at') == row['decided_at'] for h in history):
        history.insert(0, {'from': None, 'to': row['status'], 'at': row['decided_at'], 'by': row['decided_by'],
                           'note': (row['decision_note'] or '')[:300]})
    event = {'from': row['status'], 'to': status, 'at': at, 'by': actor, 'note': note.strip()}
    if approval_id:
        event.update(approval_id=approval_id, proposal_hash=version, replaces=replaces)
    if replacing_guidance:
        event['replaced_by_guidance'] = replacing_guidance
    payload['history'] = history + [event]
    if status == 'SUPERSEDED':
        payload['superseded_by'] = replaced_by
    store.db.execute('UPDATE strategy_proposals SET status=?,decided_at=?,decided_by=?,decision_note=?,payload_json=? WHERE id=?',
                     (status, at, actor, note.strip(), json.dumps(payload, ensure_ascii=False), row['id']))


def _retire_guidance(store, item, at, approval_id, actor, note, replacing_guidance=None):
    payload = json.loads(store.db.execute('SELECT payload_json FROM strategy_guidance WHERE id=?', (item['id'],)).fetchone()[0])
    event = {'action': 'RETIRED', 'at': at, 'approval_id': approval_id, 'by': actor, 'note': note}
    if replacing_guidance:
        event['replaced_by_guidance'] = replacing_guidance
    payload['history'] = [h for h in payload.get('history', []) if isinstance(h, dict)] + [event]
    store.db.execute("UPDATE strategy_guidance SET status='RETIRED',retired_at=?,payload_json=? WHERE id=? AND status='ADOPTED'",
                     (at, json.dumps(payload, ensure_ascii=False), item['id']))


def decide(store, proposal_id, status, *, decided_by=None, note, at=None, replaced_by=None,
           approval_id=None, replaces=None):
    """Consume one exact confirmation and apply the complete lifecycle action atomically."""
    from . import approvals
    at = normalize_time(at or now())
    with approvals.atomic(store):
        row = check_decision(store, proposal_id, status, note=note, replaced_by=replaced_by)
        snapshot, summary = _decision_plan(store, row, status, note, replaces, replaced_by, at)
        required = needs_confirmation(row, status)
        if required or approval_id:
            if not approval_id:
                raise ValueError('需要用户确认回执 approval_id；填写批准人姓名不能授权')
            receipt = approvals.check_receipt(store, approval_id, kind='PROPOSAL_DECISION', subject_id=proposal_id,
                                              action=status, snapshot=snapshot, at=at)
            actor = receipt['actor']
        else:
            # A caller cannot impersonate a named user's rejection or other decision.
            if decided_by and decided_by.strip():
                raise ValueError('具名用户决定须通过确认回执，不能仅填写批准人')
            actor = None
        if status == 'SUPERSEDED':
            new = json.loads(store.db.execute('SELECT payload_json FROM strategy_proposals WHERE id=?', (replaced_by,)).fetchone()[0])
            new['supersedes'] = sorted(set(new.get('supersedes', [])) | {proposal_id})
            store.db.execute('UPDATE strategy_proposals SET payload_json=? WHERE id=?', (json.dumps(new, ensure_ascii=False), replaced_by))
        gid = 'G-' + proposal_id
        if status == 'ADOPTED' and row['kind'] == 'RESEARCH_GUIDANCE':
            g = summary['guidance']
            for old in snapshot['rules']:
                _retire_guidance(store, old, at, approval_id, actor, note.strip(), gid)
                if old['proposal_id']:
                    old_proposal = store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (old['proposal_id'],)).fetchone()
                    _record_transition(store, old_proposal, 'RETIRED', actor, note, at, approval_id=approval_id,
                                       version=proposal_version(old_proposal), replacing_guidance=gid)
            store.db.execute('INSERT INTO strategy_guidance VALUES(?,?,?,?,?,?,?,?,?,?)',
                             (gid, g['route'], g['scope'], g['text'], 'ADOPTED', proposal_id, at, None,
                              actor, json.dumps({'note': note.strip(), 'approval_id': approval_id,
                                                 'proposal_hash': snapshot['proposal_hash'], 'replaces': snapshot['replaces']}, ensure_ascii=False)))
        if status == 'RETIRED':
            for old in snapshot['rules']:
                _retire_guidance(store, old, at, approval_id, actor, note.strip())
        _record_transition(store, row, status, actor, note, at, approval_id=approval_id,
                           version=snapshot['proposal_hash'], replaces=snapshot['replaces'], replaced_by=replaced_by)
        if approval_id:
            approvals.consume(store, approval_id, result={'id': proposal_id, 'status': status,
                              'guidance_id': gid if status == 'ADOPTED' and row['kind'] == 'RESEARCH_GUIDANCE' else None,
                              'retired_guidance': [g['id'] for g in snapshot['rules']] if status in ('ADOPTED', 'RETIRED') else []}, at=at)
        result = dict(store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (proposal_id,)).fetchone())
    return result


def _proposal(store, row):
    item = {**dict(row), 'payload': json.loads(row['payload_json'])}
    item['additional_evidence'] = [dict(e) for e in store.db.execute(
        'SELECT run_id,created_at,payload_json FROM selfcheck_evidence WHERE proposal_id=? ORDER BY created_at', (row['id'],))]
    item['review_observations'] = [dict(e) for e in store.db.execute(
        'SELECT review_id,created_at,payload_json FROM review_observations WHERE proposal_id=? ORDER BY created_at', (row['id'],))]
    return item


def proposal(store, identity):
    row = store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (identity,)).fetchone()
    if not row:
        raise ValueError('未找到该提案')
    return _proposal(store, row)


def proposals(store, status=None):
    sql = 'SELECT * FROM strategy_proposals' + (' WHERE status=?' if status else '') + ' ORDER BY created_at DESC LIMIT 200'
    return [_proposal(store, r) for r in store.db.execute(sql, (status,) if status else ())]


def issues(store, status='OPEN'):
    sql = 'SELECT * FROM engineering_issues' + (' WHERE status=?' if status else '') + ' ORDER BY last_seen_at DESC LIMIT 200'
    return [{**dict(r), 'payload': json.loads(r['payload_json'])} for r in store.db.execute(sql, (status,) if status else ())]
