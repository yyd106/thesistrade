"""One local scheduler; durable job keys; never replay a missed trading Slot."""
from __future__ import annotations
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
from threading import Event
from .calendar import SH,local,trading_day
from .pipeline import load_config
from .storage import Store,now,normalize_time
from .workflow import execute
from .cloud_protocol import role
from .connectivity import NEEDS_NETWORK,PAUSE_SECONDS,Offline,log_interval,offline_since


def enqueue(store,kind,scheduled,key=None,status='PENDING',payload=None):
    jid=key or 'manual:'+uuid.uuid4().hex
    with store.db:
        store.db.execute('INSERT OR IGNORE INTO jobs(id,kind,scheduled_at,status) VALUES(?,?,?,?)',(jid,kind,normalize_time(scheduled),status))
        if payload is not None:store.db.execute('INSERT OR IGNORE INTO job_inputs VALUES(?,?)',(jid,json.dumps(payload,ensure_ascii=False)))
    return jid


def schedule_due(store,config,at):
    at=normalize_time(at)
    prior=store.db.execute("SELECT value FROM service_state WHERE key='last_scan'").fetchone()
    # First startup also catches the latest research window; old trades are missed,
    # never replayed. On wake, the persisted scan time spans the offline interval.
    if prior:previous=prior[0]
    else:
        current=local(at);times=[]
        for offset in (0,1):
            day=current-timedelta(days=offset)
            for hhmm in config['collection_times']:
                h,m=map(int,hhmm.split(':'));candidate=day.replace(hour=h,minute=m,second=0,microsecond=0)
                if candidate<=current:times.append(candidate)
        previous=normalize_time((max(times)-timedelta(seconds=1)).isoformat())
    start=local(previous).date();finish=local(at).date()
    cursor=start;items=[]
    while cursor<=finish:
        for kind,times in (('cycle',config['collection_times']),('review',[config['review_time']]),
            ('slot',config['slot_times'] if trading_day(cursor) is True else []),
            # Model-free evaluation after each trading day's data is collected, and a weekly report.
            ('evaluate',[config['evaluation_time']] if config.get('evaluation_enabled',True) and trading_day(cursor) is True else []),
            ('weekly_report',[config['weekly_report_time']] if config.get('evaluation_enabled',True) and cursor.weekday()==config.get('weekly_report_weekday',5) else []),
            # One-page daily summary of every stage, for the operator and the supervisor.
            ('digest',[config.get('digest_time','23:50')])):
            if role(config)=='research' and kind=='slot':continue
            if role(config)=='cloud' and kind!='slot':continue
            for hhmm in times:
                stamp=normalize_time(datetime.fromisoformat(cursor.isoformat()+'T'+hhmm+':00').replace(tzinfo=SH).isoformat())
                if previous<stamp<=at:items.append((kind,stamp))
        cursor+=timedelta(days=1)
    latest_cycle=max((t for k,t in items if k=='cycle'),default=None)
    for kind,stamp in items:
        delay=(datetime.fromisoformat(at)-datetime.fromisoformat(stamp)).total_seconds()
        status='MISSED' if kind=='slot' and delay>=config['slot_deadline_seconds'] else 'PENDING'
        if kind=='cycle' and stamp!=latest_cycle:status='SKIPPED_CATCHUP'
        enqueue(store,kind,stamp,kind+':'+stamp,status)
        if kind=='slot' and status=='MISSED':
            from .storage import digest
            with store.db:store.db.execute("INSERT OR IGNORE INTO slots VALUES(?,?,?,?,?,?,?)",
                (digest('slot:'+stamp)[:24],stamp,at,at,'MISSED','{}','NOT_RUN'))
    from .investment_policy import enabled
    if enabled(config) and latest_cycle and role(config)!='cloud':
        enqueue(store,'global_research',latest_cycle,'global_research:'+latest_cycle)
        with store.db:store.db.execute("UPDATE jobs SET status='SKIPPED_CATCHUP',finished_at=? WHERE kind='global_research' AND status='PENDING' AND scheduled_at<?",(at,latest_cycle))
    # A long study may span several ticks. Keep only the newest pending
    # scheduled cycle; manual requests and the running study retain their identity.
    def scheduled_cycle(jid):
        while jid.startswith('recover:'):jid=jid[len('recover:'):]
        return jid.startswith('cycle:')
    pending=[r for r in store.db.execute("SELECT id FROM jobs WHERE kind='cycle' AND status='PENDING' ORDER BY scheduled_at DESC,rowid DESC") if scheduled_cycle(r['id'])]
    if len(pending)>1:
        with store.db:store.db.executemany("UPDATE jobs SET status='SKIPPED_CATCHUP',finished_at=?,error='已合并至较新的定时研究' WHERE id=? AND status='PENDING'",[(at,r['id']) for r in pending[1:]])
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO service_state VALUES('last_scan',?)",(at,))
    return items


def schedule_dynamic_due(store,config,at):
    from .dynamic import window
    at=normalize_time(at)
    if config.get('dynamic_enabled',False) and role(config)!='cloud':
        start,end=window(at)
        enqueue(store,'dynamic_cycle',end,'dynamic_cycle:'+end)
        with store.db:
            store.db.execute("UPDATE jobs SET status='SKIPPED_CATCHUP',finished_at=?,error='动态新闻已合并至最新窗口，按采集水位补漏' WHERE kind='dynamic_cycle' AND status='PENDING' AND scheduled_at<?",(at,end))
    exposure=store.db.execute("SELECT 1 FROM dynamic_lots WHERE qty>0 UNION ALL SELECT 1 FROM dynamic_orders WHERE status IN ('OPEN','PARTIAL','UNKNOWN') LIMIT 1").fetchone()
    if role(config)!='research' and (config.get('dynamic_enabled',False) or exposure):
        from .calendar import phase
        if phase(at)=='CONTINUOUS' and local(at).strftime('%H:%M') in config['slot_times']:
            minute=normalize_time(datetime.fromisoformat(at).replace(second=0,microsecond=0).isoformat())
            missed=(datetime.fromisoformat(at)-datetime.fromisoformat(minute)).total_seconds()>=config['slot_deadline_seconds']
            enqueue(store,'dynamic_slot',minute,'dynamic_slot:'+minute,'MISSED' if missed else 'PENDING')
        with store.db:
            store.db.execute("UPDATE jobs SET status='MISSED',finished_at=? WHERE kind='dynamic_slot' AND status='PENDING' AND scheduled_at<?",(at,normalize_time((datetime.fromisoformat(at)-timedelta(seconds=config['slot_deadline_seconds'])).isoformat())))


def schedule_review_retry(store,config,at):
    """Retry the latest completed window twice, at least 30 minutes apart."""
    if not config['scheduler_enabled'] or not config['model_enabled']:return None
    from .calendar import review_window
    end=normalize_time(review_window(at,config['review_time'])[1].isoformat())
    latest=store.db.execute('SELECT * FROM reviews WHERE window_end=? ORDER BY revision DESC LIMIT 1',(end,)).fetchone()
    if not latest or latest['model_status']!='DEFERRED':return None
    if store.db.execute("SELECT 1 FROM jobs WHERE (kind='review' AND status IN ('PENDING','RUNNING')) OR (kind IN ('cycle','research','collect','repair') AND status IN ('PENDING','RUNNING')) LIMIT 1").fetchone():return None
    attempts=list(store.db.execute("SELECT * FROM jobs WHERE kind='review' AND id LIKE ? ORDER BY rowid",('review-retry:'+end+':%',)))
    if len(attempts)>=2:return None
    last=max([latest['ready_at']]+[r['finished_at'] or r['started_at'] or r['scheduled_at'] for r in attempts])
    if (datetime.fromisoformat(normalize_time(at))-datetime.fromisoformat(last)).total_seconds()<1800:return None
    return enqueue(store,'review',end,'review-retry:'+end+':'+str(len(attempts)+1))


def schedule_reconnected(store,config,at):
    if role(config)!='research' or not config['scheduler_enabled']:return
    from .cloud_runtime import value,put
    from .calendar import review_window
    stamp=value(store,'reconnect_pending')
    if not stamp:return
    current=local(at);candidates=[]
    for offset in (0,1):
        for hhmm in config['collection_times']:
            h,m=map(int,hhmm.split(':'));t=(current-timedelta(days=offset)).replace(hour=h,minute=m,second=0,microsecond=0)
            if t<=current:candidates.append(t)
    from .dynamic import window
    times={'cycle':normalize_time(max(candidates).isoformat()),'global_research':normalize_time(max(candidates).isoformat()),
           'dynamic_cycle':window(at)[1],'review':normalize_time(review_window(at,config['review_time'])[1].isoformat())}
    for kind,end in times.items():
        if kind=='dynamic_cycle' and not config.get('dynamic_enabled'):continue
        if not store.db.execute("SELECT 1 FROM jobs WHERE kind=? AND status IN ('PENDING','RUNNING')",(kind,)).fetchone():
            enqueue(store,kind,end,'reconnect:'+kind+':'+stamp)
    with store.db:put(store,'reconnect_pending',None)


class Scheduler:
    def __init__(self,config_path):
        self.config_path=config_path;self.stop=Event();self.pool=ThreadPoolExecutor(max_workers=3,thread_name_prefix='ashare')
        self.futures={};self.last_settle=0;self.last_followups=0;self.last_maintenance=0
        self.sync_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='cloud-sync');self.sync_future=None;self.last_sync=0
        self.dynamic_pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='dynamic')
        self.dynamic_futures={}
        self.global_pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='global-paper')
        self.global_futures={}
        self.poll_seconds=10
        from .monitor import MarketMonitor
        self.monitor=MarketMonitor()

    def recover(self):
        config=load_config(self.config_path);store=Store(config['data_dir'])
        try:
            if role(config)=='cloud':
                # Log mutable ledger changes from startup so research replicas can pull increments.
                from .cloud_ledger import ensure_change_log
                with store.db:ensure_change_log(store)
            with store.db:
                store.db.execute("UPDATE jobs SET status='INTERRUPTED',finished_at=?,error='服务重启：保留中断记录' WHERE status='RUNNING'",(now(),))
                store.db.execute("UPDATE slots SET status='INTERRUPTED',finished_at=? WHERE status='RUNNING'",(now(),))
            # Research and reviews are repeatable with a fresh job/snapshot. Never replay a trading job.
            for r in store.db.execute("SELECT * FROM jobs WHERE status='INTERRUPTED' AND kind IN ('cycle','research','review','dynamic_cycle','global_research','portfolio_strategy')").fetchall():
                recovery='recover:'+r['id']
                enqueue(store,r['kind'],r['scheduled_at'],recovery)
        finally:store.close()

    def work(self,job):
        config=load_config(self.config_path)
        store=Store(config['data_dir'])
        try:
            row=store.db.execute('SELECT payload_json FROM job_inputs WHERE job_id=?',(job['id'],)).fetchone()
            payload=json.loads(row[0]) if row else {}
        finally:store.close()
        try:
            result=execute(config,job['kind'],key=job['id'] if job['kind'] in ('cycle','collect') else None,
                symbol=payload.get('symbol'),
                scheduled_at=job['scheduled_at'] if job['kind'] in ('slot','dynamic_slot') else None,
                end=job['scheduled_at'] if job['kind'] in ('review','dynamic_cycle','digest') else None)
            failed=result.get('status')=='DEFERRED' if isinstance(result,dict) else any(r.get('status')=='DEFERRED' for r in result) if isinstance(result,list) else False
            state='DEFERRED' if failed else 'DONE';error=None
            if job['kind']=='slot' and isinstance(result,dict):
                # No per-minute HOLD or rejected-attempt history in job payloads.
                result={**result,'decisions':[d for d in result.get('decisions',[]) if d['status']=='SUBMITTED']}
        except Offline as exc:
            # Stopped because this machine lost its network; research is redone after reconnect.
            result=None;state='DEFERRED';error=str(exc)[:500]
        except Exception as exc:
            result=None;state='FAILED';error=type(exc).__name__+': '+str(exc)[:500]
        store=Store(config['data_dir'])
        try:
            with store.db:store.db.execute('UPDATE jobs SET status=?,finished_at=?,result_json=?,error=? WHERE id=?',
                (state,now(),json.dumps(result,ensure_ascii=False),error,job['id']))
        finally:store.close()

    def tick(self):
        config=load_config(self.config_path);store=Store(config['data_dir'])
        self.poll_seconds=config['scheduler_poll_seconds']
        try:
            stamp=now()
            offline=None
            if role(config)!='cloud':
                # A long gap between ticks means the machine slept or the service was stopped.
                prior=store.db.execute("SELECT value FROM service_state WHERE key='tick_at'").fetchone()
                if prior and (datetime.fromisoformat(stamp)-datetime.fromisoformat(prior[0])).total_seconds()>=PAUSE_SECONDS:
                    log_interval(store.root,'pause',prior[0],stamp)
                with store.db:store.db.execute("INSERT OR REPLACE INTO service_state VALUES('tick_at',?)",(stamp,))
            if role(config)=='research':offline=offline_since(store,stamp)
            if role(config)!='research':self.monitor.tick(config)
            if role(config)=='research':
                if self.sync_future and self.sync_future.done():
                    try:self.sync_future.result()
                    except Exception:pass  # Details persist in cloud_state; never block the scheduler clock.
                    self.sync_future=None
                if self.sync_future is None and time.monotonic()-self.last_sync>=60:
                    from .cloud_sync import sync_once
                    self.sync_future=self.sync_pool.submit(sync_once,config);self.last_sync=time.monotonic()
                with store.db:store.db.execute("UPDATE jobs SET status='MIGRATED',finished_at=? WHERE kind IN ('slot','dynamic_slot','global_slot','settle') AND status='PENDING'",(stamp,))
            elif role(config)=='cloud':
                from .cloud_runtime import value,fence
                with store.db:
                    store.db.execute("UPDATE jobs SET status='LOCAL_ONLY',finished_at=? WHERE kind NOT IN ('slot','dynamic_slot','global_slot','settle') AND status='PENDING'",(stamp,))
                    store.db.execute("INSERT OR REPLACE INTO service_state VALUES('heartbeat',?)",(stamp,))
                if not value(store,'execution_enabled',False):return
                fence(store,config,stamp)
            from .investment_policy import enabled,seed
            if enabled(config):
                seed(store,stamp)
                if config['scheduler_enabled'] and role(config)!='research':
                    minute=normalize_time(datetime.fromisoformat(stamp).replace(second=0,microsecond=0).isoformat())
                    enqueue(store,'global_slot',minute,'global_slot:'+minute)
                    with store.db:store.db.execute("UPDATE jobs SET status='MISSED',finished_at=? WHERE kind='global_slot' AND status='PENDING' AND scheduled_at<?",(stamp,minute))
            for jid,f in list(self.futures.items()):
                if f.done():
                    f.result();del self.futures[jid]
            if config['scheduler_enabled']:
                schedule_due(store,config,stamp)
                schedule_dynamic_due(store,config,stamp)
                schedule_reconnected(store,config,stamp)
            from .recovery import enqueue_recovery
            if role(config)!='cloud':
                enqueue_recovery(store,config,stamp)
                schedule_review_retry(store,config,stamp)
            from .portfolio_strategy import request
            if role(config)!='cloud':request(store,config,stamp)
            with store.db:store.db.execute("INSERT OR REPLACE INTO service_state VALUES('heartbeat',?)",(stamp,))
            # Process report queues and trading Slots in separate workers; slow PDF/model calls cannot stall the clock.
            active_kinds={r[0] for r in store.db.execute("SELECT kind FROM jobs WHERE status='RUNNING'")}
            for row in store.db.execute("SELECT * FROM jobs WHERE status='PENDING' AND kind NOT IN ('dynamic_cycle','dynamic_slot','global_research','global_slot','portfolio_strategy') ORDER BY CASE kind WHEN 'slot' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,scheduled_at LIMIT 40").fetchall():
                if len(self.futures)>=3:break
                if row['kind'] in active_kinds:continue
                if offline and row['kind'] in NEEDS_NETWORK:continue
                if row['kind'] in ('research','cycle','collect','repair') and active_kinds & {'research','cycle','collect','repair'}:continue
                with store.db:
                    store.db.execute("UPDATE jobs SET status='RUNNING',started_at=?,attempts=attempts+1 WHERE id=? AND status='PENDING'",(stamp,row['id']))
                active_kinds.add(row['kind']);self.futures[row['id']]=self.pool.submit(self.work,dict(row))
            for jid,f in list(self.dynamic_futures.items()):
                if f.done():
                    try:f.result()
                    except Exception as exc:
                        with store.db:store.db.execute("INSERT OR REPLACE INTO dynamic_state VALUES('worker_error',?)",(str(exc)[:200],))
                    finally:del self.dynamic_futures[jid]
            for row in store.db.execute("SELECT * FROM jobs WHERE status='PENDING' AND kind IN ('dynamic_cycle','dynamic_slot') ORDER BY CASE kind WHEN 'dynamic_slot' THEN 0 ELSE 1 END,scheduled_at DESC LIMIT 4").fetchall():
                if len(self.dynamic_futures)>=2 or row['kind'] in active_kinds:continue
                if offline and row['kind'] in NEEDS_NETWORK:continue
                # Preserve watchlist's existing allocation order. Dynamic receives
                # the remaining shared budget after that minute's original slot.
                if row['kind']=='dynamic_slot' and store.db.execute("SELECT 1 FROM jobs WHERE kind='slot' AND scheduled_at=? AND status IN ('PENDING','RUNNING')",(row['scheduled_at'],)).fetchone():continue
                with store.db:store.db.execute("UPDATE jobs SET status='RUNNING',started_at=?,attempts=attempts+1 WHERE id=? AND status='PENDING'",(stamp,row['id']))
                active_kinds.add(row['kind']);self.dynamic_futures[row['id']]=self.dynamic_pool.submit(self.work,dict(row))
            for jid,f in list(self.global_futures.items()):
                if f.done():
                    f.result();del self.global_futures[jid]
            for row in store.db.execute("SELECT * FROM jobs WHERE status='PENDING' AND kind IN ('global_slot','global_research','portfolio_strategy') ORDER BY CASE kind WHEN 'global_slot' THEN 0 ELSE 1 END,scheduled_at DESC").fetchall():
                if len(self.global_futures)>=2 or row['kind'] in active_kinds:continue
                if offline and row['kind'] in NEEDS_NETWORK:continue
                if row['kind'] in ('global_research','portfolio_strategy') and active_kinds & {'research','cycle','collect','repair','review','dynamic_cycle','global_research','portfolio_strategy'}:continue
                if row['kind']=='global_slot' and store.db.execute("SELECT 1 FROM jobs WHERE kind IN ('slot','dynamic_slot') AND scheduled_at=? AND status IN ('PENDING','RUNNING')",(row['scheduled_at'],)).fetchone():continue
                with store.db:store.db.execute("UPDATE jobs SET status='RUNNING',started_at=?,attempts=attempts+1 WHERE id=? AND status='PENDING'",(stamp,row['id']))
                active_kinds.add(row['kind']);self.global_futures[row['id']]=self.global_pool.submit(self.work,dict(row))
            # Outstanding paper orders need a post-decision quote, not a fictitious instant fill.
            if role(config)!='research' and time.monotonic()-self.last_settle>=30 and 'settle' not in active_kinds and 'slot' not in active_kinds:
                if store.db.execute("SELECT 1 FROM paper_orders WHERE status IN ('OPEN','PARTIAL') LIMIT 1").fetchone():
                    enqueue(store,'settle',stamp)
                self.last_settle=time.monotonic()
            if time.monotonic()-self.last_maintenance>=3600:
                from .maintenance import run as maintain
                try:maintain(store,config,stamp)
                except Exception as exc:
                    with store.db:store.db.execute("INSERT OR REPLACE INTO service_state VALUES('maintenance_error',?)",(stamp+' '+str(exc)[:300],))
                self.last_maintenance=time.monotonic()
            if role(config)!='cloud' and time.monotonic()-self.last_followups>=60:
                from .followups import reconcile
                try:
                    reconcile(store,config,stamp)
                    with store.db:store.db.execute("DELETE FROM service_state WHERE key='followups_error'")
                except Exception as exc:
                    with store.db:store.db.execute("INSERT OR REPLACE INTO service_state VALUES('followups_error',?)",(stamp+' '+type(exc).__name__,))
                self.last_followups=time.monotonic()
        finally:store.close()

    def run(self):
        self.recover()
        while not self.stop.is_set():
            try:self.tick()
            except Exception as exc:
                # Make scheduler failure visible without killing the local UI.
                config=load_config(self.config_path);store=Store(config['data_dir'])
                with store.db:store.db.execute("INSERT OR REPLACE INTO service_state VALUES('last_error',?)",(now()+' '+str(exc)[:500],))
                store.close()
            self.stop.wait(self.poll_seconds)

    def close(self):
        self.stop.set();self.pool.shutdown(wait=True,cancel_futures=True);self.dynamic_pool.shutdown(wait=True,cancel_futures=True);self.global_pool.shutdown(wait=True,cancel_futures=True);self.monitor.close();self.sync_pool.shutdown(wait=True,cancel_futures=True)
