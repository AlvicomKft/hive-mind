---
name: hive-mind
description: "Search or read the team's shared Claude Code / Codex session history — use for 'did we already', 'last time', 'why is it done this way', 'what is X working on', or anything an earlier session (yours or a teammate's) decided or tried."
---

# hive-mind

Server-side search over every teammate's agent sessions, scoped to the current repo's `origin`
remote by default. Tool results and thinking are never stored; you get prompts, assistant text,
and one-line tool calls. Purpose and privacy charter: `vision.md` in the plugin root.

```bash
M="python3 ${CLAUDE_PLUGIN_ROOT}/hive_mind.py"
```

Session ids print as 8-char prefixes; `dump`/`purge` accept any unique prefix. `--json` on every
read command, `-v` adds full ids, scores and web links, `--links` adds just the links.

## Entry points, in the order you normally need them

```bash
$M today                                   # sessions touched today: HH:MM shortId author turns model title
$M search <terms...>                       # AND-ed terms, grouped by session: hits shortId date author title
$M search <terms...> --flat -C 2           # one line per hit plus neighbour turns
$M dump <shortId> --around <seq> -C 5      # read the window around a hit
$M tail [--follow]                         # new turns since the last tail, one line each
$M share [<shortId>]                       # print this session's shareable pointer
```

- **Share this session**: when the user asks to share, send, link or show *this* session to
  someone, run `$M share` (no id = the current directory's session) and reply with the printed three-line block and nothing else.
- `today` = `sessions --since today`. `sessions [--since 14d|yesterday|ISO] [--titles] [--limit N]`
  lists newest first; `--titles` drops everything but time, id and title.
- Search terms are **AND**-ed. `"quoted phrase"` is a phrase, `-term` excludes, `a | b`
  (or `a OR b`) alternates. `-e '<regex>'` switches to Postgres regex (`-s`, `-w`, `-F`);
  TERMS and `-e` are exclusive.
- `sessions`/`today` print the session's compaction summary prefixed `Σ ` when it has one, and
  the first prompt otherwise; search hits on a compaction summary are labelled `[summary]`.
- `--sort time` on `search` returns one chronology across sessions (implies `--flat`).
- Grouped search output is the default: one line per session ordered by hit count, best snippet
  indented. Use `--flat` when you want every hit.
- `dump` prints `[seq] role HH:MM text`, tool calls as `[seq] tool HH:MM Name: args`, compact
  summaries as `[seq] summary`. `--max-msg N` truncates and marks the cut as `[+N chars]`;
  `--role user` gives just the prompts (fast gist of what someone asked).
- Subagent runs are their own sessions, hidden by default. `sessions`/`today --with-subagents`
  shows them with a `↳<parentShortId>` marker; `dump <parent> --subagents` lists a session's
  children; spawn lines print as `[seq] agent HH:MM <description> → <childShortId>` and the
  subagent's report as a `[seq] tool HH:MM Agent result:` line in the parent. Search hits inside
  a child carry `↳<parentShortId>`.
- Scope: `--project SUBSTR` matches a remote like `github.com/alvicom/x`, `--all` spans every
  project, `--author A` filters by name/email, `--branch B` matches any branch the session touched
  (`dump` prints a `[seq] branch` line at each switch), `--mine` keeps only the caller's own
  sessions.
- `$M local [--since 30d]` lists this laptop's transcripts (date, id, turns, beamed marker,
  first prompt); `$M beam <id…>` ships older ones. A beamed session sorts by when the work
  happened, not by when it was sent. `$M doctor` checks config, server, token and hook install.
- `search`/`sessions`/`today`/`tail` exit 1 with a note on stderr when nothing matched, so
  `$M search x && …` composes like grep.
- `--tsv` on `search`/`sessions`/`today`/`tail` prints the same columns tab-separated with no
  header, so `cut -f`/`awk -F'\t'` survive spaces in titles.
- `tail` keeps a per-project watermark under the state dir: the first call only sets it, later
  calls print what arrived since. `--follow --interval 15` polls.

## Review a colleague's PR

```bash
$M sessions --branch <branch> --all          # the sessions behind the diff (any branch they touched)
$M dump <shortId> --role user --max-msg 300  # prompt outline: what was actually asked
$M dump <shortId> --tools | head -60         # what was run: tests, suites, fetches
```

- The prompt outline is where misread requirements show up: compare it with the ticket.
- `--tools` prints one line per tool call; `Agent <description>` lines are subagent spawns and
  `Agent result` lines their reports — `dump <parent> --subagents` lists the children.
- If the session has a `Σ` summary, `dump` renders it as `[seq] summary`: read that first, it
  is the agent's own account of intent and of what it verified.

## Workflow for "what did we do about X"

1. `search` broad terms — the grouped output already ranks the sessions.
2. `dump <shortId> --around <seq> -C 5` on the top one or two.
3. Still unclear? Widen with `--all`, drop a term, or `search -e` for an exact string. For many
   sessions, delegate batches to subagents with `dump --max-msg 800` and ask for a terse gist.

## Recipes

```bash
$M search retry backoff --tsv | cut -f1,2,6            # sessions ranked by hits + title
$M dump <shortId> --role user --max-msg 200            # outline: just the prompts
$M sessions --author laszlo --since 7d --tsv | awk -F'\t' '$4>20'   # X's substantial sessions
$M today --titles && $M search <task keywords>         # before starting: has this been done?
```

Token budget: never dump a whole session into the main context. Read a window
(`dump --around <seq> -C 5`), and delegate fan-out reads over several sessions to subagents that
return a terse gist.
