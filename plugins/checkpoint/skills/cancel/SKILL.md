---
name: cancel
description: "Cancel this task: it ends (history kept), its running jobs are stopped (interrupt) or left running, detached from it (detach)"
argument-hint: "[interrupt|detach] [note]"
disable-model-invocation: true
---

The checkpoint hook has already canceled the task: what happened to each job
and command is in the <checkpoint-result> block attached to this message
(with Claude Code 2.1.287 or newer, /check-cancel does the same at once,
mid-turn). Don't continue the canceled work and don't call any tools. Reply
with the result in one or two lines (say plainly if something could NOT be
stopped), and stop.

If there is no <checkpoint-result> block, the hook did not run: say "The
checkpoint hook did not handle this command, so nothing was canceled." and
stop.
