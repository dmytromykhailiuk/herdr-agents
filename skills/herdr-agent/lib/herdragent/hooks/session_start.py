import os
import shlex
import subprocess
import time

from .. import core
from . import common

INJECT_SOURCES = ("startup", "clear", "compact")
BUDGET_S = 2.0
PS_LEVELS = 4


def is_main_session(payload, env):
    if core.agent_depth(env) != 0 or common.is_sdk(env):
        return False
    return not payload.get("agent_id") and not payload.get("agent_type")


def export_line(cli):
    return f"export HERDR_AGENT={shlex.quote(cli)}\n"


def write_env_export(env, cli):
    path = env.get("CLAUDE_ENV_FILE")
    if not path or not core.in_herdr(env):
        return
    line = export_line(cli)
    try:
        with open(path, encoding="utf-8") as f:
            if line.strip() in (l.strip() for l in f):
                return
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def inject(payload, env, cli):
    if payload.get("source", "startup") not in INJECT_SOURCES or not is_main_session(payload, env):
        return
    from ..context import build_context
    text = build_context(env=env, cli_path=cli)
    if text:
        common.emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})


def is_claude(argv0):
    return os.path.basename(argv0 or "") == "claude"


def claude_args(argv):
    if not argv:
        return None
    if is_claude(argv[0]):
        return list(argv[1:])
    if len(argv) > 1 and os.path.basename(argv[0]).startswith(("node", "bun")) and is_claude(argv[1]):
        return list(argv[2:])
    return None


def argv_from_herdr(pane_id, timeout):
    from ..herdr import HerdrError, process_info
    try:
        info = process_info(pane_id, timeout=max(timeout, 0.1))
    except HerdrError:
        return None
    for proc in info.get("foreground_processes") or []:
        if not isinstance(proc, dict):
            continue
        argv = proc.get("argv")
        if isinstance(argv, list) and (is_claude(proc.get("name")) or claude_args(argv) is not None):
            args = claude_args(argv)
            return args if args is not None else list(argv[1:])
    return None


def ps_field(pid, field, timeout):
    try:
        p = subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True,
                           timeout=max(timeout, 0.1))
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def argv_from_ps(deadline):
    pid = os.getppid()
    for _ in range(PS_LEVELS):
        if pid <= 1 or time.monotonic() >= deadline:
            return None
        args = ps_field(pid, "args", deadline - time.monotonic())
        try:
            argv = shlex.split(args)
        except ValueError:
            argv = args.split()
        found = claude_args(argv)
        if found is not None:
            return found
        try:
            pid = int(ps_field(pid, "ppid", deadline - time.monotonic()) or 0)
        except ValueError:
            return None
    return None


def should_record(payload, env):
    return (core.in_herdr(env) and env.get("HERDR_PANE_ID") and not env.get("HERDR_AGENT_ID")
            and not common.is_sdk(env) and not core.env_off("HERDR_AGENTS_RECORD", env=env)
            and payload.get("session_id"))


def record(payload, env, started):
    if not should_record(payload, env):
        return
    deadline = started + BUDGET_S
    pane_id = env["HERDR_PANE_ID"]
    argv = argv_from_herdr(pane_id, deadline - time.monotonic())
    if argv is None:
        argv = argv_from_ps(deadline)
    if argv is None:
        return
    config_dir = env.get("CLAUDE_CONFIG_DIR") or ""
    if config_dir and os.path.realpath(os.path.expanduser(config_dir)) == os.path.realpath(core.home() / ".claude"):
        config_dir = ""
    from .. import launches
    launches.record_session(
        payload["session_id"], argv, payload.get("cwd") or os.getcwd(),
        pane_id=pane_id, workspace_id=env.get("HERDR_WORKSPACE_ID"), tab_id=env.get("HERDR_TAB_ID"),
        tab_env={"CLAUDE_CONFIG_DIR": config_dir} if config_dir else None,
        claude_config_dir=config_dir or None, session_env=env,
    )


def run(env):
    started = time.monotonic()
    if common.hooks_off(env):
        return
    payload = common.parse_payload(common.read_stdin())
    if payload is None:
        return
    cli = common.cli_path()
    for step in (lambda: write_env_export(env, cli), lambda: inject(payload, env, cli),
                 lambda: record(payload, env, started)):
        try:
            step()
        except Exception as e:
            core.log(f"session-start: {e!r}")


def main(argv=None):
    try:
        run(os.environ)
    except Exception as e:
        core.log(f"session-start: {e!r}")
    return 0
