"""Terminal commands, for when no session is idle to type into. Through the
helper every session start writes into the state folder (CHECKPOINT_HOME,
by default ~/.local/state/checkpoint):

    ~/.local/state/checkpoint/checkpoint status                 sessions, pause state, jobs
    ~/.local/state/checkpoint/checkpoint pause all [now]        pause every running session
    ~/.local/state/checkpoint/checkpoint pause <session> [now]  pause one (id prefix from status)
    ~/.local/state/checkpoint/checkpoint resume all             un-pause every session
    ~/.local/state/checkpoint/checkpoint resume <session>       un-pause one
    ~/.local/state/checkpoint/checkpoint doctor                 versions, runtime, gate
    ~/.local/state/checkpoint/checkpoint summary [<task>|<session>|last]   a task's recovery summary
    ~/.local/state/checkpoint/checkpoint tasks                  recent tasks (journal)
    ~/.local/state/checkpoint/checkpoint save <session> [why]   checkpoint now (fsynced)
    ~/.local/state/checkpoint/checkpoint list <session>|<task>  a task's checkpoints (time, seq, line, staleness)
    ~/.local/state/checkpoint/checkpoint diff <task> [<cp> [<cp>|now]]   between checkpoints, or one and now
    ~/.local/state/checkpoint/checkpoint reconcile <task>|last [<cp>]    what a resume would check first
    ~/.local/state/checkpoint/checkpoint export <task>|last [<path>]     a sanitized bundle (0600)
    ~/.local/state/checkpoint/checkpoint stop <job>             stop one job (checkpoint's runner, or the gateway's)
    ~/.local/state/checkpoint/checkpoint prune [--dry-run]      journals past retention (CHECKPOINT_KEEP_DAYS ...)
    ~/.local/state/checkpoint/checkpoint gateway                what checkpoint sees of the gateway (CONTRACT-v1)
(An un-paused session continues when you send it a message, and gets its
resume notes then.) In a checkout, `pixi run status` etc. do the same.
Internal (hooks and the mod): ingest <session> [--sync], snap <session> <tool_use_id>,
gw-pause <session> (the gateway's half of the mod's /check-pause),
status --session <session>, list-json <session>, resume-now <session> [<checkpoint>],
cancel <session> [interrupt|detach], complete <session> [note], stop-job <session> <job>,
diff-session <session> [<cp> [<cp>|now]].
"""
from __future__ import annotations

import sys

import json
import time

from . import capture, core, gateway, journal, recovery, sanitize, state, summary

USAGE = __doc__


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE.strip())
        return 0
    cmd, args = argv[0], argv[1:]
    state.migrate_legacy()
    if cmd == "status":
        sid = args[1] if args[:1] == ["--session"] and len(args) > 1 else ""
        print(core.status_text(sid))
        return 0
    if cmd == "ingest" and args:
        r = capture.ingest(args[0], sync="--sync" in args)
        g = gateway.sync(args[0])       # what the gateway saw meanwhile (short; never fails the ingest)
        core.gateway_settle(args[0])
        print(json.dumps({"task": r.task, "events": r.events, "records": r.records, "error": r.error,
                          "gateway": {"status": g.status, "recorded": g.recorded, **({"error": g.error} if g.error else {})}}))
        return 1 if r.error else 0
    if cmd == "gw-pause" and args:
        # The mod's /check-pause: the gateway's half (POST /v1/pause), its line for the ack.
        line = core.gateway_pause(args[0], reason="checkpoint pause (/check-pause)")
        print(json.dumps({"text": line}))
        return 0
    if cmd == "gateway":
        g = gateway.discover()
        print("\n".join([core.gateway_status_line(g)] + core.gateway_doctor(args[0] if args else "")))
        return 0
    if cmd == "snap" and len(args) == 2:
        r = capture.snap(args[0], args[1])
        print(json.dumps({"task": r.task, "records": r.records, "error": r.error}))
        return 1 if r.error else 0
    if cmd == "save" and args:
        ok, msg = core.save(args[0], " ".join(args[1:]))
        print(msg)
        return 0 if ok else 1
    if cmd == "list" and args:
        s = state.find_session(args[0])
        print(core.list_checkpoints(s["session_id"]) if s and len(args) == 1 else core.list_checkpoints("", args[0]))
        return 0
    if cmd == "list-json" and args:
        capture.ingest(args[0])
        task = state.load_session(args[0]).get("task_id") or ""
        print(json.dumps(recovery.list_data(task) if task else {"task": "", "checkpoints": []}))
        return 0
    if cmd == "diff" and args:
        task = core.find_task(args[0])
        if not task:
            print(f"No task matches '{args[0]}'.", file=sys.stderr)
            return 2
        print(recovery.diff(task, args[1] if len(args) > 1 else "", args[2] if len(args) > 2 else "now")[0])
        return 0
    if cmd == "diff-session" and args:
        print(core.diff_text(args[0], args[1:]))
        return 0
    if cmd == "reconcile" and args:
        task = core.find_task(args[0])
        if not task:
            print(f"No task matches '{args[0]}'.", file=sys.stderr)
            return 2
        print(recovery.reconcile(task, checkpoint=args[1] if len(args) > 1 else "").text(limit=100000))
        return 0
    if cmd == "export" and args:
        task = core.find_task(args[0])
        if not task:
            print(f"No task matches '{args[0]}'.", file=sys.stderr)
            return 2
        ok, where = recovery.export(task, args[1] if len(args) > 1 else "")
        print(where)
        return 0 if ok else 1
    if cmd == "resume-now" and args:
        ok, msg = core.resume_now(args[0], args[1] if len(args) > 1 else "")
        print(msg)
        return 0 if ok else 1
    if cmd == "cancel" and args:
        print(core.cancel(args[0], args[1:]))
        return 0
    if cmd == "complete" and args:
        print(core.complete(args[0], args[1:]))
        return 0
    if cmd == "stop-job" and args:
        print(core.stop_job(args[0], args[1:]))
        return 0
    if cmd == "stop":
        print(core.stop_job("", args))
        return 0
    if cmd == "prune":
        dry = "--dry-run" in args
        gone = recovery.prune_tasks(dry_run=dry)
        print(("Would remove" if dry else "Removed") + f" {len(gone)} journal(s) past retention"
              + (f": {', '.join(gone)}" if gone else "") + f" (finished tasks after {state.journal_keep_days() or 'never'} "
              f"day(s), open ones after {state.journal_keep_open_days() or 'never'}).")
        return 0
    if cmd == "summary":
        limit = summary.LIMIT
        if "--limit" in args:
            i = args.index("--limit")
            try:
                limit = int(args[i + 1])
            except (IndexError, ValueError):
                pass
            args = args[:i] + args[i + 2:]
        ref = args[0] if args else "last"
        task = core.find_task(ref)
        if not task:
            print(f"No task matches '{ref}' (`tasks` lists them).", file=sys.stderr)
            return 2
        print(summary.build(task, limit=limit).text)
        return 0
    if cmd == "tasks":
        rows = summary.task_list(args[0] if args else "", limit=20)
        if not rows:
            print("No tasks recorded yet.")
        for m in rows:
            fin = (m.get("finished") or {}).get("how")
            st = fin or ("interrupted" if summary.interrupted(m) else ("ended" if m.get("ended") else "active"))
            print(f"{m['task']}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(float(m.get('updated') or 0)))}  "
                  f"{st:<11} {sanitize.clip(m.get('goal'), 70)}  ({m.get('cwd') or '?'})")
        return 0
    if cmd == "doctor":
        print(core.doctor_text(args[0] if args else ""))
        return 0
    if cmd == "pause":
        if not [a for a in args if a not in ("now", "--stop-jobs")]:
            print("Say which: `pause all` or `pause <session>` (see `status`).", file=sys.stderr)
            return 2
        print(core.pause("", args, by="terminal"))
        return 0
    if cmd == "resume":
        if args[:1] == ["all"]:
            n = core.resume_all()
            print(f"Un-paused {n} session(s)." if n else "No session was paused.")
            if n:
                print("Each continues when you send it a message.")
            return 0
        if args:
            s = state.find_session(args[0])
            if not s:
                print(f"No single session matches '{args[0]}'.", file=sys.stderr)
                return 2
            was = state.clear_paused(s["session_id"], pending=True)
            print(f"Session {s['session_id'][:8]} un-paused; send it a message to continue."
                  if was else f"Session {s['session_id'][:8]} wasn't paused.")
            gw = core.gateway_resume(s["session_id"])
            if gw:
                print(gw)
            return 0
        print("Say which: `resume all` or `resume <session>`.", file=sys.stderr)
        return 2
    print(USAGE.strip(), file=sys.stderr)
    return 2
