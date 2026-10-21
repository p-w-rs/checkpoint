# Architecture

Hooks, an MCP server, skills (the `/check:*` commands) and a mod (Claude
Code's native hooks module, `hooks/mod/`), sharing `src/checkpoint/` through
`run.py`, plus a POSIX `sh` pre-filter in front of every hook. The hooks and
the mod record each task in a durable journal ("The journal", below). Python runs
from checkpoint's own pixi env in Claude Code's plugin data folder (the
runtime, below). Why it's built this way, and the spikes behind it:
[DESIGN.md](DESIGN.md); the v4 plan: [PLAN-v4.md](PLAN-v4.md).

```
user prompt ──▶ mod prompt.submit: not the user's (isMeta, scheduled, ...; slash commands
                  too)? noresume/<sid>, or held back if it can't be written
                  `pause` typed over a running turn: pause at once (below)
              ─▶ UserPromptSubmit ─ fast.sh ─▶ hooks.handle_prompt
                  drop this session's running/ files (no call is running now);
                  /check:pause|resume|status|doctor, plain "pause", or a message the
                  user typed in a paused (or un-paused, resume pending) session = resume
                  no Python: fast.sh's fallback pauses / resumes / says why
tool call ────▶ mod tool.call gate: paused? deny (but read-only tools, tracker writes);
                  can't tell? .catch denies
              ─▶ PreToolUse(*) ──── fast.sh ─▶ hooks.gate
                  job_status/tail/stop: allow (shell, no Python)
                  flag file or queued pause in the transcript tail? else go on
                  paused (or a pause for it queued) and no Python answer: deny (sh)
                  not paused and no runtime ready: allow, degraded/<sid> (sh)
                  foreground Bash: write running/<id>.json, start watch.sh
tool result ──▶ PostToolUse(*) / PostToolUseFailure ─ fast.sh
                  remove running/<id>.json; count calls; every N ─▶ hooks.post
turn end ─────▶ Stop ───────────── fast.sh ─▶ hooks.stop (paused, or trackers exist)
session start ▶ SessionStart ───── fast.sh ─▶ hooks.session_main (+ prune, modes)
                  no runtime yet: start its build, say so once, stay fail-safe
              ▶ SessionStart (async) env.sh ensure: build the runtime
              ▶ mod session.start: /check-pause, /check-doctor, status line
/check-pause ─▶ mod (immediate, mid-turn): flag, [now: stop the command], turn.abort, ack
job tools ────▶ MCP server "jobs" (sh hooks/env.sh mcp: the env's python ─▶ mcp_server.py ─▶ jobs.py)
                  gateway available (CONTRACT-v1, feature jobs)? POST /v1/jobs: the gateway supervises it;
                  else checkpoint's own detached runner; checkpoint_note / task_policy ─▶ the task's journal
                  a thread follows the gateway's events (gateway.sync) into the journal
pause/resume ─▶ core.gateway_pause / gateway_resume: POST /v1/pause, /v1/resume (once per pause, with
                  its pause id; late delivery while still paused, never just before a resume; an
                  uncertain one resent only when that can't signal a job twice)
every event ──▶ fast.sh ck_spool: payload file + one index line in spool/<sid>/ (no Python)
  (also SessionEnd, PreCompact, PostCompact, SubagentStart/Stop, Notification, PermissionDenied,
   PermissionRequest, TaskCreated/Completed; the mod adds turn.start/complete, compactions, its
   denials, and Claude Code's permission verdicts (tool.check) with what came of an ask: env.sh spool)
Python moments ▶ capture.ingest: spool ─▶ sanitized records ─▶ journal/<task>/seg-*.jsonl
                  (SessionStart + dead sessions' spools, Stop, SessionEnd, Pre/PostCompact, the every-N
                   PostToolUse, a pause, /check:*, MCP calls, the mod's 20 s timer, a test command's snapshot)
/check:save, /check-save ─▶ core.save: ingest, summary, file hashes, git ─▶ checkpoint record, fsynced
                  (every pause saves one too: core.pause_checkpoint, once per pause)
resume ───────▶ a typed message / /check:resume [task|checkpoint] / /check-resume / a session start after
                  a crash, --resume or /compact ─▶ recovery.reconcile (reads only) ─▶ the resume context
                  (Check first, the five classes, files, git, validation, instructions, lineage, the
                  hosts the task used and its authorizations against the gateway's snapshot now) + summary
/check:list|diff|cancel|stop|complete|export ─▶ core / recovery (never resume; available while paused)
```

## Modules

| File | Does |
|---|---|
| `hooks/fast.sh` | Pre-filter for all hooks. Reads `session_id`, `transcript_path`, `cwd`, `tool_name` with grep/sed; starts Python only when there's work. Not paused + nothing queued costs a `tail \| grep` of the transcript (the same tail Python reads: last 256 KB, cut first line dropped). Allows the three job tools that only read or stop jobs. |
| `hooks/watch.sh` | One per running foreground Bash call. Every 2 s: a queued `pause now` or `now/<sid>`? Then `run.py watch-fire`. Exits when PostToolUse removes `running/<id>.json`, when the call's `tool_result` is in the transcript (a cancelled or denied call fires no PostToolUse; a subagent's goes to its own transcript), or when Claude exits. |
| `hooks/pixi.sh` | Finds pixi on PATH or in the usual install folders (`$PIXI_HOME/bin`, `~/.pixi/bin`, `~/.local/bin`, Homebrew, MacPorts, Linuxbrew) and execs it. |
| `hooks/lib.sh` | Sourced by the sh scripts: where the runtime lives (`ck_env`), whether it is ready, the previous env during an update, running `run.py` with it (`ck_exec_py`: `-I -S -X pycache_prefix=$DATA/pycache`), the build lock with live owners, the build (`pixi install --locked`), warming, cleanup, the background kick, `ck_state`, atomic writes, `degraded/<sid>`. |
| `hooks/env.sh` | `ensure` (the async SessionStart hook and the kick), `mcp` (the MCP server's launcher: starts a build and waits up to 25 s), `cli` (the terminal helper), `run` (the mod: Python now or exit 75), `doctor` (the runtime half of /check:doctor, no Python), `state`, `flag` (the mod's atomic flag writes: pause, now, noresume). |
| `hooks/mod/register.tsx` | The mod: `/check-pause [now]` and `/check-doctor` (immediate), answers `/check:pause [now]`, `/check:doctor` and `/check:status` without a model turn, the `tool.call` gate (fails closed), the no-self-resume mark, `pause` typed over a running turn, the status line. State on disk only (flags/, rt/, jobs.json); module variables hold the running turn and the calls in flight. |
| `hooks/mod/logic.ts` | Its pure helpers (ids, the allow list, the pause forms, texts), checked against Python by the suite. |
| `state.py` | State dir layout (see its docstring), flock, atomic JSON writes, pause flags, the fixed settings, live sessions (newest record per running Claude pid), the queued-command memory (`queue_handled`, the `queued_pauses` list + `queued/<sid>`), the trackers each session wrote, pruning (skipped without a process table), and migrating 0.1's global `paused-all` file. |
| `transcript.py` | Transcript tail; replays `queue-operation` enqueue/dequeue/remove to find the pending pause commands (oldest first), and (over the whole file) what became of a handled one: still pending, dequeued or removed; tool results; Bash commands and recent tool calls. |
| `tracker.py` | Finds and parses `.work/<task>/TRACKER.md` (stages table, sections, `Session:` id). A tracker naming this session is its own; one naming no session is a fallback only for a session that wrote it; one naming another session is never used. Read-only. |
| `core.py` | Pause (incl. `now` (= `interrupt`), `all`, `--stop-jobs`, another session, and the queued command's second run; a plain pause names a foreground command it leaves running; `now` says exactly what stopping did (processes, SIGTERM or SIGKILL, survivors) and records `op.killed`; `now` without a process table says it couldn't stop the command, never "No command was running", and so does a command nothing of which died even of SIGKILL; the session's `unstopped` list, with why and cleared on resume, lets the queued command's echo say so too), checkpoint (and one per pause), the resume context (built on `recovery`), resume here / of a task / from a checkpoint (`select`), `resume_now` (the mod's `/check-resume`), `cancel` (child-work policy), `complete`, `stop_job`, list and diff, resume all, status. |
| `procs.py` | Process table with state and start time (`/proc` on Linux, `ps` on macOS: `/bin/ps` first, and the next `ps` whenever one fails or gives no rows; empty means unknown; `zombie()` likewise takes only an output that is a process state), finding Claude (`CLAUDE_PID`, else from a hook's parent), reading a process's exact argv (`/proc` on Linux, sysctl on macOS), parsing the Bash tool shell's `eval '<command>'` like a shell, matching it to the guarded call (exact command, shell started after the call's PreToolUse), `command_shells` (the same match anywhere in the table, for reconciling after a crash: Claude may be gone and its shells adopted by init), killing a tree group by group (with the caller's table; without one, the pid it was given and the group that pid leads; while it waits, a table it can't read means asking each pid, never "all gone"; it says which processes went and which survived SIGKILL). |
| `jobs.py` | Detached jobs, two supervisors and never both for one job: with the gateway available (`gateway.discover()`, feature `jobs`), `start()` hands the job to the gateway (`_start_gateway`: `POST /v1/jobs` with the session, `task_ref`, the idempotency key `ck:<tool_use_id>`, `on_pause`; one retry with the same key after an ambiguous try; once a try may have started it, the outcome is unknown whatever the retry gets: never a fallback) and records `owner: gateway` with the gateway's job id; state, tail and stop go through the contract (`_status_gateway`, `_tail_gateway`, `_stop_gateway` with `outcome` stopped / stop_failed / unknown), and the registry stores the last state the gateway reported (`note_gateway_states`, `_apply_view`; a job a new epoch doesn't list is `unknown`, `gone`). `is_live` counts a gateway job's live states and `unknown` (gone or not: it may still run), `is_final` only exited/failed/canceled, so unknown ones are never pruned nor reported stopped. Otherwise, checkpoint's own runner (each records its journal `task_id` and `on_cancel` policy, `interrupt` or `detach`; `detach()` releases one from its task): `sh` wrapper in its own session writing `exit_code`; states running / exited / killed / vanished / stop_failed (running while the wrapper or anything in its process group runs, also a wrapper whose command line isn't readable yet but that started with the job: just after `Popen` Linux can show it as `[sh]`; left as is when there is no process table). `job_stop` waits for the whole process group to go (SIGKILL after the grace period). The MCP server reaps the jobs it started (SIGCHLD), and `procs.alive` treats an unreaped zombie as dead, so another session can stop a job cleanly. Jobs get `job_env()`: the environment minus checkpoint's own pixi activation. With the gateway installed, the job's shell (`WRAPPER`) then sources its `env.sh` (`gateway_env()`: only a regular file you own, not writable by others, in a folder likewise; `CHECKPOINT_GATEWAY_ENV` overrides the path), after a trial run in a subshell, and writes `applied`/`failed` to `jobs/<id>/gateway_env`, which the registry's `gateway_env` picks up (`refused: <why>` when it wasn't passed). Finished jobs are pruned after 14 days. |
| `mcp_server.py` | Hand-rolled stdio JSON-RPC (initialize, tools/list, tools/call, ping). Session id per call: the newest session record for `CLAUDE_PID` (so `/clear` is followed), else `CLAUDE_CODE_SESSION_ID`. Each tool carries `_meta: {"anthropic/alwaysLoad": true}` and `.mcp.json` sets `"alwaysLoad": true` (0.7.1: Claude Code 2.1.304 otherwise defers the tools behind ToolSearch; the cost is ~4.5 KB of schemas per request, and a session start waits for the server, at most a few seconds). |
| `cli.py` | `status / pause / resume / doctor / summary / tasks / save / list` for the terminal helper (`$CHECKPOINT_HOME/checkpoint`, written at every session start) and `pixi run` in a checkout; `ingest`, `snap` and `status --session` for the hooks and the mod. |
| `journal.py` | The durable journal: per task, append-only JSONL segments of canonical records with CRC32 and gap-free seq; flock (writers exclusive, readers shared); verifies segments incrementally from a head cache (every segment's content CRC checked, so damage in an older one is found); dedups (kind, op); starts a new segment after a bad tail; refuses to write behind a seq gap (`JournalDamaged`); fsync every N records and on request (F_FULLFSYNC on macOS); failure markers (`journal-errors/<sid>`). |
| `sanitize.py` | Scrubbing before anything is journaled: token shapes, PEM blocks, URL credentials, auth headers, sensitive assignments/flags/JSON fields, sensitive field names (also written without separators: PGPASSWORD, APITOKEN); bounded summaries of tool inputs and results: never file contents (Read, Grep, Glob, WebFetch, WebSearch keep paths, counts and sizes; a Bash command's output only as sizes unless it is a test, build or check), edit strings, an unknown tool's raw input, or more than an unknown tool result's keys and size; whether a result is only a start (`background_of`) and the exit code it states (`stated_exit`). |
| `capture.py` | The spool (fast.sh's and the mod's) into the journal (a prompt's op is its prompt_id plus its text's hash: a prompt typed mid-turn carries the turn's prompt_id; a task notification also becomes `background.result` records keyed by the start's tool_use_id; a TaskOutput/TaskStop result too): reads the index from its cursor, turns events into sanitized records, appends, then deletes payloads (a failed write leaves the spool as it was); orphans, cut payloads, index rotation; tasks and lineage (startup/clear start one, resume/fork/compact continue the parent's); git identity and the version a validation runs against (`snap`); `ingest_dead` for killed sessions. |
| `summary.py` | The recovery summary, deterministic from the journal: Goal, Latest instructions, Constraints & decisions, In flight / unknown, Plan, Validation (staleness against current hashes), Failures, Files changed, Done, Children, Lineage, Evidence; observed vs inferred; bounded (sections left out are named); the task list and "interrupted". A background start is linked to the end Claude Code reported later (`bg_result`: a task notification, TaskOutput/TaskStop), also for a call whose turn was aborted (no outcome at all); an intent without an outcome whose command a process runs now reads "still running" (process table). Recorded text is scrubbed again before it is clipped. |
| `recovery.py` | PLAN-v4 phase 3. `reconcile(task, sid, checkpoint)`: the saved state (the checkpoint named, else the latest, else the journal's last record of each file) against now: files (changed outside the task / by the task since / deleted), git (HEAD moved and how, branch, newly dirty), validations (stale), instructions (then, since, the resuming message), operations in five classes, the task's own-runner jobs, lineage (original session, `claude --resume`, alive elsewhere or in this same process, concurrent sessions); `Recon.text()` the resume context, `.short()` the person's line, `record()` the `reconcile` journal record. `external_effect()` (push, remote, http write, deploy, publish, db, system, MCP). `list_data`/`list_text`, `diff`, `find_checkpoint`, `export` (rescrubbed bundle), `prune_tasks` (retention). Reads only: never writes files, the repository or transcripts. |
| `hosts.py` | 0.7.0: the gateway hosts a task used (`used`: its gateway jobs and terminals, the gateway tools' `host`, `ssh NAME` / `NAME:` to a host the gateway lists) and each one against the gateway's snapshot now (`now`, `HostNow`: reachable, auth, runtime as the gateway reports them, "not reported" or "unknown" otherwise; `blocked()`; `identity_change()`). |
| `authz.py` | 0.7.0: authorization references. `check_record` (the mod's tool.check verdict as a journal record: decision, rule, deciding hook, the gateway's right names read from its fixed wording by `gateway_rights`, never the text), `collect` (rules, gateway rights, asks and their answers, auto mode's denials, gateway accounts, dialogs, rights changes, permission modes), `refused_ops` (calls Claude Code refused: never dispatched), `summary_lines`, `check` (each against now: settings files, the gateway's snapshot). |
| `gateway.py` | PLAN-v4 phase 4: the CONTRACT-v1 client ([CONTRACT-v1.md](CONTRACT-v1.md), `gateway.v1`). `discover()` (the discovery file: absent, off, unsafe, unreadable, dead, unknown_contract, mismatch, available; `hooks/env.sh doctor` makes the same checks in sh), `call()` over the Unix socket (HTTP/1.1, JSON, `"contract": 1`; errors are `unreachable` and `refused`, which certainly did nothing, or `ambiguous` and `protocol`, which may have), the calls (capabilities, snapshot, events, jobs, signal, pause, resume), `clean_event`/`clean_job`/`clean_host`/`clean_terminal` (the second layer: each kind's allowed fields, values of the gateway's allowlisted characters, and token-shaped ones scrubbed), `sync()` (the session's cursor: a new epoch re-reads the snapshot, events in seq order, `gw:<epoch>:<seq>` ops drop duplicates, the session's or task's jobs, terminals and dialog requests become journal records). |

## The gate

1. Queued pause commands, paused or not: if the transcript tail has a
   pause line not in `seen/<sid>` (while paused, Python runs anyway and
   always looks), replay the queue. Every pending pause newer than
   `queue_handled` (several can be typed before one tool call) is claimed
   by its own timestamp and applied, oldest first, as typed: `all` or
   another session, and `now` stops every target's running command. The
   watcher does the same as soon as one of them has `now`. In a paused
   session an error here still leaves the deny below.
   The command still runs as a prompt of its own (when the turn ends, or
   absorbed into the turn); that echo is recognized and not applied twice,
   so sessions resumed meanwhile stay resumed. The command is remembered
   before it is applied (an absorbed echo can arrive as soon as `now` has
   killed the call, while other commands are still being stopped), each
   queued command on its own (`queued_pauses`, keyed by enqueue timestamp,
   matched oldest first). Each memory ends with its echo, or with another
   prompt once its entry is no longer queued; `queued/<sid>` sends every
   prompt to Python while any is left.
2. Paused (now or already): allow `ALLOW_WHILE_PAUSED` tools (no decision; normal
   permissions) and Write/Edit inside `<project>/.work/` (an explicit allow,
   so the agent doesn't stall at a prompt while the user is away); deny
   everything else. The first deny writes the checkpoint. After
   `MAX_DENIES`, deny **plus** `continue: false` (alone it would let the
   tool run). Subagents get "return a short report".
3. Any exception: while paused, a deny (fails closed); otherwise exit 3
   with no output, so the call goes ahead and fast.sh records
   degraded/<sid>. When Python can't run at all (no runtime yet, a crash,
   past the 20 s deadline, `CHECKPOINT_HOOK_DEADLINE`), fast.sh decides: paused, or a pause for this
   session queued, means deny (and the queued pause is recorded as the
   flag); not paused, the call goes ahead and degraded/<sid> says so (also
   on the plain fast path, which needs no Python, when no runtime is ready:
   the gate is running without its Python half). degraded/<sid> is history:
   it stays until the session is pruned, so `/check:doctor` says "normal"
   once a runtime answers again and names the last degraded time, and
   `/check:status` lists the last day's as past events.
4. With the mod, its `tool.call` gate runs first: while paused it denies
   everything but `ALLOW_WHILE_PAUSED` and writes inside `<project>/.work/`
   (those go on to the classic gate, which decides again with symlinks
   resolved), ends the turn after `MAX_DENIES`, and its `.catch` denies when
   the check itself fails. Its denies never reach the Python gate, so the
   mod's pause calls `run.py sync-pause` afterwards for the session record
   and the checkpoint.

`pause all` sets a flag for every running session, so a session resumed by a
message is back on the shell fast path, and sessions started later aren't
paused.

## The journal (PLAN-v4 phase 2)

**Format.** `CHECKPOINT_HOME/journal/<task>/seg-NNNNNN.jsonl`: one record per
line, canonical JSON (sorted keys, ASCII), `{v: 1, seq, op, ts, kind, session,
agent?, task, data, crc}` where `crc` is the CRC32 of the record without it.
`seq` runs from 1 per task with no gaps; segments roll over at 1 MiB. Kinds:
`session.start` (source, parent, how the parent is known, cwd, Claude pid,
transcript), `session.end`, `session.adopt`, `repo`, `prompt` (scrubbed text,
the person's or not, a `/check:*` command or not), `tool.intent` /
`tool.outcome` (op = the tool_use_id; input and result summaries;
`before`/`after` file hashes; `ok`; an exit code only when the payload states
one (Claude Code's Bash result doesn't: "Exit code N" comes with
PostToolUseFailure), and the words of an exit it doesn't count as an error
(`interpretation`, grep's "No matches found"); `background` instead of `ok`
when the call only started work that goes on (a Bash `backgroundTaskId`:
run_in_background, Ctrl+B, moved to deliver a message, a timeout; an Agent
launched async); `denied` when it never ran),
`validation.version`, `plan.event`, `subagent.start` / `.stop`, `turn.start` /
`.complete`, `compact`, `notification`, `stop`, `pause` / `resume`, `note`,
`checkpoint`, `task.continued`, `spool.problem`. Folders 0700, files 0600.

**Writing.** One writer at a time per task (`flock` on `.lock`; readers take
it shared, so they never see half a write). A writer verifies the segments
from `head.json` (per segment: size, mtime, next seq, and the CRC32 of its
whole records, checked against the bytes for every segment; recent keys;
rebuilt from the segments when it disagrees), drops a record whose kind and op it has seen recently (a
duplicate delivery; a session start's or end's op carries its spool file's name, so two of them within
one second, a quick exit and `--resume`, are two records and a re-ingested file is still one), writes a
batch with one `write` (O_APPEND), and syncs
every 32 records and whenever asked: checkpoints, pauses, Stop, SessionEnd,
session start (`F_FULLFSYNC` on macOS, then the folder when a segment is new).
Nothing says "saved" before that returns.

**Damage.** A reader stops a segment at its first line that isn't a whole
record with the right CRC and seq, says where and how much it skipped, and
never rewrites it; a writer that finds such a tail starts the next segment,
continuing the seq readers can see. A segment that starts out of step (one
missing, or one before it damaged) stops reading for good; a writer refuses
to append behind it (it re-reads any segment whose bytes no longer match the
cached CRC, so a flipped bit in an older segment, size and mtime unchanged,
is found too), and capture continues the session in a new task whose first
record names the damaged one.

**Capture without Python.** `fast.sh` (and `env.sh spool` for the mod) writes
each payload to `spool/<sid>/<pid>.ev` (noclobber: never shared) and then
appends `1 TAB event TAB file TAB bytes [TAB h=hash] [TAB cut=bytes]` to
`spool/<sid>/index`. A line that short is one `write`, so concurrent hooks
never mix lines (measured on macOS: 16 writers of 1023-byte lines never mixed
under sh, bash or dash; 1025-byte lines did, so payloads go in files of their
own). Payloads over 256 KiB are cut (`cut=`); the record keeps what the cut
prefix still says. Edit tools get the file's sha256 before (PreToolUse) and
after (PostToolUse) from the shell (`h=`; `h=escaped` when the path has JSON
escapes the shell can't spell, and Python looks then). A Bash command whose
text looks like a test, build or check gets the HEAD and branch it starts on
in its index line (`g=`, `b=`: read by the hook from `.git/HEAD` and the ref
file or `packed-refs`, a worktree's `commondir` followed, no git process),
and starts `run.py snap` in the background (the hook never waits): it
ingests and records HEAD, branch and the hashes of changed and untracked files
(and of the task's edited files: outside git that is all there is), how long
after the command's intent it ran (`lag_ms`), and `head_moved` when its HEAD
isn't the one the hook read (the command moved it first). Measured cost on the not-paused path: about 1 ms per hook (median of
40, against a copy with the spool calls removed; macOS, sh and dash).

**Ingesting.** At the Python moments (the diagram above) `capture.ingest`
reads the index from its cursor, turns events into records (`sanitize.py`
first), appends, and only then deletes the payload files and moves the
cursor: a write that fails leaves the spool as it was, records the failure
(`journal-errors/<sid>`) and says so once in the hook's output; the status
line ("recording FAILED"), `/check:status` and `/check:doctor` show it until a
write works again. A spool that can't be written is said by the hook itself,
once (`spool-errors/<sid>`: since when and why, and a systemMessage); its
events are lost. On a full disk neither marker can be created as a new file,
so the session start writes a reserve for each (`.reserve.<sid>`, 1 KiB of
spaces) that a failure renames into place and overwrites (no new blocks on
most filesystems; checked on a filled HFS+ image). When recording works
again the failure becomes history (`spool-errors/<sid>.last`,
`journal-errors/<sid>.last`) and a lost window gets a `recording.gap` record
(from when, until when, why): the summary's header names it, `/check:doctor`
and `/check:status` too (0.7.1; before, a full-disk window left nothing). A
payload whose index line never came
(its hook died) is taken after a minute (`orphan`). The index rotates once
ingested past 256 KiB. About 8,000 events per second (1,000 in ~130 ms).

**Tasks and lineage.** A session started fresh (`startup`) or after `/clear`
begins a task (`clear` names the previous session of the same Claude process
as its parent); `resume`, `fork` and `compact` continue the parent's task (the
same session id, or the transcript's earlier session id). `/check:resume
<task|last>` makes a session adopt a task. A session start ingests the spools
of sessions whose Claude process is gone (killed), and lists the project's
unfinished tasks; after a compaction, a fork or a `--resume`, the
reconciliation and the recovery summary go into the context (after a clean
exit only what changed since, and nothing when nothing did: reconciled and
recorded either way). `claude --resume <id>` while another live Claude process
still has that session open is said and recorded (`session.concurrent`); a
process that has moved on (after `/clear` it holds a newer session) or a
session whose run ended doesn't count.

**The recovery summary** (`summary.py`) comes from the journal alone, so the
same journal and files give the same text: Goal, Latest instructions,
Constraints & decisions (`checkpoint_note`, tracker sections), In flight /
unknown (intents without outcome: "outcome unknown", transcript evidence as a
supplement, never done; work started in the background: "its result was not
observed"), Plan (steps and their recorded status), Validation (result, never
"passed" for a background start; the HEAD the hook read before it ran, the
snapshot's lag, "uncertain" when HEAD moved in between; whether it is still
current: the recorded hashes compared with now, in git or not, and HEAD and
`git status` in a repository), Failures, Files changed (hashes, changed since?,
failed edits; a failed edit is never a written file), Done, Children (started,
stopped, or no result observed), Lineage (each run of each session), Evidence
(journal path and seq range, transcripts). The header is never left out: the
task, journal problems, the pause, a finished or reopened task, recording
gaps and the last checkpoint (0.7.1: it was in Evidence, the first section a
resume context under long paths lost). Over its size
limit (8,000 characters; in a resume context whatever the reconciliation
leaves, 1,200 to 4,000, at a session start up to 3,500) a section that doesn't fit keeps its first lines or,
when not even one fits, is left out; either way it is named. In flight lists
the ones with a possible external effect first. A task picked up again after
`/check:complete` or `/check:cancel` reads "Reopened" (`task.reopen`).

## Recovery (PLAN-v4 phase 3)

**Reconciliation** (`recovery.reconcile`) runs before every resume and only
reads. Its base is a checkpoint: the one chosen (`/check:resume <checkpoint>`,
`/check-resume <checkpoint>`, the pane), else the task's latest (every pause
saves one, so a resume compares with the state at the pause), else the
journal's last record of each file. It compares:

- **Files.** The checkpoint's hashes (the task's changed files and git's dirty
  files) with the files now. A difference no edit the task recorded explains
  is "changed outside this task": listed, kept, never reverted. One the task
  made after the checkpoint is said as such; a file back to its checkpoint
  state although the task edited it later was undone outside the task.
- **Git.** HEAD moved (`git merge-base --is-ancestor`: new commits on top, or
  rebased/reset/another branch), the branch, files dirty now that weren't and
  dirty then that are clean now.
- **Validation.** The summary's staleness: a result whose recorded hashes or
  HEAD no longer match is STALE.
- **Instructions.** The one in force at the checkpoint, the ones given since
  (newer, they take precedence), the notes recorded since, and the message
  that resumes the task.
- **Operations**, each in one of five classes, from the journal, the
  transcript (supplementary), the process table and the own job runner:
  *never dispatched* (denied by the gate, the mod or a permission), *confirmed
  completed* (an outcome, a background end reported later, a subagent's
  stop, a call in flight at the checkpoint that ended after it), *still
  running* (a shell anywhere in the table with exactly that command, started
  after the intent: Claude may be gone and its shells adopted by init; or a
  non-Bash call of a session still alive elsewhere), *confirmed failed or
  canceled* (an error, an interrupt, a `pause now` kill (`op.killed`), a
  background end that failed), *outcome unknown* (no result and nothing runs
  it; or no process table). An edit with no result carries its evidence (the
  file still has its before-hash: it looks unapplied). An unknown or running
  one whose command may change state outside the repository
  (`external_effect`: a push, ssh/scp/rsync, an HTTP write, kubectl, helm,
  terraform, a deploy or publish, a database client, sudo; a non-read MCP
  tool) goes first under "Check first", with what to look at (`git ls-remote`
  for a push) and "do not run it again until you know". Nothing ever says to
  redo a stage from the start. A resume checks what is still open, not the
  task's whole history (`_scope`; the summary keeps everything): a failed or
  interrupted call is left out once a later call doing the same (the same
  command, the same file) succeeded, or once its session went on with other
  work (a call that ran, not a read, not while paused: its conversation holds
  the failure); an unknown outcome once a later identical call succeeded; and
  whatever an earlier resume already reported, once work went on from that
  reconciliation (the horizon; one made while the session stayed paused
  doesn't count). What still runs always stays, and an unknown outcome with a
  possible external effect that an earlier resume reported stays named in one
  line of "Check first".
- **Jobs** of checkpoint's runner that belong to the task (its `task_id`, or
  one of its sessions for older jobs): running, exited (0 or not), stopped,
  vanished (unknown).
- **Lineage.** The task's original session (the last other session that ran
  it): its `claude --resume <id>` (with its folder), whether it is alive in
  another Claude process (then: continue there), or ran in this same process
  (after `/clear`), and other live sessions recording into the task.

The result is the resume context (`Recon.text`), a one-line report for the
person (`Recon.short`), and a `reconcile` journal record with the counts (and
whether the session was still paused). The resume context is bounded as a
whole (8,500 characters; 7,000 at a session start): "Check first" and
"Interrupted operations" are never left out, their items are cut instead
(every unknown outcome with a possible external effect stays, then what
still runs, then the rest, and "...and N more" names the installed CLI that
lists them all: `$CHECKPOINT_HOME/checkpoint reconcile <task>`, never `pixi
run`, which in a user's pixi project would run that project's own task); the
other sections lose items from the end; "Check first" names the files
changed outside and the stale results itself; the summary comes last and is
cut first. The routes: a message typed into a paused (or resume-pending)
session and `/check:resume` (`core.resume_here`), a task or checkpoint picked
up in another session (`core.resume_task`, which adopts the task: an explicit
continuation, `session.adopt` with its parent and whether that one is alive;
a finished task is reopened, `task.reopen`; a reference that matches nothing
changes nothing, and a paused session stays paused), the session start after
a crash, a fork, every `--resume`, or a compaction (inside
`<checkpoint-recovery>`), and the mod's `/check-resume` (`resume-now`:
reconciles, lifts the flag with the resume pending, and the mod's timer sends
a prompt whose classic hook adds the context).

**Management.** `/check:list` (time, seq range, a one-line summary saved with
the checkpoint, current or stale against the files and HEAD now), `/check:diff`
(between two checkpoints or one and now: files, plan steps, operations,
validation, git, instructions; each side's journal state is the records up to
its seq), select (above), `/check:complete` (`task.complete`) and
`/check:cancel` (`task.cancel`: a foreground command stopped first with
interrupt, then each running job of the task by its own `on_cancel` or the
policy given, interrupt = `jobs.stop`, all at once, detach = `jobs.detach`;
the outcomes recorded; the pause lifted; the mod's `/check-cancel` holds every
tool call while this runs, then ends the turn). Both leave the journal and
move the session to a new task (`session.adopt` naming the old one); picking
the task up again reopens it. `/check:stop <job>` stops one job (`job.stop`
recorded) and says the outcome only: never the log's last lines, since both
routes answer straight into the model's context where nothing masks them (a
job's log can hold what the gateway's env file exported); `job_tail` shows the
log, and the gateway's hook masks that tool's result. The resume context
names a job that ended while paused the same way. `/check:export` writes a bundle (0600): the summary
and each checkpoint's summary rebuilt from the records scrubbed again, a
journal excerpt (rescrubbed, sensitive keys dropped), references (paths, ids,
`claude --resume` lines), never file contents or transcripts. Retention
(`prune_tasks`, from `state.prune`): finished tasks after
`CHECKPOINT_KEEP_DAYS` (30), open ones after `CHECKPOINT_KEEP_OPEN_DAYS`
(180), never one with a live or paused session or a running job, nothing
without a process table.

**While paused** every one of these is a prompt the hook answers (or an
immediate mod command) and never a resume: `transcript.COMMAND_RE` names
them, the mod's `typedResumes` and fast.sh's fallback (no Python) agree.

**Background completions.** Claude Code 2.1.301 reports a background
command's end as a task notification prompt (UserPromptSubmit sees it, idle
or mid-turn): `<task-id>`, `<tool-use-id>`, `<status>completed|failed|killed`
and a summary with the exit code; a check-in has no status. Capture keeps the
status, the stated exit code and a scrubbed summary line (never the output
file or the "Last output" tail) as `background.result`, keyed by the start's
tool_use_id; summary and reconciliation link it to the start. A Bash call
whose turn was aborted gets no PostToolUse at all (measured), so the link
works from the intent too.

## The gateway (PLAN-v4 phase 4, CONTRACT-v1)

Checkpoint is the contract's consumer; the gateway owns hosts, terminals,
jobs and their execution state. Everything about it is optional and short:
every call has a timeout (`CHECKPOINT_GATEWAY_TIMEOUT`, 2 s; a start 2.5x, a
pause 5x), and "not available" always comes with its reason
(`/check:status`, `/check:doctor`, the job tools). `CHECKPOINT_GATEWAY_CONTRACT`
points discovery elsewhere (tests: into their temp dir); set but empty, it
turns the integration off. A discovery file and a live pid don't say the
daemon serves this session: it serves only processes of Claude Code sessions
it knows (with the gateway plugin on), so a daemon left running from another
session while the plugin is off here refuses every call. `/check:status` asks
(`gateway.serves`: one capabilities call) and says "available: new jobs run
there" only when it answered; refused, "doesn't serve this session" and the
own runner's limits; no answer in time, "found, didn't answer" (0.7.1:
acceptance saw "available" while the job ran in the own runner). The sh
doctor (`env.sh doctor`) can't ask and says so.

**Jobs.** One supervisor per job. `job_start` with the gateway available
starts it there (above, `jobs.py`); a start that never connected, or one
refused as not served (`gateway.not_served`: the codes the gateway's daemon
sends when it doesn't serve the caller at all, 403 `plane`,
`not_registered`, `peer`, `uid`, and 404/405 `no_route`), certainly did
nothing and falls back to the own runner, and the tool's answer says why
(`gateway: not used: ...`); every other refusal (403 `policy` with its
`rule`, 403 `session`, 409 `paused`, 400, a code checkpoint doesn't know or
none) is the gateway's decision: the answer says "refused by the gateway"
with its message, code and rule, nothing starts anywhere, and the journal
records `job.started` with `ok: false`, `outcome: refused`, `refused_by:
gateway`, the code and the reason (the summary lists it as REFUSED, started
nowhere). Before 0.7.2 any 403 fell back, so a job the gateway refused by
policy ran in the own runner. A stop the gateway refuses is `stop_failed`
("refused by the gateway"), and a refused pause is said as refused: neither
ever becomes a signal of checkpoint's own (a gateway job's record has no
process group);
a start sent but not answered, or answered outside the contract, is retried
once with the same key, and if neither try gave a job, the outcome is
unknown: `job.started` with `ok: false`, `outcome: unknown` and the key, and
nothing runs in checkpoint's runner. The task's default policies
(`task_policy`: `on_pause`, `on_cancel`) live in `journal/<task>/policy.json`
and a `task.policy` record; a `job_start` that names its own wins.

**Pause and resume.** `core.gateway_pause(sid)` sends `POST /v1/pause` for
the session and its task once per pause (the pause's mark, `paused_ts`, in
the session's `gateway_paused`; one decision at a time per session:
`gateway/<sid>.lock`), records `gateway.pause` with each job's policy and
outcome, and returns the line the pause shows. Every pause route reaches
it: `core.pause` (typed `pause`, `/check:pause`), the mod's `/check-pause`
(`run.py gw-pause`, whose line goes into the ack, beside `sync-pause`: the
lock makes it one delivery). The task's live gateway jobs that other
sessions started (`_pause_others`): a session whose Claude process has
ended gets its own `/v1/pause` with the task_ref (remembered in
`gateway_paused.others`, resumed with this session); a session still open,
or of unknown liveness (no process table), is left alone (a gateway pause
holds that whole session) and its jobs are named as not paused; a session
paused itself is covered by its own pause. A pause that didn't reach the
gateway (it was away, or the call failed) is delivered by
`gateway_settle(sid)` at a later moment while the session is still paused:
the gate's next denied call, the MCP server's follow thread, `run.py
ingest`, and a prompt only after `_handle_prompt` decided (a typed message
that resumes must never deliver the old pause first: `on_pause interrupt`
would stop the job being resumed). `gateway_resume(sid)` sends `/v1/resume`
when the pause reached the gateway or may have (for this session, then for
`others`); a pause that certainly never reached it is dropped and nothing
is sent (`gateway.resume` with `sent: false`); a resume that can't be
delivered is kept and retried by `gateway_settle`.

**Cancel, stop, status.** `/check:cancel` and `/check:pause --stop-jobs`
stop jobs all at once (`core.stop_jobs_at_once`) from the registry's
records (`jobs.is_live` reads a record's `owner`: a public job dict has no
owner and would read as the own runner's), `/check:stop` one; each says
the outcome: stopped, could NOT be stopped (the gateway's reason), or NOT
confirmed stopped ("outcome unknown, it may still run": the gateway
unreachable, the job lost in a restart, or a state it can't prove).
`recovery.reconcile` classes a gateway job from the state the gateway
reports now (`_gw_job_class`; unreachable: unknown with the last state
seen) and adds "Check first" items for a gateway that isn't available or
runs on another machine than the task's jobs did.

**Events.** `gateway.sync(sid)` keeps `CHECKPOINT_HOME/gateway/<sid>.json`
(epoch, next seq, the jobs, terminals and dialog requests it follows). A
new epoch (or the daemon's `reset`) re-reads the snapshot (`gateway.snapshot`
record: hosts, workspaces, the session's or task's jobs and terminals, no
command text) and marks a followed job it no longer lists `unknown`, `gone`
(`gateway.epoch`); events are taken in seq order, duplicates dropped by
their op, `job.output` once per batch (counts only), and only what concerns
the session's task: its jobs (the registry's and those whose `job.started`
names the session or task), its terminals (`terminal.opened` with its
session, then their owner changes and closing: a takeover is recorded),
its dialog requests (`auth.waiting`, `auth.supplied`: ids and outcome only),
`pause.applied` for the session, and the daemon's own. A gateway that goes
away is recorded once (`gateway.unavailable`). The MCP server follows
events in a thread (a long poll, `CHECKPOINT_GATEWAY_POLL`; off with
`CHECKPOINT_GATEWAY_FOLLOW=0`), and `run.py ingest` syncs too. What crosses
is ids, enums, counts and names: each event kind's allowed fields
(`EVENT_FIELDS`), values of the gateway's own allowlisted characters, and
those scrubbed again (`sanitize.scrub`: a token shape is masked even in an
allowed field); a job's command and cwd are scrubbed and clipped.

**Tests.** `tests/fake_gateway.py` serves CONTRACT-v1 on a Unix socket in a
thread of the suite (knobs for hangs, refusals, restarts, injected events,
signals a job ignores); `tests/gateway_tests.py` (run by the suite after the
recovery tests) covers discovery (Python and sh agree), calls, delegated
jobs (no second start after a lost answer, also when the retry finds the
daemon gone or is refused), pause/resume/cancel/stop through the gateway
(policies, once per pause, late delivery, no late pause before a resume,
other sessions' jobs), unknown states (never stopped, never pruned), the
journal (snapshots, duplicates, order, epochs, scrubbing) and independence
(alone, versions [2] only, a dead daemon, disabled mid-session).
`tests/acceptance_gateway.py` runs both plugins installed the normal way in
the real TUI (the gateway through the TUI harness's e2e route).

## Hosts, authorizations and pause retries (0.7.0)

**Hosts.** `hosts.used(st, registry)` collects the gateway hosts a task used:
its gateway jobs (checkpoint's `job.started` records and registry: the
machine the gateway ran them on), the jobs and terminals the gateway's events
and snapshots tie to its sessions, the gateway tools it called with a `host`
(`sanitize.tool_input` keeps a plain host name from an MCP tool's input; the
gateway's `job_start` runs its command on its `host`, recorded as `localhost`
when the call named none), and a Bash command's or checkpoint's own
`job_start`'s `ssh NAME` (options skipped, `user@` dropped) or
`scp`/`rsync`/`sftp` `NAME:` when the gateway lists NAME (marked inferred).
`recovery.reconcile` asks for one fresh snapshot (`_gateway_view`) and
`hosts.now` puts each used host against it: reachable, auth and runtime as
the gateway reports them (gateway 0.8's words `ok`/`down`/`refused`/`locked`
and 0.9's `true`/`false`/`null` read the same: `gateway.state_word`), a field
it didn't send "not reported", every state "unknown" with the reason when the
gateway isn't available or its snapshot can't be read; never what the
journal last saw passed off as now (that is shown beside it, labelled). An
interrupted operation whose host is unreachable or unauthenticated now is
marked (`Recon.host_mark`, `Op.hosts` from `hosts.call_hosts`), and "Check
first" names the host with the operations that need it ("don't run them
again meanwhile, nor elsewhere"). A changed identity is rank 0 (never cut):
the gateway's last `host.state` in the task's journal says `changed_key`,
`changed_machine` or `wrong_host` with no later event clearing it (a later
`verified`, or `unknown` in the same gateway epoch: its accept path, where it
forgets the identity it checked; a restarted gateway's first `unknown` for
every host confirms nothing; its snapshot says `verified` for any host with
a machine token, also while a change waits for the person), the snapshot
names a host change it waits on, or the machine token it reports now differs
from the one the task's latest snapshot recorded. A used host the gateway no
longer lists is said, and keeps a change its last `host.state` said. The
`reconcile` record keeps each host's enums. The summary's "Jobs & gateway"
lists the hosts the task used and the gateway's last report of each.

**Authorization references** (brief §5, §7). Recorded by name and decision,
never a reason, value or command: the mod's `tool.check` observer spools
Claude Code's verdict for each real call (one with a `tool_use_id`) that a
rule, a hook or an ask decided (`mod.tool.check`: decision, rule, hook; the
reason only into the spool, where `authz.gateway_rights` keeps the gateway's
right names: "ssh on build" from 0.8's "is set to \"ask\"" / "is off for",
"exec on build" from 0.9's policy refusals, by the rule's key or what the
action covers), and the gate spools what came of an ask or a deny
(`mod.tool.answer`: refused, error, result); the observer returns the
verdict beneath unchanged (a failure is skipped: the verdict stands). The
classic PermissionRequest hook adds a permission dialog without mods, linked
to its call by the tool and the hash of its sanitized input (`input_sha` on
every intent; the raw input is never hashed: a guess at a masked secret could
be checked against it); every intent carries the permission mode; a
PermissionDenied is auto mode's classifier. The gateway's own events add its
dialogs (`auth.waiting`/`auth.supplied`), `rights.changed` and
`account.state` (0.9); its tools' `account` names the accounts used.
`authz.collect` groups them (settings rule, gateway right, a hook's
decision, calls put to the person and the answers, the classifier, gateway
accounts and dialogs, rights changes, the modes) for the summary's
"Authorizations" section. A call Claude Code refused (the tool.call came back
refused and no outcome was recorded; the verdict alone isn't enough, a hook
above the observer may change it) is "never dispatched", not an unknown
outcome (`authz.refused_ops`, in recovery and the summary). Before a resume
`authz.check` compares each with now: a settings rule with the user, project
and project-local settings files (its list then is the verdict's decision:
an ask rule the person answered Yes was an ask rule; still in that list:
holds; in another list or gone: flagged), a gateway right with the snapshot
(its host or account still configured; the right's decision now when the
snapshot lists `rights`, by its own name or 0.9's action: ssh = exec, sudo =
elevation, and "not compared" when it lists none for it, as before 0.9; the
host's identity and authentication; a `rights.changed` narrowing or
removal), an account with
its state (`expired`, `revoked`, `reauthentication_required`, `missing` are
flagged) and credential right; a one-time answer, a hook's or the
classifier's decision is said for what it was. The resume context lists what
still holds under "Authorizations" ("carry on within it; no need to ask the
person again") and each revoked or changed one in "Check first" ("don't rely
on it: the person's OK again").

**Pause retries.** Every gateway pause has an id (`core.pause_id`: `ckp-` and
24 hex digits of the session, its pause mark and, for another session's
jobs, that session), the same for the same pause: the mark (`paused_ts`) is
set once, when the session becomes paused, and nothing rewrites a flag that
is there (`state.set_paused`, `env.sh flag pause`, fast.sh's fallbacks), so a
second pause command during the pause (typed again, a queued command's echo,
`pause <session>` or `pause all` from elsewhere, `/check-pause`) is the same
pause: nothing is sent for a delivered one, and an uncertain one goes through
the retry decision below. The id is sent as `pause_id` on `/v1/pause` and `/v1/resume`
(CONTRACT-v1, feature `pause_id`, gateway 0.9; older gateways ignore it).
Before the call goes out the session record says the pause may reach the
gateway (`uncertain`, with the event cursor and the time), so checkpoint
killed during the call never reads as "not sent". A certain failure
(unreachable, refused) is retried as before. An uncertain one, at the next
moment (`_retry_decision`): resent with the same id when the gateway lists
`pause_id` (it answers the first pause's outcomes, `replayed`); taken as
delivered when the gateway's `pause.applied` event for the session (after the
send, same epoch; its pause id when it carries one) shows it
(`gateway.pause_applied`); resent when none of the session's live jobs has
`on_pause interrupt` (a second pause can't signal one twice); else held:
nothing sent, `gateway.pause` with `held` once, the line says which jobs a
second pause could signal again, `/check:doctor` says so, and it is looked at
again at most every 10 s. The resume undoes a held pause there (it may have
applied).

## Not paused-proof on purpose

The gate steers a cooperative agent. It's not a sandbox, and nothing stops
Bash from writing to `.work/` or the state dir.

## The runtime

pixi builds checkpoint's Python env from a copy of `pixi.toml` and
`pixi.lock` with `pixi install --locked --manifest-path <copy>` into
`$CLAUDE_PLUGIN_DATA/envs/<first 12 hex of sha256(pixi.lock)>/` (fallback
`${CLAUDE_CONFIG_DIR:-~/.claude}/plugins/data/check-checkpoint`, Claude Code's
own folder for this plugin; never the plugin folder, which an update replaces
and which may be read-only). `.ready` marks a finished build; after it, hooks
and the MCP server run `.pixi/envs/default/bin/python3` directly, no pixi on
the hot path. A new lock (an update) builds a new env while the previous one
keeps running (the code is stdlib only); envs unused for 7 days are removed.

- **Who builds.** The async SessionStart hook (`env.sh ensure`), and the sync
  SessionStart, which never waits: with no env it starts a detached build,
  says "first-time setup is running" once, and answers fail-safe meanwhile.
  While an update's env builds the previous one runs: the mod's status line
  says "recording · updating its runtime in the background", and its Python
  commands (`/check-save`, `/check-list`, ...) use it (0.7.1; they used to
  say "first run" and refuse).
- **Deadlines.** Every hook that waits for Python (SessionStart, a prompt or
  tool call that needs it, PostToolUse's reminder, Stop) stops it at
  `CHECKPOINT_HOOK_DEADLINE` (20 s; Claude Code's own limits are 30 and
  60 s) and goes on fail-safe, recorded in degraded/<sid>: a hung runtime
  once held the first prompt for Claude Code's 60 s at SessionStart and left
  the hook's shell and the hung child behind (0.7.1). Background moments
  (ingest, snap) are stopped at `CHECKPOINT_BG_DEADLINE` (300 s), and none is
  started after a foreground timeout.
  The MCP launcher starts one too and waits up to 25 s. A hook that finds no
  env and no attempt (an env deleted mid-session) starts one.
- **One at a time.** `mkdir $ENVD.building` is the lock; it holds `pid` (the
  builder) and `pixi` (pixi's pid). A lock whose processes are gone is broken
  at once, under a short `$ENVD.break` mutex with a second look, so two
  processes never both build; a builder killed while its pixi runs keeps the
  lock until pixi exits (pixi's own lock is the backstop).
- **Progress and failure.** pixi's output goes to `install.log`; a failed
  build is said at the next session start with its reason (the log's first
  line starting with `Error`, pixi's summary, else its last line that isn't
  blank: pixi's output ends with a blank line), retried at every session
  start, and shown by `/check:doctor`.
- **Read-only plugin folder.** The copies of `pixi.toml` and `pixi.lock`
  would take the plugin's modes; a build removes old copies first and makes
  the new ones writable, so an attempt after a failed or killed one doesn't
  fail at its copy (it once did, for good).
- **pixi versions.** `requires-pixi = ">=0.68.0"`: `pixi install --locked` was
  tried on copies with a scratch cache for 0.68.0, 0.68.1, 0.72.0, 0.76.0,
  0.79.0, 0.81.0 and 0.82.0 (lock unchanged, Python 3.12 working); 0.67.2 and
  older refuse lock format 7, and with `requires-pixi` they say "update pixi
  to a version that satisfies '>=0.68'". `/check:doctor` places the user's
  pixi in that range.
- **Private files.** `run.py` and every sh script run under umask 077:
  `CHECKPOINT_HOME` 0700, its files 0600 (an older folder is converted once,
  `.modes-v1`). Jobs and git get the user's own umask back
  (`CHECKPOINT_USER_UMASK`); a job's `exit_code` and `gateway_env` are made
  empty (0600) before it starts and filled by its shell.

## The mod and the immediate pause

Mods need Claude Code 2.1.287 (terminal) / 2.1.286 (desktop), and Anthropic
can switch them off; everything that must hold then is in the classic hooks
above. Measured findings on 2.1.301 (scratch config, fake Messages API, the
real TUI in a PTY):

- Commands a mod registers are bare names: `name` takes letters, digits, `_`
  and `-` only (`check:pause` is refused), `/pause` is refused as the
  `/check:pause` skill's own short name, and `/status` as a built-in. So the
  immediate forms are `/check-pause` and `/check-doctor`, and the skills keep
  `/check:*` (they work without mods); when a skill runs, the mod answers it
  first (`command.run` without `next`), so `/check:pause [now]`,
  `/check:doctor` and `/check:status` need no model turn.
- A slash command typed while a turn runs waits in the queue: no
  `prompt.submit`, no queue-door `session.append` for it, and its transcript
  `queue-operation` record has no `content` any more (it had in 2.1.277,
  DESIGN.md Spike 1), so neither the mod nor fast.sh can see a queued
  `/check:pause`: it takes effect when the turn ends. A plain prompt typed
  mid-turn reaches `prompt.submit` at once (with the running `turnId`), so a
  typed `pause` pauses at once.
- `$.turn.abort` ends the running turn but leaves a running Bash call's
  process alive as a background shell (FEASIBILITY-v4 P3). So plain pause
  says "left running"; `now` first runs Python's `watch-fire` (the same kill
  of the guarded call's process tree as `pause now`), then aborts.

Pause responsiveness (`sleep 31.5` running in the foreground Bash, the next
scripted step a second Bash; three runs each on macOS arm64, 2026-10-19; ack =
from Enter to the acknowledgement on screen, which the driver polls every
0.2 s, so 0.20 s means "by the first poll"; the mod's own time is what its
acknowledgement reports):

| Typed | Ack on screen | Mod's own time | Turn | The running command | The queued next call |
|---|---|---|---|---|---|
| `/check-pause` | 0.20-0.21 s | 0.04-0.08 s | aborted | left running (background shell), said so | never ran |
| `pause` (plain text) | 0.20-0.21 s | 0.04-0.06 s | aborted | left running, said so | never ran |
| `/check-pause now` | 0.20-0.42 s (8 runs) | 0.17-0.27 s | aborted after the kill | stopped (gone from `ps` within 1 s) | never ran |
| `/check:pause` (skill) | 6.6-6.8 s, when the turn ended (`sleep 8.5`, typed 2 s in) | 0.02 s | ended normally | ran to its end | **ran** (no queued text to see) |

In every run the pause flag was written, the status line switched from
`check: recording` to `check: paused · ...`, and the next message typed
resumed (flag cleared, status back to recording). The driver is
`scratchpad/upgrade/ck-pack/drv/ckpause.py` of the v4 work (built on the
prototype in `scratchpad/upgrade/tui/`; outside the repo); the repo's own TUI
harness is `tests/tui/`.

Measured for phase 3 (2.1.301, the TUI harness):

- `$.prompt.submit` called inside a command's hook never entered (the
  session is busy with the command until the hook returns, and the call left
  for later is refused): `/check-resume` sets a flag and the session.start
  timer (every 250 ms) submits the resume prompt with the timer's `$`; it
  enters as a plugin prompt, between turns.
- Ending a turn (`$.turn.abort`) resolves a running Bash call at once (it goes
  on as a background shell) with no PostToolUse; the pause's
  acknowledgement therefore takes its list of running calls before it ends
  the turn.
- A prompt typed while a turn runs carries that turn's prompt_id; queued
  this way before a pause, it starts a turn of its own after the abort with
  no new UserPromptSubmit, so it doesn't resume, and the gate holds its tool
  calls.
- A long command output row (`/check:status`) is folded in the transcript.
- The Checkpoints pane (`/check-list` opens it, `ui.render` on `Pane`
  `check-checkpoints`) docks beside the transcript (a tab next to Claude
  Code's Diff pane when that is open); its buttons run Python's diff and
  resume-now and redraw (`$.ui.invalidate`); a click on a pane that doesn't
  hold the keyboard yet may only focus it.

Pause responsiveness with phase 3 (acceptance_recovery.py, macOS arm64,
2026-10-20): `/check-pause` during a streamed reply 0.05-0.08 s (its tool
call never ran), during a long command 0.05 s (left running, named), `/check-pause now`
0.28-0.31 s (the command's processes gone within a second, what was stopped
said).

Prompts that aren't the user's never resume: the mod's `prompt.submit` sees
`isMeta` and the origin (`composer`, `bridge` and `sdk` are the person; a
scheduled trigger, `/loop`, a task notification, a peer or a plugin are not)
and writes `noresume/<sid>` before the classic hook runs; Python (and
fast.sh's fallback) honours a fresh one and removes it. That includes a slash
command a schedule or `/loop` fires (the event carries `command`; the
classic hook alone takes any slash command but `/check:*` as the person's and
resumes): measured 2026-10-19, a one-shot `CronCreate` of a project skill
fired while paused, and the session stayed paused, its Bash call denied
(before the fix it resumed). If neither `env.sh flag noresume` nor the mod's
own `$.fs.write` can write the mark, the prompt is dropped (held back), not
let through unmarked. A pause the mod could only hold in memory (the flag
couldn't be written) is lifted by what would lift the flag: a typed message
or slash command, `/check:resume` included, but not `/check:status` or a
pause keyword. Without mods the classic payload carries no origin, so then
any prompt resumes, as in 0.2.
