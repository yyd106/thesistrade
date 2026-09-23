"""Stable test defaults, independent of features enabled in the operator's config."""
from ashare.pipeline import load_config as _load

def load_config(*args, **kwargs):
    config = _load(*args, **kwargs)
    config['investment_policy'] = None
    config['portfolio_strategy'] = None
    return config
