"""Cloud-owned accounting snapshots and local read replicas; never replay executions."""
import json
from .storage import digest,now
from .cloud_protocol import canonical
from .cloud_runtime import put,value

MUTABLE=('paper_accounts','paper_withdrawals','paper_orders','dynamic_orders','global_orders','paper_lots','dynamic_lots','global_lots','portfolio_risk','latest_trade_checks')
APPEND=('quotes','dynamic_quotes','global_quotes','slots','decisions','paper_order_terms','paper_fills','dynamic_fills','global_fills','paper_flows','equity_marks','portfolio_risk_events','portfolio_order_events')
SUPPORT=('snapshots','studies','plans','global_plans','dynamic_news','dynamic_cases')
BOOT=SUPPORT+APPEND+MUTABLE+('portfolio_runs','portfolio_decisions','reviews','lessons','research_methods','research_improvements','app_users')


def rows(store,table,where='',args=()):return [dict(r) for r in store.db.execute('SELECT * FROM '+table+' '+where,args)]


def upsert(store,table,records,*,immutable=False):
    columns=[r['name'] for r in store.db.execute('PRAGMA table_info('+table+')')]
    pk=[r['name'] for r in store.db.execute('PRAGMA table_info('+table+')') if r['pk']]
    if len(records)>150000:raise ValueError('同步记录数量超限')
    for record in records:
        if set(record)!=set(columns):raise ValueError('同步字段与数据库不匹配: '+table)
        keys=','.join('"'+c+'"' for c in columns)
        changes=','.join('"'+c+'"=excluded."'+c+'"' for c in columns if c not in pk)
        sql='INSERT INTO '+table+' ('+keys+') VALUES('+','.join('?' for _ in columns)+') ON CONFLICT DO '+('NOTHING' if immutable else 'UPDATE SET '+changes)
        store.db.execute(sql,[record[c] for c in columns])


def version(store):
    # Quotes do not invalidate the portfolio analysis. Fills, orders, cash and risk do.
    state={t:rows(store,t,'ORDER BY '+','.join('"'+r['name']+'"' for r in store.db.execute('PRAGMA table_info('+t+')') if r['pk'])) for t in MUTABLE if t not in ('latest_trade_checks','portfolio_risk')}
    from .portfolio_risk import state as risk_state
    state['risk_halted']=bool(risk_state(store)['halted'])
    return digest(canonical(state))


def projection(table,record):
    r=dict(record)
    if table=='snapshots':r.update(batch_id=None,packet_json='{"cloud_projection":true}')
    if table=='slots':r['input_json']='{"cloud_projection":true}'
    for k in ('raw_path','report_path'):
        if k in r:r[k]=''
    return r


def bootstrap_packet(store,config):
    tables={t:[projection(t,r) for r in rows(store,t)] for t in BOOT}
    # Only the order-associated quote history and the most recent 48h are necessary.
    return {'protocol':1,'kind':'bootstrap','tables':tables,'ledger_version':version(store),'created_at':now()}


def import_bootstrap(store,config,body,at):
    if value(store,'initialized',False):raise ValueError('云端账本已初始化，禁止再次覆盖')
    if set(body.get('tables',{}))!=set(BOOT):raise ValueError('迁移表清单不匹配')
    if any(store.db.execute('SELECT 1 FROM '+t+' LIMIT 1').fetchone() for t in ('paper_fills','dynamic_fills','global_fills','app_users')):raise ValueError('云端已有账户或交易，不能覆盖')
    store.db.execute('PRAGMA defer_foreign_keys=ON')
    for table in BOOT:
        upsert(store,table,body['tables'][table],immutable=table in ('snapshots','studies'))
    if version(store)!=body['ledger_version']:raise ValueError('迁移账本校验失败')
    put(store,'initialized',True);put(store,'execution_enabled',False);put(store,'migration_at',at)
    return {'status':'INITIALIZED','ledger_version':version(store),'cursors':{t:store.db.execute('SELECT coalesce(max(rowid),0) FROM '+t).fetchone()[0] for t in APPEND}}


def export_ledger(store,cursors,at,limit=1500):
    if not value(store,'initialized',False):raise ValueError('云端账本尚未初始化')
    result={};updated={};more=False
    for table in APPEND:
        cursor=cursors.get(table,0)
        if type(cursor) is not int or cursor<0:raise ValueError('账本游标错误')
        selected=list(store.db.execute('SELECT rowid AS sync_rowid,* FROM '+table+' WHERE rowid>? ORDER BY rowid LIMIT ?',(cursor,limit+1)))
        more=more or len(selected)>limit;selected=selected[:limit]
        updated[table]=selected[-1]['sync_rowid'] if selected else cursor
        result[table]=[projection(table,{k:r[k] for k in r.keys() if k!='sync_rowid'}) for r in selected]
    # Include all small dependencies: a referenced old order/fill may arrive on an earlier page.
    for t in ('paper_orders','dynamic_orders','global_orders','paper_fills','dynamic_fills','global_fills','decisions','slots','paper_order_terms'):
        result[t]=[projection(t,r) for r in rows(store,t)]
    refs={t:[projection(t,r) for r in rows(store,t)] for t in SUPPORT}
    # Fill quotes are required even before the quote-history page reaches them.
    for q,f in (('quotes','paper_fills'),('dynamic_quotes','dynamic_fills'),('global_quotes','global_fills')):
        existing={r['id'] for r in result[q]}
        result[q]+=[projection(q,r) for r in rows(store,q,'WHERE id IN (SELECT quote_id FROM '+f+')') if r['id'] not in existing]
        # Latest quotes let the local portfolio value holdings immediately.
        result[q]+=[projection(q,r) for r in rows(store,q,'WHERE rowid IN (SELECT max(rowid) FROM '+q+' GROUP BY symbol)') if r['id'] not in existing]
    return {'protocol':1,'at':at,'ledger_version':version(store),'tables':result,'mutable':{t:rows(store,t) for t in MUTABLE},'support':refs,'cursors':updated,'more':more}


def import_ledger(store,config,packet):
    if config.get('deployment_role')!='research':raise ValueError('账本只能导入本地研究副本')
    if set(packet['tables'])!=set(APPEND)|{'paper_orders','dynamic_orders','global_orders'} or set(packet['mutable'])!=set(MUTABLE) or set(packet['support'])!=set(SUPPORT):raise ValueError('远程账本表清单错误')
    store.db.execute('BEGIN IMMEDIATE');store.db.execute('PRAGMA defer_foreign_keys=ON')
    try:
        for t in SUPPORT:
            incoming=packet['support'][t]
            if t=='plans':
                incoming=[{**r,'status':'EXPIRED'} if json.loads(r['payload_json']).get('kind')=='RISK_EXIT_ONLY' else r for r in incoming]
            # Local research is authoritative; never overwrite its current plans.
            upsert(store,t,incoming,immutable=True)
        for t,records in packet['tables'].items():upsert(store,t,records,immutable=t not in ('paper_orders','dynamic_orders','global_orders','slots'))
        for t,records in packet['mutable'].items():upsert(store,t,records)
        put(store,'ledger_cursors',packet['cursors'])
        if not packet['more']:
            put(store,'remote_ledger_version',packet['ledger_version']);put(store,'remote_ledger_at',packet['at'])
        store.db.commit()
    except BaseException:store.db.rollback();raise
