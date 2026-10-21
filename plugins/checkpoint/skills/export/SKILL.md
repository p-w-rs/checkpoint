---
name: export
description: "Export a task as a sanitized bundle (summary, checkpoints, journal excerpt, references; never credentials or file contents)"
argument-hint: "[<task>|last] [<path>]"
disable-model-invocation: true
---

The checkpoint hook has already written the export: where it is (or why it
failed) is in the <checkpoint-result> block attached to this message. Relay
it in one line, with the path. Do not call any tools and don't open or read
the bundle.

If there is no <checkpoint-result> block, the hook did not run: say "The
checkpoint hook did not handle this command." and stop.
