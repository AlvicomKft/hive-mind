---
name: search
description: "Search or read the team's shared Claude Code / Codex session history — use for 'did we already', 'last time', 'why is it done this way', 'what is X working on', or anything an earlier session (yours or a teammate's) decided or tried."
---

# search

Server-side search over every teammate's agent sessions, scoped to the current repo's `origin`
remote by default. Tool results and thinking are never stored; you get prompts, assistant text,
and one-line tool calls. Purpose and privacy charter: `vision.md` in the plugin root.

```bash
M="python3 ${CLAUDE_PLUGIN_ROOT}/hive_mind.py"
```

Session ids print as 8-char prefixes; `show`/`fetch`/`purge` accept any unique prefix. `--json` on every
list/search command (not `show`), `-v` adds full ids, scores and web links, `--links` adds just the links.

## Entry points, in the order you normally need them

```bash
$M today                                   # sessions touched today: meta line + `↳ latest reply`
$M search <terms...>                       # AND-ed terms, grouped by session: hits shortId date author title
$M search <terms...> --flat -C 2           # one line per hit plus neighbour turns
$M fetch <shortId>                         # one session → ~/.cache/hive-mind/sessions/<id>.jsonl; prints the path
$M show <shortId> --around <seq> -C 5      # render the window around a hit
$M show <shortId> --last 10                # render the final turns, e.g. right before a compaction
$M tail --since today                      # first look: today's turns across the project
$M tail                                    # only what landed since your last tail
$M tail --role user                        # what people are asking, nothing else
$M share [<shortId>]                       # print this session's shareable pointer
$M usage --since 3h [--mine]               # token burn per session and model over a window
```

- `usage [--since today|3h|ISO] [--until ...] [--mine] [--author A] [--project|--all]` answers
  "what burned tokens between X and Y": one line per session×model with turns, uncached input,
  output, cache read and cache creation, then per-model totals. Cache reads dominate the bill on
  long sessions, so they are counted separately from `in`.
- **Share this session**: `$M share [<shortId>]` prints the pointer block; the `/hive-mind:share`
  skill wraps it for the "share this chat" ask.
- `today` = `sessions --since today`. `sessions [--since 14d|yesterday|ISO] [--titles] [--limit N]`
  lists newest first; `--titles` drops everything but time, id and title.
- Search terms are **AND**-ed. `"quoted phrase"` is a phrase, `-term` excludes, `a | b`
  (or `a OR b`) alternates. `-e '<regex>'` switches to Postgres regex (`-s`, `-w`, `-F`);
  TERMS and `-e` are exclusive.
- `sessions`/`today` print two lines per session: `HH:MM shortId author branch turns model
  [N agents] [Σ] title`, then `  ↳ <first sentence of the latest assistant reply>`. `-v` adds
  `  › <latest user prompt>` above the reply. `Σ` marks a session that has a compaction summary
  (`show` renders the summary itself); `--titles` keeps the one-line time/id/title form.
  Search hits on a compaction summary are labelled `[summary]`, hits on a `/hive-mind:gist`
  handoff brief `[gist]`; `$M gist <shortId>` prints a session's latest brief on its own.
- `--sort time` on `search` returns one chronology across sessions (implies `--flat`).
- Grouped search output is the default: one line per session ordered by hit count, best snippet
  indented. Use `--flat` when you want every hit.
- **Context rule**: never read a whole session into the conversation. `fetch`'s only job is to
  put the session on disk and print the path — the file is yours to explore with your own tools.
  One JSON object per turn (`seq, role, toolName, ts, branch, text`), so `rg -n <term> <path>`,
  `jq -r 'select(.role=="user") | .text' <path>`, `tail -n 20`, `wc -l` all work, and Read with
  an offset if you want raw lines. Locate the seq you care about first, then `show` that window.
  Bare `show` renders the last 30 turns at 600 chars each and says so in a `#` header; `--all`
  is for piping to a file, never for reading into the conversation.
- `show` prints `[seq] role HH:MM text`, tool calls as `[seq] tool HH:MM Name: args`, compact
  summaries as `[seq] summary`. `--max-msg N` truncates and marks the cut as `[+N chars]`;
  `--role user` gives just the prompts (fast gist of what someone asked).
- Subagent runs are their own sessions, hidden by default. `sessions`/`today --with-subagents`
  shows them with a `↳<parentShortId>` marker; `show <parent> --subagents` lists a session's
  children; spawn lines print as `[seq] agent HH:MM <description> → <childShortId>` and the
  subagent's report as a `[seq] tool HH:MM Agent result:` line in the parent. Search hits inside
  a child carry `↳<parentShortId>`.
- Scope: `--project SUBSTR` matches a remote like `github.com/alvicom/x`, `--all` spans every
  project, `--author A` filters by name/email, `--branch B` matches any branch the session touched
  (`show` prints a `[seq] branch` line at each switch), `--mine` keeps only the caller's own
  sessions.
- `$M local [--since 30d]` lists this laptop's transcripts (date, id, turns, beamed marker,
  first prompt); `$M beam <id…>` ships older ones. A beamed session sorts by when the work
  happened, not by when it was sent. `$M doctor` checks config, server, token and hook install.
- `search`/`sessions`/`today`/`tail` exit 1 with a note on stderr when nothing matched, so
  `$M search x && …` composes like grep.
- `--tsv` on `search`/`sessions`/`today`/`tail` prints the same columns tab-separated with no
  header, so `cut -f`/`awk -F'\t'` survive spaces in titles. `sessions`/`today --tsv` ends with
  the latest prompt and the latest reply as the last two columns.
- `tail` keeps a per-project watermark under the state dir: the first call only sets it, later
  calls print what arrived since; `--since X` ignores the watermark (then sets it), so
  `tail --since today` is the first look. Output is grouped: a `# shortId author · title · branch`
  header per session, then `HH:MM role: text`. At most 40 turns (newest, `--limit N`); a
  `+N older turns skipped` note goes to stderr. The current directory's own session is left out
  unless `--self`. `--follow` is for humans watching a terminal — as an agent, just call `tail`
  again later.

## Review a colleague's PR

```bash
$M sessions --branch <branch> --all          # the sessions behind the diff (any branch they touched)
$M show <shortId> --role user --max-msg 300  # prompt outline: what was actually asked
$M show <shortId> --tools | head -60         # what was run: tests, suites, fetches
```

- The prompt outline is where misread requirements show up: compare it with the ticket.
- `--tools` prints one line per tool call; `Agent <description>` lines are subagent spawns and
  `Agent result` lines their reports — `show <parent> --subagents` lists the children.
- If the session has a `Σ` summary, `show` renders it as `[seq] summary`: read that first, it
  is the agent's own account of intent and of what it verified.

## Workflow for "what did we do about X"

1. `search` broad terms — the grouped output already ranks the sessions.
2. `fetch <shortId>`, `rg -n` the file for the term, then `show <shortId> --around <seq> -C 5`.
3. Still unclear? Widen with `--all`, drop a term, or `search -e` for an exact string. For many
   sessions, delegate batches to subagents with `show --max-msg 800` and ask for a terse gist.

## Recipes

```bash
$M search retry backoff --tsv | cut -f1,2,6            # sessions ranked by hits + title
$M show <shortId> --role user --max-msg 200            # outline: just the prompts
$M sessions --author laszlo --since 7d --tsv | awk -F'\t' '$4>20'   # X's substantial sessions
$M today --titles && $M search <task keywords>         # before starting: has this been done?
```

Token budget: never render a whole session into the main context. `fetch` it, narrow with
`rg`/`jq`, read the window (`show --around <seq> -C 5`), and delegate fan-out reads over several
sessions to subagents that return a terse gist.
