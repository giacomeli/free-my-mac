"""Node: global CLIs moved to mise, fnm, the old Node 16 and package manager leftovers."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from ..core import CLEAN, PENDING, Context, Detection, Item, Op, Step, plural
from ..paths import PathsStep, links_into
from .mise import global_tools

FNM_DIRS = ('~/.local/share/fnm', '~/Library/Application Support/fnm', '~/.fnm')
SKIP_PACKAGES = {'npm', 'corepack'}
# fnm packages already covered by Homebrew formulas (package -> formula).
BREW_EQUIVALENTS = {'@google/gemini-cli': 'gemini-cli', 'opencode-ai': 'opencode'}


def fnm_globals(ctx: Context) -> Dict[str, List[str]]:
    """Global package -> Node versions (fnm) it shows up in."""
    found: Dict[str, List[str]] = {}
    for spec in FNM_DIRS:
        versions = ctx.user(spec) / 'node-versions'
        if not versions.is_dir():
            continue
        for version in sorted(versions.iterdir()):
            modules = version / 'installation/lib/node_modules'
            if not modules.is_dir():
                continue
            for entry in sorted(modules.iterdir()):
                names = [entry.name] if not entry.name.startswith('@') else \
                    [entry.name + '/' + sub.name for sub in sorted(entry.iterdir()) if sub.is_dir()]
                for name in names:
                    if name not in SKIP_PACKAGES:
                        found.setdefault(name, []).append(version.name)
    return found


def _has_bin(ctx: Context, name: str, versions: List[str]) -> bool:
    for spec in FNM_DIRS:
        manifest = ctx.user(spec) / 'node-versions' / versions[0] / 'installation/lib/node_modules' / name / 'package.json'
        if manifest.is_file():
            try:
                return bool(json.loads(manifest.read_text()).get('bin'))
            except ValueError:
                return False
    return False


class NodeGlobals(Step):
    id = 'node-globals'
    group = 'node'
    title = 'Move global fnm CLIs to mise'
    requires = ('mise-node',)
    what = ('Lists the npm packages installed globally in the fnm versions and reinstalls in mise (npm '
            'backend) the ones that are commands and do not exist anywhere else.')
    why = ('In mise, a global CLI (mise use -g npm:<package>) does not depend on the Node version active in '
           'the project. That is what lets you remove fnm without losing commands.')
    consequences = ("Packages that already come from Homebrew (gemini-cli, opencode) or from Homebrew's node, "
                    'and libraries without a command, are not reinstalled; they show up under Notes.')
    undo = 'mise unuse -g npm:<package>.'

    def detect(self, ctx: Context) -> Detection:
        packages = fnm_globals(ctx)
        if not packages:
            return Detection(CLEAN, summary='No global packages in fnm.')
        tools = global_tools(ctx)
        brew_modules = ctx.brew_prefix() / 'lib/node_modules'
        items: List[Item] = []
        notes: List[str] = []
        for name, versions in sorted(packages.items()):
            if 'npm:' + name in tools:
                continue
            if (brew_modules / name).is_dir():
                notes.append("%s: already in Homebrew's node" % name)
            elif name in BREW_EQUIVALENTS and BREW_EQUIVALENTS[name] in ctx.brew_formulas():
                notes.append('%s: already provided by the Homebrew formula %s' % (name, BREW_EQUIVALENTS[name]))
            elif not _has_bin(ctx, name, versions):
                notes.append('%s: library without a command, not reinstalled' % name)
            else:
                items.append(Item('pkg:' + name, '%s (in fnm: Node %s)' % (name, ', '.join(versions))))
        summary = '%s to reinstall.' % plural(len(items), 'CLI', 'CLIs') if items else 'Nothing to reinstall.'
        return Detection(PENDING if items else CLEAN, items, summary=summary, notes=notes)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        specs = ['npm:' + i.key[4:] for i in items]
        return [Op('Install in mise: ' + ', '.join(specs), 'mise use -g ' + ' '.join(specs),
                   argv=[ctx.tool('mise') or 'mise', 'use', '-g'] + specs)]


class Fnm(PathsStep):
    id = 'fnm'
    group = 'node'
    title = 'Remove fnm'
    requires = ('mise-node', 'node-globals')
    formulas = ('fnm',)
    targets = FNM_DIRS + ('~/.local/state/fnm_multishells',)
    what = 'Uninstalls fnm and deletes its 6 Node versions and the thousands of session links (fnm_multishells).'
    why = ('fnm was active but never won in the PATH: .zshrc put Homebrew back in front of it. mise now reads '
           'the .nvmrc files.')
    consequences = "The \"fnm env\" line in .zshrc is removed in the Shell step. fnm's Node versions go away."
    undo = 'brew install fnm.'


class Node16(PathsStep):
    id = 'node16'
    group = 'node'
    title = 'Remove the old Node 16 (pkg installer)'
    targets = ('/usr/local/lib/node_modules', '/usr/local/include/node', '/usr/local/share/doc/node',
               '/usr/local/share/man/man1/node.1', '/usr/local/share/systemtap/tapset/node.stp')
    receipts = r'^org\.nodejs\.'
    what = ('Deletes Node 16.14, installed by the nodejs.org pkg in 2022 (/usr/local/bin/node and '
            '/usr/local/lib/node_modules), and its global commands: yarn, corepack, ionic, cordova-res, '
            'gulp, react-native-cli and others.')
    why = 'It is where the shell gets yarn and corepack from today. Outdated and outside any version manager.'
    consequences = 'These old global commands go away; yarn now comes from mise.'
    undo = 'Reinstall it from the nodejs.org pkg (not recommended).'

    def extra_paths(self, ctx: Context) -> List[Path]:
        paths: List[Path] = []
        node = ctx.sys('/usr/local/bin/node')
        if node.is_file() and not node.is_symlink():
            paths.append(node)
        modules = str(ctx.sys('/usr/local/lib/node_modules'))
        paths.extend(links_into(ctx, ['/usr/local/bin'], lambda target: target.startswith(modules)))
        return paths


class NodeLeftovers(PathsStep):
    id = 'node-leftovers'
    group = 'node'
    title = 'Leftovers of bun, Yarn, pnpm and the npx cache'
    requires = ('mise-node',)
    targets = ('~/.npm/_npx', '~/Library/pnpm', '~/.bun', '~/.yarn', '~/.yarnrc')
    what = ('Deletes the npx cache (packages downloaded by "npx <command>"), the global pnpm directory, a bun '
            'outside the PATH, a loose Yarn install and ~/.yarnrc, which contains a lockfile by mistake.')
    why = 'The npx cache alone is over 4 GB; the rest is left over from old installs.'
    consequences = 'The next npx run of each package downloads it again. The pnpm store is rebuilt on the next install.'
    undo = 'Everything is rebuilt on its own the next time it is used.'


STEPS = [NodeGlobals, Fnm, Node16, NodeLeftovers]
