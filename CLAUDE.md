# CLAUDE.md: working on checkpoint

The plugin (`plugins/checkpoint/`, v0.1.0) is **installed** for the user
from this checkout (edits are live in new sessions). User docs: [README.md](README.md). How it fits
together: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Why, the user's
decisions, and the spike findings it rests on: [docs/DESIGN.md](docs/DESIGN.md).
Work in progress: `.work/<task>/TRACKER.md` (this repo uses its own tracker
convention; `.work/` is gitignored here).

## Commands

```bash
cd plugins/checkpoint
pixi run test                     # must stay green; prints "N passed, 0 failed"
claude plugin validate .          # plugin manifest (and `claude plugin validate ../..`)
claude --plugin-dir .             # try it in a session without installing
pixi run status                   # sessions, pause state, jobs
pixi run install --dry-run        # what the installer would change
```

Tests are hermetic: `CHECKPOINT_HOME` is a temp dir, hooks run through the
real `hooks/fast.sh` with `CHECKPOINT_PYTHON` set to the test interpreter
(through a wrapper that counts calls, to prove the fast path skips Python),
and process tests build their own fake Claude trees. Add a check for every
behavior change; mutation-check the important ones.

The spike prototype and its drivers (`relay.py`: an interactive `claude` in
a pty that both the user and an agent can type into, run from the Terminal
panel since the sandboxed CLI can't reach the keychain; `drive_sdk.py`: the
stream-json input path the desktop app uses) were removed after the build.
Recover them with `git show b9420c1:spike/relay.py` etc. if a hook question
needs a new spike.

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
   `TAIL_BYTES`, `CHECKPOINT_WORK_DIR`).

## Conventions (copied from the sister project)

The user built [../gateway](../gateway) the same way, so mirror it:
layout (`.claude-plugin/marketplace.json`, `plugins/checkpoint/` with
`run.py`, `src/<pkg>/`, `hooks/`, `commands/`, `tests/run_tests.py`),
hand-rolled stdio MCP server, `pixi run test` / `pixi run install`, Python
3.12 stdlib only, macOS and Linux. Its CLAUDE.md and docs/ARCHITECTURE.md
explain the patterns (prompt-hook pass-through with `additionalContext`, shell
pre-filters, hermetic tests with temp state dirs).

Gotcha: gateway is installed in the user's sessions. Its PreToolUse hook
blocks Bash commands whose text contains `curl`, `wget`, etc. (including commit
messages); use `git commit -F file` if a message mentions them.

## User preferences

- Show a plan and stop before installing anything or changing the user's
  settings; back up settings and show the diff first.
- Plain, concise docs. No license file unless asked.
