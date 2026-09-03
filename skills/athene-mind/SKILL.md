---
name: athene-mind
description: "Search or read the team's shared Claude Code / Codex session history — what earlier sessions (yours or a teammate's) did, decided, or tried on this project."
---

# athene-mind

Server-side search over every teammate's agent sessions, scoped to the current
repo's `origin` remote by default. Tool results and thinking are never stored;
you get prompts, assistant text, and one-line tool calls.

```bash
M="python3 ${CLAUDE_PLUGIN_ROOT}/athene_mind.py"
```

## Commands

```bash
$M search <terms...> [--role user|assistant|tool_call] [--author A] [--since 14d] [--limit 20] [-C 2]
$M search -e '<regex>' [-s] [-w] [-F]            # rg-style regex, newest first
$M sessions [--author A] [--since 14d] [--limit 20]
$M dump <session-id> [--start SEQ] [--end SEQ] [--max-msg 2000]
```

- Scope: `--project SUBSTR` matches a remote like `github.com/alvicom/x`; `--all` spans every project.
- Terms mode is ranked: terms are OR-ed, `"quoted phrase"` is a phrase, `-term` excludes.
- `-e` switches to regex (`-s` case-sensitive, `-w` whole word, `-F` literal). TERMS and `-e` are exclusive.
- `search` prints `score sessionId:seq ts author role` + snippet. Feed `sessionId` and `seq`
  into `dump --start/--end` to read around a hit. `--json` on any read command gives raw JSON.
- Aggregate hits per session: `$M search ... --limit 100 | grep -oE '^ *[0-9.]+ [0-9a-f-]{36}' | awk '{print $2}' | sort | uniq -c | sort -rn`.

## Workflow for "what did we do about X"

1. `search` broad terms with a high `--limit`; rank sessions by hit count.
2. `sessions` for who/when/branch/title.
3. `dump` the top sessions with `--max-msg 800`; for many sessions delegate batches to
   subagents and have each return a terse gist.
