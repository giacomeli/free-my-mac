# Turn off the local Cowork VM and keep the app installed

This guide contains no user names, projects or private paths. Public names of Claude
and of the tools are kept so the procedure can be identified.

## Compatibility and expected effect

Anthropic documents `secureVmFeaturesEnabled` as the access control for Cowork in the
[enterprise configuration](https://support.claude.com/en/articles/12622667-enterprise-configuration-for-claude-desktop).
That does not guarantee the same support for the key under `preferences` in the local
JSON in every version. The step below applies this local setting, and you should check
it when you reopen the app and after updates. It does not promise to block every
future download.

The step does not uninstall Claude Desktop and does not touch the Claude Code install,
`~/.claude`, local sessions or MCP settings. We cannot guarantee that every integration
and feature of the app behaves the same after Cowork is turned off.

## Check without changing anything

In the script folder, without sudo:

```sh
sh free-my-mac.sh --show cowork
```

This shows what is left to do (turn off the preference, delete `vm_bundles` and its
size) and the command that would run. If the preference is already `false` and the
folder does not exist, the state is "Already clean" (or "Done", if this tool did it).

## Apply

1. In Claude Desktop, use Developer > Open App Config File, if available, to check
   the file. The step expects the default path
   `~/Library/Application Support/Claude/claude_desktop_config.json`.
2. Quit the app with Cmd+Q. Do not keep VM sessions running.
3. Run it and confirm with `y`:

   ```sh
   sh free-my-mac.sh --run cowork
   ```

   Or, in the interface (`sh free-my-mac.sh`), open the "Claude Cowork: turn off and
   delete the VM" step in the Maintenance group and press `x`.
4. The step checks that Claude Desktop is closed and that no process is using the
   VM. It writes a private backup of the configuration to
   `~/bkp/free-my-mac/<date>/claude/`, keeps the other keys and writes
   `preferences.secureVmFeaturesEnabled = false`.
5. It permanently removes only `~/Library/Application Support/Claude/vm_bundles`, if
   it exists. It refuses a symbolic link, JSON with duplicate keys and a configuration
   that changes during the operation. It does not empty the Trash and does not clean
   the whole Claude folder.
6. Reopen the app and check your chats and integrations. Run `--show cowork` again.

The backup only protects the JSON, not the contents of the VM. If the removal fails
after the preference was changed, the step ends with a failure, the preference stays
written and the backup stays in place. Do not reopen the app while the operation is in
progress. There is no transactional lock against apps started at the same time.

## Manual alternative

With the app closed, back up the JSON and set only this property, keeping all the
others:

```json
{
  "preferences": {
    "secureVmFeaturesEnabled": false
  }
}
```

The snippet is illustrative; it does not replace the whole file. Remove only
`vm_bundles` in the Finder. Moving it to the Trash does not necessarily free up space:
delete only that item permanently once you are sure, without emptying other items.

## Rollback and checks after an update

With the app closed, restore the previous preference from the backup, keeping later
changes to the rest of the configuration. The backup does not restore the deleted VM;
bringing the feature back may require the app to provision or download it again. If
an update recreates the folder or ignores the preference, the step shows up as Pending
again; review the version and the official options before repeating the removal.

Do not share the real JSON or its backup: they may contain MCP credentials.
