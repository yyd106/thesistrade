"""Storage housekeeping: bounded publication records, backup retention and disk-space warnings.

Nothing here touches the paper ledger. Removing old publication files on disk is an explicit,
operator-initiated command, never automatic.
"""
import json
import shutil
from datetime import datetime, timedelta, timezone
from .storage import now, digest, normalize_time

PRUNED_PREFIX = '{"pruned":true'


def disk_status(store, config):
    root = store.root
    usage = shutil.disk_usage(root)
    db = sum((root / n).stat().st_size for n in ('agent.sqlite3', 'agent.sqlite3-wal') if (root / n).exists())
    folder = root / 'backups'
    backups = sum(p.stat().st_size for p in folder.rglob('*.sqlite3')) if folder.exists() else 0
    free_gb = usage.free / 1e9
    warning = free_gb < config.get('disk_free_warn_gb', 10) or db / 1e9 > config.get('db_size_warn_gb', 5)
    return {'free_gb': round(free_gb, 1), 'db_gb': round(db / 1e9, 2), 'backups_gb': round(backups / 1e9, 2),
            'warning': bool(warning), 'thresholds': {'free_gb': config.get('disk_free_warn_gb', 10), 'db_gb': config.get('db_size_warn_gb', 5)}}


def prune_outbox(store, limit=None):
    """Replace delivered, superseded or expired publication bodies with a hash marker.
    PENDING bodies stay intact because they may still be sent."""
    ids = [r[0] for r in store.db.execute("SELECT id FROM cloud_outbox WHERE status!='PENDING' AND substr(payload_json,1,14)!=? ORDER BY created_at", (PRUNED_PREFIX,))]
    count = 0
    for oid in ids[:limit] if limit else ids:
        with store.db:
            row = store.db.execute('SELECT payload_json FROM cloud_outbox WHERE id=?', (oid,)).fetchone()
            body = row[0]
            marker = json.dumps({'pruned': True, 'sha256': digest(body), 'bytes': len(body.encode())}, separators=(',', ':'))
            store.db.execute('UPDATE cloud_outbox SET payload_json=? WHERE id=?', (marker, oid))
        count += 1
    return count


def backup_retention(folder, hourly_keep=6, daily_keep=7):
    """Keep the newest `hourly_keep` hourly files plus the newest file of each of the last `daily_keep` days."""
    files = sorted(folder.glob('*.sqlite3'))
    keep = set(files[-hourly_keep:]) if hourly_keep else set()
    per_day = {}
    for f in files:
        per_day[f.name[:8]] = f
    for day in sorted(per_day)[-daily_keep:] if daily_keep else []:
        keep.add(per_day[day])
    removed = [f for f in files if f not in keep]
    for f in removed:
        f.unlink()
    return [f.name for f in removed]


def prune_ledger_changes(store, keep_days=7, at=None):
    """Cloud change log older than a week is no longer needed; a local replica that far behind resyncs fully."""
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='ledger_changes'").fetchone():
        return 0
    cutoff = normalize_time((datetime.fromisoformat(normalize_time(at or now())) - timedelta(days=keep_days)).isoformat())
    with store.db:
        return store.db.execute('DELETE FROM ledger_changes WHERE changed_at<?', (cutoff,)).rowcount


def publication_files(store, older_than_days=1):
    """Legacy publication.json copies; the database keeps a hash marker, the file is only an audit copy."""
    cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
    folder = store.root / 'workflow' / 'cloud-sync'
    if not folder.exists():
        return []
    return sorted(p for p in folder.glob('*/publication.json') if p.stat().st_mtime < cutoff)


def remove_publication_files(store, older_than_days=1):
    removed = []
    for path in publication_files(store, older_than_days):
        body = path.read_bytes()
        manifest = path.parent / 'manifest.json'
        if not manifest.exists():
            manifest.write_text(json.dumps({'publication_sha256': digest(body), 'bytes': len(body), 'removed_at': now(),
                                            'note': '原发布包副本已删除；数据库保留同一哈希的标记'}, ensure_ascii=False))
        path.unlink()
        removed.append({'path': str(path.relative_to(store.root)), 'bytes': len(body)})
    return removed


def run(store, config, at=None):
    at = normalize_time(at or now())
    result = {'at': at}
    role = config.get('deployment_role', 'standalone')
    if role != 'cloud':
        result['outbox_pruned'] = prune_outbox(store)
    if role == 'cloud':
        result['ledger_changes_pruned'] = prune_ledger_changes(store, at=at)
    result['disk'] = disk_status(store, config)
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO service_state VALUES('maintenance',?)", (json.dumps(result, ensure_ascii=False),))
    return result


def vacuum(store):
    """Reclaim space after pruning. Needs free disk about the database size; run with the service stopped."""
    before = (store.root / 'agent.sqlite3').stat().st_size
    store.db.execute('VACUUM')
    after = (store.root / 'agent.sqlite3').stat().st_size
    return {'before_mb': round(before / 1e6, 1), 'after_mb': round(after / 1e6, 1)}
