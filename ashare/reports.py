"""Optional private summary backup and external reviewer exchange.

The research node (or a standalone node) pushes, with a deploy key that can write to this one repository:
  digests/<day>.md                 daily digests of the last DIGEST_DAYS days
  weekly/<year-Wnn>.md             weekly evaluation reports
  evaluations/<batch id>/<file>    evaluation batches: the manifest, every file it lists, and notes.md
  notices/state.json               every notice raised for Dean and his answer
and imports the two kinds of file external reviewers write there:
  notices/outbox/<name>.md         a notice for Dean: front matter (id, kind, title, optional deadline), then the body
  checks/<batch id>.md             an external check of an evaluation batch
Nothing else is read from the repository and nothing in it is executed. Code never travels this way: the
program repository and its deployment are separate. The node's database, raw documents, settings and
secrets are never copied; only the pages listed above. Links are checked out as plain files and no path is
written or read through a link, so a commit to the repository cannot reach any file outside it.

Setup, once: `./agent reports setup --remote git@github.com:<owner>/<repo>.git` makes an ed25519 key pair
under data_dir/secrets (the private key never leaves this machine), pins GitHub's published host keys and
turns sync on. The public key is then added to that repository as a deploy key with write access. Git runs
with the user's global and system git settings ignored, so the node never uses the user's own GitHub login.
"""
import json
import os
import platform
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path
from .storage import now, normalize_time, json_write
from .calendar import local

# GitHub's published SSH host keys (docs.github.com, "GitHub's SSH key fingerprints"):
# Ed25519 SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU, ECDSA SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM.
# Port 443 (ssh.github.com) serves the same keys, for networks that block port 22.
ED25519 = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl'
ECDSA = ('ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRg'
         'g6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=')
KNOWN_HOSTS = ''.join(f'{host} {key}\n' for host in ('github.com', '[ssh.github.com]:443') for key in (ED25519, ECDSA))
REMOTE = re.compile(r'git@github\.com:([A-Za-z0-9_.-]{1,39})/([A-Za-z0-9_.-]{1,100})\.git')
DIGEST_DAYS = 120
MAX_FILE = 1_000_000       # larger pages stay on the node and are listed as skipped
MAX_IMPORT = 200_000       # a notice or check file larger than this is ignored
GIT_TIMEOUT = 120
AUTHOR = ('ThesisTrade research node', 'thesistrade-research@users.noreply.github.com')
UNREACHABLE = ('Connection timed out', 'Connection refused', 'Network is unreachable', 'Operation timed out',
               'Could not resolve hostname', 'port 22')
README = """# ThesisTrade 报告仓库

本仓库由研究端程序自动推送，用于报告摘要备份和可选外部审查。本机 ChatGPT 监督审查直接读本地报告，不依赖本仓库。这里不存数据库、原始资料、设置或密钥。

| 目录 | 写入方 | 内容 |
|---|---|---|
| `digests/` | 研究端 | 每日运行日报（最近 120 天） |
| `weekly/` | 研究端 | 周度评估报告 |
| `evaluations/<批次编号>/` | 研究端 | 评估批次：`manifest.json` 列出程序生成的文件和 SHA-256；`notes.md` 是桌面 agent 的小结 |
| `notices/state.json` | 研究端 | 给 Dean 的通知和他的答复 |
| `notices/outbox/*.md` | 外部审查员 | 给 Dean 的新通知，研究端导入后在网页弹窗 |
| `checks/<批次编号>.md` | 外部审查员 | 检查结论，可在头部注明 reviewer、model、review_version；历史无头部文件按 Claude 兼容 |
| `supervision/summary.json` | 本机 | 最近审查的摘要、版本和状态；不包含模型输入或日志 |

外部写入文件（`notices/outbox/`、`checks/`）文件名只用英文字母、数字和连字符，扩展名为 `.md`。历史 Claude 文件保留。

通知文件格式（`kind` 取 DECISION 需要决定、VETO 可否决、INFO 通知；`deadline` 可省略）：

```
---
id: N-20261009-reviewer-feed
kind: DECISION
title: 建议购买稳定的行情数据源
deadline: 2026-10-12T12:00:00+08:00
---
正文：改什么、为什么、证据有多强、最坏会怎样、怎么撤回、建议。
```

研究端导入失败的文件和原因记在 `./agent reports status` 的上次同步结果里。
"""


def paths(config):
    root = Path(config['data_dir'])
    return {'repo': Path(config.get('reports_repo_dir') or root / 'reports-repo'),
            'key': Path(config.get('reports_ssh_key') or root / 'secrets' / 'reports_deploy_key'),
            'known_hosts': root / 'secrets' / 'reports_known_hosts'}


def _env(p):
    # -F /dev/null: the user's own ~/.ssh/config (and any personal GitHub key it names) is never used.
    ssh = (f'ssh -F /dev/null -i "{p["key"]}" -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="{p["known_hosts"]}" '
           '-o GlobalKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=15')
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_SSH_COMMAND=ssh, GIT_TERMINAL_PROMPT='0', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
               GIT_AUTHOR_NAME=AUTHOR[0], GIT_AUTHOR_EMAIL=AUTHOR[1], GIT_COMMITTER_NAME=AUTHOR[0], GIT_COMMITTER_EMAIL=AUTHOR[1])
    return env


def _git(args, cwd, env, check=True):
    # surrogateescape: a file name that is not UTF-8, committed by anyone, must not break every sync.
    r = subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'commit.gpgsign=false', '-c', 'core.symlinks=false', *args],
                       cwd=str(cwd), env=env, capture_output=True, text=True, encoding='utf-8', errors='surrogateescape',
                       timeout=GIT_TIMEOUT)
    if check and r.returncode:
        raise RuntimeError(f"git {args[0]} 失败：{(r.stderr or r.stdout).strip()[-400:]}")
    return r


def remote_url(config, port443=False):
    m = REMOTE.fullmatch(config.get('reports_remote') or '')
    if not m:
        raise ValueError('reports_remote 须为 git@github.com:<owner>/<repo>.git')
    return f'ssh://git@ssh.github.com:443/{m.group(1)}/{m.group(2)}.git' if port443 else m.group(0)


def setup(config, remote):
    """Create the deploy key and pin GitHub's host keys; the caller then turns sync on in the settings."""
    m = REMOTE.fullmatch(remote or '')
    if not m:
        raise ValueError('仓库地址须为 git@github.com:<owner>/<repo>.git')
    if shutil.which('git') is None or shutil.which('ssh-keygen') is None:
        raise RuntimeError('本机缺少 git 或 ssh-keygen')
    p = paths(config)
    p['key'].parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p['key'].parent, 0o700)
    p['known_hosts'].write_text(KNOWN_HOSTS)
    created = not p['key'].exists()
    if created:
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', f'thesistrade-reports@{platform.node() or "node"}', '-f', str(p['key'])],
                       check=True, capture_output=True, timeout=30)
    os.chmod(p['key'], 0o600)
    public = Path(str(p['key']) + '.pub').read_text().strip()
    return {'status': 'KEY_READY', 'key_created': created, 'public_key': public, 'repository': f'{m.group(1)}/{m.group(2)}',
            'add_key_url': f'https://github.com/{m.group(1)}/{m.group(2)}/settings/keys/new',
            'next': '在上面的网址添加部署密钥：标题填 thesistrade-mac，粘贴 public_key，勾选 Allow write access，保存；然后运行 ./agent reports sync。'}


def _inside(repo, path):
    """A path in the working tree with no link anywhere between the repository root and it."""
    root = repo.resolve()
    rel = path.relative_to(repo)
    cursor = root
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f'{rel} 经过链接，拒绝读写')
    if not cursor.resolve().is_relative_to(root):
        raise ValueError(f'{rel} 不在报告仓库里')
    return cursor


def _write(repo, path, data):
    """Write bytes at a path inside the repository; returns 1 when the content changed."""
    target = _inside(repo, path)
    if target.is_file() and target.read_bytes() == data:
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    _inside(repo, path)  # the directories just made are plain directories
    target.write_bytes(data)
    return 1


def _read(repo, path):
    """Read a small external summary; links, devices and oversized files are refused."""
    target = _inside(repo, path)
    if not target.is_file():
        raise ValueError('不是普通文件')
    with target.open('rb') as handle:
        data = handle.read(MAX_IMPORT + 1)
    if len(data) > MAX_IMPORT:
        raise ValueError('文件过大')
    return data.decode('utf-8')


OWNED_FILES = ('README.md', 'notices/state.json')
OWNED_DIRS = ('digests', 'weekly', 'evaluations', 'notices', 'supervision')


def _heal(repo, env):
    """Paths this node writes must be plain files in plain directories. A link committed there by anyone
    (checked out as a small file), or a file where one of these directories belongs, is dropped from the
    index and the working tree; the export then writes the real page as a regular file."""
    healed = []
    for entry in _git(['ls-files', '-s', '-z'], repo, env).stdout.split('\0'):
        meta, _, path = entry.partition('\t')
        if not path:
            continue
        owned = path in OWNED_FILES or any(path == d or path.startswith(d + '/') for d in OWNED_DIRS)
        if meta.split()[0] in ('120000', '160000') and owned:  # a link or a submodule where a page belongs
            target = repo / path
            _inside(repo, target.parent)  # a link placed by hand above it: refuse rather than delete through it
            _git(['rm', '-q', '--cached', '--', path], repo, env)
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            healed.append(path)
    for d in OWNED_DIRS:
        target = repo / d
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            _git(['rm', '-q', '--cached', '--ignore-unmatch', '--', d], repo, env)
            target.unlink()
            healed.append(d)
    return healed


def _copy(repo, src, dst, skipped):
    if src.stat().st_size > MAX_FILE:
        skipped.append(str(src.name))
        return 0
    return _write(repo, dst, src.read_bytes())


def export(store, repo, at):
    """Write this node's pages into the working tree; returns how many files changed and which were too large."""
    from .notices import export as notice_log
    changed, skipped = 0, []
    if not _inside(repo, repo / 'README.md').exists():
        changed += _write(repo, repo / 'README.md', README.encode('utf-8'))
    cutoff = (local(at).date().toordinal() - DIGEST_DAYS)
    for f in sorted((store.root / 'workflow' / 'digests').glob('*.md')):
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}\.md', f.name):
            if date.fromisoformat(f.stem).toordinal() >= cutoff:
                changed += _copy(repo, f, repo / 'digests' / f.name, skipped)
    for f in sorted((store.root / 'workflow' / 'evaluation' / 'weekly').glob('*.md')):
        changed += _copy(repo, f, repo / 'weekly' / f.name, skipped)
    from .evaluation import SCORE_METHOD
    for f in sorted((store.root / 'workflow' / 'evaluation' / SCORE_METHOD / 'weekly').glob('*.md')):
        changed += _copy(repo, f, repo / 'weekly' / SCORE_METHOD / f.name, skipped)
    from .evaluation_batches import ID
    for folder in sorted((store.root / 'workflow' / 'evaluations').glob('EV-*')):
        if not (folder.is_dir() and ID.fullmatch(folder.name) and (folder / 'manifest.json').exists()):
            continue
        names = list(json.loads((folder / 'manifest.json').read_text(encoding='utf-8')).get('files', {})) + ['manifest.json', 'notes.md']
        for name in names:
            if (folder / name).is_file():
                changed += _copy(repo, folder / name, repo / 'evaluations' / folder.name / name, skipped)
    changed += _write(repo, repo / 'notices' / 'state.json', (json.dumps(notice_log(store), ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    from .supervision import view
    changed += _write(repo, repo / 'supervision' / 'summary.json', (json.dumps(view(store),ensure_ascii=False,indent=2)+'\n').encode('utf-8'))
    return changed, skipped


def import_from(store, repo, at):
    """External notices and checks. Bad files are listed and skipped; nothing else is read."""
    from . import notices
    from .evaluation_batches import ID, record_check
    done = {'notices': [], 'checks': [], 'rejected': []}
    for f in sorted((repo / 'notices' / 'outbox').glob('*.md')):
        try:
            n = notices.parse_markdown(_read(repo, f))
            nid = n['id'] or 'N-reviewer-' + re.sub(r'[^0-9A-Za-z._-]', '-', f.stem)[:60]
            if notices.get(store, nid):
                continue
            notices.create(store, title=n['title'], body=n['body'], kind=n['kind'], author='claude' if nid.startswith('N-claude-') else 'reviewer', at=at, notice_id=nid,
                           payload={'source': 'reports:notices/outbox/' + f.name, **({'deadline': n['deadline']} if n['deadline'] else {})})
            done['notices'].append(nid)
        except Exception as exc:
            done['rejected'].append(f'notices/outbox/{f.name}: {str(exc)[:120]}')
    for f in sorted((repo / 'checks').glob('*.md')):
        try:
            if not ID.fullmatch(f.stem):
                raise ValueError('文件名须为批次编号')
            text=_read(repo,f);identity={}
            if text.startswith('---\n'):
                header,separator,body=text[4:].partition('\n---\n')
                if not separator:raise ValueError('审查头部未结束')
                for line in header.splitlines():
                    key,_,val=line.partition(':')
                    if key.strip() in ('reviewer','model','review_version'):
                        if not re.fullmatch(r'[A-Za-z0-9._/ -]{1,80}',val.strip()):raise ValueError('审查身份格式无效')
                        identity[key.strip()]=val.strip()
                identity.setdefault('reviewer','external');text=body
            if record_check(store, f.stem, text, 'reports:checks/' + f.name, at, **identity):
                done['checks'].append(f.stem)
        except Exception as exc:
            done['rejected'].append(f'checks/{f.name}: {str(exc)[:120]}')
    return done


def _fetch(repo, env, config, store):
    """Fetch origin; on a network that blocks port 22, switch to GitHub's port 443 once and remember it."""
    r = _git(['fetch', '-q', 'origin'], repo, env, check=False)
    if r.returncode and any(s in (r.stderr or '') for s in UNREACHABLE) and 'ssh.github.com' not in _git(['remote', 'get-url', 'origin'], repo, env).stdout:
        _git(['remote', 'set-url', 'origin', remote_url(config, port443=True)], repo, env)
        with store.db:
            store.db.execute("INSERT OR REPLACE INTO service_state VALUES('reports_transport','443')")
        r = _git(['fetch', '-q', 'origin'], repo, env, check=False)
    if r.returncode:
        text = (r.stderr or r.stdout).strip()
        if 'Permission denied' in text or 'publickey' in text:
            raise RuntimeError('GitHub 拒绝了部署密钥：确认密钥已添加到报告仓库，并勾选了 Allow write access')
        if 'not found' in text.lower() or 'does not appear to be a git repository' in text:
            raise RuntimeError('找不到报告仓库，或部署密钥不属于这个仓库')
        raise RuntimeError('git fetch 失败：' + text[-400:])
    return _git(['rev-parse', '--verify', '-q', 'refs/remotes/origin/main'], repo, env, check=False).returncode == 0


def sync(store, config, at=None):
    """Bring the working tree to origin/main, import external summaries, write this node's pages, push."""
    at = normalize_time(at or now())
    if not config.get('reports_sync_enabled') or not config.get('reports_remote'):
        return {'status': 'DISABLED'}
    p = paths(config)
    if not p['key'].exists():
        raise RuntimeError('部署密钥不存在：先运行 ./agent reports setup')
    if not p['known_hosts'].exists():
        p['known_hosts'].write_text(KNOWN_HOSTS)
    env, repo = _env(p), p['repo']
    port443 = (store.db.execute("SELECT value FROM service_state WHERE key='reports_transport'").fetchone() or [None])[0] == '443'
    if not (repo / '.git').exists():
        repo.mkdir(parents=True, exist_ok=True)
        _git(['init', '-q'], repo, env)
        _git(['symbolic-ref', 'HEAD', 'refs/heads/main'], repo, env)
        _git(['remote', 'add', 'origin', remote_url(config, port443)], repo, env)
    else:
        _git(['remote', 'set-url', 'origin', remote_url(config, port443)], repo, env)
    result = {'status': 'SYNCED', 'imported': {'notices': [], 'checks': [], 'rejected': []}, 'changed': 0, 'skipped': [], 'pushed': False}
    for attempt in range(2):
        remote_main = _fetch(repo, env, config, store)
        if remote_main:
            # The export is rebuilt from this node's files every time, so nothing local needs to survive.
            _git(['checkout', '-q', '-f', '-B', 'main', 'refs/remotes/origin/main'], repo, env)
            _git(['clean', '-q', '-fd'], repo, env)
            result['imported'] = import_from(store, repo, at)
            result['healed'] = _heal(repo, env)
        result['changed'], result['skipped'] = export(store, repo, at)
        _git(['add', '-A', '--', '.'], repo, env)
        if _git(['diff', '--cached', '--quiet'], repo, env, check=False).returncode:
            _git(['commit', '-q', '-m', f'研究端同步 {local(at).strftime("%Y-%m-%d %H:%M")}'], repo, env)
        if not _ahead(repo, env, remote_main):
            break
        push = _git(['push', '-q', 'origin', 'main'], repo, env, check=False)
        if push.returncode == 0:
            result['pushed'] = True
            break
        text = (push.stderr or push.stdout).strip()
        if 'read only' in text or 'denied to deploy key' in text or 'Permission to' in text:
            raise RuntimeError('部署密钥没有写权限：在报告仓库的 Deploy keys 里重新添加，并勾选 Allow write access')
        if attempt:
            raise RuntimeError('git push 失败：' + text[-400:])
        # Another reviewer pushed in between: start again from the new origin/main.
    return result


def _ahead(repo, env, remote_main):
    """Whether local main has commits the remote does not."""
    if _git(['rev-parse', '--verify', '-q', 'HEAD'], repo, env, check=False).returncode:
        return False
    span = 'refs/remotes/origin/main..HEAD' if remote_main else 'HEAD'
    return int(_git(['rev-list', '--count', span], repo, env).stdout.strip() or 0) > 0


def _plain(value):
    """Text that can be stored and printed: a file name that is not UTF-8 (carried as surrogates) shows as U+FFFD."""
    if isinstance(value, str):
        try:
            return value.encode('utf-8', 'surrogateescape').decode('utf-8', 'replace')
        except UnicodeEncodeError:
            return value.encode('utf-8', 'backslashreplace').decode('utf-8')
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {_plain(k): _plain(v) for k, v in value.items()}
    return value


def run_sync(config):
    """Scheduler entry: one sync at a time, the outcome kept in service_state."""
    from .storage import Store
    from .workflow import task_lock
    store = Store(config['data_dir'])
    try:
        try:
            with task_lock(store.root, 'reports-sync'):
                with store.db:  # taken only once this sync owns the lock, so a request made meanwhile is kept
                    store.db.execute("DELETE FROM service_state WHERE key='reports_sync_requested'")
                result = sync(store, config)
            imported = result.get('imported') or {}
            state = {'at': now(), 'status': result['status'], 'pushed': result.get('pushed'), 'changed': result.get('changed'),
                     'imported': {k: len(v) for k, v in imported.items() if k != 'rejected'},
                     'rejected': imported.get('rejected', [])[:5], 'skipped': result.get('skipped', [])[:10]}
        except Exception as exc:
            if str(exc).startswith('BUSY:'):
                return {'status': 'BUSY'}
            result = state = {'at': now(), 'status': 'FAILED', 'error': f'{type(exc).__name__}: {str(exc)[:300]}'}
        result, state = _plain(result), _plain(state)
        with store.db:
            store.db.execute("INSERT OR REPLACE INTO service_state VALUES('reports_sync',?)", (json.dumps(state, ensure_ascii=False),))
        return result
    finally:
        store.close()


def request_sync(store):
    """Ask the scheduler for a sync soon (after a digest or a new batch)."""
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO service_state VALUES('reports_sync_requested',?)", (now(),))
