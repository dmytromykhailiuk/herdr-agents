import json
import os
import sys

from .. import core


def environ(env=None):
    return env if env is not None else os.environ


def hooks_off(env=None):
    return core.env_off("HERDR_AGENTS", env=environ(env))


def is_sdk(env=None):
    return environ(env).get("CLAUDE_CODE_ENTRYPOINT", "").strip().lower().startswith("sdk")


def parse_payload(raw):
    try:
        data = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def read_stdin():
    try:
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def cli_path():
    if core.CLI_PATH:
        return core.CLI_PATH
    from ..context import default_cli_path
    return default_cli_path()


def cli_realpaths():
    from ..context import DEFAULT_CLI
    paths = {os.path.realpath(str(DEFAULT_CLI))}
    if core.CLI_PATH:
        paths.add(os.path.realpath(core.CLI_PATH))
    return paths
