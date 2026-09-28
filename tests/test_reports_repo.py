import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ashare import reports, notices, evaluation_batches as batches, config_ops
from ashare.storage import Store, normalize_time
from test_config import load_config

AT = normalize_time('2026-09-28T16:00:00+08:00')


def git(*args, cwd):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_AUTHOR_NAME='Claude', GIT_AUTHOR_EMAIL='c@example.com',
               GIT_COMMITTER_NAME='Claude', GIT_COMMITTER_EMAIL='c@example.com')
    return subprocess.run(['git', *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


class ReportsRepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data, self.bare, self.claude = root / 'data', root / 'remote.git', root / 'claude'
        self.store = Store(self.data)
        self.cfg = load_config(Path(__file__).resolve().parents[1] / 'config.json')
        self.cfg.update(data_dir=str(self.data), deployment_role='research', reports_sync_enabled=True,
                        reports_remote='git@github.com:yyd106/thesistrade-reports.git')
        git('init', '-q', '--bare', '--initial-branch=main', str(self.bare), cwd=root)
        key = reports.paths(self.cfg)['key']
        key.parent.mkdir(parents=True)
        key.write_text('test key, never used for a local path remote')
        # A local bare repository stands in for GitHub; everything else runs as in production.
        self.remote = patch('ashare.reports.remote_url', return_value=str(self.bare))
        self.remote.start()

    def tearDown(self):
        self.remote.stop()
        self.store.close()
        self.tmp.cleanup()

    def claude_push(self, files):
        if not self.claude.exists():
            git('clone', '-q', str(self.bare), str(self.claude), cwd=self.tmp.name)
        git('pull', '-q', 'origin', 'main', cwd=self.claude)
        for name, text in files.items():
            path = self.claude / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        git('add', '-A', cwd=self.claude)
        git('commit', '-q', '-m', 'Claude', cwd=self.claude)
        git('push', '-q', 'origin', 'main', cwd=self.claude)

    def remote_files(self):
        return sorted(git('ls-tree', '-r', '--name-only', 'main', cwd=self.bare).split())

    def test_setup_makes_key_pins_github_and_asks_for_a_write_deploy_key(self):
        def keygen(cmd, **kw):
            path = Path(cmd[cmd.index('-f') + 1])
            path.write_text('PRIVATE');Path(str(path) + '.pub').write_text('ssh-ed25519 AAAAtest thesistrade-reports@mac\n')
            return subprocess.CompletedProcess(cmd, 0)
        cfg = {**self.cfg, 'reports_ssh_key': str(self.data / 'secrets' / 'new_key')}
        with patch('ashare.reports.shutil.which', return_value='/usr/bin/tool'), patch('ashare.reports.subprocess.run', side_effect=keygen) as run:
            result = reports.setup(cfg, 'git@github.com:yyd106/thesistrade-reports.git')
            again = reports.setup(cfg, 'git@github.com:yyd106/thesistrade-reports.git')
        self.assertEqual(run.call_count, 1)  # an existing key is kept
        self.assertEqual((result['key_created'], again['key_created']), (True, False))
        self.assertEqual(result['add_key_url'], 'https://github.com/yyd106/thesistrade-reports/settings/keys/new')
        self.assertEqual(result['public_key'], 'ssh-ed25519 AAAAtest thesistrade-reports@mac')
        self.assertEqual(Path(cfg['reports_ssh_key']).stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.data / 'secrets').stat().st_mode & 0o777, 0o700)
        known = reports.paths(cfg)['known_hosts'].read_text()
        self.assertIn('github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl', known)
        self.assertIn('[ssh.github.com]:443 ssh-ed25519', known)
        for bad in ('https://github.com/yyd106/x.git', 'git@gitlab.com:a/b.git', 'git@github.com:a/b.git; rm -rf /'):
            with self.assertRaises(ValueError):
                reports.setup(cfg, bad)

    def test_the_remote_can_only_be_set_by_setup(self):
        path = Path(self.tmp.name) / 'config.json'
        raw = json.loads((Path(__file__).resolve().parents[1] / 'config.json').read_text())
        raw['data_dir'] = str(self.data)
        path.write_text(json.dumps(raw))
        with self.assertRaises(ValueError):
            config_ops.apply(path, {'reports_remote': 'git@github.com:someone/else.git'}, reason='改到别人的仓库')
        changed = config_ops.apply(path, {'reports_remote': 'git@github.com:yyd106/thesistrade-reports.git', 'reports_sync_enabled': True},
                                   reason='reports setup', setup=True)
        self.assertEqual([c['class'] for c in changed['changes']], ['SETUP', 'OPERATIONAL'])
        with self.assertRaises(ValueError):
            config_ops.apply(path, {'reports_remote': 'https://example.com/x.git'}, reason='格式错误', setup=True)

    def test_pages_go_out_and_claude_files_come_back(self):
        digests = self.data / 'workflow' / 'digests'
        digests.mkdir(parents=True)
        (digests / '2026-09-28.md').write_text('# 运行日报 2026-09-28\n', encoding='utf-8')
        (digests / '2026-01-01.md').write_text('# 太旧的日报\n', encoding='utf-8')
        (digests / 'rollup-2026-09-01_2026-09-28.md').write_text('# 汇总\n', encoding='utf-8')
        n = notices.create(self.store, title='购买行情源', body='建议购买付费行情源，理由见批次报告。', kind='DECISION', at=AT)
        bid = batches.start(self.store, self.cfg, at=AT)['id']
        batches.note(self.store, bid, '本批次没有需要 Dean 决定的事项，行情没有中断。')
        first = reports.sync(self.store, self.cfg, AT)
        self.assertTrue(first['pushed'])
        files = self.remote_files()
        self.assertIn('digests/2026-09-28.md', files)
        self.assertNotIn('digests/2026-01-01.md', files)
        self.assertFalse(any('rollup' in f for f in files))
        for name in ('manifest.json', 'report.md', 'digests.md', 'notes.md', 'quotes-health.json'):
            self.assertIn(f'evaluations/{bid}/{name}', files)
        self.assertIn('README.md', files)
        state = json.loads(git('show', 'main:notices/state.json', cwd=self.bare))
        self.assertEqual([(x['id'], x['status']) for x in state], [(n['id'], 'OPEN')])
        self.assertFalse(any(f.endswith(('.sqlite3', 'config.json')) or 'secrets' in f for f in files))
        self.assertEqual(reports.sync(self.store, self.cfg, AT)['pushed'], False)  # nothing new: no commit
        self.claude_push({'notices/outbox/feed.md': '---\nid: N-claude-feed-01\nkind: DECISION\ntitle: 购买稳定行情源\n---\n两周内持仓有 35 分钟没有报价，建议购买。\n',
                          'notices/outbox/broken.md': '没有头部',
                          f'checks/{bid}.md': '# 检查结论\n\n没有阻断问题。\n',
                          'checks/not-a-batch.md': '# 文件名不是批次编号\n'})
        second = reports.sync(self.store, self.cfg, AT)
        self.assertEqual(second['imported']['notices'], ['N-claude-feed-01'])
        self.assertEqual(second['imported']['checks'], [bid])
        self.assertEqual(len(second['imported']['rejected']), 2)
        self.assertEqual(notices.get(self.store, 'N-claude-feed-01')['author'], 'claude')
        self.assertEqual(batches.get(self.store, bid)['status'], 'CHECKED')
        self.assertTrue(second['pushed'])  # the new notice now appears in notices/state.json
        self.assertIn('N-claude-feed-01', git('show', 'main:notices/state.json', cwd=self.bare))
        self.assertNotIn(f'evaluations/{bid}/claude-check.md', self.remote_files())
        third = reports.sync(self.store, self.cfg, AT)
        self.assertEqual((third['imported']['notices'], third['imported']['checks'], third['pushed']), ([], [], False))

    def test_scheduler_entry_records_success_and_failure(self):
        self.assertEqual(reports.run_sync(self.cfg)['status'], 'SYNCED')
        state = json.loads(self.store.db.execute("SELECT value FROM service_state WHERE key='reports_sync'").fetchone()[0])
        self.assertEqual((state['status'], state['pushed']), ('SYNCED', True))
        reports.paths(self.cfg)['key'].unlink()
        self.assertEqual(reports.run_sync(self.cfg)['status'], 'FAILED')
        state = json.loads(self.store.db.execute("SELECT value FROM service_state WHERE key='reports_sync'").fetchone()[0])
        self.assertIn('reports setup', state['error'])
        self.assertEqual(reports.sync(self.store, {**self.cfg, 'reports_sync_enabled': False}, AT), {'status': 'DISABLED'})

    def test_links_committed_to_the_repository_reach_nothing_outside_it(self):
        victim = Path(self.tmp.name) / 'victim.txt'
        victim.write_text('untouched')
        bid = batches.start(self.store, self.cfg, at=AT)['id']
        reports.sync(self.store, self.cfg, AT)
        git('clone', '-q', str(self.bare), str(self.claude), cwd=self.tmp.name)
        for name, target in (('notices/state.json', str(victim)), ('digests/2026-09-28.md', str(self.data / 'agent.sqlite3')),
                             (f'checks/{bid}.md', '/etc/hostname'), ('notices/outbox/link.md', '/etc/passwd')):
            path = self.claude / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.unlink(missing_ok=True)
            os.symlink(target, path)
        git('add', '-A', cwd=self.claude);git('commit', '-q', '-m', 'links', cwd=self.claude);git('push', '-q', 'origin', 'main', cwd=self.claude)
        digests = self.data / 'workflow' / 'digests'
        digests.mkdir(parents=True, exist_ok=True)
        (digests / '2026-09-28.md').write_text('# 运行日报\n', encoding='utf-8')
        result = reports.sync(self.store, self.cfg, AT)
        self.assertEqual(victim.read_text(), 'untouched')
        self.assertEqual(self.store.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        checked = (self.data / 'workflow' / 'evaluations' / bid / 'claude-check.md')
        self.assertFalse(checked.exists() and 'root:' in checked.read_text())  # a link arrives as a plain file holding its target path
        self.assertNotIn('root:x', json.dumps(result, ensure_ascii=False))
        repo = reports.paths(self.cfg)['repo']
        self.assertFalse(any(p.is_symlink() for p in repo.rglob('*')))

    def test_links_at_paths_this_node_owns_are_replaced_by_real_pages(self):
        reports.sync(self.store, self.cfg, AT)
        git('clone', '-q', str(self.bare), str(self.claude), cwd=self.tmp.name)
        state = self.claude / 'notices' / 'state.json'
        state.unlink()
        os.symlink('/etc/passwd', state)
        os.symlink('/tmp', self.claude / 'digests')
        git('add', '-A', cwd=self.claude);git('commit', '-q', '-m', 'links', cwd=self.claude);git('push', '-q', 'origin', 'main', cwd=self.claude)
        digests = self.data / 'workflow' / 'digests'
        digests.mkdir(parents=True, exist_ok=True)
        (digests / '2026-09-28.md').write_text('# 运行日报\n', encoding='utf-8')
        result = reports.sync(self.store, self.cfg, AT)
        self.assertEqual(sorted(result['healed']), ['digests', 'notices/state.json'])
        modes = {line.split('\t')[1]: line.split(' ')[0] for line in git('ls-tree', '-r', 'main', cwd=self.bare).splitlines()}
        self.assertEqual(modes['notices/state.json'], '100644')
        self.assertEqual(modes['digests/2026-09-28.md'], '100644')
        self.assertNotIn('120000', modes.values())
        self.assertEqual(reports.sync(self.store, self.cfg, AT)['pushed'], False)  # healed for good

    def weekly_page(self):
        weekly = self.data / 'workflow' / 'evaluation' / 'weekly'
        weekly.mkdir(parents=True)
        (weekly / '2026-W40.md').write_text('# 周报\n', encoding='utf-8')

    def test_a_submodule_entry_where_a_page_belongs_is_healed(self):
        reports.sync(self.store, self.cfg, AT)
        git('clone', '-q', str(self.bare), str(self.claude), cwd=self.tmp.name)
        commit = git('rev-parse', 'HEAD', cwd=self.claude).strip()
        git('update-index', '--add', '--cacheinfo', f'160000,{commit},weekly', cwd=self.claude)
        git('commit', '-q', '-m', 'submodule', cwd=self.claude);git('push', '-q', 'origin', 'main', cwd=self.claude)
        self.weekly_page()
        result = reports.sync(self.store, self.cfg, AT)
        self.assertIn('weekly', result['healed'])
        self.assertTrue(result['pushed'])
        self.assertIn('weekly/2026-W40.md', self.remote_files())

    def test_healing_never_deletes_through_a_link_above_the_path(self):
        reports.sync(self.store, self.cfg, AT)
        outside = Path(self.tmp.name) / 'outside'
        (outside / 'weekly').mkdir(parents=True)
        (outside / 'weekly' / 'keep.md').write_text('keep', encoding='utf-8')
        repo = reports.paths(self.cfg)['repo']
        with patch('ashare.reports._git') as fake:
            fake.return_value = subprocess.CompletedProcess([], 0, stdout='160000 abc 0\tlinked/weekly\0', stderr='')
            (repo / 'linked').symlink_to(outside)
            with patch('ashare.reports.OWNED_DIRS', ('linked',)):
                with self.assertRaises(ValueError):
                    reports._heal(repo, {})
        self.assertTrue((outside / 'weekly' / 'keep.md').exists())
        self.assertEqual([c.args[0][0] for c in fake.call_args_list], ['ls-files'])

    @unittest.skipIf(sys.platform == 'darwin', 'APFS refuses file names that are not UTF-8')
    def test_a_file_name_that_is_not_utf8_neither_stops_publishing_nor_hides_the_result(self):
        reports.sync(self.store, self.cfg, AT)
        git('clone', '-q', str(self.bare), str(self.claude), cwd=self.tmp.name)
        (self.claude / 'checks').mkdir()
        (self.claude / 'checks' / os.fsdecode(b'\xff\xfe.md')).write_text('x')
        git('add', 'checks', cwd=self.claude);git('commit', '-q', '-m', 'odd', cwd=self.claude);git('push', '-q', 'origin', 'main', cwd=self.claude)
        self.weekly_page()
        result = reports.run_sync(self.cfg)
        self.assertEqual((result['status'], result['pushed']), ('SYNCED', True))
        json.dumps(result, ensure_ascii=False).encode('utf-8')  # printable by the CLI
        state = json.loads(self.store.db.execute("SELECT value FROM service_state WHERE key='reports_sync'").fetchone()[0])
        self.assertEqual(state['status'], 'SYNCED')
        self.assertTrue(state['rejected'][0].startswith('checks/\ufffd\ufffd.md'))
        self.assertIn('weekly/2026-W40.md', self.remote_files())

    def test_a_push_the_remote_refuses_is_an_error_not_a_success(self):
        real = reports._git
        def refusing(args, cwd, env, check=True):
            if args[0] == 'push':
                return subprocess.CompletedProcess(args, 1, '', 'ERROR: The key you are authenticating with has been marked as read only.')
            return real(args, cwd, env, check)
        with patch('ashare.reports._git', side_effect=refusing):
            with self.assertRaisesRegex(RuntimeError, '没有写权限'):
                reports.sync(self.store, self.cfg, AT)
            with self.assertRaisesRegex(RuntimeError, '没有写权限'):  # the unpushed commit is still there: still an error
                reports.sync(self.store, self.cfg, AT)
        self.assertTrue(reports.sync(self.store, self.cfg, AT)['pushed'])
        self.assertEqual(self.remote_files()[:1], ['README.md'])

    def test_the_notice_example_in_the_readme_is_accepted(self):
        example = reports.README.split('```')[1].strip('\n') + '\n'
        n = notices.parse_markdown(example)
        self.assertEqual((n['kind'], n['deadline']), ('DECISION', '2026-10-12T12:00:00+08:00'))
        commented = example.replace('kind: DECISION', 'kind: DECISION   # 需要决定')
        self.assertEqual(notices.parse_markdown(commented)['kind'], 'DECISION')
        self.assertEqual(notices.parse_markdown(example.replace('title: 建议', 'title: C# 与 #1 建议'))['title'], 'C# 与 #1 建议购买稳定的行情数据源')


if __name__ == '__main__':
    unittest.main()
