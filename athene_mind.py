#!/usr/bin/env python3
"""Athene Mind: ships coding-agent transcripts to Athene (hook) and searches them (CLI)."""
import argparse
import getpass
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("ATHENE_MIND_CONFIG") or "~/.config/athene-mind/config.json").expanduser()
STATE_DIR = Path(os.environ.get("ATHENE_MIND_STATE_DIR") or "~/.local/state/athene-mind").expanduser()
USER_IGNORE = Path("~/.config/athene-mind/ignore").expanduser()
API = "/api/v1/agent-history"
TOOL_INPUT_CHARS = 200
TITLE_CHARS = 240
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


def load_config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        sys.exit(f"no config at {CONFIG_PATH}; run: athene-mind login")
    if not cfg.get("server") or not cfg.get("token"):
        sys.exit(f"config {CONFIG_PATH} needs server and token; run: athene-mind login")
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
        extra.append(Path(top) / ".athene-mind-ignore")
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
_HARNESS_NOISE = ("<local-command-", "<command-name>", "<command-message>")


def clean_user_text(text):
    text = _SYSTEM_REMINDER.sub("", text).strip()
    return "" if text.startswith(_HARNESS_NOISE) else text


def parse_claude(lines, meta):
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = rec.get("type")
        if kind not in ("user", "assistant") or rec.get("isSidechain"):
            continue
        ts = rec.get("timestamp")
        meta.setdefault("startedAt", ts)
        msg = rec.get("message") or {}
        content = msg.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else (content or [])
        texts = [b.get("text", "") for b in blocks if b.get("type") == "text" and b.get("text")]
        if kind == "assistant":
            usage = msg.get("usage") or {}
            if usage and msg.get("id") != meta.get("usageMsgId"):
                meta["usageMsgId"] = msg.get("id")
                meta["inputTokens"] = meta.get("inputTokens", 0) + (usage.get("input_tokens") or 0)
                meta["outputTokens"] = meta.get("outputTokens", 0) + (usage.get("output_tokens") or 0)
        text = "\n".join(texts)
        if kind == "user":
            text = clean_user_text(text)
        if text:
            if kind == "user" and not meta.get("title"):
                meta["title"] = " ".join(text.split())[:TITLE_CHARS]
            out.append({"role": kind, "toolName": None, "text": text, "ts": ts})
        for b in blocks:
            if b.get("type") == "tool_use":
                name = b.get("name") or "tool"
                out.append({"role": "tool_call", "toolName": name, "text": tool_call_text(name, b.get("input", {})), "ts": ts})
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
    if os.environ.get("ATHENE_MIND", "").lower() == "off":
        return
    try:
        run_hook(json.load(sys.stdin), dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001 - never block the harness
        log(f"error: {type(e).__name__}: {e}")


def run_hook(event, dry_run=False):
    session_id = event.get("session_id")
    transcript = event.get("transcript_path")
    cwd = event.get("cwd") or os.getcwd()
    if not session_id or not transcript or not os.path.isfile(transcript):
        return
    remote = cwd_remote(cwd)
    if not remote:
        return
    state_path = STATE_DIR / f"{session_id}.json"
    state = json.loads(state_path.read_text()) if state_path.is_file() else {"bytes": 0, "next_seq": 0, "meta": {}}
    lines, new_offset = read_new_lines(transcript, state["bytes"])
    source = detect_source(transcript)
    meta = state["meta"]
    raw = PARSERS[source](lines, meta)
    completed = event.get("hook_event_name") == "SessionEnd"
    if not raw and not (completed and state["next_seq"]):
        return
    patterns = load_patterns(cwd)
    if meta.get("title"):
        meta["title"] = scrub(meta["title"], patterns)
    messages = []
    seq = state["next_seq"]
    for m in raw:
        messages.append({"seq": seq, "role": m["role"], "toolName": m["toolName"], "text": scrub(m["text"], patterns), "ts": m["ts"]})
        seq += 1
    payload = {
        "source": source,
        "remote": remote,
        "branch": git(cwd, "symbolic-ref", "--short", "HEAD"),
        "cwd": cwd,
        "title": meta.get("title"),
        "parentSessionId": meta.get("parentSessionId"),
        "startedAt": meta.get("startedAt") or datetime.now(timezone.utc).isoformat(),
        "completed": completed,
        "inputTokens": meta.get("inputTokens", 0),
        "outputTokens": meta.get("outputTokens", 0),
        "messages": messages,
    }
    if dry_run:
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        print()
        return
    cfg = load_config()
    status, body = request(cfg, "POST", f"/sessions/{session_id}", payload, timeout=5)
    if status != 202:
        raise RuntimeError(f"unexpected status {status}")
    last_seq = (body or {}).get("lastSeq")
    if isinstance(last_seq, int) and last_seq + 1 < seq:
        log(f"{session_id}: server lastSeq={last_seq} behind local next_seq={seq}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"bytes": new_offset, "next_seq": seq, "meta": meta}))


# --- cli -----------------------------------------------------------------


def parse_since(value):
    if not value:
        return None
    m = re.fullmatch(r"(\d+)([dhw])", value)
    if not m:
        return value
    n, unit = int(m.group(1)), m.group(2)
    delta = timedelta(**{{"d": "days", "h": "hours", "w": "weeks"}[unit]: n})
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


def project_filter(args):
    if args.all:
        return None
    if args.project:
        return args.project
    remote = cwd_remote(os.getcwd())
    if not remote:
        sys.exit("cwd has no origin remote; pass --project or --all")
    return remote


def cmd_login(args):
    server = args.server or input("Athene server URL: ").strip()
    token = args.token or getpass.getpass("Personal access token (athmind_...): ").strip()
    if not server or not token:
        sys.exit("server and token required")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.touch(mode=0o600)
    CONFIG_PATH.chmod(0o600)
    CONFIG_PATH.write_text(json.dumps({"server": server.rstrip("/"), "token": token}, indent=2) + "\n")
    try:
        _, remotes = request(load_config(), "GET", "/remotes")
    except (RuntimeError, urllib.error.URLError) as e:
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
        "author": args.author,
        "since": parse_since(args.since),
        "role": args.role,
        "limit": args.limit,
        "context": args.context,
    }
    _, res = request(load_config(), "POST", "/search", {k: v for k, v in body.items() if v is not None})
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return
    hits = res.get("hits") or []
    if not hits:
        print("no hits")
    for h in hits:
        snippet = " ".join(re.sub(r"</?b>", "", h.get("snippet") or "").split())
        print(f"{h.get('score') or 0:6.2f} {h['sessionId']}:{h['seq']} {(h.get('ts') or '')[:16]} {h.get('author')} {h.get('role')}\n       {snippet}")
        for c in h.get("context") or []:
            tool = f" [{c['toolName']}]" if c.get("toolName") else ""
            print(f"         {c['seq']} {c['role']}{tool}: {' '.join((c.get('text') or '').split())[:200]}")
        print()


def cmd_sessions(args):
    query = {"remote": project_filter(args), "author": args.author, "since": parse_since(args.since), "page": 0, "size": args.limit}
    _, res = request(load_config(), "GET", "/sessions", query=query)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return
    for s in res.get("content") or []:
        print(f"{(s.get('updatedAt') or '')[:16]} {s['id']} {s.get('author')} {s.get('branch') or '-'} {s.get('turns', 0):4d} {s.get('title') or ''}")


def cmd_dump(args):
    query = {"from": args.start, "to": args.end, "maxMsg": args.max_msg}
    _, res = request(load_config(), "GET", f"/sessions/{args.session_id}", query=query)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return
    s = res["session"]
    print(f"# {s['id']} {s.get('author')} {s.get('remote')} {s.get('branch') or '-'} {s.get('startedAt')}\n# {s.get('title') or ''}\n")
    for m in res.get("messages") or []:
        tool = f" [{m['toolName']}]" if m.get("toolName") else ""
        print(f"--- {m['seq']} {(m.get('ts') or '')[:16]} {m['role']}{tool}\n{m.get('text') or ''}\n")


def cmd_purge(args):
    status, _ = request(load_config(), "DELETE", f"/sessions/{args.session_id}")
    print(f"deleted {args.session_id}" if status == 204 else f"status {status}")


def build_parser():
    p = argparse.ArgumentParser(prog="athene-mind", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("hook", help="harness Stop/SessionEnd hook; reads event JSON on stdin")
    h.add_argument("--dry-run", action="store_true", help="print payload instead of posting")
    h.set_defaults(fn=cmd_hook)

    lg = sub.add_parser("login", help="store server URL + token, verify")
    lg.add_argument("--server")
    lg.add_argument("--token")
    lg.set_defaults(fn=cmd_login)

    def scope(sp):
        sp.add_argument("--project", help="remote substring; default = cwd origin remote")
        sp.add_argument("--all", action="store_true", help="every project")
        sp.add_argument("--author", help="author substring")
        sp.add_argument("--since", help="14d, 12h, 2w or ISO timestamp")
        sp.add_argument("--json", action="store_true", help="raw JSON output")

    s = sub.add_parser("search", help="ranked term search (default) or regex with -e")
    s.add_argument("terms", nargs="*")
    s.add_argument("-e", "--pattern", help="regex (Postgres ARE) instead of terms")
    s.add_argument("-F", "--fixed", action="store_true", help="literal match")
    s.add_argument("-s", "--case-sensitive", action="store_true")
    s.add_argument("-w", "--word", action="store_true", help="whole-word match")
    s.add_argument("-C", "--context", type=int, default=0, help="neighbor turns per hit")
    s.add_argument("--role", choices=["user", "assistant", "tool_call"])
    s.add_argument("--limit", type=int, default=20)
    scope(s)
    s.set_defaults(fn=cmd_search)

    ls = sub.add_parser("sessions", help="list sessions")
    ls.add_argument("--limit", type=int, default=20)
    scope(ls)
    ls.set_defaults(fn=cmd_sessions)

    d = sub.add_parser("dump", help="read one session")
    d.add_argument("session_id")
    d.add_argument("--start", type=int, default=0, help="first seq")
    d.add_argument("--end", type=int, help="last seq")
    d.add_argument("--max-msg", type=int, default=2000, help="chars kept per message")
    d.add_argument("--json", action="store_true")
    d.set_defaults(fn=cmd_dump)

    pg = sub.add_parser("purge", help="delete one session from the server")
    pg.add_argument("session_id")
    pg.set_defaults(fn=cmd_purge)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (RuntimeError, urllib.error.URLError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
