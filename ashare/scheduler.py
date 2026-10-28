"""One local scheduler; durable job keys; never replay a missed trading Slot."""
from __future__ import annotations
import json
import logging
import sqlite3
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


LOG = logging.getLogger(__name__)
PORTFOLIO_BLOCKERS = frozenset(('research', 'cycle', 'collect', 'repair', 'industry_research',
    'review', 'dynamic_cycle', 'global_research', 'portfolio_strategy'))


class JobCompletionPending(RuntimeError):
    """The work has ended; retry recording its outcome, never execute it again."""
    def __init__(self, completion, cause):
        self.completion = {**completion, 'worker_error': type(cause).__name__ + ': ' + str(cause)[:500]}
        super().__init__('任务完成状态尚未保存：' + self.completion['worker_error'])


def persist_completion(store, jid, completion):
    """Idempotent finalization; a later terminal/recovery state must not be overwritten."""
    with store.db:
        store.db.execute('''UPDATE jobs SET status=?,finished_at=?,result_json=?,error=?
            WHERE id=? AND status IN ('PENDING','RUNNING')''',
            (completion['status'], completion['finished_at'], completion['result_json'], completion['error'], jid))
        if completion.get('worker_error'):
            store.db.execute("INSERT OR REPLACE INTO service_state VALUES('scheduler_worker_error',?)",
                (completion['finished_at'] + ' ' + jid + ' ' + completion['worker_error'],))


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
            # Daily model-free checks also cover the global route on A-share holidays.
            ('evaluate',[config['evaluation_time']] if config.get('evaluation_enabled',True) else []),
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
        self.futures={};self.last_settle=0;self.last_followups=0;self.last_maintenance=0;self.last_dividends=0
        self.last_withdrawal_check=None
        self.pending_completions={}
        self.sync_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='cloud-sync');self.sync_future=None;self.last_sync=0
        # Page collection and HTTPS must never occupy the ledger/strategy worker.
        self.page_display_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='page-display')
        self.page_display_future=None;self.last_page_display=None
        self.strategy_export_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='strategy-export')
        self.strategy_export_future=None;self.last_strategy_export=None
        self.dynamic_pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='dynamic')
        self.dynamic_futures={}
        self.global_pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='global-paper')
        self.global_futures={}
        # Reports repository pushes run apart from jobs: git over the network must never delay research.
        self.reports_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='reports');self.reports_future=None;self.last_reports=None
        self.supervision_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='supervision')
        self.supervision_future=None;self.supervision_cancel=Event();self.last_supervision_scan=None
        self.experiment_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='experiment')
        self.experiment_future=None;self.experiment_cancel=Event();self.last_experiment_scan=None
        self.experiment_scan_ok=True
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
            if role(config)!='cloud':
                from .supervision import recover
                recover(store)
        finally:store.close()

    def work(self,job):
        config=None
        try:
            config=load_config(self.config_path)
            store=Store(config['data_dir'])
            try:
                row=store.db.execute('SELECT payload_json FROM job_inputs WHERE job_id=?',(job['id'],)).fetchone()
                payload=json.loads(row[0]) if row else {}
            finally:store.close()
            result=execute(config,job['kind'],key=job['id'] if job['kind'] in ('cycle','collect') else None,
                symbol=payload.get('symbol'),
                scheduled_at=job['scheduled_at'] if job['kind'] in ('slot','dynamic_slot') else None,
                end=job['scheduled_at'] if job['kind'] in ('review','dynamic_cycle','digest') else None)
            failed=result.get('status')=='DEFERRED' if isinstance(result,dict) else any(r.get('status')=='DEFERRED' for r in result) if isinstance(result,list) else False
            state='DEFERRED' if failed else 'DONE';error=None
            if job['kind']=='slot' and isinstance(result,dict):
                # No per-minute HOLD or rejected-attempt history in job payloads.
                result={**result,'decisions':[d for d in result.get('decisions',[]) if d['status']=='SUBMITTED']}
            result_json=json.dumps(result,ensure_ascii=False)
        except Offline as exc:
            # Stopped because this machine lost its network; research is redone after reconnect.
            result_json='null';state='DEFERRED';error=str(exc)[:500]
        except Exception as exc:
            result_json='null';state='FAILED';error=type(exc).__name__+': '+str(exc)[:500]
        completion={'status':state,'finished_at':now(),'result_json':result_json,'error':error}
        try:
            # A failed initial config read is recorded by the scheduler's own Store.
            if config is None:raise RuntimeError(error)
            store=Store(config['data_dir'])
            try:persist_completion(store,job['id'],completion)
            finally:store.close()
        except Exception as exc:
            raise JobCompletionPending(completion,exc) from exc

    def collect_workers(self,store,stamp):
        """Consume each completed future once, then independently retry durable finalization."""
        if not hasattr(self,'pending_completions'):self.pending_completions={}
        for workers in (self.futures,self.dynamic_futures,self.global_futures):
            for jid,future in list(workers.items()):
                if not future.done():continue
                try:future.result()
                except JobCompletionPending as exc:
                    self.pending_completions[jid]=exc.completion
                    LOG.warning('Job %s completion needs persistence: %s',jid,exc)
                except Exception as exc:
                    error=type(exc).__name__+': '+str(exc)[:500]
                    self.pending_completions[jid]={'status':'FAILED','finished_at':stamp,
                        'result_json':'null','error':error,'worker_error':error}
                    LOG.exception('Job %s worker failed',jid)
                finally:del workers[jid]
        for jid,completion in list(self.pending_completions.items()):
            try:persist_completion(store,jid,completion)
            except (sqlite3.Error,OSError):
                # Keep only the final outcome, not the failed Future or its traceback.
                # A service restart retains RUNNING as INTERRUPTED and never replays trades.
                LOG.exception('Job %s completion could not be saved; will retry next tick',jid)
            else:del self.pending_completions[jid]

    def withdrawal_tick(self,store,config,stamp):
        if config.get('mode')!='paper' or role(config)=='research':return
        if role(config)=='cloud':
            from .cloud_runtime import value
            if not value(store,'initialized',False):return
        current=time.monotonic()
        if self.last_withdrawal_check is not None and current-self.last_withdrawal_check<60:return
        self.last_withdrawal_check=current
        from .withdrawals import refresh
        try:
            refresh(store,config,stamp)
        except Exception as exc:
            LOG.exception('Withdrawal review failed; order transactions still recheck before execution')
            try:
                with store.db:store.db.execute("INSERT OR REPLACE INTO service_state VALUES('withdrawal_error',?)",(stamp+' '+str(exc)[:300],))
            except (sqlite3.Error,OSError):LOG.exception('Withdrawal error could not be saved')
        else:
            try:
                with store.db:store.db.execute("DELETE FROM service_state WHERE key='withdrawal_error'")
            except (sqlite3.Error,OSError):LOG.exception('Withdrawal error could not be cleared')

    def start_waiting_portfolio(self, store, config, stamp, active_kinds, offline):
        """Use an idle research boundary before starting another ordinary batch.

        Never interrupt a running study or relax the publication snapshot guard.
        The cloud trading queues keep their existing dispatch order.
        """
        if role(config) != 'research' or offline or len(self.global_futures) >= 2:
            return False
        if active_kinds & PORTFOLIO_BLOCKERS:
            return False
        row = store.db.execute("""SELECT * FROM jobs WHERE kind='portfolio_strategy'
            AND status='PENDING' AND scheduled_at<=? ORDER BY scheduled_at,id LIMIT 1""", (stamp,)).fetchone()
        if row is None:
            return False
        with store.db:
            changed = store.db.execute("""UPDATE jobs SET status='RUNNING',started_at=?,attempts=attempts+1
                WHERE id=? AND status='PENDING'""", (stamp, row['id'])).rowcount
        if not changed:
            return False
        active_kinds.add('portfolio_strategy')
        self.global_futures[row['id']] = self.global_pool.submit(self.work, dict(row))
        return True

    def tick(self):
        config=load_config(self.config_path);store=Store(config['data_dir'])
        self.poll_seconds=config['scheduler_poll_seconds']
        try:
            stamp=now()
            self.collect_workers(store,stamp)
            offline=None
            if role(config)!='cloud':
                # A long gap between ticks means the machine slept or the service was stopped.
                prior=store.db.execute("SELECT value FROM service_state WHERE key='tick_at'").fetchone()
                if prior and (datetime.fromisoformat(stamp)-datetime.fromisoformat(prior[0])).total_seconds()>=PAUSE_SECONDS:
                    log_interval(store.root,'pause',prior[0],stamp)
                with store.db:store.db.execute("INSERT OR REPLACE INTO service_state VALUES('tick_at',?)",(stamp,))
            if role(config)=='research':offline=offline_since(store,stamp)
            if role(config)!='research':self.monitor.tick(config)
            self.withdrawal_tick(store,config,stamp)
            if role(config)=='research':
                if self.sync_future and self.sync_future.done():
                    try:self.sync_future.result()
                    except Exception:pass  # Details persist in cloud_state; never block the scheduler clock.
                    self.sync_future=None
                if self.sync_future is None and time.monotonic()-self.last_sync>=60:
                    from .cloud_sync import sync_once
                    self.sync_future=self.sync_pool.submit(sync_once,config);self.last_sync=time.monotonic()
                self.page_display_tick(store,config)
                self.strategy_export_tick(store,config,offline)
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
            # Research wins immediately; only the supervision subprocess is cancelled.
            if role(config)!='cloud':
                from .supervision import busy
                if busy(store) or offline or not config['scheduler_enabled'] or not config.get('supervision_enabled',True):
                    self.supervision_cancel.set()
                if busy(store) or offline or not config['scheduler_enabled'] or not config.get('model_enabled'):
                    self.experiment_cancel.set()
            with store.db:store.db.execute("INSERT OR REPLACE INTO service_state VALUES('heartbeat',?)",(stamp,))
            # Process report queues and trading Slots in separate workers; slow PDF/model calls cannot stall the clock.
            active_kinds={r['kind'] for r in store.db.execute("SELECT id,kind FROM jobs WHERE status='RUNNING'")
                if r['id'] not in self.pending_completions}
            self.start_waiting_portfolio(store, config, stamp, active_kinds, offline)
            for row in store.db.execute("SELECT * FROM jobs WHERE status='PENDING' AND kind NOT IN ('dynamic_cycle','dynamic_slot','global_research','global_slot','portfolio_strategy') ORDER BY CASE kind WHEN 'slot' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,scheduled_at LIMIT 40").fetchall():
                if len(self.futures)>=3:break
                if row['kind'] in active_kinds:continue
                if offline and row['kind'] in NEEDS_NETWORK:continue
                if role(config)=='research' and 'portfolio_strategy' in active_kinds and row['kind'] in PORTFOLIO_BLOCKERS:continue
                if row['kind'] in ('research','cycle','collect','repair','industry_research') and active_kinds & {'research','cycle','collect','repair','industry_research'}:continue
                with store.db:
                    store.db.execute("UPDATE jobs SET status='RUNNING',started_at=?,attempts=attempts+1 WHERE id=? AND status='PENDING'",(stamp,row['id']))
                active_kinds.add(row['kind']);self.futures[row['id']]=self.pool.submit(self.work,dict(row))
            for row in store.db.execute("SELECT * FROM jobs WHERE status='PENDING' AND kind IN ('dynamic_cycle','dynamic_slot') ORDER BY CASE kind WHEN 'dynamic_slot' THEN 0 ELSE 1 END,scheduled_at DESC LIMIT 4").fetchall():
                if len(self.dynamic_futures)>=2 or row['kind'] in active_kinds:continue
                if offline and row['kind'] in NEEDS_NETWORK:continue
                if role(config)=='research' and 'portfolio_strategy' in active_kinds and row['kind']=='dynamic_cycle':continue
                # Preserve watchlist's existing allocation order. Dynamic receives
                # the remaining shared budget after that minute's original slot.
                if row['kind']=='dynamic_slot' and any(r[0] not in self.pending_completions for r in store.db.execute(
                    "SELECT id FROM jobs WHERE kind='slot' AND scheduled_at=? AND status IN ('PENDING','RUNNING')",(row['scheduled_at'],))):continue
                with store.db:store.db.execute("UPDATE jobs SET status='RUNNING',started_at=?,attempts=attempts+1 WHERE id=? AND status='PENDING'",(stamp,row['id']))
                active_kinds.add(row['kind']);self.dynamic_futures[row['id']]=self.dynamic_pool.submit(self.work,dict(row))
            for row in store.db.execute("SELECT * FROM jobs WHERE status='PENDING' AND kind IN ('global_slot','global_research','portfolio_strategy') ORDER BY CASE kind WHEN 'global_slot' THEN 0 ELSE 1 END,scheduled_at DESC").fetchall():
                if len(self.global_futures)>=2 or row['kind'] in active_kinds:continue
                if offline and row['kind'] in NEEDS_NETWORK:continue
                if row['kind'] in ('global_research','portfolio_strategy') and active_kinds & PORTFOLIO_BLOCKERS:continue
                if row['kind']=='global_slot' and any(r[0] not in self.pending_completions for r in store.db.execute(
                    "SELECT id FROM jobs WHERE kind IN ('slot','dynamic_slot') AND scheduled_at=? AND status IN ('PENDING','RUNNING')",(row['scheduled_at'],))):continue
                with store.db:store.db.execute("UPDATE jobs SET status='RUNNING',started_at=?,attempts=attempts+1 WHERE id=? AND status='PENDING'",(stamp,row['id']))
                active_kinds.add(row['kind']);self.global_futures[row['id']]=self.global_pool.submit(self.work,dict(row))
            # Outstanding paper orders need a post-decision quote, not a fictitious instant fill.
            if role(config)!='research' and time.monotonic()-self.last_settle>=30 and 'settle' not in active_kinds and 'slot' not in active_kinds:
                if store.db.execute("SELECT 1 FROM paper_orders WHERE status IN ('OPEN','PARTIAL') LIMIT 1").fetchone():
                    enqueue(store,'settle',stamp)
                self.last_settle=time.monotonic()
            # The executing ledger credits verified cash dividends on held shares (the research node never does).
            if role(config)!='research' and time.monotonic()-self.last_dividends>=300:
                self.credit_dividends(store,config,stamp);self.last_dividends=time.monotonic()
            if time.monotonic()-self.last_maintenance>=3600:
                from .maintenance import run as maintain
                try:maintain(store,config,stamp)
                except Exception as exc:
                    with store.db:store.db.execute("INSERT OR REPLACE INTO service_state VALUES('maintenance_error',?)",(stamp+' '+str(exc)[:300],))
                self.last_maintenance=time.monotonic()
            if role(config)!='cloud' and config.get('reports_sync_enabled'):
                if self.reports_future and self.reports_future.done():
                    try:self.reports_future.result()
                    except Exception:pass  # run_sync records its own failures in service_state
                    self.reports_future=None
                requested=store.db.execute("SELECT 1 FROM service_state WHERE key='reports_sync_requested'").fetchone()
                if self.reports_future is None and (requested or self.last_reports is None or time.monotonic()-self.last_reports>=3600):
                    from .reports import run_sync
                    self.reports_future=self.reports_pool.submit(run_sync,dict(config));self.last_reports=time.monotonic()
            if role(config)!='cloud':self.supervision_tick(store,config,stamp,offline)
            if role(config)!='cloud':self.experiment_tick(store,config,stamp,offline)
            if role(config)!='cloud' and time.monotonic()-self.last_followups>=60:
                from .followups import reconcile
                try:
                    reconcile(store,config,stamp)
                    with store.db:store.db.execute("DELETE FROM service_state WHERE key='followups_error'")
                except Exception as exc:
                    with store.db:store.db.execute("INSERT OR REPLACE INTO service_state VALUES('followups_error',?)",(stamp+' '+type(exc).__name__,))
                self.last_followups=time.monotonic()
        finally:store.close()

    def supervision_tick(self,store,config,stamp,offline=None):
        from . import supervision
        if self.supervision_future and self.supervision_future.done():
            try:self.supervision_future.result()
            except Exception as exc:
                with store.db:store.db.execute("INSERT OR REPLACE INTO service_state VALUES('supervision_worker_error',?)",(str(exc)[:200],))
            self.supervision_future=None
        if offline or not config['scheduler_enabled'] or not config.get('supervision_enabled',True) or not config['model_enabled']:
            self.supervision_cancel.set();return
        if self.last_supervision_scan is None or time.monotonic()-self.last_supervision_scan>=60:
            try:supervision.discover(store,config,stamp)
            except Exception as exc:
                with store.db:store.db.execute("INSERT OR REPLACE INTO service_state VALUES('supervision_worker_error',?)",(str(exc)[:200],))
            self.last_supervision_scan=time.monotonic()
        if self.supervision_future is not None or supervision.busy(store):return
        pending=supervision.next_pending(store,stamp)
        if pending:
            self.supervision_cancel=Event()
            self.supervision_future=self.supervision_pool.submit(supervision.worker,dict(config),pending['id'],self.supervision_cancel)

    def page_display_tick(self,store,config):
        """One independent, feature-gated page refresh at most every minute."""
        from .cloud_sync import page_display_once,remote_supports
        from .page_display import FEATURE
        if self.page_display_future is not None and self.page_display_future.done():
            try:self.page_display_future.result()
            except Exception as exc:
                from .cloud_runtime import put
                try:
                    with store.db:put(store,'page_display_sync',{'at':now(),'status':'FAILED','error':str(exc)[:300]})
                except Exception:pass
            self.page_display_future=None
        if self.stop.is_set() or role(config)!='research' or not (remote_supports(store,FEATURE) or remote_supports(store,'strategy_freshness_v1')):return
        if self.page_display_future is not None:return
        if self.last_page_display is not None and time.monotonic()-self.last_page_display<60:return
        self.last_page_display=time.monotonic()
        self.page_display_future=self.page_display_pool.submit(page_display_once,dict(config),self.stop)

    def strategy_export_tick(self,store,config,offline=None):
        """Read-only download preparation cannot occupy research or ledger workers."""
        from .cloud_sync import remote_supports
        from .strategy_export_sync import FEATURE,worker
        if self.strategy_export_future is not None and self.strategy_export_future.done():
            try:self.strategy_export_future.result()
            except Exception as exc:
                from .cloud_runtime import put
                try:
                    with store.db:put(store,'strategy_export_sync',{'at':now(),'status':'FAILED','error':'策略档案同步未完成，将自动重试。','error_type':type(exc).__name__})
                except Exception:pass
            self.strategy_export_future=None
        if self.stop.is_set() or offline or role(config)!='research' or not remote_supports(store,FEATURE):return
        if self.strategy_export_future is not None:return
        if self.last_strategy_export is not None and time.monotonic()-self.last_strategy_export<300:return
        self.last_strategy_export=time.monotonic()
        self.strategy_export_future=self.strategy_export_pool.submit(worker,dict(config),cancel_event=self.stop)

    def experiment_tick(self,store,config,stamp,offline=None):
        """Bounded local observation work; research and supervision take priority."""
        if role(config)=='cloud':
            self.experiment_cancel.set();return
        from . import experiment_runner,supervision
        if self.experiment_future and self.experiment_future.done():
            try:self.experiment_future.result()
            except Exception as exc:
                with store.db:store.db.execute("INSERT OR REPLACE INTO service_state VALUES('experiment_worker_error',?)",(type(exc).__name__,))
            self.experiment_future=None
        # Deadline accounting is model-free and remains active while offline.
        if self.last_experiment_scan is None or time.monotonic()-self.last_experiment_scan>=60:
            try:
                experiment_runner.advance(store,config,stamp)
                self.experiment_scan_ok=True
            except Exception as exc:
                self.experiment_scan_ok=False
                with store.db:store.db.execute("INSERT OR REPLACE INTO service_state VALUES('experiment_worker_error',?)",(type(exc).__name__,))
            self.last_experiment_scan=time.monotonic()
        if not self.experiment_scan_ok or offline or not config['scheduler_enabled'] or not config.get('model_enabled') or supervision.busy(store):
            self.experiment_cancel.set();return
        if self.supervision_future is not None or (config.get('supervision_enabled',True) and supervision.next_pending(store,stamp)):
            self.experiment_cancel.set();return
        if self.experiment_future is not None:return
        try:pending=experiment_runner.next_pending(store,config,stamp)
        except Exception as exc:
            with store.db:store.db.execute("INSERT OR REPLACE INTO service_state VALUES('experiment_worker_error',?)",(type(exc).__name__,))
            return
        if pending:
            self.experiment_cancel=Event()
            self.experiment_future=self.experiment_pool.submit(experiment_runner.worker,dict(config),pending['id'],self.experiment_cancel)

    def credit_dividends(self,store,config,stamp):
        """Credit dividends and reconcile closed-day tax; failures remain explicit in service_state."""
        from .dividends import credit
        try:
            result=credit(store,config,stamp)
        except Exception as exc:
            if store.db.in_transaction:store.db.rollback()
            with store.db:store.db.execute("INSERT OR REPLACE INTO service_state VALUES('dividend_error',?)",(stamp+' '+type(exc).__name__+': '+str(exc)[:300],))
            return None
        with store.db:
            store.db.execute("DELETE FROM service_state WHERE key='dividend_error'")
            if result['conflicts']:
                store.db.execute("INSERT OR REPLACE INTO service_state VALUES('dividend_conflicts',?)",(stamp+' '+','.join(result['conflicts'])[:300],))
            else:store.db.execute("DELETE FROM service_state WHERE key='dividend_conflicts'")
        return result

    def run(self):
        recovered=False
        while not self.stop.is_set():
            try:
                if not recovered:
                    self.recover();recovered=True
                self.tick()
            except Exception as exc:
                # Make scheduler failure visible without killing the local UI.
                LOG.exception('Scheduler iteration failed')
                try:
                    config=load_config(self.config_path);store=Store(config['data_dir'])
                    try:
                        with store.db:store.db.execute("INSERT OR REPLACE INTO service_state VALUES('last_error',?)",(now()+' '+str(exc)[:500],))
                    finally:store.close()
                except Exception:
                    # Logging remains visible when the same outage prevents the DB error record.
                    LOG.exception('Scheduler error record could not be saved; will retry the loop')
            self.stop.wait(self.poll_seconds)

    def close(self):
        self.stop.set()
        self.experiment_cancel.set()
        self.experiment_pool.shutdown(wait=True,cancel_futures=True)
        self.supervision_cancel.set()
        self.supervision_pool.shutdown(wait=True,cancel_futures=True)
        self.stop.set();self.pool.shutdown(wait=True,cancel_futures=True);self.dynamic_pool.shutdown(wait=True,cancel_futures=True);self.global_pool.shutdown(wait=True,cancel_futures=True);self.monitor.close();self.sync_pool.shutdown(wait=True,cancel_futures=True);self.reports_pool.shutdown(wait=True,cancel_futures=True)
        self.page_display_pool.shutdown(wait=True,cancel_futures=True)
        self.strategy_export_pool.shutdown(wait=True,cancel_futures=True)
