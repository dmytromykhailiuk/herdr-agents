import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

from _base import LIB, IsolatedTestCase

sys.path.insert(0, str(LIB))

from herdragent import results  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "transcripts"


def fixture_entries(name):
    return [json.loads(line) for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines() if line.strip()]


class StopHookTest(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.agent_dir = self.agent_root / "worker-abc123"
        self.agent_dir.mkdir(parents=True)
        self.hook_env = self.make_env(HERDR_AGENT_DIR=str(self.agent_dir))

    def test_writes_result_files(self):
        payload = {"session_id": "sid-1", "last_assistant_message": "the answer", "transcript_path": "/nope.jsonl"}
        r = self.run_hook("stop", payload, env=self.hook_env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")
        self.assertEqual((self.agent_dir / "result.md").read_text(encoding="utf-8"), "the answer")
        rec = json.loads((self.agent_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["message"], "the answer")
        self.assertEqual(rec["source"], "hook")
        self.assertEqual(rec["session_id"], "sid-1")
        self.assertEqual(rec["transcript_path"], "/nope.jsonl")
        self.assertEqual(rec["background_tasks"], [])
        self.assertIsInstance(rec["written_at_ns"], int)
        turns = [json.loads(x) for x in (self.agent_dir / "turns.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["chars"], len("the answer"))
        self.assertNotIn("message", turns[0])
        self.assertEqual([p.name for p in self.agent_dir.iterdir() if p.name.startswith(".")], [])

    def test_appends_turns(self):
        for text in ("one", "two"):
            self.run_hook("stop", {"session_id": "s", "last_assistant_message": text}, env=self.hook_env)
        self.assertEqual((self.agent_dir / "result.md").read_text(encoding="utf-8"), "two")
        self.assertEqual(len((self.agent_dir / "turns.jsonl").read_text(encoding="utf-8").splitlines()), 2)

    def test_transcript_fallback_when_no_message(self):
        transcript = self.root / "t.jsonl"
        shutil.copy(FIXTURES / "normal.jsonl", transcript)
        r = self.run_hook("stop", {"session_id": "s", "transcript_path": str(transcript)}, env=self.hook_env)
        self.assertEqual(r.returncode, 0)
        rec = json.loads((self.agent_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["source"], "transcript")
        self.assertEqual(rec["message"], "Final report: all good.")

    def test_no_agent_dir_is_noop(self):
        r = self.run_hook("stop", {"last_assistant_message": "x"})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertFalse((self.agent_dir / "result.md").exists())

    def test_garbage_stdin_never_fails(self):
        for data in ("not json", "[1, 2]", ""):
            r = self.run_hook("stop", data, env=self.hook_env)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.agent_dir / "result.md").read_text(encoding="utf-8"), "")

    def test_errors_logged_not_raised(self):
        (self.agent_dir / "result.md").mkdir()
        r = self.run_hook("stop", {"last_assistant_message": "x"}, env=self.hook_env)
        self.assertEqual(r.returncode, 0)
        self.assertTrue((self.agent_dir / "hook-errors.log").read_text(encoding="utf-8").strip())


class TranscriptTest(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def put_transcript(self, config_dir, sid, fixture="normal.jsonl"):
        d = Path(config_dir) / "projects" / "-some-project"
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / fixture, d / f"{sid}.jsonl")
        return d / f"{sid}.jsonl"

    def test_fallback_uses_meta_claude_config_dir(self):
        profile = self.root / "profile-dir"
        self.put_transcript(profile, "sid-p")
        meta = {"id": "x-000000", "session_id": "sid-p", "claude_config_dir": str(profile)}
        self.assertEqual(results.transcript_last(meta, 0), ("Final report: all good.", False))
        self.assertEqual(results.transcript_last(dict(meta, claude_config_dir=""), 0), ("", None))

    def test_default_config_dir_is_home_claude(self):
        self.put_transcript(self.home / ".claude", "sid-h")
        meta = {"id": "x-000000", "session_id": "sid-h", "claude_config_dir": ""}
        self.assertEqual(results.transcript_last(meta, 0)[0], "Final report: all good.")

    def test_result_json_transcript_path_first(self):
        path = self.put_transcript(self.root / "elsewhere", "sid-r", "api-error.jsonl")
        d = self.agent_root / "x-000000"
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps({"session_id": "sid-r", "transcript_path": str(path)}))
        meta = {"id": "x-000000", "session_id": "sid-r", "claude_config_dir": str(self.root / "missing")}
        text, marked = results.transcript_last(meta, 0)
        self.assertTrue(text.startswith("API Error: 529"))
        self.assertTrue(marked)

    def test_since_filter(self):
        profile = self.root / "p"
        self.put_transcript(profile, "sid-s")
        meta = {"id": "x-000000", "session_id": "sid-s", "claude_config_dir": str(profile)}
        self.assertEqual(results.transcript_last(meta, 4102444800 * 10**9), ("", None))

    def test_api_error_detection(self):
        normal, api, synthetic = (fixture_entries(n)[-1] for n in
                                  ("normal.jsonl", "api-error.jsonl", "synthetic-non-error.jsonl"))
        self.assertFalse(results.is_api_error_entry(normal))
        self.assertTrue(results.is_api_error_entry(api))
        self.assertFalse(results.is_api_error_entry(synthetic))
        unmarked = {"message": {"model": "<synthetic>", "content": "API Error: Connection error."}}
        self.assertTrue(results.is_api_error_entry(unmarked))

    def test_api_error_check(self):
        profile = self.root / "p"
        self.put_transcript(profile, "sid-a", "api-error.jsonl")
        self.put_transcript(profile, "sid-n", "synthetic-non-error.jsonl")
        meta = {"id": "x-000000", "claude_config_dir": str(profile)}
        self.assertTrue(results.api_error_check(dict(meta, session_id="sid-a"), 0, ""))
        self.assertFalse(results.api_error_check(dict(meta, session_id="sid-n"), 0, "API Error: looks like one"))
        self.assertTrue(results.api_error_check(dict(meta, session_id="none"), 0, "  API Error: 500"))
        self.assertFalse(results.api_error_check(dict(meta, session_id="none"), 0, "fine"))

    def test_api_error_hint_uses_config_dir(self):
        meta = {"id": "w-1", "session_id": "sid", "cwd": "/a b", "claude_config_dir": "/p/.claude-x"}
        hint = results.api_error_hint(meta)
        self.assertIn("cd '/a b' && CLAUDE_CONFIG_DIR=/p/.claude-x claude --resume sid", hint)
        self.assertIn("&& claude --resume sid", results.api_error_hint(dict(meta, claude_config_dir="")))


if __name__ == "__main__":
    unittest.main()
