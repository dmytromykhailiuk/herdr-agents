import json
import os
import sys
import time
import unittest

from _base import IsolatedTestCase, LIB

sys.path.insert(0, str(LIB))

from herdragent import core, launches  # noqa: E402


class LaunchesTestCase(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        patches = {
            "HOME": str(self.home),
            "CLAUDE_CONFIG_DIR": str(self.claude_dir),
            "HERDR_AGENTS_STATE_DIR": str(self.state_dir),
            "HERDR_AGENT_ROOT": str(self.agent_root),
        }
        old = {k: os.environ.get(k) for k in patches}
        os.environ.update(patches)

        def restore():
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.addCleanup(restore)
        old_cli = core.CLI_PATH
        core.CLI_PATH = "/opt/herdr-agent/bin/herdr-agent"
        self.addCleanup(setattr, core, "CLI_PATH", old_cli)

    def meta(self, aid="explore-0615d7", sid="s-1", pane="w1:p2"):
        return {"id": aid, "session_id": sid, "cwd": "/work", "workspace_id": "w1",
                "pane_id": pane, "tab_id": "w1:t2", "claude_config_dir": str(self.claude_dir)}

    def make_transcript(self, sid, cfg=None):
        d = (cfg or self.claude_dir) / "projects" / "-work"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sid}.jsonl").write_text("{}\n")


class SanitizerTests(unittest.TestCase):
    def test_keeps_allowlisted_both_forms(self):
        out = launches.sanitize_args(["--model", "opus", "--effort=high", "--strict-mcp-config",
                                      "--dangerously-skip-permissions"])
        self.assertEqual(out, ["--model", "opus", "--effort=high", "--strict-mcp-config",
                               "--dangerously-skip-permissions"])

    def test_variadic(self):
        out = launches.sanitize_args(["--add-dir", "/a", "/b", "--model", "x",
                                      "--allowedTools", "Bash", "Read", "--mcp-config=/m.json"])
        self.assertEqual(out, ["--add-dir", "/a", "/b", "--model", "x",
                               "--allowedTools", "Bash", "Read", "--mcp-config=/m.json"])

    def test_drops_resume_continue_session_fork(self):
        out = launches.sanitize_args(["--resume", "abc", "--model", "m", "-c", "--session-id", "u",
                                      "--fork-session", "-r", "--name=n"])
        self.assertEqual(out, ["--model", "m", "--name=n"])

    def test_resume_without_value_before_flag(self):
        self.assertEqual(launches.sanitize_args(["--resume", "--model", "m"]), ["--model", "m"])

    def test_drops_positionals_and_unknown_flags(self):
        out = launches.sanitize_args(["claude", "--verbose", "--model", "m", "do the thing", "--unknown=1"])
        self.assertEqual(out, ["--model", "m"])

    def test_print_sessions_dropped_entirely(self):
        self.assertIsNone(launches.sanitize_args(["-p", "hi", "--model", "m"]))
        self.assertIsNone(launches.sanitize_args(["--model", "m", "--print"]))

    def test_double_dash_stops(self):
        self.assertEqual(launches.sanitize_args(["--model", "m", "--", "--effort", "high"]), ["--model", "m"])

    def test_missing_value_dropped(self):
        self.assertEqual(launches.sanitize_args(["--model"]), [])

    def test_marker_helper(self):
        self.assertTrue(launches.marker_in_argv("--name a1", ["claude", "--name", "a1"]))
        self.assertFalse(launches.marker_in_argv("--name a1", ["claude", "--name", "a2"]))
        self.assertTrue(launches.marker_in_argv("--x=1", ["claude", "--x=1"]))
        self.assertFalse(launches.marker_in_argv("", ["claude"]))


class RecordTests(LaunchesTestCase):
    def test_record_spawn_schema(self):
        settings = self.root / "settings.json"
        settings.write_text(json.dumps({"hooks": {"Stop": []}}))
        args = ["--settings", str(settings), "--session-id", "s-1", "--name", "explore-0615d7",
                "--model", "opus", "--append-system-prompt", "sys"]
        rec = launches.record_spawn(self.meta(), args, ["HERDR_AGENT_DEPTH=1", "FOO=bar"])
        self.assertEqual(rec["v"], 1)
        self.assertEqual(rec["kind"], "spawn")
        self.assertEqual(rec["name"], "explore-0615d7")
        self.assertEqual(rec["marker"], "--name explore-0615d7")
        self.assertEqual(rec["args"], ["--name", "explore-0615d7", "--model", "opus",
                                       "--append-system-prompt", "sys"])
        self.assertEqual(rec["settings"], {"hooks": {"Stop": []}})
        self.assertEqual(rec["env"], {"HERDR_AGENT_DEPTH": "1", "FOO": "bar",
                                      "CLAUDE_CONFIG_DIR": str(self.claude_dir)})
        self.assertEqual(rec["agent_dir"], str(self.agent_root / "explore-0615d7"))
        self.assertEqual((rec["pane_id"], rec["tab_id"], rec["workspace_id"]), ("w1:p2", "w1:t2", "w1"))
        self.assertEqual((rec["restored_count"], rec["unmatched_startups"]), (0, 0))
        on_disk = json.loads((self.state_dir / "launches" / "s-1.json").read_text())
        self.assertEqual(on_disk, rec)
        self.assertEqual(launches.load_all(), [rec])

    def test_tab_env_dict(self):
        rec = launches.record_spawn(self.meta(), ["--name", "x"], {"A": "1"})
        self.assertEqual(rec["env"]["A"], "1")

    def test_record_session(self):
        rec = launches.record_session("s-9", ["claude", "--model", "opus", "--resume", "s-9", "hello",
                                              "--settings", "/x.json"],
                                      "/work", pane_id="w1:p1", workspace_id="w1", tab_id="w1:t1",
                                      claude_config_dir=str(self.claude_dir))
        self.assertEqual(rec["kind"], "session")
        self.assertEqual(rec["args"], ["--model", "opus"])
        self.assertEqual(rec["marker"], "--model opus")
        self.assertIsNone(rec["name"])

    def test_record_session_not_needed(self):
        self.assertIsNone(launches.record_session("s-9", ["claude"], "/work", pane_id="w1:p1"))
        self.assertIsNone(launches.record_session("s-9", ["claude", "-p", "x", "--model", "m"], "/work"))
        self.assertEqual(launches.load_all(), [])

    def test_profile_only_session_recorded(self):
        prof = str(self.root / "profile")
        rec = launches.record_session("s-p", ["claude"], "/work", pane_id="w1:p1", claude_config_dir=prof)
        self.assertEqual(rec["args"], [])
        self.assertIsNone(rec["marker"])
        self.assertEqual(rec["match_env"], {"CLAUDE_CONFIG_DIR": prof})
        self.assertEqual(launches.get("s-p")["match_env"], {"CLAUDE_CONFIG_DIR": prof})
        self.assertFalse(launches.marker_in_argv(rec["marker"], ["claude", "--resume", "s-p"]))

    def test_profile_only_print_not_recorded(self):
        prof = str(self.root / "profile")
        self.assertIsNone(launches.record_session("s-p", ["claude", "-p", "x"], "/work", claude_config_dir=prof))

    def test_default_config_dir_not_profile(self):
        self.assertIsNone(launches.record_session("s-d", ["claude"], "/work",
                                                  claude_config_dir=str(self.claude_dir)))

    def test_behaviour_env_only_session_recorded(self):
        env = {"CLAUDE_ORCHESTRATOR": "off", "HERDR_AGENTS_RESTORE_GRACE_S": "9", "HERDR_AGENT_AUTO_TRUST": "1",
               "HERDR_AGENT_MAX_DEPTH": "3", "HERDR_AGENTS_STATE_DIR": "/s", "PATH": "/bin", "HERDR_AGENT_ID": "x"}
        rec = launches.record_session("s-e", ["claude"], "/work", pane_id="w1:p1", session_env=env)
        want = {"CLAUDE_ORCHESTRATOR": "off", "HERDR_AGENTS_RESTORE_GRACE_S": "9", "HERDR_AGENT_AUTO_TRUST": "1",
                "HERDR_AGENT_MAX_DEPTH": "3"}
        self.assertEqual(rec["env"], want)
        self.assertEqual(rec["match_env"], want)
        self.assertIsNone(rec["marker"])

    def test_behaviour_env_with_profile(self):
        prof = str(self.root / "profile")
        rec = launches.record_session("s-e", ["claude"], "/work", claude_config_dir=prof,
                                      session_env={"CLAUDE_CONFIG_DIR": prof, "CLAUDE_ORCHESTRATOR": "off"})
        self.assertEqual(rec["match_env"], {"CLAUDE_CONFIG_DIR": prof, "CLAUDE_ORCHESTRATOR": "off"})

    def test_location_env_alone_not_recorded(self):
        env = {"HERDR_AGENTS_STATE_DIR": "/s", "HERDR_AGENTS_DATA_DIR": "/d", "PATH": "/bin"}
        self.assertIsNone(launches.record_session("s-e", ["claude"], "/work", session_env=env))

    def test_spawn_records_behaviour_env(self):
        rec = launches.record_spawn(self.meta(), ["--name", "x"], {"CLAUDE_ORCHESTRATOR": "on"},
                                    session_env={"CLAUDE_ORCHESTRATOR": "off", "HERDR_AGENTS_X": "1", "PATH": "/b"})
        self.assertEqual(rec["env"]["CLAUDE_ORCHESTRATOR"], "on")
        self.assertEqual(rec["env"]["HERDR_AGENTS_X"], "1")
        self.assertNotIn("PATH", rec["env"])

    def test_args_session_has_no_match_env(self):
        rec = launches.record_session("s-a", ["--model", "m"], "/w", claude_config_dir=str(self.root / "p"))
        self.assertNotIn("match_env", rec)

    def test_supersede_by_pane(self):
        launches.record_session("old", ["--model", "a"], "/w", pane_id="w1:p1")
        launches.record_session("other", ["--model", "a"], "/w", pane_id="w1:p9")
        launches.record_session("new", ["--model", "b"], "/w", pane_id="w1:p1")
        self.assertEqual(sorted(r["session_id"] for r in launches.load_all()), ["new", "other"])

    def test_supersede_when_new_session_needs_no_record(self):
        launches.record_session("old", ["--model", "a"], "/w", pane_id="w1:p1")
        launches.record_session("new", ["claude"], "/w", pane_id="w1:p1")
        self.assertEqual(launches.load_all(), [])

    def test_spawn_supersedes_session_in_same_pane(self):
        launches.record_session("old", ["--model", "a"], "/w", pane_id="w1:p2")
        launches.record_spawn(self.meta(), ["--name", "explore-0615d7"], [])
        self.assertEqual([r["session_id"] for r in launches.load_all()], ["s-1"])

    def test_forget(self):
        launches.record_spawn(self.meta(), ["--name", "explore-0615d7"], [])
        self.assertTrue(launches.forget("s-1"))
        self.assertFalse(launches.forget("s-1"))
        self.assertEqual(launches.load_all(), [])

    def test_forget_rejects_traversal(self):
        self.assertFalse(launches.forget("../x"))

    def test_update(self):
        rec = launches.record_spawn(self.meta(), ["--name", "explore-0615d7"], [])
        time.sleep(0.01)
        new = launches.update("s-1", pane_id="w1:p7", restored_count=1)
        self.assertEqual(new["pane_id"], "w1:p7")
        self.assertEqual(new["restored_count"], 1)
        self.assertGreater(new["updated_at"], rec["updated_at"])
        self.assertEqual(launches.load_all()[0]["pane_id"], "w1:p7")
        self.assertIsNone(launches.update("missing", pane_id="x"))

    def test_update_env_field_and_environ(self):
        launches.record_spawn(self.meta(), ["--name", "explore-0615d7"], [])
        new = launches.update("s-1", env={"A": "1"})
        self.assertEqual(new["env"], {"A": "1"})
        self.assertEqual(launches.get("s-1")["env"], {"A": "1"})
        other = self.root / "other-state"
        environ = {"HERDR_AGENTS_STATE_DIR": str(other)}
        self.assertIsNone(launches.update("s-1", environ=environ, pane_id="x"))
        self.assertEqual(launches.load_all(environ=environ), [])

    def test_record_session_keeps_restore_status(self):
        prof = str(self.root / "profile")
        launches.record_session("s-p", ["claude"], "/work", pane_id="w1:p1", claude_config_dir=prof)
        launches.update("s-p", status="running", restored_at=123.0, restored_count=1)
        rec = launches.record_session("s-p", ["claude", "--resume", "s-p"], "/work", pane_id="w1:p4",
                                      claude_config_dir=prof)
        self.assertEqual((rec["status"], rec["restored_at"], rec["restored_count"]), ("running", 123.0, 1))

    def test_load_all_skips_garbage(self):
        launches.record_spawn(self.meta(), ["--name", "explore-0615d7"], [])
        d = self.state_dir / "launches"
        (d / "bad.json").write_text("{nope")
        (d / "v2.json").write_text(json.dumps({"v": 2, "session_id": "v2"}))
        (d / "note.txt").write_text("x")
        self.assertEqual([r["session_id"] for r in launches.load_all()], ["s-1"])

    def test_load_all_empty(self):
        self.assertEqual(launches.load_all(), [])

    def test_record_file_private(self):
        launches.record_spawn(self.meta(), ["--name", "explore-0615d7"], [])
        mode = (self.state_dir / "launches" / "s-1.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


class PruneTests(LaunchesTestCase):
    def test_keeps_fresh_with_transcript(self):
        launches.record_spawn(self.meta(), ["--name", "x"], [])
        self.make_transcript("s-1")
        self.assertEqual(launches.prune(), [])
        self.assertEqual(len(launches.load_all()), 1)

    def test_drops_expired(self):
        launches.record_spawn(self.meta(), ["--name", "x"], [])
        self.make_transcript("s-1")
        launches.update("s-1", updated_at=time.time() - 200 * 3600)
        self.assertEqual(launches.prune(), ["s-1"])
        self.assertEqual(launches.load_all(), [])

    def test_max_age_param(self):
        launches.record_spawn(self.meta(), ["--name", "x"], [])
        self.make_transcript("s-1")
        launches.update("s-1", updated_at=time.time() - 3 * 3600)
        self.assertEqual(launches.prune(max_age_h=2), ["s-1"])

    def test_drops_missing_transcript(self):
        launches.record_spawn(self.meta(), ["--name", "x"], [])
        self.assertEqual(launches.prune(), ["s-1"])

    def test_transcript_under_record_config_dir(self):
        other = self.root / "other-config"
        meta = self.meta()
        meta["claude_config_dir"] = str(other)
        launches.record_spawn(meta, ["--name", "x"], [])
        self.make_transcript("s-1", other)
        self.assertEqual(launches.prune(), [])

    def test_drops_after_two_unmatched(self):
        launches.record_spawn(self.meta(), ["--name", "x"], [])
        self.make_transcript("s-1")
        launches.update("s-1", unmatched_startups=1)
        self.assertEqual(launches.prune(), [])
        launches.update("s-1", unmatched_startups=2)
        self.assertEqual(launches.prune(), ["s-1"])


class PointerTests(LaunchesTestCase):
    def test_refresh_pointer_writes_only_when_changed(self):
        path = self.state_dir / "cli.json"
        self.assertTrue(launches.refresh_pointer())
        data = json.loads(path.read_text())
        self.assertEqual(data["cli"], "/opt/herdr-agent/bin/herdr-agent")
        self.assertEqual(data["python"], sys.executable)
        self.assertEqual(data["version"], launches.__version__)
        mtime = path.stat().st_mtime_ns
        self.assertFalse(launches.refresh_pointer())
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        core.CLI_PATH = "/elsewhere/herdr-agent"
        self.assertTrue(launches.refresh_pointer())
        self.assertEqual(json.loads(path.read_text())["cli"], "/elsewhere/herdr-agent")

    def test_spawn_refreshes_pointer(self):
        launches.record_spawn(self.meta(), ["--name", "x"], [])
        self.assertTrue((self.state_dir / "cli.json").exists())

    def test_no_cli_path_no_pointer(self):
        core.CLI_PATH = ""
        self.assertFalse(launches.refresh_pointer())
        self.assertFalse((self.state_dir / "cli.json").exists())


if __name__ == "__main__":
    unittest.main()
