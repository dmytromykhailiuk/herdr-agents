import json
import os
import sys
import time
import unittest
from unittest import mock

from _base import LIB, IsolatedTestCase
from test_cli import CliTestCase

sys.path.insert(0, str(LIB))

from herdragent import spawn  # noqa: E402

RULE = "─" * 60


def enter_calls(calls):
    return [c for c in calls if c[:2] == ["agent", "send-keys"] and "enter" in c]


class ScreenParsingTest(unittest.TestCase):
    def test_box_between_two_rules(self):
        snap = f"old\n{RULE}\n❯ hello world\n{RULE}\n  status"
        self.assertEqual(spawn.input_box(snap), "❯ hello world")

    def test_only_bottom_rule_visible(self):
        snap = "tail of a tall prompt\nlast line here\n" + RULE + "\n  status"
        self.assertEqual(spawn.input_box(snap), "tail of a tall prompt\nlast line here")

    def test_no_rule(self):
        self.assertEqual(spawn.input_box("nothing"), "")

    def test_pending_matches_last_line_not_first(self):
        prompt = "first line\n" + "middle\n" * 50 + "the very last line of it\n"
        box = "...\nthe very last line of it"
        self.assertTrue(spawn.prompt_pending(box, prompt))
        self.assertFalse(spawn.prompt_pending("first line", prompt))

    def test_pending_wrapped_and_pasted(self):
        self.assertTrue(spawn.prompt_pending("❯ [Pasted text #1 +40 lines]", "x"))
        self.assertTrue(spawn.prompt_pending("the very last\n line of it", "a\nthe very last line of it"))

    def test_pending_soft_wrapped_mid_word(self):
        prompt = "Please review the change in src/module/file.py and report findings"
        box = "❯ Please review the change in src/module/fi\n  le.py and report findi\n  ngs"
        self.assertTrue(spawn.prompt_pending(box, prompt))
        self.assertFalse(spawn.prompt_pending("❯ something else entirely", prompt))

    def test_empty_box_is_not_pending(self):
        self.assertFalse(spawn.prompt_pending("  ❯ ", "something"))


class TranscriptTest(IsolatedTestCase):
    def write(self, sid, entries):
        d = self.claude_dir / "projects" / "-x"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    def meta(self):
        return {"session_id": "sid-1", "claude_config_dir": str(self.claude_dir)}

    def test_user_entry_after_prompt(self):
        since = time.time_ns() - 10**9
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        self.write("sid-1", [{"type": "user", "timestamp": ts, "message": {}}])
        self.assertTrue(spawn.transcript_has_user_turn(self.meta(), since - 5 * 10**9))

    def test_older_user_entry_ignored(self):
        self.write("sid-1", [{"type": "user", "timestamp": "2020-01-01T00:00:00.000Z"}])
        self.assertFalse(spawn.transcript_has_user_turn(self.meta(), time.time_ns()))

    def test_meta_and_assistant_entries_ignored(self):
        self.write("sid-1", [{"type": "user", "isMeta": True}, {"type": "assistant"}])
        self.assertFalse(spawn.transcript_has_user_turn(self.meta(), 0))

    def test_missing_transcript(self):
        self.assertFalse(spawn.transcript_has_user_turn(self.meta(), 0))


class StuckPromptTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.henv = self.herdr_env(HERDR_AGENT_SUBMIT_DELAYS="0.6,0.6,0.6")

    def pane(self):
        return next(iter(self.fake_panes().values()))

    def test_short_prompt_gets_one_enter(self):
        self.fake_set(prompt_stuck=1)
        self.spawn(prompt="short prompt")
        self.assertEqual(len(enter_calls(self.fake_calls())), 1)
        self.assertEqual(self.pane()["prompts"], 1)
        self.assertNotIn("pending_prompt", self.pane())

    def test_tall_prompt_beyond_visible_rows_gets_enter(self):
        self.fake_set(prompt_stuck=1, screen_rows=40)
        prompt = "TOP LINE\n" + "\n".join(f"body {i}" for i in range(150)) + "\nBOTTOM LINE"
        self.spawn(prompt=prompt)
        self.assertEqual(len(enter_calls(self.fake_calls())), 1)
        self.assertNotIn("pending_prompt", self.pane())

    def test_second_enter_when_one_is_not_enough(self):
        self.fake_set(prompt_stuck=2)
        self.spawn(prompt="needs two")
        self.assertEqual(len(enter_calls(self.fake_calls())), 2)
        self.assertNotIn("pending_prompt", self.pane())

    def test_submitted_prompt_never_gets_enter(self):
        self.spawn(prompt="fine")
        self.assertEqual(enter_calls(self.fake_calls()), [])

    def test_follow_up_send_gets_enter(self):
        aid = self.spawn(prompt="first")
        self.fake_set(prompt_stuck=1)
        r = self.run_cli("send", aid, "--prompt", "second", env=self.henv)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(enter_calls(self.fake_calls())), 1)
        self.assertEqual(self.pane()["prompts"], 2)


class GuardTest(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, self.herdr_env(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stuck_pane(self, status):
        self.fake_add_pane(
            pane_id="w1:p9", agent_status=status, agent="claude", agent_name="a-1",
            process_argv=["claude"], pending_prompt="hello there", stuck_left=1,
            agent_session={"agent": "claude", "kind": "id", "source": "x", "value": "sid"},
        )
        self.fake_set(prompt_stuck=1)

    def run_submit(self):
        meta = {"id": "a-1", "pane_id": "w1:p9", "session_id": "sid", "last_prompt_at_ns": time.time_ns(),
                "claude_config_dir": str(self.claude_dir)}
        spawn.ensure_submitted(meta, "hello there", delays=(0.6, 0.6))

    def test_blocked_agent_never_gets_enter(self):
        self.stuck_pane("blocked")
        self.run_submit()
        self.assertEqual(enter_calls(self.fake_calls()), [])

    def test_working_agent_never_gets_enter(self):
        self.stuck_pane("working")
        self.run_submit()
        self.assertEqual(enter_calls(self.fake_calls()), [])

    def test_idle_agent_with_pending_prompt_gets_enter(self):
        self.stuck_pane("idle")
        self.run_submit()
        self.assertEqual(len(enter_calls(self.fake_calls())), 1)

    def test_idle_agent_with_empty_box_gets_no_enter(self):
        self.fake_add_pane(pane_id="w1:p9", agent_status="idle", agent="claude", agent_name="a-1",
                           process_argv=["claude"], screen=f"{RULE}\n❯ \n{RULE}\n")
        self.run_submit()
        self.assertEqual(enter_calls(self.fake_calls()), [])


class FakeIdsTest(CliTestCase):
    def test_new_ids_skip_seeded_panes(self):
        self.fake_add_pane(pane_id="w1:p1", tab_id="w1:t1")
        self.fake_add_pane(pane_id="w1:p2", tab_id="w1:t2")
        self.fake_add_pane(pane_id="w1:p3", tab_id="w1:t3")
        before = set(self.fake_panes())
        r = self.run_cli("spawn", "w", "--prompt", "x", env=self.henv)
        self.assertEqual(r.returncode, 0, r.stderr)
        after = self.fake_panes()
        self.assertEqual(len(after), len(before) + 1)
        self.assertTrue(before <= set(after))


if __name__ == "__main__":
    unittest.main()
