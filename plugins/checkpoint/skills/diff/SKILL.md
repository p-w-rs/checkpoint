---
name: diff
description: "What changed between two checkpoints, or a checkpoint and now: files, plan steps, operations, validation, git, instructions"
argument-hint: "[<checkpoint> [<checkpoint>|now]]"
disable-model-invocation: true
---

The checkpoint hook has already answered this command: the diff is in the
<checkpoint-result> block attached to this message (without arguments: the
newest checkpoint against now). Show it to the user as it is, section by
section. Do not call any tools and don't change what you were doing (a
paused session stays paused).

If there is no <checkpoint-result> block, the hook did not run: say "The
checkpoint hook did not handle this command." and stop.
