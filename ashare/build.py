"""Derived build identity: which code, strategy settings, model and adopted guidance produced a record.

strategy_version stays the name of the rule family. build_id changes whenever any input that can
change a trading or research judgment changes, so results can always be attributed to one build.
"""
import hashlib
import json
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
                 'cloud_stale_policy', 'mode')


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
