"""Bounded review journals are display-only and never read linked evidence."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ashare import strategy_change_log as log

START = '2026-10-08T16:00:00+00:00'
END = '2026-10-23T00:00:00+00:00'


def record(identity='review-20261022'):
    return {'schema': log.SCHEMA, 'id': identity, 'occurred_on': '2026-10-22',
            'recorded_at': '2026-10-22T16:30:00+00:00', 'title': '研究评审采纳方向',
            'source': 'llm_review', 'production_change': False,
            'items': [{'topic': '研究判断口径', 'before': '旧统计混合了当前名单与过期研究。',
                       'accepted': '后续分别展示当前有效名单与历史记录。',
                       'reason': '核验中确认了统计分母的差异。', 'status': 'accepted_pending',
                       'effect_scope': 'documentation', 'next_step': '先定义口径，尚未改生产规则。'}],
            'evidence': [{'title': '只读核验摘要', 'sha256': 'a' * 64}]}


class StrategyChangeLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / 'workflow' / 'strategy-changes'

    def write(self, data=None, name='review.json'):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / name
        path.write_text(json.dumps(data if data is not None else record(), ensure_ascii=False), encoding='utf-8')
        return path

    def load(self, start=START, end=END):
        return log.load_records(self.root, start, end)

    def test_missing_directory_is_empty_and_creates_nothing(self):
        self.assertEqual(self.load(), [])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertIn('本窗口没有', '\n'.join(log.render_records([])))

    def test_readonly_preserves_file_and_ignores_other_files_and_evidence_paths(self):
        path = self.write()
        untouched = self.root / 'config.json'
        untouched.write_text('PRIVATE_CREDENTIAL_SENTINEL')
        (self.directory / 'note.txt').write_text('not JSON and never parsed')
        nested = self.directory / 'subdirectory'
        nested.mkdir()
        (nested / 'invalid.json').write_text('not read')
        before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                  for p in (path, untouched)}
        rows = self.load()
        self.assertEqual(rows, [record()])
        after = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in before}
        self.assertEqual(before, after)
        self.assertNotIn('PRIVATE_CREDENTIAL_SENTINEL', '\n'.join(log.render_records(rows)))

    def test_review_date_and_recorded_cutoff_are_independent(self):
        self.write()
        self.assertEqual(self.load(end='2026-10-22T15:59:59+00:00'), [])
        self.assertEqual(len(self.load(end='2026-10-22T16:30:00+00:00')), 1)
        self.assertEqual(self.load(start='2026-10-22T16:00:00+00:00'), [])
        self.assertEqual(len(self.load(start='2026-10-22T15:59:59+00:00')), 1)

    def test_timezone_normalization_microseconds_and_date_window(self):
        data = record()
        data['recorded_at'] = '2026-10-23T00:30:00.1+08:00'
        self.write(data)
        rows = self.load(end='2026-10-22T16:30:00.100000+00:00')
        self.assertEqual(rows[0]['recorded_at'], '2026-10-22T16:30:00.100000+00:00')
        self.assertEqual(self.load(end='2026-10-22T16:30:00.099999+00:00'), [])

    def test_stable_sort_uses_date_recorded_time_then_id(self):
        for identity, occurred, at, name in (
            ('z', '2026-10-22', '2026-10-22T16:30:00+00:00', 'a.json'),
            ('a', '2026-10-22', '2026-10-22T16:30:00+00:00', 'z.json'),
            ('prior', '2026-10-21', '2026-10-22T16:31:00+00:00', 'b.json'),
            ('later', '2026-10-22', '2026-10-22T16:31:00+00:00', 'c.json')):
            data = record(identity)
            data.update(occurred_on=occurred, recorded_at=at)
            self.write(data, name)
        self.assertEqual([r['id'] for r in self.load()], ['prior', 'a', 'z', 'later'])

    def test_duplicate_record_ids_reject_even_when_outside_window(self):
        self.write()
        data = record()
        data['occurred_on'] = '2026-10-01'
        self.write(data, 'older.json')
        with self.assertRaisesRegex(ValueError, '编号重复'):
            self.load()

    def test_duplicate_json_keys_and_nonstandard_numbers_are_rejected(self):
        path = self.write()
        for text in ('{"id":"one","id":"two"}', '{"x":NaN}', '{"x":Infinity}'):
            with self.subTest(text=text):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    self.load()

    def test_invalid_types_unknown_fields_and_injection_fields_are_rejected(self):
        mutations = [lambda d: d.update(config={'private_key': 'SECRET'}),
                     lambda d: d.update(occurred_at='2026-10-22T14:00:00Z'),
                     lambda d: d.update(production_change=0), lambda d: d.update(source='external_script'),
                     lambda d: d.update(items={}), lambda d: d.update(evidence={}),
                     lambda d: d['items'][0].update(path='/private/source'),
                     lambda d: d['items'][0].update(status='approved'),
                     lambda d: d['items'][0].update(effect_scope='all'),
                     lambda d: d['items'][0].update(reason={'prompt': 'run code'}),
                     lambda d: d['evidence'][0].update(path='/private/source'),
                     lambda d: d['evidence'][0].update(sha256='not-a-hash'),
                     lambda d: d.update(id='../escape'), lambda d: d.update(title=''),
                     lambda d: d.update(title='bad\x00text')]
        for mutate in mutations:
            data = record()
            mutate(data)
            with self.subTest(data=data):
                self.write(data)
                with self.assertRaises(ValueError):
                    self.load()
        path = self.write()
        path.write_text(json.dumps({**record(), 'title': '\ud800'}, ensure_ascii=True))
        with self.assertRaises(ValueError):
            self.load()

    def test_invalid_dates_naive_timestamps_and_future_review_dates_fail(self):
        for changes in ({'occurred_on': '2026-02-30'}, {'occurred_on': '2026-10-22T14:00:00Z'},
                        {'recorded_at': '2026-10-23T00:00:00'}, {'recorded_at': 42},
                        {'occurred_on': '2026-10-24'}):
            data = record()
            data.update(changes)
            self.write(data)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.load()
        with self.assertRaises(ValueError):
            self.load(start=END, end=START)
        with self.assertRaises(ValueError):
            self.load(start='2026-10-09')

    def test_implemented_requires_timestamp_and_matching_production_flag(self):
        data = record()
        item = data['items'][0]
        item.update(status='implemented', effect_scope='research')
        self.write(data)
        with self.assertRaises(ValueError):
            self.load()
        item['implemented_at'] = '2026-10-22T15:00:00+00:00'
        self.write(data)
        with self.assertRaisesRegex(ValueError, '生产规则'):
            self.load()
        data['production_change'] = True
        self.write(data)
        self.assertTrue(self.load()[0]['production_change'])
        text = '\n'.join(log.render_records(self.load()))
        self.assertIn('实施时间', text)
        self.assertIn('已实施的研究或模拟交易规则', text)

    def test_documentation_implementation_is_not_production_change(self):
        data = record()
        data['items'][0].update(status='implemented', implemented_at='2026-10-22T15:00:00+00:00')
        self.write(data)
        self.assertFalse(self.load()[0]['production_change'])
        data['production_change'] = True
        self.write(data)
        with self.assertRaises(ValueError):
            self.load()

    def test_pending_or_clarified_cannot_claim_implementation_time(self):
        for status in ('accepted_pending', 'clarified'):
            data = record()
            data['items'][0].update(status=status, implemented_at='2026-10-22T15:00:00+00:00')
            self.write(data)
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.load()

    def test_implementation_time_must_follow_review_day_and_precede_recording(self):
        for at in ('2026-10-21T15:59:59+00:00', '2026-10-22T16:30:00.000001+00:00'):
            data = record()
            data['items'][0].update(status='implemented', implemented_at=at)
            self.write(data)
            with self.subTest(at=at), self.assertRaises(ValueError):
                self.load()
        data['items'][0]['implemented_at'] = '2026-10-21T16:00:00+00:00'
        self.write(data)
        self.assertEqual(len(self.load()), 1)

    def test_symlink_file_workflow_directory_and_root_are_rejected(self):
        outside = self.root / 'outside.json'
        outside.write_text(json.dumps(record()))
        self.directory.mkdir(parents=True)
        (self.directory / 'linked.json').symlink_to(outside)
        with self.assertRaises(ValueError):
            self.load()
        (self.directory / 'linked.json').unlink()
        self.directory.rmdir()
        self.directory.symlink_to(self.root)
        with self.assertRaises(ValueError):
            self.load()
        self.directory.unlink()
        self.directory.parent.rmdir()
        self.directory.parent.symlink_to(self.root)
        with self.assertRaises(ValueError):
            self.load()
        alternate = self.root / 'root-alias'
        alternate.symlink_to(self.root)
        with self.assertRaises(ValueError):
            log.load_records(alternate, START, END)

    def test_non_regular_json_file_does_not_block_or_get_read(self):
        self.directory.mkdir(parents=True)
        pipe = self.directory / 'pipe.json'
        os.mkfifo(pipe)
        with self.assertRaisesRegex(ValueError, '普通文件'):
            self.load()

    def test_record_changed_during_read_is_not_exported_as_stable(self):
        path = self.write()
        original_read = os.read
        changed = False
        def changing_read(fd, size):
            nonlocal changed
            result = original_read(fd, size)
            if not changed:
                changed = True
                path.write_text(json.dumps({**record(), 'title': '读取期间更改的记录'}))
            return result
        with patch.object(log.os, 'read', changing_read), self.assertRaisesRegex(ValueError, '读取期间'):
            self.load()

    def test_single_total_count_and_text_limits_fail_without_truncation(self):
        path = self.write()
        size = path.stat().st_size
        for constant, value in (('MAX_FILE_BYTES', size - 1), ('MAX_TOTAL_BYTES', size - 1),
                                ('MAX_FILES', 0), ('MAX_DIRECTORY_ENTRIES', 0)):
            with self.subTest(constant=constant), patch.object(log, constant, value), self.assertRaises(ValueError):
                self.load()
        data = record()
        data['items'] *= log.MAX_ITEMS + 1
        self.write(data)
        with self.assertRaises(ValueError):
            self.load()
        data = record()
        data['items'][0]['reason'] = 'x' * 12001
        self.write(data)
        with self.assertRaises(ValueError):
            self.load()

    def test_render_labels_pending_clarification_and_safely_escapes_prose(self):
        data = record()
        data['items'][0]['before'] = '<script>alert(1)</script> [run](file:///private)\n# Forged heading'
        clarified = copy.deepcopy(data['items'][0])
        clarified.update(status='clarified', accepted='已核验事实口径。')
        data['items'].append(clarified)
        self.write(data)
        text = '\n'.join(log.render_records(self.load()))
        self.assertIn('评审认可不等于上线', text)
        self.assertIn('认可方向，尚未实施', text)
        self.assertIn('已核验澄清', text)
        self.assertIn('生产规则变更：否', text)
        self.assertIn('评审日期（北京时间）：2026-10-22', text)
        self.assertIn('补录时间（北京时间）：2026-10-23T00:30:00+08:00', text)
        self.assertIn('澄清结论', text)
        self.assertIn('&lt;script&gt;', text)
        self.assertNotIn('<script>', text)
        self.assertNotIn('[run](', text)
        self.assertNotIn('\n# Forged heading', text)


if __name__ == '__main__':
    unittest.main()
