"""Append-only local forward trials; never part of the execution ledger."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS experiment_runs(
 experiment_id TEXT PRIMARY KEY REFERENCES experiment_designs(id),
 started_at TEXT NOT NULL, manifest_hash TEXT NOT NULL, manifest_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS experiment_pairs(
 id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES experiment_runs(experiment_id),
 window_index INTEGER NOT NULL, enrolled_at TEXT NOT NULL, as_of TEXT NOT NULL,
 symbol TEXT NOT NULL, snapshot_id TEXT NOT NULL, input_hash TEXT NOT NULL, input_json TEXT NOT NULL,
 baseline_prompt TEXT NOT NULL, candidate_prompt TEXT NOT NULL,
 event_key TEXT NOT NULL, target_json TEXT NOT NULL, day_key TEXT NOT NULL,
 UNIQUE(experiment_id,snapshot_id), UNIQUE(experiment_id,event_key), UNIQUE(experiment_id,symbol,day_key));
CREATE TABLE IF NOT EXISTS experiment_attempts(
 id TEXT PRIMARY KEY, pair_id TEXT NOT NULL REFERENCES experiment_pairs(id),
 arm TEXT NOT NULL, ordinal INTEGER NOT NULL, started_at TEXT NOT NULL,
 UNIQUE(pair_id,arm,ordinal));
CREATE TABLE IF NOT EXISTS experiment_results(
 attempt_id TEXT PRIMARY KEY REFERENCES experiment_attempts(id), finished_at TEXT NOT NULL,
 status TEXT NOT NULL, result_json TEXT NOT NULL, measurement_json TEXT NOT NULL,
 error_code TEXT, meta_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS experiment_pair_events(
 id INTEGER PRIMARY KEY, pair_id TEXT NOT NULL REFERENCES experiment_pairs(id),
 at TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS experiment_conclusions(
 experiment_id TEXT PRIMARY KEY REFERENCES experiment_runs(experiment_id),
 at TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, summary_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS experiment_sources(
 experiment_id TEXT NOT NULL REFERENCES experiment_runs(experiment_id), source_key TEXT NOT NULL,
 pair_id TEXT NOT NULL REFERENCES experiment_pairs(id), PRIMARY KEY(experiment_id,source_key));
CREATE INDEX IF NOT EXISTS experiment_pairs_run ON experiment_pairs(experiment_id,enrolled_at);
"""
for _table in ('experiment_runs', 'experiment_pairs', 'experiment_attempts',
               'experiment_results', 'experiment_pair_events', 'experiment_conclusions', 'experiment_sources'):
    for _action in ('UPDATE', 'DELETE'):
        SCHEMA += f"""
CREATE TRIGGER IF NOT EXISTS frozen_{_table}_{_action.lower()} BEFORE {_action} ON {_table}
 BEGIN SELECT RAISE(ABORT,'forward experiment records are append-only'); END;
"""
