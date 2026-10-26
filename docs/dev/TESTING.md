# Testing

> Developer documentation: how checkpoint is tested and what was verified for each release. Run diaries: [TEST-LOG.md](TEST-LOG.md).

## The suite

From `plugins/checkpoint`:

```bash
pixi run test                                  # prints "N passed, M failed"
CHECKPOINT_TEST_SH=/bin/dash pixi run test     # the hooks under dash (Debian's sh)
TMPDIR=<a ~180-character folder> pixi run test # no check may depend on the temp path's length
```

`tests/run_tests.py` runs everything, in its own namespace:

| File | Covers |
|---|---|
| `run_tests.py` | Packaging, the hooks (the real `hooks/fast.sh`), the pause gate, trackers, the runtime build, jobs, docs checks |
| `journal_tests.py` | The journal format and durability, capture, the scrubber |
| `recovery_tests.py` | Reconciliation, list, diff, export, retention, lineage |
| `gateway_tests.py` | The CONTRACT-v1 client against `fake_gateway.py` |
| `hosts_authz_tests.py` | Hosts, authorization references, pause retries |
| `ux_tests.py` | The spool without a runtime, status, the crash notice, forget and purge, trackers off, `forget-job` (run last) |
| `claude plugin test .` | The mod's tests (`tests/mod/*.test.ts`), for the latest Claude Code (since 2.1.309 its test kit checks a call's permission again inside the call; on an older one they fail) |
| `claude plugin validate` | The plugin and the marketplace |

The suite is hermetic: state, plugin data and Claude Code's config are temp
folders, Claude processes are fakes, and nothing touches the network or a
real gateway. The rules are in [CLAUDE.md](../../CLAUDE.md#commands).

The two hook-overhead budgets (the spool adds at most 10 ms per hook) take
the median of the differences of 40 pairs of calls, with and without the
spool, back to back: on a loaded machine the medians of two identical
copies differed by up to 39 ms, the median of their pairs' differences by
at most 2 ms. A missed budget is measured again, twice at most: under other
load the spool's file writes can stall for a while, and a real cost shows in
every try. Under Rosetta (an x86_64 build on Apple silicon: `sysctl -n
sysctl.proc_translated` prints 1), the budgets print their numbers as notes
instead of failing: there every hook is about 6 times slower, and two
identical copies still differ by more than 10 ms.

Important fixes get a mutation check: undo the fix on a scratch copy, see a
named check fail, restore.

## Acceptance scripts

These drive the real Claude Code TUI in a pseudo-terminal against a fake
model API (`tests/tui/`), with the plugin installed from a git marketplace
into a scratch configuration. Run each from `plugins/checkpoint` with
`pixi exec --spec python --spec pyte python tests/<script>`:

| Script | Checks | Time |
|---|---|---|
| `acceptance_journal.py` | A task killed mid-way (SIGKILL), then its recovery summary | ~25 s |
| `acceptance_recovery.py` | Every pause route, no self-resume, a crash after a remote write, `--resume`, `/compact`, `/clear`, the Checkpoints pane | ~4 min |
| `acceptance_authz.py --root <dir>` | Claude Code's permission verdicts into the journal and the resume | ~1 min |
| `acceptance_gateway.py --tmp <dir> --pixi-cache <dir> --gateway ../gateway` | Both plugins: gateway jobs with each pause policy, pause, resume, cancel | ~1 min |
| `acceptance_independence.py --tmp <dir> --pixi-cache <dir>` | Either plugin alone, both, either disabled, an incompatible contract, upgrades; pixi-guard (`--pixi-guard ../pixi-guard`) alone, with each and with both: a bare `python3` refused in Bash and in both plugins' jobs and the gateway's terminals, the others still working (`--only` picks parts) | ~4 min |
| `acceptance_durability.py --tmp <dir> --pixi-cache <dir>` | Concurrent sessions, duplicate events, a hung runtime, a stopped gateway, a full disk, an upgrade under a live job | ~2 min |

## Coverage as of 0.7.3

"Live" means a real Claude Code session in a terminal, driven against a
stand-in model, with the plugin installed from a git marketplace: macOS
arm64 (Claude Code 2.1.301) and a Debian 13 arm64 VM (2.1.304), 2026-10-21.
The suite passed on macOS (961 checks under sh, 955 under dash) and the VM
(967 under bash, 955 under dash). The install from GitHub was tested too:
the `/plugin` forms and both kinds of uninstall on the VM; from the command
line on macOS, with an update from 0.7.2, disable and enable.

| Area | Built | Tested |
|---|---|---|
| Install, first-use setup, update, uninstall | yes | live (macOS, Linux VM) from a local git marketplace and from GitHub |
| The journal survives a kill without a handoff; recovery summary | yes | live (macOS, Linux VM) |
| Immediate pause during a reply, a long command, queued work; no self-resume | yes | live (macOS, Linux VM) |
| Resume after a crash, `--resume`, `/compact`, `/clear`; stale tests; a lost push not repeated | yes | live (macOS, Linux VM) |
| Checkpoints pane (`/check-list`) | yes | live in the terminal; desktop app untested |
| Permission verdicts recorded and checked before a resume | yes | live (macOS, Linux VM) |
| Jobs handed to the gateway, per-job pause policies | yes | live, both plugins (local jobs; remote hosts not from a session) |
| Either plugin alone, disabled, an incompatible or upgraded gateway | yes | live (macOS, Linux VM) |
| Concurrent sessions, duplicate events, a hung runtime, a full disk | yes | live (full disk on macOS only; the Linux VM on 0.7.1) |
| The desktop app | the same mod | untested |
| Artifact references beyond changed files | no | |

On the VM, the concurrency checks last ran on 0.7.1, and the permission and
either-plugin-alone checks on an earlier 0.7.3 build with a mod change since
reverted.

## 0.9.0 verification

Run on 2026-10-24 and 25 (Claude Code 2.1.310, the newest then, on macOS and
Linux; pixi 0.83.0; Python 3.15.1; scratch configs and the fake model API
throughout).

- The suite on macOS arm64: 1117 passed under sh, 1111 under dash (the dash
  run skips the 6 "hooks under dash" checks), 1117 with a 180-character
  TMPDIR. `claude plugin test` (the mod's 66 tests) and `claude plugin
  validate` (plugin and marketplace) pass inside it. The mod's tests follow
  the newest Claude Code's test kit (2.1.309 rechecks permission inside a
  call); on 2.1.301 one of them fails.
- The suite on Intel macOS through Rosetta (osx-64 packages): 1115 passed
  after one rerun for a timing check (the two spool-budget checks are notes
  under Rosetta, where identical hooks differ by more than the 10 ms
  budget). Rosetta is not real Intel hardware.
- The suite on Linux (Debian 13 arm64 VM): 1123 passed under dash, 1123
  under bash (after one rerun for a timing check).
- A test that fails without the change for every fix of the release.
- The acceptance scripts on macOS: journal 28 of 28 (and 28 of 28 as an
  installed copy), recovery 40 of 40 (40 of 40), authz 18 of 18 (18 of 18),
  gateway 27 of 27 (with gate 0.11.0), independence 96 of 96 (checkpoint,
  the gateway and pixi-guard alone, in pairs and all three: 11 parts),
  durability 29 of 29; on Linux: independence 96 of 96.
- Not run: an install from GitHub of the released version, the desktop app
  (by hand; the harness can't drive it).

## 0.8.0 verification

Run on 2026-10-22 and 23 (Claude Code 2.1.301 on macOS, 2.1.304 on Linux;
pixi 0.81.0 / 0.82.0; scratch configs and the fake model API throughout).

- The suite on macOS arm64: 1092 passed under sh, 1086 under dash (the
  dash run skips the 6 "hooks under dash" checks), 1091 with a 180-character
  TMPDIR. `claude plugin test` and `claude plugin validate` (plugin and
  marketplace) pass inside it.
- The suite on Intel macOS through Rosetta (osx-64 packages): 1090 passed,
  1 failed, the fast-path overhead check (11.6 ms against 10 ms; hooks take
  100-200 ms under Rosetta, and it passed in two of four runs). Rosetta is
  not real Intel hardware.
- The suite on Linux (Debian 13 arm64 VM): 1098 passed under dash, 1098
  under bash.
- A mutation check for every fix of the release (the fix undone on a scratch
  copy, a named check failed), the scrubber's joined-line pass and its
  option-quote rule included.
- The acceptance scripts on macOS: journal 28 of 28 (8 new checks: the crash
  notice on screen, none on `--resume`, `/check:complete`, `/check:forget`
  refused for a running task and done from another session), recovery 40 of
  40, authz 18 of 18, gateway 27 of 27 (with gate 0.10.0), independence 49 of
  49, durability 29 of 29; on Linux: journal 28 of 28, recovery 40 of 40,
  gateway 27 of 27.
- Not run: an install from GitHub of the released version,
  the desktop app (by hand; the harness can't drive it).

