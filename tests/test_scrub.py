import json
import re
import unittest
from pathlib import Path

import hive_mind as am

FIXTURE = Path(__file__).parent / "fixtures" / "claude_session.jsonl"
SECRETS = [
    "AKIAIOSFODNN7EXAMPLE",
    "sk-abcdefghijklmnopqrstuvwxyz123456",
    "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij12",
    "ghp_ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ99",
    "xoxb-1234567890-ABCDEFGHIJKL",
    "xoxp-9876543210-ZYXWVUTSRQPO",
    "AIzaSyA1234567890abcdefghijklmnopqrstuv",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0",
    "hunter2hunter",
    "Pa55w0rd",
    "MIIEowIBAAKCAQEA7",
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCyEXAMPLEKEY",
    "athmind_0123456789abcdef0123456789abcdef01234567",
    "Zq9vK2mXp7Lr4TnB8wYc3JdF6gHs1AeU5oIiR0kNbVtQxWzM",
    "supersecret123",
    "shouldnotappear1",
    "~/.aws/credentials",
]
KEEP = ["3f2a9c1e8b7d6f5a4c3b2a1e0d9c8b7a6f5e4d3c", "11111111-2222-4333-8444-555555555555", "README.md"]


class ScrubTest(unittest.TestCase):
    def setUp(self):
        self.patterns = am.load_patterns()

    def test_fixture_messages_carry_no_secret(self):
        meta = {}
        msgs = am.parse_claude(FIXTURE.read_text().splitlines(), meta)
        body = json.dumps([am.scrub(m["text"], self.patterns) for m in msgs] + [am.scrub(meta["title"], self.patterns)])
        for s in SECRETS:
            self.assertNotIn(s, body, s)
        for k in KEEP:
            self.assertIn(k, body, k)
        self.assertIn("Bash [redacted]", body)
        self.assertIn("[REDACTED:private-key]", body)
        self.assertIn("[REDACTED:high-entropy]", body)

    def test_kinds_and_shapes(self):
        cases = {
            "aws-access-key": "key AKIAIOSFODNN7EXAMPLE here",
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.sig",
            "basic-auth-url": "https://u:p@h.example/x",
            "generic-credential": 'export GITHUB_TOKEN="abcdef123456"',
            "athmind-token": "athmind_0123456789abcdef0123456789abcdef01234567",
        }
        for kind, text in cases.items():
            self.assertIn(f"[REDACTED:{kind}]", am.scrub(text, self.patterns), kind)

    def test_entropy_keeps_hex_and_uuid(self):
        text = "sha 3f2a9c1e8b7d6f5a4c3b2a1e0d9c8b7a6f5e4d3c uuid 11111111-2222-4333-8444-555555555555 path /home/dev/alvicom/Athene-AI/gateway/src/gateway/api/routers/agent_history.py"
        self.assertEqual(am.scrub(text, self.patterns), text)

    def test_custom_ignore_pattern(self):
        patterns = self.patterns + [("custom", re.compile(r"ACME-\d{4}"))]
        self.assertEqual(am.scrub("ticket ACME-1234 done", patterns), "ticket [REDACTED:custom] done")

    def test_tool_call_redaction_list(self):
        self.assertEqual(am.tool_call_text("Bash", {"command": "printenv | sort"}), "Bash [redacted]")
        self.assertEqual(am.tool_call_text("Read", {"file_path": "/etc/kubeconfig"}), "Read [redacted]")
        self.assertEqual(am.tool_call_text("Bash", {"command": "git status"}), 'Bash {"command":"git status"}')
        self.assertLessEqual(len(am.tool_call_text("Bash", {"command": "x" * 500})), len("Bash ") + am.TOOL_INPUT_CHARS)


if __name__ == "__main__":
    unittest.main()
