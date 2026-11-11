"""R1 market-regime exposure cap on the stored CSI300 comparison series.

Method R1-v1 (identical algorithm to analysis/backtest/p05_backtest.regime_r1):
for every stored bar compute a raw cap — 90 when close>MA60 and MA20>MA60, 50 when
exactly one holds, 10 when neither — and a confirmed state that adopts the first
raw value and switches only when raw[i] != state and raw[i-1] == raw[i] (two-day
confirmation). Only bars dated on or before the last completed trading day count;
a missing or stale series yields the conservative 10% cap, which limits new buys
only (Q2-approved behavior)."""
import hashlib
import json
from collections import deque

from .calendar import last_completed_day

METHOD = 'R1-v1'
INDEX = 'sh000300'
MIN_BARS = 80
FEATURE = 'exposure_caps_v1'


def _day(b):
    return b['date'] if isinstance(b, dict) else b[0]


def _close(b):
    return float(b['close']) if isinstance(b, dict) else float(b[2])


def _ma(values, n):
    out = [None] * len(values)
    q = deque()
    acc = 0.0
    for i, v in enumerate(values):
        q.append(v)
        acc += v
        if len(q) > n:
            acc -= q.popleft()
        if len(q) == n:
            out[i] = acc / n
    return out


def _caps(closes):
    """(confirmed, raw) per-bar caps — the p05 regime_r1 confirmation rule."""
    m20, m60 = _ma(closes, 20), _ma(closes, 60)
    raw = [None] * len(closes)
    for i, c in enumerate(closes):
        if m60[i] is not None:
            raw[i] = {2: 90, 1: 50, 0: 10}[(c > m60[i]) + (m20[i] > m60[i])]
    conf = [None] * len(closes)
    state = None
    for i in range(len(closes)):
        if raw[i] is None:
            continue
        if state is None:
            state = raw[i]
        elif raw[i] != state and raw[i - 1] == raw[i]:
            state = raw[i]
        conf[i] = state
    return conf, raw


def r1(store, at):
    """The confirmed R1 cap at the newest completed bar, bound to the series version."""
    result = {'method': METHOD, 'status': 'MISSING', 'as_of_day': None, 'close': None,
              'ma20': None, 'ma60': None, 'raw_cap_pct': 10, 'cap_pct': 10,
              'confirmed_cap_pct': None, 'series_ready_at': None, 'series_sha256': None}
    row = store.db.execute('SELECT rowid,ready_at,payload_json FROM comparison_series WHERE symbol=? AND ready_at<=?'
                           ' ORDER BY ready_at DESC,rowid DESC LIMIT 1', (INDEX, at)).fetchone()
    if not row:
        return result
    payload = row[2]
    result['series_ready_at'] = row[1]
    result['series_sha256'] = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    bars = (json.loads(payload).get('bars') or [])
    cutoff = last_completed_day(at)
    bars = [b for b in bars if _day(b) <= (cutoff or '0000-00-00')]
    if len(bars) < MIN_BARS:
        return result
    closes = [_close(b) for b in bars]
    m20, m60 = _ma(closes, 20), _ma(closes, 60)
    conf, raw = _caps(closes)
    result.update(as_of_day=_day(bars[-1]), close=closes[-1], ma20=m20[-1], ma60=m60[-1],
                  raw_cap_pct=raw[-1] or 10, cap_pct=conf[-1] or 10,
                  confirmed_cap_pct=conf[-1] or 10)
    if cutoff is None or result['as_of_day'] < cutoff:
        # The series is older than the last completed day: cap new buys at 10%.
        result['status'] = 'STALE'
        result['cap_pct'] = 10
    else:
        result['status'] = 'OK'
    return result


def regime_active(store, config):
    """The single pairing predicate. Research: exposure_regime='R1' and the cloud
    advertised exposure_caps_v1. Cloud: a stored exposure_caps bundle with mode R1.
    Standalone and unpaired nodes stay off."""
    if config.get('deployment_role') == 'cloud':
        from .cloud_runtime import value
        return ((value(store, 'exposure_caps') or {}).get('mode') in ('R1', 'D14'))
    if config.get('exposure_regime') not in ('R1', 'D14'):
        return False
    from .cloud_sync import remote_supports
    return remote_supports(store, 'exposure_caps_v2' if config.get('exposure_regime') == 'D14' else FEATURE)
