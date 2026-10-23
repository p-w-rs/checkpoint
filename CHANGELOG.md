# Changelog

## 0.8.0 (unreleased)

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
