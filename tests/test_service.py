import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from ashare import service


class LaunchdSequenceTests(unittest.TestCase):
    """Reinstalling right after a stop must wait for launchd to finish the old job instead of failing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        self.loaded_checks = 0

    def tearDown(self):
        self.tmp.cleanup()

    def fake_run(self, still_loaded, bootstrap_errors):
        def run(args, **kwargs):
            self.calls.append(args[1])
            if args[1] == 'print':
                self.loaded_checks += 1
                return SimpleNamespace(returncode=0 if self.loaded_checks <= still_loaded else 113, stdout='state = running', stderr='')
            if args[1] == 'bootstrap' and bootstrap_errors:
                bootstrap_errors.pop()
                return SimpleNamespace(returncode=5, stdout='', stderr='Bootstrap failed: 5: Input/output error')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        return run

    def manage(self, action, still_loaded=0, bootstrap_errors=()):
        config = {'data_dir': self.tmp.name, 'config_path': str(Path(self.tmp.name) / 'config.json'), 'ui_port': 8765}
        with patch.object(service.sys, 'platform', 'darwin'), \
             patch.object(service, 'plist_path', return_value=Path(self.tmp.name) / 'agent.plist'), \
             patch.object(service.subprocess, 'run', side_effect=self.fake_run(still_loaded, list(bootstrap_errors))), \
             patch.object(service.time, 'sleep'):
            return service.manage(config, action)

    def test_install_waits_for_the_old_job_then_loads(self):
        result = self.manage('install', still_loaded=3)
        self.assertEqual(result['status'], 'INSTALLED')
        self.assertEqual(self.calls.count('bootout'), 1)
        self.assertEqual(self.calls[-1], 'bootstrap')
        self.assertGreater(self.calls.index('bootstrap'), max(i for i, c in enumerate(self.calls) if c == 'print'))

    def test_bootstrap_retries_the_transient_input_output_error(self):
        result = self.manage('install', bootstrap_errors=['EIO'])
        self.assertEqual(result['status'], 'INSTALLED')
        self.assertEqual(self.calls.count('bootstrap'), 2)

    def test_uninstall_reports_whether_the_job_stopped(self):
        (Path(self.tmp.name) / 'agent.plist').write_bytes(service.plistlib.dumps({'WorkingDirectory': str(Path(service.__file__).resolve().parents[1])}))
        result = self.manage('uninstall', still_loaded=2)
        self.assertEqual(result, {'status': 'UNINSTALLED', 'data_preserved': True, 'stopped': True})
        self.assertFalse((Path(self.tmp.name) / 'agent.plist').exists())


if __name__ == '__main__':
    unittest.main()
