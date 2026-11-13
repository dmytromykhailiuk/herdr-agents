import re
import shlex

from . import core

READ_ONLY_ROLES = ("explore", "plan")
READONLY_DISALLOWED = ("Edit", "Write", "NotebookEdit", "MultiEdit", "Agent", "Task", "EnterPlanMode", "ExitPlanMode")
DISALLOWED_FLAGS = ("--disallowedTools", "--disallowed-tools")

SYSTEM_PROMPT = (
    "You are a delegated sub-agent running in a separate Herdr tab, started by an "
    "orchestrating Claude session. Work autonomously on the task you are given. "
    "Your final message of each turn is captured verbatim and returned to the "
    "orchestrator, which cannot see your tool calls or intermediate output. Make "
    "that final message a complete, self-contained report: the answer or findings, "
    "relevant file paths and line numbers, what you changed, and anything left "
    "undone. Do not end a turn with a question unless you truly cannot proceed; "
    "state your assumptions instead. Do not leave background tasks running when "
    "you finish."
)

READONLY_PROMPT = (
    "This is read-only research: never modify files or any other state. Do not edit or create project "
    "files, install packages, commit, push or run commands that write; file-editing tools are disabled. "
    "Report your findings with file paths and line numbers."
)

PLAN_PROMPT = (
    "Your job is to plan, not to implement: return a step-by-step implementation plan, the critical files "
    "to change, the trade-offs you considered and the risks."
)

DELEGATE_TEMPLATE = (
    "You may split your work and delegate read-only subtasks (research, code exploration, analysis, "
    "option evaluation) to your own sub-agents with `{cli} run <role>` (role `explore` for read-only "
    "research, `plan` for read-only planning; see the herdr-agent skill; one background Bash call per "
    "parallel agent). Pick model and effort per subtask by the model/effort guidance in the herdr-agent "
    "skill. Your sub-agents cannot delegate further and must not edit project files: say so in their "
    "prompts. Give each a self-contained prompt and ask for a short summary plus file paths. Combine their "
    "results into your own final report. Delegate only when it pays off (broad or parallelizable work); do "
    "small parts yourself."
)


def delegate_prompt(cli_path=None):
    path = cli_path if cli_path is not None else core.CLI_PATH
    return DELEGATE_TEMPLATE.format(cli=shlex.quote(path) if path else "herdr-agent")


DELEGATE_PROMPT = delegate_prompt()


def sanitize_role(role):
    r = re.sub(r"[^a-z0-9-]+", "-", (role or "").lower())
    r = re.sub(r"-{2,}", "-", r).strip("-")
    r = re.sub(r"^[^a-z]+", "", r)
    return (r[:24].rstrip("-")) or "agent"


def is_read_only(role, read_only=False):
    return bool(read_only) or sanitize_role(role) in READ_ONLY_ROLES


def system_prompt(role, read_only=False, delegate=False, cli_path=None):
    parts = [SYSTEM_PROMPT]
    if is_read_only(role, read_only):
        parts.append(READONLY_PROMPT)
    if sanitize_role(role) == "plan":
        parts.append(PLAN_PROMPT)
    if delegate:
        parts.append(delegate_prompt(cli_path))
    return " ".join(parts)


def split_tool_list(value):
    items, depth, cur = [], 0, ""
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            items.append(cur)
            cur = ""
        else:
            cur += ch
    items.append(cur)
    return [i.strip() for i in items if i.strip()]


def extract_disallowed(user_args):
    """(user args without any --disallowedTools option, the tools those options listed)."""
    rest, tools = [], []
    i = 0
    while i < len(user_args):
        a = user_args[i]
        flag, _, inline = a.partition("=")
        if flag in DISALLOWED_FLAGS:
            if inline or "=" in a:
                tools += split_tool_list(inline)
                i += 1
                continue
            i += 1
            while i < len(user_args) and not user_args[i].startswith("-"):
                tools += split_tool_list(user_args[i])
                i += 1
            continue
        rest.append(a)
        i += 1
    return rest, tools


def merge_disallowed(base, user_args):
    """Merge our disallowed tools with the user's into one comma-joined --disallowedTools."""
    rest, user_tools = extract_disallowed(user_args)
    merged = []
    for t in list(base) + user_tools:
        if t not in merged:
            merged.append(t)
    return rest, merged


def has_permission_override(user_args):
    return any(a == "--permission-mode" or a.startswith("--permission-mode=")
               or a == "--dangerously-skip-permissions" for a in user_args)


def build_claude_args(*, settings_path, session_id, name, role, model=None, effort=None,
                      permission_mode="auto", read_only=False, agent=None, delegate=False,
                      no_system_prompt=False, user_args=(), cli_path=None):
    user_args = list(user_args)
    args = ["--settings", settings_path, "--session-id", session_id, "--name", name]
    if agent:
        args += ["--agent", agent]
    if model:
        args += ["--model", model]
    if effort:
        args += ["--effort", effort]
    if permission_mode and not has_permission_override(user_args):
        args += ["--permission-mode", permission_mode]
    if is_read_only(role, read_only):
        user_args, disallowed = merge_disallowed(READONLY_DISALLOWED, user_args)
        args += ["--disallowedTools", ",".join(disallowed)]
    if not no_system_prompt:
        args += ["--append-system-prompt", system_prompt(role, read_only, delegate, cli_path)]
    return args + user_args
