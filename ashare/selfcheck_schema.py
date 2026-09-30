"""Local, additive evidence and experiment-design records. Never execution inputs."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS judgment_contracts(
 id TEXT PRIMARY KEY, route TEXT NOT NULL, symbol TEXT NOT NULL, source_id TEXT NOT NULL,
 created_at TEXT NOT NULL, frozen_at TEXT NOT NULL, provenance TEXT NOT NULL,
 build_id TEXT NOT NULL, horizon_days INTEGER NOT NULL, payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS contracts_route ON judgment_contracts(route,created_at);
CREATE TABLE IF NOT EXISTS selfcheck_runs(
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, evidence_hash TEXT NOT NULL UNIQUE,
 payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS selfcheck_evidence(
 proposal_id TEXT NOT NULL REFERENCES strategy_proposals(id), evidence_hash TEXT NOT NULL,
 run_id TEXT NOT NULL REFERENCES selfcheck_runs(id), created_at TEXT NOT NULL,
 payload_json TEXT NOT NULL, PRIMARY KEY(proposal_id,evidence_hash));
CREATE TABLE IF NOT EXISTS review_observations(
 proposal_id TEXT NOT NULL REFERENCES strategy_proposals(id), review_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL, created_at TEXT NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(review_id,ordinal));
CREATE INDEX IF NOT EXISTS review_observations_proposal ON review_observations(proposal_id);
CREATE TABLE IF NOT EXISTS experiment_designs(
 id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE REFERENCES strategy_proposals(id),
 created_at TEXT NOT NULL, spec_hash TEXT NOT NULL, spec_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS experiment_events(
 id INTEGER PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES experiment_designs(id),
 at TEXT NOT NULL, status TEXT NOT NULL, note TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS frozen_contract_update BEFORE UPDATE ON judgment_contracts
 BEGIN SELECT RAISE(ABORT,'judgment contracts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS frozen_contract_delete BEFORE DELETE ON judgment_contracts
 BEGIN SELECT RAISE(ABORT,'judgment contracts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS frozen_selfcheck_update BEFORE UPDATE ON selfcheck_runs
 BEGIN SELECT RAISE(ABORT,'selfcheck reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS frozen_selfcheck_delete BEFORE DELETE ON selfcheck_runs
 BEGIN SELECT RAISE(ABORT,'selfcheck reports are immutable'); END;
CREATE TRIGGER IF NOT EXISTS frozen_experiment_update BEFORE UPDATE ON experiment_designs
 BEGIN SELECT RAISE(ABORT,'experiment designs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS frozen_experiment_delete BEFORE DELETE ON experiment_designs
 BEGIN SELECT RAISE(ABORT,'experiment designs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS frozen_experiment_event_update BEFORE UPDATE ON experiment_events
 BEGIN SELECT RAISE(ABORT,'experiment events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS frozen_experiment_event_delete BEFORE DELETE ON experiment_events
 BEGIN SELECT RAISE(ABORT,'experiment events are append-only'); END;
"""
