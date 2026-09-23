"""User-approved paper policy, distinct from research hypotheses and trade plans."""
from .storage import normalize_time

VERSION = 'days_cash_v1'
FIXED = {
    'GOLD': {'name': '黄金', 'unit': '美元/金衡盎司', 'provider_symbol': 'XAU'},
    'SILVER': {'name': '白银', 'unit': '美元/金衡盎司', 'provider_symbol': 'XAG'},
    'BTC': {'name': '比特币', 'unit': '美元/枚', 'provider_symbol': 'BTC-USD'},
    'ETH': {'name': '以太坊', 'unit': '美元/枚', 'provider_symbol': 'ETH-USD'},
}
MAX_ASSETS = 40
DRAWDOWN_BPS = 2500
RECHECK_SECONDS = 3600


def enabled(config):
    return config.get('investment_policy') == VERSION


def spec(asset):
    return {**FIXED[asset], 'asset': asset, 'category': 'COMMODITY', 'kind': 'SPOT',
            'identity_source': '固定现货模拟观察目录', 'fixed': True}


def seed(store, at):
    import json
    at = normalize_time(at)
    with store.db:
        for asset in FIXED:
            store.db.execute('''INSERT INTO macro_watchlist VALUES(?,?,?,?)
                ON CONFLICT(asset) DO UPDATE SET payload_json=excluded.payload_json''',
                (asset, at, at, json.dumps(spec(asset), ensure_ascii=False)))


def admitted_kind(item):
    return item['asset'] in FIXED or (item.get('category') in ('CN', 'US') and item.get('kind') == 'STOCK')


def public():
    return {'version': VERSION, 'mode': 'paper', 'max_assets': MAX_ASSETS, 'fixed_assets': list(FIXED),
            'max_drawdown_bps': DRAWDOWN_BPS, 'leverage': False, 'holding_unit': 'DAYS',
            'recheck_seconds': RECHECK_SECONDS, 'review_context_hours': 48,
            'breach_action': 'HALT_CANCEL_BUYS_ORDERLY_EXIT', 'automatic_resume': False}
