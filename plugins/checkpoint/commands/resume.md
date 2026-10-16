---
description: Resume this session, clear pause-all, or pick up a task from its tracker in a new session
argument-hint: "[all | <task>]"
disable-model-invocation: true
---

The checkpoint hook has already handled this command. If a
<checkpoint-resume> block is attached, follow it: check the tracker's "Check
first" items and the changes listed, then continue the work from "Next
action", keeping the tracker current. If only an <checkpoint-result> block is
attached, relay it to the user in a line or two and stop.

If neither block is attached, the hook did not run: say "The checkpoint hook
did not handle this command." and stop.
