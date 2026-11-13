import datetime as dt
import json
import os
import re
import secrets
import shlex
import sys
import threading
import time
import uuid

from . import core, roles
from .core import EXIT_BLOCKED, EXIT_USAGE, die, log
from .herdr import HerdrError, agent_info, herdr, screen, start_watchdog, wait_shell_ready
from .results import agent_dir, cli_ref, read_result, save_meta, transcript_path_for
from .roles import sanitize_role
from .trust import claude_invocation, ensure_trusted


def launches_call(name, *args, **kwargs):
    try:
        from . import launches
    except ImportError:
        return None
    try:
        return getattr(launches, name)(*args, **kwargs)
    except Exception as e:
        log(f"launch record {name} failed: {e!r}")
        return None


def require_herdr():
    if os.environ.get("HERDR_ENV") != "1":
        die("not running inside a Herdr pane (HERDR_ENV != 1)")


def read_prompt(args):
    if getattr(args, "prompt_file", None):
        with open(args.prompt_file, encoding="utf-8") as f:
            text = f.read()
    elif getattr(args, "prompt", None) == "-":
        text = sys.stdin.read()
    else:
        text = getattr(args, "prompt", None) or ""
    if not text.strip():
        die("empty prompt (use --prompt TEXT, --prompt - for stdin, or --prompt-file PATH)", EXIT_USAGE)
    return text


def claim_id(role):
    """Pick an unused id and atomically create its directory."""
    os.makedirs(str(core.agent_root()), exist_ok=True)
    for _ in range(8):
        aid = f"{role}-{secrets.token_hex(3)}"
        try:
            os.mkdir(agent_dir(aid))
        except FileExistsError:
            continue
        if agent_info(aid) is None:
            return aid
        os.rmdir(agent_dir(aid))
    die("could not allocate a unique agent id")


def stop_hook_command(cli_path=None):
    return f"{shlex.quote(sys.executable)} {shlex.quote(cli_path or core.CLI_PATH)} hook stop"


def agent_settings(cli_path=None):
    return {"hooks": {"Stop": [{"hooks": [{
        "type": "command",
        "command": stop_hook_command(cli_path),
        "timeout": 30,
    }]}]}}


def write_settings(d, cli_path=None):
    path = os.path.join(d, "settings.json")
    core.atomic_write(path, json.dumps(agent_settings(cli_path), indent=2))
    return path


def validate_env(pairs):
    for kv in pairs or []:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", kv):
            die(f"invalid --env {kv!r} (expected KEY=VAL)", EXIT_USAGE)
        if kv.upper().startswith("HERDR_"):
            die(f"--env {kv.split('=', 1)[0]} is reserved (HERDR_* is set by herdr-agent itself)", EXIT_USAGE)


def resolve_role(args):
    role = getattr(args, "role", None) or getattr(args, "agent", None)
    if not role:
        die("a role is required (positional ROLE or --agent NAME)", EXIT_USAGE)
    return role


def do_spawn(args, prompt):
    require_herdr()
    depth, max_depth = core.agent_depth(), core.max_depth()
    if depth >= max_depth:
        die(f"spawn depth limit reached (depth {depth} >= HERDR_AGENT_MAX_DEPTH {max_depth})")
    if depth >= 1 and os.environ.get("HERDR_AGENT_MAY_DELEGATE") != "1":
        die("this agent may not spawn sub-agents: its parent did not start it with --delegate")
    if args.delegate and depth + 1 >= max_depth:
        die(f"--delegate: the new agent would sit at the depth limit (HERDR_AGENT_MAX_DEPTH {max_depth})",
            EXIT_USAGE)
    role = resolve_role(args)
    cwd = os.path.realpath(os.path.expanduser(args.cwd or os.getcwd()))
    if not os.path.isdir(cwd):
        die(f"cwd does not exist: {cwd}", EXIT_USAGE)
    if not ensure_trusted(cwd):
        die(f"cwd is not trusted by Claude Code: {cwd}\n"
            f"An agent started there would lose its flags after the trust dialog. Ask the user to "
            f"run `{claude_invocation(os.environ.get('CLAUDE_CONFIG_DIR'))}` in that folder once and accept the "
            f"trust dialog, then retry.", EXIT_BLOCKED)
    workspace = args.workspace or os.environ.get("HERDR_WORKSPACE_ID")
    if not workspace:
        die("no workspace (HERDR_WORKSPACE_ID unset; pass --workspace)", EXIT_USAGE)
    validate_env(args.env)

    read_only = roles.is_read_only(role, args.read_only)
    aid = claim_id(sanitize_role(role))
    d = agent_dir(aid)
    session_id = str(uuid.uuid4())
    meta = {
        "id": aid, "role": role, "cwd": cwd, "workspace_id": workspace,
        "session_id": session_id, "created_at": time.time(), "status": "starting",
        "parent_pane_id": os.environ.get("HERDR_PANE_ID"), "depth": depth + 1,
        "model": args.model, "effort": args.effort, "delegate": bool(args.delegate),
        "read_only": read_only, "agent": args.agent,
        "created_tab": False, "last_prompt_at_ns": 0,
        "parent_session_id": os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        "claude_config_dir": os.environ.get("CLAUDE_CONFIG_DIR", ""),
    }
    save_meta(meta)
    core.atomic_write(os.path.join(d, "prompt.md"), prompt)
    settings_path = write_settings(d)

    tab_env = {
        "HERDR_AGENT_DIR": d,
        "HERDR_AGENT_ID": aid,
        "HERDR_AGENT_DEPTH": str(depth + 1),
        "HERDR_PARENT_SESSION_ID": os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
    }
    if meta["claude_config_dir"]:
        tab_env["CLAUDE_CONFIG_DIR"] = meta["claude_config_dir"]
    if args.delegate:
        tab_env["HERDR_AGENT_MAY_DELEGATE"] = "1"
    for kv in args.env or []:
        k, v = kv.split("=", 1)
        tab_env[k] = v

    try:
        res = herdr("tab", "create", "--workspace", workspace, "--cwd", cwd, "--label", aid,
                    *[a for k, v in tab_env.items() for a in ("--env", f"{k}={v}")],
                    "--focus" if args.focus else "--no-focus", timeout=30)
    except HerdrError as e:
        meta.update(status="error", error=str(e))
        save_meta(meta)
        die(f"tab create failed: {e}")
    meta.update(tab_id=res["tab"]["tab_id"], pane_id=res["root_pane"]["pane_id"], created_tab=True)
    save_meta(meta)

    claude_args = roles.build_claude_args(
        settings_path=settings_path, session_id=session_id, name=aid, role=role,
        model=args.model, effort=args.effort, permission_mode=args.permission_mode,
        read_only=args.read_only, agent=args.agent, delegate=args.delegate,
        no_system_prompt=args.no_system_prompt, user_args=args.claude_args,
    )
    meta["claude_args"] = claude_args
    if not wait_shell_ready(meta["pane_id"]):
        log(f"{aid}: shell prompt not detected after 30s; starting anyway")
    stop_watchdog = threading.Event()
    start_watchdog(meta["pane_id"], session_id, stop_watchdog)
    try:
        herdr("agent", "start", aid, "--kind", "claude", "--pane", meta["pane_id"],
              "--timeout", str(int(args.start_timeout * 1000)), "--", *claude_args,
              timeout=args.start_timeout + 30)
    except HerdrError as e:
        stop_watchdog.set()
        snap = screen(meta)
        if e.code == "agent_not_ready" or (agent_info(aid) or {}).get("agent_status") == "blocked":
            meta.update(status="blocked", error=str(e))
            save_meta(meta)
            return meta, "blocked", {"screen": snap, "hint":
                    f"answer the dialog ({cli_ref()} keys {aid} ...), then: "
                    f"{cli_ref()} send {aid} --prompt-file {shlex.quote(os.path.join(d, 'prompt.md'))}"}
        meta.update(status="error", error=str(e))
        save_meta(meta)
        core.atomic_write(os.path.join(d, "start-failure.txt"), snap)
        from .waiting import close_agent
        close_agent(meta, keep_files=True)
        die(f"agent start failed: {e}\n{snap}")
    stop_watchdog.set()
    launches_call("record_spawn", meta, claude_args, tab_env, session_env=os.environ)
    launches_call("refresh_pointer")

    return send_prompt(meta, prompt)


def send_prompt(meta, prompt):
    info = agent_info(meta["id"])
    if info is None:
        meta["status"] = "dead"
        save_meta(meta)
        return meta, "dead", {"screen": screen(meta)}
    if info.get("agent_status") == "blocked":
        return meta, "blocked", {"screen": screen(meta)}
    if info.get("agent_status") == "working":
        return meta, "busy", {"hint": f"agent is mid-turn; run `{cli_ref()} wait {meta['id']}` first"}
    meta["last_prompt_at_ns"] = time.time_ns()
    meta["prompts"] = meta.get("prompts", 0) + 1
    meta["status"] = "working"
    save_meta(meta)
    try:
        herdr("agent", "prompt", meta["id"], prompt, timeout=60)
    except HerdrError as e:
        if e.code == "agent_blocked":
            meta["status"] = "blocked"
            save_meta(meta)
            return meta, "blocked", {"screen": screen(meta)}
        meta.update(status="error", error=str(e))
        save_meta(meta)
        return meta, "error", {"error": str(e), "hint":
                f"delivery is uncertain: inspect with `{cli_ref()} screen {meta['id']}` before re-sending"}
    ensure_submitted(meta, prompt)
    return meta, "working", {}


SUBMIT_DELAYS = (2, 3, 5, 8, 12)
SUBMIT_POLL = 0.5
SCREEN_LINES = 200
RULE_CHARS = {"─", "━", "-"}


def submit_delays():
    raw = os.environ.get("HERDR_AGENT_SUBMIT_DELAYS", "").strip()
    if not raw:
        return SUBMIT_DELAYS
    try:
        return tuple(float(x) for x in raw.split(",") if x.strip())
    except ValueError:
        return SUBMIT_DELAYS


def input_box(snapshot):
    """Text of Claude's prompt input: the lines above the last horizontal rule, down from
    the rule before it. A prompt taller than the snapshot pushes the top rule out of view;
    then everything above the bottom rule is the box."""
    lines = snapshot.splitlines()
    rules = [i for i, ln in enumerate(lines)
             if ln.strip() and set(ln.strip()) <= RULE_CHARS and len(ln.strip()) > 20]
    if not rules:
        return ""
    top = rules[-2] + 1 if len(rules) >= 2 else 0
    return "\n".join(lines[top:rules[-1]])


def prompt_pending(box, prompt):
    """True when the input box still holds our prompt: the end of its last line sits in the box."""
    text = box.strip().lstrip("❯>").strip()
    if not text:
        return False
    if "[Pasted text" in text:
        return True
    last = next((ln for ln in reversed(prompt.splitlines()) if ln.strip()), "")
    probe = "".join(last.split())[-30:]
    return bool(probe) and probe in "".join(text.split())


def transcript_has_user_turn(meta, since_ns):
    path = transcript_path_for(meta.get("session_id"), meta.get("claude_config_dir") or os.environ.get("CLAUDE_CONFIG_DIR"))
    if not path:
        return False
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 512 * 1024))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        ts = e.get("timestamp")
        try:
            if ts and dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1e9 < since_ns:
                break
        except ValueError:
            pass
        if e.get("type") == "user" and not e.get("isMeta"):
            return True
    return False


def turn_started(meta, since):
    r = read_result(meta)
    if r and r.get("written_at_ns", 0) > since:
        return True
    if (agent_info(meta["id"]) or {}).get("agent_status") != "idle":
        return True
    return transcript_has_user_turn(meta, since)


def ensure_submitted(meta, prompt, delays=None):
    """Press Enter if the prompt was typed but never submitted.

    `herdr agent prompt` pastes the text and presses Enter. When the TUI does not see the
    burst as a bracketed paste, it is taken as typed text and the trailing Enter is lost,
    leaving the prompt unsent in the input box. Submission is confirmed by agent state
    (not idle, a result, or a user entry in the transcript); while none shows up, press
    Enter with backoff, only if the input box still holds our text and never while the
    agent is blocked on a dialog (the user decides there)."""
    delays = submit_delays() if delays is None else delays
    since = meta.get("last_prompt_at_ns", 0)
    for attempt, delay in enumerate(delays, 1):
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            time.sleep(min(SUBMIT_POLL, max(0.0, deadline - time.monotonic())) or 0.01)
            if turn_started(meta, since):
                return
        if not prompt_pending(input_box(screen(meta, lines=SCREEN_LINES)), prompt):
            continue
        if (agent_info(meta["id"]) or {}).get("agent_status") != "idle":
            return
        log(f"{meta['id']}: prompt still in the input box, pressing Enter ({attempt}/{len(delays)})")
        try:
            herdr("agent", "send-keys", meta["id"], "enter", timeout=15)
        except HerdrError as e:
            log(f"{meta['id']}: send-keys failed: {e}")
            return
