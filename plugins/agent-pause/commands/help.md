---
description: Quick guide to agent-pause - pausing agents, resuming, trackers and jobs
disable-model-invocation: true
---

Give the user a short guide to the agent-pause plugin from the reference
below, about a screen: a one-line summary, the commands, then how trackers and
jobs fit in. If their message has a question after the command, answer that
instead. Don't call tools.

Reference:

- **What it is:** a pause that interrupts nothing. Each agent finishes its
  current step; at its next tool call it updates its tracker and stops.
  Resume in the same session, or in a new one from the tracker.
- **Commands** (typed by you; Claude can't run them):
  - `/agent-pause:pause`: pause this session. Works while it's busy: the
    command waits in Claude Code's queue, and agent-pause sees it there and
    pauses at the next tool call.
  - `/agent-pause:pause all`: pause every session on the machine.
  - `/agent-pause:pause now`: also stop the running command right away (within
    ~2 s). Its stage is marked to redo. Detached jobs keep running;
    add `--stop-jobs` to stop them too.
  - `/agent-pause:pause <session>`: pause another session (ids from status).
  - `/agent-pause:resume`: resume this session. Any message does the same.
  - `/agent-pause:resume all`: clear pause-all (send each session a message to
    continue it).
  - `/agent-pause:resume <task>`: in a new session, pick up a task from
    `.claude/work/<task>/TRACKER.md`.
  - `/agent-pause:status`: sessions, pause state, tasks, jobs.
  - Typing just `pause` (or `pause now`, `pause all`) also works.
- **Terminal** (no idle session to type into):
  `pixi run --manifest-path <plugin dir>/pixi.toml pause all` (also `status`,
  `resume all`).
- **Trackers:** for multi-step tasks Claude keeps
  `.claude/work/<task>/TRACKER.md`: stages with status, the current step and
  next action, jobs, resume notes. Stages become "accepted" only when you say
  so in chat. Claude is reminded to keep it current.
- **Jobs:** work longer than ~2 minutes runs through the job tools as a
  detached process that survives pauses, sleep and the end of the session;
  status and logs via `job_status` / `job_tail`.
- **Docs:** README.md and docs/ in the plugin's repo.
