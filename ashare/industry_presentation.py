"""Compute current display state without rewriting signed permits or past research."""
from copy import deepcopy


def current(data, at, *, signed=None, research_lease=None, revoked=None, revoked_at=None):
    data=deepcopy(data or {})
    hypotheses=data.get('hypotheses',[])
    for h in hypotheses:
        if h.get('effective_state',h.get('state')) in ('ACTIVE','REVIEW'):
            if h.get('expires_at') and at>=h['expires_at']:h['effective_state']='ARCHIVED'
            elif h.get('review_at') and at>=h['review_at']:h['effective_state']='REVIEW'
    certificates={m['symbol']:m for m in (signed or {}).get('members',[])}
    for m in data.get('members',[]):
        related=[h for h in hypotheses if h['symbol']==m['symbol']]
        states={h.get('effective_state',h.get('state')) for h in related}
        expired=m.get('review_at') and at>=m['review_at'] and m['membership']!='CORE'
        if expired:
            m['buy_eligible']=False
            m['research_status']='ARCHIVED' if states and states<={'ARCHIVED','INVALIDATED','REALIZED'} else 'REVIEW'
            m['reason']='研究依据已到复核期限，更新前暂停新增买入'
        m.setdefault('research_status','TRACKING' if m.get('buy_eligible') else 'LEAD' if states and states<={'WAITING'} else 'REVIEW' if 'REVIEW' in states or m.get('tier')=='COOLING' else 'ARCHIVED')
        m['entry_allowed']=bool(m.get('buy_eligible'))
        m['entry_reason']='名单条件已满足；仍需完整公司研究、组合安排和价格条件通过'
        if not m['entry_allowed']:
            m['entry_reason']={'LEAD':'线索尚未证实，目前不能据此买入','REVIEW':'研究依据待复核，暂停新增买入','ARCHIVED':'已停止跟踪，不再新增买入'}.get(m['research_status'],'尚不满足新增买入条件')
        if signed is not None:
            cert=certificates.get(m['symbol'])
            if not cert or not cert.get('buy_eligible') or cert.get('fingerprint')!=m.get('fingerprint'):
                m['entry_allowed']=False
                if m.get('buy_eligible'):m['entry_reason']='最新名单尚未获交易端确认，暂不新增买入'
            elif cert.get('membership')!='CORE' and cert.get('review_at') and at>=cert['review_at']:
                m['entry_allowed']=False;m['entry_reason']='研究依据已到复核期限，暂停新增买入'
            if research_lease and not research_lease.get('active'):
                m['entry_allowed']=False;m['entry_reason']='最近一次组合研究已超过有效期，暂停新增买入'
            keys=(f"watchlist:{m['symbol']}",f"global:{m['symbol']}")
            if (revoked_at or '')>(signed or {}).get('as_of','') or any((revoked or {}).get(k,'')>(signed or {}).get('as_of','') for k in keys):
                m['entry_allowed']=False;m['entry_reason']='原买入安排已撤销，等待新的研究和组合决定'
        if m.get('protected'):m['entry_reason']+='；继续管理已有持仓和未完成委托'
    data['display_at']=at
    return data
