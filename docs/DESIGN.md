# checkpoint: design

Status: **built** (v0.1.0, 2026-10-15; v0.2.0, 2026-10-18: see "0.2.0
revisions" at the end); see
[ARCHITECTURE.md](ARCHITECTURE.md). Since 0.3.0 [PLAN-v4.md](PLAN-v4.md)
supersedes this document where they differ (it lists each reversal): a
native immediate pause exists (`/check-pause`), the hook-written journal is
the record (the tracker stays as optional notes), the paused gate fails
closed, only prompts the person typed resume, interrupted operations are
classified instead of redone, and jobs go to the gateway when it is there. Spikes run 2026-10-15 on Claude Code
2.1.277 (CLI) / 2.1.295 (desktop); see the Spikes section. Written from a
planning conversation with the user; decisions below are theirs unless
marked "recommended".

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
change the app's UI). Slash commands are the trigger, busy session or not.
A command typed into a busy session waits in Claude Code's queue until the
turn ends, but the queue is written to the transcript at once, so the gate
sees it at the next tool call (Spike 1).

## 1. Tracker (always on)

For any task with more than a few steps, the agent keeps:

```
<project>/.work/<task-slug>/
  TRACKER.md          the source of truth (template below)
  <stage>.md ...      optional detail docs, linked from TRACKER.md
  checkpoint.json     facts written by the hook at pause time
```

Decided: this location (committable, or gitignored per project). First
planned as `.claude/work/`, but Claude Code treats every path under
`.claude/` as sensitive and asks before each write; neither an allow rule
(`Write(.claude/work/**)`) nor a PreToolUse `allow` from a hook lifts that
(checked live). So the user chose `.work/`; `CHECKPOINT_WORK_DIR` changes it.

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

An MCP server (stdio, Python stdlib, launched through pixi like gateway's)
with:

| Tool | Does |
|---|---|
| `job_start(name, argv or shell cmd, cwd, redo_hint)` | Runs it detached in its own process group (`start_new_session`), stdout+stderr to a log, exit code captured by a small wrapper. Records command, cwd, PID/PGID, log path, start time, session id, task slug, and **how to redo or resume it** (from scratch, or a resume flag/checkpoint the agent knows). |
| `job_status(name?)` | State (running / exited N / killed / vanished), runtime, exit code. |
| `job_tail(name, lines)` | End of the log. |
| `job_stop(name, reason)` | SIGTERM the group, then SIGKILL after a grace period; records why and the last log lines. |

- Registry: `${XDG_STATE_HOME:-~/.local/state}/checkpoint/jobs.json`
  (flock'd), plus logs under `.../jobs/<id>/`.
- The SessionStart note says: anything likely to run more than ~2 minutes
  goes through `job_start`, and gets a row in the tracker's Jobs table.
- Laptop sleep freezes jobs and they usually continue on wake; jobs holding
  network connections may fail. That's what `redo_hint` is for.
- Built-in `run_in_background` Bash is not a substitute: it dies with the
  Claude process (Spike 4).
- Jobs run with the session's environment, not the MCP server's: the server
  runs under `pixi run` of checkpoint's own manifest, so `job_start` drops that
  activation (`PIXI_PROJECT_*`, `PIXI_ENVIRONMENT_*`, `PIXI_IN_SHELL`,
  `CONDA_PREFIX` etc., our `.pixi/envs/*/bin` on PATH). Otherwise a job's own
  `pixi run` uses checkpoint's pixi.toml (`PIXI_PROJECT_MANIFEST` wins over the
  cwd). Another project's activation (a session under `pixi shell`) is kept.
- **Jobs follow the gateway** when it is installed (user request,
  2026-10-19). The gateway gives Claude's Bash its environment through
  `CLAUDE_ENV_FILE` (stand-ins first on PATH, a git include,
  `GIT_SSH_COMMAND`/`RSYNC_RSH`, the user's SSH agent and tokens unset),
  which a job started from the MCP server never got. The gateway writes
  the same text to `~/.config/gateway/env.sh` at every session start, so
  the job's shell sources that file before the command (command and argv
  jobs alike, after `job_env()`). Found the way the gateway finds it: the
  password database's home, not `$HOME`; `CHECKPOINT_GATEWAY_ENV`
  overrides it (tests; empty = none). No file: nothing changes, so
  checkpoint still works alone.
  - Sourced only if it is a regular file (not a symlink) the user owns
    and nobody else can write, in a folder the user owns and nobody else
    can write, so it can't be swapped between the check and the job's
    shell. Otherwise `refused: <why>`, and the job runs without it.
  - A broken file fails open, like the rest of checkpoint: one line in
    the job's log and the job runs without it (`failed`). Failing closed
    would turn a gateway problem into lost jobs, and without this feature
    jobs never had the gateway's environment anyway. `. file || echo`
    doesn't do this: dash (Debian's sh) exits on a sourced file's syntax
    error, failing special builtin or missing file, so the job never ran,
    and bash goes on with the lines before the error applied. So the
    wrapper sources it in a subshell first, and applies it only if that
    survived and printed nothing on stderr: all of the file or none.
  - The registry's `gateway_env` (shown by `job_status` and status):
    `applied`, `failed`, `refused: <why>`, `pending` until the shell
    reports through `jobs/<id>/gateway_env`, `unknown` if it ended first;
    absent when there is no file.
  - Other plugins check jobs too (2026-10-19). checkpoint never allows
    `job_start` (CLAUDE.md invariant 7), so other plugins' PreToolUse
    hooks see it. The gateway judges a job's command like a Bash command:
    it refuses `curl`/`wget`, its private folder and session transcripts,
    rights set to `false` and any `"ask"` right (nobody can be asked while
    a job runs), and never approves a job by itself. pixi-guard refuses
    Python outside pixi. The gateway's PostToolUse hook masks what
    `job_tail`, `job_status` and `job_stop` return; a failed `job_stop`
    (an MCP error result with the job's last log lines) reaches no hook
    that can rewrite it, so its PostToolUseFailure hook only warns. Both
    match the tools by exact name (`mcp__plugin_check_jobs__job_start`
    ...), so the plugin name `check`, the server name `jobs` and the tool
    names are an interface (a test pins them). A job still running after
    its Claude session ends can no longer use the gateway's keys,
    passwords or git credentials, except from entries with
    `scripts = true`, because the gateway can't tell it came from Claude
    any more; keep the session open until such a job is done. See the
    gateway README's "With checkpoint" and pixi-guard's README
    ("Checkpoint jobs too").

## 3. Pause and resume

### Commands

| Command | Effect |
|---|---|
| `/check:pause` | Pause this session at its next tool call (typed while busy; see "Queued commands" below), or right away if idle. |
| `/check:pause all` | Pause every running session on the machine (a flag per session; sessions started later aren't paused). |
| `/check:pause now` | Also kill the session's running **foreground** command right away (see below). |
| `/check:pause now --stop-jobs` | ...and stop its detached jobs too. |
| `/check:resume` | Resume this session (see below). |
| `/check:resume all` | Un-pause every session; each gets its resume notes with its next message. |
| `/check:resume <task>` | In a **new** session: pick up a task from its tracker alone. |
| `/check:status` | Which sessions are paused / working, their tasks, jobs. |

Recommended (user deferred to us): `pause now` leaves **detached jobs running**
by default, since surviving pauses is the whole point of jobs; `--stop-jobs`
stops them.

A terminal command (`pixi run pause all`) is still worth having for headless
or remote sessions.

Fallback (recommended, cheap): the plain message `pause` (also `pause now`,
`pause all`; whole message, case-insensitive) does the same through
UserPromptSubmit, which runs at the next tool boundary for plain text. It
relies only on documented hook behaviour, so it keeps working if the
queue format below changes.

#### Queued commands

When a session is busy, Claude Code appends
`{"type":"queue-operation","operation":"enqueue","content":"/check:pause",...}`
to the session transcript the moment the user presses Enter, then
`dequeue` when the turn ends and the command runs (`remove` when a plain
message gets injected mid-turn instead). PreToolUse gets
`transcript_path`, so the gate:

1. pre-filter (shell): flag file exists, **or** the last ~256 KB of the
   transcript contains `/check:pause`; else exit;
2. replays the queue ops in that tail (enqueue appends, dequeue pops the
   front, remove drops by content); every pending pause command newer than
   the last one handled (timestamp kept in the session's state file) is
   applied, oldest first, with its `now`/`all`/session options (the user
   may type several, or retype one, before the next tool call), then it
   denies;
3. the queued command still runs as a prompt of its own: when the turn
   ends (`dequeue`), or, for plain text absorbed into the turn, at once
   (in 2.1.301 its UserPromptSubmit fires about 7 ms before the `remove`
   record is written). UserPromptSubmit recognizes the command the gate
   already applied and doesn't apply it a second time (so `pause all`
   doesn't re-pause sessions resumed meanwhile); the command's prompt just
   has the agent confirm in one line. The gate remembers the command
   before applying it (with `now`, Claude Code can run an absorbed echo as
   soon as the killed call ends, while other commands are still being
   stopped), and remembers each queued command on its own, so two in one
   turn are both recognized, even when typed before the same tool call. Each memory ends with its echo, or with
   another prompt once its entry is no longer queued, so a lost echo
   (interrupt, restart) can't swallow the same command typed later.

The queue entries are an undocumented transcript format, so the tests pin
it with a fixture and the fallback keyword stays.

All checkpoint commands set `disable-model-invocation: true`: in Spike 2 a
paused subagent tried to call the resume command through the Skill tool to
unpause itself.

### State

`${XDG_STATE_HOME:-~/.local/state}/checkpoint/`:

```
flags/<session_id>         exists = paused (one per session, also for `pause all`)
sessions/<session_id>.json  {cwd, claude_pid, task, paused, paused_ts, checkpoint}
jobs.json, jobs/<id>/      job registry and logs
```

SessionStart records the session (its `session_id`, `cwd`, and the Claude
process PID from the hook's parent) so `pause all`, `pause now` and `status`
can find it.

### Pause flow

1. The flag is set (`flags/<sid>`; `pause all` sets one per running session).
2. **PreToolUse (matcher `*`)** on every tool call. This runs constantly, so a
   **shell pre-filter** checks only for a flag file or a queued pause
   command in the transcript tail, and exits immediately if neither (no
   Python startup; same trick as gateway's `prompt.sh`).
3. When paused, it **denies** the call with a reason like: *"Paused by the
   user. Update TRACKER.md (Now, Jobs, Resume notes); you may use the job
   tools; then end your turn with one line."* Allowed through: Write/Edit to
   `.work/**`, Read/Glob/Grep, and the job status/tail/stop tools. Everything else is denied,
   including Skill calls to checkpoint's own commands. The model sees the
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

Nothing in the session runs while a foreground command does: a command
typed meanwhile is only queued, and plain text waits for the tool boundary.
So each foreground Bash call gets a small **watcher** (`hooks/watch.sh`,
started by the PreToolUse pre-filter, gone when PostToolUse fires). Every
2 s it looks for a queued `pause now` in the transcript, or a request file
from `pause <session> now` / `pause all now`, and then Python pauses the
session and kills the command (live: killed ~2 s after the user typed it,
reported to the agent as `Exit code 144`). Spike 4 showed how to find it:

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

The killed command goes into the checkpoint's `interrupted` list, and the
resume context tells the agent to mark that stage `interrupted` and redo it
from the start. The agent sees its tool fail, hits the gate on its next
call, and pauses as above.

### Resume flow

- Any user prompt in a paused session resumes it (the user being back is the
  signal); `/resume` is the explicit form. Not every UserPromptSubmit is the
  user, though: background-task results arrive as prompts starting with
  `<task-notification>` (Spike 2), and the pause commands and keywords
  themselves arrive as prompts (a queued pause runs after the gate already
  paused). Those must not resume. The UserPromptSubmit hook clears the
  session's flag and adds `additionalContext` with:
  - the tracker path and its Now / Resume notes sections,
  - `checkpoint.json`,
  - **what changed since the pause**: jobs that finished or died (with exit
    codes and last log lines), files changed since the git snapshot, time
    elapsed.
  - instruction: verify the "Check first" items, then continue from **Next
    action**.
- `/resume all` un-pauses every session. A plugin can't send prompts into
  other sessions, so each still needs a message (any message works), which
  then brings its resume notes.
- `/resume <task>` in a new session: same injection, from the tracker and
  checkpoint only. The tracker is written to make this nearly as good as
  resuming in place.

## Spikes (run 2026-10-15)

The throwaway prototype was removed after the build; it's in git history
(`git show b9420c1:spike/`). `hook.py` logged every hook event to
`spike/state/events.jsonl` and gated tools on a flag file; `testproj/` loads
it through a project-local `.claude/settings.json` (user settings untouched);
`drive_sdk.py` drives `claude -p --input-format stream-json`, the same input
path the desktop app uses (its `claude` process runs with
`--input-format stream-json --replay-user-messages`). Runs used Haiku.

Items marked **(manual)** need a person typing into the desktop app or an
interactive terminal. The interactive CLI can't start inside an agent's
sandbox (no keychain, so it drops into login), so `spike/relay.py` ran it
in the user's Terminal panel instead. The manual steps were in
`spike/MANUAL.md`.

### 1. Typing into a busy session

Run through both the stream-json path (`drive_sdk.py`, `state/s1b`-`s1e`)
and the real interactive CLI (`relay.py`, which runs `claude` in a pty
that both the user and the agent can type into, `state/c2`). Each time the
second message went in a few seconds into a 6-call loop of `./tick.sh 5`.

| Sent mid-turn | Path | UserPromptSubmit fired | Effect |
|---|---|---|---|
| `/spike-pause` | stream-json | 18:53:17.0, **after** the turn's Stop and all 6 calls | too late |
| `/spike-pause` | CLI | 19:06:01.3, **after** Stop and all 6 calls | too late |
| plain text | stream-json | 18:52:41.4, right after the first PostToolUse | delivered at the next tool boundary |
| `pause` (hook matches it) | stream-json | 18:54:02.8, after the first PostToolUse | flag set; next call denied; turn ended with one line |

So UserPromptSubmit alone can't pause on a slash command. But both paths
write the queued command to the **transcript** at once:

```
{"type":"queue-operation","operation":"enqueue","content":"/spike-pause","timestamp":"...01:05:22.438Z"}   <- Enter pressed
{"type":"queue-operation","operation":"dequeue","timestamp":"...01:06:01.167Z"}                         <- turn ended
```

With the gate reading the transcript tail (see "Queued commands"):

| Sent mid-turn | Path | Typed | Next PreToolUse | Turn ended |
|---|---|---|---|---|
| `/spike-pause` | CLI (`c2`) | 19:08:03.7 | 19:08:09.1: flag set from queue, denied | 19:08:10.5 |
| `/spike-pause` | stream-json (`s1e`) | 19:08:56.6 | 19:09:02.3: flag set from queue, denied | 19:09:06.5 |

In the CLI it reads cleanly: "Ran 2 shell commands", "paused", then
the queued `/spike-pause` runs and answers "pause noted". A first version
matched an already-consumed enqueue from an earlier turn; replaying
enqueue/dequeue/remove as a FIFO fixed it.

**Finding / design:** slash commands work at the next tool boundary by
reading the queue from the transcript; the plain `pause` keyword stays as a
documented-API fallback.

**Desktop app UI, checked live after the build:** `/check:pause`
typed into a busy desktop session was queued at once (02:26:41.1) and the
gate paused at the next tool call (02:26:43.7).

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
submits text. Not needed now that a typed slash command works mid-turn.

### Other notes

- Hooks from an untrusted folder's `.claude/settings.json` still ran under
  `claude -p`, but its `permissions.allow` was ignored ("workspace has not
  been trusted"). Trust is keyed on the git root.

## Build order (after the spikes)

1. Plugin skeleton: marketplace, plugin.json, pixi.toml (Python 3.12 only),
   `run.py`, test script. Mirror gateway's layout.
2. Tracker convention: SessionStart note, template, PostToolUse nudge, Stop
   nudge.
3. Pause/resume: flags, PreToolUse gate with shell pre-filter (deny +
   `continue: false`), checkpoint from the gate and Stop, prompt hook for
   the commands (queued-command detection from the transcript) and the
   plain `pause` fallback, resume injection.
4. Jobs MCP server.
5. `pause now` (process finding and killing).
6. `status`, `install.py` (mirror gateway's), README, `/check:help`.

## Constraints

- Requirements: **Claude Code and pixi only**, self-contained like
  gateway: pixi.toml pins Python 3.12, every hook and the MCP server run
  through `pixi run --manifest-path ${CLAUDE_PLUGIN_ROOT}/pixi.toml --frozen`
  (started by `hooks/pixi.sh`, which finds pixi with a minimal PATH), and the
  only things outside pixi are the POSIX `sh` scripts in `hooks/`. No system
  Python, jq or other tools. Nothing in settings.json. Python stdlib only; macOS and Linux (desktop and
  headless).
- PreToolUse runs on every tool call: keep the not-paused path to a file
  existence check in shell.
- Hooks must **fail open** for the gate (a broken hook must not freeze every
  session), unlike gateway's prompt hook.
- Never delete or rewrite a user's tracker content; only the agent edits it,
  and hooks only read it (and write `checkpoint.json`).
- Coexists with gateway (both have PreToolUse hooks; order doesn't
  matter, any deny wins). Jobs pick up the gateway's environment when it
  is installed, and the gateway and pixi-guard check `job_start` (the
  gateway also masks the other job tools' output; section 2); neither
  plugin needs the other.

## 0.2.0 revisions

From a review of 0.1.0 (2026-10-18). The user's decisions:

- **Install through the marketplace only.** `install.py` (which merged allow
  rules into the user's settings.json) and `settings.rules.json` are gone.
  The PreToolUse pre-filter allows `job_status`, `job_tail` and `job_stop`
  itself. `job_start` is never auto-allowed: a hook allow skips the
  permission prompt and the auto-mode classifier, and `job_start` runs any
  command outside the Bash tool, past Bash deny rules and other plugins'
  Bash checks.
- **No settings file.** `config/settings.json` lived in the plugin folder,
  which an update replaces; its values are constants in `state.py`.
- **`pause all` is a flag per running session**, not a global flag: the
  global one never cleared itself, kept every hook in every session on the
  slow path, and paused sessions started later.
- **Trackers naming another session are never used** (0.1 fell back to any
  tracker with a stage in progress, so one session could overwrite
  another's checkpoint and be told to continue its work).
- **`pause now` parses the command** out of the shell's exact argv instead
  of matching quoted text: Claude Code quotes `'` as `'"'"'`, and macOS `ps`
  shows a newline as `\012`, so most real commands were never found.
- Also: `job_stop` waits for the whole process group and reports
  `stop_failed`; watchers end with cancelled calls; pixi is found outside
  PATH; old records and job logs are pruned; the MCP server follows `/clear`;
  paused time is an epoch; skills replace commands/; hooks use exec form.
- **A queued pause command is applied as typed** by the gate or the
  watcher, every queued one oldest first (not only the newest), also in a
  session that is already paused: a queued
  `pause all now` or `pause <session> now` stops the
  other sessions' commands, and doesn't pause or kill the typing session
  unless it is a target. Its echo (the same command run as a prompt) is
  recognized until it comes or another prompt does, even when it comes
  while the command is still being applied or after a second queued pause
  command; a restarted session forgets them.
- **Only a session that wrote it** takes a tracker that names no session.
- **No `ps` dependency on Linux** (the table is read from `/proc`); on
  macOS `/bin/ps` comes first, and a `ps` that fails or prints no rows
  (a broken one earlier on PATH) passes to the next. A process table that
  can't be read never marks jobs vanished or prunes records, and `pause
  now` then says it couldn't stop the command (not "No command was
  running"), also in the queued command's echo when the turn ends; `zombie()` skips a `ps` output that isn't a process state. `pause now` leaves an older shell running the same text (a
  background call) alone. It kills with the table it found the command in,
  and a read that fails while it waits asks each pid (taking it for "all
  gone" left a command that ignores SIGTERM running, reported killed); a
  command nothing of which dies even of SIGKILL is one it couldn't stop.
