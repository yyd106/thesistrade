"""Additive, separate storage for the news strategy; no watchlist rows are migrated."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS dynamic_news(
 id TEXT PRIMARY KEY, source TEXT NOT NULL, url TEXT NOT NULL, published_at TEXT NOT NULL,
 first_seen_at TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
 cluster_id TEXT NOT NULL, revision_of TEXT, status TEXT NOT NULL DEFAULT 'NEW',
 attempts INTEGER NOT NULL DEFAULT 0, analyzed_at TEXT, raw_path TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS dynamic_news_pending ON dynamic_news(status,published_at);
CREATE TABLE IF NOT EXISTS dynamic_runs(
 id TEXT PRIMARY KEY, window_start TEXT NOT NULL, window_end TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dynamic_cases(
 id TEXT PRIMARY KEY, news_id TEXT NOT NULL REFERENCES dynamic_news(id),
 symbol TEXT NOT NULL, name TEXT NOT NULL, theme TEXT NOT NULL, event_type TEXT NOT NULL,
 direction TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 basis TEXT NOT NULL, status TEXT NOT NULL, analysis_json TEXT NOT NULL,
 plan_json TEXT NOT NULL, UNIQUE(news_id,symbol));
CREATE TABLE IF NOT EXISTS dynamic_market(
 symbol TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dynamic_quotes(
 id TEXT PRIMARY KEY, symbol TEXT NOT NULL, name TEXT NOT NULL,
 price_cents INTEGER NOT NULL, prev_close_cents INTEGER NOT NULL,
 observed_at TEXT NOT NULL, first_seen_at TEXT NOT NULL, raw_path TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS dynamic_quote_lookup ON dynamic_quotes(symbol,observed_at);
CREATE TABLE IF NOT EXISTS dynamic_observations(
 case_id TEXT PRIMARY KEY REFERENCES dynamic_cases(id), entry_at TEXT NOT NULL,
 exit_at TEXT NOT NULL, ready_at TEXT NOT NULL, basis TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dynamic_orders(
 id TEXT PRIMARY KEY, intent_key TEXT NOT NULL UNIQUE, case_id TEXT NOT NULL REFERENCES dynamic_cases(id),
 symbol TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL CHECK(qty>0),
 filled_qty INTEGER NOT NULL DEFAULT 0, limit_cents INTEGER NOT NULL CHECK(limit_cents>0),
 reserved_cents INTEGER NOT NULL CHECK(reserved_cents>=0), created_at TEXT NOT NULL,
 expires_at TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL, terms_json TEXT NOT NULL,
 CHECK(filled_qty>=0 AND filled_qty<=qty));
CREATE TABLE IF NOT EXISTS dynamic_fills(
 id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES dynamic_orders(id),
 quote_id TEXT NOT NULL REFERENCES dynamic_quotes(id), symbol TEXT NOT NULL, side TEXT NOT NULL,
 qty INTEGER NOT NULL, price_cents INTEGER NOT NULL, fee_cents INTEGER NOT NULL,
 realized_cents INTEGER NOT NULL, occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
 UNIQUE(order_id,quote_id));
CREATE TABLE IF NOT EXISTS dynamic_lots(
 id TEXT PRIMARY KEY REFERENCES dynamic_fills(id), case_id TEXT NOT NULL REFERENCES dynamic_cases(id),
 symbol TEXT NOT NULL, qty INTEGER NOT NULL CHECK(qty>=0), cost_cents INTEGER NOT NULL CHECK(cost_cents>=0),
 acquired_day TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dynamic_checks(
 case_id TEXT PRIMARY KEY REFERENCES dynamic_cases(id), at TEXT NOT NULL,
 action TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dynamic_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS macro_news(
 news_id TEXT PRIMARY KEY REFERENCES dynamic_news(id), status TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, analyzed_at TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS macro_events(
 id TEXT PRIMARY KEY, news_id TEXT NOT NULL REFERENCES dynamic_news(id),
 created_at TEXT NOT NULL, basis TEXT NOT NULL, theme TEXT NOT NULL,
 status TEXT NOT NULL, payload_json TEXT NOT NULL, UNIQUE(news_id));
CREATE TABLE IF NOT EXISTS macro_markets(
 asset TEXT PRIMARY KEY, checked_at TEXT NOT NULL, status TEXT NOT NULL,
 payload_json TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS macro_observations(
 event_id TEXT NOT NULL REFERENCES macro_events(id), asset TEXT NOT NULL,
 ready_at TEXT NOT NULL, basis TEXT NOT NULL, payload_json TEXT NOT NULL,
 PRIMARY KEY(event_id,asset));
CREATE TABLE IF NOT EXISTS dynamic_feed_checks(
 source TEXT PRIMARY KEY, checked_at TEXT NOT NULL, status TEXT NOT NULL,
 window_count INTEGER NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS macro_instruments(
 asset TEXT PRIMARY KEY, category TEXT NOT NULL, checked_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS macro_watchlist(
 asset TEXT PRIMARY KEY, added_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS macro_watch_links(
 asset TEXT NOT NULL REFERENCES macro_watchlist(asset), event_id TEXT NOT NULL REFERENCES macro_events(id),
 linked_at TEXT NOT NULL, impact_json TEXT NOT NULL, PRIMARY KEY(asset,event_id));
CREATE TABLE IF NOT EXISTS macro_event_revisions(
 id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, replaced_at TEXT NOT NULL, previous_json TEXT NOT NULL);
"""

SCHEMA += """
CREATE TABLE IF NOT EXISTS macro_watch_state(
 asset TEXT PRIMARY KEY REFERENCES macro_watchlist(asset), changed_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS macro_watch_transitions(
 id INTEGER PRIMARY KEY, asset TEXT NOT NULL, at TEXT NOT NULL, previous_tier TEXT, tier TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS macro_news_queue(
 news_id TEXT PRIMARY KEY REFERENCES dynamic_news(id), representative_id TEXT NOT NULL,
 priority INTEGER NOT NULL, reason TEXT NOT NULL, updated_at TEXT NOT NULL);
"""

SCHEMA += """
CREATE TABLE IF NOT EXISTS macro_impact_sources(
 id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES macro_events(id),
 published_at TEXT NOT NULL, first_seen_at TEXT NOT NULL, title TEXT NOT NULL,
 url TEXT NOT NULL, body TEXT NOT NULL, raw_path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS macro_impact_assessments(
 id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES macro_events(id), asset TEXT NOT NULL,
 created_at TEXT NOT NULL, input_hash TEXT NOT NULL, version TEXT NOT NULL,
 payload_json TEXT NOT NULL, features_json TEXT NOT NULL, UNIQUE(event_id,asset,input_hash));
CREATE INDEX IF NOT EXISTS macro_impact_lookup ON macro_impact_assessments(event_id,asset,created_at);
CREATE TABLE IF NOT EXISTS macro_impact_attempts(
 input_hash TEXT PRIMARY KEY, attempts INTEGER NOT NULL, last_at TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS macro_impact_outcomes(
 assessment_id TEXT PRIMARY KEY REFERENCES macro_impact_assessments(id), ready_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS macro_impact_calibrations(
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, feature_key TEXT NOT NULL, payload_json TEXT NOT NULL);
"""

SCHEMA += """
CREATE TABLE IF NOT EXISTS macro_feed_health(
 source TEXT PRIMARY KEY, checked_at TEXT NOT NULL, latest_at TEXT, oldest_at TEXT, valid_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS macro_news_triage(
 news_id TEXT PRIMARY KEY REFERENCES dynamic_news(id), input_hash TEXT NOT NULL,
 created_at TEXT NOT NULL, version TEXT NOT NULL, decision TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS macro_triage_attempts(
 input_hash TEXT PRIMARY KEY, attempts INTEGER NOT NULL, last_at TEXT NOT NULL, error TEXT);
"""

SCHEMA += """
CREATE TABLE IF NOT EXISTS macro_article_texts(
 id TEXT PRIMARY KEY, news_id TEXT NOT NULL REFERENCES dynamic_news(id), available_at TEXT NOT NULL,
 version TEXT NOT NULL, body TEXT NOT NULL, truncated INTEGER NOT NULL, modified_at TEXT NOT NULL, raw_path TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS macro_article_lookup ON macro_article_texts(news_id,available_at);
CREATE TABLE IF NOT EXISTS macro_article_attempts(
 news_id TEXT PRIMARY KEY REFERENCES dynamic_news(id), attempts INTEGER NOT NULL, last_at TEXT NOT NULL, error TEXT);
"""
SCHEMA += """
CREATE TABLE IF NOT EXISTS macro_statement_context(
 news_id TEXT PRIMARY KEY REFERENCES dynamic_news(id), available_at TEXT NOT NULL, payload_json TEXT NOT NULL);
"""

SCHEMA += """
CREATE TABLE IF NOT EXISTS macro_article_fetch_attempts(
 news_id TEXT NOT NULL REFERENCES dynamic_news(id), version TEXT NOT NULL,
 attempts INTEGER NOT NULL, last_at TEXT NOT NULL, error TEXT, PRIMARY KEY(news_id,version));
"""
