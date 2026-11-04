"""Catalog steps exercised in a temporary home and a temporary system root."""
import io
import json
import os
import unicodedata
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from helpers import Sandbox

from fmm import shellfiles
from fmm.core import AVAILABLE, CLEAN, PENDING
from fmm.history import History
from fmm.pyenvs import rebuild_venv
from fmm.runner import run_step
from fmm.steps import all_steps
from fmm.steps import docker, general, maintenance, node, php, python, shell, simulators
from fmm.steps.artifacts import Artifacts

MISE = '/fake/mise'
UV = '/fake/uv'
BREW = '/fake/brew'
GIT = '/fake/git'
XCRUN = '/fake/xcrun'
TOOLS = {'mise': MISE, 'uv': UV, 'brew': BREW, 'git': GIT, 'xcrun': XCRUN, 'rustup': None,
         'docker': None, 'zsh': '/bin/zsh', 'removemacai': None, 'pkgutil': '/fake/pkgutil'}


def quiet(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()) as out:
        result = fn(*args, **kwargs)
    return result, out.getvalue()


class CatalogTests(unittest.TestCase):
    def test_ids_unique_requirements_known_and_before(self):
        steps = all_steps()
        ids = [s.id for s in steps]
        self.assertEqual(len(ids), len(set(ids)))
        for step in steps:
            for req in step.requires:
                self.assertIn(req, ids, step.id)
                self.assertLess(ids.index(req), ids.index(step.id), step.id)

    def test_every_step_explains_itself_without_emoji(self):
        for step in all_steps():
            for attr in ('title', 'what', 'why', 'consequences', 'undo'):
                text = getattr(step, attr)
                self.assertTrue(text, '%s.%s is empty' % (step.id, attr))
                for ch in text:
                    self.assertNotIn(unicodedata.category(ch), ('So',), '%s.%s has symbol %r' % (step.id, attr, ch))


class PythonStepTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)
        self.mise_root = self.box.home / '.local/share/mise/installs/python'
        installs = []
        for version in ('3.11.14', '3.12.12'):
            self.box.make('.local/share/mise/installs/python/%s/bin/python3' % version)
            installs.append({'version': version, 'installed': True,
                             'install_path': str(self.mise_root / version)})
        self.box.query.answers[(MISE, 'ls', '--json', '--installed', 'python')] = json.dumps(installs)

    def tearDown(self):
        self.box.close()

    def venv(self, relative, home):
        folder = self.box.mkdir('Projects/' + relative)
        (folder / 'pyvenv.cfg').write_text('home = %s\nversion_info = 3.12.11\n' % home)
        self.box.make('Projects/%s/bin/python' % relative)
        return folder

    def test_detects_only_venvs_outside_mise_and_honors_pins(self):
        conda_bin = self.box.mkdir('miniforge3/bin')
        (conda_bin / 'python3').write_text('')
        self.venv('a/.venv', conda_bin)
        self.venv('b/.venv', self.mise_root / '3.12.12/bin')
        self.box.make('Projects/c/.python-version', '3.11\n')
        self.venv('c/.venv', '/gone/python/bin')
        self.box.make('Projects/c/uv.lock', '')
        det = python.Venvs().detect(self.box.ctx)
        keys = sorted(Path(i.path).parent.name for i in det.items)
        self.assertEqual(keys, ['a', 'c'])
        by_name = {Path(i.path).parent.name: i for i in det.items}
        self.assertIn('3.12.12', by_name['a'].label)
        self.assertEqual(by_name['a'].data['sources'], ['same packages'])
        self.assertIn('3.11.14', by_name['c'].label)
        self.assertEqual(by_name['c'].data['sources'], ['uv.lock'])

    def test_dead_base_without_sources_is_not_selected(self):
        self.venv('d/.venv', '/gone/bin')
        det = python.Venvs().detect(self.box.ctx)
        self.assertFalse(det.items[0].selected)
        self.assertIn('rebuild it by hand', det.items[0].note)
        ops = python.Venvs().ops(self.box.ctx, det.items)
        self.assertEqual(len(ops), 1)
        with self.assertRaises(RuntimeError):
            ops[0].func(self.box.ctx)

    def test_rebuild_rolls_back_when_install_fails(self):
        venv = self.venv('e/.venv', '/x/bin')
        marker = self.box.make('Projects/e/.venv/marker', 'old')
        requirements = self.box.make('req.txt', 'six==1.0\n')

        def create(argv, cwd):
            Path(argv[-1]).mkdir(parents=True)
            (Path(argv[-1]) / 'new').write_text('new')
        self.box.caller.effects[(UV, 'venv')] = create
        self.box.caller.codes[(UV, 'pip', 'install')] = 1
        with self.assertRaises(RuntimeError):
            quiet(rebuild_venv, self.box.ctx, venv, Path('/py'), requirements)
        self.assertEqual(marker.read_text(), 'old')
        self.assertFalse((venv / 'new').exists())
        self.assertFalse(venv.with_name('.venv.free-my-mac-old').exists())

    def test_rebuild_replaces_and_prefers_lock_then_list(self):
        venv = self.venv('f/.venv', '/x/bin')
        requirements = self.box.make('req.txt', 'six==1.0\n')
        self.box.caller.effects[(UV, 'venv')] = lambda argv, cwd: Path(argv[-1]).mkdir(parents=True)
        self.box.caller.codes[(UV, 'sync')] = 1
        used, _ = quiet(rebuild_venv, self.box.ctx, venv, Path('/py'), requirements, use_lock=True)
        self.assertEqual(used, 'package list')
        self.assertFalse((venv / 'pyvenv.cfg').exists())
        sync = [c for c in self.box.caller.calls if c[0][1] == 'sync'][0]
        self.assertEqual(sync[0][-1], '/py')
        self.assertFalse(venv.with_name('.venv.free-my-mac-old').exists())

    def test_uv_only_mise_writes_config_keeping_other_lines(self):
        self.box.make('.config/uv/uv.toml', 'native-tls = true\npython-downloads = "automatic"\n')
        self.box.mkdir('.local/share/uv/python/cpython-3.11.15-macos-aarch64-none')
        step = python.UvOnlyMise()
        det = step.detect(self.box.ctx)
        self.assertEqual({i.key.split(':')[0] for i in det.items}, {'config', 'managed'})
        ops = step.ops(self.box.ctx, det.items)
        quiet(ops[0].func, self.box.ctx)
        text = (self.box.home / '.config/uv/uv.toml').read_text()
        self.assertIn('native-tls = true', text)
        self.assertIn('python-downloads = "never"', text)
        self.assertNotIn('automatic', text)
        self.assertEqual(ops[1].argv, [UV, 'python', 'uninstall', '--all'])

    def test_brew_python_unlink_only_linked(self):
        for formula in ('python@3.13', 'python@3.14'):
            self.box.mkdir('opt/homebrew/Cellar/' + formula, base=self.box.root)
        self.box.make('opt/homebrew/var/homebrew/linked/python@3.13', base=self.box.root)
        self.box.query.answers[(BREW, 'uses', '--installed', 'python@3.13')] = 'tool-a\ntool-b\n'
        det = python.BrewPythonUnlink().detect(self.box.ctx)
        self.assertEqual([i.key for i in det.items], ['formula:python@3.13'])
        self.assertIn('tool-a', det.items[0].note)

    def test_conda_migrate_items(self):
        self.box.mkdir('miniforge3/bin')
        self.box.make('.config/free-my-mac/local.json', json.dumps({
            'conda_projects': {'audio_model': 'lab/audio-model'},
            'uv_tools': {'scanner': 'git+https://example.com/scanner'}}))
        env = self.box.mkdir('miniforge3/envs/audio_model')
        self.box.make('miniforge3/envs/audio_model/bin/python')
        self.box.mkdir('miniforge3/envs/audio_model/lib/python3.11/site-packages')
        # Folder 'python3.1' without site-packages: sorted as text, it came before 'python3.11'.
        self.box.mkdir('miniforge3/envs/audio_model/lib/python3.1')
        self.box.make('miniforge3/envs/scanner/bin/python')
        self.box.make('miniforge3/envs/py311/bin/python')
        self.box.mkdir('Projects/lab/audio-model')
        self.box.query.answers[(UV, 'tool', 'list')] = 'ruff v0.1\n'
        det = python.CondaMigrate().detect(self.box.ctx)
        keys = [i.key for i in det.items]
        self.assertEqual(keys, ['env:audio_model', 'tool:scanner'])
        self.assertIn('3.11.14', det.items[0].data['python'])
        self.assertTrue(any('py311' in n for n in det.notes))
        ops = python.CondaMigrate().ops(self.box.ctx, det.items)
        self.assertIn('git+https://example.com/scanner', ops[1].argv)
        self.assertTrue(str(env))


class PhpDockerTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)

    def tearDown(self):
        self.box.close()

    def test_hosts_only_removes_test_only_lines(self):
        hosts = self.box.make('etc/hosts', '127.0.0.1 localhost\n127.0.0.1 a.test b.test\n'
                              '# 127.0.0.1 c.test\n127.0.0.1 keep.local x.test\n', base=self.box.root)
        step = php.HostsTest()
        det = step.detect(self.box.ctx)
        self.assertEqual([i.label for i in det.items], ['127.0.0.1 a.test b.test'])
        quiet(step.ops(self.box.ctx, det.items)[0].func, self.box.ctx)
        self.assertEqual(hosts.read_text(), '127.0.0.1 localhost\n# 127.0.0.1 c.test\n127.0.0.1 keep.local x.test\n')

    def test_docker_leftovers_does_not_double_count_vm_and_finds_links(self):
        self.box.mkdir('Applications/Docker.app/Contents/Resources/bin', base=self.box.root)
        self.box.make('Library/Containers/com.docker.docker/Data/vm.raw', 'x' * 5000)
        bin_dir = self.box.mkdir('usr/local/bin', base=self.box.root)
        os.symlink('/Applications/Docker.app/Contents/Resources/bin/docker', bin_dir / 'docker')
        os.symlink('/usr/local/aws-cli/aws', bin_dir / 'aws')
        det = docker.DockerLeftovers().detect(self.box.ctx)
        labels = {i.label: i for i in det.items}
        vm = labels['~/Library/Containers/com.docker.docker']
        self.assertIsNone(vm.size)
        self.assertTrue(any(label.endswith('/usr/local/bin/docker') for label in labels))
        self.assertFalse(any(label.endswith('/aws') for label in labels))

    def test_desktop_quits_by_bundle_id_and_runs_official_uninstaller(self):
        self.box.make('Applications/Docker.app/Contents/MacOS/uninstall', base=self.box.root)
        step = docker.DockerDesktop()
        det = step.detect(self.box.ctx)
        ops = step.ops(self.box.ctx, det.items)
        self.assertIn('com.docker.docker', ops[0].argv[-1])
        self.assertTrue(ops[0].tolerate_failure)
        self.assertTrue(ops[1].argv[0].endswith('Contents/MacOS/uninstall'))


class SimulatorTests(unittest.TestCase):
    def test_runtimes_devices_and_dyld(self):
        box = Sandbox(tools=TOOLS)
        try:
            runtimes = {'A': {'identifier': 'A', 'version': '17.0', 'build': '21A', 'deletable': True,
                              'platformIdentifier': 'com.apple.platform.iphonesimulator', 'sizeBytes': 100},
                        'B': {'identifier': 'B', 'version': '18.4', 'deletable': False,
                              'platformIdentifier': 'com.apple.platform.iphonesimulator'}}
            box.query.answers[(XCRUN, 'simctl', 'runtime', 'list', '-j')] = json.dumps(runtimes)
            box.query.answers[(XCRUN, 'simctl', 'list', 'devices', '-j')] = json.dumps(
                {'devices': {'r': [{'udid': '1'}, {'udid': '2'}]}})
            box.make('Library/Developer/CoreSimulator/Caches/dyld/x', base=box.root)
            step = simulators.Simulators()
            det = step.detect(box.ctx)
            self.assertEqual([i.key.split(':')[0] for i in det.items], ['runtime', 'devices', 'dyld'])
            ops = step.ops(box.ctx, det.items)
            self.assertEqual(ops[0].argv[-2:], ['delete', 'all'])
            self.assertEqual(ops[1].argv[-3:], ['runtime', 'delete', 'A'])
        finally:
            box.close()


class NodeTests(unittest.TestCase):
    def test_globals_classification(self):
        box = Sandbox(tools=TOOLS)
        try:
            base = 'Library/Application Support/fnm/node-versions/v22.0.0/installation/lib/node_modules/'
            box.make(base + 'tsx/package.json', json.dumps({'bin': {'tsx': 'x'}}))
            box.make(base + 'lodash/package.json', json.dumps({}))
            box.make(base + '@google/gemini-cli/package.json', json.dumps({'bin': 'x'}))
            box.make(base + 'npm/package.json', json.dumps({'bin': 'x'}))
            box.mkdir('opt/homebrew/Cellar/gemini-cli', base=box.root)
            det = node.NodeGlobals().detect(box.ctx)
            self.assertEqual([i.key for i in det.items], ['pkg:tsx'])
            self.assertEqual(len(det.notes), 2)
            ops = node.NodeGlobals().ops(box.ctx, det.items)
            self.assertEqual(ops[0].argv, [MISE, 'use', '-g', 'npm:tsx'])
        finally:
            box.close()


class GeneralTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)

    def tearDown(self):
        self.box.close()

    def test_intel_links_ignore_pkg_installs(self):
        root = self.box.root
        self.box.make('usr/local/Cellar/wget/1.0/bin/wget', base=root)
        self.box.make('usr/local/aws-cli/aws', base=root)
        bin_dir = self.box.mkdir('usr/local/bin', base=root)
        os.symlink('../Cellar/wget/1.0/bin/wget', bin_dir / 'wget')
        os.symlink('../aws-cli/aws', bin_dir / 'aws')
        os.symlink('/usr/local/heroku/bin/heroku', bin_dir / 'heroku')
        found = general.intel_links(self.box.ctx)
        self.assertEqual([p.name for p in found], ['wget'])
        det = general.IntelBrew().detect(self.box.ctx)
        self.assertTrue(det.items[0].key.startswith('links:'))
        self.assertTrue(any('wget' in n for n in det.notes))

    def test_broken_links_only(self):
        bin_dir = self.box.mkdir('.local/bin')
        target = self.box.make('real')
        os.symlink(str(target), bin_dir / 'ok')
        os.symlink('/nowhere/at/all', bin_dir / 'broken')
        det = general.BrokenLinks().detect(self.box.ctx)
        self.assertEqual([Path(i.path).name for i in det.items], ['broken'])

    def test_rust_keeps_default_and_pinned(self):
        self.box.ctx._tools['rustup'] = '/fake/rustup'
        self.box.query.answers[('/fake/rustup', 'toolchain', 'list')] = (
            'stable-aarch64-apple-darwin (active, default)\n1.78-aarch64-apple-darwin\n1.85-aarch64-apple-darwin\n')
        self.box.make('Projects/p/rust-toolchain.toml', '[toolchain]\nchannel = "1.85"\n')
        det = general.RustBrew().detect(self.box.ctx)
        self.assertEqual([i.key for i in det.items], ['toolchain:1.78-aarch64-apple-darwin'])


class ArtifactTests(unittest.TestCase):
    def test_age_and_git_tracking(self):
        box = Sandbox(tools=TOOLS)
        try:
            now = 1793534400.0  # 2026-11-01 12:00 local time, roughly
            box.make('Projects/rs/Cargo.toml')
            box.make('Projects/rs/target/debug/x', 'x' * 100)
            box.make('Projects/old/package.json')
            box.make('Projects/old/node_modules/a/index.js')
            box.make('Projects/new/package.json')
            box.make('Projects/new/node_modules/a/index.js')
            box.make('Projects/tracked/package.json')
            box.make('Projects/tracked/node_modules/a/index.js')

            def log(argv, cwd):
                folder = argv[argv.index('-C') + 1]
                age = {'old': 200, 'new': 5, 'rs': 1, 'tracked': 300}[Path(folder).name]
                return '%d\n' % (now - age * 86400)

            def ls_files(argv, cwd):
                return 'node_modules/a/index.js\n' if '/tracked' in argv[argv.index('-C') + 1] else ''
            box.query.answers[(GIT, '-c', 'core.fsmonitor=false', '-C')] = \
                lambda argv, cwd: log(argv, cwd) if 'log' in argv else ls_files(argv, cwd)
            det = Artifacts().detect(box.ctx)
            names = sorted(Path(i.path).parent.name + '/' + Path(i.path).name for i in det.items)
            self.assertEqual(names, ['old/node_modules', 'rs/target'])
        finally:
            box.close()


class ShellStepTests(unittest.TestCase):
    TOKEN = 'fake-token-value-123'

    def setUp(self):
        self.box = Sandbox(tools=TOOLS)
        self.history = History(self.box.home / 'h.jsonl')

    def tearDown(self):
        self.box.close()

    def test_rules_apply_with_snapshot_and_keep_personal_lines(self):
        zshrc = self.box.make('.zshrc', 'alias ll="ls -la"\n\neval "$(fnm env --use-on-cd)"\n\nexport PATH="/opt/homebrew/bin:$PATH"\nalias g=git\n')
        step = shell.ShellRules()
        det = step.detect(self.box.ctx)
        self.assertEqual(det.state, PENDING)
        self.assertTrue(any('fnm' in n or 'homebrew' in n for n in det.notes))
        quiet(step.ops(self.box.ctx, det.items)[0].func, self.box.ctx)
        text = zshrc.read_text()
        self.assertIn('alias ll="ls -la"', text)
        self.assertIn('alias g=git', text)
        self.assertNotIn('fnm env', text)
        copies = list((self.box.ctx.backup_dir() / 'shell-before').iterdir())
        self.assertEqual(len(copies), 1)
        self.assertEqual(oct(copies[0].stat().st_mode & 0o777), '0o600')
        self.assertEqual(step.detect(self.box.ctx).state, CLEAN)

    def test_cf_token_never_printed_nor_recorded(self):
        zshrc = self.box.make('.zshrc', 'export CF_API_TOKEN="%s"\nexport CLOUDFLARE_API_TOKEN="%s"\n' % (self.TOKEN, self.TOKEN))
        stored = {}

        class FakeKeychain:
            def store(self, service, account, value):
                stored[service] = value
        step = shell.CfToken()
        det = step.detect(self.box.ctx)
        self.assertNotIn(self.TOKEN, repr(det))
        with patch.object(shellfiles, 'KEYCHAIN', FakeKeychain()):
            result, output = quiet(run_step, step, self.box.ctx, [i.key for i in det.items], self.history)
        self.assertEqual(result.status, 'ok')
        self.assertEqual(stored, {'cloudflare-api-token': self.TOKEN})
        self.assertNotIn(self.TOKEN, output)
        self.assertNotIn(self.TOKEN, (self.box.home / 'h.jsonl').read_text())
        self.assertNotIn(self.TOKEN, zshrc.read_text())
        self.assertIn('security find-generic-password', zshrc.read_text())

    def test_old_files_backed_up_before_removal(self):
        self.box.make('.zshrc.old', 'old stuff')
        self.box.make('.zcompdump-host-5.9', 'cache')
        step = shell.ShellOldFiles()
        det = step.detect(self.box.ctx)
        result, _ = quiet(run_step, step, self.box.ctx, [i.key for i in det.items], self.history)
        self.assertEqual(result.status, 'ok')
        self.assertFalse((self.box.home / '.zshrc.old').exists())
        self.assertFalse((self.box.home / '.zcompdump-host-5.9').exists())
        self.assertEqual((self.box.ctx.backup_dir() / 'shell-old/.zshrc.old').read_text(), 'old stuff')


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)

    def tearDown(self):
        self.box.close()

    def cowork_home(self):
        base = self.box.mkdir('Library/Application Support/Claude')
        config = base / 'claude_desktop_config.json'
        config.write_text(json.dumps({'mcpServers': {'fixture': {'command': 'test'}}, 'preferences': {'other': True}}))
        self.box.make('Library/Application Support/Claude/vm_bundles/disk', 'fixture')
        self.box.make('Library/Application Support/Claude/local-agent-mode-sessions/session', 'keep')
        return base, config

    def test_cowork_detect_is_read_only(self):
        base, config = self.cowork_home()
        before = config.read_bytes()
        det = maintenance.Cowork().detect(self.box.ctx)
        self.assertEqual({i.key for i in det.items}, {'pref', 'vm'})
        self.assertEqual(config.read_bytes(), before)
        self.assertTrue((base / 'vm_bundles/disk').exists())

    def test_cowork_apply_backs_up_and_preserves_everything_else(self):
        base, config = self.cowork_home()
        before = config.read_bytes()
        with patch.object(maintenance, 'assert_claude_closed'):
            quiet(maintenance.Cowork().apply, self.box.ctx)
        data = json.loads(config.read_text())
        self.assertIs(data['preferences']['secureVmFeaturesEnabled'], False)
        self.assertTrue(data['preferences']['other'])
        self.assertIn('fixture', data['mcpServers'])
        self.assertFalse((base / 'vm_bundles').exists())
        self.assertTrue((base / 'local-agent-mode-sessions/session').exists())
        backups = list((self.box.ctx.backup_dir() / 'claude').iterdir())
        self.assertEqual(backups[0].read_bytes(), before)
        self.assertEqual(maintenance.Cowork().detect(self.box.ctx).state, CLEAN)

    def test_cowork_running_app_blocks_before_writes(self):
        _base, config = self.cowork_home()
        before = config.read_bytes()
        with patch.object(maintenance, 'assert_claude_closed', side_effect=ValueError('close the app')):
            with self.assertRaises(ValueError):
                maintenance.Cowork().apply(self.box.ctx)
        self.assertEqual(config.read_bytes(), before)

    def test_cowork_rejects_duplicate_keys_and_symlinked_vm(self):
        base, config = self.cowork_home()
        os.rename(base / 'vm_bundles', base / 'real_vm')
        os.symlink(str(base / 'real_vm'), base / 'vm_bundles')
        with patch.object(maintenance, 'assert_claude_closed'), self.assertRaises(ValueError):
            maintenance.Cowork().apply(self.box.ctx)
        self.assertTrue((base / 'real_vm/disk').exists())
        config.write_text('{"preferences": {}, "preferences": {}}')
        with self.assertRaises(ValueError):
            maintenance.Cowork().detect(self.box.ctx)

    def test_cache_threshold_and_brew_size(self):
        self.box.make('.npm/_cacache/x', 'x')
        det = maintenance.NpmCache().detect(self.box.ctx)
        self.assertEqual(det.state, CLEAN)
        self.box.query.answers[('/usr/bin/du',)] = None
        with patch.object(self.box.ctx, 'du', return_value=200 * 1000 * 1000):
            self.assertEqual(maintenance.NpmCache().detect(self.box.ctx).state, PENDING)
        self.assertEqual(maintenance.parse_brew_size('==> This operation would free approximately 1.5GB of disk space.'),
                         1500000000)

    def test_brew_cleanup_lists_orphans(self):
        self.box.query.answers[(BREW, 'cleanup')] = 'Would remove: /x/foo--1.0 (10MB)\n==> This operation would free approximately 10MB of disk space.\n'
        self.box.query.answers[(BREW, 'autoremove', '-n')] = (
            '==> Would autoremove 3 unneeded formulae:\nlibfoo\nlibbar\npython@3.13\n')
        det = maintenance.BrewCleanup().detect(self.box.ctx)
        self.assertEqual([i.key for i in det.items], ['cleanup', 'orphan:libfoo', 'orphan:libbar', 'orphan:python@3.13'])
        self.assertFalse(det.items[-1].selected)
        chosen = [i for i in det.items if i.selected]
        ops = maintenance.BrewCleanup().ops(self.box.ctx, chosen)
        self.assertEqual(ops[0].argv, [BREW, 'uninstall', '--formula', 'libfoo', 'libbar'])
        self.assertEqual(ops[1].argv, [BREW, 'cleanup', '--prune=all'])
        self.assertNotIn('autoremove', ' '.join(' '.join(o.argv) for o in ops))

    def test_apple_intelligence_is_always_available(self):
        det = maintenance.AppleIntelligence().detect(self.box.ctx)
        self.assertEqual(det.state, AVAILABLE)
        self.assertEqual([i.key for i in det.items], ['install', 'off'])


class PathsStepVariantsTests(unittest.TestCase):
    def test_taps_and_tolerated_brew_failures(self):
        box = Sandbox(tools=TOOLS)
        try:
            box.mkdir('opt/homebrew/Library/Taps/shivammathur/homebrew-php', base=box.root)
            box.mkdir('opt/homebrew/Cellar/composer', base=box.root)
            step = php.PhpLeftovers()
            det = step.detect(box.ctx)
            keys = [i.key for i in det.items]
            self.assertIn('tap:shivammathur/php', keys)
            ops = step.ops(box.ctx, det.items)
            self.assertEqual(ops[-1].argv, [BREW, 'untap', 'shivammathur/php'])
            self.assertTrue(ops[-1].tolerate_failure)
        finally:
            box.close()

    def test_receipts_found_via_pkgutil(self):
        box = Sandbox(tools=TOOLS, answers={('/fake/pkgutil', '--pkgs'): 'com.microsoft.dotnet.a\norg.other\n'})
        try:
            from fmm.steps.dotnet import DotnetPkg
            det = DotnetPkg().detect(box.ctx)
            receipts = [i for i in det.items if i.key.startswith('receipts:')][0]
            self.assertEqual(receipts.data['ids'], ['com.microsoft.dotnet.a'])
        finally:
            box.close()


if __name__ == '__main__':
    unittest.main()
