"""Preparation: backup and exports before any removal."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import List

from ..core import CLEAN, PENDING, Context, Detection, Item, Op, Step, plural
from ..fs import copy_preserving, write_private
from ..pyenvs import conda_envs, conda_root, package_lines, slug, tracked_venvs, venv_config

SHELL_FILES = ('.zshrc', '.zprofile', '.zshenv', '.bashrc', '.bash_profile', '.profile')


def latest_backup(ctx: Context):
    if not ctx.backup_root.is_dir():
        return None
    manifests = sorted(ctx.backup_root.glob('*/manifest.json'))
    return manifests[-1] if manifests else None


def backed_up_keys(ctx: Context) -> set:
    """Items that a finished backup already saved (manifests from every date)."""
    keys: set = set()
    if not ctx.backup_root.is_dir():
        return keys
    for manifest in ctx.backup_root.glob('*/manifest.json'):
        try:
            keys.update(json.loads(manifest.read_text()).get('keys', []))
        except (OSError, ValueError):
            continue
    return keys


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


class Backup(Step):
    id = 'backup'
    group = 'prep'
    title = 'Back up and export'
    rerunnable = True
    what = ('Saves everything the following steps can change in a private folder: shell files, '
            '/etc/hosts, /etc/paths.d, the Homebrew list (Brewfile), the installer receipts, the recipe '
            'and packages of each conda environment, and the package list of each venv.')
    why = ('The Python, conda, PHP and Shell steps depend on this backup: it is what you use to rebuild an '
           'environment or restore a config file.')
    consequences = ('Only creates files in ~/bkp/free-my-mac/<date> (permissions 700). The .zshrc backup '
                    'contains the Cloudflare token in plain text, just like the original file.')
    undo = 'Delete the backup folder when you no longer need it.'

    def detect(self, ctx: Context) -> Detection:
        items: List[Item] = []
        shell = [n for n in SHELL_FILES if (ctx.home / n).is_file()]
        if shell:
            items.append(Item('shell', 'Shell files: ' + ', '.join(shell)))
        items.append(Item('etc', '/etc/hosts and /etc/paths.d'))
        if ctx.tool('brew'):
            items.append(Item('brewfile', 'Brewfile (brew bundle dump)'))
        items.append(Item('pkgutil', 'Installer receipt list (pkgutil --pkgs)'))
        if ctx.user('~/.config/mise').is_dir():
            items.append(Item('mise', 'mise settings (~/.config/mise)'))
        root = conda_root(ctx)
        if root:
            for name in conda_envs(ctx):
                items.append(Item('conda:' + name, 'Conda environment ' + name + ': recipe, explicit list and packages'))
        for venv, _app in tracked_venvs(ctx):
            # With the base already removed there is no list to save; the venvs step uses uv.lock or requirements.
            if os.path.exists(venv / 'bin/python'):
                items.append(Item('venv:' + str(venv), 'Packages of venv ' + ctx.pretty(venv), path=str(venv)))
        last = latest_backup(ctx)
        if not last:
            return Detection(PENDING, items, summary='No backup made yet.')
        done = backed_up_keys(ctx)
        missing = [i for i in items if i.key not in done]
        if not missing:
            return Detection(CLEAN, items, summary='Last backup: %s. You can run it again at any time.'
                             % ctx.pretty(last.parent))
        for item in items:
            item.selected = item.key not in done
        return Detection(PENDING, items, summary='Missing from the backup: ' + ', '.join(i.label for i in missing[:4]) +
                         (' and %d more' % (len(missing) - 4) if len(missing) > 4 else ''))

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        target = ctx.backup_dir()
        keys = {i.key for i in items}
        ops: List[Op] = [Op('Create the backup folder', 'mkdir -m 700 ' + ctx.pretty(target),
                            func=lambda c: _private_dir(target))]
        if 'shell' in keys:
            def shell(c: Context) -> None:
                folder = _private_dir(target / 'shell')
                for name in SHELL_FILES:
                    copied = copy_preserving(c.home / name, folder / name)
                    if copied:
                        os.chmod(copied, 0o600)
            ops.append(Op('Copy shell files', 'cp ~/.zshrc ~/.zprofile ... %s/shell/' % ctx.pretty(target), func=shell))
        if 'etc' in keys:
            def etc(c: Context) -> None:
                folder = _private_dir(target / 'etc')
                copy_preserving(c.sys('/etc/hosts'), folder / 'hosts')
                copy_preserving(c.sys('/etc/paths.d'), folder / 'paths.d')
            ops.append(Op('Copy /etc/hosts and /etc/paths.d', 'cp -R /etc/hosts /etc/paths.d %s/etc/' % ctx.pretty(target), func=etc))
        if 'brewfile' in keys:
            brewfile = target / 'Brewfile'
            ops.append(Op('Export the Homebrew list', 'brew bundle dump --force --file=' + ctx.pretty(brewfile),
                          argv=[ctx.tool('brew') or 'brew', 'bundle', 'dump', '--force', '--file=' + str(brewfile)],
                          tolerate_failure=True))
        if 'pkgutil' in keys:
            def pkgutil(c: Context) -> None:
                result = c.query([c.tool('pkgutil') or '/usr/sbin/pkgutil', '--pkgs'], cache=False)
                _write(target / 'pkgutil.txt', result.out)
            ops.append(Op('Save the receipt list', 'pkgutil --pkgs > %s/pkgutil.txt' % ctx.pretty(target), func=pkgutil))
        if 'mise' in keys:
            ops.append(Op('Copy the mise settings', 'cp -R ~/.config/mise %s/mise' % ctx.pretty(target),
                          func=lambda c: copy_preserving(c.user('~/.config/mise'), target / 'mise')))
        envs = [i.key[6:] for i in items if i.key.startswith('conda:')]
        if envs:
            ops.append(Op('Export ' + plural(len(envs), 'conda environment', 'conda environments'),
                          'conda env export --from-history; conda list --explicit; uv pip list (per env) > %s/conda/'
                          % ctx.pretty(target), func=lambda c: _export_conda(c, envs, target / 'conda')))
        venvs = [Path(i.path) for i in items if i.key.startswith('venv:')]
        if venvs:
            ops.append(Op('Save the package lists of ' + plural(len(venvs), 'venv', 'venvs'),
                          'uv pip list --format json (per venv) > %s/venvs/' % ctx.pretty(target),
                          func=lambda c: _export_venvs(c, venvs, target / 'venvs')))
        ops.append(Op('Write the backup manifest', 'manifest.json', func=lambda c: _manifest(c, target, items)))
        return ops


def _write(path: Path, text: str) -> None:
    if path.exists():
        path.unlink()
    write_private(path, text)


def _export_conda(ctx: Context, envs: List[str], folder: Path) -> None:
    _private_dir(folder)
    root = conda_root(ctx)
    conda = str(root / 'bin/conda') if root else 'conda'
    available = conda_envs(ctx)
    for name in envs:
        if name not in available:
            continue
        print('  exporting ' + name, flush=True)
        recipe = ctx.query([conda, 'env', 'export', '-n', name, '--from-history'], cache=False, timeout=300)
        _write(folder / (name + '.yml'), recipe.out)
        explicit = ctx.query([conda, 'list', '-n', name, '--explicit'], cache=False, timeout=300)
        _write(folder / (name + '.explicit.txt'), explicit.out)
        try:
            lines = package_lines(ctx, available[name] / 'bin/python')
            _write(folder / (name + '.packages.txt'), '\n'.join(lines) + '\n')
        except (RuntimeError, ValueError) as exc:
            print('  warning: packages of %s not exported: %s' % (name, exc), flush=True)
        if not recipe.ok or not explicit.ok:
            raise RuntimeError('conda failed to export ' + name)


def _export_venvs(ctx: Context, venvs: List[Path], folder: Path) -> None:
    _private_dir(folder)
    for venv in venvs:
        python = venv / 'bin/python'
        info = venv_config(venv)
        try:
            lines = package_lines(ctx, python)
        except (RuntimeError, ValueError) as exc:
            print('  warning: no package list for %s (%s)' % (ctx.pretty(venv), exc), flush=True)
            continue
        header = '# venv: %s\n# base python: %s (%s)\n' % (ctx.pretty(venv), info.get('home', '?'),
                                                          info.get('version_info') or info.get('version', '?'))
        _write(folder / (slug(ctx, venv) + '.txt'), header + '\n'.join(lines) + '\n')


def _saved(ctx: Context, target: Path, item: Item) -> bool:
    """An item only goes into the manifest if its file exists in the backup."""
    if item.key.startswith('conda:'):
        name = item.key[6:]
        return all((target / 'conda' / (name + suffix)).is_file() for suffix in ('.yml', '.explicit.txt'))
    if item.key.startswith('venv:'):
        return (target / 'venvs' / (slug(ctx, Path(item.path)) + '.txt')).is_file()
    if item.key == 'shell':
        return (target / 'shell').is_dir()
    if item.key == 'etc':
        return (target / 'etc/hosts').is_file()
    return True


def _manifest(ctx: Context, target: Path, items: List[Item]) -> None:
    keys = [i.key for i in items if _saved(ctx, target, i)]
    path = target / 'manifest.json'
    if path.exists():
        # Same day: merge what earlier backups from that day already saved.
        try:
            keys = sorted(set(keys) | set(json.loads(path.read_text()).get('keys', [])))
        except (OSError, ValueError):
            pass
    data = dict(created=time.time(), keys=keys, items=[i.label for i in items])
    _write(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


STEPS = [Backup]
