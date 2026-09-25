from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_time(value):
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if isinstance(value, bytes):
        tmp.write_bytes(value)
    else:
        tmp.write_text(value, encoding="utf-8")
    tmp.replace(path)


def json_write(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2))


def tokenize(text):
    """版本 cn-bigram-v1；无下载依赖，适合第一版中文词法检索。"""
    words = re.findall(r"[a-z0-9_]+", text.lower())
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        words.extend(run[i:i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            words.append(run)
    return " ".join(words)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, job_key TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
 started_at TEXT NOT NULL, as_of TEXT, finished_at TEXT, status TEXT NOT NULL,
 model_status TEXT, error TEXT, report_path TEXT);
CREATE TABLE IF NOT EXISTS source_checks(
 id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), source TEXT NOT NULL,
 symbol TEXT, status TEXT NOT NULL, detail TEXT NOT NULL, checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quotes(
 id TEXT PRIMARY KEY, symbol TEXT NOT NULL, name TEXT NOT NULL, price_cents INTEGER NOT NULL,
 prev_close_cents INTEGER NOT NULL, observed_at TEXT NOT NULL, first_seen_at TEXT NOT NULL,
 source TEXT NOT NULL, raw_path TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS quotes_lookup ON quotes(symbol,observed_at);
CREATE TABLE IF NOT EXISTS documents(
 id TEXT PRIMARY KEY, symbol TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
 source TEXT NOT NULL, url TEXT NOT NULL, published_at TEXT NOT NULL, time_precision TEXT NOT NULL,
 first_seen_at TEXT NOT NULL, available_at TEXT NOT NULL, content_hash TEXT NOT NULL,
 raw_path TEXT NOT NULL, extraction_quality TEXT NOT NULL, cloud_allowed INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS docs_lookup ON documents(symbol,available_at);
CREATE TABLE IF NOT EXISTS chunks(
 id TEXT PRIMARY KEY, doc_id TEXT NOT NULL REFERENCES documents(id), page INTEGER,
 ordinal INTEGER NOT NULL, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chunks_document_order ON chunks(doc_id,ordinal);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, tokens);
CREATE TABLE IF NOT EXISTS research(
 run_id TEXT PRIMARY KEY REFERENCES runs(id), created_at TEXT NOT NULL,
 packet_hash TEXT NOT NULL, result_json TEXT NOT NULL, model_status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_accounts(
 id TEXT PRIMARY KEY, cash_cents INTEGER NOT NULL, initial_cents INTEGER NOT NULL,
 current_stage INTEGER NOT NULL DEFAULT 1, withdrawn_cents INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS paper_withdrawals(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES paper_accounts(id), stage INTEGER NOT NULL,
 threshold_cents INTEGER NOT NULL, retain_cents INTEGER NOT NULL, equity_cents INTEGER NOT NULL,
 planned_cents INTEGER NOT NULL, transferred_cents INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT,
 UNIQUE(account_id,stage));
CREATE TABLE IF NOT EXISTS paper_flows(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES paper_accounts(id), kind TEXT NOT NULL,
 amount_cents INTEGER NOT NULL, reference TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
INSERT OR IGNORE INTO metadata VALUES('schema_version','1');
INSERT OR IGNORE INTO metadata VALUES('tokenizer_version','cn-bigram-v1');
"""


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "agent.sqlite3", timeout=20)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        from .migrations import migrate
        migrate(self.db, now(), digest)
        from .cloud_schema import SCHEMA as CLOUD_SCHEMA
        self.db.executescript(CLOUD_SCHEMA)

    def raw(self, data, suffix=".bin"):
        h = digest(data)
        path = self.root / "raw" / h[:2] / (h + suffix)
        if not path.exists():
            atomic_write(path, data)
        return str(path.relative_to(self.root))

    def record_attempt(self, source, symbol, status, detail='', *, resource_key='', title='', run_id=None, at=None):
        self.db.execute('INSERT INTO data_attempts(source,symbol,resource_key,title,status,detail,checked_at,run_id) VALUES(?,?,?,?,?,?,?,?)',
                        (source,symbol or 'MARKET',resource_key,title,status,str(detail)[:1000],normalize_time(at or now()),run_id))
        self.db.commit()

    def check(self, run_id, source, symbol, status, detail, *, track=True):
        self.db.execute("INSERT INTO source_checks(run_id,source,symbol,status,detail,checked_at) VALUES(?,?,?,?,?,?)",
                        (run_id, source, symbol, status, detail, now()))
        self.db.commit()
        if track:
            self.record_attempt(source,symbol,status,detail,run_id=run_id)

    def add_document(self, *, symbol, kind, title, source, url, published_at,
                     pages, raw_path, first_seen_at=None, time_precision="second",
                     quality="text", cloud_allowed=False, ready_at=None, reference_period=None):
        seen = normalize_time(first_seen_at or now())
        published = normalize_time(published_at)
        # 日期精度用系统首次实际获取时刻作为可用时间，不伪造历史可得性。
        available = seen if time_precision == "date" else max(published, seen)
        full_text = "\n".join(t for _, t in pages)
        content_hash = digest(title + "\n" + full_text)
        ready = max(normalize_time(ready_at or now()), available)
        family = digest(url + '|' + symbol + '|' + kind)[:24]
        prior = self.db.execute("SELECT d.* FROM documents d JOIN document_meta m ON m.doc_id=d.id WHERE m.family_id=? ORDER BY m.ready_at DESC,d.rowid DESC LIMIT 1", (family,)).fetchone()
        if prior and prior['content_hash'] == content_hash:
            return prior['id'], False
        doc_id = digest(family + '|' + content_hash + '|' + ready + '|' + (prior['id'] if prior else ''))[:24]
        with self.db:
            self.db.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (doc_id, symbol, kind, title, source, url, published, time_precision,
                             seen, available, content_hash, raw_path, quality, int(cloud_allowed)))
            self.db.execute("INSERT INTO document_meta VALUES(?,?,?,?,?,?,?,?)",
                (doc_id, family, ready, 'OPINION' if kind == 'broker_report' else 'FACT',
                 content_hash, reference_period, prior['id'] if prior else None, quality))
            ordinal = 0
            for page, text in pages:
                text = text.strip()
                for offset in range(0, len(text), 720):
                    fragment = text[offset:offset + 800]
                    if not fragment.strip():
                        continue
                    cid = doc_id + ":" + str(ordinal)
                    self.db.execute("INSERT INTO chunks VALUES(?,?,?,?,?)", (cid, doc_id, page, ordinal, fragment))
                    self.db.execute("INSERT INTO chunks_fts VALUES(?,?)", (cid, tokenize(title + " " + fragment)))
                    ordinal += 1
        return doc_id, True

    def search(self, query, as_of, symbol=None, limit=10, cloud_only=False):
        tokens = list(dict.fromkeys(tokenize(query).split()))[:32]
        if not tokens:
            return []
        match = " OR ".join('"' + x.replace('"', '""') + '"' for x in tokens)
        sql = """SELECT c.id AS evidence_id,c.doc_id,c.page,c.text,d.symbol,d.kind,d.title,
          d.url,d.published_at,d.first_seen_at,d.available_at,d.extraction_quality,
          bm25(chunks_fts) AS rank FROM chunks_fts
          JOIN chunks c ON c.id=chunks_fts.chunk_id JOIN documents d ON d.id=c.doc_id
          JOIN document_meta m ON m.doc_id=d.id
          WHERE chunks_fts MATCH ? AND d.available_at<=? AND m.ready_at<=?
          AND NOT EXISTS(SELECT 1 FROM document_meta newer JOIN documents nd ON nd.id=newer.doc_id
             WHERE newer.family_id=m.family_id AND newer.ready_at<=? AND nd.available_at<=?
             AND (newer.ready_at>m.ready_at OR (newer.ready_at=m.ready_at AND nd.rowid>d.rowid)))"""
        args = [match] + [normalize_time(as_of)] * 4
        if symbol:
            sql += " AND d.symbol IN (?, 'MARKET')"
            args.append(symbol)
        if cloud_only:
            sql += " AND d.cloud_allowed=1"
        sql += " ORDER BY rank,d.available_at DESC,c.id LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.execute(sql, args)]

    def documents_as_of(self, as_of, symbol=None):
        sql = """SELECT d.*,m.ready_at,m.family_id,m.claim_type,m.event_cluster_id,m.reference_period,m.revision_of
          FROM documents d JOIN document_meta m ON m.doc_id=d.id
          WHERE d.available_at<=? AND m.ready_at<=?
          AND NOT EXISTS(SELECT 1 FROM document_meta n JOIN documents nd ON nd.id=n.doc_id
            WHERE n.family_id=m.family_id AND n.ready_at<=? AND nd.available_at<=?
            AND (n.ready_at>m.ready_at OR (n.ready_at=m.ready_at AND nd.rowid>d.rowid)))"""
        args = [normalize_time(as_of)] * 4
        if symbol:
            sql += " AND d.symbol=?"
            args.append(symbol)
        return [dict(r) for r in self.db.execute(sql + " ORDER BY published_at DESC,id", args)]

    def latest_trade_check(self, symbol, at=None):
        """One current check; legacy history is a read-only fallback during upgrades."""
        stamp=normalize_time(at or now())
        row=self.db.execute('''SELECT 'latest-check:'||symbol AS id,at,action,status,reason,payload_json
            FROM latest_trade_checks WHERE symbol=? AND at<=?
            UNION ALL SELECT id,at,action,status,reason,payload_json FROM decisions WHERE symbol=? AND at<=?
            ORDER BY at DESC LIMIT 1''',(symbol,stamp,symbol,stamp)).fetchone()
        return dict(row) if row else None

    def latest_quote(self, symbol, as_of):
        row = self.db.execute("SELECT * FROM quotes WHERE symbol=? AND first_seen_at<=? AND observed_at<=? ORDER BY observed_at DESC,first_seen_at DESC LIMIT 1",
                              (symbol, normalize_time(as_of), normalize_time(as_of))).fetchone()
        return dict(row) if row else None

    def backup(self):
        path = self.root / "backups" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".sqlite3")
        path.parent.mkdir(exist_ok=True)
        with sqlite3.connect(path) as dest:
            self.db.backup(dest)
        return path

    def periodic_backup(self,at=None,hourly_keep=6,daily_keep=7):
        """Minute jobs share one atomic hourly backup; preserve manual backup files.
        Retention: the newest `hourly_keep` hourly files plus one file per day for `daily_keep` days."""
        import fcntl
        stamp=datetime.fromisoformat(normalize_time(at or now()))
        folder=self.root/'backups'/'hourly';folder.mkdir(parents=True,exist_ok=True)
        path=folder/(stamp.strftime('%Y%m%dT%H0000Z')+'.sqlite3')
        with (folder/'.lock').open('a+') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return None
            if path.exists():return path
            if self.db.in_transaction:raise RuntimeError('自动备份须在事务提交后进行')
            temp=path.with_suffix('.tmp')
            try:
                with sqlite3.connect(temp) as dest:self.db.backup(dest)
                temp.replace(path)
            finally:temp.unlink(missing_ok=True)
            from .maintenance import backup_retention
            backup_retention(folder,hourly_keep,daily_keep)
            return path

    def close(self):
        self.db.close()
