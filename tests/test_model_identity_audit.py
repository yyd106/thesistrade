import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ashare import model, model_identity_audit as audit
from ashare.storage import Store

START = '2026-10-04T00:00:00+00:00'
STAMP = '2026-10-04T01:00:00+00:00'
END = '2026-10-05T00:00:00+00:00'


class ModelIdentityAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.counter = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def attempt(self, meta=None, *, status='OK', detail='', run_id='new'):
        self.counter += 1
        run_id = f'{self.counter:032x}' if run_id == 'new' else run_id
        self.store.record_attempt('research_analysis', 'sh600000', status, detail, run_id=run_id, at=STAMP)
        if meta is not None:
            folder = self.store.root / 'workflow' / 'research' / run_id
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'meta.json').write_text(json.dumps(meta))
        return run_id

    @staticmethod
    def meta(**changes):
        return {'requested_model': 'gpt-test', 'requested_effort': 'high',
                'actual_model': 'gpt-test', 'actual_effort': 'high',
                'started_at': START, 'finished_at': STAMP, **changes}

    def check(self, **kwargs):
        return audit.check(self.store, START, END, **kwargs)

    def test_legacy_metadata_without_new_identity_object_is_verifiable(self):
        self.attempt(self.meta())
        out = self.check()
        self.assertEqual((out['status'], out['checked'], out['missing']), ('PASS', 1, 0))
        self.assertEqual(out['coverage']['routes'], ['watchlist'])
        self.assertIn('global', out['coverage']['excluded_routes'])

    def test_changed_error_prose_does_not_hide_structured_mismatch_in_health(self):
        self.attempt(self.meta(actual_model='replacement'), status='FAILED', detail='身份校验不合格，已放弃本次结果')
        from ashare.review_checks import health
        with patch('ashare.maintenance.disk_status', return_value={'warning': False}):
            checks = health(self.store, {}, START, END, END)
        out = next(c for c in checks if c['check'] == 'CHECK_MODEL_IDENTITY')
        self.assertEqual((out['status'], out['failures']), ('FAIL', 1))
        self.assertEqual(out['examples'][0]['code'], 'MODEL_IDENTITY_MISMATCH')
        self.assertEqual(out['examples'][0]['actual']['model'], 'replacement')
        self.assertEqual(out['examples'][0]['mismatched_fields'], ['model'])

    def test_missing_meta_for_actual_call_does_not_pass_in_health(self):
        self.attempt()
        from ashare.review_checks import health
        with patch('ashare.maintenance.disk_status', return_value={'warning': False}):
            out = next(c for c in health(self.store, {}, START, END, END) if c['check'] == 'CHECK_MODEL_IDENTITY')
        self.assertEqual((out['status'], out['checked'], out['missing']), ('INSUFFICIENT', 0, 1))

    def test_reuse_without_call_id_is_excluded_without_claiming_pass(self):
        self.attempt(run_id=None, detail='沿用未变化的研究结论')
        out = self.check()
        self.assertEqual((out['status'], out['checked'], out['missing']), ('NOT_APPLICABLE', 0, 0))
        self.assertEqual(out['coverage']['reused_without_call'], 1)

    def test_unknown_failed_attempt_is_not_false_model_mismatch(self):
        self.attempt(status='FAILED', detail='网络失败', run_id=None)
        out = self.check()
        self.assertEqual((out['status'], out['failures'], out['missing']), ('INSUFFICIENT', 0, 1))

    def test_legacy_explicit_mismatch_is_retained_and_identified_as_legacy(self):
        self.attempt(status='FAILED', detail='模型实际运行配置与固定配置不一致（x≠y），结果未采用')
        out = self.check()
        self.assertEqual((out['status'], out['coverage']['legacy_failures']), ('FAIL', 1))
        self.assertEqual(out['examples'][0]['code'], 'LEGACY_MODEL_IDENTITY_MISMATCH')

    def test_negated_legacy_prose_does_not_create_false_failure(self):
        self.attempt(status='FAILED', detail='未发现模型实际运行配置与固定配置不一致（x≠y），其他错误')
        self.assertEqual(self.check()['status'], 'INSUFFICIENT')

    def test_missing_or_malformed_pinned_fields_never_pass(self):
        for changes in ({'actual_model': None}, {'actual_effort': None}, {'requested_model': {'bad': 'field'}}):
            with self.subTest(changes=changes):
                self.assertEqual(audit.identity(self.meta(**changes))['status'], 'UNKNOWN')
        self.attempt(self.meta(actual_model=None))
        self.assertEqual(self.check()['status'], 'INSUFFICIENT')

    def test_unpinned_defaults_are_not_claimed_verified(self):
        self.attempt(self.meta(requested_model=None, requested_effort=None))
        self.assertEqual(self.check()['status'], 'INSUFFICIENT')

    def test_missing_identity_time_is_not_claimed_timely(self):
        self.attempt(self.meta(finished_at=None))
        out = self.check()
        self.assertEqual(out['status'], 'INSUFFICIENT')
        self.assertEqual(out['examples'][0]['code'], 'MODEL_METADATA_TIME_UNKNOWN')

    def test_metadata_after_ready_is_not_used_even_for_mismatch(self):
        self.attempt(self.meta(actual_model='replacement', finished_at='2026-10-05T00:00:01+00:00'))
        out = self.check(ready=END)
        self.assertEqual((out['status'], out['failures']), ('INSUFFICIENT', 0))
        self.assertEqual(out['examples'][0]['code'], 'MODEL_METADATA_AFTER_CUTOFF')
        self.assertEqual(self.check(ready='2026-10-05T00:00:02+00:00')['status'], 'FAIL')

    def test_health_passes_its_ready_cutoff_to_identity_check(self):
        self.attempt(self.meta(actual_model='replacement'))
        from ashare.review_checks import health
        with patch('ashare.maintenance.disk_status', return_value={'warning': False}):
            out = next(c for c in health(self.store, {}, START, END, START)
                       if c['check'] == 'CHECK_MODEL_IDENTITY')
        self.assertEqual((out['status'], out['failures']), ('INSUFFICIENT', 0))
        self.assertEqual(out['examples'][0]['code'], 'MODEL_METADATA_AFTER_CUTOFF')

    def test_reversed_or_naive_metadata_time_is_invalid(self):
        for changes in ({'started_at': END}, {'finished_at': '2026-10-04T01:00:00'}):
            with self.subTest(changes=changes):
                self.assertEqual(audit.timing(self.meta(**changes), END), 'MODEL_METADATA_TIME_INVALID')

    def test_invalid_call_reference_never_opens_paths(self):
        self.attempt(run_id='../../outside')
        with patch('ashare.model_identity_audit.os.open', side_effect=AssertionError('unsafe path was opened')):
            out = self.check()
        self.assertEqual(out['examples'][0]['code'], 'MODEL_METADATA_INVALID_REFERENCE')

    def test_symlink_file_and_parent_are_refused(self):
        target = self.store.root / 'synthetic-external.json'
        target.write_text(json.dumps(self.meta()))
        run_id = self.attempt()
        folder = self.store.root / 'workflow' / 'research' / run_id
        folder.mkdir(parents=True)
        (folder / 'meta.json').symlink_to(target)
        self.assertEqual(self.check()['status'], 'INSUFFICIENT')
        second_id = self.attempt()
        (folder.parent / second_id).symlink_to(folder, target_is_directory=True)
        self.assertEqual(self.check()['missing'], 2)

    def test_oversized_or_nonregular_metadata_is_refused(self):
        run_id = self.attempt({'padding': 'x' * (audit.MAX_METADATA_BYTES + 1)})
        self.assertEqual(audit.metadata(self.store, run_id)[1], 'MODEL_METADATA_TOO_LARGE')
        other = self.attempt()
        folder = self.store.root / 'workflow' / 'research' / other
        folder.mkdir()
        os.mkfifo(folder / 'meta.json')
        self.assertEqual(audit.metadata(self.store, other)[1], 'MODEL_METADATA_NOT_REGULAR')

    def test_metadata_reader_and_public_check_keep_only_identity_fields(self):
        run_id = self.attempt(self.meta(actual_model='replacement', prompt='PRIVATE_PROMPT', arbitrary='PRIVATE_DATA'))
        folder = self.store.root / 'workflow' / 'research' / run_id
        (folder / 'stdout.log').symlink_to(self.store.root / 'missing')
        (folder / 'prompt.txt').symlink_to(self.store.root / 'missing')
        value, error = audit.metadata(self.store, run_id)
        self.assertIsNone(error)
        self.assertEqual(set(value), set(audit.FIELDS + audit.TIME_FIELDS))
        encoded = json.dumps(self.check())
        self.assertNotIn('PRIVATE_', encoded)
        self.assertNotIn(self.temp.name, encoded)


class ModelIdentityProducerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_pinned = model.pinned()
        model.configure({'model_name': 'gpt-test', 'model_reasoning_effort': 'high'})

    def tearDown(self):
        model.configure({'model_name': self.old_pinned['name'], 'model_reasoning_effort': self.old_pinned['effort']})
        self.temp.cleanup()

    def invoke(self, header):
        class Process:
            returncode = 0
            pid = 12345

            def __init__(self, command, **kwargs):
                self.command = command
                self.error = kwargs['stderr']

            def communicate(self, prompt, timeout):
                self.error.write(header);self.error.flush()
                path = Path(self.command[self.command.index('--output-last-message') + 1])
                path.write_text('{"ok":true,"echo":"synthetic"}')

        with patch('ashare.model.codex_executable', return_value='/synthetic/codex'), \
                patch('ashare.model.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='ChatGPT', stderr='')), \
                patch('ashare.model.subprocess.Popen', Process):
            return model.run_json('synthetic prompt', model.CHECK_SCHEMA, self.temp.name)

    def test_mismatch_stable_metadata_written_before_original_gate_rejects(self):
        with self.assertRaisesRegex(RuntimeError, '实际运行配置与固定配置不一致'):
            self.invoke('model: replacement\nreasoning effort: high\n')
        meta = json.loads((Path(self.temp.name) / 'meta.json').read_text())
        self.assertEqual(meta['identity']['code'], 'MODEL_IDENTITY_MISMATCH')
        self.assertEqual(meta['identity']['requested']['model'], 'gpt-test')
        self.assertEqual(meta['identity']['actual']['model'], 'replacement')

    def test_matching_call_keeps_result_and_writes_identity(self):
        self.assertTrue(self.invoke('model: gpt-test\nreasoning effort: high\n')['ok'])
        meta = json.loads((Path(self.temp.name) / 'meta.json').read_text())
        self.assertEqual(meta['identity']['code'], 'MODEL_IDENTITY_MATCH')

    def test_missing_header_is_unknown_without_changing_old_adoption_gate(self):
        self.assertTrue(self.invoke('no identity header\n')['ok'])
        meta = json.loads((Path(self.temp.name) / 'meta.json').read_text())
        self.assertEqual(meta['identity']['code'], 'MODEL_IDENTITY_UNKNOWN')


if __name__ == '__main__':
    unittest.main()
