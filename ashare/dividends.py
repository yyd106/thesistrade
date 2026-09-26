"""Cash dividends on shares the paper account held at the record date.

Research verifies the implementation announcement and publishes it with the stock's plan
(corporate_actions: record date, ex-date, pre-tax cash per share). After the ex-date the ledger
that executes trades (the cloud, or a standalone install) credits the cash for the shares held at
the record-date close, once per dividend. The lots keep their cost; instead the cost stop counts
the dividends already received on the shares still held, so it measures price plus cash received
against cost (paper.positions -> dividend_cents, slots.hard_reason).

Not handled: dividend tax deducted at sale, bonus shares and capital-reserve conversions,
rights issues, differentiated dividends (never verified), and the dynamic and global routes."""
from __future__ import annotations
import json
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from .calendar import local
from .storage import digest, normalize_time

ACCOUNT = 'DEMO_PAPER'
KIND = 'CASH_DIVIDEND'
FEATURE = 'cash_dividend_credit'
PREFIX = 'cash_dividend:'
LOOKBACK_DAYS = 40  # plans and sales this recent are searched; a dividend is credited minutes after its first plan
MAX_GAP_DAYS = 15   # record date to ex-date


def supported(store, config):
    """Whether the ledger that executes trades credits cash dividends. A research node relies on the
    cloud advertising it; the cloud and a standalone install run the credit themselves."""
    if config.get('deployment_role', 'standalone') == 'research':
        from .cloud_sync import remote_supports
        return remote_supports(store, FEATURE)
    return True


def amount_cents(qty, cash):
    """Cash for qty shares at cash yuan per share, rounded down to the cent."""
    return int((Decimal(qty) * Decimal(cash) * 100).to_integral_value(rounding=ROUND_DOWN))


def reference(symbol, action, qty):
    return f"{PREFIX}{symbol}:{action['ex_date']}:{action['record_date']}:{qty}:{action['cash']}"


def parse(ref):
    """symbol, ex_date, record_date, qty, cash (Decimal) from a credit's reference; None if not one."""
    parts = (ref or '').split(':')
    if len(parts) != 6 or parts[0] + ':' != PREFIX:
        return None
    try:
        return {'symbol': parts[1], 'ex_date': parts[2], 'record_date': parts[3], 'qty': int(parts[4]), 'cash': Decimal(parts[5])}
    except (ValueError, InvalidOperation):
        return None


def _valid(action):
    """A published action in the plan's corporate_actions, checked again before money moves."""
    try:
        record, ex = date.fromisoformat(action['record_date']), date.fromisoformat(action['ex_date'])
        cash = Decimal(str(action['cash_per_share']))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None
    if not cash.is_finite() or not 0 < cash < 100 or not 0 < (ex - record).days <= MAX_GAP_DAYS:
        return None
    return {'record_date': record.isoformat(), 'ex_date': ex.isoformat(), 'cash': format(cash.normalize(), 'f'),
            'doc_id': action.get('doc_id')}


def actions(store, symbol, since):
    """Verified cash dividends for a stock from its research plans received since `since`, one per
    ex-date. An ex-date published with two different amounts or record dates is left out and reported."""
    found, conflicts = {}, set()
    rows = store.db.execute("""SELECT json_extract(payload_json,'$.corporate_actions') FROM plans
        WHERE symbol=? AND activated_at>=? AND json_extract(payload_json,'$.kind') IN ('PAPER_TRADE','NO_ENTRY')
        ORDER BY activated_at DESC,rowid DESC LIMIT 200""", (symbol, since)).fetchall()
    for (raw,) in rows:
        try:
            listed = json.loads(raw) if raw else []
        except ValueError:
            continue
        for item in listed if isinstance(listed, list) else []:
            a = _valid(item) if isinstance(item, dict) else None
            if not a:
                continue
            old = found.setdefault(a['ex_date'], a)
            if (old['cash'], old['record_date']) != (a['cash'], a['record_date']):
                conflicts.add(a['ex_date'])
    return [a for ex, a in sorted(found.items()) if ex not in conflicts], sorted(conflicts)


def held_at_record(store, symbol, record_date):
    cutoff = normalize_time(record_date + 'T23:59:59+08:00')
    return store.db.execute("SELECT coalesce(sum(CASE side WHEN 'BUY' THEN qty ELSE -qty END),0) FROM paper_fills WHERE symbol=? AND occurred_at<=?",
                            (symbol, cutoff)).fetchone()[0]


def _credited(store, symbol, ex_date):
    prefix = f'{PREFIX}{symbol}:{ex_date}:'
    return store.db.execute('SELECT 1 FROM paper_flows WHERE kind=? AND substr(reference,1,?)=?',
                            (KIND, len(prefix), prefix)).fetchone() is not None


def apply(store, symbol, action, at):
    """Credit one verified dividend for the shares held at the record-date close; a no-op when it was
    already credited, the ex-date has not come, or nothing was held. Returns the credit or None."""
    at = normalize_time(at)
    if action['ex_date'] > local(at).date().isoformat():
        return None
    store.db.execute('BEGIN IMMEDIATE')
    try:
        qty = held_at_record(store, symbol, action['record_date'])
        cents = amount_cents(qty, action['cash']) if qty > 0 else 0
        if _credited(store, symbol, action['ex_date']) or cents <= 0:
            store.db.commit()
            return None
        ref = reference(symbol, action, qty)
        store.db.execute('INSERT INTO paper_flows VALUES(?,?,?,?,?,?)', (digest(ACCOUNT + ':' + ref)[:24], ACCOUNT, KIND, cents, ref, at))
        if store.db.execute('UPDATE paper_accounts SET cash_cents=cash_cents+? WHERE id=?', (cents, ACCOUNT)).rowcount != 1:
            raise ValueError('模拟账户未初始化，不能记入分红')
        store.db.commit()
    except BaseException:
        store.db.rollback()
        raise
    return {'symbol': symbol, 'ex_date': action['ex_date'], 'record_date': action['record_date'], 'qty': qty,
            'cash_per_share': action['cash'], 'amount_cents': cents, 'reference': ref}


def _published(store, at):
    """(symbol, action) for every verified dividend of a stock held now or traded in the last
    LOOKBACK_DAYS (a position sold after the record date is still entitled), and the conflicts."""
    since = normalize_time((datetime.fromisoformat(at) - timedelta(days=LOOKBACK_DAYS)).isoformat())
    symbols = {r[0] for r in store.db.execute('SELECT DISTINCT symbol FROM paper_lots WHERE qty>0')}
    symbols |= {r[0] for r in store.db.execute('SELECT DISTINCT symbol FROM paper_fills WHERE occurred_at>=?', (since,))}
    found, conflicts = [], []
    for symbol in sorted(symbols):
        listed, conflicting = actions(store, symbol, since)
        found += [(symbol, a) for a in listed]
        conflicts += [f'{symbol}:{ex}' for ex in conflicting]
    return found, conflicts


def credit(store, config, at):
    """Credit every verified cash dividend due on shares held at its record date."""
    at = normalize_time(at)
    found, conflicts = _published(store, at)
    credited = [done for done in (apply(store, symbol, a, at) for symbol, a in found) if done]
    return {'credited': credited, 'conflicts': conflicts}


def received(store, lots):
    """Cash dividends already credited on the shares still held, per symbol, in cents. `lots` maps a
    symbol to its open lots (qty, acquired_day); a lot acquired after the record date got nothing."""
    result = {}
    for (ref,) in store.db.execute('SELECT reference FROM paper_flows WHERE kind=?', (KIND,)):
        d = parse(ref)
        if not d or d['symbol'] not in lots:
            continue
        cents = sum(amount_cents(l['qty'], d['cash']) for l in lots[d['symbol']] if l['acquired_day'] <= d['record_date'])
        if cents:
            result[d['symbol']] = result.get(d['symbol'], 0) + cents
    return result


def due(store, at):
    """Verified dividends on shares held at their record date whose ex-date has passed, and whether
    each was credited. On the research node this shows a credit the cloud has not made (its errors
    stay in the cloud's service_state)."""
    at = normalize_time(at);today = local(at).date().isoformat()
    found, conflicts = _published(store, at)
    rows = []
    for symbol, a in found:
        qty = held_at_record(store, symbol, a['record_date'])
        if a['ex_date'] <= today and qty > 0:
            rows.append({'symbol': symbol, 'ex_date': a['ex_date'], 'record_date': a['record_date'], 'qty': qty,
                         'cash_per_share': a['cash'], 'amount_cents': amount_cents(qty, a['cash']),
                         'credited': _credited(store, symbol, a['ex_date'])})
    return {'due': rows, 'conflicts': conflicts}


def history(store):
    """Every credit, newest first, for reports and the dividends command."""
    rows = []
    for r in store.db.execute('SELECT reference,amount_cents,created_at FROM paper_flows WHERE kind=? ORDER BY created_at DESC,rowid DESC', (KIND,)):
        d = parse(r['reference'])
        if d:
            rows.append({'symbol': d['symbol'], 'ex_date': d['ex_date'], 'record_date': d['record_date'], 'qty': d['qty'],
                         'cash_per_share': format(d['cash'], 'f'), 'amount_cents': r['amount_cents'], 'credited_at': r['created_at']})
    return rows
