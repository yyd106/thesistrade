"""One membership query for discovery, company research, portfolio and execution."""
import json
import re
from .storage import now, digest, normalize_time


def membership(store,config,at=None):
    at=normalize_time(at or now())
    if config.get('deployment_role')=='cloud':
        from .cloud_runtime import value
        signed=value(store,'research_membership')
        if signed:return signed['members']
    core={i['symbol']:i for i in config['watchlist']}
    if not config.get('industry_enabled'):
        return [{**i,'membership':'CORE','tier':'CORE','buy_eligible':True,'fingerprint':'legacy'} for i in core.values()]
    from .observation import all_items
    from .observation_pool import allocate,protected_assets
    pool=allocate(store,all_items(store,at),at,config);protected=protected_assets(store)
    selected={i['asset']:i for i in pool['items']+pool['archived_items'] if i.get('kind')=='STOCK'}
    result=[]
    for symbol in sorted(set(core)|set(selected)|protected):
        if not (re.fullmatch(r'(sh|sz)\d{6}',symbol) or symbol.startswith('US:')):continue
        item=selected.get(symbol,{})
        links=[l for l in item.get('links',[]) if l['status']=='TRACKING' and l.get('review_state')=='CURRENT' and (l.get('materiality') or {}).get('admitted')]
        tier='CORE' if symbol in core else item.get('pool_tier','ARCHIVED')
        eligible=symbol in core or tier in ('FOCUS','ACTIVE') and bool(links)
        member={'symbol':symbol,'name':core.get(symbol,item).get('name',symbol),'membership':'CORE' if symbol in core else 'DYNAMIC','tier':tier,'buy_eligible':bool(eligible),'protected':symbol in protected,
          'reason':'固定研究名单' if symbol in core else item.get('pool_reason','仅保留已有敞口管理'),
          'review_at':min([t for t in [item.get('review_due_at')]+[l.get('qualification_review_at') for l in links] if t],default=None),'hypothesis_ids':[l['event_id'] for l in links],
          'methods':sorted({l['method'] for l in item.get('links',[]) if l.get('method')}),
          'domains':sorted({l['theme'] for l in item.get('links',[]) if l.get('theme')})}
        member['fingerprint']=digest(json.dumps({k:member[k] for k in ('membership','tier','buy_eligible','hypothesis_ids')},sort_keys=True))
        result.append(member)
    return result


def company_targets(store,config,at=None):
    at=at or now()
    if not config.get('industry_enabled'):
        return config['watchlist']
    return [i for i in membership(store,config,at) if re.fullmatch(r'(sh|sz)\d{6}',i['symbol']) and (i['membership']=='CORE' or i['buy_eligible'] or i.get('protected'))]


def permit(store,config,symbol,at):
    if not config.get('industry_enabled'):return symbol in {w['symbol'] for w in config['watchlist']}
    m=next((m for m in membership(store,config,at) if m['symbol']==symbol),None)
    return bool(m and m['buy_eligible'] and (not m.get('review_at') or at<m['review_at'] or m['membership']=='CORE'))


def reconcile(store,config,at):
    members=membership(store,config,at)
    known={r['symbol']:json.loads(r['payload_json']) for r in store.db.execute('SELECT * FROM industry_memberships ORDER BY id')}
    current={m['symbol'] for m in members}
    removed=[]
    for symbol,prior in known.items():
        if symbol not in current:
            m={**prior,'membership':'DYNAMIC','tier':'ARCHIVED','buy_eligible':False,'protected':False,'hypothesis_ids':[],'reason':'固定名单人工调整或已无活跃依据；保留历史'}
            m['fingerprint']=digest(json.dumps({k:m[k] for k in ('membership','tier','buy_eligible','hypothesis_ids')},sort_keys=True));removed.append(m)
    with store.db:
        for m in members+removed:
            old=store.db.execute('SELECT fingerprint FROM industry_memberships WHERE symbol=? ORDER BY id DESC LIMIT 1',(m['symbol'],)).fetchone()
            if not old or old[0]!=m['fingerprint']:
                store.db.execute('INSERT INTO industry_memberships(symbol,at,membership,tier,buy_eligible,fingerprint,payload_json) VALUES(?,?,?,?,?,?,?)',(m['symbol'],at,m['membership'],m['tier'],int(m['buy_eligible']),m['fingerprint'],json.dumps(m,ensure_ascii=False)))
    return members


def publication(store,config,completed,until):
    members=[m for m in membership(store,config,completed) if m['membership']=='CORE' or m['buy_eligible'] or m.get('protected')]
    return {'version':'industry_v1','as_of':completed,'valid_until':until,'core':sorted(w['symbol'] for w in config['watchlist']),'members':members}


def validate_publication(store,config,body,items,completed,until):
    if not isinstance(body,dict) or body.get('version')!='industry_v1' or body.get('as_of')!=completed or body.get('valid_until')!=until:raise ValueError('名单版本或授权时间不匹配')
    if body.get('core')!=sorted(w['symbol'] for w in config['watchlist']):raise ValueError('固定名单配置不一致')
    rows=body.get('members')
    if not isinstance(rows,list) or len(rows)>100:raise ValueError('名单数量错误')
    members={m['symbol']:m for m in rows}
    if len(members)!=len(rows) or not set(body['core'])<=set(members):raise ValueError('名单重复或固定成员缺失')
    from .observation_pool import protected_assets,policy
    protected=protected_assets(store);core=set(body['core'])
    for symbol,m in members.items():
        if not re.fullmatch(r'(sh|sz)\d{6}|US:[A-Z][A-Z0-9.-]{0,9}',symbol):raise ValueError('名单证券身份错误')
        if m['membership']!=('CORE' if symbol in core else 'DYNAMIC') or type(m['buy_eligible']) is not bool:raise ValueError('名单归属或资格错误')
        if m.get('protected') and symbol not in protected:raise ValueError('保护对象与云端敞口不一致')
        if m['buy_eligible'] and symbol not in core and (m['tier'] not in ('FOCUS','ACTIVE') or not m.get('hypothesis_ids') or not m.get('review_at') or m['review_at']<=completed):raise ValueError('动态名单缺少有效研究依据')
        if not m['buy_eligible'] and symbol not in core and symbol not in protected:raise ValueError('无资格无敞口的对象不能进入执行名单')
        expected=digest(json.dumps({k:m[k] for k in ('membership','tier','buy_eligible','hypothesis_ids')},sort_keys=True))
        if m.get('fingerprint')!=expected:raise ValueError('名单摘要不一致')
    dynamic=set(members)-core
    capacity=min(policy(config)['active_limit'],40-len(core)-4)
    if len(dynamic-protected)>max(0,capacity-len(protected-core-{'GOLD','SILVER','BTC','ETH'})):raise ValueError('动态名单超出共享额度')
    for d in items:
        if d['symbol'] in ('GOLD','SILVER','BTC','ETH'):continue
        m=members.get(d['symbol'])
        if d['action']=='ALLOW' and (not m or not m['buy_eligible'] or d.get('membership_token')!=m['fingerprint']):raise ValueError('名单未授权新增买入')
        if d['route']=='watchlist' and not m:raise ValueError('缺少公司研究资格')
        if d['action']=='ALLOW' and d['route']=='dynamic' and re.fullmatch(r'(sh|sz)\d{6}',d['symbol']):raise ValueError('动态公司新买入须使用统一公司研究')
    return members
