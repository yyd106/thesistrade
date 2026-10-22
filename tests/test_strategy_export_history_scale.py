"""The 14-day export remains bounded even when years of old history exist."""
import json
import sqlite3
import unittest
from unittest.mock import patch

from ashare.strategy_export import collect


class StrategyExportHistoryScaleTests(unittest.TestCase):
    def test_only_required_baselines_and_publication_records_are_read(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript('''
            CREATE TABLE portfolio_decisions(id TEXT PRIMARY KEY,created_at TEXT,valid_until TEXT,status TEXT,version TEXT,payload_json TEXT);
            CREATE TABLE cloud_outbox(id TEXT PRIMARY KEY,created_at TEXT,status TEXT);
            CREATE TABLE cloud_receipts(sequence INTEGER,bundle_id TEXT,received_at TEXT,completed_at TEXT);
        ''')
        payload = json.dumps({'summary': '历史理由', 'decisions': [], 'risk_groups': []})
        for i in range(30):
            stamp = '2026-10-01T01:%02d:00+00:00' % i
            identity = 'old-%02d' % i
            db.execute('INSERT INTO portfolio_decisions VALUES(?,?,?,?,?,?)',
                       (identity, stamp, stamp, 'SUPERSEDED', 'cross_research_v1', payload))
            db.execute('INSERT INTO cloud_outbox VALUES(?,?,?)', (identity, stamp, 'SENT'))
            db.execute('INSERT INTO cloud_receipts VALUES(?,?,?,?)', (i, identity, stamp, stamp))
        db.execute('INSERT INTO portfolio_decisions VALUES(?,?,?,?,?,?)',
                   ('pending-before', '2026-10-08T15:59:59+00:00', '2026-10-09T01:00:00+00:00', 'SUPERSEDED', 'cross_research_v1', payload))
        db.execute('INSERT INTO cloud_outbox VALUES(?,?,?)', ('pending-before', '2026-10-08T15:59:59+00:00', 'SUPERSEDED'))
        db.execute('INSERT INTO portfolio_decisions VALUES(?,?,?,?,?,?)',
                   ('current', '2026-10-22T01:00:00+00:00', '2026-10-22T13:00:00+00:00', 'ACTIVE', 'cross_research_v1', payload))
        db.execute('INSERT INTO cloud_outbox VALUES(?,?,?)', ('current', '2026-10-22T01:00:00+00:00', 'SENT'))
        # More retained history than the per-window cap is not itself an error.
        with patch('ashare.strategy_export.MAX_ROWS', 8):
            archive = collect(db, '2026-10-22T12:00:00+00:00')
        self.assertEqual(archive['baseline']['record']['id'], 'pending-before')
        self.assertEqual(archive['published_baseline']['record']['id'], 'old-29')
        self.assertEqual([r['record']['id'] for r in archive['decisions']], ['current'])
        self.assertEqual(archive['published_decision_ids'], ['current'])


if __name__ == '__main__':
    unittest.main()
