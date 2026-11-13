import sys
import unittest

from _base import LIB, IsolatedTestCase

sys.path.insert(0, str(LIB))

from herdragent import roles  # noqa: E402

RO = "Edit,Write,NotebookEdit,MultiEdit,Agent,Task,EnterPlanMode,ExitPlanMode"


def build(role="worker", **kw):
    base = dict(settings_path="/s.json", session_id="sid", name=f"{roles.sanitize_role(role)}-abc123", role=role,
                model="sonnet", effort="medium", cli_path="/opt/x y/herdr-agent")
    base.update(kw)
    return roles.build_claude_args(**base)


def flag_values(args, flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


class RolesTest(unittest.TestCase):
    def test_default_role(self):
        args = build()
        self.assertEqual(args[:6], ["--settings", "/s.json", "--session-id", "sid", "--name", "worker-abc123"])
        self.assertEqual(flag_values(args, "--permission-mode"), ["auto"])
        self.assertNotIn("--disallowedTools", args)
        self.assertNotIn("--agent", args)
        prompt = flag_values(args, "--append-system-prompt")[0]
        self.assertEqual(prompt, roles.SYSTEM_PROMPT)

    def test_explore_is_read_only(self):
        args = build("explore")
        self.assertEqual(flag_values(args, "--disallowedTools"), [RO])
        self.assertEqual(flag_values(args, "--permission-mode"), ["auto"])
        prompt = flag_values(args, "--append-system-prompt")[0]
        self.assertIn(roles.READONLY_PROMPT, prompt)
        self.assertNotIn(roles.PLAN_PROMPT, prompt)

    def test_plan_is_read_only_with_plan_prompt(self):
        args = build("Plan")
        self.assertEqual(flag_values(args, "--disallowedTools"), [RO])
        prompt = flag_values(args, "--append-system-prompt")[0]
        self.assertIn(roles.READONLY_PROMPT, prompt)
        self.assertIn(roles.PLAN_PROMPT, prompt)
        self.assertNotIn("plan", flag_values(args, "--permission-mode"))

    def test_read_only_flag_on_any_role(self):
        args = build("reviewer", read_only=True)
        self.assertEqual(flag_values(args, "--disallowedTools"), [RO])
        self.assertIn(roles.READONLY_PROMPT, flag_values(args, "--append-system-prompt")[0])

    def test_agent_flag(self):
        args = build("code-reviewer", agent="code-reviewer")
        self.assertEqual(flag_values(args, "--agent"), ["code-reviewer"])

    def test_user_permission_mode_wins(self):
        for user in (["--permission-mode", "plan"], ["--permission-mode=default"], ["--dangerously-skip-permissions"]):
            args = build("explore", user_args=user)
            self.assertEqual(args[-len(user):], user)
            self.assertEqual(args.count("--permission-mode") + sum(a.startswith("--permission-mode=") for a in args),
                             1 if "--dangerously-skip-permissions" not in user else 0)

    def test_empty_permission_mode(self):
        self.assertNotIn("--permission-mode", build(permission_mode=""))

    def test_disallowed_merge(self):
        args = build("explore", user_args=["--disallowedTools", "Bash(git push:*)", "WebFetch", "--model", "x"])
        self.assertEqual(flag_values(args, "--disallowedTools"), [RO + ",Bash(git push:*),WebFetch"])
        self.assertEqual(args[-2:], ["--model", "x"])
        args = build("plan", user_args=["--disallowed-tools=Edit,Bash(rm a,b)"])
        self.assertEqual(flag_values(args, "--disallowedTools"), [RO + ",Bash(rm a,b)"])
        self.assertNotIn("--disallowed-tools=Edit,Bash(rm a,b)", args)

    def test_read_only_kept_against_user_overrides(self):
        args = build("explore", user_args=["--allowedTools", "Edit", "--disallowedTools", ""])
        self.assertEqual(flag_values(args, "--disallowedTools"), [RO])
        self.assertEqual(args[-2:], ["--allowedTools", "Edit"])

    def test_disallowed_untouched_without_preset(self):
        user = ["--disallowedTools", "Bash", "WebFetch"]
        self.assertEqual(build(user_args=user)[-3:], user)

    def test_delegate_prompt_embeds_cli_path(self):
        prompt = flag_values(build("plan", delegate=True), "--append-system-prompt")[0]
        self.assertIn("`'/opt/x y/herdr-agent' run <role>`", prompt)
        self.assertIn("herdr-agent skill", prompt)
        self.assertNotIn("`orchestrator` skill", prompt)
        self.assertIn("Agent", flag_values(build("plan", delegate=True), "--disallowedTools")[0])

    def test_no_system_prompt(self):
        self.assertNotIn("--append-system-prompt", build("explore", no_system_prompt=True))

    def test_sanitize_role(self):
        self.assertEqual(roles.sanitize_role("Code Reviewer!"), "code-reviewer")
        self.assertEqual(roles.sanitize_role("123"), "agent")
        self.assertEqual(roles.sanitize_role("plugin:my-agent"), "plugin-my-agent")


class RolesCliTest(IsolatedTestCase):
    def argv_of_started_agent(self, *args):
        r = self.run_cli("spawn", "--prompt", "hi", *args, env=self.herdr_env())
        self.assertEqual(r.returncode, 0, r.stderr)
        aid = r.stdout.strip()
        pane = next(p for p in self.fake_panes().values() if p.get("agent_name") == aid)
        return aid, pane["process_argv"]

    def test_explore_argv(self):
        aid, argv = self.argv_of_started_agent("explore", "--", "--disallowedTools", "WebFetch")
        self.assertTrue(aid.startswith("explore-"))
        self.assertEqual(flag_values(argv, "--disallowedTools"), [RO + ",WebFetch"])

    def test_agent_defaults_role(self):
        aid, argv = self.argv_of_started_agent("--agent", "Code Reviewer", "--read-only")
        self.assertTrue(aid.startswith("code-reviewer-"))
        self.assertEqual(flag_values(argv, "--agent"), ["Code Reviewer"])
        self.assertEqual(flag_values(argv, "--disallowedTools"), [RO])

    def test_abbreviated_options_rejected(self):
        r = self.run_cli("spawn", "--prompt", "hi", "--mod", "explore", "general", env=self.herdr_env())
        self.assertEqual(r.returncode, 2)
        self.assertIn("unrecognized arguments", r.stderr)
        self.assertEqual(self.fake_calls(mutating=True), [])

    def test_role_or_agent_required(self):
        r = self.run_cli("spawn", "--prompt", "hi", env=self.herdr_env())
        self.assertEqual(r.returncode, 2)
        self.assertIn("role is required", r.stderr)


if __name__ == "__main__":
    unittest.main()
