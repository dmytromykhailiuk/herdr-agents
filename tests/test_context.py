import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _base import LIB, REPO  # noqa: E402

sys.path.insert(0, str(LIB))

from herdragent import context, core  # noqa: E402

CLI = "/opt/plug ins/herdr-agents/0.1.0/skills/herdr-agent/bin/herdr-agent"
QUOTED = "'" + CLI + "'"
PLAIN_CLI = "/opt/herdr-agents/bin/herdr-agent"
FORBIDDEN = re.compile(r"vault|vloop|verify-loop|taskflow|/task-|-claude\b|~/\.claude/skills", re.I)


class OrchestratorSkillTest(unittest.TestCase):
    def setUp(self):
        self.text = (REPO / "skills" / "orchestrator" / "SKILL.md").read_text(encoding="utf-8")

    def test_frontmatter(self):
        self.assertTrue(self.text.startswith("---\nname: orchestrator\ndescription: "))

    def test_generic(self):
        self.assertIsNone(FORBIDDEN.search(self.text))
        self.assertNotIn("Lessons from past retros", self.text)

    def test_placeholder_and_plan_mode(self):
        self.assertIn("{{HERDR_AGENT}} run explore", self.text)
        self.assertIn("{{HERDR_AGENT}} run plan --delegate", self.text)
        for heading in ("## Model and effort", "## Decomposition", "## Prompt contract", "## Context hygiene",
                        "## Opt-out", "## Plan mode"):
            self.assertIn(heading, self.text)


class HerdrAgentSkillTest(unittest.TestCase):
    def setUp(self):
        self.text = (REPO / "skills" / "herdr-agent" / "SKILL.md").read_text(encoding="utf-8")

    def test_frontmatter(self):
        self.assertTrue(self.text.startswith("---\nname: herdr-agent\ndescription: "))

    def test_generic(self):
        self.assertIsNone(FORBIDDEN.search(self.text))
        self.assertNotIn("{{HERDR_AGENT}}", self.text)

    def test_documents_features(self):
        for needle in ("`explore`", "`plan`", "--read-only", "--agent NAME", "## Plan mode", "$HERDR_AGENT",
                       "herdr-agent path", "## Model and effort", "| 7 | api_error", "herdr status", "doctor"):
            self.assertIn(needle, self.text)


class OrchestratorBodyTest(unittest.TestCase):
    def test_strips_frontmatter_and_substitutes(self):
        body = context.orchestrator_body(CLI)
        self.assertTrue(body.startswith("# Orchestrator mode"))
        self.assertNotIn("name: orchestrator", body)
        self.assertNotIn("{{HERDR_AGENT}}", body)
        self.assertIn(QUOTED + " run explore", body)

    def test_plain_path_unquoted(self):
        self.assertIn(PLAIN_CLI + " run plan --delegate", context.orchestrator_body(PLAIN_CLI))

    def test_missing_skill_file(self):
        self.assertEqual(context.orchestrator_body(CLI, "/nonexistent/SKILL.md"), "")

    def test_custom_skill_without_frontmatter(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "SKILL.md"
            p.write_text("\n  use {{HERDR_AGENT}} now \n", encoding="utf-8")
            self.assertEqual(context.orchestrator_body(PLAIN_CLI, p), f"use {PLAIN_CLI} now")


class HerdrSectionTest(unittest.TestCase):
    def test_contents(self):
        text = context.herdr_section(CLI)
        self.assertTrue(text.startswith("# Herdr sub-agents"))
        self.assertIn(QUOTED + " run <role>", text)
        self.assertIn(QUOTED + " run explore --model <m> --prompt-file <file>", text)
        self.assertIn(QUOTED + " run plan --delegate --model opus --prompt-file <file>", text)
        self.assertIn("run_in_background: true", text)
        self.assertIn("folder-trust", text)
        self.assertIn("does not violate plan mode", text)
        self.assertIsNone(FORBIDDEN.search(text))

    def test_plan_mode_section_in_herdr_section(self):
        self.assertIn(context.plan_mode_section(CLI), context.herdr_section(CLI))


class BuildContextTest(unittest.TestCase):
    def build(self, **env):
        return context.build_context(env=env, cli_path=CLI)

    def test_matrix(self):
        body = context.orchestrator_body(CLI)
        section = context.herdr_section(CLI)
        self.assertEqual(self.build(), body)
        self.assertEqual(self.build(HERDR_ENV="1"), body + "\n\n" + section)
        for off in ("off", "0", "false", "no", "OFF"):
            self.assertEqual(self.build(CLAUDE_ORCHESTRATOR=off, HERDR_ENV="1"), section)
            self.assertEqual(self.build(CLAUDE_ORCHESTRATOR=off), "")
        self.assertEqual(self.build(CLAUDE_ORCHESTRATOR="on", HERDR_ENV="0"), body)
        self.assertEqual(self.build(CLAUDE_ORCHESTRATOR="", HERDR_ENV="1"), body + "\n\n" + section)

    def test_no_placeholder_left(self):
        self.assertNotIn("{{HERDR_AGENT}}", self.build(HERDR_ENV="1"))

    def test_missing_skill_file_keeps_herdr_section(self):
        text = context.build_context(env={"HERDR_ENV": "1"}, cli_path=CLI, skill_path="/nonexistent/SKILL.md")
        self.assertEqual(text, context.herdr_section(CLI))

    def test_default_cli_path_from_core(self):
        saved = core.CLI_PATH
        try:
            core.CLI_PATH = PLAIN_CLI
            self.assertIn(f"`{PLAIN_CLI}`", context.build_context(env={"HERDR_ENV": "1"}))
            core.CLI_PATH = ""
            fallback = str(REPO / "skills" / "herdr-agent" / "bin" / "herdr-agent")
            self.assertEqual(context.default_cli_path(), fallback)
        finally:
            core.CLI_PATH = saved


if __name__ == "__main__":
    unittest.main()
