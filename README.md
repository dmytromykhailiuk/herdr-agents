# herdr-agents

A Claude Code plugin that runs Claude sub-agents as real interactive sessions in [Herdr](https://herdr.dev) tabs. Each
agent gets its own tab that you can watch (or ignore); the tab closes when the agent finishes, and the agent's final
message comes back through a file, never by scraping the terminal.

| Piece | What it does |
|---|---|
| `herdr-agent` skill + CLI | `run`, `spawn`, `wait`, `send`, `keys`, `close`, `list`, … : one Claude session per Herdr tab, result captured by a per-agent Stop hook. Python 3.10+, stdlib only. |
| `orchestrator` skill | Orchestrator mode: the main session delegates all task work and keeps its own context small. Injected into every main session at start; opt out with `CLAUDE_ORCHESTRATOR=off`. |
| SessionStart hook | Injects the orchestrator rules and the Herdr rules (CLI path, plan-mode commands), exports `$HERDR_AGENT`, records the launch for restore. |
| SessionEnd hook | Marks a recorded main session as ended when the user quits it (`/exit`, Ctrl-D, logout), so restore does not reopen it. |
| PreToolUse hooks | Inside Herdr, blocks the built-in Agent tool and names the `herdr-agent` command to run instead (plan-mode Explore/Plan included); auto-allows `herdr-agent` Bash commands. |
| Herdr plugin | After a Herdr restart, relaunches agents and main sessions that were started with extra flags or behaviour env (`CLAUDE_CONFIG_DIR`, `CLAUDE_ORCHESTRATOR`, `HERDR_AGENTS_*`, ...), with their original flags and env. |

## Requirements

- [Herdr](https://herdr.dev) ≥ 0.9.2 with its Claude integration (`herdr integration install claude`).
- Python ≥ 3.10. The `bin/herdr-agent` shim finds one itself (`python3.14` … `python3.10` on `PATH`, Homebrew, pyenv shims);
  set `HERDR_AGENTS_PYTHON` to force an interpreter.
- Claude Code.

Run the CLI inside Herdr (`HERDR_ENV=1`). Outside Herdr the hooks do nothing and sub-agents go through the Agent tool as usual.

## Install

### Claude Code plugin

```
/plugin marketplace add dmytromykhailiuk/herdr-agents
/plugin install herdr-agents@herdr-agents
```

This installs the skills and the hooks. The Herdr restore plugin is a separate, global registration in Herdr, so install it once:

```
herdr-agent setup --only herdr
```

`herdr-agent` is not on `PATH` after a plugin install. Get its absolute path from:

- the session context: the SessionStart hook prints `herdr-agent CLI: '<path>'` at the start of every session;
- `$HERDR_AGENT` in any Bash call inside a Claude session in Herdr (`echo "$HERDR_AGENT"`);
- `herdr-agent path`, once the CLI is on `PATH`.

The simplest way: inside Herdr, ask Claude to run `"$HERDR_AGENT" setup --only herdr`.

The path points into the plugin cache (`~/.claude/plugins/cache/…/skills/herdr-agent/bin/herdr-agent`), so after a plugin update
rerun `setup --only herdr` with the new path. `setup --only herdr,bin` also links the CLI to `~/.local/bin/herdr-agent`
(`$HERDR_AGENT_BIN_DIR` overrides the directory) so `herdr-agent` works from any shell.

### Without the plugin system

```
git clone https://github.com/dmytromykhailiuk/herdr-agents
cd herdr-agents
skills/herdr-agent/bin/herdr-agent setup
```

`setup` writes the hooks and permission rules into `~/.claude/settings.json` and installs the Herdr restore plugin. Link the
two skills (`skills/herdr-agent`, `skills/orchestrator`) into `~/.claude/skills/` to get the skill texts. Add `--only
hook,perms,bin,herdr` to pick parts.

### `herdr-agent setup`

```
herdr-agent setup [--scope user|project] [--only hook,perms,bin,herdr] [--dry-run] [--uninstall] [--force]
```

| Part | What it installs |
|---|---|
| `hook` | SessionStart, SessionEnd and PreToolUse (`Agent\|Task`, `Bash`) hooks in `settings.json`. Skipped when the CLI runs from an installed plugin (the plugin provides them); `--force` installs them anyway. |
| `perms` | `Bash(<cli> run\|spawn\|wait\|send\|status\|result\|list\|screen *)` allow rules for the CLI path (and its realpath, and the bin link). |
| `bin` | Symlink `~/.local/bin/herdr-agent` → the CLI. Not part of the default parts. |
| `herdr` | Copies `herdr-plugin/` to `~/.local/share/herdr-agents/herdr-plugin`, links it with `herdr plugin link`, writes the CLI pointer (`cli.json` in the state dir) and sets `resume_agents_on_restore = true` in Herdr's `config.toml` when it is not set. |

Default parts: `hook,perms,herdr`. `--scope project` writes `<repo>/.claude/settings.json` and supports only `hook` and `perms`.
Setup is idempotent, keeps every other key in `settings.json` and saves a `settings.json.bak` before the first change.
`--dry-run` prints what would change.

## Use

Call the CLI by its absolute path (below: `$HA`, which is `$HERDR_AGENT` in Bash).

```
$HA run <role> --model sonnet --prompt "<self-contained task>" [--timeout SEC] [-- <claude args>]
```

`run` opens a tab, sends the prompt, waits, prints the agent's final message on stdout (status lines go to stderr), then
closes the tab and removes the agent's files. Run anything long with `run_in_background: true`; for parallel agents make one
background Bash call per agent.

| Command | What it does |
|---|---|
| `run <role>` | spawn, wait, print the result, close the tab |
| `spawn <role>` | spawn, send the prompt, print the id and return |
| `wait <id> [--close]` | wait for the current turn and print its result |
| `send <id> --prompt … [--wait] [--close]` | follow-up prompt to a live agent (refused while it is working) |
| `keys <id> <keys…>` | answer a dialog (`enter`, `down enter`, `esc`, `1`) and wait until unblocked |
| `result <id>` / `screen <id> [--lines N]` / `status <id>` | latest result / terminal snapshot / meta and live state as JSON |
| `list [--json]` | all known agents with live state |
| `close <id> [--keep-files] [--force]` | close the tab and remove the files |
| `gc [--older-than H] [--close-idle] [--dry-run]` | remove directories of agents whose tabs are gone |
| `path`, `version [--json]` | the CLI's absolute path; version and integration number |
| `setup`, `doctor`, `hook …`, `herdr …` | installation, diagnostics, hook and Herdr-plugin entry points |

Options of `run`/`spawn`:

| Option | Meaning |
|---|---|
| `<role>` | Free label, becomes the id `<role>-<6 hex>` (Herdr agent name, tab label, directory `$TMPDIR/herdr-agents/<id>/`). `explore` and `plan` are read-only roles. Defaults to `--agent NAME`. |
| `--prompt TEXT` / `--prompt -` / `--prompt-file PATH` | The task. The agent sees nothing else from your conversation. |
| `--model M`, `--effort low\|medium\|high\|xhigh\|max` | Model alias/id and reasoning effort (default `medium`). |
| `--read-only` | Read-only preset for any role: Edit, Write, NotebookEdit, MultiEdit, Agent, Task, EnterPlanMode and ExitPlanMode are disabled. |
| `--agent NAME` | Start the agent as the Claude Code custom agent `NAME` (`claude --agent NAME`). |
| `--delegate` | Let the agent spawn its own read-only sub-agents, one level deeper. |
| `--permission-mode M` | Default `auto`; `''` keeps Claude's own default. A `--permission-mode` after `--` wins. |
| `--cwd DIR`, `--workspace W`, `--focus` | Working directory, Herdr workspace, switch to the new tab. |
| `--env KEY=VAL` | Extra env for the agent's tab (repeatable). `HERDR_*` keys are reserved. |
| `--timeout SEC` | `run` only; `0` = no limit. `--keep` leaves the tab open after success. |
| `--json`, `--keep-files` | One JSON document (includes `session_id`); keep the agent directory. |
| `-- <claude args>` | Anything after a bare `--` goes to `claude`, e.g. `-- --allowedTools Read Grep`. |

### Exit codes

| Code | Status | Meaning |
|---|---|---|
| 0 | done | result on stdout |
| 1 | error / busy | read stderr; busy means the agent is mid-turn |
| 2 | usage | fix the command line |
| 3 | blocked | the agent waits on a dialog (permission, folder trust) or the cwd is not trusted; answer with `keys`, then `wait <id> --close` |
| 4 | timeout | still running; `wait <id> --close` later |
| 5 | dead | the tab was closed or Claude exited; the result is lost |
| 6 | no_result | the agent went idle without a final message; look at `screen <id>` |
| 7 | api_error | the turn ended on a Claude Code API error; the CLI already sent one automatic `continue`; tab and files are kept; resume with `send <id> --prompt continue --wait --close` |
| 130 | interrupted | the agent keeps running in its tab; `wait <id>` resumes |

Never approve an agent's permission or folder-trust dialog on your own judgment: show it to the user. An untrusted cwd (never
opened in Claude, no trusted parent) is refused before any tab opens: run `claude` there once and accept the dialog.
A nested git repo (or a linked worktree of one) inside a trusted folder, which Claude Code would ask about again, is
trusted automatically: herdr-agent sets `hasTrustDialogAccepted` for that one project in `.claude.json` (atomic write,
first-time backup `.claude.json.herdr-agent.bak`) and logs `auto-trusted <repo>`.
It also copies the parent's `.mcp.json` approvals (`enableAllProjectMcpServers`, `enabledMcpjsonServers`,
`disabledMcpjsonServers` from its `.claude/settings.local.json`, `.claude/settings.json` and legacy `.claude.json` entry) into the
repo's `.claude/settings.local.json`, where Claude Code's "New MCP server found" dialog stores its answer: lists are merged, an
explicit value already in the repo is kept, and it logs `inherited MCP approvals for <repo> from <parent>`. That file is personal and
conventionally git-ignored; add `.claude/settings.local.json` to the repo's `.gitignore`. `HERDR_AGENT_AUTO_TRUST=off` disables both.

### Plan mode

Inside Herdr the built-in Explore and Plan agents are replaced by read-only `herdr-agent` roles, so plan mode works with
visible tabs:

- Explore → `$HA run explore --model haiku --prompt-file <file>`
- Plan → `$HA run plan --delegate --model opus --prompt-file <file>`

When Claude calls the Agent tool in plan mode, the PreToolUse hook denies it and puts the ready-made command (prompt written to
a file) in the deny reason. Both roles disable the editing tools, so they do not violate plan mode. The main session writes the
plan file from the result and calls ExitPlanMode. Read-only sub-agents run in `auto` permission mode, not in plan mode (a
sub-agent in plan mode would end on an ExitPlanMode dialog).

### Depth and delegation

An agent may spawn its own sub-agents only when its parent started it with `--delegate`; those run one level deeper and cannot
delegate further (`HERDR_AGENT_MAX_DEPTH`, default 2). Without `--delegate`, a spawn attempt from a sub-agent is refused.

## Orchestrator mode

Every main Claude session gets the `orchestrator` skill text injected at start: the main agent never does task work itself (no
code reading, edits, investigation, review, QA or commands). It splits the work, picks model and effort per subtask, delegates
through `herdr-agent` inside Herdr (the Agent tool outside it) and passes only condensed results along.

**Opt out:** set `CLAUDE_ORCHESTRATOR=off` (`0`, `false`, `no`) in the environment, e.g. in the `env` block of
`settings.json`, to stop the injection. Saying "do it yourself" lifts the rules for that one request. Sub-agents never receive
the orchestrator text; `/orchestrator` loads the skill by hand. Set `HERDR_AGENTS=off` to switch off all herdr-agents hooks.

## Restore after a Herdr restart

What Herdr does natively (≥ 0.9.2, `resume_agents_on_restore`, default on): it relaunches `claude --resume <session>` in the
panes that held a Claude session. What it does not do is reproduce the original flags and env, so an agent would come back
without `--settings` (no result capture), `--agent`, `--model`, tools or its `CLAUDE_CONFIG_DIR` profile.

The bundled Herdr plugin (`herdr-plugin/`, a `[[startup]]` hook running `herdr-agent herdr startup`) adds that:

1. At launch, agents spawned by the CLI and main sessions started with extra flags or behaviour env are recorded as JSON
   files in `<state>/launches/`. Behaviour env is an allowlist read from the session's environment: a non-default
   `CLAUDE_CONFIG_DIR`, `CLAUDE_ORCHESTRATOR`, `HERDR_AGENT_AUTO_TRUST`, `HERDR_AGENT_MAX_DEPTH` and every `HERDR_AGENTS_*`
   variable. It is passed to the new tab on restore, so e.g. `CLAUDE_ORCHESTRATOR=off` survives a restart.
2. On Herdr start it polls for up to the grace period (600 s) for the native relaunches. A pane whose `claude` process is
   missing the recorded flags is replaced: a new tab with the recorded env, the old pane closed, and
   `claude --resume <session> <original flags>` started in it. Spawn records get their agent directory and `--settings` file
   recreated.
3. Panes that are healthy, busy, working or running a different session are left alone and never killed. Records with no
   matching pane are dropped after two startups. A main session the user quit (SessionEnd `prompt_input_exit` or `logout`)
   whose pane sits at a shell is not reopened and its record is dropped; a Herdr shutdown does not count as quitting.

Replacing a relaunched pane appends a new tab; tab order and any splits around the old pane are not kept.

**Turning restore off.** The two mechanisms are independent: `[session] resume_agents_on_restore = false` in Herdr's
`config.toml` disables only Herdr's native resume. The package's restore does not read it as an opt-out (it exists exactly
to fix sessions that native resume brings back without their flags and env) and still relaunches recorded sessions. To
disable the package's restore, set `HERDR_AGENTS_RESTORE=off` in the Herdr server's environment; to stop recording new
sessions, set `HERDR_AGENTS_RECORD=off`.

```
herdr-agent herdr status [--json]     # records and how each one currently matches a pane
herdr-agent herdr startup --dry-run   # one pass, prints decisions, changes nothing
```

Logs: `<state>/restore.log` (and `$HERDR_PLUGIN_STATE_DIR/restore.log` when Herdr sets it). The `startup` hook detaches into the
background when Herdr runs it and always exits 0.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `CLAUDE_ORCHESTRATOR` | on | `off` disables the orchestrator text injection. |
| `HERDR_AGENTS` | on | `off` disables all herdr-agents hooks (session start, redirect, auto-allow). |
| `HERDR_AGENTS_REDIRECT` | on | `off` stops denying the Agent tool inside Herdr. |
| `HERDR_AGENTS_PASSTHROUGH` | `claude-code-guide,statusline-setup` | Comma-separated Agent `subagent_type`s that are not redirected. |
| `HERDR_AGENTS_AUTO_ALLOW` | on | `off` stops the pre-bash hook from auto-allowing `herdr-agent` commands. |
| `HERDR_AGENTS_RECORD` | on | `off` stops recording main sessions for restore. |
| `HERDR_AGENTS_RESTORE` | on | `off` disables the restore startup hook. |
| `HERDR_AGENTS_RESTORE_GRACE_S` | `600` | How long restore waits for native relaunches. |
| `HERDR_AGENTS_RESTORE_MAX_AGE_H` | `168` | Launch records older than this are pruned. |
| `HERDR_AGENT_MAX_DEPTH` | `2` | Maximum nesting depth of delegated agents. |
| `HERDR_AGENT_ROOT` | `$TMPDIR/herdr-agents` | Where agent directories live. |
| `HERDR_AGENT_AUTO_TRUST` | on | `off` stops auto-trusting a nested repo or worktree inside a trusted folder (and inheriting its MCP approvals). |
| `HERDR_AGENT_SUBMIT_DELAYS` | `2,3,5,8,12` | Seconds between checks that a typed prompt was submitted. |
| `HERDR_AGENT_BIN` | the running CLI | Overrides the CLI path advertised to hooks and agents. |
| `HERDR_AGENT_BIN_DIR` | `~/.local/bin` | Directory of the `setup --only bin` link. |
| `HERDR_AGENTS_STATE_DIR` / `HERDR_AGENTS_DATA_DIR` | `~/.local/state/herdr-agents` / `~/.local/share/herdr-agents` | Launch records, `restore.log`, `cli.json` / the Herdr plugin copy. `XDG_STATE_HOME` and `XDG_DATA_HOME` apply. |
| `HERDR_AGENTS_PYTHON` | auto | Interpreter the shim re-executes with. |
| `HERDR_BIN_PATH`, `HERDR_CONFIG_PATH` | `herdr` on `PATH` | Herdr binary and `config.toml` location. |

`on` variables are switched off by `off`, `0`, `false` or `no`.

## Permissions

- **Auto-allow.** The PreToolUse `Bash` hook allows `herdr-agent` commands without a prompt when the first word resolves
  (realpath) to this CLI (absolute path, `~/…`, `$HERDR_AGENT`, or a name found on `PATH`) and the command has no shell
  metacharacters beyond plain `$VAR`. Allowed subcommands: `run`, `spawn`, `wait`, `send`, `status`, `result`, `list`, `screen`,
  `close`, `gc`, `path`, `version`. In plan mode only read-only roles (`explore`, `plan`, `--read-only`) and
  read-only subcommands are allowed; the command is parsed with the CLI's own parser (no option abbreviations), and a
  `run`/`spawn` with extra claude args after `--` or a `--permission-mode` other than `auto`, `default` or `plan` is not
  auto-allowed. Any doubt falls through to the normal permission flow.
- **`keys` is never auto-allowed**, nor `setup` and `hook`: answering a dialog is the user's decision.
- **Fallback rules.** If a Claude Code version ignores a hook `allow` (notably in plan mode), `herdr-agent setup --only perms`
  adds classic `Bash(<cli> run * )`-style allow rules.
- Agents run in auto permission mode by default; narrow tools with `-- --allowedTools Read Grep Glob --strict-mcp-config`.

## Doctor

```
herdr-agent doctor [--json]
```

Checks Python, Herdr version and its Claude integration, whether the shell is inside Herdr, hooks (warns about duplicates when
both the plugin and `setup` installed them), permission rules, the bin link, the Herdr plugin link, the CLI pointer,
`resume_agents_on_restore`, `claude`, and the number of launch records with the last restore log line. Exit 1 when a check fails.

## Uninstall

```
herdr-agent setup --uninstall [--only hook,perms,bin,herdr]
/plugin uninstall herdr-agents@herdr-agents
```

`--uninstall` removes only what setup wrote: its hooks and allow rules, the bin link, and the Herdr plugin link and copy. It
leaves Herdr's `config.toml` and the launch records (`<state>/launches/`) in place.

## Limitations

- Restoring a pane replaces it with a new tab; tab order and splits around it are lost.
- Restore depends on Herdr exposing panes and session ids after a restart; the pane id + cwd fallback covers the gaps. Main
  sessions are re-created with the behaviour env allowlist only; other wrapper-set env vars are not reproduced.
- Hook `allow` decisions and deny-reason compliance in plan mode are Claude Code behavior; use `setup --only perms` as a
  fallback.
- A plugin update changes the cache path of the CLI. A running session keeps the old path (it may disappear), and `setup --only
  herdr` must be rerun to repoint the Herdr plugin.
- Plugin and `setup` hooks together run twice (double injection). `doctor` warns; `setup --uninstall --only hook` fixes it.
- The orchestrator injection is on by default; set `CLAUDE_ORCHESTRATOR=off` if you do not want it.
- Other tools that dispatch custom agents through the Agent tool are redirected inside Herdr; they need to support
  `herdr-agent run --agent NAME` or be listed in `HERDR_AGENTS_PASSTHROUGH`.
- The Herdr plugin link is global to your OS user.

## Development

```
python3 -m unittest discover -s tests
```

Tests use a fake `herdr` (`tests/fakes/herdr`) and never touch a real Herdr. The design is in [docs/DESIGN.md](docs/DESIGN.md).

## License

MIT
