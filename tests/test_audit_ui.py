import concurrent.futures
from pathlib import Path
import unittest
from unittest.mock import patch

import audit_ui
from dev_audit import Finding, Policy
from dataclasses import asdict


class Screen:
    def __init__(self, keys):
        self.keys = iter(keys)
        self.frames = []
        self.current = []

    def timeout(self, value):
        pass

    def getmaxyx(self):
        return 30, 240

    def erase(self):
        self.current = []

    def addnstr(self, y, x, text, width, style=0):
        self.current.append(text[:width])

    def refresh(self):
        self.frames.append('\n'.join(self.current))

    def getch(self):
        return next(self.keys)


class Pool:
    def __init__(self, report):
        self.report = report

    def submit(self, *args):
        f = concurrent.futures.Future()
        f.set_result(self.report)
        return f

    def shutdown(self, **kwargs):
        pass


class InterfaceTests(unittest.TestCase):
    def show(self, keys):
        report = {'findings': [asdict(Finding('A', '/cache', 'npm', 'cache', action='delete-cache')),
                               asdict(Finding('A', '/simulator', 'Apple', 'revisão')),
                               asdict(Finding('B', '/deps', 'Node', 'deps', action='delete-node-modules')),
                               asdict(Finding('B', '/protected', 'Agent', 'preservar', action='protected'))]}
        screen = Screen(keys)
        with patch.object(audit_ui.concurrent.futures, 'ThreadPoolExecutor', return_value=Pool(report)):
            audit_ui.display(screen, Policy(Path.home()), Path.home(), 60, False, False, False)
        return screen.frames

    def test_completed_screen_is_not_redrawn_on_idle_timeouts(self):
        frames = self.show([-1, -1, -1, ord('q')])
        self.assertEqual(len(frames), 1)
        self.assertIn('Leitura concluída', frames[0])

    def test_review_has_no_checkbox(self):
        frame = self.show([ord('q')])[0]
        line = next(line for line in frame.splitlines() if '/simulator' in line)
        self.assertNotIn('[ ]', line)
        self.assertIn('somente análise', line)

    def test_category_selection_all_and_clear_respect_eligibility(self):
        frames = self.show([9, ord('a'), ord('A'), ord('c'), ord('q')])
        self.assertIn('1 nesta categoria / 1 no total', frames[2])
        self.assertIn('1 nesta categoria / 2 no total', frames[3])
        self.assertIn('0 nesta categoria / 0 no total', frames[4])
        self.assertFalse(any('[x]' in line and ('/protected' in line or '/simulator' in line)
                             for frame in frames for line in frame.splitlines()))
