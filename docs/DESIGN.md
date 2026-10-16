# agent-pause: design

Status: **design only, nothing built yet** (2026-10-15). Written from a planning
conversation with the user; decisions below are theirs unless marked
"recommended".

## The problem

The user often has several Claude Code agents running on a laptop and needs to
pick the laptop up and leave. Today both options break the work:

- **Stop / Esc** cuts an agent off mid-step: a command half run, a file half
  edited, a subagent dropped.
- **Closing the lid or the shell** breaks the in-flight model request and kills
  running commands. On resume the transcript is there, but nothing in it says
  what state the work was left in.

The transcript itself mostly survives. What's missing is (a) a **clean
stopping point** and (b) a **record of where the work stands**.

## The idea

A **pause** that interrupts nothing: each agent finishes its current step, and
at its next tool call it brings its notes up to date and stops. Resume picks up
from those notes, in the same session (best) or a brand-new one (nearly as
good, because the notes are written to stand alone).

Three parts, in order of importance:

1. **Tracker**: an always-kept progress document per task. The foundation.
2. **Jobs**: long work runs detached, so it survives pauses and sessions.
3. **Pause / resume**: slash commands and hooks that bring every agent to a
   clean stop and back.

A real pause button next to Stop in the app isn't possible (plugins can't
change the app's UI), so slash commands are the trigger.

## 1. Tracker (always on)

For any task with more than a few steps, the agent keeps:

```
<project>/.claude/work/<task-slug>/
  TRACKER.md          the source of truth (template below)
  <stage>.md ...      optional detail docs, linked from TRACKER.md
  checkpoint.json     facts written by the hook at pause time
```

Decided: this location is fine (committable, or gitignored per project).

`TRACKER.md` template:

```markdown
# <Task title>
Goal: <one or two sentences; what "finished" means>
Started: <date>  Session: <session id>  Updated: <timestamp>

## Stages
| # | Stage | Status | Notes / details |
|---|-------|--------|-----------------|
| 1 | ...   | accepted | [details](stage-1.md) |
| 2 | ...   | done     | waiting for the user to look |
| 3 | ...   | in progress | |
| 4 | ...   | todo     | |

Status: todo | in progress | done (agent thinks it's finished) |
accepted (the user confirmed it in chat) | interrupted (redo from start of stage)

## Now
Current step: <exactly what is being done>
Next action: <the very next concrete thing to do>

## Jobs
| Job | Command | Started | State | Redo / resume |
|-----|---------|---------|-------|---------------|

## Resume notes
- Check first: <things that may have changed or died while paused>
- Assumptions: <anything a new session wouldn't know>
```

Rules given to the agent (SessionStart note):

- Create the tracker as soon as the task clearly has several steps; keep
  **Now** current; update a stage's status when it changes.
- Mark a stage **accepted** only when the user says so in chat. Decided: no
  `/accept` command; chat is enough.
- Put long detail in linked stage docs, not in the tracker.

How it's encouraged (hooks):

- **PostToolUse** counts tool calls per session; after N calls (~25,
  tunable) without a write to the tracker, it adds a short reminder as
  `additionalContext`.
- **Stop**: if the session has a tracker with a stage `in progress` and the
  tracker wasn't touched this turn, block the stop once with "update the
  tracker's Now section" (respect `stop_hook_active` to avoid loops).

Because the tracker is kept current all along, pausing only has to top it up.

## 2. Jobs (long work that survives)

An MCP server (stdio, Python stdlib, launched through pixi like net-gateway's)
with:

| Tool | Does |
|---|---|
| `job_start(name, argv or shell cmd, cwd, redo_hint)` | Runs it detached in its own process group (`start_new_session`), stdout+stderr to a log, exit code captured by a small wrapper. Records command, cwd, PID/PGID, log path, start time, session id, task slug, and **how to redo or resume it** (from scratch, or a resume flag/checkpoint the agent knows). |
| `job_status(name?)` | State (running / exited N / killed / vanished), runtime, exit code. |
| `job_tail(name, lines)` | End of the log. |
| `job_stop(name, reason)` | SIGTERM the group, then SIGKILL after a grace period; records why and the last log lines. |

- Registry: `${XDG_STATE_HOME:-~/.local/state}/agent-pause/jobs.json`
  (flock'd), plus logs under `.../jobs/<id>/`.
- The SessionStart note says: anything likely to run more than ~2 minutes
  goes through `job_start`, and gets a row in the tracker's Jobs table.
- Laptop sleep freezes jobs and they usually continue on wake; jobs holding
  network connections may fail. That's what `redo_hint` is for.

## 3. Pause and resume

### Commands

| Command | Effect |
|---|---|
| `/agent-pause:pause` | Pause this session at its next tool call. |
| `/agent-pause:pause all` | Pause every session on the machine. |
| `/agent-pause:pause now` | Also kill the session's running **foreground** command right away (see below). |
| `/agent-pause:pause now --stop-jobs` | ...and stop its detached jobs too. |
| `/agent-pause:resume` | Resume this session (see below). |
| `/agent-pause:resume all` | Clear the global flag. |
| `/agent-pause:resume <task>` | In a **new** session: pick up a task from its tracker alone. |
| `/agent-pause:status` | Which sessions are paused / working, their tasks, jobs. |

Recommended (user deferred to us): `pause now` leaves **detached jobs running**
by default, since surviving pauses is the whole point of jobs; `--stop-jobs`
stops them.

Probably also a terminal command (`pixi run pause all`) for when no session
is idle to type into (see Spike 1).

### State

`${XDG_STATE_HOME:-~/.local/state}/agent-pause/`:

```
paused-all                 global flag (exists = paused)
sessions/<session_id>.json  {cwd, claude_pid, task, paused, paused_at, checkpoint}
jobs.json, jobs/<id>/      job registry and logs
```

SessionStart records the session (its `session_id`, `cwd`, and the Claude
process PID from the hook's parent) so `pause all`, `pause now` and `status`
can find it.

### Pause flow

1. The flag is set (session file `paused: true`, or `paused-all`).
2. **PreToolUse (matcher `*`)** on every tool call. This runs constantly, so a
   **shell pre-filter** checks only whether a flag file exists and exits
   immediately if not (no Python startup; same trick as net-gateway's
   `prompt.sh`).
3. When paused, it **denies** the call with a reason like: *"Paused by the
   user. Update TRACKER.md (Now, Jobs, Resume notes); you may use the job
   tools; then end your turn with one line."* Allowed through: Write/Edit to
   `.claude/work/**`, and `job_*` tools. Everything else is denied.
4. After several denied calls (agent not cooperating), return
   `{"continue": false, "stopReason": "Paused"}` to end the turn outright.
5. **Stop** hook (when paused): writes `checkpoint.json` with plain facts:
   last ~10 tool calls (from the transcript), `git status` + diff stat,
   the session's jobs and their states, time paused. Lets the stop happen.
6. Subagents: PreToolUse fires for their tool calls too, so they stop at
   their next step; the parent then hits the gate itself. (Verify: Spike 2.)

### `pause now`

The `/pause now` prompt hook itself (not the agent) finds the session's
running foreground command, i.e. the shell children of the recorded Claude
PID, and records the command, then SIGTERM/SIGKILLs it. It marks the current
stage `interrupted: redo from start of stage` in the checkpoint. The agent
then sees its tool fail, hits the gate on its next call, and pauses as above.

### Resume flow

- Any user prompt in a paused session resumes it (the user being back is the
  signal); `/resume` is the explicit form. The UserPromptSubmit hook clears the
  session's flag and adds `additionalContext` with:
  - the tracker path and its Now / Resume notes sections,
  - `checkpoint.json`,
  - **what changed since the pause**: jobs that finished or died (with exit
    codes and last log lines), files changed since the git snapshot, time
    elapsed.
  - instruction: verify the "Check first" items, then continue from **Next
    action**.
- `/resume all` clears `paused-all`. A plugin can't send prompts into other
  sessions, so each paused session still needs a message (any message works).
- `/resume <task>` in a new session: same injection, from the tracker and
  checkpoint only. The tracker is written to make this nearly as good as
  resuming in place.

## Spikes before building (do these first)

1. **Queued slash commands.** When the user types `/agent-pause:pause` into a
   session that is busy, does the UserPromptSubmit hook run at the next tool
   boundary, or only after the turn ends? If only after, per-session pause
   must be triggered from elsewhere: an idle session (`/pause <session>`,
   `/pause all`) or the terminal command. **The whole UX hinges on this.**
   Test in both the desktop app and the CLI.
2. **Subagents.** Does PreToolUse fire for subagent tool calls, and does a
   deny there behave sensibly for the parent? Same for workflows.
3. **`continue: false`** from PreToolUse/PostToolUse: does it end the turn
   cleanly in the desktop app, and how does the session look afterwards?
4. **Killing the foreground command**: can the Claude PID be found reliably
   from the hook (parent chain), and how does the Bash tool report a killed
   child? Background Bash (`run_in_background`) too.
5. **Keybinding** (CLI only): can a key be bound to submit
   `/agent-pause:pause`? Nice to have.

Build a throwaway prototype for 1-3 (a flag file plus a deny hook is enough)
before writing the real thing.

## Build order (after the spikes)

1. Plugin skeleton: marketplace, plugin.json, pixi.toml (Python 3.12 only),
   `run.py`, test script. Mirror net-gateway's layout.
2. Tracker convention: SessionStart note, template, PostToolUse nudge, Stop
   nudge.
3. Pause/resume: flags, PreToolUse gate with shell pre-filter, Stop
   checkpoint, prompt hook for the commands, resume injection.
4. Jobs MCP server.
5. `pause now` (process finding and killing).
6. `status`, `install.py` (mirror net-gateway's), README, `/agent-pause:help`.

## Constraints

- Requirements: **Claude Code and pixi only**; Python stdlib only; macOS and
  Linux (desktop and headless). Same as net-gateway.
- PreToolUse runs on every tool call: keep the not-paused path to a file
  existence check in shell.
- Hooks must **fail open** for the gate (a broken hook must not freeze every
  session), unlike net-gateway's prompt hook.
- Never delete or rewrite a user's tracker content; only the agent edits it,
  and hooks only read it (and write `checkpoint.json`).
- Coexists with net-gateway (both have PreToolUse hooks; order doesn't
  matter, any deny wins).
