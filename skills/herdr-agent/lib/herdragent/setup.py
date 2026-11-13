import argparse
import copy
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, core, herdrconfig
from .core import CliError

PARTS = ("hook", "perms", "bin", "herdr")
DEFAULT_PARTS = ("hook", "perms", "herdr")
PROJECT_PARTS = ("hook", "perms")
MIN_HERDR = (0, 9, 2)
PLUGIN_ID = "dmytromykhailiuk.herdr-agents"
PERM_SUBCOMMANDS = ("run", "spawn", "wait", "send", "status", "result", "list", "screen")
HOOKS = (
    ("SessionStart", "^(startup|resume|clear|compact)$", "session-start", 10),
    ("SessionEnd", "^(prompt_input_exit|logout)$", "session-end", 5),
    ("PreToolUse", "^(Agent|Task)$", "pre-agent", 5),
    ("PreToolUse", "^Bash$", "pre-bash", 5),
)
HOOK_EVENTS = ("SessionStart", "SessionEnd", "PreToolUse")
HOOK_CMD_RE = re.compile(r"/bin/herdr-agent'?\"?\s+hook\s+(session-start|session-end|pre-agent|pre-bash)\b")
PERM_RE = re.compile(r"^Bash\(.*herdr-agent (" + "|".join(PERM_SUBCOMMANDS) + r") \*\)$")
PERM_PATH_RE = re.compile(r"^Bash\((.*) (?:" + "|".join(PERM_SUBCOMMANDS) + r") \*\)$")
CLI_SUFFIX = "/skills/herdr-agent/bin/herdr-agent"
PLUGIN_LINE_RE = re.compile(r"\[local:([^\]]*)\]")


def cli_path():
    return os.path.abspath(core.CLI_PATH or sys.argv[0])


def _roots():
    cli = cli_path()
    out = []
    for p in (cli, os.path.realpath(cli)):
        root = Path(p).parent.parent.parent.parent
        if root not in out:
            out.append(root)
    return out


def plugin_bundle():
    for root in _roots():
        if (root / ".claude-plugin" / "plugin.json").is_file():
            return root
    return None


def herdr_plugin_source():
    for root in _roots():
        src = root / "herdr-plugin"
        if (src / "herdr-plugin.toml").is_file():
            return src
    return None


def _tilde(p):
    home = str(core.home())
    s = str(p)
    return "~" + s[len(home):] if s == home or s.startswith(home + "/") else s


def hook_command(name, cli=None):
    return f"'{cli or cli_path()}' hook {name}"


def bin_link():
    base = os.environ.get("HERDR_AGENT_BIN_DIR")
    base = Path(os.path.expanduser(base)) if base else core.home() / ".local" / "bin"
    return base / "herdr-agent"


def installed_file():
    return core.state_dir() / "installed.json"


def say(msg=""):
    print(msg, flush=True)


def _git_toplevel(cwd=None):
    try:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                              timeout=10, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return None
    out = proc.stdout.strip()
    return Path(out) if proc.returncode == 0 and out else None


def settings_path(scope):
    if scope == "project":
        top = _git_toplevel()
        if not top:
            core.die("--scope project needs a git repository", core.EXIT_USAGE)
        return top / ".claude" / "settings.json"
    return core.claude_config_dir() / "settings.json"


def _load_settings(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        core.die(f"{path} is not a valid JSON object; not changed")
    hooks, perms = data.get("hooks"), data.get("permissions")
    if hooks is not None and not isinstance(hooks, dict):
        core.die(f"{path}: `hooks` must be a JSON object; not changed")
    if isinstance(hooks, dict):
        for event in HOOK_EVENTS:
            if hooks.get(event) is not None and not isinstance(hooks[event], list):
                core.die(f"{path}: `hooks.{event}` must be a JSON array; not changed")
    if perms is not None and not isinstance(perms, dict):
        core.die(f"{path}: `permissions` must be a JSON object; not changed")
    if isinstance(perms, dict) and perms.get("allow") is not None and not isinstance(perms["allow"], list):
        core.die(f"{path}: `permissions.allow` must be a JSON array; not changed")
    return data


def _write_settings(path, before, after, dry_run):
    if after == before:
        return False
    if dry_run:
        say(f"would write {path}:")
        say(json.dumps(after, indent=2, ensure_ascii=False))
        return True
    target = Path(os.path.realpath(path))
    mode = 0o644
    if target.exists():
        mode = target.stat().st_mode & 0o777
        bak = target.with_name(target.name + ".bak")
        if not os.path.lexists(bak):
            shutil.copy2(target, bak)
            say(f"backup: {bak}")
    core.write_json(target, after, mode)
    say(f"updated {path}")
    return True


def _is_ours(hook):
    return isinstance(hook, dict) and bool(HOOK_CMD_RE.search(str(hook.get("command", ""))))


def _ours_signature(data):
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    sig = []
    for event in HOOK_EVENTS:
        for grp in hooks.get(event) or []:
            inner = grp.get("hooks") if isinstance(grp, dict) else None
            if isinstance(inner, list):
                for h in inner:
                    if _is_ours(h):
                        sig.append((event, grp.get("matcher"), h.get("command"), h.get("timeout")))
    return sig


def foreign_orchestrator_hooks(data):
    hooks = data.get("hooks") if isinstance(data, dict) and isinstance(data.get("hooks"), dict) else {}
    found = []
    for grp in hooks.get("SessionStart") or []:
        inner = grp.get("hooks") if isinstance(grp, dict) else None
        for h in inner if isinstance(inner, list) else []:
            cmd = str(h.get("command", "")) if isinstance(h, dict) else ""
            if "orchestrator" in cmd.lower() and not _is_ours(h):
                found.append(cmd)
    return found


def foreign_orchestrator_in(path):
    try:
        return foreign_orchestrator_hooks(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def count_hooks(path):
    try:
        return len(_ours_signature(json.loads(Path(path).read_text(encoding="utf-8"))))
    except (OSError, ValueError, AttributeError):
        return 0


def _strip_hooks(data):
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event in HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept = []
        for grp in groups:
            inner = grp.get("hooks") if isinstance(grp, dict) else None
            if not isinstance(inner, list):
                kept.append(grp)
                continue
            keep = [h for h in inner if not _is_ours(h)]
            removed += len(inner) - len(keep)
            if keep:
                kept.append(dict(grp, hooks=keep))
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    if not hooks:
        data.pop("hooks")
    return removed


def _add_hooks(data, cli=None):
    desired = [(e, m, hook_command(n, cli), t) for e, m, n, t in HOOKS]
    if _ours_signature(data) == desired:
        return
    _strip_hooks(data)
    hooks = data.setdefault("hooks", {})
    for event, matcher, command, timeout in desired:
        hooks.setdefault(event, []).append({
            "matcher": matcher,
            "hooks": [{"type": "command", "command": command, "timeout": timeout}],
        })


def perm_rules(paths):
    rules = []
    for p in paths:
        for form in dict.fromkeys([str(p), _tilde(p)]):
            rules += [f"Bash({form} {sub} *)" for sub in PERM_SUBCOMMANDS]
    return list(dict.fromkeys(rules))


def _our_perms(data):
    perms = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
    return [r for r in perms.get("allow") or [] if isinstance(r, str) and PERM_RE.match(r)]


def _strip_perms(data):
    perms = data.get("permissions")
    if not isinstance(perms, dict) or not isinstance(perms.get("allow"), list):
        return
    perms["allow"] = [r for r in perms["allow"] if not (isinstance(r, str) and PERM_RE.match(r))]
    if not perms["allow"]:
        perms.pop("allow")
    if not perms:
        data.pop("permissions")


def _add_perms(data, rules):
    if _our_perms(data) == rules:
        return
    _strip_perms(data)
    perms = data.setdefault("permissions", {})
    perms["allow"] = perms.get("allow", []) + rules


def perm_paths(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    out = []
    for rule in _our_perms(data):
        m = PERM_PATH_RE.match(rule)
        if m:
            out.append(m.group(1))
    return out


def count_perms(path):
    try:
        return len(_our_perms(json.loads(Path(path).read_text(encoding="utf-8"))))
    except (OSError, ValueError, AttributeError):
        return 0


def _read_installed():
    try:
        data = core.read_json(installed_file())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_installed(data, dry_run):
    if dry_run:
        return
    if not data.get("parts"):
        try:
            installed_file().unlink()
        except OSError:
            pass
        return
    core.write_json(installed_file(), data)


def write_pointer():
    try:
        from . import launches
        launches.refresh_pointer()
        return
    except (ImportError, AttributeError):
        pass
    path = core.state_dir() / "cli.json"
    want = {"cli": cli_path(), "python": sys.executable, "version": __version__}
    try:
        if core.read_json(path) == want:
            return
    except (OSError, ValueError):
        pass
    core.write_json(path, want)


def pointer_state():
    try:
        data = core.read_json(core.state_dir() / "cli.json")
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def herdr_binary():
    return os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")


def _herdr(*args, timeout=30):
    binary = herdr_binary()
    if not binary:
        return 127, "", "herdr not found"
    try:
        proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)
    return proc.returncode, proc.stdout, proc.stderr


def herdr_version():
    rc, out, _ = _herdr("--version")
    if rc != 0:
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else None


def _fmt(v):
    return ".".join(str(x) for x in v)


def plugin_link_state():
    rc, out, _ = _herdr("plugin", "list")
    if rc != 0:
        return None
    for line in out.splitlines():
        if PLUGIN_ID in line:
            m = PLUGIN_LINE_RE.search(line)
            return {"path": m.group(1) if m else None, "enabled": "disabled" not in line.lower()}
    return {}


def plugin_copy_dir():
    return core.data_dir() / "herdr-plugin"


def _same_tree(a, b):
    cmp = filecmp.dircmp(a, b)
    if cmp.left_only or cmp.right_only or cmp.funny_files:
        return False
    if filecmp.cmpfiles(a, b, cmp.common_files, shallow=False)[1:] != ([], []):
        return False
    return all(_same_tree(Path(a) / d, Path(b) / d) for d in cmp.common_dirs)


def _build_plugin_copy(src, dest):
    stage = dest.with_name(f"{dest.name}.new.{os.getpid()}")
    shutil.rmtree(stage, ignore_errors=True)
    shutil.copytree(src, stage, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    manifest = stage / "herdr-plugin.toml"
    text = manifest.read_text(encoding="utf-8")
    text = re.sub(r'^(\s*command\s*=\s*\[)\s*"[^"]*"', lambda m: m.group(1) + json.dumps(sys.executable),
                  text, count=1, flags=re.M)
    manifest.write_text(text, encoding="utf-8")
    return stage


def _swap_dir(stage, dest):
    old = None
    if dest.exists():
        old = dest.with_name(f"{dest.name}.old.{os.getpid()}")
        os.rename(dest, old)
    os.rename(stage, dest)
    if old:
        shutil.rmtree(old, ignore_errors=True)


class Ctx:
    def __init__(self, args, parts):
        self.dry = args.dry_run
        self.force = args.force
        self.uninstall = args.uninstall
        self.scope = args.scope
        self.parts = parts
        self.errors = []
        self.installed = _read_installed()

    def error(self, msg):
        self.errors.append(msg)
        core.log(f"error: {msg}")


def part_settings(ctx):
    path = settings_path(ctx.scope)
    before = _load_settings(path)
    after = copy.deepcopy(before)
    bundle = plugin_bundle()
    do_hook = "hook" in ctx.parts
    if do_hook and bundle and not ctx.force and not ctx.uninstall:
        say(f"skipped hook: the plugin at {_tilde(bundle)} provides it (--force installs it anyway)")
        do_hook = False
    if do_hook:
        if ctx.uninstall:
            _strip_hooks(after)
        else:
            _add_hooks(after)
    if "perms" in ctx.parts:
        if ctx.uninstall:
            _strip_perms(after)
        else:
            paths = [cli_path(), os.path.realpath(cli_path())]
            link = bin_link()
            if "bin" in ctx.parts or (link.is_symlink() and os.path.realpath(link) == os.path.realpath(cli_path())):
                paths.append(str(link))
            _add_perms(after, perm_rules(list(dict.fromkeys(paths))))
    if do_hook and not ctx.uninstall:
        for cmd in foreign_orchestrator_hooks(after):
            say(f"warning: another SessionStart hook in {path} injects orchestrator rules ({cmd}); "
                "they would be injected twice. Remove it or set CLAUDE_ORCHESTRATOR=0")
    changed = _write_settings(path, before, after, ctx.dry)
    if not changed:
        say(f"settings unchanged: {path}")
    if not ctx.uninstall:
        ctx.installed["settings"] = str(path)
        ctx.installed["scope"] = ctx.scope


def _link_owned(link, installed):
    try:
        target = os.readlink(link)
    except OSError:
        return False
    if os.path.abspath(os.path.join(os.path.dirname(link), target)) == cli_path():
        return True
    if os.path.realpath(link) == os.path.realpath(cli_path()):
        return True
    return target == installed.get("bin_target") or target.endswith(CLI_SUFFIX)


def part_bin(ctx):
    link = bin_link()
    if ctx.uninstall:
        if link.is_symlink() and _link_owned(link, ctx.installed):
            if ctx.dry:
                say(f"would remove {link}")
            else:
                link.unlink()
                say(f"removed {link}")
        elif os.path.lexists(link):
            say(f"kept {link}: not a symlink created by setup")
        return
    target = cli_path()
    if link.is_symlink() and os.readlink(link) == target:
        say(f"bin link unchanged: {link}")
    elif os.path.lexists(link):
        if not link.is_symlink():
            if not ctx.force:
                ctx.error(f"{link} exists and is not a symlink; --force replaces it")
                return
        elif not (_link_owned(link, ctx.installed) or ctx.force):
            ctx.error(f"{link} is a symlink to {os.readlink(link)}, not to this CLI; --force replaces it")
            return
        if ctx.dry:
            say(f"would replace {link} -> {target}")
            return
        link.unlink()
        link.symlink_to(target)
        say(f"linked {link} -> {target}")
    else:
        if ctx.dry:
            say(f"would link {link} -> {target}")
            return
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        say(f"linked {link} -> {target}")
    ctx.installed["bin_link"] = str(link)
    ctx.installed["bin_target"] = target
    if str(link.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        say(f"hint: add {link.parent} to PATH to run `herdr-agent` directly")


def part_herdr(ctx):
    dest = plugin_copy_dir()
    if ctx.uninstall:
        link_state = plugin_link_state()
        if link_state:
            if ctx.dry:
                say(f"would run herdr plugin unlink {PLUGIN_ID}")
            else:
                rc, _, err = _herdr("plugin", "unlink", PLUGIN_ID)
                say(f"unlinked herdr plugin {PLUGIN_ID}" if rc == 0 else f"herdr plugin unlink failed: {err.strip()}")
        if dest.exists():
            if ctx.dry:
                say(f"would remove {dest}")
            else:
                shutil.rmtree(dest, ignore_errors=True)
                say(f"removed {dest}")
        say(f"left in place: Herdr config.toml and launch records ({core.state_dir() / 'launches'})")
        return
    version = herdr_version()
    if version is None:
        ctx.error("herdr not found or `herdr --version` failed (set HERDR_BIN_PATH or install Herdr >= "
                  f"{_fmt(MIN_HERDR)})")
        return
    if version < MIN_HERDR:
        ctx.error(f"herdr {_fmt(version)} is too old; the plugin needs >= {_fmt(MIN_HERDR)} (update Herdr)")
        return
    src = herdr_plugin_source()
    if not src:
        ctx.error("herdr-plugin/ not found next to this CLI; install the full plugin or run setup from the repo")
        return
    stage = _build_plugin_copy(src, dest)
    if dest.is_dir() and _same_tree(stage, dest):
        shutil.rmtree(stage, ignore_errors=True)
        say(f"herdr plugin copy unchanged: {dest}")
    elif ctx.dry:
        shutil.rmtree(stage, ignore_errors=True)
        say(f"would copy {src} -> {dest} (command[0] = {sys.executable})")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _swap_dir(stage, dest)
        say(f"copied herdr plugin to {dest}")
    cfg = herdrconfig.config_path()
    if ctx.dry:
        say(f"would write {core.state_dir() / 'cli.json'}")
    else:
        write_pointer()
    changed, status = herdrconfig.ensure(cfg, ctx.dry)
    if changed:
        say(f"{'would set' if ctx.dry else 'set'} resume_agents_on_restore = true in {cfg} ({status})")
        if not ctx.dry:
            herdrconfig.reload_config()
    elif status == "false":
        say(f"warning: resume_agents_on_restore = false in {cfg}; kept (agents will not be resumed on restore)")
    link_state = plugin_link_state()
    same = bool(link_state) and link_state.get("path") and \
        os.path.realpath(link_state["path"]) == os.path.realpath(dest)
    if same:
        say("herdr plugin already linked")
    elif ctx.dry:
        say(f"would run herdr plugin link {dest}")
    else:
        if link_state:
            _herdr("plugin", "unlink", PLUGIN_ID)
        rc, _, err = _herdr("plugin", "link", str(dest))
        if rc != 0:
            ctx.error(f"herdr plugin link failed: {err.strip() or 'unknown error'}")
            return
        say(f"linked herdr plugin {PLUGIN_ID}")
    ctx.installed["herdr_plugin_dir"] = str(dest)


def run_setup(args):
    scope = args.scope
    if args.only:
        parts = tuple(p.strip() for p in args.only.split(",") if p.strip())
        bad = [p for p in parts if p not in PARTS]
        if bad:
            core.die(f"unknown part(s): {', '.join(bad)} (choose from {', '.join(PARTS)})", core.EXIT_USAGE)
    else:
        parts = DEFAULT_PARTS
    if scope == "project":
        dropped = [p for p in parts if p not in PROJECT_PARTS]
        if dropped:
            say(f"project scope supports only hook and perms; ignoring {', '.join(dropped)}")
        parts = tuple(p for p in parts if p in PROJECT_PARTS)
    ctx = Ctx(args, parts)
    if ctx.dry:
        say("dry run: nothing will be written")
    if "hook" in parts or "perms" in parts:
        part_settings(ctx)
    if "bin" in parts and ctx.scope == "user":
        part_bin(ctx)
    if "herdr" in parts and ctx.scope == "user":
        part_herdr(ctx)
    state = ctx.installed
    if ctx.uninstall:
        remaining = [p for p in state.get("parts", []) if p not in parts]
        state["parts"] = remaining
        if "bin" in parts:
            state.pop("bin_link", None)
            state.pop("bin_target", None)
        if "herdr" in parts:
            state.pop("herdr_plugin_dir", None)
    else:
        done = [p for p in parts if not (p == "hook" and plugin_bundle() and not ctx.force)]
        state["parts"] = [p for p in PARTS if p in set(state.get("parts", [])) | set(done)]
        state.update(version=__version__, cli=cli_path())
    if not ctx.errors:
        _write_installed(state, ctx.dry)
    return core.EXIT_ERR if ctx.errors else core.EXIT_OK


def _check(checks, name, status, detail):
    checks.append({"name": name, "status": status, "detail": detail})


def collect_checks():
    checks = []
    py = sys.version_info
    _check(checks, "python", "ok" if py >= (3, 10) else "fail", f"{py.major}.{py.minor}.{py.micro}")

    version = herdr_version()
    if version is None:
        _check(checks, "herdr", "fail", "herdr not found on PATH (or `herdr --version` failed)")
    elif version < MIN_HERDR:
        _check(checks, "herdr", "fail", f"{_fmt(version)} < {_fmt(MIN_HERDR)}")
    else:
        _check(checks, "herdr", "ok", _fmt(version))

    if version is not None:
        rc, out, _ = _herdr("integration", "status")
        line = next((ln for ln in out.splitlines() if ln.strip().startswith("claude")), "")
        if rc == 0 and "current" in line:
            _check(checks, "herdr_integration", "ok", line.strip())
        else:
            _check(checks, "herdr_integration", "warn",
                   (line.strip() or "claude integration not reported") + " (run `herdr integration install claude`)")

    if os.environ.get("HERDR_ENV") == "1" and os.environ.get("HERDR_WORKSPACE_ID"):
        _check(checks, "herdr_env", "ok", f"workspace {os.environ['HERDR_WORKSPACE_ID']}")
    else:
        _check(checks, "herdr_env", "warn", "HERDR_ENV/HERDR_WORKSPACE_ID not set (this shell is not inside Herdr)")

    bundle = plugin_bundle()
    files = [("user", settings_path("user"))]
    top = _git_toplevel()
    if top:
        files.append(("project", top / ".claude" / "settings.json"))
    total = 0
    for scope, path in files:
        n = count_hooks(path)
        total += n
        name = f"hooks_{scope}"
        if bundle:
            if n:
                _check(checks, name, "warn", f"{n} hooks in {path} duplicate the plugin's hooks; "
                       "run `herdr-agent setup --uninstall --only hook`")
            else:
                _check(checks, name, "ok", f"none in {path} (the plugin provides them)")
        elif scope == "project" and n == 0:
            _check(checks, name, "ok", f"none in {path}")
        elif n == len(HOOKS):
            _check(checks, name, "ok", f"{n} in {path}")
        elif n == 0:
            _check(checks, name, "fail", f"no hooks in {path}; run `herdr-agent setup`")
        else:
            _check(checks, name, "fail", f"{n} hooks in {path}, expected {len(HOOKS)}; run `herdr-agent setup`")
    for scope, path in files:
        for cmd in foreign_orchestrator_in(path):
            _check(checks, f"orchestrator_hook_{scope}", "warn",
                   f"another SessionStart hook in {path} injects orchestrator rules ({cmd}); "
                   "they would be injected twice")
    if not bundle and total > len(HOOKS):
        _check(checks, "hooks_duplicates", "warn", f"{total} hooks across user and project settings")

    link = bin_link()
    running = os.path.realpath(cli_path())
    ours, foreign = [], []
    for _, p in files:
        for rule_path in perm_paths(p):
            (ours if os.path.realpath(os.path.expanduser(rule_path)) == running else foreign).append(rule_path)
    foreign_installs = sorted({os.path.realpath(os.path.expanduser(r)) for r in foreign})
    if foreign:
        _check(checks, "perms", "warn", f"{len(foreign)} allow rules for another herdr-agent install found at "
               f"{', '.join(foreign_installs)} ({len(ours)} for this CLI {running}); run `herdr-agent setup --only perms`")
    elif ours:
        _check(checks, "perms", "ok", f"{len(ours)} allow rules for {running}")
    else:
        _check(checks, "perms", "warn", "no allow rules (the pre-bash hook still auto-allows)")

    if link.is_symlink():
        target = os.path.realpath(link)
        if not (os.path.exists(link) and os.access(link, os.X_OK)):
            _check(checks, "bin", "fail", f"{link} is a broken symlink")
        elif target != running:
            _check(checks, "bin", "warn", f"another herdr-agent install found at {target} ({link} -> "
                   f"{os.readlink(link)}, this CLI is {running}); `herdr-agent setup --only bin --force` relinks it")
        else:
            _check(checks, "bin", "ok", f"{link} -> {os.readlink(link)}")
    elif link.exists():
        _check(checks, "bin", "warn", f"{link} exists but is not a symlink")
    else:
        _check(checks, "bin", "ok", "not installed (optional)")

    link_state = plugin_link_state() if version is not None else None
    if link_state is None:
        _check(checks, "herdr_plugin", "fail", "cannot query `herdr plugin list`")
    elif not link_state:
        _check(checks, "herdr_plugin", "fail", "not linked; run `herdr-agent setup --only herdr`")
    else:
        path = link_state.get("path")
        if not link_state["enabled"]:
            _check(checks, "herdr_plugin", "fail", "linked but disabled")
        elif path and not Path(path).is_dir():
            _check(checks, "herdr_plugin", "fail", f"linked directory missing: {path}")
        else:
            _check(checks, "herdr_plugin", "ok", path or "linked")
    pointer = pointer_state()
    if pointer and pointer.get("cli") and Path(pointer["cli"]).is_file():
        _check(checks, "cli_pointer", "ok", pointer["cli"])
    else:
        _check(checks, "cli_pointer", "fail", f"{core.state_dir() / 'cli.json'} missing or points to a missing CLI; "
               "run `herdr-agent setup --only herdr`")

    cfg = herdrconfig.config_path()
    st = herdrconfig.state(cfg)
    if st == "false":
        _check(checks, "resume_agents_on_restore", "warn", f"false in {cfg}; sessions will not be resumed on restore")
    elif st == "true":
        _check(checks, "resume_agents_on_restore", "ok", "true")
    else:
        _check(checks, "resume_agents_on_restore", "ok", f"not set in {cfg} (Herdr default is on)")

    claude = shutil.which("claude")
    if claude:
        try:
            proc = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=15)
            _check(checks, "claude", "ok" if proc.returncode == 0 else "fail",
                   proc.stdout.strip() or proc.stderr.strip())
        except (OSError, subprocess.SubprocessError) as e:
            _check(checks, "claude", "fail", str(e))
    else:
        _check(checks, "claude", "fail", "claude not found on PATH")

    records = list((core.state_dir() / "launches").glob("*.json"))
    last = ""
    try:
        lines = (core.state_dir() / "restore.log").read_text(encoding="utf-8").strip().splitlines()
        last = lines[-1] if lines else ""
    except OSError:
        pass
    _check(checks, "records", "ok", f"{len(records)} launch records" + (f"; last restore: {last}" if last else ""))
    return checks


def run_doctor(args):
    checks = collect_checks()
    ok = all(c["status"] != "fail" for c in checks)
    if args.json:
        say(json.dumps({"ok": ok, "version": __version__, "cli": cli_path(), "checks": checks}, indent=2))
    else:
        marks = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
        for c in checks:
            say(f"[{marks[c['status']]}] {c['name']}: {c['detail']}")
        say("all checks passed" if ok else "some checks failed")
    return core.EXIT_OK if ok else core.EXIT_ERR


def build_parser():
    p = argparse.ArgumentParser(prog="herdr-agent")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup", help="install hooks, permissions, bin link and the Herdr plugin")
    s.add_argument("--scope", choices=("user", "project"), default="user")
    s.add_argument("--only", help="comma-separated parts: " + ",".join(PARTS))
    s.add_argument("--uninstall", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true")
    d = sub.add_parser("doctor", help="check the installation")
    d.add_argument("--json", action="store_true")
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    try:
        return run_setup(args) if args.command == "setup" else run_doctor(args)
    except CliError as e:
        core.log(f"error: {e}")
        return e.code
