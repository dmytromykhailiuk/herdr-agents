import json
import os
import shlex
import subprocess
import sys
import time
import unittest

from _base import CLI, IsolatedTestCase


class CliTestCase(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.henv = self.herdr_env()

    def agent_dirs(self):
        return sorted(p.name for p in self.agent_root.iterdir()) if self.agent_root.exists() else []

    def spawn(self, *args, prompt="hello", env=None):
        r = self.run_cli("spawn", "worker", "--prompt", prompt, *args, env=env or self.henv)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def meta(self, aid):
        return json.loads((self.agent_root / aid / "meta.json").read_text(encoding="utf-8"))


class RunTest(CliTestCase):
    def test_run_happy_path(self):
        r = self.run_cli("run", "worker", "--prompt", "do it", "--model", "haiku", env=self.henv)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "RESULT: do it\n")
        self.assertIn("status=done", r.stderr)
        self.assertEqual(self.agent_dirs(), [])
        self.assertEqual(self.fake_tabs(), {})
        create = next(c for c in self.fake_calls() if c[:2] == ["tab", "create"])
        self.assertIn(f"CLAUDE_CONFIG_DIR={self.claude_dir}", create)
        self.assertIn("HERDR_AGENT_DEPTH=1", create)
        self.assertIn("--no-focus", create)
        start = next(c for c in self.fake_calls() if c[:2] == ["agent", "start"])
        claude_args = start[start.index("--") + 1:]
        self.assertEqual(claude_args[claude_args.index("--model") + 1], "haiku")
        self.assertEqual(claude_args[claude_args.index("--effort") + 1], "medium")
        self.assertEqual(claude_args[claude_args.index("--permission-mode") + 1], "auto")

    def test_run_keep_files_and_tab(self):
        r = self.run_cli("run", "worker", "--prompt", "x", "--keep", "--json", env=self.henv)
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["status"], "done")
        self.assertEqual(doc["result"], "RESULT: x")
        self.assertEqual(len(self.fake_tabs()), 1)
        self.assertEqual(self.agent_dirs(), [doc["id"]])

    def test_settings_stop_hook_command(self):
        aid = self.spawn()
        settings = json.loads((self.agent_root / aid / "settings.json").read_text(encoding="utf-8"))
        hook = settings["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual(hook["timeout"], 30)
        self.assertEqual(shlex.split(hook["command"]), [sys.executable, str(CLI), "hook", "stop"])

    def test_prompt_file_and_stdin(self):
        f = self.root / "p.md"
        f.write_text("from file", encoding="utf-8")
        r = self.run_cli("run", "w", "--prompt-file", str(f), env=self.henv)
        self.assertEqual(r.stdout, "RESULT: from file\n")
        r = self.run_cli("run", "w", "--prompt", "-", env=self.henv, stdin="from stdin")
        self.assertEqual(r.stdout, "RESULT: from stdin\n")

    def test_empty_prompt_is_usage_error(self):
        r = self.run_cli("run", "w", "--prompt", "  ", env=self.henv)
        self.assertEqual(r.returncode, 2)
        self.assertIn("empty prompt", r.stderr)

    def test_outside_herdr(self):
        r = self.run_cli("run", "w", "--prompt", "x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("HERDR_ENV", r.stderr)


class GateTest(CliTestCase):
    def test_depth_limit(self):
        r = self.run_cli("spawn", "w", "--prompt", "x", env=self.herdr_env(HERDR_AGENT_DEPTH="2"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("depth limit", r.stderr)

    def test_no_delegate_permission(self):
        r = self.run_cli("spawn", "w", "--prompt", "x", env=self.herdr_env(HERDR_AGENT_DEPTH="1"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("--delegate", r.stderr)

    def test_delegate_at_limit(self):
        env = self.herdr_env(HERDR_AGENT_DEPTH="1", HERDR_AGENT_MAY_DELEGATE="1")
        r = self.run_cli("spawn", "w", "--prompt", "x", "--delegate", env=env)
        self.assertEqual(r.returncode, 2)
        aid = self.spawn(env=env)
        self.assertEqual(self.meta(aid)["depth"], 2)

    def test_delegate_env(self):
        aid = self.spawn("--delegate")
        create = next(c for c in self.fake_calls() if c[:2] == ["tab", "create"])
        self.assertIn("HERDR_AGENT_MAY_DELEGATE=1", create)
        self.assertTrue(self.meta(aid)["delegate"])

    def test_env_validation(self):
        for bad, needle in (("NOEQUALS", "KEY=VAL"), ("1X=y", "KEY=VAL"), ("HERDR_X=1", "reserved")):
            r = self.run_cli("spawn", "w", "--prompt", "x", "--env", bad, env=self.henv)
            self.assertEqual(r.returncode, 2, bad)
            self.assertIn(needle, r.stderr)
        self.spawn("--env", "FOO=bar baz")
        create = next(c for c in self.fake_calls() if c[:2] == ["tab", "create"])
        self.assertIn("FOO=bar baz", create)

    def test_untrusted_cwd(self):
        work = self.root / "work"
        work.mkdir()
        (self.claude_dir / ".claude.json").write_text(json.dumps({"projects": {"/elsewhere": {
            "hasTrustDialogAccepted": True}}}), encoding="utf-8")
        r = self.run_cli("spawn", "w", "--prompt", "x", "--cwd", str(work), env=self.henv)
        self.assertEqual(r.returncode, 3)
        self.assertIn(f"CLAUDE_CONFIG_DIR={self.claude_dir} claude", r.stderr)
        self.assertEqual(self.fake_calls(mutating=True), [])
        (self.claude_dir / ".claude.json").write_text(json.dumps({"projects": {str(self.root): {
            "hasTrustDialogAccepted": True}}}), encoding="utf-8")
        self.spawn("--cwd", str(work))

    def test_nested_repo_auto_trusted(self):
        repo = self.root / "work" / "repo"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        config = self.claude_dir / ".claude.json"
        config.write_text(json.dumps({"projects": {str(self.root / "work"): {
            "hasTrustDialogAccepted": True}}}), encoding="utf-8")
        env = {k: v for k, v in self.henv.items() if k != "HERDR_AGENT_AUTO_TRUST"}
        r = self.run_cli("spawn", "w", "--prompt", "x", "--cwd", str(repo), env={**env, "HERDR_AGENT_AUTO_TRUST": "off"})
        self.assertEqual(r.returncode, 3)
        self.assertNotIn(str(repo), json.loads(config.read_text())["projects"])
        r = self.run_cli("spawn", "w", "--prompt", "x", "--cwd", str(repo), env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"auto-trusted {repo} (parent {self.root / 'work'} is trusted)", r.stderr)
        self.assertTrue(json.loads(config.read_text())["projects"][str(repo)]["hasTrustDialogAccepted"])

    def test_missing_cwd(self):
        r = self.run_cli("spawn", "w", "--prompt", "x", "--cwd", str(self.root / "nope"), env=self.henv)
        self.assertEqual(r.returncode, 2)

    def test_no_workspace(self):
        env = dict(self.henv)
        del env["HERDR_WORKSPACE_ID"]
        r = self.run_cli("spawn", "w", "--prompt", "x", env=env)
        self.assertEqual(r.returncode, 2)


class LifecycleTest(CliTestCase):
    def test_spawn_wait_send_close(self):
        aid = self.spawn(prompt="first")
        self.assertRegex(aid, r"^worker-[0-9a-f]{6}$")
        meta = self.meta(aid)
        self.assertEqual(meta["status"], "working")
        self.assertEqual(meta["claude_config_dir"], str(self.claude_dir))
        r = self.run_cli("wait", aid, env=self.henv)
        self.assertEqual((r.returncode, r.stdout), (0, "RESULT: first\n"), r.stderr)
        self.assertEqual(len(self.fake_tabs()), 1)
        r = self.run_cli("send", aid, "--prompt", "second", "--wait", "--close", env=self.henv)
        self.assertEqual((r.returncode, r.stdout), (0, "RESULT: second\n"), r.stderr)
        self.assertEqual(self.fake_tabs(), {})
        self.assertEqual(self.agent_dirs(), [])

    def test_result_status_list_close(self):
        aid = self.spawn(prompt="p")
        r = self.run_cli("result", aid, env=self.henv)
        self.assertEqual(r.stdout, "RESULT: p")
        r = self.run_cli("status", aid, env=self.henv)
        doc = r.json()
        self.assertEqual(doc["live"], "idle")
        self.assertTrue(doc["has_result"])
        rows = self.run_cli("list", "--json", env=self.henv).json()
        self.assertEqual([x["id"] for x in rows], [aid])
        r = self.run_cli("close", aid, "--keep-files", env=self.henv)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.fake_tabs(), {})
        self.assertTrue(self.meta(aid)["closed"])
        r = self.run_cli("close", aid, env=self.henv)
        self.assertEqual(r.returncode, 0)
        self.assertIn("nothing", r.stderr)
        self.assertEqual(self.agent_dirs(), [])
        self.assertIn("no herdr agents", self.run_cli("list", env=self.henv).stdout)

    def test_close_only_our_pane_in_shared_tab(self):
        aid = self.spawn()
        meta = self.meta(aid)
        self.fake_add_pane("w1:pUSER", meta["tab_id"])
        self.run_cli("close", aid, env=self.henv)
        self.assertIn(meta["tab_id"], self.fake_tabs())
        self.assertEqual(list(self.fake_panes()), ["w1:pUSER"])

    def test_unknown_agent(self):
        r = self.run_cli("wait", "nope-000000", env=self.henv)
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown agent", r.stderr)
        r = self.run_cli("result", "nope-000000")
        self.assertEqual(r.returncode, 1)

    def test_no_result_yet(self):
        self.fake_set(auto_result=False)
        aid = self.spawn()
        r = self.run_cli("result", aid, env=self.henv)
        self.assertEqual(r.returncode, 6)
        r = self.run_cli("wait", aid, "--timeout", "1", env=self.henv)
        self.assertEqual(r.returncode, 4)
        self.assertEqual(self.meta(aid)["status"], "timeout")
        r = self.run_cli("close", aid, env=self.henv)
        self.assertEqual(r.returncode, 1)
        self.assertIn("still working", r.stderr)
        self.assertEqual(self.run_cli("close", aid, "--force", env=self.henv).returncode, 0)

    def test_dead_agent(self):
        aid = self.spawn()
        state = self.fake_read()
        state["panes"] = {}
        state["tabs"] = {}
        self.fake_write(state)
        meta = self.meta(aid)
        meta["last_prompt_at_ns"] = time.time_ns()
        (self.agent_root / aid / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        r = self.run_cli("wait", aid, env=self.henv)
        self.assertEqual(r.returncode, 5)
        self.assertIn("tab/pane was closed", r.stderr)


class BlockedTest(CliTestCase):
    def test_start_blocked(self):
        self.fake_set(start_blocked=True)
        r = self.run_cli("run", "w", "--prompt", "x", env=self.henv)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("trust the files", r.stderr)
        self.assertIn(" keys w-", r.stderr)
        self.assertIn(str(CLI), r.stderr)
        self.assertEqual(len(self.fake_tabs()), 1)
        aid = self.agent_dirs()[0]
        self.assertEqual(self.meta(aid)["status"], "blocked")

    def test_agent_start_error_closes_tab(self):
        self.fake_set(agent_start_error={"code": "boom", "message": "kaput"})
        r = self.run_cli("run", "w", "--prompt", "x", env=self.henv)
        self.assertEqual(r.returncode, 1)
        self.assertIn("agent start failed", r.stderr)
        self.assertEqual(self.fake_tabs(), {})
        aid = self.agent_dirs()[0]
        self.assertTrue((self.agent_root / aid / "start-failure.txt").exists())
        self.assertEqual(self.meta(aid)["status"], "error")

    def test_tab_create_error(self):
        self.fake_set(tab_create_error={"code": "workspace_not_found", "message": "no"})
        r = self.run_cli("spawn", "w", "--prompt", "x", env=self.henv)
        self.assertEqual(r.returncode, 1)
        self.assertIn("tab create failed", r.stderr)

    def test_slow_shell(self):
        self.fake_set(shell_ready_after=3)
        r = self.run_cli("run", "w", "--prompt", "x", env=self.henv)
        self.assertEqual(r.returncode, 0, r.stderr)


class ApiErrorTest(CliTestCase):
    def test_auto_continue_recovers(self):
        self.fake_set(api_error_first_turn=True)
        r = self.run_cli("run", "w", "--prompt", "x", env=self.henv)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("RESULT: continue"))
        self.assertIn("auto_continue: sent after API error", r.stderr)
        prompts = [c for c in self.fake_calls() if c[:2] == ["agent", "prompt"]]
        self.assertEqual(len(prompts), 2)

    def test_api_error_session_gone_exit_7(self):
        self.fake_set(api_error_first_turn=True)
        aid = self.spawn()
        state = self.fake_read()
        state["panes"], state["tabs"] = {}, {}
        self.fake_write(state)
        r = self.run_cli("wait", aid, env=self.henv)
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertIn("session is gone", r.stderr)
        self.assertIn(f"CLAUDE_CONFIG_DIR={self.claude_dir} claude --resume", r.stderr)
        self.assertEqual(self.meta(aid)["status"], "api_error")


class GcTest(CliTestCase):
    def make_old(self, name, meta=True):
        d = self.agent_root / name
        d.mkdir(parents=True)
        if meta:
            (d / "meta.json").write_text(json.dumps({"id": name, "status": "done", "pane_id": "w1:pGONE",
                                                     "updated_at": 1000}), encoding="utf-8")
        os.utime(d, (1000, 1000))
        return d

    def test_gc_dry_run_and_real(self):
        gone = self.make_old("old-000001")
        nometa = self.make_old("old-000002", meta=False)
        r = self.run_cli("gc", "--dry-run", env=self.henv)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("remove old-000001 (done, tab gone)", r.stdout)
        self.assertIn("remove old-000002 (no meta)", r.stdout)
        self.assertTrue(gone.exists() and nometa.exists())
        self.assertEqual(self.fake_calls(mutating=True), [])
        self.run_cli("gc", env=self.henv)
        self.assertFalse(gone.exists() or nometa.exists())

    def test_gc_keeps_recent_and_live(self):
        aid = self.spawn()
        r = self.run_cli("gc", "--older-than", "0", "--dry-run", "--close-idle", env=self.henv)
        self.assertIn(f"close {aid}", r.stdout)
        r = self.run_cli("gc", env=self.henv)
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.agent_dirs(), [aid])


class MiscTest(CliTestCase):
    def test_path_and_version(self):
        r = self.run_cli("path")
        self.assertEqual(r.stdout.strip(), str(CLI))
        r = self.run_cli("path", env=self.make_env(HERDR_AGENT_BIN="/x/bin/herdr-agent"))
        self.assertEqual(r.stdout.strip(), "/x/bin/herdr-agent")
        doc = self.run_cli("version", "--json").json()
        self.assertEqual(set(doc), {"version", "integration"})
        self.assertIn("herdr-agents", self.run_cli("version").stdout)

    def test_unknown_hook_never_blocks(self):
        r = self.run_cli("hook", "bogus")
        self.assertEqual(r.returncode, 1)

    def test_herdr_route_usage(self):
        self.assertEqual(self.run_cli("herdr", "bogus").returncode, 2)


class LaunchesIntegrationTest(CliTestCase):
    def test_spawn_records_and_close_forgets(self):
        try:
            sys.path.insert(0, str(CLI.parent.parent / "lib"))
            import herdragent.launches  # noqa: F401
        except ImportError:
            self.skipTest("launches.py not available yet")
        finally:
            sys.path.pop(0)
        aid = self.spawn()
        sid = self.meta(aid)["session_id"]
        rec_path = self.state_dir / "launches" / f"{sid}.json"
        self.assertTrue(rec_path.exists(), list(self.state_dir.rglob("*")))
        self.assertTrue((self.state_dir / "cli.json").exists())
        self.run_cli("close", aid, env=self.henv)
        self.assertFalse(rec_path.exists())


if __name__ == "__main__":
    unittest.main()
