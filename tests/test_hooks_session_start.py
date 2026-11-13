import itertools
import json
import os
import unittest

from _base import CLI, IsolatedTestCase


class SessionStartInjectTest(IsolatedTestCase):
    def env_for(self, herdr, depth, orchestrator, sdk, **extra):
        env = self.herdr_env() if herdr else self.make_env()
        env["HERDR_AGENTS_RECORD"] = "off"
        if depth:
            env["HERDR_AGENT_DEPTH"] = str(depth)
        if not orchestrator:
            env["CLAUDE_ORCHESTRATOR"] = "off"
        if sdk:
            env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-py"
        env.update(extra)
        return env

    def test_matrix(self):
        for herdr, depth, orch, sdk, agent_id, source in itertools.product(
                (False, True), (0, 1), (True, False), (False, True), (False, True), ("startup", "resume")):
            with self.subTest(herdr=herdr, depth=depth, orch=orch, sdk=sdk, agent_id=agent_id, source=source):
                payload = {"session_id": "s1", "source": source, "cwd": str(self.root)}
                if agent_id:
                    payload["agent_id"] = "a1"
                r = self.run_hook("session-start", payload, env=self.env_for(herdr, depth, orch, sdk))
                self.assertEqual(r.returncode, 0, r.stderr)
                expected = source == "startup" and depth == 0 and not sdk and not agent_id and (herdr or orch)
                if not expected:
                    self.assertEqual(r.stdout, "")
                    continue
                out = r.json()
                self.assertEqual(set(out), {"hookSpecificOutput"})
                hso = out["hookSpecificOutput"]
                self.assertEqual(set(hso), {"hookEventName", "additionalContext"})
                self.assertEqual(hso["hookEventName"], "SessionStart")
                text = hso["additionalContext"]
                self.assertEqual("# Herdr sub-agents" in text, herdr)
                self.assertNotIn("{{HERDR_AGENT}}", text)
                self.assertIn(str(CLI), text)

    def test_clear_and_compact_inject(self):
        for source in ("clear", "compact"):
            r = self.run_hook("session-start", {"session_id": "s1", "source": source},
                              env=self.env_for(True, 0, True, False))
            self.assertIn("additionalContext", r.json()["hookSpecificOutput"])

    def test_agent_type_blocks_injection(self):
        r = self.run_hook("session-start", {"session_id": "s1", "source": "startup", "agent_type": "Explore"},
                          env=self.env_for(True, 0, True, False))
        self.assertEqual(r.stdout, "")

    def test_master_switch_off(self):
        r = self.run_hook("session-start", {"session_id": "s1", "source": "startup"},
                          env=self.env_for(True, 0, True, False, HERDR_AGENTS="off"))
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_garbage_stdin(self):
        for data in ("not json", "[1,2]", "{"):
            r = self.run_hook("session-start", data, env=self.env_for(True, 0, True, False))
            self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_empty_stdin_still_injects(self):
        r = self.run_hook("session-start", "", env=self.env_for(False, 0, True, False))
        self.assertIn("additionalContext", r.json()["hookSpecificOutput"])


class SessionStartEnvFileTest(IsolatedTestCase):
    def test_export_written_once(self):
        env_file = self.root / "claude-env"
        env = self.herdr_env(CLAUDE_ENV_FILE=str(env_file), HERDR_AGENTS_RECORD="off")
        for _ in range(3):
            r = self.run_hook("session-start", {"session_id": "s1", "source": "startup"}, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
        lines = env_file.read_text().splitlines()
        self.assertEqual(lines, [f"export HERDR_AGENT={CLI}"])

    def test_no_export_outside_herdr(self):
        env_file = self.root / "claude-env"
        self.run_hook("session-start", {"session_id": "s1", "source": "startup"},
                      env=self.make_env(CLAUDE_ENV_FILE=str(env_file)))
        self.assertFalse(env_file.exists())

    def test_export_on_resume_and_subagent(self):
        env_file = self.root / "claude-env"
        env = self.herdr_env(CLAUDE_ENV_FILE=str(env_file), HERDR_AGENTS_RECORD="off", HERDR_AGENT_DEPTH="1")
        self.run_hook("session-start", {"session_id": "s1", "source": "resume"}, env=env)
        self.assertIn("export HERDR_AGENT=", env_file.read_text())


class SessionStartRecordTest(IsolatedTestCase):
    def launches(self):
        d = self.state_dir / "launches"
        if not d.is_dir():
            return {}
        return {p.stem: json.loads(p.read_text()) for p in d.glob("*.json")}

    def record(self, sid="s1", source="startup", **env_extra):
        env = self.herdr_env(**env_extra)
        r = self.run_hook("session-start", {"session_id": sid, "source": source, "cwd": str(self.root)}, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_records_from_process_info(self):
        self.fake_add_pane("w1:p1", process_argv=["claude", "--model", "opus", "--effort", "high", "fix it"])
        self.record()
        recs = self.launches()
        self.assertEqual(set(recs), {"s1"})
        rec = recs["s1"]
        self.assertEqual(rec["kind"], "session")
        self.assertEqual(rec["args"], ["--model", "opus", "--effort", "high"])
        self.assertEqual((rec["pane_id"], rec["workspace_id"], rec["tab_id"]), ("w1:p1", "w1", "w1:t1"))
        self.assertEqual(rec["cwd"], str(self.root))
        self.assertIn(["pane", "process-info", "--pane", "w1:p1"], self.fake_calls())
        self.assertEqual(self.fake_calls(mutating=True), [])

    def test_records_on_resume_source(self):
        self.fake_add_pane("w1:p1", process_argv=["/opt/bin/claude", "--resume", "s1", "--model", "sonnet"])
        self.record(source="resume")
        self.assertEqual(self.launches()["s1"]["args"], ["--model", "sonnet"])

    def test_not_needed_without_flags(self):
        self.fake_add_pane("w1:p1", process_argv=["claude"])
        self.record()
        self.assertEqual(self.launches(), {})

    def test_print_session_not_recorded(self):
        self.fake_add_pane("w1:p1", process_argv=["claude", "-p", "--model", "opus", "hi"])
        self.record()
        self.assertEqual(self.launches(), {})

    def test_supersede_by_pane(self):
        self.fake_add_pane("w1:p1", process_argv=["claude", "--model", "opus"])
        self.record("old")
        self.assertEqual(set(self.launches()), {"old"})
        self.record("new")
        self.assertEqual(set(self.launches()), {"new"})
        state = self.fake_read()
        state["panes"]["w1:p1"]["process_argv"] = ["claude"]
        self.fake_write(state)
        self.record("newest")
        self.assertEqual(self.launches(), {})

    def test_profile_config_dir_recorded(self):
        profile = self.home / ".claude-work"
        self.fake_add_pane("w1:p1", process_argv=["claude", "--model", "opus"])
        self.record(CLAUDE_CONFIG_DIR=str(profile))
        rec = self.launches()["s1"]
        self.assertEqual(rec["claude_config_dir"], str(profile))
        self.assertEqual(rec["env"].get("CLAUDE_CONFIG_DIR"), str(profile))

    def test_default_config_dir_not_carried(self):
        self.fake_add_pane("w1:p1", process_argv=["claude", "--model", "opus"])
        self.record()
        rec = self.launches()["s1"]
        self.assertFalse(rec["claude_config_dir"])
        self.assertNotIn("CLAUDE_CONFIG_DIR", rec["env"])

    def test_behaviour_env_only_session_recorded(self):
        self.fake_add_pane("w1:p1", process_argv=["claude"])
        self.record(CLAUDE_ORCHESTRATOR="off", HERDR_AGENTS_RESTORE_GRACE_S="5", UNRELATED="x")
        rec = self.launches()["s1"]
        want = {"CLAUDE_ORCHESTRATOR": "off", "HERDR_AGENTS_RESTORE_GRACE_S": "5"}
        self.assertEqual(rec["args"], [])
        self.assertIsNone(rec["marker"])
        self.assertEqual(rec["env"], want)
        self.assertEqual(rec["match_env"], want)

    def test_behaviour_env_carried_with_flags(self):
        self.fake_add_pane("w1:p1", process_argv=["claude", "--model", "opus"])
        self.record(HERDR_AGENT_MAX_DEPTH="3", HERDR_AGENT_AUTO_TRUST="1")
        rec = self.launches()["s1"]
        self.assertEqual(rec["env"], {"HERDR_AGENT_MAX_DEPTH": "3", "HERDR_AGENT_AUTO_TRUST": "1"})
        self.assertNotIn("match_env", rec)

    def test_skipped_for_agents_sdk_and_opt_out(self):
        self.fake_add_pane("w1:p1", process_argv=["claude", "--model", "opus"])
        for extra in ({"HERDR_AGENT_ID": "general-abc123"}, {"CLAUDE_CODE_ENTRYPOINT": "sdk-ts"},
                      {"HERDR_AGENTS_RECORD": "off"}, {"HERDR_AGENTS": "off"}):
            with self.subTest(extra=extra):
                self.record(**extra)
                self.assertEqual(self.launches(), {})
                self.assertNotIn(["pane", "process-info", "--pane", "w1:p1"], self.fake_calls())

    def test_not_recorded_outside_herdr(self):
        env = self.make_env(HERDR_PANE_ID="w1:p1", CLAUDE_ORCHESTRATOR="off")
        self.fake_add_pane("w1:p1", process_argv=["claude", "--model", "opus"])
        self.run_hook("session-start", {"session_id": "s1", "source": "startup"}, env=env)
        self.assertEqual(self.launches(), {})

    def test_herdr_failure_never_raises(self):
        env = self.herdr_env(CLAUDE_ORCHESTRATOR="off", HERDR_BIN_PATH=str(self.root / "missing-herdr"))
        r = self.run_hook("session-start", {"session_id": "s1", "source": "startup"}, env=env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("# Herdr sub-agents", r.json()["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("Traceback", r.stderr)


class SessionEndTest(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.fake_add_pane("w1:p1", process_argv=["claude", "--model", "opus"])
        env = self.herdr_env(CLAUDE_ORCHESTRATOR="off")
        r = self.run_hook("session-start", {"session_id": "s1", "source": "startup", "cwd": str(self.root)}, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.path = self.state_dir / "launches" / "s1.json"
        self.assertTrue(self.path.exists())

    def end(self, reason, sid="s1", **env_extra):
        r = self.run_hook("session-end", {"session_id": sid, "reason": reason, "hook_event_name": "SessionEnd"},
                          env=self.herdr_env(**env_extra))
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        return json.loads(self.path.read_text())

    def test_user_exit_marks_ended(self):
        for reason in ("prompt_input_exit", "logout"):
            with self.subTest(reason):
                rec = self.end(reason)
                self.assertEqual(rec["ended_reason"], reason)
                self.assertIn("ended_at", rec)

    def test_other_reasons_keep_record(self):
        for reason in ("other", "clear", "resume", None):
            with self.subTest(reason):
                self.assertNotIn("ended_at", self.end(reason))

    def test_resume_clears_ended(self):
        self.end("prompt_input_exit")
        env = self.herdr_env(CLAUDE_ORCHESTRATOR="off")
        self.run_hook("session-start", {"session_id": "s1", "source": "resume", "cwd": str(self.root)}, env=env)
        self.assertNotIn("ended_at", json.loads(self.path.read_text()))

    def test_agent_and_off_and_unknown_ignored(self):
        self.assertNotIn("ended_at", self.end("prompt_input_exit", HERDR_AGENT_ID="general-abc123"))
        self.assertNotIn("ended_at", self.end("prompt_input_exit", HERDR_AGENTS="off"))
        self.assertNotIn("ended_at", self.end("prompt_input_exit", sid="other"))


class SessionStartUnitTest(unittest.TestCase):
    def test_claude_args(self):
        import sys
        from _base import LIB
        sys.path.insert(0, str(LIB))
        from herdragent.hooks import session_start
        self.assertEqual(session_start.claude_args(["claude", "--model", "x"]), ["--model", "x"])
        self.assertEqual(session_start.claude_args(["/usr/local/bin/node", "/x/claude", "-c"]), ["-c"])
        self.assertIsNone(session_start.claude_args(["zsh"]))
        self.assertIsNone(session_start.claude_args([]))
        self.assertEqual(session_start.export_line("/a b/herdr-agent"), "export HERDR_AGENT='/a b/herdr-agent'\n")


if __name__ == "__main__":
    unittest.main()
