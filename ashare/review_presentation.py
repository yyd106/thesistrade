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


def accounting(portfolio):
    if not isinstance(portfolio, dict):
        return None
    dividends = portfolio.get('dividends') or []
    valid = [d for d in dividends if isinstance(d, dict) and type(d.get('amount_cents')) is int]
    return {key: portfolio.get(key) for key in ('window_start', 'window_end', 'totals', 'opening', 'closing')} | {
        'dividend_cents': sum(d['amount_cents'] for d in valid), 'dividend_count': len(valid),
        'dividends_known': 'dividends' in portfolio,
        'valuation_notice': text(portfolio.get('valuation_notice'), 700)}


def projection(store, review, payload, at):
    facts = payload.get('facts') or {}
    daily = facts.get('daily_accounting') or facts.get('daily_portfolio')
    if not daily and not facts.get('context_48h'):
        daily = facts.get('portfolio')
    return {'version': 'review-display-v1', 'checks': checks_summary(payload.get('consistency_checks')),
            'findings': findings(store, review, payload, at), 'daily': accounting(daily),
            'context': accounting(facts.get('context_48h')),
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
