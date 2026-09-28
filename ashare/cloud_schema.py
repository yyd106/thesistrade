SCHEMA='''
CREATE TABLE IF NOT EXISTS cloud_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_nonces(nonce TEXT PRIMARY KEY,received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_receipts(sequence INTEGER PRIMARY KEY,bundle_id TEXT UNIQUE NOT NULL,received_at TEXT NOT NULL,completed_at TEXT NOT NULL,payload_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_contracts(key TEXT PRIMARY KEY,updated_at TEXT NOT NULL,payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_outbox(id TEXT PRIMARY KEY,created_at TEXT NOT NULL,status TEXT NOT NULL,payload_json TEXT NOT NULL,error TEXT);
CREATE TABLE IF NOT EXISTS supervision_reviews(
 id TEXT PRIMARY KEY,kind TEXT NOT NULL,subject_id TEXT NOT NULL,input_hash TEXT NOT NULL,
 created_at TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TEXT,finished_at TEXT,error TEXT,input_json TEXT NOT NULL,result_json TEXT);
CREATE INDEX IF NOT EXISTS supervision_pending ON supervision_reviews(status,next_attempt_at);
'''
