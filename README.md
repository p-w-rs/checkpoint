# checkpoint

A Claude Code plugin that automatically keeps a record of each task, lets
you pause Claude at once, and resumes safely after a pause, a crash, a
compaction or a restart. Resuming never just picks up where the notes say:
checkpoint first works out what changed and what may have happened, and
puts anything risky at the top.

```
you    > /check-pause
         checkpoint: paused this session (at once, in 0.21 s). Stopped the running turn.
         Still running: Bash `pytest -q`, left running (...)
         Tool calls stay held until you resume: type a message (any non-/check command resumes) or /check:resume.
you    > go on
Claude gets this first (abridged):
         Check first
         - Bash `git push origin staging`: its outcome is unknown and it may have changed state outside this
           repository. Check whether the push landed (`git fetch` then `git status -sb`) ... do not run it again until you know.
         - 1 file(s) changed outside this task since checkpoint c_01k8...: app/models.py (...) never revert them.
```

Plugin `check` 0.8.0, marketplace `checkpoint`. It works alone; with the
[gateway](https://github.com/p-w-rs/gateway) plugin, long jobs run under the
gateway's supervision and the hosts a task used are checked before a resume.

## What changes when you install it

- **Every session is recorded.** Prompts, tool calls and their outcomes,
  plan steps, file edits (as hashes), test runs, subagents and session
  changes go into a journal in `~/.local/state/checkpoint`, scrubbed first.
  You don't ask for it, and no model is involved.
- **A footer label.** The first session builds checkpoint's Python with pixi
  in the background: `check: setting up its runtime (first run); pausing
  works`, then `check: recording`.
- **Trackers.** For tasks with several steps, Claude is asked to keep notes
  in `<project>/.work/<task>/TRACKER.md` and to update them before it stops.
  In a git repository `.work/` goes into the clone's local exclude file
  (never committed), unless the repository already tracks it.
  `CHECKPOINT_TRACKERS=off` turns trackers off.
- **New commands:** `/check:*`, and `/check-*` forms that act at once, even
  mid-reply.
- **While paused,** a status line: `check: paused · type a message (any
  non-/check command resumes) or /check:resume`.
- **After a crash,** the next session in that project (within a week) shows:
  `checkpoint: task t_01k8... was interrupted ("migrate the schema..."):
  claude --resume <id> continues its session, or /check:resume t_01k8...
  picks it up here.`
- **A terminal helper** at `~/.local/state/checkpoint/checkpoint`.

Nothing is added to your settings or permission rules.

## Requirements

- **Claude Code 2.1.287 or newer** in the terminal, **2.1.286 or newer** in
  the desktop app. These versions have mods, which checkpoint uses for its
  immediate `/check-*` commands, the footer label, the status line, the
  Checkpoints pane and telling your messages from automated ones. Tested on
  2.1.301 (macOS) and 2.1.304 (Linux). Without mods (VS Code, `claude -p`,
  mods turned off) the hooks still record and the `/check:*` commands work,
  but there are no immediate commands, label, status line or pane, a pause
  waits for the next tool call, and any prompt resumes a paused session.
  Older versions are untested; the hooks need at least 2.1.139 (exec form).
- **pixi 0.68.0 or newer** (from pixi.sh). checkpoint brings its own Python
  through it.
- **git**, which Claude Code needs to add a marketplace from GitHub.
- **macOS or Linux.** Windows only through WSL 2 (untested). Tested on macOS
  (Apple silicon) and Debian 13 (ARM64); Intel Macs and Linux x86-64 are in
  the lock but untested.

## Install

In Claude Code:

```
/plugin marketplace add p-w-rs/checkpoint
/plugin install check@checkpoint
```

Or from a shell:

```
claude plugin marketplace add p-w-rs/checkpoint
claude plugin install check@checkpoint
```

In the desktop app, add the marketplace from a terminal first, then in the
Code tab click **+** → **Plugins** → **Add plugin** and choose **check**.

### First five minutes

1. Start a new session (or `/reload-plugins`). The footer goes from
   `check: setting up its runtime (first run); pausing works` to `check: recording`.
2. Run `/check:help` and `/check:status`.
3. Ask Claude to "run `sleep 60`, then say done", and type `/check-pause`
   while it runs. Then type `go on`.
4. `/check-save before the refactor`, then `/check-list`.
5. Kill that Claude from another terminal: `pgrep -fl claude` lists the
   processes with `claude` in their command line, and the session is the
   one whose command is just `claude`; `kill -9 <its number>`. Start
   `claude` again in the same folder: the interrupted task is offered. A
   normal quit (`/exit`, Ctrl-D) isn't a crash and shows no notice.

## Everyday use

| Command | What it does |
|---|---|
| `/check-pause [now]` | Pause now, even mid-reply. `now` also stops the running command (its whole process tree). |
| `/check:pause [all\|now\|<session>] [--stop-jobs]` | Pause this, every, or another session. Typed while Claude is busy it waits for the turn to end. |
| `/check:resume`, `/check-resume` | Resume after reconciling. |
| `/check:resume <task>\|last\|<checkpoint>` | Pick up a task in this session, or continue from a checkpoint (nothing is rolled back). |
| `/check:status` | Sessions, pause state, jobs, this project's unfinished tasks, this session's summary. |
| `/check:save [why]`, `/check-save` | Record a checkpoint now (synced to disk); stops nothing. Every pause saves one too. |
| `/check:list`, `/check-list` | The task's checkpoints (the dash form opens a pane to diff or resume). |
| `/check:diff [<cp> [<cp>\|now]]` | What changed between checkpoints, or since one. |
| `/check:stop [<job>]` | Stop a detached job, or list them. |
| `/check:cancel [interrupt\|detach]`, `/check:complete` | End the task, history kept. |
| `/check:export [<task>] [<path>]` | A scrubbed bundle of the task, to hand over. Review it before sharing. |
| `/check:forget <task>` | Delete one task's recorded history. |
| `/check:doctor`, `/check-doctor` | Versions, pixi, the runtime, the gate, the gateway, stuck jobs. |

Only you can run these; Claude can't. All but the pause and resume commands
work while paused and never resume.

**Pausing.** `/check-pause` (or typing `pause` while Claude is busy) ends
the running turn at once and holds every tool call except reads, tracker
edits, to-do lists, `checkpoint_note`, `task_policy` and the job tools
other than `job_start`. A running Bash command keeps running unless you add
`now`. Detached jobs keep running too (a gateway job follows its pause
policy; `/check:pause --stop-jobs` stops them). `pause all` pauses every
session on the machine at its next tool call.

**Resuming.** Any message you type resumes, and so does any slash command
except checkpoint's own (another plugin's skill, for example). With mods,
scheduled prompts, `/loop`, notifications and other agents never do. Claude
first gets the reconciliation: files changed outside the task (kept, never reverted),
test results that are now stale, newer instructions, and every interrupted
operation as never dispatched, confirmed completed, still running, confirmed
failed, or outcome unknown. An unknown one that may have changed something
outside the repository (a push, a deploy, a remote command) comes first, and
is never simply run again.

**After a crash.** Use the `claude --resume <id>` from the notice to get the
conversation back too, or type `/check:resume last` in the new session. In a
session, `last` is the newest task in this project other than this session's
own, an interrupted one first.

**What Claude can use.** `job_start` runs long work as a detached job that
survives pauses and the end of the session; it asks like a Bash command.
`job_status`, `job_tail`, `job_stop`, `checkpoint_note` (constraints,
decisions, open questions) and `task_policy` run without a prompt.

**From a terminal**, when no session is free:
`~/.local/state/checkpoint/checkpoint pause all` (also `status`,
`resume all`, `doctor`, `tasks`, `summary`, `reconcile <task>`,
`export <task>`, `stop <job>`, `forget <task>`, `purge`, and `help` for the
rest).

**Jobs and the gateway.** With the gateway serving the session,
`job_start` hands the job to the gateway, which runs and supervises it.
Each job has a pause policy: continue (the default), interrupt, suspend or
detach; a pause says what each one did. A job the gateway refuses runs
nowhere. The gateway also checks a job's command at `job_start`: it refuses
`curl`/`wget`, its private folder and session transcripts, rights set to
`false` and any `"ask"` right (nobody can be asked while a job runs; on a new
gateway install, sudo on this computer is `"ask"`), and it never approves a
job by itself. pixi-guard refuses Python outside pixi. The gateway masks what
`job_tail`, `job_status` and `job_stop` show Claude, except a failed
`job_stop`'s error text, which no hook can rewrite; it warns instead. More:
the gateway's [With checkpoint](https://github.com/p-w-rs/gateway#with-checkpoint)
section and [pixi-guard's README](https://github.com/p-w-rs/pixi-guard).
Without the gateway, checkpoint runs jobs itself: no pause policy, no remote
hosts, nothing survives a reboot, and a job's log keeps its last 32-64 MB.

## Configuration

No settings file; a few optional environment variables, set where Claude
Code starts. The ones people use:

| Variable | Default | |
|---|---|---|
| `CHECKPOINT_TRACKERS` | on | `off`: no tracker instructions, reminders or stop prompts |
| `CHECKPOINT_KEEP_DAYS` | 30 | days a finished task's journal is kept (`0`: forever) |
| `CHECKPOINT_KEEP_OPEN_DAYS` | 180 | the same for unfinished tasks |
| `CHECKPOINT_HOME` | `~/.local/state/checkpoint` | the state folder |

All of them: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Security and privacy

checkpoint is a record and a guardrail for pausing, not a sandbox. Claude's
commands run as you, with your permissions; checkpoint holds tool calls only
while a session is paused.

What it guarantees:

- **Local only.** It sends nothing over the network; pixi downloads Python
  from conda-forge once. Resume notes go into Claude's context like any text.
- **Private files.** The state folder is 0700, its files 0600.
- **Scrubbed before writing:** the journal, `checkpoint.json` and exports.
  File contents are never recorded.
- **Fails closed while paused:** if checkpoint can't check a call, it is held.
  Not paused, the call goes ahead and `/check:status` says so.
- **No new rights.** `job_start` goes through Claude Code's permission check.
- **Reconciling only reads.** It never writes your files, the repository or
  a transcript.

What it doesn't:

- **Scrubbing is pattern-based.** Known token shapes and credential forms
  are masked; a secret in a form no rule knows stays (`the password is
  hunter2` stays). Review an export before sharing it. The full list:
  [docs/dev/SCRUBBING.md](docs/dev/SCRUBBING.md).
- **Trackers are Claude's own text** and aren't scrubbed.
- **A job's log** from checkpoint's own runner is kept raw (capped).

## Your data

**Recorded** (scrubbed): your prompts (the first 4,000 characters of each),
plan steps, each tool call and its outcome (commands up to 2,000
characters, file paths, sizes, sha256 hashes, exit codes), the last 400
characters of a test, build or check's output, subagents and Claude's last
message of a turn (300 characters), session starts, the repository's branch
and HEAD, permission decisions (names only), and Claude's `checkpoint_note`
notes.

**Never recorded:** file contents, edit strings, what Read, Grep, Glob,
WebFetch or WebSearch returned, the output of other commands (only its
size), a failed command's output (only its exit code, or the last 500
characters for a failed test), reasons for permission decisions.

**Where and how long:**

| What | Where | Kept |
|---|---|---|
| Journals | `~/.local/state/checkpoint/journal/` | finished tasks 30 days after their last activity, unfinished ones 180; never while a session of the task runs or is paused, or a job of it may still run |
| Session records, and the files a pause leaves | the state folder | 7 days after the session's last activity, once its Claude has quit and it isn't paused. The checkpoints `/check:list` shows are journal records, kept with the journal |
| The spool (events waiting to be journaled) | `spool/<session>/` | seconds to a turn; raw while the runtime is ready or being set up, and without one only which tool ran, ids and sizes. Every session's spool holds at most 10,000 events or 50 MB; a spool goes 7 quiet days after its session is gone |
| Jobs and their logs | `jobs/` | 14 days after the job ends. A gateway job whose outcome is unknown is kept until the gateway reports its end, or you release it (`checkpoint forget-job <job>`, after 7 days unknown; `/check:doctor` names them) |
| Exports | `exports/`, or the path you gave | until you delete them |
| Trackers and `checkpoint.json` | `<project>/.work/<task>/` | until you delete them |
| The runtime (Python) | Claude Code's plugin data folder | rebuilt when missing; removed after 7 days unused, or by uninstall |

Claude Code keeps its own transcripts of every session; nothing here
touches them.

**Deleting it** (`checkpoint` is the terminal helper,
`~/.local/state/checkpoint/checkpoint`):

- `/check:forget <task>`, or `checkpoint forget <task> [--dry-run]` in a
  terminal: one task's journal, checkpoints, finished jobs and exports in the
  state folder. Refused while a session of that task runs or is paused (the
  current one too) or one of its jobs runs.
- `checkpoint purge [--dry-run]`: everything in the state folder except what
  a live or paused session or a running job still needs. It lists what it
  removes.
- `checkpoint prune --dry-run` shows what retention would remove now.

Everything, for good: see [Uninstall](#update-disable-uninstall).

## Limits

- The desktop app is untested (the same mod runs there).
- Without mods (older Claude Code, VS Code, `claude -p`, mods off), a pause
  waits for the next tool call, and any prompt resumes a paused session.
- `/check:pause` typed while Claude is busy waits for the turn to end. Use
  `/check-pause` or type `pause`.
- An immediate pause leaves a running Bash command running unless you add
  `now`.
- Whether a call changed something outside the repository is judged from its
  command and tool name. Anything else with an unknown outcome still says
  "check its effects".
- A plugin can't send messages to other sessions: after `resume all`, each
  session continues with your next message to it.
- Gateway jobs: `/check:cancel detach` detaches a job in checkpoint's
  records only; a task's jobs that another open session started are paused
  only once that session has ended; with the gateway gone, their state stays
  unknown.
- A job still running after its Claude session ends can no longer use the
  gateway's keys, passwords or git credentials, except from entries with
  `scripts = true`, because the gateway can't tell it came from Claude any
  more; keep the session open until such a job is done.

More: the scrubber's limits in [docs/dev/SCRUBBING.md](docs/dev/SCRUBBING.md),
the design's in [docs/dev/ARCHITECTURE.md](docs/dev/ARCHITECTURE.md).

## Update, disable, uninstall

| What | Command |
|---|---|
| Update | `claude plugin marketplace update checkpoint`, then `claude plugin update check@checkpoint`; then `/reload-plugins` or a new session |
| Turn off for now | `claude plugin disable check@checkpoint` (back on: `enable`) |
| Uninstall | `claude plugin uninstall check@checkpoint` (`--keep-data` keeps the built Python) |
| Forget the marketplace | `claude plugin marketplace remove checkpoint` |

In Claude Code, `/plugin` opens the same actions as a menu. An update that
changes the runtime builds it in the background while the old one keeps
recording.

**After an uninstall,** quit and restart every open Claude Code session:
they still have checkpoint's hooks and commands loaded, pointing at files
that are gone.

**What stays behind:** the state folder, your projects' `.work/` folders,
the `.work/` line in each clone's exclude file, and exports you saved
elsewhere. To remove everything:

1. Optional, before uninstalling: `~/.local/state/checkpoint/checkpoint purge
   --dry-run` names the projects it has seen, where `.work/` folders may be.
2. Uninstall, and restart open sessions.
3. `rm -rf ~/.local/state/checkpoint` (or your `$CHECKPOINT_HOME`).
4. In each project: `rm -rf .work`, and delete the `.work/` line from the
   file `git rev-parse --git-path info/exclude` prints.

**Upgrading.** Nothing to do; your history carries over (from 0.1, the
journal starts with the first session after the update). Records written
before 0.8.0 were scrubbed with fewer rules and kept failed commands' output,
and older `checkpoint.json` files in projects weren't scrubbed at all.
New exports scrub old records again with the new rules; exports made
earlier stay as they were (delete them; `purge` removes those in the state
folder). To drop old records, use `checkpoint forget` or `purge`, and delete
old `.work/*/checkpoint.json` files. What changed: [CHANGELOG.md](CHANGELOG.md).

**Rolling back.** Releases are tagged. To go back to one:
`claude plugin marketplace remove checkpoint` (this uninstalls the plugin),
then `claude plugin marketplace add p-w-rs/checkpoint#v0.7.3` and
`claude plugin install check@checkpoint`. To follow new releases again,
remove the marketplace and add it without the tag. Going back is untested;
the state folder stays as it is.

## Troubleshooting

Run `/check:doctor` (or `/check-doctor`, or `checkpoint doctor` in a
terminal). It shows the versions, pixi and the runtime, the gate, the
gateway, the state folder and any stuck gateway job.

- **`check: no runtime: pixi not found`.** Install pixi (it is also found in
  `~/.pixi/bin`, `~/.local/bin`, Homebrew and MacPorts, off PATH). Until
  then checkpoint records only which tools ran.
- **`check: runtime setup FAILED`.** The doctor shows pixi's error. The setup
  is retried at every session start.
- **`check: recording FAILED`.** Usually a full disk. Events that couldn't
  be saved at all are lost; a failed journal write is redone from the spool.
  Free space and recording resumes by itself, with any gap noted.

## Status

0.8.0 was tested on macOS (Apple silicon, Claude Code 2.1.301) and Debian 13
(ARM64, Claude Code 2.1.304): the test suite, and live Claude Code sessions
driven against a stand-in model; the suite also on Intel macOS packages
through Rosetta. Not tested: the desktop app, real x86-64 hardware. Details:
[docs/dev/TESTING.md](docs/dev/TESTING.md).

## Develop

Start with [CLAUDE.md](CLAUDE.md), then
[docs/dev/ARCHITECTURE.md](docs/dev/ARCHITECTURE.md).
The gateway contract is [docs/CONTRACT-v1.md](docs/CONTRACT-v1.md).

## Reporting security issues

See [SECURITY.md](SECURITY.md).
