import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_config import load_config
from ashare.storage import Store,normalize_time
from ashare.settings import validate_settings
from ashare.scheduler import schedule_due,enqueue
from ashare.reporting import next_runs
from ashare.slots import run_slot
from ashare.demo import seed,study,research_model,put_quote,SYMBOL
from ashare.paper import settle
from ashare.review import model_facts


def minute_config(config):
    return {**config,'collection_times':['00:00','06:00','12:00','18:00'],
        'slot_times':[f'{m//60:02d}:{m%60:02d}' for start,end in ((570,690),(780,897)) for m in range(start,end)],
        'slot_execution_mode':'RULES','slot_deadline_seconds':45,'scheduler_poll_seconds':5}


class MinuteStrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.cfg=minute_config(load_config(Path(__file__).resolve().parents[1]/'config.json'))
        self.cfg.update(data_dir=self.tmp.name,watchlist=[{'symbol':SYMBOL,'name':'合成测试'}],paper_slippage_bps=0,paper_max_fill_qty=100)
        self.at='2026-09-15T10:00:00+08:00'

    def tearDown(self):self.store.close();self.tmp.cleanup()

    def plan(self):
        packet=seed(self.store,self.cfg)
        return study(self.store,self.cfg,packet,model_fn=research_model(packet),at='2026-09-15T09:01:00+08:00')

    def slot(self,at=None,events='OK'):
        at=at or self.at;put_quote(self.store,at)
        with patch('ashare.slots.run_json',side_effect=AssertionError('minute execution must not call a model')):
            return run_slot(self.store,self.cfg,at,clock=lambda:at,refresh_fn=lambda *a:{'event_status':{SYMBOL:events}})

    def test_rule_buy_works_without_new_model_and_does_not_duplicate_open_order(self):
        self.plan();self.cfg['model_enabled']=False
        first=self.slot();self.assertEqual(first['model_status'],'NOT_NEEDED')
        self.assertEqual(first['decisions'][0]['status'],'SUBMITTED')
        self.assertEqual(self.slot()['status'],'ALREADY_DONE')
        again=self.slot('2026-09-15T10:01:00+08:00')
        self.assertEqual(again['decisions'][0]['status'],'BLOCKED')
        self.assertIn('EXISTING_OPEN_ORDER',again['decisions'][0]['reason'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],1)

    def test_rules_still_block_missing_announcements_and_stale_prices(self):
        self.plan();result=self.slot(events='FAILED')
        self.assertEqual(result['decisions'][0]['status'],'BLOCKED')
        at='2026-09-15T10:05:00+08:00'
        result=run_slot(self.store,self.cfg,at,clock=lambda:at,refresh_fn=lambda *a:{'event_status':{SYMBOL:'OK'}})
        self.assertEqual(result['decisions'][0]['status'],'BLOCKED')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],0)

    def test_plan_change_before_order_still_blocks_rules(self):
        self.plan();put_quote(self.store,self.at);calls=[]
        def refresh(*args):
            calls.append(1)
            if len(calls)==2:
                with self.store.db:self.store.db.execute("UPDATE plans SET status='EXPIRED'")
            return {'event_status':{SYMBOL:'OK'}}
        result=run_slot(self.store,self.cfg,self.at,clock=lambda:self.at,refresh_fn=refresh)
        self.assertIn('PLAN_CHANGED_OR_EXPIRED',result['decisions'][0]['reason'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],0)

    def test_rules_stop_preserves_t_plus_one_then_sells_next_day(self):
        self.cfg['paper_max_fill_qty']=10000
        self.plan();self.slot();at='2026-09-15T10:00:10+08:00';put_quote(self.store,at);settle(self.store,self.cfg,at)
        for at,expected in [('2026-09-15T10:01:00+08:00','BLOCKED'),('2026-09-16T10:00:00+08:00','SUBMITTED')]:
            put_quote(self.store,at,935)
            result=run_slot(self.store,self.cfg,at,clock=lambda:at,refresh_fn=lambda *a:{'event_status':{SYMBOL:'OK'}})
            self.assertEqual(result['decisions'][0]['action'],'SELL')
            self.assertEqual(result['decisions'][0]['status'],expected)

    def test_six_hour_research_and_minute_execution_respect_market_breaks(self):
        validate_settings(self.cfg);self.assertEqual(len(self.cfg['slot_times']),237)
        for stamp,expected in [('2026-09-21T11:29:10+08:00','13:00'),('2026-09-21T14:56:10+08:00','09:30'),('2026-09-20T10:00:00+08:00','09:30')]:
            slot=next(r for r in next_runs(self.cfg,stamp) if r['kind']=='slot')
            from ashare.calendar import local
            self.assertEqual(local(slot['scheduled_at']).strftime('%H:%M'),expected)
        self.assertEqual([r for r in next_runs(self.cfg,'2026-09-21T10:00:01+08:00') if r['kind']=='slot'][0]['scheduled_at'],normalize_time('2026-09-21T10:01:00+08:00'))
        with self.assertRaises(ValueError):validate_settings({**self.cfg,'slot_deadline_seconds':60})
        with self.assertRaises(ValueError):validate_settings({**self.cfg,'collection_times':['00:00','00:30']})

    def test_wake_catches_only_latest_research_and_current_minute(self):
        with self.store.db:self.store.db.execute("INSERT INTO service_state VALUES('last_scan',?)",(normalize_time('2026-09-20T19:00:00+08:00'),))
        at='2026-09-21T10:15:05+08:00';schedule_due(self.store,self.cfg,at);schedule_due(self.store,self.cfg,at)
        pending=[dict(r) for r in self.store.db.execute("SELECT kind,scheduled_at FROM jobs WHERE status='PENDING' AND kind IN ('cycle','slot')")]
        self.assertEqual(pending,[{'kind':'cycle','scheduled_at':normalize_time('2026-09-21T06:00:00+08:00')},{'kind':'slot','scheduled_at':normalize_time('2026-09-21T10:15:00+08:00')}])
        self.assertTrue(self.store.db.execute("SELECT 1 FROM jobs WHERE kind='slot' AND status='MISSED'").fetchone())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM paper_orders').fetchone()[0],0)

    def test_first_start_catches_latest_research_and_backlog_coalesces(self):
        schedule_due(self.store,self.cfg,'2026-09-21T07:00:00+08:00')
        enqueue(self.store,'cycle','2026-09-21T06:00:00+08:00','recover:cycle:old')
        enqueue(self.store,'cycle','2026-09-21T07:00:00+08:00','manual:keep')
        schedule_due(self.store,self.cfg,'2026-09-21T12:00:05+08:00')
        pending=list(self.store.db.execute("SELECT id,scheduled_at FROM jobs WHERE kind='cycle' AND status='PENDING'"))
        self.assertEqual({r['id'] for r in pending},{'manual:keep','cycle:'+normalize_time('2026-09-21T12:00:00+08:00')})
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs WHERE id='recover:cycle:old'").fetchone()[0],'SKIPPED_CATCHUP')

    def test_minute_jobs_share_hourly_backup_and_keep_manual_backups(self):
        manual=self.store.backup()
        one=self.store.periodic_backup('2026-09-21T10:01:00+08:00')
        self.assertEqual(one,self.store.periodic_backup('2026-09-21T10:02:00+08:00'))
        two=self.store.periodic_backup('2026-09-21T11:01:00+08:00')
        self.assertNotEqual(one,two);self.assertTrue(manual.exists())
        self.assertEqual(len(list((Path(self.tmp.name)/'backups'/'hourly').glob('*.sqlite3'))),2)

    def test_large_review_uses_full_counts_and_disclosed_bounded_samples(self):
        decisions=[{'id':f'd{i}','symbol':f'sh{i%22:06d}','slot_id':f's{i//22}','plan_id':'old-plan','at':self.at,'action':'HOLD','status':'BLOCKED','reason':'行情暂未到齐','payload_json':'{}'} for i in range(5214)]
        inputs=[{'id':f's{i}','input_json':json.dumps({'input_as_of':self.at,'stocks':[],'unneeded':'x'*3000})} for i in range(237)]
        facts={'window_start':self.at,'window_end':self.at,'decisions':decisions,'related_decisions_outside_window':[],
               'orders_known_at_review':[],'fills':[],'slot_inputs':inputs,'equity_marks':[None,None],'statistics':{'decision_count':5214,'fill_count':0}}
        small=model_facts(facts)
        self.assertLessEqual(len(json.dumps(small,ensure_ascii=False)),110000)
        self.assertTrue(small['coverage']['sampled']);self.assertEqual(small['coverage']['all_decisions'],5214)
        self.assertEqual(sum(r['decision_count'] for r in small['stock_statistics']),5214)
        self.assertEqual({i['decision_id'] for i in small['slot_inputs']},{d['id'] for d in small['decisions']})
        self.assertTrue(all(i['stock'] is None for i in small['slot_inputs']))
        self.assertEqual(len(facts['decisions']),5214)


if __name__=='__main__':unittest.main()
