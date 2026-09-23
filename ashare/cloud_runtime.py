"""Deployment ownership, research lease and deterministic execution fencing."""
import json
from datetime import datetime,timedelta
from .cloud_protocol import role
from .storage import normalize_time


def value(store,key,default=None):
    r=store.db.execute('SELECT value FROM cloud_state WHERE key=?',(key,)).fetchone()
    return json.loads(r[0]) if r else default


def put(store,key,data):store.db.execute('INSERT INTO cloud_state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(data,ensure_ascii=False,sort_keys=True)))


def contract(store,key):
    r=store.db.execute('SELECT payload_json FROM cloud_contracts WHERE key=?',(key,)).fetchone()
    return json.loads(r[0]) if r else None


def lease(store,config,at):
    if role(config)!='cloud':return {'required':False,'active':True}
    completed=value(store,'research_completed_at');received=value(store,'research_received_at')
    initialized=value(store,'initialized',False);activated=value(store,'execution_enabled',False)
    age=(datetime.fromisoformat(normalize_time(at))-datetime.fromisoformat(completed)).total_seconds() if completed else None
    active=bool(initialized and activated and age is not None and 0<=age<43200)
    return {'required':True,'active':active,'initialized':initialized,'execution_enabled':activated,'completed_at':completed,'received_at':received,
            'expires_at':normalize_time((datetime.fromisoformat(completed)+timedelta(hours=12)).isoformat()) if completed else None,
            'state':'ACTIVE' if active else 'AWAITING_ACTIVATION' if not activated else 'RESEARCH_STALE'}


def execution_allowed(store,config,at,side=None):
    if role(config)=='research':raise ValueError('LOCAL_RESEARCH_ONLY: 模拟交易由云端执行')
    if role(config)=='cloud':
        status=lease(store,config,at)
        if not status['execution_enabled']:raise ValueError('云端交易尚未启用')
        if not status['active'] and (side=='BUY' or config.get('cloud_stale_policy','reduce_only')=='stop_all'):
            raise ValueError('RESEARCH_HEARTBEAT_EXPIRED: 研究心跳超过12小时，暂停交易')


def fence(store,config,at):
    if role(config)!='cloud':return 0
    status=lease(store,config,at)
    if status['active']:return 0
    count=0
    with store.db:
        for t in ('paper_orders','dynamic_orders','global_orders'):
            # UNKNOWN orders remain reserved until reconciled; never guess their outcome.
            count+=store.db.execute("UPDATE "+t+" SET status='CANCELLED',reserved_cents=0 WHERE status IN ('OPEN','PARTIAL') AND side='BUY'").rowcount
    return count


def assert_command(config,command):
    r=role(config)
    execution={'slot','settle','dynamic_slot','global_slot'}
    if r=='research' and command in execution:raise ValueError('本地仅研究，禁止执行模拟交易')
    if r=='cloud' and command not in execution:raise ValueError('云端仅执行，策略只能由签名研究端上传')
