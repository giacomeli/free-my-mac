"""Declarative step: removes known paths, pkgutil receipts, formulas and casks."""
from __future__ import annotations

import glob
import os
import re
import shlex
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .core import CLEAN, PENDING, Context, Detection, Item, Op, Step, digest, plural
from .fs import safe_remove


def remove_paths_op(ctx: Context, paths: Sequence[Path], label: str = '') -> List[Op]:
    """Splits user paths (removed in Python) from system paths (sudo rm -rfx)."""
    user = [p for p in paths if not ctx.needs_sudo(p)]
    system = [p for p in paths if ctx.needs_sudo(p)]
    ops: List[Op] = []
    if user:
        def remove_user(_ctx, targets=tuple(user)):
            for target in targets:
                print('  removing ' + ctx.pretty(target), flush=True)
                safe_remove(target)
        ops.append(Op(label=label or 'Remove ' + plural(len(user), 'user item', 'user items'),
                      display='rm -rf -- ' + ' '.join(shlex.quote(ctx.pretty(p)) for p in user),
                      func=remove_user))
    if system:
        argv = ['/bin/rm', '-rfx', '--'] + [str(p) for p in system]
        ops.append(Op(label=label or 'Remove ' + plural(len(system), 'system item', 'system items'),
                      display='sudo ' + shlex.join(argv), argv=argv, sudo=True))
    return ops


def forget_receipts_op(receipts: Sequence[str]) -> Op:
    def forget(_ctx, ids=tuple(receipts)):
        failed = []
        for identifier in ids:
            code = _ctx.call(['/usr/bin/sudo', '/usr/sbin/pkgutil', '--forget', identifier])
            if code:
                failed.append(identifier)
        if failed:
            raise RuntimeError('pkgutil failed for: ' + ', '.join(failed))
    shown = ', '.join(receipts[:3]) + (' and %d more' % (len(receipts) - 3) if len(receipts) > 3 else '')
    return Op(label='Forget ' + plural(len(receipts), 'installer receipt', 'installer receipts'),
              display='sudo pkgutil --forget ' + shown, func=forget, sudo=True)


def quit_app_op(name: str, bundle_id: str) -> Op:
    """Quits the app only if it is running (by bundle id: it neither launches the app nor asks where it is)."""
    script = 'if application id "%s" is running then tell application id "%s" to quit' % (bundle_id, bundle_id)
    return Op(label='Quit %s (if running)' % name, display="osascript -e '%s'" % script,
              argv=['/usr/bin/osascript', '-e', script], tolerate_failure=True)


def bootout_op(plist: Path) -> Op:
    argv = ['/bin/launchctl', 'bootout', 'system', str(plist)]
    return Op(label='Unload service ' + plist.name, display='sudo ' + shlex.join(argv),
              argv=argv, sudo=True, tolerate_failure=True)


class PathsStep(Step):
    """Subclasses fill in the lists; detect() and ops() are generic."""
    targets: Sequence[str] = ()
    receipts: Optional[str] = None
    formulas: Sequence[str] = ()
    casks: Sequence[str] = ()
    taps: Sequence[str] = ()
    tolerate_brew_failure = False
    quit_apps: Sequence[tuple] = ()  # (name, bundle id)

    def target_paths(self, ctx: Context) -> List[Path]:
        found: List[Path] = []
        for spec in self.targets:
            expanded = ctx.expand(spec)
            if any(ch in spec for ch in '*?['):
                found.extend(Path(p) for p in sorted(glob.glob(str(expanded))))
            elif os.path.lexists(expanded):
                found.append(expanded)
        found.extend(self.extra_paths(ctx))
        unique: List[Path] = []
        for p in found:
            if p not in unique:
                unique.append(p)
        return unique

    def extra_paths(self, ctx: Context) -> List[Path]:
        return []

    def path_note(self, ctx: Context, path: Path) -> str:
        return ''

    def detect(self, ctx: Context) -> Detection:
        items: List[Item] = []
        for formula in self.formulas:
            if formula in ctx.brew_formulas():
                items.append(Item(key='brew:' + formula, label='Homebrew: ' + formula,
                                  size=ctx.du(ctx.brew_prefix() / 'Cellar' / formula)))
        for cask in self.casks:
            if cask in ctx.brew_casks():
                items.append(Item(key='cask:' + cask, label='Homebrew cask: ' + cask))
        for path in self.target_paths(ctx):
            items.append(Item(key='path:' + str(path), label=ctx.pretty(path), path=str(path),
                              size=ctx.du(path), note=self.path_note(ctx, path)))
        for tap in self.taps:
            user, repo = tap.split('/', 1)
            if (ctx.brew_prefix() / 'Library/Taps' / user / ('homebrew-' + repo)).is_dir():
                items.append(Item(key='tap:' + tap, label='Homebrew tap: ' + tap))
        if self.receipts:
            pattern = re.compile(self.receipts)
            ids = [p for p in ctx.pkgs() if pattern.search(p)]
            if ids:
                items.append(Item(key='receipts:' + digest(ids), label='Installer receipts: ' + plural(len(ids), 'record', 'records'),
                                  note='pkgutil: ' + ', '.join(ids[:4]) + (' ...' if len(ids) > 4 else ''),
                                  data={'ids': ids}))
        return Detection(PENDING if items else CLEAN, items,
                         summary=plural(len(items), 'item found', 'items found') if items else 'Nothing found.')

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        keys = {i.key for i in items}
        ops: List[Op] = []
        if any(k.startswith('path:') for k in keys):
            for name, bundle_id in self.quit_apps:
                ops.append(quit_app_op(name, bundle_id))
        paths = [Path(i.path) for i in items if i.key.startswith('path:')]
        for plist in paths:
            if plist.suffix == '.plist' and 'LaunchDaemons' in plist.parts and ctx.real_system:
                ops.append(bootout_op(plist))
        formulas = [i.key[5:] for i in items if i.key.startswith('brew:')]
        if formulas:
            argv = [ctx.tool('brew') or 'brew', 'uninstall', '--formula'] + formulas
            ops.append(Op(label='Uninstall from Homebrew: ' + ', '.join(formulas),
                          display='brew uninstall --formula ' + ' '.join(formulas), argv=argv,
                          tolerate_failure=self.tolerate_brew_failure))
        casks = [i.key[5:] for i in items if i.key.startswith('cask:')]
        if casks:
            argv = [ctx.tool('brew') or 'brew', 'uninstall', '--cask'] + casks
            ops.append(Op(label='Uninstall casks: ' + ', '.join(casks),
                          display='brew uninstall --cask ' + ' '.join(casks), argv=argv,
                          tolerate_failure=self.tolerate_brew_failure))
        ops.extend(remove_paths_op(ctx, paths))
        for tap in [i.key[4:] for i in items if i.key.startswith('tap:')]:
            ops.append(Op(label='Remove the tap ' + tap, display='brew untap ' + tap,
                          argv=[ctx.tool('brew') or 'brew', 'untap', tap], tolerate_failure=True))
        for item in items:
            if item.key.startswith('receipts:'):
                ops.append(forget_receipts_op(item.data['ids']))
        return ops


def links_into(ctx: Context, folders: Sequence[str], predicate: Callable[[str], bool]) -> List[Path]:
    """Direct symbolic links in the folders whose target satisfies the predicate."""
    found: List[Path] = []
    for folder in folders:
        base = ctx.sys(folder) if folder.startswith('/') else ctx.user(folder)
        if not base.is_dir():
            continue
        for entry in sorted(os.scandir(base), key=lambda e: e.name):
            if entry.is_symlink():
                target = os.readlink(entry.path)
                absolute = os.path.normpath(os.path.join(str(base), target))
                if predicate(absolute):
                    found.append(Path(entry.path))
    return found
