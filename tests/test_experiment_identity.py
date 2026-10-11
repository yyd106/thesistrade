"""Synthetic identity/release regressions; no production data or model calls."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ashare import build, experiments, experiment_runner as runner, research
from ashare.demo import SYMBOL, seed, research_model
from ashare.governance import draft_proposal
from ashare.judgments import encode
from ashare.storage import Store
import test_experiment_runner as fixtures


class SourceTree:
    """Small real files exercise source hashing instead of mocking its result."""
    def __init__(self, path):
        self.root = Path(path) / 'ashare'
        self.root.mkdir()
        (self.root / 'data').mkdir()
        self.write('__init__.py', '__version__ = "0.1.0"\n')
        for name in ('build.py', 'research.py', 'sources.py', 'scheduler.py',
                     'experiment_measurement.py', 'presentation.py', 'reports.py',
                     *build.DISPLAY_ONLY_MODULES):
            self.write(name, 'VALUE = "original"\n')
        self.write('data/calendar.json', '{"calendar":1}')
        (self.root.parent / 'pyproject.toml').write_text(
            '[project]\nversion = "0.1.0"\nrequires-python = ">=3.9"\n'
            'dependencies = ["pypdf==6.10.0"]\n')
        (self.root.parent / 'requirements.lock').write_text('pypdf==6.10.0\n')

    def write(self, name, value):
        (self.root / name).write_text(value)
        build.code_fingerprint.cache_clear()

    def release(self):
        for name in build.DISPLAY_ONLY_MODULES:
            self.write(name, 'VALUE = "new display wording"\n')
        self.write('__init__.py', '__version__ = "0.1.1"\n')
        project = self.root.parent / 'pyproject.toml'
        project.write_text(project.read_text().replace('0.1.0', '0.1.1'))


class BehaviorFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tree = SourceTree(self.tmp.name)
        self.store = Store(Path(self.tmp.name) / 'data')
        self.cfg = {'model_name': 'synthetic-model', 'watchlist': [{'symbol': SYMBOL, 'name': '合成公司'}]}
        self.source = patch.object(build, '__file__', str(self.tree.root / 'build.py'))
        self.source.start()
        self.addCleanup(self.source.stop)
        self.addCleanup(build.code_fingerprint.cache_clear)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_real_display_release_changes_deployment_only(self):
        full = build.info(self.cfg, self.store)
        frozen = build.behavior_info(self.cfg, self.store)
        self.tree.release()
        self.assertNotEqual(build.info(self.cfg, self.store)['build_id'], full['build_id'])
        self.assertEqual(build.behavior_info(self.cfg, self.store), frozen)
        self.tree.write('__init__.py', '__version__ = "0.1.1"\nRUN_MODE = "changed behavior"\n')
        self.assertFalse(build.behavior_matches(frozen, self.cfg, self.store))

    def test_unknown_code_and_all_input_measurement_dependencies_are_included(self):
        for name in ('research.py', 'sources.py', 'scheduler.py', 'experiment_measurement.py',
                     'presentation.py', 'reports.py', 'future_dependency.py', 'data/new-source.json'):
            with self.subTest(path=name):
                frozen = build.behavior_info(self.cfg, self.store)
                self.tree.write(name, 'changed dependency')
                self.assertFalse(build.behavior_matches(frozen, self.cfg, self.store))

    def test_dependency_and_runtime_changes_are_included(self):
        frozen = build.behavior_info(self.cfg, self.store)
        path = self.tree.root.parent / 'requirements.lock'
        path.write_text('pypdf==6.10.1\n')
        self.assertFalse(build.behavior_matches(frozen, self.cfg, self.store))
        frozen = build.behavior_info(self.cfg, self.store)
        path = self.tree.root.parent / 'pyproject.toml'
        path.write_text(path.read_text().replace('>=3.9', '>=3.12'))
        self.assertFalse(build.behavior_matches(frozen, self.cfg, self.store))
        frozen = build.behavior_info(self.cfg, self.store)
        with patch.object(build.metadata, 'version', return_value='future-runtime'):
            self.assertFalse(build.behavior_matches(frozen, self.cfg, self.store))

    def test_only_the_project_release_label_is_ignored(self):
        path = self.tree.root.parent / 'pyproject.toml'
        path.write_text(path.read_text() + '\n[[project.custom_runtime]]\nversion = "one"\n')
        frozen = build.behavior_info(self.cfg, self.store)
        path.write_text(path.read_text().replace('version = "one"', 'version = "two"'))
        self.assertFalse(build.behavior_matches(frozen, self.cfg, self.store))

    def test_model_executable_upgrade_at_same_path_changes_identity_without_running_it(self):
        path = Path(self.tmp.name) / 'synthetic-codex'
        path.write_bytes(b'not executable: synthetic model client version one')
        with patch('ashare.model.codex_executable', return_value=str(path)), \
             patch('ashare.model.subprocess.run', side_effect=AssertionError('Must not launch CLI')):
            frozen = build.behavior_info(self.cfg, self.store)
            first_hash = build.model_executable_fingerprint()
            self.assertEqual(build.model_executable_fingerprint(), first_hash)
            path.write_bytes(b'not executable: synthetic model client version two changed')
            self.assertNotEqual(build.model_executable_fingerprint(), first_hash)
            self.assertFalse(build.behavior_matches(frozen, self.cfg, self.store))
            self.assertNotIn(str(path), encode(build.behavior_info(self.cfg, self.store)))
        with patch('ashare.model.codex_executable', side_effect=RuntimeError('missing')):
            self.assertEqual(build.model_executable_fingerprint(), '<missing>')

    def test_moving_identical_model_executable_preserves_identity(self):
        first, second = Path(self.tmp.name) / 'client-a', Path(self.tmp.name) / 'client-b'
        first.write_bytes(b'synthetic model client')
        second.write_bytes(first.read_bytes())
        with patch('ashare.model.codex_executable', return_value=str(first)):
            frozen = build.behavior_info(self.cfg, self.store)
        with patch('ashare.model.codex_executable', return_value=str(second)):
            self.assertTrue(build.behavior_matches(frozen, self.cfg, self.store))

    def test_inputs_sampling_model_and_unknown_settings_are_hashed_without_raw_values(self):
        frozen = build.behavior_info(self.cfg, self.store)
        changes = {'collection_times': ['12:34'], 'scheduler_poll_seconds': 11,
                   'research_reuse_hours': 20, 'model_timeout_seconds': 600,
                   'model_name': 'other-model', 'model_reasoning_effort': 'high',
                   'news_url': 'https://synthetic.invalid/private-sentinel',
                   'research_topics': ['private-sentinel'], 'announcement_lookback_days': 50,
                   'external_news_enabled': False, 'future_sampling': 'private-sentinel',
                   'watchlist': [{'symbol': SYMBOL, 'name': 'different input name'}]}
        for key, value in changes.items():
            with self.subTest(setting=key):
                changed = build.behavior_info({**self.cfg, key: value}, self.store)
                self.assertNotEqual(changed, frozen)
                self.assertNotIn('private-sentinel', encode(changed))
        transient = {**self.cfg, 'data_dir': '/synthetic/location', 'config_path': '/synthetic/config', '_deadline': object()}
        self.assertEqual(build.behavior_info(transient, self.store), frozen)
        with self.assertRaises(TypeError):
            build.behavior_info({**self.cfg, 'unknown_runtime_object': object()}, self.store)

    def test_adopted_guidance_changes_identity(self):
        frozen = build.behavior_info(self.cfg, self.store)
        with self.store.db:
            self.store.db.execute("INSERT INTO strategy_guidance VALUES('G-1','watchlist','ALL','合成规则','ADOPTED',NULL,?,NULL,'fixture','{}')", (fixtures.AT,))
        self.assertFalse(build.behavior_matches(frozen, self.cfg, self.store))


class ExperimentIdentityTests(unittest.TestCase):
    # These fixture helpers use DEFAULTS and a fresh synthetic Store only.
    tearDown = fixtures.ExperimentRunnerTests.tearDown
    packet = fixtures.ExperimentRunnerTests.packet
    enroll = fixtures.ExperimentRunnerTests.enroll
    output = fixtures.ExperimentRunnerTests.output
    model = fixtures.ExperimentRunnerTests.model
    start = fixtures.ExperimentRunnerTests.start

    def setUp(self):
        fixtures.ExperimentRunnerTests.setUp(self)
        self.tree = SourceTree(self.tmp.name)
        self.source = patch.object(build, '__file__', str(self.tree.root / 'build.py'))
        self.source.start()
        self.addCleanup(self.source.stop)
        self.addCleanup(build.code_fingerprint.cache_clear)

    def legacy_design(self):
        new = runner.prepare(self.store, self.cfg, fixtures.AT)
        spec = experiments.view(self.store, new['id'])['spec']
        spec.pop('behavior_identity')
        spec['version'] = experiments.VERSION
        # Construct an independent legacy record, never rewrite a frozen design.
        with self.store.db:
            proposal = draft_proposal(self.store, source='synthetic', kind='PROMPT', target='watchlist',
                title='legacy fixture', payload={'experiment_design': spec}, at=fixtures.AT,
                dedupe_key='synthetic-legacy')
            identity = experiments.register(self.store, proposal, spec, fixtures.AT)
        return identity

    def test_display_release_keeps_prepare_start_enrollment_and_two_arms_compatible(self):
        self.cfg['private_sentinel'] = 'must-not-be-saved-as-config'
        prepared = runner.prepare(self.store, self.cfg, fixtures.AT)
        design = self.store.db.execute('SELECT * FROM experiment_designs WHERE id=?', (prepared['id'],)).fetchone()
        original = tuple(design)
        baseline = json.loads(design['spec_json'])['baseline_build']
        self.assertNotIn('must-not-be-saved-as-config', design['spec_json'])
        self.tree.release()
        self.assertEqual(runner.prepare(self.store, self.cfg, fixtures.AT), prepared)
        self.assertNotEqual(build.info(self.cfg, self.store)['build_id'], baseline)
        runner.start(self.store, self.cfg, prepared['id'], 'synthetic activation', fixtures.AT)
        manifest = runner._manifest(self.store, prepared['id'])
        self.assertEqual(manifest['version'], runner.BEHAVIOR_VERSION)
        self.assertEqual(manifest['baseline_build'], baseline)
        self.assertEqual(manifest['build']['build_id'], build.info(self.cfg, self.store)['build_id'])
        self.assertNotIn('must-not-be-saved-as-config', encode(manifest))
        packet = self.packet()
        pair = self.enroll(packet)['id']
        self.tree.write('review_presentation.py', 'VALUE = "report layout revision"\n')
        calls = []
        result = runner.run_pair(self.store, self.cfg, pair, model_fn=self.model(packet, calls), clock=lambda: fixtures.later(seconds=1))
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(len(calls), 2)
        state = runner.summary(self.store, prepared['id'])
        self.assertEqual((state['status'], state['enrolled'], state['model_calls']), ('RUNNING', 1, 2))
        self.assertEqual(tuple(self.store.db.execute('SELECT * FROM experiment_designs WHERE id=?', (prepared['id'],)).fetchone()), original)
        self.assertEqual(runner._manifest(self.store, prepared['id']), manifest)

    def test_behavior_change_rejects_activation(self):
        prepared = runner.prepare(self.store, self.cfg, fixtures.AT)
        self.cfg['collection_times'] = ['12:34']
        with self.assertRaisesRegex(ValueError, '基线版本已变化'):
            runner.start(self.store, self.cfg, prepared['id'], 'synthetic activation', fixtures.AT)
        self.assertEqual(experiments.view(self.store, prepared['id'])['status'], 'DESIGNED')
        self.assertIsNone(runner._manifest(self.store, prepared['id']))

    def test_behavior_change_stops_pending_trial_and_keeps_failed_denominator(self):
        prepared = self.start()
        packet = self.packet()
        pair = self.enroll(packet)['id']
        self.tree.write('future_dependency.py', 'NEW_SAMPLING = True\n')
        result = runner.run_pair(self.store, self.cfg, pair,
            model_fn=lambda *a, **k: self.fail('No call after behavior changes'), clock=lambda: fixtures.later(seconds=1))
        self.assertEqual(result['status'], 'IMPLEMENTATION_CHANGED')
        state = runner.summary(self.store, prepared['id'])
        self.assertEqual((state['status'], state['enrolled'], state['failed'], state['model_calls']), ('INCONCLUSIVE', 1, 1, 0))
        self.assertEqual(state['stop_reason'], 'IMPLEMENTATION_CHANGED')
        with self.assertRaisesRegex(ValueError, '不重复或重开'):
            runner.start(self.store, self.cfg, prepared['id'], 'cannot reopen', fixtures.AT)

    def test_behavior_change_between_arms_stops_second_call(self):
        prepared = self.start()
        packet = self.packet()
        pair = self.enroll(packet)['id']
        calls = []
        def changing_model(*args, **kwargs):
            result = self.model(packet, calls)(*args, **kwargs)
            self.cfg['model_timeout_seconds'] += 1
            return result
        runner.run_pair(self.store, self.cfg, pair, model_fn=changing_model, clock=lambda: fixtures.later(seconds=1))
        self.assertEqual(len(calls), 1)
        state = runner.summary(self.store, prepared['id'])
        self.assertEqual((state['status'], state['enrolled'], state['failed'], state['model_calls']), ('INCONCLUSIVE', 1, 1, 1))

    def test_legacy_design_still_rejects_display_build_drift(self):
        identity = self.legacy_design()
        before = experiments.view(self.store, identity)['spec']
        self.tree.release()
        with self.assertRaisesRegex(ValueError, '基线版本已变化'):
            runner.start(self.store, self.cfg, identity, 'legacy activation', fixtures.AT)
        self.assertEqual(experiments.view(self.store, identity)['spec'], before)

    def test_legacy_manifest_stays_full_build_and_is_never_migrated(self):
        identity = self.legacy_design()
        runner.start(self.store, self.cfg, identity, 'legacy activation', fixtures.AT)
        manifest = runner._manifest(self.store, identity)
        self.assertEqual(manifest['version'], runner.VERSION)
        self.assertNotIn('behavior_identity', manifest)
        packet = self.packet()
        self.assertEqual(self.enroll(packet)['status'], 'ENROLLED')
        self.tree.release()
        runner.advance(self.store, self.cfg, fixtures.later(seconds=1))
        state = runner.summary(self.store, identity)
        self.assertEqual((state['status'], state['enrolled'], state['failed']), ('INCONCLUSIVE', 1, 1))
        self.assertEqual(runner._manifest(self.store, identity), manifest)

    def test_design_fields_and_identity_are_versioned_and_exact(self):
        prepared = runner.prepare(self.store, self.cfg, fixtures.AT)
        spec = experiments.view(self.store, prepared['id'])['spec']
        self.assertEqual(experiments.validate(spec, fixtures.AT), spec)
        legacy = copy.deepcopy(spec)
        legacy.pop('behavior_identity')
        legacy['version'] = experiments.VERSION
        self.assertEqual(experiments.validate(legacy, fixtures.AT), legacy)
        variants = [dict(spec, extra='unknown'), dict(spec, version='forward-design-v99'),
                    dict(legacy, behavior_identity=spec['behavior_identity']),
                    {k: v for k, v in spec.items() if k != 'behavior_identity'}]
        bad = copy.deepcopy(spec)
        bad['behavior_identity']['code_hash'] = '0' * 64
        variants.append(bad)
        for value in variants:
            with self.subTest(spec=value):
                with self.assertRaises(ValueError):
                    experiments.validate(value, fixtures.AT)

    def test_unknown_or_missing_identity_never_falls_back_to_full_build(self):
        prepared = self.start()
        manifest = runner._manifest(self.store, prepared['id'])
        for changed in (dict(manifest, version='forward-runner-v99'), dict(manifest, behavior_identity=None),
                        dict(manifest, version=runner.VERSION),
                        {k: v for k, v in manifest.items() if k != 'behavior_identity'}):
            with self.subTest(version=changed['version']):
                self.assertFalse(runner._compatible(self.store, self.cfg, changed))


class ResearchBehaviorReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'data')
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        self.tree = SourceTree(self.tmp.name)
        self.source = patch.object(build, '__file__', str(self.tree.root / 'build.py'))
        self.source.start()
        self.addCleanup(self.source.stop)
        self.addCleanup(build.code_fingerprint.cache_clear)
        from ashare.pipeline import load_config
        self.cfg = load_config(Path(__file__).resolve().parents[1] / 'config.example.json')
        self.cfg.update(data_dir=str(self.store.root), watchlist=[{'symbol': SYMBOL, 'name': '合成测试'}],
                        model_enabled=True, research_reuse_hours=20)
        self.first = seed(self.store, self.cfg)

    def study(self, legacy=False):
        if legacy:
            # Generate a historical-style record; never alter an immutable record.
            def old_record(store, cfg, stamp):
                with store.db:
                    return build.record(store, cfg, stamp)
            with patch.object(research, 'record_build', side_effect=old_record):
                return research.study(self.store, self.cfg, self.first, model_fn=research_model(self.first), at='2026-09-15T09:01:00+08:00')
        return research.study(self.store, self.cfg, self.first, model_fn=research_model(self.first), at='2026-09-15T09:01:00+08:00')

    def packet(self):
        return research.make_snapshot(self.store, self.cfg, SYMBOL, at='2026-09-15T15:00:00+08:00', persist=False)

    def test_new_research_reuses_across_display_release_but_not_input_change(self):
        done = self.study()
        self.tree.release()
        packet = self.packet()
        reuse = research.reusable(self.store, self.cfg, packet)
        self.assertIsNotNone(reuse)
        renewed = research.renew(self.store, self.cfg, packet, reuse, at=packet['as_of'])
        self.assertEqual(renewed['study_id'], done['study_id'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM studies').fetchone()[0], 1)
        self.assertIsNone(research.reusable(self.store, {**self.cfg, 'news_url': 'https://changed.invalid'}, packet))

    def test_legacy_research_keeps_full_build_boundary(self):
        self.study(legacy=True)
        packet = self.packet()
        self.assertIsNotNone(research.reusable(self.store, self.cfg, packet))
        self.tree.release()
        self.assertIsNone(research.reusable(self.store, self.cfg, packet))


if __name__ == '__main__':
    unittest.main()
