import json
import os
import shlex
import shutil
import stat
import subprocess
import tempfile

from .core import log


def git_toplevel(path):
    """The toplevel of the git checkout containing path (a linked worktree is its own
    toplevel), or None outside git."""
    try:
        p = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    top = p.stdout.strip()
    return os.path.realpath(top) if p.returncode == 0 and top else None


def git_main_worktree(path):
    """The main checkout of the repository when path is inside a linked worktree, else None."""
    try:
        p = subprocess.run(["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    common = p.stdout.strip()
    if p.returncode != 0 or not common or os.path.basename(common) != ".git":
        return None
    main = os.path.realpath(os.path.dirname(common))
    return main if main != git_toplevel(path) else None


def claude_config_path():
    profile_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.join(os.path.expanduser(profile_dir), ".claude.json") if profile_dir \
        else os.path.expanduser("~/.claude.json")


def claude_invocation(config_dir=None):
    return f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} claude" if config_dir else "claude"


def is_trusted(cwd):
    """Whether Claude Code trusts cwd, mirroring its observed rule (2.1.30x): inside a
    git checkout an entry for cwd or a parent up to the checkout's toplevel counts
    (folders above it are not inherited); a linked worktree is also covered by the
    main checkout's entry, which is where accepting the dialog in a worktree stores
    it; outside git any trusted parent folder is enough.

    An agent started in an untrusted folder stops at the trust dialog, and once it
    is accepted the session carries on without its CLI flags: a different session
    id, no --settings (so no Stop hook and no result), no --effort. Refuse to start
    there instead. Unknown state (no readable .claude.json) counts as trusted."""
    path = os.path.realpath(cwd)
    try:
        with open(claude_config_path(), encoding="utf-8") as f:
            projects = json.load(f).get("projects") or {}
    except (OSError, ValueError, AttributeError):
        return True
    main = git_main_worktree(path)
    if main and (projects.get(main) or {}).get("hasTrustDialogAccepted"):
        return True
    stop = git_toplevel(path)
    while True:
        if (projects.get(path) or {}).get("hasTrustDialogAccepted"):
            return True
        parent = os.path.dirname(path)
        if path == stop or parent == path:
            return False
        path = parent


AUTO_TRUST_OFF = ("off", "0", "false", "no")


def auto_trust_enabled():
    return os.environ.get("HERDR_AGENT_AUTO_TRUST", "").strip().lower() not in AUTO_TRUST_OFF


def trust_key(path):
    """The projects key Claude Code checks for path: the main checkout of a linked
    worktree, the git toplevel, or path itself outside git."""
    path = os.path.realpath(path)
    return git_main_worktree(path) or git_toplevel(path) or path


def trusted_ancestor(projects, key):
    home = os.path.realpath(os.path.expanduser("~"))
    path = key
    while True:
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
        if path != home and (projects.get(path) or {}).get("hasTrustDialogAccepted"):
            return path


def grant_trust(config, key, attempts=2):
    """Set projects[key].hasTrustDialogAccepted in config without touching anything
    else. Claude sessions rewrite the file often, so the write is atomic and dropped
    (then retried) when the file changed between read and replace."""
    config = os.path.realpath(config)
    for _ in range(attempts):
        try:
            before = os.stat(config)
            with open(config, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        projects = data.setdefault("projects", {}) if isinstance(data, dict) else None
        if not isinstance(projects, dict) or not isinstance(projects.get(key, {}), dict):
            return False
        projects.setdefault(key, {})["hasTrustDialogAccepted"] = True
        backup = config + ".herdr-agent.bak"
        tmp = None
        try:
            if not os.path.exists(backup):
                shutil.copy2(config, backup)
            fd, tmp = tempfile.mkstemp(prefix=".claude.json.herdr-agent.", dir=os.path.dirname(config))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.chmod(tmp, stat.S_IMODE(before.st_mode))
            now = os.stat(config)
            if (now.st_mtime_ns, now.st_size) != (before.st_mtime_ns, before.st_size):
                os.unlink(tmp)
                continue
            os.replace(tmp, config)
            return True
        except OSError:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
            return False
    return False


MCP_LISTS = ("enabledMcpjsonServers", "disabledMcpjsonServers")
MCP_FLAG = "enableAllProjectMcpServers"


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def mcp_decision(folder, project_entry=None):
    """The .mcp.json approval state Claude Code 2.1.32x keeps for a project: its
    .claude/settings.local.json (where the dialog writes), .claude/settings.json and the
    legacy projects[<folder>] fields of .claude.json (migrated into settings.local.json)."""
    sources = [_read_json(os.path.join(folder, ".claude", name)) or {}
               for name in ("settings.local.json", "settings.json")]
    sources.append(project_entry if isinstance(project_entry, dict) else {})
    decision = {}
    for src in sources:
        if isinstance(src.get(MCP_FLAG), bool) and MCP_FLAG not in decision:
            decision[MCP_FLAG] = src[MCP_FLAG]
        for name in MCP_LISTS:
            values = src.get(name) if isinstance(src.get(name), list) else []
            merged = decision.setdefault(name, [])
            merged.extend(v for v in values if isinstance(v, str) and v not in merged)
    return {k: v for k, v in decision.items() if v != []}


def inherit_mcp_approvals(key, decision):
    """Merge decision into <key>/.claude/settings.local.json: lists are unioned, an explicit
    enableAllProjectMcpServers in the target is kept, other keys are untouched."""
    path = os.path.join(key, ".claude", "settings.local.json")
    exists = os.path.exists(path)
    data = _read_json(path) if exists else {}
    if data is None:
        return False
    changed = False
    if MCP_FLAG in decision and not isinstance(data.get(MCP_FLAG), bool):
        data[MCP_FLAG] = decision[MCP_FLAG]
        changed = True
    for name in MCP_LISTS:
        current = data.get(name) if isinstance(data.get(name), list) else []
        added = [v for v in decision.get(name, []) if v not in current]
        if added:
            data[name] = current + added
            changed = True
    if not changed:
        return False
    tmp = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".settings.local.json.herdr-agent.", dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode) if exists else 0o644)
        os.replace(tmp, path)
        return True
    except OSError:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
        return False


def ensure_trusted(cwd):
    """is_trusted, plus: a folder Claude Code would ask about (a nested git repo or
    worktree) inside a trusted parent folder gets trusted automatically, together with
    the parent's .mcp.json approvals, unless HERDR_AGENT_AUTO_TRUST is off."""
    if is_trusted(cwd):
        return True
    if not auto_trust_enabled():
        return False
    config = claude_config_path()
    try:
        with open(config, encoding="utf-8") as f:
            projects = json.load(f).get("projects") or {}
    except (OSError, ValueError, AttributeError):
        return False
    key = trust_key(cwd)
    parent = trusted_ancestor(projects, key)
    if not parent or not grant_trust(config, key) or not is_trusted(cwd):
        return False
    log(f"auto-trusted {key} (parent {parent} is trusted)")
    decision = mcp_decision(parent, projects.get(parent))
    if decision and inherit_mcp_approvals(key, decision):
        log(f"inherited MCP approvals for {key} from {parent}")
    return True
