# Why Hive Mind exists

Each developer's Claude Code / Codex transcripts live only on their laptop. Hive Mind ships
them to your own Athene server so the team can read them back.

Three uses, in order of how often they come up:

1. **Recall.** "Did we already try this?" — search every session on the project, yours and
   your teammates', from the agent or the web viewer, instead of re-deriving a dead end.
2. **Read your own history from another machine.** The laptop that did the work is not
   always the one in front of you.
3. **PR context.** A diff does not show intent. The session behind it does: did the agent
   have the right context, did it verify properly (which tests, which suites, which docs it
   actually read), and did the human steer it wrong?

Two ways in: the plugin marketplace (`/plugin install hive-mind`, auto-updated by the harness)
or `uvx --from git+<repo> hive-mind install --harness claude` for scripted setups.

## Privacy charter

Hive Mind exists for recall and PR context, not for review of people. Timestamps are kept
(useful data, e.g. a developer's own work log), so the guarantee is about surfaces, not data:

- No per-person analytics in the product: no sessions/hours per author, no heatmaps, no
  "last active", no metadata export. Author is a filter for finding work on a topic only.
- Symmetric access: every authenticated user sees the same thing. Admin's only extra right is
  purge (leaked secrets). No manager role, no reports.
- Developer controls in the client: per-repo opt-in (exists), `pause`, per-session off switch,
  owner-only purge, curated historic beam. Visibility toggle (shared/private) is a later option.
- Retention (90 days) is a privacy control; keep it.
- `today`/`tail` show what the team works on, not how much or when: no per-author counts.
- A developer may compute their own log (`sessions --mine --tsv | awk …`); the tool never does it
  for someone else.

## What is sent

Per finished assistant turn, only from a git checkout with an `origin` remote:
session id, normalized remote, branch, cwd, first prompt as title, token totals, models, and
messages of three kinds — your prompts, assistant text, one-line tool calls (input cut at 200
chars). Subagent runs are shipped the same way, as child sessions of the main one.

## What is never sent

Tool results (file contents, command output), thinking blocks, system/harness bookkeeping, and
anything matching the local secret scrub (`scrub_patterns.json`, seeded from gitleaks rules) —
keys, tokens, JWTs, PEM blocks, high-entropy strings. Tool calls whose input mentions env files,
credentials or key paths are sent as `<tool> [redacted]`. Scrubbing happens on your laptop,
before the request; the server never sees the raw text.

Opt out per shell with `HIVE_MIND=off`, per repo by not having one, per pattern with
`.hive-mind-ignore`, and after the fact with `hive-mind purge <id>`.

## Harness support

- **Claude Code** — supported, in daily use: main sessions, subagents, compaction summaries.
- **Codex** — the `rollout-*.jsonl` parser is implemented but not yet exercised against real
  sessions; treat it as best effort.
- Anything else — no parser. The hook contract (stdin JSON with `session_id`,
  `transcript_path`, `cwd`) is harness-neutral, so a new harness is one parser.
