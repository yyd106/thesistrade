"""Evaluation and governance tables. Additive only; none of them feeds execution directly."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS builds(id TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS strategy_guidance(
 id TEXT PRIMARY KEY, route TEXT NOT NULL, scope TEXT NOT NULL, text TEXT NOT NULL,
 status TEXT NOT NULL, proposal_id TEXT, adopted_at TEXT NOT NULL, retired_at TEXT,
 approved_by TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS strategy_proposals(
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
 target TEXT NOT NULL, status TEXT NOT NULL, title TEXT NOT NULL, payload_json TEXT NOT NULL,
 decided_at TEXT, decided_by TEXT, decision_note TEXT, dedupe_key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS engineering_issues(
 id TEXT PRIMARY KEY, issue_key TEXT NOT NULL, category TEXT NOT NULL, symbol TEXT NOT NULL,
 status TEXT NOT NULL, title TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 occurrences INTEGER NOT NULL, payload_json TEXT NOT NULL, resolved_at TEXT, resolution TEXT);
CREATE TABLE IF NOT EXISTS signal_registry(
 id TEXT PRIMARY KEY, route TEXT NOT NULL, symbol TEXT NOT NULL, source_id TEXT NOT NULL,
 created_at TEXT NOT NULL, as_of_day TEXT, build_id TEXT, horizon_days INTEGER NOT NULL,
 benchmark TEXT, status TEXT NOT NULL, judgment_json TEXT NOT NULL, score_json TEXT, scored_at TEXT);
CREATE INDEX IF NOT EXISTS signal_registry_open ON signal_registry(status,route);
CREATE INDEX IF NOT EXISTS signal_registry_symbol ON signal_registry(symbol,created_at);
CREATE TABLE IF NOT EXISTS signal_scores(
 signal_id TEXT NOT NULL, method TEXT NOT NULL, status TEXT NOT NULL,
 score_json TEXT NOT NULL, scored_at TEXT NOT NULL,
 PRIMARY KEY(signal_id,method));
CREATE TABLE IF NOT EXISTS shadow_evaluations(
 id TEXT PRIMARY KEY, method TEXT NOT NULL, computed_at TEXT NOT NULL,
 payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shadow_days_v2(
 run_id TEXT NOT NULL, book TEXT NOT NULL, day TEXT NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(run_id,book,day));
CREATE TABLE IF NOT EXISTS shadow_trades_v2(
 run_id TEXT NOT NULL, id TEXT NOT NULL, book TEXT NOT NULL, day TEXT NOT NULL,
 symbol TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL, price_cents INTEGER NOT NULL,
 fee_cents INTEGER NOT NULL, reason TEXT NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(run_id,id));
CREATE INDEX IF NOT EXISTS shadow_trades_v2_book ON shadow_trades_v2(run_id,book,day);
CREATE TABLE IF NOT EXISTS shadow_book_days(
 book TEXT NOT NULL, day TEXT NOT NULL, computed_at TEXT NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(book,day));
CREATE TABLE IF NOT EXISTS shadow_trades(
 id TEXT PRIMARY KEY, book TEXT NOT NULL, day TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
 qty REAL NOT NULL, price_cents INTEGER NOT NULL, fee_cents INTEGER NOT NULL, reason TEXT NOT NULL,
 payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS shadow_trades_book ON shadow_trades(book,day);
CREATE TABLE IF NOT EXISTS quote_health(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, started_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, ended_at TEXT,
 symbols_json TEXT NOT NULL, held_json TEXT NOT NULL, detail TEXT NOT NULL, check_json TEXT, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS quote_health_time ON quote_health(started_at);
CREATE INDEX IF NOT EXISTS quote_health_updated ON quote_health(updated_at);
CREATE TABLE IF NOT EXISTS notices(
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, author TEXT NOT NULL, kind TEXT NOT NULL,
 title TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, acked_at TEXT, delivered_at TEXT,
 payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluation_batches(
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, trigger TEXT NOT NULL, period_start TEXT NOT NULL,
 period_end TEXT NOT NULL, trading_days INTEGER NOT NULL, status TEXT NOT NULL, manifest_json TEXT NOT NULL,
 annex_json TEXT NOT NULL);
"""
