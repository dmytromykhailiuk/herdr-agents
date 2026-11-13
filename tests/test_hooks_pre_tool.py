import json
import os
import shlex
import subprocess
import sys
import time
import unittest
from pathlib import Path

from _base import CLI, IsolatedTestCase


class PreAgentTest(IsolatedTestCase):
    def agent(self, subagent_type="Explore", env=None, mode=None, **tool_input):
        payload = {"session_id": "s1", "tool_name": "Agent", "tool_use_id": "toolu_01ABC",
                   "tool_input": dict({"prompt": "find the parser", "description": "d"}, **tool_input)}
        if mode:
            payload["permission_mode"] = mode
        if subagent_type is not None:
            payload["tool_input"]["subagent_type"] = subagent_type
        r = self.run_hook("pre-agent", payload, env=env if env is not None else self.herdr_env())
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def denied(self, r):
        out = r.json()["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "deny")
        return out["permissionDecisionReason"]

    def command_of(self, reason):
        start = reason.index("parallel work): ") + len("parallel work): ")
        end = reason.index(". The explore/plan roles")
        return shlex.split(reason[start:end])

    def test_explore_denied_in_herdr(self):
        reason = self.denied(self.agent("Explore"))
        argv = self.command_of(reason)
        self.assertEqual(argv[0], str(CLI))
        self.assertEqual(argv[1:5], ["run", "explore", "--model", "haiku"])
        prompt_file = Path(argv[argv.index("--prompt-file") + 1])
        self.assertEqual(prompt_file.parent, self.state_dir / "prompts")
        self.assertEqual(prompt_file.read_text(), "find the parser")
        self.assertIn("plan mode", reason)
        self.assertIn("read-only", reason)

    def test_vault_keeper_search_read_only_only_in_plan_mode(self):
        prompt = "Operation: search\nQuestion: x"
        argv = self.command_of(self.denied(self.agent("vault-keeper", prompt=prompt, mode="plan")))
        self.assertEqual(argv[1:5], ["run", "vault-keeper", "--agent", "vault-keeper"])
        self.assertIn("--read-only", argv)
        for mode in (None, "default", "auto"):
            with self.subTest(mode):
                argv = self.command_of(self.denied(self.agent("vault-keeper", prompt=prompt, mode=mode)))
                self.assertNotIn("--read-only", argv)

    def test_plugin_namespaced_agents_use_bare_role(self):
        prompt = "Operation: search\nQuestion: x"
        argv = self.command_of(self.denied(
            self.agent("agent-vault:vault-keeper", prompt=prompt, mode="plan")))
        self.assertEqual(argv[1:5], ["run", "vault-keeper", "--agent", "agent-vault:vault-keeper"])
        self.assertIn("--read-only", argv)
        argv = self.command_of(self.denied(self.agent("agent-vault:vault-keeper", prompt=prompt)))
        self.assertNotIn("--read-only", argv)
        argv = self.command_of(self.denied(self.agent("verify-loop:verify-reviewer")))
        self.assertEqual(argv[1:5], ["run", "verify-reviewer", "--agent", "verify-loop:verify-reviewer"])

    def test_vault_reviewer_always_read_only(self):
        for name in ("vault-reviewer", "agent-vault:vault-reviewer"):
            for mode in (None, "default", "plan"):
                with self.subTest(name=name, mode=mode):
                    argv = self.command_of(self.denied(self.agent(name, prompt="review", mode=mode)))
                    self.assertEqual(argv[1:5], ["run", "vault-reviewer", "--agent", name])
                    self.assertIn("--read-only", argv)

    def test_vault_keeper_other_operations_not_read_only(self):
        for prompt in ("Operation: remember\nnote", "Operation: update", "do something"):
            with self.subTest(prompt):
                argv = self.command_of(self.denied(self.agent("vault-keeper", prompt=prompt)))
                self.assertNotIn("--read-only", argv)

    def test_plan_delegates_below_max_depth(self):
        argv = self.command_of(self.denied(self.agent("Plan")))
        self.assertEqual(argv[1:4], ["run", "plan", "--delegate"])
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        env = self.herdr_env(HERDR_AGENT_DEPTH="1", HERDR_AGENT_MAY_DELEGATE="1")
        argv = self.command_of(self.denied(self.agent("Plan", env=env)))
        self.assertEqual(argv[1:3], ["run", "plan"])
        self.assertNotIn("--delegate", argv)

    def test_general_and_custom_types(self):
        for t in ("general-purpose", None):
            argv = self.command_of(self.denied(self.agent(t)))
            self.assertEqual(argv[1:3], ["run", "general"])
            self.assertNotIn("--model", argv)
        argv = self.command_of(self.denied(self.agent("Code Reviewer")))
        self.assertEqual(argv[1:5], ["run", "code-reviewer", "--agent", "Code Reviewer"])

    def test_model_and_effort_mapped(self):
        argv = self.command_of(self.denied(self.agent("general-purpose", model="sonnet", effort="HIGH")))
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        argv = self.command_of(self.denied(self.agent("Explore", model="opus", effort="bogus")))
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertNotIn("--effort", argv)

    def test_prompt_file_name_sanitized(self):
        payload = {"tool_use_id": "../../etc/x", "tool_input": {"subagent_type": "Explore", "prompt": "p"}}
        r = self.run_hook("pre-agent", payload, env=self.herdr_env())
        argv = self.command_of(self.denied(r))
        prompt_file = Path(argv[argv.index("--prompt-file") + 1])
        self.assertEqual(prompt_file.parent, self.state_dir / "prompts")

    def test_old_prompt_files_pruned(self):
        d = self.state_dir / "prompts"
        d.mkdir(parents=True)
        old, recent, other = d / "toolu_old.md", d / "toolu_recent.md", d / "keep.txt"
        for f in (old, recent, other):
            f.write_text("x")
        stale = time.time() - 8 * 24 * 3600
        os.utime(old, (stale, stale))
        os.utime(other, (stale, stale))
        self.denied(self.agent("Explore"))
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(other.exists())
        self.assertTrue((d / "toolu_01ABC.md").exists())

    def test_allowed_cases(self):
        cases = {
            "outside herdr": self.make_env(),
            "no workspace": dict(self.herdr_env(), HERDR_WORKSPACE_ID=""),
            "redirect off": self.herdr_env(HERDR_AGENTS_REDIRECT="off"),
            "master off": self.herdr_env(HERDR_AGENTS="off"),
            "sdk": self.herdr_env(CLAUDE_CODE_ENTRYPOINT="sdk-cli"),
            "depth 1 no delegate": self.herdr_env(HERDR_AGENT_DEPTH="1"),
        }
        for name, env in cases.items():
            with self.subTest(name):
                self.assertEqual(self.agent("Explore", env=env).stdout, "")
        self.assertFalse((self.state_dir / "prompts").exists())

    def test_passthrough(self):
        for t in ("claude-code-guide", "statusline-setup"):
            self.assertEqual(self.agent(t).stdout, "")
        env = self.herdr_env(HERDR_AGENTS_PASSTHROUGH="Explore")
        self.assertEqual(self.agent("Explore", env=env).stdout, "")
        self.denied(self.agent("claude-code-guide", env=env))

    def test_depth_1_with_delegate_denied(self):
        env = self.herdr_env(HERDR_AGENT_DEPTH="1", HERDR_AGENT_MAY_DELEGATE="1")
        self.denied(self.agent("Explore", env=env))

    def test_garbage_fail_open(self):
        for data in ("nope", "[]", json.dumps({"tool_input": "x"})):
            r = self.run_hook("pre-agent", data, env=self.herdr_env())
            self.assertEqual((r.returncode, r.stdout), (0, ""))


class PreBashTest(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.bindir = self.home / ".local" / "bin"
        self.bindir.mkdir(parents=True)
        self.link = self.bindir / "herdr-agent"
        self.link.symlink_to(CLI)
        self.foreign = self.bindir / "other" / "herdr-agent"
        self.foreign.parent.mkdir()
        self.foreign.write_text("#!/bin/sh\n")
        self.foreign.chmod(0o755)

    def bash(self, command, mode="default", env=None):
        payload = {"session_id": "s1", "tool_name": "Bash", "permission_mode": mode,
                   "tool_input": {"command": command, "description": "x"}}
        r = self.run_hook("pre-bash", payload, env=env if env is not None else self.herdr_env())
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def assert_allowed(self, command, **kw):
        r = self.bash(command, **kw)
        self.assertTrue(r.stdout, f"not allowed: {command!r}")
        out = r.json()["hookSpecificOutput"]
        self.assertEqual(out, {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                               "permissionDecisionReason": "herdr-agent (herdr-agents plugin)"})

    def assert_silent(self, command, **kw):
        self.assertEqual(self.bash(command, **kw).stdout, "", f"allowed: {command!r}")

    def test_allowed_forms(self):
        cli = shlex.quote(str(CLI))
        for command in (
            f"{cli} run explore --model haiku --prompt-file /tmp/p.md",
            f"'{CLI}' status general-abc123",
            f"{cli} wait reviewer-1a2b3c --timeout 600",
            "~/.local/bin/herdr-agent list --json",
            f"{self.link} gc --dry-run",
            "\"$HERDR_AGENT\" run general -f \"$TMPDIR/p.md\"",
            "${HERDR_AGENT} path",
            f"{cli} send x -p 'a; b | c && $(d) `e` > f'",
            f"{cli} run general \\\n  --prompt-file /tmp/p.md",
        ):
            with self.subTest(command):
                self.assert_allowed(command)

    def test_bare_name_via_path(self):
        env = self.herdr_env()
        env["PATH"] = f"{self.bindir}:{env['PATH']}"
        self.assert_allowed("herdr-agent version --json", env=env)
        self.assert_silent("herdr-agent version --json")

    def test_rejected(self):
        cli = shlex.quote(str(CLI))
        for command in (
            f"{self.foreign} run general -p x",
            "/usr/bin/herdr-agent run general -p x",
            f"{cli} keys general-abc123 enter",
            f"{cli} setup",
            f"{cli} hook stop",
            f"{cli}",
            f"{cli} status x; rm -rf /tmp/y",
            f"{cli} status x && echo hi",
            f"{cli} status x | cat",
            f"{cli} status x & sleep 1",
            f"{cli} run general -p \"$(cat /etc/passwd)\"",
            f"{cli} run general -p `id`",
            f"{cli} run general -p \"`id`\"",
            f"{cli} status x > /tmp/out",
            f"{cli} status x 2>&1",
            f"{cli} run general -f <(echo x)",
            f"{cli} status x\nrm -rf /tmp/y",
            f"{cli} status ${{x:-y}}",
            f"{cli} status 'unterminated",
            f"FOO=1 {cli} status x",
            f"echo {cli} status x",
            f"cd /tmp && {cli} status x",
            "$OTHER status x",
        ):
            with self.subTest(command):
                self.assert_silent(command)

    def test_plan_mode(self):
        cli = shlex.quote(str(CLI))
        for command in (
            f"{cli} run explore --model haiku -f /tmp/p.md",
            f"{cli} run plan --delegate --model opus -f /tmp/p.md",
            f"{cli} spawn --model sonnet general --read-only -p x",
            f"{cli} run explore --permission-mode plan -p x",
            f"{cli} run --model haiku --agent Explore -p x",
            f"{cli} wait explore-abc123",
            f"{cli} status x",
            f"{cli} result x",
            f"{cli} list",
            f"{cli} screen x",
        ):
            with self.subTest(command):
                self.assert_allowed(command, mode="plan")
        for command in (
            f"{cli} run general -p x",
            f"{cli} run --model explore general -p x",
            f"{cli} run --mod explore general -p 'implement X'",
            f"{cli} run --prompt-f /tmp/p.md explore",
            f"{cli} run explore --read -p x",
            f"{cli} run explore --bogus -p x",
            f"{cli} run explore -p x -- --permission-mode bypassPermissions",
            f"{cli} run explore -p x -- --dangerously-skip-permissions",
            f"{cli} run explore -p x -- --disallowedTools ''",
            f"{cli} run plan -p x -- --allowedTools Edit",
            f"{cli} run explore -p x --permission-mode bypassPermissions",
            f"{cli} run explore --effort huge -p x",
            f"{cli} status",
            f"{cli} run general -p x -- --read-only",
            f"{cli} spawn reviewer -p x",
            f"{cli} send x -p y",
            f"{cli} close x",
            f"{cli} gc",
            f"{cli} keys x enter",
        ):
            with self.subTest(command):
                self.assert_silent(command, mode="plan")

    def test_plan_mode_vault_keeper_needs_read_only(self):
        cli = shlex.quote(str(CLI))
        self.assert_allowed(f"{cli} run vault-keeper --agent vault-keeper --read-only -f /tmp/p.md", mode="plan")
        self.assert_silent(f"{cli} run vault-keeper --agent vault-keeper -f /tmp/p.md", mode="plan")
        self.assert_silent(f"{cli} run vault-keeper -f /tmp/p.md", mode="plan")
        self.assert_allowed(f"{cli} run vault-keeper --agent vault-keeper -f /tmp/p.md")

    def test_plan_mode_vault_reviewer_read_only_allowed(self):
        cli = shlex.quote(str(CLI))
        for name in ("vault-reviewer", "agent-vault:vault-reviewer"):
            self.assert_allowed(f"{cli} run vault-reviewer --agent {name} --read-only -f /tmp/p.md", mode="plan")
        self.assert_silent(f"{cli} run vault-reviewer --agent vault-reviewer -f /tmp/p.md", mode="plan")

    def test_opt_outs(self):
        cli = shlex.quote(str(CLI))
        self.assert_silent(f"{cli} status x", env=self.herdr_env(HERDR_AGENTS_AUTO_ALLOW="off"))
        self.assert_silent(f"{cli} status x", env=self.herdr_env(HERDR_AGENTS="off"))
        self.assert_allowed(f"{cli} status x", env=self.make_env())

    def test_fast_path_and_garbage(self):
        self.assert_silent("ls -la")
        for data in ("herdr-agent not json", "[\"herdr-agent\"]", json.dumps({"tool_input": {"command": 5},
                                                                              "x": "herdr-agent"})):
            r = self.run_hook("pre-bash", data, env=self.herdr_env())
            self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_timing_under_100ms(self):
        payload = json.dumps({"permission_mode": "default",
                              "tool_input": {"command": f"{shlex.quote(str(CLI))} run explore -f /tmp/p.md"}})
        fast = json.dumps({"tool_input": {"command": "ls"}})
        for data, expect_out in ((payload, True), (fast, False)):
            best = None
            for _ in range(5):
                t0 = time.perf_counter()
                p = subprocess.run([sys.executable, str(CLI), "hook", "pre-bash"], input=data, env=self.herdr_env(),
                                   capture_output=True, text=True, timeout=30)
                elapsed = time.perf_counter() - t0
                self.assertEqual(bool(p.stdout), expect_out)
                best = elapsed if best is None else min(best, elapsed)
            self.assertLess(best, 0.1, f"pre-bash took {best * 1000:.0f} ms")


if __name__ == "__main__":
    unittest.main()
