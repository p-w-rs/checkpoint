# Changelog

## 0.9.1 (2026-10-26)

- While the runtime isn't ready, the session start tells Claude which state
  setup is in (running, failed, no pixi, or a pixi too old) and what to tell
  you, instead of "still setting up" in every case.
- A pixi older than this version needs: the session start says so and how
  to update it (`pixi self-update`, or your package manager: Homebrew,
  MacPorts), instead of pixi's error and "retried at every session start".
  This includes a plugin update, while checkpoint runs on its previous
  runtime: that session start said nothing. 0.9.0's changelog promised
  this; only `/check:doctor` said it.
- `/check:doctor` during an update tells a failed build of the new runtime
  and one not started yet from a build that is running (it said "building"
  in all three).
- The session start waits at most 5 s for `pixi --version`.
- The no-pixi message says to start a new session, as Claude is told, not
  to restart Claude Code.
- README: removing the marketplace also deletes the plugin's data folder,
  the built Python included, even one kept with `--keep-data`. A
  Troubleshooting entry for a pixi that is too old.

## 0.9.0 (2026-10-26)

Only the latest Claude Code and pixi are supported, and nothing is kept for
older versions of checkpoint, the gateway or Claude Code.

- The pause-time checkpoint (`checkpoint.json`) no longer goes into your
  project: it is written in the state folder, next to its task's journal
  (`journal/<task>/checkpoint-<session>.json`), and removed with it. The hook
  no longer adds `.work/` to a clone's exclude file (the session note still
  asks Claude to, when it starts a tracker). `/check:resume <tracker>` takes
  the tracker's newest checkpoint, whichever session wrote it, also when that
  session reached the project through a symlink or the scrubber masked part
  of the path. `checkpoint.json` files earlier versions left in
  `<project>/.work/<task>/` are no longer read; delete them.
- A gateway without the `pause_id` feature (before 0.9) counts as a contract
  mismatch: checkpoint works alone beside it. A pause whose answer was lost
  is simply sent again with the same pause id; the hold and the
  `pause.applied` look-up for older gateways are gone.
- Removed: turning 0.1's global `paused-all` file into flags, making a state
  folder from before 0.3 private, reading checkpoints without `paused_ts`
  or `session_id`, and the background tools Claude Code no longer has
  (`TaskOutput`, `BashOutput`, `KillShell` and others: only `TaskStop` and
  the task notifications are read). The files these left in the state folder
  (`paused-all`, `now-all`, `.modes-v1`) are no longer touched: `purge` names
  them as files it doesn't know; delete them.
- The runtime's Python is the newest from conda-forge (3.15), no longer
  pinned to 3.12.
- pixi 0.83 or newer is needed (`requires-pixi`, the pixi that made the
  lock); an older one refuses the runtime's manifest, so the setup fails and
  says to update pixi. `/check:doctor` shows pixi's version without a tested
  range and calls out one below that.
- A job the gateway runs gets your session's environment, as checkpoint's
  own runner would, minus credential variables: Claude Code's own
  (`ANTHROPIC_API_KEY`, `ANTHROPIC_CUSTOM_HEADERS`, ...) and any name the
  scrubber treats as secret (`GH_TOKEN`, `PGPASSWORD`, ...), but not the ssh
  agent's `SSH_AUTH_SOCK`. git's config variables (`GIT_CONFIG_COUNT`,
  `GIT_CONFIG_KEY_<n>`, `GIT_CONFIG_VALUE_<n>`, `GIT_CONFIG_PARAMETERS`) are
  sent without the entries that hold a credential, so git still runs. Sent
  only to a gateway that lists the `job_env` feature (gateway 0.11);
  checkpoint keeps no copy.
- A gateway 0.11 refusal for a right that is off in one permission mode
  ("ssh is off for homelab in Auto mode (gateway.toml)") keeps the right's
  name in the task's authorization records, as one off in every mode does.
- `/check:doctor`'s advice for a pixi that is too old adds your package
  manager (Homebrew, MacPorts) to `pixi self-update`.
- Docs: the README states the latest Claude Code and pixi, with no old
  minimums or rollback recipe; its upgrade note says only what older records
  still hold. The README and `/check:help` describe gateway 0.11 (a new
  install configures nothing and asks about network commands, so a job's are
  refused unless `[network]` allows them); git is worded as the gateway
  words it; the mod's tests are for the latest Claude Code. The README's
  first-five-minutes pause runs `sleep 20`: Claude Code refuses a foreground
  `sleep` of 25 seconds or more.

## 0.8.0 (2026-10-23)

Your data:

- The scrubber masks command-line credentials: mysql, mariadb and mongo
  `-p`, curl and wget `user:password`, `sshpass -p`, `login -p` of docker,
  podman, helm and similar tools, `redis-cli -a`, `htpasswd -b`, openssl
  `-k`/`-pass`, the ldap tools' `-w`, smbclient's `-U user%password`, what
  `echo` pipes into `sudo -S`, and Telegram bot tokens. It finds them after
  any separator, inside quoted commands and `$(...)`, past comments,
  here-documents and line continuations. What it still misses:
  [docs/dev/SCRUBBING.md](docs/dev/SCRUBBING.md).
- `checkpoint.json` in a project is scrubbed when it is written. The first
  time, the hook adds `.work/` to the clone's local exclude file, unless the
  repository tracks `.work/`.
- A failed command that isn't a test, build or check keeps only its exit
  code and size, not its output. Another tool's failure keeps its first line.
- Exports start with a line saying that scrubbing is pattern-based and the
  file should be reviewed before sharing.
- With no runtime (no pixi, a failed setup, a setup running over an hour),
  the spool keeps only which tool ran, ids and sizes. Each session's spool
  holds at most 10,000 events or 50 MB (the oldest go, noted as a gap), and
  the spools of sessions gone for 7 days are removed.
- A job of checkpoint's own runner keeps the last 32-64 MB of its log;
  `job_status` and `job_tail` say when earlier output was dropped.
- Spooled events are deleted only after the journal write that holds them
  was fully synced to disk.
- New: `/check:forget <task>`, `checkpoint forget <task>` and
  `checkpoint purge` delete recorded history.
- New: `checkpoint forget-job <job>` releases a gateway job whose outcome has
  been unknown for over 7 days; `/check:doctor` names such jobs.

Using it:

- After a crash, the next session in the project shows the interrupted task
  on screen, with `claude --resume <id>` and `/check:resume <task>`.
- `/check:status` lists this project's unfinished tasks, and no longer shows
  another session's summary.
- In a session, `/check:resume last` is the newest task in the project other
  than this session's own, an interrupted one first.
- `CHECKPOINT_TRACKERS=off` turns trackers off (no instructions, reminders or
  stop prompts); the journal records the same.
- The status line and the pause reply say that any message or non-/check
  command resumes.
- `/check-doctor` also shows the journal, the gateway and stuck jobs.
- Commands recorded while no runtime was there are listed in one line, and a
  call that returned without its result recorded is said as returned, its
  result unknown.
- Recovery on large journals is about twice as fast.
- `/check:cancel detach` says "detached"; "released" now means only
  `forget-job`.

Fixes:

- A size or counter read with a leading zero no longer kills a hook, and a
  torn size count no longer triggers a trim.
- `$PWD:/src` (as in `docker run -v "$PWD:/src"`) is no longer masked as if
  it were a password.
- `/check-stop` is described as stopping gateway jobs too, and its replies
  no longer say "checkpoint:" twice.

Docs: a new README, [SECURITY.md](SECURITY.md),
[docs/CONFIGURATION.md](docs/CONFIGURATION.md); developer docs moved to
[docs/dev/](docs/dev/).

## 0.7.3 (2026-10-21)

- The first status after a start says "setting up" while the runtime
  builds, and tells a failed setup and a missing pixi apart.
- The immediate pause finds the gateway's contract file in your home from
  the system's user database, as the gateway does, not `$HOME`.
- Normal states (recording, setting up, updating) are a dim footer label;
  the status line shows only a pause and failures.

## 0.7.2 (2026-10-21)

The step from 0.1.0 (0.2.0 to 0.7.1 were stages of the work, not committed
separately): a journal recorded by hooks, an immediate pause
(`/check-pause`), reconciliation before every resume, checkpoints to list,
diff, resume from and export, retention, jobs run by the gateway when it is
installed (per-job pause policies), and hosts and authorizations checked
before a resume. A job the gateway refuses runs nowhere.

## 0.1.0 (2026-10-15)

First version: trackers kept by Claude, pause and resume by slash command or
by typing `pause`, `pause now`, and detached jobs.
