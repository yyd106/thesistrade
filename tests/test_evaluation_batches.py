import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ashare import evaluation_batches as batches, notices
from ashare.storage import Store, normalize_time
from test_config import load_config


def bj(text):
    return normalize_time(text + '+08:00')


class EvaluationBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cfg = load_config(Path(__file__).resolve().parents[1] / 'config.json')
        self.cfg.update(data_dir=self.tmp.name, deployment_role='research')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_trading_days_count_by_close_and_skip_holidays(self):
        # 9-25 is the Mid-Autumn holiday; a day counts once its 15:00 close has passed.
        self.assertEqual(batches.closes(bj('2026-09-21T15:10:00'), bj('2026-09-28T16:00:00')),
                         ['2026-09-22', '2026-09-23', '2026-09-24', '2026-09-28'])
        self.assertEqual(batches.closes(bj('2026-09-21T15:10:00'), bj('2026-09-28T14:59:00')), ['2026-09-22', '2026-09-23', '2026-09-24'])
        # National Day week and the Saturday make-up workday (no trading) add nothing.
        self.assertEqual(batches.closes(bj('2026-09-30T16:00:00'), bj('2026-10-10T16:00:00')), ['2026-10-08', '2026-10-09'])

    def test_first_batch_then_too_soon_unless_forced(self):
        notices.create(self.store, title='购买行情源', body='建议购买付费行情源，理由见批次报告。', kind='DECISION', at=bj('2026-09-27T10:00:00'))
        first = batches.start(self.store, self.cfg, at=bj('2026-09-28T16:00:00'))
        self.assertEqual((first['status'], first['id'], first['trading_days'], first['forced']), ('READY', 'EV-20260928-1600', 4, False))
        folder = self.store.root / first['folder']
        manifest = json.loads((folder / 'manifest.json').read_text())
        self.assertEqual(manifest['period'], {'from': bj('2026-09-21T16:00:00'), 'to': bj('2026-09-28T16:00:00')})
        self.assertEqual(manifest['trading_days'], ['2026-09-22', '2026-09-23', '2026-09-24', '2026-09-28'])
        for name, meta in manifest['files'].items():
            self.assertEqual(hashlib.sha256((folder / name).read_bytes()).hexdigest(), meta['sha256'], name)
        self.assertIn('评估批次 EV-20260928-1600 报告', (folder / 'report.md').read_text())
        self.assertIn('运行汇总 2026-09-21 至 2026-09-28', (folder / 'digests.md').read_text())
        self.assertFalse((folder / 'digests.json').exists())
        self.assertEqual([n['title'] for n in json.loads((folder / 'notices.json').read_text())], ['购买行情源'])
        self.assertEqual(json.loads((folder / 'quotes-health.json').read_text())['events'], [])
        soon = batches.start(self.store, self.cfg, at=bj('2026-09-29T16:00:00'))
        self.assertEqual((soon['status'], soon['trading_days'], soon['minimum'], soon['previous']), ('TOO_SOON', 1, 3, 'EV-20260928-1600'))
        self.assertIn('--force', soon['message'])
        self.assertEqual(len(batches.listing(self.store)), 1)
        forced = batches.start(self.store, self.cfg, force=True, at=bj('2026-09-29T16:00:00'))
        self.assertEqual((forced['status'], forced['forced'], forced['trading_days']), ('READY', True, 1))
        self.assertEqual(batches.get(self.store, forced['id'])['period_start'], bj('2026-09-28T16:00:00'))

    def test_digest_cuts_a_batch_every_five_trading_days(self):
        batches.start(self.store, self.cfg, force=True, at=bj('2026-09-29T23:50:00'))
        self.assertIsNone(batches.auto(self.store, self.cfg, bj('2026-10-09T23:50:00')))  # 9-30, 10-8, 10-9
        made = batches.auto(self.store, self.cfg, bj('2026-10-13T23:50:00'))
        self.assertEqual((made['status'], made['trading_days']), ('READY', 5))
        self.assertEqual(batches.get(self.store, made['id'])['trigger'], 'auto')
        self.assertIsNone(batches.auto(self.store, {**self.cfg, 'evaluation_auto_trading_days': 0}, bj('2026-10-30T23:50:00')))

    def test_agent_notes_and_claude_check_sit_beside_unchanged_program_files(self):
        bid = batches.start(self.store, self.cfg, at=bj('2026-09-28T16:00:00'))['id']
        with self.assertRaises(ValueError):
            batches.note(self.store, bid, '太短')
        self.assertEqual(batches.note(self.store, bid, '本批次：研究成功率正常，行情没有中断，没有需要 Dean 决定的事项。')['status'], 'NOTED')
        with self.assertRaises(ValueError):
            batches.note(self.store, bid, '第二份小结，没有加 --replace 时不能覆盖第一份。')
        batches.note(self.store, bid, '替换后的小结：补充了组合决策的变化和两条工程问题。', replace=True)
        self.assertTrue(batches.get(self.store, bid)['annex']['notes']['replaced'])
        self.assertEqual(batches.record_check(self.store, bid, '# 检查结论\n\n没有阻断问题。\n', 'reports:checks/x.md'), bid)
        self.assertIsNone(batches.record_check(self.store, bid, '# 检查结论\n\n没有阻断问题。\n', 'reports:checks/x.md'))
        self.assertIsNone(batches.record_check(self.store, 'EV-20990101-0000', '不存在的批次', 'reports'))
        shown = batches.show(self.store, bid)
        self.assertEqual((shown['status'], shown['notes_file'], shown['check_file']), ('CHECKED', True, True))
        self.assertEqual(set(shown['integrity'].values()), {'OK'})
        (self.store.root / shown['folder'] / 'report.md').write_text('改过的报告')
        self.assertEqual(batches.show(self.store, bid)['integrity']['report.md'], 'CHANGED')
        with self.assertRaises(ValueError):
            batches.show(self.store, '../etc')

    def test_new_settings_are_bounded_operational_and_outside_the_build(self):
        from ashare.settings import validate_settings
        from ashare.build import config_fingerprint
        from ashare.config_ops import classify
        for bad in ({'evaluation_min_trading_days': 0}, {'evaluation_auto_trading_days': 21}, {'quote_fallback_enabled': 'yes'},
                    {'reports_sync_enabled': True, 'reports_remote': None}, {'reports_remote': 'https://github.com/a/b.git'}):
            with self.assertRaises(ValueError):
                validate_settings({**self.cfg, **bad})
        self.assertEqual(config_fingerprint(self.cfg), config_fingerprint({**self.cfg, 'evaluation_min_trading_days': 5, 'quote_fallback_enabled': False}))
        self.assertEqual({classify(k) for k in ('quote_fallback_enabled', 'evaluation_min_trading_days', 'evaluation_auto_trading_days',
                                                'reports_sync_enabled')}, {'OPERATIONAL'})
        self.assertEqual(classify('reports_remote'), 'FORBIDDEN')


if __name__ == '__main__':
    unittest.main()
