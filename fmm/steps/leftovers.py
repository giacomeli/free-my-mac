"""Leftovers of apps that are no longer installed, and launchd agents whose program is gone."""
from __future__ import annotations

import glob
import os
import plistlib
import re
import shlex
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from ..core import AVAILABLE, CLEAN, PENDING, Context, Detection, Item, Op, Step, plural
from ..launchd import SYSTEM_FOLDERS, USER_AGENTS, domain, read_agents, resolve
from ..paths import forget_receipts_op, remove_paths_op

APP_FOLDERS = ('/Applications', '~/Applications')


def installed_apps(ctx: Context, patterns: Sequence[str]) -> List[Path]:
    """Apps (or suite folders) in /Applications and ~/Applications that match the patterns."""
    found: List[Path] = []
    for folder in APP_FOLDERS:
        base = ctx.expand(folder)
        for pattern in patterns:
            found.extend(Path(p) for p in sorted(glob.glob(str(base / pattern))))
    return found


def bundle_id(app: Path) -> Optional[str]:
    try:
        with open(app / 'Contents/Info.plist', 'rb') as stream:
            return plistlib.load(stream).get('CFBundleIdentifier')
    except Exception:  # noqa: BLE001 - app without a readable Info.plist
        return None


def _apps_guard(*patterns: str) -> Callable[[Context], Optional[str]]:
    def guard(ctx: Context) -> Optional[str]:
        apps = installed_apps(ctx, patterns)
        return 'installed: ' + ', '.join(ctx.pretty(a) for a in apps[:2]) if apps else None
    return guard


def _tools_guard(*names: str) -> Callable[[Context], Optional[str]]:
    def guard(ctx: Context) -> Optional[str]:
        present = [n for n in names if ctx.tool(n)]
        return 'command present: ' + ', '.join(present) if present else None
    return guard


def _all(*guards: Callable[[Context], Optional[str]]) -> Callable[[Context], Optional[str]]:
    def guard(ctx: Context) -> Optional[str]:
        for check in guards:
            reason = check(ctx)
            if reason:
                return reason
        return None
    return guard


def _whatsapp_guard(ctx: Context) -> Optional[str]:
    """Only the old Electron app (desktop.WhatsApp) uses these folders; the native one uses net.whatsapp.WhatsApp."""
    old = [a for a in installed_apps(ctx, ('WhatsApp*.app',)) if bundle_id(a) == 'desktop.WhatsApp']
    return 'the old WhatsApp is still installed: ' + ctx.pretty(old[0]) if old else None


def _java_guard(ctx: Context) -> Optional[str]:
    plugin = ctx.sys('/Library/Internet Plug-Ins/JavaAppletPlugin.plugin')
    return 'the Java plugin is still installed' if plugin.exists() else None


# (id, name, paths, guard that returns the reason to leave it alone)
LEFTOVERS: Sequence[Tuple[str, str, Sequence[str], Callable[[Context], Optional[str]]]] = (
    ('whatsapp-electron', 'Old WhatsApp (Electron)',
     ('~/Library/Containers/desktop.WhatsApp', '~/Library/Application Support/WhatsApp'), _whatsapp_guard),
    ('adobe', 'Adobe (Photoshop 2023 and Creative Cloud)',
     ('/Library/Application Support/Adobe', '~/Library/Application Support/Adobe'), _apps_guard('Adobe*')),
    ('logitech', 'Logitech Options',
     ('/Library/Application Support/Logitech.localized',), _apps_guard('Logi*', 'Logitech*')),
    ('riot', 'Riot Games', ('/Users/Shared/Riot Games',),
     _apps_guard('Riot*', 'League of Legends*', 'VALORANT*')),
    ('java-plugin', 'Oracle Java plugin',
     ('~/Library/Caches/com.oracle.java.JavaAppletPlugin', '~/Library/Application Support/Oracle/Java',
      '/Library/Application Support/Oracle/Java'), _java_guard),
    ('fig', 'Fig', ('~/.fig', '~/.fig.dotfiles.bak'), _all(_tools_guard('fig'), _apps_guard('Fig.app'))),
    ('wine', 'Wine', ('~/.wine',),
     _all(_tools_guard('wine', 'wine64'), _apps_guard('Wine*', 'Whisky*', 'CrossOver*'))),
    ('dart', 'Dart and Flutter', ('~/.pub-cache', '~/.dart', '~/.dartServer', '~/.dart-tool', '~/.flutter',
                                  '~/.flutter-devtools'), _tools_guard('dart', 'flutter')),
    ('axway', 'Axway and Appcelerator Titanium', ('~/.axway', '~/.titanium', '~/.appcelerator'),
     _tools_guard('ti', 'appc', 'axway')),
    ('openjfx', 'OpenJFX cache', ('~/.openjfx',), lambda ctx: None),
)


class AppLeftovers(Step):
    id = 'app-leftovers'
    group = 'leftovers'
    title = 'Data left by removed apps'
    what = ('Deletes data and caches of apps that are no longer installed: the old WhatsApp (Electron), Adobe, '
            'Logitech Options, Riot Games, the Oracle Java plugin, Fig, Wine, Dart and Flutter, Axway/Titanium '
            'and the OpenJFX cache. Each app only shows up if nothing of it is still installed.')
    why = 'The apps are gone, but their data folders were left behind.'
    consequences = ('The current (native) WhatsApp uses other folders and is not affected; the chats of the old '
                    'app are on your phone. Reinstalling any of these apps starts from scratch.')
    undo = 'The tool cannot undo this; reinstall the app if you need it.'

    def detect(self, ctx: Context) -> Detection:
        items: List[Item] = []
        notes: List[str] = []
        for key, name, specs, guard in LEFTOVERS:
            paths = [ctx.expand(s) for s in specs if os.path.lexists(ctx.expand(s))]
            if not paths:
                continue
            reason = guard(ctx)
            if reason:
                notes.append('%s is kept (%s).' % (name, reason))
                continue
            sizes = [ctx.du(p) or 0 for p in paths]
            items.append(Item('app:%s' % key, name, size=sum(sizes) or None,
                              note=', '.join(ctx.pretty(p) for p in paths), data={'paths': [str(p) for p in paths]}))
        return Detection(PENDING if items else CLEAN, items, notes=notes)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        paths = [Path(p) for i in items for p in i.data['paths']]
        return remove_paths_op(ctx, paths)


class LaunchdOrphans(Step):
    id = 'launchd-orphans'
    group = 'leftovers'
    title = 'Orphaned launchd agents'
    what = ('Removes launchd agents and daemons (~/Library/LaunchAgents, /Library/LaunchAgents, '
            '/Library/LaunchDaemons) whose program no longer exists, and plists that are broken links. '
            "Apple's are kept, and the ones that run Python are handled by the \"Fix launchd agents with a "
            'removed Python" step.')
    why = 'launchd tries to start each of them at login or from time to time, and logs the failure.'
    consequences = 'Each agent is unloaded (launchctl bootout) before its plist is deleted.'
    undo = 'Reinstalling the app recreates its agent.'

    def detect(self, ctx: Context) -> Detection:
        items: List[Item] = []
        for agent in read_agents(ctx, [USER_AGENTS] + list(SYSTEM_FOLDERS)):
            if agent.plist.name.startswith('com.apple.') or agent.label.startswith('com.apple.'):
                continue
            if agent.broken_link:
                reason = 'broken link to ' + os.readlink(agent.plist)
            elif agent.program and os.path.isabs(agent.program) and not agent.is_python():
                if os.path.exists(resolve(ctx, agent.program)):
                    continue
                reason = 'missing program: ' + ctx.pretty(agent.program)
            else:
                continue
            items.append(Item('agent:' + str(agent.plist), '%s (%s)' % (agent.label, ctx.pretty(agent.plist.parent)),
                              path=str(agent.plist), note=reason,
                              data={'label': agent.label, 'domain': domain(agent)}))
        return Detection(PENDING if items else CLEAN, items)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        ops: List[Op] = []
        if ctx.real_system:
            for item in items:
                target = '%s/%s' % (item.data['domain'], item.data['label'])
                argv = ['/bin/launchctl', 'bootout', target]
                system = item.data['domain'] == 'system'
                ops.append(Op('Unload ' + item.data['label'], ('sudo ' if system else '') + shlex.join(argv),
                              argv=argv, sudo=system, tolerate_failure=True))
        return ops + remove_paths_op(ctx, [Path(i.path) for i in items])


# Apps installed by pkg that are gone but left folders, agents, PATH entries and receipts behind.
# (id, name, apps that show it is still installed, paths, receipts, launchd agents)
PKG_REMNANTS: Sequence[Tuple[str, str, Tuple[str, ...], Tuple[str, ...], str, Tuple[str, ...]]] = (
    ('xquartz', 'XQuartz', ('Utilities/XQuartz.app', 'XQuartz.app'),
     ('/opt/X11', '/Library/LaunchAgents/org.xquartz.startx.plist',
      '/Library/LaunchDaemons/org.xquartz.privileged_startx.plist', '/etc/paths.d/40-XQuartz',
      '/etc/manpaths.d/40-XQuartz'),
     r'^org\.xquartz\.', ('org.xquartz.startx', 'org.xquartz.privileged_startx')),
    ('wireshark', 'Wireshark', ('Wireshark.app',),
     ('/etc/paths.d/Wireshark', '/Library/LaunchDaemons/org.wireshark.ChmodBPF.plist',
      '/Library/Application Support/Wireshark'),
     r'^org\.wireshark\.', ('org.wireshark.ChmodBPF',)),
)
X11_ROOT = '/opt/X11'


def brew_binaries_using(ctx: Context, prefix: str) -> List[str]:
    """Executables in <brew>/bin that link libraries under the prefix (one otool call for all of them)."""
    folder = ctx.brew_prefix() / 'bin'
    files = sorted(str(p) for p in folder.iterdir()) if folder.is_dir() else []
    if not files:
        return []
    result = ctx.query(['/usr/bin/otool', '-L'] + files, timeout=180)
    users, current = [], None
    for line in result.out.splitlines():
        if not line.startswith(('\t', ' ')) and line.endswith(':'):
            current = line[:-1]
        elif current and prefix + '/' in line and current not in users:
            users.append(current)
    return users


class PkgLeftovers(Step):
    id = 'pkg-leftovers'
    group = 'leftovers'
    title = 'Leftovers of pkg-installed apps'
    what = ('Removes what XQuartz and Wireshark left behind after the apps were removed: /opt/X11, launchd '
            'agents, entries in /etc/paths.d and /etc/manpaths.d, and the installer receipts. The access_bpf '
            "group, created by Wireshark's ChmodBPF, comes unchecked.")
    why = 'The XQuartz agent still runs at every login, and /opt/X11 takes up hundreds of MB without the app.'
    consequences = ('Apps that need X11 already stopped working without XQuartz. A Homebrew binary that links '
                    'against /opt/X11 leaves the XQuartz item unchecked.')
    undo = 'Reinstall the app (brew install --cask xquartz, brew install --cask wireshark).'
    heavy = True

    def detect(self, ctx: Context) -> Detection:
        items: List[Item] = []
        notes: List[str] = []
        receipts = ctx.pkgs()
        for key, name, apps, paths, receipt_pattern, labels in PKG_REMNANTS:
            if installed_apps(ctx, apps):
                continue
            found = [ctx.sys(p) for p in paths if os.path.lexists(ctx.sys(p))]
            ids = [r for r in receipts if re.search(receipt_pattern, r)]
            if not found and not ids:
                continue
            agents = [(label, 'system' if '/LaunchDaemons/' in str(p) else 'gui')
                      for label in labels for p in found if p.name == label + '.plist']
            parts = [ctx.pretty(p) for p in found] + ([plural(len(ids), 'receipt', 'receipts')] if ids else [])
            item = Item('pkg:' + key, '%s (app already removed)' % name,
                        size=sum(ctx.du(p) or 0 for p in found) or None,
                        note=', '.join(parts),
                        data={'paths': [str(p) for p in found], 'receipts': ids, 'agents': agents})
            if key == 'xquartz' and any(p.name == 'X11' for p in found):
                users = brew_binaries_using(ctx, str(ctx.sys(X11_ROOT)))
                if users:
                    item.selected = False
                    item.note = 'in use by: %s; %s' % (', '.join(Path(u).name for u in users[:4]), item.note)
            items.append(item)
        if not installed_apps(ctx, ('Wireshark.app',)):
            group = ctx.query(['/usr/bin/dscl', '.', '-read', '/Groups/access_bpf', 'PrimaryGroupID'])
            if group.ok:
                items.append(Item('group:access_bpf', 'macOS access_bpf group (created by Wireshark)',
                                  selected=False, note='grants access to the capture interfaces; '
                                                       'useless without Wireshark'))
        return Detection(PENDING if any(i.selected for i in items) else (AVAILABLE if items else CLEAN), items,
                         notes=notes)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        ops: List[Op] = []
        uid = os.getuid()
        for item in items:
            for label, kind in item.data.get('agents', []):
                if not ctx.real_system:
                    continue
                target = 'system/' + label if kind == 'system' else 'gui/%d/%s' % (uid, label)
                argv = ['/bin/launchctl', 'bootout', target]
                ops.append(Op('Unload ' + label, ('sudo ' if kind == 'system' else '') + shlex.join(argv),
                              argv=argv, sudo=kind == 'system', tolerate_failure=True))
        paths = [Path(p) for i in items for p in i.data.get('paths', [])]
        ops.extend(remove_paths_op(ctx, paths, 'Delete the leftovers'))
        receipts = [r for i in items for r in i.data.get('receipts', [])]
        if receipts:
            ops.append(forget_receipts_op(receipts))
        if any(i.key == 'group:access_bpf' for i in items):
            argv = ['/usr/sbin/dseditgroup', '-o', 'delete', 'access_bpf']
            ops.append(Op('Delete the access_bpf group', 'sudo dseditgroup -o delete access_bpf', argv=argv, sudo=True))
        return ops


STEPS = [AppLeftovers, LaunchdOrphans, PkgLeftovers]
