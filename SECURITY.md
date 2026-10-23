# Security

checkpoint keeps a record of Claude's work and holds tool calls while a
session is paused. It is not a sandbox: Claude's commands run as you, and
checkpoint doesn't contain them. What it records, and how it scrubs, is in
the README's [Security and privacy](README.md#security-and-privacy) and
[Your data](README.md#your-data); the scrubber's known gaps are in
[docs/dev/SCRUBBING.md](docs/dev/SCRUBBING.md).

## Reporting a problem

Report it privately through GitHub's vulnerability reporting:
<https://github.com/p-w-rs/checkpoint/security/advisories/new> (the
repository's **Security** tab, **Report a vulnerability**). If that button
is missing, open an issue asking for a private contact, with no details.
Please don't open a public issue about the problem itself, and don't paste a
real secret into the report: show its shape with a made-up value.

Include what happened (the command or prompt, with made-up values), what you
expected, where the data ended up, and the versions of checkpoint
(`/check:doctor` shows it), Claude Code and your OS.

## In scope

- **A secret reaching the journal, an export or your project** in an
  ordinary form the scrubber should know: a token shape or a common
  command-line credential that comes through unmasked. This includes
  `checkpoint.json` in `<project>/.work/`, and anything written there or to
  the clone's exclude file that git would then publish.
- **Data that should never be recorded** turning up in the state folder:
  file contents, edit strings, the output of a command that isn't a test,
  build or check, a failed command's output.
- **Files readable by others:** the state folder or its files not private
  (0700 and 0600).
- **Data loss:** a journal or checkpoint lost or damaged by checkpoint
  itself, `checkpoint forget`, `purge` or retention removing what a live or
  paused session or a running job still needs, or a checkpoint reported as
  saved that wasn't.
- **A pause that doesn't hold:** a tool call that runs while a session is
  paused, other than what a pause allows: reads, tracker edits, to-do lists,
  `checkpoint_note`, `task_policy` and the job tools other than `job_start`
  (`job_status`, `job_tail`, `job_stop`).
- **A job run twice, or run when the gateway refused it.**

## Out of scope

- The forms listed in [docs/dev/SCRUBBING.md](docs/dev/SCRUBBING.md), and a
  secret with no known shape and no telling name next to it.
- Your trackers (`TRACKER.md`), which Claude writes and checkpoint doesn't
  scrub, and the raw log of a job checkpoint runs itself.
- What Claude Code, the gateway or another plugin do with a command or with
  the context checkpoint gives Claude.

## Supported versions

Only the latest release gets fixes. To update:
`claude plugin marketplace update checkpoint`, then
`claude plugin update check@checkpoint`.
