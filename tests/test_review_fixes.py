"""Regression tests for the findings of the independent review (2026-11-01)."""
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from helpers import Sandbox

from fmm import pyenvs
from fmm.core import CLEAN, PENDING, Detection, Item, Op, Step, redact
from fmm.history import History
from fmm.pyenvs import OLD_SUFFIX, find_venvs, rebuild_venv
from fmm.runner import run_step
from fmm.steps import general, maintenance, php, prep, python, shell
from fmm.steps.artifacts import Artifacts

UV = '/fake/uv'
GIT = '/fake/git'
TOOLS = {'mise': '/fake/mise', 'uv': UV, 'brew': '/fake/brew', 'git': GIT, 'xcrun': None, 'rustup': None,
         'docker': None, 'zsh': '/bin/zsh', 'removemacai': None, 'pkgutil': '/fake/pkgutil'}


def quiet(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()) as out:
        result = fn(*args, **kwargs)
    return result, out.getvalue()


class VenvSafetyTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)
        self.box.caller.effects[(UV, 'venv')] = lambda argv, cwd: Path(argv[-1]).mkdir(parents=True)

    def tearDown(self):
        self.box.close()

    def test_project_root_venv_is_never_rebuilt(self):
        project = self.box.mkdir('Projects/app')
        (project / 'pyvenv.cfg').write_text('home = /gone/bin\n')
        self.box.make('Projects/app/main.py', 'print(1)')
        self.box.mkdir('Projects/app/.git')
        with self.assertRaises(RuntimeError):
            quiet(rebuild_venv, self.box.ctx, project, Path('/py'), self.box.make('r.txt', ''))
        self.assertTrue((project / 'main.py').exists())
        self.assertTrue((project / '.git').is_dir())
        self.assertEqual(self.box.caller.calls, [])
        det = python.Venvs().detect(self.box.ctx)
        self.assertFalse(det.items[0].selected)
        self.assertIn('main.py', det.items[0].note)
        ops = python.Venvs().ops(self.box.ctx, det.items)
        self.assertEqual([o.argv for o in ops], [None])
        with self.assertRaises(RuntimeError):
            ops[0].func(self.box.ctx)
        self.assertTrue((project / 'main.py').exists())

    def test_rollback_keeps_old_when_cleanup_of_new_fails(self):
        venv = self.box.mkdir('Projects/p/.venv')
        (venv / 'pyvenv.cfg').write_text('home = /x\n')
        self.box.caller.codes[(UV, 'pip', 'install')] = 1
        with patch.object(pyenvs, 'safe_remove', side_effect=OSError('busy')):
            with self.assertRaises(OSError):
                quiet(rebuild_venv, self.box.ctx, venv, Path('/py'), self.box.make('r.txt', ''))
        old = venv.with_name('.venv' + OLD_SUFFIX)
        self.assertTrue((old / 'pyvenv.cfg').exists())
        self.assertNotIn(old, find_venvs(self.box.ctx))

    def test_exact_list_comes_before_lock_when_base_is_alive(self):
        venv = self.box.mkdir('Projects/q/.venv')
        (venv / 'pyvenv.cfg').write_text('home = /x\n')
        used, _ = quiet(rebuild_venv, self.box.ctx, venv, Path('/py'), self.box.make('r.txt', 'six==1\n'),
                        use_lock=True, prefer_list=True)
        self.assertEqual(used, 'package list')
        self.assertFalse(any(c[0][1] == 'sync' for c in self.box.caller.calls))

    def test_app_venv_on_conda_is_tracked_and_blocks_conda_remove(self):
        conda_bin = self.box.mkdir('miniforge3/bin')
        (conda_bin / 'python3').write_text('')
        app = self.box.mkdir('Library/Application Support/dev.example.app/engine/venv')
        (app / 'pyvenv.cfg').write_text('home = %s\nversion_info = 3.12.11\n' % conda_bin)
        other = self.box.mkdir('Library/Application Support/Other/venv')
        (other / 'pyvenv.cfg').write_text('home = /opt/homebrew/opt/python@3.13/bin\n')
        tracked = pyenvs.tracked_venvs(self.box.ctx)
        self.assertEqual(tracked, [(app, True)])
        mise_root = self.box.home / '.local/share/mise/installs/python/3.12.12'
        self.box.make('.local/share/mise/installs/python/3.12.12/bin/python3')
        self.box.query.answers[('/fake/mise', 'ls', '--json', '--installed', 'python')] = json.dumps(
            [{'version': '3.12.12', 'installed': True, 'install_path': str(mise_root)}])
        det = python.Venvs().detect(self.box.ctx)
        self.assertEqual(len(det.items), 1)
        self.assertIn('3.12.12', det.items[0].label)
        self.assertIn('app venv', det.items[0].note)
        remove = python.CondaRemove().detect(self.box.ctx)
        self.assertFalse(any(i.selected for i in remove.items))
        self.assertTrue(any('engine/venv' in n for n in remove.notes))


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)
        self.history = History(self.box.home / 'h.jsonl')

    def tearDown(self):
        self.box.close()

    def test_uv_uninstalls_only_checked_versions(self):
        for name in ('cpython-3.11.15-macos-aarch64-none', 'cpython-3.13.1-macos-aarch64-none'):
            self.box.mkdir('.local/share/uv/python/' + name)
        step = python.UvOnlyMise()
        det = step.detect(self.box.ctx)
        one = [i for i in det.items if i.key.endswith('3.11.15-macos-aarch64-none')]
        self.assertEqual(step.ops(self.box.ctx, one)[0].argv[-1], 'cpython-3.11.15-macos-aarch64-none')
        both = [i for i in det.items if i.key.startswith('managed:')]
        self.assertEqual(step.ops(self.box.ctx, both)[0].argv[-1], '--all')

    def test_shell_rules_remove_only_the_checked_occurrence(self):
        line = 'export PATH="/opt/homebrew/bin:$PATH"'
        zshrc = self.box.make('.zshrc', 'alias a=b\n%s\nalias c=d\n%s\n' % (line, line))
        step = shell.ShellRules()
        det = step.detect(self.box.ctx)
        brew = sorted((i for i in det.items if i.data['rule'] == 'zshrc-brew-path'), key=lambda i: i.data['begin'])
        self.assertEqual(len(brew), 2)
        result, _ = quiet(run_step, step, self.box.ctx, [brew[1].key], self.history)
        self.assertEqual(result.status, 'ok')
        self.assertEqual(zshrc.read_text().count(line), 1)
        self.assertTrue(zshrc.read_text().startswith('alias a=b\n' + line))

    def test_cowork_pref_only_keeps_vm(self):
        base = self.box.mkdir('Library/Application Support/Claude')
        (base / 'claude_desktop_config.json').write_text('{"preferences": {}}')
        self.box.make('Library/Application Support/Claude/vm_bundles/disk', 'x')
        step = maintenance.Cowork()
        det = step.detect(self.box.ctx)
        pref = [i for i in det.items if i.key == 'pref']
        with patch.object(maintenance, 'assert_claude_closed'):
            quiet(step.ops(self.box.ctx, pref)[0].func, self.box.ctx)
        self.assertTrue((base / 'vm_bundles/disk').exists())
        data = json.loads((base / 'claude_desktop_config.json').read_text())
        self.assertIs(data['preferences']['secureVmFeaturesEnabled'], False)
        vm = [i for i in step.detect(self.box.ctx).items if i.key == 'vm']
        with patch.object(maintenance, 'assert_claude_closed'):
            quiet(step.ops(self.box.ctx, vm)[0].func, self.box.ctx)
        self.assertFalse((base / 'vm_bundles').exists())

    def test_changed_list_drops_the_item_instead_of_running_more(self):
        bin_dir = self.box.mkdir('usr/local/bin', base=self.box.root)
        self.box.make('usr/local/Cellar/a/1/bin/a', base=self.box.root)
        self.box.make('usr/local/Cellar/b/1/bin/b', base=self.box.root)
        os.symlink('../Cellar/a/1/bin/a', bin_dir / 'a')
        step = general.IntelBrew()
        reviewed = step.detect(self.box.ctx).items[0].key
        os.symlink('../Cellar/b/1/bin/b', bin_dir / 'b')
        result, output = quiet(run_step, step, self.box.ctx, [reviewed], self.history)
        self.assertEqual(result.status, 'nothing')
        self.assertTrue(os.path.lexists(bin_dir / 'a'))


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=dict(TOOLS, brew=None))
        self.box.make('.zshrc', 'alias a=b\n')
        self.box.make('etc/hosts', '127.0.0.1 localhost\n', base=self.box.root)
        self.history = History(self.box.home / 'h.jsonl')

    def tearDown(self):
        self.box.close()

    def test_partial_backup_keeps_step_pending_with_missing_items(self):
        self.box.mkdir('miniforge3/bin')
        self.box.make('miniforge3/envs/analytics/bin/python')
        step = prep.Backup()
        det = step.detect(self.box.ctx)
        keys = [i.key for i in det.items if i.key in ('shell', 'etc')]
        result, _ = quiet(run_step, step, self.box.ctx, keys, self.history)
        self.assertEqual(result.status, 'ok')
        again = step.detect(self.box.ctx)
        self.assertEqual(again.state, PENDING)
        self.assertEqual([i.key for i in again.items if i.selected], ['pkgutil', 'conda:analytics'])

    def test_failed_conda_export_does_not_count(self):
        self.box.mkdir('miniforge3/bin')
        self.box.make('miniforge3/envs/analytics/bin/python')
        step = prep.Backup()
        det = step.detect(self.box.ctx)
        result, _ = quiet(run_step, step, self.box.ctx, [i.key for i in det.items], self.history)
        self.assertEqual(result.status, 'failed')
        self.assertIn('conda:analytics', [i.key for i in step.detect(self.box.ctx).items if i.selected])

    def test_complete_backup_is_clean(self):
        step = prep.Backup()
        det = step.detect(self.box.ctx)
        self.box.query.answers[('/fake/pkgutil', '--pkgs')] = 'com.a\n'
        result, _ = quiet(run_step, step, self.box.ctx, [i.key for i in det.items], self.history)
        self.assertEqual(result.status, 'ok')
        self.assertEqual(step.detect(self.box.ctx).state, CLEAN)


class RunnerRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)
        self.history = History(self.box.home / 'h.jsonl')

    def tearDown(self):
        self.box.close()

    def test_unexpected_error_after_first_op_is_recorded(self):
        class Two(Step):
            id = 'two'
            title = 'Two steps'

            def detect(self, ctx):
                return Detection(PENDING, [Item('a', 'a')])

            def ops(self, ctx, items):
                return [Op('first', 'true', func=lambda c: None),
                        Op('second', 'boom', argv=['/bin/echo'])]
        self.box.caller.effects[('/bin/echo',)] = lambda argv, cwd: (_ for _ in ()).throw(MemoryError('x'))
        result, _ = quiet(run_step, Two(), self.box.ctx, ['a'], self.history)
        self.assertEqual(result.status, 'failed')
        entry = self.history.entries()[-1]
        self.assertEqual(entry['status'], 'failed')
        self.assertEqual(entry['ops'][0]['label'], 'first')

    def test_detect_error_on_revalidation_runs_nothing(self):
        class Bad(Step):
            id = 'bad'
            title = 'Bad'

            def detect(self, ctx):
                raise OSError('gone')
        result, _ = quiet(run_step, Bad(), self.box.ctx, ['a'], self.history)
        self.assertEqual(result.status, 'cancelled')


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)

    def tearDown(self):
        self.box.close()

    def test_herd_sites_folder_only_when_empty(self):
        self.box.make('Herd/site/index.php', '<?php')
        paths = [str(p) for p in php.Herd().target_paths(self.box.ctx)]
        self.assertNotIn(str(self.box.home / 'Herd'), paths)
        os.remove(self.box.home / 'Herd/site/index.php')
        os.rmdir(self.box.home / 'Herd/site')
        self.assertIn(str(self.box.home / 'Herd'), [str(p) for p in php.Herd().target_paths(self.box.ctx)])

    def test_hosts_snapshot_before_rewrite(self):
        self.box.make('etc/hosts', '127.0.0.1 localhost\n127.0.0.1 a.test\n', base=self.box.root)
        step = php.HostsTest()
        det = step.detect(self.box.ctx)
        quiet(step.ops(self.box.ctx, det.items)[0].func, self.box.ctx)
        copies = list((self.box.ctx.backup_dir() / 'etc-before').iterdir())
        self.assertEqual(copies[0].read_text(), '127.0.0.1 localhost\n127.0.0.1 a.test\n')

    def test_artifacts_skip_when_git_fails_for_other_reasons(self):
        from fmm.core import QueryResult
        self.box.make('Projects/rs/Cargo.toml')
        self.box.make('Projects/rs/target/x')
        self.box.make('Projects/plain/Cargo.toml')
        self.box.make('Projects/plain/target/x')

        def git(argv, cwd):
            folder = argv[argv.index('-C') + 1]
            if folder.endswith('/rs'):
                return QueryResult(128, '', 'fatal: detected dubious ownership in repository')
            return QueryResult(128, '', 'fatal: not a git repository (or any of the parent directories): .git')
        self.box.query.answers[(GIT,)] = git
        det = Artifacts().detect(self.box.ctx)
        self.assertEqual([Path(i.path).parent.name for i in det.items], ['plain'])
        self.assertTrue(any('dubious' in n for n in det.notes))

    def test_transient_broken_links_unchecked(self):
        bin_dir = self.box.mkdir('.local/bin')
        os.symlink('/Applications/Gone.app/Contents/bin/tool', bin_dir / 'tool')
        os.symlink('/nowhere/x', bin_dir / 'x')
        det = general.BrokenLinks().detect(self.box.ctx)
        selected = {Path(i.path).name: i.selected for i in det.items}
        self.assertEqual(selected, {'tool': False, 'x': True})

    def test_redact_more_secret_shapes(self):
        self.assertEqual(redact('export GITHUB_PAT=abc'), 'export GITHUB_PAT=<hidden>')
        self.assertEqual(redact('https://user:pw@host/x'), 'https://user:<hidden>@host/x')
        self.assertEqual(redact('export PATH="/opt/homebrew/bin:$PATH"'), 'export PATH="/opt/homebrew/bin:$PATH"')


if __name__ == '__main__':
    unittest.main()
