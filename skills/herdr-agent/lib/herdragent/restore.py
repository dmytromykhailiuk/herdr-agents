import argparse
import fcntl
import json
import os
import sys
import threading
import time

from . import core, herdrconfig, launches
from .core import EXIT_OK, EXIT_USAGE, CliError
from .herdr import PROMPT_ENDINGS, HerdrError, agent_list, herdr, pane_list, process_info, start_watchdog, \
    wait_shell_ready

DEFAULT_GRACE_S = 600
POLL_S = 1.0
PACE_S = 0.5
SHELL_SETTLE_POLLS = 2
START_TIMEOUT_MS = 60000
SESSION_FLAGS = ("--resume", "-r", "--session-id")

RELAUNCH = ("native", "shell")
RUN_STARTED = time.time()


def _float_env(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


class Logger:
    def __init__(self, to_files=True):
        self.paths = []
        if to_files:
            self.paths.append(core.state_dir() / "restore.log")
            plugin_state = os.environ.get("HERDR_PLUGIN_STATE_DIR")
            if plugin_state:
                self.paths.append(os.path.join(os.path.expanduser(plugin_state), "restore.log"))

    def __call__(self, msg):
        try:
            print(msg, flush=True)
        except (OSError, ValueError):
            pass
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {msg}\n"
        for path in self.paths:
            try:
                os.makedirs(os.path.dirname(str(path)), exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass


def _norm(path):
    return os.path.realpath(os.path.expanduser(path)) if path else ""


def _short(sid):
    return str(sid)[:8]


def _flag_value(argv, names):
    for i, tok in enumerate(argv):
        name, eq, value = tok.partition("=")
        if name in names:
            if eq:
                return value
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                return argv[i + 1]
    return None


def _is_claude(proc):
    argv = proc.get("argv") or []
    names = [proc.get("name") or "", proc.get("argv0") or ""] + [str(a) for a in argv[:2]]
    return any(os.path.basename(n) == "claude" for n in names)


def snapshot():
    panes = {p["pane_id"]: dict(p) for p in pane_list() if p.get("pane_id")}
    try:
        agents = agent_list()
    except HerdrError:
        agents = []
    for a in agents:
        pane = panes.setdefault(a.get("pane_id"), dict(a)) if a.get("pane_id") else None
        if pane is not None:
            for key in ("name", "agent_status", "agent_session", "agent"):
                if a.get(key) is not None:
                    pane[key] = a[key]
    by_sid, by_name = {}, {}
    for pid, p in panes.items():
        sid = (p.get("agent_session") or {}).get("value")
        if sid:
            by_sid.setdefault(sid, pid)
        if p.get("name"):
            by_name.setdefault(p["name"], pid)
    return {"panes": panes, "by_sid": by_sid, "by_name": by_name}


def match(rec, snap, claimed):
    panes = snap["panes"]
    sid = rec["session_id"]
    candidates = [(snap["by_sid"].get(sid), "session")]
    if rec.get("kind") == "spawn" and rec.get("name"):
        candidates.append((snap["by_name"].get(rec["name"]), "name"))
    for pid, how in candidates:
        if pid and pid in panes and pid not in claimed:
            return panes[pid], how
    pid = rec.get("pane_id")
    if pid and pid in panes and pid not in claimed:
        pane = panes[pid]
        if not rec.get("cwd") or _norm(pane.get("cwd")) == _norm(rec.get("cwd")):
            return pane, "pane"
    return None, None


def _profile_only(rec):
    return rec.get("kind") == "session" and not rec.get("marker") and bool(_match_env(rec))


def _match_env(rec):
    match_env = rec.get("match_env")
    if isinstance(match_env, dict) and match_env:
        return {str(k): str(v) for k, v in match_env.items()}
    cfg = rec.get("claude_config_dir")
    return {"CLAUDE_CONFIG_DIR": cfg} if launches._non_default_config_dir(cfg) else {}


def _env_matches(want, have):
    for key, value in want.items():
        got = have.get(key)
        if got is None:
            return False
        if key == "CLAUDE_CONFIG_DIR" and _norm(got) == _norm(value):
            continue
        if got != value:
            return False
    return True


def server_started_at():
    sock = os.environ.get("HERDR_SOCKET_PATH")
    if sock:
        try:
            return os.stat(os.path.expanduser(sock)).st_mtime
        except OSError:
            pass
    return RUN_STARTED


def _classify_profile_only(rec, claude, argv_sid, pane_sid, server_started):
    proc_env = claude.get("env") or claude.get("environ")
    if isinstance(proc_env, dict):
        if not _env_matches(_match_env(rec), {str(k): str(v) for k, v in proc_env.items()}):
            return None
        return "healthy" if argv_sid == rec["session_id"] or pane_sid == rec["session_id"] else None
    if argv_sid is None:
        return "healthy" if pane_sid == rec["session_id"] else None
    started = server_started_at() if server_started is None else server_started
    restored_at = rec.get("restored_at") or 0
    if rec.get("status") == "running" and restored_at >= started:
        return "healthy"
    return None


def classify(rec, pane, server_started=None):
    pid = pane["pane_id"]
    try:
        info = process_info(pid)
    except HerdrError as e:
        return "gone" if e.code == "pane_not_found" else "unknown", str(e)
    fg = info.get("foreground_processes") or []
    sid = rec["session_id"]
    claude = next((p for p in fg if _is_claude(p)), None)
    if claude:
        argv = [str(a) for a in claude.get("argv") or []]
        argv_sid = _flag_value(argv, SESSION_FLAGS)
        detail = " ".join(argv)
        if argv_sid and argv_sid != sid:
            return "foreign", detail
        if launches.marker_in_argv(rec.get("marker"), argv):
            return "healthy", detail
        pane_sid = (pane.get("agent_session") or {}).get("value")
        if _profile_only(rec):
            state = _classify_profile_only(rec, claude, argv_sid, pane_sid, server_started)
            if state:
                return state, detail
        if argv_sid == sid or pane_sid == sid:
            if pane.get("agent_status") == "working":
                return "working", detail
            return "native", detail
        return "foreign", detail
    if not fg or not all(p.get("pid") == info.get("shell_pid") for p in fg):
        return "busy", " ".join(str(p.get("cmdline") or p.get("name") or "") for p in fg)
    try:
        screen = herdr("pane", "read", pid, "--source", "visible", "--lines", "15", parse=False, timeout=15)
    except HerdrError as e:
        return "unknown", str(e)
    lines = [ln.rstrip() for ln in screen.splitlines() if ln.strip()]
    if lines and lines[-1].endswith(PROMPT_ENDINGS):
        return ("ended" if rec.get("ended_at") else "shell"), lines[-1]
    return "busy", lines[-1] if lines else ""


def _close_pane(pane, snap, log):
    tab_id = pane.get("tab_id")
    count = None
    if tab_id:
        try:
            count = (herdr("tab", "get", tab_id, timeout=15).get("tab") or {}).get("pane_count")
        except HerdrError:
            count = None
        if count is None:
            count = sum(1 for p in snap["panes"].values() if p.get("tab_id") == tab_id)
    try:
        if tab_id and count == 1:
            herdr("tab", "close", tab_id, timeout=15)
        else:
            herdr("pane", "close", pane["pane_id"], timeout=15)
    except HerdrError as e:
        if e.code not in ("pane_not_found", "tab_not_found"):
            log(f"  could not close old pane {pane['pane_id']}: {e}")


def _update_meta(rec, create=False, **fields):
    from .results import load_meta, save_meta
    name = rec.get("name")
    if not name or rec.get("kind") != "spawn":
        return
    try:
        meta = load_meta(name)
    except (CliError, OSError, ValueError):
        if not create:
            return
        meta = {
            "id": name, "role": name.rsplit("-", 1)[0], "cwd": rec.get("cwd"),
            "workspace_id": rec.get("workspace_id"), "session_id": rec["session_id"],
            "created_at": rec.get("created_at") or time.time(), "created_tab": True,
            "last_prompt_at_ns": 0, "claude_config_dir": rec.get("claude_config_dir") or "",
            "depth": core.env_int("HERDR_AGENT_DEPTH", 1, rec.get("env") or {}),
            "restored": True,
        }
    meta.update(fields)
    save_meta(meta)


def _prepare_agent_dir(rec, env, log):
    from .results import agent_dir
    from .spawn import write_settings
    d = agent_dir(rec["name"])
    if not os.path.isdir(d):
        log(f"  agent dir {d} missing (reboot?); recreating it")
    os.makedirs(d, exist_ok=True)
    env["HERDR_AGENT_DIR"] = d
    return d, write_settings(d)


def relaunch(rec, pane, snap, log):
    sid = rec["session_id"]
    env = {str(k): str(v) for k, v in (rec.get("env") or {}).items()}
    if rec.get("claude_config_dir"):
        env.setdefault("CLAUDE_CONFIG_DIR", rec["claude_config_dir"])
    is_spawn = rec.get("kind") == "spawn" and bool(rec.get("name"))
    settings_path, agent_d = None, None
    if is_spawn:
        agent_d, settings_path = _prepare_agent_dir(rec, env, log)
    name = rec.get("name") or f"claude-{sid[:6]}"
    label = rec.get("label") or name
    workspace = pane.get("workspace_id") or rec.get("workspace_id")
    cwd = rec.get("cwd") or pane.get("cwd")
    tab_args = ["tab", "create"]
    if workspace:
        tab_args += ["--workspace", workspace]
    tab_args += ["--cwd", cwd, "--label", label]
    for k, v in env.items():
        tab_args += ["--env", f"{k}={v}"]
    tab_args.append("--no-focus")
    try:
        res = herdr(*tab_args, timeout=30)
    except HerdrError as e:
        log(f"  tab create failed: {e}; old pane left as is")
        return "error"
    new_tab = (res.get("tab") or {}).get("tab_id")
    new_pane = (res.get("root_pane") or {}).get("pane_id")
    new_ws = (res.get("tab") or {}).get("workspace_id") or workspace
    _close_pane(pane, snap, log)

    claude_args = ["--resume", sid]
    if settings_path:
        claude_args += ["--settings", settings_path]
    claude_args += list(rec.get("args") or [])
    if not wait_shell_ready(new_pane):
        log(f"  shell prompt not detected in {new_pane} after 30s; starting anyway")
    stop = threading.Event()
    start_watchdog(new_pane, sid, stop)
    status, error = "running", None
    try:
        herdr("agent", "start", name, "--kind", "claude", "--pane", new_pane,
              "--timeout", str(START_TIMEOUT_MS), "--", *claude_args, timeout=START_TIMEOUT_MS / 1000 + 30)
    except HerdrError as e:
        error = str(e)
        status = "blocked" if e.code in ("agent_not_ready", "agent_blocked") else "error"
    finally:
        stop.set()

    fields = {"pane_id": new_pane, "tab_id": new_tab, "workspace_id": new_ws, "status": status,
              "unmatched_startups": 0, "restored_at": time.time()}
    if status == "running":
        fields["restored_count"] = int(rec.get("restored_count") or 0) + 1
    if is_spawn:
        fields["agent_dir"] = agent_d
    if env != (rec.get("env") or {}):
        fields["env"] = env
    launches.update(sid, **fields)
    meta_fields = {"pane_id": new_pane, "tab_id": new_tab, "workspace_id": new_ws, "status": status,
                   "created_tab": True}
    if error:
        meta_fields["error"] = error
    _update_meta(rec, create=True, **meta_fields)
    if status == "blocked":
        log(f"  {name} is blocked on a dialog in tab {new_tab}; answer it there "
            f"({core.CLI_PATH or 'herdr-agent'} keys {name} ...). Tab left open.")
    elif status == "error":
        log(f"  agent start failed: {error}; tab {new_tab} left open")
    return status


def _describe(rec):
    who = rec.get("name") or rec.get("label")
    kind = rec.get("kind", "?")
    return f"{kind} {who} ({_short(rec['session_id'])})" if who else f"{kind} ({_short(rec['session_id'])})"


def _prunable(rec, max_age_h, now):
    age_h = (now - rec.get("updated_at", rec.get("created_at", now))) / 3600
    return (age_h > max_age_h or rec.get("unmatched_startups", 0) >= launches.MAX_UNMATCHED
            or not launches.transcript_exists(rec))


def _ensure_config(log, dry_run):
    try:
        changed, status = herdrconfig.ensure(dry_run=dry_run)
    except OSError as e:
        log(f"could not check the Herdr config: {e}")
        return
    key = herdrconfig.KEY
    if status == "false":
        log(f"warning: [session] {key} = false in the Herdr config; left as is "
            f"(Herdr will not resume agents natively; records are still relaunched here)")
    elif status == "inline":
        log(f"warning: [session] is an inline table in the Herdr config; {key} not checked")
    if not changed:
        return
    if dry_run:
        log(f"dry-run: would set [session] {key} = true ({status}) and reload the Herdr config")
        return
    ok = herdrconfig.reload_config()
    log(f"set [session] {key} = true ({status}); reload-config {'ok' if ok else 'failed'}")


def _acquire_lock():
    path = core.state_dir() / "restore.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def _detach():
    pid = os.fork()
    if pid > 0:
        os.waitpid(pid, 0)
        return True
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    return False


def evaluate(recs):
    try:
        snap = snapshot()
    except HerdrError as e:
        return [dict(rec=r, pane=None, how=None, state="herdr-unavailable", detail=str(e)) for r in recs]
    out, claimed = [], set()
    for rec in recs:
        pane, how = match(rec, snap, claimed)
        if pane is None:
            out.append(dict(rec=rec, pane=None, how=None, state="unmatched", detail=""))
            continue
        claimed.add(pane["pane_id"])
        state, detail = classify(rec, pane)
        out.append(dict(rec=rec, pane=pane, how=how, state=state, detail=detail))
    return out


def run_startup(grace, dry_run, foreground):
    if core.env_off("HERDR_AGENTS_RESTORE"):
        print("herdr-agents restore disabled (HERDR_AGENTS_RESTORE=off)")
        return EXIT_OK
    if os.environ.get("HERDR_PLUGIN_EVENT") and not foreground and not dry_run:
        if _detach():
            return EXIT_OK
    log = Logger(to_files=not dry_run)
    lock = None if dry_run else _acquire_lock()
    if not dry_run and lock is None:
        log("another herdr-agents restore is running; exiting")
        return EXIT_OK
    try:
        return _startup(grace, dry_run, log)
    except Exception as e:
        log(f"restore failed: {e!r}")
        return EXIT_OK
    finally:
        if lock:
            lock.close()


def _startup(grace, dry_run, log):
    prefix = "dry-run: " if dry_run else ""
    log(f"{prefix}herdr-agents restore start (herdr={os.environ.get('HERDR_BIN_PATH') or 'herdr'}, "
        f"socket={os.environ.get('HERDR_SOCKET_PATH') or 'default'}, grace={grace:g}s)")
    _ensure_config(log, dry_run)
    max_age = core.env_int("HERDR_AGENTS_RESTORE_MAX_AGE_H", launches.DEFAULT_MAX_AGE_H)
    if dry_run:
        now = time.time()
        recs = []
        for rec in launches.load_all():
            if _prunable(rec, max_age, now):
                log(f"dry-run: would prune {_describe(rec)}")
            else:
                recs.append(rec)
    else:
        for sid in launches.prune(max_age):
            log(f"pruned record {_short(sid)} (expired, unmatched twice, or transcript gone)")
        recs = launches.load_all()
    if not recs:
        log(f"{prefix}nothing to restore")
        return EXIT_OK
    if dry_run:
        for item in evaluate(recs):
            log(f"dry-run: {_describe(item['rec'])}: {_decision(item)}")
        return EXIT_OK
    return _poll(recs, grace, log)


def _decision(item):
    state = item["state"]
    where = f" in {item['pane']['pane_id']} (by {item['how']})" if item.get("pane") else ""
    if state in RELAUNCH:
        return f"would relaunch ({state}){where}"
    if state == "healthy":
        return f"healthy{where}; leave"
    if state == "ended":
        return f"exited by the user{where}; would forget"
    if state == "unmatched":
        return "no matching pane"
    return f"{state}{where}; wait"


def _poll(recs, grace, log):
    poll = _float_env("HERDR_AGENTS_RESTORE_POLL_S", POLL_S)
    pace = _float_env("HERDR_AGENTS_RESTORE_PACE_S", PACE_S)
    deadline = time.monotonic() + grace
    pending = {r["session_id"]: r for r in recs}
    shell_seen = {}
    left = {}
    claimed = set()
    counts = {"healthy": 0, "running": 0, "blocked": 0, "error": 0}
    last_relaunch = 0.0
    while pending:
        try:
            snap = snapshot()
        except HerdrError as e:
            log(f"herdr not reachable yet: {e}")
            snap = None
        for sid, rec in list(pending.items()) if snap else []:
            pane, how = match(rec, snap, claimed)
            if pane is None:
                continue
            state, detail = classify(rec, pane)
            if state == "shell":
                shell_seen[sid] = shell_seen.get(sid, 0) + 1
                if shell_seen[sid] < SHELL_SETTLE_POLLS:
                    left[sid] = "shell (settling)"
                    continue
            else:
                shell_seen[sid] = 0
            if state == "healthy":
                claimed.add(pane["pane_id"])
                del pending[sid]
                left.pop(sid, None)
                counts["healthy"] += 1
                log(f"{_describe(rec)}: healthy in {pane['pane_id']}")
                _refresh_healthy(rec, pane)
                continue
            if state == "ended":
                claimed.add(pane["pane_id"])
                del pending[sid]
                left.pop(sid, None)
                launches.forget(sid)
                log(f"{_describe(rec)}: exited by the user, shell in {pane['pane_id']}; record forgotten")
                continue
            if state in RELAUNCH:
                claimed.add(pane["pane_id"])
                wait = last_relaunch + pace - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                log(f"{_describe(rec)}: {state} in {pane['pane_id']} (matched by {how}); relaunching"
                    + (f" [{detail}]" if detail and state == "native" else ""))
                result = relaunch(rec, pane, snap, log)
                last_relaunch = time.monotonic()
                counts[result] = counts.get(result, 0) + 1
                del pending[sid]
                left.pop(sid, None)
                continue
            left[sid] = f"{state} in {pane['pane_id']}" + (f" [{detail}]" if detail else "")
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(min(poll, max(0.0, deadline - time.monotonic())))

    unmatched = 0
    for sid, rec in pending.items():
        if sid in left:
            log(f"{_describe(rec)}: left alone ({left[sid]})")
            continue
        unmatched += 1
        n = int(rec.get("unmatched_startups") or 0) + 1
        if n >= launches.MAX_UNMATCHED:
            launches.forget(sid)
            log(f"{_describe(rec)}: no matching pane ({n} startups); record dropped")
        else:
            launches.update(sid, unmatched_startups=n, updated_at=rec.get("updated_at", time.time()))
            log(f"{_describe(rec)}: no matching pane (unmatched {n}/{launches.MAX_UNMATCHED})")
    log(f"restore done: {counts['healthy']} healthy, {counts['running']} relaunched, "
        f"{counts['blocked']} blocked, {counts['error']} failed, {len(left)} left alone, {unmatched} unmatched")
    return EXIT_OK


def _refresh_healthy(rec, pane):
    fields = {}
    for key in ("pane_id", "tab_id", "workspace_id"):
        if pane.get(key) and pane.get(key) != rec.get(key):
            fields[key] = pane[key]
    if fields or rec.get("unmatched_startups"):
        launches.update(rec["session_id"], unmatched_startups=0, **fields)
    if fields:
        _update_meta(rec, **fields)


def run_status(as_json):
    recs = launches.load_all()
    items = evaluate(recs) if recs else []
    rows = []
    for item in items:
        rec, pane = item["rec"], item["pane"]
        rows.append({
            "session_id": rec["session_id"], "kind": rec.get("kind"), "name": rec.get("name"),
            "label": rec.get("label"), "cwd": rec.get("cwd"), "recorded_pane": rec.get("pane_id"),
            "pane": pane["pane_id"] if pane else None, "match": item["how"], "state": item["state"],
            "detail": item["detail"], "status": rec.get("status"),
            "restored_count": rec.get("restored_count", 0),
            "unmatched_startups": rec.get("unmatched_startups", 0),
        })
    if as_json:
        print(json.dumps({"records": rows, "config": _config_state()}, indent=2))
        return EXIT_OK
    print(f"Herdr config [session] {herdrconfig.KEY}: {_config_state()}")
    if not rows:
        print("no launch records")
        return EXIT_OK
    for r in rows:
        print(f"{_short(r['session_id'])}  {r['kind'] or '?':7} {r['name'] or r['label'] or '-':24} "
              f"recorded={r['recorded_pane'] or '-'} pane={r['pane'] or '-'} match={r['match'] or '-'} "
              f"state={r['state']} restored={r['restored_count']} unmatched={r['unmatched_startups']}")
    return EXIT_OK


def _config_state():
    try:
        return herdrconfig.state()
    except OSError as e:
        return f"error: {e}"


def main(argv):
    argv = list(argv)
    ap = argparse.ArgumentParser(prog="herdr-agent herdr", description="Herdr restore plugin entry points")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("startup", help="relaunch recorded Claude sessions after a Herdr server restart")
    p.add_argument("--dry-run", action="store_true", help="print the decisions; change nothing")
    p.add_argument("--grace", type=float, default=None,
                   help=f"seconds to poll for restored panes (default $HERDR_AGENTS_RESTORE_GRACE_S or "
                        f"{DEFAULT_GRACE_S})")
    p.add_argument("--foreground", action="store_true",
                   help="stay attached even when started as a Herdr plugin hook")
    s = sub.add_parser("status", help="show launch records and how they match the current panes")
    s.add_argument("--json", action="store_true")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else EXIT_OK
    if args.cmd == "status":
        return run_status(args.json)
    grace = args.grace if args.grace is not None else _float_env("HERDR_AGENTS_RESTORE_GRACE_S", DEFAULT_GRACE_S)
    return run_startup(max(0.0, grace), args.dry_run, args.foreground)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
