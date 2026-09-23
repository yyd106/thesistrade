SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_risk(id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portfolio_risk_events(id TEXT PRIMARY KEY, at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan_rechecks(plan_id TEXT PRIMARY KEY, checked_at TEXT NOT NULL, valid_until TEXT NOT NULL, fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS global_quotes(id TEXT PRIMARY KEY, symbol TEXT NOT NULL, observed_at TEXT NOT NULL, first_seen_at TEXT NOT NULL, price_micros INTEGER NOT NULL CHECK(price_micros>0), fx_micros INTEGER NOT NULL CHECK(fx_micros>0), fx_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS global_quote_time ON global_quotes(symbol,observed_at);
CREATE TABLE IF NOT EXISTS global_market(symbol TEXT PRIMARY KEY, checked_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS global_plans(id TEXT PRIMARY KEY, symbol TEXT NOT NULL, created_at TEXT NOT NULL, valid_until TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS global_plan_time ON global_plans(symbol,created_at);
CREATE TABLE IF NOT EXISTS global_orders(id TEXT PRIMARY KEY, intent_key TEXT UNIQUE NOT NULL, plan_id TEXT, symbol TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('BUY','SELL')), qty INTEGER NOT NULL CHECK(qty>0), filled_qty INTEGER NOT NULL DEFAULT 0, limit_micros INTEGER NOT NULL CHECK(limit_micros>0), reserved_cents INTEGER NOT NULL CHECK(reserved_cents>=0), created_at TEXT NOT NULL, expires_at TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL, CHECK(filled_qty>=0 AND filled_qty<=qty));
CREATE TABLE IF NOT EXISTS global_fills(id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES global_orders(id), quote_id TEXT NOT NULL REFERENCES global_quotes(id), symbol TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL, price_micros INTEGER NOT NULL, fx_micros INTEGER NOT NULL, gross_cents INTEGER NOT NULL, fee_cents INTEGER NOT NULL, realized_cents INTEGER NOT NULL, occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL, UNIQUE(order_id,quote_id));
CREATE TABLE IF NOT EXISTS global_lots(id TEXT PRIMARY KEY REFERENCES global_fills(id), symbol TEXT NOT NULL, qty INTEGER NOT NULL CHECK(qty>=0), cost_cents INTEGER NOT NULL CHECK(cost_cents>=0), acquired_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_methods(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_improvements(id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES reviews(id), created_at TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL);
"""

SCHEMA += """
CREATE TABLE IF NOT EXISTS portfolio_runs(id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, error TEXT, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portfolio_decisions(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, valid_until TEXT NOT NULL, status TEXT NOT NULL, version TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portfolio_order_events(id TEXT PRIMARY KEY, decision_id TEXT NOT NULL, route TEXT NOT NULL, order_id TEXT NOT NULL, at TEXT NOT NULL, reason TEXT NOT NULL);
"""
