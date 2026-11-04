"""Running a step in the normal terminal: revalidation, sudo, live output and history."""
from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .core import Context, Item, Op, Step, human, redact
from .fs import disk_free
from .history import History

BOLD = '\033[1m'
DIM = '\033[2m'
GREEN = '\033[32m'
RED = '\033[31m'
YELLOW = '\033[33m'
CYAN = '\033[36m'
RESET = '\033[0m'


def paint(text: str, color: str, stream=None) -> str:
    stream = stream or sys.stdout
    return color + text + RESET if getattr(stream, 'isatty', lambda: False)() else text


@dataclass
class RunResult:
    status: str
    ops: List[Dict] = field(default_factory=list)
    freed: Optional[int] = None
    message: str = ''


def run_step(step: Step, ctx: Context, selected: Sequence[str], history: History,
             out=None) -> RunResult:
    """Detects again, runs only the checked items that still exist, and records the run."""
    out = out or sys.stdout

    def say(text: str = '') -> None:
        print(text, file=out, flush=True)

    ctx.invalidate()
    try:
        fresh = step.detect(ctx)
    except Exception as exc:  # noqa: BLE001
        say(paint('Fresh detection failed (%s); nothing was run.' % exc, RED, out))
        return RunResult('cancelled', message=str(exc))
    wanted = set(selected)
    items: List[Item] = [i for i in fresh.items if i.key in wanted]
    vanished = len(wanted - {i.key for i in fresh.items})
    say(paint(step.title, BOLD, out))
    if vanished:
        say(paint(('%d item no longer exists and was skipped.' if vanished == 1
                    else '%d items no longer exist and were skipped.') % vanished, DIM, out))
    if not items:
        say('Nothing to do: the checked items no longer exist.')
        return RunResult('nothing')
    try:
        ops: List[Op] = step.ops(ctx, items)
    except Exception as exc:  # noqa: BLE001
        say(paint('Could not build the commands (%s); nothing was run.' % exc, RED, out))
        return RunResult('cancelled', message=str(exc))
    if not ops:
        say(paint('No commands for the checked items; nothing was run. See the step notes.',
                  YELLOW, out))
        return RunResult('cancelled', message='no commands for the checked items')
    if any(op.sudo for op in ops) and ctx.real_system:
        say(paint('Some commands need administrator privileges. sudo will ask for your password.', YELLOW, out))
        if ctx.call(['/usr/bin/sudo', '-v']):
            say(paint('sudo not authorized; nothing was run.', RED, out))
            return RunResult('cancelled', message='sudo refused')
    before = disk_free(ctx.home)
    results: List[Dict] = []
    status = 'ok'
    try:
        for index, op in enumerate(ops, 1):
            say()
            say(paint('[%d/%d] %s' % (index, len(ops), op.label), BOLD, out))
            say(paint('$ ' + redact(op.display), DIM, out))
            if op.argv is not None:
                argv = (['/usr/bin/sudo'] if op.sudo and ctx.real_system else []) + list(op.argv)
                code = ctx.call(argv, cwd=op.cwd)
            else:
                try:
                    op.func(ctx)  # type: ignore[misc]
                    code = 0
                except Exception as exc:  # noqa: BLE001 - any failure of the action becomes a failure of the op
                    say(paint('Failed: %s' % exc, RED, out))
                    if not isinstance(exc, (OSError, RuntimeError, ValueError)):
                        traceback.print_exc(file=out)
                    code = 1
            results.append(dict(label=op.label, display=op.display, code=code))
            if code:
                if op.tolerate_failure:
                    say(paint('Tolerated failure (code %d); continuing.' % code, YELLOW, out))
                    continue
                say(paint('Failed with code %d. The remaining commands were not run.' % code, RED, out))
                status = 'failed'
                break
            say(paint('ok', GREEN, out))
    except KeyboardInterrupt:
        status = 'interrupted'
        say()
        say(paint('Interrupted. There may be partial changes; the state will be detected again.', YELLOW, out))
    except Exception as exc:  # noqa: BLE001 - always record what was already done
        status = 'failed'
        say(paint('Unexpected error: %s' % exc, RED, out))
        traceback.print_exc(file=out)
    finally:
        after = disk_free(ctx.home)
        history.record_run(step.id, step.title, status, [i.label for i in items], results, before, after)
    freed = after - before
    say()
    if status == 'ok':
        say(paint('Completed.', GREEN, out) + ' Free space: %s (%s%s).' % (
            human(after), '+' if freed >= 0 else '-', human(abs(freed))))
    else:
        say(paint('Finished with status: %s.' % status, RED, out))
    return RunResult(status, results, freed)
