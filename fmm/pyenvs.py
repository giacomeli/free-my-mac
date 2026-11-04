"""Python helpers: mise Pythons, project venvs, conda, and rebuilding with rollback."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .core import Context, inside
from .fs import safe_remove

WANTED_PYTHONS = ('3.11', '3.12', '3.14.6')
GLOBAL_PYTHON = '3.12'
SKIP_DIRS = {'node_modules', '.git', '.pio', 'target', 'vendor', '.next', 'dist', 'build', '__pycache__',
             '.mypy_cache', '.pytest_cache', '.tox', 'site-packages'}
CONDA_ROOTS = ('~/miniforge3', '~/miniconda3', '~/anaconda3')
OLD_SUFFIX = '.free-my-mac-old'


def mise_installs(ctx: Context, tool: str = 'python') -> Dict[str, Path]:
    mise = ctx.tool('mise')
    if not mise:
        return {}
    result = ctx.query([mise, 'ls', '--json', '--installed', tool])
    if not result.ok:
        return {}
    try:
        data = json.loads(result.out or '[]')
    except ValueError:
        return {}
    entries = data.get(tool, []) if isinstance(data, dict) else data
    installs: Dict[str, Path] = {}
    for entry in entries or []:
        if entry.get('installed') and entry.get('install_path'):
            installs[entry['version']] = Path(entry['install_path'])
    return installs


def _version_key(version: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r'\d+', version)[:3])


def resolve_version(installs: Dict[str, Path], wanted: str) -> Optional[Tuple[str, Path]]:
    """Highest installed version that matches 'wanted' (exactly or by prefix)."""
    matches = [v for v in installs if v == wanted or v.startswith(wanted + '.')]
    if not matches:
        return None
    best = max(matches, key=_version_key)
    return best, installs[best]


def python_bin(install: Path) -> Path:
    for name in ('python3', 'python'):
        candidate = install / 'bin' / name
        if os.path.lexists(candidate):
            return candidate
    return install / 'bin/python3'


def mise_python_root(ctx: Context) -> Path:
    return ctx.user('~/.local/share/mise/installs/python')


# Venvs outside ~/Projects that also need to survive (e.g. apps that create their own venv).
APP_VENV_ROOTS = ('~/Library/Application Support',)
APP_VENV_SKIP = {'pipx', 'mise', 'uv'}
# Expected contents of a venv; anything else means a project folder, which is never rebuilt.
VENV_ALLOWED = {'bin', 'lib', 'lib64', 'include', 'share', 'etc', 'man', 'pyvenv.cfg', '.gitignore',
                'CACHEDIR.TAG', '.lock', 'pip-selfcheck.json'}


def venv_extras(venv: Path) -> List[str]:
    """Entries that do not belong in a venv (code, .git...)."""
    try:
        return sorted(e.name for e in os.scandir(venv) if e.name not in VENV_ALLOWED)
    except OSError:
        return []


def find_venvs(ctx: Context, max_depth: int = 6, root: Optional[Path] = None,
               skip: Optional[set] = None) -> List[Path]:
    """Folders with a pyvenv.cfg under the root (default: the projects folder)."""
    root = root or ctx.projects
    skip = SKIP_DIRS | (skip or set())
    found: List[Path] = []
    if not root.is_dir():
        return found
    stack: List[Tuple[Path, int]] = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        if any(e.name == 'pyvenv.cfg' and e.is_file(follow_symlinks=False) for e in entries):
            if not folder.name.endswith(OLD_SUFFIX):
                found.append(folder)
            continue
        if depth >= max_depth:
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False) and entry.name not in skip:
                stack.append((Path(entry.path), depth + 1))
    return sorted(found)


def doomed_base(ctx: Context, home: Path) -> bool:
    """Base Python that some catalog step removes (conda, uv, pyenv, /usr/local, Python.org)."""
    roots = [ctx.user(r) for r in CONDA_ROOTS] + [ctx.user('~/.local/share/uv/python'), ctx.user('~/.pyenv'),
                                                  ctx.sys('/usr/local'), ctx.sys('/Library/Frameworks/Python.framework')]
    return any(inside(home, r) for r in roots)


def tracked_venvs(ctx: Context) -> List[Tuple[Path, bool]]:
    """Venvs to protect: every project venv and the app venvs whose base some step removes. (venv, is_app)"""
    result: List[Tuple[Path, bool]] = [(v, False) for v in find_venvs(ctx)]
    for spec in APP_VENV_ROOTS:
        for venv in find_venvs(ctx, max_depth=4, root=ctx.user(spec), skip=APP_VENV_SKIP):
            home = venv_config(venv).get('home')
            if home and doomed_base(ctx, Path(home)):
                result.append((venv, True))
    return result


def venv_config(venv: Path) -> Dict[str, str]:
    config: Dict[str, str] = {}
    try:
        for line in (venv / 'pyvenv.cfg').read_text(errors='replace').splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                config[key.strip()] = value.strip()
    except OSError:
        pass
    return config


def venv_version(config: Dict[str, str]) -> str:
    return config.get('version_info') or config.get('version') or '?'


def pinned_version(ctx: Context, project: Path) -> Tuple[Optional[str], str]:
    """Version pinned in the project or in a parent up to the projects root, and the file it came from."""
    current = project
    while True:
        pv = current / '.python-version'
        if pv.is_file():
            first = pv.read_text(errors='replace').strip().splitlines()
            if first and first[0].strip():
                return first[0].strip(), ctx.pretty(pv)
        tv = current / '.tool-versions'
        if tv.is_file():
            for line in tv.read_text(errors='replace').splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == 'python':
                    return parts[1], ctx.pretty(tv)
        if current == ctx.projects or current == current.parent or not inside(current, ctx.projects):
            return None, ''
        current = current.parent


def conda_root(ctx: Context) -> Optional[Path]:
    for spec in CONDA_ROOTS:
        path = ctx.user(spec)
        if (path / 'bin').is_dir():
            return path
    return None


def conda_envs(ctx: Context) -> Dict[str, Path]:
    root = conda_root(ctx)
    if not root or not (root / 'envs').is_dir():
        return {}
    return {p.name: p for p in sorted((root / 'envs').iterdir()) if (p / 'bin/python').exists()}


def conda_python_version(env: Path) -> Optional[str]:
    """X.Y of a conda env's Python: from conda-meta, otherwise the highest lib/python3.N with site-packages."""
    meta = env / 'conda-meta'
    if meta.is_dir():
        for entry in sorted(meta.glob('python-3.*.json')):
            match = re.match(r'python-(\d+)\.(\d+)\.', entry.name)
            if match:
                return '%s.%s' % match.groups()
    minors = []
    for candidate in (env / 'lib').glob('python3.*'):
        match = re.fullmatch(r'python3\.(\d+)', candidate.name)
        if match and (candidate / 'site-packages').is_dir():
            minors.append(int(match.group(1)))
    return '3.%d' % max(minors) if minors else None


def conda_backups(ctx: Context) -> Dict[str, Path]:
    """Env -> conda/ folder of the most recent backup that has its package list."""
    found: Dict[str, Path] = {}
    if not ctx.backup_root.is_dir():
        return found
    for folder in sorted(ctx.backup_root.glob('*/conda'), reverse=True):
        for packages in sorted(folder.glob('*.packages.txt')):
            found.setdefault(packages.name[:-len('.packages.txt')], folder)
    return found


def backup_recipe(folder: Path, env: str) -> Tuple[Optional[str], List[str]]:
    """Python version and the dependencies that do not come from PyPI, read from the env's exported recipe."""
    version: Optional[str] = None
    others: List[str] = []
    recipe = folder / (env + '.yml')
    if recipe.is_file():
        in_deps = False
        for line in recipe.read_text(errors='replace').splitlines():
            if re.match(r'^dependencies\s*:', line):
                in_deps = True
                continue
            if in_deps and not line.startswith((' ', '-')):
                in_deps = False
            item = re.match(r'^\s*-\s*(.+?)\s*$', line) if in_deps else None
            if not item:
                continue
            spec = item.group(1)
            match = re.match(r'^python\s*=+\s*(\d+)\.(\d+)', spec)
            if match:
                version = '%s.%s' % match.groups()
            elif not re.match(r'^(python|pip)\b', spec):
                others.append(spec)
    if version is None:
        explicit = folder / (env + '.explicit.txt')
        if explicit.is_file():
            match = re.search(r'/python-(\d+)\.(\d+)\.\d+-', explicit.read_text(errors='replace'))
            if match:
                version = '%s.%s' % match.groups()
    return version, others


def remap_editables(ctx: Context, lines: List[str]) -> Tuple[List[str], List[str]]:
    """Replaces '-e <folder that no longer exists>' with the folder of the same name in ~/Projects (up to 2 levels)."""
    result: List[str] = []
    warnings: List[str] = []
    for line in lines:
        if line.startswith('-e '):
            target = Path(line[3:].strip())
            if not target.exists():
                matches = [m for m in list(ctx.projects.glob(target.name)) + list(ctx.projects.glob('*/' + target.name))
                           if m.is_dir()]
                if len(matches) == 1:
                    result.append('-e ' + str(matches[0]))
                    warnings.append('editable %s -> %s' % (ctx.pretty(target), ctx.pretty(matches[0])))
                else:
                    warnings.append('editable %s no longer exists and is left out%s' % (
                        ctx.pretty(target), ' (%d folders with that name)' % len(matches) if matches else ''))
                continue
        result.append(line)
    return result, warnings


def package_lines(ctx: Context, python: Path) -> List[str]:
    """Installable list of the packages in an environment; editables become -e <path>."""
    uv = ctx.tool('uv')
    if not uv:
        raise RuntimeError('uv not found')
    result = ctx.query([uv, 'pip', 'list', '--format', 'json', '--python', str(python)], cache=False, timeout=120)
    if not result.ok:
        raise RuntimeError('uv pip list failed: ' + (result.err.strip().splitlines() or ['?'])[-1])
    lines = []
    for package in json.loads(result.out or '[]'):
        location = package.get('editable_project_location')
        if location:
            lines.append('-e ' + location)
        else:
            lines.append('%s==%s' % (package['name'], package['version']))
    return lines


def slug(ctx: Context, path: Path) -> str:
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', ctx.pretty(path).strip('~/')) or 'root'


def rebuild_venv(ctx: Context, venv: Path, python: Path, requirements: Optional[Path],
                 fallback_project: Optional[Path] = None, use_lock: bool = False,
                 prefer_list: bool = False, allow_no_deps: bool = False) -> str:
    """Creates the venv again; the old one is only deleted if the install succeeds. Returns the method used."""
    uv = ctx.tool('uv')
    if not uv:
        raise RuntimeError('uv not found')
    if venv in (ctx.home, ctx.projects) or venv.name.endswith(OLD_SUFFIX) or not venv.parent.is_dir():
        raise RuntimeError('%s cannot be rebuilt automatically' % ctx.pretty(venv))
    extras = venv_extras(venv) if os.path.lexists(venv) else []
    if extras:
        raise RuntimeError('%s contains files that are not part of a venv (%s); rebuild it by hand'
                           % (ctx.pretty(venv), ', '.join(extras[:5])))
    old = venv.with_name(venv.name + OLD_SUFFIX)
    if os.path.lexists(old):
        raise RuntimeError('%s already exists (interrupted previous run?). Review it before continuing.' % ctx.pretty(old))
    had_old = os.path.lexists(venv)
    if had_old:
        os.rename(venv, old)
    project = venv.parent
    try:
        if ctx.call([uv, 'venv', '--python', str(python), str(venv)]):
            raise RuntimeError('uv venv failed')
        target = str(venv / 'bin/python')
        methods = []
        lock = ('uv.lock', [uv, 'sync', '--frozen', '--python', str(python)])
        if requirements is not None:
            methods.append(('package list', [uv, 'pip', 'install', '--python', target, '-r', str(requirements)]))
            if allow_no_deps:
                # List from an environment that worked: copied as it was, even with inconsistent pins.
                methods.append(('exact list without resolving dependencies',
                                [uv, 'pip', 'install', '--python', target, '--no-deps', '-r', str(requirements)]))
        if use_lock:
            # With the exact list from the old venv, the lock is only plan B; without it, the lock comes first.
            methods.insert(len(methods) if prefer_list else 0, lock)
        if fallback_project is not None and (fallback_project / 'pyproject.toml').is_file():
            methods.append(('pyproject.toml', [uv, 'pip', 'install', '--python', target, '-e', str(fallback_project)]))
        if not methods:
            raise RuntimeError('no package source to install from')
        for name, argv in methods:
            print('  installing packages via %s' % name, flush=True)
            if ctx.call(argv, cwd=str(project)) == 0:
                break
            print('  failed via %s' % name, flush=True)
        else:
            raise RuntimeError('installing the packages failed from every source')
    except BaseException:
        try:
            if os.path.lexists(venv):
                safe_remove(venv)
        finally:
            if had_old:
                if os.path.lexists(venv):
                    print('  WARNING: the old venv was left at %s; restore it by hand.' % ctx.pretty(old), flush=True)
                else:
                    os.rename(old, venv)
                    print('  old venv restored: ' + ctx.pretty(venv), flush=True)
        raise
    if had_old:
        safe_remove(old)
    return name
