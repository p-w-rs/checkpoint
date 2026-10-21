---
name: save
description: "Checkpoint now: record this task's state in checkpoint's journal (synced to disk) without stopping anything"
argument-hint: "[why]"
disable-model-invocation: true
---

With Claude Code 2.1.287 or newer checkpoint's mod answers `/check:save`
itself, at once, and this skill doesn't run (`/check-save` does the same
mid-turn). Otherwise the checkpoint hook has already saved the checkpoint:
its result is in the <checkpoint-result> block attached to this message.
Relay it to the user in one line (say plainly if it says NOT saved, and what
to do). Do not call any tools, and don't change what you were doing: a
checkpoint stops nothing.

If there is no <checkpoint-result> block, the hook did not run: say "The
checkpoint hook did not handle this command, so nothing was saved." and stop.
