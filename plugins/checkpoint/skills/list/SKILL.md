---
name: list
description: "List this task's checkpoints: time, journal seq, a one-line summary, and whether each is still current"
argument-hint: "[<task>]"
disable-model-invocation: true
---

The checkpoint hook has already answered this command: the list is in the
<checkpoint-result> block attached to this message (with Claude Code 2.1.287
or newer, /check-list shows the same at once, with a pane to diff or resume
from one). Show it to the user, keeping every checkpoint line. Do not call
any tools and don't change what you were doing (a paused session stays
paused).

If there is no <checkpoint-result> block, the hook did not run: say "The
checkpoint hook did not handle this command." and stop.
