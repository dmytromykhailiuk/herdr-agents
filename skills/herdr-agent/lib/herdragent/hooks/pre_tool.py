import os
import re
import time

from .. import core
from . import common

DEFAULT_PASSTHROUGH = "claude-code-guide,statusline-setup"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MODELS = {"explore": "haiku", "plan": "opus"}
PROMPT_MAX_AGE_S = 7 * 24 * 3600

ALLOWED_SUBCOMMANDS = frozenset(("run", "spawn", "wait", "send", "status", "result", "list", "screen", "close", "gc",
                                 "path", "version"))
PLAN_MODE_READ = frozenset(("wait", "status", "result", "list", "screen", "path", "version"))
UNQUOTED_FORBIDDEN = set(";&|<>()`\n\r")
VAR_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})")
CLI_VAR_RE = re.compile(r"^\$(?:HERDR_AGENT|\{HERDR_AGENT\})$")

VAULT_SEARCH_RE = re.compile(r"^\s*Operation:\s*search\b", re.IGNORECASE | re.MULTILINE)

ALLOW_REASON = "herdr-agent (herdr-agents plugin)"


def deny_reason(command):
    return ("Inside Herdr, sub-agents run through herdr-agent, not the Agent tool (this includes plan mode). "
            "Run this Bash command instead (run_in_background: true; one Bash call per agent for parallel work): "
            f"{command}. The explore/plan roles are read-only (Edit/Write/NotebookEdit disabled) and are the "
            "plan-mode equivalents of the built-in Explore/Plan agents. Its stdout is the agent's final report.")


def passthrough_types(env):
    raw = env.get("HERDR_AGENTS_PASSTHROUGH")
    raw = DEFAULT_PASSTHROUGH if raw is None else raw
    return {t.strip() for t in raw.split(",") if t.strip()}


def should_redirect(tool_input, env):
    if not core.in_herdr(env) or not env.get("HERDR_WORKSPACE_ID"):
        return False
    if core.env_off("HERDR_AGENTS_REDIRECT", env=env) or common.is_sdk(env):
        return False
    depth = core.agent_depth(env)
    if depth != 0 and not (depth >= 1 and env.get("HERDR_AGENT_MAY_DELEGATE") == "1"):
        return False
    return (tool_input.get("subagent_type") or "") not in passthrough_types(env)


def role_args(subagent_type, env):
    from ..roles import sanitize_role
    t = (subagent_type or "").strip()
    if t == "Explore":
        return "explore", ["explore"]
    if t == "Plan":
        if core.agent_depth(env) + 1 < core.max_depth(env):
            return "plan", ["plan", "--delegate"]
        return "plan", ["plan"]
    if not t or t == "general-purpose":
        return "general", ["general"]
    role = sanitize_role(t.rsplit(":", 1)[-1])
    return role, [role, "--agent", t]


def safe_name(value):
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or ""))[:96].strip("._")
    if not name:
        import uuid
        name = uuid.uuid4().hex
    return name


def write_prompt(payload, tool_input, env):
    prompt = tool_input.get("prompt")
    prompt = prompt if isinstance(prompt, str) else ""
    path = core.state_dir(env) / "prompts" / f"{safe_name(payload.get('tool_use_id'))}.md"
    core.atomic_write(path, prompt, mode=0o600)
    prune_prompts(path.parent, keep=path)
    return str(path)


def prune_prompts(directory, keep=None, max_age_s=PROMPT_MAX_AGE_S, now=None):
    cutoff = (time.time() if now is None else now) - max_age_s
    removed = []
    for old in directory.glob("*.md"):
        try:
            if old != keep and old.stat().st_mtime < cutoff:
                old.unlink()
                removed.append(old.name)
        except OSError:
            pass
    return removed


def pre_agent(payload, env):
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or not should_redirect(tool_input, env):
        return
    import shlex
    role, args = role_args(tool_input.get("subagent_type"), env)
    if role == "vault-reviewer" or (
            role == "vault-keeper" and payload.get("permission_mode") == "plan"
            and VAULT_SEARCH_RE.search(str(tool_input.get("prompt") or ""))):
        args.append("--read-only")
    model = tool_input.get("model")
    model = model.strip() if isinstance(model, str) and model.strip() else DEFAULT_MODELS.get(role)
    if model:
        args += ["--model", model]
    effort = tool_input.get("effort")
    if isinstance(effort, str) and effort.strip().lower() in EFFORTS:
        args += ["--effort", effort.strip().lower()]
    prompt_file = write_prompt(payload, tool_input, env)
    command = " ".join([shlex.quote(common.cli_path()), "run", *(shlex.quote(a) for a in args),
                        "--prompt-file", shlex.quote(prompt_file)])
    common.emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": deny_reason(command),
    }})


def has_unsafe_syntax(command):
    quote = None
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if quote == "'":
            if c == "'":
                quote = None
            i += 1
            continue
        if c == "\\":
            if quote is None and i + 1 < n and command[i + 1] in "\n\r":
                i += 2
                continue
            i += 2
            continue
        if c == "$":
            m = VAR_RE.match(command, i)
            if not m:
                return True
            i = m.end()
            continue
        if c == "`":
            return True
        if quote == '"':
            if c == '"':
                quote = None
        elif c in "'\"":
            quote = c
        elif c in UNQUOTED_FORBIDDEN:
            return True
        i += 1
    return quote is not None


def resolve_argv0(token, env):
    import shutil
    if CLI_VAR_RE.match(token):
        token = env.get("HERDR_AGENT") or common.cli_path()
    if "$" in token:
        return None
    if token.startswith("~"):
        home = env.get("HOME") or str(core.home())
        if token == "~" or token.startswith("~/"):
            token = home + token[1:]
        else:
            return None
    if "/" not in token:
        token = shutil.which(token, path=env.get("PATH")) or ""
        if not token:
            return None
    return os.path.realpath(token)


PLAN_MODE_PERMISSION_MODES = frozenset(("", "auto", "default", "plan"))


def parse_cli(argv):
    import contextlib
    import io

    from ..cli import build_parser, split_extra
    args, extra = split_extra(list(argv))
    sink = io.StringIO()
    try:
        with contextlib.redirect_stderr(sink), contextlib.redirect_stdout(sink):
            ns = build_parser().parse_args(args)
    except SystemExit:
        return None, extra
    return ns, extra


def plan_mode_allows(sub, args):
    ns, extra = parse_cli([sub, *args])
    if ns is None or ns.cmd != sub:
        return False
    if sub in PLAN_MODE_READ:
        return True
    if sub not in ("run", "spawn") or extra:
        return False
    if (ns.permission_mode or "") not in PLAN_MODE_PERMISSION_MODES:
        return False
    from ..roles import is_read_only
    role = ns.role or ns.agent
    return bool(role or ns.read_only) and is_read_only(role or "", ns.read_only)


def bash_allowed(payload, env):
    if core.env_off("HERDR_AGENTS_AUTO_ALLOW", env=env):
        return False
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip() or has_unsafe_syntax(command):
        return False
    import shlex
    try:
        argv = shlex.split(command.replace("\\\n", " "))
    except ValueError:
        return False
    if len(argv) < 2:
        return False
    resolved = resolve_argv0(argv[0], env)
    if not resolved or resolved not in common.cli_realpaths():
        return False
    sub = argv[1]
    if sub not in ALLOWED_SUBCOMMANDS:
        return False
    if payload.get("permission_mode") == "plan":
        return plan_mode_allows(sub, argv[2:])
    return True


def pre_bash(raw, env):
    if "herdr-agent" not in raw and "HERDR_AGENT" not in raw:
        return
    payload = common.parse_payload(raw)
    if payload is None or not bash_allowed(payload, env):
        return
    common.emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": ALLOW_REASON,
    }})


def main(argv=None):
    argv = list(argv or [])
    name = argv[0] if argv else ""
    env = os.environ
    try:
        if common.hooks_off(env):
            return 0
        raw = common.read_stdin()
        if name == "pre-bash":
            pre_bash(raw, env)
        elif name == "pre-agent":
            payload = common.parse_payload(raw)
            if payload is not None:
                pre_agent(payload, env)
        else:
            return 1
    except Exception as e:
        core.log(f"{name or 'pre-tool'}: {e!r}")
    return 0
