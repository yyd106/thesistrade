"""Shadow books: record-only paper accounts that replay the same daily prices and fees under
different rule sets, so the contribution of each layer can be measured side by side.

  A  mechanical rule only (moving-average band entry, trend filter, stop and take-profit)
  B  A plus research eligibility (the plan active that morning must allow entry)
  C  B plus the portfolio decision (ALLOW required, its target caps the size, REDUCE/EXIT honoured)
  D  C's entries with rule-based sizing: fixed risk per trade over a volatility-based stop;
     the model may only veto (no ALLOW) or reduce (a lower target caps the rule size)

Each book runs twice: with board lot sizes (100 shares, STAR 200) at live account scale, and with
fractional shares, because at CNY 100,000 lot rounding alone can decide whether a trade happens.
Daily bars approximate intraday execution: an entry fills when the day's range touches the band,
exits fill at the stop or target level, or at the open when the price gaps through it. Opening
events come first, then intraday double touches assume the stop first. No book places orders.
"""
import json
from collections import Counter
from datetime import date, datetime, timedelta
from .calendar import trading_day, last_completed_day, SH
from .storage import now, normalize_time, digest
from .paper import fee, lot_rules

BOOKS = ('A', 'B', 'C', 'D')
VARIANTS = ('lot', 'frac')
INITIAL_CENTS = 10_000_000
METHOD = 'opening-evidence-v3'
CURRENT_KEY = 'shadow_current_v3'


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def bars_for(store, symbol):
    """Merge retained history; the newest collection wins for revised dates."""
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='market_features'").fetchone():
        return {}
    out = {}
    for row in store.db.execute('SELECT payload FROM market_features WHERE symbol=? ORDER BY created_at,rowid', (symbol,)):
        for b in (json.loads(row[0]).get('unadjusted') or {}).get('bars', []):
            try:
                prices = tuple(int(round(float(x) * 100)) for x in (b[1], b[2], b[3], b[4]))
                if min(prices) > 0 and prices[2] >= max(prices[0:2]) and prices[3] <= min(prices[0:2]):
                    out[b[0]] = prices
            except (IndexError, ValueError, TypeError, OverflowError):
                continue
    return out


def indicators(bars, day, config):
    """Signals from bars strictly before `day`, as a plan made after the prior close would see them."""
    dates = sorted(d for d in bars if d < day)
    if len(dates) < 60:
        return None
    if days_between(dates[-60], day)[:-1] != dates[-60:]:
        return None  # Missing history cannot silently change the MA/ATR window.
    closes = [bars[d][1] for d in dates]
    ma20, ma60, close = sum(closes[-20:]) // 20, sum(closes[-60:]) // 60, closes[-1]
    band = config.get('paper_entry_band_bps', 200)
    ranges = []
    for i in range(len(dates) - 14, len(dates)):
        o, c, h, l = bars[dates[i]]
        prev = bars[dates[i - 1]][1]
        ranges.append(max(h - l, abs(h - prev), abs(l - prev)))
    return {'ma20': ma20, 'ma60': ma60, 'close': close, 'trend_ok': ma20 > ma60 and close >= ma60,
            'levels': {'buy_low_cents': ma20 * (10000 - band) // 10000, 'buy_high_cents': ma20 * (10000 + band) // 10000,
                       'stop_cents': ma20 * (10000 - config['paper_stop_loss_bps']) // 10000,
                       'sell_cents': ma20 * (10000 + config['paper_take_profit_bps']) // 10000},
            'atr_bps': sum(ranges) * 10000 // 14 // close if close else None}


def plan_at(store, symbol, at):
    rows = store.db.execute('''SELECT * FROM plans WHERE symbol=? AND activated_at<?
        AND json_extract(payload_json,'$.kind')!='RISK_EXIT_ONLY' ORDER BY activated_at DESC,rowid DESC''', (symbol, at))
    for row in rows:
        event = store.db.execute('''SELECT status FROM plan_events WHERE plan_id=? AND at<?
            ORDER BY at DESC,id DESC LIMIT 1''', (row['id'], at)).fetchone()
        if not event or event[0] != 'ACTIVE':
            continue
        # The newest historical ACTIVE plan is authoritative even after expiry;
        # an older, superseded plan must never become eligible again.
        return {**dict(row), 'status': 'ACTIVE', 'payload': json.loads(row['payload_json'])} if row['valid_until'] > at else None
    return None


def decision_at(store, at, diagnostics=None):
    """Reconstruct the opening authorization, not today's mutable source rows."""
    from .portfolio_strategy import row_token
    from . import portfolio_history
    row = store.db.execute('SELECT * FROM portfolio_decisions WHERE created_at<? ORDER BY created_at DESC,rowid DESC LIMIT 1', (at,)).fetchone()
    if not row:
        return None
    payload = json.loads(row['payload_json'])
    items = payload.get('decisions', [])
    bykey = {d.get('key'): d for d in items}
    accepted = {}
    history = portfolio_history.snapshot(store, row['id'])
    required = {'key', 'route', 'identity', 'source_id', 'source_token', 'reference_id', 'related_keys', 'evidence_ids'}
    for d in items:
        key = d.get('key', '')
        if not key.startswith('watchlist:'):
            continue
        status, reason = 'VERIFIED', None
        dependencies = {key}
        if row['valid_until'] <= at:
            status, reason = 'INVALIDATED', 'PORTFOLIO_EXPIRED'
        elif len(bykey) != len(items) or not required <= set(d) or 'risk_groups' not in payload:
            status, reason = 'INSUFFICIENT', 'SOURCE_PROOF_MISSING'
        else:
            dependencies.update(d['related_keys'])
            refs = {x.get('reference_id'): x.get('key') for x in items if x.get('reference_id')}
            if any(ref not in refs for ref in d['evidence_ids']):
                status, reason = 'INSUFFICIENT', 'DEPENDENCY_RECORD_MISSING'
            dependencies.update(refs[ref] for ref in d['evidence_ids'] if ref in refs)
            for group in payload['risk_groups']:
                if key in group.get('keys', []):
                    dependencies.update(group['keys'])
            for dependency in sorted(dependencies):
                if status != 'VERIFIED':
                    break
                item = bykey.get(dependency)
                if not item or not required <= set(item):
                    status, reason = 'INSUFFICIENT', 'DEPENDENCY_RECORD_MISSING'
                    break
                source, missing = (portfolio_history.source_at(store, history, item, at) if history is not None else
                    portfolio_history.legacy_source_at(store, item['route'], item['identity'], at))
                if missing:
                    status = 'INVALIDATED' if missing == 'SOURCE_CHANGED_SINCE_DECISION' else 'INSUFFICIENT'
                    reason = missing
                elif (source or {}).get('id') != item['source_id']:
                    status, reason = 'INVALIDATED', 'SOURCE_ID_CHANGED'
                elif row_token(source) != item['source_token']:
                    status, reason = 'INVALIDATED', 'SOURCE_TOKEN_CHANGED'
            if status == 'VERIFIED' and d.get('action') == 'ALLOW' and d.get('membership_token') not in (None, 'legacy'):
                member = store.db.execute('''SELECT fingerprint FROM industry_memberships
                    WHERE symbol=? AND at<? ORDER BY at DESC,id DESC LIMIT 1''', (d.get('symbol') or d['identity'], at)).fetchone()
                token = d.get('membership_token')
                if member and member[0] != token:
                    status, reason = 'INVALIDATED', 'MEMBERSHIP_CHANGED'
                elif not member and token not in (None, 'legacy'):
                    status, reason = 'INSUFFICIENT', 'MEMBERSHIP_HISTORY_MISSING'
            if status == 'VERIFIED' and history is None:
                # Legacy created_at was sampled before taking the publication
                # write lock. It cannot establish visibility before this open.
                status, reason = 'INSUFFICIENT', 'LEGACY_PUBLICATION_TIME_UNPROVEN'
        check = {'at': at, 'portfolio_id': row['id'], 'key': key, 'status': status,
                 'reason': reason, 'dependencies': sorted(dependencies)}
        if diagnostics is not None:
            diagnostics.append(check)
        if status == 'VERIFIED':
            accepted[key] = {**d, '_valid_until': row['valid_until'], '_id': row['id']}
    return accepted


def entry_price(levels, o, h, l):
    """First price inside the buy band during the day, or None if the band was never touched."""
    low, high = levels['buy_low_cents'], levels['buy_high_cents']
    if low <= o <= high:
        return o
    if o > high and l <= high:
        return high
    if o < low and h >= low:
        return low
    return None


def exit_price(stop, target, o, h, l):
    """Known opening events precede the unknown intraday path; then stop first."""
    if stop and o <= stop:
        return o, 'STOP'
    if target and o >= target:
        return o, 'TARGET'
    if stop and l <= stop:
        return stop, 'STOP'
    if target and h >= target:
        return target, 'TARGET'
    return None, None


def start_day(store, config, at):
    configured = config.get('shadow_start_date')
    if configured:
        return configured
    row = store.db.execute("SELECT value FROM service_state WHERE key='shadow_start_date'").fetchone()
    if row:
        return row[0]
    first = last_completed_day(at)
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO service_state VALUES('shadow_start_date',?)", (first,))
    return first


def days_between(first, last):
    d = date.fromisoformat(first)
    out = []
    while d.isoformat() <= last:
        if trading_day(d) is True:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def previous_state(store, book, day, run_id):
    row = store.db.execute('SELECT payload_json FROM shadow_days_v2 WHERE run_id=? AND book=? AND day<? ORDER BY day DESC LIMIT 1', (run_id, book, day)).fetchone()
    if row:
        state = json.loads(row[0])
        return {'cash': state['cash'], 'positions': state['positions'], 'peak': state.get('peak', state['equity'])}
    return {'cash': INITIAL_CENTS, 'positions': {}, 'peak': INITIAL_CENTS}


def quantity(symbol, budget, price, variant):
    if price <= 0 or budget <= 0:
        return 0
    if variant == 'frac':
        return round(budget / price, 4)
    rules = lot_rules(symbol)
    if not rules:
        return 0
    qty = int(budget // price // 100 * 100)
    return qty if qty >= rules['min_buy'] else 0


def run_day(store, config, day, symbols, data, run_id):
    """Advance every book by one trading day. `data[symbol]` holds that symbol's bars."""
    morning = normalize_time(day + 'T09:30:00+08:00')
    close_at = normalize_time(day + 'T15:00:00+08:00')
    decisions = decision_at(store, morning) or {}
    slip = config['paper_slippage_bps']
    stored = []
    for book in BOOKS:
        for variant in VARIANTS:
            name = book + '-' + variant
            state = previous_state(store, name, day, run_id)
            cash, positions, trades = state['cash'], state['positions'], []
            opening_symbols = set(positions)
            equity_open = cash + sum(q['qty'] * data[s][day][0] for s, q in positions.items())
            invested = equity_open - cash
            buy_cash = cash  # No reuse of sales whose intraday time daily bars cannot establish.

            def record(symbol, side, qty, price, reason, extra=None):
                nonlocal cash
                gross = int(round(qty * price))
                cost = fee(config, side, gross)
                cash += -gross - cost if side == 'BUY' else gross - cost
                tid = digest(name + day + symbol + side)[:24]
                trades.append({'id': tid, 'symbol': symbol, 'side': side, 'qty': qty, 'price_cents': price, 'fee_cents': cost, 'reason': reason, **(extra or {})})
                return gross, cost

            # Exits affect closing cash only. Opening buy capacity is frozen above.
            for symbol in sorted(list(positions)):
                p = positions[symbol]
                bar = data.get(symbol, {}).get(day)
                if not bar or p['entry_day'] >= day:
                    continue
                o, c, h, l = bar
                sig = indicators(data[symbol], day, config) or {}
                plan = plan_at(store, symbol, morning) if book != 'A' else None
                levels = (plan['payload'].get('levels') if plan else None) or sig.get('levels') or {}
                cost_stop = int(p['cost_cents'] / p['qty'] * (10000 - config['paper_stop_loss_bps']) / 10000) if p['qty'] else 0
                stop = p.get('stop_cents') if book == 'D' else max(cost_stop, levels.get('stop_cents', 0))
                target = levels.get('sell_cents')
                price, why = exit_price(stop, target, o, h, l)
                wanted = p['qty'] if price else 0
                if book in ('C', 'D'):
                    d = decisions.get('watchlist:' + symbol)
                    if d and d['action'] == 'EXIT':
                        price, why, wanted = o, 'PORTFOLIO_EXIT', p['qty']
                    elif d and d['action'] == 'REDUCE' and not price:
                        keep = equity_open * d['target_bps'] / 10000 / o if o else p['qty']
                        excess = p['qty'] - keep
                        if variant == 'lot':
                            excess = int(-(-excess // 100) * 100) if excess > 0 else 0
                        wanted = min(p['qty'], max(0, excess))
                        if wanted:
                            price, why = o, 'PORTFOLIO_REDUCE'
                if price and wanted:
                    fill = int(price * (10000 - slip) // 10000)
                    gross = wanted * fill
                    basis = p['cost_cents'] * wanted / p['qty']
                    record(symbol, 'SELL', wanted, fill, why, {'realized_cents': int(round(gross - fee(config, 'SELL', int(round(gross))) - basis)),
                                                               'held_days': len(days_between(p['entry_day'], day)) - 1})
                    p['qty'] = round(p['qty'] - wanted, 4)
                    p['cost_cents'] -= basis
                    if p['qty'] <= 0:
                        del positions[symbol]

            # Entries use only cash/exposure known at the open; no same-day re-entry.
            for symbol in symbols:
                if symbol in opening_symbols:
                    continue
                bar = data.get(symbol, {}).get(day)
                sig = indicators(data.get(symbol, {}), day, config)
                if not bar or not sig:
                    continue
                o, c, h, l = bar
                if book == 'A':
                    if not sig['trend_ok']:
                        continue
                    levels = sig['levels']
                else:
                    plan = plan_at(store, symbol, morning)
                    if not plan or plan['payload'].get('kind') != 'PAPER_TRADE' or not plan['payload'].get('levels'):
                        continue
                    levels = plan['payload']['levels']
                target_cap = None
                valid_through_close = book == 'A' or plan['valid_until'] > close_at
                if book in ('C', 'D'):
                    d = decisions.get('watchlist:' + symbol)
                    if not d or d['action'] != 'ALLOW':
                        continue
                    target_cap = equity_open * d['target_bps'] // 10000
                    valid_through_close = valid_through_close and d['_valid_until'] > close_at
                # If authorization expires intraday, OHLC cannot locate the touch:
                # only an opening price already inside the band is admissible.
                price = entry_price(levels, o, h, l) if valid_through_close else entry_price(levels, o, o, o)
                if not price:
                    continue
                fill = min(levels['buy_high_cents'], (price * (10000 + slip) + 9999) // 10000)
                room = min(equity_open * config['paper_max_stock_pct'] // 100,
                           equity_open * config['paper_max_gross_pct'] // 100 - invested, min(cash, buy_cash) - 10_000)
                stop_cents = None
                if book == 'D':
                    distance = min(1000, max(300, 2 * (sig['atr_bps'] or 300)))
                    risk = equity_open * config.get('shadow_risk_per_trade_bps', 50) // 10000
                    room = min(room, risk * 10000 // distance)
                    stop_cents = fill * (10000 - distance) // 10000
                if target_cap is not None:
                    room = min(room, target_cap)
                qty = quantity(symbol, room, fill, variant)
                if not qty:
                    continue
                gross, cost = record(symbol, 'BUY', qty, fill, 'ENTRY', {'levels': levels, 'authorization_cutoff': morning,
                    'plan_id': plan['id'] if book != 'A' else None,
                    'portfolio_id': d['_id'] if book in ('C', 'D') else None})
                invested += gross
                buy_cash -= gross + cost
                positions[symbol] = {'qty': qty, 'cost_cents': gross + cost, 'entry_day': day, 'last': c, **({'stop_cents': stop_cents} if stop_cents else {})}

            # The caller has verified every symbol/day before entering this transaction.
            for symbol, p in positions.items():
                bar = data.get(symbol, {}).get(day)
                if bar:
                    p['last'] = bar[1]
            equity = cash + sum(int(round(p['qty'] * p['last'])) for p in positions.values())
            peak = max(state['peak'], equity)
            payload = {'cash': int(cash), 'positions': positions, 'equity': int(equity), 'peak': int(peak),
                       'drawdown_bps': int((peak - equity) * 10000 // peak) if peak else 0,
                       'exposure_bps': int((equity - cash) * 10000 // equity) if equity else 0,
                       'trades': [t['id'] for t in trades]}
            store.db.execute('INSERT INTO shadow_days_v2 VALUES(?,?,?,?)', (run_id, name, day, encode(payload)))
            for t in trades:
                store.db.execute('INSERT INTO shadow_trades_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                 (run_id, t['id'], name, day, t['symbol'], t['side'], t['qty'], t['price_cents'], t['fee_cents'], t['reason'], encode(t)))
            stored.append(name)
    return stored


def run(store, config, at=None):
    """Replay a complete consecutive prefix into an immutable, input-addressed batch.

    Late/corrected bars regenerate all dependent days. All previous method batches
    remain intact; a single pointer is published only after the replay commits.
    """
    if not config.get('shadow_books_enabled', True):
        return {'status': 'DISABLED'}
    at = normalize_time(at or now())
    from .portfolio_history import observe
    observe(store)
    last = last_completed_day(at)
    if not last:
        return {'status': 'CALENDAR_UNKNOWN'}
    first = start_day(store, config, at)
    # Freeze source evidence, its fingerprint and every resulting book-day in
    # one snapshot; a concurrent publication cannot alter the replay mid-run.
    with store.db:
        store.db.execute('BEGIN IMMEDIATE')
        return _run_locked(store, config, at, first, last)


def _run_locked(store, config, at, first, last):
    symbols = sorted(i['symbol'] for i in config['watchlist'])
    data = {s: {d: b for d, b in bars_for(store, s).items() if d <= last} for s in symbols}
    processed, missing = [], []
    for day in days_between(first, last):
        missing = [{'symbol': s, 'day': day, 'reason': 'MISSING_BAR' if day not in data[s] else 'INSUFFICIENT_HISTORY'}
                   for s in symbols if day not in data[s] or not indicators(data[s], day, config)]
        if missing:
            break
        processed.append(day)
    cutoffs = [normalize_time(d + 'T09:30:00+08:00') for d in processed]
    checks = []
    evidence = [{'at': t, 'plans': {s: plan_at(store, s, t) for s in symbols}, 'decisions': decision_at(store, t, checks)} for t in cutoffs]
    authorization = {'status': 'INSUFFICIENT' if any(c['status'] == 'INSUFFICIENT' for c in checks) else 'CHECKED',
                     'counts': dict(Counter(c['status'] for c in checks)),
                     'reasons': dict(Counter(c['reason'] for c in checks if c['reason'])), 'checks': checks}
    settings = {k: v for k, v in config.items() if k.startswith(('paper_', 'shadow_'))}
    rid = digest(encode([METHOD, first, last, symbols, data, evidence, authorization, settings]))[:32]
    result = {'status': 'SUCCEEDED' if not missing else ('PARTIAL' if processed else 'WAITING_DATA'),
              'method': METHOD, 'run_id': rid, 'start': first, 'requested_through': last,
              'through': processed[-1] if processed else None, 'missing': missing, 'days': processed,
              'authorization_checks': authorization,
              'limitations': '日线近似；未模拟盘中授权更新、12小时心跳、账户回撤停机及公司行为，不代表可执行净收益。缺价未确证停牌时等待。'
                  '开盘部分减仓与同日剩余仓位退出的先后仍是独立已知边界。'
                  '授权核验：' + encode(authorization['counts']) + '；原因：' + encode(authorization['reasons']) +
                  '。A/B按历史计划与行情近似；C/D另需来源和发布可见性证明。旧组合缺少提交时间上界，不能仅凭created_at确认开盘前可用。'
                  'INSUFFICIENT表示历史证据不足，相关C/D项保守跳过，不是研究WAIT，也不能用对应收益评价组合有效性。'}
    if not missing and authorization['status'] == 'INSUFFICIENT':
        result['status'] = 'INSUFFICIENT'
    exists = store.db.execute('SELECT 1 FROM shadow_evaluations WHERE id=?', (rid,)).fetchone()
    if not exists:
        for day in processed:
            run_day(store, config, day, symbols, data, rid)
        store.db.execute('INSERT INTO shadow_evaluations VALUES(?,?,?,?)', (rid, METHOD, at, encode(result)))
    store.db.execute('INSERT OR REPLACE INTO service_state VALUES(?,?)', (CURRENT_KEY, rid))
    return {**result, 'days': [] if exists else processed, 'reused': bool(exists)}


def status(store):
    row = store.db.execute("""SELECT e.payload_json FROM shadow_evaluations e JOIN service_state s
        ON s.key=? AND e.id=s.value WHERE e.method=?""", (CURRENT_KEY, METHOD)).fetchone()
    return json.loads(row[0]) if row else {'status': 'NOT_RUN', 'method': METHOD}


def summary(store, config, since=None):
    """Per book: return, drawdown, exposure and closed-trade statistics."""
    result = {}
    current = status(store)
    rid = current.get('run_id')
    for book in BOOKS:
        for variant in VARIANTS:
            name = book + '-' + variant
            rows = [json.loads(r[0]) | {'day': r[1]} for r in store.db.execute('SELECT payload_json,day FROM shadow_days_v2 WHERE run_id=? AND book=? ORDER BY day', (rid, name))]
            if not rows:
                continue
            base = next((r['equity'] for r in reversed(rows) if since and r['day'] < since), INITIAL_CENTS)
            window = [r for r in rows if not since or r['day'] >= since]
            trades = [json.loads(r[0]) for r in store.db.execute("SELECT payload_json FROM shadow_trades_v2 WHERE run_id=? AND book=? AND side='SELL'" + (' AND day>=?' if since else ''), (rid, name, since) if since else (rid, name))]
            wins = [t for t in trades if t.get('realized_cents', 0) > 0]
            result[name] = {'method': METHOD, 'run_id': rid, 'completeness': current['status'],
                            'from': window[0]['day'] if window else None, 'to': rows[-1]['day'], 'equity_cents': rows[-1]['equity'],
                            'return_pct': round((rows[-1]['equity'] / base - 1) * 100, 2),
                            'max_drawdown_pct': round(max(r['drawdown_bps'] for r in window) / 100, 2) if window else None,
                            'avg_exposure_pct': round(sum(r['exposure_bps'] for r in window) / len(window) / 100, 1) if window else None,
                            'closed_trades': len(trades), 'win_rate': round(len(wins) / len(trades), 3) if trades else None,
                            'avg_held_days': round(sum(t.get('held_days', 0) for t in trades) / len(trades), 1) if trades else None,
                            'open_positions': len(rows[-1]['positions'])}
    return result
