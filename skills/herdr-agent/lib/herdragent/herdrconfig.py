import os
import re
import shutil
import subprocess
from pathlib import Path

from . import core

KEY = "resume_agents_on_restore"
HEADER_RE = re.compile(r"^\s*\[(?!\[)\s*([^\[\]]*?)\s*\]\s*(?:#.*)?$")
ANY_HEADER_RE = re.compile(r"^\s*\[")
KEY_RE = re.compile(r"^\s*" + KEY + r"\s*=\s*([^\s#]+)")
DOTTED_RE = re.compile(r"^\s*session\s*\.\s*" + KEY + r"\s*=\s*([^\s#]+)")
INLINE_RE = re.compile(r"^\s*session\s*=\s*\{")


def config_path(env=None):
    e = env if env is not None else os.environ
    if e.get("HERDR_CONFIG_PATH"):
        return Path(os.path.expanduser(e["HERDR_CONFIG_PATH"]))
    base = e.get("XDG_CONFIG_HOME")
    return (Path(base) if base else core.home() / ".config") / "herdr" / "config.toml"


def _value(raw):
    return raw.strip().strip("'\"").lower()


def inspect(text):
    lines = text.splitlines()
    in_session = False
    header = None
    for i, line in enumerate(lines):
        m = DOTTED_RE.match(line)
        if m:
            return "true" if _value(m.group(1)) == "true" else "false", None
        if INLINE_RE.match(line):
            return "inline", None
        h = HEADER_RE.match(line)
        if h:
            in_session = h.group(1) == "session"
            if in_session and header is None:
                header = i
            continue
        if ANY_HEADER_RE.match(line):
            in_session = False
            continue
        if in_session:
            k = KEY_RE.match(line)
            if k:
                return ("true" if _value(k.group(1)) == "true" else "false"), header
    return ("absent" if header is not None else "no-table"), header


def state(path=None):
    path = Path(path) if path else config_path()
    if not path.is_file():
        return "no-file"
    return inspect(path.read_text(encoding="utf-8"))[0]


def ensure(path=None, dry_run=False):
    path = Path(path) if path else config_path()
    exists = path.is_file()
    if not exists:
        new, status = f"[session]\n{KEY} = true\n", "created"
    else:
        text = path.read_text(encoding="utf-8")
        status, header = inspect(text)
        if status in ("true", "false", "inline"):
            return False, status
        nl = "\r\n" if "\r\n" in text else "\n"
        lines = text.splitlines(keepends=True)
        if status == "no-table":
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += nl
            if lines:
                lines.append(nl)
            lines += ["[session]" + nl, f"{KEY} = true" + nl]
            status = "table-added"
        else:
            if not lines[header].endswith(("\n", "\r")):
                lines[header] += nl
            lines.insert(header + 1, f"{KEY} = true" + nl)
            status = "key-added"
        new = "".join(lines)
    if dry_run:
        return True, status
    if exists:
        bak = path.with_name(path.name + ".bak")
        if not os.path.lexists(bak):
            shutil.copy2(path, bak)
        mode = path.stat().st_mode & 0o777
    else:
        mode = 0o644
    core.atomic_write(path, new, mode)
    return True, status


def ensure_resume_on(path=None, dry_run=False):
    changed, status = ensure(path, dry_run)
    if status == "false":
        core.log(f"{KEY} is false in the Herdr config; leaving it (agents will not be resumed on restore)")
    elif status == "inline":
        core.log("Herdr config defines [session] as an inline table; not editing it")
    return changed


def reload_config():
    binary = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if not binary:
        return False
    try:
        proc = subprocess.run([binary, "server", "reload-config"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0
