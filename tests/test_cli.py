import json
import os
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "hive_mind.py"
HOOK_EVENTS = ("Stop", "SessionEnd")
SHARE_SESSION = "11111111-2222-4333-8444-555555555555"
OTHER_SESSION = "55555555-2222-4333-8444-555555555555"


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
        if self.path.startswith("/api/v1/agent-history/sessions/"):
            self._reply(Stub.payloads.get("session", {"session": {}, "messages": []}))
            return
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
        config.write_text(json.dumps({"server": f"http://127.0.0.1:{cls.server.server_port}", "web": "http://app.test", "token": "athmind_testtoken"}))
        cls.env = {**os.environ, "HIVE_MIND_CONFIG": str(config), "HIVE_MIND_STATE_DIR": str(tmp / "state"), "HOME": str(tmp)}
        cls.env.pop("CLAUDE_CODE_SESSION_ID", None)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def seed_transcript(self, session_id):
        """A local transcript plus a hook state file = a session `share` can resolve for this cwd."""
        directory = Path(self.tmp.name) / ".claude" / "projects" / "repo"
        directory.mkdir(parents=True, exist_ok=True)
        transcript = directory / f"{session_id}.jsonl"
        transcript.write_text(
            json.dumps({"type": "user", "cwd": str(self.repo), "sessionId": session_id, "timestamp": "2026-09-03T10:00:00.000Z", "message": {"role": "user", "content": "share me"}}) + "\n"
        )
        state = Path(self.tmp.name) / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / f"{session_id}.json").write_text(json.dumps({"bytes": 0, "next_seq": 0, "meta": {}}))
        return transcript

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

    def test_sessions_prints_reply_line_and_markers(self):
        Stub.payloads["sessions"] = {
            "content": [
                {
                    "id": "abcdef0123456789",
                    "author": "Alice",
                    "branch": "feat/x",
                    "turns": 12,
                    "models": ["claude-fable-5-1"],
                    "title": "first prompt",
                    "summary": "1. Primary Request and Intent:\n   ship it",
                    "agents": {"count": 3, "maxDepth": 2, "models": [], "inputTokens": 0, "outputTokens": 0},
                    "lastPrompt": "now do the client side",
                    "lastReply": "Client updated. Tests pass.",
                    "updatedAt": "2026-09-03T10:00:00Z",
                }
            ],
            "totalElements": 1,
        }
        res = self.run_cli("sessions")
        self.assertEqual(res.returncode, 0, res.stderr)
        meta, reply = res.stdout.splitlines()[:2]
        self.assertIn("fable-5-1", meta)
        self.assertIn("3 agents", meta)
        self.assertIn("Σ", meta)
        self.assertIn("feat/x", meta)
        self.assertIn("first prompt", meta)
        self.assertNotIn("ship it", res.stdout)
        self.assertEqual(reply, "  ↳ Client updated.")
        self.assertNotIn("now do the client side", res.stdout)

        verbose = self.run_cli("sessions", "-v")
        self.assertIn("  › now do the client side", verbose.stdout)

        tsv = self.run_cli("sessions", "--tsv")
        cols = tsv.stdout.splitlines()[0].split("\t")
        self.assertEqual(cols[-2:], ["now do the client side", "Client updated. Tests pass."])

        titles = self.run_cli("sessions", "--titles")
        self.assertEqual(len(titles.stdout.splitlines()), 1)

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



    def test_share_resolves_the_current_session_beams_and_prints_the_block(self):
        Stub.payloads.clear()
        Stub.seen.clear()
        self.seed_transcript(SHARE_SESSION)
        Stub.payloads["session"] = {
            "session": {
                "id": SHARE_SESSION,
                "title": "share me",
                "author": "Alice",
                "remote": "github.com/Alvicom/Demo",
                "branch": "main",
                "branches": ["main"],
            },
            "messages": [],
        }
        res = self.run_cli("share")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(
            res.stdout.splitlines(),
            [
                "share me · Alice · github.com/Alvicom/Demo @ main",
                f"web:  http://app.test/agent-history/{SHARE_SESSION}",
                "cli:  hive-mind show 11111111",
            ],
        )
        # Read-only: the Stop hook already beams every turn, share must not write.
        self.assertEqual(len(Stub.seen), 1)
        self.assertTrue(Stub.seen[0].startswith(f"/api/v1/agent-history/sessions/{SHARE_SESSION}?"))

    def test_share_json_and_unresolvable_cwd(self):
        Stub.payloads.clear()
        self.seed_transcript(SHARE_SESSION)
        Stub.payloads["session"] = {"session": {"id": SHARE_SESSION, "title": "t", "author": "Alice", "remote": "r", "branch": "main", "branches": ["main"]}, "messages": []}
        res = self.run_cli("share", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout)["cli"], "hive-mind show 11111111")
        empty = subprocess.run([sys.executable, str(SCRIPT), "share"], cwd=self.tmp.name, capture_output=True, text=True, env=self.env)
        self.assertEqual(empty.returncode, 1)
        self.assertEqual(empty.stdout, "")
        self.assertIn("no beamed session", empty.stderr)

    def test_the_exported_session_id_beats_the_newest_transcript_in_the_directory(self):
        """A planner and a worker session share this cwd; only the harness knows which one we are."""
        Stub.payloads.clear()
        self.seed_transcript(SHARE_SESSION)
        newest = self.seed_transcript(OTHER_SESSION)
        os.utime(newest, (time.time() + 10, time.time() + 10))
        Stub.payloads["session"] = {"session": {"id": "resolved", "title": "t", "author": "A", "remote": "r", "branch": "main", "branches": ["main"]}, "messages": []}

        Stub.seen.clear()
        self.assertEqual(self.run_cli("share").returncode, 0)
        self.assertTrue(Stub.seen[-1].startswith(f"/api/v1/agent-history/sessions/{OTHER_SESSION}?"), Stub.seen)

        Stub.seen.clear()
        res = subprocess.run(
            [sys.executable, str(SCRIPT), "share"], cwd=self.repo, capture_output=True, text=True,
            env={**self.env, "CLAUDE_CODE_SESSION_ID": SHARE_SESSION},
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(Stub.seen[-1].startswith(f"/api/v1/agent-history/sessions/{SHARE_SESSION}?"), Stub.seen)

    def test_doctor_flags_a_different_deployment(self):
        res = self.run_cli("doctor", "https://athene.dev.alvicom.ai")
        self.assertEqual(res.returncode, 1)
        self.assertIn(
            "configured for http://app.test, you asked for https://athene.dev.alvicom.ai;"
            " re-run login --server https://athene.dev.alvicom.ai",
            res.stdout,
        )
        self.assertNotIn("reachable", res.stdout)

    def test_doctor_takes_the_configured_deployment(self):
        res = self.run_cli("doctor", "http://app.test/")
        self.assertNotIn("you asked for", res.stdout)
        self.assertIn("reachable", res.stdout)


class LoginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        self.config = home / "config.json"
        self.config.write_text(json.dumps({"server": "http://api.test", "web": "http://app.test", "token": "athmind_" + "a" * 40}))
        self.env = {**os.environ, "HOME": str(home), "HIVE_MIND_CONFIG": str(self.config), "HIVE_MIND_STATE_DIR": str(home / "state")}

    def run_cli(self, *args, stdin=""):
        return subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True, text=True, env=self.env, cwd=self.tmp.name)

    def test_a_piped_token_is_read_from_stdin(self):
        res = self.run_cli("login", "--server", "http://127.0.0.1:1", stdin="athmind_" + "0f" * 20 + "\n")
        self.assertEqual(res.returncode, 1)
        self.assertIn("is not an Athene app config", res.stderr)

    def test_a_piped_mangled_paste_never_reaches_the_config(self):
        before = self.config.read_text()
        res = self.run_cli("login", "--server", "http://app.test", stdin="\x1b[200~ab12")
        self.assertEqual(res.returncode, 1)
        self.assertIn("not a Hive Mind token", res.stderr)
        self.assertEqual(self.config.read_text(), before)

    def test_a_mangled_paste_never_reaches_the_config(self):
        before = self.config.read_text()
        res = self.run_cli("login", "--server", "http://app.test", "--token", "\x1b[200~ab12")
        self.assertEqual(res.returncode, 1)
        self.assertIn("not a Hive Mind token", res.stderr)
        self.assertIn("wl-paste", res.stderr)
        self.assertEqual(self.config.read_text(), before)

    def test_a_padded_token_is_accepted_and_the_app_url_is_probed(self):
        res = self.run_cli("login", "--server", "http://127.0.0.1:1", "--token", "  athmind_" + "0f" * 20 + "\n")
        self.assertEqual(res.returncode, 1)
        self.assertIn("is not an Athene app config", res.stderr)

    def test_no_token_anywhere_names_the_piped_form(self):
        res = self.run_cli("login", "--server", "http://app.test")
        self.assertEqual(res.returncode, 1)
        self.assertIn("wl-paste | hive-mind login", res.stderr)

class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        (home / ".claude").mkdir()
        (home / ".claude" / "settings.json").write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}}))
        self.env = {**os.environ, "HOME": str(home), "HIVE_MIND_CONFIG": str(home / "config.json"), "HIVE_MIND_STATE_DIR": str(home / "state")}
        self.home = home

    def seed_transcript(self, session_id):
        """A local transcript plus a hook state file = a session `share` can resolve for this cwd."""
        directory = Path(self.tmp.name) / ".claude" / "projects" / "repo"
        directory.mkdir(parents=True, exist_ok=True)
        transcript = directory / f"{session_id}.jsonl"
        transcript.write_text(
            json.dumps({"type": "user", "cwd": str(self.repo), "sessionId": session_id, "timestamp": "2026-09-03T10:00:00.000Z", "message": {"role": "user", "content": "share me"}}) + "\n"
        )
        state = Path(self.tmp.name) / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / f"{session_id}.json").write_text(json.dumps({"bytes": 0, "next_seq": 0, "meta": {}}))
        return transcript

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
        self.assertIn("no token; pipe it in", res.stderr)
        settings = self.settings()
        self.assertEqual(settings["model"], "opus")
        commands = [h["command"] for e in HOOK_EVENTS for m in settings["hooks"][e] for h in m["hooks"]]
        self.assertEqual(sum("hive_mind.py" in c for c in commands), 2)
        self.assertIn("other", commands)
        link = self.home / ".claude" / "skills" / "hive-mind"
        self.assertEqual(link.resolve(), (ROOT / "skills" / "search").resolve())
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
