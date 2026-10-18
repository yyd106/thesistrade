import ssl
import subprocess
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from ashare import public_tls


class PublicTLSTests(unittest.TestCase):
    def setUp(self):
        public_tls._macos_root_context.cache_clear()
        self.addCleanup(public_tls._macos_root_context.cache_clear)

    def process(self, chunks, returncode=0):
        process = MagicMock()
        process.__enter__.return_value = process
        process.poll.return_value = returncode
        process.wait.return_value = returncode
        process.stdout.fileno.return_value = 23
        popen = patch.object(public_tls.subprocess, "Popen", return_value=process)
        ready = patch.object(public_tls.select, "select", return_value=([process.stdout], [], []))
        read = patch.object(public_tls.os, "read", side_effect=chunks)
        return process, popen, ready, read

    def test_only_fixed_public_system_keychain_command_is_read(self):
        process, popen, ready, read = self.process([b"public PEM", b""])
        with popen as run, ready, read as reader:
            self.assertEqual(public_tls._system_root_pem(), "public PEM")
        run.assert_called_once_with(
            ("/usr/bin/security", "find-certificate", "-a", "-p",
             "/System/Library/Keychains/SystemRootCertificates.keychain"),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.assertEqual(reader.call_args_list[0].args, (23, 65536))
        self.assertLessEqual(process.wait.call_args.kwargs["timeout"], 5)
        process.kill.assert_not_called()

    def test_non_macos_does_not_read_certificates_or_create_context(self):
        for platform in ("linux", "win32"):
            with self.subTest(platform=platform), patch.object(public_tls.sys, "platform", platform), \
                    patch.object(public_tls.subprocess, "Popen") as run, \
                    patch.object(public_tls.ssl, "create_default_context") as create:
                self.assertIsNone(public_tls.system_root_context())
                run.assert_not_called()
                create.assert_not_called()

    def test_success_is_cached_and_verification_remains_enabled(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with patch.object(public_tls.sys, "platform", "darwin"), \
                patch.object(public_tls.ssl, "create_default_context", return_value=context) as create, \
                patch.object(public_tls, "_system_root_pem", return_value="public PEM") as read, \
                patch.object(ssl.SSLContext, "load_verify_locations") as load:
            self.assertIs(public_tls.system_root_context(), context)
            self.assertIs(public_tls.system_root_context(), context)
        create.assert_called_once_with()
        read.assert_called_once_with()
        load.assert_called_once_with(cadata="public PEM")
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_failure_is_not_cached_and_next_attempt_can_succeed(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with patch.object(public_tls.sys, "platform", "darwin"), \
                patch.object(public_tls.ssl, "create_default_context", return_value=context), \
                patch.object(public_tls, "_system_root_pem", side_effect=[OSError("unavailable"), "PEM"]) as read, \
                patch.object(ssl.SSLContext, "load_verify_locations"):
            self.assertIsNone(public_tls.system_root_context())
            self.assertIs(public_tls.system_root_context(), context)
        self.assertEqual(read.call_count, 2)

    def test_unverified_context_is_rejected_before_read(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with patch.object(public_tls.sys, "platform", "darwin"), \
                patch.object(public_tls.ssl, "create_default_context", return_value=context), \
                patch.object(public_tls, "_system_root_pem") as read:
            self.assertIsNone(public_tls.system_root_context())
        read.assert_not_called()

    def test_oversized_output_is_rejected_and_process_is_killed(self):
        process, popen, ready, read = self.process([b"123456"], returncode=None)
        with popen, ready, read as reader, patch.object(public_tls, "_MAX_ROOT_BYTES", 5):
            with self.assertRaises(ValueError):
                public_tls._system_root_pem()
        reader.assert_called_once_with(23, 6)
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=1)

    def test_stalled_output_times_out_and_process_is_killed(self):
        process, popen, _, read = self.process([], returncode=None)
        with popen, read as reader, \
                patch.object(public_tls.select, "select", return_value=([], [], [])):
            with self.assertRaises(subprocess.TimeoutExpired):
                public_tls._system_root_pem()
        process.kill.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=1)
        reader.assert_not_called()

    def test_empty_non_ascii_and_nonzero_exit_are_rejected(self):
        for chunks, code, error in (([b""], 0, ValueError),
                                    ([b"\xff", b""], 0, UnicodeDecodeError),
                                    ([b"PEM", b""], 1, subprocess.CalledProcessError)):
            with self.subTest(chunks=chunks, code=code):
                _, popen, ready, read = self.process(chunks, returncode=code)
                with popen, ready, read, self.assertRaises(error):
                    public_tls._system_root_pem()

    def test_load_failure_keeps_original_fetch_error(self):
        from ashare import dynamic_sources

        original = urllib.error.URLError(ssl.SSLCertVerificationError("original verification error"))
        opener = MagicMock()
        opener.open.side_effect = original
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with patch.object(public_tls.sys, "platform", "darwin"), \
                patch.object(public_tls.ssl, "create_default_context", return_value=context), \
                patch.object(public_tls, "_system_root_pem", return_value="invalid PEM"), \
                patch.object(ssl.SSLContext, "load_verify_locations", side_effect=ssl.SSLError("load failed")), \
                patch.object(dynamic_sources.urllib.request, "build_opener", return_value=opener) as build:
            with self.assertRaises(urllib.error.URLError) as raised:
                dynamic_sources.fetch("https://www.ecb.europa.eu/rss/press.html")
        self.assertIs(raised.exception, original)
        self.assertEqual(build.call_count, 1)


if __name__ == "__main__":
    unittest.main()
