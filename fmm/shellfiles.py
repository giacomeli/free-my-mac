"""Cleanup rules for shell files: exact blocks, conditions, masked diff and zsh -n."""
from __future__ import annotations

import difflib
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Pattern, Sequence, Tuple, Union

from .core import Context, redact

LineSpec = Union[str, Pattern]


@dataclass
class Rule:
    id: str
    file: str
    title: str
    reason: str
    lines: Tuple[LineSpec, ...] = ()
    start: Optional[str] = None
    end: Optional[str] = None
    condition: Callable[[Context, str], bool] = field(default=lambda ctx, text: True)


@dataclass
class Match:
    rule: Rule
    file: str
    begin: int
    end: int  # exclusive


def _same(spec: LineSpec, line: str) -> bool:
    text = line.strip()
    if isinstance(spec, str):
        return spec.strip() == text
    return bool(spec.fullmatch(text))


def _marker_regions(lines: List[str], rules: Sequence[Rule]) -> List[Tuple[int, int, Rule]]:
    regions = []
    for rule in rules:
        if not rule.start:
            continue
        i = 0
        while i < len(lines):
            if lines[i].strip() == rule.start:
                for j in range(i + 1, len(lines)):
                    if lines[j].strip() == rule.end:
                        regions.append((i, j + 1, rule))
                        i = j
                        break
            i += 1
    return regions


def find_matches(ctx: Context, file: str, text: str, rules: Sequence[Rule]) -> List[Match]:
    """Blocks in this file whose rules apply now. Plain blocks do not match inside marked blocks."""
    lines = text.splitlines()
    own = [r for r in rules if r.file == file]
    regions = _marker_regions(lines, own)
    matches: List[Match] = []
    for begin, end, rule in regions:
        if rule.condition(ctx, text):
            matches.append(Match(rule, file, begin, end))
    for rule in own:
        if rule.start or not rule.lines:
            continue
        size = len(rule.lines)
        i = 0
        while i + size <= len(lines):
            if all(_same(rule.lines[k], lines[i + k]) for k in range(size)):
                inside_marker = any(b <= i < e for b, e, _ in regions)
                if not inside_marker and rule.condition(ctx, text):
                    matches.append(Match(rule, file, i, i + size))
                i += size
            else:
                i += 1
    matches.sort(key=lambda m: m.begin)
    return matches


def apply(text: str, matches: Sequence[Match]) -> str:
    """Removes the blocks; collapses the blank line that would be left doubled where they join."""
    lines = text.splitlines()
    drop = set()
    for m in matches:
        drop.update(range(m.begin, m.end))
    for m in matches:
        before = m.begin - 1
        while before >= 0 and before in drop:
            before -= 1
        after = m.end
        while after < len(lines) and after in drop:
            after += 1
        before_blank = before < 0 or not lines[before].strip()
        if before_blank and after < len(lines) and not lines[after].strip():
            drop.add(after)
    kept = [line for i, line in enumerate(lines) if i not in drop]
    return '\n'.join(kept) + ('\n' if text.endswith('\n') and kept else '')


def masked_diff(old: str, new: str, name: str) -> List[str]:
    diff = difflib.unified_diff(old.splitlines(), new.splitlines(), 'before/' + name, 'after/' + name,
                                n=1, lineterm='')
    return [redact(line) for line in diff]


def zsh_check(text: str, zsh: str = '/bin/zsh') -> Optional[str]:
    """None if zsh accepts the syntax; otherwise the error message (masked)."""
    fd, path = tempfile.mkstemp(prefix='fmm-zsh-', suffix='.zsh')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
        proc = subprocess.run([zsh, '-n', path], capture_output=True, text=True, timeout=20,
                              stdin=subprocess.DEVNULL)
        return None if proc.returncode == 0 else redact(proc.stderr.strip() or 'zsh -n failed')
    finally:
        os.unlink(path)


def _missing(*paths: str) -> Callable[[Context, str], bool]:
    def check(ctx: Context, _text: str) -> bool:
        return not any(os.path.lexists(ctx.expand(p)) for p in paths)
    return check


def _fnm_absent(ctx: Context, _text: str) -> bool:
    return 'fnm' not in ctx.brew_formulas() and not os.path.lexists(ctx.user('~/.local/share/fnm'))


def build_rules(ctx: Context) -> List[Rule]:
    home = str(ctx.home)
    openjdk = ('export PATH="/opt/homebrew/opt/openjdk@21/bin:$PATH"',
               'export CPPFLAGS="-I/opt/homebrew/opt/openjdk@21/include"',
               'export JAVA_HOME="/opt/homebrew/opt/openjdk@21"')
    openjdk_gone = _missing('/opt/homebrew/opt/openjdk@21')
    rules = [
        Rule('zshenv-flutter', '.zshenv', 'Flutter PATH', 'The ~/flutter folder does not exist.',
             lines=('export PATH=$HOME/flutter/bin:$PATH',), condition=_missing('~/flutter')),
        Rule('zshenv-openjdk-comment', '.zshenv', 'openjdk@21 comment',
             'Leftover from the openjdk@21 block, which was removed.',
             lines=('# Adicione ao seu shell (ex: ~/.zshrc ou ~/.bashrc)',), condition=openjdk_gone),
        Rule('zshenv-openjdk', '.zshenv', 'openjdk@21 (PATH, CPPFLAGS, JAVA_HOME)',
             'The Homebrew openjdk@21 is no longer installed; mise sets JAVA_HOME.',
             lines=openjdk, condition=openjdk_gone),
        Rule('zprofile-python310', '.zprofile', 'Python.org 3.10 PATH',
             'Python.framework version 3.10 does not exist.',
             lines=('# Setting PATH for Python 3.10', '# The original version is saved in .zprofile.pysave',
                    'PATH="/Library/Frameworks/Python.framework/Versions/3.10/bin:${PATH}"', 'export PATH'),
             condition=_missing('/Library/Frameworks/Python.framework/Versions/3.10')),
        Rule('zprofile-python36', '.zprofile', 'Python.org 3.6 PATH',
             'Python.framework version 3.6 does not exist.',
             lines=('# Setting PATH for Python 3.6', '# The original version is saved in .zprofile.pysave',
                    'PATH="/Library/Frameworks/Python.framework/Versions/3.6/bin:${PATH}"', 'export PATH'),
             condition=_missing('/Library/Frameworks/Python.framework/Versions/3.6')),
        Rule('zprofile-macports-note', '.zprofile', 'MacPorts installer notice',
             'MacPorts (/opt/local) is not installed.',
             lines=('##', re.compile(r'# Your previous .* was backed up as .*macports-saved.*'), '##'),
             condition=_missing('/opt/local')),
        Rule('zprofile-macports-path', '.zprofile', 'MacPorts PATH',
             'MacPorts (/opt/local) is not installed.',
             lines=(re.compile(r'# MacPorts Installer addition on .*PATH variable for use with MacPorts\.'),
                    'export PATH="/opt/local/bin:/opt/local/sbin:$PATH"',
                    '# Finished adapting your PATH environment variable for use with MacPorts.'),
             condition=_missing('/opt/local')),
        Rule('zprofile-macports-manpath', '.zprofile', 'MacPorts MANPATH',
             'MacPorts (/opt/local) is not installed.',
             lines=(re.compile(r'# MacPorts Installer addition on .*MANPATH variable for use with MacPorts\.'),
                    'export MANPATH="/opt/local/share/man:$MANPATH"',
                    '# Finished adapting your MANPATH environment variable for use with MacPorts.'),
             condition=_missing('/opt/local')),
        Rule('zprofile-orbstack', '.zprofile', 'OrbStack integration',
             'OrbStack is not installed (~/.orbstack does not exist).',
             lines=('# Added by OrbStack: command-line tools and integration',
                    'source ~/.orbstack/shell/init.zsh 2>/dev/null || :'),
             condition=_missing('~/.orbstack')),
        Rule('zshrc-flutter-wrong', '.zshrc', 'Flutter PATH with a wrong path',
             'Points to $HOME followed by the home folder again; the path does not exist.',
             lines=('export PATH=$HOME%s/flutter/bin:$PATH' % home,),
             condition=_missing('~/%s/flutter' % home.lstrip('/'))),
        Rule('zshrc-openjdk', '.zshrc', 'openjdk@21 (PATH, CPPFLAGS, JAVA_HOME)',
             'The Homebrew openjdk@21 is no longer installed; mise sets JAVA_HOME.',
             lines=openjdk, condition=openjdk_gone),
        Rule('zshrc-conda', '.zshrc', '"conda initialize" block', 'Miniforge (~/miniforge3) was removed.',
             start='# >>> conda initialize >>>', end='# <<< conda initialize <<<',
             condition=_missing('~/miniforge3')),
        Rule('zshrc-mamba', '.zshrc', '"mamba initialize" block', 'Miniforge (~/miniforge3) was removed.',
             start='# >>> mamba initialize >>>', end='# <<< mamba initialize <<<',
             condition=_missing('~/miniforge3')),
        Rule('zshrc-docker-completions', '.zshrc', 'Docker Desktop completions',
             'Docker Desktop was removed (~/.docker/completions does not exist).',
             start='# The following lines have been added by Docker Desktop to enable Docker CLI completions.',
             end='# End of Docker CLI completions', condition=_missing('~/.docker/completions')),
        Rule('zshrc-compinit', '.zshrc', 'Repeated compinit',
             'oh-my-zsh already runs compinit; running it again only makes the shell slower.',
             lines=('autoload -Uz compinit', 'compinit')),
        Rule('zshrc-fnm', '.zshrc', 'fnm activation', 'fnm was uninstalled; mise handles Node.',
             lines=('eval "$(fnm env --use-on-cd)"',), condition=_fnm_absent),
        Rule('zshrc-brew-path', '.zshrc', 'Homebrew put back in front of the PATH',
             'It already comes from .zprofile; repeating it here made Homebrew win over other managers.',
             lines=('export PATH="/opt/homebrew/bin:$PATH"',)),
        Rule('zshrc-local-bin-dup', '.zshrc', 'Second ~/.local/bin in the PATH (Antigravity)',
             'Duplicate: .zshrc already adds $HOME/.local/bin earlier.',
             lines=('# Added by Antigravity CLI installer', 'export PATH="%s/.local/bin:$PATH"' % home),
             condition=lambda ctx, text: 'export PATH="$HOME/.local/bin:$PATH"' in text),
    ]
    return rules


SHELL_FILES = ('.zshenv', '.zprofile', '.zshrc')


def plan_edits(ctx: Context, rules: Sequence[Rule]) -> Dict[str, List[Match]]:
    plan: Dict[str, List[Match]] = {}
    for name in SHELL_FILES:
        path = ctx.home / name
        if path.is_file() and not path.is_symlink():
            matches = find_matches(ctx, name, path.read_text(), rules)
            if matches:
                plan[name] = matches
    return plan


# Cloudflare token -------------------------------------------------------------------

CF_COMMENT = '# Cloudflare API token (used by flarectl, wrangler, etc.)'
_EXPORT = re.compile(r'^\s*export\s+(?P<name>CF_API_TOKEN|CLOUDFLARE_API_TOKEN)=(?P<value>.*?)\s*$')
KEYCHAIN_SERVICE = 'cloudflare-api-token'
KEYCHAIN_SERVICE_ALT = 'cloudflare-api-token-alt'


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
        return value[1:-1]
    return value


def literal_tokens(text: str) -> Dict[str, Tuple[int, str]]:
    """export lines with a literal value (not read from Keychain or from another variable)."""
    found: Dict[str, Tuple[int, str]] = {}
    for index, line in enumerate(text.splitlines()):
        match = _EXPORT.match(line)
        if not match:
            continue
        value = _unquote(match.group('value'))
        if not value or value.startswith('$'):
            continue
        found[match.group('name')] = (index, value)
    return found


def keychain_lines(tokens: Dict[str, Tuple[int, str]]) -> Dict[int, str]:
    """New lines, by index, that read the token from Keychain."""
    lookup = '"$(security find-generic-password -a "$USER" -s %s -w 2>/dev/null)"'
    lines: Dict[int, str] = {}
    cf = tokens.get('CF_API_TOKEN')
    alt = tokens.get('CLOUDFLARE_API_TOKEN')
    if cf:
        lines[cf[0]] = 'export CF_API_TOKEN=' + lookup % KEYCHAIN_SERVICE
    if alt:
        if cf and alt[1] == cf[1]:
            lines[alt[0]] = 'export CLOUDFLARE_API_TOKEN="$CF_API_TOKEN"'
        else:
            lines[alt[0]] = 'export CLOUDFLARE_API_TOKEN=' + lookup % KEYCHAIN_SERVICE_ALT
    return lines


def secrets_to_store(tokens: Dict[str, Tuple[int, str]]) -> Dict[str, str]:
    stores: Dict[str, str] = {}
    cf = tokens.get('CF_API_TOKEN')
    alt = tokens.get('CLOUDFLARE_API_TOKEN')
    if cf:
        stores[KEYCHAIN_SERVICE] = cf[1]
    if alt and not (cf and alt[1] == cf[1]):
        stores[KEYCHAIN_SERVICE_ALT] = alt[1]
    return stores


def replace_lines(text: str, new_lines: Dict[int, str]) -> str:
    lines = text.splitlines()
    for index, line in new_lines.items():
        lines[index] = line
    return '\n'.join(lines) + ('\n' if text.endswith('\n') else '')


class Keychain:
    """User Keychain through /usr/bin/security; the value is never printed."""

    def store(self, service: str, account: str, value: str) -> None:
        # Interactive mode reading from standard input: the value does not show up in the process list.
        for text in (service, account, value):
            if any(ch in text for ch in '"\\\n\r'):
                raise RuntimeError('value contains a quote, backslash or line break; store it in Keychain by hand')
        command = 'add-generic-password -U -a "%s" -s "%s" -w "%s"\n' % (account, service, value)
        proc = subprocess.run(['/usr/bin/security', '-i'], input=command, capture_output=True,
                              text=True, timeout=30)
        if proc.returncode or self.read(service, account) != value:
            raise RuntimeError('security add-generic-password failed (exit code %d)' % proc.returncode)

    def read(self, service: str, account: str) -> Optional[str]:
        proc = subprocess.run(['/usr/bin/security', 'find-generic-password', '-a', account,
                               '-s', service, '-w'], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=30)
        return proc.stdout.rstrip('\n') if proc.returncode == 0 else None


KEYCHAIN = Keychain()
