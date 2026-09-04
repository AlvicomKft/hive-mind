import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import hive_mind as am

SID = "11111111-2222-4333-8444-555555555555"


def turn(seq, role="user", text=None):
    return {"seq": seq, "role": role, "toolName": None, "ts": "2026-09-03T10:00:00Z", "branch": None, "text": text or f"turn {seq}"}


def stub_request(turns):
    """Serves the metadata probe (`to=0`) and the message page (`from=N`) fetch_session asks for."""

    def request(cfg, method, path, body=None, query=None, timeout=15):
        session = {"id": SID, "title": "t", "author": "A", "remote": "r", "branches": ["main"], "models": [], "turns": len(turns), "childCount": 0}
        if (query or {}).get("to") == 0:
            return 200, {"session": session, "messages": []}
        return 200, {"session": session, "messages": turns[(query or {}).get("from") or 0 :]}

    return request


class FetchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        self.enterContext(mock.patch.object(am, "CACHE_DIR", self.cache))
        self.enterContext(mock.patch.object(am, "load_config", lambda: {"server": "http://x", "web": "http://x", "token": "t"}))
        self.path = self.cache / f"{SID}.jsonl"

    def lines(self):
        return [json.loads(x) for x in self.path.read_text().splitlines()]

    def test_appends_only_the_new_turns(self):
        with mock.patch.object(am, "request", stub_request([turn(0), turn(1)])):
            _, _, turns, added, rewritten = am.fetch_session({}, SID)
        self.assertEqual((turns, added, rewritten), (2, 2, False))

        with mock.patch.object(am, "request", stub_request([turn(0), turn(1), turn(2)])):
            _, _, turns, added, rewritten = am.fetch_session({}, SID)
        self.assertEqual((turns, added, rewritten), (3, 1, False))
        self.assertEqual([m["seq"] for m in self.lines()], [0, 1, 2])

    def test_shrunk_session_is_rewritten_from_scratch(self):
        self.path.write_text("".join(json.dumps(turn(i)) + "\n" for i in range(5)))
        with mock.patch.object(am, "request", stub_request([turn(0, text="rebeamed"), turn(1, text="rebeamed")])):
            _, _, turns, added, rewritten = am.fetch_session({}, SID)
        self.assertEqual((turns, added, rewritten), (2, 2, True))
        self.assertEqual([m["text"] for m in self.lines()], ["rebeamed", "rebeamed"])

    def test_stale_cache_files_are_swept(self):
        stale, fresh = self.cache / "old.jsonl", self.cache / "new.jsonl"
        stale.write_text("{}\n")
        fresh.write_text("{}\n")
        old = time.time() - (am.CACHE_TTL_DAYS + 1) * 86400
        os.utime(stale, (old, old))
        with mock.patch.object(am, "request", stub_request([turn(0)])):
            am.fetch_session({}, SID)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_only_the_dropped_fields_are_cached(self):
        with mock.patch.object(am, "request", stub_request([{**turn(0), "tokens": 99, "id": "drop me"}])):
            am.fetch_session({}, SID)
        self.assertEqual(sorted(self.lines()[0]), sorted(am.FETCH_FIELDS))


class ShowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        self.enterContext(mock.patch.object(am, "CACHE_DIR", self.cache))
        self.enterContext(mock.patch.object(am, "load_config", lambda: {"server": "http://x", "web": "http://x", "token": "t"}))
        self.enterContext(mock.patch.object(am, "child_sessions", lambda cfg, sid: []))

    def show(self, turns, *argv):
        args = am.build_parser().parse_args(["show", SID, *argv])
        out = io.StringIO()
        with mock.patch.object(am, "request", stub_request(turns)), redirect_stdout(out):
            args.fn(args)
        return out.getvalue().splitlines()

    def test_default_window_is_the_last_turns_with_a_header(self):
        lines = self.show([turn(i) for i in range(40)])
        self.assertTrue(lines[0].startswith("# 11111111 A r "))
        self.assertEqual(lines[2], f"# 40 turns, showing 10..39 · rg {self.cache / f'{SID}.jsonl'} for the rest")
        self.assertEqual([l.split()[0] for l in lines if l.startswith("[")][0], "[10]")

    def test_role_filter_keeps_the_short_default_cap(self):
        long = "x" * 3000
        lines = self.show([turn(0, text=long)], "--role", "user")
        self.assertIn(f"[+{3000 - am.SHOW_DEFAULT_CHARS} chars]", "\n".join(lines))
        lines = self.show([turn(0, text=long)], "--role", "user", "--last", "5")
        self.assertIn(f"[+{3000 - am.SHOW_MAX_CHARS} chars]", "\n".join(lines))

    def test_full_session_prints_no_window_header(self):
        lines = self.show([turn(i) for i in range(3)], "--all")
        self.assertEqual(lines[1], "# t")
        self.assertFalse(any("showing" in l for l in lines))

    def test_end_past_the_last_turn_clamps(self):
        lines = self.show([turn(i) for i in range(5)], "--start", "3", "--end", "900")
        self.assertIn("showing 3..4", lines[2])
        self.assertEqual([l.split()[0] for l in lines if l.startswith("[")], ["[3]", "[4]"])

    def test_start_past_the_last_turn_prints_only_the_header(self):
        lines = self.show([turn(i) for i in range(5)], "--start", "900")
        self.assertIn("nothing to show past seq 900", lines[2])
        self.assertEqual([l for l in lines if l.startswith("[")], [])

    def test_empty_session_does_not_crash(self):
        lines = self.show([])
        self.assertIn("nothing to show past seq 0", lines[2])


if __name__ == "__main__":
    unittest.main()
