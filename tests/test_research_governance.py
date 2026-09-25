import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ashare.storage import Store, normalize_time
from ashare import model, governance, build, research
from ashare.demo import seed, research_model, put_quote, SYMBOL
from ashare.research import make_snapshot, study, reusable, renew
from ashare.paper import settle
from ashare.review import run_review
import test_workflow as workflow_fixtures

FAKE_CODEX = r'''#!/bin/bash
if [ "$1" = "login" ]; then echo "Logged in using ChatGPT"; exit 0; fi
out=""; model="cli-default-model"; effort="medium"
while [ $# -gt 0 ]; do
  case "$1" in
    --output-last-message) out="$2"; shift 2;;
    --cd) workdir="$2"; shift 2;;
    -m) model="$2"; shift 2;;
    -c) if [[ "$2" == model_reasoning_effort=* ]]; then effort="${2#model_reasoning_effort=}"; effort="${effort//\"/}"; fi; shift 2;;
    *) shift;;
  esac
done
cat > /dev/null
printf 'OpenAI Codex v9.9.9-test\n--------\nmodel: %s\nreasoning effort: %s\n--------\n' "${FAKE_MODEL_OVERRIDE:-$model}" "$effort" >&2
echo '{"ok":true,"echo":"模型连通"}' > "$out"
printf 'tokens used\n1,234\n' >&2
if [ -n "$FAKE_CD_LOG" ]; then printf '%s\n' "$workdir" > "$FAKE_CD_LOG"; ls -A "$workdir" >> "$FAKE_CD_LOG"; fi
'''


class ModelPinningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / 'codex'
        path.write_text(FAKE_CODEX);path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.env = patch.dict(os.environ, {'PATH': self.tmp.name + os.pathsep + os.environ['PATH']});self.env.start()

    def tearDown(self):
        self.env.stop();model.configure({});self.tmp.cleanup()

    def test_pinned_model_and_effort_are_passed_recorded_and_verified(self):
        model.configure({'model_name': 'gpt-test', 'model_reasoning_effort': 'low'})
        result = model.check(Path(self.tmp.name) / 'call')
        self.assertEqual(result['status'], 'OK')
        meta = json.loads((Path(self.tmp.name) / 'call' / 'meta.json').read_text())
        self.assertEqual((meta['requested_model'], meta['actual_model'], meta['actual_effort']), ('gpt-test', 'gpt-test', 'low'))
        self.assertEqual(meta['cli_version'], '9.9.9-test');self.assertEqual(meta['tokens_used'], 1234)
        self.assertEqual(len(meta['prompt_sha256']), 64)
        self.assertEqual(research.model_record(Path(self.tmp.name) / 'call')['actual_model'], 'gpt-test')

    def test_silently_substituted_model_is_rejected(self):
        model.configure({'model_name': 'gpt-test', 'model_reasoning_effort': 'low'})
        with patch.dict(os.environ, {'FAKE_MODEL_OVERRIDE': 'another-model'}):
            with self.assertRaisesRegex(RuntimeError, '不一致'):model.run_json('x', model.CHECK_SCHEMA, Path(self.tmp.name) / 'swap')
        self.assertEqual(json.loads((Path(self.tmp.name) / 'swap' / 'meta.json').read_text())['actual_model'], 'another-model')

    def test_cli_runs_in_an_empty_directory_outside_the_workspace(self):
        log = Path(self.tmp.name) / 'cd.log'
        with patch.dict(os.environ, {'FAKE_CD_LOG': str(log)}):
            model.run_json('x', model.CHECK_SCHEMA, Path(self.tmp.name) / 'ws' / 'call')
        workdir, *listing = log.read_text().splitlines()
        self.assertEqual(listing, [])
        self.assertFalse(Path(workdir).resolve().is_relative_to(Path(self.tmp.name).resolve()))
        self.assertFalse(Path(workdir).exists())

    def test_unpinned_calls_still_record_what_ran(self):
        model.configure({})
        model.run_json('x', model.CHECK_SCHEMA, Path(self.tmp.name) / 'default')
        self.assertEqual(model.last_meta()['actual_model'], 'cli-default-model')
        with self.assertRaises(ValueError):model.configure({'model_reasoning_effort': 'extreme'})


class BuildIdentityTests(unittest.TestCase):
    def test_build_changes_with_strategy_settings_model_and_guidance_only(self):
        cfg = {'strategy_version': 'paper_baseline_v1', 'paper_entry_band_bps': 200, 'watchlist': [{'symbol': 'sh600000'}], 'collection_times': ['00:00']}
        base = build.info(cfg)['build_id']
        self.assertEqual(build.info({**cfg, 'collection_times': ['06:00']})['build_id'], base)
        self.assertNotEqual(build.info({**cfg, 'paper_entry_band_bps': 100})['build_id'], base)
        self.assertNotEqual(build.info({**cfg, 'model_name': 'gpt-x'})['build_id'], base)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            try:
                before = build.info(cfg, store)['build_id']
                with store.db:
                    store.db.execute("INSERT INTO strategy_guidance VALUES('G-1','watchlist','ALL','只看公司披露的经营变化','ADOPTED',NULL,'2026-09-01T00:00:00+00:00',NULL,'Dean','{}')")
                self.assertNotEqual(build.info(cfg, store)['build_id'], before)
            finally:
                store.close()

    def test_doctor_and_config_changes_report_the_recorded_build(self):
        cfg = {'strategy_version': 'paper_baseline_v1', 'watchlist': [{'symbol': 'sh600000'}]}
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(build.info({**cfg, 'data_dir': tmp})['guidance'], 'none')  # no database yet
            store = Store(tmp)
            try:
                with_dir = {**cfg, 'data_dir': tmp}
                self.assertEqual(build.info(with_dir)['build_id'], build.info(cfg, store)['build_id'])
                with store.db:
                    store.db.execute("INSERT INTO strategy_guidance VALUES('G-1','watchlist','ALL','只看公司披露的经营变化','ADOPTED',NULL,'2026-09-01T00:00:00+00:00',NULL,'Dean','{}')")
                self.assertEqual(build.info(with_dir)['build_id'], build.info(cfg, store)['build_id'])
                self.assertNotEqual(build.info(with_dir)['guidance'], 'none')
            finally:
                store.close()


class ResearchReuseTests(unittest.TestCase):
    def setUp(self):
        from test_config import load_config
        self.tmp = tempfile.TemporaryDirectory();self.store = Store(self.tmp.name)
        self.cfg = load_config(Path(__file__).resolve().parents[1] / 'config.json')
        self.cfg.update(data_dir=self.tmp.name, watchlist=[{'symbol': SYMBOL, 'name': '合成测试'}], model_enabled=True, research_reuse_hours=20)
        self.first = seed(self.store, self.cfg)
        self.done = study(self.store, self.cfg, self.first, model_fn=research_model(self.first), at='2026-09-15T09:01:00+08:00')

    def tearDown(self):
        self.store.close();self.tmp.cleanup()

    def test_unchanged_inputs_renew_the_plan_without_a_model_call(self):
        packet = make_snapshot(self.store, self.cfg, SYMBOL, at='2026-09-15T15:00:00+08:00', persist=False)
        reuse = reusable(self.store, self.cfg, packet)
        self.assertIsNotNone(reuse)
        result = renew(self.store, self.cfg, packet, reuse, at='2026-09-15T15:00:00+08:00')
        self.assertEqual(result['status'], 'REUSED');self.assertEqual(result['study_id'], self.done['study_id'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM studies').fetchone()[0], 1)
        self.assertIsNone(self.store.db.execute('SELECT 1 FROM snapshots WHERE id=?', (packet['snapshot_id'],)).fetchone())
        plan = self.store.db.execute("SELECT * FROM plans WHERE status='ACTIVE'").fetchone()
        payload = json.loads(plan['payload_json'])
        self.assertEqual(plan['id'], result['plan_id']);self.assertEqual(plan['study_id'], self.done['study_id'])
        self.assertEqual(payload['research_reuse']['previous_plan_id'], self.done['plan_id'])
        self.assertEqual(payload['build']['build_id'], build.info(self.cfg, self.store)['build_id'])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM signal_registry WHERE route='watchlist'").fetchone()[0], 2)

    def test_new_evidence_changed_build_or_old_study_force_a_fresh_study(self):
        self.store.add_document(symbol=SYMBOL, kind='news', title='新公告', source='fixture', url='https://example.test/new',
                                published_at='2026-09-15T11:00:00+08:00', first_seen_at='2026-09-15T11:00:00+08:00',
                                ready_at='2026-09-15T11:00:00+08:00', pages=[(1, '公司披露新的经营数据。')], raw_path='synthetic', cloud_allowed=True)
        self.assertIsNone(reusable(self.store, self.cfg, make_snapshot(self.store, self.cfg, SYMBOL, at='2026-09-15T15:00:00+08:00', persist=False)))
        self.cfg['research_reuse_hours'] = 20
        other = {**self.cfg, 'model_name': 'gpt-other'}
        self.assertIsNone(reusable(self.store, other, make_snapshot(self.store, other, SYMBOL, at='2026-09-15T12:00:00+08:00', persist=False)))
        late = make_snapshot(self.store, self.cfg, SYMBOL, at='2026-09-16T08:00:00+08:00', persist=False)
        self.assertIsNone(reusable(self.store, {**self.cfg, 'research_reuse_hours': 12}, late))
        self.assertIsNone(reusable(self.store, {**self.cfg, 'research_reuse_hours': 0}, late))

    def test_workflow_research_round_renews_instead_of_calling_the_model(self):
        from ashare.workflow import execute
        with patch('ashare.workflow.study', side_effect=AssertionError('model research must not run')), \
             patch('ashare.research.now', return_value=normalize_time('2026-09-15T15:00:00+08:00')):
            out = execute(self.cfg, 'research', use_model=True)
        self.assertEqual(out[0]['status'], 'REUSED')


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close();self.tmp.cleanup()

    def test_lifecycle_requires_named_approval_and_retires_guidance(self):
        with self.store.db:
            pid = governance.draft_proposal(self.store, source='agent', kind='RESEARCH_GUIDANCE', target='global', title='非公司资产的研究输入',
                                            payload={'guidance': {'route': 'global', 'scope': 'BTC', 'text': '加密资产不以公司基本面缺失为由等待'}}, at='2026-09-20T00:00:00+00:00')
        with self.assertRaisesRegex(ValueError, '不能从DRAFT变为ADOPTED'):
            governance.decide(self.store, pid, 'ADOPTED', decided_by='Dean', note='跳步', at='2026-09-20T00:01:00+00:00')
        governance.decide(self.store, pid, 'READY', decided_by=None, note='整理完成', at='2026-09-20T00:01:00+00:00')
        with self.assertRaisesRegex(ValueError, '批准人'):
            governance.decide(self.store, pid, 'APPROVED', decided_by=' ', note='代理不能自行批准')
        governance.decide(self.store, pid, 'APPROVED', decided_by='Dean', note='同意', at='2026-09-20T00:02:00+00:00')
        governance.decide(self.store, pid, 'ADOPTED', decided_by='Dean', note='已上线', at='2026-09-20T00:03:00+00:00')
        self.assertEqual(len(governance.guidance(self.store, 'global', 'BTC', '2026-09-20T00:04:00+00:00')), 1)
        self.assertEqual(governance.guidance(self.store, 'global', 'ETH', '2026-09-20T00:04:00+00:00'), [])
        self.assertEqual(governance.guidance(self.store, 'global', 'BTC', '2026-09-20T00:02:30+00:00'), [])
        governance.decide(self.store, pid, 'RETIRED', decided_by='Dean', note='效果不佳', at='2026-09-21T00:00:00+00:00')
        self.assertEqual(governance.guidance(self.store, 'global', 'BTC', '2026-09-22T00:00:00+00:00'), [])

    def test_issues_collapse_by_key_and_reopen_after_resolution(self):
        with self.store.db:
            first = governance.record_issue(self.store, 'QUOTE_RECORDED_AFTER_CUTOFF', 'sh600000', '收盘价事后补齐', ['review:1'], '2026-09-20T00:00:00+00:00')
            again = governance.record_issue(self.store, 'QUOTE_RECORDED_AFTER_CUTOFF', 'sh600000', '同一问题再次出现', ['review:2'], '2026-09-21T00:00:00+00:00')
        self.assertEqual(first, again)
        row = self.store.db.execute('SELECT * FROM engineering_issues').fetchone()
        self.assertEqual(row['occurrences'], 2);self.assertEqual(row['category'], 'DATA')
        governance.resolve_issue(self.store, first, '补录时间改为按入库时间判断', '2026-09-22T00:00:00+00:00')
        with self.store.db:
            governance.record_issue(self.store, 'QUOTE_RECORDED_AFTER_CUTOFF', 'sh600000', '又出现', ['review:3'], '2026-09-23T00:00:00+00:00')
        self.assertEqual(self.store.db.execute('SELECT status FROM engineering_issues').fetchone()[0], 'OPEN')


class ReviewRoutingTests(unittest.TestCase):
    setUp = workflow_fixtures.WorkflowTests.setUp
    tearDown = workflow_fixtures.WorkflowTests.tearDown
    plan = workflow_fixtures.WorkflowTests.plan
    slot = workflow_fixtures.WorkflowTests.slot

    def buy_and_fill(self):
        self.plan();put_quote(self.store, self.at);self.slot(action='BUY')
        put_quote(self.store, '2026-09-15T10:01:00+08:00')
        self.assertEqual(len(settle(self.store, self.cfg, '2026-09-15T10:01:00+08:00')), 1)

    def test_defects_become_issues_observations_become_drafts(self):
        self.buy_and_fill()
        did = self.store.db.execute('SELECT id FROM decisions').fetchone()[0]
        lessons = [{'symbol': SYMBOL, 'category': 'DATA', 'issue_key': 'QUOTE_RECORDED_AFTER_CUTOFF', 'lesson': '收盘价事后补齐',
                    'decision_ids': [did], 'fill_ids': [], 'applicability': '复盘截止后入库的行情'},
                   {'symbol': SYMBOL, 'category': 'RESEARCH', 'issue_key': 'STRATEGY_OBSERVATION', 'lesson': '趋势失效后仍持有',
                    'decision_ids': [did], 'fill_ids': [], 'applicability': '用20日超额收益检验趋势退出'}]
        def model_fn(prompt, *args):
            packet = json.loads(prompt.split('<UNTRUSTED_REVIEW>', 1)[1].split('</UNTRUSTED_REVIEW>', 1)[0])
            self.assertTrue(any(c['check'] == 'CHECK_BUY_OUTSIDE_PLAN_BAND' for c in packet['program_checks']))
            self.assertIn('lessons不会进入任何研究输入', prompt)
            return {'summary': '测试复盘', 'lessons': lessons, 'positions': [{'position_key': p['key'], 'verdict': 'PENDING' if p['research_ids'] else 'INSUFFICIENT',
                    'reason': '测试', 'supported_points': [], 'contradicted_points': [], 'pending_points': [], 'next_check': '测试',
                    'research_ids': p['research_ids']} for p in packet['portfolio']['positions']]}
        # Disk space depends on the machine running the tests; only routing is under test here.
        with patch('ashare.maintenance.disk_status', return_value={'warning': False}):
            review = run_review(self.store, self.cfg, end='2026-09-15T19:30:00+08:00', model_fn=model_fn, clock=lambda: '2026-09-15T20:05:00+08:00')
        self.assertEqual(review['status'], 'SUCCEEDED')
        issues = governance.issues(self.store)
        self.assertEqual([i['issue_key'] for i in issues], ['QUOTE_RECORDED_AFTER_CUTOFF'])
        drafts = governance.proposals(self.store, 'DRAFT')
        self.assertEqual(len(drafts), 1);self.assertEqual(drafts[0]['payload']['observations'][0]['lesson'], '趋势失效后仍持有')
        payload = json.loads(self.store.db.execute('SELECT payload_json FROM reviews').fetchone()[0])
        self.assertEqual([r['to'] for r in payload['routing']], ['engineering_issue', 'proposal_draft'])
        self.assertTrue(payload['build']['build_id'])
        after = make_snapshot(self.store, self.cfg, SYMBOL, at='2026-09-15T20:10:00+08:00')
        self.assertNotIn('趋势失效后仍持有', json.dumps(research.model_packet(after, self.cfg), ensure_ascii=False))

    def test_fill_above_plan_band_is_flagged(self):
        from ashare import review_checks
        self.buy_and_fill()
        fill = self.store.db.execute('SELECT * FROM paper_fills').fetchone()
        plan = self.store.db.execute('SELECT id,payload_json FROM plans WHERE status=\'ACTIVE\'').fetchone()
        payload = json.loads(plan['payload_json']);payload['levels']['buy_high_cents'] = fill['price_cents'] - 1
        with self.store.db:self.store.db.execute('UPDATE plans SET payload_json=? WHERE id=?', (json.dumps(payload), plan['id']))
        checks = review_checks.execution(self.store, self.cfg, normalize_time('2026-09-15T00:00:00+08:00'), normalize_time('2026-09-16T00:00:00+08:00'), normalize_time('2026-09-16T00:00:00+08:00'))
        failed = {c['check'] for c in checks if c['status'] == 'FAIL'}
        self.assertEqual(failed, {'CHECK_BUY_OUTSIDE_PLAN_BAND'})


if __name__ == '__main__':
    unittest.main()
