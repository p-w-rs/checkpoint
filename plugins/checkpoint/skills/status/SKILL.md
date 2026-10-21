---
name: status
description: Show which sessions are paused or working, their tasks, and detached jobs
disable-model-invocation: true
---

The checkpoint hook has already handled this command: its result is in the
<checkpoint-result> block attached to this message. Show it to the user as a
short list (keep every session and job line). Do not call any tools.

If there is no <checkpoint-result> block, the hook did not run: say "The
checkpoint hook did not handle this command." and stop.
