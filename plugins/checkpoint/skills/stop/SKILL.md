---
name: stop
description: "Stop one detached job of checkpoint's runner (its whole process group); without a name, list the running ones"
argument-hint: "[<job name or id>]"
disable-model-invocation: true
---

The checkpoint hook has already handled this command: the result is in the
<checkpoint-result> block attached to this message (with Claude Code 2.1.287
or newer, /check-stop does the same at once). Relay it in one line (say
plainly if the job could NOT be stopped). Do not call any tools and don't
start anything new (a paused session stays paused).

If there is no <checkpoint-result> block, the hook did not run: say "The
checkpoint hook did not handle this command, so nothing was stopped." and
stop.
