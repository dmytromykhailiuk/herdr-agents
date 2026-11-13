import json
import os
import sys
import tempfile
from pathlib import Path

EXIT_OK, EXIT_ERR, EXIT_USAGE, EXIT_BLOCKED, EXIT_TIMEOUT, EXIT_DEAD, EXIT_NORESULT = 0, 1, 2, 3, 4, 5, 6
EXIT_API_ERROR = 7
EXIT_INTERRUPTED = 130

OFF_VALUES = ("off", "0", "false", "no")

CLI_PATH = ""


class CliError(Exception):
    def __init__(self, msg, code=EXIT_ERR):
        super().__init__(msg)
        self.code = code


def log(msg):
    print(f"[herdr-agent] {msg}", file=sys.stderr, flush=True)


def die(msg, code=EXIT_ERR):
    raise CliError(msg, code)


def env_off(name, default_on=True, env=None):
    value = (env if env is not None else os.environ).get(name)
    if value is None or value.strip() == "":
        return not default_on
    return value.strip().lower() in OFF_VALUES


def env_on(name, default_on=True, env=None):
    return not env_off(name, default_on, env)


def env_truthy(name, env=None):
    value = (env if env is not None else os.environ).get(name)
    return value is not None and value.strip().lower() in ("1", "true", "yes", "on")


def env_int(name, default, env=None):
    value = (env if env is not None else os.environ).get(name)
    try:
        return int(value) if value is not None and value.strip() != "" else default
    except ValueError:
        return default


def home():
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


def claude_config_dir(env=None):
    value = (env if env is not None else os.environ).get("CLAUDE_CONFIG_DIR")
    return Path(os.path.expanduser(value)) if value else home() / ".claude"


def state_dir(env=None):
    e = env if env is not None else os.environ
    if e.get("HERDR_AGENTS_STATE_DIR"):
        return Path(os.path.expanduser(e["HERDR_AGENTS_STATE_DIR"]))
    base = e.get("XDG_STATE_HOME")
    return (Path(base) if base else home() / ".local" / "state") / "herdr-agents"


def data_dir(env=None):
    e = env if env is not None else os.environ
    if e.get("HERDR_AGENTS_DATA_DIR"):
        return Path(os.path.expanduser(e["HERDR_AGENTS_DATA_DIR"]))
    base = e.get("XDG_DATA_HOME")
    return (Path(base) if base else home() / ".local" / "share") / "herdr-agents"


def agent_root(env=None):
    e = env if env is not None else os.environ
    if e.get("HERDR_AGENT_ROOT"):
        return Path(os.path.expanduser(e["HERDR_AGENT_ROOT"]))
    return Path(e.get("TMPDIR") or "/tmp") / "herdr-agents"


def resolve_cli_path(running, env=None):
    override = (env if env is not None else os.environ).get("HERDR_AGENT_BIN")
    return os.path.abspath(os.path.expanduser(override)) if override else os.path.abspath(running)


def in_herdr(env=None):
    return (env if env is not None else os.environ).get("HERDR_ENV") == "1"


def agent_depth(env=None):
    return env_int("HERDR_AGENT_DEPTH", 0, env)


def max_depth(env=None):
    return env_int("HERDR_AGENT_MAX_DEPTH", 2, env)


STATE_DIR = state_dir()
DATA_DIR = data_dir()
AGENT_ROOT = agent_root()


def atomic_write(path, data, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path, data, mode=0o644):
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
