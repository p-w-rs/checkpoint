---
name: doctor
description: checkpoint's diagnostics - versions, pixi and its tested range, the runtime env, the gate, the state folder
disable-model-invocation: true
---

The checkpoint hook has already handled this command: its report is in the
<checkpoint-result> block attached to this message (with Claude Code 2.1.287
or newer the plugin's mod answers /check:doctor itself, at once, and this
skill doesn't run; /check-doctor is the same report mid-turn). Show the
report to the user as a short list, keeping every line. Do not call any
tools. If a line says something is wrong (pixi missing or too old, setup
failed, the gate degraded), say in one line what the report says to do.

If there is no <checkpoint-result> block, the hook did not run: say "The
checkpoint hook did not handle this command." and stop.
