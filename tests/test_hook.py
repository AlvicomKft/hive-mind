import fcntl
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "hive_mind.py"
FIXTURE = ROOT / "tests" / "fixtures" / "claude_session.jsonl"
SESSION = "11111111-2222-4333-8444-555555555555"
LOCK_SESSION = "99999999-2222-4333-8444-555555555555"
SECRETS = ["AKIAIOSFODNN7EXAMPLE", "hunter2hunter", "Pa55w0rd", "MIIEowIBAAKCAQEA7", "athmind_0123456789abcdef", "Zq9vK2mXp7Lr4TnB8wYc3JdF6gHs1AeU5oIiR0kNbVtQxWzM", "wJalrXUtnFEMI", "xoxp-9876543210", ".aws/credentials"]


class Stub(BaseHTTPRequestHandler):
    requests = []
    last_seq = "auto"
    fail_from = None

    @classmethod
    def reset(cls):
        cls.requests = []
        cls.last_seq = "auto"
        cls.fail_from = None

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if Stub.fail_from is not None and len(Stub.requests) > Stub.fail_from:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "nope"}')
            return
        seqs = [m["seq"] for r in Stub.requests for m in r["body"]["messages"]]
        last = (max(seqs) if seqs else None) if Stub.last_seq == "auto" else Stub.last_seq
        out = json.dumps({"lastSeq": last}).encode()
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class HookTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.repo = tmp / "repo"
        cls.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(cls.repo)], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "remote", "add", "origin", "git@github.com:Alvicom/Demo.git"], check=True)
        cls.state = tmp / "state"
        cls.config = tmp / "config.json"
        cls.config.write_text(json.dumps({"server": f"http://127.0.0.1:{cls.server.server_port}", "web": "http://app.test", "token": "athmind_testtoken"}))
        cls.transcript = tmp / f"{SESSION}.jsonl"
        cls.env = {**os.environ, "HIVE_MIND_CONFIG": str(cls.config), "HIVE_MIND_STATE_DIR": str(cls.state), "HOME": str(tmp)}
        cls.env.pop("HIVE_MIND", None)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def run_hook(self, event_name, extra=()):
        event = {"session_id": SESSION, "transcript_path": str(self.transcript), "cwd": str(self.repo), "hook_event_name": event_name}
        cmd = [sys.executable, str(SCRIPT), "hook", *extra]
        return subprocess.run(cmd, input=json.dumps(event), capture_output=True, text=True, env=self.env)

    def test_incremental_sync(self):
        Stub.requests.clear()
        lines = FIXTURE.read_text().splitlines(keepends=True)
        self.transcript.write_text("".join(lines[:10]))
        res = self.run_hook("Stop")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))
        self.assertEqual(len(Stub.requests), 1)
        first = Stub.requests[0]
        self.assertEqual(first["path"], f"/api/v1/agent-history/sessions/{SESSION}")
        self.assertEqual(first["auth"], "Bearer athmind_testtoken")
        body = first["body"]
        self.assertEqual(set(body), {"source", "remote", "branch", "cwd", "title", "parentSessionId", "startedAt", "updatedAt", "completed", "inputTokens", "outputTokens", "cacheReadTokens", "cacheCreationTokens", "models", "spawnDepth", "modelExplicit", "messages"})
        self.assertEqual((body["source"], body["remote"], body["branch"], body["completed"]), ("claude", "github.com/Alvicom/Demo", "main", False))
        self.assertEqual(body["startedAt"], "2026-09-03T10:00:00.000Z")
        self.assertEqual(body["updatedAt"], "2026-09-03T10:00:06+00:00")
        self.assertEqual([m["seq"] for m in body["messages"]], [0, 1, 2, 3, 4])
        self.assertEqual(set(body["messages"][0]), {"seq", "role", "toolName", "text", "ts", "branch"})
        self.assertEqual([m.get("branch") for m in body["messages"]], ["main", None, None, None, None])
        self.assertEqual((body["inputTokens"], body["outputTokens"]), (600, 60))
        state = json.loads((self.state / f"{SESSION}.json").read_text())
        self.assertEqual(state["next_seq"], 5)
        self.assertEqual(state["bytes"], len("".join(lines[:10]).encode()))

        self.transcript.write_text("".join(lines))
        res = self.run_hook("Stop")
        self.assertEqual(res.returncode, 0, res.stderr)
        second = Stub.requests[1]["body"]
        self.assertEqual([m["seq"] for m in second["messages"]], [5, 6])
        self.assertEqual(second["title"], body["title"])
        self.assertEqual((second["inputTokens"], second["outputTokens"]), (1000, 100))
        self.assertFalse(second["completed"])
        self.assertEqual(json.loads((self.state / f"{SESSION}.json").read_text())["next_seq"], 7)

        res = self.run_hook("SessionEnd")
        self.assertEqual(res.returncode, 0, res.stderr)
        third = Stub.requests[2]["body"]
        self.assertTrue(third["completed"])
        # A completed-only post keeps the historic updatedAt: the last message ts, not now.
        self.assertEqual(third["updatedAt"], second["updatedAt"])
        self.assertEqual(third["messages"], [])

        sent = json.dumps(Stub.requests) + (self.state / f"{SESSION}.json").read_text()
        for s in SECRETS:
            self.assertNotIn(s, sent, s)
        self.assertFalse((self.state / "hook.log").exists())

    def test_a_second_hook_waits_for_the_state_file_lock(self):
        """Stop runs in the background: a fast next turn starts a second hook on this state file."""
        Stub.requests.clear()
        transcript = Path(self.tmp.name) / f"{LOCK_SESSION}.jsonl"
        transcript.write_text(FIXTURE.read_text())
        event = {"session_id": LOCK_SESSION, "transcript_path": str(transcript), "cwd": str(self.repo), "hook_event_name": "Stop"}
        self.state.mkdir(parents=True, exist_ok=True)
        with open(self.state / f"{LOCK_SESSION}.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            second = subprocess.Popen([sys.executable, str(SCRIPT), "hook"], stdin=subprocess.PIPE, text=True, env=self.env)
            second.stdin.write(json.dumps(event))
            second.stdin.close()
            with self.assertRaises(subprocess.TimeoutExpired):
                second.wait(timeout=2)
            self.assertEqual(Stub.requests, [])
        self.assertEqual(second.wait(timeout=15), 0)
        self.assertEqual(len(Stub.requests), 1)
        self.assertEqual(Stub.requests[0]["path"], f"/api/v1/agent-history/sessions/{LOCK_SESSION}")

    def test_dry_run_and_off_and_non_git(self):
        n = len(Stub.requests)
        self.transcript.write_text(FIXTURE.read_text())
        res = subprocess.run([sys.executable, str(SCRIPT), "hook", "--dry-run"], input=json.dumps({"session_id": "dry", "transcript_path": str(self.transcript), "cwd": str(self.repo), "hook_event_name": "Stop"}), capture_output=True, text=True, env=self.env)
        payload = json.loads(res.stdout)[0]
        self.assertEqual(payload["remote"], "github.com/Alvicom/Demo")
        self.assertEqual(len(payload["messages"]), 7)
        self.assertEqual(len(Stub.requests), n)
        res = subprocess.run([sys.executable, str(SCRIPT), "hook"], input="{}", capture_output=True, text=True, env={**self.env, "HIVE_MIND": "off"})
        self.assertEqual((res.returncode, res.stdout), (0, ""))
        res = subprocess.run([sys.executable, str(SCRIPT), "hook"], input=json.dumps({"session_id": "x", "transcript_path": str(self.transcript), "cwd": self.tmp.name, "hook_event_name": "Stop"}), capture_output=True, text=True, env=self.env)
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))
        self.assertEqual(len(Stub.requests), n)

    def test_roots_limit_the_hook_to_listed_directories(self):
        self.transcript.write_text(FIXTURE.read_text())
        base = json.loads(self.config.read_text())
        event = json.dumps({"session_id": "rooted", "transcript_path": str(self.transcript), "cwd": str(self.repo), "hook_event_name": "Stop"})
        try:
            self.config.write_text(json.dumps({**base, "roots": [str(Path(self.tmp.name) / "elsewhere")]}))
            res = subprocess.run([sys.executable, str(SCRIPT), "hook", "--dry-run"], input=event, capture_output=True, text=True, env=self.env)
            self.assertEqual((res.returncode, res.stdout), (0, ""))
            self.config.write_text(json.dumps({**base, "roots": [self.tmp.name]}))
            res = subprocess.run([sys.executable, str(SCRIPT), "hook", "--dry-run"], input=event, capture_output=True, text=True, env=self.env)
            self.assertEqual(json.loads(res.stdout)[0]["remote"], "github.com/Alvicom/Demo")
        finally:
            self.config.write_text(json.dumps(base))

    def test_subagents_post_as_child_sessions(self):
        Stub.requests.clear()
        self.transcript.write_text(FIXTURE.read_text())
        subagents = self.transcript.parent / SESSION / "subagents"
        subagents.mkdir(parents=True, exist_ok=True)
        (subagents / "agent-abc123.meta.json").write_text(json.dumps({"agentType": "general-purpose", "description": "Child task", "model": "opus"}))
        (subagents / "agent-abc123.jsonl").write_text(
            json.dumps({"type": "user", "isSidechain": True, "agentId": "abc123", "timestamp": "2026-09-03T11:00:00.000Z", "message": {"role": "user", "content": "do the thing"}}) + "\n"
            + json.dumps({"type": "assistant", "isSidechain": True, "agentId": "abc123", "timestamp": "2026-09-03T11:00:01.000Z", "message": {"role": "assistant", "model": "claude-opus-5", "id": "m9", "usage": {"input_tokens": 7, "output_tokens": 3}, "content": [{"type": "text", "text": "done"}]}}) + "\n"
        )
        (subagents / "agent-nested.meta.json").write_text(json.dumps({"description": "Nested", "spawnDepth": 2, "parentAgentId": "abc123"}))
        (subagents / "agent-nested.jsonl").write_text(
            json.dumps({"type": "user", "isSidechain": True, "agentId": "nested", "timestamp": "2026-09-03T11:01:00.000Z", "message": {"role": "user", "content": "deeper"}}) + "\n"
            + json.dumps({"type": "assistant", "isSidechain": True, "agentId": "nested", "timestamp": "2026-09-03T11:01:01.000Z", "message": {"role": "assistant", "model": "claude-fable-5-1", "id": "m10", "usage": {"input_tokens": 1, "output_tokens": 1}, "content": [{"type": "text", "text": "ok"}]}}) + "\n"
        )
        (subagents / "agent-empty.meta.json").write_text(json.dumps({"description": "No text"}))
        (subagents / "agent-empty.jsonl").write_text(json.dumps({"type": "system", "content": "noise"}) + "\n")
        res = self.run_hook("Stop")
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        paths = [r["path"] for r in Stub.requests]
        self.assertEqual(paths, ["/api/v1/agent-history/sessions/abc123", "/api/v1/agent-history/sessions/nested"])
        child = Stub.requests[0]["body"]
        nested = Stub.requests[1]["body"]
        # Nesting is flattened onto the main session; spawnDepth carries the real shape.
        self.assertEqual((nested["parentSessionId"], nested["spawnDepth"], nested["modelExplicit"]), (SESSION, 2, False))
        self.assertEqual((child["parentSessionId"], child["title"], child["remote"], child["branch"]), (SESSION, "Child task", "github.com/Alvicom/Demo", "main"))
        self.assertEqual(child["models"], ["claude-opus-5"])
        self.assertEqual((child["spawnDepth"], child["modelExplicit"]), (1, True))
        self.assertEqual((child["inputTokens"], child["outputTokens"]), (7, 3))
        self.assertEqual([(m["role"], m["text"]) for m in child["messages"]], [("user", "do the thing"), ("assistant", "done")])
        state = json.loads((self.state / f"{SESSION}.json").read_text())
        self.assertEqual(state["children"]["abc123"]["next_seq"], 2)
        self.assertEqual(state["children"]["empty"]["next_seq"], 0)


class StateTest(unittest.TestCase):
    """Offsets are per server, and a session lands whole or not at all."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Stub.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        self.repo = home / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", "git@github.com:Alvicom/Demo.git"], check=True)
        self.state = home / "state"
        self.config = home / "config.json"
        self.server_url = f"http://127.0.0.1:{self.server.server_port}"
        self.config.write_text(json.dumps({"server": self.server_url, "web": "http://app.test", "token": "athmind_testtoken"}))
        self.transcript = home / f"{SESSION}.jsonl"
        self.env = {**os.environ, "HOME": str(home), "HIVE_MIND_CONFIG": str(self.config), "HIVE_MIND_STATE_DIR": str(self.state)}
        self.env.pop("HIVE_MIND", None)

    def run_hook(self, event_name="Stop"):
        event = {"session_id": SESSION, "transcript_path": str(self.transcript), "cwd": str(self.repo), "hook_event_name": event_name}
        res = subprocess.run([sys.executable, str(SCRIPT), "hook"], input=json.dumps(event), capture_output=True, text=True, env=self.env)
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))

    def state_file(self):
        return json.loads((self.state / f"{SESSION}.json").read_text())

    def turns(self, count):
        self.transcript.write_text("".join(
            json.dumps({"type": "user", "cwd": str(self.repo), "sessionId": SESSION, "timestamp": f"2026-09-03T10:00:00.{i:03d}Z", "message": {"role": "user", "content": f"turn {i}"}}) + "\n"
            for i in range(count)
        ))

    def test_a_new_server_re_ships_the_session_from_zero(self):
        self.transcript.write_text(FIXTURE.read_text())
        self.run_hook()
        self.assertEqual([m["seq"] for m in Stub.requests[0]["body"]["messages"]], list(range(7)))
        self.assertEqual((self.state_file()["server"], self.state_file()["next_seq"]), (self.server_url, 7))
        # Same stub, different origin string: a `login` elsewhere must not resume mid-transcript.
        elsewhere = self.server_url.replace("127.0.0.1", "localhost")
        self.config.write_text(json.dumps({"server": elsewhere, "web": "http://app.test", "token": "athmind_testtoken"}))
        self.run_hook()
        self.assertEqual([m["seq"] for m in Stub.requests[1]["body"]["messages"]], list(range(7)))
        self.assertEqual((self.state_file()["server"], self.state_file()["next_seq"]), (elsewhere, 7))

    def test_a_server_missing_messages_resets_the_slot(self):
        self.transcript.write_text(FIXTURE.read_text())
        Stub.last_seq = 2
        self.run_hook()
        self.assertEqual((self.state_file()["bytes"], self.state_file()["next_seq"], self.state_file()["meta"]), (0, 0, {}))
        self.assertIn("re-shipping this session from 0", (self.state / "hook.log").read_text())
        Stub.last_seq = "auto"
        self.run_hook()
        self.assertEqual([m["seq"] for m in Stub.requests[1]["body"]["messages"]], list(range(7)))
        self.assertEqual(self.state_file()["next_seq"], 7)

    def test_long_sessions_post_in_chunks_and_a_failed_chunk_keeps_the_offsets(self):
        self.turns(1200)
        Stub.fail_from = 2
        self.run_hook()
        sizes = [len(r["body"]["messages"]) for r in Stub.requests]
        self.assertEqual(sizes[:2], [500, 500])
        self.assertEqual(len(sizes), 3)
        self.assertIn("HTTP 400", (self.state / "hook.log").read_text())
        # Nothing is persisted for a session that did not land whole: the retry renumbers identically.
        self.assertFalse((self.state / f"{SESSION}.json").is_file())
        Stub.fail_from = None
        Stub.requests.clear()
        self.run_hook()
        self.assertEqual([len(r["body"]["messages"]) for r in Stub.requests], sizes)
        seqs = [m["seq"] for r in Stub.requests for m in r["body"]["messages"]]
        self.assertEqual(seqs, list(range(sum(sizes))))
        self.assertEqual(self.state_file()["next_seq"], sum(sizes))


class BeamTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Stub.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        self.repo = home / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", "git@github.com:Alvicom/Demo.git"], check=True)
        transcript = home / ".claude" / "projects" / "repo" / f"{SESSION}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("".join(
            json.dumps({"type": "user", "cwd": str(self.repo), "sessionId": SESSION, "timestamp": f"2026-09-03T10:00:0{i}.000Z", "message": {"role": "user", "content": f"turn {i}"}}) + "\n"
            for i in range(4)
        ))
        config = home / "config.json"
        config.write_text(json.dumps({"server": f"http://127.0.0.1:{self.server.server_port}", "web": "http://app.test", "token": "athmind_testtoken"}))
        self.env = {**os.environ, "HOME": str(home), "HIVE_MIND_CONFIG": str(config), "HIVE_MIND_STATE_DIR": str(home / "state")}
        self.env.pop("HIVE_MIND", None)

    def beam(self, *args):
        res = subprocess.run([sys.executable, str(SCRIPT), "beam", SESSION[:8], *args], capture_output=True, text=True, env=self.env, cwd=self.repo)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_beam_reports_what_it_posted_and_force_re_ships(self):
        self.assertIn("4 sent", self.beam())
        self.assertEqual(sum(len(r["body"]["messages"]) for r in Stub.requests), 4)
        self.assertIn("nothing new", self.beam())
        self.assertEqual(sum(len(r["body"]["messages"]) for r in Stub.requests), 4)
        self.assertIn("4 sent", self.beam("--force"))
        self.assertEqual([m["seq"] for m in Stub.requests[-1]["body"]["messages"]], [0, 1, 2, 3])


if __name__ == "__main__":
    unittest.main()
