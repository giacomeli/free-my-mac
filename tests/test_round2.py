"""Second round (2026-11-01): fixes from the first run and additional cleanup steps."""
import io
import json
import os
import plistlib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from helpers import Sandbox

from fmm.core import CLEAN, PENDING, Detection, Item, Step, QueryResult
from fmm.history import History
from fmm.pyenvs import backup_recipe, conda_python_version, remap_editables
from fmm.runner import run_step
from fmm.steps import all_steps, apps, devtools, leftovers, maintenance, python, shell, simulators

MISE = '/fake/mise'
UV = '/fake/uv'
BREW = '/fake/brew'
TOOLS = {'mise': MISE, 'uv': UV, 'brew': BREW, 'git': None, 'xcrun': None, 'zsh': '/bin/zsh', 'pkgutil': None,
         'security': None, 'rustup': None, 'docker': None, 'removemacai': None, 'fig': None, 'wine': None,
         'wine64': None, 'dart': None, 'flutter': None, 'ti': None, 'appc': None, 'axway': None}


def quiet(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()) as out:
        result = fn(*args, **kwargs)
    return result, out.getvalue()


def mise_answer(box, *versions):
    root = box.home / '.local/share/mise/installs/python'
    entries = []
    for version in versions:
        box.make('.local/share/mise/installs/python/%s/bin/python3' % version, '')
        entries.append({'version': version, 'installed': True, 'install_path': str(root / version)})
    box.query.answers[(MISE, 'ls', '--json', '--installed', 'python')] = json.dumps(entries)
    return root


class CondaVersionTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)

    def tearDown(self):
        self.box.close()

    def test_conda_meta_wins(self):
        env = self.box.mkdir('env')
        self.box.make('env/conda-meta/python-3.11.13-hc22306f_0_cpython.json', '{}')
        self.box.make('env/conda-meta/python_abi-3.11-8_cp311.json', '{}')
        self.box.make('env/conda-meta/python-dateutil-2.9.0-pyhd8ed1ab_0.json', '{}')
        self.assertEqual(conda_python_version(env), '3.11')

    def test_lib_folders_compare_numbers_and_need_site_packages(self):
        env = self.box.mkdir('env')
        self.box.mkdir('env/lib/python3.1')
        self.box.mkdir('env/lib/python3.9/site-packages')
        self.box.mkdir('env/lib/python3.11/site-packages')
        self.assertEqual(conda_python_version(env), '3.11')
        self.assertIsNone(conda_python_version(self.box.mkdir('empty')))

    def test_backup_recipe(self):
        folder = self.box.mkdir('bkp/conda')
        (folder / 'model.yml').write_text("name: model\nchannels:\n  - conda-forge\ndependencies:\n  - python=3.11\n"
                                        "  - ffmpeg[version='<5']\n  - pip\nprefix: /x\n")
        self.assertEqual(backup_recipe(folder, 'model'), ('3.11', ["ffmpeg[version='<5']"]))
        (folder / 'other.explicit.txt').write_text('https://x/osx-arm64/python-3.10.4-h1_0_cpython.conda\n')
        self.assertEqual(backup_recipe(folder, 'other'), ('3.10', []))

    def test_remap_editables(self):
        self.box.mkdir('Projects/lab/audio-model')
        self.box.mkdir('Projects/lab/dup')
        self.box.mkdir('Projects/archive/dup')
        lines = ['six==1.0', '-e /old/Projects/audio-model', '-e /old/gone', '-e /old/dup',
                 '-e ' + str(self.box.home / 'Projects/lab/audio-model')]
        result, warnings = remap_editables(self.box.ctx, lines)
        self.assertEqual(result, ['six==1.0', '-e ' + str(self.box.home / 'Projects/lab/audio-model'),
                                  '-e ' + str(self.box.home / 'Projects/lab/audio-model')])
        self.assertEqual(len(warnings), 3)
        self.assertTrue(any('2 folders' in w for w in warnings))


class CondaRestoreTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)
        self.history = History(self.box.home / 'h.jsonl')
        mise_answer(self.box, '3.11.18', '3.12.16')
        self.box.make('.config/free-my-mac/local.json', json.dumps({'conda_projects': {'audio_model': 'lab/audio-model'}}))
        self.project = self.box.mkdir('Projects/lab/audio-model')
        self.box.mkdir('Projects/lab/audio-utils')
        folder = self.box.mkdir('bkp/free-my-mac/2026-11-01/conda')
        (folder / 'audio_model.yml').write_text('dependencies:\n  - python=3.11\n  - ffmpeg\n')
        (folder / 'audio_model.packages.txt').write_text(
            'torch==2.12.0\ntorchaudio==2.7.1\n-e /Users/x/Projects/audio-model\n-e /Users/x/Projects/audio-utils\n')

    def tearDown(self):
        self.box.close()

    def test_restore_item_from_backup(self):
        det = python.CondaMigrate().detect(self.box.ctx)
        self.assertEqual([i.key for i in det.items], ['restore:audio_model'])
        item = det.items[0]
        self.assertIn('from the Nov 01 backup', item.label)
        self.assertIn('Python 3.11', item.label)
        self.assertIn('3.11.18', item.data['python'])
        self.assertIn('ffmpeg', item.note)
        self.assertTrue(item.selected)

    def test_restore_falls_back_to_exact_list_without_resolution(self):
        calls = []

        def caller(argv, cwd=None):
            calls.append(list(argv))
            if argv[1] == 'venv':
                Path(argv[-1]).mkdir(parents=True)
                return 0
            return 1 if ('-r' in argv and '--no-deps' not in argv) else 0
        self.box.ctx._caller = caller
        det = python.CondaMigrate().detect(self.box.ctx)
        result, output = quiet(run_step, python.CondaMigrate(), self.box.ctx, [det.items[0].key], self.history)
        self.assertEqual(result.status, 'ok')
        self.assertIn('done via exact list without resolving dependencies', output)
        installs = [c for c in calls if c[1] == 'pip']
        self.assertEqual(['--no-deps' in c for c in installs], [False, True])
        requirements = self.box.ctx.backup_dir() / 'venvs/conda-audio_model.recreate.txt'
        text = requirements.read_text()
        self.assertIn('-e ' + str(self.project), text)
        self.assertIn('-e ' + str(self.box.home / 'Projects/lab/audio-utils'), text)
        self.assertNotIn('/Users/x/', text)
        self.assertTrue((self.project / '.venv').is_dir())

    def test_item_without_python_fails_instead_of_vanishing(self):
        self.box.query.answers[(MISE, 'ls', '--json', '--installed', 'python')] = '[]'
        det = python.CondaMigrate().detect(self.box.ctx)
        self.assertFalse(det.items[0].selected)
        result, output = quiet(run_step, python.CondaMigrate(), self.box.ctx, [det.items[0].key], self.history)
        self.assertEqual(result.status, 'failed')
        self.assertIn('mise', output)
        self.assertEqual(self.history.entries()[-1]['status'], 'failed')

    def test_conda_remove_waits_for_migration(self):
        self.box.mkdir('miniforge3/bin')
        self.box.make('miniforge3/envs/audio_model/bin/python')
        self.box.make('miniforge3/envs/audio_model/conda-meta/python-3.11.13-h_cpython.json', '{}')
        self.box.query.answers[(UV, 'tool', 'list')] = ''
        det = python.CondaRemove().detect(self.box.ctx)
        self.assertTrue(det.items)
        self.assertFalse(any(i.selected for i in det.items))
        self.assertTrue(any('audio_model' in n for n in det.notes))


class RunnerEmptyOpsTests(unittest.TestCase):
    def test_marked_items_without_commands_are_not_done(self):
        class Empty(Step):
            id = 'empty'
            title = 'Empty'

            def detect(self, ctx):
                return Detection(PENDING, [Item('a', 'a')])

            def ops(self, ctx, items):
                return []
        box = Sandbox(tools=TOOLS)
        try:
            history = History(box.home / 'h.jsonl')
            result, _ = quiet(run_step, Empty(), box.ctx, ['a'], history)
            self.assertEqual(result.status, 'cancelled')
            self.assertEqual(history.entries(), [])
        finally:
            box.close()


class LaunchdPythonTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)
        self.history = History(self.box.home / 'h.jsonl')
        self.root = mise_answer(self.box, '3.12.16')
        self.box.make('.local/share/mise/installs/python/3.12/bin/python3', '')
        self.new = str(self.root / '3.12/bin/python3')
        bridge = self.box.mkdir('Library/Application Support/App/bridge')
        (bridge / 'bridge.py').write_text('print(1)\n')
        (bridge / 'service.py').write_text("PYTHON = '/opt/homebrew/bin/python3.13'\n")
        self.plist = self.box.mkdir('Library/LaunchAgents') / 'dev.example.bridge.plist'
        self.plist.write_bytes(plistlib.dumps({
            'Label': 'dev.example.bridge', 'RunAtLoad': True, 'StartInterval': 30,
            'ProgramArguments': ['/opt/homebrew/bin/python3.13', str(bridge / 'bridge.py'), '--once']}))
        self.box.query.answers[(self.new,)] = ''

    def tearDown(self):
        self.box.close()

    def test_detects_and_repairs_agent(self):
        det = python.LaunchdPython().detect(self.box.ctx)
        self.assertEqual(len(det.items), 1)
        item = det.items[0]
        self.assertTrue(item.selected)
        self.assertEqual(item.data['new'], self.new)
        self.assertIn('service.py', item.note)
        result, _ = quiet(run_step, python.LaunchdPython(), self.box.ctx, [item.key], self.history)
        self.assertEqual(result.status, 'ok')
        data = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(data['ProgramArguments'][0], self.new)
        self.assertEqual(data['StartInterval'], 30)
        service = self.box.home / 'Library/Application Support/App/bridge/service.py'
        self.assertIn(self.new, service.read_text())
        saved = self.box.ctx.backup_dir() / 'launchd-before'
        self.assertTrue((saved / 'dev.example.bridge.plist').is_file())
        self.assertIn('/opt/homebrew/bin/python3.13', (saved / 'dev.example.bridge.service.py').read_text())
        self.assertEqual(python.LaunchdPython().detect(self.box.ctx).state, CLEAN)

    def test_script_that_does_not_compile_is_unchecked(self):
        self.box.query.answers[(self.new,)] = QueryResult(1, '', 'SyntaxError: invalid syntax')
        item = python.LaunchdPython().detect(self.box.ctx).items[0]
        self.assertFalse(item.selected)
        self.assertIn('SyntaxError', item.note)

    def test_unlink_waits_for_launchd_python(self):
        self.assertIn('launchd-python', python.BrewPythonUnlink.requires)


class ShellAndSimulatorTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)

    def tearDown(self):
        os.chmod(self.box.home / '.zshrc.save', 0o600) if (self.box.home / '.zshrc.save').exists() else None
        self.box.close()

    def test_unreadable_old_shell_file_is_copied_with_sudo(self):
        self.box.make('.zshrc.old', 'a')
        locked = self.box.make('.zshrc.save', 'b')
        os.chmod(locked, 0)
        if os.access(locked, os.R_OK):
            self.skipTest('running as root')
        step = shell.ShellOldFiles()
        det = step.detect(self.box.ctx)
        self.assertIn('sudo', [i for i in det.items if i.path == str(locked)][0].note)
        ops = step.ops(self.box.ctx, det.items)
        install = [o for o in ops if o.argv and o.argv[0] == '/usr/bin/install']
        self.assertEqual(len(install), 1)
        self.assertTrue(install[0].sudo)
        self.assertEqual(install[0].argv[-2], str(locked))

    def test_orphan_runtime_note(self):
        box = Sandbox(tools=dict(TOOLS, xcrun='/fake/xcrun'))
        try:
            box.query.answers[('/fake/xcrun', 'simctl', 'runtime', 'list', '-j')] = '{}'
            box.query.answers[('/fake/xcrun', 'simctl', 'list', 'devices', '-j')] = '{"devices": {}}'
            box.make(simulators.ORPHAN_ASSETS.lstrip('/') + '/abc.asset/AssetData/x.dmg', 'x' * 2048, base=box.root)
            det = simulators.Simulators().detect(box.ctx)
            self.assertEqual(det.state, CLEAN)
            self.assertTrue(any('SIP' in n for n in det.notes))
        finally:
            box.close()


def app_bundle(box, name, identifier):
    app = box.mkdir('Applications/%s/Contents' % name, base=box.root)
    (app / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': identifier}))


class LeftoverTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)

    def tearDown(self):
        self.box.close()

    def test_old_whatsapp_data_only_without_electron_app(self):
        self.box.make('Library/Containers/desktop.WhatsApp/data', 'x')
        app_bundle(self.box, 'WhatsApp.app', 'net.whatsapp.WhatsApp')
        keys = [i.key for i in leftovers.AppLeftovers().detect(self.box.ctx).items]
        self.assertIn('app:whatsapp-electron', keys)
        app_bundle(self.box, 'WhatsApp.app', 'desktop.WhatsApp')
        det = leftovers.AppLeftovers().detect(self.box.ctx)
        self.assertNotIn('app:whatsapp-electron', [i.key for i in det.items])
        self.assertTrue(any('old WhatsApp' in n for n in det.notes))

    def test_installed_app_or_tool_keeps_data(self):
        self.box.make('Library/Application Support/Adobe/x', base=self.box.root)
        self.box.make('.wine/drive_c/x')
        self.box.mkdir('Applications/Adobe Photoshop 2023', base=self.box.root)
        box_tools = dict(TOOLS, wine='/opt/homebrew/bin/wine')
        self.box.ctx._tools.update(box_tools)
        keys = [i.key for i in leftovers.AppLeftovers().detect(self.box.ctx).items]
        self.assertNotIn('app:adobe', keys)
        self.assertNotIn('app:wine', keys)

    def test_paths_are_removed(self):
        target = self.box.make('.fig/bin/fig', 'x')
        step = leftovers.AppLeftovers()
        det = step.detect(self.box.ctx)
        quiet(step.ops(self.box.ctx, det.items)[0].func, self.box.ctx)
        self.assertFalse(target.parent.parent.exists())

    def test_launchd_orphans(self):
        agents = self.box.mkdir('Library/LaunchAgents')
        alive = self.box.make('bin/tool', '')
        for name, program in (('dead', '/Applications/Gone.app/Contents/MacOS/gone'), ('alive', str(alive)),
                              ('com.apple.x', '/gone'), ('py', '/opt/homebrew/bin/python3.13')):
            (agents / (name + '.plist')).write_bytes(plistlib.dumps({'Label': name, 'ProgramArguments': [program]}))
        system = self.box.mkdir('Library/LaunchDaemons', base=self.box.root)
        os.symlink('/Library/Internet Plug-Ins/Gone.plugin/x.plist', system / 'com.oracle.java.Helper-Tool.plist')
        det = leftovers.LaunchdOrphans().detect(self.box.ctx)
        labels = sorted(i.data['label'] for i in det.items)
        self.assertEqual(labels, ['com.oracle.java.Helper-Tool', 'dead'])
        daemon = [i for i in det.items if i.data['label'] == 'com.oracle.java.Helper-Tool'][0]
        self.assertEqual(daemon.data['domain'], 'system')
        self.assertIn('broken link', daemon.note)


class FontAndChromeTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)
        fonts = self.box.mkdir('Library/Fonts')
        for name in ('MesloLGSNerdFont-Regular.ttf', 'MesloLGS NF Regular.ttf', 'FiraCodeNerdFontMono-Bold.ttf',
                     'HackNerdFont-Regular.ttf', 'HackNerdFontMono-Bold.ttf', 'MonaspiceArNerdFont-Regular.otf',
                     'Rubik-Regular.ttf'):
            (fonts / name).write_text('f')
        self.fonts = fonts
        self.box.make('Library/Application Support/Code/User/settings.json',
                      '{"editor.fontFamily": "\'MesloLGS NF\', Monaco, monospace"}')
        self.box.mkdir('opt/homebrew/Caskroom/font-monaspice-nerd-font', base=self.box.root)
        self.box.query.answers[('/usr/bin/defaults', 'read', 'com.googlecode.iterm2')] = (
            '"Normal Font" = "FiraCode-Retina 12";')

    def tearDown(self):
        self.box.close()

    def test_only_unused_nerd_families_go(self):
        step = apps.NerdFonts()
        det = step.detect(self.box.ctx)
        self.assertEqual([i.label for i in det.items], ['Hack (2 files)'])
        quiet(step.ops(self.box.ctx, det.items)[0].func, self.box.ctx)
        self.assertEqual(sorted(p.name for p in self.fonts.iterdir()),
                         ['FiraCodeNerdFontMono-Bold.ttf', 'MesloLGS NF Regular.ttf', 'MesloLGSNerdFont-Regular.ttf',
                          'MonaspiceArNerdFont-Regular.otf', 'Rubik-Regular.ttf'])

    def test_font_base(self):
        self.assertEqual(apps.font_base("'MesloLGS NF'"), 'meslolgs')
        self.assertEqual(apps.font_base('MesloLGS-NF-Regular 12'), 'meslolgs')
        self.assertEqual(apps.font_base('JetBrainsMono Nerd Font Mono'), 'jetbrainsmono')

    def _chrome(self):
        self.box.mkdir('Applications/Google Chrome.app', base=self.box.root)
        self.model = self.box.make('Library/Application Support/Google/Chrome/OptGuideOnDeviceModel/model.bin', 'm')
        step = apps.ChromeAiModel()
        return step, step.detect(self.box.ctx)

    def _approve(self):
        managed = self.box.mkdir('Library/Managed Preferences', base=self.box.root)
        (managed / 'com.google.Chrome.plist').write_bytes(plistlib.dumps({apps.POLICY: 1}))

    def test_chrome_policy_goes_through_a_profile_not_defaults_write(self):
        # 2026-11-01: sudo defaults write to /Library/Managed Preferences returns 0 and writes nothing
        # (cfprefsd discards it); only an approved configuration profile creates the file there.
        step, det = self._chrome()
        self.assertEqual([i.key.split(':')[0] for i in det.items], ['policy', 'path'])
        ops = step.ops(self.box.ctx, det.items)
        self.assertFalse([o for o in ops if o.argv and o.argv[0] in ('/usr/bin/defaults', '/bin/chmod')])
        self.assertFalse(any(o.sudo for o in ops))
        self.box.query.answers[('/usr/bin/pgrep', '-x', 'Google Chrome')] = '123\n'
        with self.assertRaises(RuntimeError):
            ops[0].func(self.box.ctx)
        quiet(ops[1].func, self.box.ctx)
        profile = self.box.ctx.backup_dir() / 'chrome' / apps.PROFILE_FILE
        data = plistlib.loads(profile.read_bytes())
        self.assertEqual((data['PayloadType'], data['PayloadScope'], data['PayloadIdentifier']),
                         ('Configuration', 'System', apps.PROFILE_ID))
        payload = data['PayloadContent'][0]
        self.assertEqual(payload['PayloadType'], 'com.apple.ManagedClient.preferences')
        forced = payload['PayloadContent']['com.google.Chrome']['Forced'][0]['mcx_preference_settings']
        self.assertEqual(forced, {apps.POLICY: 1})
        self.assertEqual(oct(os.stat(self.box.ctx.backup_dir()).st_mode & 0o777), '0o700')
        opened = [o for o in ops if o.argv and o.argv[0] == '/usr/bin/open']
        self.assertEqual(opened[0].argv[1], str(profile))
        labels = [o.label for o in ops]
        self.assertLess(labels.index(apps.WAIT_LABEL), labels.index('Delete the downloaded model'))

    def test_waits_for_approval_and_keeps_model_without_it(self):
        step, det = self._chrome()
        wait = [o for o in step.ops(self.box.ctx, det.items) if o.label == apps.WAIT_LABEL][0]
        with patch.object(apps, 'APPROVAL_TIMEOUT', 0):
            with self.assertRaises(RuntimeError):
                quiet(wait.func, self.box.ctx)
        self._approve()
        _, output = quiet(wait.func, self.box.ctx)
        self.assertIn('profile approved', output)

    def test_unapproved_profile_fails_the_step_and_keeps_the_model(self):
        step, det = self._chrome()
        history = History(self.box.home / 'h.jsonl')
        with patch.object(apps, 'APPROVAL_TIMEOUT', 0):
            result, _ = quiet(run_step, step, self.box.ctx, [i.key for i in det.items], history)
        self.assertEqual(result.status, 'failed')
        self.assertTrue(self.model.exists())

    def test_approved_policy_leaves_only_the_model(self):
        step, _ = self._chrome()
        self._approve()
        det = step.detect(self.box.ctx)
        self.assertEqual([i.key.split(':')[0] for i in det.items], ['path'])
        self.assertEqual([o.label for o in step.ops(self.box.ctx, det.items)],
                         ['Check that Chrome is closed', 'Delete the downloaded model'])


class DevtoolsTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)
        self.brew = self.box.root / 'opt/homebrew'

    def tearDown(self):
        self.box.close()

    def test_brew_extras_respect_shell_and_taps(self):
        for formula in ('qemu', 'mysql-client', 'mysql-client@8.4'):
            (self.brew / 'Cellar' / formula / '1').mkdir(parents=True)
        (self.brew / 'Library/Taps/asmvik/homebrew-formulae').mkdir(parents=True)
        (self.brew / 'Library/Taps/mutagen-io/homebrew-mutagen').mkdir(parents=True)
        self.box.query.answers[(BREW, 'list', '--formula', '--full-name')] = 'qemu\nmutagen-io/mutagen/mutagen\n'
        self.box.query.answers[(BREW, 'list', '--cask', '--full-name')] = ''
        self.box.make('.zshrc', 'export PATH="/opt/homebrew/opt/mysql-client@8.4/bin:$PATH"\n')
        det = devtools.BrewExtras().detect(self.box.ctx)
        by_key = {i.key: i for i in det.items}
        self.assertNotIn('brew:mysql-client', by_key)
        self.assertFalse(by_key['brew:mysql-client@8.4'].selected)
        self.assertTrue(by_key['brew:qemu'].selected)
        self.assertTrue(by_key['tap:asmvik/formulae'].selected)
        self.assertFalse(by_key['tap:mutagen-io/mutagen'].selected)
        self.box.make('.zshrc', 'export PATH="/opt/homebrew/opt/mysql-client/bin:$PATH"\n')
        self.assertTrue({i.key: i for i in devtools.BrewExtras().detect(self.box.ctx).items}['brew:mysql-client@8.4'].selected)

    def test_orphan_databases(self):
        (self.brew / 'var/mongodb').mkdir(parents=True)
        (self.brew / 'var/mongodb/collection.wt').write_text('d')
        (self.brew / 'var/postgresql@14').mkdir(parents=True)
        (self.brew / 'Cellar/postgresql@14/14.1').mkdir(parents=True)
        self.box.make('Library/PostgreSQL/9.6/data/base', 'd', base=self.box.root)
        self.box.query.answers[('/usr/bin/dscl', '.', '-read', '/Users/postgres')] = 'NFSHomeDirectory: /Library/PostgreSQL/9.6\n'
        step = devtools.OrphanDbData()
        det = step.detect(self.box.ctx)
        self.assertEqual([i.key for i in det.items], ['brewdb:mongodb', 'edb', 'postgres-user'])
        self.assertFalse(det.items[-1].selected)
        edb = det.items[1]
        self.assertEqual(edb.data['paths'], [str(self.box.root / 'Library/PostgreSQL')])
        ops = step.ops(self.box.ctx, [i for i in det.items if i.selected])
        labels = [o.label for o in ops]
        self.assertLess(max(i for i, l in enumerate(labels) if l.startswith('Archive')),
                        min(i for i, l in enumerate(labels) if l.startswith('Delete')))
        self.box.query.answers[('/usr/bin/pgrep', '-x', 'mongod')] = '42\n'
        self.assertNotIn('brewdb:mongodb', [i.key for i in step.detect(self.box.ctx).items])

    def test_android_keys_go_to_backup_first(self):
        self.box.make('.android/debug.keystore', 'k')
        self.box.make('.android/avd/x', 'a')
        step = devtools.AndroidTizen()
        det = step.detect(self.box.ctx)
        ops = step.ops(self.box.ctx, det.items)
        self.assertEqual(ops[0].label, 'Save the debug keys to the backup')
        quiet(ops[0].func, self.box.ctx)
        self.assertEqual((self.box.ctx.backup_dir() / 'android/debug.keystore').read_text(), 'k')

    def test_dev_caches(self):
        self.box.make('Library/Caches/node-gyp/22/include/x.h', 'h')
        extensions = self.box.mkdir('.vscode/extensions')
        (extensions / 'pub.ext-1.0.0').mkdir()
        (extensions / '.obsolete').write_text('{"pub.ext-1.0.0": true, "gone-1.0": true}')
        step = maintenance.DevCaches()
        det = step.detect(self.box.ctx)
        self.assertEqual(det.state, CLEAN)
        self.assertEqual([i.key for i in det.items], ['cache:node-gyp', 'ext:pub.ext-1.0.0'])
        with patch.object(self.box.ctx, 'du', return_value=80 * 1000 * 1000):
            self.assertEqual(step.detect(self.box.ctx).state, PENDING)


class CatalogTests(unittest.TestCase):
    def test_new_steps_are_registered_in_order(self):
        ids = [s.id for s in all_steps()]
        for step_id in ('launchd-python', 'app-leftovers', 'launchd-orphans', 'nerd-fonts', 'chrome-ai-model',
                        'brew-extras', 'android-tizen', 'platformio-qmk', 'nvim-mason', 'orphan-db-data',
                        'dev-caches'):
            self.assertIn(step_id, ids)
        self.assertLess(ids.index('launchd-python'), ids.index('brew-python-unlink'))


if __name__ == '__main__':
    unittest.main()
