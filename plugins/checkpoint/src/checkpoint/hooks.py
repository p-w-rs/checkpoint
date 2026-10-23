"""Hook entry points, run via run.py after hooks/fast.sh decides Python is needed.

session_main -> SessionStart: record the session, inject the tracker/jobs note,
                prune old records.
prompt_main  -> UserPromptSubmit: /check:* commands and the plain `pause`
                keyword; any other user message in a paused session resumes it.
gate_main    -> PreToolUse(*): apply queued pause commands (also while paused);
                deny while paused (allow tracker writes).
post_main    -> PostToolUse(*): tracker reminder every N tool calls.
stop_main    -> Stop: refresh the checkpoint when paused; tracker nudge otherwise.
watch_fire   -> hooks/watch.sh: `pause now` while a foreground command runs.
sync_pause   -> the mod paused this session (flags/<sid>): record it, write the checkpoint.

When not paused, a broken hook must never freeze every session: the *_main
functions catch everything and exit 3 with no output, and hooks/fast.sh lets
the call go ahead and records degraded/<sid>. While paused the gate fails
closed: an error still denies (here, or in fast.sh when Python can't run).
"""
from __future__ import annotations

import fnmatch
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import capture, core, journal, procs, sanitize, state, summary, tracker, transcript

SESSION_NOTE = """\
checkpoint is on. Work so that you can be paused at any tool call and resumed later, possibly in a new session:
- Tracker: for any task with more than a few steps, keep <cwd>/{work}/<task-slug>/TRACKER.md (template below). Create it as soon as the task clearly has several steps, put this session's id after "Session:", keep the Now section current, and update a stage's status when it changes. Put long detail in linked stage docs, not in the tracker.
- When you create the first tracker in a git repository, keep {work}/ out of commits unless the user wants trackers committed: if `git check-ignore -q {work}/` fails, append the line `{work}/` to the file that `git rev-parse --git-path info/exclude` prints (a local ignore list that is never committed).
- Mark a stage "accepted" only when the user says so in chat.
- Anything likely to run more than ~2 minutes goes through the checkpoint job_start tool (it survives pauses and new sessions), with a row in the tracker's Jobs table and a redo_hint.
- If a tool call is denied with "Paused by the user", don't start new work: bring the tracker up to date (Write/Edit to it are allowed), then end your turn with one line saying you're paused.
This session's id: {sid}
Tracker template:
{template}"""

# CHECKPOINT_TRACKERS=off: the journal still records everything; nothing
# asks for trackers.
SESSION_NOTE_NO_TRACKERS = """\
checkpoint is on (trackers off). Work so that you can be paused at any tool call and resumed later, possibly in a new session:
- Anything likely to run more than ~2 minutes goes through the checkpoint job_start tool (it survives pauses and new sessions), with a redo_hint.
- If a tool call is denied with "Paused by the user", don't start new work: end your turn with one line saying you're paused.
This session's id: {sid}"""


def _emit(obj: dict[str, Any] | None) -> None:
    if obj:
        print(json.dumps(obj))


def _ctx(event: str, text: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    out.update(extra)
    return out


FAILURE_NOTE = ("checkpoint: recording failed ({err}). Nothing is lost yet: events wait in the spool and "
                "are written once it works again; /check:doctor says more.")


def _with_failure(out: dict[str, Any] | None, res: "capture.IngestResult | None") -> dict[str, Any] | None:
    """Add a one-time systemMessage when this ingest hit a new recording failure."""
    if res is None or not res.new_failure:
        return out
    out = dict(out or {})
    msg = FAILURE_NOTE.format(err=sanitize.clip(res.error, 160))
    out["systemMessage"] = (out["systemMessage"] + " " + msg) if out.get("systemMessage") else msg
    return out


def _ingest(sid: str, sync: bool = False) -> "capture.IngestResult | None":
    """Ingest the session's spool at a Python moment; never raises."""
    try:
        return capture.ingest(sid, sync=sync)
    except Exception as e:  # noqa: BLE001
        print(f"checkpoint: ingesting the spool failed: {e}", file=sys.stderr)
        return None


def _payload() -> dict[str, Any]:
    try:
        data = json.load(sys.stdin)
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def _hook_ppid() -> int:
    return int(os.environ.get("CHECKPOINT_HOOK_PPID") or os.getppid())


def _note_session(payload: dict[str, Any]) -> str:
    """Make sure the session record has cwd/transcript (and a Claude pid)."""
    state.migrate_legacy()
    sid = payload.get("session_id") or ""
    if not sid:
        return ""
    state.sync_flag(sid)  # a pause the shell fallback or the mod recorded
    sess = state.load_session(sid)
    fields: dict[str, Any] = {}
    for k in ("cwd", "transcript_path"):
        # cwd is the session's project folder: set once, not moved by each `cd`.
        if k == "cwd" and sess.get("cwd"):
            continue
        if payload.get(k) and sess.get(k) != payload[k]:
            fields[k] = payload[k]
    if not sess.get("claude_pid"):
        fields["claude_pid"] = procs.claude_pid(_hook_ppid())
    if not sess.get("created_ts"):
        fields["created_ts"] = time.time()
    if fields:
        state.update_session(sid, **fields)
    return sid


def _dirs(payload: dict[str, Any]) -> list[str]:
    """Where this session's trackers live: the project folder first, then the
    shell's current folder (payload cwd), which moves with every `cd`."""
    sess = state.load_session(payload.get("session_id") or "") if payload.get("session_id") else {}
    out: list[str] = []
    for d in (os.environ.get("CLAUDE_PROJECT_DIR"), sess.get("cwd"), payload.get("cwd")):
        if d and d not in out:
            out.append(d)
    return out


def _tracker_for(payload: dict[str, Any], sid: str) -> dict[str, Any] | None:
    return core.session_tracker(sid, _dirs(payload))


# -- SessionStart ---------------------------------------------------------------

def session_main() -> int:
    payload = _payload()
    sid = payload.get("session_id") or ""
    if not sid:
        return 0
    try:
        state.fix_modes()
        state.migrate_legacy()
        state.sync_flag(sid)
        now = time.time()
        sess = state.load_session(sid)
        my_pid = procs.claude_pid(_hook_ppid())
        twin = _open_elsewhere(sess, my_pid)
        fields: dict[str, Any] = dict(
            cwd=os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd(),
            transcript_path=payload.get("transcript_path") or "",
            claude_pid=my_pid, source=payload.get("source") or "",
            started=state.now_iso(), started_ts=now)
        if not sess.get("created_ts"):
            fields["created_ts"] = now
        state.update_session(sid, **fields)
        res = _ingest(sid, sync=True)
        if res is not None and not res.started and not res.error:
            # Its SessionStart wasn't in the spool (it couldn't be written): from here.
            _t, recs = capture.start_records(sid, None, payload.get("source") or "startup")
            res = capture.record(sid, recs, sync=True)
        try:
            capture.ingest_dead(except_sid=sid)
        except Exception as e:  # noqa: BLE001
            print(f"checkpoint: ingesting other sessions' spools failed: {e}", file=sys.stderr)
        if state.queued_pauses(sess) and payload.get("source") in ("startup", "resume"):
            # A new Claude process: the old one's queue, and the echoes the
            # queued pause commands were waiting for, are gone with it.
            state.drop_queued(sid)
        if state.trackers_on():
            text = SESSION_NOTE.format(sid=sid, work=state.work_dir_name(), template=tracker.TEMPLATE)
        else:
            text = SESSION_NOTE_NO_TRACKERS.format(sid=sid)
        if state.is_paused(sid):
            text += "\nThis session is PAUSED. The user's next message resumes it."
        cwd = payload.get("cwd") or ""
        open_tasks = [tracker.info(t) for t in tracker.trackers(cwd)] if cwd else []
        open_tasks = [t for t in open_tasks if t["open"]]
        if open_tasks:
            text += "\nUnfinished tasks in this project (trackers): " + "; ".join(
                f'{t["slug"]} ({len(t["open"])} open stage(s))' for t in open_tasks[:10])
            text += ". If the user wants to continue one, they can run /check:resume <task>."
        try:
            text += _journal_note(sid, payload.get("source") or "", payload.get("cwd") or "")
        except Exception as e:  # noqa: BLE001  the rest of the session start still goes out
            print(f"checkpoint: the journal note failed: {e}", file=sys.stderr)
            text += f"\n(checkpoint's recovery summary failed: {type(e).__name__}; /check:doctor)"
        extra: dict[str, Any] = {}
        try:
            warn = _concurrency(sid, payload.get("source") or "", twin)
        except Exception as e:  # noqa: BLE001
            print(f"checkpoint: the concurrency check failed: {e}", file=sys.stderr)
            warn = ""
        if warn:
            text += "\n" + warn
            extra["systemMessage"] = "checkpoint: " + warn
        try:
            crash = _crash_notice(sid, payload.get("source") or "", payload.get("cwd") or "")
        except Exception as e:  # noqa: BLE001
            print(f"checkpoint: the crash notice failed: {e}", file=sys.stderr)
            crash = ""
        if crash:
            extra["systemMessage"] = (extra["systemMessage"] + " " + crash) if extra.get("systemMessage") else crash
        news = _degraded_news()
        if news:
            text += ("\ncheckpoint's gate ran degraded (without its Python half) since the last session "
                     "start: " + "; ".join(news) + ". While a session was paused its tool calls were "
                     "held; otherwise they went ahead.")
            msg = ("checkpoint: its gate ran degraded (" + news[0] + (f"; {len(news) - 1} more" if len(news) > 1 else "")
                   + "). /check:status and /check:doctor have details.")
            extra["systemMessage"] = (extra["systemMessage"] + " " + msg) if extra.get("systemMessage") else msg
        _emit(_with_failure(_ctx("SessionStart", text, **extra), res))
    except Exception as e:  # noqa: BLE001
        print(f"checkpoint: SessionStart failed: {e}", file=sys.stderr)
        return 0
    try:
        state.prune()
    except Exception as e:  # noqa: BLE001
        print(f"checkpoint: pruning failed: {e}", file=sys.stderr)
    return 0


def _open_elsewhere(sess: dict[str, Any], my_pid: int | None) -> int | None:
    """The pid of another live Claude process that has this session open
    (`claude --resume <id>` of a session still running in another terminal:
    two processes, one session id), else None. Not the process it last ran
    in when that one has moved on: after /clear the process holds a newer
    session (as live_sessions counts it), and a session whose run ended
    (SessionEnd, also the one /clear sends) isn't open anywhere."""
    old = sess.get("claude_pid")
    sid = sess.get("session_id") or ""
    if not isinstance(old, int) or old == my_pid:
        return None
    table = procs.table()
    if not table or not procs.is_claude(old, table):
        return None
    newest = max((s for s in state.all_sessions() if s.get("claude_pid") == old), key=state.started_ts, default=None)
    if newest is not None and newest.get("session_id") != sid:
        return None
    task = sess.get("task_id") or ""
    if task and summary.session_ended(task, sid):
        return None
    return old


def _concurrency(sid: str, source: str, twin: int | None) -> str:
    """Concurrent sessions on one task, said once at a session start and
    recorded (session.concurrent): this session also open in another
    process, a fork whose parent still runs, other live sessions on the task."""
    task = state.load_session(sid).get("task_id") or ""
    if not task:
        return ""
    table = procs.table()
    others = [s for s in (state.live_sessions(table) if table else [])
              if s.get("task_id") == task and s["session_id"] != sid]
    what, text = "", ""
    if twin:
        what = f"was opened again while another Claude process (pid {twin}) still had it open"
        text = (f"This session is also open in another Claude process (pid {twin}): two processes now write one "
                f"session and task ({task}). Close one of them; until then their tool calls and edits interleave.")
    elif others:
        names = ", ".join(f"{s['session_id'][:8]} (pid {s.get('claude_pid')})" for s in others[:4])
        what = f"runs alongside {names} on the same task ({source or 'start'})"
        text = (f"Other live sessions record into this task ({task}) too: {names}. Two sessions changing the same "
                "files overwrite each other: coordinate, or give this one a task of its own (/check:complete or "
                "/check:cancel there, or start fresh here with /clear).")
    if not what:
        return ""
    capture.record(sid, [{"kind": "session.concurrent", "session": sid,
                          "data": {"what": sanitize.clip(what, 300), "twin_pid": twin,
                                   "others": [s["session_id"] for s in others[:8]], "source": source}}])
    return text


SESSION_LIMIT = 7000   # the reconciliation and the summary at a session start


def _journal_note(sid: str, source: str, cwd: str) -> str:
    """What the journal adds at a session start: after a compaction, a fork
    or a resume of an interrupted or paused task, the reconciliation and its
    recovery summary; after a resume that follows a clean exit, only what
    changed since (nothing when nothing did; reconciled and recorded either
    way); at a fresh start, the unfinished tasks of this project (one line
    each)."""
    task = state.load_session(sid).get("task_id") or ""
    if source in ("compact", "resume", "fork") and task:
        meta = state.read_json(journal.jdir(task) / "meta.json", {}) or {}
        if not meta.get("goal"):
            return ""
        # Every resume, fork and compaction reconciles (invariant 16). A
        # session resumed after a clean exit has its whole conversation: it
        # hears only what changed since (files, HEAD, results gone stale,
        # anything still open), and nothing when nothing did (the reconcile
        # record says it was checked).
        from . import recovery
        with sanitize.remembered():     # the summary below scrubs the commands reconcile scrubbed
            was_paused = state.is_paused(sid)
            r = recovery.reconcile(task, sid, cwd=cwd)
            # (from the journal reconcile read: reconciling writes nothing)
            clean = source == "resume" and not was_paused and summary.previous_run_ended(task, sid, view=r.view)
            recovery.record(sid, r, f"session start ({source}{', after a clean exit' if clean else ''})")
            if clean:
                if not r.check_first():
                    return ""
                return ("\nThis session continues checkpoint task " + task + " after a clean exit. Reconciled with "
                        "the journal, the files and the process table; what changed since its last run:\n"
                        "<checkpoint-recovery>\n" + r.text(limit=SESSION_LIMIT) + "\n</checkpoint-recovery>")
            paused = (" This session is PAUSED: don't act on the checks yet; the user's next message resumes it."
                      if state.is_paused(sid) else "")
            return ("\nThis session continues checkpoint task " + task + "." + paused + " Reconciled with the "
                    "journal, the files and the process table, then its recovery summary (observed facts and marked "
                    "inferences):\n<checkpoint-recovery>\n"
                    + r.text(lambda n: summary.build(task, limit=n, cwd=cwd).text, limit=SESSION_LIMIT,
                             summary_max=3500)
                    + "\n</checkpoint-recovery>")
    if source not in ("startup", "clear"):
        return ""
    lines = []
    for m in summary.task_list(cwd, limit=8):
        if m.get("task") == task or (m.get("cwd") or "") != cwd:
            continue
        if (m.get("finished") or {}).get("how"):
            continue   # marked complete or canceled
        st_ = "interrupted (no clean end)" if summary.interrupted(m) else "ended"
        resume = f"; `claude --resume {m['last_session']}`" if m.get("last_session") else ""
        lines.append(f"{m['task']}: {sanitize.clip(m.get('goal'), 90)} [{st_}{resume}]")
        if len(lines) >= 3:
            break
    if not lines:
        return ""
    return ("\nRecent checkpoint tasks in this project (journal): " + "; ".join(lines)
            + ". To continue one: prefer its original session (`claude --resume <id>`, its whole conversation), or "
              "/check:resume <task id> here (an explicit continuation: reconciled, with the recovery summary). This "
              "session's task: " + task + ".")


CRASH_NOTICE_DAYS = 7   # an interrupted task is shown to the person at a session start this long


def _crash_notice(sid: str, source: str, cwd: str) -> str:
    """One line for the person (the context's task list reaches only the
    model) at a fresh start in a project whose newest unfinished task was
    interrupted (no clean end, its Claude process gone) in the last
    CRASH_NOTICE_DAYS: the task, its goal, and both ways to continue it."""
    if source not in ("startup", "clear") or not cwd:
        return ""
    own = state.load_session(sid).get("task_id") or ""
    since = time.time() - CRASH_NOTICE_DAYS * 86400
    hits = [m for m in core.unfinished_tasks(cwd, own=own) if m["interrupted"]
            and float(m.get("updated") or 0) >= since]
    if not hits:
        return ""
    m = hits[0]
    goal = sanitize.clip(" ".join(str(m.get("goal") or "").split()), 60)
    via = f"`claude --resume {m['last_session']}` continues its session, or " if m.get("last_session") else ""
    more = f" ({len(hits) - 1} more: /check:status)" if len(hits) > 1 else ""
    return f'checkpoint: task {m["task"]} was interrupted ("{goal}"): {via}/check:resume {m["task"]} picks it up here.{more}'


def _degraded_news() -> list[str]:
    """degraded/<sid> markers newer than the last ones reported (each is said
    once, at the next session start)."""
    seen = state.p("degraded-seen")
    try:
        since = float(seen.read_text().strip() or 0)
    except (OSError, ValueError):
        since = 0.0
    since = max(since, time.time() - state.DEGRADED_SHOW_HOURS * 3600)
    marks = state.degraded_markers(since)
    if marks:
        state.touch(seen, str(marks[0][2]))
    return [f"{sid[:8]} at {text}" for sid, text, _ in marks[:5]]


# -- UserPromptSubmit -------------------------------------------------------------

def _is_user_prompt(prompt: str) -> bool:
    p = prompt.lstrip()
    return not any(p.startswith(x) for x in state.NON_USER_PROMPT_PREFIXES)


def _queued_echo(sid: str, prompt: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """The pause command the gate (or a watcher) already applied while it
    waited in the queue, if this prompt is that command now running as a
    prompt of its own (when the turn ends, or absorbed into the turn: both
    fire UserPromptSubmit). Called for every user prompt while any is
    remembered (queued/<sid> sends them all to Python). Each one is
    remembered until its echo, or until another prompt comes while it is no
    longer queued (a prompt queued ahead of it doesn't count)."""
    sess = state.load_session(sid)
    entries = state.queued_pauses(sess)
    if not entries:
        if state.p("queued", state.safe_id(sid)).exists():
            state.drop_queued(sid)  # a stray marker: back to the fast path
        return None
    tp = payload.get("transcript_path") or sess.get("transcript_path") or ""
    fates = (transcript.queue_fates(transcript.queue_lines(tp), [e.get("ts") or "" for e in entries])
             if tp else {})
    text = prompt.strip()
    now = time.time()
    hit, drop = None, []
    for e in entries:  # oldest first: echoes come in queue order
        fate = fates.get(e.get("ts") or "")
        recent = now - float(e.get("at_ts") or 0) < state.QUEUED_ECHO_MAX_AGE_SECONDS
        if (hit is None and text == (e.get("content") or "").strip()
                # No trace in the transcript: trust a recent one. 'removed'
                # means absorbed without a prompt of its own: typed anew.
                and (fate in ("pending", "dequeued") or (fate is None and recent))):
            hit = e
        elif fate == "pending" and recent:
            continue  # still queued: its echo is yet to come
        # Echoed now, or the echo isn't coming. (A queue entry still pending
        # after hours is a stale record: stop waiting on it.)
        drop.append(e)
    if drop:
        state.drop_queued(sid, drop)
    return hit


def handle_prompt(payload: dict[str, Any]) -> dict[str, Any] | None:
    sid = _note_session(payload)
    prompt = payload.get("prompt") or ""
    if not sid:
        return None
    res = _ingest(sid)
    out = _handle_prompt(sid, prompt, payload)
    # The gateway in step with the pause state this prompt left (never
    # before it decides: a pause the gateway hasn't had, delivered just
    # before a resume, would apply on_pause interrupt to the jobs the person
    # is resuming). Still paused: a late pause goes there now; resumed: an
    # undelivered pause is dropped (gateway_resume), a delivered one resumed.
    core.gateway_settle(sid)
    return _with_failure(out, res)


def _handle_prompt(sid: str, prompt: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if state.noresume(sid) or not _is_user_prompt(prompt):
        # Not the user's own message (the mod saw isMeta or a scheduled,
        # automation or notification origin; or a known prefix): never resumes.
        if state.is_paused(sid):
            return _ctx("UserPromptSubmit", "This session is paused (checkpoint): this message isn't "
                        "from the user, so it didn't resume the session. Don't start any work; end "
                        "your turn with one line saying you're paused.")
        return None
    dirs = _dirs(payload)
    cwd = (dirs or [""])[0]
    echo = _queued_echo(sid, prompt, payload)
    cmd = transcript.parse_command(prompt)
    if cmd:
        name, args = cmd
        if name == "pause":
            plain = not prompt.strip().startswith("/")
            result = core.pause(sid, args, by="keyword" if plain else "command", echo=echo)
            text = f"<checkpoint-result>{result}</checkpoint-result>"
            if plain and state.is_paused(sid):
                text += ("\nThe user typed 'pause': don't start anything new. "
                         + ("Bring the tracker up to date, then end" if state.trackers_on() else "End")
                         + " your turn with one line saying you're paused.")
            return _ctx("UserPromptSubmit", text)
        if name == "status":
            return _ctx("UserPromptSubmit", f"<checkpoint-result>{core.status_text(sid)}</checkpoint-result>")
        if name == "save":
            ok, msg = core.save(sid, " ".join(args))
            return _ctx("UserPromptSubmit", f"<checkpoint-result>{msg}</checkpoint-result>",
                        systemMessage=f"checkpoint: {'saved' if ok else 'NOT saved'}.")
        if name == "doctor":
            return _ctx("UserPromptSubmit", f"<checkpoint-result>{core.doctor_text(sid)}</checkpoint-result>")
        if name == "resume":
            return _ctx("UserPromptSubmit", _resume(sid, cwd, args, dirs))
        # Inspection and management: answered here, never a resume, and
        # available while paused (no gate blocks its own recovery controls).
        if name == "list":
            return _ctx("UserPromptSubmit", f"<checkpoint-result>{core.list_checkpoints(sid, args[0] if args else '')}"
                        "</checkpoint-result>")
        if name == "diff":
            return _ctx("UserPromptSubmit", f"<checkpoint-result>{core.diff_text(sid, args)}</checkpoint-result>")
        if name == "stop":
            return _ctx("UserPromptSubmit", f"<checkpoint-result>{core.stop_job(sid, args)}</checkpoint-result>")
        if name == "cancel":
            res = core.cancel(sid, args)
            return _ctx("UserPromptSubmit", f"<checkpoint-result>{res}</checkpoint-result>\nThe user canceled the "
                        "task: don't continue its work. Reply with the result in one or two lines and stop.",
                        systemMessage="checkpoint: task canceled.")
        if name == "complete":
            return _ctx("UserPromptSubmit", f"<checkpoint-result>{core.complete(sid, args)}</checkpoint-result>")
        if name == "export":
            return _ctx("UserPromptSubmit", f"<checkpoint-result>{_export(sid, cwd, args)}</checkpoint-result>")
        if name == "forget":
            from . import purge
            _ok, msg = purge.forget(args[0] if args else "", sid=sid, cwd=cwd)
            return _ctx("UserPromptSubmit", f"<checkpoint-result>{msg}</checkpoint-result>")
        return None  # help: the command's own prompt answers it
    if state.is_paused(sid) or state.resume_pending(sid):
        return _ctx("UserPromptSubmit", core.resume_here(sid, dirs, prompt=prompt),
                    systemMessage="checkpoint: resumed this session (reconciled first).")
    return None


def _export(sid: str, cwd: str, args: list[str]) -> str:
    """/check:export [<task>|last] [<path>]: a sanitized bundle of the task."""
    from . import recovery
    ref = args[0] if args and not args[0].startswith(("/", "~", ".")) else ""
    out = next((a for a in args if a.startswith(("/", "~", "."))), "")
    task = core.find_task(ref, cwd, sid) if ref else (state.load_session(sid).get("task_id") or "")
    if sid:
        capture.ingest(sid)
    if not task:
        return f"No task matches '{ref}'." if ref else "This session has no task journal yet."
    ok, where = recovery.export(task, out)
    return (f"Exported task {task} to {where} (0600): its summary, checkpoints, a journal excerpt and references, "
            "every string sanitized again; no file contents or transcripts. Scrubbing is pattern-based: review the "
            "file before sharing it." if ok else
            f"Export of task {task} failed: {where}")


def _resume(sid: str, cwd: str, args: list[str], dirs: list[str]) -> str:
    """/check:resume [all | <task> | <checkpoint>]: every form reconciles
    before the work goes on (core.resume_context)."""
    if args and args[0] == "all":
        n = core.resume_all(except_sid=sid)
        body = core.resume_here(sid, dirs) if state.is_paused(sid) or state.resume_pending(sid) else ""
        msg = (f"Un-paused {n} other session(s). Each continues when you send it a message, "
               "and gets its resume notes then." if n else "No other session was paused.")
        return f"<checkpoint-result>{msg}</checkpoint-result>\n{body}".strip()
    if args:
        # What it names first: a reference that matches nothing (a typo)
        # must leave a paused session paused, unreconciled and unrecorded.
        found = core.resolve_ref(cwd, args[0], sid)
        paused = state.is_paused(sid) or state.resume_pending(sid)
        if not found[0] and not found[1]:
            msg = core.no_match(cwd, args[0])
            if not paused:
                return f"<checkpoint-result>{msg}</checkpoint-result>"
            return (f"<checkpoint-result>{msg} This session is STILL PAUSED: nothing was resumed. /check:resume alone "
                    "resumes it here (reconciled first), or name a task or checkpoint from the lists above."
                    "</checkpoint-result>\nThe session stays paused: don't start any work. Tell the user that "
                    "nothing matched and that the session is still paused, then end your turn.")
        gw = ""
        if paused:
            was = state.clear_paused(sid)
            if was:
                core.journal_event(sid, "resume", by="/check:resume " + sanitize.clip(args[0], 80))
            gw = core.gateway_resume(sid)
        ctx = core.resume_task(cwd, args[0], sid=sid, resolved=found)
        if gw and ctx.startswith("<checkpoint-resume>"):
            ctx = ctx.replace("<checkpoint-resume>", "<checkpoint-resume>\n\n" + gw, 1)
        if ctx.startswith("<checkpoint-resume>"):
            t = tracker.by_slug(cwd, args[0])
            if not t:
                return ctx + ("\nYou are picking up this task here: do the 'Check first' items before redoing "
                              "anything, then go on from the plan.")
            state.update_session(sid, task=t["slug"])
            return ctx + f"\nYou are picking up this task in a new session: write this session's id ({sid}) after 'Session:' in the tracker."
        return f"<checkpoint-result>{ctx}</checkpoint-result>"
    if state.is_paused(sid) or state.resume_pending(sid):
        return core.resume_here(sid, dirs)
    open_tasks = [tracker.info(t) for t in tracker.trackers(cwd)] if cwd else []
    names = ", ".join(t["slug"] for t in open_tasks if t["open"])
    msg = "This session wasn't paused."
    if names:
        msg += f" Unfinished trackers here: {names} (/check:resume <task> picks one up)."
    return f"<checkpoint-result>{msg}</checkpoint-result>"


def prompt_main() -> int:
    try:
        _emit(handle_prompt(_payload()))
    except Exception as e:  # noqa: BLE001
        print(f"checkpoint: prompt hook failed: {e}", file=sys.stderr)
        return 3  # fast.sh's fallback handles pause and resume
    return 0


# -- PreToolUse gate --------------------------------------------------------------

def allowed_while_paused(payload: dict[str, Any]) -> str:
    """'' if the call is denied while paused; 'tool' for a tool that stays
    allowed; 'tracker' for a write inside the project's work dir."""
    name = payload.get("tool_name") or ""
    if any(fnmatch.fnmatchcase(name, pat) for pat in state.ALLOW_WHILE_PAUSED):
        return "tool"
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        inp = payload.get("tool_input") or {}
        fp = inp.get("file_path") or inp.get("notebook_path") or ""
        dirs = _dirs(payload)
        if not fp or not dirs:
            return ""
        target = (Path(fp) if os.path.isabs(fp) else Path(payload.get("cwd") or dirs[0]) / fp).resolve()
        for d in dirs:
            try:
                target.relative_to(tracker.work_dir(d).resolve())
                return "tracker"
            except ValueError:
                continue
    return ""


Hit = tuple[dict[str, Any], list[str]]  # a queued pause command: (queue op, args)


def _unhandled(sid: str, hits: list[Hit]) -> list[Hit]:
    """The pending pause commands not yet handled (newer than queue_handled),
    oldest first; a pre-check only, claim_queue decides."""
    handled = state.load_session(sid).get("queue_handled") or ""
    return [h for h in hits if not h[0].get("timestamp") or h[0]["timestamp"] > handled]


def _claim(sid: str, hits: list[Hit]) -> list[Hit]:
    """Claim these queued pause commands, each by its own timestamp; returns
    the ones this process now applies (oldest first)."""
    mine = set(state.claim_queue(sid, [h[0].get("timestamp") or "" for h in hits]))
    return [h for h in hits if (h[0].get("timestamp") or "") in mine]


def check_queue(sid: str, payload: dict[str, Any]) -> bool:
    """Pause if pause commands are waiting in the transcript queue: every one
    not handled yet, oldest first (several can be typed before the next
    tool call). Records the last pause line seen so the shell pre-filter
    can skip Python."""
    tp = payload.get("transcript_path") or ""
    if not tp:
        return False
    lines = transcript.tail_lines(tp)
    state.touch(state.p("seen", state.safe_id(sid)), transcript.last_pause_line(lines))
    hits = _claim(sid, transcript.queued_pauses(lines))
    if not hits:
        return False
    _pause_queued(sid, hits, by="queued command")
    return True


def _pause_queued(sid: str, hits: list[Hit], by: str) -> list[dict[str, Any]]:
    """Apply pause commands found in the queue, oldest first, each exactly as
    typed: `all` or another session as its target, `now` killing each
    target's running command. Remembers them all first, each on its own, so
    that when one runs as a prompt of its own it isn't applied a second
    time, even if that comes while they are still being applied. Returns
    the commands `now` killed."""
    if not hits:
        return []
    now = time.time()
    entries = [{"content": op.get("content") or "", "ts": op.get("timestamp") or "",
                "at": state.now_iso(), "at_ts": now, "result": ""} for op, _ in hits]
    state.add_queued(sid, *entries)
    killed: list[dict[str, Any]] = []
    for i, ((_op, args), entry) in enumerate(zip(hits, entries)):
        try:
            result = core.pause(sid, args, by=by + (" (now)" if "now" in args else ""),
                                killed_out=killed)
        except BaseException:
            state.drop_queued(sid, entries[i:])  # not applied: their echoes must apply them
            raise
        state.update_queued(sid, entry, result=result)
    return killed


def gate(payload: dict[str, Any]) -> dict[str, Any] | None:
    sid = _note_session(payload)
    if not sid:
        return None
    if state.is_paused(sid):
        # Pause commands typed into this paused (still busy) session are
        # applied now too: they may name other sessions, or `all`.
        try:
            check_queue(sid, payload)
        except Exception as e:  # noqa: BLE001  this session stays paused: still deny
            print(f"checkpoint: queued pause commands not applied: {e}", file=sys.stderr)
        # A pause the gateway hasn't had yet (made without Python, or while it
        # was away): its jobs' pause policies apply now (once per pause).
        core.gateway_settle(sid)
    else:
        check_queue(sid, payload)
        if not state.is_paused(sid):
            return None
    allowed = allowed_while_paused(payload)
    if allowed == "tracker":
        # The agent stops by updating its tracker: don't let it stall at a
        # permission prompt while the user is away.
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "allow",
            "permissionDecisionReason": "checkpoint: tracker update while paused"}}
    if allowed:
        return None
    sess = state.load_session(sid)
    count = int(sess.get("deny_count") or 0) + 1
    state.update_session(sid, deny_count=count)
    _record_denied(sid, payload)
    dirs = _dirs(payload)
    if not sess.get("checkpoint_written"):
        core.write_checkpoint(sid, dirs=dirs)
    cwd = (dirs or [""])[0]
    t = _tracker_for(payload, sid)
    if payload.get("agent_id"):
        reason = ("Paused by the user (checkpoint). Don't call more tools: stop here and "
                  "return a short report of what you did and what is left.")
    elif not state.trackers_on():
        reason = ("Paused by the user (checkpoint). Don't start new work: end your turn with one line saying "
                  "you're paused.")
    elif t:
        reason = (f"Paused by the user (checkpoint). Don't start new work. Update {t['path']} "
                  "(Now, Jobs, Resume notes; Write/Edit to it are allowed), then end your "
                  "turn with one line saying you're paused.")
    else:
        reason = ("Paused by the user (checkpoint). Don't start new work. If this task has "
                  f"several steps, write {tracker.work_dir(cwd or '.')}/<task-slug>/TRACKER.md "
                  "(what's done, Now, next action; Write is allowed there), then end your "
                  "turn with one line saying you're paused.")
    out: dict[str, Any] = {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": reason}}
    if count > state.MAX_DENIES:
        # continue:false alone would let the tool run (Spike 3): keep the deny.
        out["continue"] = False
        out["stopReason"] = "Paused by checkpoint. Send any message to resume."
    return out


def _record_denied(sid: str, payload: dict[str, Any]) -> None:
    """The journal learns the call never ran (else its intent would read as
    in flight). Best effort: the deny goes out either way."""
    tid = payload.get("tool_use_id") or ""
    if not tid:
        return
    try:
        rec: dict[str, Any] = {"kind": "tool.outcome", "op": str(tid), "session": sid,
                               "data": {"tool": sanitize.clip(payload.get("tool_name") or "?", 120), "ok": False,
                                        "denied": "paused"}}
        if payload.get("agent_id"):
            rec["agent"] = sanitize.clip(payload["agent_id"], 80)
        capture.record(sid, [rec])
    except Exception as e:  # noqa: BLE001
        print(f"checkpoint: recording a denied call failed: {e}", file=sys.stderr)


PAUSED_GATE_FAILED = ("checkpoint: this session is paused, and checking this call failed, so it is "
                      "held. Don't start new work: end your turn with one line saying you're paused.")


def gate_main() -> int:
    payload = _payload()
    try:
        _emit(gate(payload))
    except Exception as e:  # noqa: BLE001
        print(f"checkpoint: gate failed: {e}", file=sys.stderr)
        sid = payload.get("session_id") or ""
        try:
            paused = bool(sid) and state.is_paused(sid)
        except Exception:  # noqa: BLE001
            paused = True
        if paused:  # fail closed while paused
            _emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                          "permissionDecisionReason": PAUSED_GATE_FAILED}})
            return 0
        return 3  # not paused: fast.sh lets the call go ahead and records it
    return 0


# -- PostToolUse nudge -------------------------------------------------------------

def post(payload: dict[str, Any]) -> dict[str, Any] | None:
    sid = _note_session(payload)
    if not sid:
        return None
    return _with_failure(_post(sid, payload), _ingest(sid))


def _post(sid: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    state.touch(state.p("count", state.safe_id(sid)), "0")
    if state.is_paused(sid) or not state.trackers_on():
        return None
    cwd = (_dirs(payload) or [""])[0]
    n = state.nudge_every()
    t = _tracker_for(payload, sid)
    if t:
        return _ctx("PostToolUse", f"checkpoint: {n}+ tool calls since {t['path']} was last "
                    "updated. Bring its Now section (and stage statuses) up to date.")
    sess = state.load_session(sid)
    if not sess.get("nudged_no_tracker"):
        state.update_session(sid, nudged_no_tracker=True)
        return _ctx("PostToolUse", f"checkpoint: {n}+ tool calls and no tracker. If this "
                    f"task has several steps, start {tracker.work_dir(cwd or '.')}/<task-slug>/TRACKER.md.")
    return None


def post_main() -> int:
    try:
        _emit(post(_payload()))
    except Exception as e:  # noqa: BLE001
        print(f"checkpoint: post hook failed: {e}", file=sys.stderr)
        return 3
    return 0


# -- Stop ---------------------------------------------------------------------------

def stop(payload: dict[str, Any]) -> dict[str, Any] | None:
    sid = _note_session(payload)
    if not sid:
        return None
    return _with_failure(_stop(sid, payload), _ingest(sid, sync=True))


def _stop(sid: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if state.is_paused(sid):
        core.write_checkpoint(sid, dirs=_dirs(payload))
        state.update_session(sid, stopped_paused_at=state.now_iso())
        return None
    if payload.get("stop_hook_active") or not state.trackers_on():
        return None
    t = _tracker_for(payload, sid)
    if not t or not t["in_progress"]:
        return None
    try:
        tools_this_turn = int(state.p("turn", state.safe_id(sid)).read_text().strip() or 0)
        turn_start = state.p("turn-start", state.safe_id(sid)).stat().st_mtime
    except (OSError, ValueError):
        return None
    if tools_this_turn == 0 or t["mtime"] >= turn_start:
        return None
    return {"decision": "block",
            "reason": (f"checkpoint: before you stop, update {t['path']}: the Now section "
                       "(current step, next action) and any stage whose status changed. Then stop.")}


def stop_main() -> int:
    try:
        _emit(stop(_payload()))
    except Exception as e:  # noqa: BLE001
        print(f"checkpoint: stop hook failed: {e}", file=sys.stderr)
        return 3
    return 0


# -- watcher (pause now during a running command) -------------------------------------

def watch_fire(sid: str, tool_use_id: str) -> int:
    """Called by hooks/watch.sh when it sees a possible `pause now`. Verifies
    the request and acts on it: now/<sid> (from `pause <this> now` or
    `pause all now` elsewhere) pauses this session and kills its foreground
    command(s); when a queued pause command in this session's transcript
    not handled yet has `now`, every one not handled yet is applied, oldest
    first, as typed (each may name another session, or all), just as the
    gate would at the next tool call. Prints the killed commands of this
    watcher's call, so the watcher knows to stop."""
    state.migrate_legacy()
    sess = state.load_session(sid)
    killed: list[dict[str, Any]] = []
    self_now = False
    if state.p("now", state.safe_id(sid)).exists():
        self_now = True
        state.remove(state.p("now", state.safe_id(sid)))
    tp = sess.get("transcript_path") or ""
    if tp:
        hits = _unhandled(sid, transcript.queued_pauses(transcript.tail_lines(tp)))
        if any("now" in args for _, args in hits):
            hits = _claim(sid, hits)
            killed = [c for c in _pause_queued(sid, hits, by="queued command")
                      if c.get("session_id") == sid]
            # pause targets now/<sid> for a watcher; this one acts on it now.
            if any("now" in args and core.pause_targets_self(sid, args) for _, args in hits):
                state.remove(state.p("now", state.safe_id(sid)))
    if self_now:
        if not state.is_paused(sid):
            state.set_paused(sid, by="pause now")
            core.journal_event(sid, "pause", by="pause now", now=True)
        killed += core.kill_foreground(sid)
    mine = [c for c in killed if c.get("tool_use_id") == tool_use_id]
    if killed or self_now:
        print(json.dumps({"killed": mine, "others": len(killed) - len(mine)}))
    return 0


# -- the mod's immediate pause ----------------------------------------------------------

def sync_pause(sid: str) -> int:
    """The mod paused `sid` by writing flags/<sid> (hooks/env.sh flag pause),
    at once and without Python. Afterwards (in the background) it calls this:
    give the session record its pause fields and write the checkpoint, as the
    gate's first deny would have (the mod's own gate denies first, so the
    Python gate never sees those calls)."""
    state.migrate_legacy()
    if not state.is_paused(sid):
        print(json.dumps({"paused": False}))
        return 0
    state.sync_flag(sid)
    by = state.flag_info(sid)[1] or "mod"
    err = core.journal_event(sid, "pause", by=by)
    path = core.write_checkpoint(sid)
    # The gateway's half, unless the mod's own gw-pause got there first (one
    # delivery per pause: core.gateway_pause holds the session's gateway lock).
    gw = core.gateway_pause(sid, reason=f"checkpoint pause ({by})")
    print(json.dumps({"paused": True, "checkpoint": str(path), **({"journal_error": err} if err else {}),
                      **({"gateway": gw} if gw else {})}))
    return 0
