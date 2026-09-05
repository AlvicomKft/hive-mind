#!/usr/bin/env python3
"""Hive Mind: ships coding-agent transcripts to Athene (hook) and searches them (CLI)."""
import argparse
import fcntl
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
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("HIVE_MIND_CONFIG") or "~/.config/hive-mind/config.json").expanduser()
STATE_DIR = Path(os.environ.get("HIVE_MIND_STATE_DIR") or "~/.local/state/hive-mind").expanduser()
CACHE_DIR = Path(os.environ.get("HIVE_MIND_CACHE_DIR") or "~/.cache/hive-mind/sessions").expanduser()
USER_IGNORE = Path("~/.config/hive-mind/ignore").expanduser()
CLAUDE_PROJECTS = Path("~/.claude/projects").expanduser()
CLAUDE_SETTINGS = Path("~/.claude/settings.json").expanduser()
CLAUDE_SKILLS = Path("~/.claude/skills").expanduser()
HOOK_EVENTS = ("Stop", "SessionEnd")
SESSION_ID_ENV = "CLAUDE_CODE_SESSION_ID"
CODEX_SESSIONS = Path("~/.codex/sessions").expanduser()
API = "/api/v1/agent-history"
ACCESS_TOKEN = re.compile(r"athmind_[0-9a-f]{40}\Z")
TOOL_INPUT_CHARS = 200
CHUNK_MESSAGES = 500
TITLE_CHARS = 240
MAX_MODELS = 8
REDACT_TOOL_INPUT = re.compile(
    r"env|printenv|\.env|secret|token|password|credentials|gh auth|\.pem|\.key|\.netrc|\.npmrc|kubeconfig|\.aws/|\.ssh/",
    re.I,
)
ENTROPY_CANDIDATE = re.compile(r"(?<![A-Za-z0-9+/=_.~-])[A-Za-z0-9+/=_-]{32,}")
ENTROPY_MIN = 4.5
UUID_TOKEN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.I)
HEX_TOKEN = re.compile(r"[0-9a-f]{32,64}\Z", re.I)


def log(msg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "hook.log", "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}\n")


def load_config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        sys.exit(f"no config at {CONFIG_PATH}; run: hive-mind login")
    if not all(cfg.get(k) for k in ("server", "web", "token")):
        sys.exit(f"config {CONFIG_PATH} is incomplete; run: hive-mind login")
    cfg["server"] = cfg["server"].rstrip("/")
    cfg["web"] = cfg["web"].rstrip("/")
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


def high_entropy_secret(tok):
    # Paths, UUIDs and git SHAs are long and look random too; only a mix of character
    # classes separates a real credential from an identifier we must keep readable.
    if UUID_TOKEN.match(tok) or HEX_TOKEN.match(tok):
        return False
    if "/" in tok and any(s.isalpha() and s.islower() and len(s) > 2 for s in tok.split("/")):
        return False
    classes = (
        any(c.islower() for c in tok)
        + any(c.isupper() for c in tok)
        + any(c.isdigit() for c in tok)
        + any(c in "+/=" for c in tok)
    )
    return classes >= 3 and entropy(tok) > ENTROPY_MIN


def scrub(text, patterns):
    for kind, rx in patterns:
        text = rx.sub(f"[REDACTED:{kind}]", text)
    return ENTROPY_CANDIDATE.sub(
        lambda m: "[REDACTED:high-entropy]" if high_entropy_secret(m.group()) else m.group(), text
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
GIST = "gist"
AGENT_SPAWN = "Agent"
AGENT_RESULT = "Agent result"
AGENT_RESULT_CHARS = 4000
_AGENT_RESULT = re.compile(r"<task-notification>.*?<result>(.*?)</result>", re.S)
USAGE_TOTALS = {"input": "inputTokens", "output": "outputTokens", "cacheRead": "cacheReadTokens", "cacheCreation": "cacheCreationTokens"}


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
        row = None
        if kind == "assistant":
            model = msg.get("model") or None
            if model and model.startswith("<"):
                model = None
            if model:
                meta.setdefault("models", [])
                if model not in meta["models"]:
                    meta["models"].append(model)
            usage = msg.get("usage") or {}
            # Streaming repeats one message id across records, each carrying the same usage.
            if usage and msg.get("id") != meta.get("usageMsgId"):
                meta["usageMsgId"] = msg.get("id")
                counts = {
                    "input": usage.get("input_tokens") or 0,
                    "output": usage.get("output_tokens") or 0,
                    "cacheRead": usage.get("cache_read_input_tokens") or 0,
                    "cacheCreation": usage.get("cache_creation_input_tokens") or 0,
                }
                for field, key in USAGE_TOTALS.items():
                    meta[key] = meta.get(key, 0) + counts[field]
                # The server requires a model on a usage row; `<synthetic>` turns have none.
                row = {"model": model, **counts} if model else None
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
            if row:
                emitted[0]["usage"] = row
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
                # Codex counts cache reads inside input_tokens, Claude does not; subtract so
                # inputTokens means uncached input on both sources.
                cached = total.get("cached_input_tokens") or 0
                meta["inputTokens"] = max((total.get("input_tokens") or 0) - cached, 0)
                meta["outputTokens"] = total.get("output_tokens") or 0
                meta["cacheReadTokens"] = cached
                meta["cacheCreationTokens"] = total.get("cache_write_input_tokens") or 0
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


def build_payload(path, slot, base, patterns, *, sidechain, parent_session_id, completed, appended=()):
    lines, new_offset = read_new_lines(path, slot["bytes"])
    meta = slot["meta"]
    raw = (parse_claude(lines, meta, True) if sidechain else PARSERS[base["source"]](lines, meta)) + list(appended)
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
        if m.get("usage"):
            message["usage"] = m["usage"]
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
        "cacheReadTokens": meta.get("cacheReadTokens", 0),
        "cacheCreationTokens": meta.get("cacheCreationTokens", 0),
        "messages": messages,
    }
    return payload, new_offset, seq


def under_roots(cwd):
    """Empty roots = every git checkout; otherwise only checkouts inside a listed directory."""
    try:
        roots = json.loads(CONFIG_PATH.read_text()).get("roots") or []
    except (OSError, json.JSONDecodeError):
        return True
    path = Path(cwd).resolve()
    return not roots or any(path == Path(r) or Path(r) in path.parents for r in roots)


def fresh_slot():
    return {"bytes": 0, "next_seq": 0, "meta": {}}


def load_state(state_path, server):
    """Offsets and seq counters only describe one server: a `login` elsewhere re-ships from 0."""
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    if state.get("server") != server:
        state = {"server": server}
    return {**fresh_slot(), "children": {}, **state}


@contextmanager
def state_lock(session_id):
    """The Stop hook runs in the background and `gist --post` posts from inside a turn, so two
    hook processes on one state file is a normal path; a lost update renumbers or drops messages."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / f"{session_id}.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def config_server():
    try:
        return json.loads(CONFIG_PATH.read_text()).get("server", "")
    except (OSError, json.JSONDecodeError):
        return ""


def run_hook(event, dry_run=False, reset=False, timeout=5, appended=()):
    """Returns the number of messages posted (main + subagents)."""
    session_id = event.get("session_id")
    transcript = event.get("transcript_path")
    cwd = event.get("cwd") or os.getcwd()
    if not session_id or not transcript or not os.path.isfile(transcript):
        return 0
    remote = cwd_remote(cwd)
    if not remote or not under_roots(cwd):
        return 0
    with state_lock(session_id):
        state_path = STATE_DIR / f"{session_id}.json"
        state = load_state(state_path, config_server())
        if reset:
            state = {"server": state["server"], **fresh_slot(), "children": {}}
        children = state["children"]
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
        main = build_payload(transcript, state, base, patterns, sidechain=False, parent_session_id=state["meta"].get("parentSessionId"), completed=completed, appended=appended)
        if main:
            posts.append((session_id, state, main))
        if source == "claude":
            for path in subagent_files(transcript, session_id):
                agent_id = path.stem.removeprefix("agent-")
                slot = children.setdefault(agent_id, fresh_slot())
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
            return sum(len(p["messages"]) for _, _, (p, _, _) in posts)
        cfg = load_config()
        sent = 0
        for sid, slot, (payload, new_offset, seq) in posts:
            messages = payload["messages"]
            # Offsets move only once the whole session is in: seq is derived from file order, so a
            # half-advanced counter would renumber every message on the retry. The server dedupes
            # what a failed run already stored.
            chunks = [messages[i:i + CHUNK_MESSAGES] for i in range(0, len(messages), CHUNK_MESSAGES)] or [[]]
            for chunk in chunks:
                status, body = request(cfg, "POST", f"/sessions/{sid}", {**payload, "messages": chunk}, timeout=timeout)
                if status != 202:
                    raise RuntimeError(f"unexpected status {status}")
                last_seq = (body or {}).get("lastSeq")
                stored = chunk[-1]["seq"] if chunk else slot["next_seq"] - 1
                if stored >= 0 and (not isinstance(last_seq, int) or last_seq < stored):
                    log(f"{sid}: server lastSeq={last_seq} behind {stored}; re-shipping this session from 0 next run")
                    slot.update(fresh_slot())
                    state_path.write_text(json.dumps(state))
                    break
                sent += len(chunk)
            else:
                slot["bytes"], slot["next_seq"] = new_offset, seq
                state_path.write_text(json.dumps(state))
        return sent


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
        posted = run_hook(event, dry_run=args.dry_run, reset=args.force, timeout=30)
        print(f"{short_id(e['id'], args.verbose)}  {e['turns']:5d} turns  {'nothing new' if not posted else f'{posted} sent'}  {one_line(e['title'], 80)}")


# --- doctor --------------------------------------------------------------


def installed_hook():
    """The plugin manager's install record wins over stray cache dirs of older versions."""
    record = Path("~/.claude/plugins/installed_plugins.json").expanduser()
    try:
        entries = json.loads(record.read_text())["plugins"]["hive-mind@hive-mind"]
        path = Path(entries[0]["installPath"])
        if (path / "hooks" / "hooks.json").is_file():
            return path
    except (OSError, ValueError, KeyError, IndexError):
        pass
    root = Path("~/.claude/plugins").expanduser()
    for hooks in root.glob("**/hooks/hooks.json") if root.is_dir() else []:
        if "hive_mind.py" in hooks.read_text(errors="replace"):
            return hooks.parent.parent
    return None


def last_beam():
    stamps = [p.stat().st_mtime for p in STATE_DIR.glob("*.json") if not p.name.startswith("tail-")]
    return datetime.fromtimestamp(max(stamps), timezone.utc).isoformat() if stamps else None


def same_origin(a, b):
    def origin(u):
        p = urllib.parse.urlsplit(u if "://" in u else "https://" + u)
        return p.scheme, (p.hostname or "").lower(), p.port or (443 if p.scheme == "https" else 80)

    return origin(a) == origin(b)


def cmd_doctor(args):
    checks = []
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        server = cfg.get("server", "").rstrip("/")
        roots = ", ".join(cfg.get("roots") or []) or "every git checkout"
        checks.append((bool(server and cfg.get("token")), "config", f"{CONFIG_PATH} server={server or '?'} roots={roots}"))
    except (OSError, json.JSONDecodeError) as e:
        cfg, server = None, None
        checks.append((False, "config", f"{CONFIG_PATH}: {e}; run: hive-mind login"))
    if cfg and args.server and not same_origin(cfg.get("web") or "", args.server):
        checks.append((False, "server", f"configured for {cfg.get('web')}, you asked for {args.server}; re-run login --server {args.server}"))
    elif cfg and server:
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
    skill_src = HERE / "skills" / "search"
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
        cmd_login(argparse.Namespace(server=args.server, token=None, root=args.root))
    cmd_doctor(args)


# --- cli -----------------------------------------------------------------

SHORT_ID = 8
TAG_LABELS = {COMPACT_SUMMARY: "summary", GIST: "gist"}
TAIL_LINE_CHARS = 200
TAIL_MAX_SESSIONS = 10
TAIL_MAX_TURNS = 40


def parse_since(value):
    if not value:
        return None
    now = datetime.now().astimezone()
    if value in ("today", "yesterday"):
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day = midnight if value == "today" else midnight - timedelta(days=1)
        return day.isoformat(timespec="seconds")
    m = re.fullmatch(r"(\d+)([mdhw])", value)
    if not m:
        return value
    n, unit = int(m.group(1)), m.group(2)
    delta = timedelta(**{{"m": "minutes", "d": "days", "h": "hours", "w": "weeks"}[unit]: n})
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
    url = f"{cfg['web']}/agent-history/{session_id}"
    return url if seq is None else f"{url}?seq={seq}"


def model_label(models):
    if not models:
        return "-"
    first = re.sub(r"^claude-", "", models[0])
    return first if len(models) == 1 else f"{first}+{len(models) - 1}"


def agents_label(s):
    a = s.get("agents")
    if not a:
        return ""
    return f"  {a['count']} agents"


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


SUMMARY_MARK = "  Σ"
RECAP_CHARS = 160


def branch_label(s, verbose):
    branches = s.get("branches") or []
    latest = s.get("branch") or (branches[-1] if branches else None)
    if verbose and len(branches) > 1:
        return " → ".join(branches)
    return latest or "-"


def session_label(s, limit):
    parent = s.get("parentSessionId")
    prefix = f"↳{short_id(parent)} " if parent else ""
    return prefix + one_line(s.get("title"), limit)


def first_sentence(text, limit):
    flat = " ".join((text or "").split())
    cut = re.search(r"(?<=[.!?])\s", flat)
    return one_line(flat[: cut.start()] if cut else flat, limit)


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


def api_url(app_url):
    """The Athene app publishes its API origin in /config.json; the user only knows the app URL."""
    try:
        with urllib.request.urlopen(app_url + "/config.json", timeout=15) as resp:
            backend = json.load(resp)["frontend"]["urls"]["backend"]
    except (OSError, ValueError, KeyError, TypeError):
        sys.exit(f"{app_url}/config.json is not an Athene app config; pass the URL you open Athene at")
    return backend.rstrip("/")


def cmd_login(args):
    """Piped stdin is the recommended token path: hidden prompts drop pasted characters in some terminals."""
    tty = sys.stdin.isatty()
    try:
        app = args.server or (input("Athene URL (the one you open in the browser): ").strip() if tty else "")
        token = (args.token or (getpass.getpass("Personal access token (athmind_...): ") if tty else sys.stdin.read())).strip()
    except (EOFError, OSError):
        sys.exit("no terminal for the prompt; run this in a terminal window (Claude Code's ! prompt has no TTY), or pipe the token in")
    if not token:
        sys.exit("no token; pipe it in (wl-paste | hive-mind login --server <app-url>), pass --token, or run login in a terminal window — Claude Code's ! prompt has no TTY")
    if not app:
        sys.exit("--server is required")
    if not ACCESS_TOKEN.match(token):
        sys.exit('that is not a Hive Mind token (athmind_ + 40 hex); the paste probably failed, try middle-click paste or --token "$(wl-paste)"')
    app = app.rstrip("/")
    cfg = {"server": api_url(app), "web": app, "token": token}
    if args.root:
        cfg["roots"] = [str(Path(r).expanduser().resolve()) for r in args.root]
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.touch(mode=0o600)
    CONFIG_PATH.chmod(0o600)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    try:
        _, remotes = request(load_config(), "GET", "/remotes")
    except (RuntimeError, OSError) as e:
        sys.exit(f"config written to {CONFIG_PATH} but verification failed: {e}")
    scope = f", hook limited to {', '.join(cfg['roots'])}" if cfg.get("roots") else ""
    print(f"ok: {app} → api {cfg['server']} ({len(remotes or [])} remotes visible){scope}, config at {CONFIG_PATH}")


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
    label = TAG_LABELS.get(h.get("toolName"))
    return f"[{label}]" if label else h.get("role")


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
    print(f"\n{len(hits)} hits in {len(groups)} {plural}; read one with: show <shortId> --around <seq> -C 5")


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
            tsv(stamp, sid, s.get("author"), s.get("turns", 0), model_label(s.get("models")) + agents_label(s).strip(),
                session_label(s, 120), one_line(s.get("lastPrompt"), 300), one_line(s.get("lastReply"), 300))
            continue
        if args.titles:
            print(f"{stamp}  {sid}  {session_label(s, 120)}")
            continue
        mark = SUMMARY_MARK if s.get("summary") else ""
        print(
            f"{stamp}  {sid}  {s.get('author')}  {branch_label(s, args.verbose)}  {s.get('turns', 0):4d}  "
            f"{model_label(s.get('models'))}{agents_label(s)}{mark}  {session_label(s, 100)}"
        )
        if args.verbose and s.get("lastPrompt"):
            print(f"  › {one_line(s['lastPrompt'], RECAP_CHARS)}")
        if s.get("lastReply"):
            print(f"  ↳ {first_sentence(s['lastReply'], RECAP_CHARS)}")
        if args.verbose and s.get("agents"):
            for c in sorted_children(cfg, s["id"]):
                print(agent_line(c, args.verbose))
        if args.links or args.verbose:
            print(f"        {deep_link(cfg, s['id'])}")


USAGE_FIELDS = ("input", "output", "cacheRead", "cacheCreation")
USAGE_HEAD = f"{'id':<10}{'model':<18}{'turns':>6}{'in':>9}{'out':>9}{'cread':>9}{'ccreate':>9}  title"


def k_units(n):
    n = n or 0
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}k" if n < 1_000_000 else f"{n / 1_000_000:.1f}M"


def usage_numbers(row):
    return "".join(f"{k_units(row.get(f)):>9}" for f in USAGE_FIELDS)


def cmd_usage(args):
    query = {
        "since": parse_since(args.since),
        "until": parse_since(args.until),
        "remote": project_filter(args),
        "author": args.author,
        "mine": "true" if args.mine else None,
        "limit": args.limit,
    }
    cfg = load_config()
    _, res = request(cfg, "GET", "/usage", query=query)
    if args.json:
        dump_json(res)
        return
    rows = res.get("rows") or []
    if not rows:
        sys.exit("no usage in this window")
    if not args.tsv:
        print(USAGE_HEAD)
    for r in rows:
        sid, model, turns = short_id(r["sessionId"], args.verbose), model_label([r.get("model")]), r.get("turns", 0)
        if args.tsv:
            tsv(sid, r.get("author"), model, turns, *(r.get(f) or 0 for f in USAGE_FIELDS), one_line(r.get("title"), 120))
            continue
        print(f"{sid:<10}{model:<18}{turns:>6}{usage_numbers(r)}  {one_line(r.get('title'), 60)}")
    if args.tsv:
        return
    print()
    for t in res.get("totals") or []:
        print(f"{'total':<10}{model_label([t.get('model')]):<18}{t.get('turns', 0):>6}{usage_numbers(t)}")


def cmd_today(args):
    args.since = "today"
    cmd_sessions(args)


def child_sessions(cfg, session_id):
    _, page = request(cfg, "GET", "/sessions", query={"parent": session_id, "size": 50})
    return page.get("content") or []


SHOW_DEFAULT_TURNS = 30
SHOW_DEFAULT_CHARS = 600
SHOW_MAX_CHARS = 2000
CACHE_TTL_DAYS = 30
FETCH_FIELDS = ("seq", "role", "toolName", "ts", "branch", "text")


def sweep_cache():
    cutoff = time.time() - CACHE_TTL_DAYS * 86400
    for f in CACHE_DIR.glob("*.jsonl"):
        if f.stat().st_mtime < cutoff:
            f.unlink()


def fetch_session(cfg, session_id):
    """Sync one session into the JSONL cache; returns (session, path, cached turns, added, rewritten)."""
    _, meta = request(cfg, "GET", f"/sessions/{session_id}", query={"to": 0})
    s = meta["session"]
    turns = int(s.get("turns") or 0)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sweep_cache()
    path = CACHE_DIR / f"{s['id']}.jsonl"
    if not path.exists():
        path.touch()
    with path.open() as f:
        have = sum(1 for _ in f)
    if have == turns:
        return s, path, have, 0, False
    # A purge-and-rebeam shortens the session server-side: the cached tail is stale, so start over.
    rewritten = have > turns
    start = 0 if rewritten else have
    _, res = request(cfg, "GET", f"/sessions/{s['id']}", query={"from": start}, timeout=60)
    rows = [{k: m.get(k) for k in FETCH_FIELDS} for m in res.get("messages") or []]
    with path.open("w" if rewritten else "a") as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    return s, path, start + len(rows), len(rows), rewritten


def cmd_fetch(args):
    _, path, turns, added, rewritten = fetch_session(load_config(), args.session_id)
    print(f"{path}  {turns} turns  ({'rewritten' if rewritten else f'+{added}'})")


def cmd_show(args):
    if args.tools:
        args.role = "tool_call"
    if args.around is not None:
        args.start = max(0, args.around - args.context)
        args.end = args.around + args.context
    ranged = args.all or args.around is not None or args.start or args.end is not None or args.last is not None
    if args.last is None and not ranged and not args.role:
        args.last = SHOW_DEFAULT_TURNS
    if args.max_msg is None:
        args.max_msg = SHOW_MAX_CHARS if ranged else SHOW_DEFAULT_CHARS
    cfg = load_config()
    s, path, turns, _, _ = fetch_session(cfg, args.session_id)
    start = max(0, turns - args.last) if args.last is not None else args.start
    end = turns - 1 if args.end is None else min(args.end, turns - 1)
    with path.open() as f:
        rows = [json.loads(line) for line in f]
    messages = [m for m in rows[start : end + 1] if not args.role or m["role"] == args.role]
    children = child_sessions(cfg, s["id"]) if s.get("childCount") else []
    print(f"# {short_id(s['id'], args.verbose)} {s.get('author')} {s.get('remote')} {branch_label(s, args.verbose)} {model_label(s.get('models'))} {local_time(s.get('startedAt'))}")
    print(f"# {one_line(s.get('title'), 200)}")
    if start > end:
        print(f"# {turns} turns, nothing to show past seq {start} · rg {path}")
    elif start or end < turns - 1:
        print(f"# {turns} turns, showing {start}..{end} · rg {path} for the rest")
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
    label = TAG_LABELS.get(m.get("toolName")) or m["role"]
    return f"[{m['seq']}] {label}  {stamp}\n{text}\n"


def beamed_here(cwd):
    """Most recently beamed session for this working directory, per the hook's state files."""
    here = [
        e
        for e in scan_local(None, None)
        if e["cwd"] == cwd and (STATE_DIR / f"{e['id']}.json").is_file()
    ]
    return here[0] if here else None


def current_session(session_id=None):
    """A planner and a worker session often share one directory, so the id the harness exports
    beats `beamed_here`, which can only guess at the most recently beamed one."""
    if session_id:
        entry = next((e for e in scan_local(None, None) if e["id"].startswith(session_id)), None)
        return entry, entry["id"] if entry else session_id
    if live := os.environ.get(SESSION_ID_ENV):
        entry = next((e for e in scan_local(None, None) if e["id"] == live), None)
        if entry:
            return entry, entry["id"]
    entry = beamed_here(os.getcwd())
    if not entry:
        sys.exit("no beamed session for this directory; run `hive-mind local` to pick one")
    return entry, entry["id"]


def cmd_share(args):
    _, session_id = current_session(args.session_id)
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
        "cli": f"hive-mind show {short_id(s['id'])}",
    }
    if args.json:
        dump_json(block)
        return
    print(f"{block['title']} · {block['author']} · {block['remote']} @ {block['branch']}")
    print(f"web:  {block['web']}")
    print(f"cli:  {block['cli']}")


def gist_closing(session_id):
    short = short_id(session_id)
    return (
        f"Session {short}: run `hive-mind show {short} --last 20` for the full history. "
        "Peer agent names may have changed; re-check ListAgents before messaging any."
    )


def post_gist(args):
    entry, session_id = current_session()
    body = sys.stdin.read() if args.post == "-" else Path(args.post).read_text()
    if not body.strip():
        sys.exit("empty gist")
    text = f"{body.strip()}\n\n{gist_closing(session_id)}"
    event = {"session_id": session_id, "transcript_path": entry["path"], "cwd": entry["cwd"]}
    message = {"role": "assistant", "toolName": GIST, "text": text, "ts": datetime.now(timezone.utc).isoformat()}
    if not run_hook(event, timeout=30, appended=[message]):
        sys.exit("the gist was not stored; `hive-mind doctor` says why")
    print(text)


def cmd_gist(args):
    if args.post:
        post_gist(args)
        return
    cfg = load_config()
    _, session_id = current_session(args.session_id)
    s, path, _, _, _ = fetch_session(cfg, session_id)
    with path.open() as f:
        rows = [json.loads(line) for line in f]
    if stored := [r for r in rows if r.get("toolName") == GIST]:
        print(stored[-1]["text"])
        return
    replies = [r for r in rows if r["role"] == "assistant"]
    if not replies:
        sys.exit(f"{short_id(s['id'])} has no assistant turns yet")
    print(replies[-1].get("text") or "")
    print(f"\n{gist_closing(s['id'])}")


def cmd_purge(args):
    status, _ = request(load_config(), "DELETE", f"/sessions/{args.session_id}")
    print(f"deleted {args.session_id}" if status == 204 else f"status {status}")


# --- tail ----------------------------------------------------------------


def tail_state_path(remote):
    key = re.sub(r"[^A-Za-z0-9]+", "-", remote or "all").strip("-")
    return STATE_DIR / f"tail-{key}.json"


def at_or_after(ts, since):
    return bool(ts) and datetime.fromisoformat(ts.replace("Z", "+00:00")) >= since


def tail_once(cfg, args, state, first):
    query = {"remote": args.remote, "branch": args.branch, "since": parse_since(args.since or "1d"), "mine": "true" if args.mine else None, "page": 0, "size": args.sessions}
    _, page = request(cfg, "GET", "/sessions", query=query)
    since = datetime.fromisoformat(parse_since(args.since)) if args.since else None
    skip = None if args.self else (beamed_here(os.getcwd()) or {}).get("id")
    lines = []
    for s in reversed(page.get("content") or []):
        sid = s["id"]
        if sid == skip:
            state[sid] = s.get("turns", 0)
            continue
        known = sid in state
        seq_from = 0 if since else state[sid] if known else s.get("turns", 0)
        if first and not known and not since:
            state[sid] = seq_from
            continue
        _, detail = request(cfg, "GET", f"/sessions/{sid}", query={"from": seq_from})
        for m in detail.get("messages") or []:
            state[sid] = m["seq"] + 1
            if m["role"] not in args.roles or (since and not at_or_after(m.get("ts"), since)):
                continue
            lines.append(
                {
                    "sessionId": sid,
                    "seq": m["seq"],
                    "ts": m.get("ts"),
                    "author": s.get("author"),
                    "title": s.get("title"),
                    "branch": branch_label(s, args.verbose),
                    "role": m["role"],
                    "text": m.get("text") or "",
                }
            )
    return lines


def print_tail(cfg, args, lines):
    skipped = len(lines) - args.limit
    if skipped > 0:
        lines = lines[-args.limit :]
        print(f"+{skipped} older turns skipped", file=sys.stderr)
    header = None
    for line in lines:
        if args.json:
            dump_json(line)
            continue
        if args.tsv:
            tsv(short_id(line["sessionId"], args.verbose), line["seq"], line["ts"], line["author"], line["role"], line["text"])
            continue
        group = line["sessionId"]
        if group != header:
            header = group
            print(f"# {short_id(group, args.verbose)} {line['author']} · {one_line(line['title'], 80)} · {line['branch']}")
        print(f"{local_time(line['ts'], '%H:%M')} {line['role']}: {one_line(line['text'], TAIL_LINE_CHARS)}")
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
                    sys.exit("no new turns" if args.since else "no new turns; run `tail --since today` for a first look")
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
    lg.add_argument("--server", help="Athene app URL (the one you open in the browser)")
    lg.add_argument("--root", action="append", metavar="DIR", help="only beam checkouts under DIR (repeatable); default: every git checkout")
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
        sp.add_argument("--since", help="today, yesterday, 30m, 12h, 14d, 2w or ISO timestamp")
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

    us = sub.add_parser("usage", help="token burn per session and model over a time window")
    us.add_argument("--since", default="today", help="today, yesterday, 30m, 12h, 14d, 2w or ISO timestamp")
    us.add_argument("--until", help="same formats; exclusive upper bound")
    us.add_argument("--project", help="remote substring; default = cwd origin remote")
    us.add_argument("--all", action="store_true", help="every project")
    us.add_argument("--author", help="author substring")
    us.add_argument("--mine", action="store_true", help="only your own sessions")
    us.add_argument("--limit", type=int, default=50)
    us.add_argument("--tsv", action="store_true", help="tab-separated columns, no header")
    us.add_argument("--json", action="store_true", help="raw JSON output")
    us.add_argument("-v", "--verbose", action="store_true", help="full session ids")
    us.set_defaults(fn=cmd_usage)

    td = sub.add_parser("today", help="sessions touched today (sugar for sessions --since today)")
    td.add_argument("--limit", type=int, default=30)
    td.add_argument("--titles", action="store_true", help="time, id and title only")
    td.add_argument("--with-subagents", action="store_true", help="include subagent child sessions")
    scope(td)
    td.set_defaults(fn=cmd_today)

    d = sub.add_parser("show", help=f"render a window of one session (default: last {SHOW_DEFAULT_TURNS} turns); SESSION_ID may be an 8-char prefix")
    d.add_argument("session_id")
    d.add_argument("--start", type=int, default=0, help="first seq")
    d.add_argument("--end", type=int, help="last seq")
    d.add_argument("--around", type=int, help="center the window on this seq")
    d.add_argument("--last", type=int, metavar="N", help="only the final N turns")
    d.add_argument("-C", "--context", type=int, default=5, help="turns either side of --around")
    d.add_argument("--role", choices=["user", "assistant", "tool_call"])
    d.add_argument("--tools", action="store_true", help="tool calls only, one line each")
    d.add_argument("--all", action="store_true", help="the whole session; prefer `fetch` + rg")
    d.add_argument("--max-msg", type=int, help=f"chars kept per message (default {SHOW_DEFAULT_CHARS}, {SHOW_MAX_CHARS} with --start/--end/--around/--last/--all)")
    d.add_argument("--subagents", action="store_true", help="list this session's subagent children")
    d.add_argument("--links", action="store_true", help="append web deep links")
    d.add_argument("-v", "--verbose", action="store_true", help="full ids, links")
    d.set_defaults(fn=cmd_show)

    f = sub.add_parser("fetch", help="download one session as JSONL into the local cache and print the path; rg/jq it")
    f.add_argument("session_id")
    f.set_defaults(fn=cmd_fetch)

    t = sub.add_parser("tail", help=f"new turns since the last tail (newest {TAIL_MAX_TURNS}), across the project's sessions")
    t.add_argument("--follow", action="store_true", help="keep polling (human use; agents just call tail again later)")
    t.add_argument("--interval", type=int, default=15, help="seconds between polls with --follow")
    t.add_argument("--sessions", type=int, default=TAIL_MAX_SESSIONS, help="sessions polled per tick")
    t.add_argument("--limit", type=int, default=TAIL_MAX_TURNS, help="turns printed, newest kept")
    t.add_argument("--self", action="store_true", help="include this directory's own session")
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
    bm.add_argument("--force", action="store_true", help="re-ship the whole transcript, ignoring what was already sent")
    output(bm)
    bm.set_defaults(fn=cmd_beam)

    ins = sub.add_parser("install", help="register the hook and skill for a harness, then log in")
    ins.add_argument("--harness", required=True, help="claude (the only supported harness)")
    ins.add_argument("--server", help="Athene app URL for the login step")
    ins.add_argument("--root", action="append", metavar="DIR", help="only beam checkouts under DIR (repeatable)")
    ins.add_argument("--uninstall", action="store_true", help="remove what install added")
    ins.add_argument("--dry-run", action="store_true", help="print the changes instead of making them")
    ins.set_defaults(fn=cmd_install)

    dr = sub.add_parser("doctor", help="check config, server, token, hook install and last beam")
    dr.add_argument("server", nargs="?", help="Athene app URL the config is expected to point at")
    dr.set_defaults(fn=cmd_doctor)

    sh = sub.add_parser("share", help="beam this session and print a shareable web + CLI pointer")
    sh.add_argument("session_id", nargs="?", help="local id prefix; default = the current directory's session")
    sh.add_argument("--json", action="store_true", help="machine-readable block")
    sh.set_defaults(fn=cmd_share)

    gi = sub.add_parser("gist", help="print a session's latest gist; --post stores one for this session")
    gi.add_argument("session_id", nargs="?", help="local id prefix; default = the current directory's session")
    gi.add_argument("--post", metavar="FILE", help="store FILE (- for stdin) as this session's gist, then print it back")
    gi.set_defaults(fn=cmd_gist)

    pg = sub.add_parser("purge", help="delete one session; SESSION_ID may be an 8-char prefix")
    pg.add_argument("session_id")
    pg.set_defaults(fn=cmd_purge)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (RuntimeError, OSError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
