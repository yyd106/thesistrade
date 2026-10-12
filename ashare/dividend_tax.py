"""Individual A-share dividend tax, on credited watchlist dividends only.

Tax lots use end-of-day net acquisitions/disposals (财税〔2012〕85号, art. 3),
independently of the execution ledger's cost lots. A closed day's net disposal
uses chronological FIFO; a natural month/year anniversary is inclusive.
财税〔2015〕101号 supplies the effective 20%, 10%, 0% rates. ChinaClear's
settlement interface rounds cash/share * rate * taxable shares to cents per
dividend, disposal day and rate. No accrued tax is mistaken for a cash payment.

Only negative DIVIDEND_TAX cash flows are added. Original dividend credits,
fills, costs, capital and withdrawals remain immutable. Late corrections are
posted at the reconciliation clock, with the original disposal day referenced.
"""
from __future__ import annotations

from calendar import monthrange
from collections import defaultdict, deque
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from .calendar import local, trading_day
from .storage import digest, normalize_time

ACCOUNT = 'DEMO_PAPER'
KIND = 'DIVIDEND_TAX'
PREFIX = 'dividend_tax:v1:'


def can_post(store, config):
    """Ownership gate only: an expired research heartbeat does not erase tax debt."""
    role = config.get('deployment_role', 'standalone')
    if role == 'research' or config.get('mode', 'paper') != 'paper':
        return False
    if role == 'cloud':
        from .cloud_runtime import value
        return value(store, 'initialized', False) is True and value(store, 'execution_enabled', False) is True
    return role == 'standalone'


def holding_rate_bps(acquired_day, sold_day):
    """The sale anniversary itself still belongs to the shorter holding tier."""
    acquired, sold = date.fromisoformat(acquired_day), date.fromisoformat(sold_day)
    if sold < acquired:
        raise ValueError('红利税持有日期倒置')
    month = acquired.month % 12 + 1
    year = acquired.year + (acquired.month == 12)
    month_end = date(year, month, min(acquired.day, monthrange(year, month)[1]))
    year_end = date(acquired.year + 1, acquired.month,
                    min(acquired.day, monthrange(acquired.year + 1, acquired.month)[1]))
    # Published examples establish ordinary anniversaries, but not a missing
    # day (Jan 31 -> February, or Feb 29 -> a common year). Do not guess a lower
    # tax tier in the narrow interval where clamping and carrying differ.
    for end, overflow in (
            (month_end, date(year, month, 1) + timedelta(days=acquired.day - 1)),
            (year_end, date(acquired.year + 1, acquired.month, 1) + timedelta(days=acquired.day - 1))):
        if end < sold <= overflow:
            raise ValueError('DIVIDEND_TAX_HOLDING_BOUNDARY_UNVERIFIED：自然月/年无对应日的税档边界待核')
    return 2000 if sold <= month_end else 1000 if sold <= year_end else 0


def reference(dividend_flow_id, sell_day):
    return PREFIX + dividend_flow_id + ':' + sell_day


def _closed_day(at):
    stamp = local(at)
    # Same-day trading is final only after the known A-share session closes.
    if stamp.strftime('%H:%M:%S') >= '15:00:00' and trading_day(stamp.date()) is True:
        return stamp.date().isoformat()
    return (stamp.date() - timedelta(days=1)).isoformat()


def _timeline(store, symbol, at):
    """Reconstruct tax lots from immutable fills, never paper_lots or cost basis."""
    daily = defaultdict(list)
    cutoff = _closed_day(at)
    for row in store.db.execute('''SELECT rowid AS source_rowid,* FROM paper_fills
            WHERE symbol=? AND occurred_at<=? AND recorded_at<=?
            ORDER BY occurred_at,recorded_at,rowid''', (symbol, at, at)):
        fill = dict(row)
        day = local(fill['occurred_at']).date().isoformat()
        if day > cutoff:
            continue
        if fill['side'] not in ('BUY', 'SELL') or type(fill['qty']) is not int or fill['qty'] <= 0:
            raise ValueError('红利税成交份额无效')
        daily[day].append(fill)
    lots, sales, balances = deque(), [], []
    held = 0
    for day, fills in sorted(daily.items()):
        net = sum(f['qty'] * (1 if f['side'] == 'BUY' else -1) for f in fills)
        if net > 0:
            remaining = net
            for fill in (f for f in fills if f['side'] == 'BUY'):
                take = min(remaining, fill['qty'])
                lots.append({'buy_fill_id': fill['id'], 'acquired_at': fill['occurred_at'],
                             'acquired_day': day, 'qty': take})
                remaining -= take
                if not remaining:
                    break
        elif net < 0:
            remaining, allocations = -net, []
            while remaining and lots:
                lot = lots[0]
                take = min(remaining, lot['qty'])
                allocations.append({**lot, 'qty': take})
                lot['qty'] -= take
                remaining -= take
                if not lot['qty']:
                    lots.popleft()
            if remaining:
                raise ValueError('红利税成交历史不完整：日净卖出超过此前持仓')
            sellers = [f for f in fills if f['side'] == 'SELL']
            sales.append({'sell_day': day, 'sold_at': sellers[-1]['occurred_at'],
                          'sell_fill_id': sellers[-1]['id'],
                          'sell_fill_ids': [f['id'] for f in sellers],
                          'allocations': allocations})
        held += net
        balances.append((day, held))
    return sales, balances


def _expected(store, at):
    from .dividends import KIND as DIVIDEND_KIND, parse, amount_cents
    result, timelines = [], {}
    for raw in store.db.execute('''SELECT * FROM paper_flows
            WHERE account_id=? AND kind=? AND created_at<=? ORDER BY created_at,id''',
            (ACCOUNT, DIVIDEND_KIND, at)):
        flow = dict(raw)
        d = parse(flow['reference'])
        if not d or d['qty'] <= 0 or not d['cash'].is_finite() or d['cash'] <= 0:
            raise ValueError('红利税缺少有效的原分红凭证')
        try:
            record = date.fromisoformat(d['record_date'])
            ex = date.fromisoformat(d['ex_date'])
        except ValueError as exc:
            raise ValueError('红利税原分红日期无效') from exc
        if ex <= record or flow['amount_cents'] <= 0 or amount_cents(d['qty'], d['cash']) != flow['amount_cents']:
            raise ValueError('红利税原分红金额或数量与凭证不一致')
        if d['symbol'] not in timelines:
            timelines[d['symbol']] = _timeline(store, d['symbol'], at)
        sales, balances = timelines[d['symbol']]
        record_qty = next((qty for day, qty in reversed(balances) if day <= d['record_date']), 0)
        if record_qty != d['qty']:
            raise ValueError('红利税登记日持仓与已入账分红数量不一致')
        sold_qty, total_tax = 0, 0
        for sale in sales:
            if sale['sell_day'] <= d['record_date']:
                continue
            allocations = []
            quantities = defaultdict(int)
            for lot in sale['allocations']:
                if lot['acquired_day'] > d['record_date']:
                    continue
                rate = holding_rate_bps(lot['acquired_day'], sale['sell_day'])
                quantities[rate] += lot['qty']
                allocations.append({**lot, 'rate_bps': rate,
                                    'taxable_dividend_cents': str(d['cash'] * 100 * lot['qty'])})
            qty = sum(quantities.values())
            sold_qty += qty
            taxes = [{'rate_bps': rate, 'qty': count,
                      'tax_cents': int((d['cash'] * count * rate / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))}
                     for rate, count in sorted(quantities.items(), reverse=True)]
            tax = sum(t['tax_cents'] for t in taxes)
            total_tax += tax
            if not tax:
                continue
            result.append({**{k: sale[k] for k in ('sell_day', 'sold_at', 'sell_fill_id', 'sell_fill_ids')},
                           'reference': reference(flow['id'], sale['sell_day']),
                           'dividend_flow_id': flow['id'], 'symbol': d['symbol'],
                           'record_date': d['record_date'], 'ex_date': d['ex_date'],
                           'cash_per_share': str(d['cash']), 'qty': qty,
                           'tax_cents': tax, 'tax_rates': taxes, 'allocations': allocations})
        if sold_qty > d['qty'] or total_tax > flow['amount_cents']:
            raise ValueError('红利税超过原分红权益数量或已入账金额，需核对')
    return result


def _due_inside(store, at):
    expected = {row['reference']: row for row in _expected(store, at)}
    posted = {row['reference']: dict(row) for row in store.db.execute('''SELECT * FROM paper_flows
        WHERE account_id=? AND kind=? AND created_at<=?''', (ACCOUNT, KIND, at))}
    for ref, row in posted.items():
        if ref not in expected or row['amount_cents'] != -expected[ref]['tax_cents']:
            raise ValueError('红利税已记流水与重放结果不一致，禁止重复或覆盖扣款')
    due = [row for ref, row in expected.items() if ref not in posted]
    return {'due': due, 'due_cents': sum(row['tax_cents'] for row in due),
            'settled_through_day': _closed_day(at), 'basis': 'A_SHARE_INDIVIDUAL_DAILY_NET_FIFO'}


def tax_due(store, at):
    """Read-only preview of unpaid tax for closed days, including old sold holdings."""
    at = normalize_time(at)
    if store.db.in_transaction:
        return _due_inside(store, at)
    store.db.execute('BEGIN')
    try:
        result = _due_inside(store, at)
        store.db.commit()
        return result
    except BaseException:
        store.db.rollback()
        raise


preview = tax_due


def reconcile_taxes_inside(store, config, at):
    """Caller owns the cash transaction; insufficient cash aborts the whole batch."""
    if not can_post(store, config):
        return {'status': 'SKIPPED', 'posted': [], 'tax_cents': 0}
    if not store.db.in_transaction:
        raise RuntimeError('红利税扣款需要资金事务')
    at = normalize_time(at)
    due = _due_inside(store, at)
    if not due['due']:
        return {'status': 'CURRENT', 'posted': [], 'tax_cents': 0}
    account = store.db.execute('SELECT cash_cents FROM paper_accounts WHERE id=?', (ACCOUNT,)).fetchone()
    if account is None or account['cash_cents'] < due['due_cents']:
        raise ValueError('DIVIDEND_TAX_INSUFFICIENT_CASH：红利税可用现金不足，未扣款，需核对')
    posted = []
    for row in due['due']:
        fid = digest(ACCOUNT + ':' + row['reference'])[:24]
        store.db.execute('INSERT INTO paper_flows VALUES(?,?,?,?,?,?)',
                         (fid, ACCOUNT, KIND, -row['tax_cents'], row['reference'], at))
        posted.append({**row, 'flow_id': fid, 'amount_cents': -row['tax_cents'], 'posted_at': at})
    store.db.execute('UPDATE paper_accounts SET cash_cents=cash_cents-? WHERE id=?', (due['due_cents'], ACCOUNT))
    return {'status': 'POSTED', 'posted': posted, 'tax_cents': due['due_cents']}


def reconcile_taxes(store, config, at):
    if not can_post(store, config):
        return {'status': 'SKIPPED', 'posted': [], 'tax_cents': 0}
    store.db.execute('BEGIN IMMEDIATE')
    try:
        result = reconcile_taxes_inside(store, config, at)
        store.db.commit()
        return result
    except BaseException:
        store.db.rollback()
        raise


def tax_history(store):
    """Posted cash taxes and their source dividend/sale provenance for reporting."""
    flows = [dict(r) for r in store.db.execute('''SELECT * FROM paper_flows
        WHERE account_id=? AND kind=? ORDER BY created_at DESC,rowid DESC''', (ACCOUNT, KIND))]
    if not flows:
        return []
    # Reconstruct at each posting clock, preserving the original known history.
    expected_by_at, result = {}, []
    for flow in flows:
        at = flow['created_at']
        if at not in expected_by_at:
            expected_by_at[at] = {r['reference']: r for r in _expected(store, at)}
        row = expected_by_at[at].get(flow['reference'])
        if row is None or flow['amount_cents'] != -row['tax_cents']:
            raise ValueError('红利税历史流水缺少一致的分红及卖出依据')
        result.append({**row, 'flow_id': flow['id'], 'amount_cents': flow['amount_cents'],
                       'posted_at': at})
    return result
