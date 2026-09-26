import json
import tempfile
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock, patch
from ashare import connectivity, cloud_runtime as runtime
from ashare.demo import SYMBOL
from ashare.storage import Store, normalize_time

DNS = 'URLError: <urlopen error [Errno 8] nodename nor servname provided, or not known>'


def offline(store, since, error=DNS):
    with store.db:
        runtime.put(store, 'offline_since', normalize_time(since))
        runtime.put(store, 'last_sync', {'at': normalize_time(since), 'status': 'FAILED', 'phase': 'pull', 'error': error})


class ConnectivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_only_a_lasting_local_network_failure_counts_as_offline(self):
        at = '2026-09-26T18:10:00+08:00'
        self.assertIsNone(connectivity.offline_since(self.store, at))
        offline(self.store, '2026-09-26T18:08:30+08:00')
        self.assertIsNone(connectivity.offline_since(self.store, at))  # 90 seconds: still within the grace period
        offline(self.store, '2026-09-26T18:05:00+08:00')
        self.assertEqual(connectivity.offline_since(self.store, at), normalize_time('2026-09-26T18:05:00+08:00'))
        offline(self.store, '2026-09-26T18:05:00+08:00', 'HTTPError: HTTP Error 502: Bad Gateway')
        self.assertIsNone(connectivity.offline_since(self.store, at))  # the cloud answered; this machine is online

    def test_intervals_are_clipped_and_overlaps_counted_once(self):
        root = Path(self.tmp.name)
        connectivity.log_interval(root, 'offline', '2026-09-25T17:30:00+08:00', '2026-09-25T18:30:00+08:00', cause='NETWORK')
        connectivity.log_interval(root, 'pause', '2026-09-25T18:00:00+08:00', '2026-09-25T19:00:00+08:00')
        spans = connectivity.intervals(root, normalize_time('2026-09-25T18:00:00+08:00'), normalize_time('2026-09-26T00:00:00+08:00'))
        self.assertEqual([(s['kind'], s['minutes']) for s in spans], [('offline', 30), ('pause', 60)])
        self.assertEqual(connectivity.overlap_minutes(normalize_time('2026-09-25T17:00:00+08:00'), normalize_time('2026-09-25T20:00:00+08:00'),
                                                      connectivity.intervals(root, normalize_time('2026-09-25T00:00:00+08:00'), normalize_time('2026-09-26T00:00:00+08:00'))), 90)

    def test_collection_stops_before_the_next_stock_and_marks_the_batch_partial(self):
        from ashare.research import collect_batch
        cfg = {'watchlist': [{'symbol': SYMBOL, 'name': '合成测试'}], 'external_news_enabled': False, 'news_url': 'https://example.invalid'}
        ready = []
        with patch('ashare.pipeline.sources', MagicMock()), patch('ashare.inbox.import_inbox'), patch('ashare.market_context.collect_comparisons'), \
                patch('ashare.connectivity.offline_since', return_value=normalize_time('2026-09-26T18:05:00+08:00')):
            with self.assertRaisesRegex(connectivity.Offline, 'OFFLINE'):
                collect_batch(self.store, cfg, 'cycle:test', on_ready=ready.append)
        self.assertEqual(ready, [])
        batch = self.store.db.execute("SELECT b.status,r.status run_status,r.error FROM batches b JOIN runs r ON r.id=b.id WHERE r.job_key='cycle:test'").fetchone()
        self.assertEqual((batch['status'], batch['run_status']), ('PARTIAL', 'PARTIAL'))
        self.assertIn('OFFLINE', batch['error'])

    def test_recovery_after_an_outage_is_logged(self):
        from ashare import cloud_sync
        cfg = {'data_dir': self.tmp.name, 'deployment_role': 'research'}
        def sync(at, error=None):
            with patch('ashare.cloud_sync.now', return_value=normalize_time(at)), patch('ashare.cloud_sync.flush', return_value=None), \
                    patch('ashare.cloud_sync.pull', side_effect=RuntimeError(error) if error else None, return_value={'ledger_version': 'v1'}):
                if error:
                    with self.assertRaises(RuntimeError):
                        cloud_sync.sync_once(cfg)
                else:
                    cloud_sync.sync_once(cfg)
        sync('2026-09-26T18:00:00+08:00', DNS)
        sync('2026-09-26T18:20:00+08:00')
        line = json.loads((Path(self.tmp.name) / 'workflow' / 'service' / 'offline.jsonl').read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual((line['minutes'], line['cause']), (20, 'NETWORK'))


class OfflineSchedulingTests(unittest.TestCase):
    """While offline, work that needs the network waits in the queue; model-free work still runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = json.loads((Path(__file__).resolve().parents[1] / 'config.json').read_text(encoding='utf-8'))
        base.update(data_dir=self.tmp.name, deployment_role='research', scheduler_enabled=True, dynamic_enabled=False,
                    watchlist=[{'symbol': SYMBOL, 'name': '合成测试'}])
        self.path = Path(self.tmp.name) / 'config.json'
        self.path.write_text(json.dumps(base, ensure_ascii=False), encoding='utf-8')
        self.store = Store(self.tmp.name)
        self.stamp = normalize_time('2026-09-26T20:00:00+08:00')  # a Saturday: no trading-day recovery work

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def tick(self):
        from ashare.scheduler import Scheduler, enqueue
        submitted = []

        class Pool:
            def submit(self, fn, job):
                submitted.append(job['kind'])
                done = Future()
                done.set_result(None)
                return done
        sched = Scheduler(str(self.path))
        sched.pool = sched.dynamic_pool = sched.global_pool = Pool()
        sched.last_sync = sched.last_maintenance = sched.last_followups = time.monotonic()
        with patch('ashare.scheduler.now', return_value=self.stamp), patch('ashare.portfolio_strategy.request'), \
                patch('ashare.scheduler.schedule_due'), patch('ashare.scheduler.schedule_reconnected'):
            sched.tick()
        sched.monitor.close()
        return submitted

    def queue(self, kind):
        from ashare.scheduler import enqueue
        return enqueue(self.store, kind, self.stamp, kind + ':test')

    def test_network_work_waits_while_offline_and_runs_after_reconnect(self):
        self.queue('cycle');self.queue('digest');self.queue('portfolio_strategy')
        offline(self.store, '2026-09-26T19:40:00+08:00')
        submitted = self.tick()
        self.assertIn('digest', submitted)
        self.assertNotIn('cycle', submitted);self.assertNotIn('portfolio_strategy', submitted)
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs WHERE id='cycle:test'").fetchone()[0], 'PENDING')
        with self.store.db:runtime.put(self.store, 'offline_since', None)
        self.assertIn('cycle', self.tick())

    def test_a_long_gap_between_ticks_is_logged_as_a_pause(self):
        with self.store.db:self.store.db.execute("INSERT OR REPLACE INTO service_state VALUES('tick_at',?)", (normalize_time('2026-09-26T18:00:00+08:00'),))
        self.tick()
        line = json.loads((Path(self.tmp.name) / 'workflow' / 'service' / 'pauses.jsonl').read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual(line['minutes'], 120)

    def test_a_job_stopped_by_an_outage_is_deferred_not_failed(self):
        from ashare.scheduler import Scheduler
        jid = self.queue('cycle')
        sched = Scheduler(str(self.path))
        with patch('ashare.scheduler.execute', side_effect=connectivity.Offline('OFFLINE: 本机自 x 起断网，本轮在此停止，联网后重做')):
            sched.work({'id': jid, 'kind': 'cycle', 'scheduled_at': self.stamp})
        sched.monitor.close()
        row = self.store.db.execute('SELECT status,error FROM jobs WHERE id=?', (jid,)).fetchone()
        self.assertEqual(row['status'], 'DEFERRED');self.assertTrue(row['error'].startswith('OFFLINE:'))


if __name__ == '__main__':
    unittest.main()
