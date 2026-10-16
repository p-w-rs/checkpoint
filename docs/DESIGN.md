# agent-pause: design

Status: **design only, nothing built yet** (2026-10-15). Spikes run
2026-10-15 on Claude Code 2.1.277 (CLI) / 2.1.295 (desktop); see the Spikes section. Written from a planning
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
change the app's UI). The trigger in a **busy** session is a plain-text
message `pause` (Spike 1: slash commands typed mid-turn wait until the turn
ends, plain text is delivered at the next tool boundary). Slash commands
cover idle sessions, `all`, `status` and resume.

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
- Built-in `run_in_background` Bash is not a substitute: it dies with the
  Claude process (Spike 4).

## 3. Pause and resume

### Commands

| Command | Effect |
|---|---|
| `pause` (plain message, typed while busy) | Pause this session at its next tool call. Also `pause now`, `pause all`. Matched exactly (whole message, case-insensitive, optional `.`/`!`), so ordinary sentences never trigger it. |
| `/agent-pause:pause` | Same, for an idle session (or as a queued command that applies when the turn ends). |
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

A terminal command (`pixi run pause all`) is still worth having for headless
or remote sessions.

All agent-pause commands set `disable-model-invocation: true`: in Spike 2 a
paused subagent tried to call the resume command through the Skill tool to
unpause itself.

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
   `.claude/work/**`, and `job_*` tools. Everything else is denied,
   including Skill calls to agent-pause's own commands. The model sees the
   reason as `PreToolUse:<Tool> hook error: <reason>` and, in the spikes,
   ended its turn on the first deny.
4. After several denied calls (agent not cooperating), return **deny plus**
   `{"continue": false, "stopReason": "Paused"}`. Spike 3: `continue: false`
   on its own does **not** block the tool (the call runs, then the turn
   stops), so the deny must always be in the same output.
5. **Checkpoint**: `checkpoint.json` holds plain facts: last ~10 tool calls
   (from the transcript), `git status` + diff stat, the session's jobs and
   their states, time paused. The gate writes it on the first deny, and
   the **Stop** hook refreshes it if Stop fires. Spike 3: when
   `continue: false` ends the turn, **Stop does not fire**, so Stop can't be
   the only writer.
6. Subagents (Spike 2): PreToolUse fires for their tool calls with the
   parent's `session_id` plus `agent_id`/`agent_type`, so the per-session
   flag covers them. Subagents run in the background by default, so the
   parent's turn may already be over; a paused subagent ends, and its
   result comes back to the parent as a `<task-notification>` prompt. A
   `continue: false` inside a subagent ends only that subagent; the parent
   carries on until its own next tool call hits the gate.

### `pause now`

The `pause now` prompt hook itself (not the agent) finds the session's
running foreground command and records it, then SIGTERM/SIGKILLs it. Spike 4
showed how:

- The hook's parent process **is** the Claude process (no shell between),
  in both the CLI and the desktop app (`.../claude.app/Contents/MacOS/claude`).
- Every Bash tool call, foreground or `run_in_background`, is a child
  `bash -c source <snapshot> ... && eval '<command>' ...` in its own process
  group, and the user's command inside gets **another** process group. So
  kill the whole descendant tree (each pgid), not one group.
- `ps` can't tell foreground from background: both write to a
  `tasks/<id>.output` file. The gate's own log can: the foreground command is
  the Bash call without `run_in_background` whose `tool_use_id` has no
  PostToolUse yet; match its `eval '<command>'` in the shell's argv.

It It marks the current
stage `interrupted: redo from start of stage` in the checkpoint. The agent
then sees its tool fail, hits the gate on its next call, and pauses as above.

### Resume flow

- Any user prompt in a paused session resumes it (the user being back is the
  signal); `/resume` is the explicit form. Not every UserPromptSubmit is the
  user, though: background-task results arrive as prompts starting with
  `<task-notification>` (Spike 2), and the pause keywords themselves arrive
  as prompts. Those must not resume. The UserPromptSubmit hook clears the
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

## Spikes (run 2026-10-15)

Prototype in [spike/](../spike): `hook.py` logs every hook event to
`spike/state/events.jsonl` and gates tools on a flag file; `testproj/` loads
it through a project-local `.claude/settings.json` (user settings untouched);
`drive_sdk.py` drives `claude -p --input-format stream-json`, the same input
path the desktop app uses (its `claude` process runs with
`--input-format stream-json --replay-user-messages`). Runs used Haiku.

Items marked **(manual)** need a person typing into the desktop app or an
interactive terminal: the interactive CLI can't start inside an agent's
sandbox (it drops into first-run login). Steps are in
[spike/MANUAL.md](../spike/MANUAL.md).

### 1. Typing into a busy session

Stream-json path (`state/s1b`, `s1c`, `s1d`): the second message was sent
3 s into a 6-call loop of `./tick.sh 5`.

| Sent mid-turn | UserPromptSubmit fired | Effect |
|---|---|---|
| `/spike-pause` | 18:53:17.0, **after** the turn's Stop (16.9) and after all 6 calls | too late to pause anything |
| plain text | 18:52:41.4, right after the first PostToolUse (41.2) | delivered at the next tool boundary |
| `pause` (hook matches it) | 18:54:02.8, after the first PostToolUse | flag set; the next PreToolUse was denied (04.8); the agent replied one line and stopped (Stop 09.4) |

**Finding:** slash commands wait for the turn to end; plain messages are
injected at the next tool boundary and run UserPromptSubmit then. **Design
change:** the mid-turn trigger is the plain message `pause` (exact match),
and slash commands stay for idle sessions, `all`, `status` and resume. The
desktop app also surfaces mid-turn messages "within the running turn,
alongside the next tool result" (seen in this very session).

**(manual)** Confirm in the desktop app UI and the interactive CLI; in the
CLI also try `chat:sendNow` (ctrl+enter) with `/spike-pause`, in case
send-now delivers a slash command mid-turn.

### 2. Subagents

`state/s2a`: flag set during a subagent's first Bash call.

- PreToolUse fires for subagent calls with the **parent's** `session_id`
  plus `agent_id` and `agent_type` (`general-purpose`). SubagentStart and
  SubagentStop fire too.
- The Agent tool returned at once (`"status": "async_launched"`): subagents
  run in the background by default. The parent's turn ended (Stop) while the
  subagent was still working.
- The subagent's next Bash call was denied. It then tried the **Skill tool
  with `spike-resume`** to unpause itself (also denied), then reported
  "I am paused". Its result came back to the parent as a new
  UserPromptSubmit whose prompt starts with `<task-notification>`.
- Workflows: not tested (the Workflow tool needs the user's explicit
  opt-in). They run subagents, so they're presumably the same; check during
  the build.

**Design changes:** commands get `disable-model-invocation: true` and the
gate denies Skill calls to them; resume-on-any-prompt must ignore
`<task-notification>` prompts.

### 3. `continue: false`

`state/s3a`-`s3d`:

- `continue: false` **alone** from PreToolUse: the tool **still ran**
  (PostToolUse with normal output), then the turn stopped. Headless result:
  `stop_reason: "tool_use"`, empty result text, no Stop hook.
- **deny + `continue: false`** in one output: the tool was blocked, the
  model saw `PreToolUse:Bash hook error: <reason>`, the turn ended at once,
  with no Stop hook.
- Inside a subagent, either form ends only the **subagent**; the parent got
  the hand-back and kept going until its own next call was denied, then
  ended normally ("I am paused", Stop fired).
- A plain deny was enough in every run: the model ended its turn on the
  first deny.

**Design changes:** always deny together with `continue: false`; write the
checkpoint from the gate, because Stop doesn't fire.
**(manual)** How a `continue: false` stop looks in the desktop app, and
whether the session takes the next message normally.

### 4. Finding and killing the foreground command

`state/s4c`: `./tick.sh 120` in the background, then `./tick.sh 45` in the
foreground.

- The hook's parent PID is the Claude process itself (CLI: `claude -p ...`;
  desktop: `.../claude.app/Contents/MacOS/claude`).
- Each Bash call is a child `bash -c source <snapshot> ... && eval
  '<cmd>' < /dev/null && pwd -P >| /tmp/claude-XXXX-cwd`, which leads its
  own process group; `tick.sh` inside it leads **another** group.
- Foreground and background shells look the same (both stdout/stderr to
  `tasks/<id>.output`); only the command text and our own PreToolUse log
  tell them apart.
- SIGTERM to the foreground groups: the tool call ended at once as
  **PostToolUseFailure**, with error `Exit code 144` followed by the output
  so far. The model reported it plainly. The background task kept running.
- When `claude -p` exited, its `run_in_background` task was killed too:
  built-in background Bash does **not** outlive the session, which
  confirms the need for the Jobs server (`start_new_session`).
- Side finding: Claude Code itself blocks foreground `sleep N && ...`
  commands (before PreToolUse runs), so tests need a script like
  `tick.sh` rather than `sleep`.

### 5. Keybinding

Not possible: `keybindings.json` maps keys only to built-in actions
(`chat:submit`, `chat:queueSubmit`, `chat:sendNow`, ...), and none of them
submits text. Typing `pause` + Enter is short enough.

### Other notes

- Hooks from an untrusted folder's `.claude/settings.json` still ran under
  `claude -p`, but its `permissions.allow` was ignored ("workspace has not
  been trusted"). Trust is keyed on the git root.

## Build order (after the spikes)

1. Plugin skeleton: marketplace, plugin.json, pixi.toml (Python 3.12 only),
   `run.py`, test script. Mirror net-gateway's layout.
2. Tracker convention: SessionStart note, template, PostToolUse nudge, Stop
   nudge.
3. Pause/resume: flags, PreToolUse gate with shell pre-filter (deny +
   `continue: false`), checkpoint from the gate and Stop, prompt hook for
   the plain `pause` keyword and the commands, resume injection.
4. Jobs MCP server.
5. `pause now` (process finding and killing).
6. `status`, `install.py` (mirror net-gateway's), README, `/agent-pause:help`.

## Constraints

- Requirements: **Claude Code and pixi only**, self-contained like
  net-gateway: pixi.toml pins Python 3.12, every hook and the MCP server run
  through `pixi run --manifest-path ${CLAUDE_PLUGIN_ROOT}/pixi.toml --frozen`,
  and the only thing outside pixi is the POSIX `sh` pre-filter. No system
  Python, jq or other tools. Python stdlib only; macOS and Linux (desktop and
  headless).
- PreToolUse runs on every tool call: keep the not-paused path to a file
  existence check in shell.
- Hooks must **fail open** for the gate (a broken hook must not freeze every
  session), unlike net-gateway's prompt hook.
- Never delete or rewrite a user's tracker content; only the agent edits it,
  and hooks only read it (and write `checkpoint.json`).
- Coexists with net-gateway (both have PreToolUse hooks; order doesn't
  matter, any deny wins).
