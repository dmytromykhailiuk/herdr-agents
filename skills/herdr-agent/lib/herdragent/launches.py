import glob
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, core

SCHEMA = 1
DEFAULT_MAX_AGE_H = 168
MAX_UNMATCHED = 2

ARITY = {
    "--model": 1,
    "--effort": 1,
    "--agent": 1,
    "--permission-mode": 1,
    "--settings": 1,
    "--append-system-prompt": 1,
    "--system-prompt": 1,
    "--add-dir": "+",
    "--allowedTools": "+",
    "--allowed-tools": "+",
    "--disallowedTools": "+",
    "--disallowed-tools": "+",
    "--mcp-config": "+",
    "--strict-mcp-config": 0,
    "--dangerously-skip-permissions": 0,
    "--name": 1,
    "--plugin-dir": 1,
    "--setting-sources": 1,
}

DROP_ARITY = {
    "--resume": "?",
    "-r": "?",
    "--continue": 0,
    "-c": 0,
    "--session-id": 1,
    "--fork-session": 0,
}

PRINT_FLAGS = ("-p", "--print")

NOT_RESTORED = ("--settings",)

BEHAVIOUR_ENV = ("CLAUDE_CONFIG_DIR", "CLAUDE_ORCHESTRATOR", "HERDR_AGENT_AUTO_TRUST", "HERDR_AGENT_MAX_DEPTH")
BEHAVIOUR_ENV_PREFIXES = ("HERDR_AGENTS_",)
LOCATION_ENV = ("HERDR_AGENTS_STATE_DIR", "HERDR_AGENTS_DATA_DIR")


def behaviour_env(environ=None):
    environ = os.environ if environ is None else environ
    out = {}
    for key, value in environ.items():
        if key in LOCATION_ENV:
            continue
        if key in BEHAVIOUR_ENV or key.startswith(BEHAVIOUR_ENV_PREFIXES):
            out[str(key)] = str(value)
    if not _non_default_config_dir(out.get("CLAUDE_CONFIG_DIR")):
        out.pop("CLAUDE_CONFIG_DIR", None)
    return out


def launches_dir(*, environ=None):
    return core.state_dir(environ) / "launches"


def _path(sid, *, environ=None):
    sid = str(sid or "")
    if not sid or "/" in sid or "\\" in sid or sid.startswith("."):
        return None
    return launches_dir(environ=environ) / f"{sid}.json"


def _is_flag(token):
    return token.startswith("-") and token != "-"


def sanitize_args(argv):
    out = []
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok == "--":
            break
        if not _is_flag(tok):
            i += 1
            continue
        name, eq, value = tok.partition("=")
        if tok in PRINT_FLAGS or name == "--print":
            return None
        if name in DROP_ARITY:
            arity = DROP_ARITY[name]
            i += 1
            if not eq and arity:
                if i < n and (arity == 1 or not _is_flag(argv[i])):
                    i += 1
            continue
        if name not in ARITY:
            i += 1
            continue
        arity = ARITY[name]
        if eq:
            out.append(tok)
            i += 1
            continue
        if arity == 0:
            out.append(tok)
            i += 1
        elif arity == 1:
            if i + 1 >= n:
                break
            out += [tok, argv[i + 1]]
            i += 2
        else:
            vals = []
            j = i + 1
            while j < n and not _is_flag(argv[j]):
                vals.append(argv[j])
                j += 1
            if vals:
                out += [tok, *vals]
            i = j
    return out


def _strip_flags(args, names):
    out = []
    i = 0
    while i < len(args):
        tok = args[i]
        name = tok.partition("=")[0]
        if name in names:
            i += 1
            if "=" not in tok:
                while i < len(args) and not _is_flag(args[i]):
                    i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _first_flag_marker(args):
    for i, tok in enumerate(args):
        if _is_flag(tok):
            if "=" in tok or i + 1 >= len(args) or _is_flag(args[i + 1]):
                return tok
            return f"{tok} {args[i + 1]}"
    return ""


def marker_in_argv(marker, argv):
    if not marker:
        return False
    want = marker.split(" ")
    argv = list(argv)
    for i in range(len(argv) - len(want) + 1):
        if argv[i:i + len(want)] == want:
            return True
    return False


def _flag_value(args, name):
    for i, tok in enumerate(args):
        if tok == name and i + 1 < len(args):
            return args[i + 1]
        if tok.startswith(name + "="):
            return tok.partition("=")[2]
    return None


def _load_settings(args):
    value = _flag_value(args, "--settings")
    if not value:
        return None
    try:
        if value.lstrip().startswith("{"):
            data = json.loads(value)
        else:
            data = core.read_json(os.path.expanduser(value))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _env_dict(tab_env):
    if not tab_env:
        return {}
    if isinstance(tab_env, dict):
        return {str(k): str(v) for k, v in tab_env.items()}
    out = {}
    for item in tab_env:
        key, eq, value = str(item).partition("=")
        if eq and key:
            out[key] = value
    return out


def _supersede(pane_id, keep_sid, *, environ=None):
    if not pane_id:
        return
    for rec in load_all(environ=environ):
        if rec.get("pane_id") == pane_id and rec.get("session_id") != keep_sid:
            forget(rec["session_id"], environ=environ)


def _write(rec, *, environ=None):
    path = _path(rec["session_id"], environ=environ)
    if path is None:
        raise core.CliError(f"invalid session id: {rec['session_id']!r}")
    _supersede(rec.get("pane_id"), rec["session_id"], environ=environ)
    core.write_json(path, rec, 0o600)
    try:
        refresh_pointer(environ=environ)
    except OSError:
        pass
    return rec


def _base(kind, sid, now):
    return {
        "v": SCHEMA,
        "kind": kind,
        "session_id": sid,
        "name": None,
        "label": None,
        "cwd": None,
        "workspace_id": None,
        "pane_id": None,
        "tab_id": None,
        "claude_config_dir": "",
        "env": {},
        "args": [],
        "settings": None,
        "agent_dir": None,
        "marker": "",
        "created_at": now,
        "updated_at": now,
        "restored_count": 0,
        "unmatched_startups": 0,
    }


def record_spawn(meta, claude_args, tab_env, *, environ=None, session_env=None):
    now = time.time()
    sid = meta["session_id"]
    aid = meta["id"]
    rec = _base("spawn", sid, now)
    cfg = meta.get("claude_config_dir") or ""
    rec_env = _env_dict(tab_env)
    if cfg:
        rec_env.setdefault("CLAUDE_CONFIG_DIR", cfg)
    for key, value in behaviour_env(session_env or {}).items():
        rec_env.setdefault(key, value)
    args = sanitize_args(list(claude_args)) or []
    rec.update(
        name=aid,
        label=aid,
        cwd=meta.get("cwd"),
        workspace_id=meta.get("workspace_id"),
        pane_id=meta.get("pane_id"),
        tab_id=meta.get("tab_id"),
        claude_config_dir=cfg,
        env=rec_env,
        args=_strip_flags(args, NOT_RESTORED),
        settings=_load_settings(list(claude_args)),
        agent_dir=meta.get("agent_dir") or str(core.agent_root(environ) / aid),
        marker=f"--name {aid}",
    )
    return _write(rec, environ=environ)


def _non_default_config_dir(cfg):
    if not cfg:
        return False
    default = os.path.realpath(os.path.expanduser("~/.claude"))
    return os.path.realpath(os.path.expanduser(cfg)) != default


def record_session(session_id, argv, cwd, pane_id=None, workspace_id=None, tab_id=None,
                   tab_env=None, label=None, name=None, claude_config_dir=None, session_env=None, *,
                   environ=None):
    sanitized = sanitize_args(list(argv))
    _supersede(pane_id, session_id, environ=environ)
    if sanitized is None:
        return None
    args = _strip_flags(sanitized, NOT_RESTORED)
    cfg = claude_config_dir or ""
    match_env = behaviour_env(session_env or {})
    if _non_default_config_dir(cfg):
        match_env["CLAUDE_CONFIG_DIR"] = cfg
    if not args and not match_env:
        return None
    now = time.time()
    rec = _base("session", session_id, now)
    rec_env = _env_dict(tab_env)
    if cfg:
        rec_env.setdefault("CLAUDE_CONFIG_DIR", cfg)
    for key, value in match_env.items():
        rec_env.setdefault(key, value)
    existing = _read(session_id, environ=environ)
    if existing:
        rec["created_at"] = existing.get("created_at", now)
        rec["restored_count"] = existing.get("restored_count", 0)
        for key in ("status", "restored_at"):
            if key in existing:
                rec[key] = existing[key]
    rec.update(
        name=name,
        label=label,
        cwd=cwd,
        workspace_id=workspace_id,
        pane_id=pane_id,
        tab_id=tab_id,
        claude_config_dir=cfg,
        env=rec_env,
        args=args,
        marker=_first_flag_marker(args) or None,
    )
    if not args:
        rec["match_env"] = match_env
    return _write(rec, environ=environ)


def _read(sid, *, environ=None):
    path = _path(sid, environ=environ)
    if path is None:
        return None
    try:
        data = core.read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("v") != SCHEMA or not data.get("session_id"):
        return None
    return data


def get(sid, *, environ=None):
    return _read(sid, environ=environ)


def forget(sid, *, environ=None):
    path = _path(sid, environ=environ)
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def load_all(*, environ=None):
    d = launches_dir(environ=environ)
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        rec = _read(name[:-5], environ=environ)
        if rec:
            out.append(rec)
    out.sort(key=lambda r: r.get("created_at", 0))
    return out


def update(sid, *, environ=None, **fields):
    rec = _read(sid, environ=environ)
    if rec is None:
        return None
    rec.update(fields)
    rec["session_id"] = sid
    if "updated_at" not in fields:
        rec["updated_at"] = time.time()
    core.write_json(_path(sid, environ=environ), rec, 0o600)
    return rec


def transcript_exists(rec, *, environ=None):
    cfg = rec.get("claude_config_dir")
    base = Path(os.path.expanduser(cfg)) if cfg else core.claude_config_dir(environ)
    pattern = os.path.join(glob.escape(str(base)), "projects", "*", glob.escape(rec["session_id"]) + ".jsonl")
    return bool(glob.glob(pattern))


def prune(max_age_h=DEFAULT_MAX_AGE_H, *, environ=None, now=None):
    now = time.time() if now is None else now
    removed = []
    for rec in load_all(environ=environ):
        age_h = (now - rec.get("updated_at", rec.get("created_at", now))) / 3600
        if (age_h > max_age_h
                or rec.get("unmatched_startups", 0) >= MAX_UNMATCHED
                or not transcript_exists(rec, environ=environ)):
            forget(rec["session_id"], environ=environ)
            removed.append(rec["session_id"])
    return removed


def refresh_pointer(*, environ=None):
    cli = core.CLI_PATH
    if not cli:
        return False
    data = {"cli": cli, "python": sys.executable, "version": __version__}
    path = core.state_dir(environ) / "cli.json"
    try:
        if core.read_json(path) == data:
            return False
    except (OSError, ValueError):
        pass
    core.write_json(path, data)
    return True
