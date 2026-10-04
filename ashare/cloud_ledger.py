"""Cloud-owned accounting snapshots and local read replicas; never replay executions."""
import json
from .storage import digest,now
from .cloud_protocol import canonical
from .cloud_runtime import put,value

MUTABLE=('paper_accounts','paper_withdrawals','paper_orders','dynamic_orders','global_orders','paper_lots','dynamic_lots','global_lots','portfolio_risk','latest_trade_checks')
APPEND=('quotes','dynamic_quotes','global_quotes','slots','decisions','paper_order_terms','paper_fills','dynamic_fills','global_fills','paper_flows','equity_marks','portfolio_risk_events','portfolio_order_events')
SUPPORT=('snapshots','studies','plans','global_plans','dynamic_news','dynamic_cases')
BOOT=SUPPORT+APPEND+MUTABLE+('portfolio_runs','portfolio_decisions','reviews','lessons','research_methods','research_improvements','app_users')
# Advertised by the cloud so a newer research node only uses what the deployed cloud understands.
FEATURES=('ledger_v2','targeted_invalidation','display_delta','industry_lists_v1','cash_dividend_credit','quote_health','notices','supervision_summary','review_news_display_v1','review_news_display_v2')
PRIMARY={'latest_trade_checks':'symbol'}
# Appended rows the cloud later updates in place; their updates travel through the change log.
UPDATED_APPEND=('slots',)
# Foreign keys a replica needs inside one import transaction: (table, column) -> referenced table.
REFERENCES={('paper_lots','id'):'paper_fills',('dynamic_lots','id'):'dynamic_fills',('global_lots','id'):'global_fills',
    ('paper_fills','order_id'):'paper_orders',('paper_fills','quote_id'):'quotes',
    ('dynamic_fills','order_id'):'dynamic_orders',('dynamic_fills','quote_id'):'dynamic_quotes',
    ('global_fills','order_id'):'global_orders',('global_fills','quote_id'):'global_quotes',
    ('paper_orders','decision_id'):'decisions',('paper_orders','plan_id'):'plans',
    ('decisions','slot_id'):'slots',('decisions','plan_id'):'plans',('paper_order_terms','order_id'):'paper_orders'}


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


def ensure_change_log(store):
    """Cloud-side log of mutable-row changes, so replicas pull only what changed since their cursor."""
    store.db.execute("CREATE TABLE IF NOT EXISTS ledger_changes(id INTEGER PRIMARY KEY AUTOINCREMENT,tbl TEXT NOT NULL,pk TEXT NOT NULL,changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00','now')))")
    store.db.execute('CREATE INDEX IF NOT EXISTS ledger_changes_time ON ledger_changes(changed_at)')
    for t in MUTABLE:
        pk=PRIMARY.get(t,'id')
        for event in ('INSERT','UPDATE'):
            store.db.execute(f"CREATE TRIGGER IF NOT EXISTS ledger_change_{t}_{event.lower()} AFTER {event} ON {t} BEGIN INSERT INTO ledger_changes(tbl,pk) VALUES('{t}',NEW.{pk}); END")
    for t in UPDATED_APPEND:
        store.db.execute(f"CREATE TRIGGER IF NOT EXISTS ledger_change_{t}_update AFTER UPDATE ON {t} BEGIN INSERT INTO ledger_changes(tbl,pk) VALUES('{t}',NEW.id); END")


def _row(store,table,key):
    r=store.db.execute('SELECT * FROM '+table+' WHERE "'+PRIMARY.get(table,'id')+'"=?',(key,)).fetchone()
    return dict(r) if r else None


def _close(store,tables,mutable,support):
    """Add every row a replica needs to satisfy foreign keys for the rows already in this response."""
    def bucket(t):
        if t in MUTABLE:return mutable.setdefault(t,[])
        if t in SUPPORT:return support.setdefault(t,[])
        return tables.setdefault(t,[])
    present={}
    def ids(t):
        if t not in present:present[t]={r[PRIMARY.get(t,'id')] for r in bucket(t)}
        return present[t]
    changed=True
    while changed:
        changed=False
        for (table,column),ref in REFERENCES.items():
            for r in list(bucket(table)) if (table in tables or table in mutable or table in support) else []:
                key=r.get(column)
                if not key or key in ids(ref):continue
                row=_row(store,ref,key)
                if row:
                    bucket(ref).append(projection(ref,row));ids(ref).add(key);changed=True


def export_ledger_v2(store,body,at,limit=1500):
    """Incremental replica feed: appended rows by rowid cursor, mutable rows by change-log cursor,
    plus only the referenced rows needed for foreign keys. Size no longer grows with history."""
    if not value(store,'initialized',False):raise ValueError('云端账本尚未初始化')
    ensure_change_log(store)
    cursors=body.get('cursors',{});tables={};updated={};more=False
    for table in APPEND:
        cursor=cursors.get(table,0)
        if type(cursor) is not int or cursor<0:raise ValueError('账本游标错误')
        selected=list(store.db.execute('SELECT rowid AS sync_rowid,* FROM '+table+' WHERE rowid>? ORDER BY rowid LIMIT ?',(cursor,limit+1)))
        more=more or len(selected)>limit;selected=selected[:limit]
        updated[table]=selected[-1]['sync_rowid'] if selected else cursor
        tables[table]=[projection(table,{k:r[k] for k in r.keys() if k!='sync_rowid'}) for r in selected]
    high,low=store.db.execute('SELECT coalesce(max(id),0),coalesce(min(id),0) FROM ledger_changes').fetchone()
    cursor=body.get('change_cursor')
    full=type(cursor) is not int or cursor<0 or cursor>high or bool(low and cursor<low-1)
    mutable={t:[] for t in MUTABLE};changes={t:[] for t in UPDATED_APPEND}
    if full:
        mutable={t:rows(store,t) for t in MUTABLE}
        # Rows updated before the change log existed: recent slots are the only appended rows that change.
        changes['slots']=[projection('slots',r) for r in rows(store,'slots','WHERE started_at>=?',(at[:10],))]
    else:
        for tbl,pk in store.db.execute('SELECT DISTINCT tbl,pk FROM ledger_changes WHERE id>? ORDER BY tbl,pk',(cursor,)).fetchall():
            row=_row(store,tbl,pk) if tbl in MUTABLE or tbl in UPDATED_APPEND else None
            if row is None:continue
            (mutable if tbl in MUTABLE else changes)[tbl].append(row if tbl in MUTABLE else projection(tbl,row))
    support={t:[] for t in SUPPORT}
    # Cloud-created protective plans are the only research records a replica cannot already hold.
    support['plans']=[projection('plans',r) for r in rows(store,'plans',"WHERE json_extract(payload_json,'$.kind')='RISK_EXIT_ONLY' AND activated_at>=?",(body.get('support_since') or '0000',))]
    _close(store,tables,mutable,support)
    for q,f in (('quotes','paper_fills'),('dynamic_quotes','dynamic_fills'),('global_quotes','global_fills')):
        # Latest quotes let the replica value holdings immediately.
        existing={r['id'] for r in tables[q]}
        tables[q]+=[projection(q,r) for r in rows(store,q,'WHERE rowid IN (SELECT max(rowid) FROM '+q+' GROUP BY symbol)') if r['id'] not in existing]
    packet={'protocol':2,'at':at,'ledger_version':version(store),'tables':tables,'updated':changes,'mutable':mutable,'support':support,
            'cursors':updated,'change_cursor':high,'full_mutable':full,'support_since':at,'more':more,'features':list(FEATURES)}
    extras=body.get('extras')
    if isinstance(extras,dict) and not more:
        # Only asked for by 0.15.6+ research nodes, and only on the last page; older nodes never see the key.
        from .quote_health import changed_since
        from .notices import for_replica
        packet['extras']={}
        for key,build in (('quote_health',lambda:changed_since(store,extras['quote_health'],at)),
                          ('notices',lambda:for_replica(store,extras['notices']))):
            if key not in extras:continue
            try:packet['extras'][key]=build()
            except Exception as exc:
                # A bad cursor costs only this extra; the ledger itself is always answered.
                packet['extras'][key+'_error']=f'{type(exc).__name__}: {str(exc)[:200]}'
    return packet


def export_ledger(store,cursors,at,limit=1500,body=None):
    if body and body.get('protocol')==2:return export_ledger_v2(store,body,at,limit)
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
    return {'protocol':1,'at':at,'ledger_version':version(store),'tables':result,'mutable':{t:rows(store,t) for t in MUTABLE},'support':refs,'cursors':updated,'more':more,'features':list(FEATURES)}


def import_ledger_v2(store,config,packet):
    if not set(packet['tables'])<=set(APPEND) or not set(packet['mutable'])<=set(MUTABLE) or not set(packet['support'])<=set(SUPPORT) or not set(packet['updated'])<=set(UPDATED_APPEND):
        raise ValueError('远程账本表清单错误')
    store.db.execute('BEGIN IMMEDIATE');store.db.execute('PRAGMA defer_foreign_keys=ON')
    try:
        for t,incoming in packet['support'].items():
            if t=='plans':
                incoming=[{**r,'status':'EXPIRED'} if json.loads(r['payload_json']).get('kind')=='RISK_EXIT_ONLY' else r for r in incoming]
            # Local research is authoritative; never overwrite its current plans.
            upsert(store,t,incoming,immutable=True)
        for t,records in packet['tables'].items():upsert(store,t,records,immutable=t not in UPDATED_APPEND)
        for t,records in packet['updated'].items():upsert(store,t,records)
        for t,records in packet['mutable'].items():upsert(store,t,records)
        import_extras(store,config,packet.get('extras'))
        put(store,'ledger_cursors',packet['cursors']);put(store,'ledger_change_cursor',packet['change_cursor'])
        put(store,'ledger_support_since',packet['support_since']);put(store,'remote_features',packet.get('features',[]))
        if not packet['more']:
            put(store,'remote_ledger_version',packet['ledger_version']);put(store,'remote_ledger_at',packet['at'])
        store.db.commit()
    except BaseException:store.db.rollback();raise


def import_extras(store,config,extras):
    """Outage records and notices that ride on the last ledger page. Inside the import transaction, but a
    savepoint keeps a bad extra from rolling back the ledger itself; the error is kept for the digest."""
    if not isinstance(extras,dict):return
    from datetime import datetime,timezone
    stamp=datetime.now(timezone.utc).isoformat(timespec='seconds')
    for key,apply in (('quote_health',_mirror_health),('notices',_mirror_notices)):
        if key+'_error' in extras:
            put(store,'extras_error_'+key,{'at':stamp,'error':'云端：'+str(extras[key+'_error'])[:200]});continue
        if key not in extras:continue
        store.db.execute('SAVEPOINT ledger_extra')
        try:
            apply(store,extras[key]);store.db.execute('RELEASE ledger_extra')
            put(store,'extras_error_'+key,None)
        except Exception as exc:
            store.db.execute('ROLLBACK TO ledger_extra');store.db.execute('RELEASE ledger_extra')
            put(store,'extras_error_'+key,{'at':stamp,'error':f'{type(exc).__name__}: {str(exc)[:200]}'})


def _mirror_health(store,rows):
    from .quote_health import upsert
    upsert(store,rows)
    if rows:put(store,'quote_health_since',max(r['updated_at'] for r in rows))


def _mirror_notices(store,data):
    from .notices import mirror
    since=mirror(store,data)
    if since:put(store,'cloud_notices_since',since)


def import_ledger(store,config,packet):
    if config.get('deployment_role')!='research':raise ValueError('账本只能导入本地研究副本')
    if packet.get('protocol')==2:return import_ledger_v2(store,config,packet)
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
        put(store,'ledger_cursors',packet['cursors']);put(store,'remote_features',packet.get('features',[]))
        if not packet['more']:
            put(store,'remote_ledger_version',packet['ledger_version']);put(store,'remote_ledger_at',packet['at'])
        store.db.commit()
    except BaseException:store.db.rollback();raise
