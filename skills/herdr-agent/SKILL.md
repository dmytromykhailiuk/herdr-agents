---
name: herdr-agent
description: Run Claude sub-agents as real interactive Claude sessions, each in its own Herdr tab that closes automatically when the agent finishes, with the result returned through a file (never scraped from the terminal). Inside Herdr this replaces the built-in Agent tool, including the Explore/Plan agents in plan mode (read-only roles explore and plan). Use when running inside Herdr, or when the user asks for sub-agents/delegation "in herdr", "in a separate tab" or "visible agents". Requires HERDR_ENV=1.
---

# herdr-agent

CLI (below: `$HA`): use the absolute path given in the session context. It is also exported as `$HERDR_AGENT`
in Bash, and `herdr-agent path` prints it when `herdr-agent` is on PATH. Always call it by that absolute path.

Every agent gets a unique id `<role>-<6 hex>`, used as the Herdr agent name, the tab
label and its directory `$TMPDIR/herdr-agents/<id>/` (`meta.json`, `prompt.md`,
`result.md`, `turns.jsonl`). A Stop hook, installed only for that agent through a
per-agent `--settings` file, writes the final message atomically to `result.md`.
The directory is deleted after a successful run unless `--keep-files` is passed.

Check `test "$HERDR_ENV" = 1` first. If it fails, say so and fall back to the Agent tool.

## Run one agent

```bash
$HA run <role> --model sonnet --prompt "<self-contained task>" [--timeout SEC] [-- <claude args>]
```

Stdout carries only the agent's final message. Status lines go to stderr. On
success the tab is closed and the files are removed.

`--json` output includes `session_id` (the agent's Claude session), and every agent tab gets
`HERDR_PARENT_SESSION_ID` (the spawning session's `CLAUDE_CODE_SESSION_ID`).

- The Bash tool times out after 10 minutes, so run anything that may take longer
  with `run_in_background: true`. You are notified when it exits, and the output
  holds the result.
- Use `--prompt -` for stdin or `--prompt-file PATH` for long prompts.
- Use `--env KEY=VAL` (repeatable, also on `spawn`) to set extra environment variables in the agent's tab.
  `HERDR_*` keys are reserved and rejected.
- Use `--cwd DIR` for another directory. Use `--focus` only when the user wants to
  watch the agent.

## Roles, read-only agents and custom agents

`<role>` is a free label (`general`, `review`, `fix`, ...) except for two built-in read-only roles:

| role | what it is |
|---|---|
| `explore` | read-only research: Edit, Write, NotebookEdit, MultiEdit, Agent, Task, EnterPlanMode and ExitPlanMode are disabled; it reports findings with paths and lines |
| `plan` | read-only like `explore`, and returns a step-by-step plan with critical files, trade-offs and risks |

- `--read-only` applies the same read-only preset to any role.
- `--agent NAME` starts the agent as the Claude Code custom agent `NAME` (`claude --agent NAME`); the role
  defaults to `NAME`.
- Read-only agents still run in auto permission mode; they are not started in plan mode, because a sub-agent in
  plan mode would end its turn on an ExitPlanMode dialog.

## Plan mode

Inside Herdr the built-in Agent tool is replaced by herdr-agent, plan mode included (a hook blocks the Agent tool
and names the command to run instead). The read-only roles are the plan-mode equivalents of the built-in agents
and do not violate plan mode:

- Explore → `$HA run explore --model <m> --prompt-file <file>`
- Plan → `$HA run plan --delegate --model opus --prompt-file <file>`

The main session writes the plan file itself from the result, then calls ExitPlanMode.

## Run agents in parallel

Make one Bash call per agent in the same message, each with
`run_in_background: true`. Each call is an independent `$HA run ...` with its own
unique id. Do not chain them with `&` inside one call; separate calls give
separate notifications and outputs.

## Exit codes and what to do

| code | status | action |
|---|---|---|
| 0 | done | use the stdout result |
| 3 | blocked | the agent is waiting on a dialog (permission, folder trust). Show the user the snapshot from stderr and ask how to answer. Then `$HA keys <id> <keys>` (e.g. `enter`, `down enter`, `esc`, `2 enter`) and `$HA wait <id> --close` |
| 4 | timeout | still running. `$HA wait <id> --close` later |
| 5 | dead | the user closed the tab or Claude exited. Report it; the result is lost |
| 6 | no_result | the agent went idle without a final message. `$HA screen <id>`, then decide |
| 7 | api_error | the turn ended on a Claude Code API error ("API Error: …", e.g. the computer slept or the connection dropped), not a result. If the session was still alive, the CLI already sent one automatic `continue`; exit 7 means that turn also ended on an API error or the continue could not be delivered (see stderr). The tab and files are kept. Resume with `$HA send <id> --prompt continue --wait --close` (or, if the tab is gone, `claude --resume <session id>` in the agent's cwd, prefixed with `CLAUDE_CONFIG_DIR=<dir>` when stderr shows one). Never treat it as the agent's answer |
| 1 | error/busy | read stderr. `busy` means the agent is mid-turn: wait before sending |
| 2 | usage | fix the command line |
| 130 | interrupted | the agent keeps running in its tab; resume with `$HA wait <id>` |

Never approve permission dialogs or folder-trust dialogs on your own judgment.
They are the user's decision. If the user wants fewer prompts, pass the
permissions up front with `-- --allowedTools ...` or
`-- --permission-mode acceptEdits`.

An untrusted cwd (never opened in Claude, with no trusted parent) is refused before
any tab opens: exit 3 with "cwd is not trusted". Once the trust dialog is accepted
inside an agent's tab, the session would go on without its flags (new session id, no
`--settings`, so no result capture). Ask the user to run `claude` in that folder once and
accept the dialog, then retry. Or use a trusted dir (the current project dir) and pass
absolute paths in the prompt.

## Multi-turn agents

```bash
$HA spawn <role> --prompt "..."          # prints the id, returns immediately
$HA wait <id>                            # result of the current turn (tab stays)
$HA send <id> --prompt "follow-up" --wait [--close]
$HA close <id>                           # closes the tab and removes the files
```

`send` refuses while the agent is working. That guard stops a queued prompt from
picking up the previous turn's answer.

## Other commands

`$HA list` (all agents plus live state), `$HA status <id>`, `$HA result <id>`,
`$HA screen <id>`, `$HA gc [--older-than H] [--dry-run]` (removes directories of
agents whose tabs are gone; never closes live tabs unless `--close-idle` is passed),
`$HA path` (the CLI's absolute path), `$HA version [--json]`.

## Writing the prompt

The agent sees nothing from this conversation. Give it the goal, absolute paths,
constraints, and the shape of the answer you want back. Its system prompt already
tells it that its final message is the deliverable. For large outputs, ask for a
concise summary plus file paths, not a dump of everything.

## Model and effort

| task | model | effort |
|---|---|---|
| run a script or command, simple lookup | haiku | medium |
| well-defined work with little unpredictability (spec'd edit, known fix, tests to a spec) | sonnet | medium |
| needs thinking or investigation (debugging, exploring code, planning, review, design within known bounds) | opus | medium |
| very ambiguous or complex, needs thinking outside the frame | opus | high |

- Without `--model` the agent uses the user's default model. `--effort` defaults to `medium`.
- Escalation: when a debug or investigation task comes back weak at `medium` (no root cause, a shallow or
  contradictory answer, obvious leads left unchecked), run it again at `high` with a new prompt that includes
  what the `medium` run found and where it fell short. Do not use `high` for other kinds of tasks.

## Permissions and depth

- Every agent tab opens in **auto mode** (`--permission-mode auto` is added by the CLI). Override with
  `--permission-mode <mode>`, with `--permission-mode ''` for Claude's own default, or by passing
  `--permission-mode` / `--dangerously-skip-permissions` after `--`, which takes precedence.
  Dialogs that still appear remain the user's decision (see above).
- Narrow tools and MCP for focused agents: `-- --allowedTools Read Grep Glob --strict-mcp-config`.
  `--disallowedTools` after `--` is merged with the role's disabled tools.
- Depth limit: `HERDR_AGENT_MAX_DEPTH`, default 2. A sub-agent may spawn its own sub-agents only when its parent
  started it with `--delegate`. Use that only for broad read-only work (planning, research, investigation). Its
  sub-agents run one level deeper, cannot delegate further, and must not edit project files. Without
  `--delegate`, a sub-agent's spawn attempt is refused.
- The herdr-agents plugin auto-allows `$HA` commands (except `keys`) without a permission prompt; in plan mode
  only the read-only roles and read-only subcommands are auto-allowed, with full option names and no extra claude args
  after `--`.

## Restore after a Herdr restart

Agents and main Claude sessions started with extra flags or a `CLAUDE_CONFIG_DIR` are recorded at launch. After a
Herdr server restart, the bundled Herdr plugin relaunches them with their original flags and env.
`$HA herdr status` lists the records and how each one currently matches a pane.

## Setup and diagnostics

As a Claude Code plugin, the hooks come with the plugin. For a plain skill install, `$HA setup` installs the
hooks, permission rules and the Herdr restore plugin (`--only hook,perms,bin,herdr`, `--dry-run`,
`--uninstall`). `$HA doctor` checks Python, Herdr, hooks, permissions and the restore plugin.

## Edge cases already handled by the CLI

- Id collisions between concurrent spawns (atomic `mkdir` plus a live-name check).
- Stale `blocked` state just after a dialog is answered (2 s debounce, and `keys` waits for the state to clear).
- A turn that ends with background tasks still running (keeps waiting for the final turn).
- A Stop hook that never fires (falls back to the session transcript, under `CLAUDE_CONFIG_DIR` when set).
- A turn that ends on a Claude Code API error: not reported as done; one automatic `continue`, otherwise exit 7.
- A pane moved to another tab (follows it by agent name). A tab the user split (closes only the agent's pane).
- Closing twice (idempotent). An untrusted cwd (refused up front).
- A shell still sourcing its profile when several tabs start at once: the CLI waits for an idle prompt before
  starting Claude, and a watchdog presses Enter for its own command if it still sits at the prompt.
- A prompt typed into Claude's input box but never submitted: the CLI presses Enter, up to 3 times, never on a
  dialog.
