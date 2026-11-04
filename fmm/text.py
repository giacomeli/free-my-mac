"""Parallel detection, detail texts and text modes (--list, --show, --run, --history)."""
from __future__ import annotations

import concurrent.futures
import os
import sys
import textwrap
import traceback
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .core import (ERROR, Context, Detection, Item, Op, Step, StateView, format_when, human, plural, redact,
                   resolve_states)
from .fs import disk_free, disk_total
from .history import History

STATUS_LABELS = {'ok': 'Completed', 'failed': 'Failed', 'interrupted': 'Interrupted',
                 'cancelled': 'Cancelled', 'nothing': 'Nothing to do'}


def safe_detect(step: Step, ctx: Context) -> Detection:
    try:
        return step.detect(ctx)
    except Exception as exc:  # noqa: BLE001 - detecting one step never brings the interface down
        detail = '%s: %s' % (type(exc).__name__, exc)
        if os.environ.get('FMM_DEBUG'):
            detail += '\n' + traceback.format_exc()
        return Detection(ERROR, summary=detail)


def detect_all(ctx: Context, steps: Sequence[Step],
               progress: Optional[Callable[[int, int, str], None]] = None) -> Dict[str, Detection]:
    results: Dict[str, Detection] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(safe_detect, s, ctx): s for s in steps}
        for count, future in enumerate(concurrent.futures.as_completed(futures), 1):
            step = futures[future]
            results[step.id] = future.result()
            if progress:
                progress(count, len(steps), step.title)
    return results


def selected_items(det: Optional[Detection], overrides: Optional[Dict[str, bool]] = None) -> List[Item]:
    if det is None:
        return []
    overrides = overrides or {}
    return [i for i in det.items if overrides.get(i.key, i.selected)]


def preview_ops(step: Step, ctx: Context, items: List[Item]) -> Tuple[List[Op], Optional[str]]:
    if not items:
        return [], None
    try:
        return step.ops(ctx, items), None
    except Exception as exc:  # noqa: BLE001
        return [], '%s: %s' % (type(exc).__name__, exc)


def runnable(view: StateView, items: List[Item]) -> Optional[str]:
    """None if it can run; otherwise, the reason."""
    if view.code == 'blocked':
        return view.detail
    if view.code == 'ignored':
        return 'Step ignored. Press i to consider it again.'
    if view.code == 'detecting':
        return 'Still detecting.'
    if view.code == 'error':
        return 'Detection failed; press r to try again.'
    if not items:
        return 'No items selected.' if view.code in ('pending', 'partial', 'available') else 'Nothing to do.'
    return None


def detail_sections(step: Step, det: Optional[Detection], view: StateView, ctx: Context,
                    items: List[Item]) -> List[Tuple[str, List[str]]]:
    sections: List[Tuple[str, List[str]]] = []
    for title, body in (('What it does', step.what), ('Why', step.why),
                        ('Consequences', step.consequences), ('How to undo', step.undo)):
        if body:
            sections.append((title, [line for line in body.strip().splitlines()]))
    state = [view.label + (' - ' + view.detail if view.detail else '')]
    if det is not None and det.summary:
        state.append(det.summary)
    sections.append(('State', state))
    if det is not None and det.notes:
        sections.append(('Notes', list(det.notes)))
    ops, error = preview_ops(step, ctx, items)
    commands = ['$ ' + redact(op.display) for op in ops]
    if error:
        commands = ['Could not build the commands: ' + error]
    if not commands:
        commands = ['(no commands: nothing checked or nothing to do)']
    sections.append(('Commands', commands))
    return sections


def item_line(item: Item, checked: bool) -> str:
    mark = '[x]' if checked else '[ ]'
    size = human(item.size) if item.size else ''
    text = '%s %s' % (mark, item.label)
    if size:
        text += '  (%s)' % size
    if item.note:
        text += '  - ' + item.note
    return text


def disk_line(ctx: Context) -> str:
    return 'Free: %s of %s' % (human(disk_free(ctx.home)), human(disk_total(ctx.home)))


def pending_text(total: int, count: int) -> str:
    if not count:
        return 'Nothing pending'
    steps = '%d step%s' % (count, '' if count == 1 else 's')
    return 'Pending: ~%s in %s' % (human(total), steps) if total else 'Pending: ' + steps


def pending_total(steps: Sequence[Step], dets: Dict[str, Optional[Detection]],
                  views: Dict[str, StateView]) -> Tuple[int, int]:
    total, count = 0, 0
    for step in steps:
        det, view = dets.get(step.id), views.get(step.id)
        if det is not None and view is not None and view.code in ('pending', 'partial', 'blocked'):
            count += 1
            total += det.size or 0
    return total, count


# Text modes ------------------------------------------------------------------------

def _progress(count: int, total: int, title: str) -> None:
    if sys.stderr.isatty():
        sys.stderr.write('\r\033[KDetecting %d/%d: %s' % (count, total, title[:60]))
        sys.stderr.flush()


def _end_progress() -> None:
    if sys.stderr.isatty():
        sys.stderr.write('\r\033[K')
        sys.stderr.flush()


def print_list(ctx: Context, steps: Sequence[Step], history: History, out=None, title: str = 'FREE MY MAC') -> None:
    from .steps import GROUP_TITLES
    out = out or sys.stdout
    dets = detect_all(ctx, steps, _progress)
    _end_progress()
    views = resolve_states(steps, dets, history)
    total, count = pending_total(steps, dets, views)
    print('%s  %s  |  %s' % (title, disk_line(ctx), pending_text(total, count)), file=out)
    group = None
    for step in steps:
        if step.group != group:
            group = step.group
            print('\n' + GROUP_TITLES.get(group, group), file=out)
        view, det = views[step.id], dets[step.id]
        size = human(det.size) if view.code in ('pending', 'partial', 'blocked', 'available') and det.size else ''
        print('  %-20s %-50s %-24s %9s' % (step.id, step.title[:50], view.label[:24], size), file=out)
        if view.code in ('blocked', 'error') and view.detail:
            print('  %-20s %s' % ('', view.detail.splitlines()[0][:100]), file=out)


def requirements_closure(steps: Sequence[Step], step_id: str) -> List[Step]:
    by_id = {s.id: s for s in steps}
    needed: Set[str] = set()
    stack = [step_id]
    while stack:
        current = stack.pop()
        if current in needed or current not in by_id:
            continue
        needed.add(current)
        stack.extend(by_id[current].requires)
    return [s for s in steps if s.id in needed]


def show(ctx: Context, steps: Sequence[Step], history: History, step_id: str, out=None,
         width: int = 100) -> Tuple[Step, Detection, StateView, List[Item]]:
    out = out or sys.stdout
    if step_id not in {s.id for s in steps}:
        raise ValueError('Unknown step: %s. Use --list to see the ids.' % step_id)
    subset = requirements_closure(steps, step_id)
    dets = detect_all(ctx, subset, _progress)
    _end_progress()
    views = resolve_states(subset, dets, history)
    step = [s for s in subset if s.id == step_id][0]
    det, view = dets[step_id], views[step_id]
    items = selected_items(det)
    print(step.title + '  [' + step.id + ']', file=out)
    print('=' * min(width, len(step.title) + len(step.id) + 4), file=out)
    for title, lines in detail_sections(step, det, view, ctx, items)[:-1]:
        _print_section(title, lines, out, width)
    if det.items:
        _print_section('Items (%d)' % len(det.items), [item_line(i, i.selected) for i in det.items], out, width,
                       hang='    ')
    _print_section(*detail_sections(step, det, view, ctx, items)[-1], out=out, width=width, hang='    ')
    return step, det, view, items


def _print_section(title: str, lines: Iterable[str], out=None, width: int = 100, hang: str = '') -> None:
    out = out or sys.stdout
    print('\n' + title + ':', file=out)
    for line in lines:
        wrapped = textwrap.wrap(line, width - 2, subsequent_indent=hang, break_on_hyphens=False) or ['']
        for piece in wrapped:
            print('  ' + piece, file=out)


def read_key() -> str:
    if not sys.stdin.isatty():
        return sys.stdin.readline()[:1]
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return os.read(fd, 1).decode(errors='ignore')
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def confirm(question: str) -> bool:
    print(question + ' [y/N] ', end='', flush=True)
    key = read_key()
    print(key if key.strip() else '')
    # s/S still means yes: the prompt used to be [s/N].
    return key in ('y', 'Y', 's', 'S')


def run_text(ctx: Context, steps: Sequence[Step], history: History, step_id: str) -> int:
    from .runner import run_step
    step, det, view, items = show(ctx, steps, history, step_id)
    reason = runnable(view, items)
    print()
    if reason:
        print('Cannot run: ' + reason)
        return 1
    if not sys.stdin.isatty():
        print('Running requires an interactive terminal to confirm.')
        return 1
    if not confirm('Run this step?'):
        print('Cancelled. Nothing was changed.')
        return 0
    print()
    result = run_step(step, ctx, [i.key for i in items], history)
    return 0 if result.status in ('ok', 'nothing') else 1


def history_lines(history: History, limit: int = 200) -> List[str]:
    lines = []
    for entry in reversed(history.entries()[-limit:]):
        when = format_when(entry['ts'])
        if entry.get('kind') == 'run':
            status = STATUS_LABELS.get(entry.get('status', ''), entry.get('status', ''))
            freed = ''
            if entry.get('free_before') is not None and entry.get('free_after') is not None:
                delta = entry['free_after'] - entry['free_before']
                if delta > 0:
                    freed = 'freed ' + human(delta)
            ops = entry.get('ops') or []
            failed = [o for o in ops if o.get('code')]
            detail = plural(len(ops), 'command', 'commands') + (', %d failed' % len(failed) if failed else '')
            lines.append('%s  %-13s %-46s %-18s %s' % (when, status, entry.get('title', entry['step'])[:46], freed, detail))
        else:
            action = 'Ignored' if entry.get('kind') == 'ignore' else 'Unignored'
            lines.append('%s  %-13s %s' % (when, action, entry.get('title', entry['step'])))
    return lines


def print_history(history: History, out=None) -> None:
    out = out or sys.stdout
    lines = history_lines(history)
    if not lines:
        print('No runs recorded yet (%s).' % history.path, file=out)
        return
    print('History (%s), most recent first:' % history.path, file=out)
    for line in lines:
        print('  ' + line, file=out)
