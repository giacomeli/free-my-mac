"""Tweaks mode (2026-11-01): catalog, RemoveMacAI steps, own tweaks and pkg leftovers."""
import io
import json
import unittest
from contextlib import redirect_stdout

from helpers import Sandbox

from fmm.core import AVAILABLE, CLEAN, ERROR, PENDING, QueryResult, resolve_states
from fmm.history import History
from fmm.localconfig import conda_projects, own_agent_prefixes, uv_tools
from fmm.runner import run_step
from fmm.steps import CATALOG_TITLES, all_steps, leftovers, rmai, tweaks
from fmm.text import pending_text, print_list

RMAI = '/fake/removemacai'
TOOLS = {'mise': None, 'uv': None, 'brew': None, 'git': None, 'xcrun': None, 'zsh': '/bin/zsh', 'pkgutil': None,
         'security': None, 'rustup': None, 'docker': None, 'removemacai': RMAI, 'fig': None, 'wine': None,
         'wine64': None, 'dart': None, 'flutter': None, 'ti': None, 'appc': None, 'axway': None}

TWEAKS_JSON = [
    {'group': 'privacy', 'id': 'analytics', 'presets': ['privacy', 'recommended'], 'profile': True,
     'state': 'notApplied', 'title': 'Turn off Mac Analytics'},
    {'group': 'privacy', 'id': 'game-center', 'presets': ['privacy'], 'profile': True, 'state': 'notApplied',
     'title': 'Turn off Game Center'},
    {'group': 'privacy', 'id': 'improve-search', 'presets': ['recommended'], 'profile': True,
     'state': 'pendingApproval', 'title': 'Turn off Improve Search'},
    {'group': 'finder', 'id': 'file-extensions', 'presets': ['recommended'], 'profile': False, 'state': 'applied',
     'title': 'Show file extensions'},
    {'group': 'finder', 'id': 'path-bar', 'presets': ['recommended'], 'profile': False, 'state': 'notApplied',
     'title': 'Show the path bar'},
    {'group': 'finder', 'id': 'hidden-files', 'presets': [], 'profile': False, 'state': 'notApplied',
     'title': 'Show hidden files'},
    {'group': 'typing', 'id': 'key-repeat', 'presets': [], 'profile': False, 'state': 'notApplied',
     'title': 'Repeat held keys instead of showing accents'},
    {'group': 'apps', 'id': 'brand-new', 'presets': [], 'profile': False, 'state': 'notApplied',
     'title': 'Something new'},
    {'group': 'windows', 'id': 'dock-delay', 'presets': [], 'profile': False, 'state': 'applied',
     'title': 'Remove the Dock auto-hide delay'},
]

# Output format of `removemacai background` (1.1.4), with example labels.
BACKGROUND_TEXT = """RemoveMacAI 1.1.4  \u00b7  macOS 26.2

Background items from other apps
  com.example.hotkeys                             hotkeys               on   helper
  dev.example.mytool                              mytool                on   helper
  com.microsoft.update.agent                      Microsoft AutoUpdate  on   updater
  com.topazofd.warsaw.user                        core                  on   helper
  org.xquartz.startx                              launchd_startx        on   helper
  com.google.GoogleUpdater.wake.system            GoogleUpdater         on   updater  (all users)
  net.tunnelblick.tunnelblick.tunnelblickd        Tunnelblick           off  helper  (all users)

Turning off a helper can stop its app working, for example a VPN or Docker.
Turn one off with: removemacai background off <label>
"""

# Output format of `pmset -g custom` (excerpt).
PMSET_TEXT = """Battery Power:
 Sleep On Power Button 1
 standby              1
 powernap             1
 displaysleep         5
AC Power:
 Sleep On Power Button 1
 powernap             0
 sleep                0
"""


def quiet(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()) as out:
        result = fn(*args, **kwargs)
    return result, out.getvalue()


class CatalogTests(unittest.TestCase):
    def test_catalogs_are_separate_and_ids_unique(self):
        cleanup = {s.id for s in all_steps('cleanup')}
        tweak = {s.id for s in all_steps('tweaks')}
        self.assertFalse(cleanup & tweak)
        self.assertIn('pkg-leftovers', cleanup)
        for step_id in ('rmai-privacy', 'rmai-annoyances', 'rmai-apps', 'rmai-finder', 'rmai-windows',
                        'rmai-typing', 'background-items', 'ui-speed', 'firewall-stealth', 'battery-powernap'):
            self.assertIn(step_id, tweak)
        with self.assertRaises(ValueError):
            all_steps('other')

    def test_header_and_pending_text(self):
        box = Sandbox(tools=TOOLS)
        try:
            box.query.answers[('/usr/bin/pmset', '-g', 'custom')] = PMSET_TEXT
            out = io.StringIO()
            print_list(box.ctx, [tweaks.BatteryPowerNap()], History(box.home / 'h.jsonl'), out=out,
                       title=CATALOG_TITLES['tweaks'])
            self.assertTrue(out.getvalue().startswith('FREE MY MAC | TWEAKS  '))
        finally:
            box.close()
        self.assertEqual(pending_text(0, 2), 'Pending: 2 steps')
        self.assertTrue(pending_text(10 ** 9, 1).startswith('Pending: ~'))


class RemoveMacAiTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)
        self.box.query.answers[(RMAI, 'tweaks', '--json')] = json.dumps(TWEAKS_JSON)
        self.history = History(self.box.home / 'h.jsonl')

    def tearDown(self):
        self.box.close()

    def test_privacy_items_selection_and_profile_note(self):
        det = rmai.RmaiPrivacy().detect(self.box.ctx)
        self.assertEqual(det.state, PENDING)
        self.assertEqual([(i.key, i.selected) for i in det.items],
                         [('tweak:analytics', True), ('tweak:game-center', False)])
        self.assertEqual(det.items[0].label, 'Turn off Mac Analytics')
        self.assertTrue(any('profile' in n for n in det.notes))
        self.assertTrue(any('improve-search (pendingApproval)' in n for n in det.notes))
        self.assertEqual(det.items[0].note, '')

    def test_applied_tweaks_are_left_out(self):
        det = rmai.RmaiFinder().detect(self.box.ctx)
        self.assertEqual([(i.key, i.selected) for i in det.items],
                         [('tweak:path-bar', True), ('tweak:hidden-files', False)])
        self.assertIn('Already applied: 1 of 3.', det.notes)

    def test_optional_only_is_available_and_notes_explain(self):
        det = rmai.RmaiTyping().detect(self.box.ctx)
        self.assertEqual(det.state, AVAILABLE)
        self.assertIn('accents', det.items[0].note)
        apps = rmai.RmaiApps().detect(self.box.ctx)
        self.assertEqual(apps.items[0].label, 'Something new')

    def test_all_applied_reads_as_applied(self):
        step = rmai.RmaiWindows()
        det = step.detect(self.box.ctx)
        self.assertEqual(det.state, CLEAN)
        views = resolve_states([step], {step.id: det}, self.history)
        self.assertEqual(views[step.id].label, 'Already applied')

    def test_apply_delegates_to_removemacai(self):
        step = rmai.RmaiPrivacy()
        det = step.detect(self.box.ctx)
        result, _ = quiet(run_step, step, self.box.ctx, [i.key for i in det.items], self.history)
        self.assertEqual(result.status, 'ok')
        self.assertIn(((RMAI, 'apply', 'analytics', 'game-center', '--yes'), None), self.box.caller.calls)

    def test_missing_tool_points_to_cleanup_step(self):
        box = Sandbox(tools=dict(TOOLS, removemacai=None))
        try:
            det = rmai.RmaiPrivacy().detect(box.ctx)
            self.assertEqual(det.state, ERROR)
            self.assertIn('apple-intelligence', det.summary)
            self.assertEqual(rmai.BackgroundItems().detect(box.ctx).state, ERROR)
        finally:
            box.close()

    def test_tool_failure_is_an_error(self):
        self.box.query.answers[(RMAI, 'tweaks', '--json')] = QueryResult(1, '', 'boom')
        det = rmai.RmaiFinder().detect(self.box.ctx)
        self.assertEqual(det.state, ERROR)
        self.assertIn('boom', det.summary)


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)
        self.box.query.answers[(RMAI, 'background')] = BACKGROUND_TEXT
        self.box.make('.config/free-my-mac/local.json', json.dumps({'own_agent_prefixes': ['dev.example.']}))

    def tearDown(self):
        self.box.close()

    def test_parse_real_output(self):
        rows = rmai.background_items(BACKGROUND_TEXT)
        self.assertEqual(len(rows), 7)
        by_label = {r['label']: r for r in rows}
        self.assertEqual(by_label['com.microsoft.update.agent']['name'], 'Microsoft AutoUpdate')
        self.assertTrue(by_label['com.google.GoogleUpdater.wake.system']['all_users'])
        self.assertEqual(by_label['net.tunnelblick.tunnelblick.tunnelblickd']['state'], 'off')

    def test_own_bank_and_xquartz_are_skipped_and_nothing_selected(self):
        det = rmai.BackgroundItems().detect(self.box.ctx)
        self.assertEqual(det.state, AVAILABLE)
        labels = [i.data['label'] for i in det.items]
        self.assertEqual(labels, ['com.example.hotkeys', 'com.microsoft.update.agent',
                                  'com.google.GoogleUpdater.wake.system'])
        self.assertFalse(any(i.selected for i in det.items))
        microsoft = det.items[1]
        self.assertIn('no Microsoft apps installed', microsoft.note)
        self.box.mkdir('Applications/Microsoft Word.app', base=self.box.root)
        microsoft = rmai.BackgroundItems().detect(self.box.ctx).items[1]
        self.assertIn('Word, Excel and Teams', microsoft.note)

    def test_off_per_label(self):
        step = rmai.BackgroundItems()
        det = step.detect(self.box.ctx)
        ops = step.ops(self.box.ctx, det.items[:1])
        self.assertEqual(ops[0].argv, [RMAI, 'background', 'off', 'com.example.hotkeys'])


def defaults_answers(box, values):
    for (domain, key), value in values.items():
        box.query.answers[('/usr/bin/defaults', 'read', domain, key)] = value


class UiSpeedTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)

    def tearDown(self):
        self.box.close()

    def test_detects_current_values(self):
        defaults_answers(self.box, {('com.apple.dock', 'autohide-time-modifier'): '0.15\n',
                                    ('com.apple.dock', 'expose-animation-duration'): '0.5\n',
                                    ('com.apple.dock', 'mru-spaces'): '1\n',
                                    ('NSGlobalDomain', 'QLPanelAnimationDuration'): '0\n'})
        det = tweaks.UiSpeed().detect(self.box.ctx)
        self.assertEqual(det.state, AVAILABLE)
        self.assertEqual([i.data['key'] for i in det.items], ['mission-control', 'fixed-spaces', 'window-resize'])
        self.assertFalse(any(i.selected for i in det.items))
        self.assertIn('current: 0.5', det.items[0].note)
        self.assertIn('current: macOS default', det.items[2].note)

    def test_ops_and_dock_restart(self):
        step = tweaks.UiSpeed()
        det = step.detect(self.box.ctx)
        by_key = {i.data['key']: i for i in det.items}
        ops = step.ops(self.box.ctx, [by_key['fixed-spaces']])
        self.assertEqual(ops[0].argv, ['/usr/bin/defaults', 'write', 'com.apple.dock', 'mru-spaces', '-bool', 'false'])
        self.assertEqual(ops[-1].argv, ['/usr/bin/killall', 'Dock'])
        self.assertTrue(ops[-1].tolerate_failure)
        ops = step.ops(self.box.ctx, [by_key['window-resize']])
        self.assertEqual([o.argv for o in ops],
                         [['/usr/bin/defaults', 'write', 'NSGlobalDomain', 'NSWindowResizeTime', '-float', '0.001']])

    def test_all_applied_is_clean(self):
        defaults_answers(self.box, {(t.domain, t.name): ('0' if t.kind == 'bool' else '%g' % t.value)
                                    for t in tweaks.UI_TWEAKS})
        self.assertEqual(tweaks.UiSpeed().detect(self.box.ctx).state, CLEAN)


class FirewallAndPowerTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)
        self.fw = tweaks.SOCKETFILTERFW

    def tearDown(self):
        self.box.close()

    def test_stealth(self):
        self.box.query.answers[(self.fw, '--getstealthmode')] = 'Firewall stealth mode is off\n'
        self.box.query.answers[(self.fw, '--getglobalstate')] = 'Firewall is enabled. (State = 1)\n'
        step = tweaks.FirewallStealth()
        det = step.detect(self.box.ctx)
        self.assertEqual(det.state, PENDING)
        self.assertTrue(det.items[0].selected)
        self.assertEqual(det.notes, [])
        op = step.ops(self.box.ctx, det.items)[0]
        self.assertTrue(op.sudo)
        self.assertEqual(op.argv, [self.fw, '--setstealthmode', 'on'])
        self.box.query.answers[(self.fw, '--getglobalstate')] = 'Firewall is disabled. (State = 0)\n'
        self.assertTrue(step.detect(self.box.ctx).notes)
        self.box.query.answers[(self.fw, '--getstealthmode')] = 'Firewall stealth mode is on\n'
        self.assertEqual(step.detect(self.box.ctx).state, CLEAN)

    def test_battery_powernap(self):
        self.assertEqual(tweaks.battery_setting(PMSET_TEXT, 'powernap'), '1')
        self.assertEqual(tweaks.battery_setting(PMSET_TEXT, 'displaysleep'), '5')
        self.assertIsNone(tweaks.battery_setting('AC Power:\n powernap 1\n', 'powernap'))
        self.box.query.answers[('/usr/bin/pmset', '-g', 'custom')] = PMSET_TEXT
        step = tweaks.BatteryPowerNap()
        det = step.detect(self.box.ctx)
        self.assertEqual(det.state, AVAILABLE)
        self.assertFalse(det.items[0].selected)
        self.assertEqual(step.ops(self.box.ctx, det.items)[0].argv, ['/usr/bin/pmset', '-b', 'powernap', '0'])
        self.box.query.answers[('/usr/bin/pmset', '-g', 'custom')] = PMSET_TEXT.replace('powernap             1',
                                                                                        'powernap             0')
        self.assertEqual(step.detect(self.box.ctx).state, CLEAN)


class PkgLeftoversTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools=TOOLS)
        root = self.box.root
        self.x11 = self.box.make('opt/X11/lib/libX11.6.dylib', 'x', base=root).parent.parent
        self.box.make('Library/LaunchAgents/org.xquartz.startx.plist', 'p', base=root)
        self.box.make('etc/paths.d/40-XQuartz', '/opt/X11/bin\n', base=root)
        self.box.make('etc/paths.d/Wireshark', '/Applications/Wireshark.app/Contents/MacOS\n', base=root)
        self.tool = self.box.make('opt/homebrew/bin/foo', 'bin', base=root)
        self.box.query.answers[('/usr/sbin/pkgutil', '--pkgs')] = (
            'com.apple.pkg.X\norg.xquartz.X11\norg.wireshark.ChmodBPF.pkg\norg.wireshark.path_helper.pkg\n')
        self.box.query.answers[('/usr/bin/dscl', '.', '-read', '/Groups/access_bpf')] = 'PrimaryGroupID: 101\n'
        self.otool('/usr/lib/libSystem.B.dylib')

    def otool(self, library):
        self.box.query.answers[('/usr/bin/otool', '-L')] = '%s:\n\t%s (compatibility version 1.0.0)\n' % (
            self.tool, library)

    def tearDown(self):
        self.box.close()

    def test_detects_remnants(self):
        det = leftovers.PkgLeftovers().detect(self.box.ctx)
        self.assertEqual(det.state, PENDING)
        self.assertEqual([(i.key, i.selected) for i in det.items],
                         [('pkg:xquartz', True), ('pkg:wireshark', True), ('group:access_bpf', False)])
        xquartz = det.items[0]
        self.assertEqual(xquartz.data['receipts'], ['org.xquartz.X11'])
        self.assertEqual(xquartz.data['agents'], [('org.xquartz.startx', 'gui')])
        self.assertEqual(det.items[1].data['receipts'], ['org.wireshark.ChmodBPF.pkg', 'org.wireshark.path_helper.pkg'])

    def test_installed_app_or_x11_user_keeps_it(self):
        self.otool(str(self.x11 / 'lib/libX11.6.dylib'))
        item = leftovers.PkgLeftovers().detect(self.box.ctx).items[0]
        self.assertFalse(item.selected)
        self.assertIn('in use by: foo', item.note)
        self.box.mkdir('Applications/Utilities/XQuartz.app', base=self.box.root)
        keys = [i.key for i in leftovers.PkgLeftovers().detect(self.box.ctx).items]
        self.assertNotIn('pkg:xquartz', keys)

    def test_run_removes_then_forgets(self):
        step = leftovers.PkgLeftovers()
        det = step.detect(self.box.ctx)
        selected = [i.key for i in det.items if i.selected]
        labels = [o.label for o in step.ops(self.box.ctx, [i for i in det.items if i.selected])]
        self.assertEqual(labels, ['Delete the leftovers', 'Forget 3 installer receipts'])
        result, _ = quiet(run_step, step, self.box.ctx, selected, History(self.box.home / 'h.jsonl'))
        self.assertEqual(result.status, 'ok')
        self.assertFalse(self.x11.exists())
        self.assertFalse((self.box.root / 'etc/paths.d/Wireshark').exists())
        forgotten = [c[0][-1] for c in self.box.caller.calls if c[0][1:3] == ('/usr/sbin/pkgutil', '--forget')]
        self.assertEqual(forgotten, ['org.xquartz.X11', 'org.wireshark.ChmodBPF.pkg', 'org.wireshark.path_helper.pkg'])

    def test_group_removal_is_explicit(self):
        step = leftovers.PkgLeftovers()
        det = step.detect(self.box.ctx)
        ops = step.ops(self.box.ctx, [i for i in det.items if i.key == 'group:access_bpf'])
        self.assertEqual([o.argv for o in ops], [['/usr/sbin/dseditgroup', '-o', 'delete', 'access_bpf']])
        self.assertTrue(ops[0].sudo)



class LocalConfigTests(unittest.TestCase):
    """Personal data (projects, tools, agents) comes from ~/.config/free-my-mac/local.json, not from the code."""

    def setUp(self):
        self.box = Sandbox(tools=TOOLS)

    def tearDown(self):
        self.box.close()

    def test_missing_invalid_or_wrong_types_mean_empty(self):
        self.assertEqual((conda_projects(self.box.ctx), uv_tools(self.box.ctx), own_agent_prefixes(self.box.ctx)),
                         ({}, {}, ()))
        self.box.make('.config/free-my-mac/local.json', '{not json')
        self.assertEqual(conda_projects(self.box.ctx), {})
        self.box.make('.config/free-my-mac/local.json', json.dumps({
            'conda_projects': ['x'], 'uv_tools': {'a': 'src', 'b': 3}, 'own_agent_prefixes': ['dev.me.', 7]}))
        self.assertEqual(conda_projects(self.box.ctx), {})
        self.assertEqual(uv_tools(self.box.ctx), {'a': 'src'})
        self.assertEqual(own_agent_prefixes(self.box.ctx), ('dev.me.',))

    def test_own_agents_show_unchecked_without_config(self):
        self.box.query.answers[(RMAI, 'background')] = BACKGROUND_TEXT
        det = rmai.BackgroundItems().detect(self.box.ctx)
        mine = [i for i in det.items if i.data['label'] == 'dev.example.mytool']
        self.assertEqual(len(mine), 1)
        self.assertFalse(mine[0].selected)


if __name__ == '__main__':
    unittest.main()
