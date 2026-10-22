# CLAUDE.md: working on checkpoint

The plugin (`plugins/checkpoint/`, v0.7.3; plugin name `check`, marketplace
`checkpoint`, install key `check@checkpoint`). User docs and the install
route: [README.md](README.md). How it fits together:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Why, the user's decisions, and
the spike findings it rests on: [docs/DESIGN.md](docs/DESIGN.md). The upgrade
in progress: [docs/PLAN-v4.md](docs/PLAN-v4.md) (authoritative where it
differs from DESIGN.md; phase 1, packaging and the mod skeleton, is done:
0.3.0; phase 2, the durable journal, capture and the recovery summary: 0.4.0;
phase 3, recovery controls (reconciliation, resume/cancel/stop, checkpoint
list/diff/select/complete/export, retention, lineage): 0.5.0; phase 4, the
gateway integration (CONTRACT-v1 client, jobs delegated to the gateway,
per-job pause policies, unknown states): 0.6.0; the last build step
(reconciliation against the gateway's hosts, authorization references,
idempotent pause retries with pause ids): 0.7.0; the acceptance fixes:
0.7.1; a gateway refusal never runs a job in the own runner: 0.7.2; the
GitHub install test's fixes (a truthful first status, the gateway's contract
file in the password database's home for the mod too, the state's normal
forms in the footer): 0.7.3, unshipped), with
the gateway contract
in [docs/CONTRACT-v1.md](docs/CONTRACT-v1.md) (identical to the gateway's
copy; `gateway.py` is its client).
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
                                    `env.sh ensure` (async:
                                    builds the runtime); "modules": ["./mod/register.tsx"]
  hooks/fast.sh, watch.sh           POSIX sh: pre-filter, the spool (every event), the gate's fail-safe
                                    half, `pause now` watcher
  hooks/lib.sh, env.sh, pixi.sh     the runtime (env in plugin data by lock hash, build lock, launcher,
                                    doctor, flag writes, `spool`, `state` and `contract-path` for the mod),
                                    ck_spool/ck_hash; pixi finder
  hooks/mod/register.tsx, logic.ts  the mod: /check-pause, /check-save, /check-list (+ the Checkpoints
                                    pane), /check-resume, /check-cancel, /check-stop, /check-diff,
                                    /check-doctor, its state (normal ones as a SessionMode footer label, a
                                    pause or failure on the status line), fail-closed tool gate, no self-resume,
                                    turn/compaction/denial events to the spool, the 20 s ingest timer, the
                                    250 ms resume-prompt timer; /check:pause|save|doctor|status answered
                                    without a model turn; a tool.check observer (permission verdicts) and
                                    what came of an ask (mod.tool.check / mod.tool.answer to the spool)
  skills/{pause,resume,status,save,list,diff,cancel,stop,complete,export,doctor,help}/SKILL.md   /check:*
  run.py, src/checkpoint/           Python 3.12 stdlib only, run by the env's python; the journal:
                                    journal.py (format, writer, reader), capture.py (spool -> journal,
                                    tasks, lineage, git versions, background ends), sanitize.py, summary.py;
                                    recovery.py (reconcile, list, diff, select, export, retention, lineage);
                                    gateway.py (the CONTRACT-v1 client: discovery, calls, events -> journal);
                                    hosts.py (the gateway hosts a task used, their state now); authz.py
                                    (authorization references: recorded, summarized, checked before a resume);
                                    jobs.py (two supervisors: the gateway's jobs through the contract, or the
                                    own detached runner)
  pixi.toml, pixi.lock              the only runtime dependency besides Claude Code
  tests/run_tests.py, release_check.py   the suite (it also runs `claude plugin test` and `validate`,
                                    then tests/journal_tests.py, tests/recovery_tests.py,
                                    tests/gateway_tests.py and tests/hosts_authz_tests.py in its own
                                    namespace); is the index the tested tree?
  tests/fake_gateway.py             a fake CONTRACT-v1 daemon (Unix socket, in a thread) for gateway_tests
  tests/hosts_authz_tests.py        0.7.0: hosts against the gateway now, authorization references, pause
                                    retries (SIGINTs counted); run by the suite after gateway_tests
  tests/mod/*.test.ts, world.ts     the mod's tests (`claude plugin test .`) and the world beneath them
  tests/acceptance_journal.py       brief 14.3 in the real TUI (tests/tui): SIGKILL mid-task, recovery
  tests/acceptance_recovery.py      brief 14.4 and 14.5 in the real TUI: pause routes, no self-resume,
                                    crash after a remote write, --resume, /compact, /clear
  tests/acceptance_authz.py         Claude Code's permission verdicts in the real TUI (settings rules allow /
                                    deny, an ask answered Yes and one No) -> the journal, summary, reconcile
  tests/acceptance_gateway.py       both plugins installed (git-source marketplaces; the gateway through
                                    tests/tui's e2e route) in the real TUI: gateway jobs with on_pause
                                    interrupt/continue/suspend, /check-pause, resume, /check-cancel
  tests/acceptance_independence.py  brief 14.2: either plugin alone, both, either disabled, an incompatible
                                    contract, upgrades (checkpoint's view of the gateway read from inside the
                                    session: `ck_view`, a scripted Bash call; the daemon serves by ancestry)
  tests/acceptance_durability.py    brief 14.11: concurrent sessions, duplicate events, a hung runtime (the
                                    hook deadline), a stopped gateway daemon, a full disk (macOS disk image),
                                    an upgrade under a live job, the per-call overhead
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
pixi exec --spec python=3.12 --spec pyte python tests/acceptance_journal.py   # brief 14.3 in the TUI (~25 s)
pixi exec --spec python=3.12 --spec pyte python tests/acceptance_recovery.py  # 14.4 + 14.5 + the pane (~4 min; --only a,b,c,d)
pixi exec --spec python=3.12 --spec pyte python tests/acceptance_authz.py --root <scratch dir>  # verdicts (~1 min)
pixi exec --spec python=3.12 --spec pyte python tests/acceptance_gateway.py --tmp <plain path> \
    --pixi-cache <dir> --gateway ../gateway   # both plugins, gateway jobs (~1 min; the gateway's env build first)
pixi exec --spec python=3.12 --spec pyte python tests/acceptance_independence.py --tmp <dir> --pixi-cache <dir>  # 14.2 (~2 min)
pixi exec --spec python=3.12 --spec pyte python tests/acceptance_durability.py --tmp <dir> --pixi-cache <dir>   # 14.11 (~2 min)
```

Last full run (2026-10-21, 0.7.3 unshipped, the stall guard reverted, the
harness fixed): the suite 961 passed (macOS, sh = bash 3.2), under dash
955; `claude plugin test .` 61 passed; `validate` passes for the plugin
and the marketplace. The verification's guard (9d09797: no status change
while a prompt is being submitted, `submitting`) is reverted: the 60 s
stall of the first prompt after `claude --resume` in acceptance_recovery
part a was the test. `last_request` waited through `FakeAPI.wait_main`,
which sleeps without reading the PTY, and Claude Code writes to its
terminal synchronously: its main thread blocked in write() on the
harness's PTY (a `sample`: `__write_nocancel`) until the test read again,
so the stall always lasted as long as the test's wait (75 s with a 75 s
wait). An independent re-verification: with `last_request` reading the
terminal, the code before the guard passed part a 3 of 3 with the status
clear landing inside the hook; the guard with the old wait still stalled 6
of 17 times. Nothing to report upstream. Every acceptance wait after a
submit now reads the terminal (`wait_main` in acceptance_recovery and
acceptance_journal; acceptance_gateway's already did, and independence and
durability import it). TUI (macOS 2.1.301, installed copies, scratch
config, fake API): recovery part a 4 of 4 runs 19/19 (the first prompt
after the restart reached the fake API 0.39-0.49 s after Enter; the first
run's script had no timer yet), then `acceptance_recovery` 40/40 (0.44 s there) and
`acceptance_journal` 20/20. On the Debian 13 arm64 VM (Claude Code
2.1.304), the final tree (dbea426, with gate 0.9.1 at ee2f679): the suite
967 passed / 0 failed under bash, 955 / 0 under dash; `acceptance_journal`
20/20, `acceptance_recovery` 40/40, `acceptance_gateway` 27/27 (earlier,
at cc4b3d6: `acceptance_authz` 18/18, `acceptance_independence` 49/49).
The VM's first recovery run failed part b's "the scheduled prompt fired"
once: Claude Code 2.1.304 fires a one-shot cron task whose minute is :00 or
:30 up to 90 s early (`oneShotMinuteMod` 30, `oneShotMaxMs` 90 s), and that
run's target was 02:00, so it fired before the test took its request count.
Part b now never targets such a minute and takes the count right after the
CronCreate (pausing and reloading call no model). Not run this round:
authz, independence, durability and gateway on macOS on the final tree, a
TMPDIR of ~180 characters.
Before (2026-10-21, 0.7.3, the GitHub install test's fixes): the
suite 961 passed (macOS, sh = bash 3.2), under dash 955 (two earlier dash
runs failed only the fast-path overhead timing: sequential medians,
PreToolUse 47.6 -> 60.1 ms, with the machine at load 8-10 from other users'
processes; interleaved on a scratch copy, 60 runs each: +0.2 ms PreToolUse
and PostToolUse; the third run +0.9 ms); `claude plugin test .` 61 passed (new
`tests/mod/status.test.ts`: 15); `validate` passes for the plugin and the
marketplace; the mod typechecks (tsc, 2.1.301's shipped types). The fixes:
the first status draw read `rt/<sid>` before the classic SessionStart hook
wrote it, so it said "recording" while a first run built (and a two-second
build never showed the setup): now, not ready by the files or not recorded
yet, `env.sh state` answers (`CHECKPOINT_PLUGIN_DATA` from
`logic.pluginDataDir` until the env is recorded), with looks 0.3 s and 1 s
after the start; the mod's pause looked for the gateway's contract file
under `$HOME` (the gateway and Python use the password database's home), so
with HOME elsewhere the ack said the gateway wasn't told: now `env.sh
contract-path` (Python decides when the shell can't say); the first-use
notice says "usually under a minute" (no fixed time); `$.ui.status` has no
level (2.1.299 and 2.1.301 types) and Claude Code draws every plugin status
line with a warning sign, so the normal states (recording, setting up,
updating) are a dim footer mode label (a `SessionMode` render hook) and the
status line holds only a pause and failures (setup FAILED and no pixi are
new states there). Mutations (each on a scratch copy; 3 of 3): the old
first draw (an unrecorded env counted as recording, no shell: 5 mod tests
failed), the contract file under `$HOME` again (3), every state on the
status line (9). TUI (macOS 2.1.301, scratch config, fake API; a scratch
script on `tests/tui`, sampling every 0.1 s from the spawn): a git-snapshot
install with a warm pixi cache: `check: setting up its runtime (first run);
pausing works` (footer, no warning sign) at 0.44 s, the env ready at 0.77 s,
`check: recording` at 0.77 s; `--plugin-dir` with an empty pixi cache
(downloaded): setting up at 0.67 s, ready at 2.17 s, recording at 2.59 s;
both 6/6 (the notice's wording, `/check-pause` on the warning line with no
footer label, a typed message back to the footer label). Not run this
round: the Debian VM, the acceptance scripts (their `check: recording` and
`check: paused` matches still hold: footer row and status line), a TMPDIR
of ~180 characters, and the gateway's acceptance_install (its status-row
parser expects `⚠ check: recording`).
Before (2026-10-21, 0.7.2, the gateway-refusal security fix): the
suite 955 passed (macOS, sh = bash 3.2), under dash 949 (one earlier dash run
failed only the fast-path overhead timing, 11.6 ms against 10, with another
agent's TUI runs loading the machine; the rerun passed); `claude plugin test
.` 46 passed; `validate` passes for the plugin and the marketplace. The fix:
`jobs._start_gateway` ran ANY 403 from `POST /v1/jobs` in the own runner, so
a job the gateway refused by policy ran anyway; now only the gateway
daemon's not-served codes fall back (`gateway.NOT_SERVED`: 403 `plane`,
`not_registered`, `peer`, `uid`; 404/405 `no_route`, read from the
gateway's `daemon/planes.py` and `server.py`), every other refusal is
"refused by the gateway" (its message, code and rule), started nowhere,
journaled `job.started` with `outcome: refused` and listed as REFUSED in the
summary; a refused stop is `stop_failed` "refused by the gateway" (no KILL
after it, never a signal of checkpoint's own) and a refused pause says so;
`gateway.serves` uses the same codes. Also: acceptance_journal and
acceptance_recovery end what their commands start (`accept_install`:
`note_marked` / `kill_marked` / `kill_started`: the marked shell, its
descendants and its orphaned process group; Linux runs left `sleep 300`
behind) and check it. Mutations (each on a scratch copy; 2 of 2): the old
fallback condition back (7 named checks failed: 403 policy, its journal
record and summary line, an unknown code, no code, 403 session, a 404
no_host; a 409 paused was refused before too); `kill_marked` back to the
marked processes only (both reaper checks failed; that run's two orphaned
`sleep 300` were then killed by pid). TUI on macOS (2.1.301):
`acceptance_gateway` 27/27 with the gateway checkout of the day (check 0.7.2,
gate 0.9.0, installed copies), `acceptance_journal` 20/20 and
`acceptance_recovery` 40/40 (`--plugin-dir` copies; one new check each:
nothing the scripted commands started left running). Not run this round: the
Debian VM (where the leftovers were seen), the other TUI scripts, and a
policy refusal from the real gateway's daemon in a session (the codes and
`rule` were read from its source; the fake gateway sends them).
Re-runs (2026-10-21, 0.7.1, after the gateway's acceptance fixes): the suite
939 passed (macOS, sh = bash 3.2), under dash 933; on the Debian 13.7 VM 945
under sh = dash and 945 under bash (a fresh copy of the tree); TUI on installed
copies: `acceptance_durability` 29/29 on macOS against the fixed gateway
checkout (the stopped daemon: the first Bash 4.5 s after the prompt, was 12.0
s, ~4.1 s of it the gateway mod's session-start `gated start` the test stops
gated during; with gated stopped after the session start, ten Bash calls took
0.216 s each, median, against 0.220 s with it running, and 0.224 s after 35 s
idle: no per-call delay; that config has no vault), on the VM
`acceptance_journal` 19/19 and `acceptance_recovery` 39/39 (38/39 before
`accept_install.cli_env`: the run's own scratch `CLAUDE_PLUGIN_DATA` reached
`env.sh run status`, exit 75; the new suite check failed with the old
behaviour put back on a scratch copy).
Before (2026-10-21, 0.7.1, the acceptance fixes; PLAN-v4 phase 5): the
suite 938 passed (macOS, sh = bash 3.2), under dash 932; on the Debian 13.7
arm64 VM (Claude Code 2.1.304, under ~/fix-acc-ck) 944 under sh = dash and 944
under bash; `claude plugin test .` 46 passed; `validate` passes for the plugin
and the marketplace. The fixes: every hook that waits for Python has the
deadline (SessionStart, PostToolUse's reminder and Stop waited for Claude
Code's own timeout: 60 s at a hung runtime's session start, and the hook's
processes were left behind), background ingests stop at
`CHECKPOINT_BG_DEADLINE`; `/check:status` says whether the gateway's daemon
serves this session (`gateway.serves`), not only that it runs; the summary's
header names the last checkpoint (a resume context under long paths lost
it with Evidence); the mod counts an update's older env as ready (status
line and its Python commands); the jobs server is `alwaysLoad` (2.1.304
deferred its tools behind ToolSearch); a full disk's recording failure stays
on record (reserves, `.last`, `recording.gap`); env.sh mcp's no-pixi reason
no longer races the build it starts. Mutations (each on a scratch copy; 13
of 13 made a named check fail): SessionStart and PostToolUse without the
deadline, Stop starting a background ingest after a timeout, no background
deadline, no serving check, the checkpoint id out of the header, no
alwaysLoad, the sh and the Python reserve ignored, a recovery just removing
the marker, recording.gap not journaled, gaps not in the header; in the mod:
an older env counted as missing. TUI on installed copies (macOS 2.1.301):
journal 19/19, durability 29/29, independence 49/49, gateway 27/27, recovery
39/39, authz 18/18 (the acceptance scripts' CLI calls from the test process now
always name the scratch contract and env file: `Box.ck_env`,
acceptance_recovery's `Env.cli_env`; unset, checkpoint looks for the real
`~/.config/gateway/run/contract.json`); on the VM (2.1.304): journal 19/19, independence 49/49,
durability 23/23 (no disk image there; the first call 3.7 s with a 3 s
deadline, was 60.3-60.5 s), and a tools probe (19 tools with the job tools;
13, deferred, without alwaysLoad). Not re-run: the gateway's
`acceptance_install.py` (its update part saw the old status line on the VM;
the mod test and its mutation cover the new one).
Before (2026-10-21, 0.7.0 after its verification fixes): the suite
915 passed (macOS, sh = bash 3.2), under dash 909, and 915 with a
181-character TMPDIR; `claude plugin test .` 44 passed; `validate` passes for
the plugin and the marketplace. The fixes: a second pause command during the
same pause (typed again, a queued one's echo, `pause <session>` / `pause all`
from elsewhere) made `set_paused` rewrite the flag, so `sync_flag` took a new
mark: a second `/v1/pause` with another pause id and a second SIGINT (the
retry decision skipped); the gateway's own `job_start` was read by its
command, never its `host` (and the sanitizer dropped that host); an ask rule
the person approved was "allow then"; a restarted gateway's `unknown`
identity cleared a host-key change, and a host no longer listed lost it; a
gateway right the snapshot doesn't list "still held". TUI: the verifier's
ask-rule run (a settings ask rule answered Yes in Claude Code 2.1.301's
dialog, settings unchanged) now holds, nothing flagged. Mutations of the
fixes (each on a scratch copy, one at a time, each made a named check fail;
7 of 7): `set_paused` rewriting a flag that is there; the gateway's
job_start read by its command; its host not journaled; a rule's list then
taken from the call's answer; `unknown` in any epoch clearing a change; a
host no longer listed losing its change; an unlisted right holding. Not run
this round: the Debian VM, the other TUI acceptance scripts.
Before (2026-10-20, 0.7.0, the last build step): the suite 903
passed (macOS, sh = bash 3.2), under dash 897, and 903 with a 195-character
TMPDIR; on the Debian 13.7 arm64 VM (pixi 0.82.0, Claude Code 2.1.304, git
from `pixi exec --spec git`, under ~/ck-final) 909 under sh = dash and 909
under `CHECKPOINT_TEST_SH=bash`; `claude plugin test .` 44 passed (the new
`tests/mod/authz.test.ts`: 5); `validate` passes for the plugin and the
marketplace; the mod typechecks (tsc with the shipped types). TUI:
`tests/acceptance_authz.py` 18 of 18 (~20 s; settings rules allow and deny,
an ask answered Yes and one No through Claude Code 2.1.301's real permission
dialog: every verdict, the two answers, both PermissionRequest records, the
summary, never-dispatched refusals, the rule still holding then flagged once
removed). Its first runs found two things the suite's fakes had wrong: a call
a deny rule refused, and one the person declined, come back to `tool.call` as
an error result (not `{deny}`), so the mod now marks `rejected` from Claude
Code's own refusal text; and the Bash dialog's option 3 is "switch to auto
mode" (No is 4, or Escape). Mutations of 0.7.0 (each on a scratch copy, one
at a time, each made a named check fail; 24 of 24): a retry that always
resends (the interrupt job got a second SIGINT); no "uncertain" record before
the call; the gateway's `pause.applied` evidence ignored; the `pause_id`
feature ignored; no pause id sent; a blocked host never read as blocked;
identity changes ignored; a later snapshot clearing an identity change; the
journal's last state passed off as now when the gateway is away; 0.9's
booleans not read; a reason text kept in the journal; another hook's text
taken for the gateway's; a host removed from the gateway not flagged; a
changed settings rule kept as holding; a refusal in Claude Code's words not
believed; an error after an ask taken as declined; refused calls listed as in
flight; the raw input hashed; an account's expired state not flagged; an
operation's host not marked; in the mod: the observer recording queries,
changing the verdict, no `rejected` mark, no answer after an ask.
Before (0.6.0 after its verification fixes): the suite
(with `tests/gateway_tests.py`, now run by it) 854 passed (macOS, sh = bash
3.2), under dash 848, and 854 with a 169-character TMPDIR; on the Debian 13.7
arm64 VM (pixi 0.82.0, Claude Code 2.1.304, git from `pixi exec --spec git`)
860 under sh = dash and 860 under `CHECKPOINT_TEST_SH=bash`; `claude plugin
test .` 39 passed; `validate` passes for the plugin and the marketplace; the
mod typechecks. TUI: `tests/acceptance_gateway.py` 27 of 27 (~30 s after the
gateway's env build; `/check-pause` acked in 0.46 s with each gateway job's
outcome), `tests/acceptance_recovery.py` 39 of 39 (acks 0.09 / 0.05 / 0.30 s),
`tests/acceptance_journal.py` 19 of 19. Two TUI-harness facts found on the way:
a test that waits without pumping the pty stalls the session (Claude Code
writes to its terminal synchronously; with the gateway's mod drawing too, a
job_start round trip "took" 120-400 s until the test read the screen again:
`acceptance_gateway.wait_main` pumps), and `/check:status`'s long output
scrolls its head out of `all_text()` (read its end, the summary's "Jobs &
gateway"). The 0.6.0 verification found the summary's own note cut off under
a long state path (`summary.build`'s fit loop gave up after 4 rounds and cut
the note), so `recovery_tests`' length check failed with a long TMPDIR only;
the body is cut now, never the note, and a check pins it with a ~230-character
CLI pointer. Mutations of the 0.6.0 verification fixes (each on a scratch copy,
each made a named check fail): a start whose first try may have reached the
gateway handed to the own runner after an unreachable or 403 retry; the
prompt hook settling the gateway before it decided (a late pause before the
resume); unknown + gone counted as final (and not live): stop, cancel and
pruning; `--stop-jobs` reading `jobs.status`' public dicts; no pause of the
task's jobs that an ended session started; pausing them while that session
is open; `gateway._safe` without the scrub; a resume sent for a pause that
never reached the gateway; the summary's old fit loop and hard cut.
Before (0.5.0 after its verification fixes): `pixi run
test` 740 passed (macOS, sh = bash 3.2), under dash 734 passed, and 740 with
a 179-character TMPDIR (`TMPDIR=<long path> pixi run test`: two checks used to
depend on the temp path's length; paths in texts are now clipped in the
middle); `claude plugin test .` 37 passed; `claude plugin validate` passes for
the plugin and the marketplace; the mod typechecks (tsc with the shipped
types); on Linux (Debian 13.7 arm64 VM, pixi 0.82.0, Claude Code 2.1.304, git
from `pixi exec --spec git`) 746 passed under sh = dash and 746 under
`CHECKPOINT_TEST_SH=bash`. `tests/acceptance_recovery.py` (TUI,
`--plugin-dir`): 39 of 39, ~3.5 min (acks: streamed reply 0.08 s, long command
0.06 s, `now` 0.28 s); `tests/acceptance_journal.py`: 19 of 19, ~21 s.
Before those fixes: 703 / 697 on macOS (701 / 2 failed with a long TMPDIR),
709 / 709 on the VM; one earlier dash run failed `(a) queued 'pause
now' echo names the killed command`, a race in that test (the echo raced the
watcher's record of the kill), which now waits for the record.
Mutations of the verification fixes of 0.5.0 (each on a scratch copy, each
made a named check fail): `/check:stop` with the log's last lines back (the
opaque and the token-shaped canary in the hook's context and in `run
stop-job`'s output); the resume context's job tail back; the never-cut items
cut for length; the "+N more" pointer back to `pixi run`; `/check:resume
<typo>` lifting the pause; no reconciliation scope (the resolved
interruptions listed again); no horizon; failed calls the session moved on
from listed; a clean-exit `--resume` not reconciled; no `task.reopen`; the
twin check without the newest-session test (a /clear whose SessionEnd was
lost) and without the ended-session test; cancel stopping jobs one at a time;
paths clipped at the end again; in the mod: `/check-cancel` not holding the
gate.
Mutations of 0.5.0 (each on a scratch copy, each made a named check fail):
no external effect for a push (the lost `git push` no longer first under
"Check first"); no process-table check (a running command called unknown);
a shell started before the call counted as the call's (a command line
alone proving it ran); a `pause now` kill ignored; a file changed outside
the task taken as the task's; validations not reconciled; the old "redo the
stage from the start" resume text; `/check:list` missing from the commands
the hook answers (it resumed a paused session); fast.sh's fallback resuming
on a management command; cancel ignoring each job's `on_cancel`; the export
built from unscrubbed records (both layers removed: each alone holds); a
journal with a live session pruned; task notifications not linked;
a mid-turn prompt's op without its text hash (dropped as a duplicate); a
cleared session in the same process called alive elsewhere; no detection
of a session opened twice; a journal checkpoint at every pause refresh; an
aborted turn's call not linked to its later notification; the chained-name
sanitizer fix (`note: PGPASSWORD: x`) removed; in the mod: the resume prompt
submitted inside the command's hook (it never enters), `/check-cancel`
ending the turn before Python stops the command, `/check:list` resuming a
memory-only pause, and the pause ack built after the turn ended (the running
Bash call missing from it; found in the TUI, now in the test world).
Before (0.4.0 after the verification fixes): 611 / 605 passed on macOS, 617
on the VM (three runs under sh = dash, two under `CHECKPOINT_TEST_SH=bash`);
the 7 gateway-env job checks that used to fail there under bash were a race
in `jobs._refresh_state` (a wrapper polled just after `Popen` can show as
`[sh]` in /proc). `tests/acceptance_journal.py`: 19 of 19, ~19 s.
Measured: the spool adds ~1 ms per hook on the not-paused path (median of 40
against a copy without it; macOS sh and dash, Linux dash); ingest ~9,000
events/s (macOS), ~26,000 (VM); a writer's start (every segment's CRC
checked) ~6 ms for a 25 MB journal. Reconciling a task of ~5,000 records: 0.3 s, its
resume context with the summary 0.1 s more (the process table read once).
Mutations of the verification fixes (each on a scratch copy, each made a
named check fail): a backgroundTaskId / async Agent result recorded as done;
every Bash success claiming exit 0; the summary ignoring the `background`
marker (a cut payload's start listed as a failure); no inner-word rule for
names (PGPASSWORD, APITOKEN); no second look inside a value whose name isn't
sensitive; values stopping at a comma; Read results back to a clipped dump;
every command keeping its output tail; staleness comparing hashes only inside
git; a failed edit's after-hash counted as written; the hook not reading
HEAD; the snapshot not comparing with the hook's HEAD; only the last
segment's content CRC checked; a just-started job wrapper whose command line
isn't readable yet called vanished.
Mutations of 0.4.0 (each on a scratch copy, each made a named check fail):
no fsync before "saved", no CRC check, appending after a torn tail, a seq gap
not fatal, no dedup of (kind, op), payloads dropped when the journal write
fails, no token shapes, no sensitive field names, Python on the fast path,
payloads inside the index line (16 parallel writers mix them), the active
segment's content CRC ignored (a flipped bit), unknown outcomes listed as
done, "saved" without a sync, a spool failure kept silent; in the mod: no
record of a denied call, the ingest timer ignoring the index's mtime,
/check-save ending the turn.
Mutations of 0.3.0: fast.sh's
paused deny removed, the build lock ignoring pids, `--frozen` for
`--locked`, no umask 077, the Python gate failing open while paused, a stale
noresume honoured, the build's copies neither removed nor made writable (a
read-only plugin folder), the failure reason back to the log's last line, no
degraded mark on the not-paused path, doctor taking a stale degraded mark for
the gate's state; in the mod: the gate's `.catch` passing, no noresume mark,
slash commands skipping the origin check (a scheduled `/skill` resumed), no
`$.fs.write` fallback for the noresume mark, a failed mark letting the prompt
through, a typed `/check:resume` not lifting a memory-only pause, no
`$.turn.abort`, the gate letting a paused call through, `now` not stopping
the command first, a failed flag write not held in memory.

`pixi run ...` here uses the checkout's own dev env (`.pixi/`). An installed
copy never runs pixi in its folder: `hooks/env.sh` builds the runtime in
Claude Code's plugin data folder (invariant 8). To typecheck the mod:
`pixi exec --spec nodejs --spec typescript tsc -p <a tsconfig outside the
plugin>` with the types the plugin-authoring skill ships (the header of
`claude-code.d.ts` has the tsconfig).

Releasing: bump the version (invariant 10), `pixi run test`, `git add -A`
from the repo root, then `pixi run release-check` before `git commit`.
Installs get only what is committed, and a finished tree once had
`hooks/pixi.sh` and `skills/` untracked and its deletions unstaged:
`git commit -a` would have shipped a plugin with no `/check:*` commands
whose jobs server can't start. The check fails on anything untracked,
unstaged, or ignored in a plugin folder (other than `.pixi/`,
`__pycache__/`, `.DS_Store`, `.claude-plugin/types/`); it only reads (`git ls-files`), and the
suite prints its findings as a note.

Tests are hermetic: `CHECKPOINT_HOME`, `CLAUDE_PLUGIN_DATA` and
`CLAUDE_CONFIG_DIR` (the runtime's fallback) are temp dirs (removed at the
end), `CHECKPOINT_GATEWAY_CONTRACT` points the doctor's existence check into
it (never the real `~/.config/gateway`), the runtime tests build envs with a
fake pixi in plugin copies under paths with spaces and non-ASCII letters, and
`claude plugin test`/`validate` run with a scratch `CLAUDE_CONFIG_DIR` and no
network;
hooks run through the real `hooks/fast.sh` with `CHECKPOINT_PYTHON` set to
the test interpreter (through a wrapper that counts calls, to prove the fast
path skips Python), `CLAUDE_PID` points at fake Claude processes, and
process tests build fake Claude trees that run commands the way Claude Code
2.1.29x does (`eval '...'` with `'"'"'`). `CHECKPOINT_GATEWAY_ENV` points
at a temp file (set before anything else, inherited by every server and
hook the suite starts, and checked): jobs source the gateway's env file,
and the real one (`~/.config/gateway/env.sh`) belongs to the user's live
gateway. Never run tests, jobs or the server against it, and never unset
the override in a test except around a call that only computes the path.
The TUI acceptance scripts' sessions get scratch values from tests/tui; a
script that runs checkpoint's CLI itself (`env.sh run ...` from the test
process) must name `CHECKPOINT_GATEWAY_CONTRACT` and `CHECKPOINT_GATEWAY_ENV`
too (`Box.ck_env`, `accept_install.cli_env`): unset, `gateway.discover` looks for the
real contract file and `/check:status` calls the daemon it names. It names
`CLAUDE_PLUGIN_DATA` as well (the sessions' data folder): lib.sh takes it
before `CHECKPOINT_ENVD`, so a caller's own value sent `env.sh run status`
to an env that isn't there (exit 75, nothing printed; a suite check pins it).
An acceptance wait after a submit must pump the PTY (the script's `wait()`
or `wait_main()`, never `FakeAPI.wait_main` or a bare sleep): Claude Code
writes to its terminal synchronously, so a test that only sleeps blocks it
in write() for exactly as long as the test waits (0.7.3's 60 s "stall" of
the first prompt after `--resume` was this, not Claude Code).
Add a check for every behavior
change; mutation-check the important ones (undo the fix, see the check
fail, restore). Mutate a scratch copy of the checkout, and run copies one
at a time: with suites running side by side, the process checks (`ps
fails after the caller's read` ...) fail.

The spike prototype and its drivers (`relay.py`: an interactive `claude` in
a pty that both the user and an agent can type into, run from the Terminal
panel since the sandboxed CLI can't reach the keychain; `drive_sdk.py`: the
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
   `ck_spool`; no extra process: measured ~1 ms per hook). Python runs in the
   background, never waited for, only for a Bash command whose text looks
   like a test or build (`snap`: the git state it runs against) and at Stop,
   PreCompact and PostCompact (ingest); SessionEnd ingests in the hook.
3. Hooks never write the user's or agent's tracker text; only
   `checkpoint.json` next to it. A tracker whose `Session:` names another
   session is that session's: never used for this session's checkpoint,
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
   `capture.SPOOL_MAX`, `CK_HASH_MAX` = `capture.HASH_MAX`), and so must the mod's
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
   and may be read-only): state lives in `CHECKPOINT_HOME` (the journal
   and the spool too; private: umask
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
    PGPASSWORD/APITOKEN count as sensitive). Spool payloads are deleted only
    after the journal write that holds them succeeded; a failed write leaves
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
    its file hashes come later and say how much later. The summary is built
    from the journal alone (no model).

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
    complete, export) never resume and work while paused:
    `transcript.COMMAND_RE`, the mod's `typedResumes` and fast.sh's
    `prompt_fallback` must agree. `/check:cancel` applies each job's
    `on_cancel` (or the policy given) to the task's own-runner jobs, all at
    once, and says exactly what happened to each (the mod holds every tool
    call meanwhile); a finished task picked up again is reopened
    (`task.reopen`); `/check:stop` and the resume context never show a job's
    log (it would reach the model unmasked: `job_tail` is the route the
    gateway masks); exports are rebuilt from records scrubbed
    again; retention never removes a journal with a live or paused session or
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
    running", never pruned, and its task's journal is kept. A pause reaches
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
    delivered pause and goes through the retry decision for an uncertain
    one), the session
    record says "uncertain" before the call goes out, and an uncertain pause
    is sent again only to a gateway listing `pause_id`, or when none of the
    session's live jobs has `on_pause interrupt`; the gateway's
    `pause.applied` event after the send counts as delivered; otherwise it is
    held (nothing sent; said once, `/check:doctor`).

18. **Hosts: only what the gateway reported** (0.7.0, `hosts.py`). A host's
    reachable / auth / runtime now come from a fresh snapshot; without one
    every state is "unknown" with the reason, a field the gateway didn't send
    is "not reported", and what the journal last saw is labelled as such,
    never passed off as now. 0.8's words and 0.9's booleans read the same
    (`gateway.state_word`). A changed identity (the last `host.state` in the
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
  mock clock's `settle()` to let unawaited work finish).

## Conventions (shared with the sister project)

The user built [gateway](https://github.com/p-w-rs/gateway) (checked out at
`../gateway`) the same way: hand-rolled stdio MCP server, shell pre-filters
in front of Python hooks, `pixi run test`, Python 3.12 stdlib only, macOS
and Linux (POSIX sh that works under dash and with BSD and GNU tools).

Gotcha: if gateway is installed in your session, its PreToolUse hook blocks
Bash commands whose text contains `curl`, `wget`, etc. (including commit
messages); use `git commit -F file` if a message mentions them.

## User preferences

- Show a plan and stop before installing anything or changing the user's
  settings.
- Plain, concise docs. No license file unless asked.
