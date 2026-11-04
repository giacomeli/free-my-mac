"""Unused development tools and data from databases whose installation is already gone."""
from __future__ import annotations

import getpass
import os
import re
from pathlib import Path
from typing import List, Sequence, Tuple

from ..core import CLEAN, PENDING, Context, Detection, Item, Op, Step, inside
from ..fs import copy_preserving, safe_remove
from ..paths import PathsStep, remove_paths_op
from ..shellfiles import SHELL_FILES


def shell_mentions(ctx: Context, formula: str) -> bool:
    """Does the opt/<formula> path appear in the shell files (PATH, aliases...)?"""
    pattern = re.compile(r'/opt/%s(?:/|["\'\s:]|$)' % re.escape(formula), re.MULTILINE)
    for name in SHELL_FILES:
        path = ctx.home / name
        if path.is_file() and pattern.search(path.read_text(errors='replace')):
            return True
    return False


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    os.chmod(path, 0o700)
    return path


class BrewExtras(PathsStep):
    id = 'brew-extras'
    group = 'devtools'
    title = 'Homebrew: duplicate or unused formulae'
    formulas = ('qemu', 'mysql-client@8.4', 'mongocli', 'mssql-tools18', 'msodbcsql18')
    taps = ('1password/tap', 'asmvik/formulae', 'magalucloud/mgccli', 'mutagen-io/mutagen', 'microsoft/mssql-release')
    tolerate_brew_failure = False
    what = ('Uninstalls qemu (with no Docker or Podman, nothing else uses it), mysql-client@8.4 (mysql-client '
            'stays, and .zshrc puts it in PATH), mongocli (replaced by mongodb-atlas-cli) and the SQL Server '
            'tools (msodbcsql18 and mssql-tools18). Also removes the taps left with no installed formula.')
    why = 'They are duplicates or leftovers from environments that are already gone (Docker, .NET).'
    consequences = ('sqlcmd and bcp go away. A formula mentioned in the shell files comes unchecked, and so '
                    'does a tap that still has an installed formula.')
    undo = 'brew install <formula> (and brew tap <tap>, if needed).'

    def detect(self, ctx: Context) -> Detection:
        det = super().detect(ctx)
        brew = ctx.tool('brew')
        owned: List[str] = []
        if brew:
            for kind in ('--formula', '--cask'):
                result = ctx.query([brew, 'list', kind, '--full-name'], timeout=120)
                owned.extend(result.out.split() if result.ok else [])
        leaving = {i.key.split(':', 1)[1] for i in det.items if i.key.startswith('brew:')}
        for item in det.items:
            if item.key.startswith('brew:'):
                formula = item.key.split(':', 1)[1]
                if shell_mentions(ctx, formula):
                    item.selected = False
                    item.note = 'mentioned in the shell files; verify first'
            elif item.key.startswith('tap:'):
                tap = item.key.split(':', 1)[1]
                remaining = [n for n in owned if n.startswith(tap + '/') and n.split('/')[-1] not in leaving]
                if remaining:
                    item.selected = False
                    item.note = 'still has: ' + ', '.join(n.split('/')[-1] for n in remaining)
        det.state = PENDING if any(i.selected for i in det.items) else CLEAN
        return det


class AndroidTizen(PathsStep):
    id = 'android-tizen'
    group = 'devtools'
    title = 'Remove Android and Tizen'
    casks = ('android-commandlinetools',)
    targets = ('~/.android', '~/Library/Android', '~/tizen-sdk', '~/tizen-sdk-data', '~/.tizen')
    what = ('Uninstalls the Android command line tools (cask) and deletes ~/.android (emulators, cache and '
            'debug keys), ~/Library/Android and the Tizen SDK.')
    why = 'You no longer develop for Android or for Samsung TVs.'
    consequences = ('The debug key (debug.keystore) and the adb key (adbkey) go to the backup first; '
                    'emulators and their data are lost.')
    undo = 'brew install --cask android-commandlinetools; the keys are in ~/bkp/free-my-mac/<date>/android/.'

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        android = ctx.user('~/.android')
        ops: List[Op] = []
        if any(i.path == str(android) for i in items):
            def keys(c: Context) -> None:
                folder = _private_dir(c.backup_dir() / 'android')
                for name in ('debug.keystore', 'adbkey', 'adbkey.pub'):
                    copied = copy_preserving(android / name, folder / name)
                    if copied:
                        os.chmod(copied, 0o600)
            ops.append(Op('Save the debug keys to the backup',
                          'cp ~/.android/{debug.keystore,adbkey,adbkey.pub} ~/bkp/free-my-mac/<date>/android/',
                          func=keys))
        return ops + super().ops(ctx, items)


class PlatformioQmk(PathsStep):
    id = 'platformio-qmk'
    group = 'devtools'
    title = 'Remove PlatformIO and QMK'
    formulas = ('platformio', 'qmk', 'arm-none-eabi-gcc@8', 'arm-none-eabi-binutils', 'avr-gcc@8', 'avr-binutils',
                'avrdude', 'hid_bootloader_cli', 'mdloader')
    taps = ('qmk/qmk', 'osx-cross/arm', 'osx-cross/avr')
    targets = ('~/.platformio',)
    what = ('Uninstalls PlatformIO, QMK and its build toolchain (gcc 8 and binutils for ARM and AVR, avrdude, '
            'hid_bootloader_cli, mdloader), the taps they came from and ~/.platformio (downloaded platforms '
            'and toolchains).')
    why = 'You no longer use PlatformIO or QMK.'
    consequences = 'The qmk_firmware project folder stays where it is.'
    undo = 'brew install platformio; brew install qmk/qmk/qmk and qmk setup.'

    def detect(self, ctx: Context) -> Detection:
        det = super().detect(ctx)
        for item in det.items:
            if item.key == 'brew:avrdude':
                item.note = 'AVR microcontroller programmer used by QMK; uncheck it if you still flash boards'
        return det


class NvimMason(PathsStep):
    id = 'nvim-mason'
    group = 'devtools'
    title = 'Neovim Mason'
    targets = ('~/.local/share/nvim/mason',)
    what = 'Deletes the language servers and tools that Mason downloaded for Neovim.'
    why = 'They take more than 1 GB and Neovim is not your main editor.'
    consequences = ('Neovim stays installed. If your config has ensure_installed, Mason downloads everything '
                    'again the next time Neovim opens.')
    undo = ':MasonInstall <package> inside Neovim.'


# (id, name, formulae that keep the data in use, process, data folder, extras)
BREW_DATABASES: Sequence[Tuple[str, str, str, str, str, Tuple[str, ...]]] = (
    ('mongodb', 'Homebrew MongoDB', 'mongodb-community', 'mongod', 'var/mongodb',
     ('var/log/mongodb', 'etc/mongod.conf')),
    ('postgresql@14', 'Homebrew PostgreSQL 14', 'postgresql@14', 'postgres', 'var/postgresql@14',
     ('var/log/postgresql@14.log',)),
)
EDB_ROOT = '/Library/PostgreSQL'
EDB_VERSION = '9.6'


def _running(ctx: Context, process: str) -> bool:
    result = ctx.query(['/usr/bin/pgrep', '-x', process], cache=False)
    return result.ok and bool(result.out.strip())


class OrphanDbData(Step):
    id = 'orphan-db-data'
    group = 'devtools'
    title = 'Data from uninstalled databases'
    what = ('Archives and deletes data from databases whose program is already gone: the Homebrew MongoDB '
            'and PostgreSQL 14 (uninstalled formulae) and PostgreSQL %s from the EDB installer. Each data '
            'folder becomes a .tar.gz in ~/bkp/free-my-mac/<date>/databases/ before it is deleted.' % EDB_VERSION)
    why = 'Without the server, nobody opens this data; it only takes up space.'
    consequences = ('To open the data again you need to install the same database version and extract the '
                    'archive. Nothing is removed while the server is running. The macOS postgres user, created '
                    'by EDB, comes unchecked.')
    undo = 'tar -xzf ~/bkp/free-my-mac/<date>/databases/<name>.tar.gz and install the same database version.'

    def detect(self, ctx: Context) -> Detection:
        items: List[Item] = []
        notes: List[str] = []
        prefix = ctx.brew_prefix()
        formulas = ctx.brew_formulas()
        for key, name, formula, process, data, extras in BREW_DATABASES:
            folder = prefix / data
            if not folder.is_dir():
                continue
            if any(f == formula or f.startswith(formula + '@') for f in formulas):
                continue
            if _running(ctx, process):
                notes.append('%s stays: the %s process is running.' % (name, process))
                continue
            paths = [folder] + [prefix / e for e in extras if os.path.lexists(prefix / e)]
            items.append(Item('brewdb:' + key, name, size=sum(ctx.du(p) or 0 for p in paths) or None,
                              note=', '.join(ctx.pretty(p) for p in paths),
                              data={'archive': str(folder), 'paths': [str(p) for p in paths], 'name': key}))
        edb = ctx.sys(EDB_ROOT) / EDB_VERSION
        if edb.is_dir():
            if _running(ctx, 'postgres'):
                notes.append('PostgreSQL %s (EDB) stays: a postgres process is running.' % EDB_VERSION)
            else:
                paths = [edb]
                others = [p for p in ctx.sys(EDB_ROOT).iterdir() if p.name != EDB_VERSION and not p.name.startswith('.')]
                if not others:
                    paths = [ctx.sys(EDB_ROOT)]
                registry = ctx.sys('/etc/postgres-reg.ini')
                if registry.exists():
                    paths.append(registry)
                items.append(Item('edb', 'PostgreSQL %s (EDB installer)' % EDB_VERSION, size=ctx.du(edb),
                                  note=', '.join(ctx.pretty(p) for p in paths),
                                  data={'archive': str(edb / 'data'), 'paths': [str(p) for p in paths],
                                        'name': 'postgresql-' + EDB_VERSION}))
            user = ctx.query(['/usr/bin/dscl', '.', '-read', '/Users/postgres', 'NFSHomeDirectory'])
            if user.ok and EDB_ROOT in user.out:
                items.append(Item('postgres-user', 'macOS postgres user (created by EDB)', selected=False,
                                  note='service account; remove it only if nothing else uses this user'))
        return Detection(PENDING if any(i.selected for i in items) else CLEAN, items, notes=notes)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        folder = ctx.backup_dir() / 'databases'
        ops: List[Op] = []
        archived = [i for i in items if 'archive' in i.data]
        if archived:
            ops.append(Op('Create the databases folder in the backup', 'mkdir -m 700 ' + ctx.pretty(folder),
                          func=lambda c: _private_dir(folder)))
        user = getpass.getuser()
        for item in archived:
            source = Path(item.data['archive'])
            target = folder / (item.data['name'] + '.tar.gz')
            argv = ['/usr/bin/tar', '-czf', str(target), '-C', str(source.parent), source.name]
            system = ctx.needs_sudo(source) and not inside(source, ctx.brew_prefix())
            ops.append(Op('Archive ' + item.label, ('sudo ' if system else '') + 'tar -czf %s -C %s %s' % (
                ctx.pretty(target), ctx.pretty(source.parent), source.name), argv=argv, sudo=system))
            if system:
                ops.append(Op('Give the archive back to your user', 'sudo chown %s %s' % (user, ctx.pretty(target)),
                              argv=['/usr/sbin/chown', user, str(target)], sudo=True))
        brew_paths = [Path(p) for i in archived for p in i.data['paths'] if inside(Path(p), ctx.brew_prefix())]
        other_paths = [Path(p) for i in archived for p in i.data['paths'] if not inside(Path(p), ctx.brew_prefix())]
        if brew_paths:
            def remove_brew(c: Context, targets=tuple(brew_paths)) -> None:
                for target in targets:
                    print('  removing ' + c.pretty(target), flush=True)
                    safe_remove(target)
            ops.append(Op('Delete the Homebrew data', 'rm -rf -- ' + ' '.join(ctx.pretty(p) for p in brew_paths),
                          func=remove_brew))
        ops.extend(remove_paths_op(ctx, other_paths, 'Delete PostgreSQL %s (EDB)' % EDB_VERSION))
        if any(i.key == 'postgres-user' for i in items):
            argv = ['/usr/bin/dscl', '.', '-delete', '/Users/postgres']
            ops.append(Op('Remove the postgres user', 'sudo dscl . -delete /Users/postgres', argv=argv, sudo=True))
        return ops


STEPS = [BrewExtras, AndroidTizen, PlatformioQmk, NvimMason, OrphanDbData]
