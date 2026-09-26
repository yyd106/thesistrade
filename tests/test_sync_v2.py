import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import test_cloud_sync as fixtures
from ashare.storage import Store, normalize_time
from ashare import cloud_runtime as runtime, cloud_ledger as ledger, cloud_sync as sync, portfolio_strategy as ps, global_paper, maintenance
from ashare.cloud_protocol import canonical


class SyncV2Tests(unittest.TestCase):
    quote = fixtures.CloudSyncTests.quote
    plan = fixtures.CloudSyncTests.plan
    later = fixtures.CloudSyncTests.later
    output = fixtures.CloudSyncTests.output
    transact = fixtures.CloudSyncTests.transact
    publication = fixtures.CloudSyncTests.publication
    receive = fixtures.CloudSyncTests.receive

    def setUp(self):fixtures.CloudSyncTests.setUp(self)
    def tearDown(self):fixtures.CloudSyncTests.tearDown(self)

    def publish(self, display, at=None):
        at = at or self.at
        result = ps.run(self.store, self.cfg, at, model_fn=lambda p, *a: self.output(json.loads(p.split('<UNTRUSTED_INPUT>')[1].split('</UNTRUSTED_INPUT>')[0])), clock=lambda: at)
        self.assertEqual(result['status'], 'SUCCEEDED', result)
        with patch('ashare.cloud_sync.display_packet', return_value=display), patch('ashare.cloud_sync.now', return_value=at):
            sync.queue_publication(self.store, self.cfg, result['decision_id'])
        return json.loads(self.store.db.execute('SELECT payload_json FROM cloud_outbox WHERE id=?', (result['decision_id'],)).fetchone()[0])

    def export(self, at, limit=1500):
        body = {'cursors': runtime.value(self.store, 'ledger_cursors', {}), 'protocol': 2,
                'change_cursor': runtime.value(self.store, 'ledger_change_cursor'), 'support_since': runtime.value(self.store, 'ledger_support_since')}
        return self.transact(lambda: sync.handle(self.cloud, self.cloud_cfg, '/api/sync/ledger', body, at))

    def pull(self, at, limit=1500):
        while True:
            body = {'cursors': runtime.value(self.store, 'ledger_cursors', {}), 'protocol': 2,
                    'change_cursor': runtime.value(self.store, 'ledger_change_cursor'), 'support_since': runtime.value(self.store, 'ledger_support_since')}
            packet = self.transact(lambda: ledger.export_ledger(self.cloud, body['cursors'], at, limit=limit, body=body))
            ledger.import_ledger(self.store, self.cfg, packet)
            if not packet['more']:
                return packet

    def cloud_fill(self):
        body = self.publication(overrides={'global:BTC': {'action': 'ALLOW', 'target_bps': 100}});self.receive(body)
        from ashare.global_market import latest, targets
        from ashare.global_research import active_plan
        q = latest(self.cloud, 'BTC', self.at);p = active_plan(self.cloud, 'BTC', self.at);items = targets(self.cloud, self.cloud_cfg, self.at)
        o = global_paper.submit(self.cloud, self.cloud_cfg, 'BTC', 'BUY', q, p, items['BTC'], self.at)
        self.assertEqual(o.get('status'), 'OPEN', o)
        original = self.store;self.store = self.cloud;self.quote(at=self.later(60));self.store = original
        self.assertEqual(len(global_paper.settle(self.cloud, self.cloud_cfg, self.later(60), items)), 1)

    def test_research_verifies_held_dividends_only_after_the_cloud_advertises_credits(self):
        from ashare import dividends
        self.assertFalse(dividends.supported(self.store, self.cfg))  # an older cloud: held shares stay blocked
        with self.cloud.db:  # what dividends.apply writes on the cloud
            self.cloud.db.execute('INSERT INTO paper_flows VALUES(?,?,?,?,?,?)', ('f1', 'DEMO_PAPER', 'CASH_DIVIDEND', 50200,
                                  'cash_dividend:sh600519:2026-09-30:2026-09-29:200:2.51', self.later(10)))
            self.cloud.db.execute("UPDATE paper_accounts SET cash_cents=cash_cents+50200 WHERE id='DEMO_PAPER'")
        self.pull(self.later(20))
        self.assertTrue(dividends.supported(self.store, self.cfg))
        self.assertEqual(ledger.version(self.store), ledger.version(self.cloud))
        self.assertEqual(dividends.history(self.store)[0]['amount_cents'], 50200)

    def test_first_pull_is_full_then_only_changes_travel(self):
        first = self.export(self.at)
        self.assertEqual(first['protocol'], 2);self.assertTrue(first['full_mutable']);self.assertIn('ledger_v2', first['features'])
        ledger.import_ledger(self.store, self.cfg, first)
        self.assertEqual(runtime.value(self.store, 'remote_features'), list(ledger.FEATURES))
        quiet = self.export(self.later(1))
        self.assertFalse(quiet['full_mutable'])
        self.assertEqual(sum(len(v) for v in quiet['mutable'].values()), 0)
        ledger.import_ledger(self.store, self.cfg, quiet)
        with self.cloud.db:self.cloud.db.execute('UPDATE paper_accounts SET cash_cents=cash_cents-100')
        changed = self.export(self.later(2))
        self.assertEqual({t: len(v) for t, v in changed['mutable'].items() if v}, {'paper_accounts': 1})
        ledger.import_ledger(self.store, self.cfg, changed)
        self.assertEqual(ledger.version(self.store), ledger.version(self.cloud))
        self.assertLess(len(canonical(changed)), len(canonical(self.transact(lambda: ledger.export_ledger(self.cloud, {}, self.later(3))))))

    def test_paged_incremental_pull_keeps_foreign_keys_and_matches_cloud(self):
        self.pull(self.at)
        self.cloud_fill()
        self.pull(self.later(60), limit=1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM global_fills').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(ledger.version(self.store), ledger.version(self.cloud))
        from ashare.review_portfolio import build
        review = build(self.store, self.cfg, self.later(-1), self.later(120), self.later(120))
        self.assertEqual(review['positions'][0]['symbol'], 'BTC')

    def test_stale_or_pruned_change_cursor_falls_back_to_full(self):
        self.pull(self.at)
        with self.store.db:runtime.put(self.store, 'ledger_change_cursor', 10**9)
        self.assertTrue(self.export(self.later(1))['full_mutable'])

    def test_targeted_invalidation_pauses_only_dependent_keys(self):
        self.quote('ETH');self.plan('ETH')
        body = self.publication(overrides={'global:BTC': {'action': 'ALLOW', 'target_bps': 100}, 'global:ETH': {'action': 'ALLOW', 'target_bps': 100}})
        self.receive(body)
        self.assertIsNotNone(ps.decision(self.cloud, self.cloud_cfg, 'global', 'ETH', self.later(5)))
        answer = self.transact(lambda: sync.handle(self.cloud, self.cloud_cfg, '/api/sync/invalidate', {'changed_at': self.later(5), 'keys': ['global:BTC']}, self.later(5)))
        self.assertEqual(answer['scope'], 'KEYS')
        self.assertIsNone(ps.decision(self.cloud, self.cloud_cfg, 'global', 'BTC', self.later(6)))
        self.assertIsNotNone(ps.decision(self.cloud, self.cloud_cfg, 'global', 'ETH', self.later(6)))
        # Without keys the old behaviour stays: everything pauses until the next decision.
        self.transact(lambda: sync.handle(self.cloud, self.cloud_cfg, '/api/sync/invalidate', {'changed_at': self.later(7)}, self.later(7)))
        self.assertIsNone(ps.decision(self.cloud, self.cloud_cfg, 'global', 'ETH', self.later(8)))

    def test_changed_keys_empty_when_research_did_not_change(self):
        ps.run(self.store, self.cfg, self.at, model_fn=lambda p, *a: self.output(json.loads(p.split('<UNTRUSTED_INPUT>')[1].split('</UNTRUSTED_INPUT>')[0])), clock=lambda: self.at)
        self.assertEqual(ps.changed_keys(self.store, self.cfg, self.at), [])
        self.plan('BTC', at=self.later(10))
        self.assertEqual(ps.changed_keys(self.store, self.cfg, self.later(11)), ['global:BTC'])

    def test_display_delta_sends_only_changed_sections_and_self_heals(self):
        display = {'watchlist': [{'symbol': 'sh600519', 'plan': {'id': 'p1'}, 'quote': {'price_cents': 1}}], 'reviews': [{'id': 'r1'}], 'followups': {'items': []}}
        with self.store.db:runtime.put(self.store, 'remote_features', list(ledger.FEATURES))
        first = self.publish(display)
        self.assertIsNone(first['display']);self.assertEqual(len(first['display_delta']['sections']), 4)
        answer = self.receive(first)
        self.assertEqual(runtime.value(self.cloud, 'display')['watchlist'][0], {'symbol': 'sh600519', 'plan': {'id': 'p1'}})
        with self.store.db:runtime.put(self.store, 'remote_display_hashes', answer['display_hashes'])
        changed = copy.deepcopy(display);changed['reviews'] = [{'id': 'r2'}]
        second = self.publish(changed, self.later(10))
        self.assertEqual(list(second['display_delta']['sections']), ['top:reviews'])
        self.receive(second, self.later(10))
        stored = runtime.value(self.cloud, 'display')
        self.assertEqual(stored['reviews'], [{'id': 'r2'}]);self.assertEqual(stored['watchlist'][0]['plan'], {'id': 'p1'})

    def test_new_strategy_time_alone_resends_nothing(self):
        # Every strategy version stamps each asset; the cloud recomputes that stamp, so it must not count as a change.
        def display(stamp):
            return {'watchlist': [{'symbol': 'sh600519', 'plan': {'id': 'p1'}, 'last_strategy_updated_at': stamp}],
                    'observation': {'items': [{'asset': 'BTC', 'role': 'CORE', 'last_strategy_updated_at': stamp}]}, 'reviews': []}
        with self.store.db:runtime.put(self.store, 'remote_features', list(ledger.FEATURES))
        first = self.publish(display(self.at))
        answer = self.receive(first)
        with self.store.db:runtime.put(self.store, 'remote_display_hashes', answer['display_hashes'])
        second = self.publish(display(self.later(55)), self.later(55))
        self.assertEqual(list(second['display_delta']['sections']), [])
        self.receive(second, self.later(55))
        stored = runtime.value(self.cloud, 'display')
        self.assertEqual(stored['watchlist'][0]['plan'], {'id': 'p1'});self.assertNotIn('last_strategy_updated_at', stored['watchlist'][0])
        self.assertEqual(stored['observation']['items'][0]['asset'], 'BTC')

    def test_old_cloud_gets_full_display(self):
        with self.store.db:runtime.put(self.store, 'remote_features', [])
        body = self.publish({'watchlist': [], 'reviews': []})
        self.assertEqual(body['display'], {'watchlist': [], 'reviews': []});self.assertNotIn('display_delta', body)

    def test_publication_writes_manifest_and_prunes_sent_body(self):
        with patch('ashare.cloud_sync.display_packet', return_value={}):
            body = self.publication()
        folder = Path(self.tmp.name) / 'workflow/cloud-sync' / body['bundle_id']
        self.assertFalse((folder / 'publication.json').exists())
        manifest = json.loads((folder / 'manifest.json').read_text())
        self.assertEqual(manifest['sequence'], body['sequence'])
        with patch('ashare.cloud_sync.request', side_effect=lambda cfg, path, payload: self.receive(payload)), patch('ashare.cloud_sync.now', return_value=self.at):
            sync.flush(self.store, self.cfg)
        row = self.store.db.execute('SELECT status,payload_json FROM cloud_outbox').fetchone()
        self.assertEqual(row['status'], 'SENT')
        marker = json.loads(row['payload_json'])
        self.assertTrue(marker['pruned']);self.assertEqual(marker['sha256'], manifest['sha256'])
        self.assertEqual(maintenance.prune_outbox(self.store), 0)


class MaintenanceTests(unittest.TestCase):
    def test_backup_retention_keeps_recent_hours_and_one_per_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            names = [f'202609{d:02d}T{h:02d}0000Z.sqlite3' for d in range(10, 25) for h in range(0, 24, 6)]
            for n in names:(folder / n).write_bytes(b'x')
            maintenance.backup_retention(folder, hourly_keep=6, daily_keep=7)
            kept = sorted(p.name for p in folder.glob('*.sqlite3'))
            self.assertEqual(kept[-6:], names[-6:])
            self.assertEqual({k[:8] for k in kept}, {f'202609{d:02d}' for d in range(18, 25)})
            self.assertEqual(len(kept), 6 + 5)

    def test_review_sync_sends_only_new_rows(self):
        from test_config import load_config
        from ashare.workflow import execute
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(Path(__file__).resolve().parents[1] / 'config.json')
            cfg.update(data_dir=tmp, deployment_role='research', model_enabled=False, watchlist=[{'symbol': 'sh600000', 'name': '测试'}])
            sent = []
            with patch('ashare.cloud_sync.pull'), patch('ashare.cloud_protocol.request', side_effect=lambda c, p, b: sent.append(b) or {'status': 'ACCEPTED'}), \
                 patch('ashare.cloud_sync.display_packet', return_value={'reviews': []}):
                execute(cfg, 'review', use_model=False, end='2026-09-15T19:30:00+08:00')
                execute(cfg, 'review', use_model=False, end='2026-09-16T19:30:00+08:00')
            self.assertEqual([len(b['reviews']) for b in sent], [1, 1])


if __name__ == '__main__':
    unittest.main()
