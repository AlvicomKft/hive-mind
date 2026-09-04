#!/usr/bin/env python3
"""Hive Mind: ships coding-agent transcripts to Athene (hook) and searches them (CLI)."""
import argparse
import getpass
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("HIVE_MIND_CONFIG") or "~/.config/hive-mind/config.json").expanduser()
STATE_DIR = Path(os.environ.get("HIVE_MIND_STATE_DIR") or "~/.local/state/hive-mind").expanduser()
USER_IGNORE = Path("~/.config/hive-mind/ignore").expanduser()
LEGACY_DIRS = (("~/.config/athene-mind", CONFIG_PATH.parent), ("~/.local/state/athene-mind", STATE_DIR))
CLAUDE_PROJECTS = Path("~/.claude/projects").expanduser()
CLAUDE_SETTINGS = Path("~/.claude/settings.json").expanduser()
CLAUDE_SKILLS = Path("~/.claude/skills").expanduser()
HOOK_EVENTS = ("Stop", "SessionEnd")
CODEX_SESSIONS = Path("~/.codex/sessions").expanduser()
API = "/api/v1/agent-history"
TOOL_INPUT_CHARS = 200
TITLE_CHARS = 240
MAX_MODELS = 8
REDACT_TOOL_INPUT = re.compile(
    r"env|printenv|\.env|secret|token|password|credentials|gh auth|\.pem|\.key|\.netrc|\.npmrc|kubeconfig|\.aws/|\.ssh/",
    re.I,
)
ENTROPY_CANDIDATE = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
ENTROPY_MIN = 4.5


def log(msg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "hook.log", "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}\n")


def migrate_legacy_dirs():
    """0.1 shipped as athene-mind; move the old config/state once so logins survive the rename."""
    for old, new in LEGACY_DIRS:
        old = Path(old).expanduser()
        if old.is_dir() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            old.rename(new)


def load_config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        sys.exit(f"no config at {CONFIG_PATH}; run: hive-mind login")
    if not cfg.get("server") or not cfg.get("token"):
        sys.exit(f"config {CONFIG_PATH} needs server and token; run: hive-mind login")
    cfg["server"] = cfg["server"].rstrip("/")
    return cfg


def request(cfg, method, path, body=None, query=None, timeout=15):
    url = cfg["server"] + API + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {cfg['token']}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            detail = json.loads(raw).get("error") or raw
        except (json.JSONDecodeError, AttributeError):
            detail = raw
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {detail[:300]}") from None


def normalize_remote(url):
    url = url.strip()
    url = re.sub(r"^\w[\w+.-]*://", "", url)
    url = re.sub(r"^[^/@]+@", "", url)
    url = re.sub(r"^([^/:]+):(?!\d)", r"\1/", url)
    url = re.sub(r"^([^/:]+):\d+/", r"\1/", url)
    url = re.sub(r"\.git$", "", url.rstrip("/"))
    host, sep, path = url.partition("/")
    return host.lower() + sep + path


def git(cwd, *args):
    try:
        out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def cwd_remote(cwd):
    url = git(cwd, "remote", "get-url", "origin")
    return normalize_remote(url) if url else None


# --- scrub ---------------------------------------------------------------


def load_patterns(cwd=None):
    pats = [(p["kind"], re.compile(p["regex"])) for p in json.loads((HERE / "scrub_patterns.json").read_text())]
    extra = [USER_IGNORE]
    top = git(cwd, "rev-parse", "--show-toplevel") if cwd else None
    if top:
        extra.append(Path(top) / ".hive-mind-ignore")
    for path in extra:
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    pats.append(("custom", re.compile(line)))
                except re.error as e:
                    log(f"ignore pattern {line!r} in {path}: {e}")
    return pats


def entropy(s):
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def scrub(text, patterns):
    for kind, rx in patterns:
        text = rx.sub(f"[REDACTED:{kind}]", text)
    return ENTROPY_CANDIDATE.sub(
        lambda m: "[REDACTED:high-entropy]" if entropy(m.group()) > ENTROPY_MIN else m.group(), text
    )


def tool_call_text(name, tool_input):
    compact = json.dumps(tool_input, separators=(",", ":"), ensure_ascii=False) if not isinstance(tool_input, str) else tool_input
    if REDACT_TOOL_INPUT.search(compact):
        return f"{name} [redacted]"
    return f"{name} {compact[:TOOL_INPUT_CHARS]}"


# --- parsers -------------------------------------------------------------
# Both return raw {role, toolName, text, ts} dicts (no seq) and mutate `meta`:
# startedAt, title, inputTokens, outputTokens, parentSessionId, usageMsgId.


_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_TASK_NOTIFICATION = re.compile(r"<task-notification>.*?</task-notification>", re.S)
_BASH_INPUT = re.compile(r"^<bash-input>(.*?)</bash-input>", re.S)
_HARNESS_NOISE = (
    "<bash-stdout>",
    "<bash-stderr>",
    "<local-command-",
    "<command-name>",
    "<command-message>",
    "<task-notification>",
    "[SYSTEM NOTIFICATION",
    "[Request interrupted by user",
    "Base directory for this skill",
)
COMPACT_SUMMARY = "compact-summary"
AGENT_SPAWN = "Agent"
AGENT_RESULT = "Agent result"
AGENT_RESULT_CHARS = 4000
_AGENT_RESULT = re.compile(r"<task-notification>.*?<result>(.*?)</result>", re.S)


def clean_user_text(text):
    text = _SYSTEM_REMINDER.sub("", text)
    text = _TASK_NOTIFICATION.sub("", text).strip()
    return "" if text.startswith(_HARNESS_NOISE) else text


def agent_result(text):
    """The subagent's report; the rest of the task-notification turn is harness bookkeeping."""
    match = _AGENT_RESULT.search(text)
    if not match:
        return None
    body = match.group(1).strip()
    extra = len(body) - AGENT_RESULT_CHARS
    return body if extra <= 0 else body[:AGENT_RESULT_CHARS] + f"… [+{extra} chars]"


def branch_change(rec, meta):
    """Claude Code stamps every record with its branch; only changes are worth carrying."""
    branch = rec.get("gitBranch") or None
    if "lastBranch" in meta and branch == meta["lastBranch"]:
        return None
    meta["lastBranch"] = branch
    return branch


def parse_claude(lines, meta, sidechain=False):
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = rec.get("type")
        if kind not in ("user", "assistant") or bool(rec.get("isSidechain")) is not sidechain:
            continue
        ts = rec.get("timestamp")
        meta.setdefault("startedAt", ts)
        msg = rec.get("message") or {}
        content = msg.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        texts = [b.get("text", "") for b in blocks if b.get("type") == "text" and b.get("text")]
        if kind == "assistant":
            if (model := msg.get("model")) and not model.startswith("<"):
                meta.setdefault("models", [])
                if model not in meta["models"]:
                    meta["models"].append(model)
            usage = msg.get("usage") or {}
            if usage and msg.get("id") != meta.get("usageMsgId"):
                meta["usageMsgId"] = msg.get("id")
                meta["inputTokens"] = meta.get("inputTokens", 0) + (usage.get("input_tokens") or 0)
                meta["outputTokens"] = meta.get("outputTokens", 0) + (usage.get("output_tokens") or 0)
        text = "\n".join(texts)
        summary = kind == "user" and bool(rec.get("isCompactSummary"))
        result = None
        shell = None
        if kind == "user":
            result = agent_result(text)
            text = clean_user_text(text)
            if m := _BASH_INPUT.match(text):
                shell, text = m.group(1).strip(), ""
        emitted = []
        if shell:
            emitted.append({"role": "tool_call", "toolName": "Bash", "text": f"Bash {shell}"[:TOOL_INPUT_CHARS], "ts": ts})
        if text:
            if kind == "user" and not summary and not meta.get("title"):
                meta["title"] = " ".join(text.split())[:TITLE_CHARS]
            tool = COMPACT_SUMMARY if summary else None
            emitted.append({"role": kind, "toolName": tool, "text": text, "ts": ts})
        if result:
            emitted.append({"role": "tool_call", "toolName": AGENT_RESULT, "text": result, "ts": ts})
        for b in blocks:
            if b.get("type") == "tool_use":
                name = b.get("name") or "tool"
                tool_input = b.get("input") or {}
                # A subagent spawn is worth keeping: its prompt trips the redaction rules, its description does not.
                text = f"{name} {tool_input.get('description', '')}".strip() if name == AGENT_SPAWN and isinstance(tool_input, dict) else tool_call_text(name, tool_input)
                emitted.append({"role": "tool_call", "toolName": name, "text": text, "ts": ts})
        if emitted:
            emitted[0]["branch"] = branch_change(rec, meta)
            out.extend(emitted)
    return out


def parse_codex(lines, meta):
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = rec.get("type")
        payload = rec.get("payload") or {}
        ts = rec.get("timestamp")
        if kind == "session_meta":
            meta.setdefault("startedAt", payload.get("timestamp") or ts)
            continue
        if kind == "event_msg" and payload.get("type") == "token_count":
            total = (payload.get("info") or {}).get("total_token_usage") or {}
            if total:
                meta["inputTokens"] = total.get("input_tokens") or 0
                meta["outputTokens"] = total.get("output_tokens") or 0
            continue
        if kind != "response_item":
            continue
        meta.setdefault("startedAt", ts)
        ptype = payload.get("type")
        if ptype == "message" and payload.get("role") in ("user", "assistant"):
            texts = [c.get("text", "") for c in payload.get("content") or [] if c.get("type") in ("input_text", "output_text") and c.get("text")]
            text = "\n".join(texts)
            if not text or (payload["role"] == "user" and text.lstrip().startswith("<")):
                continue
            if payload["role"] == "user" and not meta.get("title"):
                meta["title"] = " ".join(text.split())[:TITLE_CHARS]
            out.append({"role": payload["role"], "toolName": None, "text": text, "ts": ts})
        elif ptype in ("function_call", "custom_tool_call"):
            name = payload.get("name") or "tool"
            raw = payload.get("arguments") if ptype == "function_call" else payload.get("input")
            out.append({"role": "tool_call", "toolName": name, "text": tool_call_text(name, raw if raw is not None else {}), "ts": ts})
    return out


PARSERS = {"claude": parse_claude, "codex": parse_codex}


def detect_source(transcript_path):
    return "codex" if "/.codex/" in str(transcript_path) or Path(transcript_path).name.startswith("rollout-") else "claude"


# --- hook ----------------------------------------------------------------


def read_new_lines(path, offset):
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    end = chunk.rfind(b"\n")
    if end < 0:
        return [], offset
    return chunk[: end + 1].decode(errors="replace").splitlines(), offset + end + 1


def cmd_hook(args):
    if os.environ.get("HIVE_MIND", "").lower() == "off":
        return
    try:
        run_hook(json.load(sys.stdin), dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001 - never block the harness
        log(f"error: {type(e).__name__}: {e}")


def subagent_files(transcript, session_id):
    directory = Path(transcript).parent / session_id / "subagents"
    return sorted(directory.glob("agent-*.jsonl")) if directory.is_dir() else []


def subagent_meta(path):
    """Claude Code keeps every subagent file flat under `<session>/subagents/`; `spawnDepth`
    is the only nesting signal we forward (`parentAgentId` is present but the server keys the
    whole tree on the main session)."""
    try:
        return json.loads(path.with_suffix(".meta.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def aware_iso(ts):
    try:
        parsed = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    return parsed.isoformat() if parsed.tzinfo else None


def build_payload(path, slot, base, patterns, *, sidechain, parent_session_id, completed):
    lines, new_offset = read_new_lines(path, slot["bytes"])
    meta = slot["meta"]
    raw = parse_claude(lines, meta, True) if sidechain else PARSERS[base["source"]](lines, meta)
    if not raw and not (completed and slot["next_seq"]):
        return None
    if sidechain and not slot["next_seq"] and not any(m["role"] in ("user", "assistant") for m in raw):
        return None
    if meta.get("title"):
        meta["title"] = scrub(meta["title"], patterns)
    if stamps := [ts for m in raw if (ts := aware_iso(m["ts"]))]:
        meta["lastTs"] = max(stamps)
    seq = slot["next_seq"]
    messages = []
    for m in raw:
        message = {"seq": seq, "role": m["role"], "toolName": m["toolName"], "text": scrub(m["text"], patterns), "ts": m["ts"]}
        if m.get("branch"):
            message["branch"] = m["branch"]
        messages.append(message)
        seq += 1
    payload = {
        **base,
        "branch": meta.get("lastBranch") or base["branch"],
        "title": meta.get("title"),
        "parentSessionId": parent_session_id,
        "startedAt": meta.get("startedAt") or datetime.now(timezone.utc).isoformat(),
        "updatedAt": meta.get("lastTs") or aware_iso(meta.get("startedAt")) or datetime.now(timezone.utc).isoformat(),
        "completed": completed,
        "models": sorted(set(meta.get("models") or []))[:MAX_MODELS],
        "spawnDepth": meta.get("spawnDepth", 0),
        "modelExplicit": meta.get("modelExplicit", True),
        "inputTokens": meta.get("inputTokens", 0),
        "outputTokens": meta.get("outputTokens", 0),
        "messages": messages,
    }
    return payload, new_offset, seq


def run_hook(event, dry_run=False):
    """Returns the number of sessions posted (main + subagents)."""
    session_id = event.get("session_id")
    transcript = event.get("transcript_path")
    cwd = event.get("cwd") or os.getcwd()
    if not session_id or not transcript or not os.path.isfile(transcript):
        return 0
    remote = cwd_remote(cwd)
    if not remote:
        return 0
    state_path = STATE_DIR / f"{session_id}.json"
    state = json.loads(state_path.read_text()) if state_path.is_file() else {"bytes": 0, "next_seq": 0, "meta": {}}
    children = state.setdefault("children", {})
    source = detect_source(transcript)
    completed = event.get("hook_event_name") == "SessionEnd"
    patterns = load_patterns(cwd)
    base = {
        "source": source,
        "remote": remote,
        "branch": git(cwd, "symbolic-ref", "--short", "HEAD"),
        "cwd": cwd,
    }
    posts = []
    main = build_payload(transcript, state, base, patterns, sidechain=False, parent_session_id=state["meta"].get("parentSessionId"), completed=completed)
    if main:
        posts.append((session_id, state, main))
    if source == "claude":
        for path in subagent_files(transcript, session_id):
            agent_id = path.stem.removeprefix("agent-")
            slot = children.setdefault(agent_id, {"bytes": 0, "next_seq": 0, "meta": {}})
            info = subagent_meta(path)
            slot["meta"].setdefault("title", info.get("description"))
            slot["meta"].setdefault("spawnDepth", int(info.get("spawnDepth") or 1))
            if info:
                slot["meta"]["modelExplicit"] = bool(info.get("model"))
            built = build_payload(path, slot, base, patterns, sidechain=True, parent_session_id=session_id, completed=completed)
            if built:
                posts.append((agent_id, slot, built))
    if not posts:
        return 0
    if dry_run:
        json.dump([p for _, _, (p, _, _) in posts], sys.stdout, indent=2, ensure_ascii=False)
        print()
        return len(posts)
    cfg = load_config()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for sid, slot, (payload, new_offset, seq) in posts:
        status, body = request(cfg, "POST", f"/sessions/{sid}", payload, timeout=5)
        if status != 202:
            raise RuntimeError(f"unexpected status {status}")
        last_seq = (body or {}).get("lastSeq")
        if isinstance(last_seq, int) and last_seq + 1 < seq:
            log(f"{sid}: server lastSeq={last_seq} behind local next_seq={seq}")
        slot["bytes"], slot["next_seq"] = new_offset, seq
        state_path.write_text(json.dumps(state))
    return len(posts)


# --- local transcripts ---------------------------------------------------


def local_transcripts():
    paths = list(CLAUDE_PROJECTS.glob("*/*.jsonl")) if CLAUDE_PROJECTS.is_dir() else []
    if CODEX_SESSIONS.is_dir():
        paths += CODEX_SESSIONS.glob("*/*/*/rollout-*.jsonl")
    return paths


def transcript_head(lines):
    """cwd and session id live in the harness bookkeeping records at the top of the file."""
    for line in lines[:50]:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        cwd = rec.get("cwd") or payload.get("cwd")
        if cwd:
            return cwd, rec.get("sessionId") or payload.get("session_id")
    return None, None


def beam_state(path, session_id):
    state_path = STATE_DIR / f"{session_id}.json"
    if not state_path.is_file():
        return "-"
    try:
        sent = json.loads(state_path.read_text()).get("bytes", 0)
    except (OSError, json.JSONDecodeError):
        return "-"
    return "beamed" if sent >= path.stat().st_size else "partial"


def scan_local(since, project):
    entries = []
    remotes = {}
    cutoff = datetime.fromisoformat(since).timestamp() if since else 0
    for path in local_transcripts():
        stat = path.stat()
        if stat.st_mtime < cutoff:
            continue
        lines = path.read_text(errors="replace").splitlines()
        cwd, embedded_id = transcript_head(lines)
        if not cwd:
            continue
        if cwd not in remotes:
            remotes[cwd] = cwd_remote(cwd)
        remote = remotes[cwd]
        if not remote or (project and project not in remote):
            continue
        source = detect_source(path)
        meta = {}
        messages = PARSERS[source](lines, meta)
        if not messages:
            continue
        session_id = embedded_id if source == "codex" and embedded_id else path.stem
        entries.append({
            "id": session_id,
            "path": str(path),
            "cwd": cwd,
            "remote": remote,
            "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "turns": len(messages),
            "title": meta.get("title") or "",
            "state": beam_state(path, session_id),
        })
    return sorted(entries, key=lambda e: e["mtime"], reverse=True)


def local_scope(args):
    return project_filter(args)


def cmd_local(args):
    entries = scan_local(parse_since(args.since), local_scope(args))
    if args.json:
        dump_json(entries)
        return
    if not entries:
        sys.exit("no local transcripts")
    for e in entries:
        if args.tsv:
            tsv(local_time(e["mtime"]), short_id(e["id"], args.verbose), e["turns"], e["state"], e["title"])
            continue
        print(f"{local_time(e['mtime'])}  {short_id(e['id'], args.verbose)}  {e['turns']:5d}  {e['state']:>7}  {one_line(e['title'], 100)}")


def cmd_beam(args):
    entries = scan_local(parse_since(args.since), local_scope(args))
    if args.all_unbeamed:
        chosen = [e for e in entries if e["state"] != "beamed"]
    else:
        chosen = [e for e in entries if any(e["id"].startswith(prefix) for prefix in args.session_id)]
        missing = [p for p in args.session_id if not any(e["id"].startswith(p) for e in chosen)]
        if missing:
            sys.exit(f"no local transcript matches: {' '.join(missing)}")
    if not chosen:
        sys.exit("nothing to beam")
    for e in chosen:
        event = {
            "session_id": e["id"],
            "transcript_path": e["path"],
            "cwd": e["cwd"],
            "hook_event_name": "SessionEnd",
        }
        posted = run_hook(event, dry_run=args.dry_run)
        print(f"{short_id(e['id'], args.verbose)}  {e['turns']:5d} turns  {'nothing new' if not posted else f'{posted} session(s) sent'}  {one_line(e['title'], 80)}")


# --- doctor --------------------------------------------------------------


def installed_hook():
    root = Path("~/.claude/plugins").expanduser()
    for hooks in root.glob("**/hooks/hooks.json") if root.is_dir() else []:
        if "hive_mind.py" in hooks.read_text(errors="replace"):
            return hooks.parent.parent
    return None


def last_beam():
    stamps = [p.stat().st_mtime for p in STATE_DIR.glob("*.json") if not p.name.startswith("tail-")]
    return datetime.fromtimestamp(max(stamps), timezone.utc).isoformat() if stamps else None


def cmd_doctor(args):
    checks = []
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        server = cfg.get("server", "").rstrip("/")
        checks.append((bool(server and cfg.get("token")), "config", f"{CONFIG_PATH} server={server or '?'}"))
    except (OSError, json.JSONDecodeError) as e:
        cfg, server = None, None
        checks.append((False, "config", f"{CONFIG_PATH}: {e}; run: hive-mind login"))
    if cfg and server:
        cfg["server"] = server
        try:
            _, remotes = request(cfg, "GET", "/remotes", timeout=10)
            checks.append((True, "server", f"{server} reachable, {len(remotes or [])} projects"))
            _, page = request(cfg, "GET", "/sessions", query={"mine": "true", "size": 1})
            rows = page.get("content") or []
            checks.append((True, "token", rows[0]["author"] if rows else "valid, no sessions of yours yet"))
        except (RuntimeError, OSError) as e:
            checks.append((False, "server", str(e)))
    plugin = installed_hook()
    checks.append((plugin is not None, "hook", f"registered in {plugin}" if plugin else "no installed plugin registers the hook"))
    remote = cwd_remote(os.getcwd())
    checks.append((remote is not None, "repo", remote or "cwd has no origin remote; the hook stays silent here"))
    beamed = last_beam()
    checks.append((True, "last beam", local_time(beamed, "%Y-%m-%d %H:%M") if beamed else "never"))
    for ok, label, detail in checks:
        print(f"{'ok  ' if ok else 'FAIL'}  {label:<10} {detail}")
    if not all(ok for ok, _, _ in checks):
        sys.exit(1)


# --- install -------------------------------------------------------------


def hook_command():
    return f'python3 "{HERE / "hive_mind.py"}" hook'


def settings_with_hooks(settings, command):
    changed = False
    hooks = settings.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        matchers = hooks.setdefault(event, [])
        if any(h.get("command") == command for m in matchers for h in m.get("hooks", [])):
            continue
        matchers.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
        changed = True
    return changed


def settings_without_hooks(settings):
    changed = False
    hooks = settings.get("hooks") or {}
    for event in HOOK_EVENTS:
        matchers = hooks.get(event)
        if not matchers:
            continue
        kept = []
        for matcher in matchers:
            inner = [h for h in matcher.get("hooks", []) if "hive_mind.py" not in (h.get("command") or "")]
            changed = changed or len(inner) != len(matcher.get("hooks", []))
            if inner:
                kept.append({**matcher, "hooks": inner})
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    if not hooks:
        settings.pop("hooks", None)
    return changed


def read_settings():
    try:
        return json.loads(CLAUDE_SETTINGS.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def cmd_install(args):
    if args.harness != "claude":
        sys.stderr.write(f"harness {args.harness!r} is not supported yet; only claude\n")
        sys.exit(2)
    skill_src = HERE / "skills" / "hive-mind"
    link = CLAUDE_SKILLS / "hive-mind"
    settings = read_settings()
    if args.uninstall:
        hooks_changed = settings_without_hooks(settings)
        actions = [f"remove Stop/SessionEnd hooks from {CLAUDE_SETTINGS}"] if hooks_changed else []
        drop_link = link.is_symlink() and link.resolve() == skill_src.resolve()
        if drop_link:
            actions.append(f"remove {link}")
        if args.dry_run:
            print("\n".join(actions) or "nothing to remove")
            return
        if hooks_changed:
            CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
        if drop_link:
            link.unlink()
        print("\n".join(actions) or "nothing to remove")
        return
    command = hook_command()
    hooks_changed = settings_with_hooks(settings, command)
    actions = [f"register Stop/SessionEnd hook in {CLAUDE_SETTINGS}: {command}"] if hooks_changed else [f"hook already registered in {CLAUDE_SETTINGS}"]
    link_action = None
    if not skill_src.is_dir():
        link_action = f"skip skill link: {skill_src} is missing"
    elif link.is_symlink() and link.resolve() == skill_src.resolve():
        link_action = f"skill already linked: {link}"
    elif link.exists() or link.is_symlink():
        link_action = f"skip skill link: {link} exists and is not ours"
    else:
        link_action = f"link {link} -> {skill_src}"
    actions.append(link_action)
    needs_login = not CONFIG_PATH.is_file()
    actions.append("run login (no config yet)" if needs_login else f"config already at {CONFIG_PATH}")
    if args.dry_run:
        print("\n".join(actions))
        return
    if hooks_changed:
        CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        CLAUDE_SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    if link_action.startswith("link "):
        CLAUDE_SKILLS.mkdir(parents=True, exist_ok=True)
        link.symlink_to(skill_src)
    print("\n".join(actions))
    if needs_login:
        cmd_login(argparse.Namespace(server=args.server, token=None))
    cmd_doctor(args)


# --- cli -----------------------------------------------------------------

SHORT_ID = 8
TAIL_LINE_CHARS = 200
TAIL_MAX_SESSIONS = 10


def parse_since(value):
    if not value:
        return None
    now = datetime.now().astimezone()
    if value in ("today", "yesterday"):
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day = midnight if value == "today" else midnight - timedelta(days=1)
        return day.isoformat(timespec="seconds")
    m = re.fullmatch(r"(\d+)([dhw])", value)
    if not m:
        return value
    n, unit = int(m.group(1)), m.group(2)
    delta = timedelta(**{{"d": "days", "h": "hours", "w": "weeks"}[unit]: n})
    return (now - delta).isoformat(timespec="seconds")


def local_time(iso, fmt="%m-%d %H:%M"):
    if not iso:
        return " " * len(datetime.now().strftime(fmt))
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime(fmt)
    except ValueError:
        return iso[:16]


def short_id(session_id, verbose=False):
    return session_id if verbose else session_id[:SHORT_ID]


def deep_link(cfg, session_id, seq=None):
    url = f"{cfg['server']}/agent-history/{session_id}"
    return url if seq is None else f"{url}?seq={seq}"


def model_label(models):
    if not models:
        return "-"
    first = re.sub(r"^claude-", "", models[0])
    return first if len(models) == 1 else f"{first}+{len(models) - 1}"


MODEL_SHORT = {"fable": "f", "opus": "o", "sonnet": "s", "haiku": "h"}


def model_short(model):
    name = re.sub(r"^claude-", "", model)
    return MODEL_SHORT.get(name.split("-")[0], (name[:1] or "?"))


def model_use(m):
    """`~6f` = all six inherited the session model, `6o~1` = one of the six did."""
    body = f"{m['count']}{model_short(m['model'])}"
    if m["inherited"] == m["count"]:
        return f"~{body}"
    return f"{body}~{m['inherited']}" if m["inherited"] else body


def agents_label(s):
    """`▸ ~6f 6o !2`: child counts per model, `~` = inherited session model, `!N` = max depth."""
    a = s.get("agents")
    if not a:
        return ""
    parts = [model_use(m) for m in a["models"]]
    depth = f" !{a['maxDepth']}" if a.get("maxDepth", 0) >= 2 else ""
    return f"  ▸ {' '.join(parts)}{depth}"


def agent_line(c, verbose):
    depth = int(c.get("spawnDepth") or 1)
    mark = "" if c.get("modelExplicit", True) else "~"
    tokens = f"↑{c.get('inputTokens', 0)} ↓{c.get('outputTokens', 0)}"
    return (
        f"        {'  ' * depth}↳ {short_id(c['id'], verbose)}  {mark}{model_label(c.get('models'))}  "
        f"{c.get('turns', 0):4d}  {tokens}  {one_line(c.get('title'), 80)}"
    )


def sorted_children(cfg, session_id):
    return sorted(child_sessions(cfg, session_id), key=lambda c: (int(c.get("spawnDepth") or 1), c.get("startedAt") or ""))


SUMMARY_PREFIX = "Σ "
SUMMARY_CHARS = 120


def branch_label(s, verbose):
    branches = s.get("branches") or []
    latest = s.get("branch") or (branches[-1] if branches else None)
    if verbose and len(branches) > 1:
        return " → ".join(branches)
    return latest or "-"


def session_label(s, limit):
    """Compaction summary beats the first-prompt title: it describes the whole session."""
    parent = s.get("parentSessionId")
    prefix = f"↳{short_id(parent)} " if parent else ""
    summary = s.get("summary")
    if summary:
        return prefix + SUMMARY_PREFIX + one_line(summary, SUMMARY_CHARS)
    return prefix + one_line(s.get("title"), limit)


def one_line(text, limit):
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def project_filter(args):
    if args.all:
        return None
    if args.project:
        return args.project
    remote = cwd_remote(os.getcwd())
    if not remote:
        sys.exit("cwd has no origin remote; pass --project or --all")
    return remote


def tsv(*cols):
    print("\t".join(" ".join(str(c).split()) for c in cols))


def dump_json(payload):
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def cmd_login(args):
    try:
        server = args.server or input("Athene server URL: ").strip()
        token = args.token or getpass.getpass("Personal access token (athmind_...): ").strip()
    except (EOFError, OSError):
        sys.exit("no terminal for the prompt; pass --server and --token")
    if not server or not token:
        sys.exit("server and token required")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.touch(mode=0o600)
    CONFIG_PATH.chmod(0o600)
    CONFIG_PATH.write_text(json.dumps({"server": server.rstrip("/"), "token": token}, indent=2) + "\n")
    try:
        _, remotes = request(load_config(), "GET", "/remotes")
    except (RuntimeError, OSError) as e:
        sys.exit(f"config written to {CONFIG_PATH} but verification failed: {e}")
    print(f"ok: {server} ({len(remotes or [])} remotes visible), config at {CONFIG_PATH}")


def cmd_search(args):
    if args.terms and args.pattern:
        sys.exit("give either TERMS or -e PATTERN, not both")
    if not args.terms and not args.pattern:
        sys.exit("nothing to search")
    regex = bool(args.pattern or args.fixed or args.word)
    q = args.pattern or " ".join(args.terms)
    if args.fixed:
        q = re.escape(q)
    if args.word:
        q = rf"\m(?:{q})\M"
    body = {
        "q": q,
        "regex": regex,
        "caseSensitive": args.case_sensitive,
        "remote": project_filter(args),
        "branch": args.branch,
        "author": args.author,
        "since": parse_since(args.since),
        "role": args.role,
        "order": args.sort,
        "mine": args.mine or None,
        "limit": args.limit,
        "context": args.context,
    }
    cfg = load_config()
    _, res = request(cfg, "POST", "/search", {k: v for k, v in body.items() if v is not None})
    if args.json:
        dump_json(res)
        return
    hits = res.get("hits") or []
    if not hits:
        sys.exit("no hits")
    if args.flat or args.sort == "time":
        print_hits(cfg, args, hits)
    else:
        print_hits_by_session(cfg, args, hits)


def parent_mark(h):
    parent = h.get("parentSessionId")
    return f" ↳{short_id(parent)}" if parent else ""


def hit_role(h):
    return "[summary]" if h.get("toolName") == COMPACT_SUMMARY else h.get("role")


def print_hits(cfg, args, hits):
    for h in hits:
        if args.tsv:
            tsv(local_time(h.get("ts")), short_id(h["sessionId"], args.verbose), h["seq"],
                h.get("author"), hit_role(h), strip_marks(h.get("snippet")))
            continue
        score = f"{h.get('score') or 0:6.2f} " if args.verbose else ""
        branch = f" {h['branch']}" if len(h.get("branches") or []) > 1 and h.get("branch") else ""
        line = f"{score}{local_time(h.get('ts'))} {short_id(h['sessionId'], args.verbose)}:{h['seq']}{parent_mark(h)} {h.get('author')}{branch} {hit_role(h)}"
        print(f"{line}\n  {one_line(strip_marks(h.get('snippet')), 300)}")
        if args.links or args.verbose:
            print(f"  {deep_link(cfg, h['sessionId'], h['seq'])}")
        for c in h.get("context") or []:
            tool = f" [{c['toolName']}]" if c.get("toolName") else ""
            print(f"    {c['seq']} {c['role']}{tool}: {one_line(c.get('text'), 200)}")
        print()


def print_hits_by_session(cfg, args, hits):
    groups = {}
    for h in hits:
        groups.setdefault(h["sessionId"], []).append(h)
    for group in sorted(groups.values(), key=lambda g: -len(g)):
        best = group[0]
        sid = best["sessionId"]
        if args.tsv:
            tsv(len(group), short_id(sid, args.verbose), best["seq"], local_time(best.get("ts")),
                best.get("author"), hit_role(best), best.get("title"), strip_marks(best.get("snippet")))
            continue
        print(
            f"{len(group):4d}  {short_id(sid, args.verbose)}{parent_mark(best)}  {local_time(best.get('ts'))}  "
            f"{best.get('author')}  {hit_role(best)}  {one_line(best.get('title'), 90)}"
        )
        print(f"      {one_line(strip_marks(best.get('snippet')), 240)}")
        if args.links or args.verbose:
            print(f"      {deep_link(cfg, sid, best['seq'])}")
    if args.tsv:
        return
    plural = "session" if len(groups) == 1 else "sessions"
    print(f"\n{len(hits)} hits in {len(groups)} {plural}; read one with: dump <shortId> --around <seq> -C 5")


def strip_marks(snippet):
    return re.sub(r"</?b>", "", snippet or "")


def cmd_sessions(args):
    query = {
        "remote": project_filter(args),
        "branch": args.branch,
        "author": args.author,
        "since": parse_since(args.since),
        "includeChildren": "true" if args.with_subagents else None,
        "mine": "true" if args.mine else None,
        "page": 0,
        "size": args.limit,
    }
    cfg = load_config()
    _, res = request(cfg, "GET", "/sessions", query=query)
    if args.json:
        dump_json(res)
        return
    rows = res.get("content") or []
    if not rows:
        sys.exit("no sessions")
    fmt = "%H:%M" if args.since in ("today", "yesterday") else "%m-%d %H:%M"
    for s in rows:
        stamp = local_time(s.get("updatedAt"), fmt)
        sid = short_id(s["id"], args.verbose)
        if args.tsv and args.titles:
            tsv(stamp, sid, session_label(s, 120))
            continue
        if args.tsv:
            tsv(stamp, sid, s.get("author"), s.get("turns", 0), model_label(s.get("models")) + agents_label(s).strip(), session_label(s, 120))
            continue
        if args.titles:
            print(f"{stamp}  {sid}  {session_label(s, 120)}")
            continue
        print(
            f"{stamp}  {sid}  {s.get('author')}  {s.get('turns', 0):4d}  "
            f"{model_label(s.get('models'))}{agents_label(s)}  {session_label(s, 100)}"
        )
        if args.verbose and len(s.get("branches") or []) > 1:
            print(f"        {branch_label(s, True)}")
        if args.verbose and s.get("agents"):
            for c in sorted_children(cfg, s["id"]):
                print(agent_line(c, args.verbose))
        if args.links or args.verbose:
            print(f"        {deep_link(cfg, s['id'])}")


def cmd_today(args):
    args.since = "today"
    cmd_sessions(args)


def child_sessions(cfg, session_id):
    _, page = request(cfg, "GET", "/sessions", query={"parent": session_id, "size": 50})
    return page.get("content") or []


def cmd_dump(args):
    if args.tools:
        args.role = "tool_call"
    if args.around is not None:
        args.start = max(0, args.around - args.context)
        args.end = args.around + args.context
    cfg = load_config()
    _, res = request(cfg, "GET", f"/sessions/{args.session_id}", query={"from": args.start, "to": args.end})
    messages = [m for m in res.get("messages") or [] if not args.role or m["role"] == args.role]
    s = res["session"]
    children = child_sessions(cfg, s["id"]) if s.get("childCount") else []
    if args.json:
        dump_json({"session": s, "messages": messages, "subagents": children})
        return
    print(f"# {short_id(s['id'], args.verbose)} {s.get('author')} {s.get('remote')} {branch_label(s, args.verbose)} {model_label(s.get('models'))} {local_time(s.get('startedAt'))}")
    print(f"# {one_line(s.get('title'), 200)}")
    if args.links or args.verbose:
        print(f"# {deep_link(cfg, s['id'])}")
    if args.subagents:
        for c in sorted(children, key=lambda c: (int(c.get("spawnDepth") or 1), c.get("startedAt") or "")):
            print("#" + agent_line(c, args.verbose)[1:])
    print()
    spawned = {c.get("title"): c["id"] for c in children}
    for m in messages:
        if m.get("branch"):
            print(f"[{m['seq']}] branch {local_time(m.get('ts'), '%H:%M')}  {m['branch']}")
        print(format_message(m, args.max_msg, spawned))


def format_message(m, max_msg, spawned=None):
    stamp = local_time(m.get("ts"), "%H:%M")
    text = m.get("text") or ""
    extra = len(text) - max_msg
    if extra > 0:
        text = text[:max_msg] + f" [+{extra} chars]"
    if m["role"] == "tool_call":
        name = m.get("toolName") or "tool"
        body = text[len(name) :].lstrip() if text.startswith(name) else text
        if name == AGENT_SPAWN:
            child = (spawned or {}).get(body)
            arrow = f" → {short_id(child)}" if child else ""
            return f"[{m['seq']}] agent {stamp}  {one_line(body, 160)}{arrow}"
        return f"[{m['seq']}] tool  {stamp}  {name}: {one_line(body, 200)}"
    label = "summary" if m.get("toolName") == COMPACT_SUMMARY else m["role"]
    return f"[{m['seq']}] {label}  {stamp}\n{text}\n"


def beamed_here(cwd):
    """Most recently beamed session for this working directory, per the hook's state files."""
    here = [
        e
        for e in scan_local(None, None)
        if e["cwd"] == cwd and (STATE_DIR / f"{e['id']}.json").is_file()
    ]
    return here[0] if here else None


def cmd_share(args):
    cwd = os.getcwd()
    if args.session_id:
        entry = next((e for e in scan_local(None, None) if e["id"].startswith(args.session_id)), None)
        session_id = entry["id"] if entry else args.session_id
    else:
        entry = beamed_here(cwd)
        if not entry:
            sys.exit("no beamed session for this directory; run `hive-mind local` to pick one")
        session_id = entry["id"]
    cfg = load_config()
    _, res = request(cfg, "GET", f"/sessions/{session_id}", query={"to": 0})
    s = res["session"]
    block = {
        "id": s["id"],
        "title": one_line(s.get("title"), 120),
        "author": s.get("author"),
        "remote": s.get("remote"),
        "branch": branch_label(s, False),
        "web": deep_link(cfg, s["id"]),
        "cli": f"hive-mind dump {short_id(s['id'])}",
    }
    if args.json:
        dump_json(block)
        return
    print(f"{block['title']} · {block['author']} · {block['remote']} @ {block['branch']}")
    print(f"web:  {block['web']}")
    print(f"cli:  {block['cli']}")


def cmd_purge(args):
    status, _ = request(load_config(), "DELETE", f"/sessions/{args.session_id}")
    print(f"deleted {args.session_id}" if status == 204 else f"status {status}")


# --- tail ----------------------------------------------------------------


def tail_state_path(remote):
    key = re.sub(r"[^A-Za-z0-9]+", "-", remote or "all").strip("-")
    return STATE_DIR / f"tail-{key}.json"


def tail_once(cfg, args, state, first):
    query = {"remote": args.remote, "branch": args.branch, "since": parse_since(args.since or "1d"), "mine": "true" if args.mine else None, "page": 0, "size": args.sessions}
    _, page = request(cfg, "GET", "/sessions", query=query)
    lines = []
    for s in reversed(page.get("content") or []):
        sid = s["id"]
        known = sid in state
        seq_from = state[sid] if known else s.get("turns", 0)
        if first and not known:
            state[sid] = seq_from
            continue
        _, detail = request(cfg, "GET", f"/sessions/{sid}", query={"from": seq_from})
        for m in detail.get("messages") or []:
            state[sid] = m["seq"] + 1
            if m["role"] not in args.roles:
                continue
            lines.append(
                {
                    "sessionId": sid,
                    "seq": m["seq"],
                    "author": s.get("author"),
                    "role": m["role"],
                    "text": m.get("text") or "",
                }
            )
    return lines


def print_tail(cfg, args, lines):
    for line in lines:
        if args.json:
            dump_json(line)
            continue
        if args.tsv:
            tsv(short_id(line["sessionId"], args.verbose), line["seq"], line["author"], line["role"], line["text"])
            continue
        prefix = f"{short_id(line['sessionId'], args.verbose)} {line['author']} {line['role']}:"
        print(f"{prefix} {one_line(line['text'], TAIL_LINE_CHARS)}")
        if args.links or args.verbose:
            print(f"  {deep_link(cfg, line['sessionId'], line['seq'])}")


def cmd_tail(args):
    cfg = load_config()
    args.remote = project_filter(args)
    args.roles = (args.role,) if args.role else ("user", "assistant")
    path = tail_state_path(args.remote)
    state = json.loads(path.read_text()) if path.is_file() else {}
    first = args.follow or not state
    try:
        while True:
            lines = tail_once(cfg, args, state, first)
            print_tail(cfg, args, lines)
            first = False
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state))
            if not args.follow:
                if not lines:
                    sys.exit("no new turns")
                return
            time.sleep(args.interval)
    except KeyboardInterrupt:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))


def build_parser():
    p = argparse.ArgumentParser(prog="hive-mind", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("hook", help="harness Stop/SessionEnd hook; reads event JSON on stdin")
    h.add_argument("--dry-run", action="store_true", help="print payload instead of posting")
    h.set_defaults(fn=cmd_hook)

    lg = sub.add_parser("login", help="store server URL + token, verify")
    lg.add_argument("--server")
    lg.add_argument("--token")
    lg.set_defaults(fn=cmd_login)

    def output(sp):
        sp.add_argument("--json", action="store_true", help="raw JSON output")
        sp.add_argument("--links", action="store_true", help="append web deep links")
        sp.add_argument("-v", "--verbose", action="store_true", help="full ids, scores, links")

    def scope(sp):
        sp.add_argument("--project", help="remote substring; default = cwd origin remote")
        sp.add_argument("--all", action="store_true", help="every project")
        sp.add_argument("--branch", help="exact branch name")
        sp.add_argument("--author", help="author substring")
        sp.add_argument("--mine", action="store_true", help="only your own sessions")
        sp.add_argument("--since", help="today, yesterday, 14d, 12h, 2w or ISO timestamp")
        sp.add_argument("--tsv", action="store_true", help="tab-separated columns, no header")
        output(sp)

    s = sub.add_parser("search", help="ranked term search (AND by default) or regex with -e")
    s.add_argument("terms", nargs="*")
    s.add_argument("-e", "--pattern", help="regex (Postgres ARE) instead of terms")
    s.add_argument("-F", "--fixed", action="store_true", help="literal match")
    s.add_argument("-s", "--case-sensitive", action="store_true")
    s.add_argument("-w", "--word", action="store_true", help="whole-word match")
    s.add_argument("-C", "--context", type=int, default=0, help="neighbor turns per hit (--flat only)")
    s.add_argument("--role", choices=["user", "assistant", "tool_call"])
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--flat", action="store_true", help="one line per hit instead of per session")
    s.add_argument("--sort", choices=["rank", "time"], default="rank", help="time = one chronology across sessions (implies --flat)")
    scope(s)
    s.set_defaults(fn=cmd_search)

    ls = sub.add_parser("sessions", help="list sessions, newest first")
    ls.add_argument("--limit", type=int, default=20)
    ls.add_argument("--titles", action="store_true", help="time, id and title only")
    ls.add_argument("--with-subagents", action="store_true", help="include subagent child sessions")
    scope(ls)
    ls.set_defaults(fn=cmd_sessions)

    td = sub.add_parser("today", help="sessions touched today (sugar for sessions --since today)")
    td.add_argument("--limit", type=int, default=30)
    td.add_argument("--titles", action="store_true", help="time, id and title only")
    td.add_argument("--with-subagents", action="store_true", help="include subagent child sessions")
    scope(td)
    td.set_defaults(fn=cmd_today)

    d = sub.add_parser("dump", help="read one session; SESSION_ID may be an 8-char prefix")
    d.add_argument("session_id")
    d.add_argument("--start", type=int, default=0, help="first seq")
    d.add_argument("--end", type=int, help="last seq")
    d.add_argument("--around", type=int, help="center the window on this seq")
    d.add_argument("-C", "--context", type=int, default=5, help="turns either side of --around")
    d.add_argument("--role", choices=["user", "assistant", "tool_call"])
    d.add_argument("--tools", action="store_true", help="tool calls only, one line each")
    d.add_argument("--max-msg", type=int, default=2000, help="chars kept per message")
    d.add_argument("--subagents", action="store_true", help="list this session's subagent children")
    output(d)
    d.set_defaults(fn=cmd_dump)

    t = sub.add_parser("tail", help="new turns since the last tail, across the project's sessions")
    t.add_argument("--follow", action="store_true", help="keep polling")
    t.add_argument("--interval", type=int, default=15, help="seconds between polls with --follow")
    t.add_argument("--sessions", type=int, default=TAIL_MAX_SESSIONS, help="sessions polled per tick")
    t.add_argument("--role", choices=["user", "assistant", "tool_call"])
    scope(t)
    t.set_defaults(fn=cmd_tail)

    lo = sub.add_parser("local", help="local transcripts on this laptop, beamed or not")
    lo.add_argument("--since", default="30d", help="today, yesterday, 30d, 12h, 2w or ISO timestamp")
    lo.add_argument("--project", help="remote substring; default = cwd origin remote")
    lo.add_argument("--all", action="store_true", help="every project")
    lo.add_argument("--tsv", action="store_true", help="tab-separated columns, no header")
    output(lo)
    lo.set_defaults(fn=cmd_local)

    bm = sub.add_parser("beam", help="ship chosen local transcripts (curated historic beam)")
    bm.add_argument("session_id", nargs="*", help="local id prefixes from `local`")
    bm.add_argument("--all-unbeamed", action="store_true", help="every local transcript not fully sent")
    bm.add_argument("--since", default="30d", help="how far back `local` scans")
    bm.add_argument("--project", help="remote substring; default = cwd origin remote")
    bm.add_argument("--all", action="store_true", help="every project")
    bm.add_argument("--dry-run", action="store_true", help="print payloads instead of posting")
    output(bm)
    bm.set_defaults(fn=cmd_beam)

    ins = sub.add_parser("install", help="register the hook and skill for a harness, then log in")
    ins.add_argument("--harness", required=True, help="claude (the only supported harness)")
    ins.add_argument("--server", help="Athene server URL for the login step")
    ins.add_argument("--uninstall", action="store_true", help="remove what install added")
    ins.add_argument("--dry-run", action="store_true", help="print the changes instead of making them")
    ins.set_defaults(fn=cmd_install)

    dr = sub.add_parser("doctor", help="check config, server, token, hook install and last beam")
    dr.set_defaults(fn=cmd_doctor)

    sh = sub.add_parser("share", help="beam this session and print a shareable web + CLI pointer")
    sh.add_argument("session_id", nargs="?", help="local id prefix; default = the current directory's session")
    sh.add_argument("--json", action="store_true", help="machine-readable block")
    sh.set_defaults(fn=cmd_share)

    pg = sub.add_parser("purge", help="delete one session; SESSION_ID may be an 8-char prefix")
    pg.add_argument("session_id")
    pg.set_defaults(fn=cmd_purge)
    return p


def main(argv=None):
    migrate_legacy_dirs()
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (RuntimeError, OSError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
