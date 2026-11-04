"""Python: venvs on mise, conda migration, uv without its own Pythons, and old leftovers."""
from __future__ import annotations

import os
import plistlib
import re
from pathlib import Path
from typing import Dict, List, Optional

from ..core import CLEAN, MONTHS, PENDING, Context, Detection, Item, Op, Step, failing_op, inside, plural
from ..fs import copy_preserving, write_atomic, write_private
from ..launchd import USER_AGENTS, Agent, domain, read_agents, read_plist, resolve
from ..paths import PathsStep, links_into
from ..localconfig import conda_projects, uv_tools
from ..pyenvs import (GLOBAL_PYTHON, backup_recipe, conda_backups,
                      conda_envs, conda_python_version, conda_root, doomed_base, mise_installs, mise_python_root,
                      package_lines, pinned_version, python_bin, rebuild_venv, remap_editables,
                      resolve_version, slug, tracked_venvs, venv_config, venv_extras, venv_version)

PIPX_HOMES = ('~/Library/Application Support/pipx', '~/.local/pipx', '~/.local/share/pipx')


def _requirements_file(ctx: Context, name: str) -> Path:
    folder = ctx.backup_dir() / 'venvs'
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(ctx.backup_dir(), 0o700)
    return folder / (name + '.recreate.txt')


def _save(path: Path, lines: List[str]) -> None:
    if path.exists():
        path.unlink()
    write_private(path, '\n'.join(lines) + '\n')


class Venvs(Step):
    id = 'venvs'
    group = 'python'
    title = 'Rebuild venvs on the mise Pythons'
    requires = ('mise-python', 'backup')
    what = ('Finds project .venv folders whose base Python is not from mise (Miniforge, Homebrew or uv), and '
            'app venvs in ~/Library/Application Support whose base a step removes, and rebuilds each one '
            'with the mise Python, installing exactly the same packages.')
    why = ('These venvs break when their base Python is removed. Rebuilding them first lets you remove conda '
           'and the Homebrew Pythons without losing an environment.')
    consequences = ('The old venv is renamed and only deleted after the new one installs everything. If the '
                    'install fails, the old one comes back and the step is marked Partial. Projects with '
                    '.python-version use that version; the others use Python %s.' % GLOBAL_PYTHON)
    undo = 'The package list of each venv stays in the backup (venvs/*.recreate.txt).'

    def detect(self, ctx: Context) -> Detection:
        installs = mise_installs(ctx)
        root = mise_python_root(ctx)
        items: List[Item] = []
        for venv, is_app in tracked_venvs(ctx):
            config = venv_config(venv)
            home = Path(config.get('home', ''))
            if config.get('home') and inside(home, root):
                continue
            project = venv.parent
            wanted, source = pinned_version(ctx, project) if not is_app else (None, '')
            if is_app:
                # App venv: keep the minor version it already uses.
                current = venv_version(config)
                wanted = '.'.join(current.split('.')[:2]) if current[:1].isdigit() else None
            wanted = wanted or GLOBAL_PYTHON
            resolved = resolve_version(installs, wanted)
            base_alive = (home / 'python3').exists() or (home / 'python').exists()
            use_lock = (project / 'uv.lock').is_file() and venv.name == '.venv'
            source_file = None if base_alive else (
                'requirements.txt' if (project / 'requirements.txt').is_file() else None)
            sources = (['uv.lock'] if use_lock else []) + (['same packages'] if base_alive else []) + \
                ([source_file] if source_file else [])
            note = 'current base: %s (%s)' % (ctx.pretty(home) if config.get('home') else '?', venv_version(config))
            selected = True
            if not resolved:
                note += '; Python %s is not in mise' % wanted
                selected = False
            elif not sources:
                note += '; base removed and no uv.lock or requirements.txt: rebuild it by hand'
                selected = False
            extras = venv_extras(venv)
            if extras:
                note += '; the folder has files that are not part of a venv (%s): rebuild it by hand' % ', '.join(extras[:3])
                selected = False
                sources = []
            if is_app:
                note += '; app venv, outside the projects folder'
            label = '%s -> Python %s%s' % (ctx.pretty(venv), resolved[0] if resolved else wanted,
                                           ' (%s)' % source if source else '')
            items.append(Item('venv:' + str(venv), label, path=str(venv), note=note, selected=selected,
                              size=ctx.du(venv),
                              data={'python': str(python_bin(resolved[1])) if resolved else None,
                                    'use_lock': use_lock, 'list': base_alive, 'sources': sources}))
        return Detection(PENDING if items else CLEAN, items,
                         summary=(plural(len(items), 'venv', 'venvs') + ' outside mise.') if items
                         else 'All venvs use a mise Python.')

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        ops: List[Op] = []
        unhandled: List[str] = []
        for item in items:
            venv, python, sources = Path(item.path), item.data['python'], item.data['sources']
            if not python or not sources:
                unhandled.append('%s (%s)' % (ctx.pretty(venv), item.note))
                continue

            def recreate(c: Context, venv=venv, python=python, data=item.data) -> None:
                requirements = None
                if data['list']:
                    requirements = _requirements_file(c, slug(c, venv))
                    _save(requirements, package_lines(c, venv / 'bin/python'))
                elif (venv.parent / 'requirements.txt').is_file():
                    requirements = venv.parent / 'requirements.txt'
                used = rebuild_venv(c, venv, Path(python), requirements, use_lock=data['use_lock'],
                                    prefer_list=data['list'])
                print('  done via %s' % used, flush=True)

            ops.append(Op('Rebuild ' + ctx.pretty(venv),
                          'uv venv --python %s %s && install via %s (rollback on failure)' % (
                              ctx.pretty(python), ctx.pretty(venv), ' or '.join(sources)),
                          func=recreate))
        if unhandled:
            ops.append(failing_op('Venvs that cannot be rebuilt automatically',
                                  'rebuild them by hand: ' + '; '.join(unhandled)))
        return ops


class CondaMigrate(Step):
    id = 'conda-migrate'
    group = 'python'
    title = 'Migrate conda environments to uv'
    requires = ('mise-python', 'backup')
    what = ('Creates the .venv of each project mapped in conda_projects (~/.config/free-my-mac/local.json) with '
            'the packages of the matching conda env, and installs each conda env or pipx venv listed in '
            'uv_tools as a uv tool. The other envs stay exported in the backup. If the env was already '
            'removed, it rebuilds the .venv from the package list saved in the backup.')
    why = 'This is what lets you remove Miniforge without losing the environments you use.'
    consequences = ('Tries the env package list first, then the same list without resolving dependencies '
                    '(copying the environment as it was), and finally the project pyproject.toml. Editable '
                    'packages whose folder moved are looked up by name in ~/Projects. Dependencies that came '
                    'from conda and are not on PyPI (such as ffmpeg) do not come back: use the Homebrew ones. '
                    'Tools come from the source set in uv_tools.')
    undo = 'Delete the created .venv and run uv tool uninstall <tool>.'

    def detect(self, ctx: Context) -> Detection:
        envs = conda_envs(ctx)
        backups = conda_backups(ctx)
        installs = mise_installs(ctx)
        items: List[Item] = []
        projects = conda_projects(ctx)
        tools_wanted = uv_tools(ctx)
        for env, relative in projects.items():
            project = ctx.projects / relative
            if not project.is_dir():
                continue
            venv = project / '.venv'
            config = venv_config(venv) if venv.is_dir() else {}
            if config and inside(Path(config.get('home', '')), mise_python_root(ctx)):
                continue
            if env in envs:
                version = conda_python_version(envs[env])
                key, origin, data = 'env:' + env, env, {'env': str(envs[env])}
                extra = '' if version else 'could not identify the Python version of the env'
            elif env in backups and not os.path.lexists(venv):
                folder = backups[env]
                version, others = backup_recipe(folder, env)
                key = 'restore:' + env
                origin = '%s (from the %s backup)' % (env, _date_label(folder.parent.name))
                data = {'packages': str(folder / (env + '.packages.txt'))}
                extra = ('from conda and not on PyPI, install separately: ' + ', '.join(others)) if others else ''
            else:
                continue
            resolved = resolve_version(installs, version) if version else None
            notes = [n for n in (extra, '' if resolved or not version else 'Python %s is not in mise' % version) if n]
            data.update(python=str(python_bin(resolved[1])) if resolved else None, project=str(project),
                        version=version)
            items.append(Item(key, '%s -> %s/.venv (Python %s)' % (origin, ctx.pretty(project), version or '?'),
                              path=str(venv), selected=bool(resolved), note='; '.join(notes), data=data))
        uv = ctx.tool('uv')
        for name, source in sorted(tools_wanted.items()):
            known = name in envs or any((ctx.user(h) / 'venvs' / name).is_dir() for h in PIPX_HOMES)
            if not (known and uv):
                continue
            tools = ctx.query([uv, 'tool', 'list'])
            if re.search(r'^%s\b' % re.escape(name), tools.out, re.M):
                continue
            resolved = resolve_version(installs, '3.11') or resolve_version(installs, GLOBAL_PYTHON)
            items.append(Item('tool:' + name, '%s -> uv tool (%s)' % (name, source), selected=bool(resolved),
                              data={'python': str(python_bin(resolved[1])) if resolved else None,
                                    'tool': name, 'source': source}))
        others = [n for n in envs if n not in projects and n not in tools_wanted]
        notes = ['Only in the backup (recipe, explicit list and packages): ' + ', '.join(others)] if others else []
        return Detection(PENDING if items else CLEAN, items, notes=notes)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        uv = ctx.tool('uv') or 'uv'
        ops: List[Op] = []
        unhandled: List[str] = []
        for item in items:
            if not item.data.get('python'):
                unhandled.append('%s (%s)' % (item.label, item.note or 'no target Python'))
                continue
            if item.key.startswith('tool:'):
                source = item.data['source']
                argv = [uv, 'tool', 'install', '--force', '--python', item.data['python'], source]
                ops.append(Op('Install %s with uv tool' % item.data['tool'], 'uv tool install --force --python %s %s'
                              % (ctx.pretty(item.data['python']), source), argv=argv))
                continue
            name = item.key.split(':', 1)[1]

            def migrate(c: Context, item=item, name=name) -> None:
                requirements = _requirements_file(c, 'conda-' + name)
                if item.key.startswith('restore:'):
                    lines = [l.strip() for l in Path(item.data['packages']).read_text(errors='replace').splitlines()
                             if l.strip() and not l.startswith('#')]
                else:
                    lines = package_lines(c, Path(item.data['env']) / 'bin/python')
                lines, warnings = remap_editables(c, lines)
                for warning in warnings:
                    print('  ' + warning, flush=True)
                _save(requirements, lines)
                used = rebuild_venv(c, Path(item.path), Path(item.data['python']), requirements,
                                    fallback_project=Path(item.data['project']), allow_no_deps=True)
                print('  done via %s' % used, flush=True)
            source = 'backup list' if item.key.startswith('restore:') else 'env packages'
            ops.append(Op('Create ' + ctx.pretty(item.path),
                          'uv venv --python %s %s && uv pip install (%s; --no-deps; pyproject)'
                          % (ctx.pretty(item.data['python']), ctx.pretty(item.path), source), func=migrate))
        if unhandled:
            ops.append(failing_op('Environments without a target Python', 'install the version in mise '
                                  '(mise-python) and run again: ' + '; '.join(unhandled)))
        return ops


def _date_label(name: str) -> str:
    """'2026-11-01' -> 'Nov 01', the same style as format_when."""
    parts = name.split('-')
    if len(parts) == 3 and parts[1].isdigit() and 1 <= int(parts[1]) <= 12:
        return '%s %s' % (MONTHS[int(parts[1]) - 1], parts[2])
    return name


class CondaRemove(PathsStep):
    id = 'conda-remove'
    group = 'python'
    title = 'Remove conda, mamba and Miniforge'
    requires = ('backup', 'conda-migrate', 'venvs')
    targets = ('~/miniforge3', '~/miniconda3', '~/anaconda3', '~/.conda', '~/.mamba', '~/.condarc',
               '~/Miniforge3.sh', '~/.continuum')
    what = 'Deletes Miniforge (conda, mamba, the base Python 3.12 and the 7 environments) and the conda settings.'
    why = ('It was the second Python manager active in the shell: python came from it and python3 from '
           'Homebrew. The envs in use were already migrated, and all of them are exported in the backup.')
    consequences = ('The conda environments stop existing. The "conda initialize" and "mamba initialize" '
                    'blocks in .zshrc are removed in the Shell step.')
    undo = 'Reinstall Miniforge and rebuild the envs from ~/bkp/free-my-mac/<date>/conda/.'

    def detect(self, ctx: Context) -> Detection:
        det = super().detect(ctx)
        root = conda_root(ctx)
        if root and det.items:
            unmigrated = [i.key.split(':', 1)[1] for i in CondaMigrate().detect(ctx).items if i.key.startswith('env:')]
            if unmigrated:
                det.notes.append('The "Migrate conda environments to uv" step has not created the .venv of: '
                                 + ', '.join(unmigrated))
                for item in det.items:
                    item.selected = False
                det.summary = 'Some project envs are not migrated yet; nothing is checked.'
            dependent = [v for v, _app in tracked_venvs(ctx)
                         if inside(Path(venv_config(v).get('home', '/')), root)]
            if dependent:
                det.notes.append('These still use the Miniforge Python and break when it is removed (rebuild '
                                 'them in the "Rebuild venvs on the mise Pythons" step or by hand): '
                                 + ', '.join(ctx.pretty(v) for v in dependent))
                for item in det.items:
                    item.selected = False
                det.summary = '%s still %s on Miniforge; nothing is checked.' % (
                    plural(len(dependent), 'venv', 'venvs'), 'depends' if len(dependent) == 1 else 'depend')
        return det


class PipxRemove(PathsStep):
    id = 'pipx-remove'
    group = 'python'
    title = 'Remove pipx'
    requires = ('conda-migrate',)
    formulas = ('pipx',)
    targets = PIPX_HOMES
    what = ('Uninstalls pipx from Homebrew, its folder (~/Library/Application Support/pipx) and the links '
            'it created in ~/.local/bin.')
    why = 'Its tools now come from uv tool (uv_tools in local.json). One less tool for Python CLIs.'
    consequences = 'Commands installed by pipx go away; the ones listed in uv_tools keep working through uv.'
    undo = 'brew install pipx.'

    def extra_paths(self, ctx: Context) -> List[Path]:
        homes = tuple(str(ctx.user(h)) + '/' for h in PIPX_HOMES)
        return links_into(ctx, ['~/.local/bin'], lambda target: target.startswith(homes))


class UvOnlyMise(Step):
    id = 'uv-only-mise'
    group = 'python'
    title = 'Make uv use only the mise Pythons'
    requires = ('mise-python', 'venvs', 'conda-migrate')
    what = ('Configures uv to never download its own Pythons (python-preference = "only-system", '
            'python-downloads = "never") and removes the Pythons it already downloaded.')
    why = 'Without this, uv quietly becomes a second Python version manager.'
    consequences = ('A project that asks for a version missing from mise fails in uv with "no interpreter '
                    'found"; fix it with mise install or mise use.')
    undo = 'Delete ~/.config/uv/uv.toml (or the two keys).'

    SETTINGS = (('python-preference', '"only-system"'), ('python-downloads', '"never"'))

    def detect(self, ctx: Context) -> Detection:
        items: List[Item] = []
        config = ctx.user('~/.config/uv/uv.toml')
        text = config.read_text(errors='replace') if config.is_file() else ''
        missing = [k for k, v in self.SETTINGS if not re.search(r'^\s*%s\s*=\s*%s\s*$' % (re.escape(k), re.escape(v)), text, re.M)]
        if missing:
            items.append(Item('config', 'Write to ~/.config/uv/uv.toml: ' + ', '.join(missing)))
        managed = ctx.user('~/.local/share/uv/python')
        if managed.is_dir():
            for entry in sorted(managed.iterdir()):
                if entry.is_dir() and entry.name.startswith(('cpython-', 'pypy-', 'graalpy-')):
                    items.append(Item('managed:' + entry.name, 'uv Python: ' + entry.name, path=str(entry),
                                      size=ctx.du(entry)))
        return Detection(PENDING if items else CLEAN, items)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        ops: List[Op] = []
        if any(i.key == 'config' for i in items):
            def write_config(c: Context) -> None:
                config = c.user('~/.config/uv/uv.toml')
                config.parent.mkdir(parents=True, exist_ok=True)
                lines = config.read_text().splitlines() if config.is_file() else []
                for key, value in self.SETTINGS:
                    lines = [l for l in lines if not re.match(r'^\s*%s\s*=' % re.escape(key), l)]
                    lines.append('%s = %s' % (key, value))
                if config.is_file():
                    write_atomic(config, '\n'.join(lines) + '\n')
                else:
                    config.write_text('\n'.join(lines) + '\n')
            ops.append(Op('Configure uv', 'uv.toml: python-preference = "only-system", python-downloads = "never"',
                          func=write_config))
        names = [i.key.split(':', 1)[1] for i in items if i.key.startswith('managed:')]
        if names:
            uv = ctx.tool('uv') or 'uv'
            managed = ctx.user('~/.local/share/uv/python')
            installed = [e.name for e in managed.iterdir() if e.is_dir() and e.name.startswith(
                ('cpython-', 'pypy-', 'graalpy-'))] if managed.is_dir() else []
            targets = ['--all'] if set(installed) <= set(names) else names
            ops.append(Op('Remove the Pythons downloaded by uv', 'uv python uninstall ' + ' '.join(targets),
                          argv=[uv, 'python', 'uninstall'] + targets))
        return ops


COMPILE_CHECK = 'import sys; compile(open(sys.argv[1]).read(), sys.argv[1], "exec")'


def _python_problem(ctx: Context, program: str) -> Optional[str]:
    """Why the agent's interpreter breaks (or will break); None if it is fine."""
    path = resolve(ctx, program)
    if not os.path.exists(path):
        return 'the interpreter no longer exists'
    if inside(path, ctx.brew_prefix() / 'bin'):
        return 'Homebrew link that leaves PATH in the "Remove the Homebrew Pythons from PATH" step'
    if doomed_base(ctx, path):
        return 'it lives in an install that a step removes'
    return None


def _replacement_python(ctx: Context, installs: Dict[str, Path], name: str) -> Optional[Path]:
    """mise version alias (installs/python/3.N): same minor version, otherwise the global one."""
    match = re.match(r'python(\d+\.\d+)', name)
    for version in ([match.group(1)] if match else []) + [GLOBAL_PYTHON]:
        resolved = resolve_version(installs, version)
        if resolved:
            alias = mise_python_root(ctx) / version / 'bin/python3'
            return alias if os.path.exists(alias) else python_bin(resolved[1])
    return None


def _files_citing(folder: Optional[Path], old: str) -> List[Path]:
    if folder is None or not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.glob('*.py')):
        try:
            if old in path.read_text(errors='replace'):
                found.append(path)
        except OSError:
            continue
    return found


class LaunchdPython(Step):
    id = 'launchd-python'
    group = 'python'
    title = 'Fix launchd agents with a removed Python'
    requires = ('mise-python',)
    what = ('Looks for agents in ~/Library/LaunchAgents that run a Python that no longer exists, or one that '
            'a step removes or takes off PATH (Homebrew links, conda, uv, pyenv, /usr/local, Python.org). '
            'Replaces it with the mise Python of the same minor version, or with %s (global), in the plist '
            'and in the .py files in the script folder that mention the old path.' % GLOBAL_PYTHON)
    why = 'Without this, the agent silently stops starting: launchd only logs exit code 78 (EX_CONFIG).'
    consequences = ('Uses the mise version alias (installs/python/3.N), which follows patch updates. It only '
                    'switches if the script compiles on the new Python. The previous copies stay in the '
                    'backup and the agent is reloaded.')
    undo = ('Copy the files from ~/bkp/free-my-mac/<date>/launchd-before/ back and run launchctl bootout and '
            'launchctl bootstrap gui/$(id -u) <plist>.')

    def detect(self, ctx: Context) -> Detection:
        installs = mise_installs(ctx)
        items: List[Item] = []
        for agent in read_agents(ctx, [USER_AGENTS]):
            if not agent.is_python():
                continue
            old = str(agent.program)
            reason = _python_problem(ctx, old)
            if not reason:
                continue
            new = _replacement_python(ctx, installs, os.path.basename(old))
            args = [str(a) for a in agent.data.get('ProgramArguments') or []]
            script = next((a for a in args[1:] if a.endswith('.py')), None)
            files = _files_citing(resolve(ctx, script).parent if script else None, old)
            notes = [reason]
            selected = new is not None
            if new is None:
                notes.append('no mise Python installed')
            elif script and resolve(ctx, script).is_file():
                check = ctx.query([str(new), '-I', '-c', COMPILE_CHECK, str(resolve(ctx, script))], cache=False)
                if not check.ok:
                    notes.append('the script does not compile on the new Python: ' +
                                 ((check.err.strip().splitlines() or ['?'])[-1])[:160])
                    selected = False
            if files:
                notes.append('these also mention the old path: ' + ', '.join(f.name for f in files))
            items.append(Item('agent:' + str(agent.plist), '%s: %s -> %s' % (
                agent.label, ctx.pretty(old), ctx.pretty(new) if new else '?'), path=str(agent.plist),
                note='; '.join(notes), selected=selected,
                data={'old': old, 'new': str(new) if new else None, 'files': [str(f) for f in files],
                      'label': agent.label}))
        return Detection(PENDING if items else CLEAN, items,
                         summary='' if items else 'No agent points to a removed Python.')

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        ops: List[Op] = []
        unhandled: List[str] = []
        for item in items:
            if not item.data.get('new'):
                unhandled.append(item.data['label'])
                continue

            def repair(c: Context, item=item) -> None:
                plist, old, new = Path(item.path), item.data['old'], item.data['new']
                files = [Path(f) for f in item.data['files']]
                folder = c.backup_dir() / 'launchd-before'
                folder.mkdir(parents=True, exist_ok=True)
                os.chmod(c.backup_dir(), 0o700)
                os.chmod(folder, 0o700)
                for source, name in [(plist, plist.name)] + [(f, '%s.%s' % (plist.stem, f.name)) for f in files]:
                    copy_preserving(source, folder / name)
                    os.chmod(folder / name, 0o600)
                data = read_plist(plist)
                if data is None:
                    raise RuntimeError('unreadable plist: ' + c.pretty(plist))
                changed = False
                if data.get('Program') == old:
                    data['Program'] = new
                    changed = True
                args = data.get('ProgramArguments')
                if isinstance(args, list) and args and args[0] == old:
                    args[0] = new
                    changed = True
                if not changed:
                    raise RuntimeError('the plist no longer points to ' + old)
                write_atomic(plist, plistlib.dumps(data))
                for path in files:
                    text = path.read_text()
                    if old in text:
                        write_atomic(path, text.replace(old, new))
                print('  %s now uses %s; copies in %s' % (c.pretty(plist), c.pretty(new), c.pretty(folder)),
                      flush=True)
                if c.real_system:
                    target = domain(Agent(plist, item.data['label']))
                    c.call(['/bin/launchctl', 'bootout', '%s/%s' % (target, item.data['label'])])
                    if c.call(['/bin/launchctl', 'bootstrap', target, str(plist)]):
                        raise RuntimeError('launchctl bootstrap failed; run: launchctl bootstrap %s %s'
                                           % (target, plist))
            extra = ' and ' + ', '.join(Path(f).name for f in item.data['files']) if item.data['files'] else ''
            ops.append(Op('Fix the agent ' + item.data['label'],
                          'plist%s: %s -> %s; launchctl bootout and bootstrap gui/<uid>' % (
                              extra, ctx.pretty(item.data['old']), ctx.pretty(item.data['new'])), func=repair))
        if unhandled:
            ops.append(failing_op('Agents without a target Python',
                                  'install a Python in mise (mise-python): ' + ', '.join(unhandled)))
        return ops


class BrewPythonUnlink(Step):
    id = 'brew-python-unlink'
    group = 'python'
    title = 'Remove the Homebrew Pythons from PATH'
    requires = ('mise-python', 'venvs', 'launchd-python')
    what = ('Unlinks (brew unlink) the Homebrew python@3.x formulas. They stay installed, because other '
            'formulas depend on them (each item lists which), but they leave /opt/homebrew/bin.')
    why = 'Today python3 and pip come from Homebrew and python comes from conda. After this, all three come from mise.'
    consequences = ('The dependent formulas keep working (they use the opt/ path). A brew upgrade can relink '
                    'one of them; if that happens, this step shows up as Pending again.')
    undo = 'brew link python@3.x.'

    def detect(self, ctx: Context) -> Detection:
        brew = ctx.tool('brew')
        linked_dir = ctx.brew_prefix() / 'var/homebrew/linked'
        items: List[Item] = []
        for formula in ctx.brew_formulas():
            if not formula.startswith('python@'):
                continue
            if not os.path.lexists(linked_dir / formula):
                continue
            uses = ctx.query([brew, 'uses', '--installed', formula], timeout=120) if brew else None
            users = ', '.join(uses.out.split()) if uses and uses.ok and uses.out.strip() else 'none'
            items.append(Item('formula:' + formula, formula + ' (linked)', note='used by: ' + users))
        return Detection(PENDING if items else CLEAN, items)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        formulas = [i.key.split(':', 1)[1] for i in items]
        return [Op('Unlink ' + ', '.join(formulas), 'brew unlink ' + ' '.join(formulas),
                   argv=[ctx.tool('brew') or 'brew', 'unlink'] + formulas)]


class PythonLeftovers(PathsStep):
    id = 'python-leftovers'
    group = 'python'
    title = 'Remove Python.org and pyenv leftovers'
    requires = ('venvs',)
    targets = ('/Library/Frameworks/Python.framework', '/Applications/Python 3.*', '~/.pyenv')
    what = ('Removes Python.framework (from the python.org installer, already half removed), the '
            '"Python 3.x" folders in Applications, its links in /usr/local/bin and ~/.pyenv, which has no '
            'versions.')
    why = 'They are unused leftovers that still show up in the PATH set by .zprofile.'
    consequences = 'The .zprofile lines that point to Python.framework are removed in the Shell step.'
    undo = 'Reinstall from python.org if you need it.'

    def extra_paths(self, ctx: Context) -> List[Path]:
        return links_into(ctx, ['/usr/local/bin'], lambda target: 'Python.framework' in target)


STEPS = [Venvs, CondaMigrate, CondaRemove, PipxRemove, UvOnlyMise, LaunchdPython, BrewPythonUnlink,
         PythonLeftovers]
