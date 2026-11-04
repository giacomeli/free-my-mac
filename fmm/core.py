"""Step model: detection, items, operations, context and displayed state."""
from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# Detection states (what the machine shows).
PENDING = 'pending'
PARTIAL = 'partial'
CLEAN = 'clean'
AVAILABLE = 'available'
ERROR = 'error'

# Displayed states (detection combined with history and dependencies).
SHOW_PENDING = 'pending'
SHOW_PARTIAL = 'partial'
SHOW_DONE = 'done'
SHOW_CLEAN = 'clean'
SHOW_BLOCKED = 'blocked'
SHOW_IGNORED = 'ignored'
SHOW_AVAILABLE = 'available'
SHOW_ERROR = 'error'
SHOW_DETECTING = 'detecting'

SATISFIED = (SHOW_DONE, SHOW_CLEAN, SHOW_IGNORED)

COMMAND_ENV = {
    'HOMEBREW_NO_AUTO_UPDATE': '1',
    'HOMEBREW_NO_ANALYTICS': '1',
    'HOMEBREW_NO_ENV_HINTS': '1',
    'HOMEBREW_NO_INSTALL_CLEANUP': '1',
}


@dataclass
class Item:
    """Something detected on the machine that the step can handle."""
    key: str
    label: str
    size: Optional[int] = None
    path: Optional[str] = None
    note: str = ''
    selected: bool = True
    data: dict = field(default_factory=dict)


@dataclass
class Detection:
    state: str
    items: List[Item] = field(default_factory=list)
    summary: str = ''
    notes: List[str] = field(default_factory=list)

    @property
    def size(self) -> Optional[int]:
        sizes = [i.size for i in self.items if i.size]
        return sum(sizes) if sizes else None


@dataclass
class Op:
    """One operation of a run: an external command or an internal action, always with displayable text."""
    label: str
    display: str
    argv: Optional[List[str]] = None
    sudo: bool = False
    func: Optional[Callable[['Context'], None]] = None
    cwd: Optional[str] = None
    tolerate_failure: bool = False


def failing_op(label: str, reason: str) -> Op:
    """Checked item the step cannot handle: fails with the reason instead of vanishing from the run."""
    def fail(_ctx: 'Context') -> None:
        raise RuntimeError(reason)
    return Op(label, 'nothing runs: ' + reason, func=fail)


@dataclass
class QueryResult:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def command_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ)
    env.update(COMMAND_ENV)
    if extra:
        env.update(extra)
    return env


def run_query(argv: Sequence[str], timeout: int = 60, cwd: Optional[str] = None) -> QueryResult:
    """Read-only query; never inherits stdin, so it cannot hang waiting for input."""
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout,
                              cwd=cwd, env=command_env(), stdin=subprocess.DEVNULL)
        return QueryResult(proc.returncode, proc.stdout, proc.stderr)
    except FileNotFoundError:
        return QueryResult(127, '', 'command not found: ' + str(argv[0]))
    except subprocess.TimeoutExpired:
        return QueryResult(124, '', 'timeout after %ss' % timeout)
    except OSError as exc:
        return QueryResult(126, '', str(exc))


def run_live(argv: Sequence[str], cwd: Optional[str] = None) -> int:
    try:
        return subprocess.call(list(argv), cwd=cwd, env=command_env())
    except FileNotFoundError:
        print('Command not found: ' + str(argv[0]), flush=True)
        return 127
    except OSError as exc:
        print('Could not run %s: %s' % (argv[0], exc), flush=True)
        return 126


def inside(path: Path, parent: Path) -> bool:
    try:
        Path(path).relative_to(parent)
        return True
    except ValueError:
        return False


class Context:
    """Session environment: paths, injectable queries and read caches."""

    def __init__(self, home: Optional[Path] = None, projects: Optional[Path] = None,
                 root: Optional[Path] = None, backup_root: Optional[Path] = None,
                 state_dir: Optional[Path] = None,
                 query: Optional[Callable[..., QueryResult]] = None,
                 tools: Optional[Dict[str, Optional[str]]] = None,
                 today: Optional[_dt.date] = None,
                 caller: Optional[Callable[..., int]] = None):
        self.home = Path(home or Path.home())
        self.projects = Path(projects or self.home / 'Projects')
        self.root = Path(root or '/')
        self.backup_root = Path(backup_root or self.home / 'bkp/free-my-mac')
        self.state_dir = Path(state_dir or self.home / '.local/state/free-my-mac')
        self._query = query or run_query
        self._tools = dict(tools or {})
        self._today = today
        self._caller = caller or run_live
        self._cache: Dict[Tuple, object] = {}
        self._lock = threading.Lock()

    # Paths --------------------------------------------------------------------
    @property
    def real_system(self) -> bool:
        return str(self.root) == '/'

    def sys(self, path: str) -> Path:
        """System path (/usr/local, /Library...) relative to the context root."""
        return self.root / path.lstrip('/')

    def user(self, path: str) -> Path:
        """User path; accepts '~/x' or a path relative to home."""
        return self.home / (path[2:] if path.startswith('~/') else path)

    def expand(self, path: str) -> Path:
        return self.user(path) if path.startswith('~') else self.sys(path)

    def pretty(self, path) -> str:
        p = Path(path)
        if inside(p, self.home):
            rel = p.relative_to(self.home)
            return '~' if str(rel) == '.' else '~/' + str(rel)
        if not self.real_system and inside(p, self.root):
            return '/' + str(p.relative_to(self.root))
        return str(p)

    def needs_sudo(self, path: Path) -> bool:
        """Outside home, removal is privileged; inside home, never."""
        return self.real_system and not inside(Path(path), self.home)

    def today(self) -> _dt.date:
        return self._today or _dt.date.today()

    def backup_dir(self) -> Path:
        return self.backup_root / self.today().isoformat()

    # Tools --------------------------------------------------------------------
    def tool(self, name: str) -> Optional[str]:
        if name in self._tools:
            return self._tools[name]
        known = {
            'brew': ['/opt/homebrew/bin/brew'],
            'mise': [str(self.home / '.local/bin/mise'), '/opt/homebrew/bin/mise'],
            'uv': ['/opt/homebrew/bin/uv', str(self.home / '.local/bin/uv'), str(self.home / '.cargo/bin/uv')],
            'rustup': [str(self.home / '.cargo/bin/rustup'), '/opt/homebrew/bin/rustup'],
            'git': ['/usr/bin/git'],
            'xcrun': ['/usr/bin/xcrun'],
            'docker': ['/usr/local/bin/docker', '/Applications/Docker.app/Contents/Resources/bin/docker'],
            'zsh': ['/bin/zsh'],
            'security': ['/usr/bin/security'],
            'pkgutil': ['/usr/sbin/pkgutil'],
        }
        for candidate in known.get(name, []):
            if os.access(candidate, os.X_OK):
                return candidate
        return shutil.which(name)

    def call(self, argv: Sequence[str], cwd: Optional[str] = None) -> int:
        """Runs with inherited stdio (live output, sudo password in the terminal)."""
        return self._caller(list(argv), cwd=cwd)

    # Cached queries --------------------------------------------------------------
    def query(self, argv: Sequence[str], timeout: int = 60, cwd: Optional[str] = None,
              cache: bool = True) -> QueryResult:
        key = ('q', tuple(argv), cwd)
        if cache:
            with self._lock:
                if key in self._cache:
                    return self._cache[key]  # type: ignore[return-value]
        result = self._query(list(argv), timeout=timeout, cwd=cwd)
        if cache:
            with self._lock:
                self._cache[key] = result
        return result

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def du(self, path) -> Optional[int]:
        """Allocated size in bytes, without crossing volumes; None if unavailable."""
        p = Path(path)
        if not os.path.lexists(p):
            return None
        if p.is_symlink() or not p.is_dir():
            try:
                return p.lstat().st_blocks * 512
            except OSError:
                return None
        result = self.query(['/usr/bin/du', '-skx', str(p)], timeout=300)
        try:
            return int(result.out.split()[0]) * 1024
        except (IndexError, ValueError):
            return None

    def pkgs(self) -> List[str]:
        result = self.query([self.tool('pkgutil') or '/usr/sbin/pkgutil', '--pkgs'])
        return [line.strip() for line in result.out.splitlines() if line.strip()] if result.ok else []

    def brew_prefix(self) -> Path:
        return self.sys('/opt/homebrew')

    def brew_formulas(self) -> List[str]:
        cellar = self.brew_prefix() / 'Cellar'
        return sorted(p.name for p in cellar.iterdir() if p.is_dir()) if cellar.is_dir() else []

    def brew_casks(self) -> List[str]:
        caskroom = self.brew_prefix() / 'Caskroom'
        return sorted(p.name for p in caskroom.iterdir() if p.is_dir()) if caskroom.is_dir() else []


class Step:
    """A catalog step. Subclasses define the texts, detect() and ops()."""
    id = ''
    group = ''
    title = ''
    what = ''
    why = ''
    consequences = ''
    undo = ''
    requires: Tuple[str, ...] = ()
    heavy = False
    rerunnable = False
    clean_label = 'Already clean'  # tweaks say 'Already applied'

    def detect(self, ctx: Context) -> Detection:
        raise NotImplementedError

    def ops(self, ctx: Context, items: List[Item]) -> List[Op]:
        raise NotImplementedError


@dataclass
class StateView:
    code: str
    label: str
    detail: str = ''


MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def format_when(timestamp: float) -> str:
    """Local time as 'Nov 01 02:12'. Fixed month names: strftime('%b') follows the locale curses sets."""
    moment = _dt.datetime.fromtimestamp(timestamp)
    return '%s %02d %02d:%02d' % (MONTHS[moment.month - 1], moment.day, moment.hour, moment.minute)


def resolve_states(steps: Sequence[Step], detections: Dict[str, Optional[Detection]],
                   history) -> Dict[str, StateView]:
    """Displayed state of each step; dependencies in catalog order."""
    by_id = {s.id: s for s in steps}
    views: Dict[str, StateView] = {}
    for step in steps:
        det = detections.get(step.id)
        if history.is_ignored(step.id):
            views[step.id] = StateView(SHOW_IGNORED, 'Ignored')
            continue
        if det is None:
            views[step.id] = StateView(SHOW_DETECTING, 'Detecting...')
            continue
        if det.state == ERROR:
            views[step.id] = StateView(SHOW_ERROR, 'Detection failed', det.summary)
            continue
        if det.state == CLEAN:
            last = history.last_success(step.id)
            if last:
                views[step.id] = StateView(SHOW_DONE, 'Done ' + format_when(last['ts']))
            else:
                views[step.id] = StateView(SHOW_CLEAN, step.clean_label)
            continue
        missing = [r for r in step.requires
                   if r in views and views[r].code not in SATISFIED]
        if missing:
            names = ', '.join(by_id[r].title for r in missing)
            views[step.id] = StateView(SHOW_BLOCKED, 'Blocked', 'First finish or ignore: ' + names)
            continue
        if det.state == AVAILABLE:
            last = history.last_success(step.id)
            label = 'Available' + (' (last ' + format_when(last['ts']) + ')' if last else '')
            views[step.id] = StateView(SHOW_AVAILABLE, label)
        elif det.state == PARTIAL:
            views[step.id] = StateView(SHOW_PARTIAL, 'Partial')
        else:
            views[step.id] = StateView(SHOW_PENDING, 'Pending')
    return views


def human(size: Optional[int]) -> str:
    """Size in decimal units, like Finder, with a decimal point."""
    if size is None:
        return '-'
    value = float(size)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1000 or unit == 'TB':
            if unit in ('B', 'KB'):
                return '%d %s' % (round(value), unit)
            return '%.1f %s' % (value, unit)
        value /= 1000
    return '-'


_SECRET_ASSIGN = re.compile(
    r'(?P<name>\b[A-Za-z_][A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|PASS|PWD|KEY|PAT|AUTH|CREDENTIALS?)[A-Za-z0-9_]*)'
    r'(?P<sep>\s*=\s*)(?P<value>"[^"]*"|\'[^\']*\'|[^\s;\'"]+)',
    re.IGNORECASE)

_URL_PASSWORD = re.compile(r'([a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+@', re.IGNORECASE)


def redact(text: str) -> str:
    """Masks values assigned to variables with secret-like names."""
    def repl(match):
        value = match.group('value')
        if value.startswith('"$(') or value.startswith('$('):
            return match.group(0)
        return match.group('name') + match.group('sep') + '<hidden>'
    return _URL_PASSWORD.sub(r'\g<1><hidden>@', _SECRET_ASSIGN.sub(repl, text))


def digest(values) -> str:
    """Short digest of a list: it goes into the item key so revalidation catches content changes."""
    import hashlib
    return hashlib.sha1('\n'.join(sorted(str(v) for v in values)).encode()).hexdigest()[:10]


def plural(n: int, one: str, many: str) -> str:
    return '%d %s' % (n, one if n == 1 else many)
