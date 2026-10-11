"""Cloud-authoritative simulated withdrawals; legacy paper records stay intact."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS withdrawal_cycles(
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES paper_accounts(id),
 stage INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(account_id,stage));
CREATE TABLE IF NOT EXISTS withdrawal_plans(
 request_id TEXT PRIMARY KEY REFERENCES approval_requests(id),
 cycle_id TEXT NOT NULL REFERENCES withdrawal_cycles(id), action TEXT NOT NULL,
 created_at TEXT NOT NULL, snapshot_hash TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS withdrawal_events(
 id INTEGER PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES withdrawal_cycles(id),
 at TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS withdrawal_transfers(
 id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES withdrawal_cycles(id),
 request_id TEXT NOT NULL UNIQUE REFERENCES approval_requests(id), amount_cents INTEGER NOT NULL,
 flow_id TEXT NOT NULL UNIQUE REFERENCES paper_flows(id), created_at TEXT NOT NULL,
 payload_json TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS withdrawal_plan_update BEFORE UPDATE ON withdrawal_plans
 BEGIN SELECT RAISE(ABORT,'withdrawal plans are immutable'); END;
CREATE TRIGGER IF NOT EXISTS withdrawal_plan_delete BEFORE DELETE ON withdrawal_plans
 BEGIN SELECT RAISE(ABORT,'withdrawal plans are immutable'); END;
CREATE TRIGGER IF NOT EXISTS withdrawal_event_update BEFORE UPDATE ON withdrawal_events
 BEGIN SELECT RAISE(ABORT,'withdrawal events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS withdrawal_event_delete BEFORE DELETE ON withdrawal_events
 BEGIN SELECT RAISE(ABORT,'withdrawal events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS withdrawal_transfer_update BEFORE UPDATE ON withdrawal_transfers
 BEGIN SELECT RAISE(ABORT,'withdrawal transfers are immutable'); END;
CREATE TRIGGER IF NOT EXISTS withdrawal_transfer_delete BEFORE DELETE ON withdrawal_transfers
 BEGIN SELECT RAISE(ABORT,'withdrawal transfers are immutable'); END;
"""
