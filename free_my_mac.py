#!/usr/bin/env python3
"""free-my-mac: guided macOS cleanup and development environment consolidation."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fmm.core import Context  # noqa: E402
from fmm.history import History  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='free-my-mac',
        description='Opens the guided interface. Each step shows what it will remove and asks y/N before running.')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--list', action='store_true', help='State of every step (read-only)')
    mode.add_argument('--show', metavar='ID', help='Details of a step, without running it')
    mode.add_argument('--run', metavar='ID', help='Runs a step after y/N confirmation')
    mode.add_argument('--history', action='store_true', help='Recorded runs')
    parser.add_argument('--tweaks', action='store_true',
                        help='Opens the tweaks catalog (debloat and optimization) instead of the cleanup')
    parser.add_argument('--projects', type=Path, default=Path.home() / 'Projects',
                        help='Projects root (default: ~/Projects)')
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if os.geteuid() == 0:
        print('Run without sudo. Steps ask for the password only in the commands that need it.', file=sys.stderr)
        return 2
    ctx = Context(projects=args.projects.expanduser().resolve())
    history = History(ctx.state_dir / 'history.jsonl')
    if args.history:
        from fmm.text import print_history
        print_history(history)
        return 0
    from fmm.steps import CATALOG_TITLES, all_steps
    catalog = 'tweaks' if args.tweaks else 'cleanup'
    steps = all_steps(catalog)
    title = CATALOG_TITLES[catalog]
    try:
        if args.list:
            from fmm.text import print_list
            print_list(ctx, steps, history, title=title)
            return 0
        if args.show:
            from fmm.text import show
            show(ctx, steps, history, args.show)
            return 0
        if args.run:
            from fmm.text import run_text
            return run_text(ctx, steps, history, args.run)
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print('The interface needs a terminal. Use --list or --show ID.', file=sys.stderr)
            return 2
        from fmm.ui import run_ui
        return run_ui(ctx, steps, history, title)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
