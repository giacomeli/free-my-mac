"""General cleanup: Intel Homebrew, Go and Podman from pkg installers, pmk, Homebrew Rust and broken links."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Set

from ..core import CLEAN, PENDING, Context, Detection, Item, Op, Step, digest, plural
from ..paths import PathsStep, remove_paths_op

INTEL_DIRS = ('/usr/local/Homebrew', '/usr/local/Cellar', '/usr/local/Caskroom', '/usr/local/var/homebrew')
INTEL_LINK_DIRS = ('bin', 'sbin', 'lib', 'include', 'share', 'etc', 'opt', 'Frameworks', 'var')


def intel_links(ctx: Context) -> List[Path]:
    """Links in /usr/local whose target falls inside Intel Homebrew (Cellar, Caskroom or Homebrew)."""
    prefix = ctx.sys('/usr/local')
    owned = [str(prefix / name) for name in ('Cellar', 'Caskroom', 'Homebrew')]
    found: List[Path] = []
    for name in INTEL_LINK_DIRS:
        base = prefix / name
        if not base.is_dir() or base.is_symlink():
            continue
        for folder, dirs, files in os.walk(str(base)):
            dirs[:] = [d for d in dirs if d != 'node_modules' and not os.path.islink(os.path.join(folder, d))]
            for entry in files + [d for d in os.listdir(folder) if os.path.islink(os.path.join(folder, d))]:
                path = os.path.join(folder, entry)
                if not os.path.islink(path):
                    continue
                target = os.path.normpath(os.path.join(folder, os.readlink(path)))
                if any(target == o or target.startswith(o + '/') for o in owned):
                    found.append(Path(path))
    return sorted(set(found))


class IntelBrew(Step):
    id = 'intel-brew'
    group = 'general'
    title = 'Remove Intel Homebrew (/usr/local)'
    requires = ('venvs',)
    what = ('Removes the 2022 Intel Homebrew left in /usr/local: the links it created in bin, lib, share and '
            'so on, and the Homebrew, Cellar, Caskroom and var/homebrew folders (about 80 x86_64 formulae).')
    why = ('It is abandoned (brew does not even respond) and it is where composer, pyenv and the old Python '
           '3.9 and 3.10 come from today. The Homebrew in use is the one in /opt/homebrew.')
    consequences = ('Only links that point inside Intel Homebrew are deleted; aws-cli, heroku and others '
                    'installed by pkg in /usr/local stay. Intel formulae you still use (see Notes) must be '
                    'reinstalled with the current Homebrew.')
    undo = 'There is no direct way back; reinstall what you need with the Homebrew in /opt/homebrew.'

    def detect(self, ctx: Context) -> Detection:
        dirs = [ctx.sys(d) for d in INTEL_DIRS if ctx.sys(d).exists()]
        if not dirs:
            return Detection(CLEAN, summary='No Homebrew in /usr/local.')
        links = intel_links(ctx)
        items: List[Item] = []
        if links:
            items.append(Item('links:' + digest(links), plural(len(links), 'Intel Homebrew link', 'Intel Homebrew links') +
                              ' in /usr/local', data={'paths': [str(p) for p in links]}))
        for folder in dirs:
            items.append(Item('dir:' + str(folder), ctx.pretty(folder), path=str(folder), size=ctx.du(folder)))
        notes: List[str] = []
        cellar = ctx.sys('/usr/local/Cellar')
        if cellar.is_dir():
            current = set(ctx.brew_formulas())
            intel_only = sorted(p.name for p in cellar.iterdir() if p.is_dir() and p.name not in current)
            if intel_only:
                notes.append('Formulae only in Intel Homebrew: ' + ', '.join(intel_only))
        if (ctx.sys('/usr/local/etc')).is_dir():
            notes.append('/usr/local/etc is left for manual review (configs from old formulae).')
        return Detection(PENDING, items, notes=notes)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        ops: List[Op] = []
        for item in items:
            if item.key.startswith('links:'):
                links = [Path(p) for p in item.data['paths']]
                label = 'Delete ' + plural(len(links), 'Intel Homebrew link', 'Intel Homebrew links')
                if ctx.real_system:
                    argv = ['/bin/rm', '-f', '--'] + [str(p) for p in links]
                    ops.append(Op(label,
                                  'sudo rm -f -- <%s in /usr/local pointing to Cellar, Caskroom or Homebrew>'
                                  % plural(len(links), 'link', 'links'),
                                  argv=argv, sudo=True))
                else:
                    ops.extend(remove_paths_op(ctx, links, label))
        dirs = [Path(i.path) for i in items if i.key.startswith('dir:')]
        ops.extend(remove_paths_op(ctx, dirs, 'Delete the Intel Homebrew folders'))
        return ops


class GoPkg(PathsStep):
    id = 'go-pkg'
    group = 'general'
    title = 'Remove Go 1.21 (pkg installer)'
    targets = ('/usr/local/go', '/etc/paths.d/go')
    receipts = r'^org\.golang\.go$'
    what = 'Deletes Go 1.21.4, installed by pkg in 2023, its PATH entry and its receipt.'
    why = 'The version is no longer supported and no Go project was found.'
    consequences = 'The go command goes away.'
    undo = 'mise use -g go@latest, if you need it.'


class Podman(PathsStep):
    id = 'podman'
    group = 'general'
    title = 'Remove Podman'
    targets = ('/opt/podman', '/usr/local/podman', '/etc/paths.d/podman-pkg', '~/.local/share/containers',
               '~/.config/containers', '~/Library/LaunchAgents/io.podman_desktop.PodmanDesktop.plist',
               '~/Library/Application Support/Podman Desktop')
    receipts = r'^com\.redhat\.podman'
    what = 'Deletes Podman installed by pkg, its machines and images, and the Podman Desktop login item.'
    why = 'Containers now run only on a remote server.'
    consequences = 'The podman command and its local machines and images go away.'
    undo = 'Reinstall it from the Podman website.'


class Pmk(PathsStep):
    id = 'pmk'
    group = 'general'
    title = 'Remove the empty pmk'
    targets = ('/opt/pmk', '/etc/paths.d/10-pmk-global')
    what = 'Deletes /opt/pmk (an empty folder) and its PATH entry.'
    why = 'It only makes PATH longer.'
    consequences = 'None.'
    undo = 'Not applicable.'


def pinned_toolchains(ctx: Context, max_depth: int = 4) -> Set[str]:
    pins: Set[str] = set()
    root = ctx.projects
    if not root.is_dir():
        return pins
    stack = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            if entry.name in ('rust-toolchain', 'rust-toolchain.toml') and entry.is_file():
                text = Path(entry.path).read_text(errors='replace')
                match = re.search(r'channel\s*=\s*"([^"]+)"', text)
                pins.add(match.group(1) if match else text.strip().splitlines()[0] if text.strip() else '')
            elif entry.is_dir(follow_symlinks=False) and depth < max_depth and entry.name not in (
                    'node_modules', 'target', '.git', '.venv'):
                stack.append((Path(entry.path), depth + 1))
    return {p for p in pins if p}


class RustBrew(Step):
    id = 'rust-brew'
    group = 'general'
    title = 'Rust only through rustup'
    what = ('Uninstalls the Homebrew rust, which came before rustup in PATH, and removes rustup toolchains '
            'that are neither the default nor pinned by a project.')
    why = 'With the Homebrew rust first in PATH, the rust-toolchain.toml of each project was ignored.'
    consequences = 'cargo and rustc now come from ~/.cargo/bin (rustup).'
    undo = 'rustup toolchain install <version>.'

    def detect(self, ctx: Context) -> Detection:
        items: List[Item] = []
        if 'rust' in ctx.brew_formulas():
            items.append(Item('brew', 'Homebrew rust', size=ctx.du(ctx.brew_prefix() / 'Cellar/rust')))
        rustup = ctx.tool('rustup')
        if rustup:
            listing = ctx.query([rustup, 'toolchain', 'list'])
            pins = pinned_toolchains(ctx)
            for line in listing.out.splitlines():
                name = line.split()[0] if line.split() else ''
                if not name or '(default' in line or '(active' in line:
                    continue
                if any(name == p or name.startswith(p + '-') for p in pins):
                    continue
                items.append(Item('toolchain:' + name, 'Toolchain ' + name,
                                  size=ctx.du(ctx.user('~/.rustup/toolchains') / name)))
        return Detection(PENDING if items else CLEAN, items)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        ops: List[Op] = []
        if any(i.key == 'brew' for i in items):
            ops.append(Op('Uninstall the Homebrew rust', 'brew uninstall --formula rust',
                          argv=[ctx.tool('brew') or 'brew', 'uninstall', '--formula', 'rust']))
        names = [i.key.split(':', 1)[1] for i in items if i.key.startswith('toolchain:')]
        if names:
            label = ('Remove toolchain ' if len(names) == 1 else 'Remove toolchains ') + ', '.join(names)
            ops.append(Op(label, 'rustup toolchain uninstall ' + ' '.join(names),
                          argv=[ctx.tool('rustup') or 'rustup', 'toolchain', 'uninstall'] + names))
        return ops


LINK_FOLDERS = ('/usr/local/bin', '/usr/local/sbin', '~/.local/bin')


class BrokenLinks(Step):
    id = 'broken-links'
    group = 'general'
    title = 'Broken links in command folders'
    what = 'Deletes links in /usr/local/bin, /usr/local/sbin and ~/.local/bin whose target no longer exists.'
    why = 'They are left over from earlier removals and old installers; they only clutter shell completion.'
    consequences = 'None: the link already did not work.'
    undo = 'Not applicable.'

    def detect(self, ctx: Context) -> Detection:
        items: List[Item] = []
        for folder in LINK_FOLDERS:
            base = ctx.expand(folder)
            if not base.is_dir():
                continue
            for entry in sorted(os.scandir(base), key=lambda e: e.name):
                if entry.is_symlink() and not os.path.exists(entry.path):
                    target = os.readlink(entry.path)
                    transient = target.startswith(('/Applications/', '/Volumes/'))
                    items.append(Item('link:' + entry.path, '%s -> %s' % (ctx.pretty(entry.path), target),
                                      path=entry.path, selected=not transient,
                                      note='app being updated or disk unmounted? verify first' if transient else ''))
        return Detection(PENDING if items else CLEAN, items)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        return remove_paths_op(ctx, [Path(i.path) for i in items],
                               'Delete ' + plural(len(items), 'broken link', 'broken links'))


STEPS = [IntelBrew, GoPkg, Podman, Pmk, RustBrew, BrokenLinks]
