"""mise as the single version manager: settings, Pythons, Node and the versions pinned in projects."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List

from ..core import CLEAN, ERROR, PENDING, Context, Detection, Item, Op, Step, plural
from ..pyenvs import GLOBAL_PYTHON, SKIP_DIRS, WANTED_PYTHONS, mise_installs, resolve_version

PIN_FILES = ('.tool-versions', '.nvmrc', '.node-version', '.python-version', 'mise.toml', '.mise.toml')
_WOULD_INSTALL = re.compile(r'(\S+@\S+)\s+\S*\s*would install')


def global_tools(ctx: Context) -> Dict[str, str]:
    """Tools in the [tools] section of the global mise config (simple TOML parsing)."""
    config = ctx.user('~/.config/mise/config.toml')
    tools: Dict[str, str] = {}
    if not config.is_file():
        return tools
    section = None
    for raw in config.read_text(errors='replace').splitlines():
        line = raw.strip()
        if line.startswith('['):
            section = line.strip('[]').strip()
            continue
        if section == 'tools' and '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            tools[key.strip().strip('"\'')] = value.strip().strip('"\'')
    return tools


def _setting_list(ctx: Context, mise: str, key: str) -> List[str]:
    result = ctx.query([mise, 'settings', 'get', key])
    return re.findall(r'[A-Za-z0-9_.-]+', result.out) if result.ok else []


def _mise_missing() -> Detection:
    return Detection(ERROR, summary='mise not found in ~/.local/bin/mise or on PATH.')


class MiseConfig(Step):
    id = 'mise-config'
    group = 'mise'
    title = 'Configure mise'
    what = ('Updates mise and turns on reading .nvmrc and .python-version files (ignored today). Turns off '
            'PHP in mise, so the .tool-versions files that pin php do not try to compile it. Turns on the '
            'integration with uv .venv folders.')
    why = ('For mise to be the only version manager, it has to respect the version files the projects '
           'already use.')
    consequences = 'Only changes ~/.config/mise/config.toml (settings) and the mise binary.'
    undo = 'mise settings unset <key>, or restore ~/.config/mise from the backup.'

    def detect(self, ctx: Context) -> Detection:
        mise = ctx.tool('mise')
        if not mise:
            return _mise_missing()
        items: List[Item] = []
        version = ctx.query([mise, 'version'])
        match = re.search(r'mise version (\S+) available', version.out + version.err)
        if match:
            current = (version.out.split() or ['?'])[0]
            items.append(Item('update', 'Update mise from %s to %s' % (current, match.group(1))))
        idiomatic = _setting_list(ctx, mise, 'idiomatic_version_file_enable_tools')
        for tool in ('node', 'python'):
            if tool not in idiomatic:
                items.append(Item('idiomatic:' + tool, 'Read %s version files (%s)' % (
                    tool, '.nvmrc and .node-version' if tool == 'node' else '.python-version')))
        if 'php' not in _setting_list(ctx, mise, 'disable_tools'):
            items.append(Item('disable:php', 'Turn off php in mise (ignore "php" in .tool-versions)'))
        auto = ctx.query([mise, 'settings', 'get', 'python.uv_venv_auto'])
        if auto.out.strip() != 'true':
            items.append(Item('uv_venv_auto', 'Activate the uv .venv automatically in projects'))
        return Detection(PENDING if items else CLEAN, items)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        mise = ctx.tool('mise') or 'mise'
        ops: List[Op] = []
        for item in items:
            if item.key == 'update':
                argv = [mise, 'self-update', '--yes']
            elif item.key.startswith('idiomatic:'):
                argv = [mise, 'settings', 'add', 'idiomatic_version_file_enable_tools', item.key.split(':')[1]]
            elif item.key == 'disable:php':
                argv = [mise, 'settings', 'add', 'disable_tools', 'php']
            elif item.key == 'uv_venv_auto':
                argv = [mise, 'settings', 'set', 'python.uv_venv_auto', 'true']
            else:
                continue
            ops.append(Op(item.label, ' '.join(['mise'] + argv[1:]), argv=argv))
        return ops


class MisePython(Step):
    id = 'mise-python'
    group = 'mise'
    title = 'Install the Pythons the projects use'
    requires = ('mise-config',)
    what = ('Installs in mise the Python versions the projects pin (3.11, 3.12 and 3.14.6). 3.12 becomes '
            'the global python.')
    why = ('These are the versions the projects pin in a version file. With them in mise, python, python3 '
           'and pip come from a single place.')
    consequences = 'Downloads prebuilt binaries (python-build-standalone) to ~/.local/share/mise.'
    undo = 'mise uninstall python@<version>; mise use -g python@<other>.'

    def detect(self, ctx: Context) -> Detection:
        if not ctx.tool('mise'):
            return _mise_missing()
        installs = mise_installs(ctx)
        items: List[Item] = []
        for wanted in WANTED_PYTHONS:
            if not resolve_version(installs, wanted):
                items.append(Item('install:' + wanted, 'Install Python ' + wanted))
        if global_tools(ctx).get('python') != GLOBAL_PYTHON:
            items.append(Item('global', 'Set Python %s as global (mise use -g)' % GLOBAL_PYTHON))
        summary = 'Installed in mise: ' + (', '.join(sorted(installs)) or 'none')
        return Detection(PENDING if items else CLEAN, items, summary=summary)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        mise = ctx.tool('mise') or 'mise'
        ops: List[Op] = []
        versions = ['python@' + i.key.split(':', 1)[1] for i in items if i.key.startswith('install:')]
        if versions:
            ops.append(Op('Install ' + ', '.join(versions), 'mise install ' + ' '.join(versions),
                          argv=[mise, 'install'] + versions))
        if any(i.key == 'global' for i in items):
            ops.append(Op('Set global Python ' + GLOBAL_PYTHON, 'mise use -g python@' + GLOBAL_PYTHON,
                          argv=[mise, 'use', '-g', 'python@' + GLOBAL_PYTHON]))
        return ops


NODE_GLOBALS = (('node', 'lts', 'Node LTS'), ('pnpm', '10', 'pnpm 10'), ('npm:yarn', '1', 'Yarn 1 (classic)'))


class MiseNode(Step):
    id = 'mise-node'
    group = 'mise'
    title = 'Set global Node, pnpm and Yarn in mise'
    requires = ('mise-config',)
    what = 'Sets Node LTS, pnpm 10 and Yarn 1 as global tools in mise.'
    why = ('Today node comes from Homebrew, pnpm from fnm and yarn from a 2022 Node 16. Projects with '
           '.nvmrc or .tool-versions keep using their pinned version.')
    consequences = 'Installs to ~/.local/share/mise. The Homebrew node stays only as a dependency of other formulas.'
    undo = 'Remove the lines from ~/.config/mise/config.toml and run mise prune.'

    def detect(self, ctx: Context) -> Detection:
        if not ctx.tool('mise'):
            return _mise_missing()
        tools = global_tools(ctx)
        items = [Item('tool:' + name, '%s (%s@%s)' % (label, name, version), data={'spec': name + '@' + version})
                 for name, version, label in NODE_GLOBALS if name not in tools]
        return Detection(PENDING if items else CLEAN, items)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        mise = ctx.tool('mise') or 'mise'
        specs = [i.data['spec'] for i in items]
        first = [s for s in specs if not s.startswith('npm:')]
        later = [s for s in specs if s.startswith('npm:')]
        ops: List[Op] = []
        # The npm backend needs node already installed, so it runs in a second command.
        for group in (first, later):
            if group:
                ops.append(Op('Global: ' + ', '.join(group), 'mise use -g ' + ' '.join(group),
                              argv=[mise, 'use', '-g'] + group))
        return ops


def pinned_dirs(ctx: Context, max_depth: int = 5) -> List[Path]:
    found: List[Path] = []
    root = ctx.projects
    if not root.is_dir():
        return found
    stack = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        names = {e.name for e in entries}
        if 'pyvenv.cfg' in names:
            continue
        if any(name in names for name in PIN_FILES) and folder != root:
            found.append(folder)
        if depth >= max_depth:
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False) and entry.name not in SKIP_DIRS and not entry.name.startswith('.venv'):
                stack.append((Path(entry.path), depth + 1))
    return sorted(found)


class MiseProjects(Step):
    id = 'mise-projects'
    group = 'mise'
    title = 'Install the versions pinned in each project'
    requires = ('mise-config', 'mise-python', 'mise-node')
    what = ('Looks for projects with .tool-versions, .nvmrc, .node-version, .python-version or mise.toml '
            'and installs the versions they ask for that are still missing (mise install in each folder).')
    why = 'That way, when you enter a project, the right node, python and pnpm are already there.'
    consequences = 'Downloads the missing versions to ~/.local/share/mise. Nothing changes in the projects.'
    undo = 'mise uninstall <tool>@<version>, or mise prune to remove the unused ones.'

    def detect(self, ctx: Context) -> Detection:
        mise = ctx.tool('mise')
        if not mise:
            return _mise_missing()
        items: List[Item] = []
        for folder in pinned_dirs(ctx):
            result = ctx.query([mise, 'install', '--dry-run'], cwd=str(folder), timeout=120)
            tools = sorted(set(_WOULD_INSTALL.findall(result.out + '\n' + result.err)))
            if tools:
                items.append(Item('dir:' + str(folder), '%s: %s' % (ctx.pretty(folder), ', '.join(tools)),
                                  path=str(folder)))
        return Detection(PENDING if items else CLEAN, items,
                         summary=plural(len(items), 'folder', 'folders') + ' with versions to install.' if items
                         else 'Everything is installed.')

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        mise = ctx.tool('mise') or 'mise'
        return [Op('Install in ' + ctx.pretty(i.path), 'cd %s && mise install' % ctx.pretty(i.path),
                   argv=[mise, 'install'], cwd=i.path) for i in items]


STEPS = [MiseConfig, MisePython, MiseNode, MiseProjects]
