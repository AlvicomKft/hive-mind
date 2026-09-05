---
name: gist
description: "Hand off this session to a fresh chat — use for 'gist', 'wrap this up', 'I need a new session', 'context is full', or instead of /compact."
---

# gist

Writes a short brief of the session you are in, stores it in the hive as a `gist`-tagged turn, and
prints it for the user to paste into a new chat. The replacement for `/compact`: the new session
starts clean and can pull the rest of the history with `hive-mind show` when it needs it.

`${ARGUMENTS}`, if present, is the focus — narrow the brief to that topic. With no argument, cover
the whole session.

## Write the brief

Roughly 10-15 lines, telegraphic, no headings, in this order:

1. What the work is — the goal, one or two lines.
2. Decisions made, with the reason where it is not obvious.
3. What is in flight and who owns it (peer sessions, subagents, background jobs, open PRs).
4. The next step, concrete enough to start on.
5. Up to five file references, repo-relative, each with what it is for.

Leave out anything the new session loads by itself: `CLAUDE.md`/`AGENTS.md` rules, memory, skills,
coding conventions, tool inventories. Leave out narration of what was tried and abandoned unless
the next step depends on it. Do not write the closing pointer line — `--post` appends it.

## Store and print it

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/hive_mind.py gist --post - <<'HIVE_GIST'
<the brief>
HIVE_GIST
```

The command prints the stored brief, closing line included. Reply with that output verbatim and
nothing else, as the last thing in the turn, so the user can copy it in one go. If the command
exits 1, relay its stderr line instead of the brief — nothing was stored.

## Picking one up

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/hive_mind.py gist <shortId>
```

Prints that session's latest gist, or its last assistant turn plus the pointer line when it has
none. Use it to take over a teammate's session; `hive-mind show <shortId> --last 20` is the next
step from there.
