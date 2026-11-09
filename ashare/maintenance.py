"""Storage housekeeping: bounded publication records, backup retention and disk-space warnings.

Nothing here touches the paper ledger. Removing old publication files on disk is an explicit,
operator-initiated command, never automatic.
"""
import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .storage import now, digest, normalize_time

PRUNED_PREFIX = '{"pruned":true'


def _backup_files(base, recursive=True):
    if not base.is_dir():
        return []
    it = base.rglob('*') if recursive else base.glob('*')
    return [p for p in it if p.is_file() and (p.name.endswith('.sqlite3') or p.name.endswith('.sqlite3.gz'))]


def disk_status(store, config):
    root = store.root
    usage = shutil.disk_usage(root)
    db = sum((root / n).stat().st_size for n in ('agent.sqlite3', 'agent.sqlite3-wal') if (root / n).exists())
    folder = root / 'backups'
    def size(files):
        return sum(p.stat().st_size for p in files)
    hourly = size(_backup_files(folder / 'hourly'))
    daily = size(_backup_files(folder / 'daily'))
    manual = size(_backup_files(folder, recursive=False))
    backups = size(_backup_files(folder))
    # The stored census counts the whole program tree (raw/, workflow/, etc.). Fall back to
    # the parts this function already measures when the census is missing or over 48h old.
    program = db + backups
    row = store.db.execute("SELECT value FROM service_state WHERE key='storage_inventory'").fetchone()
    if row:
        try:
            inv = json.loads(row[0])
            if abs((datetime.now(timezone.utc) - datetime.fromisoformat(inv['generated_at'])).total_seconds()) <= 48 * 3600:
                program = inv['program_bytes']
        except (KeyError, ValueError, TypeError):
            pass
    budget = config.get('storage_budget_gb', 30)
    budget_pct = round(program / 1e9 / budget * 100, 1)
    free_gb = usage.free / 1e9
    warning = (free_gb < config.get('disk_free_warn_gb', 10) or db / 1e9 > config.get('db_size_warn_gb', 5)
               or budget_pct >= 90)
    return {'free_gb': round(free_gb, 1), 'db_gb': round(db / 1e9, 2), 'backups_gb': round(backups / 1e9, 2),
            'backups_hourly_gb': round(hourly / 1e9, 2), 'backups_daily_gb': round(daily / 1e9, 2),
            'backups_manual_gb': round(manual / 1e9, 2),
            'program_gb': round(program / 1e9, 2), 'budget_gb': budget, 'budget_pct': budget_pct,
            'budget_level': 'WARNING' if budget_pct >= 90 else 'NOTICE' if budget_pct >= 70 else 'OK',
            'warning': bool(warning),
            'thresholds': {'free_gb': config.get('disk_free_warn_gb', 10), 'db_gb': config.get('db_size_warn_gb', 5),
                           'storage_budget_gb': budget}}


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


def backup_retention(folder, hourly_keep=6, daily_keep=7, compressed_days=None):
    """Keep the newest `hourly_keep` hourly files plus the newest file of each of the last
    `daily_keep` days. With `compressed_days`, days that already have a compressed daily
    copy no longer keep an uncompressed representative."""
    files = sorted(folder.glob('*.sqlite3'))
    keep = set(files[-hourly_keep:]) if hourly_keep else set()
    per_day = {}
    for f in files:
        per_day[f.name[:8]] = f
    for day in sorted(per_day)[-daily_keep:] if daily_keep else []:
        if compressed_days is None or day not in compressed_days:
            keep.add(per_day[day])
    removed = [f for f in files if f not in keep]
    for f in removed:
        f.unlink()
    return [f.name for f in removed]


def verified_daily_backup(target, source=None):
    """Read and verify the complete gzip and manifest before replacing a raw backup.

    Caller holds the shared backup lock. When a raw representative still exists,
    require this exact file and its bytes, not merely another backup of that day.
    An incomplete publication is not a backup and is repairable from the source.
    """
    import gzip
    import hashlib
    import re
    import zlib
    side = target.parent / (target.name[:8] + '.json')
    try:
        if target.is_symlink() or side.is_symlink():
            return False
        meta = json.loads(side.read_text())
        if not isinstance(meta, dict):
            return False
        name = meta.get('source', '')
        if (not isinstance(name, str) or not re.fullmatch(r'\d{8}T\d{6}Z\.sqlite3', name)
                or name[:8] != target.name[:8] or meta.get('method') != 'gzip-6'):
            return False
        size = meta.get('source_bytes')
        if type(size) is not int or size <= 0 or meta.get('gz_bytes') != target.stat().st_size:
            return False
        expected = meta.get('source_sha256')
        if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
            return False
        if source is not None and (source.is_symlink() or source.name != name or source.stat().st_size != size):
            return False
        sha, total = hashlib.sha256(), 0
        with gzip.open(target, 'rb') as fin:
            for chunk in iter(lambda: fin.read(1 << 20), b''):
                total += len(chunk)
                if total > size:
                    return False
                sha.update(chunk)
        if total != size or sha.hexdigest() != expected:
            return False
        if source is not None:
            sha = hashlib.sha256()
            with source.open('rb') as fin:
                for chunk in iter(lambda: fin.read(1 << 20), b''):
                    sha.update(chunk)
            if sha.hexdigest() != expected:
                return False
        return True
    except (OSError, EOFError, ValueError, TypeError, zlib.error):
        return False


def verified_compressed_days(hourly, daily):
    """Only verified replacements can release a daily raw representative."""
    representatives = {}
    for source in sorted(hourly.glob('*.sqlite3')):
        representatives[source.name[:8]] = source
    return {day for day, source in representatives.items()
            if verified_daily_backup(daily / (day + '.sqlite3.gz'), source)}


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def compress_daily_backups(root, daily_keep, at):
    """One gzip-6 copy per completed UTC day under backups/daily, with a sha256 sidecar.

    Holds the same backups/hourly/.lock as periodic_backup retention so a file being
    compressed cannot be deleted mid-copy. Never compresses the current UTC day."""
    import fcntl
    import gzip
    import hashlib
    start = time.monotonic()
    result = {'compressed': [], 'removed': [], 'errors': [], 'skipped_locked': False, 'seconds': 0}
    hourly = root / 'backups' / 'hourly'
    if not hourly.is_dir():
        result['seconds'] = round(time.monotonic() - start, 2)
        return result
    daily = root / 'backups' / 'daily'
    daily.mkdir(parents=True, exist_ok=True)
    today = normalize_time(at)[:10].replace('-', '')
    with (hourly / '.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result['skipped_locked'] = True
            result['seconds'] = round(time.monotonic() - start, 2)
            return result
        per_day = {}
        for f in hourly.glob('*.sqlite3'):
            day = f.name[:8]
            if day < today:
                per_day.setdefault(day, []).append(f)
        days = sorted(per_day)[-daily_keep:] if daily_keep else []
        for day in days:
            target = daily / (day + '.sqlite3.gz')
            src = sorted(per_day[day])[-1]
            if verified_daily_backup(target, src):
                continue
            tmp = daily / (day + '.sqlite3.gz.tmp')
            try:
                if src.is_symlink() or target.is_symlink():
                    raise ValueError('backup symlink is not a safe replacement')
                sha, size = hashlib.sha256(), 0
                with src.open('rb') as fin, tmp.open('wb') as raw:
                    # Close the GzipFile first so its trailer is written, then fsync the file.
                    with gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=6) as fout:
                        for chunk in iter(lambda: fin.read(1 << 20), b''):
                            sha.update(chunk)
                            size += len(chunk)
                            fout.write(chunk)
                    raw.flush()
                    os.fsync(raw.fileno())
                gz_bytes = tmp.stat().st_size
                os.replace(tmp, target)
                meta = {'source': src.name, 'source_bytes': size, 'source_sha256': sha.hexdigest(),
                        'gz_bytes': gz_bytes, 'method': 'gzip-6', 'created_at': normalize_time(at)}
                side_tmp = daily / (day + '.json.tmp')
                side_tmp.write_text(json.dumps(meta, ensure_ascii=False))
                with side_tmp.open('rb') as side_file:
                    os.fsync(side_file.fileno())
                os.replace(side_tmp, daily / (day + '.json'))
                _sync_directory(daily)
                if not verified_daily_backup(target, src):
                    raise ValueError('compressed backup verification failed; raw source retained')
                result['compressed'].append(day)
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                result['errors'].append({'day': day, 'error': str(exc)[:200]})
        gzs = sorted(daily.glob('*.sqlite3.gz'))
        # Invalid or orphaned files never crowd out the retained valid copies.
        # Validate only when deletion could happen; normal <= daily_keep is cheap.
        if len(gzs) > daily_keep:
            valid = []
            for candidate in gzs:
                if verified_daily_backup(candidate):
                    valid.append(candidate)
                else:
                    result['errors'].append({'day': candidate.name[:8], 'error': 'unverified daily backup retained'})
            gzs = valid
        for old in gzs[:-daily_keep] if daily_keep else gzs:
            old.unlink()
            (daily / (old.name[:8] + '.json')).unlink(missing_ok=True)
            result['removed'].append(old.name)
        cutoff = time.time() - 86400
        for stray in daily.glob('*.tmp'):
            try:
                if stray.stat().st_mtime < cutoff:
                    stray.unlink()
                    result['removed'].append(stray.name)
            except OSError:
                pass
    result['seconds'] = round(time.monotonic() - start, 2)
    return result


def storage_inventory(store, config, at):
    """Whole-tree size census for the 30GB program budget.

    Read-only lstat walk; never follows symlinks or opens files. The result is cached
    in service_state so disk_status can report the real program footprint."""
    start = time.monotonic()
    root = store.root
    def measure(path):
        files, total = 0, 0
        try:
            if path.is_symlink():
                return files, total
            if path.is_file():
                return 1, path.stat().st_size
            for base, _dirs, names in os.walk(path, followlinks=False):
                for n in names:
                    p = Path(base) / n
                    try:
                        if not p.is_symlink():
                            files += 1
                            total += p.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        return files, total
    top = {}
    for entry in sorted(root.iterdir()):
        files, size = measure(entry)
        top[entry.name] = {'files': files, 'bytes': size}
    workflow = {}
    wf = root / 'workflow'
    if wf.is_dir() and not wf.is_symlink():
        for entry in sorted(wf.iterdir()):
            files, size = measure(entry)
            workflow[entry.name] = {'files': files, 'bytes': size}
    backups = {}
    for name in ('hourly', 'daily'):
        files, size = measure(root / 'backups' / name)
        backups[name] = {'files': files, 'bytes': size}
    manual = {'files': 0, 'bytes': 0}
    bdir = root / 'backups'
    if bdir.is_dir():
        for p in bdir.iterdir():
            try:
                if p.is_file() and not p.is_symlink():
                    manual['files'] += 1
                    manual['bytes'] += p.stat().st_size
            except OSError:
                pass
    backups['manual'] = manual
    raw = {'files': 0, 'bytes': 0}
    if (root / 'raw').is_dir():
        files, size = measure(root / 'raw')
        raw = {'files': files, 'bytes': size}
    db = {}
    for pragma in ('page_size', 'page_count', 'freelist_count'):
        db[pragma] = store.db.execute('PRAGMA ' + pragma).fetchone()[0]
    try:
        db['tables'] = [{'name': r[0], 'bytes': r[1]} for r in store.db.execute(
            'SELECT name,sum(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC LIMIT 15')]
    except Exception as exc:
        db['dbstat_error'] = str(exc)[:200]
    inv = {'generated_at': normalize_time(at), 'program_bytes': sum(v['bytes'] for v in top.values()),
           'top': top, 'workflow': workflow, 'backups': backups, 'raw': raw, 'db': db,
           'seconds': round(time.monotonic() - start, 2)}
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO service_state VALUES('storage_inventory',?)",
                         (json.dumps(inv, ensure_ascii=False),))
    return inv


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
        result['backups_compressed'] = compress_daily_backups(store.root, config.get('backup_daily_keep', 7), at)
        # The census walks the whole tree; it is expensive, so refresh at most daily-ish.
        result['inventory_refreshed'] = False
        stored = store.db.execute("SELECT value FROM service_state WHERE key='storage_inventory'").fetchone()
        fresh = False
        if stored:
            try:
                fresh = (datetime.now(timezone.utc)
                         - datetime.fromisoformat(json.loads(stored[0])['generated_at'])).total_seconds() <= 20 * 3600
            except (KeyError, ValueError, TypeError):
                pass
        if not fresh:
            inv = storage_inventory(store, config, at)
            result['inventory_refreshed'] = True
            result['inventory_program_gb'] = round(inv['program_bytes'] / 1e9, 2)
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
