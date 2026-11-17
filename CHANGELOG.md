# Changelog

## 0.1.1 - 2026-11-17

- Orchestrator skill: delegated agents record implementation decisions with `agent-report note` and end their final report with a `## Decisions` section.
- Integration 2: agent tabs get `HERDR_ROOT_SESSION_ID` and `HERDR_AGENT`, and forward `AGENT_VAULT_DIR`/`CLAUDE_VAULT_DIR`.
- Plan mode auto-allows `run vault-keeper` only with `--read-only`; the Agent-tool redirect adds it for `Operation: search`.
- The Agent-tool redirect of `vault-reviewer` (bare or plugin-namespaced) always adds `--read-only`, so plan mode auto-allows it.
- `setup`/`doctor` warn about a foreign SessionStart hook that injects orchestrator rules.

## 0.1.0

- `herdr-agent` skill and CLI: run Claude sub-agents as real interactive sessions in Herdr tabs (`run`, `spawn`, `wait`,
  `send`, `keys`, `result`, `screen`, `status`, `list`, `close`, `gc`, `path`, `version`); the final message comes back
  through a per-agent Stop hook file; exit codes for blocked, timeout, dead, no result and API errors.
- Read-only roles `explore` and `plan`, `--read-only`, `--agent NAME` for custom agents, `--delegate` for one level of
  read-only nested sub-agents.
- Plan mode through Herdr: the Agent tool is redirected to `herdr-agent run explore|plan`.
- `orchestrator` skill injected into every main session (opt out with `CLAUDE_ORCHESTRATOR=off`).
- Hooks: SessionStart (context injection, `$HERDR_AGENT` export, launch recording), SessionEnd (marks a session the user
  quit so restore skips it), PreToolUse Agent redirect, PreToolUse Bash
  auto-allow for `herdr-agent` commands (`keys` never allowed).
- Restore after a Herdr restart: bundled Herdr plugin relaunches agents and main sessions with their original flags and
  `CLAUDE_CONFIG_DIR`; `herdr-agent herdr status|startup`.
- `herdr-agent setup` (hooks, permission rules, bin link, Herdr plugin; `--scope`, `--only`, `--dry-run`, `--uninstall`) and
  `herdr-agent doctor`.
- Python shim that finds an interpreter ≥ 3.10.
- Claude Code plugin and marketplace manifests.
