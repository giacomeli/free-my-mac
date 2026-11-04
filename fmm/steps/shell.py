"""Shell: cleanup of the zsh startup files and the Cloudflare token moved to Keychain."""
from __future__ import annotations

import getpass
import os
import time
from pathlib import Path
from typing import List

from .. import shellfiles
from ..core import CLEAN, PENDING, Context, Detection, Item, Op, Step, plural
from ..fs import copy_preserving, write_atomic
from ..paths import PathsStep


def _snapshot(ctx: Context, path: Path, tag: str) -> Path:
    """Private copy of the file before it is changed."""
    folder = ctx.backup_dir() / 'shell-before'
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(ctx.backup_dir(), 0o700)
    os.chmod(folder, 0o700)
    target = folder / ('%s.%s.%s' % (path.name.lstrip('.'), tag, time.strftime('%H%M%S')))
    copy_preserving(path, target)
    os.chmod(target, 0o600)
    return target


class ShellRules(Step):
    id = 'shell-rules'
    group = 'shell'
    title = 'Clean up .zshenv, .zprofile and .zshrc'
    requires = ('backup',)
    what = ('Removes from the zsh startup files the blocks that point to removed tools (conda, mamba, fnm, '
            'Flutter, openjdk@21, Python.framework, MacPorts, OrbStack, Docker completions) and the lines that '
            'mess up the PATH (Homebrew put back in front, duplicate ~/.local/bin, repeated compinit).')
    why = 'Right now every new shell loads managers that no longer exist, and Homebrew wins over mise in the PATH.'
    consequences = ('Each rule applies only when its target is already gone; the rest of the file, including '
                    'your personal lines, stays the same. The result goes through zsh -n before it is written. '
                    'Open a new terminal afterwards.')
    undo = 'A copy from before each change is kept in ~/bkp/free-my-mac/<date>/shell-before/.'

    def detect(self, ctx: Context) -> Detection:
        rules = shellfiles.build_rules(ctx)
        plan = shellfiles.plan_edits(ctx, rules)
        items: List[Item] = []
        notes: List[str] = []
        for name, matches in plan.items():
            for match in matches:
                items.append(Item('rule:%s:%d' % (match.rule.id, match.begin),
                                  '~/%s: %s' % (name, match.rule.title), note=match.rule.reason,
                                  data={'file': name, 'rule': match.rule.id, 'begin': match.begin}))
            path = ctx.home / name
            text = path.read_text()
            new = shellfiles.apply(text, matches)
            notes.append('Diff for ~/%s:' % name)
            notes.extend('  ' + line for line in shellfiles.masked_diff(text, new, name))
        return Detection(PENDING if items else CLEAN, items, notes=notes)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        by_file = {}
        for item in items:
            by_file.setdefault(item.data['file'], set()).add((item.data['rule'], item.data['begin']))
        ops: List[Op] = []
        for name, chosen in sorted(by_file.items()):
            def edit(c: Context, name=name, chosen=frozenset(chosen)) -> None:
                path = c.home / name
                text = path.read_text()
                rules = shellfiles.build_rules(c)
                matches = [m for m in shellfiles.find_matches(c, name, text, rules)
                           if (m.rule.id, m.begin) in chosen]
                if not matches:
                    print('  nothing to change in ~/' + name, flush=True)
                    return
                new = shellfiles.apply(text, matches)
                problem = shellfiles.zsh_check(new, c.tool('zsh') or '/bin/zsh')
                if problem:
                    raise RuntimeError('zsh -n rejected the new ~/%s: %s' % (name, problem))
                copy = _snapshot(c, path, 'rules')
                write_atomic(path, new)
                print('  ~/%s: %s removed; copy at %s' % (name, plural(len(matches), 'block', 'blocks'),
                                                          c.pretty(copy)), flush=True)
            ops.append(Op('Edit ~/' + name, 'remove %s from ~/%s (zsh -n before writing)' % (
                plural(len(chosen), 'block', 'blocks'), name), func=edit))
        return ops


class CfToken(Step):
    id = 'cf-token'
    group = 'shell'
    title = 'Move the Cloudflare token to Keychain'
    requires = ('backup',)
    what = ('Moves the Cloudflare token, now written in plain text in the shell file, to the macOS Keychain. '
            'The export line reads the value from Keychain when the shell starts.')
    why = 'A plain-text token in .zshrc leaks through any backup, screenshot or dotfiles repository.'
    consequences = ('The CF_API_TOKEN and CLOUDFLARE_API_TOKEN variables still exist with the same value. '
                    'The value is never shown or written to the history. The backups in ~/bkp/free-my-mac still '
                    'contain the token; delete them when you no longer need them.')
    undo = 'security find-generic-password -a "$USER" -s cloudflare-api-token -w shows the value.'

    def detect(self, ctx: Context) -> Detection:
        items: List[Item] = []
        for name in shellfiles.SHELL_FILES:
            path = ctx.home / name
            if not path.is_file() or path.is_symlink():
                continue
            tokens = shellfiles.literal_tokens(path.read_text())
            if tokens:
                items.append(Item('file:' + name, '~/%s: %s (value hidden)' % (name, ' and '.join(sorted(tokens))),
                                  data={'file': name}))
        return Detection(PENDING if items else CLEAN, items)

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        ops: List[Op] = []
        for item in items:
            def move(c: Context, name=item.data['file']) -> None:
                path = c.home / name
                text = path.read_text()
                tokens = shellfiles.literal_tokens(text)
                if not tokens:
                    print('  no literal token in ~/' + name, flush=True)
                    return
                new = shellfiles.replace_lines(text, shellfiles.keychain_lines(tokens))
                problem = shellfiles.zsh_check(new, c.tool('zsh') or '/bin/zsh')
                if problem:
                    raise RuntimeError('zsh -n rejected the new ~/%s: %s' % (name, problem))
                account = getpass.getuser()
                for service, value in shellfiles.secrets_to_store(tokens).items():
                    shellfiles.KEYCHAIN.store(service, account, value)
                    print('  stored in Keychain: ' + service, flush=True)
                _snapshot(c, path, 'token')
                write_atomic(path, new)
                print('  ~/%s now reads the token from Keychain' % name, flush=True)
            ops.append(Op('Move the token out of ~/' + item.data['file'],
                          'security add-generic-password -U -a "$USER" -s cloudflare-api-token (value from standard input)',
                          func=move))
        return ops


OLD_SHELL = ('~/.zshrc.old', '~/.zshrc.save', '~/.zshrc.test', '~/.zshrc.pre-oh-my-zsh*', '~/.zprofile.pysave',
             '~/.zprofile.macports-saved_*', '~/.bash_profile.pysave', '~/.bash_profile.macports-saved_*')


class ShellOldFiles(PathsStep):
    id = 'shell-old-files'
    group = 'shell'
    title = 'Old copies of shell files'
    requires = ('backup',)
    targets = OLD_SHELL + ('~/.zcompdump*',)
    what = ('Removes copies that installers left behind (.zshrc.old, .save, .test, *.pre-oh-my-zsh*, '
            '.zprofile.pysave, .zprofile.macports-saved_*) and the .zcompdump* completion caches, which '
            'oh-my-zsh rebuilds in the next terminal.')
    why = 'They are dozens of files in your home folder that no shell reads.'
    consequences = 'The old copies go to the backup first; the .zcompdump files are only a cache.'
    undo = 'The copies are kept in ~/bkp/free-my-mac/<date>/shell-old/.'

    def path_note(self, ctx: Context, path: Path) -> str:
        return '' if os.access(path, os.R_OK) or path.is_symlink() else 'owned by root: the backup copy uses sudo'

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        keep = [Path(i.path) for i in items if i.path and not Path(i.path).name.startswith('.zcompdump')]
        readable = [p for p in keep if os.access(p, os.R_OK) or p.is_symlink()]
        locked = [p for p in keep if p not in readable]
        folder = ctx.backup_dir() / 'shell-old'
        ops: List[Op] = []
        if keep:
            def save(c: Context, paths=tuple(readable)) -> None:
                folder.mkdir(parents=True, exist_ok=True)
                os.chmod(c.backup_dir(), 0o700)
                os.chmod(folder, 0o700)
                for path in paths:
                    copy_preserving(path, folder / path.name)
                    os.chmod(folder / path.name, 0o600)
            ops.append(Op('Copy the old versions to the backup',
                          'cp <%s> ~/bkp/free-my-mac/<date>/shell-old/' % plural(len(readable), 'file', 'files'),
                          func=save))
        user = getpass.getuser()
        for path in locked:
            # Root-owned file the user cannot read: the copy gets the user as owner and user-only permissions.
            argv = ['/usr/bin/install', '-m', '600', '-o', user, str(path), str(folder / path.name)]
            ops.append(Op('Copy %s (owned by root) to the backup' % ctx.pretty(path),
                          'sudo install -m 600 -o %s %s %s' % (user, ctx.pretty(path), ctx.pretty(folder / path.name)),
                          argv=argv, sudo=True))
        return ops + super().ops(ctx, items)


STEPS = [ShellRules, CfToken, ShellOldFiles]
