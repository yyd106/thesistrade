#!/usr/bin/env python3
"""Bounded, read-only EMT simulation probe. Never imports a vendor demo."""
from __future__ import annotations

import argparse
import errno
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import threading
import tempfile
import time
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
SIM_HOST, SIM_PORT = '61.152.230.41', 19088
IMAGE = 'dean-emt-query:2.27.0'
LSHW_URL = 'https://deb.debian.org/debian/pool/main/l/lshw/lshw_02.19.git.2021.06.19.996aaad9c7-2+b1_amd64.deb'
LSHW_SHA256 = '6e2481ebc0a740e7cf1f25d39b8e44a97d10fed33b3eeb271a7a57af594669d7'
PREFIX = 'EMT_PROBE_RESULT='
PROGRESS_PREFIX = 'EMT_PROBE_STAGE='
STAGES = {'starting', 'sdk_ready', 'creating_api', 'setting_version', 'login',
          'logged_in', 'assets', 'positions', 'orders', 'trades', 'logout', 'exit', 'done'}
SUCCESS_STATES = {'SDK_READY', 'QUERY_VERIFIED', 'NETWORK_READY'}
FIELDS = {
    'assets': ('total_asset', 'buying_power', 'security_asset', 'withholding_amount',
               'account_type', 'orig_banlance', 'banlance', 'captial_asset',
               'fund_buy_amount', 'fund_buy_fee', 'fund_sell_amount', 'fund_sell_fee'),
    'positions': ('ticker', 'ticker_name', 'market', 'total_qty', 'sellable_qty',
                  'avg_price', 'yesterday_position'),
    'orders': ('order_emt_id', 'order_client_id', 'ticker', 'market', 'price', 'quantity',
               'qty_traded', 'qty_left', 'side', 'order_status', 'insert_time', 'update_time'),
    'trades': ('order_emt_id', 'exec_id', 'report_index', 'ticker', 'market', 'price',
               'quantity', 'side', 'trade_time', 'trade_amount'),
}


class ProbeError(Exception):
    pass


def progress(stage):
    if stage in STAGES:
        print(PROGRESS_PREFIX + stage, flush=True)


def last_stage(output):
    if isinstance(output, bytes):
        output = output.decode('utf-8', errors='replace')
    stages = [line[len(PROGRESS_PREFIX):] for line in (output or '').splitlines()
              if line.startswith(PROGRESS_PREFIX) and line[len(PROGRESS_PREFIX):] in STAGES]
    return stages[-1] if stages else 'container_start'


def tcp_check():
    """Check the assigned endpoint without sending credentials or application data."""
    result = {'endpoint': f'{SIM_HOST}:{SIM_PORT}',
              'checked_at': datetime.now(timezone.utc).isoformat()}
    start = time.monotonic()
    try:
        with socket.create_connection((SIM_HOST, SIM_PORT), timeout=6):
            result['status'] = 'CONNECTED'
    except OSError as exc:
        result['errno'] = exc.errno
        if isinstance(exc, TimeoutError) or exc.errno == errno.ETIMEDOUT:
            result['status'] = 'TIMED_OUT'
        else:
            result['status'] = {errno.ECONNREFUSED: 'CONNECTION_REFUSED',
                                errno.ENETUNREACH: 'NETWORK_UNREACHABLE',
                                errno.EHOSTUNREACH: 'HOST_UNREACHABLE',
                                errno.EACCES: 'PERMISSION_DENIED',
                                errno.EPERM: 'PERMISSION_DENIED'}.get(exc.errno, 'SOCKET_ERROR')
    result['elapsed_ms'] = round((time.monotonic() - start) * 1000)
    return result


def host_route():
    """Record only the chosen interface, never VPN profiles or credentials."""
    if sys.platform != 'darwin':
        return {'interface': None, 'tunnel_detected': None}
    try:
        p = subprocess.run(['/sbin/route', '-n', 'get', SIM_HOST], capture_output=True,
                           text=True, timeout=3)
        for line in p.stdout.splitlines():
            key, separator, value = line.strip().partition(':')
            if separator and key == 'interface':
                interface = value.strip()
                if interface.isalnum():
                    return {'interface': interface, 'tunnel_detected': interface.startswith('utun')}
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {'interface': None, 'tunnel_detected': None}


def network_result(host, container, route):
    ready = container['status'] == 'CONNECTED'
    result = {'environment': 'EMT_SIMULATION', 'read_only': True,
              'checked_at': datetime.now(timezone.utc).isoformat(),
              'status': 'NETWORK_READY' if ready else 'NETWORK_BLOCKED',
              'login_verified': False, 'queries_complete': False, 'orders_sent': False,
              'host_tcp': host, 'container_tcp': container, 'host_route': route}
    if not ready:
        result['error_zh'] = 'Docker 无法连接 EMT 仿真端口，尚未请求账号密码或发起登录。'
        result['next_step_zh'] = (
            'EMT 的主机路由经过 VPN 隧道。请临时断开 VPN 或为该地址设置直连后重试网络检查；当前结果不能区分 VPN、沿途网络和服务端拒绝。'
            if route.get('tunnel_detected') else
            '请用另一网络复测，或向 EMT 确认测试地址与服务状态。无需反复输入账号密码。')
    return result


class Replies:
    """A zero-row reply is successful only after its final callback arrives."""
    def __init__(self):
        self.lock = threading.RLock()
        self.pending = {}
        self.connection_error = None

    def begin(self, reqid, kind, session):
        with self.lock:
            self.pending[reqid] = {'kind': kind, 'session': session, 'rows': [],
                                   'event': threading.Event(), 'error': None}

    def receive(self, kind, data, error, reqid, last, session):
        with self.lock:
            request = self.pending.get(reqid)
            if not request or request['kind'] != kind or request['session'] != session:
                return
            code = (error or {}).get('error_id', 0)
            if code:
                request['error'] = f'{kind}: broker error {code}'
                request['event'].set()
                return
            if data:
                row = {k: data[k] for k in FIELDS[kind] if k in data}
                # Some SDKs use a zero-filled struct to represent no positions/orders.
                if kind == 'assets' or str(row.get('ticker', '')).strip('0\x00 '):
                    request['rows'].append(row)
            if last:
                request['event'].set()

    def fail(self, reason):
        with self.lock:
            self.connection_error = reason
            for request in self.pending.values():
                request['error'] = reason
                request['event'].set()

    def finish(self, reqid, timeout):
        request = self.pending[reqid]
        if not request['event'].wait(timeout):
            raise ProbeError(f"{request['kind']}: query timeout (incomplete result)")
        with self.lock:
            if self.connection_error or request['error']:
                raise ProbeError(self.connection_error or request['error'])
            if request['kind'] == 'assets' and len(request['rows']) != 1:
                raise ProbeError('assets: expected one complete asset response')
            return list(request['rows'])


def api_class(base):
    class ReadOnlyApi(base):
        def __init__(self):
            super().__init__()
            self.replies = Replies()

        def onConnected(self):
            pass

        def onDisconnected(self, reason):
            self.replies.fail(f'disconnected: {reason}')

        def onError(self, data):
            self.replies.fail(f"broker error: {(data or {}).get('error_id', 'unknown')}")

        def onQueryAsset(self, data, error, reqid, last, session):
            self.replies.receive('assets', data, error, reqid, last, session)

        def onQueryPosition(self, data, error, reqid, last, session):
            self.replies.receive('positions', data, error, reqid, last, session)

        def onQueryOrder(self, data, error, reqid, last, session):
            self.replies.receive('orders', data, error, reqid, last, session)

        def onQueryTrade(self, data, error, reqid, last, session):
            self.replies.receive('trades', data, error, reqid, last, session)
    return ReadOnlyApi


def run_queries(api, session, timeout=12):
    result = {}
    calls = (
        ('assets', lambda req: api.queryAsset(session, req)),
        ('positions', lambda req: api.queryPosition('', session, req)),
        ('orders', lambda req: api.queryOrders({'ticker': '', 'begin_time': 0, 'end_time': 0}, session, req)),
        ('trades', lambda req: api.queryTrades({'ticker': '', 'begin_time': 0, 'end_time': 0}, session, req)),
    )
    for reqid, (kind, call) in enumerate(calls, 1):
        progress(kind)
        api.replies.begin(reqid, kind, session)
        if call(reqid) != 0:
            code = (api.getApiLastError() or {}).get('error_id', 'unknown')
            raise ProbeError(f'{kind}: query rejected ({code})')
        result[kind] = api.replies.finish(reqid, timeout)
    return result


def container_main(check_only, network_only=False):
    if network_only:
        print(PREFIX + json.dumps(tcp_check()), flush=True)
        os._exit(0)
    progress('starting')
    result = {'environment': 'EMT_SIMULATION', 'read_only': True,
              'checked_at': datetime.now(timezone.utc).isoformat(),
              'login_verified': False, 'queries_complete': False, 'orders_sent': False}
    api, session = None, 0
    try:
        if platform.system() != 'Linux' or platform.machine() != 'x86_64':
            raise ProbeError('Linux x86_64 runtime required')
        from vnemttrader import TraderApi
        result['python_version'] = platform.python_version()
        result['sdk_imported'] = True
        progress('sdk_ready')
        api = api_class(TraderApi)()
        progress('creating_api')
        # This SDK appends its dated log name directly to the supplied path.
        # The trailing slash keeps all files in the ephemeral writable directory.
        api.createTraderApi(97, '/work/', 1)
        result['sdk_initialized'] = True
        result['sdk_version'] = api.getApiVersion()
        progress('setting_version')
        api.setSoftwareVersion('dean-query-1')
        if check_only:
            result['status'] = 'SDK_READY'
        else:
            credentials = json.load(sys.stdin)
            user, password = credentials['account'], credentials['password']
            if not isinstance(user, str) or not user.isdigit() or not isinstance(password, str) or not password:
                raise ProbeError('Invalid local credentials')
            # Pin the SDK's documented simulation address and TCP. No live endpoint option.
            progress('login')
            session = api.login(SIM_HOST, SIM_PORT, user, password, 1, '')
            del password, credentials
            if not session:
                code = (api.getApiLastError() or {}).get('error_id', 'unknown')
                result['broker_error_code'] = code
                if code == 1008:
                    result['error_zh'] = '无法建立到 EMT 仿真服务器的 TCP 连接；账号尚未通过登录验证。'
                raise ProbeError(f'Login failed ({code})')
            result['login_verified'] = True
            progress('logged_in')
            result['account_masked'] = '*' * max(0, len(user) - 4) + user[-4:]
            result['trading_day'] = api.getTradingDay()
            result.update(run_queries(api, session))
            # EMT_ACCOUNT_NORMAL is 0. Do not proceed using credit/options accounts.
            if result['assets'][0].get('account_type') != 0:
                raise ProbeError('The selected account is not a normal cash account')
            result['queries_complete'] = True
            result['status'] = 'QUERY_VERIFIED'
    except ProbeError as exc:
        result['status'], result['error'] = 'FAILED', str(exc)
    except Exception as exc:
        # SDK exception strings can embed login arguments. Never print their contents.
        result['status'], result['error'] = 'FAILED', type(exc).__name__
    finally:
        if api and session:
            progress('logout')
            try:
                result['logout_verified'] = api.logout(session) == 0
            except Exception:
                result['logout_verified'] = False
        # vnemttrader.exit() hangs even in an offline create/exit reproduction.
        # This one-shot query worker terminates its process after logout instead;
        # the host removes its container and tmpfs. Never use it as a daemon.
        result['cleanup'] = 'isolated_process_exit'
    progress('done')
    print(PREFIX + json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
    # Exit before returning: releasing this frame's final api reference invokes
    # the same vendor destructor that blocks in exit(). Output is already flushed.
    os._exit(0 if result['status'] in ('SDK_READY', 'QUERY_VERIFIED') else 1)


def docker_environment():
    env = dict(os.environ)
    env['PATH'] = '/Applications/Docker.app/Contents/Resources/bin:' + env.get('PATH', '')
    return env


def temporary_directory():
    # Docker's access to Documents may await macOS consent. Stage only the SDK
    # and probe in /private/tmp, which was verified to support bind mounts.
    return tempfile.TemporaryDirectory(prefix='dean-emt-', dir='/private/tmp' if sys.platform == 'darwin' else None)


def build_image(docker):
    with temporary_directory() as directory:
        stage = Path(directory)
        with urllib.request.urlopen(LSHW_URL, timeout=30) as response:
            data = response.read(1_000_001)
        if hashlib.sha256(data).hexdigest() != LSHW_SHA256:
            raise ProbeError('Debian dependency checksum mismatch')
        (stage / 'lshw.deb').write_bytes(data)
        shutil.copyfile(ROOT / 'docker/emt-query/Dockerfile', stage / 'Dockerfile')
        process = subprocess.run([docker, 'build', '--platform', 'linux/amd64', '-t', IMAGE, str(stage)],
                                 env=docker_environment(), timeout=300)
        if process.returncode:
            raise ProbeError('Docker image build failed')
    return 0


def run_container(docker, sdk, credentials, check_only, network_only=False):
    with temporary_directory() as directory:
        stage = Path(directory)
        if not network_only:
            shutil.copytree(sdk / 'lib/linux', stage / 'sdk')
        shutil.copyfile(Path(__file__).resolve(), stage / 'probe.py')
        name = 'dean-emt-query-' + uuid.uuid4().hex[:12]
        command = [docker, 'run', '--pull=never', '--rm', '-i', '--name', name, '--platform', 'linux/amd64',
                   '--log-driver', 'none', '--ulimit', 'core=0',
                   '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                   '--pids-limit', '96', '--memory', '512m', '--cpus', '2',
                   '--tmpfs', '/tmp:rw,nosuid,size=64m', '--tmpfs', '/work:rw,nosuid,size=32m',
                   '--mount', f'type=bind,source={stage / "probe.py"},target=/app/probe.py,readonly']
        if not network_only:
            command += ['--mount', f'type=bind,source={stage / "sdk"},target=/sdk,readonly']
        if check_only:
            command += ['--network', 'none']
        command += [IMAGE, 'python', '/app/probe.py', '--container']
        if check_only:
            command += ['--check']
        if network_only:
            command += ['--network-check']
        try:
            return subprocess.run(command, input=json.dumps(credentials) if credentials else '',
                                  capture_output=True, text=True, timeout=100, env=docker_environment())
        except subprocess.TimeoutExpired as exc:
            result = {'environment': 'EMT_SIMULATION', 'read_only': True,
                      'checked_at': datetime.now(timezone.utc).isoformat(),
                      'status': 'FAILED', 'login_verified': False, 'queries_complete': False,
                      'orders_sent': False, 'last_stage': last_stage(exc.stdout),
                      'error': 'Probe timed out after 100 seconds; query not verified'}
            return subprocess.CompletedProcess(command, 1, PREFIX + json.dumps(result), '')
        finally:
            subprocess.run([docker, 'rm', '-f', name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15, env=docker_environment())


def container_result(process):
    # Never echo native SDK output: it may contain account details and login parameters.
    matches = [line[len(PREFIX):] for line in process.stdout.splitlines() if line.startswith(PREFIX)]
    if not matches:
        raise ProbeError(f'Container exited without a result (exit {process.returncode}); native output suppressed')
    return json.loads(matches[-1])


def host_main(args):
    docker = shutil.which('docker') or '/Applications/Docker.app/Contents/Resources/bin/docker'
    if args.build:
        return build_image(docker)
    network = None
    if not args.check:
        route, host = host_route(), tcp_check()
        container = container_result(run_container(docker, None, None, False, network_only=True))
        network = network_result(host, container, route)
        if args.network_check or network['status'] != 'NETWORK_READY':
            return emit_result(network, args.output)
    manifest = json.loads((ROOT / 'docs/broker/emt-sdk-manifest.json').read_text())
    sdk = Path(manifest['sdk_dir'])
    if not (sdk / 'lib/linux/vnemttrader.so').is_file():
        raise ProbeError('The verified EMT SDK directory is missing')
    archive = sdk.parent / 'EMT_API_Python_V2.27.0.zip'
    if hashlib.sha256(archive.read_bytes()).hexdigest() != manifest['download_sha256']:
        raise ProbeError('SDK archive checksum mismatch')
    with zipfile.ZipFile(archive) as z:
        for item in (sdk / 'lib/linux').glob('*.so'):
            member = 'emt_api_python/lib/linux/' + item.name
            if hashlib.sha256(item.read_bytes()).digest() != hashlib.sha256(z.read(member)).digest():
                raise ProbeError('Extracted SDK library checksum mismatch')
    credentials = None
    if not args.check:
        if args.credentials_file:
            path = Path(args.credentials_file)
            if path.stat().st_mode & 0o077:
                raise ProbeError('Credential file must be private (chmod 600)')
            credentials = json.loads(path.read_text())
        else:
            credentials = {'account': getpass.getpass('普通仿真账号（输入隐藏）：'),
                           'password': getpass.getpass('仿真密码（输入隐藏）：')}
    process = run_container(docker, sdk, credentials, args.check)
    result = container_result(process)
    if network:
        result['network_preflight'] = network
    return emit_result(result, args.output)


def emit_result(result, output):
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(result, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write('\n')
        os.replace(temporary, path)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result['status'] in SUCCESS_STATES else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Check SDK loading without network or credentials')
    mode.add_argument('--network-check', action='store_true', help='Check host/container TCP and route without credentials')
    mode.add_argument('--build', action='store_true', help='Build the isolated amd64 runtime image')
    parser.add_argument('--credentials-file', help='Private local JSON; omitted for hidden interactive entry')
    parser.add_argument('--output', help='Save a separate, masked simulation query result')
    parser.add_argument('--container', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.container:
            container_main(args.check, args.network_check)
        return host_main(args)
    except ProbeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
