"""Synthetic causal replay checks; no operator config, data, model, or network."""
import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ashare import backtest, portfolio_history as history, portfolio_strategy as portfolio, shadow
from ashare.calendar import trading_day
from ashare.storage import Store, normalize_time


CONFIG = {'paper_entry_band_bps': 200, 'paper_stop_loss_bps': 600, 'paper_take_profit_bps': 1000,
          'paper_slippage_bps': 0, 'paper_max_stock_pct': 20, 'paper_max_gross_pct': 90,
          'paper_min_fee_cents': 500, 'paper_commission_bps': 3, 'paper_sell_tax_bps': 5}
AT = '2026-09-15T01:30:00+00:00'


class OpeningTests(unittest.TestCase):
    def test_opening_target_is_not_overridden_by_later_stop(self):
        self.assertEqual(shadow.exit_price(940, 1100, 1200, 1250, 900), (1200, 'TARGET'))
        self.assertEqual(shadow.exit_price(940, 1100, 900, 1250, 850), (900, 'STOP'))
        self.assertEqual(shadow.exit_price(940, 1100, 1000, 1250, 900), (940, 'STOP'))

    def test_both_backtests_respect_opening_target(self):
        bars = [[f'{i:03d}', p, p, p, p] for i, p in enumerate([10] * 40 + [11] * 20)]
        bars += [['060', 11, 11, 11, 11], ['061', 12.5, 11, 13, 10]]
        trade = backtest.simulate_ma(bars, backtest.MA_VARIANTS['baseline'])[0]
        self.assertEqual((trade['exit'], trade['reason']), (12.5, 'TARGET'))
        bars = [[f'{i:03d}', p, p, p, p] for i, p in enumerate([10] * 19 + [11])]
        bars += [['020', 11, 11, 11, 11], ['021', 12.5, 11, 13, 10]]
        trade = backtest.simulate_global(bars, {'max_hold': 5})[0]
        self.assertEqual((trade['exit'], trade['reason']), (12.5, 'TARGET'))

    def test_known_trend_exit_precedes_intraday_prices(self):
        bars = [[f'{i:03d}', p, p, p, p] for i, p in enumerate([10] * 40 + [11] * 20)]
        bars += [['060', 11, 8, 11, 8], ['061', 10.8, 10.8, 13, 9]]
        trade = backtest.simulate_ma(bars, backtest.MA_VARIANTS['trend_exit'])[0]
        self.assertEqual((trade['exit'], trade['reason']), (10.8, 'TREND_EXIT'))

    def test_new_backtest_outputs_never_overwrite_an_existing_run(self):
        with tempfile.TemporaryDirectory() as root:
            result = backtest.run_ma(root, CONFIG, [], '2026-01-01', '2026-02-01')
            first = backtest.save(root, result)
            before = (first / 'report.md').read_bytes()
            second = backtest.save(root, result)
            self.assertNotEqual(first, second)
            self.assertIn(backtest.MA_METHOD, second.name)
            self.assertEqual((first / 'report.md').read_bytes(), before)


class SourceFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        with self.store.db:
            self.store.db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?)',
                ('synthetic-snapshot', None, 'sh600000', AT, AT, 'synthetic', '{}'))
            self.store.db.execute('INSERT INTO studies VALUES(?,?,?,?,?,?)',
                ('synthetic-study', 'synthetic-snapshot', 'sh600000', AT, 'OK', '{}'))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def plan(self, identity='p1', symbol='sh600000', at='2026-09-15T00:00:00+00:00', status='ACTIVE', levels=None):
        with self.store.db:
            if status == 'ACTIVE':
                for old in self.store.db.execute("SELECT id FROM plans WHERE symbol=? AND status='ACTIVE'", (symbol,)).fetchall():
                    self.store.db.execute("UPDATE plans SET status='SUPERSEDED' WHERE id=?", (old[0],))
                    self.store.db.execute('INSERT INTO plan_events(plan_id,at,status,reason) VALUES(?,?,?,?)',
                                          (old[0], at, 'SUPERSEDED', identity))
            self.store.db.execute('INSERT INTO plans VALUES(?,?,?,?,?,?,?,?)', (identity, 'synthetic-study', symbol,
                at, '2099-01-01T00:00:00+00:00', status, 'synthetic', json.dumps(
                    {'kind': 'PAPER_TRADE', 'levels': levels or {}, 'blockers': []})))
            self.store.db.execute('INSERT INTO plan_events(plan_id,at,status,reason) VALUES(?,?,?,?)',
                                  (identity, at, status, 'synthetic'))
        return self.item('watchlist', symbol)

    def item(self, route, identity):
        source = portfolio.source(self.store, route, identity)
        return {'key': route + ':' + identity, 'route': route, 'identity': identity,
            'symbol': source['symbol'] if source else identity, 'source_id': source['id'] if source else None,
            'source_token': portfolio.row_token(source), 'reference_id': 'research:' + source['id'] if source else 'missing',
            'related_keys': [], 'evidence_ids': ['research:' + source['id']] if source else [],
            'membership_token': 'legacy', 'action': 'ALLOW', 'target_bps': 1500}

    def decision(self, items, at='2026-09-15T01:00:00+00:00', identity='pd1', groups=None, forward=False):
        payload = {'decisions': items, 'risk_groups': groups or []}
        with self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            if forward:
                history.ensure(self.store)
                history.freeze(self.store, items, identity)
            self.store.db.execute("UPDATE portfolio_decisions SET status='SUPERSEDED' WHERE status='ACTIVE'")
            self.store.db.execute('INSERT INTO portfolio_decisions VALUES(?,?,?,?,?,?)', (identity, at,
                '2099-01-01T00:00:00+00:00', 'ACTIVE', portfolio.VERSION, json.dumps(payload)))
        return payload

    def checks(self, at=AT):
        checks = []
        accepted = shadow.decision_at(self.store, at, checks)
        return accepted, checks


class HistoricalAuthorizationTests(SourceFixture):
    def test_replaced_source_is_rejected_but_future_supersession_is_not(self):
        item = self.plan()
        self.decision([item])
        self.plan('future-plan', at='2026-09-15T02:00:00+00:00')
        accepted, checks = self.checks()
        self.assertEqual(accepted, {})
        # Today's SUPERSEDED does not invalidate the old source. Its separate
        # missing publication upper bound remains explicit rather than assumed.
        self.assertEqual(checks[0]['reason'], 'LEGACY_PUBLICATION_TIME_UNPROVEN')
        with self.store.db:
            self.store.db.execute("UPDATE plans SET activated_at='2026-09-15T01:20:00+00:00' WHERE id='future-plan'")
            self.store.db.execute("UPDATE plan_events SET at='2026-09-15T01:20:00+00:00' WHERE at='2026-09-15T02:00:00+00:00'")
        accepted, checks = self.checks()
        self.assertEqual(accepted, {})
        self.assertEqual(checks[0]['reason'], 'SOURCE_ID_CHANGED')

    def test_new_draft_is_a_new_source_even_though_the_old_active_plan_remains(self):
        item = self.plan()
        self.decision([item])
        self.plan('draft', at='2026-09-15T01:20:00+00:00', status='DRAFT')
        self.assertEqual(shadow.plan_at(self.store, item['identity'], AT)['id'], 'p1')
        self.assertEqual(self.checks()[1][0]['reason'], 'SOURCE_ID_CHANGED')

    def dependency_case(self, mode):
        a = self.plan()
        b = self.plan('other', 'sh600001')
        c = self.plan('unrelated', 'sh600002')
        groups = []
        if mode == 'related':
            a['related_keys'] = [b['key']]
        elif mode == 'evidence':
            a['evidence_ids'].append(b['reference_id'])
        else:
            groups = [{'name': 'synthetic', 'keys': [a['key'], b['key']], 'max_bps': 3000}]
        self.decision([a, b, c], groups=groups)
        self.plan('changed', 'sh600001', '2026-09-15T01:20:00+00:00')
        accepted, checks = self.checks()
        self.assertNotIn(a['key'], accepted)
        self.assertEqual(next(x for x in checks if x['key'] == c['key'])['reason'], 'LEGACY_PUBLICATION_TIME_UNPROVEN')
        self.assertEqual(next(x for x in checks if x['key'] == a['key'])['status'], 'INVALIDATED')

    def test_related_dependency_is_checked(self):
        self.dependency_case('related')

    def test_evidence_dependency_is_checked(self):
        self.dependency_case('evidence')

    def test_risk_group_dependency_is_checked(self):
        self.dependency_case('risk')

    def test_latest_expired_portfolio_never_revives_an_older_authorization(self):
        item = self.plan()
        self.decision([item])
        self.decision([item], '2026-09-15T01:10:00+00:00', 'newer')
        with self.store.db:
            self.store.db.execute("UPDATE portfolio_decisions SET valid_until='2026-09-15T01:20:00+00:00' WHERE id='newer'")
        self.assertEqual(self.checks()[0], {})
        self.assertEqual(self.checks()[1][0]['reason'], 'PORTFOLIO_EXPIRED')

    def test_legacy_dynamic_dependency_is_insufficient_not_wait(self):
        a = self.plan()
        dynamic = {'key': 'dynamic:case', 'route': 'dynamic', 'identity': 'case', 'source_id': 'case',
            'source_token': 'unprovable', 'reference_id': 'research:case', 'related_keys': [], 'evidence_ids': []}
        a['related_keys'] = [dynamic['key']]
        self.decision([a, dynamic])
        accepted, checks = self.checks()
        self.assertEqual(accepted, {})
        self.assertEqual((checks[0]['status'], checks[0]['reason']), ('INSUFFICIENT', 'DYNAMIC_SOURCE_HISTORY_MISSING'))

    def test_replay_holds_one_snapshot_for_evidence_and_book_rows(self):
        other = Store(self.temp.name)
        other.db.execute('PRAGMA busy_timeout=0')
        original = shadow._run_locked

        def locked(*args):
            self.assertTrue(self.store.db.in_transaction)
            with self.assertRaises(sqlite3.OperationalError):
                other.db.execute('BEGIN IMMEDIATE')
            return original(*args)

        try:
            with patch.object(shadow, '_run_locked', side_effect=locked):
                result = shadow.run(self.store, {**CONFIG, 'watchlist': [{'symbol': 'sh600000'}],
                    'shadow_start_date': '2026-09-15'}, '2026-09-15T12:00:00+00:00')
            self.assertEqual(result['status'], 'WAITING_DATA')
        finally:
            other.close()

    def test_new_replay_preserves_old_pointer_rows_and_explains_missing_proof(self):
        day = '2026-09-15'
        dates, cursor = [], date.fromisoformat(day)
        while len(dates) < 61:
            if trading_day(cursor):
                dates.append(cursor.isoformat())
            cursor -= timedelta(days=1)
        dates.reverse()
        bars = [[d, p, p, p + .1, p - .1] for i, d in enumerate(dates) for p in [10 + i / 100]]
        data = {d: tuple(int(round(v * 100)) for v in (o, c, h, l)) for d, o, c, h, l in bars}
        cfg = {**CONFIG, 'watchlist': [{'symbol': 'sh600000'}], 'shadow_start_date': day}
        levels = shadow.indicators(data, day, cfg)['levels']
        self.plan(levels=levels)
        self.decision([{'key': 'watchlist:sh600000', 'action': 'ALLOW', 'target_bps': 1500}])
        with self.store.db:
            self.store.db.execute('CREATE TABLE market_features(run_id TEXT,symbol TEXT,created_at TEXT,raw_path TEXT,payload TEXT,PRIMARY KEY(run_id,symbol))')
            self.store.db.execute('INSERT INTO market_features VALUES(?,?,?,?,?)',
                ('synthetic', 'sh600000', '2026-09-15T12:00:00+00:00', '', json.dumps({'unadjusted': {'bars': bars}})))
            self.store.db.execute('INSERT INTO shadow_evaluations VALUES(?,?,?,?)',
                ('old-run', 'opening-evidence-v2', AT, '{"old":"unchanged"}'))
            self.store.db.execute("INSERT INTO service_state VALUES('shadow_current_v2','old-run')")
            self.store.db.execute('INSERT INTO shadow_days_v2 VALUES(?,?,?,?)', ('old-run', 'C-lot', day, '{"old":true}'))
        old = [tuple(r) for r in self.store.db.execute("SELECT * FROM shadow_days_v2 WHERE run_id='old-run'")]
        result = shadow.run(self.store, cfg, '2026-09-15T12:00:00+00:00')
        self.assertEqual(result['status'], 'INSUFFICIENT')
        self.assertEqual(result['authorization_checks']['reasons'], {'SOURCE_PROOF_MISSING': 1})
        self.assertIn('不是研究WAIT', result['limitations'])
        self.assertEqual(self.store.db.execute("SELECT value FROM service_state WHERE key='shadow_current_v2'").fetchone()[0], 'old-run')
        self.assertEqual(old, [tuple(r) for r in self.store.db.execute("SELECT * FROM shadow_days_v2 WHERE run_id='old-run'")])
        self.assertEqual(shadow.status(self.store)['method'], 'opening-evidence-v3')
        self.assertEqual(shadow.run(self.store, cfg, '2026-09-15T12:00:00+00:00')['days'], [])


class ForwardHistoryTests(SourceFixture):
    def setUp(self):
        super().setUp()
        # SQLite trigger timestamps remain real UTC. Only observation clocks are
        # injected: publication is safely before all writes; cutoff is after them.
        wall = datetime.now(timezone.utc)
        self.base = wall - timedelta(minutes=5)
        self.cutoff = (wall + timedelta(minutes=1)).isoformat(timespec='milliseconds')
        self.item_a = self.plan(at=(self.base - timedelta(minutes=1)).isoformat())
        with self.store.db:
            self.store.db.execute('''INSERT INTO dynamic_news(
                id,source,url,published_at,first_seen_at,title,body,cluster_id,raw_path)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                ('news', 'synthetic', '', self.base.isoformat(), self.base.isoformat(),
                 'synthetic', '', 'synthetic-cluster', 'synthetic'))
            self.store.db.execute('INSERT INTO dynamic_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('case', 'news', 'sh600001', 'synthetic', 'theme', 'event', 'BULLISH',
                 (self.base - timedelta(minutes=1)).isoformat(), '2099-01-01T00:00:00+00:00',
                 'FORWARD', 'READY', '{}', '{"revision":1}'))
        self.dynamic = self.item('dynamic', 'case')
        self.item_a['related_keys'] = [self.dynamic['key']]
        with patch.object(history, '_clock', return_value=self.base.isoformat(timespec='milliseconds')):
            self.payload = self.decision([self.item_a, self.dynamic], self.base.isoformat(), forward=True)
        self.observe(self.base + timedelta(seconds=1))

    def observe(self, at):
        with patch.object(history, '_clock', return_value=at.isoformat(timespec='milliseconds')):
            history.observe(self.store)

    def finish_observation(self):
        self.observe(datetime.fromisoformat(self.cutoff) + timedelta(seconds=1))

    def update_dynamic(self, revision=2):
        with self.store.db:
            self.store.db.execute('UPDATE dynamic_cases SET plan_json=? WHERE id=?',
                                  (json.dumps({'revision': revision}), 'case'))

    def test_unchanged_dynamic_source_requires_continuous_observation(self):
        self.assertEqual(self.checks(self.cutoff)[1][0]['status'], 'INSUFFICIENT')
        self.finish_observation()
        self.assertIn(self.item_a['key'], self.checks(self.cutoff)[0])

    def test_fixed_list_does_not_use_a_stale_industry_membership(self):
        with self.store.db:
            self.store.db.execute('''INSERT INTO industry_memberships(
                symbol,at,membership,tier,buy_eligible,fingerprint,payload_json)
                VALUES(?,?,?,?,?,?,?)''',
                ('sh600000', self.base.isoformat(), 'CORE', 'CORE', 1, 'old-industry-fingerprint', '{}'))
        self.finish_observation()
        self.assertIn(self.item_a['key'], self.checks(self.cutoff)[0])

    def test_missing_publication_receipt_cannot_be_retroactively_assumed(self):
        with self.store.db:
            self.store.db.execute("DELETE FROM portfolio_history_publications WHERE decision_id='pd1'")
        self.finish_observation()
        self.assertEqual(self.checks(self.cutoff)[1][0]['reason'], 'SOURCE_PUBLICATION_TIME_UNPROVEN')

    def test_missing_snapshot_never_regains_authorization_through_legacy(self):
        with self.store.db:
            self.store.db.execute("DELETE FROM portfolio_history_snapshots WHERE decision_id='pd1'")
        self.finish_observation()
        accepted, checks = self.checks(self.cutoff)
        self.assertEqual(accepted, {})
        self.assertEqual(checks[0]['status'], 'INSUFFICIENT')

    def test_empty_snapshot_cannot_downgrade_to_legacy_validation(self):
        with self.store.db:
            self.store.db.execute("UPDATE portfolio_history_snapshots SET history_json='{}' WHERE decision_id='pd1'")
        # Keep the valid receipt; the malformed snapshot itself must fail closed.
        self.finish_observation()
        self.assertEqual(self.checks(self.cutoff)[1][0]['status'], 'INSUFFICIENT')

    def test_failed_log_write_aborts_source_write(self):
        before = tuple(self.store.db.execute("SELECT * FROM dynamic_cases WHERE id='case'").fetchone())
        with self.store.db:
            self.store.db.execute('''CREATE TRIGGER fail_history BEFORE INSERT ON portfolio_source_events
                BEGIN SELECT RAISE(ABORT,'synthetic history storage failure'); END''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'synthetic history storage failure'):
            self.update_dynamic()
        self.assertEqual(tuple(self.store.db.execute("SELECT * FROM dynamic_cases WHERE id='case'").fetchone()), before)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM portfolio_source_events').fetchone()[0], 0)

    def test_publication_rollback_does_not_leave_a_frozen_snapshot(self):
        with self.assertRaisesRegex(RuntimeError, 'synthetic rollback'):
            with self.store.db:
                self.store.db.execute('BEGIN IMMEDIATE')
                history.freeze(self.store, [self.item_a, self.dynamic], 'rolled-back')
                self.store.db.execute('INSERT INTO portfolio_decisions VALUES(?,?,?,?,?,?)',
                    ('rolled-back', self.base.isoformat(), self.cutoff, 'ACTIVE', portfolio.VERSION, '{}'))
                raise RuntimeError('synthetic rollback')
        self.assertIsNone(history.snapshot(self.store, 'rolled-back'))
        self.assertIsNone(self.store.db.execute("SELECT * FROM portfolio_decisions WHERE id='rolled-back'").fetchone())

    def test_same_second_change_then_reversion_does_not_restore_permission(self):
        self.update_dynamic(2)
        with self.store.db:
            self.store.db.execute("UPDATE dynamic_cases SET plan_json='{\"revision\":1}' WHERE id='case'")
            # Equal statement-clock values are intentional; seq establishes order.
            self.store.db.execute('UPDATE portfolio_source_events SET recorded_at=?',
                                  ((self.base + timedelta(seconds=30)).isoformat(timespec='milliseconds'),))
        self.observe(datetime.fromisoformat(self.cutoff) - timedelta(seconds=1))
        self.finish_observation()
        self.assertEqual(self.checks(self.cutoff)[1][0]['reason'], 'SOURCE_CHANGED_SINCE_DECISION')

    def test_delete_and_identical_reinsert_is_not_unchanged_history(self):
        original = tuple(self.store.db.execute("SELECT * FROM dynamic_cases WHERE id='case'").fetchone())
        with self.store.db:
            self.store.db.execute("DELETE FROM dynamic_cases WHERE id='case'")
            self.store.db.execute('INSERT INTO dynamic_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', original)
        self.observe(datetime.fromisoformat(self.cutoff) - timedelta(seconds=1))
        self.finish_observation()
        self.assertEqual(self.checks(self.cutoff)[1][0]['reason'], 'SOURCE_CHANGED_SINCE_DECISION')

    def test_commit_visibility_uncertainty_is_not_backdated(self):
        self.update_dynamic()
        self.finish_observation()  # The first visibility bound is after the cutoff.
        self.assertEqual(self.checks(self.cutoff)[1][0]['reason'], 'SOURCE_COMMIT_TIME_UNPROVEN')

    def test_update_at_or_after_cutoff_does_not_leak_into_opening(self):
        self.update_dynamic()
        with self.store.db:
            self.store.db.execute('UPDATE portfolio_source_events SET recorded_at=?', (self.cutoff,))
        self.finish_observation()
        self.assertIn(self.item_a['key'], self.checks(self.cutoff)[0])

    def test_missing_trigger_and_new_epoch_cannot_bridge_a_gap(self):
        with self.store.db:
            self.store.db.execute('DROP TRIGGER portfolio_history_dynamic_cases_update')
        self.assertEqual(self.checks(self.cutoff)[1][0]['reason'], 'SOURCE_HISTORY_CONTINUITY_UNKNOWN')
        with self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            history.ensure(self.store)
        self.assertEqual(self.checks(self.cutoff)[1][0]['reason'], 'SOURCE_HISTORY_CONTINUITY_UNKNOWN')

    def test_other_connection_rollback_does_not_create_a_phantom_version(self):
        other = Store(self.temp.name)
        before = self.store.db.execute('SELECT count(*) FROM portfolio_source_events').fetchone()[0]
        try:
            other.db.execute('BEGIN IMMEDIATE')
            other.db.execute("UPDATE dynamic_cases SET plan_json='{\"revision\":2}' WHERE id='case'")
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM portfolio_source_events').fetchone()[0], before)
            other.db.rollback()
        finally:
            other.close()
        self.finish_observation()
        self.assertIn(self.item_a['key'], self.checks(self.cutoff)[0])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM portfolio_source_events').fetchone()[0], before)

    def test_snapshot_cursor_and_rows_are_frozen_under_one_write_lock(self):
        with self.assertRaises(RuntimeError):
            history.freeze(self.store, [self.item_a, self.dynamic], 'unlocked')
        other = Store(self.temp.name)
        other.db.execute('PRAGMA busy_timeout=0')
        try:
            with self.store.db:
                self.store.db.execute('BEGIN IMMEDIATE')
                frozen = history.freeze(self.store, [self.item_a, self.dynamic], 'locked')
                with self.assertRaises(sqlite3.OperationalError):
                    other.db.execute('BEGIN IMMEDIATE')
                self.assertEqual(frozen['sources'][self.dynamic['key']]['record']['plan_json'], '{"revision":1}')
                self.assertEqual(frozen['cursor'], self.store.db.execute('SELECT coalesce(max(seq),0) FROM portfolio_source_events').fetchone()[0])
        finally:
            other.close()


if __name__ == '__main__':
    unittest.main()
