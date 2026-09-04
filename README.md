<p align="center"><img src="assets/banner.png" alt="Hive Mind — shared, searchable session history for coding agents" width="1280"></p>

# Hive Mind

Shared, searchable Claude Code / Codex session history for a team. A hook ships each finished
assistant turn to your Athene server; a CLI and three agent skills search it back, scoped to the
current repo's `origin` remote. Ask what a teammate tried yesterday, dig up your own reasoning
from before a compaction, or see where the tokens went — without anyone pasting transcripts
around. Why it exists, and what it will never do with the data: [vision.md](vision.md).

## What You Get

- a hook that beams every finished turn in the background, silently, with secrets scrubbed
- `/hive-mind:search` to search and read the team's history from inside a session
- `/hive-mind:share` to hand a teammate a pointer to the session you are in
- `/hive-mind:setup` to check the install and tell you what is missing
- `hive-mind`, one stdlib-only Python file that is both the hook and the CLI

## Requirements

- **An Athene account on a deployment with Hive Mind enabled**, plus a personal access token
  minted on your profile page (Hive Mind section, shown once).
- **Python 3.11 or later**
- **git** — only checkouts with an `origin` remote are beamed

## Install

Add the marketplace in Claude Code:

```bash
/plugin marketplace add AlvicomKft/hive-mind
```

Install the plugin:

```bash
/plugin install hive-mind
```

Reload plugins:

```bash
/reload-plugins
```

Then run:

```bash
/hive-mind:setup https://athene.example.com
```

`/hive-mind:setup` tells you whether Hive Mind is ready. If you have no token yet it points you at
your profile page and hands you the one command you run yourself:

```bash
!python3 ~/.claude/plugins/marketplaces/hive-mind/hive_mind.py login --server https://athene.example.com
```

The leading `!` keeps the hidden token prompt interactive. It verifies the token and stores it in
`~/.config/hive-mind/config.json` (mode 600).

After install, you should see:

- the three skills listed in `/help`
- `ok` on every line of `/hive-mind:setup`
- your own session in the Athene viewer after the next turn

One simple first run is:

```bash
/hive-mind:search what did we do today
```

## Usage

### `/hive-mind:search`

Searches the team's sessions and reads back the parts that matter. Use it for "did we already try
this", "why is it done this way", "what is X working on", or anything an earlier session decided.
It stays bounded on purpose: it fetches a session to disk and greps it rather than pulling a whole
transcript into your context.

### `/hive-mind:share`

Prints the pointer for the session you are in — title, web link, and the `show` command a teammate
runs in their own terminal. Read-only; the session is already beamed.

### `/hive-mind:setup`

Runs the doctor checks (config, server, token, hook, repo, last beam) and reports. On a failure it
says exactly which command you run, and never asks for your token in chat.

### CLI

```bash
hive-mind today                                      # what the team touched today, each with its latest reply
hive-mind search kb sync retry --since 14d           # AND-ed terms, grouped by session
hive-mind search -e 'alembic (upgrade|downgrade)' --flat -C 1
hive-mind sessions --author laszlo --titles --limit 10
hive-mind fetch <shortId>                            # session → ~/.cache/hive-mind/sessions/<id>.jsonl, then rg/jq it
hive-mind show <shortId> --around 63 -C 5            # render the window around a hit
hive-mind show <shortId> --last 10                   # render the final turns (bare show = last 30)
hive-mind tail --since today                         # today's turns across the project (bare tail = only what is new)
hive-mind usage --since 3h --mine                    # token burn per session and model in a window
hive-mind share                                      # web link + show command for this session
hive-mind purge <shortId>                            # something slipped
hive-mind local --since 30d                          # transcripts on this laptop, beamed or not
hive-mind beam <shortId>                             # ship an older session, sorted by its own time
hive-mind doctor                                     # config, server, token, hook install
hive-mind hook --dry-run < event.json                # see what would be sent
```

`hive-mind` stands for `python3 ~/.claude/plugins/marketplaces/hive-mind/hive_mind.py`; see the FAQ
for putting it on your PATH.

Ids print as 8-char prefixes and every command that takes one accepts a unique prefix.
`--project SUBSTR` / `--all` widen the scope, `--mine` narrows to your own sessions. On list and
search commands `--json` gives raw API output, `--tsv` the same columns tab-separated for
`cut`/`awk`, `-v` full ids, scores and web links. Bare `show` is bounded (last 30 turns, 600
chars each) so an agent cannot flood its context; `fetch` + `rg`/`jq` is the way to explore.

## Typical Flows

### Dig back before a compaction

```bash
/hive-mind:search why did we drop the retry wrapper in kb sync
```

The reasoning is in the transcript even when it is gone from your context.

### See what a teammate is doing

```bash
hive-mind sessions --author laszlo --titles --limit 10
hive-mind show <shortId> --last 10
```

### Share the session you are in

```bash
/hive-mind:share
```

### Check where the tokens went

```bash
hive-mind usage --since 3h --mine
```

One line per session and model: turns, uncached input, output, cache read, cache creation. Cache
reads dominate the bill on long sessions, which is why they are never summed into input.

## FAQ

### What gets sent, and what never does?

Sent per turn: session id, normalized remote (`github.com/org/repo`), branch, cwd, first prompt as
title, token counts, the assistant models used, and messages of three kinds: your prompts,
assistant text, and one-line tool calls (`Bash {"command":"git status"}`, input cut at 200 chars).
Author is derived from your token on the server. Subagent runs are shipped the same way, as child
sessions of the main one.

Never sent: tool results (file contents, command output), thinking blocks, system/harness
bookkeeping lines.

### What is scrubbed before sending?

From `scrub_patterns.json`, seeded from gitleaks rules: AWS keys, OpenAI/Anthropic `sk-`, GitHub
`gh?_`, Slack `xox?-`, Google `AIza`, JWTs, PEM private keys, `password|secret|token|api_key = ...`
assignments, basic-auth URLs, Athene `athmind_` tokens, and high-entropy strings of 32+ chars that
mix character classes (paths, UUIDs and git SHAs are left alone). Tool calls whose input mentions
`env`, `.env`, `secret`, `token`, `password`, `credentials`, `gh auth`, `.pem`, `.key`, `.netrc`,
`.npmrc`, `kubeconfig`, `.aws/`, `.ssh/` are sent as `<tool> [redacted]`.

Add your own regexes, one per line, in `.hive-mind-ignore` at the repo root or
`~/.config/hive-mind/ignore`. Something slipped anyway? `hive-mind purge <shortId>` deletes a
session you own.

### How do I pause it?

`HIVE_MIND=off` in the shell disables the hook for that shell.

### Can I limit it to certain folders?

Yes: `hive-mind login --root ~/alvicom` (repeatable) beams only checkouts under those folders. The
default is every git checkout with an `origin` remote.

### How do I uninstall?

`/plugin uninstall hive-mind` in Claude Code. For a non-plugin install,
`hive-mind install --harness claude --uninstall` reverts the `~/.claude/settings.json` hooks and
the skill link (`--dry-run` shows the changes first). Neither deletes what you already beamed —
use `hive-mind purge` for that.

### Do I need uv?

No. The plugin copy is what the hook runs, and it updates with Claude Code. If you use the CLI by
hand a lot, `uv tool install git+https://github.com/AlvicomKft/hive-mind` puts `hive-mind` on your
PATH (`uv tool upgrade hive-mind` to update).

### Where does it keep state?

`~/.local/state/hive-mind/<session-id>.json` (byte offset, next seq, token totals) and `hook.log`
(failures; the hook always exits 0 and never blocks the harness). Fetched sessions cache under
`~/.cache/hive-mind/sessions/<id>.jsonl`. Env overrides: `HIVE_MIND_CONFIG`,
`HIVE_MIND_STATE_DIR`, `HIVE_MIND_CACHE_DIR`.

## Develop

```bash
python3 -m unittest discover -s tests
python3 -m py_compile hive_mind.py
```

The Codex parser (`rollout-*.jsonl`) is best-effort and not yet exercised against real transcripts
in this repo.
