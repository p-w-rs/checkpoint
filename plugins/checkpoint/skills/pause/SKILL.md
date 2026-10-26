---
name: pause
description: "Pause this session (or all). Typed while Claude is busy it waits until the turn ends; for an immediate stop use /check-pause or type pause"
argument-hint: "[now|interrupt] [all] [--stop-jobs] [<session>]"
disable-model-invocation: true
---

With mods on, checkpoint's mod answers `/check:pause` and `/check:pause now`
itself, at once, and this skill doesn't run. Otherwise
(or for `all`, `<session>`, `--stop-jobs`) the checkpoint hook has already
handled this command: its result is in the <checkpoint-result> block
attached to this message. Don't start or continue
any work. If you were in the middle of a task with a tracker, bring its Now,
Jobs and Resume notes sections up to date (writing to the tracker is allowed
while paused). Then reply with the result in one short line, and stop.

If there is no <checkpoint-result> block, the hook did not run: say "The
checkpoint hook did not handle this command, so nothing was paused." and stop.
