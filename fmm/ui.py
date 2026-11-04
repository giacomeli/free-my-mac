"""Curses interface: step list with state, details, selection, y/N confirmation and history."""
from __future__ import annotations

import concurrent.futures
import curses
import locale
import os
import textwrap
import time
from typing import Dict, List, Optional, Sequence, Tuple

from .core import Context, Detection, Item, Step, StateView, human, plural, resolve_states
from .history import History
from .text import (detail_sections, disk_line, history_lines, item_line, pending_text, pending_total,
                   preview_ops, runnable, safe_detect, selected_items)

HELP = [
    'HOW TO USE',
    '',
    'The list shows every step, grouped by topic, with the state detected right now',
    'on your machine. Nothing changes until you open a step, review it and confirm.',
    '',
    'States:',
    '  Pending        there is something to do',
    '  Partial        part of it is already done',
    '  Done           nothing to do, and the run is in the history (with date)',
    '  Already clean  nothing to do, and this tool never ran the step',
    '  Blocked        depends on another step; the panel says which',
    '  Ignored        you skipped the step (key i); steps that depend on it are unblocked',
    '  Available      recurring action with no detectable state',
    '',
    'Main screen: arrows move, Enter opens details, i ignores, h history,',
    '             r detects everything again, q quits.',
    'Details:     arrows move between items, space toggles, a checks all, n unchecks all,',
    '             x runs (asks y/N first), Esc goes back.',
    '',
    'Commands run in the normal terminal: output shows live and sudo asks for the password',
    'there. At the end, Enter returns to this screen with the state already updated.',
    '',
    'Esc or ? goes back.',
]

STATE_COLORS = {'done': 1, 'pending': 2, 'partial': 2, 'available': 3, 'blocked': 4, 'error': 4,
                'ignored': 5, 'clean': 0, 'detecting': 0}


class App:
    def __init__(self, ctx: Context, steps: Sequence[Step], history: History, title: str = 'FREE MY MAC'):
        self.ctx = ctx
        self.title = title
        self.steps = list(steps)
        self.history = history
        self.by_id = {s.id: s for s in self.steps}
        self.dets: Dict[str, Optional[Detection]] = {s.id: None for s in self.steps}
        self.overrides: Dict[str, Dict[str, bool]] = {}
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=6)
        self.futures: Dict[str, concurrent.futures.Future] = {}
        self.cursor = 0
        self.mode = 'main'
        self.detail_cursor = 0
        self.detail_offset = 0
        self.history_offset = 0
        self.message = ''
        self.message_until = 0.0
        self.disk = ''
        self.disk_at = 0.0

    # Detection --------------------------------------------------------------------
    def detect(self, ids: Optional[Sequence[str]] = None) -> None:
        for step_id in (ids or [s.id for s in self.steps]):
            if step_id in self.futures:
                continue
            self.dets[step_id] = None
            self.futures[step_id] = self.pool.submit(safe_detect, self.by_id[step_id], self.ctx)

    def poll(self) -> None:
        for step_id, future in list(self.futures.items()):
            if future.done():
                self.dets[step_id] = future.result()
                del self.futures[step_id]

    def refresh_all(self) -> None:
        self.ctx.invalidate()
        self.detect()

    def after_run(self, step_id: str) -> None:
        self.ctx.invalidate()
        ids = [s.id for s in self.steps if not s.heavy or s.id == step_id]
        self.detect(ids)
        self.overrides.pop(step_id, None)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    # State --------------------------------------------------------------------------
    def views(self) -> Dict[str, StateView]:
        return resolve_states(self.steps, self.dets, self.history)

    def chosen(self, step: Step) -> List[Item]:
        return selected_items(self.dets.get(step.id), self.overrides.get(step.id))

    def flash(self, text: str) -> None:
        self.message = text
        self.message_until = time.monotonic() + 4

    # Drawing ------------------------------------------------------------------------
    def put(self, screen, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = screen.getmaxyx()
        if 0 <= y < height and x < width - 1:
            try:
                screen.addnstr(y, x, text.replace('\t', ' '), width - 1 - x, attr)
            except curses.error:
                pass

    def color(self, code: str) -> int:
        pair = STATE_COLORS.get(code, 0)
        if pair and curses.has_colors():
            return curses.color_pair(pair)
        return curses.A_DIM if code in ('clean', 'detecting') else 0

    def header(self, screen, views: Dict[str, StateView]) -> None:
        if time.monotonic() - self.disk_at > 2:
            self.disk, self.disk_at = disk_line(self.ctx), time.monotonic()
        total, count = pending_total(self.steps, self.dets, views)
        text = '  %s  |  %s' % (self.disk, pending_text(total, count))
        if self.futures:
            text += '  |  detecting %d...' % len(self.futures)
        self.put(screen, 0, 0, ' %s ' % self.title, curses.A_REVERSE | curses.A_BOLD)
        self.put(screen, 0, len(self.title) + 2, text)

    def footer(self, screen, text: str) -> None:
        height, _ = screen.getmaxyx()
        if self.message and time.monotonic() < self.message_until:
            self.put(screen, height - 1, 0, ' ' + self.message + ' ', curses.A_REVERSE | self.color('pending'))
        else:
            self.put(screen, height - 1, 0, text, curses.A_DIM)

    def rows(self) -> List[Tuple[str, object]]:
        from .steps import GROUP_TITLES
        rows: List[Tuple[str, object]] = []
        group = None
        for step in self.steps:
            if step.group != group:
                group = step.group
                rows.append(('group', GROUP_TITLES.get(group, group)))
            rows.append(('step', step))
        return rows

    def draw_main(self, screen) -> None:
        height, width = screen.getmaxyx()
        views = self.views()
        self.header(screen, views)
        rows = self.rows()
        step_rows = [i for i, r in enumerate(rows) if r[0] == 'step']
        self.cursor = max(0, min(self.cursor, len(step_rows) - 1))
        panel = max(5, min(9, height // 3))
        top, bottom = 2, height - panel - 2
        visible = max(1, bottom - top)
        current_row = step_rows[self.cursor]
        offset = max(0, min(current_row - visible // 2, len(rows) - visible))
        title_width = max(20, min(56, width - 41))
        self.put(screen, 1, 0, '  %-*s %-25s %9s' % (title_width, 'Step', 'State', 'Size'), curses.A_DIM)
        for line, index in enumerate(range(offset, min(len(rows), offset + visible))):
            kind, value = rows[index]
            y = top + line
            if kind == 'group':
                self.put(screen, y, 0, str(value), curses.A_BOLD | self.color('available'))
                continue
            step = value  # type: ignore[assignment]
            view = views[step.id]
            det = self.dets.get(step.id)
            size = human(det.size) if det and det.size and view.code in ('pending', 'partial', 'blocked', 'available') else ''
            selected = index == current_row
            base = curses.A_REVERSE if selected else 0
            self.put(screen, y, 0, ('> ' if selected else '  ') + step.title[:title_width].ljust(title_width) + ' ', base)
            self.put(screen, y, 2 + title_width + 1, view.label[:24].ljust(25), base | self.color(view.code))
            self.put(screen, y, 2 + title_width + 27, size.rjust(9), base)
        screen.hline(height - panel - 1, 0, curses.ACS_HLINE, width)
        step = rows[current_row][1]
        self.draw_panel(screen, step, views[step.id], height - panel, panel - 1, width)  # type: ignore[arg-type]
        self.footer(screen, 'Arrows move  Enter details  i ignore  h history  r refresh  ? help  q quit')

    def draw_panel(self, screen, step: Step, view: StateView, y: int, lines: int, width: int) -> None:
        det = self.dets.get(step.id)
        text: List[Tuple[str, int]] = [(step.title, curses.A_BOLD)]
        if view.detail:
            text.append((view.detail, self.color(view.code)))
        if det is not None and det.summary:
            text.append((det.summary, 0))
        if step.what:
            text.append((' '.join(step.what.split()), curses.A_DIM))
        row = y
        for content, attr in text:
            for piece in textwrap.wrap(content, width - 2) or ['']:
                if row >= y + lines:
                    return
                self.put(screen, row, 1, piece, attr)
                row += 1

    def detail_lines(self, step: Step, view: StateView) -> List[Tuple[str, int, Optional[str]]]:
        _, width = self.screen.getmaxyx()
        det = self.dets.get(step.id)
        items = self.chosen(step)
        out: List[Tuple[str, int, Optional[str]]] = []
        sections = detail_sections(step, det, view, self.ctx, items)
        commands = sections.pop()
        for title, body in sections:
            out.append((title, curses.A_BOLD | self.color('available'), None))
            for line in body:
                for piece in textwrap.wrap(line, width - 4) or ['']:
                    out.append(('  ' + piece, 0, None))
            out.append(('', 0, None))
        if det is not None and det.items:
            marked = self.chosen(step)
            total = sum(i.size or 0 for i in marked)
            out.append(('Items: %d of %d selected%s' % (len(marked), len(det.items), ', ' + human(total) if total else ''),
                        curses.A_BOLD | self.color('available'), None))
            checked = {i.key for i in marked}
            for item in det.items:
                out.append(('  ' + item_line(item, item.key in checked), 0, item.key))
            out.append(('', 0, None))
        out.append((commands[0], curses.A_BOLD | self.color('available'), None))
        for line in commands[1]:
            for piece in textwrap.wrap(line, width - 4, subsequent_indent='    ') or ['']:
                out.append(('  ' + piece, curses.A_DIM, None))
        return out

    def draw_details(self, screen, confirm: bool = False) -> None:
        height, width = screen.getmaxyx()
        step = self.current_step()
        view = self.views()[step.id]
        self.put(screen, 0, 0, ' ' + step.title + ' ', curses.A_REVERSE | curses.A_BOLD)
        self.put(screen, 0, len(step.title) + 3, view.label, self.color(view.code))
        lines = self.detail_lines(step, view)
        selectable = [i for i, line in enumerate(lines) if line[2]]
        visible = height - 3
        if selectable:
            self.detail_cursor = max(0, min(self.detail_cursor, len(selectable) - 1))
            focus = selectable[self.detail_cursor]
            if focus < self.detail_offset:
                self.detail_offset = focus
            elif focus >= self.detail_offset + visible:
                self.detail_offset = focus - visible + 1
        self.detail_offset = max(0, min(self.detail_offset, max(0, len(lines) - visible)))
        for row, index in enumerate(range(self.detail_offset, min(len(lines), self.detail_offset + visible))):
            text, attr, key = lines[index]
            if key and selectable and index == selectable[self.detail_cursor]:
                attr |= curses.A_REVERSE
            self.put(screen, 2 + row, 1, text, attr)
        items = self.chosen(step)
        if confirm:
            ops, _ = preview_ops(step, self.ctx, items)
            sudo = sum(1 for op in ops if op.sudo)
            question = ' Run "%s"? %s%s. [y/N] ' % (
                step.title, plural(len(ops), 'command', 'commands'), ', %d with sudo' % sudo if sudo else '')
            self.put(screen, height - 1, 0, question[:width - 1], curses.A_REVERSE | curses.A_BOLD | self.color('pending'))
            return
        reason = runnable(view, items)
        keys = 'Arrows move  Space toggle  a all  n none  PgUp/PgDn scroll  i ignore  Esc back'
        self.footer(screen, keys + ('  x run' if not reason else '  (' + reason.split('\n')[0][:60] + ')'))

    def draw_history(self, screen) -> None:
        height, width = screen.getmaxyx()
        self.put(screen, 0, 0, ' HISTORY ', curses.A_REVERSE | curses.A_BOLD)
        self.put(screen, 0, 10, '  ' + str(self.history.path))
        lines = history_lines(self.history) or ['No runs recorded yet.']
        visible = height - 3
        self.history_offset = max(0, min(self.history_offset, max(0, len(lines) - visible)))
        for row, line in enumerate(lines[self.history_offset:self.history_offset + visible]):
            self.put(screen, 2 + row, 1, line)
        self.footer(screen, 'Arrows scroll  Esc back')

    def draw_help(self, screen) -> None:
        for row, line in enumerate(HELP):
            self.put(screen, row + 1, 2, line, curses.A_BOLD if row == 0 else 0)

    def current_step(self) -> Step:
        rows = self.rows()
        step_rows = [r for r in rows if r[0] == 'step']
        return step_rows[max(0, min(self.cursor, len(step_rows) - 1))][1]  # type: ignore[return-value]

    # Keys -----------------------------------------------------------------------------
    def toggle_ignore(self, step: Step) -> None:
        ignored = self.history.is_ignored(step.id)
        self.history.record_skip(step.id, step.title, not ignored)
        self.flash('"%s" %s.' % (step.title, 'is no longer ignored' if ignored else 'marked as ignored'))

    def key_main(self, key: int):
        count = len([r for r in self.rows() if r[0] == 'step'])
        if key in (ord('q'), ord('Q')):
            return ('quit',)
        if key in (curses.KEY_DOWN, ord('j')):
            self.cursor = min(count - 1, self.cursor + 1)
        elif key in (curses.KEY_UP, ord('k')):
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_NPAGE:
            self.cursor = min(count - 1, self.cursor + 10)
        elif key == curses.KEY_PPAGE:
            self.cursor = max(0, self.cursor - 10)
        elif key in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
            self.mode, self.detail_cursor, self.detail_offset = 'details', 0, 0
        elif key == ord('i'):
            self.toggle_ignore(self.current_step())
        elif key == ord('h'):
            self.mode, self.history_offset = 'history', 0
        elif key == ord('r'):
            self.refresh_all()
            self.flash('Detecting the state of every step again.')
        elif key == ord('?'):
            self.mode = 'help'
        return None

    def key_details(self, key: int):
        step = self.current_step()
        det = self.dets.get(step.id)
        items = det.items if det else []
        if key in (27, curses.KEY_LEFT, ord('q')):
            self.mode = 'main'
        elif key in (curses.KEY_DOWN, ord('j')):
            if items:
                self.detail_cursor = min(len(items) - 1, self.detail_cursor + 1)
            else:
                self.detail_offset += 1
        elif key in (curses.KEY_UP, ord('k')):
            if items:
                self.detail_cursor = max(0, self.detail_cursor - 1)
            else:
                self.detail_offset = max(0, self.detail_offset - 1)
        elif key == curses.KEY_NPAGE:
            self.detail_offset += 10
        elif key == curses.KEY_PPAGE:
            self.detail_offset = max(0, self.detail_offset - 10)
        elif key == ord(' ') and items:
            item = items[min(self.detail_cursor, len(items) - 1)]
            current = {i.key for i in self.chosen(step)}
            self.overrides.setdefault(step.id, {})[item.key] = item.key not in current
        elif key == ord('a') and items:
            self.overrides[step.id] = {i.key: True for i in items}
        elif key == ord('n') and items:
            self.overrides[step.id] = {i.key: False for i in items}
        elif key == ord('i'):
            self.toggle_ignore(step)
        elif key == ord('x'):
            reason = runnable(self.views()[step.id], self.chosen(step))
            if reason:
                self.flash(reason.split('\n')[0])
            else:
                self.mode = 'confirm'
        return None

    def key_confirm(self, key: int):
        step = self.current_step()
        self.mode = 'details'
        # s/S still means yes: the prompt used to be [s/N].
        if key in (ord('y'), ord('Y'), ord('s'), ord('S')):
            return ('run', step.id, [i.key for i in self.chosen(step)])
        self.flash('Cancelled. Nothing was changed.')
        return None

    def loop(self, screen):
        self.screen = screen
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                background = curses.COLOR_BLACK
            for pair, color in ((1, curses.COLOR_GREEN), (2, curses.COLOR_YELLOW), (3, curses.COLOR_CYAN),
                                (4, curses.COLOR_RED), (5, curses.COLOR_MAGENTA)):
                curses.init_pair(pair, color, background)
        screen.timeout(150)
        screen.keypad(True)
        while True:
            self.poll()
            screen.erase()
            height, width = screen.getmaxyx()
            if height < 16 or width < 70:
                self.put(screen, 0, 0, 'Enlarge the window (minimum 70x16). q quits.')
            elif self.mode == 'main':
                self.draw_main(screen)
            elif self.mode in ('details', 'confirm'):
                self.draw_details(screen, confirm=self.mode == 'confirm')
            elif self.mode == 'history':
                self.draw_history(screen)
            elif self.mode == 'help':
                self.draw_help(screen)
            screen.refresh()
            key = screen.getch()
            if key == -1:
                continue
            if key == curses.KEY_RESIZE:
                continue
            action = None
            if height < 16 or width < 70:
                if key in (ord('q'), ord('Q')):
                    return ('quit',)
            elif self.mode == 'main':
                action = self.key_main(key)
            elif self.mode == 'details':
                action = self.key_details(key)
            elif self.mode == 'confirm':
                action = self.key_confirm(key)
            elif self.mode == 'history':
                if key in (27, ord('q'), curses.KEY_LEFT, ord('h')):
                    self.mode = 'main'
                elif key in (curses.KEY_DOWN, ord('j')):
                    self.history_offset += 1
                elif key in (curses.KEY_UP, ord('k')):
                    self.history_offset = max(0, self.history_offset - 1)
            elif self.mode == 'help':
                self.mode = 'main'
            if action:
                return action


def run_ui(ctx: Context, steps: Sequence[Step], history: History, title: str = 'FREE MY MAC') -> int:
    from .runner import run_step
    locale.setlocale(locale.LC_ALL, '')
    os.environ.setdefault('ESCDELAY', '25')
    app = App(ctx, steps, history, title)
    app.detect()
    try:
        while True:
            action = curses.wrapper(app.loop)
            if not action or action[0] == 'quit':
                return 0
            _, step_id, keys = action
            step = app.by_id[step_id]
            print('\033[2J\033[H', end='')
            print('%s | running a step. Ctrl-C interrupts.\n' % title)
            try:
                run_step(step, ctx, keys, history)
            except KeyboardInterrupt:
                print('\nInterrupted.')
            except Exception as exc:  # noqa: BLE001 - the interface comes back even if the run breaks
                print('\nUnexpected error while running: %s' % exc)
            app.after_run(step_id)
            try:
                input('\nEnter returns to the menu...')
            except (EOFError, KeyboardInterrupt):
                pass
    finally:
        app.shutdown()
