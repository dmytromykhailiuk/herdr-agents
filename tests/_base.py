import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAKES = REPO / "tests" / "fakes"
CLI = REPO / "skills" / "herdr-agent" / "bin" / "herdr-agent"
LIB = REPO / "skills" / "herdr-agent" / "lib"

SCRUB_PREFIXES = ("HERDR_", "CLAUDE_", "ANTHROPIC_")


class Result:
    def __init__(self, proc):
        self.returncode = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr

    def json(self):
        return json.loads(self.stdout)


class IsolatedTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="herdr-agents-test-")))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = self.root / "home"
        self.tmp = self.root / "tmp"
        self.claude_dir = self.home / ".claude"
        self.state_dir = self.root / "state"
        self.data_dir = self.root / "data"
        self.agent_root = self.root / "agents"
        self.fake_state = self.root / "fake-herdr.json"
        for d in (self.home, self.tmp, self.claude_dir, self.state_dir, self.data_dir):
            d.mkdir(parents=True)
        self.env = self.make_env()

    def make_env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(SCRUB_PREFIXES)}
        env.update(
            HOME=str(self.home),
            XDG_CONFIG_HOME=str(self.home / ".config"),
            XDG_STATE_HOME=str(self.home / ".local" / "state"),
            XDG_DATA_HOME=str(self.home / ".local" / "share"),
            XDG_CACHE_HOME=str(self.home / ".cache"),
            TMPDIR=str(self.tmp),
            CLAUDE_CONFIG_DIR=str(self.claude_dir),
            HERDR_AGENTS_STATE_DIR=str(self.state_dir),
            HERDR_AGENTS_DATA_DIR=str(self.data_dir),
            HERDR_AGENT_ROOT=str(self.agent_root),
            HERDR_CONFIG_PATH=str(self.home / ".config" / "herdr" / "config.toml"),
            FAKE_HERDR_STATE=str(self.fake_state),
            PATH=f"{FAKES}:/usr/bin:/bin",
            PYTHONDONTWRITEBYTECODE="1",
        )
        env.update(extra)
        return env

    def herdr_env(self, **extra):
        return self.make_env(
            HERDR_ENV="1",
            HERDR_WORKSPACE_ID="w1",
            HERDR_PANE_ID="w1:p1",
            HERDR_TAB_ID="w1:t1",
            **extra,
        )

    def run_cli(self, *args, env=None, stdin=None, timeout=60, cwd=None):
        proc = subprocess.run(
            [sys.executable, str(CLI), *args],
            env=env if env is not None else self.env,
            input=stdin, capture_output=True, text=True, timeout=timeout,
            cwd=str(cwd or self.root),
        )
        return Result(proc)

    def run_hook(self, name, payload, env=None, timeout=30):
        data = payload if isinstance(payload, str) else json.dumps(payload)
        return self.run_cli("hook", name, env=env, stdin=data, timeout=timeout)

    def fake_read(self):
        try:
            return json.loads(self.fake_state.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def fake_write(self, state):
        self.fake_state.write_text(json.dumps(state), encoding="utf-8")

    def fake_set(self, **knobs):
        state = self.fake_read()
        state.update(knobs)
        self.fake_write(state)

    def fake_calls(self, mutating=None):
        calls = self.fake_read().get("calls", [])
        if mutating is not None:
            calls = [c for c in calls if c["mutating"] == mutating]
        return [c["argv"] for c in calls]

    def fake_panes(self):
        return self.fake_read().get("panes", {})

    def fake_tabs(self):
        return self.fake_read().get("tabs", {})

    def fake_add_pane(self, pane_id="w1:p1", tab_id="w1:t1", workspace_id="w1", cwd=None, **fields):
        state = self.fake_read()
        pane = {
            "pane_id": pane_id, "tab_id": tab_id, "workspace_id": workspace_id,
            "terminal_id": "term_seed" + pane_id.replace(":", ""),
            "cwd": str(cwd or self.root), "env": {}, "shell_pid": 31000 + len(state.get("panes", {})) * 10,
            "shell_name": "zsh", "agent_status": "unknown", "agent": None, "agent_session": None,
            "agent_name": None, "process_argv": None, "shell_calls": 0, "revision": 0, "keys": [], "screen": "",
        }
        pane.update(fields)
        state.setdefault("tabs", {}).setdefault(tab_id, {
            "tab_id": tab_id, "workspace_id": workspace_id, "label": "seed", "number": len(state.get("tabs", {})) + 1,
        })
        state.setdefault("panes", {})[pane_id] = pane
        self.fake_write(state)
        return pane
