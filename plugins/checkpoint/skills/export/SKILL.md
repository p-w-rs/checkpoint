---
name: export
description: "Export a task as a sanitized bundle (summary, checkpoints, journal excerpt, references; no file contents; secrets masked by pattern, so review it before sharing)"
argument-hint: "[<task>|last] [<path>]"
disable-model-invocation: true
---

The checkpoint hook has already written the export: where it is (or why it
failed) is in the <checkpoint-result> block attached to this message. Relay
it in one line, with the path, and that scrubbing is pattern-based: the user
should review the file before sharing it. Do not call any tools and don't
open or read the bundle.

If there is no <checkpoint-result> block, the hook did not run: say "The
checkpoint hook did not handle this command." and stop.
