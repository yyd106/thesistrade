"""exposure_caps bundle validation and the cloud's incoming-vs-stored cap rule."""
import json
import tempfile
import unittest
from unittest.mock import patch

from ashare import build, cloud_sync, exposure
from ashare.cloud_runtime import put, value
from ashare.storage import Store

AT = '2026-11-06T08:30:00+00:00'  # Friday 16:30 Beijing; last completed day 2026-11-06
CFG = {'paper_max_gross_pct': 90, 'watchlist': [], 'industry_enabled': False,
       'deployment_role': 'cloud'}


def r1(cap=50, as_of='2026-11-06', status='OK'):
    return {'method': 'R1-v1', 'status': status, 'cap_pct': cap, 'raw_cap_pct': cap,
            'confirmed_cap_pct': cap, 'as_of_day': as_of, 'series_sha256': 'a' * 64}


class ValidateBundleTests(unittest.TestCase):
    def test_good_bundles_pass(self):
        exposure.validate_bundle({'mode': 'OFF', 'regime': None})
        exposure.validate_bundle({'mode': 'R1', 'regime': r1()})
        # r1() extras (close/ma20/ma60/series_ready_at) are allowed.
        reg = {**r1(), 'close': 4054.28, 'ma20': 4112.9, 'ma60': 4404.8,
               'series_ready_at': '2026-11-06T10:30:00+00:00'}
        exposure.validate_bundle({'mode': 'R1', 'regime': reg})
        reg = {**r1(), 'raw_cap_pct': None, 'confirmed_cap_pct': None, 'as_of_day': None,
               'series_sha256': None, 'status': 'MISSING'}
        exposure.validate_bundle({'mode': 'R1', 'regime': reg})

    def test_malformed_bundles_raise(self):
        bad = [{'mode': 'X', 'regime': None},
               {'mode': 'OFF', 'regime': r1()},
               {'mode': 'R1'},
               {'mode': 'R1', 'regime': {**r1(), 'cap_pct': 33}},
               {'mode': 'R1', 'regime': {**r1(), 'series_sha256': 'ZZZ'}},
               {'mode': 'R1', 'regime': {**r1(), 'as_of_day': '2026/11/06'}},
               {'mode': 'R1', 'regime': {**r1(), 'mystery': 1}},
               {'mode': 'R1', 'regime': {k: v for k, v in r1().items() if k != 'status'}},
               'not-a-dict', {'mode': 'R1', 'regime': None, 'extra': 1}]
        for bundle in bad:
            with self.assertRaises(ValueError, msg=bundle):
                exposure.validate_bundle(bundle)


class RealR1OutputTests(unittest.TestCase):
    """Every real r1() output must survive strict bundle validation (Q2 missing-data path)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)

    def put_series(self, bars, ready='2025-01-01T00:00:00+08:00'):
        from ashare.storage import normalize_time as _utc
        self.store.db.execute("INSERT OR IGNORE INTO runs VALUES('r1','r1-k','manual','2025-01-01',NULL,NULL,'SUCCEEDED',NULL,NULL,NULL)")
        self.store.db.execute('INSERT INTO comparison_series VALUES(?,?,?,?)',
                              ('r1', 'sh000300', _utc(ready), json.dumps({'bars': bars})))
        self.store.db.commit()

    def test_all_r1_paths_validate(self):
        from ashare import market_regime
        fixture = json.loads((__import__('pathlib').Path(__file__).parent /
                              'fixtures' / 'csi300_r1_reference.json').read_text())
        self.put_series([{'date': b[0], 'open': b[1], 'close': b[2], 'high': b[3], 'low': b[4], 'volume': 1}
                         for b in fixture['bars']])
        ok = market_regime.r1(self.store, '2026-11-06T08:30:00+00:00')
        stale = market_regime.r1(self.store, '2026-11-10T08:30:00+00:00')
        for res in (ok, stale):
            exposure.validate_bundle({'mode': 'R1', 'regime': res})
        self.assertEqual(ok['status'], 'OK')
        self.assertEqual(exposure.bundle_cap_pct({'mode': 'R1', 'regime': ok}, CFG, '2026-11-06T08:30:00+00:00')[0],
                         ok['confirmed_cap_pct'])
        self.assertEqual(stale['status'], 'STALE')
        self.assertEqual(exposure.bundle_cap_pct({'mode': 'R1', 'regime': stale}, CFG, '2026-11-10T08:30:00+00:00')[0], 10)
        store2 = Store(self.tmp.name + '-2')
        self.addCleanup(store2.close)
        missing = market_regime.r1(store2, '2026-11-06T08:30:00+00:00')
        self.assertEqual(missing['status'], 'MISSING')
        exposure.validate_bundle({'mode': 'R1', 'regime': missing})
        self.assertIsNone(missing['close'])
        self.assertIsNone(missing['series_ready_at'])
        self.assertEqual(exposure.bundle_cap_pct({'mode': 'R1', 'regime': missing}, CFG, '2026-11-06T08:30:00+00:00')[0], 10)
        # Short series (< MIN_BARS): fields degrade to None but still validate.
        self.store2 = store2
        self.store2.db.execute("INSERT OR IGNORE INTO runs VALUES('r1','r1-k','manual','2025-01-01',NULL,NULL,'SUCCEEDED',NULL,NULL,NULL)")
        self.store2.db.execute('INSERT INTO comparison_series VALUES(?,?,?,?)',
                               ('r1', 'sh000300', '2025-01-01T00:00:00+00:00',
                                json.dumps({'bars': [{'date': '2026-11-05', 'close': 4000.0}]})))
        self.store2.db.commit()
        short = market_regime.r1(store2, '2026-11-06T08:30:00+00:00')
        exposure.validate_bundle({'mode': 'R1', 'regime': short})
        self.assertEqual(exposure.bundle_cap_pct({'mode': 'R1', 'regime': short}, CFG, '2026-11-06T08:30:00+00:00')[0], 10)


class BundleCapTests(unittest.TestCase):
    def test_off_and_none_give_configured_cap(self):
        for bundle in (None, {}, {'mode': 'OFF', 'regime': None},
                       {'mode': 'OFF', 'regime': None, 'source': 'LEGACY_PUBLICATION'}):
            self.assertEqual(exposure.bundle_cap_pct(bundle, CFG, AT)[0], 90)

    def test_r1_caps_and_staleness_on_cloud_clock(self):
        for cap in (10, 50, 90):
            self.assertEqual(exposure.bundle_cap_pct(
                {'mode': 'R1', 'regime': r1(cap)}, CFG, AT)[0], cap)
        # as_of_day older than the last completed day at `at` counts as 10.
        self.assertEqual(exposure.bundle_cap_pct(
            {'mode': 'R1', 'regime': r1(90, as_of='2026-11-05')}, CFG, AT)[0], 10)
        self.assertEqual(exposure.bundle_cap_pct(
            {'mode': 'R1', 'regime': r1(90, as_of=None)}, CFG, AT)[0], 10)

    def test_gross_cap_cloud_uses_stored_bundle(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(tmp.name)
        self.addCleanup(store.close)
        with patch('ashare.exposure.regime_active', return_value=True):
            put(store, 'exposure_caps', {'mode': 'R1', 'regime': r1(50)})
            self.assertEqual(exposure.gross_cap_pct(store, CFG, AT)[0], 50)
            put(store, 'exposure_caps', {'mode': 'R1', 'regime': r1(90, as_of='2026-11-04')})
            self.assertEqual(exposure.gross_cap_pct(store, CFG, AT)[0], 10)
            put(store, 'exposure_caps', {'mode': 'OFF', 'regime': None})
            self.assertEqual(exposure.gross_cap_pct(store, CFG, AT)[0], 90)


class ReceiveStrategyBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.close)
        put(self.store, 'initialized', True)
        put(self.store, 'exposure_caps', {'mode': 'R1', 'regime': r1(10)})

    def test_malformed_incoming_rejected_stored_untouched(self):
        before = value(self.store, 'exposure_caps')
        for bad in ({'mode': 'X', 'regime': None}, {'mode': 'OFF', 'regime': r1()},
                    {'mode': 'R1', 'regime': {**r1(), 'cap_pct': 33}}):
            with self.assertRaises(ValueError):
                cloud_sync.receive_strategy(self.store, CFG, {'exposure_caps': bad}, AT)
        self.assertEqual(value(self.store, 'exposure_caps'), before)

    def test_legacy_marker_is_off(self):
        cap, detail = exposure.bundle_cap_pct(
            {'mode': 'OFF', 'regime': None, 'source': 'LEGACY_PUBLICATION'}, CFG, AT)
        self.assertEqual((cap, detail['regime']), (90, None))


class ReceiveStrategyCapsTests(__import__('test_cloud_sync').CloudSyncTests):
    """RC-02 through the real publication path: the cloud must validate an incoming
    package against the package's own cap, not the previously stored one."""

    def valid_regime(self, cap, as_of=None):
        from ashare.calendar import last_completed_day
        return {'method': 'R1-v1', 'status': 'OK', 'cap_pct': cap, 'raw_cap_pct': cap,
                'confirmed_cap_pct': cap, 'as_of_day': as_of or last_completed_day(self.at),
                'series_sha256': 'a' * 64}

    def publish(self, cfg_regime, regime, targets=500):
        """Real research publication carrying exposure_caps (or OFF)."""
        self.cfg['exposure_regime'] = cfg_regime
        with self.store.db:
            put(self.store, 'remote_features', ['exposure_caps_v1'])
        for sym in ('ETH', 'GOLD'):
            self.quote(sym)
            self.plan(sym)
        overrides = {'global:' + s: {'action': 'ALLOW', 'target_bps': targets}
                     for s in ('BTC', 'ETH', 'GOLD')}
        with patch('ashare.market_regime.r1', return_value=regime), \
             patch('ashare.exposure.r1', return_value=regime):
            return self.publication(overrides=overrides)

    def test_upgrade_from_10_to_50_accepted_with_own_cap(self):
        with self.cloud.db:
            put(self.cloud, 'exposure_caps', {'mode': 'R1', 'regime': self.valid_regime(10)})
        body = self.publish('R1', self.valid_regime(50))
        self.assertEqual(body['exposure_caps']['regime']['cap_pct'], 50)
        self.assertEqual(self.receive(body)['status'], 'ACCEPTED')
        self.assertEqual(value(self.cloud, 'exposure_caps')['regime']['cap_pct'], 50)

    def test_off_package_accepted_against_configured_cap(self):
        with self.cloud.db:
            put(self.cloud, 'exposure_caps', {'mode': 'R1', 'regime': self.valid_regime(10)})
        body = self.publish('OFF', None)
        self.assertEqual(body['exposure_caps'], {'mode': 'OFF', 'regime': None})
        self.assertEqual(self.receive(body)['status'], 'ACCEPTED')
        self.assertEqual(value(self.cloud, 'exposure_caps')['mode'], 'OFF')

    def test_downgrade_to_10_rejected_and_stored_unchanged(self):
        # Research never publishes 15% targets under a 10% cap; the defence still has
        # to exist on the cloud, so the accepted package's bundle is swapped for a
        # valid-but-tighter one after publication (as a tampered/older build would send).
        with self.cloud.db:
            put(self.cloud, 'exposure_caps', {'mode': 'R1', 'regime': self.valid_regime(50)})
        body = self.publish('R1', self.valid_regime(50))
        body['exposure_caps'] = {'mode': 'R1', 'regime': self.valid_regime(10)}
        before = value(self.cloud, 'exposure_caps')
        with self.assertRaises(ValueError):
            self.receive(body)
        self.assertEqual(value(self.cloud, 'exposure_caps'), before)

    def test_stale_incoming_rejected_by_cloud_clock(self):
        body = self.publish('R1', self.valid_regime(50))
        body['exposure_caps'] = {'mode': 'R1', 'regime': self.valid_regime(90, as_of='2026-09-10')}
        with self.assertRaises(ValueError):
            self.receive(body)
        self.assertNotEqual(value(self.cloud, 'exposure_caps'), body['exposure_caps'])

    def test_legacy_package_without_key_accepted_as_off(self):
        body = self.publish('R1', self.valid_regime(50))
        del body['exposure_caps']
        self.assertEqual(self.receive(body)['status'], 'ACCEPTED')
        self.assertEqual(value(self.cloud, 'exposure_caps'),
                         {'mode': 'OFF', 'regime': None, 'source': 'LEGACY_PUBLICATION'})

    def test_malformed_bundle_rejected_stored_unchanged(self):
        with self.cloud.db:
            put(self.cloud, 'exposure_caps', {'mode': 'R1', 'regime': self.valid_regime(10)})
        body = self.publish('R1', self.valid_regime(50))
        before = value(self.cloud, 'exposure_caps')
        for bad in ({'mode': 'X', 'regime': None}, {'mode': 'OFF', 'regime': body['exposure_caps']['regime']},
                    {'mode': 'R1', 'regime': {**self.valid_regime(50), 'cap_pct': 33}}):
            tampered = dict(body, exposure_caps=bad)
            with self.assertRaises(ValueError):
                self.receive(tampered)
        self.assertEqual(value(self.cloud, 'exposure_caps'), before)


class BuildIdentityTests(unittest.TestCase):
    def test_exposure_regime_is_a_strategy_key(self):
        self.assertIn('exposure_regime', build.STRATEGY_KEYS)
        base = {'strategy_version': 'paper_baseline_v1', 'mode': 'paper'}
        self.assertNotEqual(build.config_fingerprint({**base, 'exposure_regime': 'R1'}),
                            build.config_fingerprint(base))


if __name__ == '__main__':
    unittest.main()
