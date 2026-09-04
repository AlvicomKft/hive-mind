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
SECRETS = ["AKIAIOSFODNN7EXAMPLE", "hunter2hunter", "Pa55w0rd", "MIIEowIBAAKCAQEA7", "athmind_0123456789abcdef", "Zq9vK2mXp7Lr4TnB8wYc3JdF6gHs1AeU5oIiR0kNbVtQxWzM", "wJalrXUtnFEMI", "xoxp-9876543210", ".aws/credentials"]


class Stub(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        seqs = [m["seq"] for r in Stub.requests for m in r["body"]["messages"]]
        out = json.dumps({"lastSeq": max(seqs) if seqs else None}).encode()
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
        self.assertEqual(set(body), {"source", "remote", "branch", "cwd", "title", "parentSessionId", "startedAt", "updatedAt", "completed", "inputTokens", "outputTokens", "models", "spawnDepth", "modelExplicit", "messages"})
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


if __name__ == "__main__":
    unittest.main()
