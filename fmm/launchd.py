"""launchd: reading agents and services, the program each one runs, and reloading."""
from __future__ import annotations

import os
import plistlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from .core import Context, inside

USER_AGENTS = '~/Library/LaunchAgents'
SYSTEM_FOLDERS = ('/Library/LaunchAgents', '/Library/LaunchDaemons')
PYTHON_NAME = re.compile(r'^python(\d+(\.\d+)?)?$')


@dataclass
class Agent:
    plist: Path
    label: str
    program: Optional[str] = None
    broken_link: bool = False
    readable: bool = True
    data: dict = field(default_factory=dict)

    @property
    def is_daemon(self) -> bool:
        return 'LaunchDaemons' in self.plist.parts

    def is_python(self) -> bool:
        return bool(self.program) and bool(PYTHON_NAME.match(os.path.basename(self.program or '')))


def read_plist(path: Path) -> Optional[dict]:
    try:
        with open(path, 'rb') as stream:
            data = plistlib.load(stream)
    except Exception:  # noqa: BLE001 - unreadable or corrupt plist: treated as unknown
        return None
    return data if isinstance(data, dict) else None


def read_agents(ctx: Context, folders: Sequence[str]) -> List[Agent]:
    """Plists in the folders, sorted by name; broken links and unreadable files are included too, flagged."""
    agents: List[Agent] = []
    for folder in folders:
        base = ctx.expand(folder)
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir(), key=lambda p: p.name):
            if entry.suffix != '.plist':
                continue
            if entry.is_symlink() and not entry.exists():
                agents.append(Agent(entry, entry.stem, broken_link=True, readable=False))
                continue
            data = read_plist(entry)
            if data is None:
                agents.append(Agent(entry, entry.stem, readable=False))
                continue
            args = [str(a) for a in data.get('ProgramArguments') or []]
            program = data.get('Program') or (args[0] if args else None)
            agents.append(Agent(entry, str(data.get('Label') or entry.stem), program=program, data=data))
    return agents


def resolve(ctx: Context, path: str) -> Path:
    """Absolute plist path in the context: home is real; everything else lives under the context root."""
    p = Path(path)
    return p if inside(p, ctx.home) else ctx.sys(path)


def domain(agent: Agent) -> str:
    return 'system' if agent.is_daemon else 'gui/%d' % os.getuid()
