# Hive Mind

Shared, searchable Claude Code / Codex session history for a team. A hook ships
each finished assistant turn to your Athene server; a CLI and an agent skill
search it back, scoped to the current repo's `origin` remote.

Why it exists, and what it will never do with the data: [vision.md](vision.md).

Stdlib-only Python 3.11+, one file: `hive_mind.py` is both the hook and the CLI.

## Install

1. Mint a personal access token on your Athene profile page.
2. In Claude Code:

```
/plugin marketplace add AlvicomKft/hive-mind
/plugin install hive-mind
```

3. In a terminal, `hive-mind login --server https://athene.example.com` (the URL you open Athene
   at). It asks for the token with hidden input, verifies it, and stores both in
   `~/.config/hive-mind/config.json` (mode 600). Done: every turn in a git checkout with an
   `origin` remote is beamed from now on, silently.

The `hive-mind` command is `python3 ~/.claude/plugins/marketplaces/hive-mind/hive_mind.py`;
alias it, or use `uvx --from git+https://github.com/AlvicomKft/hive-mind hive-mind`.

Options, only if you need them:

- `hive-mind login --root ~/alvicom` (repeatable) beams only checkouts under those folders.
- `hive-mind install --harness claude --server …` does the same setup without the plugin
  manager (registers the hooks in `~/.claude/settings.json`, links the skill); `--uninstall`
  reverts, `--dry-run` shows the changes.
- `HIVE_MIND=off` in the shell pauses the hook. `hive-mind doctor` checks everything.

## Use

```bash
hive-mind today                                      # what the team touched today, each with its latest reply
hive-mind search kb sync retry --since 14d           # AND-ed terms, grouped by session
hive-mind search -e 'alembic (upgrade|downgrade)' --flat -C 1
hive-mind sessions --author laszlo --titles --limit 10
hive-mind fetch <shortId>                            # session → ~/.cache/hive-mind/sessions/<id>.jsonl, then rg/jq it
hive-mind show <shortId> --around 63 -C 5            # render the window around a hit
hive-mind show <shortId> --last 10                   # render the final turns (bare show = last 30)
hive-mind tail --since today                         # today's turns across the project (bare tail = only what is new)
hive-mind share                                      # web link + show command for this session
hive-mind purge <shortId>                            # something slipped
hive-mind local --since 30d                          # transcripts on this laptop, beamed or not
hive-mind beam <shortId>                             # ship an older session, sorted by its own time
hive-mind doctor                                     # config, server, token, hook install
hive-mind hook --dry-run < event.json                # see what would be sent
```

Ids print as 8-char prefixes and every command that takes one accepts a unique prefix.
`--project SUBSTR` / `--all` widen the scope, `--mine` narrows to your own sessions. On list and
search commands `--json` gives raw API output, `--tsv` the same columns tab-separated for
`cut`/`awk`, `-v` full ids, scores and web links. Bare `show` is bounded (last 30 turns, 600
chars each) so an agent cannot flood its context; `fetch` + `rg`/`jq` is the way to explore.

## What is sent, what is not

Sent per turn: session id, normalized remote (`github.com/org/repo`), branch, cwd,
first prompt as title, token totals, the assistant models used, and messages of three kinds: your prompts,
assistant text, and one-line tool calls (`Bash {"command":"git status"}`, input cut
at 200 chars). Author is derived from your token on the server. Subagent runs are
shipped the same way, as child sessions of the main one.

Never sent: tool results (file contents, command output), thinking blocks,
system/harness bookkeeping lines.

Scrubbed before sending (`scrub_patterns.json`, seeded from gitleaks rules):
AWS keys, OpenAI/Anthropic `sk-`, GitHub `gh?_`, Slack `xox?-`, Google `AIza`,
JWTs, PEM private keys, `password|secret|token|api_key = ...` assignments,
basic-auth URLs, Athene `athmind_` tokens, and any 32+ char high-entropy string
(entropy > 4.5 bits, so UUIDs and git SHAs pass). Tool calls whose input mentions
`env`, `.env`, `secret`, `token`, `password`, `credentials`, `gh auth`, `.pem`,
`.key`, `.netrc`, `.npmrc`, `kubeconfig`, `.aws/`, `.ssh/` are sent as `<tool> [redacted]`.

Escape hatches:

- `HIVE_MIND=off` in the shell disables the hook.
- Extra regexes, one per line: `.hive-mind-ignore` at the repo root or `~/.config/hive-mind/ignore`.
- `hive-mind purge <session-id>` deletes a session you own.

State: `~/.local/state/hive-mind/<session-id>.json` (byte offset, next seq, token
totals) and `hook.log` (failures; the hook always exits 0 and never blocks the harness).
Fetched sessions cache under `~/.cache/hive-mind/sessions/<id>.jsonl`.
Env overrides: `HIVE_MIND_CONFIG`, `HIVE_MIND_STATE_DIR`, `HIVE_MIND_CACHE_DIR`.

## Develop

```bash
python3 -m unittest discover -s tests
python3 -m py_compile hive_mind.py
```

The Codex parser (`rollout-*.jsonl`) is best-effort and not yet exercised against
real transcripts in this repo.
