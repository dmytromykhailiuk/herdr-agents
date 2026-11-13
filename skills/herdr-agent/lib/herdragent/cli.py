"""herdr-agent: run Claude sub-agents in their own Herdr tabs.

Each agent gets a unique id (<role>-<6 hex>) used as the Herdr agent name, the
tab label and the name of its working directory under $HERDR_AGENT_ROOT
(default: $TMPDIR/herdr-agents). A per-agent --settings file installs a Stop
hook that writes the agent's final message to <dir>/result.md atomically, so
results never depend on scraping the terminal.

Exit codes (spawn/wait/run/send):
  0 done   1 error   2 usage   3 blocked (needs input)   4 timeout
  5 agent died / tab closed   6 finished but no result captured
  7 api_error (turn ended on a Claude Code API error; tab and files kept)   130 interrupted
"""
import importlib
import sys

from . import core
from .core import EXIT_ERR, EXIT_INTERRUPTED, EXIT_OK, EXIT_USAGE, CliError

HOOK_ROUTES = {
    "session-start": ("hooks.session_start", False),
    "pre-agent": ("hooks.pre_tool", True),
    "pre-bash": ("hooks.pre_tool", True),
    "stop": ("hooks.stop", False),
    "session-end": ("hooks.session_end", False),
}
HERDR_ROUTES = {"startup": "restore", "status": "restore"}
TOP_ROUTES = {"setup": "setup", "doctor": "setup"}


def load_module(name):
    return importlib.import_module(f"{__package__}.{name}")


def route_hook(argv):
    if not argv or argv[0] not in HOOK_ROUTES:
        print(f"usage: herdr-agent hook {{{','.join(HOOK_ROUTES)}}}", file=sys.stderr)
        return EXIT_ERR
    module, with_name = HOOK_ROUTES[argv[0]]
    try:
        mod = load_module(module)
        rc = mod.main(argv if with_name else argv[1:])
    except Exception as e:
        core.log(f"hook {argv[0]} failed: {e!r}")
        return EXIT_OK
    return rc if isinstance(rc, int) and rc != EXIT_USAGE else EXIT_OK


def route_module(argv, routes, prefix):
    if not argv or argv[0] not in routes:
        print(f"usage: herdr-agent {prefix}{{{','.join(routes)}}} ...", file=sys.stderr)
        return EXIT_USAGE
    try:
        mod = load_module(routes[argv[0]])
    except ImportError as e:
        core.log(f"error: {prefix}{argv[0]} is not available ({e})")
        return EXIT_ERR
    try:
        rc = mod.main(argv)
    except CliError as e:
        core.log(f"error: {e}")
        return e.code
    return rc if isinstance(rc, int) else EXIT_OK


def build_parser():
    import argparse

    from . import commands

    ap = argparse.ArgumentParser(prog="herdr-agent", description=__doc__, allow_abbrev=False,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Extra claude CLI args go after a bare --, e.g. "
                                        "run reviewer -p ... -- --allowedTools Read Grep")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, **kw):
        return sub.add_parser(name, allow_abbrev=False, **kw)

    def prompt_opts(p):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--prompt", "-p", help="prompt text, or '-' to read stdin")
        g.add_argument("--prompt-file", "-f")

    def spawn_opts(p):
        p.add_argument("role", nargs="?",
                       help="short role name, e.g. reviewer; becomes <role>-<hex> (default: --agent NAME). "
                            "Roles explore and plan are read-only")
        prompt_opts(p)
        p.add_argument("--cwd", help="working directory (default: current)")
        p.add_argument("--model", help="claude model alias/id, e.g. sonnet, haiku, opus")
        p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                       default="medium", help="reasoning effort (default: medium)")
        p.add_argument("--permission-mode", default="auto",
                       help="claude permission mode for the agent (default: auto; '' to use claude's own "
                            "default). A --permission-mode after -- takes precedence")
        p.add_argument("--agent", metavar="NAME", help="run claude with --agent NAME (custom agent definition)")
        p.add_argument("--read-only", action="store_true",
                       help="read-only preset: disable editing tools and ask for read-only research")
        p.add_argument("--workspace", help="herdr workspace (default: caller's)")
        p.add_argument("--focus", action="store_true", help="switch to the new tab")
        p.add_argument("--env", action="append", metavar="KEY=VAL",
                       help="extra environment variable for the agent's tab (repeatable)")
        p.add_argument("--start-timeout", type=float, default=60, help="seconds (default 60)")
        p.add_argument("--no-system-prompt", action="store_true",
                       help="do not append the sub-agent reporting instructions")
        p.add_argument("--delegate", action="store_true",
                       help="allow the agent to spawn its own read-only sub-agents (one level deeper)")

    def out_opts(p):
        p.add_argument("--json", action="store_true", help="print one JSON document")
        p.add_argument("--keep-files", action="store_true", help="keep the agent dir after closing")

    p = add("run", help="spawn, wait, print result, close tab")
    spawn_opts(p)
    out_opts(p)
    p.add_argument("--timeout", type=float, default=0, help="seconds, 0 = no limit")
    p.add_argument("--keep", action="store_true", help="leave the tab open after success")
    p.set_defaults(fn=commands.cmd_run)

    p = add("spawn", help="spawn and send the prompt, return immediately")
    spawn_opts(p)
    out_opts(p)
    p.set_defaults(fn=commands.cmd_spawn)

    p = add("wait", help="wait for the current turn and print the result")
    p.add_argument("id")
    out_opts(p)
    p.add_argument("--timeout", type=float, default=0)
    p.add_argument("--close", action="store_true", help="close the tab on success")
    p.set_defaults(fn=commands.cmd_wait)

    p = add("send", help="send a follow-up prompt to a live agent")
    p.add_argument("id")
    prompt_opts(p)
    out_opts(p)
    p.add_argument("--wait", action="store_true")
    p.add_argument("--timeout", type=float, default=0)
    p.add_argument("--close", action="store_true", help="with --wait: close on success")
    p.set_defaults(fn=commands.cmd_send)

    p = add("keys", help="send keys to answer a dialog, wait until unblocked")
    p.add_argument("id")
    p.add_argument("keys", nargs="+", help="e.g. enter, down enter, esc, 1")
    p.set_defaults(fn=commands.cmd_keys)

    p = add("result", help="print the latest result.md")
    p.add_argument("id")
    p.set_defaults(fn=commands.cmd_result)

    p = add("screen", help="print the agent's terminal")
    p.add_argument("id")
    p.add_argument("--lines", type=int, default=60)
    p.set_defaults(fn=commands.cmd_screen)

    p = add("status", help="meta + live state as JSON")
    p.add_argument("id")
    p.set_defaults(fn=commands.cmd_status)

    p = add("list", help="list known agents")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=commands.cmd_list)

    p = add("close", help="close the agent's tab and remove its files")
    p.add_argument("id")
    p.add_argument("--keep-files", action="store_true")
    p.add_argument("--force", action="store_true", help="close even while working")
    p.set_defaults(fn=commands.cmd_close)

    p = add("gc", help="remove dirs of agents whose tabs are gone")
    p.add_argument("--older-than", type=float, default=24, help="hours (default 24)")
    p.add_argument("--close-idle", action="store_true", help="also close idle agents older than cutoff")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=commands.cmd_gc)

    p = add("path", help="print the absolute path of this CLI")
    p.set_defaults(fn=commands.cmd_path)

    p = add("version", help="print the version and integration number")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=commands.cmd_version)

    add("hook", help="Claude Code hook entry points: session-start, session-end, pre-agent, pre-bash, stop")
    add("herdr", help="Herdr plugin entry points: startup, status")
    add("setup", help="install hooks, permissions, bin link and the Herdr plugin", add_help=False)
    add("doctor", help="check the installation", add_help=False)
    return ap


def split_extra(argv):
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def on_signal(signum, _frame):
    core.log("interrupted; agent keeps running in its tab (use `herdr-agent list`)")
    sys.exit(EXIT_INTERRUPTED)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "hook":
        return route_hook(argv[1:])
    if argv and argv[0] == "herdr":
        return route_module(argv[1:], HERDR_ROUTES, "herdr ")
    if argv and argv[0] in TOP_ROUTES:
        return route_module(argv, TOP_ROUTES, "")

    argv, extra = split_extra(argv)
    args = build_parser().parse_args(argv)
    args.claude_args = extra

    import signal
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    from .herdr import HerdrError
    try:
        return args.fn(args)
    except HerdrError as e:
        core.log(f"error: {e}")
        return EXIT_ERR
    except CliError as e:
        core.log(f"error: {e}")
        return e.code
