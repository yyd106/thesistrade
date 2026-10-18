"""Per-source polling and bounded retry state; skipped checks never imply success."""
from datetime import datetime, timedelta, timezone

from .news_catalog import META
from .storage import normalize_time


def interval_minutes(source):
    return META.get(source, {}).get('poll_minutes', 30)


def next_check(store, source, check=None):
    check = check or store.db.execute(
        'SELECT * FROM dynamic_feed_checks WHERE source=?', (source,)).fetchone()
    if not check:
        return None
    checked = datetime.fromisoformat(check['checked_at'])
    minutes = interval_minutes(source)
    if check['status'] == 'FAILED':
        row = store.db.execute('SELECT value FROM dynamic_state WHERE key=?',
                               ('news_poll_failures:' + source,)).fetchone()
        # A legacy failure has not tried this collector/version's recovery yet.
        if not row:
            return normalize_time(check['checked_at'])
        try:
            failures = max(1, min(4, int(row[0])))
        except (TypeError, ValueError):
            failures = 1
        minutes = max(minutes, min(360, 30 * 2 ** failures))
        return normalize_time((checked + timedelta(minutes=minutes)).isoformat())
    # Align successful checks to wall-clock slots, tolerating normal job jitter.
    period = minutes * 60
    return datetime.fromtimestamp((int(checked.timestamp()) // period + 1) * period,
                                  timezone.utc).isoformat()


def due(store, source, at):
    check = store.db.execute('SELECT * FROM dynamic_feed_checks WHERE source=?',
                             (source,)).fetchone()
    return not check or normalize_time(at) < check['checked_at'] or normalize_time(at) >= next_check(store, source, check)


def record(store, source, at, count, error=None, note=None):
    key = 'news_poll_failures:' + source
    row = store.db.execute('SELECT value FROM dynamic_state WHERE key=?', (key,)).fetchone()
    try:
        failures = min(4, max(0, int(row[0]))) if row else 0
    except (TypeError, ValueError):
        failures = 0
    with store.db:
        store.db.execute('INSERT OR REPLACE INTO dynamic_feed_checks VALUES(?,?,?,?,?)',
                         (source, normalize_time(at), 'FAILED' if error else 'OK', count,
                          error or note or '公开订阅标题与摘要；未声称阅读全文或完整回补48小时'))
        store.db.execute('INSERT OR REPLACE INTO dynamic_state VALUES(?,?)',
                         (key, str(min(4, failures + 1) if error else 0)))


def deferred_failure(store, source):
    check = store.db.execute('SELECT * FROM dynamic_feed_checks WHERE source=?', (source,)).fetchone()
    if check and check['status'] == 'FAILED':
        return source + '：上次检查失败，等待重试至 ' + next_check(store, source, check) + '；' + check['detail']
    return None
