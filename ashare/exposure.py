"""The effective gross-exposure cap shared by every buy path and the model packet."""
import json

from .calendar import last_completed_day
from .cloud_runtime import put, value
from .market_regime import regime_active, r1

_CAPS = {10, 50, 90}
# The keys r1() may emit; an incoming bundle carrying anything else is rejected outright.
_REGIME_KEYS = {'method', 'status', 'cap_pct', 'raw_cap_pct', 'confirmed_cap_pct', 'as_of_day',
                'series_sha256', 'series_ready_at', 'close', 'ma20', 'ma60'}


def validate_bundle(bundle):
    """Strict shape check for a signed exposure_caps payload; raises ValueError."""
    import re
    if not isinstance(bundle, dict) or set(bundle) != {'mode', 'regime'}:
        raise ValueError('exposure_caps结构无效')
    if bundle['mode'] not in ('R1', 'OFF'):
        raise ValueError('exposure_caps.mode无效')
    regime = bundle['regime']
    if bundle['mode'] == 'OFF':
        if regime is not None:
            raise ValueError('OFF模式不得携带regime')
        return
    if not isinstance(regime, dict):
        raise ValueError('exposure_caps.regime无效')
    unknown = set(regime) - _REGIME_KEYS
    if unknown:
        raise ValueError('exposure_caps.regime含未知字段:' + sorted(unknown)[0])
    for key in ('method', 'status', 'cap_pct', 'raw_cap_pct', 'confirmed_cap_pct',
                'as_of_day', 'series_sha256'):
        if key not in regime:
            raise ValueError('exposure_caps.regime缺少字段:' + key)
    if regime['method'] != 'R1-v1':
        raise ValueError('exposure_caps.regime.method无效')
    if regime['status'] not in ('OK', 'STALE', 'MISSING'):
        raise ValueError('exposure_caps.regime.status无效')
    if regime['cap_pct'] not in _CAPS:
        raise ValueError('exposure_caps.regime.cap_pct无效')
    for key in ('raw_cap_pct', 'confirmed_cap_pct'):
        if regime[key] is not None and regime[key] not in _CAPS:
            raise ValueError('exposure_caps.regime.' + key + '无效')
    if regime['as_of_day'] is not None and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', regime['as_of_day']):
        raise ValueError('exposure_caps.regime.as_of_day无效')
    if regime['series_sha256'] is not None and not re.fullmatch(r'[0-9a-f]{64}', regime['series_sha256']):
        raise ValueError('exposure_caps.regime.series_sha256无效')
    # close/ma*/series_ready_at are None on the MISSING and short-series paths;
    # that is the approved degraded state, not a malformed bundle.
    for key in ('close', 'ma20', 'ma60'):
        if regime.get(key) is not None and not isinstance(regime[key], (int, float)):
            raise ValueError('exposure_caps.regime.' + key + '无效')
    if regime.get('series_ready_at') is not None and not isinstance(regime['series_ready_at'], str):
        raise ValueError('exposure_caps.regime.series_ready_at无效')


def bundle_cap_pct(bundle, config, at):
    """(pct, detail): the effective gross cap for an exposure_caps bundle on this node's
    clock. None/OFF → the configured cap; R1 → min(base, regime cap) where a regime
    missing or older than the last completed trading day counts as 10."""
    base = config.get('paper_max_gross_pct', 90)
    detail = {'base_pct': base, 'regime': None}
    if not bundle or bundle.get('mode') != 'R1':
        return base, detail
    regime = bundle.get('regime') or {}
    cap = regime.get('cap_pct', 10)
    as_of = regime.get('as_of_day')
    cutoff = last_completed_day(at)
    if not as_of or not cutoff or as_of < cutoff:
        cap = 10
    detail['regime'] = {'source': 'CLOUD_BUNDLE', 'as_of_day': as_of, 'cap_pct': cap,
                        'series_sha256': regime.get('series_sha256')}
    return min(base, cap), detail


def _cap_state(store, config, at):
    """(cap_pct, regime_info) when a live OK R1 cap governs on this node, else (None, {}).
    STALE/MISSING regimes limit new buys only — they never trigger forced sells."""
    if config.get('deployment_role') == 'cloud':
        bundle = value(store, 'exposure_caps') or {}
        regime = bundle.get('regime') or {}
        as_of = regime.get('as_of_day')
        cutoff = last_completed_day(at)
        if bundle.get('mode') != 'R1' or regime.get('status') != 'OK' or not as_of or not cutoff or as_of < cutoff:
            return None, {}
        return regime.get('cap_pct', 10), {'source': 'CLOUD_BUNDLE', 'as_of_day': as_of,
                                           'cap_pct': regime.get('cap_pct', 10)}
    if not regime_active(store, config):
        return None, {}
    info = r1(store, at)
    if info['status'] != 'OK':
        return None, {}
    return info['cap_pct'], info


def _holdings(store, a, at):
    """Every held position across routes, sellable or not, with whether its own market
    is open at `at` (A-shares: CONTINUOUS phase; global: its market's session)."""
    from .calendar import phase
    ashare_open = phase(at) == 'CONTINUOUS'
    holdings = []
    for sym, p in sorted(a['positions'].items()):
        if p['qty']:
            holdings.append({'key': 'watchlist:' + sym, 'route': 'watchlist', 'symbol': sym,
                             'mv': p['market_value_cents'], 'held': p['qty'],
                             'sellable': p['sellable_qty'], 'session_open': ashare_open})
    for cid in [r[0] for r in store.db.execute('SELECT DISTINCT case_id FROM dynamic_lots WHERE qty>0')]:
        case = store.db.execute('SELECT * FROM dynamic_cases WHERE id=?', (cid,)).fetchone()
        from .dynamic_paper import case_position
        p = case_position(store, cid, at)
        if not p.get('qty'):
            continue
        sym = case['symbol'] if case else cid
        from .dynamic_sources import latest_quote as dquote
        q = dquote(store, sym, at)
        holdings.append({'key': 'dynamic:' + cid, 'route': 'dynamic', 'symbol': sym,
                         'case': dict(case) if case else None,
                         'mv': p.get('market_value_cents') or (p['qty'] * q['price_cents'] if q else 0),
                         'held': p['qty'], 'sellable': p['sellable_qty'], 'session_open': ashare_open})
    for sym, p in sorted(a.get('global_positions', {}).items()):
        if not p['qty']:
            continue
        from .global_market import latest as glatest, session_open
        q = glatest(store, sym, at)
        holdings.append({'key': 'global:' + sym, 'route': 'global', 'symbol': sym,
                         'mv': p['market_value_cents'], 'held': p['qty'], 'sellable': p['qty'],
                         'session_open': bool(q) and session_open(sym, at, json.loads(q['payload_json']))})
    return holdings


def _episode(store):
    ep = value(store, 'exposure_reduce_episode')
    return ep if isinstance(ep, dict) and ep.get('state', 'OPEN') == 'OPEN' else None


def _committed(store, ep):
    """Value committed by this episode's own orders per holding key: the whole order
    while OPEN/PARTIAL, only the filled part once closed. Closed-unfilled releases
    the remaining quota so it can be resubmitted on a later run."""
    committed = {}
    for o in ep.get('orders', []):
        row = store.db.execute('SELECT status,qty,filled_qty FROM ' + _order_table(o) + ' WHERE id=?',
                               (o['order_id'],)).fetchone()
        if row is None:
            qty = o['qty']  # Missing records remain committed until reconciled.
        elif row['status'] in ('OPEN', 'PARTIAL', 'UNKNOWN'):
            qty = row['qty']
        else:
            qty = row['filled_qty']
        cents = _order_value(o, qty)
        committed[o['key']] = committed.get(o['key'], 0) + cents
    return committed


def _order_table(order):
    return {'watchlist': 'paper_orders', 'dynamic': 'dynamic_orders',
            'global': 'global_orders'}[order['key'].split(':', 1)[0]]


def _order_value(order, qty):
    if 'price_micros' in order:
        from .global_market import notional
        return notional(qty, order['price_micros'], order['fx_micros'])
    return qty * order['price_cents']


def register_order_inside(store, order, order_id, qty):
    """Save ownership in the same transaction as the order, including across restarts."""
    ep = _episode(store)
    if not ep or ep['id'] != order['episode']:
        raise ValueError('EXPOSURE_EPISODE_CHANGED')
    if not any(o['order_id'] == order_id for o in ep['orders']):
        ep['orders'].append({k: v for k, v in order.items() if k != 'episode'} |
                            {'order_id': order_id, 'qty': qty})
        put(store, 'exposure_reduce_episode', ep)


def _cancel_episode_orders_inside(store, ep):
    for order in ep.get('orders', []):
        store.db.execute('UPDATE ' + _order_table(order) +
                         " SET status='CANCELLED',reserved_cents=0 WHERE id=? AND side='SELL' AND status IN ('OPEN','PARTIAL')",
                         (order['order_id'],))


def _end_episode_inside(store, ep, outcome, at, cap, gross_bps, regime_info):
    from .storage import digest
    _cancel_episode_orders_inside(store, ep)
    store.db.execute('INSERT OR IGNORE INTO portfolio_risk_events VALUES(?,?,?)',
        (digest('reduce-episode-end:' + ep['id'] + ':' + outcome)[:24], at,
         json.dumps({'kind': 'EXPOSURE_CAP_EPISODE_' + outcome, 'episode': ep['id'],
                     'cap_pct': cap, 'gross_bps': gross_bps, 'quotas': ep['quotas'],
                     'orders': ep.get('orders', []), 'regime': regime_info}, sort_keys=True)))
    put(store, 'exposure_reduce_episode', {**ep, 'state': outcome, 'ended_at': at})


def _end_episode(store, ep, outcome, at, cap, gross_bps, regime_info):
    with store.db:
        _end_episode_inside(store, ep, outcome, at, cap, gross_bps, regime_info)


def order_can_settle_inside(store, config, at, route, order, terms):
    """Recheck only episode-owned SELLs under the settlement write lock.

    The marker also prevents an orphaned/stale episode order from being mistaken
    for an ordinary exit. Legacy r2 orders are recognized by the saved order IDs.
    UNKNOWN is never cancelled or released here.
    """
    if order['side'] != 'SELL':
        return True
    ep = value(store, 'exposure_reduce_episode') or {}
    listed = any(o['order_id'] == order['id'] and o['key'].startswith(route + ':')
                 for o in ep.get('orders', []))
    owner = terms.get('exposure_episode')
    if not owner and not listed:
        return True
    table = {'watchlist': 'paper_orders', 'dynamic': 'dynamic_orders', 'global': 'global_orders'}[route]
    if (owner and owner != ep.get('id')) or not listed or ep.get('state') != 'OPEN':
        store.db.execute('UPDATE ' + table + " SET status='CANCELLED',reserved_cents=0 WHERE id=? AND status IN ('OPEN','PARTIAL')",
                         (order['id'],))
        return False
    cap, info = _cap_state(store, config, at)
    cap = min(cap, config.get('paper_max_gross_pct', 90)) if cap is not None else None
    if cap is None or cap != ep['cap_pct']:
        _end_episode_inside(store, ep, 'SUPERSEDED', at, cap, None, info)
        return False
    if order['expires_at'] <= at:
        store.db.execute('UPDATE ' + table + " SET status='EXPIRED',reserved_cents=0 WHERE id=? AND status IN ('OPEN','PARTIAL')",
                         (order['id'],))
        return False
    from .paper import account_inside_transaction
    from .portfolio_risk import valuation_ready
    a = account_inside_transaction(store, at)
    if not valuation_ready(store, a, at):
        return False
    gross_bps = a['market_value_cents'] * 10000 // max(1, a['equity_cents'])
    if gross_bps <= cap * 100 + 500:
        _end_episode_inside(store, ep, 'RESOLVED', at, cap, gross_bps, info)
        return False
    return True


def _start_episode(store, at, cap, a, holdings):
    from .storage import digest
    equity = max(1, a['equity_cents'])
    excess = a['market_value_cents'] - cap * equity // 100
    total_mv = sum(h['mv'] for h in holdings) or 1
    ep = {'id': digest('reduce-episode:' + at)[:24], 'state': 'OPEN', 'cap_pct': cap,
          'started_at': at, 'excess_cents': excess,
          'quotas': {h['key']: excess * h['mv'] // total_mv for h in holdings}, 'orders': []}
    with store.db:
        put(store, 'exposure_reduce_episode', ep)
    return ep


def needs_reduction(store, config, at):
    """Cheap scheduler predicate: an unfinished deterministic reduction exists for a
    holding whose market is open now, or the book is over the live cap+5pp. No orders."""
    if config.get('deployment_role', 'standalone') == 'research':
        return False
    cap, _info = _cap_state(store, config, at)
    if cap is None:
        return False
    cap = min(cap, config.get('paper_max_gross_pct', 90))
    from .paper import account
    a = account(store, at)
    from .portfolio_risk import valuation_ready
    if not valuation_ready(store, a, at):
        return False
    ep = _episode(store)
    if ep and ep['cap_pct'] == cap:
        # An open episode for this cap means the over-cap check already ran; only a
        # holding that could actually sell right now justifies waking the executor.
        committed = _committed(store, ep)
        for h in _holdings(store, a, at):
            if (ep['quotas'].get(h['key'], 0) - committed.get(h['key'], 0) > 0
                    and h['session_open'] and h['sellable'] > 0):
                return True
        return False
    equity = max(1, a['equity_cents'])
    return a['market_value_cents'] * 10000 // equity > cap * 100 + 500


def reduce_to_cap(store, config, at):
    """Deterministic forced reduction toward an OK R1 cap (Dean Q1).

    Runs on execution nodes only — never on the research role. Only an OK-status R1
    cap triggers sells: the STALE/MISSING 10% fallback limits new buys only (Q2).
    The excess is split once into per-holding quotas over ALL holdings (sellable or
    not) and persisted as an episode: an unsellable holding keeps its share instead
    of it being redistributed, and the quota survives restarts, minutes and days
    until filled, resolved (back under cap+5pp) or superseded (cap changed). Each
    route sells only while its own market is open (A-shares: CONTINUOUS; global:
    its market's session). Orders go through the same functions the REDUCE path uses."""
    summary = {'at': at, 'triggered': False, 'orders': [], 'skipped': []}
    if config.get('deployment_role', 'standalone') == 'research':
        return summary
    cap, regime_info = _cap_state(store, config, at)
    from .paper import account
    a = account(store, at)
    ep = _episode(store)
    if cap is None:
        if ep:
            _end_episode(store, ep, 'SUPERSEDED', at, cap, None, regime_info)
        return summary
    cap = min(cap, config.get('paper_max_gross_pct', 90))
    from .portfolio_risk import valuation_ready
    if not valuation_ready(store, a, at):
        summary['blocked'] = 'VALUATION_INCOMPLETE'
        return summary
    equity = max(1, a['equity_cents'])
    gross_bps = a['market_value_cents'] * 10000 // equity
    summary.update(cap_pct=cap, gross_bps=gross_bps, regime=regime_info)
    if ep and ep['cap_pct'] != cap:
        _end_episode(store, ep, 'SUPERSEDED', at, cap, gross_bps, regime_info)
        ep = None
    if gross_bps <= cap * 100 + 500:
        if ep:
            _end_episode(store, ep, 'RESOLVED', at, cap, gross_bps, regime_info)
        return summary
    if ep is None:
        ep = _start_episode(store, at, cap, a, _holdings(store, a, at))
    excess = ep['excess_cents']
    summary.update(triggered=True, excess_cents=excess, episode=ep['id'])
    from .paper import market_guard, sell_quantity
    from .storage import digest
    committed = _committed(store, ep)
    # A non-episode open SELL blocks that holding only; episode orders never do.
    episode_order_ids = {o['order_id'] for o in ep.get('orders', [])}
    foreign_sells = {o['symbol'] for o in a['orders']
                     if o['side'] == 'SELL' and o['id'] not in episode_order_ids}
    for h in _holdings(store, a, at):
        key = h['key']
        remaining = ep['quotas'].get(key, 0) - committed.get(key, 0)
        if remaining <= 0:
            continue
        sym = h['symbol']
        if not h['session_open']:
            summary['skipped'].append({'symbol': sym, 'key': key, 'reason': 'SESSION_CLOSED'}); continue
        if sym in foreign_sells:
            summary['skipped'].append({'symbol': sym, 'key': key, 'reason': 'OPEN_SELL_ORDER'}); continue
        if not h['sellable']:
            summary['skipped'].append({'symbol': sym, 'key': key, 'reason': 'T_PLUS_ONE_LOCKED'}); continue
        if h['route'] == 'watchlist':
            q = store.latest_quote(sym, at)
        elif h['route'] == 'dynamic':
            from .dynamic_sources import latest_quote as dquote
            q = dquote(store, sym, at)
        else:
            from .global_market import latest as glatest
            q = glatest(store, sym, at)
        if not q:
            summary['skipped'].append({'symbol': sym, 'key': key, 'reason': 'NO_QUOTE'}); continue
        if h['route'] in ('watchlist', 'dynamic'):
            guard = market_guard(sym, q, 'SELL')
            if guard:
                summary['skipped'].append({'symbol': sym, 'key': key, 'reason': guard}); continue
        record = {'episode': ep['id'], 'key': key}
        if h['route'] == 'global':
            from .global_market import quantity_for_notional
            target = quantity_for_notional(sym, remaining, q['price_micros'], q['fx_micros'])
            record.update(price_micros=q['price_micros'], fx_micros=q['fx_micros'])
        else:
            target = remaining // q['price_cents']
            record['price_cents'] = q['price_cents']
        if h['route'] == 'watchlist':
            qty = sell_quantity(sym, min(h['sellable'], target), h['held'], h['sellable'])
            if qty <= 0:
                summary['skipped'].append({'symbol': sym, 'key': key, 'reason': 'LOT_RULES'}); continue
            plan = store.db.execute('SELECT * FROM plans WHERE symbol=? ORDER BY activated_at DESC,rowid DESC LIMIT 1',
                                    (sym,)).fetchone()
            if not plan:
                summary['skipped'].append({'symbol': sym, 'key': key, 'reason': 'NO_PLAN'}); continue
            from .paper import submit
            did = digest('reduce-cap:' + ep['id'] + ':' + sym + ':' + at)[:24]
            order = submit(store, config, did, dict(plan), 'SELL', q, at, sell_qty=qty, exposure_order=record)
        elif h['route'] == 'dynamic':
            qty = sell_quantity(sym, min(h['sellable'], target), h['sellable'], h['sellable'])
            if qty <= 0:
                summary['skipped'].append({'symbol': sym, 'key': key, 'reason': 'LOT_RULES'}); continue
            from .dynamic_paper import submit as dsubmit
            order = dsubmit(store, config, h['case'], 'SELL', q, at, 'EXPOSURE_CAP_REDUCE', sell_qty=qty, exposure_order=record)
        else:
            qty = min(h['held'], h['sellable'], target)
            if qty <= 0:
                summary['skipped'].append({'symbol': sym, 'key': key, 'reason': 'LOT_RULES'}); continue
            from .global_paper import submit as gsubmit
            order = gsubmit(store, config, sym, 'SELL', q, None, {}, at, sell_qty=qty, exposure_order=record)
        if 'id' in order:
            previous_ids = {o['order_id'] for o in ep['orders']}
            ep = _episode(store)
            if ep is None:
                return summary
            committed = _committed(store, ep)
            if order['id'] not in previous_ids:
                summary['orders'].append({'symbol': sym, 'route': h['route'], 'qty': order['qty'], 'order_id': order['id']})
        else:
            summary['skipped'].append({'symbol': sym, 'key': key, 'reason': order.get('reason', 'ORDER_FAILED')})
    if summary['orders']:
        with store.db:
            store.db.execute('INSERT OR IGNORE INTO portfolio_risk_events VALUES(?,?,?)',
                (digest('reduce-cap-episode:' + ep['id'] + ':' + at)[:24], at,
                 json.dumps({'kind': 'EXPOSURE_CAP_REDUCE', 'episode': ep['id'], 'cap_pct': cap,
                             'gross_bps': gross_bps, 'excess_cents': excess, 'orders': summary['orders'],
                             'skipped': summary['skipped'], 'regime': regime_info}, sort_keys=True)))
    unresolved = False
    for o in ep['orders']:
        row = store.db.execute('SELECT status FROM ' + _order_table(o) + ' WHERE id=?', (o['order_id'],)).fetchone()
        if row is None or row['status'] in ('OPEN', 'PARTIAL', 'UNKNOWN'):
            unresolved = True
            break
    if not unresolved and all(committed.get(k, 0) >= v for k, v in ep['quotas'].items()):
        _end_episode(store, ep, 'COMPLETE', at, cap, gross_bps, regime_info)
        summary['episode_state'] = 'COMPLETE'
    return summary


def gross_cap_pct(store, config, at):
    """(pct, detail): min(paper_max_gross_pct, regime cap) while the R1 regime is live,
    else the configured cap. On the cloud the stored bundle's staleness is re-derived
    on the cloud clock; OFF or never-received bundles leave the configured cap."""
    base = config.get('paper_max_gross_pct', 90)
    detail = {'base_pct': base, 'regime': None}
    if not regime_active(store, config):
        return base, detail
    if config.get('deployment_role') == 'cloud':
        return bundle_cap_pct(value(store, 'exposure_caps'), config, at)
    res = r1(store, at)
    detail['regime'] = res
    return min(base, res['cap_pct']), detail
