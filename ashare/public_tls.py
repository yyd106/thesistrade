"""Optional macOS public system roots for verified public HTTPS requests."""
from __future__ import annotations

from functools import lru_cache
import os
import select
import ssl
import subprocess
import sys
import time


_ROOT_COMMAND = (
    "/usr/bin/security", "find-certificate", "-a", "-p",
    "/System/Library/Keychains/SystemRootCertificates.keychain",
)
_MAX_ROOT_BYTES = 4_000_000
_ROOT_TIMEOUT = 5


def _system_root_pem():
    """Read only public certificates, with bounded memory and process time."""
    deadline = time.monotonic() + _ROOT_TIMEOUT
    process = subprocess.Popen(
        _ROOT_COMMAND, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        data = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select(
                [process.stdout], [], [], remaining
            )[0]:
                raise subprocess.TimeoutExpired(_ROOT_COMMAND, _ROOT_TIMEOUT)
            chunk = os.read(
                process.stdout.fileno(),
                min(65536, _MAX_ROOT_BYTES + 1 - len(data)),
            )
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > _MAX_ROOT_BYTES:
                raise ValueError("System root certificate output exceeds limit")
        result = process.wait(timeout=max(0, deadline - time.monotonic()))
        if result:
            raise subprocess.CalledProcessError(result, _ROOT_COMMAND)
        if not data:
            raise ValueError("System root certificate output is empty")
        return data.decode("ascii")
    finally:
        try:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=1)
        finally:
            process.stdout.close()


@lru_cache(maxsize=1)
def _macos_root_context():
    # Exceptions are not cached; a transient read failure can recover later.
    context = ssl.create_default_context()
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
        raise ValueError("TLS verification is required")
    context.load_verify_locations(cadata=_system_root_pem())
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
        raise ValueError("TLS verification is required")
    return context


def system_root_context():
    """Return a verified context, or None so callers retain their original error.

    Only the immutable macOS system root store is read. No user keychains,
    private keys, persistent files, or global SSL defaults are accessed/changed.
    Callers must not modify the cached context.
    """
    if sys.platform != "darwin":
        return None
    try:
        return _macos_root_context()
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
