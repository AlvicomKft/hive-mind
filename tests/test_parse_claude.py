import json
import unittest
from pathlib import Path

import hive_mind as am

FIXTURE = Path(__file__).parent / "fixtures" / "claude_session.jsonl"


class ParseClaudeTest(unittest.TestCase):
    def setUp(self):
        self.meta = {}
        self.msgs = am.parse_claude(FIXTURE.read_text().splitlines(), self.meta)

    def test_roles_and_order(self):
        self.assertEqual(
            [(m["role"], m["toolName"]) for m in self.msgs],
            [("user", None), ("assistant", None), ("tool_call", "Bash"), ("tool_call", "Read"), ("assistant", None), ("user", None), ("assistant", None)],
        )

    def test_branch_marks_changes_only(self):
        self.assertEqual([m.get("branch") for m in self.msgs], ["main"] + [None] * 6)

    def test_branch_change_mid_session(self):
        def rec(seq, branch):
            body = {"type": "user", "timestamp": f"2026-09-03T10:00:0{seq}.000Z", "message": {"role": "user", "content": f"m{seq}"}}
            if branch:
                body["gitBranch"] = branch
            return json.dumps(body)

        meta = {}
        first = am.parse_claude([rec(0, "main"), rec(1, "main")], meta)
        second = am.parse_claude([rec(2, "feat/x"), rec(3, "feat/x"), rec(4, None)], meta)
        self.assertEqual([m.get("branch") for m in first + second], ["main", None, "feat/x", None, None])
        self.assertIsNone(meta["lastBranch"])

    def test_dropped_content(self):
        body = "\n".join(m["text"] for m in self.msgs)
        self.assertNotIn("secretly", body)  # thinking
        self.assertNotIn("# readme", body)  # tool_result
        self.assertNotIn("sidechain prompt", body)
        self.assertNotIn("system note", body)
        self.assertNotIn("Old summary", body)

    def test_meta(self):
        self.assertEqual(self.meta["startedAt"], "2026-09-03T10:00:00.000Z")
        self.assertTrue(self.meta["title"].startswith("Set up deploy."))
        self.assertLessEqual(len(self.meta["title"]), 240)
        self.assertEqual((self.meta["inputTokens"], self.meta["outputTokens"]), (1000, 100))
        self.assertIsNone(self.meta.get("parentSessionId"))

    def test_tokens_dedupe_across_chunks(self):
        lines = FIXTURE.read_text().splitlines()
        meta = {}
        am.parse_claude(lines[:5], meta)
        am.parse_claude(lines[5:], meta)
        self.assertEqual((meta["inputTokens"], meta["outputTokens"]), (1000, 100))

    def test_per_message_usage_and_cache_totals(self):
        def rec(msg_id, model, usage, text):
            return json.dumps({"type": "assistant", "timestamp": "2026-09-03T10:00:05.000Z",
                               "message": {"id": msg_id, "role": "assistant", "model": model,
                                           "content": [{"type": "text", "text": text}], "usage": usage}})
        u1 = {"input_tokens": 5, "output_tokens": 50, "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 700}
        u2 = {"input_tokens": 3, "output_tokens": 30, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 20}
        meta = {}
        # The middle record repeats message id m1: one streamed message, one usage row.
        msgs = am.parse_claude([rec("m1", "claude-opus-5", u1, "a"), rec("m1", "claude-opus-5", u1, "b"), rec("m2", "claude-fable-5-1", u2, "c")], meta)
        self.assertEqual([m.get("usage") for m in msgs], [
            {"model": "claude-opus-5", "input": 5, "output": 50, "cacheRead": 9000, "cacheCreation": 700},
            None,
            {"model": "claude-fable-5-1", "input": 3, "output": 30, "cacheRead": 1000, "cacheCreation": 20},
        ])
        self.assertEqual((meta["inputTokens"], meta["outputTokens"], meta["cacheReadTokens"], meta["cacheCreationTokens"]), (8, 80, 10000, 720))

    def test_synthetic_turns_carry_no_usage(self):
        line = json.dumps({"type": "assistant", "timestamp": "2026-09-03T10:00:05.000Z",
                           "message": {"id": "m3", "role": "assistant", "model": "<synthetic>",
                                       "content": [{"type": "text", "text": "No response requested."}],
                                       "usage": {"input_tokens": 0, "output_tokens": 0}}})
        meta = {}
        msgs = am.parse_claude([line], meta)
        self.assertEqual([m.get("usage") for m in msgs], [None])
        self.assertNotIn("models", meta)

    def test_codex_cache_tokens_are_split_out_of_input(self):
        line = '{"timestamp":"2026-08-29T15:26:50.608Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":1000,"cached_input_tokens":900,"cache_write_input_tokens":40,"output_tokens":7}}}}'
        meta = {}
        am.parse_codex([line], meta)
        self.assertEqual((meta["inputTokens"], meta["outputTokens"], meta["cacheReadTokens"], meta["cacheCreationTokens"]), (100, 7, 900, 40))

    def test_agent_result_kept_as_tool_call(self):
        notification = "<task-notification>\n<task-id>abc</task-id>\n<status>completed</status>\n<result>All green. 3 files changed.</result>\n<usage><tool_uses>4</tool_uses></usage>\n</task-notification>"
        line = json.dumps({"type": "user", "isSidechain": False, "timestamp": "2026-09-03T12:00:00.000Z", "message": {"role": "user", "content": notification}})
        msgs = am.parse_claude([line], {})
        self.assertEqual([(m["role"], m["toolName"], m["text"]) for m in msgs], [("tool_call", "Agent result", "All green. 3 files changed.")])

    def test_agent_result_is_capped(self):
        long = "x" * (am.AGENT_RESULT_CHARS + 50)
        line = json.dumps({"type": "user", "isSidechain": False, "timestamp": "2026-09-03T12:00:00.000Z", "message": {"role": "user", "content": f"<task-notification><result>{long}</result></task-notification>"}})
        text = am.parse_claude([line], {})[0]["text"]
        self.assertTrue(text.endswith("… [+50 chars]"))
        self.assertEqual(len(text), am.AGENT_RESULT_CHARS + len("… [+50 chars]"))

    def test_sidechain_records_parsed_only_when_asked(self):
        line = json.dumps({"type": "user", "isSidechain": True, "timestamp": "2026-09-03T12:00:00.000Z", "message": {"role": "user", "content": "child prompt"}})
        self.assertEqual(am.parse_claude([line], {}), [])
        meta = {}
        self.assertEqual([m["text"] for m in am.parse_claude([line], meta, True)], ["child prompt"])
        self.assertEqual(meta["title"], "child prompt")

    def test_agent_spawn_keeps_description(self):
        line = json.dumps({"type": "assistant", "isSidechain": False, "timestamp": "2026-09-03T12:00:00.000Z", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Agent", "input": {"description": "Rotate the token", "subagent_type": "general-purpose", "prompt": "read .env and rotate the secret"}}]}})
        msg = am.parse_claude([line], {})[0]
        self.assertEqual((msg["toolName"], msg["text"]), ("Agent", "Agent Rotate the token"))
        rendered = am.format_message(msg | {"seq": 4}, 200, {"Rotate the token": "abcdef1234"})
        self.assertTrue(rendered.startswith("[4] agent "), rendered)
        self.assertTrue(rendered.endswith("Rotate the token → abcdef12"), rendered)

    def test_normalize_remote(self):
        for url in [
            "git@github.com:Alvicom/Athene-AI.git",
            "https://GitHub.com/Alvicom/Athene-AI.git",
            "https://user:pass@github.com/Alvicom/Athene-AI",
            "ssh://git@github.com:22/Alvicom/Athene-AI.git",
            "ssh://git@github.com/Alvicom/Athene-AI/",
        ]:
            self.assertEqual(am.normalize_remote(url), "github.com/Alvicom/Athene-AI", url)

    def test_parse_codex_shape(self):
        lines = [
            '{"timestamp":"2026-08-29T15:26:45.210Z","type":"session_meta","payload":{"id":"x","timestamp":"2026-08-29T15:26:38.191Z","cwd":"/w"}}',
            '{"timestamp":"2026-08-29T15:26:45.789Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"<environment_context>x</environment_context>"}]}}',
            '{"timestamp":"2026-08-29T15:26:45.901Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"clone repo twice"}]}}',
            '{"timestamp":"2026-08-29T15:26:47.986Z","type":"response_item","payload":{"type":"reasoning","summary":[]}}',
            '{"timestamp":"2026-08-29T15:26:48.815Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Inspecting."}]}}',
            '{"timestamp":"2026-08-29T15:26:50.462Z","type":"response_item","payload":{"type":"custom_tool_call","name":"exec","input":"pwd && ls"}}',
            '{"timestamp":"2026-08-29T15:26:50.606Z","type":"response_item","payload":{"type":"custom_tool_call_output","output":"/w"}}',
            '{"timestamp":"2026-08-29T15:26:50.608Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":84493,"output_tokens":783}}}}',
        ]
        meta = {}
        msgs = am.parse_codex(lines, meta)
        self.assertEqual([(m["role"], m["text"]) for m in msgs], [("user", "clone repo twice"), ("assistant", "Inspecting."), ("tool_call", "exec pwd && ls")])
        self.assertEqual(meta["startedAt"], "2026-08-29T15:26:38.191Z")
        self.assertEqual(meta["title"], "clone repo twice")
        self.assertEqual((meta["inputTokens"], meta["outputTokens"]), (84493, 783))


class HarnessNoiseTest(unittest.TestCase):
    def test_local_command_and_system_reminder_lines_are_dropped(self):
        lines = [
            json.dumps({"type": "user", "timestamp": "2026-09-03T10:00:00Z", "message": {"role": "user", "content": "<local-command-caveat>Caveat: generated by the user</local-command-caveat>"}}),
            json.dumps({"type": "user", "timestamp": "2026-09-03T10:00:01Z", "message": {"role": "user", "content": [{"type": "text", "text": "<command-name>/model</command-name>\n<command-message>model</command-message>"}]}}),
            json.dumps({"type": "user", "timestamp": "2026-09-03T10:00:02Z", "message": {"role": "user", "content": "<local-command-stdout>Set model</local-command-stdout>"}}),
            json.dumps({"type": "user", "timestamp": "2026-09-03T10:00:03Z", "message": {"role": "user", "content": "<system-reminder>ignore me</system-reminder>real question"}}),
        ]
        meta = {}
        out = am.parse_claude(lines, meta)
        self.assertEqual([m["text"] for m in out], ["real question"])
        self.assertEqual(meta["title"], "real question")

    def test_skill_bodies_and_subagent_notices_are_dropped(self):
        lines = [
            json.dumps({"type": "user", "timestamp": "2026-09-03T10:00:00Z", "message": {"role": "user", "content": "Base directory for this skill: /home/x/skills/foo\nUse absolute paths."}}),
            json.dumps({"type": "user", "timestamp": "2026-09-03T10:00:01Z", "message": {"role": "user", "content": "<task-notification>Agent bar finished</task-notification>"}}),
            json.dumps({"type": "user", "timestamp": "2026-09-03T10:00:02Z", "message": {"role": "user", "content": "[SYSTEM NOTIFICATION - NOT USER INPUT] Agent baz finished"}}),
            json.dumps({"type": "user", "timestamp": "2026-09-03T10:00:03Z", "message": {"role": "user", "content": "keep this<task-notification>Agent qux finished</task-notification>"}}),
        ]
        out = am.parse_claude(lines, {})
        self.assertEqual([m["text"] for m in out], ["keep this"])

    def test_compact_summary_is_kept_and_tagged_but_not_the_title(self):
        lines = [
            json.dumps({"type": "user", "timestamp": "2026-09-03T10:00:00Z", "isCompactSummary": True, "message": {"role": "user", "content": "This session is being continued from a previous conversation. We fixed the hook."}}),
            json.dumps({"type": "user", "timestamp": "2026-09-03T10:00:01Z", "message": {"role": "user", "content": "now do the CLI"}}),
        ]
        meta = {}
        out = am.parse_claude(lines, meta)
        self.assertEqual([m["toolName"] for m in out], [am.COMPACT_SUMMARY, None])
        self.assertEqual(meta["title"], "now do the CLI")

    def test_models_are_collected_from_assistant_records(self):
        lines = [
            json.dumps({"type": "assistant", "timestamp": "2026-09-03T10:00:00Z", "message": {"role": "assistant", "id": "a", "model": "claude-fable-5-1", "content": [{"type": "text", "text": "hi"}]}}),
            json.dumps({"type": "assistant", "timestamp": "2026-09-03T10:00:01Z", "message": {"role": "assistant", "id": "b", "model": "claude-haiku-4-5", "content": [{"type": "text", "text": "yo"}]}}),
            json.dumps({"type": "assistant", "timestamp": "2026-09-03T10:00:02Z", "message": {"role": "assistant", "id": "c", "model": "claude-fable-5-1", "content": [{"type": "text", "text": "again"}]}}),
            json.dumps({"type": "assistant", "timestamp": "2026-09-03T10:00:03Z", "message": {"role": "assistant", "id": "d", "model": "<synthetic>", "content": [{"type": "text", "text": "synthetic"}]}}),
        ]
        meta = {}
        am.parse_claude(lines, meta)
        self.assertEqual(meta["models"], ["claude-fable-5-1", "claude-haiku-4-5"])


class FormatTest(unittest.TestCase):
    def test_model_label(self):
        self.assertEqual(am.model_label([]), "-")
        self.assertEqual(am.model_label(["claude-fable-5-1"]), "fable-5-1")
        self.assertEqual(am.model_label(["claude-fable-5-1", "gpt-5.4"]), "fable-5-1+1")

    def test_show_marks_truncation(self):
        msg = {"seq": 3, "role": "user", "toolName": None, "text": "x" * 30, "ts": "2026-09-03T10:00:00Z"}
        self.assertIn("[+20 chars]", am.format_message(msg, 10))
        tool = {"seq": 4, "role": "tool_call", "toolName": "Bash", "text": "Bash ls -la", "ts": "2026-09-03T10:00:00Z"}
        self.assertTrue(am.format_message(tool, 200).startswith("[4] tool  "))

    def test_session_label_is_the_title(self):
        self.assertEqual(am.session_label({"title": "t", "summary": "s"}, 50), "t")
        self.assertEqual(am.session_label({"title": "t", "parentSessionId": "abcdef0123"}, 50), "↳abcdef01 t")

    def test_first_sentence_cuts_at_the_first_stop(self):
        self.assertEqual(am.first_sentence("Done. And more.", 160), "Done.")
        self.assertEqual(am.first_sentence("no stop here", 160), "no stop here")
        self.assertEqual(am.first_sentence("word " * 100, 20), "word word word word…")

    def test_hit_role_labels_compact_summaries(self):
        self.assertEqual(am.hit_role({"role": "assistant", "toolName": None}), "assistant")
        self.assertEqual(am.hit_role({"role": "user", "toolName": am.COMPACT_SUMMARY}), "[summary]")

    def test_show_labels_compact_summary(self):
        msg = {"seq": 7, "role": "user", "toolName": am.COMPACT_SUMMARY, "text": "s", "ts": "2026-09-03T10:00:00Z"}
        self.assertTrue(am.format_message(msg, 200).startswith("[7] summary  "))


if __name__ == "__main__":
    unittest.main()


class ShellInputTest(unittest.TestCase):
    def test_bang_command_becomes_tool_row_not_title(self):
        lines = [
            json.dumps({"type": "user", "timestamp": "2026-09-03T10:00:00Z", "message": {"role": "user", "content": "<bash-input>ls -la</bash-input>"}}),
            json.dumps({"type": "user", "timestamp": "2026-09-03T10:00:01Z", "message": {"role": "user", "content": "<bash-stdout>total 0</bash-stdout><bash-stderr></bash-stderr>"}}),
            json.dumps({"type": "user", "timestamp": "2026-09-03T10:00:02Z", "message": {"role": "user", "content": "now fix the bug"}}),
        ]
        meta = {}
        out = am.parse_claude(lines, meta)
        self.assertEqual([(m["role"], m["text"]) for m in out], [("tool_call", "Bash ls -la"), ("user", "now fix the bug")])
        self.assertEqual(meta["title"], "now fix the bug")
