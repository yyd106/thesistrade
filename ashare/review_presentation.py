"""Read-only review facts for the dashboard, with bounded history and clear scope.

These projections never rewrite a review, close an issue or promote a proposal.
"""
from datetime import datetime, timedelta
import math

from .storage import normalize_time


def text(value, limit=800):
    return value[:limit] + ('…（原文保留在本机）' if len(value) > limit else '') if isinstance(value, str) else ''


def stamp(value):
    try:
        return normalize_time(value) if isinstance(value, str) else None
    except (ValueError, TypeError):
        return None


def age_days(value, at):
    start, end = stamp(value), stamp(at)
    return max(0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 86400) if start and end else None


def checks_summary(values):
    values = values if isinstance(values, list) else []
    rows = [v for v in values if isinstance(v, dict)]
    counts = {state: sum(v.get('status') == state for v in rows)
              for state in ('FAIL', 'INSUFFICIENT', 'PASS', 'NOT_APPLICABLE')}
    counts['UNKNOWN'] = len(rows) - sum(counts.values())
    rows.sort(key=lambda v: {'FAIL': 0, 'INSUFFICIENT': 1, 'PASS': 3, 'NOT_APPLICABLE': 4}.get(v.get('status'), 2))
    public = []
    for row in rows[:24]:
        item = {key: row.get(key) for key in ('check', 'status', 'checked', 'failures', 'missing')}
        item['check'] = text(item['check'], 100)
        item['detail'] = text(row.get('detail'), 400)
        groups = [g for g in (row.get('missing_groups') or []) if isinstance(g, dict)
                  and g.get('route') in ('watchlist', 'dynamic', 'global')
                  and type(g.get('count')) is int and g['count'] > 0]
        if groups:
            item['missing_groups'] = [{key: text(g.get(key), 100) for key in ('check', 'route', 'missing')}
                                      | {'count': g['count']} for g in groups[:24]]
            item['missing_groups_total'] = len(groups)
            item['missing_groups_omitted'] = max(0, len(groups) - 24)
        item['examples'] = []
        for example in (row.get('examples') or [])[:3]:
            if not isinstance(example, dict):
                continue
            sample = {key: text(example.get(key), 160) for key in ('route', 'symbol', 'fill_id', 'reason', 'missing', 'detail', 'position')
                      if isinstance(example.get(key), str)}
            for key in ('pending', 'failed', 'total', 'free_bytes', 'database_bytes', 'warning'):
                if type(example.get(key)) in (int, bool):
                    sample[key] = example[key]
            for key in ('free_gb', 'db_gb', 'backups_gb'):
                if type(example.get(key)) in (int, float) and math.isfinite(example[key]):
                    sample[key] = example[key]
            if 'last_sync' in example:
                sync = example['last_sync'] if isinstance(example['last_sync'], dict) else {}
                sample['last_sync'] = {key: text(sync.get(key), 60) for key in ('at', 'status', 'phase') if isinstance(sync.get(key), str)}
            if sample:
                item['examples'].append(sample)
        public.append(item)
    return {'items': public, 'counts': counts, 'total': len(rows), 'omitted': max(0, len(rows) - len(public))}


def findings(store, review, payload, at):
    lessons = (payload.get('analysis') or {}).get('lessons') or []
    routes = {v.get('lesson'): v for v in payload.get('routing', []) if isinstance(v, dict)}
    result = []
    for ordinal, lesson in enumerate(lessons[:5]):
        if not isinstance(lesson, dict):
            continue
        route = routes.get(ordinal, {})
        item = {'ordinal': ordinal, 'lesson': text(lesson.get('lesson'), 800),
                'applicability': text(lesson.get('applicability'), 500), 'category': text(lesson.get('category'), 40),
                'to': route.get('to'), 'id': text(route.get('id'), 100), 'status': 'UNKNOWN'}
        row = store.db.execute('SELECT expires_at FROM lessons WHERE id=?', (review['id'] + ':' + str(ordinal),)).fetchone()
        created = stamp(review.get('ready_at'))
        expiry = stamp(row['expires_at']) if row else (normalize_time((datetime.fromisoformat(created) + timedelta(days=30)).isoformat()) if created else None)
        item['expires_at'] = expiry
        item['expired'] = bool(expiry and stamp(at) and expiry <= stamp(at))
        if item['to'] == 'engineering_issue':
            current = store.db.execute('SELECT status,last_seen_at,resolved_at FROM engineering_issues WHERE id=?', (item['id'],)).fetchone()
            if current:
                item.update(status=current['status'], updated_at=current['resolved_at'] or current['last_seen_at'])
        elif item['to'] == 'proposal_draft':
            current = store.db.execute('SELECT status,created_at,decided_at FROM strategy_proposals WHERE id=?', (item['id'],)).fetchone()
            if current:
                item.update(status=current['status'], updated_at=current['decided_at'] or current['created_at'])
        item['current'] = not item['expired'] and item['status'] in ('OPEN', 'DRAFT', 'READY', 'APPROVED', 'ADOPTED')
        result.append(item)
    return {'items': result, 'total': len(lessons), 'omitted': max(0, len(lessons) - len(result)),
            'notice': '发现按生成后30日区分当前与历史；仅影响展示。问题和提案继续按各自状态跟踪，复盘意见不会直接进入研究。'}


def quote_provenance(store, position, value, known_at):
    """Explain the exact frozen quote; never substitute a newer market price."""
    value = value if isinstance(value, dict) else {}
    observed = stamp(value.get('quote_at'))
    seen = stamp(value.get('quote_first_seen_at'))
    source = text(value.get('quote_source'), 180)
    origin = position.get('origin') or str(position.get('key') or '').partition(':')[0]
    symbol = position.get('symbol')
    matches = []
    if observed and symbol and known_at:
        if origin == 'global' and type(value.get('price_micros')) is int:
            matches = [dict(r) for r in store.db.execute('''SELECT first_seen_at,
                json_extract(payload_json,'$.provider') AS source FROM global_quotes
                WHERE symbol=? AND observed_at=? AND price_micros=? AND fx_micros=? AND fx_at=?
                AND first_seen_at<=? ORDER BY first_seen_at DESC LIMIT 1''',
                (symbol, observed, value['price_micros'], value.get('fx_micros'), value.get('fx_at'), known_at))]
        elif origin in ('watchlist', 'dynamic') and type(value.get('price_cents')) is int:
            for table in (('quotes', 'dynamic_quotes') if origin == 'dynamic' else ('quotes',)):
                source_column = 'source' if table == 'quotes' else "'tencent_public_research' AS source"
                receipt_filter = ' AND first_seen_at=?' if seen else ''
                params = (symbol, observed, value['price_cents'], known_at) + ((seen,) if seen else ())
                row = store.db.execute(f'''SELECT first_seen_at,{source_column} FROM {table}
                    WHERE symbol=? AND observed_at=? AND price_cents=? AND first_seen_at<=?{receipt_filter}
                    ORDER BY first_seen_at DESC LIMIT 1''', params).fetchone()
                if row:
                    matches.append(dict(row))
        if matches:
            match = max(matches, key=lambda row: row['first_seen_at'])
            source = text(match.get('source'), 180)
            seen = match['first_seen_at']
    labels = {'tencent_public_research': '腾讯公开行情', 'tencent_minute_fallback': '腾讯分时备用接口'}
    provider_time = source in labels
    return {'quote_at': observed, 'quote_first_seen_at': seen, 'quote_source': source or None,
            'quote_source_label': labels.get(source, source or '历史来源未留存'),
            'quote_time_kind': 'PROVIDER_QUOTE_TIME' if source else 'UNKNOWN_SOURCE',
            'quote_time_notice': ('报价时间取自腾讯接口返回的行情时间字段；首次采集时间另列。15:00后的时间戳仅表示供应商盘后快照时间，不能据此认定发生盘后成交或已核验为交易所收盘价。'
                                  if provider_time else '报价时间为供应商返回的行情时间，首次采集时间另列。' if source else '历史快照未保留可核对的来源，不能将该时间当作成交时间或首次采集时间。'),
            'provenance_status': 'MATCHED' if matches else 'FROZEN' if source else 'UNAVAILABLE'}


def accounting(portfolio, store=None, known_at=None, positions=None):
    if not isinstance(portfolio, dict):
        return None
    dividends = portfolio.get('dividends') or []
    valid = [d for d in dividends if isinstance(d, dict) and type(d.get('amount_cents')) is int]
    result = {key: portfolio.get(key) for key in ('window_start', 'window_end', 'totals', 'opening', 'closing')} | {
        'dividend_cents': portfolio.get('dividend_cents', sum(d['amount_cents'] for d in valid)),
        'dividend_count': portfolio.get('dividend_count', len(valid)),
        'dividends_known': 'dividends' in portfolio or portfolio.get('dividends_known', False),
        'valuation_notice': text(portfolio.get('valuation_notice'), 700)}
    start, end = stamp(portfolio.get('window_start')), stamp(portfolio.get('window_end'))
    if store and start and end:
        # A read-only supplement to frozen reports. The window is the credit time,
        # not the ex-date, and the lifetime sum also retains sold-share dividends.
        from .dividends import ACCOUNT, KIND
        flows = list(store.db.execute('''SELECT kind,amount_cents,created_at FROM paper_flows
            WHERE account_id=? AND created_at<?''', (ACCOUNT, end)))
        credited = [row for row in flows if row['kind'] == KIND]
        period = [row for row in credited if row['created_at'] >= start]
        trading = (portfolio.get('totals') or {}).get('cumulative_profit_cents')
        equity = (portfolio.get('closing') or {}).get('equity_cents')
        initial = [row for row in flows if row['kind'] == 'SIMULATED_INITIAL']
        cumulative = sum(row['amount_cents'] for row in credited) if initial else portfolio.get('cumulative_dividend_cents')
        withdrawn = -sum(row['amount_cents'] for row in flows if row['kind'] == 'SIMULATED_WITHDRAWAL')
        account = equity + withdrawn - sum(row['amount_cents'] for row in initial) if type(equity) is int and initial else None
        difference = account - trading - cumulative if type(trading) is int and account is not None and type(cumulative) is int else None
        basis = 'FROZEN_EQUITY_WITH_LEDGER_FLOWS' if account is not None else 'TRADE_PNL_PLUS_LEDGER_DIVIDENDS'
        if account is None and type(trading) is int and type(cumulative) is int:
            account = trading + cumulative
        if not initial:
            basis = 'FROZEN_DISPLAY' if type(cumulative) is int else 'FLOW_HISTORY_UNAVAILABLE'
            # An incomplete cloud replica must retain a signed reconciliation,
            # including any discrepancy, rather than silently recompute it away.
            if type(portfolio.get('account_cumulative_profit_cents')) is int:
                account = portfolio['account_cumulative_profit_cents']
                difference = portfolio.get('reconciliation_difference_cents')
        if initial:
            result.update(dividend_cents=sum(row['amount_cents'] for row in period), dividend_count=len(period), dividends_known=True)
        result.update(period_dividend_cents=result['dividend_cents'] if result['dividends_known'] else None,
                      cumulative_dividend_cents=cumulative, cumulative_dividend_count=len(credited) if initial else portfolio.get('cumulative_dividend_count'),
                      account_cumulative_profit_cents=account, reconciliation_difference_cents=difference,
                      accounting_basis=basis,
                      accounting_notice='以冻结复盘估值和截止前已入账流水对账；累计分红包含此前及已清仓股份的分红，24小时分红仅统计本期入账。累计交易损益含全部历史卖出和期末浮动损益、已计费用；账户累计收益还含现金分红。展示补充不会修改原复盘记录。')
        if positions is None:
            positions = portfolio.get('positions') or []
        result['quote_provenance'] = [
            {key: position.get(key) for key in ('key', 'symbol', 'origin')} |
            {side: quote_provenance(store, position, position.get(side), stamp(known_at) or end)
             for side in ('opening', 'closing')}
            for position in positions[:100] if isinstance(position, dict)]
    return result


def refresh_accounting(store, reviews):
    """Supplement a copied signed cloud display, without recomputing review facts."""
    for review in reviews:
        facts = (review.get('payload') or {}).get('facts') or {}
        display = review.setdefault('presentation', {})
        frozen = facts.get('portfolio') or {}
        for section, candidate in (('daily', facts.get('daily_accounting') or facts.get('daily_portfolio')),
                                   ('context', facts.get('context_48h'))):
            portfolio = display.get(section) or candidate
            if not portfolio and section == 'daily' and not facts.get('context_48h'):
                portfolio = frozen
            if not portfolio:
                continue
            # Compact delivery keeps position facts in the 48-hour portfolio.
            # Closing quotes are shared only when the cutoffs actually agree.
            positions = portfolio.get('positions')
            if positions is None and portfolio.get('window_end') == frozen.get('window_end'):
                positions = frozen.get('positions') or []
                # Different opening windows must not inherit one another's quotes.
                if portfolio.get('window_start') != frozen.get('window_start'):
                    positions = [{**p, 'opening': {}} for p in positions]
            updated = accounting(portfolio, store, review.get('ready_at'), positions)
            previous = {p.get('key'): p for p in portfolio.get('quote_provenance', [])}
            for position in updated.get('quote_provenance', []):
                old = previous.get(position.get('key'), {})
                for side in ('opening', 'closing'):
                    if position[side]['provenance_status'] == 'UNAVAILABLE' and old.get(side):
                        position[side] = old[side]
            if not updated.get('quote_provenance') and previous:
                updated['quote_provenance'] = list(previous.values())
            display[section] = updated
        display['version'] = 'review-display-v2'
    return reviews


def projection(store, review, payload, at):
    facts = payload.get('facts') or {}
    daily = facts.get('daily_accounting') or facts.get('daily_portfolio')
    if not daily and not facts.get('context_48h'):
        daily = facts.get('portfolio')
    return {'version': 'review-display-v2', 'checks': checks_summary(payload.get('consistency_checks')),
            'findings': findings(store, review, payload, at), 'daily': accounting(daily, store, review.get('ready_at')),
            'context': accounting(facts.get('context_48h'), store, review.get('ready_at')),
            'status_at': stamp(at), 'data_as_of': stamp(review.get('window_end'))}


def listing(store, config, at, *, overview=True):
    """Latest five review windows, without account, strategy or market computation."""
    import json
    from .calendar import review_window
    reviews=[]
    for r in store.db.execute('''SELECT r.* FROM reviews r WHERE NOT EXISTS(
        SELECT 1 FROM reviews newer WHERE newer.window_start=r.window_start AND newer.window_end=r.window_end
        AND newer.revision>r.revision) ORDER BY r.window_end DESC LIMIT 5'''):
        d=dict(r);d['payload']=json.loads(d.pop('payload_json'))
        retry_count=store.db.execute("SELECT count(*) FROM jobs WHERE kind='review' AND id LIKE ?",('review-retry:'+r['window_end']+':%',)).fetchone()[0]
        d['automatic_retries_remaining']=max(0,2-retry_count) if config['scheduler_enabled'] and config['model_enabled'] and r['window_end']==normalize_time(review_window(at,config['review_time'])[1].isoformat()) else 0
        display=projection(store,d,d['payload'],at)
        if overview:
            p=d['payload'];d['payload']={'facts':{'statistics':p['facts']['statistics']},'analysis':p['analysis'],
                'analysis_error':p.get('analysis_error')}
            if 'daily_portfolio' in p['facts']:
                d['payload']['facts']['daily_accounting']=display['daily']
            if 'context_48h' in p['facts']:
                d['payload']['facts']['context_48h']={k:p['facts']['context_48h'][k] for k in ('window_start','window_end','totals','positions','learning_notice')}
            if 'portfolio' in p['facts']:
                portfolio_facts=p['facts']['portfolio']
                d['payload']['facts']['portfolio']={k:portfolio_facts[k] for k in ('version','scope','positions','opening','closing','totals','valuation_notice','research')}
        d['payload']['consistency_checks']=display['checks']['items']
        d['payload']['routing']=[{'lesson':v['ordinal'],**{k:v.get(k) for k in ('to','id','status','updated_at')}} for v in display['findings']['items']]
        d['presentation']=display
        reviews.append(d)
    if reviews:
        total=store.db.execute('SELECT count(*) FROM (SELECT window_start,window_end FROM reviews GROUP BY window_start,window_end)').fetchone()[0]
        reviews[0]['history']={'total':total,'shown':len(reviews),'omitted':max(0,total-len(reviews))}
    return reviews
