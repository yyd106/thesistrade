import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_config import load_config
from ashare.storage import Store,normalize_time
from ashare.demo import seed,study,research_model,put_quote,SYMBOL
from ashare.slots import run_slot
from ashare.review import run_review
from ashare.scheduler import Scheduler,enqueue
from ashare.dashboard import status


class OperationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.cfg=load_config(Path(__file__).resolve().parents[1]/'config.json')
        self.cfg.update(data_dir=self.tmp.name,watchlist=[{'symbol':SYMBOL,'name':'合成测试'}],
            mode='paper',slot_execution_mode='RULES',slot_deadline_seconds=45,paper_slippage_bps=0,paper_max_fill_qty=100)
        self.at='2026-09-15T10:00:00+08:00'
        packet=seed(self.store,self.cfg)
        self.plan=study(self.store,self.cfg,packet,model_fn=research_model(packet),at='2026-09-15T09:01:00+08:00')

    def tearDown(self):self.store.close();self.tmp.cleanup()

    def slot(self,at=None,price=1000,events='OK'):
        at=at or self.at;put_quote(self.store,at,price)
        with patch('ashare.slots.run_json',side_effect=AssertionError('no minute model')):
            return run_slot(self.store,self.cfg,at,clock=lambda:at,refresh_fn=lambda *a:{'event_status':{SYMBOL:events}})

    def count(self,table):return self.store.db.execute('SELECT count(*) FROM '+table).fetchone()[0]

    def test_noops_overwrite_current_state_without_history_or_snapshots(self):
        for n in range(3):
            result=self.slot(f'2026-09-15T10:0{n}:00+08:00',events='FAILED')
            self.assertEqual(result['operation_count'],0)
            self.assertEqual(self.count('decisions'),0)
            self.assertEqual(self.count('latest_trade_checks'),1)
            self.assertEqual(self.store.db.execute('SELECT input_json FROM slots WHERE id=?',(result['slot_id'],)).fetchone()[0],'{}')
            self.assertFalse((self.store.root/'workflow'/'slots'/result['slot_id']).exists())
        self.assertEqual(self.store.latest_trade_check(SYMBOL,'2026-09-15T10:03:00+08:00')['at'],normalize_time('2026-09-15T10:02:00+08:00'))
        self.assertEqual(self.slot('2026-09-15T10:02:00+08:00')['status'],'ALREADY_DONE')

    def test_reentry_reuses_research_and_saves_only_operated_stock(self):
        other='sh600519';self.cfg['watchlist'].append({'symbol':other,'name':'无计划的测试股票'})
        outside=self.slot(price=950);self.assertEqual(outside['operation_count'],0)
        entered=self.slot('2026-09-15T10:01:00+08:00')
        self.assertEqual(entered['operation_count'],1);self.assertEqual(self.count('studies'),1)
        d=dict(self.store.db.execute('SELECT * FROM decisions').fetchone())
        self.assertEqual(d['plan_id'],self.plan['plan_id']);self.assertEqual(d['status'],'SUBMITTED')
        packet=json.loads(self.store.db.execute('SELECT input_json FROM slots WHERE id=?',(entered['slot_id'],)).fetchone()[0])
        self.assertEqual([s['symbol'] for s in packet['stocks']],[SYMBOL])
        self.assertEqual(json.loads((self.store.root/'workflow'/'slots'/entered['slot_id']/'input.json').read_text()),packet)
        self.assertEqual(self.count('latest_trade_checks'),2)

    def test_open_order_noop_keeps_original_history_and_records_later_fills(self):
        self.slot();again=self.slot('2026-09-15T10:01:00+08:00')
        self.assertEqual(again['operation_count'],0)
        self.assertIn('EXISTING_OPEN_ORDER',again['decisions'][0]['reason'])
        self.assertEqual(self.count('decisions'),1);self.assertEqual(self.count('paper_orders'),1)
        self.assertEqual(self.count('paper_fills'),1)
        self.assertEqual(self.store.latest_trade_check(SYMBOL,'2026-09-15T10:02:00+08:00')['status'],'BLOCKED')
        with patch('ashare.dashboard.now',return_value=normalize_time('2026-09-15T10:02:00+08:00')):
            view=status(self.cfg,overview=True)
        self.assertEqual(view['watchlist'][0]['open_orders'][0]['filled_qty'],100)
        self.assertEqual(len(view['decisions']),1)

    def test_budget_rejection_leaves_no_historical_operation(self):
        with self.store.db:self.store.db.execute('UPDATE paper_accounts SET cash_cents=100')
        r=self.slot();self.assertIn('INSUFFICIENT_BUDGET_OR_TARGET_REACHED',r['decisions'][0]['reason'])
        self.assertEqual(self.count('decisions'),0);self.assertEqual(self.count('paper_orders'),0)
        self.assertEqual(self.store.db.execute('SELECT input_json FROM slots').fetchone()[0],'{}')

    def test_order_failure_rolls_back_decision_and_input_together(self):
        with self.store.db:self.store.db.execute("CREATE TRIGGER fail_order BEFORE INSERT ON paper_orders BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.slot()
        self.assertEqual(self.count('decisions'),0);self.assertEqual(self.count('paper_orders'),0)
        self.assertEqual(self.store.db.execute('SELECT input_json FROM slots').fetchone()[0],'{}')

    def test_review_excludes_noops_and_discloses_scope(self):
        self.slot(events='FAILED')
        r=run_review(self.store,self.cfg,end='2026-09-15T19:30:00+08:00',use_model=False,clock=lambda:'2026-09-15T20:00:00+08:00')
        self.assertEqual(r['statistics']['decision_count'],0)
        self.assertEqual(r['statistics']['recording_policy'],'OPERATIONS_ONLY')
        self.assertIn('不代表没有检查',r['statistics']['recording_notice'])
        facts=json.loads(self.store.db.execute('SELECT payload_json FROM reviews').fetchone()[0])['facts']
        self.assertEqual(facts['slot_inputs'],[])

    def test_job_result_does_not_archive_noop_checks(self):
        cfgpath=self.store.root/'config.json';cfgpath.write_text(json.dumps(self.cfg))
        jid=enqueue(self.store,'slot',self.at,'test-minute-job')
        job=dict(self.store.db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
        runner=Scheduler.__new__(Scheduler);runner.config_path=cfgpath
        with patch('ashare.scheduler.execute',side_effect=lambda *a,**k:self.slot(events='FAILED')):runner.work(job)
        row=self.store.db.execute('SELECT status,result_json FROM jobs WHERE id=?',(jid,)).fetchone()
        self.assertEqual(row['status'],'DONE');self.assertEqual(json.loads(row['result_json'])['decisions'],[])
        self.assertEqual(json.loads(row['result_json'])['operation_count'],0)


if __name__=='__main__':unittest.main()
