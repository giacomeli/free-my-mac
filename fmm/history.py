"""Persistent history of runs and ignored steps (JSONL, mode 600)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from .core import redact


def _redact_obj(value):
    """Masks secrets in every string before serialization (escaped quotes do not leak)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_obj(v) for v in value]
    return value


class History:
    def __init__(self, path):
        self.path = Path(path)
        self._entries: Optional[List[dict]] = None

    def entries(self) -> List[dict]:
        if self._entries is None:
            self._entries = []
            if self.path.is_file():
                for line in self.path.read_text(errors='replace').splitlines():
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(entry, dict) and 'step' in entry and 'ts' in entry:
                        self._entries.append(entry)
        return self._entries

    def _append(self, entry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        line = json.dumps(_redact_obj(entry), ensure_ascii=False) + '\n'
        fd = os.open(str(self.path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a') as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        self.entries().append(json.loads(line))

    def record_run(self, step_id: str, title: str, status: str, items: List[str],
                   ops: List[Dict], free_before: Optional[int], free_after: Optional[int]) -> dict:
        entry = dict(ts=time.time(), kind='run', step=step_id, title=title, status=status,
                     items=items, ops=ops, free_before=free_before, free_after=free_after)
        self._append(entry)
        return entry

    def record_skip(self, step_id: str, title: str, ignored: bool) -> None:
        self._append(dict(ts=time.time(), kind='ignore' if ignored else 'unignore',
                          step=step_id, title=title))

    def last_success(self, step_id: str) -> Optional[dict]:
        for entry in reversed(self.entries()):
            if entry['step'] == step_id and entry.get('kind') == 'run' and entry.get('status') == 'ok':
                return entry
        return None

    def is_ignored(self, step_id: str) -> bool:
        for entry in reversed(self.entries()):
            if entry['step'] == step_id and entry.get('kind') in ('ignore', 'unignore'):
                return entry['kind'] == 'ignore'
        return False
