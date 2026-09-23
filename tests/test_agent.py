import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ashare.finance import PaperLedger, cents, total_profit, withdrawal_plan, withdrawal_tier
from ashare.model import validate_result
from test_config import load_config
from ashare.pipeline import run
from ashare.sources import check_url, history_summary, parse_quotes
from ashare.storage import Store, now


class IsolatedStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()


class MoneyTests(IsolatedStore):
    def test_confirmed_tiers(self):
        self.assertEqual([withdrawal_tier(n) for n in range(1, 7)], [(15000000, 12000000), (20000000, 15000000), (30000000, 20000000), (40000000, 30000000), (50000000, 40000000), (60000000, 50000000)])
        self.assertFalse(withdrawal_plan(cents(150000), 1)["triggered"])
        self.assertTrue(withdrawal_plan(cents("150000.01"), 1)["triggered"])
        self.assertEqual(withdrawal_plan(cents(160000), 1)["planned_cents"], cents(40000))
        self.assertFalse(withdrawal_plan(cents(140000), 1)["triggered"])

    def test_duplicate_partial_transfer_and_restart(self):
        ledger = PaperLedger(self.store)
        ledger.initialize()
        a = ledger.plan(cents(160000))
        self.assertEqual(a["id"], ledger.plan(cents(160000))["id"])
        ledger.reconcile_simulated_transfer(a["id"], cents(10000), "test-part-1")
        self.assertEqual(self.store.db.execute("SELECT current_stage FROM paper_accounts").fetchone()[0], 1)
        ledger.reconcile_simulated_transfer(a["id"], cents(10000), "test-part-1")
        self.assertEqual(self.store.db.execute("SELECT withdrawn_cents FROM paper_accounts").fetchone()[0], cents(10000))
        self.store.close()
        self.store = Store(self.temp.name)
        ledger = PaperLedger(self.store)
        ledger.reconcile_simulated_transfer(a["id"], cents(30000), "test-part-2")
        self.assertEqual(self.store.db.execute("SELECT current_stage FROM paper_accounts").fetchone()[0], 2)
        self.assertFalse(ledger.plan(cents(160000))["triggered"])
        with self.assertRaises(ValueError):
            ledger.reconcile_simulated_transfer(a["id"], cents(30001), "test-part-2")

    def test_profit_is_not_reduced_by_withdrawal(self):
        before = total_profit(cents(160000), 0, cents(100000))
        after = total_profit(cents(120000), cents(40000), cents(100000))
        self.assertEqual(before, after)

    def test_initial_budget_and_rounding(self):
        ledger = PaperLedger(self.store)
        with self.assertRaises(ValueError):
            ledger.initialize(initial_cents=cents(100001))
        self.assertEqual(cents("1.005"), 101)
        with self.assertRaises(ValueError):
            cents("NaN")


class EvidenceTests(IsolatedStore):
    def add(self, text="经营活动现金流同比改善，但存在减值风险。", seen="2026-09-16T02:00:00+00:00", **kw):
        return self.store.add_document(symbol="sh600519", kind="company_report", title="现金流风险", source="fixture",
             url="https://example.test/report", published_at="2026-09-15T02:00:00+00:00", pages=[(2, text)],
             raw_path="fixture", first_seen_at=seen, ready_at=seen, **kw)

    def test_point_in_time_and_chinese_retrieval(self):
        self.add(cloud_allowed=True)
        self.assertFalse(self.store.search("现金流 风险", "2026-09-16T01:59:59+00:00"))
        result = self.store.search("现金流 风险", "2026-09-16T10:00:01+08:00", "sh600519")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["page"], 2)
        self.assertFalse(self.store.search("现金流", "2026-09-16T02:00:01+00:00", "sz000333"))

    def test_dedup_revisions_and_cloud_permission(self):
        a, created = self.add()
        self.assertTrue(created)
        b, created = self.add()
        self.assertEqual(a, b)
        self.assertFalse(created)
        c, created = self.add(text="修订：现金流减少。", seen="2026-09-17T02:00:00+00:00")
        self.assertNotEqual(a, c)
        self.assertEqual(len(self.store.documents_as_of("2026-09-16T10:00:00+00:00")), 1)
        self.assertFalse(self.store.search("现金流", now(), cloud_only=True))

    def test_consistent_backup(self):
        self.add()
        path = self.store.backup()
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM documents").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_schema_quote_and_symbol_guard(self):
        packet = {"stocks": [{"symbol": "sh600519"}], "evidence": [{"evidence_id": "e1", "symbol": "sh600519", "text": "营业收入同比增长。"}]}
        result = {"summary": "测试", "stocks": [{"symbol": "sh600519", "action": "WATCH", "analysis": "需要核实", "facts": [{"evidence_id": "e1", "quote": "营业收入同比增长"}], "counterpoints": [], "missing_fields": [], "next_checks": []}]}
        self.assertEqual(validate_result(result, packet)["stocks"][0]["action"], "INSUFFICIENT_DATA")
        result["stocks"][0]["facts"][0]["quote"] = "营业收入翻倍"
        with self.assertRaises(ValueError):
            validate_result(result, packet)


class PipelineTests(unittest.TestCase):
    def test_job_deduplication_and_degraded_report(self):
        with tempfile.TemporaryDirectory() as temp:
            config = {"data_dir": temp, "watchlist": [{"symbol": "sh600519", "name": "演示"}], "watchlist_kind": "demo", "model_enabled": False}
            first = run(config, job_key="test-same", use_model=False, collect_data=False)
            second = run(config, job_key="test-same", use_model=False, collect_data=False)
            self.assertEqual(first["status"], "PARTIAL")
            self.assertEqual(second["status"], "ALREADY_DONE")
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertIn("未连接", Path(first["report"]).read_text())

    def test_no_paid_or_live_configuration(self):
        base = Path(__file__).resolve().parents[1] / "config.json"
        config = json.loads(base.read_text())
        for key in ("live_execution_enabled", "paid_api_fallback"):
            with tempfile.TemporaryDirectory() as tmp:
                changed = {**config, key: True}
                path = Path(tmp) / "bad.json"
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_sources_are_allowlisted(self):
        for url in ("file:///etc/passwd", "http://www.cninfo.com.cn/", "https://localhost/", "https://www.cninfo.com.cn.evil.example/", "https://www.cninfo.com.cn:9999/"):
            with self.assertRaises(ValueError):
                check_url(url)

    def test_today_is_not_complete_daily_bar(self):
        bars = [[f"2026-{m:02d}-{d:02d}", "10", "10", "10", "10", "100"] for m in (5, 6, 7) for d in range(1, 21)]
        bars.append(["2026-09-16", "999", "999", "999", "999", "100"])
        raw = json.dumps({"data": {"sz000333": {"qfqday": bars}}}).encode()
        result = history_summary(raw, "sz000333", "2026-09-16")
        self.assertEqual(result["ma20"], "10")
        self.assertEqual(result["last_complete_date"], "2026-07-20")


if __name__ == "__main__":
    unittest.main()
