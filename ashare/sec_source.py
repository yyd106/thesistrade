"""SEC-only transport. Contact input is read-only; no configuration writer is authorized here.

All local workers using one data directory share the same conservative request budget.
This does not coordinate another machine or unrelated applications using SEC.
"""
import fcntl
import html
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from pathlib import Path

HOSTS = frozenset(('www.sec.gov', 'data.sec.gov'))
CONTACT_KEY = 'sec_contact_email'
MIN_INTERVAL = 0.5  # At most two starts/second across this data directory; no burst.


class SECUnavailable(ValueError):
    """Only fixed codes cross the transport boundary, never a request/response or contact."""


def contact(config):
    value = config.get(CONTACT_KEY)
    if (isinstance(value, str) and len(value) <= 254 and
            re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]{0,63}@'
                         r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
                         r'(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+', value)):
        local = value.split('@', 1)[0]
        if not local.endswith('.') and '..' not in local:
            return value
    return None


def availability(config):
    if config.get('sec_enabled') is not True:
        return 'SEC_DISABLED'
    if config.get('deployment_role') == 'cloud':
        return 'SEC_RESEARCH_NODE_ONLY'
    if not contact(config):
        return 'SEC_CONTACT_REQUIRED'
    return 'READY'


def public_config(config):
    """Contact values never enter config show or prospective run snapshots."""
    result = {k: v for k, v in config.items() if k != CONTACT_KEY}
    if CONTACT_KEY in config:
        result['sec_contact_configured'] = contact(config) is not None
    return result


def reject_private_value(value, private_contact):
    """Also apply after the source's full decoding/parsing chain, before persistence."""
    if isinstance(value, str):
        # Privacy matching is deliberately conservative for the whole address, including
        # local-part case. Bound repeated entity decoding; never pass an unfinished chain
        # to a later renderer that could expose a contact we did not get to inspect.
        folded = private_contact.casefold()
        for _ in range(8):
            if folded in value.casefold():
                raise SECUnavailable('SEC_PRIVATE_CONTACT_IN_RESPONSE')
            decoded = html.unescape(value)
            if decoded == value:
                return
            value = decoded
        if folded in value.casefold():
            raise SECUnavailable('SEC_PRIVATE_CONTACT_IN_RESPONSE')
        if html.unescape(value) != value:
            raise SECUnavailable('SEC_RESPONSE_DECODING_LIMIT')
    if isinstance(value, dict):
        for key, item in value.items():
            reject_private_value(key, private_contact)
            reject_private_value(item, private_contact)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_private_value(item, private_contact)


def reject_private_echo(body, private_contact):
    """Reject common wire encodings; source adapters check parsed values again."""
    text = body.decode('utf-8-sig')
    reject_private_value(text, private_contact)
    if text.lstrip().startswith(('{', '[')):
        try:
            decoded = json.loads(text)
        except ValueError:
            return  # The source parser separately rejects malformed JSON.
        reject_private_value(decoded, private_contact)


def check_url(url):
    try:
        p = urllib.parse.urlsplit(url)
        valid = (p.scheme == 'https' and p.hostname in HOSTS and p.username is None and
                 p.password is None and p.port in (None, 443) and
                 not any(ord(c) < 32 or ord(c) == 127 for c in url))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise SECUnavailable('SEC_URL_REJECTED')


class SharedBudget:
    """Separate sidecar lock survives atomic state replacement; no private data is stored."""
    def __init__(self, root, *, clock=time.time, sleep=time.sleep):
        self.root = Path(root) / 'cache'
        self.clock, self.sleep = clock, sleep

    @contextmanager
    def locked(self, name='sec-budget.lock'):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / name
        if path.is_symlink():
            raise SECUnavailable('SEC_BUDGET_UNAVAILABLE')
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a+b') as handle:
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise SECUnavailable('SEC_BUSY') from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def serialized(self):
        # One active transport prevents delayed reserved callers from starting in a burst.
        # Redirect/retry accounting uses the separate short-lived budget lock.
        return self.locked('sec-fetch.lock')

    def read(self):
        path = self.root / 'sec-budget.json'
        if path.is_symlink():
            raise SECUnavailable('SEC_BUDGET_UNAVAILABLE')
        if not path.exists():
            return {'next_at': 0, 'blocked_until': 0}
        try:
            value = json.loads(path.read_text())
            if not isinstance(value, dict) or set(value) != {'next_at', 'blocked_until'}:
                raise ValueError()
            if any(type(x) not in (float, int) or not math.isfinite(x) or x < 0 for x in value.values()):
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise SECUnavailable('SEC_BUDGET_UNAVAILABLE') from None

    def write(self, state):
        fd, name = tempfile.mkstemp(prefix='.sec-budget-', dir=self.root)
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump(state, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.root / 'sec-budget.json')
        finally:
            Path(name).unlink(missing_ok=True)

    def reserve(self):
        with self.locked():
            state = self.read()
            now = self.clock()
            if state['blocked_until'] > now:
                raise SECUnavailable('SEC_BACKOFF')
            delay = max(0, state['next_at'] - now)
            # Clock rollback or corrupt future reservation must not hang a worker or burst.
            if delay > 2:
                raise SECUnavailable('SEC_BUDGET_CLOCK_UNCERTAIN')
            # A forward wall-clock adjustment must not erase request spacing. The transport
            # lock is already held, so this sleep separates actual requests, not just slots.
            self.sleep(max(MIN_INTERVAL, delay))
            state['next_at'] = max(self.clock(), state['next_at']) + MIN_INTERVAL
            self.write(state)

    def backoff(self, retry_after=None):
        now = self.clock()
        seconds = 60.0
        try:
            if retry_after and str(retry_after).isdigit():
                seconds = max(seconds, float(retry_after))
            elif retry_after:
                seconds = max(seconds, parsedate_to_datetime(retry_after).timestamp() - now)
            if not math.isfinite(seconds):
                seconds = 86400.0
        except (TypeError, ValueError, OverflowError):
            pass
        # Long Retry-After values cause future callers to defer, not a long blocking sleep.
        with self.locked():
            state = self.read()
            state['blocked_until'] = max(state['blocked_until'], now + seconds)
            self.write(state)


class SECRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, budget):
        super().__init__()
        self.budget, self.redirects = budget, 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)  # Reject before a private header can reach another allowed source.
        self.redirects += 1
        if self.redirects > 3:
            raise SECUnavailable('SEC_REDIRECT_LIMIT')
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            self.budget.reserve()  # Redirects count against the same cross-process budget.
        return redirected


class Client:
    def __init__(self, config, root, *, opener_factory=urllib.request.build_opener, budget=None):
        state = availability(config)
        if state != 'READY':
            raise SECUnavailable(state)
        self._contact = contact(config)
        self._opener_factory = opener_factory
        self.budget = budget if budget is not None else SharedBudget(root)

    def fetch(self, url, *, max_bytes=20_000_000):
        check_url(url)
        try:
            with self.budget.serialized():
                return self._fetch(url, max_bytes)
        except SECUnavailable:
            raise
        except Exception:
            raise SECUnavailable('SEC_FETCH_FAILED') from None

    def _fetch(self, url, max_bytes):
        try:
            for attempt in range(2):
                self.budget.reserve()
                request = urllib.request.Request(url, headers={
                    'User-Agent': 'ThesisTrade personal research ' + self._contact,
                    'Accept-Encoding': 'identity'})
                opener = self._opener_factory(SECRedirect(self.budget))
                try:
                    with opener.open(request, timeout=12) as response:
                        check_url(response.url)
                        if response.headers.get('Content-Encoding', 'identity').lower() not in ('', 'identity'):
                            raise SECUnavailable('SEC_CONTENT_ENCODING_UNSUPPORTED')
                        if int(response.headers.get('Content-Length', '0')) > max_bytes:
                            raise SECUnavailable('SEC_RESPONSE_TOO_LARGE')
                        chunks, total, deadline = [], 0, time.monotonic() + 30
                        while True:
                            if time.monotonic() > deadline:
                                raise SECUnavailable('SEC_DOWNLOAD_TIMEOUT')
                            chunk = response.read(min(65536, max_bytes + 1 - total))
                            if not chunk:
                                break
                            chunks.append(chunk)
                            total += len(chunk)
                            if total > max_bytes:
                                raise SECUnavailable('SEC_RESPONSE_TOO_LARGE')
                        body = b''.join(chunks)
                        reject_private_echo(body, self._contact)
                        return body
                except urllib.error.HTTPError as exc:
                    code, retry_after = exc.code, exc.headers.get('Retry-After') if exc.headers else None
                    exc.close()
                    if code in (403, 429):
                        self.budget.backoff(retry_after)
                        raise SECUnavailable('SEC_HTTP_' + str(code)) from None
                    if code < 500 or attempt:
                        raise SECUnavailable('SEC_HTTP_FAILURE') from None
                except (OSError, TimeoutError):
                    if attempt:
                        raise SECUnavailable('SEC_NETWORK_FAILURE') from None
                time.sleep(1)
        except SECUnavailable:
            raise
        except Exception:
            # urllib/proxy/parser exceptions can include headers, URLs or response bodies.
            raise SECUnavailable('SEC_FETCH_FAILED') from None
