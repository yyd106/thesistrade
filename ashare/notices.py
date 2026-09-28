"""Notices to Dean: the one channel that interrupts him, as a pop-up on the web workbench.

Under the authorization letter only matters that are his to decide reach him: items that need his approval
(DECISION), second-class changes about to go live that he may veto (VETO), and information he should see
but need not act on, such as a drawdown halt (INFO). A recommendation to buy a stable data source is a
DECISION. Outages, failed jobs and the like stay in the digest and the evaluation batches for the program,
the desktop agent and Claude to handle.

Notices are written on the research node (`./agent notices new`, or imported from Claude's reports
repository) and delivered to the cloud, which shows them and records Dean's answer; the research node
mirrors the answer. A standalone node shows its own notices. A decision is a record only: nothing here
carries out what was approved.
"""
import json
import re
from .storage import digest, normalize_time, now
from .calendar import local

KINDS = {'DECISION': '需要你决定', 'VETO': '将自动上线，可否决', 'INFO': '通知'}
AUTHORS = ('agent', 'claude', 'program')
# Answers each kind accepts, and the status each answer leaves.
ACTIONS = {'INFO': {'ACK': 'ACKED'}, 'DECISION': {'APPROVE': 'APPROVED', 'REJECT': 'REJECTED'},
           'VETO': {'VETO': 'VETOED', 'ACK': 'ACKED'}}
STATUSES = ('OPEN', 'ACKED', 'APPROVED', 'REJECTED', 'VETOED')
ID = re.compile(r'N-[0-9A-Za-z._-]{4,80}')
FIELDS = ('id', 'created_at', 'author', 'kind', 'title', 'body', 'payload_json')
DISPLAY = ('id', 'created_at', 'author', 'kind', 'title', 'body', 'status', 'deadline')


def _check(title, body, kind, author):
    title, body = (title or '').strip(), (body or '').strip()
    if not 2 <= len(title) <= 80:
        raise ValueError('通知标题须为2到80字')
    if not 10 <= len(body) <= 4000:
        raise ValueError('通知正文须为10到4000字')
    if kind not in KINDS:
        raise ValueError('通知类型只能是：' + '、'.join(KINDS))
    if author not in AUTHORS:
        raise ValueError('通知来源只能是：' + '、'.join(AUTHORS))
    return title, body


def _payload(payload):
    payload = dict(payload or {})
    if payload.get('deadline') is not None:
        payload['deadline'] = normalize_time(payload['deadline'])
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def insert(store, *, title, body, kind='DECISION', author='agent', at=None, notice_id=None, payload=None):
    """Insert inside the caller's transaction; an existing id is left unchanged. Returns the id."""
    title, body = _check(title, body, kind, author)
    at = normalize_time(at or now())
    nid = notice_id or 'N-' + local(at).strftime('%Y%m%d-%H%M') + '-' + digest(title + body)[:6]
    if not ID.fullmatch(nid):
        raise ValueError('通知编号格式无效')
    store.db.execute('INSERT OR IGNORE INTO notices VALUES(?,?,?,?,?,?,?,?,?,?)',
                     (nid, at, author, kind, title, body, 'OPEN', None, None, _payload(payload)))
    return nid


def create(store, **kw):
    with store.db:
        nid = insert(store, **kw)
    return get(store, nid)


def get(store, nid):
    r = store.db.execute('SELECT * FROM notices WHERE id=?', (nid,)).fetchone()
    return dict(r) if r else None


def listing(store, status=None, limit=50):
    sql = 'SELECT * FROM notices' + (' WHERE status=?' if status else '') + ' ORDER BY created_at DESC LIMIT ?'
    return [dict(r) for r in store.db.execute(sql, ((status, limit) if status else (limit,)))]


def pending(store):
    return [dict(r) for r in store.db.execute('SELECT * FROM notices WHERE delivered_at IS NULL ORDER BY created_at LIMIT 50')]


def unresolved(store):
    return [r[0] for r in store.db.execute("SELECT id FROM notices WHERE delivered_at IS NOT NULL AND status='OPEN' ORDER BY created_at LIMIT 200")]


def outgoing(row):
    return {k: row[k] for k in FIELDS}


def mark_delivered(store, ids, at):
    for nid in ids:
        store.db.execute('UPDATE notices SET delivered_at=? WHERE id=? AND delivered_at IS NULL', (at, nid))


def _state(row):
    p = json.loads(row['payload_json'] or '{}')
    return {'status': row['status'], 'acked_at': row['acked_at'], 'decided_by': p.get('decided_by')}


def apply_states(store, states):
    """Answers recorded where Dean saw the notice; the research node only mirrors them."""
    for nid, s in (states or {}).items():
        if not isinstance(s, dict) or s.get('status') not in STATUSES:
            continue
        row = get(store, nid)
        if not row:
            continue
        payload = json.loads(row['payload_json'] or '{}')
        if s.get('decided_by'):
            payload['decided_by'] = str(s['decided_by'])[:40]
        store.db.execute('UPDATE notices SET status=?,acked_at=?,payload_json=? WHERE id=?',
                         (s['status'], s.get('acked_at'), json.dumps(payload, ensure_ascii=False, sort_keys=True), nid))


def receive(store, body, at):
    """Cloud side of /api/sync/notices. Content is immutable once received; the reply carries answers."""
    incoming = body.get('notices') or []
    known = body.get('known') or []
    if not isinstance(incoming, list) or len(incoming) > 50 or not isinstance(known, list) or len(known) > 200:
        raise ValueError('通知同步数量超限')
    for n in incoming:
        if not isinstance(n, dict) or set(n) != set(FIELDS) or not ID.fullmatch(str(n['id'])):
            raise ValueError('通知字段不匹配')
        _check(n['title'], n['body'], n['kind'], n['author'])
        payload = json.loads(n['payload_json'])
        if not isinstance(payload, dict):
            raise ValueError('通知附加信息格式错误')
        store.db.execute('INSERT OR IGNORE INTO notices VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (n['id'], normalize_time(n['created_at']), n['author'], n['kind'], n['title'].strip(), n['body'].strip(),
                          'OPEN', None, at, _payload(payload)))
    states = {}
    for nid in [n['id'] for n in incoming] + [k for k in known if isinstance(k, str)]:
        row = get(store, nid)
        if row:
            states[nid] = _state(row)
    return {'status': 'ACCEPTED', 'states': states}


def decide(store, nid, action, user, at=None):
    at = normalize_time(at or now())
    row = get(store, nid)
    if not row:
        raise ValueError('没有这条通知')
    allowed = ACTIONS[row['kind']]
    if action not in allowed:
        raise ValueError('这条通知不能这样处理')
    if row['status'] != 'OPEN':
        if row['status'] == allowed[action]:
            return row  # the same answer twice (a retried request) changes nothing
        raise ValueError('这条通知已经处理过')
    payload = json.loads(row['payload_json'] or '{}')
    payload.update(decided_by=user, action=action)
    with store.db:
        # Only an open notice takes an answer; a concurrent answer that got there first wins.
        done = store.db.execute("UPDATE notices SET status=?,acked_at=?,payload_json=? WHERE id=? AND status='OPEN'",
                                (allowed[action], at, json.dumps(payload, ensure_ascii=False, sort_keys=True), nid)).rowcount
    if not done:
        return decide(store, nid, action, user, at)
    return get(store, nid)


def open_for_display(store, limit=5):
    out = []
    for r in store.db.execute("SELECT * FROM notices WHERE status='OPEN' ORDER BY created_at LIMIT ?", (limit,)):
        item = {k: r[k] for k in DISPLAY if k != 'deadline'}
        item['deadline'] = json.loads(r['payload_json'] or '{}').get('deadline')
        out.append(item)
    return out


def export(store):
    """Every notice and its answer, for the reports repository."""
    rows = []
    for r in store.db.execute('SELECT * FROM notices ORDER BY created_at'):
        p = json.loads(r['payload_json'] or '{}')
        rows.append({'id': r['id'], 'created_at': r['created_at'], 'author': r['author'], 'kind': r['kind'], 'title': r['title'],
                     'status': r['status'], 'answered_at': r['acked_at'], 'decided_by': p.get('decided_by'),
                     'delivered_at': r['delivered_at'], 'deadline': p.get('deadline')})
    return rows


def parse_markdown(text):
    """Front matter (id, kind, title, optional deadline) plus body: the format Claude writes into
    notices/outbox/ in the reports repository."""
    m = re.match(r'\A---\n(.*?)\n---\n(.*)\Z', text.replace('\r\n', '\n'), re.S)
    if not m:
        raise ValueError('通知文件缺少头部')
    meta = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(':')
        if sep:
            meta[key.strip()] = value.strip()
    return {'id': meta.get('id', ''), 'kind': meta.get('kind', 'DECISION'), 'title': meta.get('title', ''),
            'deadline': meta.get('deadline') or None, 'body': m.group(2).strip()}
