"""Additive approval records. Requests, receipts and consumptions are append-only."""
SCHEMA = '''
CREATE TABLE IF NOT EXISTS approval_requests(
 id TEXT PRIMARY KEY,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,
 kind TEXT NOT NULL,subject_id TEXT NOT NULL,action TEXT NOT NULL,
 request_hash TEXT NOT NULL,payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approval_receipts(
 id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,request_hash TEXT NOT NULL,
 decision TEXT NOT NULL,actor TEXT NOT NULL,authority TEXT NOT NULL,issued_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approval_consumptions(
 request_id TEXT PRIMARY KEY,consumed_at TEXT NOT NULL,result_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approval_reservations(
 request_id TEXT NOT NULL,operation_id TEXT NOT NULL,reserved_at TEXT NOT NULL,released_at TEXT,
 PRIMARY KEY(request_id,operation_id));
CREATE UNIQUE INDEX IF NOT EXISTS approval_active_reservation ON approval_reservations(request_id)
 WHERE released_at IS NULL;
'''
