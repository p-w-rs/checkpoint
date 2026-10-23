---
name: forget
description: "Delete one task's recorded history (its journal, checkpoints, finished jobs and their logs); refused while one of its sessions runs or is paused, or a job of it runs"
argument-hint: "<task>"
disable-model-invocation: true
---

The checkpoint hook has already handled this command: what it removed (or
why it removed nothing) is in the <checkpoint-result> block attached to this
message. Relay it in one or two lines; say plainly when nothing was removed.
Do not call any tools, don't read or delete anything yourself, and don't
start anything new (a paused session stays paused).

If there is no <checkpoint-result> block, the hook did not run: say "The
checkpoint hook did not handle this command, so nothing was deleted." and
stop.
