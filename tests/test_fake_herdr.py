import json
import os
import subprocess
import sys

from _base import FAKES, IsolatedTestCase, REPO


class FakeHerdrTest(IsolatedTestCase):
    def herdr(self, *args, check=True):
        p = subprocess.run(["herdr", *args], env=self.env, capture_output=True, text=True, cwd=str(self.root))
        if check:
            self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def result(self, *args):
        return json.loads(self.herdr(*args).stdout)["result"]

    def error(self, *args):
        p = self.herdr(*args, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")
        return json.loads(p.stderr)["error"]

    def create_tab(self, label="t", *extra):
        return self.result("tab", "create", "--workspace", "w1", "--cwd", str(self.root), "--label", label,
                           *extra, "--no-focus")

    def settle_shell(self, pane_id):
        for _ in range(3):
            self.herdr("pane", "process-info", "--pane", pane_id)

    def test_version_and_integration(self):
        self.assertEqual(self.herdr("--version").stdout.strip(), "herdr 0.9.3")
        self.fake_set(version="0.8.0")
        self.assertEqual(self.herdr("--version").stdout.strip(), "herdr 0.8.0")
        self.assertIn("claude: current", self.herdr("integration", "status").stdout)

    def test_tab_create_envelope_and_ids(self):
        p = self.herdr("tab", "create", "--workspace", "w1", "--cwd", str(self.root), "--label", "x",
                       "--env", "A=1", "--env", "B=two=2", "--no-focus")
        body = json.loads(p.stdout)
        self.assertEqual(body["id"], "cli:tab:create")
        res = body["result"]
        self.assertRegex(res["tab"]["tab_id"], r"^w1:t[0-9A-Z]+$")
        self.assertRegex(res["root_pane"]["pane_id"], r"^w1:p[0-9A-Z]+$")
        self.assertEqual(res["tab"]["label"], "x")
        self.assertEqual(res["tab"]["pane_count"], 1)
        pane = self.fake_panes()[res["root_pane"]["pane_id"]]
        self.assertEqual(pane["env"], {"A": "1", "B": "two=2"})
        self.assertEqual(pane["cwd"], str(self.root))

    def test_error_envelope_on_stderr(self):
        err = self.error("pane", "get", "nope")
        self.assertEqual(err, {"code": "pane_not_found", "message": "pane nope not found"})
        self.assertEqual(self.error("agent", "get", "nope")["code"], "agent_not_found")
        self.assertEqual(self.error("tab", "get", "nope")["code"], "tab_not_found")

    def test_get_list_shapes(self):
        res = self.create_tab("shapes")
        tab_id, pane_id = res["tab"]["tab_id"], res["root_pane"]["pane_id"]
        tab = self.result("tab", "get", tab_id)
        self.assertEqual(tab["type"], "tab_info")
        self.assertEqual(set(tab["tab"]), {"agent_status", "focused", "label", "number", "pane_count", "tab_id",
                                           "workspace_id"})
        pane = self.result("pane", "get", pane_id)["pane"]
        for key in ("agent_status", "cwd", "foreground_cwd", "pane_id", "revision", "scroll", "tab_id",
                    "terminal_id", "workspace_id"):
            self.assertIn(key, pane)
        listed = self.result("pane", "list")
        self.assertEqual(listed["type"], "pane_list")
        self.assertEqual([p["pane_id"] for p in listed["panes"]], [pane_id])

    def test_process_info_shell_ready_after(self):
        self.fake_set(shell_ready_after=2)
        pane_id = self.create_tab()["root_pane"]["pane_id"]
        first = self.result("pane", "process-info", "--pane", pane_id)["process_info"]
        self.assertNotEqual(first["foreground_processes"][0]["pid"], first["shell_pid"])
        self.herdr("pane", "process-info", "--pane", pane_id)
        ready = self.result("pane", "process-info", "--pane", pane_id)["process_info"]
        self.assertEqual([p["pid"] for p in ready["foreground_processes"]], [ready["shell_pid"]])
        self.assertTrue(self.herdr("pane", "read", pane_id, "--source", "visible").stdout.rstrip().endswith("%"))

    def test_agent_start_and_process_argv(self):
        pane_id = self.create_tab("a")["root_pane"]["pane_id"]
        self.settle_shell(pane_id)
        res = self.result("agent", "start", "worker-1", "--kind", "claude", "--pane", pane_id, "--timeout", "5000",
                          "--", "--session-id", "sid-1", "--name", "worker-1")
        self.assertEqual(res["agent"]["agent_session"]["value"], "sid-1")
        self.assertEqual(self.result("agent", "get", "worker-1")["agent"]["pane_id"], pane_id)
        info = self.result("pane", "process-info", "--pane", pane_id)["process_info"]
        proc = info["foreground_processes"][0]
        self.assertEqual(proc["argv"], ["claude", "--session-id", "sid-1", "--name", "worker-1"])
        self.assertEqual(proc["argv0"], "claude")
        agents = self.result("agent", "list")["agents"]
        self.assertEqual([a["name"] for a in agents], ["worker-1"])
        self.assertEqual(self.result("pane", "get", pane_id)["pane"]["agent_session"]["source"], "herdr:claude")

    def test_agent_start_requires_shell_prompt(self):
        self.fake_set(shell_ready_after=5)
        pane_id = self.create_tab()["root_pane"]["pane_id"]
        err = self.error("agent", "start", "w", "--kind", "claude", "--pane", pane_id, "--")
        self.assertEqual(err["code"], "pane_not_at_shell_prompt")

    def test_agent_start_knobs(self):
        pane_id = self.create_tab()["root_pane"]["pane_id"]
        self.settle_shell(pane_id)
        self.fake_set(agent_start_error={"code": "timeout", "message": "boom"})
        self.assertEqual(self.error("agent", "start", "w", "--kind", "claude", "--pane", pane_id, "--"),
                         {"code": "timeout", "message": "boom"})
        self.fake_set(agent_start_error=None, start_blocked=True)
        err = self.error("agent", "start", "w", "--kind", "claude", "--pane", pane_id, "--", "--session-id", "s")
        self.assertEqual(err["code"], "agent_not_ready")
        self.assertEqual(self.result("agent", "get", "w")["agent"]["agent_status"], "blocked")
        self.assertEqual(self.error("agent", "prompt", "w", "hi")["code"], "agent_blocked")

    def test_prompt_runs_stop_hook_from_pane_settings(self):
        out_dir = self.root / "agentdir"
        out_dir.mkdir()
        hook = self.root / "hook.py"
        hook.write_text(
            "import json, os, sys\n"
            "p = json.load(sys.stdin)\n"
            "open(os.path.join(os.environ['HERDR_AGENT_DIR'], 'out.json'), 'w').write(json.dumps(p))\n",
            encoding="utf-8")
        settings = out_dir / "settings.json"
        settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{
            "type": "command", "command": f"'{sys.executable}' '{hook}'", "timeout": 30}]}]}}), encoding="utf-8")
        pane_id = self.create_tab("h", "--env", f"HERDR_AGENT_DIR={out_dir}")["root_pane"]["pane_id"]
        self.settle_shell(pane_id)
        self.herdr("agent", "start", "hk", "--kind", "claude", "--pane", pane_id, "--", "--settings", str(settings),
                   "--session-id", "sid-hook")
        self.herdr("agent", "prompt", "hk", "do the thing")
        payload = json.loads((out_dir / "out.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["session_id"], "sid-hook")
        self.assertEqual(payload["last_assistant_message"], "RESULT: do the thing")
        self.assertTrue(os.path.exists(payload["transcript_path"]))
        self.assertTrue(payload["transcript_path"].startswith(str(self.home / ".claude" / "projects")))
        self.assertEqual(self.result("agent", "get", "hk")["agent"]["agent_status"], "idle")

    def test_auto_result_off_leaves_agent_working(self):
        self.fake_set(auto_result=False)
        pane_id = self.create_tab()["root_pane"]["pane_id"]
        self.settle_shell(pane_id)
        self.herdr("agent", "start", "w", "--kind", "claude", "--pane", pane_id, "--", "--session-id", "s")
        self.herdr("agent", "prompt", "w", "go")
        self.assertEqual(self.result("agent", "get", "w")["agent"]["agent_status"], "working")

    def test_api_error_first_turn(self):
        self.fake_set(api_error_first_turn=True)
        pane_id = self.create_tab()["root_pane"]["pane_id"]
        self.settle_shell(pane_id)
        self.herdr("agent", "start", "w", "--kind", "claude", "--pane", pane_id, "--", "--session-id", "sid-api")
        self.herdr("agent", "prompt", "w", "one")
        self.herdr("agent", "prompt", "w", "two")
        transcript = self.home / ".claude" / "projects" / "-fake" / "sid-api.jsonl"
        entries = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(entries[0]["isApiErrorMessage"])
        self.assertEqual(entries[0]["message"]["model"], "<synthetic>")
        self.assertNotIn("isApiErrorMessage", entries[1])

    def test_close_tab_and_pane(self):
        first = self.create_tab("one")
        second = self.create_tab("two")
        self.herdr("tab", "close", first["tab"]["tab_id"])
        self.assertEqual(self.error("tab", "get", first["tab"]["tab_id"])["code"], "tab_not_found")
        self.herdr("pane", "close", second["root_pane"]["pane_id"])
        self.assertEqual(self.fake_panes(), {})
        self.assertEqual(self.fake_tabs(), {})

    def test_pane_close_keeps_tab_with_other_panes(self):
        self.fake_add_pane("w1:p1", "w1:t1")
        self.fake_add_pane("w1:p2", "w1:t1")
        self.herdr("pane", "close", "w1:p2")
        self.assertEqual(self.result("tab", "get", "w1:t1")["tab"]["pane_count"], 1)

    def test_seeded_panes_and_process_argv_knob(self):
        argv = ["claude", "--resume", "sid-9", "--name", "explore-0615d7"]
        self.fake_add_pane("w1:p9", "w1:t9", process_argv=argv, agent="claude", agent_status="idle",
                           agent_session={"agent": "claude", "kind": "id", "source": "herdr:claude", "value": "sid-9"})
        info = self.result("pane", "process-info", "--pane", "w1:p9")["process_info"]
        self.assertEqual(info["foreground_processes"][0]["argv"], argv)
        pane = self.result("pane", "get", "w1:p9")["pane"]
        self.assertEqual(pane["agent_session"]["value"], "sid-9")
        self.assertEqual([p["pane_id"] for p in self.result("agent", "list")["agents"]], ["w1:p9"])

    def test_send_keys(self):
        pane_id = self.create_tab()["root_pane"]["pane_id"]
        self.herdr("pane", "send-keys", pane_id, "enter")
        self.assertEqual(self.fake_panes()[pane_id]["keys"], ["enter"])

    def test_plugin_link_unlink_list_and_reload(self):
        plugin = self.root / "plug"
        plugin.mkdir()
        (plugin / "herdr-plugin.toml").write_text('id = "dmytromykhailiuk.herdr-agents"\nversion = "0.1.0"\n',
                                                  encoding="utf-8")
        self.herdr("plugin", "link", str(plugin))
        listing = self.herdr("plugin", "list").stdout
        self.assertIn("dmytromykhailiuk.herdr-agents", listing)
        self.herdr("plugin", "unlink", "dmytromykhailiuk.herdr-agents")
        self.assertNotIn("dmytromykhailiuk.herdr-agents", self.herdr("plugin", "list").stdout)
        self.assertEqual(self.error("plugin", "unlink", "dmytromykhailiuk.herdr-agents")["code"], "plugin_not_found")
        self.herdr("server", "reload-config")
        self.fake_set(server_running=False)
        self.assertEqual(self.error("server", "reload-config")["code"], "server_not_running")

    def test_session_list_json(self):
        body = json.loads(self.herdr("session", "list", "--json").stdout)
        self.assertEqual(body["sessions"][0]["name"], "default")

    def test_call_log_marks_mutations(self):
        self.herdr("--version")
        self.herdr("pane", "list")
        self.create_tab()
        self.assertEqual([a[:2] for a in self.fake_calls(mutating=True)], [["tab", "create"]])
        self.assertEqual(len(self.fake_calls()), 3)

    def test_unknown_command(self):
        self.assertEqual(self.herdr("frobnicate", "now", check=False).returncode, 2)

    def test_fake_claude_version_only(self):
        p = subprocess.run(["claude", "--version"], env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn("Claude Code", p.stdout)
        self.assertNotEqual(subprocess.run(["claude", "-p", "x"], env=self.env, capture_output=True).returncode, 0)

    def test_fakes_are_first_on_path(self):
        self.assertTrue(self.env["PATH"].startswith(str(FAKES)))
        self.assertTrue(os.access(FAKES / "herdr", os.X_OK))
        self.assertTrue(os.access(FAKES / "claude", os.X_OK))
        self.assertTrue(os.access(REPO / "skills" / "herdr-agent" / "bin" / "herdr-agent", os.X_OK))


class ShimTest(IsolatedTestCase):
    def test_version_and_path(self):
        from _base import CLI
        v = self.run_cli("version")
        self.assertEqual(v.returncode, 0, v.stderr)
        self.assertIn("0.1.0", v.stdout)
        j = self.run_cli("version", "--json")
        self.assertEqual(j.json(), {"version": "0.1.0", "integration": 2})
        p = self.run_cli("path")
        self.assertEqual(p.stdout.strip(), str(CLI))

    def test_path_honours_override(self):
        env = self.make_env(HERDR_AGENT_BIN="/opt/x/herdr-agent")
        self.assertEqual(self.run_cli("path", env=env).stdout.strip(), "/opt/x/herdr-agent")


if __name__ == "__main__":
    import unittest
    unittest.main()
