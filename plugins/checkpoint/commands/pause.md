---
description: Pause this session (or all) at the next tool call; the agent updates its tracker and stops
argument-hint: "[now] [all] [--stop-jobs] [<session>]"
disable-model-invocation: true
---

The checkpoint hook has already handled this command: its result is in the
<checkpoint-result> block attached to this message. Don't start or continue
any work. If you were in the middle of a task with a tracker, bring its Now,
Jobs and Resume notes sections up to date (writing to the tracker is allowed
while paused). Then reply with the result in one short line, and stop.

If there is no <checkpoint-result> block, the hook did not run: say "The
checkpoint hook did not handle this command, so nothing was paused." and stop.
