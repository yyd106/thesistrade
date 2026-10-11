"""Synthetic LaunchAgent installation checks; never call the host's launchctl."""
import plistlib
import subprocess
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
             patch.object(service.Path, 'home', return_value=Path(self.tmp.name)), \
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


class ModelShutdownTests(unittest.TestCase):
    """Stopping the service signals each model session; a group macOS will not signal (EPERM, only a
    zombie left) must not abort the shutdown or the remaining sessions (seen 2026-09-26 14:37)."""

    def test_permission_error_falls_back_to_the_child_and_never_raises(self):
        from ashare import model
        signalled = []

        class Proc:
            def __init__(self, pid):self.pid = pid
            def poll(self):return None
            def send_signal(self, sig):signalled.append(('child', self.pid))

        def killpg(pid, sig):
            if pid == 1:raise PermissionError(1, 'Operation not permitted')
            if pid == 2:raise ProcessLookupError(3, 'No such process')
            signalled.append(('group', pid))

        procs = {Proc(1), Proc(2), Proc(3)}
        with patch.object(model, '_processes', procs), patch('ashare.model.os.killpg', side_effect=killpg):
            try:
                model.cancel_models()
            finally:
                model._stopping.clear()
        self.assertEqual(sorted(signalled), [('child', 1), ('child', 2), ('group', 3)])


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.data = self.home / 'Documents' / 'synthetic-data'
        self.logs = self.home / 'Library' / 'Logs' / 'ThesisTrade'
        self.plist = self.home / 'Library' / 'LaunchAgents' / (service.LABEL + '.plist')
        self.project = Path(service.__file__).resolve().parents[1]
        self.config = {'data_dir': str(self.data), 'config_path': str(self.home / 'synthetic-config.json'), 'ui_port': 8765}
        success = subprocess.CompletedProcess([], 0, 'state = running\n', '')
        for target, kwargs in (
            ('ashare.service.Path.home', {'return_value': self.home}),
            ('ashare.service.sys.platform', {'new': 'darwin'}),
            ('ashare.service.subprocess.run', {'return_value': success}),
            ('ashare.service.loaded', {'return_value': False}),
            ('ashare.service.wait_unloaded', {'return_value': True}),
            ('ashare.service.bootstrap', {'return_value': success}),
        ):
            mocked = self.enter_context(patch(target, **kwargs))
            if target.endswith('subprocess.run'):
                self.commands = mocked
            elif target.endswith('bootstrap'):
                self.bootstrap = mocked
            elif target.endswith('.loaded'):
                self.loaded = mocked

    def enter_context(self, manager):
        value = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return value

    def existing(self, project=None, logs=None):
        content = {'Label': service.LABEL, 'WorkingDirectory': str(project or self.project),
                   'StandardOutPath': str((logs or self.logs) / 'service.log'),
                   'StandardErrorPath': str((logs or self.logs) / 'service-error.log')}
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        self.plist.write_bytes(plistlib.dumps(content))
        return self.plist.read_bytes()

    def assert_standard_logs(self):
        content = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(content['StandardOutPath'], str(self.logs / 'service.log'))
        self.assertEqual(content['StandardErrorPath'], str(self.logs / 'service-error.log'))
        self.assertTrue(self.logs.is_dir())
        self.assertEqual(self.plist.stat().st_mode & 0o777, 0o600)

    def test_fresh_install_uses_library_logs_without_creating_data_logs(self):
        result = service.manage(self.config, 'install')
        self.assert_standard_logs()
        self.assertEqual(result['logs'], str(self.logs))
        self.assertFalse(self.data.exists())
        self.bootstrap.assert_called_once_with('gui/' + str(service.os.getuid()), self.plist)
        self.commands.assert_not_called()

    def test_upgrade_migrates_legacy_destination_without_touching_old_logs(self):
        old = self.data / 'logs'
        old.mkdir(parents=True)
        for name in ('service.log', 'service-error.log'):
            (old / name).write_text('synthetic historical output\n')
        self.existing(logs=old)
        service.manage(self.config, 'install')
        self.assert_standard_logs()
        for name in ('service.log', 'service-error.log'):
            self.assertEqual((old / name).read_text(), 'synthetic historical output\n')

    def test_reinstall_keeps_the_working_library_destination(self):
        self.existing()
        self.logs.mkdir(parents=True)
        output = self.logs / 'service.log'
        output.write_text('synthetic existing output\n')
        service.manage(self.config, 'install')
        self.assert_standard_logs()
        self.assertEqual(output.read_text(), 'synthetic existing output\n')
        self.assertFalse(self.data.exists())

    def test_non_install_actions_do_not_create_log_or_data_directories(self):
        for action in ('status', 'restart', 'uninstall'):
            with self.subTest(action=action):
                service.manage(self.config, action)
                self.assertFalse(self.logs.exists())
                self.assertFalse(self.data.exists())

    def test_foreign_workspace_is_rejected_without_creating_logs_or_stopping_service(self):
        before = self.existing(project=self.home / 'other-project')
        with self.assertRaisesRegex(RuntimeError, '同名服务属于其他目录'):
            service.manage(self.config, 'install')
        self.assertEqual(self.plist.read_bytes(), before)
        self.assertFalse(self.logs.exists())
        self.loaded.assert_not_called()
        self.bootstrap.assert_not_called()
        self.commands.assert_not_called()

    def test_unwritable_logs_fail_before_stopping_or_replacing_existing_service(self):
        before = self.existing()
        original = Path.mkdir

        def mkdir(path, *args, **kwargs):
            if path == self.logs:
                raise PermissionError('synthetic log directory unavailable')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'mkdir', mkdir), self.assertRaises(PermissionError):
            service.manage(self.config, 'install')
        self.assertEqual(self.plist.read_bytes(), before)
        self.loaded.assert_not_called()
        self.bootstrap.assert_not_called()
        self.commands.assert_not_called()


if __name__ == '__main__':
    unittest.main()
