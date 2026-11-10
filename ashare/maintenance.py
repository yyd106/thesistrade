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


class _StorageCancelled(Exception):
    """Cooperative stop; never treat cancellation as a corrupt backup."""


def _check_storage_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise _StorageCancelled()


def _backup_chunks(stream, cancel=None):
    while True:
        _check_storage_cancel(cancel)
        chunk = stream.read(1 << 20)
        _check_storage_cancel(cancel)
        if not chunk:
            return
        yield chunk


def verified_daily_backup(target, source=None, cancel=None):
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
    _check_storage_cancel(cancel)
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
            for chunk in _backup_chunks(fin, cancel):
                total += len(chunk)
                if total > size:
                    return False
                sha.update(chunk)
        if total != size or sha.hexdigest() != expected:
            return False
        if source is not None:
            sha = hashlib.sha256()
            with source.open('rb') as fin:
                for chunk in _backup_chunks(fin, cancel):
                    sha.update(chunk)
            if sha.hexdigest() != expected:
                return False
        _check_storage_cancel(cancel)
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


def compress_daily_backups(root, daily_keep, at, max_days=None, cancel=None):
    """One verified gzip copy per completed UTC day under the shared backup lock.

    max_days caps actual compression attempts (a failed day counts). Cancellation
    covers source/gzip validation, compression, publication and retention. It never
    removes raw sources or treats a partially checked archive as a valid backup.
    """
    start = time.monotonic()
    result = {'compressed': [], 'removed': [], 'errors': [], 'skipped_locked': False,
              'cancelled': False, 'seconds': 0}
    try:
        _compress_daily_backups(root, daily_keep, at, max_days, cancel, result)
    except _StorageCancelled:
        result['cancelled'] = True
    result['seconds'] = round(time.monotonic() - start, 2)
    return result


def _compress_daily_backups(root, daily_keep, at, max_days, cancel, result):
    import fcntl
    import gzip
    import hashlib
    _check_storage_cancel(cancel)
    hourly = root / 'backups' / 'hourly'
    if not hourly.is_dir():
        return
    daily = root / 'backups' / 'daily'
    daily.mkdir(parents=True, exist_ok=True)
    today = normalize_time(at)[:10].replace('-', '')
    with (hourly / '.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result['skipped_locked'] = True
            return
        _check_storage_cancel(cancel)
        per_day = {}
        for f in hourly.glob('*.sqlite3'):
            _check_storage_cancel(cancel)
            day = f.name[:8]
            if day < today:
                per_day.setdefault(day, []).append(f)
        days = sorted(per_day)[-daily_keep:] if daily_keep else []
        attempts = 0
        for day in days:
            _check_storage_cancel(cancel)
            if max_days is not None and attempts >= max_days:
                break
            target = daily / (day + '.sqlite3.gz')
            src = sorted(per_day[day])[-1]
            if verified_daily_backup(target, src, cancel=cancel):
                continue
            _check_storage_cancel(cancel)
            attempts += 1
            tmp = daily / (day + '.sqlite3.gz.tmp')
            side_tmp = daily / (day + '.json.tmp')
            try:
                if src.is_symlink() or target.is_symlink():
                    raise ValueError('backup symlink is not a safe replacement')
                sha, size = hashlib.sha256(), 0
                with src.open('rb') as fin, tmp.open('wb') as raw:
                    # Close GzipFile before fsync so its trailer is on disk.
                    with gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=6) as fout:
                        for chunk in _backup_chunks(fin, cancel):
                            sha.update(chunk)
                            size += len(chunk)
                            fout.write(chunk)
                    _check_storage_cancel(cancel)
                    raw.flush()
                    os.fsync(raw.fileno())
                _check_storage_cancel(cancel)
                gz_bytes = tmp.stat().st_size
                os.replace(tmp, target)
                _check_storage_cancel(cancel)
                meta = {'source': src.name, 'source_bytes': size, 'source_sha256': sha.hexdigest(),
                        'gz_bytes': gz_bytes, 'method': 'gzip-6', 'created_at': normalize_time(at)}
                side_tmp.write_text(json.dumps(meta, ensure_ascii=False))
                with side_tmp.open('rb') as side_file:
                    os.fsync(side_file.fileno())
                _check_storage_cancel(cancel)
                os.replace(side_tmp, daily / (day + '.json'))
                _sync_directory(daily)
                _check_storage_cancel(cancel)
                if not verified_daily_backup(target, src, cancel=cancel):
                    raise ValueError('compressed backup verification failed; raw source retained')
                result['compressed'].append(day)
            except _StorageCancelled:
                tmp.unlink(missing_ok=True)
                side_tmp.unlink(missing_ok=True)
                raise
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                side_tmp.unlink(missing_ok=True)
                result['errors'].append({'day': day, 'error': str(exc)[:200]})
        _check_storage_cancel(cancel)
        gzs = sorted(daily.glob('*.sqlite3.gz'))
        # Invalid/orphaned files never crowd out retained valid copies. A stop
        # during validation aborts the whole prune, never classifies a partial read.
        if len(gzs) > daily_keep:
            valid = []
            for candidate in gzs:
                _check_storage_cancel(cancel)
                if verified_daily_backup(candidate, cancel=cancel):
                    valid.append(candidate)
                else:
                    result['errors'].append({'day': candidate.name[:8], 'error': 'unverified daily backup retained'})
            gzs = valid
        for old in gzs[:-daily_keep] if daily_keep else gzs:
            _check_storage_cancel(cancel)
            old.unlink()
            (daily / (old.name[:8] + '.json')).unlink(missing_ok=True)
            result['removed'].append(old.name)
        cutoff = time.time() - 86400
        for stray in daily.glob('*.tmp'):
            _check_storage_cancel(cancel)
            try:
                if stray.stat().st_mtime < cutoff:
                    stray.unlink()
                    result['removed'].append(stray.name)
            except OSError:
                pass


def storage_inventory(store, config, at, tables=True, cancel=None):
    """Whole-tree size census for the 30GB program budget.

    Read-only lstat walk; never follows symlinks or opens files. The result is cached
    in service_state so disk_status can report the real program footprint."""
    if cancel is not None and cancel.is_set():
        return None
    start = time.monotonic()
    root = store.root
    def measure(path):
        if cancel is not None and cancel.is_set():
            return None
        files, total = 0, 0
        try:
            if path.is_symlink():
                return files, total
            if path.is_file():
                return 1, path.stat().st_size
            for base, _dirs, names in os.walk(path, followlinks=False):
                if cancel is not None and cancel.is_set():
                    return None
                for n in names:
                    if cancel is not None and cancel.is_set():
                        return None
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
        res = measure(entry)
        if res is None:
            return None
        top[entry.name] = {'files': res[0], 'bytes': res[1]}
    workflow = {}
    wf = root / 'workflow'
    if wf.is_dir() and not wf.is_symlink():
        for entry in sorted(wf.iterdir()):
            res = measure(entry)
            if res is None:
                return None
            workflow[entry.name] = {'files': res[0], 'bytes': res[1]}
    backups = {}
    for name in ('hourly', 'daily'):
        res = measure(root / 'backups' / name)
        if res is None:
            return None
        backups[name] = {'files': res[0], 'bytes': res[1]}
    manual = {'files': 0, 'bytes': 0}
    bdir = root / 'backups'
    if bdir.is_dir():
        for p in bdir.iterdir():
            if cancel is not None and cancel.is_set():
                return None
            try:
                if p.is_file() and not p.is_symlink():
                    manual['files'] += 1
                    manual['bytes'] += p.stat().st_size
            except OSError:
                pass
    backups['manual'] = manual
    raw = {'files': 0, 'bytes': 0}
    if (root / 'raw').is_dir():
        res = measure(root / 'raw')
        if res is None:
            return None
        raw = {'files': res[0], 'bytes': res[1]}
    db = {}
    for pragma in ('page_size', 'page_count', 'freelist_count'):
        if cancel is not None and cancel.is_set():
            return None
        db[pragma] = store.db.execute('PRAGMA ' + pragma).fetchone()[0]
    if tables:
        try:
            db['tables'] = [{'name': r[0], 'bytes': r[1]} for r in store.db.execute(
                'SELECT name,sum(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC LIMIT 15')]
        except Exception as exc:
            db['dbstat_error'] = str(exc)[:200]
    inv = {'generated_at': normalize_time(at), 'program_bytes': sum(v['bytes'] for v in top.values()),
           'top': top, 'workflow': workflow, 'backups': backups, 'raw': raw, 'db': db,
           'seconds': round(time.monotonic() - start, 2)}
    if cancel is not None and cancel.is_set():
        return None
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


def storage_worker(config, at, cancel=None):
    """Heavy storage maintenance on its own Store so the scheduler tick never blocks.

    Compresses at most one completed backup day per call and refreshes the cached
    inventory only when it is older than 20 hours (without the dbstat table scan)."""
    from .storage import Store
    at = normalize_time(at)
    if config.get('deployment_role', 'standalone') == 'cloud':
        return {'at': at, 'compressed': [], 'removed': [], 'errors': [], 'skipped_locked': False,
                'cancelled': False, 'seconds': 0.0, 'inventory_refreshed': False, 'inventory_seconds': 0.0}
    store = Store(config['data_dir'])
    try:
        start = time.monotonic()
        comp = compress_daily_backups(store.root, config.get('backup_daily_keep', 7), at,
                                      max_days=1, cancel=cancel)
        inventory_refreshed = False
        inventory_seconds = 0.0
        inventory_cancelled = False
        if not (comp['cancelled'] or (cancel is not None and cancel.is_set())):
            stored = store.db.execute("SELECT value FROM service_state WHERE key='storage_inventory'").fetchone()
            fresh = False
            if stored:
                try:
                    fresh = (datetime.now(timezone.utc)
                             - datetime.fromisoformat(json.loads(stored[0])['generated_at'])).total_seconds() <= 20 * 3600
                except (KeyError, ValueError, TypeError):
                    pass
            if not fresh:
                istart = time.monotonic()
                inventory_cancelled = storage_inventory(store, config, at, tables=False, cancel=cancel) is None
                inventory_seconds = round(time.monotonic() - istart, 2)
                inventory_refreshed = not inventory_cancelled
        result = {'at': at, 'compressed': comp['compressed'], 'removed': comp['removed'],
                  'errors': comp['errors'], 'skipped_locked': comp['skipped_locked'],
                  'cancelled': comp['cancelled'] or inventory_cancelled,
                  'seconds': round(time.monotonic() - start, 2),
                  'inventory_refreshed': inventory_refreshed, 'inventory_seconds': inventory_seconds}
        with store.db:
            store.db.execute("INSERT OR REPLACE INTO service_state VALUES('storage_maintenance',?)",
                             (json.dumps(result, ensure_ascii=False),))
        return result
    finally:
        store.close()


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
