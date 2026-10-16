# CLAUDE.md: working on agent-pause

Nothing is built yet. Read [docs/DESIGN.md](docs/DESIGN.md) first; it records
the user's decisions and the open questions.

## Where to start

1. Do the **spikes** in DESIGN.md first, especially Spike 1 (what happens to a
   slash command typed while a session is busy). Use a throwaway prototype (a
   flag file plus a PreToolUse deny hook) and write the findings into
   DESIGN.md before building the real plugin.
2. Then follow the build order in DESIGN.md.
3. Use this repo's own tracker convention while you work:
   `.claude/work/<task>/TRACKER.md`.

## Conventions (copied from the sister project)

The user built [../net-gateway](../net-gateway) the same way, so mirror it:
layout (`.claude-plugin/marketplace.json`, `plugins/agent-pause/` with
`run.py`, `src/<pkg>/`, `hooks/`, `commands/`, `tests/run_tests.py`),
hand-rolled stdio MCP server, `pixi run test` / `pixi run install`, Python
3.12 stdlib only, macOS and Linux. Its CLAUDE.md and docs/ARCHITECTURE.md
explain the patterns (prompt-hook pass-through with `additionalContext`, shell
pre-filters, hermetic tests with temp state dirs).

Gotcha: net-gateway is installed in the user's sessions. Its PreToolUse hook
blocks Bash commands whose text contains `curl`, `wget`, etc. (including commit
messages); use `git commit -F file` if a message mentions them.

## User preferences

- Show a plan and stop before installing anything or changing the user's
  settings; back up settings and show the diff first.
- Plain, concise docs. No license file unless asked.
