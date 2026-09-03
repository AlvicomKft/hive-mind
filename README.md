# Athene Mind

Shared, searchable Claude Code / Codex session history for a team. A hook ships
each finished assistant turn to your Athene server; a CLI and an agent skill
search it back, scoped to the current repo's `origin` remote.

Stdlib-only Python 3.11+, one file: `athene_mind.py` is both the hook and the CLI.

## Install

In Claude Code (Codex: same commands through its plugin UI):

```
/plugin marketplace add alvicom/athene-mind
/plugin install athene-mind
```

Then, once per laptop, with a personal access token minted on your Athene profile page:

```bash
python3 ~/.claude/plugins/marketplaces/athene-mind/athene_mind.py login --server https://athene.example.com
# or: athene-mind login   (if you alias/symlink the script onto PATH)
```

Config lands in `~/.config/athene-mind/config.json` (mode 600), outside the plugin
dir so plugin updates keep it. The hook is silent from then on; it fires on `Stop`
and `SessionEnd`, only inside git checkouts that have an `origin` remote.

## Use

```bash
athene-mind search kb sync retry --since 14d          # ranked terms, this repo
athene-mind search -e 'alembic (upgrade|downgrade)' -C 1
athene-mind sessions --author laszlo --limit 10
athene-mind dump <session-id> --start 40 --end 60
athene-mind purge <session-id>                         # something slipped
athene-mind hook --dry-run < event.json                # see what would be sent
```

`--project SUBSTR` / `--all` widen the scope; `--json` gives raw API output.

## What is sent, what is not

Sent per turn: session id, normalized remote (`github.com/org/repo`), branch, cwd,
first prompt as title, token totals, and messages of three kinds: your prompts,
assistant text, and one-line tool calls (`Bash {"command":"git status"}`, input cut
at 200 chars). Author is derived from your token on the server.

Never sent: tool results (file contents, command output), thinking blocks,
subagent transcripts, system/summary lines.

Scrubbed before sending (`scrub_patterns.json`, seeded from gitleaks rules):
AWS keys, OpenAI/Anthropic `sk-`, GitHub `gh?_`, Slack `xox?-`, Google `AIza`,
JWTs, PEM private keys, `password|secret|token|api_key = ...` assignments,
basic-auth URLs, Athene `athmind_` tokens, and any 32+ char high-entropy string
(entropy > 4.5 bits, so UUIDs and git SHAs pass). Tool calls whose input mentions
`env`, `.env`, `secret`, `token`, `password`, `credentials`, `gh auth`, `.pem`,
`.key`, `.netrc`, `.npmrc`, `kubeconfig`, `.aws/`, `.ssh/` are sent as `<tool> [redacted]`.

Escape hatches:

- `ATHENE_MIND=off` in the shell disables the hook.
- Extra regexes, one per line: `.athene-mind-ignore` at the repo root or `~/.config/athene-mind/ignore`.
- `athene-mind purge <session-id>` deletes a session you own.

State: `~/.local/state/athene-mind/<session-id>.json` (byte offset, next seq, token
totals) and `hook.log` (failures; the hook always exits 0 and never blocks the harness).
Env overrides: `ATHENE_MIND_CONFIG`, `ATHENE_MIND_STATE_DIR`.

## Develop

```bash
python3 -m unittest discover -s tests
python3 -m py_compile athene_mind.py
```

The Codex parser (`rollout-*.jsonl`) is best-effort and not yet exercised against
real transcripts in this repo.
