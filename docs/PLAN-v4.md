# Checkpoint v4 plan (authoritative from 2026-10-19)

This plan implements the upgrade brief for checkpoint (`check@checkpoint`). It supersedes
DESIGN.md where they conflict; DESIGN.md stays the reference for what this plan keeps (the
tracker convention, the fast sh pre-filter, the jobs runner, the pause/resume vocabulary).
The gateway's half is `../gateway/docs/PLAN-v4.md`; the contract between them is
[CONTRACT-v1.md](CONTRACT-v1.md) (identical copy of the gateway's). Platform evidence:
`../gateway/docs/FEASIBILITY-v4.md`.

Checked against Claude Code CLI 2.1.301 / desktop 2.1.299 (mods need 2.1.287 terminal,
2.1.286 desktop) and pixi 0.81.0 on macOS 25.6 arm64; the final acceptance (2026-10-21) also
ran on a Debian 13.7 arm64 VM with Claude Code 2.1.304 and pixi 0.82.0. The desktop app was
never driven (no way to from the test harness).

## What changes, and which earlier decisions it reverses

| Earlier | v4 | Why |
|---|---|---|
| "A real pause button isn't possible" (Spike 5, 2.1.277/2.1.295) | A native immediate `/check:pause` (mod, runs mid-turn, aborts the running turn in ~0.3 s) plus the old slash/keyword routes | Brief §6; FEASIBILITY-v4 P3 |
| The model-written TRACKER.md is the record | A hook- and mod-written journal is the record; the tracker stays as the model's optional notes | Brief §5: automatic, model-independent |
| The pause gate fails open (invariant 1) | Fails closed while paused (sh decides without Python; mod gate has a `.catch` deny); still fails open when not paused, with a visible warning | Brief §2, §6 |
| Any user prompt resumes | A prompt the person typed resumes after reconciliation; scheduled, loop, automation and `isMeta` prompts never resume | Brief §6 no self-resume |
| Resume says "redo the killed stage from the start" | Interrupted operations are classified (never dispatched / completed / running / failed / unknown); unknown external effects are investigated, never blindly repeated | Brief §7 |
| Checkpoint owns jobs | With gateway present, `job_*` delegate to gateway jobs through CONTRACT-v1; the own runner stays only when gateway is absent or doesn't serve the session (never connected, or refused as not served: 403 `plane`, `not_registered`, `peer`, `uid`, 404/405 `no_route`); any other refusal (403 `policy`, `session`, 409 `paused`, an unknown code) is the gateway's decision: the job is refused and runs nowhere (0.7.2; before, any 403 fell back) | Brief §4 |
| Env inside `${CLAUDE_PLUGIN_ROOT}/.pixi` | Env in `$CLAUDE_PLUGIN_DATA/envs/<lock hash>`, `--locked`, run the env's python directly when ready | Brief §2; packaging audit |

Kept: plugin and command names (`/check:pause`, `/check:resume`, `/check:status`,
`/check:help`), the tracker template, the state home outside the plugin
(`${CHECKPOINT_HOME:-${XDG_STATE_HOME:-~/.local/state}/checkpoint}`, survives uninstall),
the fast sh path for every-call hooks, careful process handling, nothing in settings.json.

## Architecture

```
Claude Code process
 ├─ check mod (hooks/mod/*.tsx)   immediate pause/resume/save/cancel, status band, recovery
 │                                pane, tool.call gate (fails closed while paused),
 │                                turn/compaction/task-tool events into the journal
 ├─ classic hooks (fast.sh)       journal facts from every event; fail-closed gate without mods
 └─ MCP server "jobs"             job_* (delegated to gateway when available), checkpoint tools
journal (CHECKPOINT_HOME/journal/<task>/seg-*.jsonl, 0700/0600)
 ├─ records: v, seq, op, ts, kind, session, agent, task, data (sanitized), crc
 ├─ fsync on boundaries; corruption detected by CRC + seq gaps; never silently overwritten
 └─ checkpoints: immutable snapshot records (id, seq range, summary, file hashes, git state)
recovery = deterministic summary from the journal (no model) + optional tracker notes
gateway (optional) ── CONTRACT-v1 over its Unix socket: snapshot, events, jobs, pause policy
```

### What the journal records (sanitized: token-shape scrub; never prompt-box secrets)

- Goal and latest user instructions (prompt text, scrubbed), constraints and decisions the
  model marks (`checkpoint_note` tool or tracker), unresolved questions, plan steps (from
  TaskCreate/TaskUpdate/TodoWrite calls), completion criteria.
- Session lineage (startup/resume/clear/compact/fork), subagents (SubagentStart/Stop),
  repository identity (root, branch, HEAD, worktree), hosts and workspaces (from gateway),
  file changes (Edit/Write/NotebookEdit targets with before/after hashes), artifacts.
- Tool-call intent (PreToolUse) and outcome (PostToolUse/PostToolUseFailure) with op ids;
  commands scrubbed; validation evidence (test/build commands, exit codes, the file hashes
  they ran against); pending operations, waiting states, uncertainty.
- Authorization references (which rights/approvals applied, by name; never values).
- Pause, resume, cancel, checkpoint, compaction, shutdown events.

### Controls (brief §6)

| Control | Name | Behaviour |
|---|---|---|
| Checkpoint now | `/check:save` | writes a checkpoint record; never stops work |
| Pause task | `/check-pause` (immediate; `/check:pause` and typed `pause` too) | gate on, `$.turn.abort` for the running turn, per-command policy for an in-flight Bash call (interrupt its process tree, or leave it running and say so), gateway `POST /v1/pause` with per-job policies; shows what is running, waiting, suspended or unknown |
| Resume task | `/check:resume [checkpoint]` or a typed prompt | reconcile, then continue the same task |
| Cancel task | `/check:cancel` | ends the task, applies its child-work policy (gateway jobs: interrupt or detach), keeps history |
| Terminate process | `/check:stop <job>` | stops one job (gateway or own runner) |
| Take terminal control | gateway's takeover | checkpoint records it; pause never blocks takeover |

Paused tasks never resume on restart, child results, scheduled callbacks or reloads. While
paused: status, list, diff, save, cancel, stop, takeover and resume stay available.

### Reconciliation (brief §7)

Before resume: compare saved file hashes and git state with now (changed files, user edits
preserved, stale validation), current instructions and policy, host identity and runtime
readiness and auth (gateway), job and terminal states (gateway, or own runner), and classify
every interrupted operation. Prefer the original session (`claude --resume <id>`); otherwise
an explicit continuation that names its lineage. Never fabricate native session records.

## Requirements matrix (final, 2026-10-21, plugin 0.7.1)

Status: **implemented**, **partial**, **missing**, **blocked** (needs access or a
capability this work doesn't have; named), **deferred** (left out on purpose; why).
Validation, separate from status: **automated** (the repo suite on macOS arm64: 938
checks under sh, 932 under dash; `claude plugin test` 46; mutations on scratch copies,
13 of 13 for the 0.7.1 fixes), **VM Linux** (the suite on the Debian 13 arm64 VM: 944
under dash and 944 under bash), **synthetic e2e** (the fake CONTRACT-v1 daemon
`tests/fake_gateway.py`, hand-fed hook payloads, or a one-off probe), **live TUI** (the
real Claude Code TUI in a PTY against the harness's fake Messages API, checkpoint (and
the gateway where it says so) installed from git-source marketplaces into a scratch
config: "installed copies"; macOS = Claude Code 2.1.301, VM = the Debian VM's 2.1.304),
**untested**. Paths are relative to `plugins/checkpoint/` (Python modules in
`src/checkpoint/`) unless they start with `docs/` or `../`.

| § | Requirement | Status | Code evidence | Validation | Gap, or what blocks it |
|---|---|---|---|---|---|
| 2 | Normal marketplace install, update, disable, uninstall; dev route documented | implemented | `../.claude-plugin/marketplace.json` (`checkpoint` → `check`), `.claude-plugin/plugin.json` (0.7.1 = `__version__`); README "Install", "Update, disable, uninstall" | live TUI (macOS, VM): the gateway's `tests/acceptance_install.py` installs both (first use, update to a new version in a fresh process, a changed lock rebuilt in the background, `--keep-data`, uninstall); every acceptance script here runs `--installed` (`tests/accept_install.py`) | `/plugin` slash forms not driven; blocked: the real GitHub marketplace (the tree is uncommitted), so a local smart-HTTP git server served it |
| 2 | Runtime outside the versioned plugin folder; `--locked`; tested pixi range | implemented | `hooks/lib.sh` (`ck_env`: `$CLAUDE_PLUGIN_DATA/envs/<sha12 of pixi.lock>`), `hooks/env.sh`, `hooks/pixi.sh`, `pixi.toml` (`requires-pixi = ">=0.68.0"`) | automated (fake pixi, a read-only plugin copy, paths with spaces and non-ASCII letters); probe (0.68.0-0.82.0, macOS); live TUI with pixi 0.81.0 / 0.82.0 | the range was probed on macOS only |
| 2 | Launcher needs only Claude Code + pixi | implemented (macOS, Linux) | POSIX `sh` hooks (`hooks/fast.sh`, `env.sh`, `lib.sh`), the env's python run directly (`-I -S`) | live TUI (macOS, VM): shims for python3/python/node/npm/npx/uv saw 0 plugin calls | native Windows deferred (row "Targets") |
| 2 | Idempotent provisioning with progress; interrupted-setup recovery | implemented | `hooks/lib.sh` (build lock with builder and pixi pids, detached kick), `env.sh ensure` (async SessionStart), the mod's status line | automated (a killed build taken over; a read-only plugin folder); live TUI (macOS, VM): "check: setting up its runtime (first run); pausing works", "first-time setup is running"; a SIGKILLed setup finished by the next session | none open |
| 2 | A cold start or timeout never disables checkpointing | implemented | `hooks/fast.sh` (paused + no Python answer = deny), `py_to` with `CHECKPOINT_HOOK_DEADLINE` (20 s) on every hook that waits, `CHECKPOINT_BG_DEADLINE` (300 s), `degraded/<sid>`; the mod's gate `.catch` denies | automated (+ mutations); live TUI (macOS, VM): a hung runtime: the first call ran after 4.8 s / 3.7 s with a 3 s deadline (60.3-60.8 s before 0.7.1), no hook process left; paused, the next call denied (fail closed) | none |
| 2 | Updates after a restart; offline warm start; spaces and non-ASCII paths; concurrent sessions | implemented | `hooks/lib.sh` (`ck_older`: the previous env while a new one builds), the mod's older-env check (0.7.1) | live TUI (macOS, VM): update in a fresh process with the normal PATH, 0 pixi calls; config `cfg dir ü`, sessions from `wörk dir/ü x` and `élsewhere dir` journaled with their own cwd; two concurrent sessions, 20 records each, CRC-clean and gap-free | the update's status line ("recording · updating its runtime in the background") is covered by a mod test and its mutation, not re-run live after the fix (the VM had shown "first run" before it) |
| 2 | Versioned content separate from state; uninstall; retention and backup | implemented | state in `CHECKPOINT_HOME` (`state.py`), runtime in plugin data; `state.prune`, `recovery.prune_tasks`; README "Data, retention, backup" | automated; live TUI: uninstall removed the data folder and kept the journal; `--keep-data` reinstall made 0 pixi calls | none |
| 2 | Targets: macOS, Linux, Windows; WSL distinct | partial | lock platforms osx-arm64, osx-64, linux-64, linux-aarch64 | macOS arm64: everything; Linux arm64: VM Linux + live TUI (VM); x86-64: untested | blocked: no x86-64 machine; native Windows deferred (POSIX `sh` hooks, no win-64 in the lock, POSIX-only modules: WSL 2); WSL 2 blocked: no Windows machine |
| 3 | Smallest native component set; immediate controls; minimum versions | implemented | `hooks/hooks.json` (every event → `fast.sh`, `modules`), `hooks/mod/register.tsx` + `logic.ts`, `skills/*/SKILL.md` (`/check:*`), `.mcp.json` (`jobs`, `"alwaysLoad": true`) | automated (`claude plugin test` 46, `validate`); live TUI (macOS, VM); probe (VM, 2.1.304: 19 tools with the six job tools offered directly; 13 and deferred behind ToolSearch without `alwaysLoad`) | desktop app blocked (the TUI harness can't drive it; manual check); without mods (Claude Code < 2.1.287 terminal / 2.1.286 desktop) no immediate pause |
| 4 | Contract client: discovery, capability check, stable ids, correlation | implemented | `gateway.py` (discovery states, calls with error kinds, `serves`, `not_served`: 0.7.2), `jobs.py` (idempotency key `ck:<tool_use_id>`, gateway ids), `docs/CONTRACT-v1.md` (identical to the gateway's) | automated (`tests/gateway_tests.py` against `tests/fake_gateway.py`, `hosts_authz_tests.py`, mutations); live TUI (macOS, VM): `acceptance_gateway.py` 27/27 | none |
| 4 | Sanitized cross-plugin data; credential references only | partial | `sanitize.py`, `gateway._safe` and `clean_*` (allowed fields, then scrubbed again) | automated (synthetic secrets in 36 contexts, canary scans); live TUI: the gateway's canary run scanned the journal, spool, registry, checkpoints and the export: 0 hits | a bare secret with no recognizable shape or telling name isn't caught; spool payloads and `running/*.json` stay raw (0600) until ingested or the call ends; own-runner job logs are raw |
| 4 | Works alone, with the gateway, either disabled, incompatible and upgraded versions | implemented | `gateway.discover` (absent, off, unsafe, dead, unknown_contract, mismatch), `gateway.serves` (0.7.1: "doesn't serve this session") | live TUI (macOS, VM): `acceptance_independence.py` 49/49 after the fixes (contract [2]: "mismatch (has 2, needs 1)", works alone; upgraded to 0.9.3000/0.7.3000: delegates again) | a gateway listing versions [1, 2] wasn't tested |
| 4 | Protected shared state | partial | journal `flock` per task, one spool ingester, atomic index appends, 0700/0600 state | automated | other sh writes (flags, markers) are atomic renames without a lock; the state is protected by file modes (same-user code can write it) |
| 5 | Tracking on by default, model-independent | implemented | `hooks/fast.sh` `ck_spool` (no Python), `capture.py` | automated; live TUI (macOS, VM): `acceptance_journal.py` 19/19 (7 events still only in the spool at the SIGKILL, ingested by the next session) | none |
| 5 | Goal, instructions, constraints, decisions, questions, plan, completion criteria | implemented | `capture.py`, `checkpoint_note` (`mcp_server.py`), `tracker.py` | automated; live TUI (goal, a constraint, plan 1 of 2) | none |
| 5 | Lineage, repository identity, file changes, artifacts, hosts and workspaces | partial | `capture.py` (session.start sources, `session.adopt`, `session.concurrent`, repo, edits with sha256), `hosts.py` | automated; live TUI | artifact and output references beyond changed files aren't recorded: missing |
| 5 | Tool-call intent and outcome, validation evidence, pending work, children, waiting, uncertainty | implemented | `capture.py`, `summary.py` | automated (+ mutations); live TUI (an edited file with its hash, a test with the HEAD it ran against, two in-flight commands and a subagent as outcome unknown) | none |
| 5 | Authorization references without secrets | implemented | `authz.py`, the mod's `tool.check` observer, the classic PermissionRequest hook | automated (+ 24 mutations); live TUI (macOS, VM): `acceptance_authz.py` 18/18 | settings rules compared with user, project and local settings files only |
| 5 | Durable journal: atomic writes, seq/op ids, corruption detection, no early ack, failures surfaced | implemented | `journal.py` (CRC32, gap-free seq, flock, fsync with F_FULLFSYNC), the reserves, `.last` and `recording.gap` (0.7.1) | automated (+ mutations); VM Linux; live TUI (macOS, VM): concurrent sessions gap-free, duplicate and out-of-order PostToolUse giving one intent and one outcome; a full disk (a 4 MB macOS disk image): calls ran, the loss said, the gap on record afterwards | disk full on Linux blocked (a tmpfs or loop mount needs root) |
| 5 | Lightweight recording | implemented | spool files + one index line, ingest at Python moments, bounded summary | automated (~1 ms per hook); live TUI per Bash call: 0.145 s with checkpoint vs 0.041 s without (macOS), 0.071 vs 0.028 s (VM) | none |
| 5 | Summaries keep constraints, failures, versions; observed vs inferred | implemented | `summary.py` (the header keeps the last checkpoint and recording gaps, 0.7.1) | automated; live TUI (macOS, VM): journal 19/19 (macOS was 18/19 before 0.7.1: the 4000-character resume context lost the checkpoint id under long paths) | none |
| 5 | Separate from native rewind; no repository side effects | implemented | read-only git (`GIT_OPTIONAL_LOCKS=0`), nothing committed, stashed or restored | automated | none |
| 6 | Checkpoint now | implemented | `/check-save`, `/check:save` (`core.save`, synced before "saved") | automated (+ mutations); live TUI | none |
| 6 | Pause while busy through a native immediate control; measured | implemented | `/check-pause` and typed `pause` (mod: flag, `$.turn.abort`, per-command policy), `fast.sh` fallback, `watch.sh` | live TUI (macOS, VM): ack 0.05-0.08 s during a streamed reply, 0.05-0.06 s during a long command, 0.21-0.31 s with `now`, 0.37-0.45 s with gateway jobs; a hung runtime: ack 0.76-0.84 s | `/check:pause` typed mid-turn waits for the turn's end (2.1.301 hides queued slash commands: use `/check-pause` or type `pause`); without mods a pause applies at the next tool call |
| 6 | Resume, cancel, terminate, take terminal control | implemented | `core.resume_here`/`resume_task`/`resume_now`, `cancel`, `stop_job`; the gateway's takeover recorded (`terminal.owner`) | live TUI (macOS, VM): `acceptance_recovery.py` 39/39, `acceptance_gateway.py` (cancel through the gateway) | takeover itself is the gateway's |
| 6 | No self-resume; controls usable while paused; scope | implemented (with mods) | the mod's `prompt.submit` (`noresume/<sid>`), `transcript.COMMAND_RE` | live TUI (macOS, VM): a child result, a one-shot CronCreate, `/reload-plugins`, SIGKILL + `--resume`: still paused | without mods any prompt resumes (the classic payload carries no origin) |
| 6 | Per-job pause policy; truthful states | implemented for gateway jobs; partial for the own runner | `jobs.py`, `core.gateway_pause`/`gateway_resume` (`on_pause`, `pause_id`, retries) | automated (SIGINTs counted); live TUI (macOS, VM): interrupt (gone by SIGINT), continue (running), suspend (state T), resume continues it | the own runner (no gateway) has no per-job pause policy, terminal, input or remote host, and nothing survives a reboot (`/check:status` says so) |
| 7 | Reconcile against instructions, policy, repository, host, runtime, auth, jobs | implemented | `recovery.py`, `hosts.py`, `authz.py` | automated; live TUI (local jobs) | reconciliation against live remote gateway hosts not exercised in a TUI (the e2e gateway runs local jobs) |
| 7 | Prefer the original session; explicit continuation with lineage | implemented | the resume context's `claude --resume <id>`, `session.adopt` | live TUI: `claude --resume <killed>` gets `<checkpoint-recovery>`; `/clear` continues as an explicit continuation | none |
| 7 | File hashes, stale validation, user edits preserved | implemented | `recovery.reconcile` (files, git, validations) | live TUI: the user's edit kept, the test marked STALE | none |
| 7 | Operation outcome classes; idempotency; no blind repeat | implemented | five classes, `external_effect`, idempotency keys for gateway jobs | automated (+ mutations); live TUI: a `git push` whose result a SIGKILL lost: outcome unknown, "check first", the remote saw exactly one push | none |
| 7 | List, diff, select, complete, export | implemented | `/check:list`, `/check-list` pane, `/check:diff`, `/check:resume <cp>`, `/check:complete`, `/check:export` (`recovery.py`) | automated; live TUI (the Checkpoints pane: diff with now, resume from here) | desktop pane blocked (manual) |
| 13 | Commands, native controls, visibility, diagnostics | implemented | `skills/*` (`/check:*`), the mod's `/check-*`, status line, `/check:doctor` + `/check-doctor` | automated; live TUI | none |
| 14 | Acceptance 14.2, 14.3, 14.4, 14.5, 14.11 with labelled evidence | implemented | `tests/acceptance_independence.py`, `acceptance_journal.py`, `acceptance_recovery.py`, `acceptance_authz.py`, `acceptance_gateway.py`, `acceptance_durability.py` | live TUI (macOS, VM), all green after the 0.7.1 fixes | desktop app blocked (manual) |

## Phases (dependency order; run alongside the gateway's)

1. **Packaging and mod skeleton.** Env in plugin data by lock hash, `--locked`, direct
   python when ready, fail-closed paused gate in sh, progress, README lifecycle commands,
   Windows/WSL statement; mod skeleton with `/check:doctor`, status band, immediate pause.
   *Done in 0.3.0 (2026-10-19).* Decisions: the immediate commands are `/check-pause` and
   `/check-doctor` (mod commands can't carry `check:`, and `/pause` is the skill's short
   name); the skills stay and the mod answers them without a model turn; an immediate pause
   leaves a running Bash call running and says so (`now` stops it first). Not verified: the
   desktop surface, Linux, and a marketplace-installed copy in a live session (the suite
   covers plugin copies; the TUI runs used `--plugin-dir`).
2. **Journal.** Format, writer (fsync, CRC, seq, locks), sanitizer, capture from classic
   hooks and mod events, migration from 0.2 state, retention, export.
   *Done in 0.4.0 (2026-10-19)*, with the deterministic recovery summary, `/check:save`,
   `/check-save`, `/check-list` and the `checkpoint_note` tool pulled forward from phase
   3; retention and export move to phase 3. Decisions: one journal per task (a task
   begins at startup or /clear; resume, fork and compact continue it; /check:resume
   adopts one); hooks spool every payload to its own file plus a <1 KiB index line
   (one write, measured atomic) instead of appending payloads (1 KiB lines from 16
   writers mixed); Python ingests at the moments it runs anyway plus the mod's 20 s
   timer; a test-like Bash command pins its version: the hook reads HEAD before it
   runs, and a background snapshot (the hook never waits) hashes the files and says
   how late it was. Validated: suite (sh, dash), mod tests, mutations on scratch
   copies, the TUI acceptance run, and the suite on Debian 13 arm64 (VM). Not
   verified: a marketplace-installed copy in a live session (the TUI run used
   `--plugin-dir`), the desktop surface.
3. **Recovery.** Deterministic summary, checkpoints, list/diff/select/complete, reconciliation
   (files, git, validation staleness, operation classes), resume flow, cancel.
   *Done in 0.5.0 (2026-10-20)*: `recovery.py` (reconcile, list, diff, select, export,
   retention, lineage), every resume route reconciles first, a checkpoint per pause, cancel
   with a child-work policy (interrupt/detach) for the own runner's jobs, stop, complete,
   immediate `/check-resume|cancel|stop|diff` and the Checkpoints pane, background ends
   linked to their starts, concurrent sessions detected; retention and export pulled in from
   phase 2. Decisions: an immediate `/check-resume` lets the task go on at once (a prompt the
   mod's timer sends: one submitted inside the command's hook never enters); cancel and
   complete move the session to a new task; select never restores files (Claude Code's
   /rewind does); the default cancel policy is interrupt unless the job was started with
   `on_cancel: detach`; retention 30 days for finished tasks, 180 for open ones. Validated:
   suite (sh, dash), mod tests, mutations on scratch copies, `tests/acceptance_recovery.py`
   in the TUI, the suite on the Debian VM. Not verified: a marketplace-installed copy in a
   live session, the desktop surface (the pane), gateway jobs (phase 4).
4. **Gateway integration.** CONTRACT-v1 client (discovery, snapshot, events), job delegation,
   pause policies, disabled/incompatible behaviour.
   *Done in 0.6.0 (2026-10-20)*: `gateway.py` (discovery, calls, events into the journal), jobs
   delegated to the gateway when it offers them (one supervisor per job, idempotency keys,
   never a second start after a lost or uncertain answer), `on_pause` per job and the task's
   `task_policy`, pause/resume/cancel/stop through the contract (once per pause; late delivery
   while still paused, never just before a resume; an undelivered pause dropped at resume; the
   task's jobs of ended sessions paused too, those of open sessions named), unknown states never
   finished or stopped (kept from pruning and retention), the gateway's data scrubbed again,
   the own runner's limits stated when there is no gateway. Decisions: a gateway pause holds a
   whole session there, so jobs of the task that a still-open session started are listed, not
   paused; a stop or cancel of an unknown job is "NOT confirmed stopped"; checkpoint keeps its
   own runner for 0.1-0.5 records and when the gateway is absent or incompatible. Validated:
   suite (sh, dash, a long TMPDIR; `tests/gateway_tests.py` against a fake CONTRACT-v1 daemon),
   mod tests, mutations on scratch copies, `tests/acceptance_gateway.py` (both plugins installed
   from git-source marketplaces in the real TUI, the gateway through the harness's e2e route),
   the suite on the Debian VM. Not verified: the desktop surface; remote-host gateway jobs (the
   e2e gateway runs local jobs); a marketplace update across versions with both installed.
   **Last build step: 0.7.0 (2026-10-20).** Reconciliation against the gateway's hosts
   (`hosts.py`: the hosts a task used, their reachable/auth/runtime now from a fresh snapshot or
   "unknown", operations needing a blocked host marked, a changed identity stops), authorization
   references (`authz.py`, the mod's `tool.check` observer and `mod.tool.answer`, the classic
   PermissionRequest hook; in the summary, checked before a resume), idempotent pause retries
   (`pause_id`, the gateway's `pause.applied` evidence, held when an interrupt job could be
   signalled twice). The gateway's half (gw-policy, gateway 0.9) added `pause_id`, host rights,
   accounts and the new events to CONTRACT-v1 and copied it here. Validated: suite (sh 903, dash
   897, a 195-character TMPDIR 903; the Debian VM 909 under dash and bash), mod tests (44),
   24 of 24 mutations on scratch copies, `tests/acceptance_authz.py` in the TUI (18 of 18: the
   real permission dialog), `tests/acceptance_gateway.py` with the gateway checkout of the same
   day (27 of 27; its daemon lists `pause_id`). Not verified: a lost pause answer against the
   real gateway (the fake gateway implements the contract's `pause_id` text and the 0.8
   behaviour without it; the SIGINT counts are the fake's), remote hosts' states from a real
   remote gateway host in a checkpoint session, the gateway 0.9 policy wording beyond the
   strings in the gateway's `policy.py` of that day.
   *Verification fixes (2026-10-21)*: a second pause command during the same pause keeps the
   pause's mark and id (nothing rewrites a flag that is there: no second `/v1/pause`, no second
   SIGINT, an uncertain pause still held); the gateway's `job_start` needs its `host` (journaled;
   `localhost` when the call names none); a settings rule's list then is its verdict's (an ask
   rule answered Yes holds); a restarted gateway's `unknown` doesn't clear a host-identity
   change, nor does the host's removal; a gateway right the snapshot doesn't list is "not
   compared". Suite 915 (dash 909, long TMPDIR 915), mod tests 44, 7 of 7 mutations.
5. **Acceptance.** Brief §14 scenarios 2–5 and 11 on marketplace-installed copies.
   *Acceptance fixes, checkpoint's half (2026-10-21, 0.7.1)*, from the acceptance run's defect
   list: (1) SessionStart waited for Python without checkpoint's own deadline (so did PostToolUse's
   reminder and Stop): with a hung runtime the first prompt waited out Claude Code's 60 s and the
   hook's shell and the hung child were left behind. Every hook that waits for Python now goes
   through `py_to` (`CHECKPOINT_HOOK_DEADLINE`, 20 s) and records the timeout in degraded/<sid>;
   background ingests stop at `CHECKPOINT_BG_DEADLINE` (300 s) and none starts after a foreground
   timeout. (2) `/check:status` said "available: new jobs run there" for a gateway daemon that
   doesn't serve the session (the gateway plugin disabled, its daemon alive from an earlier
   session) while job_start fell back to the own runner: `gateway.serves` asks the daemon (one
   capabilities call; a 403 is "doesn't serve this session", jobs in the own runner; no answer is
   "found, didn't answer"). (3) A resume context under long paths lost the last checkpoint's id
   (it was in Evidence, the first section left out): the summary's header carries it. (4) The
   status line said "setting up its runtime (first run)" while an update's env built, and the mod's
   Python commands refused although the previous env runs them: the mod counts an older ready env
   (as lib.sh's `ck_older`); the status line says "recording · updating its runtime in the
   background". (5) The job tools were deferred behind ToolSearch on Claude Code 2.1.304:
   `.mcp.json` `"alwaysLoad": true` and each tool's `_meta` (probe on the Debian VM, installed
   copy: 19 tools with the six job tools, against 13 and the job tools only named as deferred
   without it; the server's flag alone sufficed). (6) A full-disk window left nothing for
   `/check:doctor` (both markers live on the same disk, and the spool's was removed on recovery):
   each session gets a 1 KiB reserve per marker at its start, which a failure on a full disk
   renames into place and overwrites; on recovery the failure becomes `.last` history and a lost
   spool window becomes a `recording.gap` record, named in the summary's header, `/check:status`
   and `/check:doctor`. Also fixed: env.sh mcp's "pixi isn't installed" reason raced the build it
   starts (a Linux suite run once read "building"). The gateway's items of that list (the
   prompt-box guard, "gate: ready" before the managed shell, the reserved word localhost, a
   dialog job's lost answer, the daemon after uninstall, the 12 s Bash stall with a stopped
   daemon, tmux after SIGTERM) are the gateway's. Validated: suite 938 (macOS sh), dash 932, the
   Debian VM 944 under dash and 944 under bash; mod tests 46; 13 of 13 mutations (each on a
   scratch copy: SessionStart, PostToolUse and Stop without the deadline or with a background
   ingest after a timeout, the background deadline, the serving check, the header's checkpoint,
   alwaysLoad, both reserves, the recovery's history, the gap record and its header line, the
   mod's older env). TUI acceptance on installed copies (macOS, Claude Code 2.1.301):
   `acceptance_journal` 19/19 (18/19 before: the checkpoint id), `acceptance_durability` 29/29
   (the first call 4.8 s with a 3 s deadline, was 60.5 s; the full disk's failure on record after
   it, the status line showing it during it), `acceptance_independence` 49/49 (with the daemon
   alive and the gateway plugin disabled checkpoint says it doesn't serve the session, as its job
   ran in its own runner), `acceptance_gateway` 27/27, `acceptance_recovery` 39/39 (acks 0.05 /
   0.05 / 0.31 s), `acceptance_authz` 18/18; on the Debian VM (Claude Code 2.1.304):
   `acceptance_journal` 19/19, `acceptance_durability` 23/23 (no disk image on Linux; the first
   call 3.7 s, was 60.3-60.5 s), `acceptance_independence` 49/49. Not re-run: the gateway's
   `acceptance_install.py`, whose update part saw the old status line on the VM (the mod test and
   its mutation cover the new one). The acceptance scripts' own fixes: checkpoint's view
   of the gateway is read from inside the session (`ck_view`: the daemon serves by process
   ancestry, so the test process is refused); `Box.ck_env` and acceptance_recovery's CLI calls
   (status, ingest) always name the scratch contract and env file (without them checkpoint's CLI
   run from the test process looked for the real `~/.config/gateway/run/contract.json`: a stat,
   and a call to whatever daemon it named; the harness's sessions were always hermetic); the reserves
   aren't recording errors; a torn tail the full disk cut off (stepped over by the next writer,
   gap-free) is accepted as designed; leftover processes get 15 s and are named by script; the
   synthetic duplicate part writes the call's result to its transcript (its `pause now` watchers
   otherwise waited for the Claude Code running the test).
   *Where it stands (2026-10-21):* the "not verified" notes of phases 1-4 about a
   marketplace-installed copy in a live session and about Linux are closed by the acceptance
   runs (installed copies, macOS and the Debian VM); still not verified: the desktop surface
   (blocked: the TUI harness can't drive it; manual check), remote-host gateway jobs from a
   session, and a lost pause answer against the real gateway (the fake gateway's). The final
   matrix is above; the completion report (outside the repo) has the scenario table.
   *Security fix (2026-10-21, 0.7.2)*: `job_start` treated any 403 from the gateway's `POST
   /v1/jobs` as "not served" and ran the job in the own runner, so a job the gateway refused by
   policy ran anyway. Only the daemon's not-served refusals fall back now (`gateway.NOT_SERVED`:
   403 `plane`, `not_registered`, `peer`, `uid`; 404/405 `no_route`); any other refusal (403
   `policy`, `session`, 409 `paused`, an unknown code or none) is reported as refused by the
   gateway with its reason and rule, starts nothing anywhere, and is journaled
   (`job.started`, `outcome: refused`); stops and pauses the gateway refuses never become
   checkpoint's own signals. Also: acceptance_journal and acceptance_recovery end the process
   trees their scripted commands start (the Linux runs left `sleep 300` behind). Validated:
   suite 955 (sh), 949 (dash); mod tests 46; 2 of 2 mutations; TUI (macOS) gateway 27/27,
   journal 20/20, recovery 40/40. Not run: the Debian VM, a real gateway policy refusal in a
   session.

## Rules for this work

- Leave the user's live state alone: a v0.1-wrapper job (pid 94342) runs from the real
  CHECKPOINT_HOME; migration must keep it visible and stoppable, and tests use scratch
  CHECKPOINT_HOME and CHECKPOINT_GATEWAY_ENV only.
- Never install into the real Claude config; scratch `CLAUDE_CONFIG_DIR` only.
- Every behaviour change gets a test; keep the suite hermetic (fake Claude process trees).
- No git commits, pushes or installs without the user's say-so.
