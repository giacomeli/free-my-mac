import importlib.util
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import subprocess
import sys
import time
import contextlib
import io


class FzfTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('fzf_ui'))
        import fzf_ui
        return fzf_ui

    def test_rows_strip_terminal_controls_and_use_numeric_identity(self):
        m = self.api()
        lines = m.rows_for([{'path': 'a\n\x1b[31m', 'category': 'SDK', 'size': None, 'action': 'review'}])
        self.assertEqual(len(lines.splitlines()), 1)
        self.assertTrue(lines.startswith('0\t'))
        self.assertNotIn('\x1b', lines)

    def test_selection_rejects_review_and_preserves_ids_after_filter(self):
        m = self.api()
        items = [{'action':'review'}, {'action':'delete-cache'}, {'action':'protected'}]
        self.assertEqual(m.eligible_ids(items), [1])

    def test_environment_does_not_load_user_fzf_commands(self):
        m = self.api()
        env = m.fzf_environment({'PATH':'/bin', 'FZF_DEFAULT_OPTS':'--bind=start:execute(bad)', 'FZF_DEFAULT_OPTS_FILE':'bad', 'FZF_DEFAULT_COMMAND':'bad'})
        self.assertEqual(env, {'PATH':'/bin'})

    def test_worker_plan_preserves_policy_and_leaves_data_intact(self):
        m = self.api()
        from dev_audit import Policy, catalog_findings
        from dataclasses import asdict
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cache = root / '.npm/_cacache'
            cache.mkdir(parents=True)
            (cache / 'fixture').write_text('keep')
            policy = Policy(root)
            config = {'home': str(root), 'protected': list(map(str, policy.roots)), 'projects': str(policy.projects),
                      'days': 60, 'items': [asdict(catalog_findings(policy)[0])]}
            with contextlib.redirect_stdout(io.StringIO()):
                plan = m.background(config, root, 'plan')
            self.assertEqual(plan['protected'], sorted(map(str, policy.roots)))
            self.assertTrue((cache / 'fixture').exists())

    def test_cancel_terminates_worker_without_waiting_for_long_command(self):
        m = self.api()
        original = subprocess.Popen
        processes = []
        def spawn(*args, **kwargs):
            process = original([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
            processes.append(process)
            return process
        with tempfile.TemporaryDirectory() as d, patch.object(m.subprocess, 'Popen', side_effect=spawn), patch.object(m.select, 'select', side_effect=KeyboardInterrupt):
            started = time.monotonic()
            with self.assertRaises(KeyboardInterrupt):
                m.background({}, Path(d))
            self.assertLess(time.monotonic() - started, 3)
            self.assertIsNotNone(processes[0].poll())
