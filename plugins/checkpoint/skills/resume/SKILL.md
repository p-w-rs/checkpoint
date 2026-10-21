---
name: resume
description: "Resume this session (after reconciling), un-pause every session, or pick up a task or a checkpoint in this session"
argument-hint: "[all | <task> | <checkpoint>]"
disable-model-invocation: true
---

The checkpoint hook has already handled this command. If a
<checkpoint-resume> block is attached, follow it: it starts with a
reconciliation of the saved state against now. Do its "Check first" items
before anything else: an operation whose outcome is unknown is investigated,
never simply run again (above all one that may have changed something
outside the repository, such as a push or a deploy); a file changed outside
the task is kept as it is; a stale test result is rerun before you rely on
it; a host whose identity changed is checked before anything runs on it,
and work that needs a host the gateway reports unreachable or
unauthenticated waits. Authorizations listed as still holding need no new
question to the user; one flagged as revoked or changed does. Then continue
the work from the plan (the tracker's "Next action" if
there is one), keeping the tracker current. If only a <checkpoint-result>
block is attached, relay it to the user in a line or two and stop (when it
says nothing matched and the session is still paused, say that plainly: the
session stays paused).

If neither block is attached, the hook did not run: say "The checkpoint hook
did not handle this command." and stop.
