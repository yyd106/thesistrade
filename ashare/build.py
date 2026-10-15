"""Derived build identity: which code, strategy settings, model and adopted guidance produced a record.

strategy_version stays the name of the rule family. build_id changes whenever any input that can
change a trading or research judgment changes, so results can always be attributed to one build.
"""
import hashlib
import json
import ast
import re
import sys
import sqlite3
from importlib import metadata
from functools import lru_cache
from pathlib import Path

# Settings that change what research concludes or what execution does. Scheduling-only keys are excluded,
# including research_reuse_hours: a renewal only happens when every research input is unchanged.
STRATEGY_KEYS = ('strategy_version', 'paper_entry_band_bps', 'paper_stop_loss_bps', 'paper_take_profit_bps',
                 'paper_max_stock_pct', 'paper_max_gross_pct', 'paper_lot_size', 'paper_commission_bps',
                 'paper_min_fee_cents', 'paper_sell_tax_bps', 'paper_slippage_bps', 'paper_max_fill_qty',
                 'paper_order_ttl_seconds', 'plan_max_age_hours', 'slot_execution_mode', 'portfolio_strategy',
                 'investment_policy', 'portfolio_authorization_hours', 'model_name',
                 'model_reasoning_effort', 'max_packet_chars', 'max_news_packet_pct', 'dynamic_enabled',
                 'cloud_stale_policy', 'mode', 'industry_enabled', 'industry_policy')


def _sha(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


@lru_cache(maxsize=1)
def code_fingerprint():
    """Hash of the program itself (prompts live in code), independent of git availability."""
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for path in sorted(list(root.rglob('*.py')) + list((root / 'data').glob('*.json'))):
        if '__pycache__' in path.parts:
            continue
        h.update(str(path.relative_to(root)).encode() + b'\0' + path.read_bytes() + b'\0')
    return h.hexdigest()[:12]


def config_fingerprint(config):
    selected = {k: config.get(k) for k in STRATEGY_KEYS}
    selected['watchlist'] = sorted(i['symbol'] for i in config.get('watchlist', []))
    return _sha(selected)[:12]


def guidance_fingerprint(store, config=None):
    """Adopted research guidance. Without a store, read the data directory's database read-only, so doctor
    and config changes show the same build id that research records. None means it could not be read."""
    import sqlite3
    query = "SELECT id,route,scope,text FROM strategy_guidance WHERE status='ADOPTED' ORDER BY id"
    try:
        if store is not None:
            rows = [tuple(r) for r in store.db.execute(query)]
        else:
            path = Path((config or {}).get('data_dir') or '') / 'agent.sqlite3'
            if not (config or {}).get('data_dir') or not path.exists():
                return 'none' if (config or {}).get('data_dir') else None
            from urllib.parse import quote
            db = sqlite3.connect('file:' + quote(str(path)) + '?mode=ro', uri=True, timeout=5)
            try:
                rows = [tuple(r) for r in db.execute(query)]
            finally:
                db.close()
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            return None
        rows = []  # database predates 0.15.0: nothing can have been adopted
    return _sha(rows)[:8] if rows else 'none'


def info(config, store=None):
    from . import __version__
    model = f"{config.get('model_name') or 'cli-default'}/{config.get('model_reasoning_effort') or 'cli-default'}"
    parts = {'version': __version__, 'rule': config.get('strategy_version'), 'code': code_fingerprint(),
             'config': config_fingerprint(config), 'model': model, 'guidance': guidance_fingerprint(store, config)}
    parts['build_id'] = _sha({k: parts[k] for k in ('code', 'config', 'model', 'guidance')})[:12]
    return parts


def record(store, config, at):
    """Keep a lookup from build_id to its parts; first appearance marks when a change went live."""
    parts = info(config, store)
    store.db.execute('INSERT OR IGNORE INTO builds VALUES(?,?,?)', (parts['build_id'], at, json.dumps(parts, ensure_ascii=False, sort_keys=True)))
    return parts


# Separate from build_id: deployment attribution and legacy records retain the
# complete source hash above. This scope is deliberately a denylist, so new code
# and new configuration keys are included without having to update an allowlist.
BEHAVIOR_VERSION = 'research-behavior-v1'
DISPLAY_ONLY_MODULES = {
    # Copies current industry state for dashboard display; never grants permits.
    'industry_presentation.py',
    # Read-only review/accounting projections. Its age helper only feeds proposal
    # display; adopted guidance is independently frozen below.
    'review_presentation.py',
    # Dashboard-only stock detail/history projections.
    'stock_detail.py',
    'strategy_freshness.py', 'freshness_sync.py',
}
LOCATION_OR_TRANSIENT_KEYS = {
    'data_dir',     # Storage location, not the stored inputs (frozen per pair).
    'config_path',  # Location of the already-loaded settings.
    '_deadline',    # Per-cycle monotonic deadline; policy budget/code are included.
    'strategy_freshness_enabled',  # Read-only telemetry does not alter a research input.
}


def _package_source(raw):
    """Ignore only a literal release label, retaining any other package logic."""
    tree = ast.parse(raw)
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and
                isinstance(node.targets[0], ast.Name) and node.targets[0].id == '__version__' and
                isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            node.value.value = '<release-version>'
    return ast.dump(tree, include_attributes=False).encode()


def _project_source(raw):
    """Keep dependency/runtime metadata; only the project release label varies."""
    lines, section = [], None
    for line in raw.decode().splitlines():
        match = re.fullmatch(r'\s*\[([^\[\]]+)\]\s*(?:#.*)?', line)
        if match:
            section = match.group(1)
        elif line.lstrip().startswith('['):
            section = None  # Array/unknown tables must not inherit [project].
        if section == 'project' and re.fullmatch(r'''\s*version\s*=\s*["'][^"']*["']\s*(?:#.*)?''', line):
            line = 'version = "<release-version>"'
        lines.append(line)
    return '\n'.join(lines).encode()


def behavior_code_fingerprint(root=None):
    """All application Python/static data and dependency declarations by default.

    Deliberately not process-cached: a long-lived worker must see added/changed
    dependencies too. Presentation.py and reports.py are mixed-purpose modules
    and MUST remain included (prompt vocabulary and report import/sync).
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    h = hashlib.sha256()
    paths = sorted(list(root.rglob('*.py')) + list((root / 'data').rglob('*.json')))
    for path in paths:
        name = path.relative_to(root).as_posix()
        if '__pycache__' in path.parts or name in DISPLAY_ONLY_MODULES:
            continue
        raw = path.read_bytes()
        if name == '__init__.py':
            raw = _package_source(raw)
        h.update(name.encode() + b'\0' + raw + b'\0')
    for name in ('requirements.lock', 'pyproject.toml'):
        path = root.parent / name
        raw = path.read_bytes() if path.exists() else b'<missing>'
        if name == 'pyproject.toml':
            raw = _project_source(raw)
        h.update(name.encode() + b'\0' + raw + b'\0')
    return h.hexdigest()


@lru_cache(maxsize=8)
def _executable_hash(path, inode, size, mtime_ns, ctime_ns):
    """Cache bytes by file identity/stat, not merely by the executable's path."""
    h = hashlib.sha256()
    with Path(path).open('rb') as executable:
        for block in iter(lambda: executable.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def model_executable_fingerprint():
    """Hash the same executable run_json selects, without executing it.

    Do not inspect CLI user settings, credential files or model material. Moving
    identical bytes is compatible; replacing an installed executable is not.
    The service-side implementation behind a pinned model name is not observable
    here and cannot be frozen by this local identity.
    """
    from .model import codex_executable
    try:
        path = Path(codex_executable()).resolve()
        stat = path.stat()
        return _executable_hash(str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    except (RuntimeError, FileNotFoundError):
        return '<missing>'
    except OSError as exc:
        return '<unreadable:' + type(exc).__name__ + '>'


def behavior_runtime_fingerprint():
    # Runtime/library upgrades can change parsing or queries even when the source
    # tree is identical. No credential/config files or model calls are read here.
    packages = {}
    for name in ('pypdf', 'cryptography'):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = '<missing>'
    return _sha({'python': list(sys.version_info[:3]), 'implementation': sys.implementation.name,
                 'sqlite': sqlite3.sqlite_version, 'packages': packages,
                 'model_executable': model_executable_fingerprint()})


def behavior_info(config, store):
    """Freeze input, sampling, model, measurement and research behavior, not data.

    Keep only hashes in records, never a copy of configuration. Unknown keys are
    included; non-JSON values fail closed instead of silently disappearing.
    Future market inputs are intentionally variable and frozen at enrollment.
    """
    rows = [tuple(r) for r in store.db.execute(
        "SELECT id,route,scope,text FROM strategy_guidance WHERE status='ADOPTED' ORDER BY id")]
    selected = {k: v for k, v in config.items() if k not in LOCATION_OR_TRANSIENT_KEYS}
    config_raw = json.dumps(selected, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    parts = {'version': BEHAVIOR_VERSION, 'code_hash': behavior_code_fingerprint(),
             'config_hash': _sha(config_raw), 'guidance_hash': _sha(rows),
             'runtime_hash': behavior_runtime_fingerprint()}
    parts['identity_hash'] = _sha(parts)
    return parts


def valid_behavior_identity(value):
    fields = {'version', 'code_hash', 'config_hash', 'guidance_hash', 'runtime_hash', 'identity_hash'}
    return (isinstance(value, dict) and set(value) == fields and value['version'] == BEHAVIOR_VERSION and
            all(isinstance(value[k], str) and re.fullmatch('[0-9a-f]{64}', value[k])
                for k in fields - {'version'}) and
            value['identity_hash'] == _sha({k: v for k, v in value.items() if k != 'identity_hash'}))


def behavior_matches(frozen, config, store):
    return bool(valid_behavior_identity(frozen) and frozen == behavior_info(config, store))
