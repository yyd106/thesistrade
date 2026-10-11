"""Authenticated, cloud-authoritative simulated profit withdrawals, in cents."""
import json
from . import approvals, finance, portfolio_risk
from .storage import digest, now, normalize_time

VERSION = 'simulated-withdrawal-v1'
ACCOUNT = 'DEMO_PAPER'
ACTIVE = ('READY', 'WAITING', 'PAUSED_THRESHOLD', 'REVIEW_REQUIRED')
MESSAGES = {
    'VALUATION_INCOMPLETE': '最新报价或汇率不足，等待完整估值',
    'BELOW_THRESHOLD': '资产未严格超过本档阈值，等待重新越线',
    'INSUFFICIENT_CASH': '现金不足以一次提取当前全部目标，等待既有交易释放现金',
    'UNKNOWN_ORDERS': '存在结果未知的委托，须先核对，预留保持',
    'ACCOUNTING_MISMATCH': '资金流水或周期对账不一致，须人工复核',
    'UNIT_NAV_UNAVAILABLE': '历史单位净值依据不足，须人工复核',
    'GROSS_LIMIT': '提取后总仓位超过现有上限',
    'SYMBOL_LIMIT': '提取后合并标的仓位超过现有上限',
    'GLOBAL_LIMIT': '提取后全球单标的仓位超过现有上限',
    'PLAN_LIMIT': '提取后持仓超过原研究计划的仓位上限',
    'PORTFOLIO_LIMIT': '提取后持仓超过有效组合目标或风险组预算',
    'PORTFOLIO_UNAVAILABLE': '组合额度依据缺失，等待有效组合决策',
    'REVIEW_LATCHED': '此前对账异常尚未解除，保留停买和预留',
    'CANCELLED': '本档未发生部分已取消，待回落后重新严格越线',
}


def _role(config):
    return config.get('deployment_role', 'standalone')


def _authority(config):
    if _role(config) not in ('cloud', 'standalone') or config.get('mode', 'paper') != 'paper':
        raise ValueError('模拟提取只能由云端模拟账本执行')


def _mirror(store):
    row = store.db.execute("SELECT value FROM cloud_state WHERE key='withdrawals'").fetchone()
    return json.loads(row[0]) if row else None


def _cycles(store):
    return [dict(r) for r in store.db.execute('SELECT * FROM withdrawal_cycles WHERE account_id=? ORDER BY stage', (ACCOUNT,))]


def _legacy(store):
    return [dict(r) for r in store.db.execute('SELECT * FROM paper_withdrawals WHERE account_id=? ORDER BY stage', (ACCOUNT,))]


def reservation(store):
    rows = _cycles(store)
    mirror = _mirror(store) if not rows else None
    if mirror:
        return max(0, int(mirror.get('reserved_cents', 0)))
    stages = {r['stage'] for r in rows}
    return sum(json.loads(r['payload_json']).get('reserved_cents', 0) for r in rows if r['status'] in ACTIVE) + sum(
        max(0, r['planned_cents']-r['transferred_cents']) for r in _legacy(store)
        if r['stage'] not in stages and r['status'] != 'COMPLETED')


def pending(store):
    rows = _cycles(store)
    mirror = _mirror(store) if not rows else None
    if mirror:
        return bool(mirror.get('pending', mirror.get('status') in ACTIVE))
    stages = {r['stage'] for r in rows}
    account = store.db.execute('SELECT * FROM paper_accounts WHERE id=?', (ACCOUNT,)).fetchone()
    return bool(account and not _audit(store, account)) or any(r['status'] in ACTIVE for r in rows) or any(
        r['stage'] not in stages and r['status'] != 'COMPLETED' for r in _legacy(store))


def _cycle(row):
    return {k: row[k] for k in ('id', 'stage', 'status', 'created_at', 'updated_at')} | json.loads(row['payload_json'])


def _event(store, identity, at, kind, data):
    store.db.execute('INSERT INTO withdrawal_events(cycle_id,at,kind,payload_json) VALUES(?,?,?,?)',
                     (identity, at, kind, approvals.canonical(data)))


def _save(store, row, state, data, at):
    previous = json.loads(row['payload_json'])
    data = {**data, 'revision': previous.get('revision', 0)}
    if state == row['status'] and data == previous:
        return row
    data['revision'] += 1
    store.db.execute('UPDATE withdrawal_cycles SET status=?,updated_at=?,payload_json=? WHERE id=?',
                     (state, at, approvals.canonical(data), row['id']))
    _event(store, row['id'], at, state, {k: data.get(k) for k in (
        'revision', 'equity_cents', 'candidate_cents', 'reserved_cents', 'transferred_cents', 'blockers')})
    return dict(store.db.execute('SELECT * FROM withdrawal_cycles WHERE id=?', (row['id'],)).fetchone())


def _account(store, at):
    from .paper import _account_without_initialize
    return _account_without_initialize(store, at)


def _audit(store, account):
    flows = {r['id']: dict(r) for r in store.db.execute(
        "SELECT * FROM paper_flows WHERE account_id=? AND kind='SIMULATED_WITHDRAWAL'", (ACCOUNT,))}
    total = -sum(f['amount_cents'] for f in flows.values())
    if total != account['withdrawn_cents'] or total < 0 or account['cash_cents'] < 0:
        return False
    known = set()
    legacy = {r['id']: r for r in _legacy(store)}
    for w in legacy.values():
        matched = {fid for fid, flow in flows.items() if fid == digest(w['id']+':'+flow['reference'])}
        if known & matched or -sum(flows[fid]['amount_cents'] for fid in matched) != w['transferred_cents']:
            return False
        known.update(matched)
    for row in _cycles(store):
        data = json.loads(row['payload_json'])
        transferred = data.get('legacy_transferred_cents', 0)
        if data.get('legacy_id') and (data['legacy_id'] not in legacy or legacy[data['legacy_id']]['transferred_cents'] != transferred):
            return False
        for t in store.db.execute('SELECT * FROM withdrawal_transfers WHERE cycle_id=?', (row['id'],)):
            flow = flows.get(t['flow_id'])
            if t['amount_cents'] <= 0 or t['flow_id'] in known or not flow or (flow['account_id'], flow['kind'], flow['amount_cents'], flow['reference']) != (
                    ACCOUNT, 'SIMULATED_WITHDRAWAL', -t['amount_cents'], 'withdrawal:'+t['request_id']):
                return False
            known.add(t['flow_id'])
            transferred += t['amount_cents']
        if transferred != data.get('transferred_cents', 0):
            return False
        if row['status'] == 'COMPLETED' and account['current_stage'] <= row['stage']:
            return False
        if row['status'] in ACTIVE and row['stage'] != account['current_stage']:
            return False
    return known == set(flows) and all(f['amount_cents'] < 0 for f in flows.values())


def _limits(store, config, at, account, retain):
    from . import portfolio_strategy as ps
    codes = []
    if account['market_value_cents']*100 > retain*min(90, config.get('paper_max_gross_pct', 90)):
        codes.append('GROSS_LIMIT')
    combined = {}
    for group in ('positions', 'dynamic_positions', 'global_positions'):
        for symbol, p in account[group].items():
            combined[symbol] = combined.get(symbol, 0)+p['market_value_cents']
    if any(v*100 > retain*min(20, config.get('paper_max_stock_pct', 20)) for v in combined.values()):
        codes.append('SYMBOL_LIMIT')
    if any(p['market_value_cents']*100 > retain*min(5, config.get('paper_max_stock_pct', 20)) for p in account['global_positions'].values()):
        codes.append('GLOBAL_LIMIT')
    sources = []
    for symbol, p in account['positions'].items():
        row = ps.source(store, 'watchlist', symbol)
        if row:
            sources.append(dict(row))
            if p['market_value_cents']*100 > retain*min(20, json.loads(row['payload_json']).get('max_stock_pct', 20)):
                codes.append('PLAN_LIMIT')
    for case_id, in store.db.execute('SELECT DISTINCT case_id FROM dynamic_lots WHERE qty>0'):
        row = ps.source(store, 'dynamic', case_id)
        if row:
            sources.append(dict(row))
            d = {'route': 'dynamic', 'identity': case_id, 'symbol': row['symbol']}
            if ps.position_value(store, account, d, at)*100 > retain*json.loads(row['plan_json']).get('max_position_pct', 5):
                codes.append('PLAN_LIMIT')
    current = ps.active(store, at) if ps.enabled(config) else None
    if ps.enabled(config) and account['market_value_cents'] and not current:
        codes.append('PORTFOLIO_UNAVAILABLE')
    if current:
        decisions = {d['key']: d for d in current['payload']['decisions']}
        held = [('watchlist', symbol) for symbol in account['positions']]
        held += [('global', symbol) for symbol in account['global_positions']]
        held += [('dynamic', row[0]) for row in store.db.execute('SELECT DISTINCT case_id FROM dynamic_lots WHERE qty>0')]
        if any(ps.decision(store, config, route, identity, at) is None for route, identity in held):
            codes.append('PORTFOLIO_UNAVAILABLE')
        values = {k: ps.position_value(store, account, d, at) for k, d in decisions.items()}
        if any(v*10000 > retain*decisions[k]['target_bps'] for k, v in values.items()):
            codes.append('PORTFOLIO_LIMIT')
        for group in current['payload']['risk_groups']:
            if sum(values.get(k, 0) for k in group['keys'])*10000 > retain*group['max_bps']:
                codes.append('PORTFOLIO_LIMIT')
    return sorted(set(codes)), {'portfolio': current, 'source_limits': sources}


def _inputs(store, config, at, account, retain):
    from .dynamic_sources import latest_quote as dynamic_quote
    from .global_market import latest as global_quote
    ready = portfolio_risk.valuation_ready(store, account, at)
    risk = portfolio_risk.state(store)
    codes, limits = _limits(store, config, at, account, retain)
    if not ready: codes.append('VALUATION_INCOMPLETE')
    if any(o['status'] == 'UNKNOWN' for o in account['orders']): codes.append('UNKNOWN_ORDERS')
    if not _audit(store, account): codes.append('ACCOUNTING_MISMATCH')
    if 'capital_units' not in risk or risk.get('historical_cashflow_gap'):
        codes.append('UNIT_NAV_UNAVAILABLE')
    quotes = {'watchlist': {s: store.latest_quote(s, at) for s in account['positions']},
              'dynamic': {s: dynamic_quote(store, s, at) for s in account['dynamic_positions']},
              'global': {s: global_quote(store, s, at) for s in account['global_positions']}}
    lots = {table: [dict(r) for r in store.db.execute('SELECT * FROM '+table+' WHERE qty>0 ORDER BY id')]
            for table in ('paper_lots', 'dynamic_lots', 'global_lots')}
    # Exclude only the risk poll timestamp and derived withdrawal reservation:
    # neither is a change to the financial state being approved.
    material = {'account': {k: v for k, v in account.items() if k not in (
                    'available_cents', 'withdrawal_reserved_cents', 'withdrawal_pending')},
                'quotes': quotes, 'lots': lots, 'risk': {k: v for k, v in risk.items() if k != 'checked_at'},
                'limits': limits, 'rules': {k: config.get(k) for k in (
                    'paper_max_stock_pct', 'paper_max_gross_pct', 'investment_policy', 'portfolio_strategy')}}
    material['account']['orders'] = sorted(account['orders'], key=lambda o: (o.get('origin', 'watchlist'), o['id']))
    return sorted(set(codes)), digest(approvals.canonical(material)), ready


def refresh_inside(store, config, at):
    _authority(config)
    if not store.db.in_transaction:
        raise RuntimeError('提取检查需要资金事务')
    at = normalize_time(at)
    if not store.db.execute('SELECT 1 FROM paper_accounts WHERE id=?', (ACCOUNT,)).fetchone():
        store.db.execute('INSERT INTO paper_accounts VALUES(?,?,?,1,0)', (ACCOUNT, 10000000, 10000000))
        store.db.execute('INSERT INTO paper_flows VALUES(?,?,?,?,?,?)',
                         (digest(ACCOUNT+':initial'), ACCOUNT, 'SIMULATED_INITIAL', 10000000, ACCOUNT+':initial', at))
    account = _account(store, at)
    portfolio_risk.evaluate_inside(store, config, at, account)
    account = _account(store, at)
    threshold, retain = finance.withdrawal_tier(account['current_stage'])
    row = store.db.execute('SELECT * FROM withdrawal_cycles WHERE account_id=? AND stage=?', (ACCOUNT, account['current_stage'])).fetchone()
    legacy = store.db.execute("SELECT * FROM paper_withdrawals WHERE account_id=? AND stage=? AND status!='COMPLETED'", (ACCOUNT, account['current_stage'])).fetchone()
    ready = portfolio_risk.valuation_ready(store, account, at)
    if not row and (legacy or (ready and account['equity_cents'] > threshold)):
        identity = 'WC-'+digest(ACCOUNT+':'+str(account['current_stage']))[:24]
        data = {'version': VERSION, 'revision': 0, 'threshold_cents': threshold, 'retain_cents': retain,
                'trigger_equity_cents': account['equity_cents'], 'transferred_cents': legacy['transferred_cents'] if legacy else 0,
                'legacy_transferred_cents': legacy['transferred_cents'] if legacy else 0,
                'legacy_id': legacy['id'] if legacy else None, 'reserved_cents': 0}
        store.db.execute('INSERT INTO withdrawal_cycles VALUES(?,?,?,?,?,?,?)',
                         (identity, ACCOUNT, account['current_stage'], 'WAITING', at, at, approvals.canonical(data)))
        _event(store, identity, at, 'TRIGGERED', {'equity_cents': account['equity_cents'], 'stage': account['current_stage']})
        row = store.db.execute('SELECT * FROM withdrawal_cycles WHERE id=?', (identity,)).fetchone()
    if row:
        row = dict(row); data = json.loads(row['payload_json'])
        if row['status'] == 'CANCELLED':
            if ready and account['equity_cents'] <= threshold:
                data['rearm_below'] = True
                _save(store, row, 'CANCELLED', data, at)
                return status(store, config, at)
            if not (ready and data.get('rearm_below') and account['equity_cents'] > threshold):
                return status(store, config, at)
            data.pop('rearm_below', None)
        if row['status'] != 'COMPLETED':
            for table in ('paper_orders', 'dynamic_orders', 'global_orders'):
                store.db.execute("UPDATE "+table+" SET status='CANCELLED',reserved_cents=0 WHERE side='BUY' AND status IN ('OPEN','PARTIAL')")
            account = _account(store, at)
            codes, fingerprint, ready = _inputs(store, config, at, account, retain)
            remaining = max(0, account['equity_cents']-retain)
            if account['equity_cents'] <= threshold: codes.append('BELOW_THRESHOLD')
            if account['cash_cents']-account['reserved_cents'] < remaining: codes.append('INSUFFICIENT_CASH')
            if data.get('review_latched'): codes.append('REVIEW_LATCHED')
            review = any(c in codes for c in ('UNKNOWN_ORDERS', 'ACCOUNTING_MISMATCH', 'UNIT_NAV_UNAVAILABLE', 'REVIEW_LATCHED'))
            state = 'REVIEW_REQUIRED' if review else 'PAUSED_THRESHOLD' if 'BELOW_THRESHOLD' in codes else 'WAITING' if codes else 'READY'
            data.update(equity_cents=account['equity_cents'], cash_cents=account['cash_cents'], candidate_cents=remaining,
                        reserved_cents=max(data.get('reserved_cents', 0), remaining) if review or not ready else remaining,
                        state_hash=fingerprint, blockers=sorted(set(codes)))
            _save(store, row, state, data, at)
    return status(store, config, at)


def refresh(store, config, at=None):
    with approvals.atomic(store):
        return refresh_inside(store, config, normalize_time(at or now()))


def status(store, config, at=None):
    at = normalize_time(at or now())
    if _role(config) == 'research':
        mirrored = _mirror(store)
        if mirrored:
            from .cloud_runtime import value
            if value(store, 'extras_error_withdrawals'):
                return {**mirrored, 'can_transfer': False, 'can_cancel': False,
                        'blockers': [*(mirrored.get('blockers') or []),
                                     {'code': 'MIRROR_SYNC_ERROR', 'message': '模拟提取摘要同步失败，以下金额为上次同步结果；最新状态请查看云端工作台。'}]}
            return mirrored
        return {'version': VERSION, 'authority': 'research', 'account_id': ACCOUNT,
                'status': 'NOT_SYNCED', 'current_stage': None, 'threshold_cents': None, 'retain_cents': None,
                'equity_cents': None, 'cash_cents': None, 'candidate_cents': None, 'withdrawn_cents': None,
                'reserved_cents': None, 'pending': pending(store), 'cycle': None, 'history': [], 'legacy': [],
                'can_transfer': False, 'can_cancel': False,
                'blockers': [{'code': 'NOT_SYNCED', 'message': '尚未收到云端模拟提取摘要，等待同步'}]}
    account = _account(store, at) if store.db.execute('SELECT 1 FROM paper_accounts WHERE id=?', (ACCOUNT,)).fetchone() else None
    stage = account['current_stage'] if account else 1
    threshold, retain = finance.withdrawal_tier(stage)
    rows = _cycles(store)
    row = next((r for r in rows if r['stage'] == stage), None)
    current = _cycle(row) if row else None
    state = current['status'] if current else 'IDLE'
    codes = current.get('blockers', []) if current else []
    if account and not _audit(store, account):
        state = 'REVIEW_REQUIRED'
        codes = sorted(set(codes) | {'ACCOUNTING_MISMATCH'})
    if state == 'CANCELLED': codes = ['CANCELLED']
    history = [{k: c.get(k) for k in ('id', 'stage', 'status', 'created_at', 'updated_at', 'transferred_cents', 'reserved_cents', 'revision')}
               for c in [_cycle(r) for r in rows[-20:]]]
    return {'version': VERSION, 'authority': _role(config), 'account_id': ACCOUNT, 'current_stage': stage,
            'threshold_cents': threshold, 'retain_cents': retain, 'equity_cents': account['equity_cents'] if account else None,
            'cash_cents': account['cash_cents'] if account else None, 'candidate_cents': max(0, account['equity_cents']-retain) if account else 0,
            'withdrawn_cents': account['withdrawn_cents'] if account else 0, 'reserved_cents': reservation(store), 'pending': pending(store),
            'status': state, 'blockers': [{'code': c, 'message': MESSAGES[c]} for c in codes], 'cycle': current,
            'can_transfer': state == 'READY', 'can_cancel': state in ('READY', 'WAITING', 'PAUSED_THRESHOLD'),
            'history': history, 'legacy': _legacy(store)[-20:]}


def _preview(view, action):
    c = view['cycle']
    return {'version': VERSION, 'cycle_id': c['id'], 'cycle_revision': c['revision'], 'action': action,
            'stage': c['stage'], 'threshold_cents': view['threshold_cents'], 'retain_cents': view['retain_cents'],
            'equity_cents': view['equity_cents'], 'cash_cents': view['cash_cents'],
            'amount_cents': view['candidate_cents'] if action == 'TRANSFER' else 0,
            'post_equity_cents': view['retain_cents'] if action == 'TRANSFER' else view['equity_cents'],
            'reserved_cents': view['reserved_cents'], 'release_cents': view['reserved_cents'] if action == 'CANCEL' else 0,
            'state_hash': c['state_hash']}


def request(store, config, at=None, action='TRANSFER'):
    _authority(config)
    if action not in ('TRANSFER', 'CANCEL'): raise ValueError('无效模拟提取动作')
    at = normalize_time(at or now())
    with approvals.atomic(store):
        view = refresh_inside(store, config, at)
        if not view['can_transfer' if action == 'TRANSFER' else 'can_cancel']:
            return {'status': 'BLOCKED', 'approval': None, 'preview': None, 'withdrawal': view}
        preview = _preview(view, action)
        approval = approvals.create_request(store, kind='WITHDRAWAL', subject_id=view['cycle']['id'], action=action,
            snapshot=preview, summary={'title': '确认模拟利润提取' if action == 'TRANSFER' else '取消本档未完成模拟提取',
                'stage': preview['stage'], 'amount_cents': preview['amount_cents'], 'retain_cents': preview['retain_cents']}, at=at, notify=False)
        store.db.execute('INSERT OR IGNORE INTO withdrawal_plans VALUES(?,?,?,?,?,?)',
                         (approval['id'], preview['cycle_id'], action, at, digest(approvals.canonical(preview)), approvals.canonical(preview)))
        return {'status': 'PREVIEW_READY', 'approval': approval, 'preview': preview, 'withdrawal': view}


def execute(store, config, request_id, user, expected_hash, at=None):
    _authority(config)
    at = normalize_time(at or now())
    with approvals.atomic(store):
        envelope = approvals.envelope(store, request_id)
        if envelope['kind'] != 'WITHDRAWAL': raise ValueError('审批不是模拟提取操作')
        approvals.require_admin_session(store, user, at=at)
        if expected_hash != envelope['hash']:
            raise ValueError('页面审批内容已变化，请重新核对')
        used = store.db.execute('SELECT result_json FROM approval_consumptions WHERE request_id=?', (request_id,)).fetchone()
        if used: return json.loads(used[0])
        if not envelope['created_at'] <= at < envelope['expires_at']:
            return {'status': 'STALE', 'request_id': request_id, 'cycle_id': envelope['subject_id'],
                    'amount_cents': 0, 'withdrawal': status(store, config, at)}
        plan = store.db.execute('SELECT * FROM withdrawal_plans WHERE request_id=?', (request_id,)).fetchone()
        if not plan: raise ValueError('缺少云端冻结提取计划')
        preview = json.loads(plan['payload_json'])
        if digest(approvals.canonical(preview)) != plan['snapshot_hash'] or preview != envelope['snapshot']:
            raise ValueError('冻结提取计划不一致')
        view = refresh_inside(store, config, at)
        result = {'request_id': request_id, 'cycle_id': plan['cycle_id'], 'amount_cents': 0}
        if not view['cycle'] or view['cycle']['id'] != plan['cycle_id'] or _preview(view, plan['action']) != preview:
            return {**result, 'status': 'STALE', 'withdrawal': view}
        if not view['can_transfer' if plan['action'] == 'TRANSFER' else 'can_cancel']:
            return {**result, 'status': 'BLOCKED', 'withdrawal': view}
        approvals.approve(store, request_id, expected_hash, user, authority='CLOUD_ADMIN', at=at)
        approvals.check_receipt(store, request_id, kind='WITHDRAWAL', subject_id=plan['cycle_id'], action=plan['action'], snapshot=preview, at=at)
        row = dict(store.db.execute('SELECT * FROM withdrawal_cycles WHERE id=?', (plan['cycle_id'],)).fetchone())
        data = json.loads(row['payload_json'])
        if plan['action'] == 'CANCEL':
            data.update(reserved_cents=0, cancelled_at=at, rearm_below=view['equity_cents'] <= view['threshold_cents'], blockers=[])
            _save(store, row, 'CANCELLED', data, at)
            result['status'] = 'CANCELLED'
        else:
            store.db.execute('SAVEPOINT withdrawal_transfer')
            try:
                transfer = finance.transfer_inside(store, at, preview['amount_cents'], 'withdrawal:'+request_id)
                if transfer['after_equity_cents'] != preview['retain_cents']:
                    raise ValueError('留存目标与提取后资产不一致')
                store.db.execute('INSERT INTO withdrawal_transfers VALUES(?,?,?,?,?,?,?)',
                    ('WT-'+digest(request_id)[:24], row['id'], request_id, preview['amount_cents'], transfer['flow_id'], at, approvals.canonical(transfer)))
                data.update(transferred_cents=data['transferred_cents']+preview['amount_cents'], reserved_cents=0, completed_at=at, blockers=[])
                _save(store, row, 'COMPLETED', data, at)
                changed = store.db.execute('UPDATE paper_accounts SET current_stage=current_stage+1 WHERE id=? AND current_stage=?',
                                           (ACCOUNT, row['stage'])).rowcount
                if changed != 1 or not _audit(store, _account(store, at)):
                    raise ValueError('模拟提取周期对账不一致')
                store.db.execute('RELEASE withdrawal_transfer')
                result.update(status='COMPLETED', **transfer)
            except (ValueError, ArithmeticError):
                store.db.execute('ROLLBACK TO withdrawal_transfer')
                store.db.execute('RELEASE withdrawal_transfer')
                data = json.loads(row['payload_json'])
                data.update(review_latched=True, blockers=['ACCOUNTING_MISMATCH'])
                _save(store, row, 'REVIEW_REQUIRED', data, at)
                return {**result, 'status': 'REVIEW_REQUIRED', 'withdrawal': status(store, config, at)}
        result['withdrawal'] = status(store, config, at)
        approvals.consume(store, request_id, result, at=at)
        return result
