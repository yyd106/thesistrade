"""Hourly eligibility proof; source changes invalidate it immediately, never extend research TTL."""
import json
from datetime import datetime, timedelta
from .storage import digest, normalize_time
from .investment_policy import enabled, RECHECK_SECONDS


def check(store, config, plan, at, *, extra_blockers=()):
    if not enabled(config):
        return []
    from .slots import unreviewed_events
    from .paper import quote_ok
    at = normalize_time(at)
    blockers = list(extra_blockers)
    q = store.latest_quote(plan['symbol'], at)
    if plan['status'] != 'ACTIVE' or not plan['activated_at'] <= at < plan['valid_until']:
        blockers.append('PLAN_EXPIRED')
    changed = unreviewed_events(store, plan, at, config)
    if changed:
        blockers.append('NEW_UNREVIEWED_EVENTS')
    if not quote_ok(q, config, at):
        blockers.append('STALE_QUOTE')
    fingerprint = digest(json.dumps([plan['id'], changed, sorted(blockers)]))
    old = store.db.execute('SELECT * FROM plan_rechecks WHERE plan_id=?', (plan['id'],)).fetchone()
    if not old or old['valid_until'] <= at or old['fingerprint'] != fingerprint:
        expiry = min(plan['valid_until'], normalize_time((datetime.fromisoformat(at)+timedelta(seconds=RECHECK_SECONDS)).isoformat()))
        # Do not commit the surrounding cash reservation transaction.
        store.db.execute('INSERT OR REPLACE INTO plan_rechecks VALUES(?,?,?,?,?)',
                         (plan['id'], at, expiry, fingerprint, json.dumps({'blockers': blockers, 'quote_id': q['id'] if q else None, 'changed_documents': changed})))
    return blockers
