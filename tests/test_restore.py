import fcntl
import json
import os
import sys
import time
import unittest
from unittest import mock

from _base import FAKES, LIB, IsolatedTestCase

sys.path.insert(0, str(LIB))

from herdragent import restore  # noqa: E402

SID = "11111111-aaaa-bbbb-cccc-000000000001"
SID2 = "22222222-aaaa-bbbb-cccc-000000000002"
AID = "explore-0615d7"


class RestoreTestCase(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.env = self.make_env(HERDR_AGENTS_RESTORE_POLL_S="0.2", HERDR_AGENTS_RESTORE_PACE_S="0.1")
        self.config = self.home / ".config" / "herdr" / "config.toml"
        self.config.parent.mkdir(parents=True)
        self.config.write_text("[session]\nresume_agents_on_restore = true\n")
        self.fake_set(panes={}, tabs={}, counter=100)
        self.cwd = str(self.root)

    def transcript(self, sid, cfg=None):
        d = (cfg or self.claude_dir) / "projects" / "-work"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sid}.jsonl").write_text("{}\n")

    def record(self, sid=SID, kind="spawn", transcript=True, **fields):
        now = time.time()
        rec = {
            "v": 1, "kind": kind, "session_id": sid, "name": AID if kind == "spawn" else None,
            "label": AID if kind == "spawn" else "main", "cwd": self.cwd, "workspace_id": "w1",
            "pane_id": "w1:p2", "tab_id": "w1:t2", "claude_config_dir": str(self.claude_dir),
            "env": {}, "args": [], "settings": None, "agent_dir": None, "marker": "",
            "created_at": now, "updated_at": now, "restored_count": 0, "unmatched_startups": 0,
        }
        if kind == "spawn":
            rec.update(
                env={"HERDR_AGENT_DIR": str(self.agent_root / AID), "HERDR_AGENT_ID": AID,
                     "HERDR_AGENT_DEPTH": "1", "CLAUDE_CONFIG_DIR": str(self.claude_dir)},
                args=["--name", AID, "--model", "sonnet", "--permission-mode", "plan"],
                agent_dir=str(self.agent_root / AID), marker=f"--name {AID}",
            )
        else:
            rec.update(env={"CLAUDE_CONFIG_DIR": str(self.claude_dir)}, args=["--model", "opus"],
                       marker="--model opus")
        rec.update(fields)
        d = self.state_dir / "launches"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sid}.json").write_text(json.dumps(rec))
        if transcript:
            self.transcript(sid)
        return rec

    def load(self, sid=SID):
        p = self.state_dir / "launches" / f"{sid}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def agent_meta(self, aid=AID, **fields):
        d = self.agent_root / aid
        d.mkdir(parents=True, exist_ok=True)
        meta = {"id": aid, "role": "explore", "cwd": self.cwd, "workspace_id": "w1", "session_id": SID,
                "status": "done", "pane_id": "w1:p2", "tab_id": "w1:t2", "created_tab": True}
        meta.update(fields)
        (d / "meta.json").write_text(json.dumps(meta))
        return meta

    def read_meta(self, aid=AID):
        return json.loads((self.agent_root / aid / "meta.json").read_text())

    def startup(self, *extra, env=None, timeout=60):
        res = self.run_cli("herdr", "startup", "--grace", "2", *extra, env=env or self.env, timeout=timeout)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        return res

    def mutating(self):
        return [c for c in self.fake_calls(mutating=True)]

    def by_cmd(self, *prefix):
        return [c for c in self.fake_calls() if tuple(c[:len(prefix)]) == prefix]

    def start_argv(self):
        starts = self.by_cmd("agent", "start")
        self.assertEqual(len(starts), 1, starts)
        argv = starts[0]
        return argv, argv[argv.index("--") + 1:]

    def restore_log(self):
        p = self.state_dir / "restore.log"
        return p.read_text() if p.exists() else ""


class RestoreScenarios(RestoreTestCase):
    def test_healthy_marker_untouched(self):
        self.record()
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd,
                           process_argv=["claude", "--resume", SID, "--settings", "/x.json", "--name", AID])
        res = self.startup()
        self.assertEqual(self.mutating(), [])
        self.assertIn("healthy", res.stdout)
        self.assertEqual(self.load()["restored_count"], 0)

    def test_healthy_by_session_updates_moved_ids(self):
        self.record(pane_id="w1:pOLD", tab_id="w1:tOLD")
        self.agent_meta(pane_id="w1:pOLD", tab_id="w1:tOLD")
        self.fake_add_pane("w1:p9", "w1:t9", cwd="/elsewhere", agent="claude", agent_name=AID,
                           agent_session={"agent": "claude", "kind": "id", "source": "herdr:claude", "value": SID},
                           process_argv=["claude", "--resume", SID, "--name", AID])
        self.startup()
        self.assertEqual(self.mutating(), [])
        rec = self.load()
        self.assertEqual((rec["pane_id"], rec["tab_id"]), ("w1:p9", "w1:t9"))
        meta = self.read_meta()
        self.assertEqual((meta["pane_id"], meta["tab_id"]), ("w1:p9", "w1:t9"))

    def test_native_relaunch_replaced(self):
        self.record()
        self.agent_meta()
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, process_argv=["claude", "--resume", SID])
        self.startup()
        self.assertEqual(self.by_cmd("tab", "close", "w1:t2"), [["tab", "close", "w1:t2"]])
        creates = self.by_cmd("tab", "create")
        self.assertEqual(len(creates), 1)
        create = creates[0]
        self.assertIn("--no-focus", create)
        self.assertEqual(create[create.index("--cwd") + 1], self.cwd)
        self.assertEqual(create[create.index("--label") + 1], AID)
        envs = [create[i + 1] for i, a in enumerate(create) if a == "--env"]
        self.assertIn(f"CLAUDE_CONFIG_DIR={self.claude_dir}", envs)
        self.assertIn(f"HERDR_AGENT_ID={AID}", envs)
        self.assertIn(f"HERDR_AGENT_DIR={self.agent_root / AID}", envs)
        argv, claude = self.start_argv()
        self.assertEqual(argv[2], AID)
        self.assertEqual(argv[argv.index("--kind") + 1], "claude")
        settings = str(self.agent_root / AID / "settings.json")
        self.assertEqual(claude, ["--resume", SID, "--settings", settings, "--name", AID,
                                  "--model", "sonnet", "--permission-mode", "plan"])
        new_pane = argv[argv.index("--pane") + 1]
        self.assertNotEqual(new_pane, "w1:p2")
        hook = json.loads((self.agent_root / AID / "settings.json").read_text())
        self.assertIn("hook stop", hook["hooks"]["Stop"][0]["hooks"][0]["command"])
        panes = self.fake_panes()
        self.assertNotIn("w1:p2", panes)
        self.assertEqual(panes[new_pane]["env"]["CLAUDE_CONFIG_DIR"], str(self.claude_dir))
        rec = self.load()
        self.assertEqual(rec["pane_id"], new_pane)
        self.assertEqual(rec["restored_count"], 1)
        self.assertEqual(rec["status"], "running")
        meta = self.read_meta()
        self.assertEqual(meta["pane_id"], new_pane)
        self.assertEqual(meta["status"], "running")
        self.assertEqual(meta["role"], "explore")

    def test_old_pane_closed_before_agent_start(self):
        self.record()
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, process_argv=["claude", "--resume", SID])
        self.startup()
        kinds = [tuple(c[:2]) for c in self.mutating()]
        self.assertEqual(kinds, [("tab", "create"), ("tab", "close"), ("agent", "start")])

    def test_shell_pane_relaunch_session_record(self):
        self.record(kind="session")
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd)
        self.startup()
        argv, claude = self.start_argv()
        self.assertEqual(argv[2], f"claude-{SID[:6]}")
        self.assertEqual(claude, ["--resume", SID, "--model", "opus"])
        create = self.by_cmd("tab", "create")[0]
        self.assertEqual(create[create.index("--label") + 1], "main")
        self.assertEqual(self.load()["restored_count"], 1)
        self.assertFalse((self.agent_root / AID).exists())

    def test_shell_pane_of_ended_session_not_relaunched(self):
        self.record(kind="session", ended_at=time.time(), ended_reason="prompt_input_exit")
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd)
        res = self.startup()
        self.assertEqual(self.mutating(), [])
        self.assertIsNone(self.load())
        self.assertIn("exited by the user", res.stdout + self.restore_log())

    def test_pane_in_multi_pane_tab_only_pane_closed(self):
        self.record()
        self.fake_add_pane("w1:p1", "w1:t2", cwd=self.cwd, process_argv=["vim"])
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, process_argv=["claude", "--resume", SID])
        self.startup()
        self.assertEqual(self.by_cmd("pane", "close"), [["pane", "close", "w1:p2"]])
        self.assertEqual(self.by_cmd("tab", "close"), [])
        self.assertIn("w1:t2", self.fake_tabs())
        self.assertIn("w1:p1", self.fake_panes())

    def test_cwd_mismatch_is_no_match(self):
        self.record()
        self.fake_add_pane("w1:p2", "w1:t2", cwd="/other", process_argv=["claude", "--resume", SID])
        self.startup("--grace", "0.3")
        self.assertEqual(self.mutating(), [])
        self.assertEqual(self.load()["unmatched_startups"], 1)

    def test_unmatched_counter_then_dropped(self):
        self.record()
        res = self.startup("--grace", "0.3")
        self.assertIn("unmatched 1/2", res.stdout)
        self.assertEqual(self.load()["unmatched_startups"], 1)
        self.startup("--grace", "0.3")
        self.assertIsNone(self.load())
        self.assertEqual(self.mutating(), [])

    def test_expired_and_missing_transcript_pruned(self):
        old = time.time() - 200 * 3600
        self.record(SID, created_at=old, updated_at=old)
        self.record(SID2, transcript=False, pane_id="w1:p5")
        res = self.startup("--grace", "0")
        self.assertIsNone(self.load(SID))
        self.assertIsNone(self.load(SID2))
        self.assertIn("nothing to restore", res.stdout)
        self.assertEqual(self.fake_calls(), [])

    def test_record_in_profile_config_dir_kept(self):
        profile = self.root / "profile"
        self.record(claude_config_dir=str(profile), transcript=False)
        self.transcript(SID, cfg=profile)
        self.startup("--grace", "0")
        self.assertIsNotNone(self.load())

    def test_lock_held_is_noop(self):
        self.record()
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, process_argv=["claude", "--resume", SID])
        with open(self.state_dir / "restore.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            res = self.startup()
        self.assertIn("another herdr-agents restore is running", res.stdout)
        self.assertEqual(self.fake_calls(), [])
        self.assertEqual(self.load()["restored_count"], 0)

    def test_agent_start_blocked_keeps_tab(self):
        self.record()
        self.agent_meta()
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, process_argv=["claude", "--resume", SID])
        self.fake_set(start_blocked=True)
        res = self.startup()
        argv, _ = self.start_argv()
        new_pane = argv[argv.index("--pane") + 1]
        rec = self.load()
        self.assertEqual(rec["status"], "blocked")
        self.assertEqual(rec["pane_id"], new_pane)
        self.assertEqual(rec["restored_count"], 0)
        self.assertIn(rec["tab_id"], self.fake_tabs())
        self.assertEqual(self.read_meta()["status"], "blocked")
        self.assertIn("blocked on a dialog", res.stdout)
        closes = [c for c in self.by_cmd("tab", "close") if c[2] == rec["tab_id"]]
        self.assertEqual(closes, [])

    def test_reboot_agent_dir_recreated(self):
        self.record()
        self.assertFalse((self.agent_root / AID).exists())
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd)
        res = self.startup()
        d = self.agent_root / AID
        self.assertTrue((d / "settings.json").is_file())
        meta = self.read_meta()
        self.assertEqual(meta["session_id"], SID)
        self.assertEqual(meta["status"], "running")
        self.assertTrue(meta["restored"])
        self.assertIn("missing (reboot?)", res.stdout)

    def test_busy_shell_retried_not_killed(self):
        self.record()
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, shell_ready_after=3)
        self.startup("--grace", "5")
        calls = self.fake_calls()
        close_at = next(i for i, c in enumerate(calls) if c[:2] == ["tab", "close"])
        infos = [i for i, c in enumerate(calls) if c[:2] == ["pane", "process-info"] and "w1:p2" in c]
        self.assertGreaterEqual(len([i for i in infos if i < close_at]), 5)
        self.assertEqual(len(self.by_cmd("agent", "start")), 1)

    def test_working_native_left_alone(self):
        self.record()
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, agent="claude", agent_status="working",
                           agent_session={"agent": "claude", "kind": "id", "source": "herdr:claude", "value": SID},
                           process_argv=["claude", "--resume", SID])
        res = self.startup("--grace", "0.4")
        self.assertEqual(self.mutating(), [])
        self.assertIn("left alone (working", res.stdout)
        self.assertEqual(self.load()["unmatched_startups"], 0)

    def test_foreign_session_in_pane_left_alone(self):
        self.record()
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, process_argv=["claude", "--resume", SID2])
        res = self.startup("--grace", "0.4")
        self.assertEqual(self.mutating(), [])
        self.assertIn("left alone (foreign", res.stdout)

    def test_two_records_paced(self):
        self.record(SID)
        self.record(SID2, kind="session", pane_id="w1:p5", tab_id="w1:t5")
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, process_argv=["claude", "--resume", SID])
        self.fake_add_pane("w1:p5", "w1:t5", cwd=self.cwd, process_argv=["claude", "--resume", SID2])
        self.startup()
        starts = self.by_cmd("agent", "start")
        self.assertEqual(sorted(s[2] for s in starts), sorted([AID, f"claude-{SID2[:6]}"]))
        self.assertEqual(self.load(SID)["restored_count"], 1)
        self.assertEqual(self.load(SID2)["restored_count"], 1)

    def test_dry_run_no_mutation(self):
        self.config.unlink()
        self.record()
        old = time.time() - 200 * 3600
        self.record(SID2, created_at=old, updated_at=old, pane_id="w1:p5")
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, process_argv=["claude", "--resume", SID])
        before = self.load()
        res = self.startup("--dry-run")
        self.assertEqual(self.mutating(), [])
        self.assertIn("would relaunch (native)", res.stdout)
        self.assertIn("would prune", res.stdout)
        self.assertIn("would set [session]", res.stdout)
        self.assertFalse(self.config.exists())
        self.assertEqual(self.load(), before)
        self.assertIsNotNone(self.load(SID2))
        self.assertEqual(self.restore_log(), "")

    def test_disabled(self):
        self.record()
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, process_argv=["claude", "--resume", SID])
        self.startup(env=self.make_env(HERDR_AGENTS_RESTORE="off"))
        self.assertEqual(self.fake_calls(), [])

    def test_log_written_to_state_and_plugin_dir(self):
        plugin_state = self.root / "plugin-state"
        self.record()
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd,
                           process_argv=["claude", "--resume", SID, "--name", AID])
        self.startup(env=self.make_env(HERDR_PLUGIN_STATE_DIR=str(plugin_state), HERDR_AGENTS_RESTORE_POLL_S="0.2"))
        self.assertIn("restore done", self.restore_log())
        self.assertIn("restore done", (plugin_state / "restore.log").read_text())

    def test_herdr_bin_path_honoured(self):
        self.record()
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, process_argv=["claude", "--resume", SID])
        env = self.make_env(PATH="/usr/bin:/bin", HERDR_BIN_PATH=str(FAKES / "herdr"),
                            HERDR_SOCKET_PATH=str(self.root / "sock"), HERDR_AGENTS_RESTORE_POLL_S="0.2")
        res = self.startup(env=env)
        self.assertIn(str(self.root / "sock"), res.stdout)
        self.assertEqual(len(self.by_cmd("agent", "start")), 1)

    def test_detached_when_run_as_plugin_hook(self):
        self.record()
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd,
                           process_argv=["claude", "--resume", SID, "--name", AID])
        env = self.make_env(HERDR_PLUGIN_EVENT="startup", HERDR_AGENTS_RESTORE_POLL_S="0.2")
        started = time.monotonic()
        res = self.run_cli("herdr", "startup", "--grace", "2", env=env)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "")
        deadline = started + 20
        while "restore done" not in self.restore_log() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertIn("restore done: 1 healthy", self.restore_log())


class ProfileOnlySessions(RestoreTestCase):
    def setUp(self):
        super().setUp()
        self.profile = self.root / "profile"
        self.socket = self.root / "herdr.sock"
        self.socket.write_text("")
        self.server_start = time.time() - 100
        os.utime(self.socket, (self.server_start, self.server_start))
        self.env = self.make_env(HERDR_AGENTS_RESTORE_POLL_S="0.2", HERDR_AGENTS_RESTORE_PACE_S="0.1",
                                 HERDR_SOCKET_PATH=str(self.socket))

    def profile_record(self, **fields):
        rec = self.record(kind="session", transcript=False, claude_config_dir=str(self.profile),
                          env={"CLAUDE_CONFIG_DIR": str(self.profile)}, args=[], marker=None,
                          match_env={"CLAUDE_CONFIG_DIR": str(self.profile)}, **fields)
        self.transcript(SID, cfg=self.profile)
        return rec

    def test_native_resume_relaunched_with_profile(self):
        self.profile_record()
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, process_argv=["claude", "--resume", SID])
        self.startup()
        argv, claude = self.start_argv()
        self.assertEqual(claude, ["--resume", SID])
        create = self.by_cmd("tab", "create")[0]
        envs = [create[i + 1] for i, a in enumerate(create) if a == "--env"]
        self.assertEqual(envs, [f"CLAUDE_CONFIG_DIR={self.profile}"])
        rec = self.load()
        self.assertEqual(rec["status"], "running")
        self.assertGreaterEqual(rec["restored_at"], self.server_start)
        self.assertEqual(rec["env"], {"CLAUDE_CONFIG_DIR": str(self.profile)})

    def test_pane_restored_this_server_lifetime_is_healthy(self):
        self.profile_record(status="running", restored_at=time.time() - 10, restored_count=1)
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, process_argv=["claude", "--resume", SID])
        res = self.startup()
        self.assertEqual(self.mutating(), [])
        self.assertIn("healthy", res.stdout)
        self.assertEqual(self.load()["restored_count"], 1)

    def test_restored_before_server_start_relaunched_again(self):
        self.profile_record(status="running", restored_at=self.server_start - 3600, restored_count=1)
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, process_argv=["claude", "--resume", SID])
        self.startup()
        self.assertEqual(self.start_argv()[1], ["--resume", SID])
        self.assertEqual(self.load()["restored_count"], 2)

    def test_second_run_after_own_relaunch_is_noop(self):
        self.profile_record()
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, process_argv=["claude", "--resume", SID])
        self.startup()
        self.assertEqual(len(self.by_cmd("agent", "start")), 1)
        self.startup()
        self.assertEqual(len(self.by_cmd("agent", "start")), 1)
        self.assertEqual(self.load()["restored_count"], 1)

    def test_original_process_without_resume_is_healthy(self):
        self.profile_record()
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, agent="claude",
                           agent_session={"agent": "claude", "kind": "id", "source": "herdr:claude", "value": SID},
                           process_argv=["claude"])
        res = self.startup()
        self.assertEqual(self.mutating(), [])
        self.assertIn("healthy", res.stdout)


class EnvOnlySessions(RestoreTestCase):
    def setUp(self):
        super().setUp()
        self.env = self.make_env(HERDR_AGENTS_RESTORE_POLL_S="0.2", HERDR_AGENTS_RESTORE_PACE_S="0.1")

    def test_orchestrator_opt_out_restored(self):
        want = {"CLAUDE_ORCHESTRATOR": "off"}
        self.record(kind="session", claude_config_dir="", env=dict(want), args=[], marker=None, match_env=dict(want))
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, process_argv=["claude", "--resume", SID])
        res = self.startup()
        self.assertEqual(self.start_argv()[1], ["--resume", SID])
        create = self.by_cmd("tab", "create")[0]
        envs = [create[i + 1] for i, a in enumerate(create) if a == "--env"]
        self.assertEqual(envs, ["CLAUDE_ORCHESTRATOR=off"])
        self.assertIn(f"session main ({SID[:8]})", res.stdout)
        self.assertNotIn("session session", res.stdout + self.restore_log())

    def test_describe_without_name_or_label(self):
        self.assertEqual(restore._describe({"kind": "session", "session_id": SID}), f"session ({SID[:8]})")


class ProfileOnlyProcessEnv(unittest.TestCase):
    def setUp(self):
        self.rec = {"kind": "session", "session_id": SID, "marker": None, "claude_config_dir": "/prof",
                    "match_env": {"CLAUDE_CONFIG_DIR": "/prof"}, "status": "running",
                    "restored_at": time.time()}
        self.pane = {"pane_id": "w1:p2", "agent_session": {"value": SID}}

    def classify(self, env):
        proc = {"argv": ["claude", "--resume", SID], "name": "claude", "pid": 2, "env": env}
        info = {"foreground_processes": [proc], "shell_pid": 1}
        with mock.patch.object(restore, "process_info", return_value=info):
            return restore.classify(self.rec, self.pane, server_started=0)[0]

    def test_env_with_profile_is_healthy(self):
        self.assertEqual(self.classify({"CLAUDE_CONFIG_DIR": "/prof"}), "healthy")

    def test_env_without_profile_is_native(self):
        self.assertEqual(self.classify({"PATH": "/bin"}), "native")

    def test_env_only_record_needs_all_vars(self):
        self.rec.update(claude_config_dir="", match_env={"CLAUDE_ORCHESTRATOR": "off"})
        self.assertEqual(self.classify({"CLAUDE_ORCHESTRATOR": "off"}), "healthy")
        self.assertEqual(self.classify({"CLAUDE_ORCHESTRATOR": "on"}), "native")


class RestoreConfig(RestoreTestCase):
    def test_config_created_and_reload_once(self):
        self.config.unlink()
        self.startup("--grace", "0")
        self.assertIn("resume_agents_on_restore = true", self.config.read_text())
        self.assertEqual(self.by_cmd("server", "reload-config"), [["server", "reload-config"]])
        self.startup("--grace", "0")
        self.assertEqual(len(self.by_cmd("server", "reload-config")), 1)

    def test_key_added_to_existing_table(self):
        self.config.write_text("[session]\nother = 1\n")
        self.startup("--grace", "0")
        self.assertIn("resume_agents_on_restore = true", self.config.read_text())
        self.assertEqual(len(self.by_cmd("server", "reload-config")), 1)

    def test_explicit_false_kept_with_warning(self):
        text = "[session]\nresume_agents_on_restore = false\n"
        self.config.write_text(text)
        res = self.startup("--grace", "0")
        self.assertEqual(self.config.read_text(), text)
        self.assertEqual(self.by_cmd("server", "reload-config"), [])
        self.assertIn("warning: [session] resume_agents_on_restore = false", res.stdout)
        self.assertIn("resume_agents_on_restore = false", self.restore_log())


class RestoreStatus(RestoreTestCase):
    def test_status_json(self):
        self.record()
        self.record(SID2, kind="session", pane_id="w1:p5", tab_id="w1:t5")
        self.fake_add_pane("w1:p2", "w1:t2", cwd=self.cwd, process_argv=["claude", "--resume", SID])
        res = self.run_cli("herdr", "status", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        data = res.json()
        rows = {r["session_id"]: r for r in data["records"]}
        self.assertEqual(rows[SID]["state"], "native")
        self.assertEqual(rows[SID]["match"], "pane")
        self.assertEqual(rows[SID2]["state"], "unmatched")
        self.assertEqual(data["config"], "true")
        self.assertEqual(self.mutating(), [])

    def test_status_text_empty(self):
        res = self.run_cli("herdr", "status")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("no launch records", res.stdout)

    def test_usage_error(self):
        res = self.run_cli("herdr", "startup", "--bogus")
        self.assertEqual(res.returncode, 2)


if __name__ == "__main__":
    unittest.main()
