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
HOOK_EVENTS = ("Stop", "SessionEnd")


class Stub(BaseHTTPRequestHandler):
    payloads = {}
    seen = []
    bodies = []

    def _reply(self, body, status=200):
        out = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        Stub.seen.append(self.path)
        self._reply(Stub.payloads.get("sessions", {"content": [], "totalElements": 0}))

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"] or 0)))
        Stub.bodies.append(body)
        Stub.seen.append(self.path)
        if self.path.startswith("/api/v1/agent-history/sessions/"):
            self._reply({"lastSeq": len(body["messages"]) - 1}, status=202)
            return
        self._reply(Stub.payloads.get("search", {"hits": []}))

    def log_message(self, *a):
        pass


class CliTest(unittest.TestCase):
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
        config = tmp / "config.json"
        config.write_text(json.dumps({"server": f"http://127.0.0.1:{cls.server.server_port}", "token": "athmind_testtoken"}))
        cls.env = {**os.environ, "HIVE_MIND_CONFIG": str(config), "HIVE_MIND_STATE_DIR": str(tmp / "state"), "HOME": str(tmp)}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.repo, capture_output=True, text=True, env=self.env)

    def test_empty_results_exit_1_with_stdout_clean(self):
        Stub.payloads.clear()
        for args in (("search", "nothing"), ("sessions",), ("today",), ("tail",)):
            with self.subTest(args=args):
                res = self.run_cli(*args)
                self.assertEqual(res.returncode, 1)
                self.assertEqual(res.stdout, "")
                self.assertTrue(res.stderr.strip())

    def test_sessions_prints_summary_and_model(self):
        Stub.payloads["sessions"] = {
            "content": [
                {
                    "id": "abcdef0123456789",
                    "author": "Alice",
                    "turns": 12,
                    "models": ["claude-fable-5-1"],
                    "title": "first prompt",
                    "summary": "1. Primary Request and Intent:\n   ship the summary column",
                    "updatedAt": "2026-09-03T10:00:00Z",
                }
            ],
            "totalElements": 1,
        }
        res = self.run_cli("sessions")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Σ 1. Primary Request and Intent: ship the summary column", res.stdout)
        self.assertIn("fable-5-1", res.stdout)
        self.assertNotIn("first prompt", res.stdout)

    def test_branch_and_sort_reach_the_api(self):
        Stub.payloads["search"] = {
            "hits": [
                {
                    "sessionId": "abcdef0123456789",
                    "seq": 4,
                    "ts": "2026-09-03T10:00:00Z",
                    "author": "Alice",
                    "role": "user",
                    "toolName": "compact-summary",
                    "score": 0.5,
                    "snippet": "a <b>hit</b>",
                    "title": "t",
                }
            ]
        }
        Stub.bodies.clear()
        res = self.run_cli("search", "hit", "--branch", "main", "--sort", "time")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(Stub.bodies[0]["branch"], "main")
        self.assertEqual(Stub.bodies[0]["order"], "time")
        # --sort time implies flat: one line per hit, "<id>:<seq>".
        self.assertIn("abcdef01:4 Alice [summary]", res.stdout)
        Stub.seen.clear()
        res = self.run_cli("sessions", "--branch", "feat/x")
        self.assertTrue(any("branch=feat" in p for p in Stub.seen), Stub.seen)


    def test_mine_reaches_the_api(self):
        Stub.payloads.clear()
        Stub.bodies.clear()
        Stub.seen.clear()
        self.run_cli("search", "hit", "--mine")
        self.assertTrue(Stub.bodies[0]["mine"])
        self.run_cli("sessions", "--mine")
        self.assertTrue(any("mine=true" in p for p in Stub.seen), Stub.seen)

    def test_local_lists_and_beam_ships_historic_transcripts(self):
        transcript = Path(self.tmp.name) / ".claude" / "projects" / "-repo" / "11111111-2222-4333-8444-555555555555.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(
            json.dumps({"type": "user", "cwd": str(self.repo), "timestamp": "2026-01-02T10:00:00.000Z", "message": {"role": "user", "content": "old work"}}) + "\n"
            + json.dumps({"type": "assistant", "cwd": str(self.repo), "timestamp": "2026-01-02T10:00:05.000Z", "message": {"role": "assistant", "model": "claude-opus-5", "id": "m1", "content": [{"type": "text", "text": "done"}]}}) + "\n"
        )
        listed = self.run_cli("local", "--since", "10w")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("11111111", listed.stdout)
        self.assertIn("old work", listed.stdout)
        self.assertIn("-", listed.stdout)
        Stub.bodies.clear()
        beamed = self.run_cli("beam", "11111111", "--since", "10w")
        self.assertEqual(beamed.returncode, 0, beamed.stderr)
        payload = Stub.bodies[0]
        self.assertTrue(payload["completed"])
        self.assertEqual(payload["updatedAt"], "2026-01-02T10:00:05+00:00")
        self.assertEqual([m["text"] for m in payload["messages"]], ["old work", "done"])
        again = self.run_cli("local", "--since", "10w")
        self.assertIn("beamed", again.stdout)


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        (home / ".claude").mkdir()
        (home / ".claude" / "settings.json").write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}}))
        self.env = {**os.environ, "HOME": str(home), "HIVE_MIND_CONFIG": str(home / "config.json"), "HIVE_MIND_STATE_DIR": str(home / "state")}
        self.home = home

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], input="", capture_output=True, text=True, env=self.env, cwd=self.tmp.name)

    def settings(self):
        return json.loads((self.home / ".claude" / "settings.json").read_text())

    def test_unknown_harness_exits_2(self):
        res = self.run_cli("install", "--harness", "codex")
        self.assertEqual(res.returncode, 2)
        self.assertIn("not supported yet", res.stderr)

    def test_dry_run_changes_nothing(self):
        res = self.run_cli("install", "--harness", "claude", "--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("hive_mind.py", res.stdout)
        self.assertEqual(self.settings()["hooks"]["Stop"][0]["hooks"][0]["command"], "other")
        self.assertFalse((self.home / ".claude" / "skills" / "hive-mind").exists())

    def test_install_is_idempotent_and_uninstall_reverts(self):
        # No config: install stops at the login prompt, after writing hook + skill link.
        res = self.run_cli("install", "--harness", "claude")
        self.assertIn("register Stop/SessionEnd hook", res.stdout)
        self.assertIn("no terminal for the prompt", res.stderr)
        settings = self.settings()
        self.assertEqual(settings["model"], "opus")
        commands = [h["command"] for e in HOOK_EVENTS for m in settings["hooks"][e] for h in m["hooks"]]
        self.assertEqual(sum("hive_mind.py" in c for c in commands), 2)
        self.assertIn("other", commands)
        link = self.home / ".claude" / "skills" / "hive-mind"
        self.assertEqual(link.resolve(), (ROOT / "skills" / "hive-mind").resolve())
        again = self.run_cli("install", "--harness", "claude")
        self.assertIn("already registered", again.stdout)
        self.assertIn("already linked", again.stdout)
        removed = self.run_cli("install", "--harness", "claude", "--uninstall")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(link.is_symlink())
        settings = self.settings()
        self.assertEqual(settings["hooks"]["Stop"][0]["hooks"][0]["command"], "other")
        self.assertNotIn("SessionEnd", settings["hooks"])


if __name__ == "__main__":
    unittest.main()
