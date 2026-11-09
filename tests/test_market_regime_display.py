"""R1 regime display support: vendored CSI300 golden file and confirmed_cap_pct."""
import json
import tempfile
import unittest
from pathlib import Path

from ashare import market_regime
from ashare.storage import Store, normalize_time as _utc

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'csi300_r1_reference.json'


def fixture():
    return json.loads(FIXTURE.read_text())


class MarketRegimeDisplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def put_series(self, bars, ready='2026-11-01T00:00:00+08:00'):
        self.store.db.execute(
            "INSERT OR IGNORE INTO runs VALUES('r1','r1-k','manual','2026-11-01',NULL,NULL,'SUCCEEDED',NULL,NULL,NULL)")
        self.store.db.execute('INSERT INTO comparison_series VALUES(?,?,?,?)',
                              ('r1', 'sh000300', _utc(ready), json.dumps({'bars': bars})))
        self.store.db.commit()

    def test_caps_match_fixture_every_day(self):
        data = fixture()
        closes = [b[2] for b in data['bars']]
        conf, raw = market_regime._caps(closes)
        expected = {d: (r, c) for d, r, c in data['expected']}
        for i, b in enumerate(data['bars']):
            exp = expected.get(b[0])
            if exp is None:
                self.assertIsNone(raw[i], b[0])
                self.assertIsNone(conf[i], b[0])
            else:
                self.assertEqual(raw[i], exp[0], b[0])
                self.assertEqual(conf[i], exp[1], b[0])

    def test_r1_ok_reports_confirmed_cap(self):
        data = fixture()
        self.put_series(data['bars'])
        res = market_regime.r1(self.store, _utc('2026-11-06T16:00:00+08:00'))
        self.assertEqual(res['status'], 'OK')
        self.assertEqual(res['as_of_day'], '2026-11-06')
        self.assertEqual(res['cap_pct'], data['expected'][-1][2])
        self.assertEqual(res['confirmed_cap_pct'], data['expected'][-1][2])
        self.assertEqual(len(res['series_sha256']), 64)

    def test_stale_keeps_confirmed_cap_but_caps_at_10(self):
        data = fixture()
        self.put_series(data['bars'])
        # Two trading days after the last bar: cap falls back to 10 while the
        # two-day-confirmed cap is still reported for display.
        res = market_regime.r1(self.store, _utc('2026-11-10T16:00:00+08:00'))
        self.assertEqual(res['status'], 'STALE')
        self.assertEqual(res['cap_pct'], 10)
        self.assertEqual(res['confirmed_cap_pct'], data['expected'][-1][2])

    def test_missing(self):
        res = market_regime.r1(self.store, _utc('2026-11-06T16:00:00+08:00'))
        self.assertEqual(res['status'], 'MISSING')
        self.assertEqual(res['cap_pct'], 10)
        self.assertIsNone(res['confirmed_cap_pct'])

    def test_today_bar_excluded_before_1505_beijing(self):
        data = fixture()
        bars = data['bars'] + [['2026-11-09', 9999.0, 9999.0, 9999.0, 9999.0]]
        self.put_series(bars)
        # Monday 10:00 Beijing: last completed day is Friday, today's bar does not count.
        res = market_regime.r1(self.store, _utc('2026-11-09T10:00:00+08:00'))
        self.assertEqual(res['as_of_day'], '2026-11-06')
        self.assertNotEqual(res['close'], 9999.0)


if __name__ == '__main__':
    unittest.main()
