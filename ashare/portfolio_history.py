"""Local, forward-only source history for observational portfolio replay.

Source writes and their complete row versions commit together. A frozen cursor is
only usable while its coverage epoch and all nine recording triggers are intact;
an absent event log is never treated as proof that a source stayed unchanged.
"""
import json
import uuid
from datetime import datetime

VERSION = 'portfolio-source-history-v1'
TABLES = {'watchlist': ('plans', 'symbol', 'activated_at'),
          'global': ('global_plans', 'symbol', 'created_at'),
          'dynamic': ('dynamic_cases', 'id', 'created_at')}
CLOCK = "strftime('%Y-%m-%dT%H:%M:%f+00:00','now')"


def _clock(store):
    return store.db.execute('SELECT ' + CLOCK).fetchone()[0]


def _triggers(store):
    result = {}
    for route, (table, identity, _) in TABLES.items():
        columns = [r['name'] for r in store.db.execute('PRAGMA table_info(' + table + ')')]
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            prefix = 'OLD' if event == 'DELETE' else 'NEW'
            record = 'json_object(' + ','.join("'" + c + "'," + prefix + '."' + c + '"' for c in columns) + ')'
            name = 'portfolio_history_' + table + '_' + event.lower()
            result[name] = (f'CREATE TRIGGER {name} AFTER {event} ON {table} BEGIN '
                'INSERT INTO portfolio_source_events(epoch,route,identity,source_id,source_order,event,record_json) '
                f"VALUES((SELECT epoch FROM portfolio_source_coverage WHERE id=1),'{route}',"
                f"{prefix}.{identity},{prefix}.id,{prefix}.rowid,'{event}',{record}); END")
    return result


def coverage(store):
    exists = store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='portfolio_source_coverage'").fetchone()
    if not exists:
        return None
    row = store.db.execute('SELECT * FROM portfolio_source_coverage WHERE id=1').fetchone()
    if not row or row['version'] != VERSION:
        return None
    expected = _triggers(store)
    actual = {r['name']: r['sql'] for r in store.db.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'portfolio_history_%'")}
    if any(actual.get(name) != sql for name, sql in expected.items()):
        return None
    return dict(row)


def ensure(store):
    """Called within the portfolio publication transaction, never backdate coverage."""
    if not store.db.in_transaction:
        raise RuntimeError('来源历史初始化必须与组合发布处于同一事务')
    store.db.execute('''CREATE TABLE IF NOT EXISTS portfolio_source_coverage(
        id INTEGER PRIMARY KEY CHECK(id=1), version TEXT NOT NULL,
        epoch TEXT NOT NULL, started_at TEXT NOT NULL, observed_through TEXT)''')
    store.db.execute(f'''CREATE TABLE IF NOT EXISTS portfolio_source_events(
        seq INTEGER PRIMARY KEY AUTOINCREMENT, epoch TEXT NOT NULL, route TEXT NOT NULL,
        identity TEXT NOT NULL, source_id TEXT NOT NULL, source_order INTEGER NOT NULL,
        recorded_at TEXT NOT NULL DEFAULT ({CLOCK}), observed_at TEXT,
        event TEXT NOT NULL, record_json TEXT NOT NULL)''')
    store.db.execute('CREATE INDEX IF NOT EXISTS portfolio_source_event_lookup ON portfolio_source_events(epoch,route,identity,seq)')
    store.db.execute('''CREATE TABLE IF NOT EXISTS portfolio_history_publications(
        decision_id TEXT PRIMARY KEY, epoch TEXT NOT NULL, cursor INTEGER NOT NULL, observed_at TEXT NOT NULL)''')
    store.db.execute('''CREATE TABLE IF NOT EXISTS portfolio_history_snapshots(
        decision_id TEXT PRIMARY KEY, history_json TEXT NOT NULL)''')
    if coverage(store):
        return
    # A missing/replaced trigger creates a new continuity boundary. Old events
    # remain available, but no old snapshot may silently bridge an unknown gap.
    for name, sql in _triggers(store).items():
        store.db.execute('DROP TRIGGER IF EXISTS ' + name)
        store.db.execute(sql)
    store.db.execute('INSERT OR REPLACE INTO portfolio_source_coverage VALUES(1,?,?,?,NULL)',
                     (VERSION, uuid.uuid4().hex, _clock(store)))


def freeze(store, decisions, decision_id):
    """Full sources and cursor from the same locked transaction as the decision."""
    if not store.db.in_transaction:
        raise RuntimeError('来源快照必须与组合发布处于同一事务')
    proof = coverage(store)
    if not proof:
        raise RuntimeError('来源变更日志连续性未建立')
    from .portfolio_strategy import source, row_token
    frozen = {}
    for decision in decisions:
        row = source(store, decision['route'], decision['identity'])
        if row_token(row) != decision['source_token']:
            raise RuntimeError('冻结时研究来源已改变')
        table = TABLES[decision['route']][0]
        order = store.db.execute('SELECT rowid FROM ' + table + ' WHERE id=?', (row['id'],)).fetchone()[0] if row else None
        frozen[decision['key']] = {'record': dict(row) if row else None, 'source_order': order}
    cursor = store.db.execute('SELECT coalesce(max(seq),0) FROM portfolio_source_events').fetchone()[0]
    history = {'version': VERSION, 'decision_id': decision_id,
            'epoch': proof['epoch'], 'coverage_started_at': proof['started_at'],
            'frozen_at': _clock(store), 'cursor': cursor, 'sources': frozen}
    store.db.execute('INSERT INTO portfolio_history_snapshots VALUES(?,?)',
                     (decision_id, json.dumps(history, ensure_ascii=False, sort_keys=True)))
    return history


def snapshot(store, decision_id):
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='portfolio_history_snapshots'").fetchone():
        return None
    row = store.db.execute('SELECT history_json FROM portfolio_history_snapshots WHERE decision_id=?',
                          (decision_id,)).fetchone()
    return json.loads(row[0]) if row else None


def observe(store):
    """Upper-bound availability after acquiring a fresh write transaction.

    A trigger timestamp is only a lower bound: its transaction may commit later.
    A cutoff between these bounds is explicitly unscorable. Never run this inside
    the publisher's transaction, where uncommitted events would look available.
    """
    if store.db.in_transaction:
        raise RuntimeError('来源可见性标记必须在发布事务提交后单独记录')
    if not coverage(store):
        return
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        stamp = _clock(store)
        store.db.execute('UPDATE portfolio_source_events SET observed_at=? WHERE observed_at IS NULL', (stamp,))
        for row in store.db.execute('''SELECT p.id,h.history_json FROM portfolio_decisions p
                JOIN portfolio_history_snapshots h ON h.decision_id=p.id
                WHERE p.id NOT IN (SELECT decision_id FROM portfolio_history_publications)''').fetchall():
            history = json.loads(row['history_json'])
            store.db.execute('INSERT INTO portfolio_history_publications VALUES(?,?,?,?)',
                             (row['id'], history['epoch'], history['cursor'], stamp))
        store.db.execute('UPDATE portfolio_source_coverage SET observed_through=? WHERE id=1', (stamp,))


def source_at(store, history, item, cutoff):
    """Return (record, reason); reason means the history cannot prove the source."""
    if not isinstance(history, dict):
        return None, 'SOURCE_HISTORY_MALFORMED'
    proof = coverage(store)
    if not proof or history.get('version') != VERSION or history.get('epoch') != proof['epoch']:
        return None, 'SOURCE_HISTORY_CONTINUITY_UNKNOWN'
    try:
        at = datetime.fromisoformat(cutoff)
        frozen_at = datetime.fromisoformat(history['frozen_at'])
        if (datetime.fromisoformat(proof['started_at']) > frozen_at or frozen_at >= at
                or history['coverage_started_at'] != proof['started_at']):
            return None, 'SOURCE_HISTORY_OUTSIDE_COVERAGE'
        if not proof['observed_through'] or datetime.fromisoformat(proof['observed_through']) < at:
            return None, 'SOURCE_HISTORY_NOT_OBSERVED_THROUGH_CUTOFF'
        cursor = history['cursor']
        if type(cursor) is not int or cursor < 0:
            return None, 'SOURCE_HISTORY_INVALID_CURSOR'
        high = store.db.execute('SELECT coalesce(max(seq),0) FROM portfolio_source_events').fetchone()[0]
        if cursor > high:
            return None, 'SOURCE_HISTORY_INVALID_CURSOR'
        published = store.db.execute('SELECT * FROM portfolio_history_publications WHERE decision_id=?',
                                     (history['decision_id'],)).fetchone()
        if (not published or published['epoch'] != proof['epoch'] or published['cursor'] != cursor
                or datetime.fromisoformat(published['observed_at']) >= at):
            return None, 'SOURCE_PUBLICATION_TIME_UNPROVEN'
        baseline = history['sources'][item['key']]
        record = baseline['record']
        from .portfolio_strategy import row_token
        if row_token(record) != item.get('source_token') or (record or {}).get('id') != item.get('source_id'):
            return None, 'SOURCE_SNAPSHOT_TOKEN_MISMATCH'
        records = {record['id']: (record, baseline['source_order'])} if record else {}
        clock = TABLES[item['route']][2]

        def latest():
            rows = [(row, order) for row, order in records.values() if datetime.fromisoformat(row[clock]) < at
                    and (item['route'] != 'watchlist' or json.loads(row['payload_json']).get('kind') != 'RISK_EXIT_ONLY')]
            return max(rows, key=lambda pair: (pair[0][clock], pair[1]))[0] if rows else None

        # Sequence numbers resolve same-clock-tick writes. Strict cutoff excludes
        # an update at the opening instant itself, whose ordering is unknowable.
        for event in store.db.execute('''SELECT * FROM portfolio_source_events
                WHERE epoch=? AND route=? AND identity=? AND seq>? ORDER BY seq''',
                (proof['epoch'], item['route'], item['identity'], cursor)):
            recorded_at = datetime.fromisoformat(event['recorded_at'])
            if recorded_at < frozen_at:
                return None, 'SOURCE_HISTORY_CLOCK_REVERSED'
            if recorded_at >= at:
                continue
            if not event['observed_at'] or datetime.fromisoformat(event['observed_at']) >= at:
                return None, 'SOURCE_COMMIT_TIME_UNPROVEN'
            if datetime.fromisoformat(event['observed_at']) < recorded_at:
                return None, 'SOURCE_HISTORY_CLOCK_REVERSED'
            if event['event'] == 'DELETE':
                records.pop(event['source_id'], None)
            else:
                records[event['source_id']] = (json.loads(event['record_json']), event['source_order'])
            # An intermediate source change also invalidates the frozen decision;
            # deletion/reinsertion or changing back cannot resurrect permission.
            if row_token(latest()) != item.get('source_token'):
                return None, 'SOURCE_CHANGED_SINCE_DECISION'
        return latest(), None
    except (KeyError, TypeError, ValueError):
        return None, 'SOURCE_HISTORY_MALFORMED'


def legacy_source_at(store, route, identity, cutoff):
    """Only immutable legacy fields plus recorded lifecycle history are reconstructible."""
    if route == 'dynamic':
        return None, 'DYNAMIC_SOURCE_HISTORY_MISSING'
    if route not in TABLES:
        return None, 'SOURCE_ROUTE_UNKNOWN'
    table, key, clock = TABLES[route]
    extra = " AND json_extract(payload_json,'$.kind')!='RISK_EXIT_ONLY'" if route == 'watchlist' else ''
    row = store.db.execute(f'SELECT * FROM {table} WHERE {key}=? AND {clock}<?' + extra +
                           f' ORDER BY {clock} DESC,rowid DESC LIMIT 1', (identity, cutoff)).fetchone()
    if not row:
        return None, None
    row = dict(row)
    if route == 'watchlist':
        event = store.db.execute('''SELECT status FROM plan_events WHERE plan_id=? AND at<?
            ORDER BY at DESC,id DESC LIMIT 1''', (row['id'], cutoff)).fetchone()
        if not event:
            return None, 'PLAN_LIFECYCLE_HISTORY_MISSING'
        row['status'] = event[0]
    elif row['status'] in ('ACTIVE', 'SUPERSEDED'):
        # Global rows only transition from ACTIVE to SUPERSEDED upon insertion
        # of the next version; the latest row before this cutoff was ACTIVE.
        row['status'] = 'ACTIVE'
    else:
        return None, 'GLOBAL_LIFECYCLE_HISTORY_MISSING'
    return row, None
