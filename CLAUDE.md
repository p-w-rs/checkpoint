# CLAUDE.md: working on checkpoint

The plugin (`plugins/checkpoint/`; plugin name `check`, marketplace
`checkpoint`, install key `check@checkpoint`; the version is in
`plugin.json`). User docs: [README.md](README.md), with
[docs/CONFIGURATION.md](docs/CONFIGURATION.md). How it fits together:
[docs/dev/ARCHITECTURE.md](docs/dev/ARCHITECTURE.md). Tests and what each
release verified: [docs/dev/TESTING.md](docs/dev/TESTING.md), run history in
[docs/dev/TEST-LOG.md](docs/dev/TEST-LOG.md). The scrubber's coverage:
[docs/dev/SCRUBBING.md](docs/dev/SCRUBBING.md). History: the 0.1/0.2 design
and its spikes, [docs/dev/DESIGN.md](docs/dev/DESIGN.md), and the plan the
0.3-0.7 build followed, [docs/dev/PLAN-v4.md](docs/dev/PLAN-v4.md). The
gateway contract: [docs/CONTRACT-v1.md](docs/CONTRACT-v1.md) (identical to
the gateway's copy; `gateway.py` is its client). Changes per release:
[CHANGELOG.md](CHANGELOG.md).
Work in progress: `.work/<task>/TRACKER.md` (this repo uses its own tracker
convention; `.work/` is gitignored here).

## Layout

```
.claude-plugin/marketplace.json     marketplace "checkpoint" -> ./plugins/checkpoint
plugins/checkpoint/
  .claude-plugin/plugin.json        name "check", version (bump it for every release)
  .mcp.json                         MCP server "jobs" (sh hooks/env.sh mcp: the env's python on run.py server;
                                    job_*, task_policy, checkpoint_note; a thread follows the gateway's events;
                                    "alwaysLoad": true and each tool's _meta anthropic/alwaysLoad: 2.1.304
                                    defers plugin MCP tools behind ToolSearch otherwise)
  hooks/hooks.json                  every event -> sh hooks/fast.sh EVENT (exec form; also SessionEnd,
                                    Pre/PostCompact, SubagentStart/Stop, Notification, PermissionDenied,
                                    PermissionRequest, TaskCreated/Completed); SessionStart also runs
                                    `env.sh ensure` (async: builds the runtime); "modules": ["./mod/register.tsx"]
  hooks/fast.sh, watch.sh           POSIX sh: pre-filter, the spool (every event), the gate's fail-safe
                                    half, `pause now` watcher
  hooks/lib.sh, env.sh, pixi.sh     the runtime (env in plugin data by lock hash, build lock, launcher,
                                    doctor, flag writes, `spool`, `state` and `contract-path` for the mod),
                                    the spool (ck_spool, ck_body: metadata only without a runtime,
                                    ck_spool_trim / ck_spool_count: the cap, ck_spool_prune), ck_hash; pixi finder
  hooks/mod/register.tsx, logic.ts  the mod: /check-pause, /check-save, /check-list (+ the Checkpoints
                                    pane), /check-resume, /check-cancel, /check-stop, /check-diff,
                                    /check-doctor (+ Python's lines: `run doctor-py`), its state (normal ones
                                    as a SessionMode footer label, a pause or failure on the status line),
                                    fail-closed tool gate, no self-resume, turn/compaction/denial events to
                                    the spool, the 20 s ingest timer, the 250 ms resume-prompt timer;
                                    /check:pause|save|doctor|status answered without a model turn; a
                                    tool.check observer (permission verdicts) and what came of an ask
  skills/{pause,resume,status,save,list,diff,cancel,stop,complete,export,forget,doctor,help}/SKILL.md   /check:*
  run.py, src/checkpoint/           stdlib only, run by the env's python (the newest from conda-forge); the journal:
                                    journal.py (format, writer, reader), capture.py (spool -> journal,
                                    tasks, lineage, git versions, background ends), sanitize.py, summary.py;
                                    recovery.py (reconcile, list, diff, select, export, retention, lineage);
                                    purge.py (forget <task>, purge); gateway.py (the CONTRACT-v1 client:
                                    discovery, calls, events -> journal); hosts.py (the gateway hosts a task
                                    used, their state now); authz.py (authorization references); jobs.py (two
                                    supervisors: the gateway's jobs through the contract, with the session's
                                    env minus credential names, or the own detached runner; release() for
                                    `forget-job`); cli.py (the terminal helper's
                                    commands, and the internal ones the hooks and the mod call)
  pixi.toml, pixi.lock              the only runtime dependency besides Claude Code (python = "*": `pixi update`
                                    relocks to the newest; requires-pixi is the pixi that made the lock)
  tests/run_tests.py, release_check.py   the suite (it also runs `claude plugin test` and `validate`,
                                    then journal_tests, recovery_tests, gateway_tests, hosts_authz_tests
                                    and, last, ux_tests in its own namespace); is the index the tested tree?
  tests/fake_gateway.py             a fake CONTRACT-v1 daemon (Unix socket, in a thread) for gateway_tests
  tests/ux_tests.py                 the spool without a runtime, status, the crash notice, forget/purge,
                                    trackers off, forget-job
  tests/mod/*.test.ts, world.ts     the mod's tests (`claude plugin test .`) and the world beneath them
  tests/tui/                        the TUI acceptance harness (identical to the gateway's copy)
  tests/acceptance_*.py, accept_install.py   the acceptance scripts in the real TUI (docs/dev/TESTING.md)
  .claude-plugin/types/             written by Claude Code when it loads the mod from this folder
                                    (interactive --plugin-dir); git-ignored
```

## Commands

```bash
cd plugins/checkpoint
pixi run test                     # must stay green; prints "N passed, 0 failed" (also runs the two below)
CHECKPOINT_TEST_SH=/bin/dash pixi run test   # hooks under dash (Debian's sh)
TMPDIR=<a ~180-character folder> pixi run test   # no check may depend on the temp path's length
CLAUDE_CONFIG_DIR=$(mktemp -d) claude plugin test .       # the mod's tests (tests/mod/*.test.ts)
CLAUDE_CONFIG_DIR=$(mktemp -d) claude plugin validate .   # plugin + the mod's source (and ../.. for the marketplace)
claude --plugin-dir .             # try it in a session without installing (writes .claude-plugin/types/)
pixi run status                   # sessions, pause state, jobs (pixi run doctor: the runtime)
pixi run summary [<task>|last]    # a task's recovery summary from the journal (pixi run tasks: the list)
pixi run reconcile <task>|last    # what a resume would check first (pixi run list / diff / export / prune)
pixi run release-check            # before a commit: is the index the tested tree?
pixi exec --spec python --spec pyte python tests/acceptance_journal.py   # one acceptance script (all: TESTING.md)
```

Record each verification run in [docs/dev/TEST-LOG.md](docs/dev/TEST-LOG.md)
(newest first) and keep [docs/dev/TESTING.md](docs/dev/TESTING.md)'s
current state true; this file holds no run diaries.

`pixi run ...` here uses the checkout's own dev env (`.pixi/`). An installed
copy never runs pixi in its folder: `hooks/env.sh` builds the runtime in
Claude Code's plugin data folder (invariant 8). To typecheck the mod:
`pixi exec --spec nodejs --spec typescript tsc -p <a tsconfig outside the
plugin>` with the types the plugin-authoring skill ships (the header of
`claude-code.d.ts` has the tsconfig).

Releasing: bump the version (invariant 10), update CHANGELOG.md, `pixi run
test`, `git add -A` from the repo root, then `pixi run release-check` before
`git commit`, and tag the commit `v<version>`. Only the latest Claude Code,
pixi and Python are supported: before a release, `pixi update` and rerun the
suite. Installs get only what is committed, and a finished tree once had
`hooks/pixi.sh` and `skills/` untracked and its deletions unstaged:
`git commit -a` would have shipped a plugin with no `/check:*` commands
whose jobs server can't start. The check fails on anything untracked,
unstaged, or ignored in a plugin folder (other than `.pixi/`,
`__pycache__/`, `.DS_Store`, `.claude-plugin/types/`); it only reads (`git
ls-files`), and the suite prints its findings as a note.

Tests are hermetic: `CHECKPOINT_HOME`, `CLAUDE_PLUGIN_DATA` and
`CLAUDE_CONFIG_DIR` (the runtime's fallback) are temp dirs (removed at the
end), `CHECKPOINT_GATEWAY_CONTRACT` points the doctor's existence check into
it (never the real `~/.config/gateway`), the runtime tests build envs with a
fake pixi in plugin copies under paths with spaces and non-ASCII letters, and
`claude plugin test`/`validate` run with a scratch `CLAUDE_CONFIG_DIR` and no
network; hooks run through the real `hooks/fast.sh` with `CHECKPOINT_PYTHON`
set to the test interpreter (through a wrapper that counts calls, to prove
the fast path skips Python), `CLAUDE_PID` points at fake Claude processes,
and process tests build fake Claude trees that run commands the way Claude
Code 2.1.29x does (`eval '...'` with `'"'"'`). `CHECKPOINT_GATEWAY_ENV`
points at a temp file (set before anything else, inherited by every server
and hook the suite starts, and checked): jobs source the gateway's env file,
and the real one (`~/.config/gateway/env.sh`) belongs to the user's live
gateway. Never run tests, jobs or the server against it, and never unset the
override in a test except around a call that only computes the path. The
TUI acceptance scripts' sessions get scratch values from tests/tui; a script
that runs checkpoint's CLI itself (`env.sh run ...` from the test process)
must name `CHECKPOINT_GATEWAY_CONTRACT`, `CHECKPOINT_GATEWAY_ENV` and
`CLAUDE_PLUGIN_DATA` too (`Box.ck_env`, `accept_install.cli_env`): unset,
`gateway.discover` looks for the real contract file, and lib.sh takes
`CLAUDE_PLUGIN_DATA` before `CHECKPOINT_ENVD` (a suite check pins it). An
acceptance wait after a submit must pump the PTY (the script's `wait()` or
`wait_main()`, never `FakeAPI.wait_main` or a bare sleep): Claude Code
writes to its terminal synchronously, so a test that only sleeps blocks it
in write() for exactly as long as the test waits. A scripted Bash call
never starts with a long `sleep`: Claude Code 2.1.310 refuses a foreground
command whose first part is `sleep N` with N >= 25 (the scripts run `true;
sleep 300; ...`).
Add a check for every behavior change; mutation-check the important ones
(undo the fix, see the check fail, restore). Mutate a scratch copy of the
checkout, and run copies one at a time: with suites running side by side,
the process checks (`ps fails after the caller's read` ...) fail.

The spike prototype and its drivers (`relay.py`: an interactive `claude` in
a pty that both the user and an agent can type into; `drive_sdk.py`: the
stream-json input path the desktop app uses) were removed after the build.
Recover them with `git show b9420c1:spike/relay.py` etc. if a hook question
needs a new spike.

## Invariants

1. The gate **fails closed while paused, open otherwise.** While the session
   is paused (flags/<sid>), or a pause command for it waits in the queue, a
   tool call is denied whenever Python can't answer: no runtime yet, a crash,
   or past `CHECKPOINT_HOOK_DEADLINE` (20 s, before Claude Code's own 30 s;
   every hook that waits for Python waits through `py_to`, SessionStart,
   PostToolUse's reminder and Stop included (0.7.1: a hung runtime held the
   first prompt for Claude Code's 60 s at SessionStart and left the hook's
   processes behind); background moments stop at `CHECKPOINT_BG_DEADLINE`,
   300 s, and none starts after a foreground timeout);
   fast.sh emits the deny itself ("checkpoint: this session is paused;
   checkpoint's runtime is not ready, so every tool call is held"), Python's
   gate denies on its own errors while paused, and the mod's `tool.call`
   gate has a `.catch` that denies. Not paused, an error still lets the call
   go ahead, never silently: degraded/<sid> records it (also a call on the
   plain fast path while no runtime is ready), and /check:status,
   /check:doctor and the next SessionStart show it. The mark is history:
   doctor says "normal" once a runtime answers, naming the last degraded
   time. Without Python a pause
   command typed is still recorded and a message the user typed still resumes
   (fast.sh's `prompt_fallback`).
2. The common path (not paused, nothing queued) never starts Python. It
   spools every event for the journal (a payload file and one index line,
   `ck_spool`; with a runtime ready no extra process: measured ~1 ms per
   hook). The exceptions, all off the common path or bounded: with no
   runtime ready (`ck_body`: no pixi, a failed setup, a setup running over an
   hour) each record's metadata is cut out with `printf | grep | awk`; a
   payload over `CK_SPOOL_MAX` is cut with `head -c`; passing the spool's cap
   recounts it (`ck_spool_count`: `ls | awk`) and trims (`ck_spool_trim`:
   `sed`, `paste`, `xargs rm`, and with no runtime one `mv` of the index); a
   session start prunes dead sessions' quiet spools (`ck_spool_prune`:
   `find`, `grep`, `ps`). The `.size` count is kept with `read` and
   arithmetic only (leading zeros stripped: sh reads `078` as octal and dies;
   a torn line counts as 0, and the recount runs before any trim). Python
   runs in the background, never waited for, only for a Bash command whose
   text looks like a test or build (`snap`: the git state it runs against)
   and at Stop, PreCompact and PostCompact (ingest); SessionEnd ingests in
   the hook.
3. Hooks never write into the user's projects: not the tracker (Claude's
   own text), not a file beside it, not the clone's `info/exclude` (the
   session note asks Claude to add `.work/` there). The pause-time
   checkpoint goes to the state folder (`core.checkpoint_path`:
   `journal/<task>/checkpoint-<session>.json`, or `checkpoints/<session>.json`
   while the task has no journal yet), scrubbed when written (its calls, jobs
   and interrupted commands, then the whole file once more: a resume puts it
   into Claude's context). Resuming this session (`core.resume_here`) never
   uses another session's checkpoint or one without its `session_id`;
   `/check:resume <tracker>` takes the tracker's newest, whoever wrote it
   (`core.tracker_checkpoint`), by `tracker_key` (the sha256 of the resolved
   tracker path, added after the scrub: the `tracker` path may be masked or
   spelled through a symlink). forget and purge name a tracker folder by the
   session record's real path (`tracker`). `CHECKPOINT_TRACKERS=off`
   (`state.trackers_on`, Python only) drops the
   tracker session note, the reminders and the Stop block; the journal
   records the same. A tracker whose `Session:` names another session is
   that session's: never used for this session's checkpoint,
   deny text or Stop block. One that names no session is used only by a
   session that wrote it (`trackers/<sid>`, recorded by `fast.sh`).
4. `continue: false` is only ever added to a deny (after `MAX_DENIES`
   denied calls), never sent alone: alone it lets the tool run (Spike 3).
5. Trackers live outside `.claude/` (`.work/` by default): Claude Code asks
   before every write under `.claude/`, and no allow rule or hook lifts that.
6. Paths in `fast.sh`/`watch.sh`/`lib.sh`, the mod (`hooks/mod/`) and
   `state.py` must agree (state layout incl. `spool/`, `journal-errors/`,
   `spool-errors/`, `TAIL_BYTES`, `CHECKPOINT_WORK_DIR`,
   `CHECKPOINT_NUDGE_EVERY`, `ENV_KEEP_DAYS`, `CK_SPOOL_MAX` =
   `capture.SPOOL_MAX`, `CK_HASH_MAX` = `capture.HASH_MAX`,
   `CK_SPOOL_KEEP_COUNT`/`_BYTES`/`_DAYS` = `capture.SPOOL_KEEP_*`), and so must the mod's
   `ALLOW_WHILE_PAUSED`, `MAX_DENIES` and `safeId` with Python's (tests
   compare them).
7. Nothing relies on settings.json: no installer, no permission rules, no
   userConfig; the mod registers its commands (`/check-pause`,
   `/check-doctor`) at session start. Mod commands can't carry the `check:`
   prefix and a bare `/pause` is the skill's short name, so the immediate
   forms are `check-<name>`, and `/check:pause` stays a skill (it works
   without mods; the mod answers it when it runs). The hook allows `job_status`/`job_tail`/`job_stop`
   and the journal's `checkpoint_note` (also while paused); it never
   allows `job_start` (it runs any command, so Claude Code's permissions and
   other plugins' hooks must see it). The gateway's and pixi-guard's hooks
   match the job tools by exact name (`mcp__plugin_check_jobs__job_*`):
   renaming the plugin, the `jobs` server or a tool breaks them (a test
   pins the names).
8. No state in the plugin folder (an installed copy is replaced on update,
   and may be read-only): state lives in `CHECKPOINT_HOME` (the journal,
   the pause-time checkpoints and the spool too; private: umask
   077 in run.py and every sh hook, folders 0700, files 0600; a job itself
   runs with the user's umask, `CHECKPOINT_USER_UMASK`). The runtime env
   lives in `$CLAUDE_PLUGIN_DATA/envs/<sha12 of pixi.lock>` (fallback
   `${CLAUDE_CONFIG_DIR:-~/.claude}/plugins/data/check-checkpoint`, never the
   plugin folder), its bytecode in `$CLAUDE_PLUGIN_DATA/pycache`; envs unused
   for 7 days are removed. In a checkout only the dev env `.pixi/` and Claude
   Code's `.claude-plugin/types/` appear (both git-ignored). Settings are
   constants in `state.py`.
9. Hooks and `.mcp.json` start Python through `hooks/lib.sh` (`env.sh`,
   `fast.sh`, `watch.sh`): the env's python directly (`-I -S -X
   pycache_prefix=...`), no pixi on the hot path. pixi only builds it
   (`pixi install --locked --manifest-path <a copy in plugin data>`, never
   `--frozen`), found by `hooks/pixi.sh` with a minimal PATH. One builder at a
   time: `$ENVD.building` holds the builder's and pixi's pids; a lock whose
   processes are gone is broken at once. SessionStart never waits for a
   build (it starts one in the background and says so once). No top-level
   `bin/` (claude.ai and Cowork refuse plugins that have one).
10. `plugin.json` version and `__version__` match (a test checks); bump
    both for every release, or marketplace installs never update.
11. An empty process table means it couldn't be read (`/proc` on Linux,
    `ps` on macOS: `/bin/ps` first, then every other `ps` until one gives
    rows), never "nothing is running": no job is marked vanished, nothing
    is pruned, `pause now` says it couldn't stop the command (and so does
    the queued command's echo later: the session's `unstopped` list, kept
    until it is resumed), liveness falls back to asking the OS about one
    pid. That includes `kill_tree` waiting after SIGTERM (a failed read
    there once meant "all gone": a command ignoring SIGTERM ran on,
    recorded as killed), and `kill_tree` without a table still signals the
    pid it was given. Only a command something of which actually died is
    recorded as killed (`interrupted`); otherwise it is `unstopped`.
    `forget` and `purge` (`purge.py`) refuse without a table too, and never
    remove what a live or paused session or a running job needs.
12. Every pause command waiting in the queue is applied, oldest first, as
    typed (`all`, another session, `now` for every target), at the next
    tool call even in a session that is already paused (the watcher
    applies them as soon as one has `now`); several can be queued before
    one tool call. Each is remembered on its own before it is
    applied (its echo can come while `now` is still killing; `queued_pauses`,
    keyed by enqueue timestamp), until its echo (the same command run as a
    prompt) or another prompt once it is no longer queued, so it is never
    applied twice.
13. Jobs follow the gateway only when its artifacts are there, and fail
    open. `jobs.gateway_env()` passes the env file (`state.gateway_env_path()`:
    `CHECKPOINT_GATEWAY_ENV`, else `~/.config/gateway/env.sh` of the
    password database's home) to the job's shell only if it is a regular
    file (lstat, no symlink) the user owns, not group/other-writable, in a
    folder the user owns, not group/other-writable; else `refused: <why>`.
    `jobs.WRAPPER` sources it in a subshell first and applies it only if
    that printed nothing on stderr and survived (dash exits on a sourced
    file's syntax error; bash applies half of it); else it logs one line
    and runs the job without it (`failed`). Its result goes to
    `jobs/<id>/gateway_env`, then the registry's `gateway_env`. Sourcing
    comes after `job_env()` (checkpoint's pixi activation is already gone).
    A job the gateway runs gets the contract's `env` only when the gateway
    lists the feature `job_env`: `jobs.gateway_job_env()`, `job_env()` minus
    every credential name (`jobs.credential_name`: Claude Code's own
    credential variables, as the gateway's managed shell and Claude Code's
    own subprocess list remove them, and whatever `sanitize.sensitive_name`
    calls sensitive except `jobs.NOT_SECRET_NAMES`: the ssh agent's socket
    and pid, the askpass programs), with git's config variables as one unit
    (`_git_config_env`: the `GIT_CONFIG_COUNT`/`KEY_<n>`/`VALUE_<n>` pairs
    that hold no credential, `jobs.git_config_secret`, numbered again;
    `GIT_CONFIG_PARAMETERS` whole or not at all: git stops on a count whose
    pair is missing); checkpoint keeps no copy of it.

14. **The mod never weakens the classic path, and fails closed.** Its
    `tool.call` gate denies while paused (but what the Python gate allows)
    and its `.catch` denies; its `prompt.submit` only marks non-user prompts
    `noresume` (slash commands too: a schedule or `/loop` fires them), holds
    one back if the mark can't be written (env.sh, then `$.fs.write`), and
    never drops a typed prompt; every pause it makes is the
    same `flags/<sid>` file fast.sh and Python read (`env.sh flag pause`,
    written whole, 0600), so with mods off or Python missing the pause holds.

15. **The journal never says more than it knows.** A checkpoint is
    reported saved only after its fsync returned (`journal.Writer.append(...,
    sync=True)`; F_FULLFSYNC on macOS). Records are sanitized before they are
    written (`sanitize.py`; never file contents: a Read, Grep, Glob, WebFetch
    or WebSearch result is its paths, counts and sizes, a command's output is
    kept only for tests, builds and checks; never edit strings, an unknown
    tool's raw input, or an unknown tool result's values; names like
    PGPASSWORD/APITOKEN count as sensitive; a failed command that isn't a
    test, build or check keeps only Claude Code's first line, `failure_mark`:
    the exit code or an interrupt; a failed non-Bash tool its first line).
    Spool payloads are deleted only after the journal write that holds them
    succeeded and was fully synced (F_FULLFSYNC on macOS); the other ways a
    payload goes before a journal write are said: the cap drops the oldest
    after a `recording.gap` record naming them (`what: dropped`), a dead
    session's spool quiet for `CK_SPOOL_KEEP_DAYS` is pruned, and with no
    runtime a payload is never kept (metadata only, `payload_dropped`, one
    `recording.gap` `what: payloads` per ingest). A failed write leaves
    the spool as it was and is surfaced (journal-errors/, spool-errors/: hook output once,
    the status line, /check:status, /check:doctor), never silent; on a full
    disk through the session's reserve (`.reserve.<sid>`, `RESERVE_BYTES` =
    `CK_RESERVE_BYTES`, armed at SessionStart and after a recovery: renamed
    into place, overwritten in place), and once writes work again it stays on
    record (`.last`; for lost spool records a `recording.gap` record, named in
    the summary's header). Readers
    stop at the first bad CRC or seq gap and never rewrite; a writer starts a
    new segment after a bad tail and refuses to write behind a gap (it checks
    every segment's content CRC, so damage at rest in an older one counts).
    An intent without an outcome is "outcome unknown" in every summary, never
    done; a call whose work went on in the background (a Bash
    `backgroundTaskId`, an async Agent) is "started", its result not
    observed, never done or passed; an exit code is recorded only when the
    payload states one; a failed edit is never a written file; a
    validation's HEAD is the one the hook read before it ran (sh, no git),
    its file hashes come later and say how much later. A command recorded
    without its contents (no runtime then) and with no outcome is an unknown
    outcome with a possible external effect (`recovery.effect_of`:
    `unrecorded`), all of them said in one never-cut line; one that returned
    is said as returned, its result unknown (`returned_unrecorded`: listed by
    its command when the PreToolUse was recorded, else in one line), never
    as completed, and in flight at a checkpoint it is an unknown outcome. The
    summary is built from the journal alone (no model).

16. **Recovery reconciles, reads only, and never repeats blindly**
    (`recovery.py`). Every resume route (a typed message, `/check:resume`,
    `/check-resume`, a picked-up task or checkpoint, the session start after
    a crash, every `--resume`, a fork, a compaction) reconciles first and
    records it (a `--resume` after a clean exit says only what changed, and
    nothing when nothing did), and reconciling never writes the user's
    files, the repository or a transcript (selecting a checkpoint restores
    nothing). `/check:resume <ref>` lifts a pause only once `<ref>` names a
    task or checkpoint. Every interrupted
    operation is in one of five classes; a process counts as running a call
    only when the table shows a shell with exactly that command started after
    its intent (a pid or command line alone proves nothing; no table: nothing
    is running or gone for sure); an unknown outcome with an external effect
    (`external_effect`) comes first under "Check first" and is never to be
    run again until checked; nothing says "redo from the start" (resume
    context, tracker template). The resume context never leaves out "Check
    first" or "Interrupted operations" for length (`Recon.text` cuts items:
    never one with an external effect, then what runs; the summary is cut
    first) and lists what is still open (`_scope`: what was rerun fine, what
    a session moved on from and what an earlier resume reported are left to
    the summary; an earlier external unknown keeps one line). Texts point at
    the installed CLI (`state.cli`), never `pixi run`. Files changed outside
    the task are kept and
    said. The management commands (status, list, diff, save, stop, cancel,
    complete, export, forget) never resume and work while paused:
    `transcript.COMMAND_RE`, the mod's `typedResumes` and fast.sh's
    `prompt_fallback` must agree. `/check:cancel` applies each job's
    `on_cancel` (or the policy given) to the task's own-runner jobs, all at
    once, and says exactly what happened to each (the mod holds every tool
    call meanwhile); a finished task picked up again is reopened
    (`task.reopen`); `/check:stop` and the resume context never show a job's
    log (it would reach the model unmasked: `job_tail` is the route the
    gateway masks); exports are rebuilt from records scrubbed
    again, and their first line (`_review`, `recovery.EXPORT_REVIEW`) says
    scrubbing is pattern-based; retention never removes a journal with a live or paused session or
    a running job, and nothing without a process table. A background end
    reported later (`background.result`) is linked to its start, also when
    the start has no outcome at all (an aborted turn), and is said as
    observed later, never as the call's own result.

17. **The gateway is the only supervisor of its jobs, and unknown is never
    done** (`gateway.py`, `jobs.py`, `core.gateway_*`; CONTRACT-v1). A job is
    the gateway's or the own runner's, never both: once a start may have
    reached the gateway (ambiguous, or an answer outside the contract),
    whatever the retry gets, the outcome is unknown with the idempotency key
    and nothing starts in the own runner; only a start every try of which
    certainly did nothing (never connected, or refused as not served:
    `gateway.not_served`, the daemon's 403 `plane` / `not_registered` /
    `peer` / `uid` and 404/405 `no_route`) falls back. **A refusal is the
    gateway's decision** (0.7.2; before, any 403 fell back, so a job the
    gateway refused by policy ran in the own runner): any other refusal
    (403 `policy`, 403 `session`, 409 `paused`, a 404 that isn't
    `no_route`, an unknown code or none) is answered "refused by the
    gateway" with its message, code and rule, starts nothing anywhere, and
    is journaled as `job.started` with `outcome: refused`; a refused stop is
    `stop_failed` and a refused pause is said as refused, never a signal of
    checkpoint's own. `/check:status` calls the gateway available only when its daemon
    answered this caller (`gateway.serves`; a daemon left from another session
    while the gateway plugin is off here refuses it: "doesn't serve this
    session", jobs in the own runner). A gateway job in state `unknown`
    (unreachable, or lost in a restart:
    `gone`) is `is_live` and never `is_final`: never reported stopped or "not
    running", never pruned, and its task's journal is kept, until the person
    releases it: `checkpoint forget-job <job>` (`jobs.release`, named by
    `/check:doctor`), only once checkpoint has seen it unknown for over
    `RELEASE_AFTER_DAYS` (7; `unknown_ts`, stored the first time and cleared
    when the gateway reports on it), after a second look under the state
    lock. `released` is final for retention and not live, and its outcome is
    still said as unknown; nothing releases a job on its own. A pause reaches
    the gateway once per pause (`gateway_paused.mark`); a late delivery
    (`gateway_settle`) happens only while the session is still paused, and a
    prompt settles only after `_handle_prompt` decided (never a late pause
    just before a resume); a pause that certainly never reached the gateway
    is dropped at resume and nothing is sent. The task's jobs of other
    sessions: paused there (by session and task_ref) only when that session
    has ended; an open one's are named as not paused. `/check:pause
    --stop-jobs` and cancel read the registry's records (a public job dict
    has no `owner`). What crosses from the gateway is scrubbed again
    (`gateway._safe`: allowlisted characters, then `sanitize.scrub`). The mod's
    `jobLive` must agree with `jobs.is_live` (a mod test pins it).
    **A pause is never applied twice** (0.7.0): every `/v1/pause` carries the
    pause's id (`core.pause_id`, the same for the same pause: its mark,
    `paused_ts`, is set once when the session becomes paused, and nothing
    rewrites a flag that is there, `state.set_paused` included, so a second
    pause command during the pause, a queued one's echo, `pause <session>` /
    `pause all` from elsewhere or `/check-pause`, sends nothing for a
    delivered pause and sends an uncertain one again only with its own id),
    the session record says "uncertain" before the call goes out, and a pause
    that didn't or may not have reached the gateway is sent again at the next
    moment with the same id: the gateway applies a pause id once (CONTRACT-v1
    `pause_id`). **A gateway without the `pause_id` feature is a contract
    mismatch** (0.9.0; `gateway.NEEDS_FEATURES`, and `ck_contract` in sh):
    checkpoint works alone beside it, as for a `versions` list without 1.
    Nothing reads what only older gateways, Claude Code or checkpoint
    versions wrote or sent.

18. **Hosts: only what the gateway reported** (0.7.0, `hosts.py`). A host's
    reachable / auth / runtime now come from a fresh snapshot; without one
    every state is "unknown" with the reason, a field the gateway didn't send
    is "not reported", and what the journal last saw is labelled as such,
    never passed off as now. The gateway's words (`ok`, `down`, `refused`,
    what its snapshots and events send) and the contract's booleans read the
    same (`gateway.state_word`). A changed identity (the last `host.state` in the
    journal says changed_key / changed_machine / wrong_host with no later
    event clearing it: `verified`, or `unknown` in the same epoch, the
    gateway's accept path; a restarted gateway's `unknown` clears nothing;
    the snapshot's own change; a machine token that differs from the task's
    latest snapshot) is a rank-0 "Check first" item: never cut, also for a
    host the gateway no longer lists. Operations needing a host the gateway
    reports unreachable or unauthenticated are marked, never retried for it
    (a tool's `host`, the gateway's `job_start` included: the journal keeps
    it, `localhost` when the call named none; a command's `ssh NAME` for
    Bash and checkpoint's own `job_start`).

19. **Authorization references: names and decisions only** (0.7.0,
    `authz.py`). Never a reason text, value or command in the journal: the
    reason reaches only the spool, where `gateway_rights` keeps the gateway's
    right names from its fixed wording (anything else is dropped); a rule is
    scrubbed; `input_sha` hashes the sanitized input, never the raw one. The
    mod's `tool.check` observer returns the verdict beneath unchanged and
    records only real calls (a `tool_use_id`). A call is "never dispatched"
    for a refusal only when the mod's `tool.call` came back refused and no
    outcome was recorded (the verdict alone may be changed by a hook above).
    Before a resume a reference is "still holds" only after it was compared
    (settings files, the gateway's snapshot); one that couldn't be compared
    says so (a gateway right the snapshot doesn't list: only its host was
    there to compare); revoked or changed ones go into "Check first". A
    settings rule's list then is its verdict's decision, never what came of
    the call (an ask rule answered Yes was an ask rule).

## Mod gotchas (Claude Code 2.1.301, measured; ARCHITECTURE "The mod")

- Mod commands are bare names (`[A-Za-z0-9_-]`); `/pause` is refused (the
  skill's short name), `/status` (a built-in). Hence `/check-pause`,
  `/check-doctor`; the gateway uses `/gate-doctor` the same way.
- A slash command typed mid-turn is queued with no `prompt.submit`, no
  queue-door `session.append`, and no `content` in its transcript
  `queue-operation` record; a plain prompt typed mid-turn reaches
  `prompt.submit` at once with `turnId`. `$.turn.abort` leaves a running Bash
  process alive (a background shell).
- An immediate command's output row shows once the turn has ended: abort
  first, or the ack waits. `$.ui.log` is one line (a newline draws as a
  replacement glyph). A `prompt.submit` drop puts the text back into the
  prompt box.
- The mod can't see `CLAUDE_PLUGIN_DATA` (Claude Code sets it for hooks and
  MCP servers only; `--plugin-dir` gets `plugins/data/check-inline`):
  SessionStart records the env folder in `rt/<sid>` for it. The mod's
  session.start (and its first status draw) runs before the classic
  SessionStart hook writes `rt/<sid>` (measured, GitHub install test): until
  then `env.sh state` answers with `CHECKPOINT_PLUGIN_DATA` from
  `logic.pluginDataDir` (`<config>/plugins/data/<name>-<marketplace>` from an
  installed copy's cache path, else `<name>-inline`), and the status is
  looked at again 0.3 s and 1 s after the start (0.7.2 drew "recording"
  while a first run built; a two-second build never showed the setup).
- `$.ui.status(text)` takes only the text (2.1.299 and 2.1.301 types; no
  level or kind) and Claude Code draws every plugin status line as a pinned
  notice with a warning sign ("⚠ check: recording", "⚠ gate: ready"). The
  normal states are a dim footer mode label instead: a `ui.render` hook on
  `SessionMode` appends `check: <state>` to `e.props.modes` (drawn at the
  right of the prompt footer; `$.ui.invalidate('ui.render')` when it
  changes; a mod test reads it through `$.ui.mount` of `SessionMode` with a
  bottom hook that returns a Text element). The status line keeps a pause
  and the failures (`logic.statusOf`). TUI tests matching `check: recording`
  in the screen text still find it (the footer row).
- The mod can't read the password database: the gateway's contract file
  (in that home, as `gateway.contract_path`) comes from `env.sh
  contract-path` (0.7.3; it used `$HOME`, so with HOME elsewhere the
  `/check-pause` ack said the gateway wasn't told).
- A `$` call a mod begins after its hook returned is refused: the mod's
  spool events start their `process.run` synchronously inside the hook and
  nobody waits for them. `session.compact` hooks are skipped when `next()`
  gets messages without `text` and `toolUses` (tests must pass whole ones).
- `$.prompt.submit` inside a command's hook never enters (the session is
  busy with the command; the call left for later is refused): `/check-resume`
  sets a flag and the session.start timer (250 ms) submits with its own `$`.
  `$.turn.abort` resolves a running Bash call at once with no PostToolUse
  (it goes on as a background shell): take the list of running calls before
  ending the turn, and expect only an intent (and later a task notification)
  in the journal. A prompt typed mid-turn carries the turn's prompt_id (the
  journal's prompt op adds the text's hash); queued before a pause, it starts
  a turn after the abort with no new UserPromptSubmit. Task notifications
  (`<task-notification>`, a background command's or subagent's end) reach
  UserPromptSubmit, idle or mid-turn. A long command output row is folded in
  the transcript (TUI tests read /check:status the way the mod gets it). The
  Checkpoints pane docks beside the transcript from 110 columns (asked: at
  any width), as a tab next to Claude Code's own Diff pane when that is
  open; a click on a pane that doesn't hold the keyboard yet may only focus
  it (the TUI test clicks again).
- `tool.check` (0.7.0, measured in the TUI, `tests/acceptance_authz.py`):
  a mod hook sees every real call's verdict with its `tool_use_id` (a
  settings rule's allow and deny carry `rule` as written; an ask before the
  dialog); the classic PermissionRequest hook fires when the dialog is
  drawn (no `tool_use_id`). A call a deny rule refused, and one the person
  declined, come back to `tool.call` as `{result, isError: true}` (not
  `{deny}`) with no PostToolUse and Claude Code's own text ("Permission to
  use Bash with command ... has been denied." / "The user doesn't want to
  proceed with this tool use..."): the mod spools `rejected` from that text,
  never the text. The Bash dialog's options on 2.1.301: 1 Yes, 2 always allow
  the folder, 3 switch to auto mode, 4 No (Escape is No too); a key pressed
  before the dialog settles is lost (press again).
- TaskCreate/TaskUpdate exist only with `CLAUDE_CODE_ENABLE_TODO_TOOLS=1`
  for the current models on 2.1.301 (the interactive default has no task
  tools); subagents (`Agent`) always run in the background.
- `claude plugin test` runs `tests/mod/*.test.ts` with nothing beneath the
  mod but the test's own hooks: every op (`fs.*`, `process.run`, `env.get`,
  `session.id`, `turn.abort`, `ui.status`, ...) is answered `{ value }` by
  the test, and the test's `$` has events only (no `$.session.id()`; use the
  mock clock's `settle()` to let unawaited work finish). Since 2.1.309 the
  kit's `$.tool.call` checks the call's permission again inside the call
  (a test sets the verdict that stands for the person's answer); the mod's
  tests are written for that and fail on an older Claude Code.

## Conventions (shared with the sister project)

The user built [gateway](https://github.com/p-w-rs/gateway) (checked out at
`../gateway`) the same way: hand-rolled stdio MCP server, shell pre-filters
in front of Python hooks, `pixi run test`, stdlib-only Python, macOS and
Linux (POSIX sh that works under dash and with BSD and GNU tools). Only the
latest Claude Code and pixi, and the newest Python from conda-forge: no
backwards compatibility.

Gotcha: if gateway is installed in your session, its PreToolUse hook asks
before a Bash command that runs `curl`, `wget` or another network command
(gateway 0.11's default; `[network]` in its gateway.toml changes it), and a
checkpoint job running one is refused; if a commit message that mentions
them is asked about, use `git commit -F file`.

## User preferences

- Show a plan and stop before installing anything or changing the user's
  settings.
- Plain, concise docs. No license file and no license wording (don't call
  the project open source).
- A hobby project: no CI/CD (no GitHub Actions) and no enterprise-style
  release process.
- Docs stay generic: no employer, work logins, emails, internal hosts,
  personal paths, pids or the user's live state.
