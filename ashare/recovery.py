"""Bounded per-stock recovery planning. No permission or trading gate is overridden."""
from .universe import company_targets
import json
import re
from datetime import datetime
from .calendar import local


def recovery_need(store,config,symbol,at):
    last=store.db.execute('SELECT model_status FROM studies WHERE symbol=? ORDER BY created_at DESC,rowid DESC LIMIT 1',(symbol,)).fetchone()
    if last and last[0]=='DEFERRED':
        failure=store.db.execute("SELECT detail FROM data_attempts WHERE symbol=? AND source='research_analysis' ORDER BY id DESC LIMIT 1",(symbol,)).fetchone()
        if failure and re.search(r'登录|认证|额度|quota|rate.?limit|usage.?limit|unauthorized',failure[0],re.I):
            return {'action':None,'why':'需先恢复订阅登录或等待额度恢复，自动重试已暂停'}
    plan=store.db.execute("SELECT * FROM plans WHERE symbol=? AND status='ACTIVE' ORDER BY activated_at DESC LIMIT 1",(symbol,)).fetchone()
    if not plan:
        return {'action':'collect' if not last else 'research','why':'缺少有效研究'}
    payload=json.loads(plan['payload_json']);codes=payload.get('blockers',[])
    if any(c.startswith(('SOURCE_GAP:','FINANCIAL_BASELINE_INCOMPLETE','MARKET_CONTEXT_INCOMPLETE','STALE_DAILY_BARS','UNADJUSTED_SERIES_MISSING','NO_COLLECTION_COVERAGE')) for c in codes):
        return {'action':'collect','why':'关键行情或财务底稿需要补取'}
    missing=False;unread=False
    for code in codes:
        if code.startswith('UNREAD_DOCUMENT:'):
            unread=True;did=code.partition(':')[2]
            doc=store.db.execute('SELECT url FROM documents WHERE id=?',(did,)).fetchone()
            body=store.db.execute("SELECT 1 FROM documents WHERE symbol=? AND url=? AND cloud_allowed=1 AND kind NOT IN ('announcement_metadata','news_brief','news_index')",(symbol,doc[0])).fetchone() if doc else None
            missing=missing or not body
    if unread:return {'action':'collect' if missing else 'research','why':'补取关键正文' if missing else '继续读取关键章节'}
    from .event_review import RULE_VERSION
    reviews=payload.get('event_reviews',[])
    for code in codes:
        if code.startswith(('CORPORATE_ACTION_UNVERIFIED:','UNRESOLVED_EVENT:')):
            did=code.partition(':')[2]
            if not any(r['doc_id']==did and r.get('rule_version')==RULE_VERSION for r in reviews):
                return {'action':'research','why':'按当前证据规则核验已有事项'}
    from .slots import unreviewed_events
    if unreviewed_events(store,dict(plan),at,config):return {'action':'collect','why':'有新的重要资料，需补取正文并重新研究'}
    if last and last[0]=='DEFERRED':return {'action':'research','why':'上次研究未完成'}
    if plan['valid_until']<=at:return {'action':'collect','why':'旧研究已过期，需更新资料和计划'}
    return {'action':None,'why':'等待价格变化、事件核验或新的实质证据；不重复消耗研究额度'}


def recovery_status(store,config,symbol,at):
    need=recovery_need(store,config,symbol,at)
    rows=list(store.db.execute("SELECT j.id,j.status,j.scheduled_at,j.finished_at FROM jobs j JOIN job_inputs i ON i.job_id=j.id WHERE j.kind='repair' AND json_extract(i.payload_json,'$.symbol')=? ORDER BY j.scheduled_at DESC",(symbol,)))
    day=local(at).date();auto=[r for r in rows if r['id'].startswith('repair:') and local(r['scheduled_at']).date()==day]
    active=next((r for r in rows if r['status'] in ('PENDING','RUNNING')),None)
    exhausted=len(auto)>=config['recovery_daily_limit']
    state='RUNNING' if active else 'NEEDS_INPUT' if not need['action'] else 'LIMIT_REACHED' if exhausted else 'PENDING'
    return {**need,'state':state,'automatic_attempts':len(auto),'automatic_limit':config['recovery_daily_limit'],
        'job_id':active['id'] if active else None,'automatic_enabled':config['scheduler_enabled'],
        'research_attempts':config['research_attempts'],
        'next_step':('正在处理，结果会更新此股票的研究和失败项。' if active else
           '今天的额外补齐次数已用完，已转入首页“待办与卡点”的后续处理方案；定时研究继续，未解决项跨日保留。' if exhausted and need['action'] else
           '等待后台检查，或点击本股票的“补齐资料并重研”。' if need['action'] and config['scheduler_enabled'] else
           '自动运行已暂停，可手动补齐资料并重研。' if need['action'] else need['why'])}


def enqueue_recovery(store,config,at):
    from .scheduler import enqueue
    from .calendar import trading_day
    if not config['scheduler_enabled'] or trading_day(local(at).date()) is not True or not 8<=local(at).hour<22:return
    if store.db.execute("SELECT 1 FROM jobs WHERE kind IN ('cycle','collect','research','repair') AND status IN ('PENDING','RUNNING') LIMIT 1").fetchone():return
    row=store.db.execute("SELECT value FROM service_state WHERE key='last_recovery'").fetchone()
    if row and (datetime.fromisoformat(at)-datetime.fromisoformat(row[0])).total_seconds()<config['recovery_interval_seconds']:return
    states=[(item['symbol'],recovery_status(store,config,item['symbol'],at)) for item in company_targets(store,config)]
    for sym,state in sorted(states,key=lambda pair:pair[1]['automatic_attempts']):
        if state['state']!='PENDING':continue
        # Persisted daily identity prevents restart from resetting the retry budget.
        key='repair:'+local(at).date().isoformat()+':'+sym+':'+str(state['automatic_attempts']+1)
        enqueue(store,'repair',at,key,payload={'symbol':sym})
        with store.db:store.db.execute("INSERT OR REPLACE INTO service_state VALUES('last_recovery',?)",(at,))
        break
