# CLAUDE.md: working on agent-pause

The plugin is built (`plugins/agent-pause/`, v0.1.0) but **not installed**
in the user's settings. User docs: [README.md](README.md). How it fits
together: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Why, the user's
decisions, and the spike findings it rests on: [docs/DESIGN.md](docs/DESIGN.md).
Work in progress: `.work/<task>/TRACKER.md` (this repo uses its own tracker
convention).

## Commands

```bash
cd plugins/agent-pause
pixi run test                     # must stay green; prints "N passed, 0 failed"
claude plugin validate .          # plugin manifest (and `claude plugin validate ../..`)
claude --plugin-dir .             # try it in a session without installing
pixi run status                   # sessions, pause state, jobs
pixi run install --dry-run        # what the installer would change
```

Tests are hermetic: `AGENT_PAUSE_HOME` is a temp dir, hooks run through the
real `hooks/fast.sh` with `AGENT_PAUSE_PYTHON` set to the test interpreter
(through a wrapper that counts calls, to prove the fast path skips Python),
and process tests build their own fake Claude trees. Add a check for every
behavior change; mutation-check the important ones.

`spike/` holds the throwaway prototype and drivers from the spikes:
`relay.py` runs an interactive `claude` in a pty that both the user and an
agent can type into (use it from the Terminal panel; inside the sandbox the
CLI can't reach the keychain), and `drive_sdk.py` uses the stream-json input
path the desktop app uses.

## Invariants

1. The gate **fails open**: any error means no output, and the call goes ahead.
2. The common path (not paused, nothing queued) never starts Python.
3. Hooks never write the user's or agent's tracker text; only
   `checkpoint.json` next to it.
4. Deny always comes with `continue: false`, never `continue: false` alone
   (that lets the tool run; Spike 3).
5. Trackers live outside `.claude/` (`.work/` by default): Claude Code asks
   before every write under `.claude/`, and no allow rule or hook lifts that.
6. Paths in `fast.sh`/`watch.sh` and `state.py` must agree (state layout,
   `TAIL_BYTES`, `AGENT_PAUSE_WORK_DIR`).

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
