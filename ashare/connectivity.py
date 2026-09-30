"""Whether the research node is online, judged from the cloud ledger sync that runs every minute.

Research does not have to run on schedule (user decision, 2026-09-26). While this machine has no
network, work that needs it waits; a collection already running stops before its next stock instead
of timing out source after source; and after the connection returns the scheduler re-runs the latest
research (scheduler.schedule_reconnected). The cloud keeps its hard rules either way: with no new
portfolio decision for 12 hours it stops new buys.

Offline and paused periods are appended to small logs under workflow/service/ so the daily digest can
tell "research was slow" from "the machine was offline or asleep".
"""
import json
from datetime import datetime
from .storage import now, normalize_time

# Errors that mean this machine cannot reach the network at all. A refused or reset connection, or an
# HTTP error, means the other side answered or is down; that is not an outage of this machine.
LOCAL_NETWORK_ERRORS = ('nodename nor servname', 'Name or service not known', 'Temporary failure in name resolution',
                        'Network is unreachable', 'No route to host', 'Network is down')
GRACE_SECONDS = 180   # a short blip must not stop a collection that is about to succeed
PAUSE_SECONDS = 300   # a scheduler that did not tick for this long was asleep or stopped
NEEDS_NETWORK = ('cycle', 'collect', 'research', 'repair', 'review', 'dynamic_cycle', 'global_research', 'portfolio_strategy', 'industry_research')
LOGS = {'offline': 'offline.jsonl', 'pause': 'pauses.jsonl'}


class Offline(RuntimeError):
    """Raised between units of work once the network has been down for GRACE_SECONDS. The job ends as
    DEFERRED with an OFFLINE: error, and the reconnect logic schedules fresh research."""


def local_network_error(detail):
    return any(e in (detail or '') for e in LOCAL_NETWORK_ERRORS)


def offline_since(store, at=None):
    """Start of the current outage when the last sync failed because this machine had no network and that
    has lasted at least GRACE_SECONDS; otherwise None."""
    from .cloud_runtime import value
    since = value(store, 'offline_since')
    last = value(store, 'last_sync') or {}
    if not since or last.get('status') != 'FAILED' or not local_network_error(last.get('error')):
        return None
    elapsed = (datetime.fromisoformat(normalize_time(at or now())) - datetime.fromisoformat(normalize_time(since))).total_seconds()
    return normalize_time(since) if elapsed >= GRACE_SECONDS else None


def check(store):
    since = offline_since(store)
    if since:
        raise Offline('OFFLINE: 本机自 ' + since + ' 起断网，本轮在此停止，联网后重做')


def log_interval(root, kind, start, end, **extra):
    start, end = normalize_time(start), normalize_time(end)
    minutes = round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60)
    path = root / 'workflow' / 'service' / LOGS[kind]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps({'from': start, 'to': end, 'minutes': minutes, **extra}, ensure_ascii=False) + '\n')


def intervals(root, a, b):
    """Offline and paused periods overlapping [a, b), clipped to it, oldest first."""
    found = []
    for kind, name in LOGS.items():
        path = root / 'workflow' / 'service' / name
        if not path.exists():
            continue
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                item = json.loads(line)
                start, end = normalize_time(item['from']), normalize_time(item['to'])
            except (ValueError, KeyError, TypeError):
                continue
            if end <= a or start >= b:
                continue
            start, end = max(start, a), min(end, b)
            found.append({'kind': kind, 'from': start, 'to': end, 'cause': item.get('cause'),
                          'minutes': round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60)})
    return sorted(found, key=lambda x: x['from'])


def overlap_minutes(start, end, spans):
    """Minutes of [start, end) covered by the given spans; overlapping spans are counted once."""
    start, end = normalize_time(start), normalize_time(end)
    cuts = sorted((max(s['from'], start), min(s['to'], end)) for s in spans if s['to'] > start and s['from'] < end)
    total, cursor = 0.0, start
    for s, e in cuts:
        s = max(s, cursor)
        if e > s:
            total += (datetime.fromisoformat(e) - datetime.fromisoformat(s)).total_seconds()
            cursor = e
    return total / 60


def inside(stamp, spans):
    stamp = normalize_time(stamp)
    return any(s['from'] <= stamp < s['to'] for s in spans)
