# checkpoint

A Claude Code plugin that keeps a durable record of each task on its own, lets
you pause an agent at once or at a clean point, and resumes it safely after a
pause, a crash, a compaction or a restart.

- **Automatic tracking.** Hooks record every prompt, tool call and its
  result, plan step, file edit (with hashes), test or build run (with the git
  state it ran against), subagent and session change into a journal, with
  secrets scrubbed first. Nothing to ask for, no model involved. If Claude
  Code is killed mid-task (no Stop, no handoff), the next session in the
  project offers the task, and its recovery summary says what the goal was,
  what was done, what failed, what was still running (its outcome unknown,
  never "done"), and whether the test results still match the files.
- **Pause.** `/check-pause` (or typing `pause` while Claude is busy) stops it
  at once, mid-reply too, records where it was, and says what is still
  running. `/check:pause` lets the current step finish first.
- **Resume, reconciled.** Resuming never just picks up where the notes say:
  files changed outside the task are listed and kept, test results whose
  files or HEAD changed are marked stale, newer instructions are named, and
  every interrupted operation is classed as never dispatched, confirmed
  completed, still running, confirmed failed or canceled, or outcome
  unknown. One whose outcome is unknown and that may have changed something
  outside the repository (a push, a deploy, a remote command) comes first
  under "Check first": find out whether it took effect, never run it again
  blindly.
- **Checkpoints and jobs.** `/check:save` records a checkpoint without
  stopping anything; list, diff, resume from or export them. Long work runs
  as detached jobs that survive pauses and the session.

Plugin `check` (version 0.7.3) in the marketplace `checkpoint`
(`github.com/p-w-rs/checkpoint`). Sister plugin:
[gateway](https://github.com/p-w-rs/gateway), built the same way; with it,
jobs run in the gateway's managed shell and each host a task used is checked
before a resume.

> Developing on this repo? Start with [CLAUDE.md](CLAUDE.md), then
> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and
> [docs/DESIGN.md](docs/DESIGN.md).

## Status: built and tested

"Tested live" means a real Claude Code session in a terminal, driven
automatically against a stand-in model (never the real one), with the plugin
installed from a git marketplace into a throwaway configuration, on macOS
arm64 (Claude Code 2.1.301) and a Debian 13 arm64 VM (Claude Code 2.1.304),
2026-10-21. The live checks ran on 0.7.3, except on the Linux VM: the
checks in the concurrency row (concurrent sessions, duplicate events, a hung
runtime) last ran on 0.7.1, and the permission and either-plugin-alone
checks ran on an earlier 0.7.3 build that still had a small mod change,
since reverted. The repository's own test suite passes on macOS (961
checks under sh, 955 under dash) and on the Linux VM (967 under bash, 955
under dash). The install from this GitHub repository was tested too: a fresh
install with a live session on both systems; on the Linux VM, the `/plugin
marketplace add` and `/plugin install` forms and both kinds of uninstall; on
macOS, from the command line, an update from 0.7.2, disable and enable, and
both kinds of uninstall.

| Area | Built | Tested |
|---|---|---|
| Install, first-use setup, update, uninstall | yes | live (macOS, Linux VM) from a local git marketplace; from GitHub as described above |
| The journal survives a kill without a handoff; recovery summary | yes | live (macOS, Linux VM) |
| Immediate pause during a reply, a long command, queued work; no self-resume | yes | live (macOS, Linux VM) |
| Resume after a crash, `--resume`, `/compact`, `/clear`; stale tests; a lost push not repeated | yes | live (macOS, Linux VM) |
| Checkpoints pane (`/check-list`) | yes | live in the terminal; desktop app untested |
| Permission verdicts recorded and checked before a resume | yes | live (macOS, Linux VM) |
| Jobs handed to the gateway, per-job pause policies | yes | live, both plugins installed (local jobs; remote hosts not from a session) |
| Either plugin alone, disabled, an incompatible or upgraded gateway | yes | live (macOS, Linux VM) |
| Concurrent sessions, duplicate events, a hung runtime, a full disk | yes | live (full disk on macOS only; the Linux VM on 0.7.1) |
| The desktop app | the same mod | untested |
| Artifact references beyond changed files | no | |

Details per requirement: [docs/PLAN-v4.md](docs/PLAN-v4.md).

## Before you install

- **Claude Code.** The hooks, skills and job tools work on any recent
  version (tested on 2.1.301 and 2.1.304). The immediate controls
  (`/check-pause`, `/check-save`, ...), the footer label and the status line,
  the Checkpoints pane and telling your messages from automated ones need its
  mods: **2.1.287 or
  newer** in the terminal, **2.1.286 or newer** in the desktop app.
- **pixi 0.68.0 or newer** ([pixi.sh](https://pixi.sh)); tested: 0.68.0,
  0.68.1, 0.72.0, 0.76.0, 0.79.0, 0.81.0 and 0.82.0. 0.67 and older can't
  read the lock.
- **git**, which Claude Code itself needs to add or update a marketplace
  from GitHub. If you don't have it: `pixi global install git`, then `git
  --version`; if that still says "not found", add pixi's folder to your PATH
  (`export PATH="$HOME/.pixi/bin:$PATH"`), which pixi's installer normally
  does.

Nothing else: checkpoint brings its own Python through pixi.

## Install

In Claude Code (terminal):

```
/plugin marketplace add p-w-rs/checkpoint
/plugin install check@checkpoint
```

From a shell (the same thing):

```
claude plugin marketplace add p-w-rs/checkpoint
claude plugin install check@checkpoint
```

In the desktop app, add the marketplace once from a terminal (above), then in
the Code tab click **+** → **Plugins** → **Add plugin** and choose **check**
(the app and the terminal share their plugins).

Then **start a new session** (or run `/reload-plugins`) and try `/check:help`.
The first session builds the plugin's Python with pixi in the background
(`pixi install --locked`: exactly the versions in its lock); it takes
seconds on a fast network, longer on a slow one, and says so once
("first-time setup is running in the background"; the prompt footer:
`check: setting up its runtime (first run); pausing works`, then `check:
recording`). Pausing works meanwhile. If Claude Code quits during the setup,
the next session finishes it. If pixi can't be found, the session says so.

Where checkpoint shows its state (with mods): the normal states as a dim
label at the right of the prompt footer (`check: recording`, `check: setting
up its runtime (first run); pausing works`, `check: recording · updating its
runtime in the background`), and only what needs you on Claude Code's status
line under the prompt, which Claude Code draws with a warning sign for every
plugin: `check: paused · type a message or /check:resume to resume`,
`check: recording FAILED · /check-doctor`, `check: runtime setup FAILED ·
/check-doctor; pausing works`, `check: no runtime: pixi not found ·
/check-doctor; pausing works`, `check: state unknown (/check-doctor)`.

Nothing is added to your settings (Claude Code records the install itself, as
for any plugin).

## Update, disable, uninstall

From a shell (in Claude Code, `/plugin` opens the same actions as a menu):

| What | Command |
|---|---|
| Update | `claude plugin marketplace update checkpoint`, then `claude plugin update check@checkpoint`; then `/reload-plugins` in a running session, or start a new one |
| Turn off for now | `claude plugin disable check@checkpoint` (back on: `claude plugin enable check@checkpoint`) |
| Uninstall | `claude plugin uninstall check@checkpoint` |
| Uninstall, keep the built runtime | `claude plugin uninstall --keep-data check@checkpoint` |
| Forget the marketplace | `claude plugin marketplace remove checkpoint` |

An update installs the new version next to the old one; when it changed the
runtime (a new `pixi.lock`), the new one builds in the background while
checkpoint keeps running on the previous one (the prompt footer says
"check: recording · updating its runtime in the background"). Updating works from
any later shell; no special PATH or wrapper is needed beyond git for
`marketplace update`. `uninstall` deletes the plugin's data folder
(`~/.claude/plugins/data/check-checkpoint`, or under `$CLAUDE_CONFIG_DIR`:
the built Python, nothing else); `--keep-data` keeps it. Either way your
history stays: the state folder and the trackers in your projects
([Data, retention, backup](#data-retention-backup)).

**Development.** Try a checkout for one session with
`claude --plugin-dir plugins/checkpoint` (Claude Code then writes
`.claude-plugin/types/` into that folder; it is git-ignored; the runtime is
built in `~/.claude/plugins/data/check-inline`). To test an install the way
users get it, use a scratch Claude Code configuration and a git source:

```
export CLAUDE_CONFIG_DIR=$(mktemp -d)
claude plugin marketplace add <git URL or path to a git clone>
claude plugin install check@checkpoint
```

Install from git only: a local folder added as a marketplace is read in place,
and its install copies the whole folder, ignored files included. Tests and
the acceptance scripts: [CLAUDE.md](CLAUDE.md).

## Supported systems

| | |
|---|---|
| macOS, Apple silicon | tested (macOS 26): everything above; from GitHub, the `/plugin` forms only on the Linux VM (see Status) |
| Linux, ARM64 | tested in a Debian 13 VM: the test suite (under dash and bash) and live sessions |
| macOS Intel, Linux x86-64 | in the lock (osx-64, linux-64), expected to work, untested (no such machine was available) |
| Windows | native Windows isn't supported (the hooks need a POSIX `sh`, the lock has no win-64, the code uses POSIX-only modules). Use WSL 2: install Claude Code and pixi inside it, and the Linux rules apply (untested) |
| Claude Code surfaces | the terminal: tested. The desktop app: built for (the same mod), untested. Without mods (VS Code, the mobile app, Remote Control, `claude -p`, or mods switched off) everything works but the immediate controls, the footer label and status line, and the pane, and pausing waits for the next tool call |

## What it installs

Python 3.12 from conda-forge through pixi, at the exact version in the
plugin's `pixi.lock`, into Claude Code's data folder for the plugin; the code
is Python's standard library. The hooks are POSIX `sh` scripts, and the mod is
run by Claude Code itself. From the system it uses only `sh`, `ps` and, when
you run `git` in a project, git itself (read-only commands). Nothing goes
system-wide.

## Commands

```
/check-pause                  pause now, even mid-turn: stops the running turn (immediate; Claude Code 2.1.287+)
/check-pause now              ...stopping the running command first (its whole process tree); `interrupt` = `now`
/check:pause                  pause this session (answered at once when idle; typed while Claude is busy it
                              waits until the turn ends: use /check-pause, or type pause, to stop it sooner)
/check:pause all              pause every running session on this machine
/check:pause now              ...and stop the running command
/check:pause now --stop-jobs  ...and stop this session's detached jobs too (the gateway's through the gateway)
/check:pause <session>        pause another session (ids from status)
/check:resume                 resume after reconciling (any message you type does the same)
/check-resume [checkpoint]    the same at once, and the task goes on (immediate; works while paused)
/check:resume all             un-pause every session; each continues with your next message to it
/check:resume <task>          pick up a task in this session (a journal task id, `last`, or a tracker)
/check:resume <checkpoint>    ...from that checkpoint (reconciled against it; nothing is rolled back)
/check:status                 sessions, pause state, tasks, jobs, the gateway, a degraded gate, and this
                              session's recovery summary (from the journal)
/check:save [why]             checkpoint now: the task's state, synced to disk; stops nothing
/check-save [why]             the same, at once, mid-turn too
/check:list [<task>]          the task's checkpoints: time, journal seq, a one-line summary, current or stale
/check-list                   the same at once, with a pane: diff a checkpoint with now, or resume from it
/check:diff [<cp> [<cp>|now]] files, plan steps, operations, validation, git and instructions between two
                              checkpoints, or one and now; /check-diff at once
/check:cancel [interrupt|detach]  end the task (history kept): its running jobs stopped, or left running and
                              released; the next message starts a new task; /check-cancel at once, mid-turn
/check:stop [<job>]           stop one detached job (its whole process group; a gateway job through the
                              gateway); alone: list them; /check-stop (says the outcome, never the job's log)
/check:complete [note]        mark the task done; the next message starts a new task
/check:export [<task>] [<path>]  a sanitized bundle of the task (0600): summary, checkpoints, journal
                              excerpt, references; never credentials or file contents
/check:doctor                 versions, pixi and its tested range, the runtime, the gate, the gateway, the state
/check-doctor                 the same, at once, mid-turn too
/check:help
```

Everything but `/check:pause` and `/check:resume` works while a session is
paused and never resumes it; `/check:resume <name>` with a name that matches
nothing leaves it paused and says so. Picking up a task you completed or
canceled reopens it. Typing just `pause` (or `pause now`, `pause all`) works
too, and typed while Claude is busy it takes effect at once (with mods).

Why two spellings: commands a plugin's mod registers can't carry the
plugin's `check:` prefix, and a bare `/pause` belongs to the `/check:pause`
skill, so the immediate form is `/check-pause`. `/check:pause` stays (it
works without mods too).

What an immediate pause does: it writes the pause flag, ends the running turn,
and says at once what was paused and what still runs. A Bash command that was
running is **left running** (the acknowledgement names it) unless you add
`now`. Detached jobs keep running (a gateway job follows its `on_pause`
policy, and the acknowledgement says what it did). Every tool call is then
held until you resume by typing a message or `/check:resume`. Messages that
aren't yours (a scheduled task, a `/loop`, a notification, another agent),
and the slash commands they run, never resume a paused session.

From a terminal, when no session is free to type into, use the helper every
session writes into the state folder:

```bash
~/.local/state/checkpoint/checkpoint pause all
```

(also `status`, `resume all`, `pause <session>`, `resume <session>`,
`doctor`, `tasks`, `summary [<task>|last]`, `save <session>`, `list
<session>|<task>`, `diff <task> [<cp> [<cp>|now]]`, `reconcile <task>|last
[<cp>]`, `export <task>|last [<path>]`, `stop <job>`, `prune [--dry-run]`; in
`$CHECKPOINT_HOME` if you set it).

## What it records and does

- **Journal (automatic).** Every event goes into a journal of the task,
  without Python on the common path: the hooks put each payload in a spool
  file, and checkpoint's Python moves it into the journal at the moments it
  runs anyway (session start, the end of each turn, session end, compaction,
  a pause, `/check:*`, the job tools, and every 20 s from the mod while there
  is something new). Recorded, after scrubbing: the goal (the task's first
  prompt) and your later instructions; plan steps from Claude's task tools;
  each tool call's intent and outcome (a second delivery of the same event is
  dropped); file edits with the file's sha256 before and after; commands
  with their result (an exit code only when Claude Code states one; work
  that went on in the background is recorded as started, never as done);
  tests and builds with the git HEAD they ran against and the hashes of the
  files; subagents; session starts (startup, resume, clear, compact, fork)
  with their parent; the repository (root, branch, HEAD, worktree);
  compactions; pauses and checkpoints. Claude can add constraints,
  decisions, open questions and completion criteria with the
  `checkpoint_note` tool. File contents and edit strings are never stored
  (their size and hash are); a command's output is kept (scrubbed, its last
  400 characters) only for tests, builds and checks. The recovery summary is
  built from the journal alone (no model) and labels what was observed and
  what is inferred.
- **Authorizations and hosts.** The journal records which rights and
  approvals the task's calls used, by name and decision only (never a reason
  text, value or command): Claude Code's permission verdicts (settings rules,
  hooks, your answers to prompts), auto mode's denials, the permission mode,
  and the gateway's rights, accounts and dialogs. Before a resume each is
  checked against now (your settings files, the gateway's current state):
  unchanged ones are said to still hold, so nobody asks you again; revoked or
  changed ones go into "Check first". With the gateway, each host the task
  used is reported as the gateway sees it now (reachable, authenticated,
  runtime ready; "unknown" when the gateway can't be asked), operations that
  need an unreachable host are marked, and a host whose identity changed
  stops the resume until it is checked.
- **Tracker.** For a task with several steps, Claude may keep
  `<project>/.work/<task>/TRACKER.md` (stages, the current step, notes for
  resuming); in a git repo `.work/` goes into the clone's local exclude file.
  Hooks remind Claude to keep it current; they never edit it.
- **Jobs.** Anything likely to run more than ~2 minutes goes through the
  `job_start` tool: a detached process with a log, which survives pauses,
  sleep and the end of the session. `job_status`, `job_tail` and `job_stop`
  manage them. `pause now` leaves jobs running unless you add `--stop-jobs`.
- **Jobs and the gateway.** With the [gateway](https://github.com/p-w-rs/gateway)
  plugin serving the session (its daemon's discovery file, CONTRACT-v1
  `gateway.v1`, feature `jobs`, and a daemon that answers this session),
  `job_start` hands the job to the gateway: it runs in the gateway's managed
  bash and the gateway supervises it; checkpoint keeps a record (the
  gateway's job id, an idempotency key from the tool call) and asks the
  gateway for its state, its output (`job_tail`, the gateway's scrubbed tail)
  and to stop it. One supervisor per job, never two: a start whose answer was
  lost is retried once with the same key, and if the first try may have
  started it, the result is "outcome unknown" with the key, never a second
  start. Each gateway job has an `on_pause` policy: `continue` (the
  default), `interrupt` (SIGINT, then SIGTERM after 10 s), `suspend` (SIGSTOP
  until the task resumes) or `detach` (keeps running, released from the
  task); Claude sets it per job, or for the task with the `task_policy` tool
  (also `on_cancel`). A pause tells the gateway (`POST /v1/pause` with a
  pause id, so it is never applied twice) and says what each policy actually
  did; the resume tells it again. A job whose state the gateway can't prove
  is "outcome unknown, it may still run": never shown as finished or stopped,
  and kept until the gateway says what became of it. Without the gateway (or
  with an incompatible one: "gateway contract mismatch (has X, needs 1)";
  or one whose daemon doesn't serve this session), jobs run in checkpoint's
  own runner, whose limits `/check:status` states: no per-job pause policy,
  no terminal or input, no remote hosts, nothing survives a reboot. A job
  the gateway itself refuses (its policy, a paused session: any refusal
  other than "this session isn't served here") is started nowhere, and the
  answer and the journal give the gateway's reason (since 0.7.2; before,
  any HTTP 403 ran it in the own runner instead). Such a
  job's shell sources the gateway's `~/.config/gateway/env.sh` when it is
  there (stand-ins first on PATH, your SSH agent and tokens removed);
  `job_status` says whether that worked (`gateway_env`: `applied`,
  `failed`, or `refused: <why>`). Other plugins also check a job's command at
  `job_start`: the gateway refuses `curl`/`wget`, its private folder and
  session transcripts, rights set to `false` and any `"ask"` right (nobody
  can be asked while a job runs), and never approves a job by itself;
  pixi-guard refuses Python outside pixi. The gateway also masks what
  `job_tail`, `job_status` and `job_stop` show Claude, except the error text
  of a failed `job_stop`, which no hook can rewrite (the gateway warns
  instead). What crosses from the gateway into the journal is ids, states,
  counts and names, scrubbed again. Details: the gateway README's
  [With checkpoint](https://github.com/p-w-rs/gateway#with-checkpoint),
  [CONTRACT-v1](docs/CONTRACT-v1.md) and
  [pixi-guard's README](https://github.com/p-w-rs/pixi-guard#what-it-does)
  ("Checkpoint jobs too").
- **When paused**, every tool call is denied except reading, editing the
  tracker and checking jobs. Claude updates the tracker and ends its turn
  with one line.
- **On resume** (a message you type, `/check:resume`, `/check-resume`, a
  restart with `claude --resume`, after `/compact`), Claude first gets the
  reconciliation: "Check first" (riskiest first: an unknown outcome that may
  have changed something outside the repository, then anything still
  running, files changed outside the task, stale test results, a moved
  HEAD), the interrupted operations in their five classes (a saved PID or
  command line alone proves nothing: a process counts only with the same
  command, started after the call), the files, git, validation, the
  instructions in force then and given since, and the task's lineage; then
  the recovery summary. Every pause saves a checkpoint, so a resume compares
  with the state at the pause. However long the list, "Check first" stays;
  `~/.local/state/checkpoint/checkpoint reconcile <task>` prints all of it.
- **Lineage.** A task prefers its original session: the summary and the
  resume context show the exact `claude --resume <id>` and whether that
  session is still running elsewhere. `/check:resume <task>` in another
  session is an explicit continuation that records its parent; two sessions
  recording into one task are detected and said.

## Permissions

Checkpoint adds no rules to Claude Code's settings and has no modes of its
own.

- `job_status`, `job_tail`, `job_stop`, `checkpoint_note` and `task_policy`
  run without a prompt (they only read, stop a job or write the journal).
- `job_start` runs any command, so it goes through Claude Code's usual check
  like Bash (a prompt in default mode, the reviewer in auto mode); the
  gateway, when installed, judges its command first.
- While a session is paused, its tool calls are denied except reading,
  tracker edits, the job tools above and the gateway's read-only terminal
  and job tools; if checkpoint's Python can't answer then, the call is
  denied (fail closed). Not paused, a failure lets the call go ahead and
  says so (`/check:status`, `/check:doctor`).
- Recorded authorizations are references (names and decisions); checkpoint
  never grants anything by itself, and a resume says which earlier approvals
  still hold so you aren't asked again for unchanged access.

## Configuration

There is no settings file. Environment variables, all optional:
`CHECKPOINT_HOME` (state, default `${XDG_STATE_HOME:-~/.local/state}/checkpoint`),
`CHECKPOINT_WORK_DIR` (tracker folder, default `.work`),
`CHECKPOINT_NUDGE_EVERY` (tracker reminder every N tool calls, default 25),
`CHECKPOINT_GATEWAY_ENV` (the gateway env file jobs source, default
`~/.config/gateway/env.sh` in your home from the password database, not
`$HOME`; set but empty: none), `CHECKPOINT_GATEWAY_CONTRACT` (the gateway's
discovery file, default `~/.config/gateway/run/contract.json` in the same home;
set but empty: checkpoint doesn't use the gateway), `CHECKPOINT_KEEP_DAYS` (days a finished task's
journal is kept after its last activity, default 30; `0` or `never`: forever),
`CHECKPOINT_KEEP_OPEN_DAYS` (the same for unfinished tasks, default 180),
`CHECKPOINT_HOOK_DEADLINE` (seconds a hook waits for checkpoint's Python
before it goes on without it, fail-safe, default 20; Claude Code's own hook
limits are 30 and 60 s), `CHECKPOINT_BG_DEADLINE` (seconds a background
ingest may run before it is stopped, default 300).

## Data, retention, backup

| What | Where | Kept |
|---|---|---|
| Trackers (the agent's notes) and `checkpoint.json` | `<project>/.work/<task>/` | until you delete them |
| Session records, pause flags, checkpoints of sessions without a tracker | the state folder (`~/.local/state/checkpoint`, or `$CHECKPOINT_HOME`) | sessions that ended more than 7 days ago and aren't paused are pruned; a paused one is kept |
| Detached jobs: registry, logs, exit codes (a gateway job: its record only; the gateway keeps its output) | the state folder, `jobs/` | 14 days after a job ends (with its log); a gateway job whose outcome is unknown until the gateway reports it ended |
| The journal: one folder per task (`journal/<task>/seg-*.jsonl`, checkpoints' summaries) | the state folder, `journal/` | a finished task (complete or canceled) 30 days after its last activity, an unfinished one 180 (`CHECKPOINT_KEEP_DAYS`, `CHECKPOINT_KEEP_OPEN_DAYS`; 0 = forever); never while one of its sessions runs or is paused or one of its jobs runs (or may still run); checked at most every 6 hours (`checkpoint prune --dry-run` shows what would go) |
| Exports (`/check:export`) | the state folder, `exports/` (or the path you give) | until you delete them |
| The spool: events waiting for Python | the state folder, `spool/<session>/` | each event until it is in the journal (seconds to a turn) |
| Recording failures: `spool-errors/<session>` and `journal-errors/<session>` while it fails, `<session>.last` after, and a 1 KiB reserve per session that a failure on a full disk turns into its record | the state folder | with the session |
| The runtime (pixi's Python) | Claude Code's plugin data folder (`~/.claude/plugins/data/check-checkpoint`) | a cache: rebuilt when missing; an old one is removed after 7 days unused; `uninstall` deletes it |

The state folder is private (0700, its files 0600). Updates never touch it or
your trackers; uninstalling doesn't either. **Backup**: copy the state folder
and your projects' `.work/` folders (with Claude Code stopped, or at least no
session paused or pausing). **Restore**: copy them back to the same places.
Everything journaled is scrubbed first (token shapes such as `ghp_`,
`github_pat_`, `hf_`, `sk-`, AWS keys, Slack tokens, JWTs, private key blocks,
passwords in URLs, Authorization headers, and `*KEY*`/`*TOKEN*`/`*SECRET*`/
`*PASSWORD*` assignments and fields, names written without separators too);
the spool holds Claude Code's raw hook payloads until they are journaled
(private, deleted right after; the session transcript holds the same). A job
checkpoint runs itself keeps its raw output in its log file.

## Upgrading

**From 0.1 or 0.2.** Nothing to do, and nothing is deleted:

- **The state folder** stays where it was; its permissions are tightened once
  (folders 0700, files 0600). 0.1's global "pause all" marker becomes a pause
  flag for each session it still covered, then goes.
- **Trackers** (`.work/<task>/TRACKER.md`) keep working: a session paused
  under 0.2 resumes with its tracker and `checkpoint.json`, and
  `/check:resume <tracker>` picks one up.
- **The journal** starts with the first session after the update: earlier
  tasks have no journal (their tracker stays their record), so their
  recovery summary is the tracker's.
- **Jobs started by 0.1 or 0.2** that still run keep running in checkpoint's
  own runner (they are never moved to the gateway): `/check:status` lists
  them, `/check:stop <job>` stops their whole process group, and their logs
  stay in `jobs/`.
- **The runtime** moves out of the plugin folder into Claude Code's data
  folder for the plugin (built once, in the background).
- **What behaves differently:** a pause can be immediate (`/check-pause`);
  only messages you type resume a paused session (with mods); every resume
  reconciles first; interrupted operations are classified instead of "redo
  the stage from the start"; with the gateway installed, jobs run there.

**From 0.3 through 0.7.2**: the state, journals (since 0.4), checkpoints and
job records carry over as they are; jobs recorded before the gateway
integration (0.6) stay in the own runner.

## Recovery procedures

- **After a crash or kill** (no clean end): start Claude Code in the
  project. The session start lists the interrupted task with its original
  session's `claude --resume <id>`. Prefer that (its conversation comes
  back, and so does the reconciliation); or type `/check:resume last` (or
  the task id) in the new session to continue it there. Either way, do the
  "Check first" items before anything else.
- **Paused, then restarted:** the pause survives (status line, flag). Type a
  message (or `/check:resume`, or `/check-resume`) when you want it to go on;
  nothing else resumes it.
- **Back to an earlier point:** `/check:list`, `/check:diff <cp>` to see what
  changed since, then `/check:resume <cp>` to continue from there (files are
  not rolled back; use git or `/rewind` for that).
- **Abandon a task:** `/check:cancel` (`detach` keeps its jobs running) or,
  when it is done, `/check:complete`. The history stays until retention;
  `/check:resume <task>` reopens it.
- **Hand it over:** `/check:export` writes a sanitized bundle (no secrets, no
  file contents; moving credentials is the gateway's encrypted export).
- **No session is free:** `~/.local/state/checkpoint/checkpoint pause all`
  (or `status`, `resume all`) from any terminal.
- **"recording FAILED"** on the status line (a full disk, an unwritable state
  folder): free the space; recording resumes by itself, and the gap is
  recorded and named in the summary, `/check:status` and `/check:doctor`.
- **The setup failed or seems stuck:** `/check:doctor` shows pixi's error
  line; it is retried at every session start.

## Limits

Known limitations, each with the smallest remedy.

- The desktop app wasn't tested (the immediate controls, the footer label,
  the status line and the Checkpoints pane run there through the same mod). Remedy: use the
  terminal for now, or try a pause and a resume there before relying on it.
- Without mods (Claude Code older than 2.1.287 in the terminal or 2.1.286 in
  the desktop app, or mods switched off) there is no immediate pause: a pause
  takes effect at the next tool call, and a long model reply or command runs
  on until then (`pause now` still stops a running command within ~2 s); and
  any prompt resumes a paused session (the classic hooks can't tell a
  scheduled or automated prompt from yours). Remedy: update Claude Code.
- `/check:pause` typed while Claude is busy waits until the turn ends (Claude
  Code 2.1.301 hides queued slash commands). Remedy: `/check-pause`, or type
  `pause`.
- An immediate pause leaves a running Bash command running unless you add
  `now` (it says so); `now` needs the runtime (until it's built, the command
  keeps running and the acknowledgement says that). Remedy: `/check-pause
  now`.
- The journal's scrubbing works on patterns: a secret with no recognizable
  shape and no telling name next to it (a bare random string in a prompt) is
  not caught. Remedy: don't paste secrets into prompts; with the gateway,
  secrets are typed only in its dialog and never reach the journal.
- The journal is only as current as the last Python moment: a killed
  session's last events wait in its spool and are journaled by the next
  session start (nothing is lost). A test's HEAD is read before it runs; its
  file hashes a moment later (~100-250 ms), so a file the command changes
  first can slip in (the summary says how late, and calls the version
  uncertain when HEAD moved in between).
- On a full disk, events the hooks can't spool are lost (said once, "recording
  FAILED" on the status line); the gap is recorded once space is back. A
  journal write that fails loses nothing. Remedy: free space.
- With the gateway installed but its plugin off in a session (its daemon
  still running from another one), the daemon doesn't serve that session:
  `/check:status` says so, and new jobs run in checkpoint's own runner.
  Remedy: enable the gateway in that session if you want its supervision.
- Jobs in checkpoint's own runner (no gateway): no per-job pause policy (a
  pause leaves them running; `--stop-jobs` stops them), no terminal or input,
  no remote hosts, nothing survives a reboot. Remedy: install the gateway.
- Gateway jobs: CONTRACT-v1 has no call to release a job from its task, so
  `/check:cancel detach` releases it in checkpoint's records only. The
  gateway pauses by session, so the task's jobs another session started are
  paused only once that session has ended (while it is open they are named as
  not paused). With the gateway gone, checkpoint can't stop or observe its
  jobs: they stay "outcome unknown, it may still run". Remedy: check them in
  the gateway (`/gate-terminal`, `job_list`) when it is back.
- A command or subagent that went on in the background is recorded as
  started; its result isn't observed unless Claude Code reports its end
  later, so the summary keeps it under "In flight". Remedy: run long work as
  a job (`job_start`), whose state checkpoint can ask for.
- Reconciliation reads; it never restores anything. Remedy: Claude Code's
  `/rewind` or git to roll files back.
- Whether a call may have changed something outside the repository is judged
  from its command (a push, ssh/scp/rsync, an HTTP write, kubectl, helm,
  terraform, a deploy or publish command, a database client, sudo) and MCP
  tool names; a command that does so by other means is still "outcome
  unknown", with "check its effects" instead of a specific hint. Remedy:
  check before running it again, as the hint says.
- A process counts as still running a call only when the process table shows
  a shell with exactly that command, started after the call; without a
  process table nothing is called running or gone for sure. Only
  checkpoint's and the gateway's jobs can be stopped by checkpoint. Remedy:
  stop a Claude Code background shell from its shell list.
- Authorization references: without mods only permission dialogs, auto
  mode's denials and the permission mode are recorded; settings rules are
  compared with the user, project and project-local settings files only (not
  managed settings or `--allowedTools`); accounts are known only from the
  gateway tools' `account` argument. Remedy: Claude Code 2.1.287+ for the
  verdicts; a rule from managed settings is then said as "not found", never
  as holding.
- Host states are the gateway's last contact with the host; a host key change
  stays a reason to stop until the gateway reports the host confirmed.
- Artifacts and output files other than changed files aren't recorded as
  references. Remedy: note them with `checkpoint_note`.
- A plugin can't send messages into other sessions, so after `resume all`
  each session needs a message to continue. Remedy: type one in each.
- Jobs source the gateway's env file only if it is a regular file (not a
  symlink) that you own and nobody else can write, in a folder you own that
  nobody else can write; otherwise it's `refused` and the job runs without
  it. A file that doesn't source cleanly isn't applied at all (`failed`),
  and the job runs anyway, since checkpoint fails open. Remedy: `chmod go-w`
  the file and its folder (the gateway writes it so).
- Checks other plugins make on Bash commands apply to a job only if their
  hooks match `job_start` (the gateway's and pixi-guard's do), and only to
  its command, once, when it starts. The gateway masks what the job tools
  show Claude, not an own-runner job's log file on disk, and not a failed
  `job_stop`'s error text (the job's last log lines): Claude Code passes an
  error result to no hook that can rewrite it, so the gateway only warns.
- A job still running after its Claude session ends can no longer use the
  gateway's keys, passwords or git credentials, except from entries with
  `scripts = true`, because the gateway can't tell it came from Claude any
  more; keep the session open until such a job is done.
- Untested systems: Intel Macs, Linux x86-64, WSL 2; native Windows
  unsupported. Remedy: WSL 2 on Windows.
