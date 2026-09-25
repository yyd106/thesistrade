import argparse
import json
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from ashare import backtest, config_ops
from ashare.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class ConfigChangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'config.json'
        raw = json.loads((ROOT / 'config.json').read_text())
        raw.update(data_dir='data', deployment_role='standalone')
        raw.pop('sync_key_file', None)
        self.path.write_text(json.dumps(raw, ensure_ascii=False))

    def tearDown(self):
        self.tmp.cleanup()

    def log(self):
        path = Path(self.tmp.name) / 'data' / 'workflow' / 'changes' / 'config-changes.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_operational_change_is_validated_logged_and_keeps_build(self):
        result = config_ops.apply(self.path, {'research_reuse_hours': 12}, reason='降低重复研究')
        self.assertEqual(json.loads(self.path.read_text())['research_reuse_hours'], 12)
        entry = self.log()[0]
        self.assertEqual((entry['class'], entry['after'], entry['approved_by']), ('OPERATIONAL', 12, None))
        self.assertEqual(result['build_before'], result['build_after'])  # operational settings never change the build

    def test_unbounded_operational_keys_have_change_limits(self):
        before = self.path.read_text()
        for change in ({'pdf_downloads_per_stock': 1000}, {'model_timeout_seconds': -5}, {'max_announcement_pages': '5'}):
            with self.assertRaisesRegex(ValueError, '不允许'):
                config_ops.apply(self.path, change, reason='x')
        self.assertEqual(self.path.read_text(), before)
        config_ops.apply(self.path, {'pdf_downloads_per_stock': 6}, reason='积压较多，加快补取')
        self.assertEqual(json.loads(self.path.read_text())['pdf_downloads_per_stock'], 6)

    def test_operational_keys_are_outside_the_build_identity(self):
        from ashare.build import STRATEGY_KEYS
        self.assertEqual(config_ops.OPERATIONAL & set(STRATEGY_KEYS), set())

    def test_strategy_change_needs_named_approval(self):
        with self.assertRaisesRegex(ValueError, '批准人'):
            config_ops.apply(self.path, {'paper_entry_band_bps': 100}, reason='测试')
        result = config_ops.apply(self.path, {'paper_entry_band_bps': 100}, reason='按提案收窄区间', approved_by='Dean 2026-10-02 聊天确认')
        self.assertNotEqual(result['build_before'], result['build_after'])
        self.assertEqual(self.log()[0]['class'], 'STRATEGY')

    def test_forbidden_and_invalid_values_never_reach_the_file(self):
        before = self.path.read_text()
        for change in ({'paper_max_stock_pct': 30}, {'live_execution_enabled': True}, {'unknown_key': 1}):
            with self.assertRaisesRegex(ValueError, '不允许'):
                config_ops.apply(self.path, change, reason='x', approved_by='Dean')
        with self.assertRaises(ValueError):
            config_ops.apply(self.path, {'research_reuse_hours': 99}, reason='超出范围')
        # A 13-hour gap between research rounds would let 12-hour plans lapse before the next round.
        with self.assertRaisesRegex(ValueError, '计划有效期'):
            config_ops.apply(self.path, {'collection_times': ['06:00', '19:00']}, reason='减少研究')
        self.assertEqual(self.path.read_text(), before)


def series(days, closes, spread=0.2):
    return [[d, c, c, c + spread, c - spread] for d, c in zip(days, closes)]


def weekdays(start, count):
    d = date.fromisoformat(start);out = []
    while len(out) < count:
        if d.weekday() < 5:out.append(d.isoformat())
        d += timedelta(days=1)
    return out


class BacktestTests(unittest.TestCase):
    def test_ma_rule_enters_on_band_touch_and_exits_by_rule(self):
        days = weekdays('2020-01-01', 90)
        closes = [10 + 0.05 * i for i in range(62)] + [12.8] + [13.5] * 27
        bars = series(days, closes)
        base = backtest.simulate_ma(bars, backtest.MA_VARIANTS['baseline'])
        self.assertEqual(base[0]['entry_date'], days[62])
        self.assertEqual(base[0]['reason'], 'END_OF_TEST')
        held = backtest.simulate_ma(bars, backtest.MA_VARIANTS['hold_20d'])
        self.assertLessEqual(held[0]['held_days'], 20)

    def test_run_writes_report_with_costs_and_benchmark(self):
        with tempfile.TemporaryDirectory() as tmp:
            days = weekdays('2020-01-01', 90)
            closes = [10 + 0.05 * i for i in range(62)] + [12.8] + [13.5] * 27
            backtest._save(tmp, 'sz000001', {'symbol': 'sz000001', 'basis': 'QFQ', 'bars': series(days, closes)})
            backtest._save(tmp, 'sh000300', {'symbol': 'sh000300', 'basis': 'INDEX', 'bars': series(days, [4000] * 90, 0)})
            universe = Path(tmp) / 'u.csv';universe.write_text('symbol,start,end\nsz000001,2020-01-01,\nsz000002,2020-01-01,2020-06-30\n')
            cfg = json.loads((ROOT / 'config.json').read_text())
            from ashare.settings import DEFAULTS
            for k, v in DEFAULTS.items():cfg.setdefault(k, v)
            result = backtest.run_ma(tmp, cfg, backtest.load_universe(universe), '2020-01-01', '2020-12-31')
            self.assertEqual(result['missing_data'], ['sz000002'])
            base = result['variants']['baseline']
            self.assertEqual(base['net']['n'], 1)
            trade = base['trades'][0]
            self.assertAlmostEqual(trade['net_bps'], trade['gross_bps'] - result['cost_bps'], places=0)
            self.assertEqual(trade['benchmark_bps'], 0.0)
            folder = backtest.save(tmp, result)
            self.assertIn('机械规则离线回测', (folder / 'report.md').read_text())
            self.assertTrue((folder / 'trades-baseline.csv').exists())

    def test_global_rule_and_empty_universe(self):
        days = weekdays('2021-01-01', 60)
        closes = [100 + i for i in range(30)] + [128.5] + [140] * 29
        trades = backtest.simulate_global(series(days, closes, 1), backtest.GLOBAL_VARIANTS['hold_5d'])
        self.assertTrue(trades);self.assertLessEqual(trades[0]['held_days'], 5)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'empty.csv';path.write_text('symbol,start,end\n')
            with self.assertRaisesRegex(ValueError, '成分股'):backtest.load_universe(path)


class CliCommandTests(unittest.TestCase):
    def test_proposal_cli_requires_complete_fields_and_named_approval(self):
        from ashare.cli import _store_command
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            try:
                spec = Path(tmp) / 'p.json'
                spec.write_text(json.dumps({'kind': 'PARAMETER', 'target': 'watchlist', 'title': '收窄买入区间'}, ensure_ascii=False))
                args = argparse.Namespace(command='proposals', action='new', file=str(spec), id=None, status=None, to=None, approved_by=None, note=None)
                with self.assertRaisesRegex(ValueError, '缺少字段'):_store_command(args, {}, store)
                spec.write_text(json.dumps({'kind': 'PARAMETER', 'target': 'watchlist', 'title': '收窄买入区间', 'hypothesis': '更窄区间减少追高',
                    'change': 'paper_entry_band_bps 200→100', 'evidence': '周报', 'test_plan': '对照账本A两版并行一个月',
                    'failure_criteria': '独立样本超额不高于原版', 'rollback': '恢复200'}, ensure_ascii=False))
                pid = _store_command(args, {}, store)['id']
                decide = argparse.Namespace(command='proposals', action='decide', id=pid, to='READY', approved_by=None, note='整理完成', file=None, status=None)
                self.assertEqual(_store_command(decide, {}, store)['status'], 'READY')
                decide.to = 'APPROVED'
                with self.assertRaisesRegex(ValueError, '批准人'):_store_command(decide, {}, store)
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
