"""Additive migrations: preserve the 0.1 database and do not invent historical ready times."""
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS document_meta(
 doc_id TEXT PRIMARY KEY REFERENCES documents(id), family_id TEXT NOT NULL,
 ready_at TEXT NOT NULL, claim_type TEXT NOT NULL, event_cluster_id TEXT NOT NULL,
 reference_period TEXT, revision_of TEXT, validation_status TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS document_family ON document_meta(family_id,ready_at);
CREATE TABLE IF NOT EXISTS batches(
 id TEXT PRIMARY KEY REFERENCES runs(id), created_at TEXT NOT NULL,
 finished_at TEXT, status TEXT NOT NULL, config_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batch_stocks(
 batch_id TEXT REFERENCES batches(id), symbol TEXT NOT NULL, ready_at TEXT NOT NULL,
 status TEXT NOT NULL, checks_json TEXT NOT NULL, PRIMARY KEY(batch_id,symbol));
CREATE TABLE IF NOT EXISTS snapshots(
 id TEXT PRIMARY KEY, batch_id TEXT REFERENCES batches(id), symbol TEXT NOT NULL,
 as_of TEXT NOT NULL, created_at TEXT NOT NULL, packet_hash TEXT NOT NULL, packet_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshot_members(
 snapshot_id TEXT REFERENCES snapshots(id), doc_id TEXT REFERENCES documents(id),
 PRIMARY KEY(snapshot_id,doc_id));
CREATE TRIGGER IF NOT EXISTS frozen_snapshot_update BEFORE UPDATE ON snapshots
 BEGIN SELECT RAISE(ABORT,'snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS frozen_snapshot_delete BEFORE DELETE ON snapshots
 BEGIN SELECT RAISE(ABORT,'snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS frozen_member_update BEFORE UPDATE ON snapshot_members
 BEGIN SELECT RAISE(ABORT,'snapshot membership is immutable'); END;
CREATE TRIGGER IF NOT EXISTS frozen_member_delete BEFORE DELETE ON snapshot_members
 BEGIN SELECT RAISE(ABORT,'snapshot membership is immutable'); END;
CREATE TABLE IF NOT EXISTS studies(
 id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id), symbol TEXT NOT NULL,
 created_at TEXT NOT NULL, model_status TEXT NOT NULL, result_json TEXT NOT NULL,
 UNIQUE(snapshot_id));
CREATE TABLE IF NOT EXISTS plans(
 id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES studies(id), symbol TEXT NOT NULL,
 activated_at TEXT NOT NULL, valid_until TEXT NOT NULL, status TEXT NOT NULL,
 strategy_version TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_plan ON plans(symbol) WHERE status='ACTIVE';
CREATE TABLE IF NOT EXISTS plan_events(
 id INTEGER PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES plans(id), at TEXT NOT NULL,
 status TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS slots(
 id TEXT PRIMARY KEY, scheduled_at TEXT NOT NULL UNIQUE, started_at TEXT NOT NULL,
 finished_at TEXT, status TEXT NOT NULL, input_json TEXT NOT NULL, model_status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions(
 id TEXT PRIMARY KEY, slot_id TEXT NOT NULL REFERENCES slots(id), symbol TEXT NOT NULL,
 plan_id TEXT REFERENCES plans(id), at TEXT NOT NULL, action TEXT NOT NULL,
 status TEXT NOT NULL, reason TEXT NOT NULL, payload_json TEXT NOT NULL,
 UNIQUE(slot_id,symbol));
CREATE INDEX IF NOT EXISTS decisions_latest ON decisions(symbol,at);
CREATE TABLE IF NOT EXISTS latest_trade_checks(
 symbol TEXT PRIMARY KEY, at TEXT NOT NULL, action TEXT NOT NULL,
 status TEXT NOT NULL, reason TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_orders(
 id TEXT PRIMARY KEY, decision_id TEXT NOT NULL UNIQUE REFERENCES decisions(id),
 plan_id TEXT NOT NULL REFERENCES plans(id), symbol TEXT NOT NULL, side TEXT NOT NULL,
 qty INTEGER NOT NULL CHECK(qty>0), filled_qty INTEGER NOT NULL DEFAULT 0,
 limit_cents INTEGER NOT NULL CHECK(limit_cents>0), reserved_cents INTEGER NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, status TEXT NOT NULL,
 CHECK(filled_qty>=0 AND filled_qty<=qty));
CREATE TABLE IF NOT EXISTS paper_fills(
 id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES paper_orders(id),
 quote_id TEXT NOT NULL REFERENCES quotes(id), symbol TEXT NOT NULL, side TEXT NOT NULL,
 qty INTEGER NOT NULL, price_cents INTEGER NOT NULL, fee_cents INTEGER NOT NULL,
 realized_cents INTEGER NOT NULL, occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
 UNIQUE(order_id,quote_id));
CREATE TABLE IF NOT EXISTS paper_order_terms(
 order_id TEXT PRIMARY KEY REFERENCES paper_orders(id), config_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_lots(
 id TEXT PRIMARY KEY REFERENCES paper_fills(id), symbol TEXT NOT NULL, qty INTEGER NOT NULL,
 cost_cents INTEGER NOT NULL, acquired_day TEXT NOT NULL, CHECK(qty>=0), CHECK(cost_cents>=0));
CREATE TABLE IF NOT EXISTS equity_marks(
 id TEXT PRIMARY KEY, at TEXT NOT NULL, recorded_at TEXT NOT NULL,
 cash_cents INTEGER NOT NULL, equity_cents INTEGER NOT NULL, withdrawn_cents INTEGER NOT NULL,
 complete INTEGER NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews(
 id TEXT PRIMARY KEY, window_start TEXT NOT NULL, window_end TEXT NOT NULL,
 revision INTEGER NOT NULL, ready_at TEXT NOT NULL, fingerprint TEXT NOT NULL,
 model_status TEXT NOT NULL, payload_json TEXT NOT NULL,
 UNIQUE(window_start,window_end,fingerprint));
CREATE TABLE IF NOT EXISTS lessons(
 id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES reviews(id), symbol TEXT NOT NULL,
 ready_at TEXT NOT NULL, expires_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, scheduled_at TEXT NOT NULL,
 started_at TEXT, finished_at TEXT, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 result_json TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS service_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS job_inputs(
 job_id TEXT PRIMARY KEY REFERENCES jobs(id), payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS app_users(
 username TEXT PRIMARY KEY, role TEXT NOT NULL, password_hash TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS app_sessions(
 token_hash TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES app_users(username),
 csrf_token TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts(id INTEGER PRIMARY KEY,remote TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feedback(
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL, username TEXT NOT NULL REFERENCES app_users(username),
 nickname TEXT NOT NULL, page TEXT NOT NULL, symbol TEXT, study_id TEXT, topic TEXT NOT NULL,
 body TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL,
 admin_note TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(username,request_id));
CREATE TABLE IF NOT EXISTS followup_items(
 id TEXT PRIMARY KEY, status TEXT NOT NULL, first_seen_at TEXT NOT NULL,
 opened_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, resolved_at TEXT,
 occurrences INTEGER NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS followup_days(
 day TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS data_attempts(
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, symbol TEXT NOT NULL, resource_key TEXT NOT NULL,
 title TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, checked_at TEXT NOT NULL, run_id TEXT);
CREATE INDEX IF NOT EXISTS data_attempt_lookup ON data_attempts(symbol,source,resource_key,id);
CREATE INDEX IF NOT EXISTS data_attempt_time_lookup ON data_attempts(symbol,source,resource_key,checked_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS learned_chunks(
 symbol TEXT NOT NULL, chunk_id TEXT NOT NULL REFERENCES chunks(id),
 study_id TEXT NOT NULL REFERENCES studies(id), learned_at TEXT NOT NULL,
 PRIMARY KEY(symbol,chunk_id));
CREATE INDEX IF NOT EXISTS learned_as_of ON learned_chunks(symbol,learned_at);
CREATE TABLE IF NOT EXISTS fundamental_records(
 doc_id TEXT PRIMARY KEY REFERENCES documents(id), payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS comparison_series(
 run_id TEXT NOT NULL REFERENCES runs(id), symbol TEXT NOT NULL,
 ready_at TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(run_id,symbol));
"""


def migrate(db, timestamp, hash_fn):
    db.executescript(SCHEMA_V2)
    from .dynamic_schema import SCHEMA as DYNAMIC_SCHEMA
    db.executescript(DYNAMIC_SCHEMA)
    from .investment_schema import SCHEMA as INVESTMENT_SCHEMA
    db.executescript(INVESTMENT_SCHEMA)
    from .evaluation_schema import SCHEMA as EVALUATION_SCHEMA
    db.executescript(EVALUATION_SCHEMA)
    from .selfcheck_schema import SCHEMA as SELFCHECK_SCHEMA
    db.executescript(SELFCHECK_SCHEMA)
    # A v1 record proves first retrieval, not completion of extraction.
    for d in db.execute("SELECT * FROM documents WHERE id NOT IN (SELECT doc_id FROM document_meta)").fetchall():
        family = hash_fn(d['url'] + '|' + d['symbol'] + '|' + d['kind'])[:24]
        claim = 'OPINION' if d['kind'] == 'broker_report' else 'FACT'
        db.execute("INSERT INTO document_meta VALUES(?,?,?,?,?,?,?,?)",
                   (d['id'], family, max(timestamp,d['available_at']), claim,
                    d['content_hash'], None, None, 'MIGRATED_READY_TIME_UNKNOWN'))
    if not db.execute("SELECT 1 FROM metadata WHERE key='data_attempts_backfilled'").fetchone():
        # Old logs identify the source, not individual documents. Preserve that granularity.
        for c in db.execute('''SELECT * FROM source_checks WHERE id IN
            (SELECT MAX(id) FROM source_checks GROUP BY source,COALESCE(symbol,'MARKET'))''').fetchall():
            if c['status']=='OK' or c['source']=='cache_only':continue
            if c['source']=='official_news' and ('错误[]' in c['detail'] or '尚未逐条读取' in c['detail']):continue
            db.execute('INSERT INTO data_attempts(source,symbol,resource_key,title,status,detail,checked_at,run_id) VALUES(?,?,?,?,?,?,?,?)',
                (c['source'],c['symbol'] or 'MARKET','','',c['status'],c['detail'],c['checked_at'],c['run_id']))
        db.execute("INSERT INTO metadata VALUES('data_attempts_backfilled','1')")
    if not db.execute("SELECT 1 FROM metadata WHERE key='study_failures_backfilled'").fetchone():
        import json
        for s in db.execute('''SELECT * FROM studies WHERE rowid IN
            (SELECT MAX(rowid) FROM studies GROUP BY symbol) AND model_status='DEFERRED' ''').fetchall():
            db.execute('INSERT INTO data_attempts(source,symbol,resource_key,title,status,detail,checked_at,run_id) VALUES(?,?,?,?,?,?,?,?)',
                ('research_analysis',s['symbol'],'','','FAILED',json.loads(s['result_json']).get('summary',''),s['created_at'],s['id']))
        db.execute("INSERT INTO metadata VALUES('study_failures_backfilled','1')")
    if not db.execute("SELECT 1 FROM metadata WHERE key='learned_chunks_backfilled'").fetchone():
        import json
        # Only actual evidence sent to a successful study counts, never snapshot membership.
        for s in db.execute('''SELECT s.*,sn.packet_json FROM studies s JOIN snapshots sn ON sn.id=s.snapshot_id
            WHERE s.model_status='SUCCEEDED' ORDER BY s.created_at,s.rowid''').fetchall():
            for e in json.loads(s['packet_json']).get('evidence',[]):
                row=db.execute('SELECT text FROM chunks WHERE id=?',(e['evidence_id'],)).fetchone()
                if row and row[0]==e['text']:
                    db.execute('INSERT OR IGNORE INTO learned_chunks VALUES(?,?,?,?)',
                        (s['symbol'],e['evidence_id'],s['id'],s['created_at']))
        db.execute("INSERT INTO metadata VALUES('learned_chunks_backfilled','1')")
    db.execute("UPDATE metadata SET value='5' WHERE key='schema_version'")
    db.commit()
