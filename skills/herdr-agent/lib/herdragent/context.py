import os
import shlex
from pathlib import Path

from . import core

PLACEHOLDER = "{{HERDR_AGENT}}"

_LIB_PKG = Path(__file__).resolve().parent
ORCHESTRATOR_SKILL = _LIB_PKG.parents[2] / "orchestrator" / "SKILL.md"
DEFAULT_CLI = _LIB_PKG.parents[1] / "bin" / "herdr-agent"


def default_cli_path():
    return core.CLI_PATH or str(DEFAULT_CLI)


def shell_cli(cli_path):
    return shlex.quote(str(cli_path))


def strip_frontmatter(text):
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


def orchestrator_body(cli_path=None, skill_path=None):
    path = Path(skill_path) if skill_path else ORCHESTRATOR_SKILL
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    body = strip_frontmatter(text)
    return body.replace(PLACEHOLDER, shell_cli(cli_path or default_cli_path()))


def plan_mode_section(cli_path=None):
    cli = shell_cli(cli_path or default_cli_path())
    return "\n".join([
        "Plan mode (inside Herdr):",
        f"- Explore → `{cli} run explore --model <m> --prompt-file <file>`",
        f"- Plan → `{cli} run plan --delegate --model opus --prompt-file <file>`",
        "- Both roles are read-only (Edit, Write, NotebookEdit and the Agent tool are disabled) and are the plan-mode "
        "equivalents of the built-in Explore and Plan agents; running them does not violate plan mode. Write the plan "
        "file yourself from the result, then call ExitPlanMode.",
    ])


def herdr_section(cli_path=None):
    cli = shell_cli(cli_path or default_cli_path())
    return "\n".join([
        "# Herdr sub-agents",
        "",
        f"herdr-agent CLI: `{cli}` (also `$HERDR_AGENT` in Bash; usage in the `herdr-agent` skill).",
        "",
        f"- Inside Herdr, run every sub-agent with `{cli} run <role> --model <m> --effort <e> --prompt-file <file>`, "
        "never the built-in Agent tool. Each agent gets its own tab that closes when it finishes; its stdout is the "
        "agent's final report. The Agent tool is blocked by a hook here.",
        "- Parallel agents: one Bash call per agent, each with `run_in_background: true`.",
        "- Never answer a sub-agent's permission or folder-trust dialog yourself (exit code 3); show it to the user "
        "and ask.",
        "- Before the first agent in a folder Claude Code has never opened, ask the user to run `claude` there once "
        "and accept the folder-trust dialog.",
        "",
        plan_mode_section(cli_path),
    ])


def build_context(env=None, cli_path=None, skill_path=None):
    env = env if env is not None else os.environ
    cli_path = cli_path or default_cli_path()
    parts = []
    if not core.env_off("CLAUDE_ORCHESTRATOR", env=env):
        body = orchestrator_body(cli_path, skill_path)
        if body:
            parts.append(body)
    if core.in_herdr(env):
        parts.append(herdr_section(cli_path))
    return "\n\n".join(parts)
