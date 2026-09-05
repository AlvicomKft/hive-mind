---
name: gist
description: "Hand off this session to a fresh chat — use for 'gist', 'wrap this up', 'I need a new session', 'context is full', or instead of /compact."
---

# gist

Writes a short brief of the session you are in and prints it for the user to paste into a new
chat. The replacement for `/compact`: the new session starts clean, and pulls whatever else it
needs out of the hive with `hive-mind show`. Nothing is stored; the only command is reading your
session id — the brief is your reply.

`${ARGUMENTS}`, if present, is the focus — narrow the brief to that topic. With no argument, cover
the whole session.

## Write the brief

At most 10-15 lines, telegraphic, no headings; shorter is better. In this order:

1. What the work is — the goal, one or two lines.
2. Decisions made, with the reason where it is not obvious.
3. What is in flight and who owns it (peer sessions, subagents, background jobs, open PRs).
4. The next step, concrete enough to start on.
5. Up to five file references, repo-relative, each with what it is for.

Leave out anything the new session loads by itself: `CLAUDE.md`/`AGENTS.md` rules, memory, skills,
coding conventions, tool inventories. Leave out narration of what was tried and abandoned unless
the next step depends on it.

End with this line, `<shortId>` being the first 8 characters of `$CLAUDE_CODE_SESSION_ID`
(`echo "${CLAUDE_CODE_SESSION_ID:0:8}"`):

```
Session <shortId>: run `hive-mind show <shortId> --last 20` for the full history. Peer agent names may have changed; re-check ListAgents before messaging any.
```

Reply with the brief verbatim and nothing else — no preamble, no offer to do more — so the user
can copy the whole turn in one go.
