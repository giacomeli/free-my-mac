"""Rebuildable build artifacts inside the projects folder."""
from __future__ import annotations

import datetime as _dt
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Tuple

from ..core import CLEAN, PENDING, Context, Detection, Item, Op, Step, plural
from ..paths import remove_paths_op

MAX_DEPTH = 6
STALE_DAYS = 60
NEVER_ENTER = {'node_modules', '.git', '.venv', 'venv', '.pio', 'target', '.next', 'vendor', 'Pods', 'build',
               'dist', '.gradle', '__pycache__'}
# Artifact folder -> file that must exist next to it, and whether the project must be stale.
KINDS = {'target': ('Cargo.toml', False), '.next': ('package.json', False), 'node_modules': ('package.json', True)}


def _now(ctx: Context) -> float:
    return _dt.datetime.combine(ctx.today(), _dt.time(12)).timestamp()


class GitUnknown(Exception):
    """git failed for some other reason (permission, timeout...): when in doubt, the folder stays."""


def _git(ctx: Context, folder: Path, *args: str, timeout: int = 60) -> Optional[str]:
    """git output; None outside a repository; GitUnknown on any other failure."""
    git = ctx.tool('git')
    if not git:
        raise GitUnknown('git not found')
    result = ctx.query([git, '-c', 'core.fsmonitor=false', '-C', str(folder)] + list(args), timeout=timeout)
    if result.ok:
        return result.out
    if 'not a git repository' in result.err:
        return None
    raise GitUnknown(result.err.strip()[:200] or 'exit code %d' % result.code)


def _last_commit(ctx: Context, folder: Path) -> Optional[float]:
    out = _git(ctx, folder, 'log', '-1', '--format=%ct', timeout=30)
    if out is None or not out.strip():
        return None
    try:
        return float(out.strip())
    except ValueError:
        raise GitUnknown('unexpected git log output')


def _tracked(ctx: Context, folder: Path, name: str) -> bool:
    out = _git(ctx, folder, 'ls-files', '--', name)
    return bool(out and out.strip())


def find_artifacts(ctx: Context) -> List[Tuple[Path, str]]:
    found: List[Tuple[Path, str]] = []
    root = ctx.projects
    if not root.is_dir():
        return found
    stack = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            continue
        names = {e.name for e in entries}
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            kind = KINDS.get(entry.name)
            if kind and kind[0] in names:
                found.append((Path(entry.path), entry.name))
            elif entry.name not in NEVER_ENTER and not entry.name.startswith('.') and depth < MAX_DEPTH:
                stack.append((Path(entry.path), depth + 1))
    return sorted(found)


class Artifacts(Step):
    id = 'artifacts'
    group = 'artifacts'
    title = 'Project build artifacts'
    heavy = True
    what = ('Looks in ~/Projects for folders that a build or install recreates: Rust target (with '
            'Cargo.toml next to it), Next.js .next, and node_modules of projects with no commit in more '
            'than %d days.' % STALE_DAYS)
    why = 'They are the largest folders inside the projects and none of them holds your code.'
    consequences = ('The next Rust or Next.js build is a full one (slower). A stale project needs npm/pnpm '
                    'install before it runs again. Folders tracked by git are never included.')
    undo = 'cargo build, next build or the package manager install.'

    def detect(self, ctx: Context) -> Detection:
        now = _now(ctx)
        candidates: List[Tuple[Path, str, str]] = []
        skipped: List[str] = []
        for path, name in find_artifacts(ctx):
            project = path.parent
            try:
                if _tracked(ctx, project, name):
                    continue
                last = _last_commit(ctx, project)
            except GitUnknown as exc:
                skipped.append('%s (%s)' % (ctx.pretty(path), exc))
                continue
            needs_stale = KINDS[name][1]
            if last is None:
                manifest = project / KINDS[name][0]
                last = manifest.stat().st_mtime if manifest.exists() else now
                origin = 'no git; project file changed'
            else:
                origin = 'last commit'
            days = int((now - last) // 86400)
            if needs_stale and days <= STALE_DAYS:
                continue
            candidates.append((path, name, '%s %s ago' % (origin, plural(max(days, 0), 'day', 'days'))))
        with ThreadPoolExecutor(max_workers=6) as pool:
            sizes = list(pool.map(lambda c: ctx.du(c[0]), candidates))
        items = [Item('path:' + str(path), ctx.pretty(path), size=size, path=str(path), note=note)
                 for (path, _name, note), size in zip(candidates, sizes)]
        notes = ['Ignored because git failed: ' + '; '.join(skipped)] if skipped else []
        return Detection(PENDING if items else CLEAN, items, notes=notes)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        return remove_paths_op(ctx, [Path(i.path) for i in items],
                               'Delete ' + plural(len(items), 'artifact folder', 'artifact folders'))


STEPS = [Artifacts]
