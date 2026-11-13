import json
import os
import subprocess
import threading
import time

from .core import log

PROMPT_ENDINGS = ("$", "%", "#", ">", "❯", "➜", "λ")


class HerdrError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def herdr_bin():
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def herdr(*args, parse=True, timeout=None):
    try:
        p = subprocess.run([herdr_bin(), *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise HerdrError("herdr_missing", "herdr binary not found in PATH")
    except subprocess.TimeoutExpired:
        raise HerdrError("cli_timeout", f"herdr {' '.join(args[:2])} did not return")
    if p.returncode != 0:
        for stream in (p.stderr, p.stdout):
            try:
                err = json.loads(stream).get("error") or {}
                raise HerdrError(err.get("code", "unknown"), err.get("message", stream.strip()))
            except (ValueError, AttributeError):
                continue
        raise HerdrError("exit_%d" % p.returncode, (p.stderr or p.stdout).strip())
    if not parse:
        return p.stdout
    try:
        return json.loads(p.stdout).get("result") or {}
    except ValueError:
        return {}


def agent_info(name):
    try:
        return herdr("agent", "get", name, timeout=15).get("agent")
    except HerdrError as e:
        if e.code == "agent_not_found":
            return None
        raise


def pane_exists(pane_id):
    if not pane_id:
        return False
    try:
        herdr("pane", "get", pane_id, timeout=15)
        return True
    except HerdrError as e:
        if e.code == "pane_not_found":
            return False
        raise


def tab_info(tab_id):
    if not tab_id:
        return None
    try:
        return herdr("tab", "get", tab_id, timeout=15).get("tab")
    except HerdrError as e:
        if e.code == "tab_not_found":
            return None
        raise


def pane_list(timeout=15):
    return herdr("pane", "list", timeout=timeout).get("panes") or []


def agent_list(timeout=15):
    return herdr("agent", "list", timeout=timeout).get("agents") or []


def process_info(pane_id, timeout=15):
    return herdr("pane", "process-info", "--pane", pane_id, timeout=timeout).get("process_info") or {}


def shell_state(pane_id):
    """(shell_idle, last_line, snapshot): the shell alone is in the foreground."""
    try:
        info = process_info(pane_id)
        snap = herdr("pane", "read", pane_id, "--source", "visible", "--lines", "15", parse=False, timeout=15)
    except HerdrError:
        return False, "", ""
    fg = info.get("foreground_processes") or []
    idle = bool(fg) and all(p.get("pid") == info.get("shell_pid") for p in fg)
    lines = [ln.rstrip() for ln in snap.splitlines() if ln.strip()]
    return idle, (lines[-1] if lines else ""), snap


def wait_shell_ready(pane_id, timeout=30, settle=0.8):
    """`herdr agent start` types the command into the pane's shell and needs that shell
    at its prompt. A shell still sourcing its profile (slow when several tabs start at
    once) drops the Enter, and the command then sits typed but never run. Wait until
    the shell alone is in the foreground, shows a prompt, and its output has settled."""
    deadline = time.monotonic() + timeout
    last, stable_since = None, None
    while time.monotonic() < deadline:
        idle, tail, snap = shell_state(pane_id)
        now = time.monotonic()
        if idle and tail.endswith(PROMPT_ENDINGS):
            if snap == last:
                stable_since = stable_since or now
                if now - stable_since >= settle:
                    return True
            else:
                stable_since = None
        else:
            stable_since = None
        last = snap
        time.sleep(0.25)
    return False


def start_watchdog(pane_id, marker, stop, delay=4, attempts=3):
    """Safety net while `herdr agent start` waits for readiness: if our claude command is
    still sitting typed at the shell prompt (the shell is idle, so nothing runs), press
    Enter for it. This only submits the command we typed; it never answers a dialog."""
    def run():
        for attempt in range(1, attempts + 1):
            if stop.wait(delay):
                return
            idle, tail, _ = shell_state(pane_id)
            if idle and marker in tail:
                log(f"claude command typed but not started; pressing Enter ({attempt}/{attempts})")
                try:
                    herdr("pane", "send-keys", pane_id, "enter", timeout=15)
                except HerdrError:
                    return
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def screen(meta, lines=40):
    """Best-effort snapshot of the agent's terminal."""
    target = meta["id"] if agent_info(meta["id"]) else meta.get("pane_id")
    if not target:
        return ""
    sub = "agent" if target == meta["id"] else "pane"
    try:
        return herdr(sub, "read", target, "--source", "recent-unwrapped",
                     "--lines", str(lines), parse=False, timeout=15).rstrip()
    except HerdrError:
        return ""
