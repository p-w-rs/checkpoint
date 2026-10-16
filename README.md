# checkpoint

A Claude Code plugin for pausing agents without breaking their work, so you
can close the laptop and pick up later.

Stop/Esc cuts an agent off mid-step, and closing the lid kills its commands.
`/check:pause` doesn't interrupt anything: the agent finishes its
current step, brings its progress tracker up to date at the next tool call,
and stops. Resume in the same session (send any message), or in a new one
from the tracker.

Sister project: [gateway](../gateway), built the same way.

> Developing on this repo? Start with [CLAUDE.md](CLAUDE.md), then
> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and
> [docs/DESIGN.md](docs/DESIGN.md).

## Commands

```
/check:pause                  pause this session at its next tool call (works while it's busy)
/check:pause all              pause every session on this machine
/check:pause now              ...and stop the running command right away (within ~2 s)
/check:pause now --stop-jobs  ...and stop this session's detached jobs too
/check:pause <session>        pause another session (ids from status)
/check:resume                 resume (any message does the same)
/check:resume all             clear pause-all; send each session a message to continue it
/check:resume <task>          in a NEW session: pick up a task from its tracker
/check:status                 sessions, pause state, tasks, jobs
/check:help
```

Typing just `pause` (or `pause now`, `pause all`) works too. From a terminal,
when no session is free to type into:

```bash
pixi run --manifest-path plugins/checkpoint/pixi.toml pause all
```

(also `status`, `resume all`, `pause <session>`).

## What the agent does

- **Tracker.** For any task with several steps, Claude keeps
  `<project>/.work/<task>/TRACKER.md`: stages with a status, the current
  step and next action, jobs, and notes for resuming. You decide whether
  `.work/` is committed or gitignored. A stage becomes `accepted` only when
  you say so in chat. Hooks remind Claude to keep the tracker current; they
  never edit it.
- **Jobs.** Anything likely to run more than ~2 minutes goes through the
  `job_start` tool: a detached process with a log, which survives pauses,
  sleep and the end of the session (Claude's own background Bash doesn't).
  `job_status`, `job_tail` and `job_stop` manage them. `pause now` leaves jobs
  running unless you add `--stop-jobs`.
- **When paused**, every tool call is denied except reading, editing the
  tracker and checking jobs. Claude updates the tracker and ends its turn with
  one line. A `checkpoint.json` next to the tracker records the last tool
  calls, git status, jobs and anything `pause now` killed.
- **On resume**, Claude gets the tracker's Now and Resume notes, the
  checkpoint, and what changed meanwhile (jobs that finished or died, files
  changed, time paused), then continues from "Next action".

## Install

Needs only [Claude Code](https://code.claude.com) and
[pixi](https://pixi.sh). macOS and Linux.

```bash
git clone https://github.com/p-w-rs/checkpoint.git
cd checkpoint/plugins/checkpoint
pixi run install --dry-run   # show what would change
pixi run install             # register the plugin, allow the job tools in settings.json
```

The installer registers this checkout as the marketplace `checkpoint`,
installs the plugin, and (after showing the diff and asking) adds allow rules
for the four job tools to your user `settings.json`, with a backup. Restart
Claude Code, then try `/check:help`.

To try it without installing: `claude --plugin-dir plugins/checkpoint`.

## Settings

`plugins/checkpoint/config/settings.json`: how often to remind about the
tracker, how many denied calls before the turn is ended outright, which tools
stay allowed while paused. Environment: `CHECKPOINT_HOME` (state, default
`~/.local/state/checkpoint`), `CHECKPOINT_WORK_DIR` (default `.work`).

## Limits

- Detecting a pause typed into a busy session relies on Claude Code writing
  queued messages to the transcript, an undocumented format. If that
  changes, typing `pause` still works (it's delivered at the next tool call).
- A plugin can't send messages into other sessions, so after `resume all`
  each session needs a message to continue.
