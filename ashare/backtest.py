"""Offline backtest of the two mechanical baselines on long daily histories.

Purpose: answer "does the rule itself have positive expectancy after costs?" before measuring what
research adds. It never touches the paper ledger and needs no model.

  ma      A-share rule: buy when the day touches MA20 +/- band while MA20 > MA60 and the prior close
          is at or above MA60; exit at the stop (the higher of cost -6% and MA20 -6%) or the target
          (MA20 +10%). Variants can add a trend exit or a maximum holding period.
  global  Spot rule for gold, silver, BTC and ETH: buy when the prior close is above its 20-day mean
          and above the close five days earlier and the day touches prior close +/- 1%; exit at cost
          -6% / +10% or after the holding period.

Prices are the provider's forward-adjusted (qfq) daily bars, so dividends do not register as losses.
Returns are per trade, net of commission, stamp tax and slippage, and measured against CSI 300 over the
same dates. The universe must be point-in-time (see UNIVERSE_HELP); testing only today's names leaves
out companies that later dropped out and makes results look better than they were.
"""
import csv
import json
import math
import statistics
from datetime import date, datetime, timezone
from pathlib import Path

UNIVERSE_HELP = ('universe CSV 列为 symbol,start,end（end 可空）。应使用当时的沪深300成分股区间（按中证指数公司历次调整公告整理）；'
                 '只用今天的成分股会漏掉中途被剔除的公司，使结果偏乐观。')
TENCENT = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,{start},{end},400,{kind}'
YAHOO = 'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=10y'
GLOBAL = {'BTC': 'BTC-USD', 'ETH': 'ETH-USD', 'GOLD': 'GC=F', 'SILVER': 'SI=F'}
MA_VARIANTS = {
    'baseline': {'band_bps': 200, 'stop_bps': 600, 'target_bps': 1000, 'trend_exit': False, 'max_hold': None},
    'trend_exit': {'band_bps': 200, 'stop_bps': 600, 'target_bps': 1000, 'trend_exit': True, 'max_hold': None},
    'hold_20d': {'band_bps': 200, 'stop_bps': 600, 'target_bps': 1000, 'trend_exit': False, 'max_hold': 20},
}
GLOBAL_VARIANTS = {'hold_5d': {'max_hold': 5}, 'hold_10d': {'max_hold': 10}, 'hold_20d': {'max_hold': 20}}


def data_dir(root):
    path = Path(root) / 'backtest' / 'data'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save(root, name, payload):
    (data_dir(root) / (name + '.json')).write_text(json.dumps(payload, ensure_ascii=False))


def _load(root, name):
    path = data_dir(root) / (name + '.json')
    return json.loads(path.read_text()) if path.exists() else None


def fetch_a_share(root, symbol, start, end, fetch=None, index=False):
    """Year-by-year pages of qfq daily bars (index: raw index levels). Stored as [date, o, c, h, l]."""
    from . import sources
    fetch = fetch or (lambda url: sources.fetch(url, max_bytes=3_000_000))
    bars = {}
    for year in range(int(start[:4]), int(end[:4]) + 1):
        url = TENCENT.format(symbol=symbol, start=max(start, f'{year}-01-01'), end=min(end, f'{year}-12-31'), kind='' if index else 'qfq')
        series = json.loads(fetch(url))['data'][symbol]
        for b in series.get('qfqday') or series.get('day') or []:
            bars[b[0]] = [b[0], float(b[1]), float(b[2]), float(b[3]), float(b[4])]
    payload = {'symbol': symbol, 'basis': 'INDEX' if index else 'QFQ', 'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
               'bars': [bars[d] for d in sorted(bars)]}
    _save(root, symbol, payload)
    return len(payload['bars'])


def fetch_global(root, asset, fetch=None):
    from .global_market import get_json
    fetch = fetch or get_json
    data = fetch(YAHOO.format(ticker=GLOBAL[asset]))['chart']['result'][0]
    q = data['indicators']['quote'][0]
    bars = []
    for i, ts in enumerate(data.get('timestamp', [])):
        values = [q[k][i] for k in ('open', 'close', 'high', 'low')]
        if all(v is not None and v > 0 for v in values):
            bars.append([datetime.fromtimestamp(ts, timezone.utc).date().isoformat(), *map(float, values)])
    _save(root, 'global-' + asset, {'symbol': asset, 'basis': 'USD', 'bars': bars})
    return len(bars)


def load_universe(path):
    rows = []
    with open(path, newline='', encoding='utf-8') as handle:
        for r in csv.DictReader(handle):
            symbol = (r.get('symbol') or '').strip()
            if symbol:
                rows.append({'symbol': symbol, 'start': (r.get('start') or '0000-00-00').strip(), 'end': (r.get('end') or '9999-12-31').strip() or '9999-12-31'})
    if not rows:
        raise ValueError('universe 文件为空。' + UNIVERSE_HELP)
    return rows


def costs_bps(config, notional_cents=2_000_000):
    """Round-trip cost as a share of the position, at a typical position size."""
    commission = max(config['paper_min_fee_cents'], notional_cents * config['paper_commission_bps'] / 10000) / notional_cents * 10000
    return 2 * commission + config['paper_sell_tax_bps'] + 2 * config['paper_slippage_bps']


def _touch(low, high, o, h, l):
    if low <= o <= high:
        return o
    if o > high and l <= high:
        return high
    if o < low and h >= low:
        return low
    return None


def simulate_ma(bars, variant, start='0000', end='9999'):
    """One position at a time per symbol. bars: [[date, o, c, h, l], ...] sorted by date."""
    trades, pos = [], None
    closes = [b[2] for b in bars]
    for i in range(60, len(bars)):
        d, o, c, h, l = bars[i]
        if d < start or d > end:
            continue
        ma20, ma60, prev = sum(closes[i - 20:i]) / 20, sum(closes[i - 60:i]) / 60, closes[i - 1]
        trend = ma20 > ma60 and prev >= ma60
        if pos:
            stop = max(pos['entry'] * (1 - variant['stop_bps'] / 10000), ma20 * (1 - variant['stop_bps'] / 10000))
            target = ma20 * (1 + variant['target_bps'] / 10000)
            held = i - pos['index']
            price, why = None, None
            if o <= stop:
                price, why = o, 'STOP'
            elif l <= stop:
                price, why = stop, 'STOP'
            elif o >= target:
                price, why = o, 'TARGET'
            elif h >= target:
                price, why = target, 'TARGET'
            elif variant.get('trend_exit') and not trend:
                price, why = o, 'TREND_EXIT'
            elif variant.get('max_hold') and held >= variant['max_hold']:
                price, why = c, 'MAX_HOLD'
            if price:
                trades.append({**pos, 'exit_date': d, 'exit': price, 'reason': why, 'held_days': held})
                pos = None
            continue
        if trend:
            price = _touch(ma20 * (1 - variant['band_bps'] / 10000), ma20 * (1 + variant['band_bps'] / 10000), o, h, l)
            if price:
                pos = {'entry_date': d, 'entry': price, 'index': i}
    return _close_open(trades, pos, bars, end)


def _close_open(trades, pos, bars, end):
    """A position still open when the test window ends is closed at the window's last close."""
    if pos:
        last = max(i for i, b in enumerate(bars) if b[0] <= end)
        trades.append({**pos, 'exit_date': bars[last][0], 'exit': bars[last][2], 'reason': 'END_OF_TEST', 'held_days': last - pos['index']})
    return trades


def simulate_global(bars, variant, start='0000', end='9999'):
    from .strategy_math import global_trend
    trades, pos = [], None
    closes = [b[2] for b in bars]
    micros = [int(round(c * 1_000_000)) for c in closes]
    for i in range(20, len(bars)):
        d, o, c, h, l = bars[i]
        if d < start or d > end:
            continue
        if pos:
            stop, target, held = pos['entry'] * 0.94, pos['entry'] * 1.10, i - pos['index']
            price, why = None, None
            if o <= stop:
                price, why = o, 'STOP'
            elif l <= stop:
                price, why = stop, 'STOP'
            elif o >= target:
                price, why = o, 'TARGET'
            elif h >= target:
                price, why = target, 'TARGET'
            elif held >= variant['max_hold']:
                price, why = c, 'MAX_HOLD'
            if price:
                trades.append({**pos, 'exit_date': d, 'exit': price, 'reason': why, 'held_days': held})
                pos = None
            continue
        prev = closes[i - 1]
        if global_trend(micros[:i]):
            price = _touch(prev * 0.99, prev * 1.01, o, h, l)
            if price:
                pos = {'entry_date': d, 'entry': price, 'index': i}
    return _close_open(trades, pos, bars, end)


def _bench_return(bench, entry_date, exit_date):
    if not bench or entry_date not in bench or exit_date not in bench:
        return None
    return (bench[exit_date][1] / bench[entry_date][0] - 1) * 10000


def describe(values):
    n = len(values)
    if not n:
        return {'n': 0}
    mean = sum(values) / n
    sd = statistics.stdev(values) if n > 1 else None
    return {'n': n, 'mean_bps': round(mean, 1), 'median_bps': round(statistics.median(values), 1),
            'hit_rate': round(sum(v > 0 for v in values) / n, 3),
            't_stat': round(mean / (sd / math.sqrt(n)), 2) if sd else None,
            'ci95_bps': [round(mean - 1.96 * sd / math.sqrt(n), 1), round(mean + 1.96 * sd / math.sqrt(n), 1)] if sd else None}


def evaluate(trades, cost_bps, bench=None):
    rows = []
    for t in trades:
        gross = (t['exit'] / t['entry'] - 1) * 10000
        b = _bench_return(bench, t['entry_date'], t['exit_date'])
        rows.append({**{k: t[k] for k in ('symbol', 'entry_date', 'exit_date', 'reason', 'held_days')},
                     'entry': round(t['entry'], 4), 'exit': round(t['exit'], 4), 'gross_bps': round(gross, 1),
                     'net_bps': round(gross - cost_bps, 1), 'benchmark_bps': None if b is None else round(b, 1),
                     'excess_bps': None if b is None else round(gross - cost_bps - b, 1)})
    by_year = {}
    for r in rows:
        by_year.setdefault(r['entry_date'][:4], []).append(r['net_bps'])
    excess = [r['excess_bps'] for r in rows if r['excess_bps'] is not None]
    return {'trades': rows, 'net': describe([r['net_bps'] for r in rows]), 'excess': describe(excess),
            'by_year': {y: describe(v) for y, v in sorted(by_year.items())},
            'exits': {k: sum(r['reason'] == k for r in rows) for k in sorted({r['reason'] for r in rows})},
            'avg_held_days': round(sum(r['held_days'] for r in rows) / len(rows), 1) if rows else None}


def run_ma(root, config, universe, start, end, variants=None):
    bench_payload = _load(root, 'sh000300')
    bench = {b[0]: (b[1], b[2]) for b in bench_payload['bars']} if bench_payload else None
    cost = costs_bps(config)
    results, missing = {}, []
    for name in variants or list(MA_VARIANTS):
        trades = []
        for member in universe:
            payload = _load(root, member['symbol'])
            if not payload:
                missing.append(member['symbol'])
                continue
            window_start, window_end = max(start, member['start']), min(end, member['end'])
            for t in simulate_ma(payload['bars'], MA_VARIANTS[name], window_start, window_end):
                trades.append({**t, 'symbol': member['symbol']})
        results[name] = evaluate(trades, cost, bench)
    return {'kind': 'ma', 'start': start, 'end': end, 'cost_bps': round(cost, 1), 'universe_size': len(universe),
            'missing_data': sorted(set(missing)), 'benchmark': bool(bench), 'variants': results}


def run_global(root, config, start, end, variants=None):
    cost = 2 * config['paper_commission_bps'] + 2 * config['paper_slippage_bps']
    results = {}
    for name in variants or list(GLOBAL_VARIANTS):
        trades = []
        for asset in GLOBAL:
            payload = _load(root, 'global-' + asset)
            if payload:
                trades += [{**t, 'symbol': asset} for t in simulate_global(payload['bars'], GLOBAL_VARIANTS[name], start, end)]
        results[name] = evaluate(trades, cost)
    return {'kind': 'global', 'start': start, 'end': end, 'cost_bps': round(cost, 1), 'variants': results,
            'method': 'global-trend-v2', 'limitations': [
                '趋势输入与生产共用公式；金银历史使用期货代理，不能视为现货模拟账户收益。',
                '美元计价，未计人民币汇率、最低佣金及盘中流动性；费用按固定基点近似。',
                '离线持有期按日观测数量，生产按自然时间；不重放模型与组合授权。']}


def _pct(v):
    return '—' if v is None else f'{v / 100:+.2f}%'


def markdown(result):
    lines = [f"# 机械规则离线回测（{result['kind']}，{result['start']} 至 {result['end']}）", '',
             f"往返成本按 {result['cost_bps'] / 100:.2f}% 计入。每笔交易收益为扣费后收益；超额收益相对沪深300同区间。", '']
    if result['kind'] == 'ma':
        lines += [f"股票池 {result['universe_size']} 个区间；缺数据 {len(result['missing_data'])} 只。{UNIVERSE_HELP}", '']
    lines += [f'- {s}' for s in result.get('limitations', [])] + ['']
    lines += ['| 规则变体 | 交易笔数 | 平均扣费收益 | t值 | 平均超额 | 超额95%区间 | 胜率 | 平均持有天数 |', '|---|---|---|---|---|---|---|---|']
    for name, r in result['variants'].items():
        n, e = r['net'], r['excess']
        ci = e.get('ci95_bps')
        lines.append(f"| {name} | {n.get('n', 0)} | {_pct(n.get('mean_bps'))} | {n.get('t_stat') or '—'} | {_pct(e.get('mean_bps'))} | "
                     + (f"{_pct(ci[0])} ~ {_pct(ci[1])}" if ci else '—') + f" | {n.get('hit_rate', '—')} | {r['avg_held_days'] or '—'} |")
    lines += ['', '## 分年度（扣费收益）', '']
    for name, r in result['variants'].items():
        lines.append(f"- {name}：" + '；'.join(f"{y} {s['n']}笔 {_pct(s.get('mean_bps'))}" for y, s in r['by_year'].items()))
    lines += ['', '解读提示：同一时期多只股票的交易受同一市场走势影响，彼此不独立，t值会高估把握程度；应同时看分年度是否稳定。']
    return '\n'.join(lines) + '\n'


def save(root, result):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder = Path(root) / 'backtest' / 'results' / f"{stamp}-{result['kind']}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'summary.json').write_text(json.dumps({**result, 'variants': {k: {kk: vv for kk, vv in v.items() if kk != 'trades'}
                                                                             for k, v in result['variants'].items()}}, ensure_ascii=False, indent=2))
    (folder / 'report.md').write_text(markdown(result), encoding='utf-8')
    for name, r in result['variants'].items():
        with (folder / f'trades-{name}.csv').open('w', newline='', encoding='utf-8') as handle:
            if r['trades']:
                writer = csv.DictWriter(handle, fieldnames=list(r['trades'][0]))
                writer.writeheader();writer.writerows(r['trades'])
    return folder
