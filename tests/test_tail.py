import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import hive_mind as am

A = "aaaaaaaa-1111-4111-8111-111111111111"
B = "bbbbbbbb-2222-4222-8222-222222222222"


def noon(days_ago=0):
    """`--since today` cuts at local midnight, so fixtures pinned to a calendar date go stale overnight."""
    local = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


TODAY = noon()
YESTERDAY = noon(1)


def hhmm(ts=TODAY):
    return am.local_time(ts, "%H:%M")


def turn(seq, ts, role="user", text=None):
    return {"seq": seq, "role": role, "ts": ts, "text": text or f"turn {seq}"}


def session(sid, turns, title="t", author="A", branch="main"):
    return {"id": sid, "title": title, "author": author, "branches": [branch], "turns": len(turns)}


def stub_request(sessions):
    """sessions: [(summary, [turns])] newest-updated first, as the list endpoint returns them."""

    def request(cfg, method, path, body=None, query=None, timeout=15):
        if path == "/sessions":
            return 200, {"content": [s for s, _ in sessions]}
        sid = path.rsplit("/", 1)[1]
        turns = next(t for s, t in sessions if s["id"] == sid)
        return 200, {"messages": [m for m in turns if m["seq"] >= (query or {}).get("from", 0)]}

    return request


class TailTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.enterContext(mock.patch.object(am, "STATE_DIR", self.state))
        self.enterContext(mock.patch.object(am, "load_config", lambda: {"server": "http://x", "web": "http://x", "token": "t"}))
        self.enterContext(mock.patch.object(am, "project_filter", lambda args: "r"))
        self.enterContext(mock.patch.object(am, "beamed_here", lambda cwd: {"id": B}))

    def tail(self, sessions, *argv):
        args = am.build_parser().parse_args(["tail", *argv])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(am, "request", stub_request(sessions)), redirect_stdout(out), redirect_stderr(err):
            try:
                args.fn(args)
            except SystemExit as e:
                err.write(str(e.code) + "\n")
        return out.getvalue().splitlines(), err.getvalue()

    def test_first_run_sets_the_cursor_and_prints_nothing(self):
        turns = [turn(i, TODAY) for i in range(3)]
        lines, err = self.tail([(session(A, turns), turns)])
        self.assertEqual(lines, [])
        self.assertIn("--since today", err)
        self.assertEqual(json.loads((self.state / "tail-r.json").read_text())[A], 3)

    def test_since_bypasses_the_cursor(self):
        turns = [turn(0, YESTERDAY), turn(1, TODAY)]
        lines, _ = self.tail([(session(A, turns), turns)], "--since", "today")
        self.assertEqual([l for l in lines if not l.startswith("#")], [f"{hhmm()} user: turn 1"])

    def test_bounded_to_limit_with_a_skipped_marker(self):
        turns = [turn(i, TODAY) for i in range(5)]
        lines, err = self.tail([(session(A, turns), turns)], "--since", "today", "--limit", "2")
        self.assertIn("+3 older turns skipped", err)
        self.assertEqual([l for l in lines if not l.startswith("#")], [f"{hhmm()} user: turn 3", f"{hhmm()} user: turn 4"])

    def test_own_session_is_excluded_unless_self(self):
        ta = [turn(0, TODAY, text="theirs")]
        tb = [turn(0, TODAY, text="mine")]
        rows = [(session(A, ta), ta), (session(B, tb, title="own"), tb)]
        lines, _ = self.tail(rows, "--since", "today")
        self.assertEqual([l for l in lines if not l.startswith("#")], [f"{hhmm()} user: theirs"])
        lines, _ = self.tail(rows, "--since", "today", "--self")
        self.assertIn(f"{hhmm()} user: mine", lines)

    def test_grouped_header_per_session(self):
        ta = [turn(0, TODAY)]
        lines, _ = self.tail([(session(A, ta, title="a title", author="Ann"), ta)], "--since", "today")
        self.assertEqual(lines[0], "# aaaaaaaa Ann · a title · main")

    def test_tsv_carries_the_timestamp(self):
        ta = [turn(7, TODAY)]
        lines, _ = self.tail([(session(A, ta), ta)], "--since", "today", "--tsv")
        self.assertEqual(lines[0].split("\t")[:3], ["aaaaaaaa", "7", TODAY])


if __name__ == "__main__":
    unittest.main()
