"""Test utilities: context in a temporary folder, fake queries and fake runs."""
import datetime
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fmm.core import Context, QueryResult  # noqa: E402


class FakeQuery:
    """Answers queries by argv prefix; records everything that was asked."""

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.calls = []

    def __call__(self, argv, timeout=60, cwd=None):
        self.calls.append((tuple(argv), cwd))
        if argv and argv[0] == '/usr/bin/du':
            return self.du(argv[-1])
        for prefix, answer in self.answers.items():
            if tuple(argv[:len(prefix)]) == prefix:
                if callable(answer):
                    answer = answer(argv, cwd)
                if isinstance(answer, QueryResult):
                    return answer
                return QueryResult(0, answer, '')
        return QueryResult(127, '', 'not mocked: ' + ' '.join(argv))

    @staticmethod
    def du(path):
        total = 0
        for folder, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.lstat(os.path.join(folder, name)).st_size
                except OSError:
                    pass
        return QueryResult(0, '%d\t%s\n' % (max(1, total // 1024), path), '')


class FakeCaller:
    def __init__(self, codes=None, effects=None):
        self.codes = dict(codes or {})
        self.effects = dict(effects or {})
        self.calls = []

    def __call__(self, argv, cwd=None):
        self.calls.append((tuple(argv), cwd))
        for prefix, effect in self.effects.items():
            if tuple(argv[:len(prefix)]) == prefix:
                effect(argv, cwd)
        for prefix, code in self.codes.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return code
        return 0


class Sandbox:
    """Home, system root and projects isolated in a temporary folder."""

    def __init__(self, answers=None, tools=None, codes=None, effects=None):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()
        self.home = base / 'home'
        self.root = base / 'root'
        self.home.mkdir()
        self.root.mkdir()
        self.query = FakeQuery(answers)
        self.caller = FakeCaller(codes, effects)
        self.ctx = Context(home=self.home, root=self.root, query=self.query, caller=self.caller,
                           tools=tools if tools is not None else {}, today=datetime.date(2026, 11, 1))

    def make(self, relative, text='x', base=None):
        p = (base or self.home) / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def mkdir(self, relative, base=None):
        p = (base or self.home) / relative
        p.mkdir(parents=True, exist_ok=True)
        return p

    def close(self):
        self._tmp.cleanup()
