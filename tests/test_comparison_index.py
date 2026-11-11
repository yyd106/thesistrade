"""Cache lookup order and R1 values survive the additive comparison index."""
import json
import sqlite3
import unittest
from types import SimpleNamespace

from ashare.market_regime import r1
from ashare.migrations import SCHEMA_V2


class ComparisonIndexTests(unittest.TestCase):
    def test_latest_tie_break_and_r1_unchanged_without_temp_sort(self):
        db=sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE comparison_series(run_id TEXT,symbol TEXT,ready_at TEXT,payload_json TEXT,PRIMARY KEY(run_id,symbol))')
        # Same timestamp, different rowid and bars: the final row must win.
        import datetime
        days=[(datetime.date(2026,7,1)+datetime.timedelta(days=i)).isoformat() for i in range(100)]
        for run,price in [('old',100),('new',200)]:
            body=json.dumps({'bars':[{'date':d,'close':price+i} for i,d in enumerate(days)]})
            db.execute('INSERT INTO comparison_series VALUES(?,?,?,?)',(run,'sh000300','2026-11-11T15:00:00+00:00',body))
        at='2026-11-11T16:00:00+00:00'
        query='SELECT rowid,ready_at,payload_json FROM comparison_series WHERE symbol=? AND ready_at<=? ORDER BY ready_at DESC,rowid DESC LIMIT 1'
        args=('sh000300',at)
        before=db.execute(query,args).fetchone()
        before_r1=r1(SimpleNamespace(db=db),at)
        index=next(line for line in SCHEMA_V2.splitlines() if 'CREATE INDEX IF NOT EXISTS comparison_series_symbol_ready' in line)
        db.execute(index)
        self.assertEqual(db.execute(query,args).fetchone(),before)
        self.assertEqual(r1(SimpleNamespace(db=db),at),before_r1)
        plan=' '.join(str(row) for row in db.execute('EXPLAIN QUERY PLAN '+query,args))
        self.assertIn('comparison_series_symbol_ready',plan)
        self.assertNotIn('TEMP B-TREE',plan)
        # market_context uses SELECT * with the same ordering and must share the plan.
        plan=' '.join(str(row) for row in db.execute('EXPLAIN QUERY PLAN '+query.replace('rowid,ready_at,payload_json','*'),args))
        self.assertIn('comparison_series_symbol_ready',plan)
        self.assertNotIn('TEMP B-TREE',plan)
