---
description: Show which sessions are paused or working, their tasks, and detached jobs
disable-model-invocation: true
---

The agent-pause hook has already handled this command: its result is in the
<agent-pause-result> block attached to this message. Show it to the user as a
short list (keep every session and job line). Do not call any tools.

If there is no <agent-pause-result> block, the hook did not run: say "The
agent-pause hook did not handle this command." and stop.
