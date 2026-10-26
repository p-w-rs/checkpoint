# Test log

> Developer documentation: what each verification run found, newest first. Kept out of [CLAUDE.md](../../CLAUDE.md) so that file stays short; the current state is in [TESTING.md](TESTING.md).

## 0.9.0 (unreleased): the latest Claude Code, 2026-10-25

Claude Code 2.1.310, pixi 0.83.0 and Python 3.15.1 on macOS arm64, scratch
configs, the machine at load 10-19 from other work. The tree includes the
overhead checks' Rosetta notes, the long-`sleep` doc check and the fixes
from their review.

- Suite: 1117 passed under sh, 1111 under dash, 1117 with a 180-character
  TMPDIR (that run before a missed budget was measured again); `claude
  plugin test` and `validate` pass inside it. The review's runs of the
  tree before its fixes: 1112 / 1106.
- The overhead budgets take the median of the pairs' differences: in a
  probe at load 12, the medians of two identical copies differed by -2 to
  39 ms, the median of their pairs' differences by -1 to 2 ms; the spool
  added 1-8 ms. Before a missed budget was measured again, two dash runs
  failed only the as-installed budget (16 and 21 ms, then 11 ms); the same
  set-up probed minutes later added under 1 ms. Hooks run under Rosetta
  (`arch -x86_64`): identical copies still differed by up to 10.8 ms.
- 8 of 8 mutations caught by a named check, on scratch copies: sysctl found
  through PATH, the Rosetta detection inverted, a budget that only prints
  notes, no second measurement, the difference of the medians, the old
  inline-only long-`sleep` pattern, a fenced `sleep 60` in a skill, the
  README's `sleep 60`.

## 0.9.0 (unreleased): latest only, 2026-10-23

The compatibility paths removed (0.1's `paused-all`, the pre-0.3 mode fix,
checkpoints without `paused_ts` or `session_id`, the hold and `pause.applied`
look-up for gateways without `pause_id`, Claude Code's removed background
tools), `checkpoint.json` moved into the state folder, Python unpinned and
relocked (3.12.15 -> 3.15.1), the doctor's tested pixi range dropped. Claude
Code 2.1.301 and pixi 0.81.0 on macOS arm64 (the newest, 2.1.309 and 0.83.0,
weren't installed here). These counts don't hold for the tree as it is now:
the mod's tests were then rewritten for the latest Claude Code (since 2.1.309
the test kit's `$.tool.call` checks the call's permission again inside the
call), and on 2.1.301 `authz.test.ts`'s "after an ask" test fails, so the
suite's `claude plugin test` check fails there. 0.9.0 hasn't been verified
on the latest Claude Code yet.

- Suite: 1089 passed under sh, 1083 under dash (baseline 1092 / 1086 on
  0.8.0: the migration checks went, new ones came).
- 11 of 11 mutations caught by a named check, on scratch copies: the
  `paused-all` reader, Python's and sh's `pause_id` requirement, `TaskOutput`
  read again, the `paused_at` fallback, a tested pixi range, the checkpoint
  next to the tracker, a checkpoint without `session_id` used, an uncertain
  pause not resent, the tracker's checkpoint looked for in the project, the
  early `checkpoints/<session>.json` kept.
- After the review: a tracker's checkpoint is matched by `tracker_key` (the
  resolved path's sha256, unscrubbed), forget and purge name tracker folders
  from the session record, purge calls an earlier version's file one it
  doesn't know, the fake gateway writes its contract file with `indent=1`,
  and the README keeps its upgrade privacy note. Suite: 1093 passed under
  sh, 1087 under dash. 7 of 7 mutations caught: the oldest checkpoint
  picked, the exact (scrubbed) path compared, no real path in the session
  record, a folder named once per spelling, the old purge wording, sh
  without the newline collapse, the README without its privacy note.

## 0.8.0 (unreleased): four fix rounds, 2026-10-22

A test-machine issue (`ps` hung) meant the runs from round 2 on used a `ps`
wrapper that gives up after 9 s, and the two `accept_install.kill_marked` /
`kill_started` checks failed for that reason only (they passed earlier the
same day, and nothing in `tests/tui` or `accept_install` changed).

- **Round 1, data safety** (checkpoint.json scrubbed and excluded, the
  command-line scrubber, failed commands' output, a full sync before
  payloads go, job log rotation, gateway cursor pruning, faster recovery):
  suite 984 (sh), 978 (dash); baseline 961. Every new scrub family, the two
  scrub layers of checkpoint.json, the exclude guards, the output tail, the
  sync and the log cap were mutation-checked. Recovery timing on synthetic
  journals: reconcile 3.49 -> 1.49 s at ~50k records, 7.07 -> 3.10 s at
  ~100k; the session-start path 5.20 -> 2.22 s and 10.78 -> 4.50 s; results
  byte-identical.
- **Round 1, user experience** (the spool without a runtime, its cap and
  prune, status, the crash notice and `last`, help texts, forget and purge,
  `CHECKPOINT_TRACKERS=off`, resume wording): suite 1028 (sh), 1022 (dash);
  mod tests 64; 17 of 17 mutations. Added overhead per hook: median 1.3 ms
  (sh), 0.6 ms (dash) on the test path.
- **Round 2** (the scrubber rewritten on absolute positions, quoted inner
  commands, curl bundles, mongo/ldap/smb, the exclude line once, `last`
  never this session's own task, unrecorded commands as possible external
  effects, purge and forget keeping shared spools and kept jobs, the index
  rotated without a runtime, `forget-job`, Python's lines in the mod's
  doctor; leading zeros and torn counts in the shell): suite 1056/2 (sh),
  1050/2 (dash); mod tests 66; 34 + 1 mutations.
- **Round 3** (command substitutions scrubbed, unrecorded commands in one
  line, `forget-job`'s 7 days from when the job was first unknown, shell
  options and escaped quotes in inner commands, linear-time scrubbing):
  suite 1074/2 (sh), 1068/2 (dash). Probes: the review's adversarial set 1
  leak of 115 (round 2: 23), a 30 s fuzz of ~400,000 cases without an
  exception.
- **Round 4** (each line read again on its own, here-document bodies, `#`
  comments, contractions, bounded option scans, the wording of a returned
  call whose result wasn't recorded): suite 1087/2 (sh), 1081/2 (dash);
  fast-path overhead +0.5 ms. Fuzz of 262,632 texts: no exception, none over
  0.5 s. The review's two fixes (lines joined by a backslash-newline in a
  here-document body; an option's attached quote such as `-e's,a,b,'`) were
  applied afterwards.
- **Docs pass** (dev docs moved to `docs/dev/`, the doc-link check now reads
  every top-level Markdown file and `docs/` recursively): suite 1089/2 (sh),
  `claude plugin test` and `validate` pass. Dash not re-run.
- **Docs fixes and the 0.8.0 version** (plugin.json and `__version__`):
  suite 1089/2 (sh), the same two `ps` checks; `claude plugin test` and
  `validate` pass. Dash not re-run.

## 0.7.3 and earlier

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
arm64 VM (Claude Code 2.1.304) 944 under sh = dash and 944
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
from `pixi exec --spec git`) 909 under sh = dash and 909
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
