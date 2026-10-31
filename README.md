# Free My Mac

A macOS developer environment auditing and maintenance tool with an fzf interface,
inventory by ecosystem, project analysis, and manually selected cleanup plans.
Requires Python 3.9+ and fzf. Interactive mode requires fzf on your PATH.
Dependencies are installed only through an explicit action.

## Start maintenance without waiting for a full audit

Run **without sudo**, from the project directory:

```sh
sh free-my-mac.sh --maintenance
```

The fzf menu lists native maintenance actions by tool. Selecting an action shows
its consequences and asks for confirmation before changing data. Missing tools
are marked as unavailable. Command output remains visible, with an elapsed-time
message every five seconds and a 30-minute timeout. Ctrl-C terminates the process
group; interruption may leave partial changes, recorded in JSON logs.

1. Caches: npm, pip, uv, pnpm, Yarn, Conda, Composer, NuGet, and Go.
2. Homebrew: cleanup, autoremove preview, and individual formula/cask uninstallation.
3. Docker: images without associated containers, build cache, or stopped containers.
   Volumes are not pruned. Local images and writable container layers may be lost.
4. Versions: pyenv, Conda/Miniforge, nvm, fnm, asdf, mise, and rustup. Selection is
   manual; requirements found in projects are displayed before confirmation.
5. Android: SDK packages, images, and AVDs; Apple: shut-down devices and runtimes.
6. Cowork: disable the preference and remove the VM with Claude Desktop closed.
7. Apple Intelligence: install the dependency, inspect models, remove them, or re-enable features.

You can also open individual version management directly:

```sh
sh free-my-mac.sh --maintenance versions --projects ~/Projects
```

File timestamps alone cannot prove that a version or app is unused. Some managers
exclude active/default versions from selection; this does not cover every project,
alias, or external environment. Casks with external installers and unverifiable
removal targets require their own uninstallers. Installed manager scripts/plugins
are executed by their manager and are not audited by this project.

## Remove Apple Intelligence

This module uses the external [RemoveMacAI](https://github.com/omlahore/RemoveMacAI)
CLI. It does not embed that tool's private Apple API implementation or install
anything during an audit:

```sh
# If not installed: preview the action without installing
sh free-my-mac.sh --maintenance ai-install
# Install through Homebrew after confirmation
sh free-my-mac.sh --maintenance ai-install --apply
# Inspect models
sh free-my-mac.sh --maintenance ai-status
# Preview the removal action
sh free-my-mac.sh --maintenance ai-remove
# Run removal after confirmation
sh free-my-mac.sh --maintenance ai-remove --apply
```

The CLI requests model removal through Apple's asset service. The profile that
blocks downloads requires approval in System Settings. File reclamation may be
asynchronous: run `ai-status` again to check. A zero exit code does not guarantee
any particular amount of reclaimed space. Disabling SIP is not a prerequisite
for this mechanism. Ollama/Hugging Face models are not deleted.
`--maintenance ai-restore --apply` re-enables features; new downloads may be needed.
Compatibility depends on the installed RemoveMacAI and macOS versions; the external
tool uses private Apple interfaces. Installation through Homebrew does not pin a version.

## Advanced individual removal

The advanced audit option lets you select one target at a time. A CLI is also available:

```sh
sh free-my-mac.sh --remove-path "$HOME/Library/Application Support/RemovedApp"
sh free-my-mac.sh --remove-path "$HOME/Library/Application Support/RemovedApp" --apply
```

The catalog includes direct children of Application Support/Caches/Preferences,
known Conda installations, specific Python/.NET versions, fonts in `/Library/Fonts`,
old logs in `/Library/Logs`, and a list of optional Apple apps.
Herd can be selected explicitly: this includes settings and potentially local data,
not just PHP. There is no blanket deletion of `/opt` or `/usr/local`.

The executor requires typing the target name, validates its identity/content,
checks open files through lsof, and rejects symlinks in the target path and
protected contents. The absence of visible processes does not prove that no other
user or process will use the target. You select leftovers manually; an absent
application does not prove its data is disposable. Deletion is permanent.

Apps such as Maps/Music can only be removed if the volume is already writable.
The script checks this condition and reports the blocker. **Disabling SIP alone
does not make the sealed system volume writable.** The script does not remount
volumes, change authenticated root, or create boot snapshots. It therefore does
not promise to remove apps from the sealed volume on a standard macOS installation.

When necessary, only the individual-target executor requests sudo; the interface
and package managers continue running as the normal user. The privileged helper
asks for a second confirmation. Its logs are saved to
`/var/log/free-my-mac-remove-*.jsonl`.

Snapshots, the unified logging database, and protected macOS updates are excluded
from generic removal. Disk cleanup does not guarantee CPU/RAM improvements; this
tool does not change global permissions, swap, Spotlight, or services in bulk.

## Open the audit

```sh
cd /path/to/free-my-mac
sh free-my-mac.sh --system --projects ~/Projects --days 60
```

The audit does not delete files. `--system` includes /opt, /usr/local, fonts,
services, and system apps. `--days` defines the activity window; 60 is an adjustable
default. Use your actual projects directory (~/projects may also exist).

Add `--measure` to measure allocated space. Without it, inventory is faster and
sizes are shown as unavailable. Each directory measurement has a 30-second limit.
Thousands of entries may take several minutes. Do not add parent and child sizes
together; APFS/clones and sparse disks prevent estimating reclaimable space by
simply summing sizes.

## fzf interface (default)

The audit shows the current stage/item, total elapsed time, and time spent in the
stage. Ctrl-C cancels the audit and its queries in a separate process. No artificial
percentage is displayed: files are discovered during scanning. On completion, it
shows `LEITURA CONCLUÍDA` (scan complete) and opens the menu. Plan validation also
shows progress and can be canceled.

The application interface and confirmation phrases are currently in Portuguese.
The descriptions below explain the menu actions in English.

1. **Explore all findings:** text search with a detail preview on the right;
   Enter or Esc returns to the menu. This list does not select items for deletion.
2. **Select eligible actions by category:** choose a category or all categories.
   Type to filter; Tab selects; Ctrl-A selects all filtered results; Ctrl-D clears
   filtered selections; Enter adds them to the current selection. If nothing is
   marked, Enter adds the current row, following fzf behavior.
3. **Review current selection:** inspect accumulated items without executing anything.
4. **Save selected plan:** validate and write JSON to the current directory.
5. **Save full report:** write JSON to the current directory.
6. **Clear selection** or **Exit**. Esc at the menu also exits.
7. **Maintenance:** open the native actions described above.
8. **Execute current selection:** save the plan, revalidate, and ask for confirmation.
9. **Advanced individual removal:** show explicitly supported targets.

Further selections accumulate items; clear the selection to start over.
The selector includes caches, eligible node_modules, recognized builds, user fonts,
old installers, and old logs. Builds require explicit selection and do not use the
node_modules age filter. Saving does not execute cleanup. Private reports/plans
(mode 0600) never overwrite existing files. Temporary interface data is stored in
a private directory and discarded on exit; exported files are retained.

The previous interface is still available with `--ui curses`; `--browse` keeps the
directory browser. fzf mode requires an interactive terminal, without sudo.

## Protections

```sh
sh free-my-mac.sh --system --protect ~/Projects/my-agent/artifacts --protect ~/models
```

`--protect` can be repeated. The .claude, .codex, .cursor, .ollama,
.cache/huggingface roots and known local Claude sessions are protected by default.
Names containing sketchpad are also protected. Add your actual artifact roots:
there is no universal way to identify agent-generated files. Protections also
block deletion of ancestor directories containing protected data.

## Preview and execute a plan

Close the managers and processes associated with the selected projects. Execution
requires the same --projects, --days, and --protect options used to create the plan.

```sh
sh free-my-mac.sh --plan free-my-mac-plan-NUMBER.json --days 60
```

The command above only validates/previews. To execute explicitly:

```sh
sh free-my-mac.sh --plan free-my-mac-plan-NUMBER.json --days 60 --apply
```

You must type the exact phrase `APAGAR ITENS` in an interactive terminal. The
executor does not accept root. It validates targets, ownership, protections,
identity, and tree metadata; changes since planning require a new plan. It does
not accept arbitrary commands in the JSON. A JSONL log records the start, result,
free space, and partial failures. Deletion is permanent, without a rollback
promise. Downloads/reinstallation may be required; manual changes inside
node_modules will be lost. Do not run while processes are writing to the targets:
validation is neither a transactional filesystem lock nor a complete detector of
active processes.

node_modules only becomes a candidate when source files are old, a lockfile exists,
and inspection completes without errors. Dependencies known to be tracked in Git
are blocked. This indicates inactivity, not proven disuse: check whether the
project is needed offline and whether dependencies can be restored. Monorepos
and workspaces require review.

## Explicit Cowork maintenance

```sh
sh free-my-mac.sh --cowork status
sh free-my-mac.sh --cowork disable
```

Status and preview do not change anything. To apply after closing Claude Desktop,
run `sh free-my-mac.sh --cowork disable --apply`. This requires interactive
confirmation, creates a private JSON backup, and restricts removal to vm_bundles;
sessions and MCP settings are preserved. The local preference's behavior depends
on the version. See the [Cowork guide (Portuguese)](docs/guia-cowork.md) for
limitations, reversal, and validation.

## Reports and storage growth

```sh
sh free-my-mac.sh --report --system --no-probes
sh free-my-mac.sh --json --system --measure --save inventory-1.json
sh free-my-mac.sh --json --system --measure --save inventory-2.json
sh free-my-mac.sh --compare inventory-1.json inventory-2.json
```

`--no-probes` skips manager and Docker queries but still reads local app metadata
through macOS plutil. Comparison includes only paths measured in both reports;
unknown values are not treated as zero. Remote Docker endpoints and overrides are
blocked; daemons are not started. A Unix socket may forward to another host;
check the displayed endpoint before confirming any prune operation.

The legacy browser remains available:

```sh
sh free-my-mac.sh --browse --path ~/Library
```

## Tests

```sh
python3 -m unittest discover -s tests -v
sh -n free-my-mac.sh
```

Deletion tests use temporary directories exclusively. No sudo is required.
No real user data was cleaned during development.
