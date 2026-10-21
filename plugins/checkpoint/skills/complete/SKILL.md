---
name: complete
description: "Mark this task complete in checkpoint's journal (history kept); the next message starts a new task"
argument-hint: "[note]"
disable-model-invocation: true
---

The checkpoint hook has already marked the task complete: the result is in
the <checkpoint-result> block attached to this message. Relay it in one line
(say plainly if it says NOT marked complete). Do not call any tools.

If there is no <checkpoint-result> block, the hook did not run: say "The
checkpoint hook did not handle this command." and stop.
