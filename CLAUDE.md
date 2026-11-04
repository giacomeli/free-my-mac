# free-my-mac

## Language

Everything in this repository is in English: code, interface text, comments, docstrings, README
and docs. This is a deliberate exception to the owner's global rule (Portuguese interface,
comments and docs), decided on 2026-11-04 because the repository is public. Commit messages stay
in Brazilian Portuguese, and so do the plans in `specs/`, which is gitignored.

## Invariants

- The repository is public. Names that only make sense on one machine (projects, tools, own
  launch agents, hostnames) go in `~/.config/free-my-mac/local.json` (`fmm/localconfig.py`),
  never in code, tests or fixtures. Test fixtures use made-up names.
- Python 3.9 standard library only, run with `/usr/bin/python3`: the tool must never depend on an
  interpreter that one of its steps can remove.
- `detect()` is read-only. Every change goes through `ops()` and the runner, which detects again
  and asks `y/N` before running.

## Known pitfalls

- Apple's system Python caches bytecode in `~/Library/Caches/com.apple.python`, keyed by mtime
  and size. Restoring or editing a file within the same second with the same size can leave stale
  bytecode in use; `touch` the source file.
- `defaults write` into `/Library/Managed Preferences` returns 0 but writes nothing (cfprefsd
  drops it). Forced policies need a configuration profile the user approves.

## Verification

```sh
/usr/bin/python3 -m unittest discover -s tests
uvx ruff@0.14.0 check --select F,E9,B006,B008 --target-version py39 fmm free_my_mac.py tests
sh -n free-my-mac.sh && sh -n tweaks.sh
```
