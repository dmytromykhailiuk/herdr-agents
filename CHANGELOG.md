# Changelog

## Unreleased

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
