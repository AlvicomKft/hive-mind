---
name: setup
description: "Check and finish the Hive Mind install — use for '/hive-mind:setup', 'set up hive mind', 'connect hive mind', 'hive mind is not working', or after installing the plugin."
---

# setup

Reports whether Hive Mind is ready on this machine and, when it is not, tells the user the one
command they have to run themselves.

`${ARGUMENTS}` is the Athene app URL (the one the user opens in a browser), optional.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/hive_mind.py doctor
```

Checks print as `ok  ` / `FAIL` plus a label: `config`, `server`, `token`, `hook`, `repo`,
`last beam`. `server` and `token` are skipped entirely when `config` fails, so a short table is
normal. Exit 1 means at least one check failed. Read the labels, do not re-run other commands to
double-check them.

## Everything passed

Print the doctor table as is, then one first run:

```
/hive-mind:search what did we do today
```

## `config` failed

The user has no token yet, or no config file. Never ask for the token in chat and never run
`login` yourself — the token is secret and the prompt is hidden. Tell them, in this order:

1. Mint a token at `<app-url>/profile`, in the Hive Mind section (shown once, copy it).
2. Run this themselves — the leading `!` keeps the hidden prompt interactive in Claude Code:

   ```
   !python3 ${CLAUDE_PLUGIN_ROOT}/hive_mind.py login --server <app-url>
   ```

3. Re-run `/hive-mind:setup`.

If `${ARGUMENTS}` is empty, ask once for the app URL and use it in both lines; do not guess a
host. Add one line: `--root ~/folder` (repeatable) on that same `login` limits beaming to
checkouts under those folders; the default is every git checkout with an `origin` remote.

## `server` or `token` failed

The config exists but the server rejected it: the URL is wrong, the token was revoked, or Hive
Mind is off for that deployment. Relay the failing line verbatim and point at the same `login`
command to re-point or re-mint. Do not retry in a loop.

## `hook` failed

The plugin is installed but no hook is registered — usually a missed `/reload-plugins`. Say that
first; `python3 ${CLAUDE_PLUGIN_ROOT}/hive_mind.py install --harness claude` is the fallback for
a non-plugin install.

## `repo` failed

Only means the current directory has no `origin` remote, so nothing is beamed from here. Not a
setup problem — say so and move on.
