---
name: hive-share
description: "Share this Claude Code session with the hive — use for 'share this chat', 'send this session', 'link to this conversation', or when a teammate should read what happened here."
---

# hive-share

Prints the pointer a teammate needs to open this session in Athene or read it from their own
terminal. Read-only: the session is already beamed on every turn, so no extra upload happens.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/hive_mind.py share ${ARGUMENTS}
```

No argument = the current directory's session. Pass a session id prefix to share another one.

Reply with the printed three-line block (title, `web:` link, `cli:` show command) and nothing
else; the user pastes it as is. If the command exits 1, relay its stderr line (not logged in,
session not beamed yet) instead of guessing a link.
