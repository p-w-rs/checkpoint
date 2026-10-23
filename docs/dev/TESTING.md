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
| `claude plugin test .` | The mod's tests (`tests/mod/*.test.ts`) |
| `claude plugin validate` | The plugin and the marketplace |

The suite is hermetic: state, plugin data and Claude Code's config are temp
folders, Claude processes are fakes, and nothing touches the network or a
real gateway. The rules are in [CLAUDE.md](../../CLAUDE.md#commands).

CI (`.github/workflows/test.yml`) runs the suite under sh and dash on Linux
(x86-64, ARM64) and macOS (Apple silicon, Intel), with the oldest supported
pixi on one Linux job, and weekly with the newest pixi and Claude Code.

Important fixes get a mutation check: undo the fix on a scratch copy, see a
named check fail, restore.

## Acceptance scripts

These drive the real Claude Code TUI in a pseudo-terminal against a fake
model API (`tests/tui/`), with the plugin installed from a git marketplace
into a scratch configuration. Run each from `plugins/checkpoint` with
`pixi exec --spec python=3.12 --spec pyte python tests/<script>`:

| Script | Checks | Time |
|---|---|---|
| `acceptance_journal.py` | A task killed mid-way (SIGKILL), then its recovery summary | ~25 s |
| `acceptance_recovery.py` | Every pause route, no self-resume, a crash after a remote write, `--resume`, `/compact`, `/clear`, the Checkpoints pane | ~4 min |
| `acceptance_authz.py --root <dir>` | Claude Code's permission verdicts into the journal and the resume | ~1 min |
| `acceptance_gateway.py --tmp <dir> --pixi-cache <dir> --gateway ../gateway` | Both plugins: gateway jobs with each pause policy, pause, resume, cancel | ~1 min |
| `acceptance_independence.py --tmp <dir> --pixi-cache <dir>` | Either plugin alone, both, either disabled, an incompatible contract, upgrades | ~2 min |
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
- Not run: a first CI run, an install from GitHub of the released version,
  the desktop app (by hand; the harness can't drive it).

