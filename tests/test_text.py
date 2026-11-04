"""Text modes: list, detail, y/N confirmation, run and history."""
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from helpers import Sandbox

from fmm import text
from fmm.core import CLEAN, PENDING, Detection, Item, Op, Step, StateView
from fmm.history import History


class Removable(Step):
    id = 'removable'
    group = 'general'
    title = 'Remove the test folder'
    what = 'Deletes the test folder.'
    why = 'Takes up space.'
    consequences = 'It goes away.'
    undo = 'Create it again.'

    def detect(self, ctx):
        target = ctx.user('~/junk')
        if not target.exists():
            return Detection(CLEAN, summary='Nothing.')
        return Detection(PENDING, [Item('junk', '~/junk', size=2_000_000, path=str(target))])

    def ops(self, ctx, items):
        def remove(c):
            for child in c.user('~/junk').iterdir():
                child.unlink()
            c.user('~/junk').rmdir()
        return [Op('Delete ~/junk', 'rm -rf ~/junk', func=remove)]


class Dependent(Removable):
    id = 'dependent'
    title = 'Step that depends on the first'
    requires = ('removable',)


class Broken(Removable):
    id = 'broken'
    title = 'Step with broken detection'

    def detect(self, ctx):
        raise OSError('disk vanished')


class TextModeTests(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(tools={})
        self.box.make('junk/file', 'x')
        self.history = History(self.box.home / 'state/history.jsonl')
        self.steps = [Removable(), Dependent(), Broken()]

    def tearDown(self):
        self.box.close()

    def capture(self, fn, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            result = fn(*args)
        return result, out.getvalue()

    def test_list_shows_states_blocked_reason_and_errors(self):
        out = io.StringIO()
        text.print_list(self.box.ctx, self.steps, self.history, out=out)
        listing = out.getvalue()
        self.assertIn('General cleanup', listing)
        self.assertIn('Pending', listing)
        self.assertIn('Blocked', listing)
        self.assertIn('First finish or ignore: Remove the test folder', listing)
        self.assertIn('Detection failed', listing)
        self.assertIn('disk vanished', listing)
        self.assertIn('in 2 steps', listing)
        self.assertFalse(self.history.path.exists())

    def test_show_explains_and_lists_commands_last(self):
        out = io.StringIO()
        text.show(self.box.ctx, self.steps, self.history, 'removable', out=out)
        shown = out.getvalue()
        order = [shown.index(t) for t in ('What it does:', 'Why:', 'Consequences:', 'How to undo:',
                                          'State:', 'Items (1):', 'Commands:')]
        self.assertEqual(order, sorted(order))
        self.assertIn('$ rm -rf ~/junk', shown)
        self.assertIn('[x] ~/junk  (2.0 MB)', shown)

    def test_show_unknown_id(self):
        with self.assertRaises(ValueError):
            text.show(self.box.ctx, self.steps, self.history, 'nope', out=io.StringIO())

    def test_runnable_reasons(self):
        items = [Item('a', 'a')]
        self.assertIsNone(text.runnable(StateView('pending', 'Pending'), items))
        self.assertIn('ignored', text.runnable(StateView('ignored', 'Ignored'), items))
        self.assertEqual(text.runnable(StateView('blocked', 'Blocked', 'First X'), items), 'First X')
        self.assertEqual(text.runnable(StateView('pending', 'Pending'), []), 'No items selected.')
        self.assertEqual(text.runnable(StateView('clean', 'Already clean'), []), 'Nothing to do.')

    def test_confirm_defaults_to_no(self):
        for key, expected in (('s', True), ('S', True), ('y', True), ('n', False), ('\n', False), ('x', False)):
            with patch.object(text, 'read_key', return_value=key):
                result, _ = self.capture(text.confirm, 'Run?')
            self.assertIs(result, expected, key)

    def test_run_cancel_changes_nothing(self):
        with patch.object(text.sys.stdin, 'isatty', return_value=True), \
                patch.object(text, 'read_key', return_value='n'):
            code, shown = self.capture(text.run_text, self.box.ctx, self.steps, self.history, 'removable')
        self.assertEqual(code, 0)
        self.assertIn('Cancelled. Nothing was changed.', shown)
        self.assertTrue((self.box.home / 'junk/file').exists())
        self.assertEqual(self.history.entries(), [])

    def test_run_blocked_refuses_without_asking(self):
        with patch.object(text, 'read_key') as key:
            code, shown = self.capture(text.run_text, self.box.ctx, self.steps, self.history, 'dependent')
        self.assertEqual(code, 1)
        self.assertIn('Cannot run: First finish or ignore', shown)
        key.assert_not_called()

    def test_run_confirmed_executes_records_and_shows_done(self):
        with patch.object(text.sys.stdin, 'isatty', return_value=True), \
                patch.object(text, 'read_key', return_value='s'):
            code, _ = self.capture(text.run_text, self.box.ctx, self.steps, self.history, 'removable')
        self.assertEqual(code, 0)
        self.assertFalse((self.box.home / 'junk').exists())
        self.assertEqual(self.history.last_success('removable')['status'], 'ok')
        out = io.StringIO()
        text.print_list(self.box.ctx, self.steps, self.history, out=out)
        self.assertRegex(out.getvalue(), r'removable\s+Remove the test folder\s+Done [A-Z][a-z]{2} \d\d \d\d:\d\d')
        lines = text.history_lines(self.history)
        self.assertIn('Completed', lines[0])

    def test_history_empty_message(self):
        out = io.StringIO()
        text.print_history(self.history, out=out)
        self.assertIn('No runs recorded yet', out.getvalue())


if __name__ == '__main__':
    unittest.main()
