"""Authenticated synthetic administrators for governance tests, never production credentials."""
from ashare import approvals, auth
from ashare.storage import now


def admin_session(store, at=None):
    stamp = at or now()
    if auth.setup_required(store):
        auth.setup(store, 'synthetic-admin-password', 'synthetic-guest-password')
    token, _ = auth.login(store, 'admin', 'synthetic-admin-password', 'synthetic-test', at=stamp)
    return auth.session(store, auth.COOKIE + '=' + token, at=stamp)


def grant(store, request, at=None):
    stamp = at or request['created_at']
    return approvals.approve(store, request['id'], expected_hash=request['hash'],
                             user=admin_session(store, stamp), at=stamp)


def decide(store, identity, status, *, note='合成测试确认', at=None, **kwargs):
    from ashare import governance
    stamp = at or now()
    kwargs.pop('decided_by', None)
    row = store.db.execute('SELECT * FROM strategy_proposals WHERE id=?', (identity,)).fetchone()
    approval_id = None
    if governance.needs_confirmation(row, status):
        request = governance.request_decision(store, identity, status, note=note, at=stamp, **kwargs)
        grant(store, request, stamp)
        approval_id = request['id']
    return governance.decide(store, identity, status, decided_by=None, note=note, at=stamp,
                             approval_id=approval_id, **kwargs)
