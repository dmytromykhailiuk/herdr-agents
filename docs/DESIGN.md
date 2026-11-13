# herdr-agents — implementation plan

New repo: `/Users/dmitromykhailiuk/Desktop/npm-packages/herdr-agents` (own git repo, MIT, Python 3.10+ stdlib only,
Claude Code plugin + `setup|doctor` fallback, a bundled Herdr plugin). Layout and conventions mirror `agent-vault`
(plugin.json/marketplace.json/hooks.json with `${CLAUDE_PLUGIN_ROOT}`, skill-embedded CLI with a `bin/` shim and
`lib/` package, `setup.py` regex-identity hook merge, CI unittest on 3.10–3.13). `util`-style helpers (`atomic_write`,
`read_json`/`write_json`, `.bak` + atomic settings write, dry-run) are copied from `claude-profiles`.
Nothing in the user's `~/.claude` or `~/.config/herdr` is touched by this task.

---

## 1. Key decisions (with justification)

| # | Decision | Why |
|---|---|---|
| D1 | **Model-facing CLI path = the absolute path printed by the SessionStart hook** (the realpath of the CLI that runs the hook, i.e. `${CLAUDE_PLUGIN_ROOT}/skills/herdr-agent/bin/herdr-agent` or the skills.sh copy). Override with `HERDR_AGENT_BIN`. The hook also appends `export HERDR_AGENT=<path>` to `$CLAUDE_ENV_FILE` (when Claude provides it) so other tools can use `$HERDR_AGENT`. `herdr-agent path` prints it. Optional `setup --only bin` symlinks `~/.local/bin/herdr-agent` for humans/other tools. | The plugin cache path is versioned and changes on every `/plugin update`; regenerating it per session is always correct. No PATH dependency. Permission auto-allow (D3) matches by realpath, so version changes never break it. Other tools (verify-loop style) keep their own override (`VERIFY_HERDR_AGENT`) and can default to `$HERDR_AGENT` / `command -v herdr-agent`. |
| D2 | **Plan mode fix = 3 layers**: (a) built-in read-only roles `explore` and `plan`; (b) PreToolUse hook on `Agent|Task` that denies the call inside Herdr at depth 0 and returns the exact `herdr-agent run` command (prompt pre-written to a file); (c) injected "Herdr sub-agents" context section that maps Explore→`run explore`, Plan→`run plan --delegate` and states they are the plan-mode-safe equivalents. | Root cause: the built-in plan-mode reminder ("only use the Explore subagent type", "supersedes any other instructions") beats CLAUDE.md prose, and `herdr-agent run` looked non-read-only (auto mode, no read-only preset). A hook denial is a hard signal the model must react to; read-only roles remove the reason to avoid herdr-agent. |
| D3 | **No permission prompts for our CLI** via a PreToolUse `Bash` hook returning `permissionDecision: "allow"` for strictly parsed `herdr-agent` invocations (setup `perms` part still writes classic allow rules for non-plugin installs). In plan mode it allows only `run/spawn` with read-only roles plus read-only subcommands. | Plugins cannot ship `permissions.allow`; the hook works for plugin installs and in plan mode, and is narrower than a wildcard rule. Opt-out `HERDR_AGENTS_AUTO_ALLOW=off`. |
| D4 | **Resume = Herdr `[[startup]]` plugin relaunch (option A)**, not `pane report-agent -- <cmd>` (option B). | B conflicts with the official `herdr:claude` source, which re-reports on every Claude hook event (pane holder race, `resume_not_accepted`); B needs a plain command on PATH (forces the bin symlink), bans apostrophes (user `--append-system-prompt` text / args can contain them), ≤64 args/8 KiB, and cannot carry tab env without a wrapper; it would also need hooks inside every recorded session. A uses only documented primitives (startup hook, state dir, `pane list`, `pane process-info`, `tab create --env`, `agent start`), leaves Herdr's integration alone, and restores env. B stays a documented follow-up if the S0 spike shows a second source can hold the pane. |
| D5 | **State lives in the package state dir** `${HERDR_AGENTS_STATE_DIR:-${XDG_STATE_HOME:-~/.local/state}/herdr-agents}`, not `HERDR_PLUGIN_STATE_DIR`. | Records are written at spawn time from a Claude Bash call, where the Herdr plugin env is absent. The startup hook logs to `HERDR_PLUGIN_STATE_DIR` too (if set). |
| D6 | **Herdr plugin is installed from a stable copy**: `setup --only herdr` copies `herdr-plugin/` to `${XDG_DATA_HOME:-~/.local/share}/herdr-agents/herdr-plugin/`, rewrites `command[0]` to the absolute `sys.executable`, writes `<state>/cli.json` (CLI path + version), then `herdr plugin link <copy>`. `startup.py` reads `cli.json` (fallback `herdr-agent` on PATH) and execs `<python> <cli> herdr startup`. Every spawn refreshes `cli.json`. | Linking the versioned plugin cache dir would break after `/plugin update`. Records only exist after a spawn, so refreshing the pointer at spawn time keeps it current without side effects in hooks. Absolute python avoids launchd/PATH and the macOS `/usr/bin/python3` CLT stub. |
| D7 | **`[session] resume_agents_on_restore`**: add `= true` explicitly when the key is absent (setup and startup hook); if the user set it to `false`, keep it and warn (doctor + restore log). | Docs default is already on; writing it makes it visible as the user wanted, while never overriding an explicit opt-out. Python 3.10 has no `tomllib`, so editing is line-based. |
| D8 | **Generic launcher**: `<profile>-claude` wrapper logic removed. Profiles are carried via `CLAUDE_CONFIG_DIR` in the tab env; hints print `CLAUDE_CONFIG_DIR=<dir> claude --resume <sid>` when set. | `herdr agent start --kind claude` always runs the canonical `claude`; env is the only portable carrier. |
| D9 | **Orchestrator mode on by default**, opt-out `CLAUDE_ORCHESTRATOR=off` (also `0/false/no`). Herdr rules are injected even when orchestrator is off. | User decision. |

---

## 2. Repo tree

```
herdr-agents/
  .claude-plugin/plugin.json          name herdr-agents, version 0.1.0, description, author{name: dmytromykhailiuk}, license MIT, keywords
  .claude-plugin/marketplace.json     marketplace "herdr-agents", plugins[{name, source "./", description, version, category "productivity", keywords}]
  hooks/hooks.json                    plugin hooks (exact JSON in §4)
  skills/herdr-agent/SKILL.md         model-facing usage doc (ported, generic, no hardcoded path; roles explore/plan; --agent; --read-only)
  skills/herdr-agent/bin/herdr-agent  python shim: version check ≥3.10, sys.path += ../lib, core.CLI_PATH = abspath(__file__), cli.main()
  skills/herdr-agent/lib/herdragent/
    __init__.py        __version__ = "0.1.0"; INTEGRATION = 1
    core.py            env/config resolution, paths (ROOT, STATE_DIR, DATA_DIR, CLI_PATH, claude_config_dir()), CliError/die,
                       exit codes, atomic_write, read_json/write_json, log(), truthy/off() env parsing
    herdr.py           herdr CLI wrapper: HerdrError(code,msg), herdr(*args) (uses $HERDR_BIN_PATH or `herdr`), envelope parse,
                       agent_info, pane_exists, tab_info, pane_list, agent_list, process_info, shell_state, wait_shell_ready,
                       start_watchdog, screen
    trust.py           git_toplevel, git_main_worktree, claude_config_path (respects CLAUDE_CONFIG_DIR), is_trusted
    roles.py           role presets (explore, plan) + --read-only + --agent mapping → claude args and system-prompt addenda;
                       SYSTEM_PROMPT, READONLY_PROMPT, DELEGATE_PROMPT (CLI path substituted, no orchestrator-skill reference
                       hard-dependency)
    spawn.py           claim_id, sanitize_role, read_prompt, do_spawn (tab create, per-agent settings, agent start, prompt),
                       send_prompt, input_box, ensure_submitted; writes launch record via launches.record_spawn
    results.py         agent_dir/load_meta/save_meta/iter_metas, read_result, transcript_path_for(sid, config_dir),
                       transcript_last, is_api_error_entry, api_error_check, api_error_hint, emit
    waiting.py         do_wait, wait_turn (API-error auto-continue), close_agent (also launches.forget), finish
    commands.py        cmd_run/spawn/wait/send/keys/result/screen/status/list/close/gc/path/version
    cli.py             argparse: run/spawn/wait/send/keys/result/screen/status/list/close/gc/path/version,
                       setup/doctor, hook {session-start,pre-agent,pre-bash,stop}, herdr {startup,status};
                       hook/herdr/setup modules imported lazily; signal handling; bare `--` passthrough
    launches.py        launch-record store: record_spawn, record_session, forget, load_all, prune, update; claude argv
                       sanitizer (replayable-flag allowlist with arity); refresh cli.json pointer
    context.py         builds injected text: orchestrator body (from skills/orchestrator/SKILL.md, frontmatter stripped,
                       `{{HERDR_AGENT}}` substituted) + Herdr rules section + plan-mode mapping
    hooks/__init__.py
    hooks/common.py    read stdin payload safely, is_main_session(payload, env), in_herdr(env), emit_json, never-raise wrapper
    hooks/session_start.py  injection (startup|clear|compact) + CLAUDE_ENV_FILE export + main-session recording (all sources)
    hooks/pre_tool.py  pre-agent (Agent/Task redirect) and pre-bash (auto-allow) decisions
    hooks/stop.py      per-agent Stop hook (port of stop_hook.py) → result.md/result.json/turns.jsonl
    herdrconfig.py     config.toml path (HERDR_CONFIG_PATH | ~/.config/herdr/config.toml), line-based ensure_resume_on(),
                       reload_config()
    restore.py         startup hook: lock, ensure config, load/prune records, match panes, classify, relaunch, log; `herdr status`
    setup.py           setup/doctor: parts hook,perms,bin,herdr; plugin-bundle detection; settings merge; bin symlink;
                       herdr plugin copy+link; installed.json
  skills/orchestrator/SKILL.md        generic orchestrator rules (no vault/vloop/task-*; `{{HERDR_AGENT}}` placeholder)
  herdr-plugin/herdr-plugin.toml      Herdr plugin manifest (§6)
  herdr-plugin/startup.py             tiny launcher: resolve CLI via <state>/cli.json → exec `<py> <cli> herdr startup`
  tests/_base.py                      temp HOME/XDG/TMPDIR/CLAUDE_CONFIG_DIR isolation, PATH with tests/fakes first, env scrub,
                                      helpers run_cli(), run_hook(payload, env), fake-herdr state accessors
  tests/fakes/herdr                   fake herdr CLI (python, state in $FAKE_HERDR_STATE JSON, call log) — contract §8
  tests/fakes/claude                  `claude --version` only (doctor)
  tests/fixtures/config-*.toml        herdr config variants (none, no [session], commented key, explicit false, explicit true)
  tests/fixtures/transcripts/*.jsonl  normal / api-error / synthetic-non-error transcripts
  tests/test_fake_herdr.py            the fake honours its contract
  tests/test_cli.py                   run/spawn/wait/send/close/gc/list/path flows on the fake
  tests/test_roles.py                 role presets, --read-only, --agent, permission-mode precedence, disallowed merge
  tests/test_results.py               stop hook output, transcript fallback with CLAUDE_CONFIG_DIR, API-error detection
  tests/test_launches.py              record/forget/prune/supersede, argv sanitizer table tests, cli.json refresh
  tests/test_hooks_session_start.py   injection matrix, JSON shape, path substitution, CLAUDE_ENV_FILE, recording
  tests/test_hooks_pre_tool.py        Agent deny matrix + reason text + prompt file; Bash allow matrix
  tests/test_setup.py                 idempotency, dry-run, uninstall, foreign hooks kept, path change, bundle skip,
                                      bin clobber refusal, herdr config edits, plugin copy/link calls, doctor output
  tests/test_restore.py               restore algorithm scenarios on the fake (§7)
  tests/test_versions.py              plugin.json == marketplace.json == herdr-plugin.toml == __version__
  .github/workflows/test.yml          ubuntu-latest, python 3.10–3.13, `python -m unittest discover -s tests -v`
  .gitignore                          __pycache__/ *.pyc .DS_Store
  README.md  CHANGELOG.md (## Unreleased / ## 0.1.0)  LICENSE (MIT)
```

Not packaged: `~/.claude/hooks/herdr-agent-state.sh` (Herdr-owned integration), user's `auto_resume.py`/launchd plists,
vault/vloop/taskflow couplings, `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`.

---

## 3. Module responsibilities

### CLI (port of `~/.claude/skills/herdr-agent/bin/herdr-agent`, 1096 lines → modules above)
Behaviour, exit codes (0,1,2,3,4,5,6,7,130), id scheme `<role>-<6hex>`, runtime dir
`${HERDR_AGENT_ROOT:-$TMPDIR/herdr-agents}/<id>/` (meta.json, prompt.md, settings.json, result.md/json, turns.jsonl),
depth gates (`HERDR_AGENT_MAX_DEPTH` default 2, `HERDR_AGENT_MAY_DELEGATE`), trust refusal, shell-ready wait + Enter
watchdog, API-error auto-continue, close-only-what-we-created: **unchanged**. Changes:
- Stop hook: per-agent `settings.json` command becomes `'<sys.executable>' '<CLI_PATH>' hook stop` (no separate file).
- Transcript fallback: `transcript_path` from result.json first, else glob `<CLAUDE_CONFIG_DIR or ~/.claude>/projects/*/<sid>.jsonl`
  using `meta.claude_config_dir` (bug fix).
- `claude_command()` removed; hints use `CLAUDE_CONFIG_DIR=<dir> claude --resume <sid>` when a config dir is set.
- `DELEGATE_PROMPT` embeds `CLI_PATH`; references "the model/effort guidance in the herdr-agent skill" (matrix copied into
  SKILL.md) instead of requiring the orchestrator skill.
- New options on run/spawn: `--agent NAME` (passes `--agent NAME` to claude; role defaults to NAME sanitized),
  `--read-only` (applies the read-only preset to any role). Roles `explore`/`plan` imply `--read-only`.
- `--disallowedTools` is passed as one comma-joined argument and merged with any user `--disallowedTools` after `--`.
- After a successful `agent start`: `launches.record_spawn(meta, claude_args, tab_env)`; on close/gc: `launches.forget(sid)`.
- New subcommands: `path` (prints CLI_PATH), `version [--json]` (version + integration number).

### roles.py — presets
| role / flag | permission mode | disallowed tools | system prompt addendum |
|---|---|---|---|
| default (any role) | `auto` (unchanged) | – | SYSTEM_PROMPT |
| `explore`, or `--read-only` | `auto` | `Edit,Write,NotebookEdit,MultiEdit,Agent,Task,EnterPlanMode,ExitPlanMode` | + READONLY_PROMPT ("read-only research: never modify files or state; no installs, commits, writes; report findings with paths/lines") |
| `plan` | `auto` | same as explore | + READONLY_PROMPT + PLAN_PROMPT ("return a step-by-step plan, critical files, trade-offs, risks") |
`--permission-mode plan` is deliberately NOT used: a sub-agent in plan mode gets the plan-mode reminder and ends turns with
`ExitPlanMode`, which opens a dialog (exit 3). `--delegate` with read-only roles keeps `Agent` disallowed (it delegates via
herdr-agent Bash). User args after `--` still win for `--permission-mode` (existing precedence rule).

### launches.py — launch records (session recorder store)
`<state>/launches/<session_id>.json`, schema v1:
`{v, kind: "spawn"|"session", session_id, name (agent id or null), label, cwd, workspace_id, pane_id (from HERDR_PANE_ID / tab
create result, never constructed), tab_id, claude_config_dir, env {KEY: VAL} (HERDR_AGENT_* + user --env + CLAUDE_CONFIG_DIR),
args [sanitized claude args without --session-id/--resume/--settings], settings (dict content of per-agent settings for kind spawn),
agent_dir, marker (argv token that proves a correct launch: `--name <id>` for spawns, first recorded flag for sessions),
created_at, updated_at, restored_count, unmatched_startups}`.
- `record_spawn` / `record_session` (supersede: delete other records with the same `pane_id`), `forget(sid)`, `prune(max_age_h)`
  (default 168 h; also drops records whose transcript no longer exists; drops after 2 unmatched startups).
- Argv sanitizer (for main sessions): keep only `--model 1, --effort 1, --agent 1, --permission-mode 1, --settings 1,
  --append-system-prompt 1, --system-prompt 1, --add-dir 1+, --allowedTools/--allowed-tools 1+, --disallowedTools/--disallowed-tools 1+,
  --mcp-config 1+, --strict-mcp-config 0, --dangerously-skip-permissions 0, --name 1, --plugin-dir 1, --setting-sources 1`
  (forms `--x=v` and `--x v`); drop `-p/--print` sessions entirely, drop `--resume/-r, --continue/-c, --session-id, --fork-session`,
  positional prompts and unknown flags.
- `refresh_pointer()` writes `<state>/cli.json {cli, python, version}` if changed.

### hooks/session_start.py
Input: SessionStart payload (`session_id, source, cwd, transcript_path, agent_id?, agent_type?`).
1. Never raises; exit 0 always; `HERDR_AGENTS=off` → no-op.
2. **Export**: if `CLAUDE_ENV_FILE` set and HERDR_ENV=1 → append `export HERDR_AGENT='<CLI_PATH>'` (once per file).
3. **Inject** (only `source ∈ {startup, clear, compact}`, main session = depth 0/empty, entrypoint not `sdk*`, payload without
   `agent_id`/`agent_type`): text = [orchestrator body if not `CLAUDE_ORCHESTRATOR` off] + [Herdr section if HERDR_ENV=1].
   Output `{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"<text>"}}`; print nothing when text empty.
4. **Record** (any source, HERDR_ENV=1, HERDR_PANE_ID set, `HERDR_AGENT_ID` unset, not sdk, `HERDR_AGENTS_RECORD` not off):
   get the claude argv from `herdr pane process-info --pane $HERDR_PANE_ID` (foreground process whose argv0/name is `claude`),
   fallback `ps -o args= -p` walking ppid ≤4 levels; sanitize; needs-record = sanitized args non-empty or
   `CLAUDE_CONFIG_DIR` set and ≠ `~/.claude`; record (or drop the pane's stale record when not needed). 2 s budget for herdr calls.

Herdr section text (generic port of CLAUDE.md "Sub-agents" lines 3-14):
- Inside Herdr, run every sub-agent with `<CLI> run <role> ...`, never the built-in Agent tool; each agent gets its own tab that
  closes when it finishes. The Agent tool is blocked by a hook here.
- Plan mode included: Explore → `<CLI> run explore --model <m> --prompt-file <f>`, Plan → `<CLI> run plan --delegate --model opus
  --prompt-file <f>`. Both roles are read-only (edit tools disabled) and are the plan-mode equivalents of the built-in agents;
  running them does not violate plan mode.
- Parallel agents: one Bash call per agent with `run_in_background: true`.
- Never answer a sub-agent's permission or folder-trust dialog yourself; show it and ask (exit 3).
- Before the first agent in a folder Claude Code has never opened, ask the user to run `claude` there once and accept trust.

### hooks/pre_tool.py
**pre-agent** (matcher `^(Agent|Task)$`). Deny iff: HERDR_ENV=1, `HERDR_WORKSPACE_ID` set, `HERDR_AGENTS`/`HERDR_AGENTS_REDIRECT`
not off, entrypoint not sdk, and (depth 0 or (depth ≥1 and `HERDR_AGENT_MAY_DELEGATE=1`)), and `subagent_type` not in
`HERDR_AGENTS_PASSTHROUGH` (default `claude-code-guide,statusline-setup`). Otherwise exit 0 silently (fail-open on any error).
On deny: write `tool_input.prompt` to `<state>/prompts/<tool_use_id or uuid>.md`; map `subagent_type`:
`Explore`→`explore`, `Plan`→`plan --delegate` (only if depth+1 < max), `general-purpose`/missing→`general`, other→`<sanitized> --agent <type>`;
map `model` (if given) and `effort`; output:
```json
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny",
 "permissionDecisionReason":"Inside Herdr, sub-agents run through herdr-agent, not the Agent tool (this includes plan mode). Run this Bash command instead (run_in_background: true; one Bash call per agent for parallel work): '<CLI>' run explore --model haiku --prompt-file '<file>'. The explore/plan roles are read-only (Edit/Write/NotebookEdit disabled) and are the plan-mode equivalents of the built-in Explore/Plan agents. Its stdout is the agent's final report."}}
```
**pre-bash** (matcher `^Bash$`). Fast path: return unless `"herdr-agent"` occurs in the raw stdin. Allow iff: `HERDR_AGENTS_AUTO_ALLOW`
not off; `shlex.split` succeeds; no shell metacharacters outside quotes (`; & | \` $( > < \n`) — exception: none; argv[0]
realpath == CLI realpath (or the `~/.local/bin` symlink resolving to it, or `$HERDR_AGENT` literal expanded by the hook);
subcommand ∈ {run, spawn, wait, send, status, result, list, screen, close, gc, path, version}; in plan mode
(`permission_mode == "plan"`) only {run, spawn} with role explore|plan or `--read-only`, plus wait/status/result/list/screen.
`keys` is never auto-allowed (it answers dialogs; user decision). Output
`{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"herdr-agent (herdr-agents plugin)"}}`.
Budget: < 100 ms (lazy imports; measured in a test).

### hooks/stop.py
Port of `stop_hook.py` unchanged in behaviour (atomic result.md then result.json, turns.jsonl, never fails the turn,
`hook-errors.log`). Uses `dict(a, **b)` style compatible with 3.10.

### setup.py — `herdr-agent setup|doctor`
`setup [--scope user|project] [--only hook,perms,bin,herdr] [--uninstall] [--dry-run] [--force]`, default parts
`hook,perms,herdr` (`bin` opt-in), user scope = `${CLAUDE_CONFIG_DIR:-~/.claude}/settings.json`, project scope =
`<git toplevel>/.claude/settings.json` (hook/perms only).
- Plugin bundle detection (`.claude-plugin/plugin.json` three levels above `bin/`): skip `hook` (plugin supplies it) and print why.
- **hook**: identity regex `HOOK_CMD_RE = r"/bin/herdr-agent'?\"?\s+hook\s+(session-start|pre-agent|pre-bash)\b"`;
  `_strip_hooks` removes ours from SessionStart and PreToolUse (dropping empty groups/keys), `_add_hooks` = strip then append
  the three groups of §4 with `'<CLI_PATH>'` quoted. Idempotent; path-change safe.
- **perms**: dedupe-append `Bash(<cli> run *)`, `Bash(<cli> spawn *)`, `wait/send/status/result/list/screen *` for both the
  `~/...` form and the absolute form (agent-vault `_perm_path`), and for the bin symlink path when present.
- **bin**: symlink `${HERDR_AGENT_BIN_DIR:-~/.local/bin}/herdr-agent` → CLI_PATH; refuse to clobber a non-symlink or a foreign
  symlink without `--force`; PATH hint.
- **herdr**: copy `herdr-plugin/` atomically (`dir.new.<pid>` → rename) to `<data>/herdr-plugin`, rewrite `command[0]` to
  `sys.executable`; `refresh_pointer()`; `herdrconfig.ensure_resume_on()`; `herdr plugin list` → `herdr plugin link <copy>` if not
  linked (or linked elsewhere: unlink + link); `herdr server reload-config` if config changed (ignore "no server").
  Requires `herdr --version` ≥ 0.9.2 else error with hint.
- Settings writes: no-op if unchanged; `--dry-run` prints the resulting JSON/diff and planned actions; one-time `.bak` next to the
  realpath; atomic write preserving mode (claude-profiles `_write_settings`).
- `installed.json` in state dir: parts, settings path, cli, bin link, herdr plugin dir, version.
- `--uninstall`: strip hooks/perms, remove our symlink only, `herdr plugin unlink <id>`, remove the copied plugin dir; leave
  `config.toml` (key equals Herdr's default) and launch records (print their path).
- **doctor** (`--json`): python ≥3.10; `herdr` found and ≥0.9.2; `herdr integration status` reports claude integration current
  (needed for native session refs); HERDR_ENV/HERDR_WORKSPACE_ID in this shell; hook count per settings file (0, 1, duplicates when
  plugin + setup both installed → warn); perms present; bin link valid; Herdr plugin linked, enabled, dir exists, `cli.json` points
  to an existing CLI; `resume_agents_on_restore` state (absent/true/false→warn); `claude --version` present; record count and last
  restore log line. Non-zero exit on any failure.

### herdrconfig.py
`ensure_resume_on(path) -> changed: bool`: absent file → create with `[session]\nresume_agents_on_restore = true\n`; no `[session]`
table → append it; table present without the key (commented default lines ignored) → insert key after the header; key true →
no-op; key false → no change, return warning. One-time `.bak`, atomic write. `reload_config()` = `herdr server reload-config`,
errors ignored when no server runs.

---

## 4. Exact hook JSON

`hooks/hooks.json` (plugin):
```json
{
  "description": "Run Claude sub-agents in Herdr tabs: inject orchestrator and Herdr rules at session start, record launches for restore, redirect the Agent tool to herdr-agent inside Herdr, and allow herdr-agent commands without prompts.",
  "hooks": {
    "SessionStart": [
      {
        "matcher": "^(startup|resume|clear|compact)$",
        "hooks": [
          {"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/skills/herdr-agent/bin/herdr-agent\" hook session-start", "timeout": 10}
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "^(Agent|Task)$",
        "hooks": [
          {"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/skills/herdr-agent/bin/herdr-agent\" hook pre-agent", "timeout": 5}
        ]
      },
      {
        "matcher": "^Bash$",
        "hooks": [
          {"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/skills/herdr-agent/bin/herdr-agent\" hook pre-bash", "timeout": 5}
        ]
      }
    ]
  }
}
```
`setup` writes the same three groups into settings.json with `'<abs CLI_PATH>' hook <name>` commands.

Per-agent `settings.json` (written by spawn, unchanged shape):
```json
{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "'<python>' '<CLI_PATH>' hook stop", "timeout": 30}]}]}}
```

---

## 5. Configuration / environment variables

| var | default | meaning |
|---|---|---|
| `HERDR_ENV`, `HERDR_WORKSPACE_ID`, `HERDR_TAB_ID`, `HERDR_PANE_ID`, `HERDR_BIN_PATH`, `HERDR_SOCKET_PATH` | Herdr | detection / API |
| `HERDR_AGENT_DEPTH`, `HERDR_AGENT_ID`, `HERDR_AGENT_DIR`, `HERDR_PARENT_SESSION_ID`, `HERDR_AGENT_MAY_DELEGATE` | set in agent tabs | unchanged semantics |
| `HERDR_AGENT_MAX_DEPTH` | 2 | depth limit |
| `HERDR_AGENT_ROOT` | `$TMPDIR/herdr-agents` | per-agent runtime dirs |
| `HERDR_AGENT_BIN` | realpath of running CLI | path printed into context / Stop hook |
| `HERDR_AGENT` | (exported via CLAUDE_ENV_FILE) | for other tools; read-only output |
| `CLAUDE_ORCHESTRATOR` | on | `off/0/false/no` disables orchestrator injection |
| `HERDR_AGENTS` | on | master switch for all hooks (`off` = every hook no-op) |
| `HERDR_AGENTS_REDIRECT` | on | Agent-tool redirect |
| `HERDR_AGENTS_PASSTHROUGH` | `claude-code-guide,statusline-setup` | subagent types allowed through the Agent tool in Herdr |
| `HERDR_AGENTS_AUTO_ALLOW` | on | pre-bash auto-allow |
| `HERDR_AGENTS_RECORD` | on | main-session recording |
| `HERDR_AGENTS_RESTORE` | on | startup restore |
| `HERDR_AGENTS_RESTORE_GRACE_S` | 600 | how long the startup hook polls for restored panes |
| `HERDR_AGENTS_RESTORE_MAX_AGE_H` | 168 | record expiry |
| `HERDR_AGENTS_STATE_DIR` | `${XDG_STATE_HOME:-~/.local/state}/herdr-agents` | records, prompts, cli.json, installed.json, restore.log |
| `HERDR_AGENTS_DATA_DIR` | `${XDG_DATA_HOME:-~/.local/share}/herdr-agents` | stable Herdr plugin copy |
| `HERDR_AGENT_BIN_DIR` | `~/.local/bin` | setup `bin` part |
| `HERDR_CONFIG_PATH` | `~/.config/herdr/config.toml` | Herdr's own override, honoured |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | profile; carried to agent tabs and records |

Env vars from Claude's `settings.json` `env` block reach hooks and the CLI; the Herdr startup hook sees the Herdr server env
(document: set restore vars in the shell that starts Herdr).

---

## 6. Herdr plugin

`herdr-plugin/herdr-plugin.toml`:
```toml
id = "dmytromykhailiuk.herdr-agents"
name = "herdr-agents"
version = "0.1.0"
min_herdr_version = "0.9.2"
description = "Relaunch herdr-agent and profile Claude sessions with their original flags and env after a server restart"
platforms = ["linux", "macos"]

[[startup]]
command = ["python3", "startup.py"]
```
(setup's copy has `command[0]` = absolute interpreter.) `startup.py` (≤40 lines, stdlib): resolve state dir → `cli.json` →
`os.execv(python, [python, cli, "herdr", "startup"])`; fallback `shutil.which("herdr-agent")`; if nothing found, log
"herdr-agents CLI not found; run herdr-agent setup --only herdr" to stderr (plugin log) and exit 0.

---

## 7. Resume algorithm (`herdr-agent herdr startup [--dry-run] [--grace S]`)

1. `HERDR_AGENTS_RESTORE=off` → exit 0. Take `fcntl.flock` on `<state>/restore.lock` non-blocking (handoff re-runs) → exit if held.
2. `ensure_resume_on()`; if changed → `herdr server reload-config`.
3. Load records; `prune()` (expired, transcript missing under the record's config dir). None left → exit.
4. `deadline = now + grace`. Loop every 1 s while pending and before deadline:
   - snapshot: `pane list` (all workspaces) + `agent list`; index by `agent_session.value` (sid), by `agent_name`, and by `pane_id`.
   - for each pending record, match: sid → else agent name (spawn records) → else recorded `pane_id` with the same cwd
     (pane ids are Herdr-issued public ids persisted in the layout; never constructed). No match → stay pending.
   - classify the matched pane with `pane process-info`:
     - **healthy**: a foreground `claude` argv contains `rec.marker` (e.g. `--name explore-0615d7`) → done (covers live handoff,
       where processes survive). Update agent meta.json pane/tab ids if they changed.
     - **native**: a `claude` argv with `--resume <sid>` but no marker (Herdr's plain relaunch) → relaunch.
     - **shell**: no foreground agent, pane at an idle shell prompt → relaunch.
     - **working/blocked** (agent status) or anything else → leave pending; at the deadline log "left alone".
   - relaunch(rec, pane), records spaced ≥0.5 s:
     a. `tab create --workspace <pane.workspace_id> --cwd <rec.cwd> --label <rec.label> --env K=V ... --no-focus` (env = rec.env).
     b. close the old pane (whole tab if it has one pane, else just the pane) → kills a wrong native process and prevents a later
        native resume of the stale ref; never two processes on one session.
     c. kind spawn: recreate `rec.agent_dir` if missing (TMPDIR cleared by reboot), rewrite its settings.json with the current
        CLI_PATH Stop hook.
     d. `wait_shell_ready(new pane)` then `agent start <rec.name or claude-<sid[:6]>> --kind claude --pane <new> --timeout 60000 --
        --resume <sid> [--settings <agent_dir>/settings.json] <rec.args>`.
     e. success → update record (pane_id, tab_id, updated_at, restored_count+1) and agent meta.json (pane/tab, status `running`);
        `agent_not_ready`/blocked → leave the tab for the user, record status `blocked`, log the hint.
5. At the deadline: unmatched records get `unmatched_startups += 1` (pruned at 2). Summary to stdout (plugin log) and
   `<state>/restore.log` (+ `HERDR_PLUGIN_STATE_DIR/restore.log` when set). `--dry-run` prints the decisions only;
   `herdr-agent herdr status` prints records and their current match/classification.

Why it is safe against the attach race: pre-attach the old panes are either closed by us (if refs/ids are visible — S0
checks) or, if native resume already ran, its process is detected as **native** and replaced. Herdr clears a ref once a wrong
relaunch exits to the shell; the `pane_id`+cwd fallback still matches it.

---

## 8. Fake `herdr` contract (tests/fakes/herdr)

Python script, state in `$FAKE_HERDR_STATE` (JSON), every call appended to `state.calls`. Responses use the real envelope
`{"id": "...", "result": {...}}` and errors `{"error": {"code", "message"}}` with non-zero exit. Ids look like real Crockford
ids (`w1:t1`, `w1:p1A`, …) so code that formats ids is caught. Implements: `--version`, `integration status`, `tab create/get/close`,
`pane get/list/read/process-info/send-keys/close`, `agent start/get/list/prompt/send-keys/read`, `plugin link/unlink/list`,
`server reload-config`, `session list --json`. Knobs in state: `version`, `agent_start_error`, `start_blocked`, `shell_ready_after`,
`auto_result` (on `agent prompt`, the fake reads the pane's recorded `--settings`, runs its Stop hook command with
`{session_id, last_assistant_message: "RESULT: <prompt>", transcript_path}` in the tab env — exercises the real Stop hook),
`api_error_first_turn`, `panes[*].process_argv`, `panes[*].agent_session`. S1 copies real JSON shapes from read-only calls
(`herdr pane list`, `agent list`, `pane process-info`, `tab create` docs).

---

## 9. Test plan (unittest, `python -m unittest discover -s tests -v`)

- Isolation: every test gets temp HOME, XDG_*, TMPDIR, CLAUDE_CONFIG_DIR, HERDR_AGENTS_STATE_DIR, PATH = `tests/fakes:/usr/bin:/bin`,
  scrubbed `HERDR_*`/`CLAUDE_*`; the user's real files are never read.
- CLI: run happy path (stdout = result, tab closed, dir removed, record written then forgotten); spawn/wait/send/close; blocked
  (exit 3 + hint); untrusted cwd (exit 3, uses CLAUDE_CONFIG_DIR/.claude.json); depth limits; `--env` validation; API error →
  auto-continue → exit 7; gc dry-run; `path`/`version --json`.
- Roles: argv for explore/plan/--read-only/--agent; user `--permission-mode` after `--` wins; disallowed lists merged.
- Results: Stop hook writes all three files atomically; transcript fallback finds `<CLAUDE_CONFIG_DIR>/projects/*/<sid>.jsonl`
  (regression for the profile bug); synthetic non-error entries are not API errors.
- Hooks session-start: matrix {HERDR_ENV 0/1} × {depth 0/1} × {CLAUDE_ORCHESTRATOR on/off} × {sdk entrypoint} × {agent_id in
  payload} × {source startup/resume}; exact JSON shape; `{{HERDR_AGENT}}` substituted with CLI path; nothing printed when empty;
  CLAUDE_ENV_FILE export written once; garbage stdin → exit 0, no output; recording from fake process-info argv (needed / not needed
  / supersede by pane).
- Hooks pre-tool: Agent denied in Herdr depth 0 (reason contains CLI path, role, `--prompt-file` that exists with the prompt);
  allowed outside Herdr, with HERDR_AGENTS_REDIRECT=off, for passthrough types, at depth 1 without MAY_DELEGATE; Plan maps to
  `plan --delegate` only below max depth. Bash: allowed for our CLI (abs, `~`, symlink); denied-by-silence for other paths,
  `;`, `&&`, `|`, `$(`, redirections, `keys`, non-read-only role in plan mode; timing < 100 ms.
- Setup: setup twice → byte-identical settings; dry-run writes nothing; foreign hooks/perms preserved; moved CLI path replaces the
  old hook; uninstall leaves foreign entries and restores shape; bundle detection skips hook; bin clobber refusal and `--force`;
  config.toml fixtures (5 variants) incl. explicit false untouched; plugin copy has absolute python; `plugin link`/`reload-config`
  calls recorded by the fake; doctor JSON fields and exit codes (old herdr version → failure).
- Restore: scenarios on the fake — healthy (marker) → untouched; native argv → new tab + old pane closed + agent start args
  (`--resume sid --settings ... --name id ...`, env incl. CLAUDE_CONFIG_DIR); shell pane → relaunch; pane in multi-pane tab → only
  pane closed; no match until deadline → unmatched counter, pruned on 2nd; expired/missing transcript pruned; lock held → no-op;
  agent start blocked → record `blocked`, tab kept; reboot case (agent dir missing → recreated); config ensured + reload-config
  called once; `--dry-run` makes no mutating calls.
- Versions test; CI on ubuntu 3.10–3.13.

## 10. Manual E2E checklist (throwaway, never the user's default session)

Setup isolation: `HERDR_AGENTS_STATE_DIR`/`HERDR_AGENTS_DATA_DIR` in a scratch dir; install the Claude plugin via
`claude --plugin-dir <repo>` (no global install); the Herdr plugin link is global to the user → link, test, then
`herdr plugin unlink` (ask the user first; note it affects other sessions only at their next server start).
1. Restart test: `herdr --session t` → in a scratch repo already trusted, `herdr-agent spawn explore --model haiku --prompt "..."`
   and one main session started as `CLAUDE_CONFIG_DIR=<alt> claude --model sonnet --effort low` → `herdr-agent herdr status`
   shows both records → `herdr session stop t` → `herdr --session t` → within the grace period both panes run claude with the
   recorded argv (`herdr pane process-info`), env present (`echo $HERDR_AGENT_ID` in a split is not possible — check
   `herdr-agent wait <id>` returns a result written by the Stop hook), restore.log lists the actions, no duplicate claude per
   session (`ps`), plain sessions resumed natively and untouched.
2. Live handoff / second startup: run startup again (`herdr-agent herdr startup --dry-run`) → all records healthy, no changes.
3. Config: in the throwaway session's config path (`HERDR_CONFIG_PATH`) remove `[session]` → startup adds the key, reload-config ok;
   set `false` → left alone, doctor warns.
4. Plan mode: scratch git repo, open `claude --plugin-dir <repo>` inside `herdr --session t`, enter plan mode, ask for a plan
   needing exploration → transcript shows Agent(Explore) denied by the hook, then Bash `herdr-agent run explore ...` without a
   permission prompt, an explore tab opens and closes, the plan file is written, ExitPlanMode works. Repeat outside Herdr → Agent
   tool used normally. Repeat with `HERDR_AGENTS_REDIRECT=off` → Agent tool used.
5. Read-only role: ask the explore agent (directly) to edit a file → it refuses / tool disabled.
6. Orchestrator: new session shows injected text; `CLAUDE_ORCHESTRATOR=off claude` → only the Herdr section; sub-agent tabs get
   no injection.
7. setup path: `npx skills add <local repo>`-style copy into a temp `CLAUDE_CONFIG_DIR`, `herdr-agent setup --dry-run`, `setup`,
   `doctor`, `setup` again (no change), `setup --uninstall`.

---

## 11. Implementation subtasks (ordered; disjoint files inside each wave)

| id | wave | deps | model/effort | files | content |
|---|---|---|---|---|---|
| S0 | 0 | – | sonnet medium | none in repo; findings → scratchpad `spike-herdr.md` | Spike on a throwaway `herdr --session spike` (ask user consent first; Herdr plugin link is global): (1) after `session stop`/start, do `pane list`/`agent list` show saved `agent_session` refs and the same pane ids **before** a client attaches? (2) does native resume skip a pane we closed / a pane already running a process? (3) does `agent start` work pre-attach? (4) does `[[startup]]` run on 0.9.3 and what env does it get? (5) option B: does a second `report-agent` source keep its resume argv while `herdr:claude` keeps reporting? (6) does `claude --agent Explore` exist; does PreToolUse `allow` skip the Bash prompt in plan mode? Result adjusts §7 constants only. |
| S1 | 0 | – | sonnet medium | `.claude-plugin/*`, `.gitignore`, `LICENSE`, `CHANGELOG.md`, `README.md` (skeleton), `.github/workflows/test.yml`, `skills/herdr-agent/bin/herdr-agent`, `lib/herdragent/__init__.py`, `core.py`, `tests/_base.py`, `tests/fakes/herdr`, `tests/fakes/claude`, `tests/test_fake_herdr.py`, `tests/test_versions.py` | `git init`, scaffold, core helpers, fake herdr per §8 (copy real JSON shapes via read-only herdr calls). |
| S2 | 1 | S1 | opus medium | `herdr.py`, `trust.py`, `results.py`, `waiting.py`, `spawn.py`, `roles.py`, `commands.py`, `cli.py`, `hooks/__init__.py`, `hooks/stop.py`, `tests/test_cli.py`, `tests/test_roles.py`, `tests/test_results.py`, `tests/fixtures/transcripts/*` | Port the 1096-line CLI + stop hook with the §3 changes; cli.py registers `hook`, `herdr`, `setup`, `doctor` routes that import modules lazily (S4/S5/S6 create them); calls `launches.record_spawn/forget/refresh_pointer` per the §3 API. |
| S3 | 1 | S1 | sonnet medium | `launches.py`, `tests/test_launches.py` | Record store + argv sanitizer + pointer, exactly the API in §3. |
| S4 | 1 | S1 | sonnet medium | `setup.py`, `herdrconfig.py`, `herdr-plugin/herdr-plugin.toml`, `herdr-plugin/startup.py`, `tests/test_setup.py`, `tests/fixtures/config-*.toml` | setup/doctor, config edit, plugin copy/link (§3, §6). Reuse agent-vault `setup.py` merge code and claude-profiles `_write_settings`/util. |
| S5a | 1 | S1 | opus medium | `skills/orchestrator/SKILL.md`, `skills/herdr-agent/SKILL.md`, `context.py` | Generic texts: orchestrator (strip vault/vloop/task-*, `{{HERDR_AGENT}}`), herdr-agent skill (roles, --agent, --read-only, plan mode, model/effort matrix, no hardcoded paths), Herdr section + plan-mode mapping builder. |
| S5 | 2 | S2, S3, S5a | opus medium | `hooks/common.py`, `hooks/session_start.py`, `hooks/pre_tool.py`, `hooks/hooks.json`, `tests/test_hooks_session_start.py`, `tests/test_hooks_pre_tool.py` | Hooks per §3/§4 incl. recorder and perf test. |
| S6 | 2 | S0, S2, S3, S4 (herdrconfig) | opus high | `restore.py`, `tests/test_restore.py` | Resume algorithm §7 (+ `herdr status`), adjusted by S0 findings. Disjoint from S5. |
| S7 | 3 | all | sonnet medium | `README.md`, `CHANGELOG.md` | Install paths (plugin marketplace; skills.sh + `herdr-agent setup`; Herdr plugin via `setup --only herdr` or `herdr plugin link`), env table, plan-mode behaviour, restore limits, uninstall. |
| S8 | 3 | all | sonnet medium | none (QA) | Full unittest run, `doctor` in isolation, manual E2E §10 with user consent; findings back to the orchestrator. |

Then the orchestrator runs `/verify-loop` on the repo.

---

## 12. Open risks

1. **Pre-attach visibility** (S0 #1): if refs/pane ids are invisible before a client attaches, restore depends on catching native
   relaunches; a wrong-profile native relaunch exits fast and Herdr clears the ref — mitigated by the pane_id+cwd fallback, but
   pane-id stability across restarts is unverified.
2. **Replacing a native relaunch** closes the pane; tab order/position changes (new tab appended) and any user split layout around
   it is lost for that pane. Accepted v1 limitation.
3. **PreToolUse `allow` in plan mode** and deny-reason compliance are behaviour of Claude Code, verified only manually (S0 #6, E2E 4).
   Fallback: classic allow rules via `setup --only perms`.
4. **Hook overhead**: a Python process on every Bash call (~30–60 ms). If too slow, switch pre-bash to a POSIX-sh prefilter that
   execs python only when the input contains `herdr-agent`.
5. **Auto-allow security**: strict shlex parse, realpath match and no metacharacters; any parse doubt → fall through to the normal
   permission flow. `keys` never auto-allowed.
6. **Main-session recording** relies on `pane process-info` argv (`ps` fallback); wrappers that exec `claude` with extra env other
   than CLAUDE_CONFIG_DIR are not reproduced.
7. **Plugin + setup double install** → hooks run twice (double injection). Doctor warns; setup skips `hook` inside a plugin bundle
   but cannot see a separately installed plugin reliably.
8. **Versioned plugin cache path**: a running session keeps the old CLI path after `/plugin update`; if Claude deletes the old cache,
   commands fail until the next session. Per-agent Stop hooks are rewritten on restore.
9. **Herdr version drift** (0.9.3 installed, docs 0.9.5): `[[startup]]`, `plugin link` exist in 0.9.3; `min_herdr_version 0.9.2`
   per decision — S0 confirms startup on 0.9.3.
10. **Orchestrator default-on** is opinionated for third-party users; README must state the opt-out prominently.
11. **Agent-tool consumers** in other harness pieces (e.g. skills that dispatch custom agents through the Agent tool) are redirected
    at depth 0 in Herdr; they must support herdr-agent (`--agent NAME`) or be listed in `HERDR_AGENTS_PASSTHROUGH`.
    `claude --agent <plugin:agent>` for namespaced plugin agents is unverified.
12. **Herdr plugin link is global** to the OS user, so E2E on this machine touches the user's real Herdr registration — requires
    explicit consent and unlink afterwards (decision 5: no migration).
