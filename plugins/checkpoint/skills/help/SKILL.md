---
name: help
description: Quick guide to checkpoint - pausing, resuming after reconciliation, checkpoints, cancel, trackers and jobs
disable-model-invocation: true
---

Give the user a short guide to the checkpoint plugin from the reference
below, about a screen: a one-line summary, the commands, then how trackers and
jobs fit in. If their message has a question after the command, answer that
instead. Don't call tools.

Reference:

- **What it is:** automatic task tracking and a pause that interrupts
  nothing. Hooks record every prompt, tool call and result, plan step, file
  change (with hashes), test run (with the version it ran against),
  subagent and session change in a journal (secrets scrubbed), so a crash or
  a killed session loses nothing: the next session's recovery summary says
  what was done, what failed, what was in flight (outcome unknown), and
  whether test results are still current. The model can add constraints
  and decisions with the `checkpoint_note` tool. Pausing: each agent
  finishes its current step; at its next tool call it updates its tracker
  and stops. Resume in the same session, or in a new one.
- **Commands** (typed by you; Claude can't run them):
  - `/check-pause`: pause now, even while Claude is busy: the running turn
    stops at once and every tool call is held. A running Bash command is
    left running (it says so) unless you add `now`. Needs Claude Code
    2.1.287+ (mods); `/check-doctor` says whether they are on.
  - `/check:pause`: pause this session. Answered at once when idle; typed
    while Claude is busy it waits until the turn ends (Claude Code keeps a
    queued command from plugins), so then use `/check-pause` or type
    `pause`.
  - `/check:pause all`: pause every running session on the machine
    (sessions started later aren't paused).
  - `/check:pause now` (or `interrupt`), `/check-pause now`, or typing
    `pause now`: also stop the running command at once (its whole process
    tree), and say exactly what was stopped. Without `now` a running command
    is left running and the pause says so. Detached jobs keep running (a
    gateway job follows its `on_pause` policy); add `--stop-jobs` to stop
    them too.
  - `/check:pause <session>`: pause another session (ids from status).
  - `/check:resume`: resume this session after reconciling: what changed
    since the checkpoint (files changed outside the task are kept, never
    overwritten), git (HEAD, branch, dirty files), test results that are now
    stale, newer instructions, and every interrupted operation as never
    dispatched / completed / still running / failed or canceled / outcome
    unknown. An unknown one that may have changed something outside the
    repository (a push, a deploy) is checked first and never simply run
    again. Any message you type does the same; `/check-resume` (immediate)
    does it at once and lets the task go on.
  - `/check:resume all`: un-pause every session (each continues, with its
    resume notes, when you send it a message).
  - `/check:resume <task>` or `<checkpoint>`: pick up a task in this
    session (a journal task id, `last`, a tracker `.work/<task>/TRACKER.md`,
    or a checkpoint id to resume from that point: nothing is rolled back,
    the reconciliation compares it with now). It names the task's original
    session: `claude --resume <id>` continues that one with its whole
    conversation, and says when it is still running elsewhere.
  - `/check:list` (or `/check-list`, at once, with a pane to diff or resume
    from one): the task's checkpoints with time, journal seq, a one-line
    summary and whether each is still current. Each pause saves one.
  - `/check:diff [<checkpoint> [<checkpoint>|now]]` (or `/check-diff`): files,
    plan steps, operations, validation, git and instructions between them.
  - `/check:cancel [interrupt|detach]` (or `/check-cancel`, at once): end
    the task, history kept. Its running jobs are stopped (`interrupt`, the
    default unless a job was started with `on_cancel: detach`) or left
    running and released (`detach`); the next message starts a new task.
  - `/check:stop <job>` (or `/check-stop`): stop one detached job (its whole
    process group); alone it lists the running ones.
  - `/check:complete [note]`: mark the task done; the next message starts a
    new task.
  - `/check:export [<task>|last] [<path>]`: a sanitized bundle of the task
    (summary, checkpoints, journal excerpt, references; never credentials or
    file contents), 0600, in the state folder unless you name a path.
  - All of these work while paused, and none of them resumes.
  - `/check:status`: sessions, pause state, tasks, jobs, a degraded gate,
    and this session's recovery summary (from the journal).
  - `/check:save` (or `/check-save`, at once): checkpoint now: the task's
    state goes to the journal, synced to disk, without stopping anything.
    `/check-list` lists this task's checkpoints.
  - `/check:doctor` (or `/check-doctor`, at once): versions, pixi and its
    tested range, the runtime, the gate, the state folder.
  - Typing just `pause` (or `pause now`, `pause all`) also works; typed
    while Claude is busy, `pause` and `pause now` take effect at once.
  - Messages that aren't the user's (scheduled tasks, loops, notifications,
    other agents) never resume a paused session.
- **Terminal** (no idle session to type into):
  `~/.local/state/checkpoint/checkpoint pause all` (also `status`,
  `resume all`, `doctor`, `tasks`, `summary`, `list`, `diff`,
  `reconcile`, `export`, `stop <job>`, `prune`; in `$CHECKPOINT_HOME` if
  set).
- **Retention:** a finished task's journal (complete or canceled) is
  removed after 30 days without activity, an unfinished one after 180
  (`CHECKPOINT_KEEP_DAYS`, `CHECKPOINT_KEEP_OPEN_DAYS`; 0 keeps them
  forever), never while one of its sessions runs or is paused.
- **Setup:** the first session builds checkpoint's Python with pixi in the
  background (about a minute); pausing works meanwhile. It lives in Claude
  Code's plugin data folder, never in the plugin folder.
- **Trackers:** for multi-step tasks Claude keeps
  `.work/<task>/TRACKER.md`: stages with status, the current step and
  next action, jobs, resume notes. Stages become "accepted" only when you say
  so in chat. Claude is reminded to keep it current. In a git repo it adds
  `.work/` to the clone's local exclude file, so trackers aren't committed
  unless you want them to be.
- **Jobs:** work longer than ~2 minutes runs through the job tools as a
  detached process that survives pauses, sleep and the end of the session;
  status and logs via `job_status` / `job_tail`. Starting a job goes through
  Claude Code's usual permission check, like a Bash command; checking or
  stopping one doesn't ask. With the gateway plugin running, the gateway
  starts and supervises each job (`job_status` says `supervisor: gateway`)
  and applies its `on_pause` policy when you pause: continue (the default),
  interrupt, suspend or detach (per job, or for the task with
  `task_policy`); a pause says what each policy did, a resume undoes it,
  and a job whose state the gateway can't prove is "outcome unknown, it
  may still run", never finished. A job the gateway refuses (its policy,
  a paused session) runs nowhere, and the answer gives the gateway's
  reason. Otherwise (no gateway running, an incompatible one, or one that
  doesn't serve this session) checkpoint's own runner runs them (a pause leaves them running; `/check:pause
  --stop-jobs` stops them), in the gateway's environment when its env file
  is there (status shows "gateway env applied"). Other plugins, when
  installed, check a job's command at
  `job_start`: the gateway refuses curl/wget, its private folder and
  transcripts, rights set to false and any "ask" right, and never approves
  a job by itself; pixi-guard refuses Python outside pixi. The gateway
  masks what `job_tail`/`job_status`/`job_stop` show Claude (not a failed
  `job_stop`'s error text, which no hook can rewrite; it warns instead).
  A job still running after its Claude session ends can no longer use the
  gateway's keys, passwords or git credentials, except from entries with
  `scripts = true`, because the gateway can't tell it came from Claude any
  more; keep the session open until such a job is done.
  More: the gateway README's
  [With checkpoint](https://github.com/p-w-rs/gateway#with-checkpoint)
  section, [pixi-guard's README](https://github.com/p-w-rs/pixi-guard).
  Finished jobs and their logs are removed after 14 days.
- **Docs:** README.md and docs/ at https://github.com/p-w-rs/checkpoint.
