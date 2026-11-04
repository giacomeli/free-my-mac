import io
import os
import stat
import unittest
from pathlib import Path

from helpers import Sandbox

from fmm.core import (CLEAN, PENDING, Detection, Item, Op, Step, human, redact,
                      resolve_states)
from fmm.fs import UnsafeRemoval, safe_remove, write_atomic
from fmm.history import History
from fmm.paths import PathsStep, links_into
from fmm.runner import run_step


class FixedStep(Step):
    def __init__(self, sid, state=PENDING, requires=(), items=None):
        self.id = sid
        self.title = 'Step ' + sid
        self.requires = requires
        self._state = state
        self._items = items if items is not None else [Item(key='a', label='a')]

    def detect(self, ctx):
        return Detection(self._state, list(self._items) if self._state != CLEAN else [])

    def ops(self, ctx, items):
        return []


class StateTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.history = History(self.box.home / 'h.jsonl')

    def tearDown(self):
        self.box.close()

    def test_clean_without_history_is_already_clean_and_with_history_is_done(self):
        steps = [FixedStep('a', CLEAN), FixedStep('b', CLEAN)]
        self.history.record_run('b', 'b', 'ok', [], [], 1, 2)
        views = resolve_states(steps, {s.id: s.detect(None) for s in steps}, self.history)
        self.assertEqual(views['a'].label, 'Already clean')
        self.assertTrue(views['b'].label.startswith('Done '))

    def test_pending_requirement_blocks_and_names_it(self):
        steps = [FixedStep('a'), FixedStep('b', requires=('a',))]
        views = resolve_states(steps, {s.id: s.detect(None) for s in steps}, self.history)
        self.assertEqual(views['b'].code, 'blocked')
        self.assertIn('Step a', views['b'].detail)

    def test_ignored_requirement_unblocks(self):
        steps = [FixedStep('a'), FixedStep('b', requires=('a',))]
        self.history.record_skip('a', 'a', True)
        views = resolve_states(steps, {s.id: s.detect(None) for s in steps}, self.history)
        self.assertEqual(views['a'].code, 'ignored')
        self.assertEqual(views['b'].code, 'pending')
        self.history.record_skip('a', 'a', False)
        views = resolve_states(steps, {s.id: s.detect(None) for s in steps}, self.history)
        self.assertEqual(views['b'].code, 'blocked')

    def test_detecting_when_no_detection_yet(self):
        steps = [FixedStep('a')]
        self.assertEqual(resolve_states(steps, {'a': None}, self.history)['a'].code, 'detecting')


class HistoryTests(unittest.TestCase):
    def test_private_file_tolerates_corrupt_lines_and_redacts(self):
        box = Sandbox()
        try:
            path = box.home / 'state/history.jsonl'
            h = History(path)
            h.record_run('x', 'X', 'ok', ['export CF_API_TOKEN="abc123"'], [], 1, 2)
            with path.open('a') as f:
                f.write('{corrupted\n')
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertNotIn('abc123', path.read_text())
            again = History(path)
            self.assertEqual(len(again.entries()), 1)
            self.assertIsNotNone(again.last_success('x'))
        finally:
            box.close()


class RedactTests(unittest.TestCase):
    def test_masks_secret_values_but_keeps_keychain_lookup(self):
        text = 'export CF_API_TOKEN="cfut_abc"\nexport CLOUDFLARE_API_TOKEN=xyz\nANTHROPIC_AUTH_TOKEN=ollama claude'
        masked = redact(text)
        for secret in ('cfut_abc', 'xyz', 'ollama'):
            self.assertNotIn(secret, masked)
        lookup = 'export CF_API_TOKEN="$(security find-generic-password -w)"'
        self.assertEqual(redact(lookup), lookup)

    def test_human_sizes(self):
        self.assertEqual(human(None), '-')
        self.assertEqual(human(6_800_000_000), '6.8 GB')
        self.assertEqual(human(512), '512 B')


class SafeRemoveTests(unittest.TestCase):
    def test_link_is_removed_without_touching_target(self):
        box = Sandbox()
        try:
            target = box.make('keep/data.txt')
            link = box.home / 'link'
            link.symlink_to(target.parent)
            self.assertTrue(safe_remove(link))
            self.assertTrue(target.exists())
            folder = box.mkdir('folder/sub')
            (folder / 'f').write_text('x')
            (folder / 'escape').symlink_to(target.parent)
            safe_remove(box.home / 'folder')
            self.assertFalse((box.home / 'folder').exists())
            self.assertTrue(target.exists())
            self.assertFalse(safe_remove(box.home / 'missing'))
        finally:
            box.close()

    def test_mount_point_is_refused(self):
        from unittest.mock import patch
        box = Sandbox()
        try:
            folder = box.mkdir('volume')
            with patch('fmm.fs.os.path.ismount', return_value=True):
                with self.assertRaises(UnsafeRemoval):
                    safe_remove(folder)
            self.assertTrue(folder.exists())
        finally:
            box.close()

    def test_write_atomic_preserves_mode(self):
        box = Sandbox()
        try:
            f = box.make('rc', 'old')
            os.chmod(f, 0o640)
            write_atomic(f, 'new')
            self.assertEqual(f.read_text(), 'new')
            self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o640)
        finally:
            box.close()


class Demo(PathsStep):
    id = 'demo'
    title = 'Demo'
    targets = ('~/.demo', '/usr/local/demo', '/etc/paths.d/demo*')
    receipts = r'^com\.demo\.'
    formulas = ('demo-formula',)


class PathsStepTests(unittest.TestCase):
    def test_detect_and_ops_split_user_and_system(self):
        box = Sandbox(answers={('/usr/sbin/pkgutil', '--pkgs'): 'com.demo.a\ncom.other\ncom.demo.b\n'},
                      tools={'brew': '/opt/homebrew/bin/brew', 'pkgutil': '/usr/sbin/pkgutil'})
        try:
            box.make('.demo/x')
            box.make('usr/local/demo/y', base=box.root)
            box.make('etc/paths.d/demo-1', base=box.root)
            box.mkdir('opt/homebrew/Cellar/demo-formula/1.0', base=box.root)
            det = Demo().detect(box.ctx)
            keys = [i.key for i in det.items]
            self.assertEqual(det.state, PENDING)
            self.assertIn('brew:demo-formula', keys)
            receipts = [i for i in det.items if i.key.startswith('receipts:')][0]
            self.assertEqual(receipts.data['ids'], ['com.demo.a', 'com.demo.b'])
            self.assertEqual(len([k for k in keys if k.startswith('path:')]), 3)
            ops = Demo().ops(box.ctx, det.items)
            labels = [o.display for o in ops]
            self.assertTrue(any(d.startswith('brew uninstall --formula demo-formula') for d in labels))
            self.assertTrue(any('pkgutil --forget' in d for d in labels))
            # In a sandbox (root != /) nothing needs sudo; the paths go to removal in Python.
            self.assertFalse(any(o.argv and o.argv[0] == '/bin/rm' for o in ops))
        finally:
            box.close()

    def test_real_system_paths_use_sudo_rm_without_crossing_mounts(self):
        from fmm.core import Context
        from fmm.paths import remove_paths_op
        ctx = Context(home=Path('/Users/x'))
        ops = remove_paths_op(ctx, [Path('/usr/local/share/dotnet'), Path('/Users/x/.dotnet')])
        system = [o for o in ops if o.sudo][0]
        self.assertEqual(system.argv[:3], ['/bin/rm', '-rfx', '--'])
        self.assertIn('/usr/local/share/dotnet', system.argv)
        self.assertNotIn('/Users/x/.dotnet', system.argv)
        self.assertTrue(any(o.func for o in ops))

    def test_links_into_filters_by_target(self):
        box = Sandbox()
        try:
            bin_dir = box.mkdir('usr/local/bin', base=box.root)
            (bin_dir / 'docker').symlink_to('/Applications/Docker.app/Contents/Resources/bin/docker')
            (bin_dir / 'aws').symlink_to('/usr/local/aws-cli/aws')
            found = links_into(box.ctx, ['/usr/local/bin'], lambda t: '/Docker.app/' in t)
            self.assertEqual([p.name for p in found], ['docker'])
        finally:
            box.close()


class RunnerTests(unittest.TestCase):
    def test_revalidation_skips_vanished_items_and_records(self):
        box = Sandbox()
        try:
            box.make('.demo/x')
            history = History(box.home / 'h.jsonl')
            step = Demo()
            det = step.detect(box.ctx)
            keys = [i.key for i in det.items]
            safe_remove(box.home / '.demo')
            out = io.StringIO()
            result = run_step(step, box.ctx, keys, history, out=out)
            self.assertEqual(result.status, 'nothing')
            box.make('.demo/x')
            result = run_step(step, box.ctx, keys, history, out=out)
            self.assertEqual(result.status, 'ok')
            self.assertFalse((box.home / '.demo').exists())
            self.assertEqual(history.last_success('demo')['items'], ['~/.demo'])
        finally:
            box.close()

    def test_failure_stops_following_ops_unless_tolerated(self):
        box = Sandbox(codes={('false',): 1})
        try:
            class Two(Step):
                id = 'two'
                title = 'Two'

                def detect(self, ctx):
                    return Detection(PENDING, [Item(key='k', label='k')])

                def ops(self, ctx, items):
                    return [Op('tolerated', 'false', argv=['false'], tolerate_failure=True),
                            Op('fails', 'false', argv=['false']),
                            Op('never', 'true', argv=['true'])]
            history = History(box.home / 'h.jsonl')
            result = run_step(Two(), box.ctx, ['k'], history, out=io.StringIO())
            self.assertEqual(result.status, 'failed')
            self.assertEqual([c[0] for c in box.caller.calls], [('false',), ('false',)])
            self.assertIsNone(history.last_success('two'))
        finally:
            box.close()


if __name__ == '__main__':
    unittest.main()
