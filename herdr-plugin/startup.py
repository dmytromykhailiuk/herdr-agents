import json
import os
import shutil
import sys


def state_dir():
    env = os.environ
    if env.get("HERDR_AGENTS_STATE_DIR"):
        return os.path.expanduser(env["HERDR_AGENTS_STATE_DIR"])
    base = env.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "herdr-agents")


def main():
    try:
        with open(os.path.join(state_dir(), "cli.json"), encoding="utf-8") as f:
            data = json.load(f)
        cli, python = data["cli"], data.get("python") or sys.executable
        if os.path.isfile(cli):
            if not os.path.isfile(python):
                python = sys.executable
            os.execv(python, [python, cli, "herdr", "startup"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    found = shutil.which("herdr-agent")
    if found:
        os.execv(found, [found, "herdr", "startup"])
    print("herdr-agents CLI not found; run herdr-agent setup --only herdr", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
