"""Recurring maintenance: caches, Homebrew, Xcode, Cowork and Apple Intelligence."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import List, Optional

from ..core import AVAILABLE, CLEAN, PENDING, Context, Detection, Item, Op, Step, human, plural
from ..fs import safe_remove, write_private
from ..paths import PathsStep, remove_paths_op

CACHE_THRESHOLD = 100 * 1000 * 1000


class CacheStep(Step):
    """Cache folder: only Pending above the threshold, so it does not show up after every use."""
    cache_path = ''

    def location(self, ctx: Context) -> Optional[Path]:
        return ctx.user(self.cache_path)

    def detect(self, ctx: Context) -> Detection:
        path = self.location(ctx)
        size = ctx.du(path) if path and os.path.lexists(path) else None
        if not size:
            return Detection(CLEAN, summary='Cache is empty.')
        item = Item('path:' + str(path), ctx.pretty(path), size=size, path=str(path))
        if size < CACHE_THRESHOLD:
            return Detection(CLEAN, [item], summary='Cache is small (%s).' % human(size))
        return Detection(PENDING, [item])

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        return remove_paths_op(ctx, [Path(i.path) for i in items], 'Delete the ' + self.title)


class NpmCache(CacheStep):
    id = 'npm-cache'
    group = 'maint'
    title = 'npm download cache'
    rerunnable = True
    cache_path = '~/.npm/_cacache'
    what = 'Deletes ~/.npm/_cacache, where npm keeps every package it downloads.'
    why = 'It grows without limit; npm never cleans it up on its own.'
    consequences = 'The next install downloads again any package that is not already in node_modules.'
    undo = 'It rebuilds itself.'


class UvCache(CacheStep):
    id = 'uv-cache'
    group = 'maint'
    title = 'uv cache'
    rerunnable = True
    what = 'Runs uv cache clean (wheels and packages downloaded by uv).'
    why = 'It grows with every venv you create; with the venvs recreated, almost everything in it is a duplicate.'
    consequences = 'Later installs download again. Existing venvs do not change.'
    undo = 'It rebuilds itself.'

    def location(self, ctx: Context) -> Optional[Path]:
        uv = ctx.tool('uv')
        if not uv:
            return None
        result = ctx.query([uv, 'cache', 'dir'])
        return Path(result.out.strip()) if result.ok and result.out.strip() else None

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        uv = ctx.tool('uv') or 'uv'
        return [Op('Clean the uv cache', 'uv cache clean', argv=[uv, 'cache', 'clean'])]


DEV_CACHES = (
    ('mise', 'mise cache (downloads and compiled versions)', '~/Library/Caches/mise'),
    ('cargo-src', 'Extracted Rust crate sources', '~/.cargo/registry/src'),
    ('cargo-git', 'Rust git dependency checkouts', '~/.cargo/git/checkouts'),
    ('node-gyp', 'Node headers for node-gyp', '~/Library/Caches/node-gyp'),
    ('vsix', 'Extension packages downloaded by VS Code', '~/Library/Application Support/Code/CachedExtensionVSIXs'),
)
VSCODE_EXTENSIONS = '~/.vscode/extensions'


class DevCaches(Step):
    id = 'dev-caches'
    group = 'maint'
    title = 'Development caches'
    rerunnable = True
    what = ('Deletes caches that the tools rebuild on their own: the mise cache (mise cache clear), the '
            'extracted crate sources in ~/.cargo/registry/src and the Cargo git checkouts, the node-gyp '
            'headers, the extension packages downloaded by VS Code and the old extension versions that '
            'VS Code has already marked as obsolete.')
    why = 'They grow with every update and nobody deletes them.'
    consequences = ('The next Rust build extracts the crates again (without downloading: the .crate cache '
                    'stays). node-gyp downloads the headers again when it needs them. Installed extensions '
                    'do not change.')
    undo = 'They rebuild themselves.'

    def detect(self, ctx: Context) -> Detection:
        items: List[Item] = []
        for key, label, spec in DEV_CACHES:
            path = ctx.user(spec)
            size = ctx.du(path) if os.path.lexists(path) else None
            if size:
                items.append(Item('cache:' + key, label, size=size, path=str(path), note=ctx.pretty(path)))
        extensions = ctx.user(VSCODE_EXTENSIONS)
        try:
            obsolete = json.loads((extensions / '.obsolete').read_text())
        except (OSError, ValueError):
            obsolete = {}
        for name in sorted(obsolete if isinstance(obsolete, dict) else {}):
            folder = extensions / name
            if obsolete[name] and folder.is_dir() and folder.parent == extensions:
                items.append(Item('ext:' + name, 'Obsolete VS Code extension: ' + name, size=ctx.du(folder),
                                  path=str(folder)))
        total = sum(i.size or 0 for i in items)
        if total < CACHE_THRESHOLD:
            return Detection(CLEAN, items, summary='Caches are small (%s).' % human(total) if items else 'No caches.')
        return Detection(PENDING, items)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        ops: List[Op] = []
        mise = ctx.tool('mise')
        paths = []
        for item in items:
            if item.key == 'cache:mise' and mise:
                ops.append(Op('Clean the mise cache', 'mise cache clear', argv=[mise, 'cache', 'clear']))
            else:
                paths.append(Path(item.path))
        return ops + remove_paths_op(ctx, paths, 'Delete ' + plural(len(paths), 'cache', 'caches') if paths else '')


_UNITS = {'B': 1, 'KB': 1e3, 'MB': 1e6, 'GB': 1e9, 'TB': 1e12}


def parse_brew_size(text: str) -> Optional[int]:
    match = re.search(r'free approximately ([\d.]+)\s*([KMGT]?B)', text)
    if not match:
        return None
    return int(float(match.group(1)) * _UNITS[match.group(2)])


class BrewCleanup(Step):
    id = 'brew-cleanup'
    group = 'maint'
    title = 'Homebrew: old versions and orphan dependencies'
    heavy = True
    rerunnable = True
    what = ('brew cleanup --prune=all deletes downloads and old versions of formulae; the dependencies that '
            'no installed formula uses anymore (listed by brew autoremove -n) are uninstalled by name.')
    why = 'After the earlier removals, orphan dependencies and old versions are left behind.'
    consequences = 'Each orphan dependency shows up as an item; only the checked ones are removed.'
    undo = 'brew install <formula> for any of them you still use directly.'

    def detect(self, ctx: Context) -> Detection:
        brew = ctx.tool('brew')
        if not brew:
            return Detection(CLEAN, summary='Homebrew not found.')
        items: List[Item] = []
        notes: List[str] = []
        cleanup = ctx.query([brew, 'cleanup', '--prune=all', '-n'], timeout=300)
        removals = [l for l in cleanup.out.splitlines() if l.startswith('Would remove')]
        if removals:
            items.append(Item('cleanup', plural_files(len(removals)), size=parse_brew_size(cleanup.out)))
        orphans = ctx.query([brew, 'autoremove', '-n'], timeout=300)
        names = [l.strip() for l in orphans.out.splitlines() if l.strip() and not l.startswith('==>')]
        for name in names:
            # An orphan python@ may be the base of a venv: it is removed only if you check it.
            python = name.startswith('python@')
            items.append(Item('orphan:' + name, 'Orphan dependency: ' + name, selected=not python,
                              size=ctx.du(ctx.brew_prefix() / 'Cellar' / name),
                              note='may be the base of a venv; verify first' if python else ''))
        if names:
            notes.append('Only the checked orphan dependencies are uninstalled, one at a time by name; '
                         'new orphans that appear later show up in the next scan.')
        return Detection(PENDING if any(i.selected for i in items) else CLEAN, items, notes=notes)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        brew = ctx.tool('brew') or 'brew'
        names = [i.key.split(':', 1)[1] for i in items if i.key.startswith('orphan:')]
        ops: List[Op] = []
        if names:
            ops.append(Op('Uninstall orphan dependencies: ' + ', '.join(names),
                          'brew uninstall --formula ' + ' '.join(names),
                          argv=[brew, 'uninstall', '--formula'] + names))
        if any(i.key == 'cleanup' for i in items) or names:
            ops.append(Op('Delete old versions and downloads', 'brew cleanup --prune=all',
                          argv=[brew, 'cleanup', '--prune=all']))
        return ops


def plural_files(count: int) -> str:
    return plural(count, 'old version or download in the cache', 'old versions and downloads in the cache')


class XcodeDerived(PathsStep):
    id = 'xcode-derived'
    group = 'maint'
    title = 'Xcode DerivedData'
    rerunnable = True
    targets = ('~/Library/Developer/Xcode/DerivedData/*',)
    what = 'Deletes the contents of ~/Library/Developer/Xcode/DerivedData (intermediate builds and indexes).'
    why = 'Xcode recreates everything on the next build; the folder only grows.'
    consequences = 'The first build and the indexing of each project are slower.'
    undo = 'It is rebuilt on the next build.'


# Claude Cowork -----------------------------------------------------------------------

def cowork_paths(ctx: Context):
    base = ctx.user('~/Library/Application Support/Claude')
    return base / 'claude_desktop_config.json', base / 'vm_bundles'


def read_cowork_config(config: Path):
    if config.is_symlink() or not config.is_file():
        raise ValueError('The configuration is missing or is a link; open the file through Claude Desktop.')
    raw = config.read_bytes()

    def unique(pairs):
        data = {}
        for key, value in pairs:
            if key in data:
                raise ValueError('The JSON has duplicate keys; fix it before applying.')
            data[key] = value
        return data
    data = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(data, dict) or not isinstance(data.get('preferences', {}), dict):
        raise ValueError('Unrecognized configuration structure.')
    return raw, data


def assert_claude_closed(vm: Path) -> None:
    result = subprocess.run(['/bin/ps', '-axo', 'comm='], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ValueError('Could not check running processes; operation blocked.')
    if any('/Claude.app/' in line or line.strip().endswith('/Claude') for line in result.stdout.splitlines()):
        raise ValueError('Quit Claude Desktop completely (Cmd+Q) before running this.')
    if vm.exists():
        result = subprocess.run(['/usr/sbin/lsof', '-nP', '+D', str(vm)], capture_output=True, text=True, timeout=60)
        if result.returncode not in (0, 1):
            raise ValueError('Could not check open files in the VM bundle.')
        if result.stdout.strip():
            raise ValueError('Processes are using the VM bundle; operation blocked.')


class Cowork(Step):
    id = 'cowork'
    group = 'maint'
    title = 'Claude Cowork: turn off and delete the VM'
    what = ('With Claude Desktop closed, turns off the Cowork VM (preferences.secureVmFeaturesEnabled = '
            'false) and deletes the vm_bundles folder.')
    why = 'The Cowork VM takes several GB and is downloaded again while the feature is on.'
    consequences = ('Only the preference and the vm_bundles folder change; chats, MCPs and sessions stay. The '
                    'configuration backup goes to ~/bkp/free-my-mac/<date>/claude/.')
    undo = 'Turn Cowork back on in the Claude Desktop settings; the VM is downloaded again.'

    def detect(self, ctx: Context) -> Detection:
        config, vm = cowork_paths(ctx)
        if not config.exists():
            return Detection(CLEAN, summary='Claude Desktop is not configured.')
        _raw, data = read_cowork_config(config)
        items: List[Item] = []
        if data.get('preferences', {}).get('secureVmFeaturesEnabled') is not False:
            items.append(Item('pref', 'Turn off secureVmFeaturesEnabled'))
        if os.path.lexists(vm):
            items.append(Item('vm', 'Delete ' + ctx.pretty(vm), size=ctx.du(vm), path=str(vm)))
        return Detection(PENDING if items else CLEAN, items)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        keys = {i.key for i in items}
        pref, vm = 'pref' in keys, 'vm' in keys
        parts = ['check that Claude is closed'] + (['back up the config', 'secureVmFeaturesEnabled=false']
                                                     if pref else []) + (['rm -rf vm_bundles'] if vm else [])
        label = 'Turn off Cowork and delete the VM' if pref and vm else ('Turn off Cowork' if pref else 'Delete the Cowork VM')
        return [Op(label, '; '.join(parts), func=lambda c: self.apply(c, pref=pref, vm=vm))]

    def apply(self, ctx: Context, pref: bool = True, vm: bool = True) -> None:
        do_pref, do_vm = pref, vm
        config, vm = cowork_paths(ctx)
        raw, data = read_cowork_config(config)
        if config.stat().st_uid != os.getuid():
            raise ValueError('The configuration belongs to another user.')
        if vm.is_symlink():
            raise ValueError('vm_bundles is a link; operation blocked.')
        assert_claude_closed(vm)
        if not do_pref:
            if os.path.lexists(vm) and do_vm:
                safe_remove(vm)
                print('  vm_bundles deleted', flush=True)
            return
        folder = ctx.backup_dir() / 'claude'
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(ctx.backup_dir(), 0o700)
        backup = folder / ('claude_desktop_config.%d.json' % time.time_ns())
        write_private(backup, raw.decode())
        print('  configuration backup: ' + ctx.pretty(backup), flush=True)
        if data.get('preferences', {}).get('secureVmFeaturesEnabled') is not False:
            data.setdefault('preferences', {})['secureVmFeaturesEnabled'] = False
            fd, temporary = tempfile.mkstemp(prefix='.cowork-config-', dir=str(config.parent))
            try:
                with os.fdopen(fd, 'w') as stream:
                    json.dump(data, stream, ensure_ascii=False, indent=2)
                    stream.write('\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                if config.read_bytes() != raw:
                    raise ValueError('The configuration changed during the operation; nothing was written.')
                os.chmod(temporary, os.stat(config).st_mode & 0o777)
                os.replace(temporary, config)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            print('  secureVmFeaturesEnabled = false', flush=True)
        if do_vm and os.path.lexists(vm):
            assert_claude_closed(vm)
            safe_remove(vm)
            print('  vm_bundles deleted', flush=True)


class AppleIntelligence(Step):
    id = 'apple-intelligence'
    group = 'maint'
    title = 'Apple Intelligence: turn off and remove models'
    rerunnable = True
    what = ('Uses RemoveMacAI (a third-party tool, tap omlahore/tap) to turn off Apple Intelligence and '
            'delete the downloaded models. Installs the tool with Homebrew if it is missing.')
    why = 'The models take several GB and macOS downloads them again while the feature is on.'
    consequences = ('It may ask you to approve a profile in System Settings; the model removal finishes in the '
                    'background. There is no reliable way to tell whether it is already done, so this step '
                    'always shows as Available.')
    undo = 'removemacai on.'

    def detect(self, ctx: Context) -> Detection:
        items: List[Item] = []
        if not ctx.tool('removemacai'):
            items.append(Item('install', 'Install RemoveMacAI (brew install omlahore/tap/removemacai)'))
        items.append(Item('off', 'Turn off and remove the models (removemacai off)'))
        return Detection(AVAILABLE, items)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        keys = {i.key for i in items}
        ops: List[Op] = []
        if 'install' in keys:
            ops.append(Op('Install RemoveMacAI', 'brew install omlahore/tap/removemacai',
                          argv=[ctx.tool('brew') or 'brew', 'install', 'omlahore/tap/removemacai']))
        if 'off' in keys:
            tool = ctx.tool('removemacai') or str(ctx.brew_prefix() / 'bin/removemacai')
            ops.append(Op('Turn off Apple Intelligence', 'removemacai off', argv=[tool, 'off']))
        return ops


STEPS = [DevCaches, NpmCache, UvCache, BrewCleanup, XcodeDerived, Cowork, AppleIntelligence]
