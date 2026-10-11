"""Asset-specific evidence requirements in the actual research workflow."""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from ashare import global_research
from ashare.settings import DEFAULTS
from ashare.storage import Store, normalize_time


class AssetResearchContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        self.at = normalize_time('2026-09-15T10:00:00+08:00')
        self.cfg = {**DEFAULTS,
                    **json.loads((Path(__file__).resolve().parents[1]/'config.example.json').read_text()),
                    'data_dir': self.tmp.name, 'investment_policy': 'days_cash_v1',
                    'model_enabled': True, 'industry_enabled': False}

    def run_asset(self, symbol, stance='WAIT', history=True):
        if history:
            bars = [{'date': (date(2026, 8, 1)+timedelta(days=i)).isoformat(),
                     'price_micros': (100+i)*1_000_000} for i in range(30)]
            with self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO global_market VALUES(?,?,?)',
                    (symbol, self.at, json.dumps({'status': 'OK', 'bars': bars})))
        prompts = []
        def model(prompt, schema, folder, timeout):
            prompts.append(prompt)
            return {'stance': stance, 'thesis': '合成量价观察，不声称宏观因果',
                    'counterpoints': ['未来趋势可能反转'], 'holding_days': 5,
                    'evidence_ids': ['PRICE_HISTORY'], 'next_checks': ['补充独立事件证据']}
        with patch('ashare.global_research.targets', return_value={symbol: {
                'asset': symbol, 'name': symbol, 'links': []}}):
            result = global_research.run(self.store, self.cfg, self.at,
                                         model_fn=model, fetch_quotes=False)
        plan = global_research.active_plan(self.store, symbol, self.at)
        return result, json.loads(plan['payload_json']), prompts

    def test_all_fixed_assets_use_asset_evidence_and_preserve_wait(self):
        for symbol in ('GOLD', 'SILVER', 'BTC', 'ETH'):
            with self.subTest(symbol=symbol):
                result, plan, prompts = self.run_asset(symbol)
                self.assertEqual(result['model_calls'], 1)
                self.assertEqual(plan['kind'], 'WAIT')
                self.assertNotIn('缺少公司基本面时只能保守等待', prompts[0])
                self.assertIn('证据不足仍须WAIT', prompts[0])
                self.assertIn('不能作为宏观、经营或链上事实的证明', prompts[0])
                self.assertEqual(plan['research_contract']['version'], 'asset-evidence-v2')
                self.assertNotEqual(plan['research_contract']['asset_type'], 'COMPANY_EQUITY')

    def test_fixed_asset_long_does_not_require_nonexistent_company_dossier(self):
        _, plan, _ = self.run_asset('GOLD', 'LONG')
        self.assertEqual(plan['kind'], 'PAPER_TRADE')
        self.assertEqual(plan['blockers'], [])

    def test_missing_price_history_still_waits_without_model(self):
        result, plan, prompts = self.run_asset('BTC', 'LONG', history=False)
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(prompts, [])
        self.assertEqual(plan['kind'], 'WAIT')
        self.assertIn('历史样本不足20个日观测，继续积累', plan['blockers'])

    def test_equities_keep_company_evidence_requirement(self):
        packet = {'symbol': 'US:TEST', 'company_dossier': {}}
        self.assertIn('缺少公司基本面时只能保守等待', global_research.build_prompt(packet))
        self.assertEqual(global_research.asset_contract('US:TEST')['asset_type'], 'COMPANY_EQUITY')
        self.assertIn('只能WAIT', global_research.build_prompt({'symbol': 'UNSUPPORTED'}))


if __name__ == '__main__':
    unittest.main()
