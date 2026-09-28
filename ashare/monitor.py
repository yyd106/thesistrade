"""Independent bounded market refresh; never executes a trading decision."""
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from . import sources
from .calendar import phase
from .storage import Store, now


def symbols_for(store, config):
    symbols={i['symbol'] for i in config['watchlist']}
    symbols.update(r[0] for r in store.db.execute('SELECT DISTINCT symbol FROM paper_lots WHERE qty>0'))
    symbols.update(r[0] for r in store.db.execute("SELECT DISTINCT symbol FROM paper_orders WHERE status IN ('OPEN','PARTIAL','UNKNOWN')"))
    return sorted(symbols)


def refresh(config, kind):
    store=Store(config['data_dir']);rid=uuid.uuid4().hex
    try:
        symbols=symbols_for(store,config)
        with store.db:
            store.db.execute("INSERT INTO runs(id,job_key,kind,started_at,status) VALUES(?,?,?,?, 'RUNNING')",
                             (rid,'monitor:'+rid,'monitor_'+kind,now()))
        status='OK'
        if kind=='quotes':
            try:
                sources.collect_quotes(store,rid,symbols,config)
                if store.db.execute("SELECT 1 FROM source_checks WHERE run_id=? AND source='tencent_quotes' AND status!='OK' LIMIT 1",(rid,)).fetchone():status='PARTIAL'
            except Exception as exc:
                status='FAILED';store.check(rid,'tencent_quotes',None,'FAILED',str(exc)[:300],track=False)
        else:
            try:
                catalog=sources.stock_catalog(store)
                store.check(rid,'slot_events',None,'OK','公告来源已确认')
            except Exception as exc:
                status='FAILED';catalog=None
                store.check(rid,'slot_events',None,'FAILED',str(exc)[:300])
            if catalog is not None:
                # Each reader owns its SQLite connection; a slow stock cannot block the quote worker.
                def stock(symbol):
                    local=Store(config['data_dir'])
                    try:
                        sources.collect_announcements(local,rid,{'symbol':symbol},
                            {**config,'announcement_lookback_days':3,'pdf_downloads_per_stock':0,'_intraday':True},catalog)
                        row=local.db.execute("SELECT status FROM source_checks WHERE run_id=? AND symbol=? AND source='cninfo_catalog' ORDER BY id DESC LIMIT 1",(rid,symbol)).fetchone()
                        state=row[0] if row else 'FAILED'
                        local.check(rid,'slot_events',symbol,state,'公告目录已完整检查' if state=='OK' else '公告目录分页尚未完整取得')
                        return state
                    except Exception as exc:
                        local.check(rid,'slot_events',symbol,'FAILED',str(exc)[:300]);return 'FAILED'
                    finally:local.close()
                with ThreadPoolExecutor(max_workers=3,thread_name_prefix='announcement') as pool:
                    states=list(pool.map(stock,symbols))
                    if any(x!='OK' for x in states):status='PARTIAL'
        with store.db:
            store.db.execute('UPDATE runs SET status=?,finished_at=?,as_of=? WHERE id=?',(status,now(),now(),rid))
            store.db.execute('INSERT OR REPLACE INTO service_state VALUES(?,?)',
                ('monitor_'+kind,json.dumps({'checked_at':now(),'status':status,'run_id':rid})))
        return {'status':status,'run_id':rid}
    finally:store.close()


def sweep_health(config):
    """Outside continuous trading: end outage events that went idle and check held stocks' stops while the
    day's minute series is still available. Never raises into the scheduler."""
    from . import quote_health
    store=Store(config['data_dir'])
    try:
        return quote_health.sweep(store,config)
    except Exception as exc:
        with store.db:store.db.execute('INSERT OR REPLACE INTO service_state VALUES(?,?)',('quote_health_error',now()+' '+str(exc)[:300]))
        return None
    finally:store.close()


def cached_checks(store, config, events=True):
    """Only a complete, recent check is usable; neither failure nor old cache is success."""
    stamp=now();states={}
    failed=store.db.execute("SELECT id FROM source_checks WHERE source='slot_events' AND (symbol IS NULL OR symbol='MARKET') AND status!='OK' ORDER BY id DESC LIMIT 1").fetchone()
    for symbol in symbols_for(store,config):
        row=store.db.execute("SELECT id,status,checked_at FROM source_checks WHERE source='slot_events' AND symbol=? ORDER BY id DESC LIMIT 1",(symbol,)).fetchone()
        age=(datetime.fromisoformat(stamp)-datetime.fromisoformat(row['checked_at'])).total_seconds() if row else None
        states[symbol]=row['status'] if row and 0<=age<=config['announcement_max_age_seconds'] else 'STALE'
        if failed and (not row or failed['id']>row['id']):states[symbol]='FAILED'
    return {'quote_status':'CHECK_PER_STOCK','event_status':states,'cached':True,'checked_at':stamp}


class MarketMonitor:
    def __init__(self):
        self.pool=ThreadPoolExecutor(max_workers=3,thread_name_prefix='market-data')
        self.pending={};self.due={'quotes':0,'events':0}

    def tick(self,config):
        # Finish errors even outside market hours; pending work is never silently abandoned.
        for kind,future in list(self.pending.items()):
            if future.done():
                try:future.result()
                finally:del self.pending[kind]
        if not config['scheduler_enabled']:return
        stamp=time.monotonic()
        # Once a minute, in every phase and apart from the quote refresh: end idle outage events and run
        # the queued stop checks for held stocks.
        if 'health' not in self.pending and stamp>=self.due.get('health',0):
            self.pending['health']=self.pool.submit(sweep_health,dict(config));self.due['health']=stamp+60
        if not config['background_market_enabled'] or phase(now())!='CONTINUOUS':return
        for kind,interval in (('quotes',config['quote_poll_seconds']),('events',config['announcement_poll_seconds'])):
            if kind not in self.pending and stamp>=self.due[kind]:
                self.pending[kind]=self.pool.submit(refresh,dict(config),kind)
                self.due[kind]=stamp+interval

    def close(self):self.pool.shutdown(wait=True,cancel_futures=True)
