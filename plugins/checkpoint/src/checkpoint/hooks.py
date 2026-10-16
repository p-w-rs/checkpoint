"""Hook entry points, run via run.py after hooks/fast.sh decides Python is needed.

session_main -> SessionStart: record the session, inject the tracker/jobs note.
prompt_main  -> UserPromptSubmit: /check:* commands and the plain `pause`
                keyword; any other user message in a paused session resumes it.
gate_main    -> PreToolUse(*): pause on a queued command; deny while paused.
post_main    -> PostToolUse(*): tracker reminder every N tool calls.
stop_main    -> Stop: refresh the checkpoint when paused; tracker nudge otherwise.
watch_main   -> hooks/watch.sh: `pause now` while a foreground command runs.

The gate fails open: a broken hook must never freeze every session.
"""
from __future__ import annotations

import fnmatch
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import core, procs, state, tracker, transcript

SESSION_NOTE = """\
checkpoint is on. Work so that you can be paused at any tool call and resumed later, possibly in a new session:
- Tracker: for any task with more than a few steps, keep <cwd>/{work}/<task-slug>/TRACKER.md (template below). Create it as soon as the task clearly has several steps, put this session's id after "Session:", keep the Now section current, and update a stage's status when it changes. Put long detail in linked stage docs, not in the tracker.
- Mark a stage "accepted" only when the user says so in chat.
- Anything likely to run more than ~2 minutes goes through the checkpoint job_start tool (it survives pauses and new sessions), with a row in the tracker's Jobs table and a redo_hint.
- If a tool call is denied with "Paused by the user", don't start new work: bring the tracker up to date (Write/Edit to it are allowed), then end your turn with one line saying you're paused.
This session's id: {sid}
Tracker template:
{template}"""


def _emit(obj: dict[str, Any] | None) -> None:
    if obj:
        print(json.dumps(obj))


def _ctx(event: str, text: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    out.update(extra)
    return out


def _payload() -> dict[str, Any]:
    try:
        data = json.load(sys.stdin)
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def _note_session(payload: dict[str, Any]) -> str:
    """Make sure the session record has cwd/transcript (and a Claude pid)."""
    sid = payload.get("session_id") or ""
    if not sid:
        return ""
    sess = state.load_session(sid)
    fields: dict[str, Any] = {}
    for k in ("cwd", "transcript_path"):
        # cwd is the session's project folder: set once, not moved by each `cd`.
        if k == "cwd" and sess.get("cwd"):
            continue
        if payload.get(k) and sess.get(k) != payload[k]:
            fields[k] = payload[k]
    if not sess.get("claude_pid"):
        fields["claude_pid"] = procs.find_claude(int(os.environ.get("CHECKPOINT_HOOK_PPID") or os.getppid()))
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
    for d in _dirs(payload):
        t = tracker.for_session(d, sid)
        if t:
            return t
    return None


# -- SessionStart ---------------------------------------------------------------

def session_main() -> int:
    payload = _payload()
    sid = payload.get("session_id") or ""
    if not sid:
        return 0
    try:
        claude_pid = procs.find_claude(int(os.environ.get("CHECKPOINT_HOOK_PPID") or os.getppid()))
        state.update_session(sid, cwd=os.environ.get("CLAUDE_PROJECT_DIR")
                             or payload.get("cwd") or os.getcwd(),
                             transcript_path=payload.get("transcript_path") or "",
                             claude_pid=claude_pid, source=payload.get("source") or "",
                             started=state.now_iso())
        text = SESSION_NOTE.format(sid=sid, work=state.work_dir_name(), template=tracker.TEMPLATE)
        if state.is_paused(sid):
            text += "\nThis session is PAUSED. The user's next message resumes it."
        cwd = payload.get("cwd") or ""
        open_tasks = [tracker.info(t) for t in tracker.trackers(cwd)] if cwd else []
        open_tasks = [t for t in open_tasks if t["open"]]
        if open_tasks:
            text += "\nUnfinished tasks in this project (trackers): " + "; ".join(
                f'{t["slug"]} ({len(t["open"])} open stage(s))' for t in open_tasks[:10])
            text += ". If the user wants to continue one, they can run /check:resume <task>."
        _emit(_ctx("SessionStart", text))
    except Exception as e:  # noqa: BLE001
        print(f"checkpoint: SessionStart failed: {e}", file=sys.stderr)
    return 0


# -- UserPromptSubmit -------------------------------------------------------------

def _is_user_prompt(prompt: str, cfg: dict[str, Any]) -> bool:
    p = prompt.lstrip()
    return not any(p.startswith(x) for x in cfg["non_user_prompt_prefixes"])


def handle_prompt(payload: dict[str, Any]) -> dict[str, Any] | None:
    cfg = state.load_config()
    sid = _note_session(payload)
    prompt = payload.get("prompt") or ""
    if not sid or not _is_user_prompt(prompt, cfg):
        return None
    cwd = (_dirs(payload) or [""])[0]
    cmd = transcript.parse_command(prompt)
    if cmd:
        name, args = cmd
        if name == "pause":
            plain = not prompt.strip().startswith("/")
            result = core.pause(sid, args, by="keyword" if plain else "command")
            text = f"<checkpoint-result>{result}</checkpoint-result>"
            if plain:
                text += ("\nThe user typed 'pause': don't start anything new. Bring the tracker "
                         "up to date, then end your turn with one line saying you're paused.")
            return _ctx("UserPromptSubmit", text)
        if name == "status":
            return _ctx("UserPromptSubmit", f"<checkpoint-result>{core.status_text()}</checkpoint-result>")
        if name == "resume":
            return _ctx("UserPromptSubmit", _resume(sid, cwd, args))
        return None  # help: the command's own prompt answers it
    if state.is_paused(sid):
        return _ctx("UserPromptSubmit", core.resume_here(sid),
                    systemMessage="checkpoint: resumed this session.")
    return None


def _resume(sid: str, cwd: str, args: list[str]) -> str:
    if args and args[0] == "all":
        was = state.clear_paused_all()
        if state.is_paused(sid):
            body = core.resume_here(sid)
        else:
            state.clear_paused(sid)
            body = ""
        msg = ("Pause-all cleared. Other paused sessions resume when you send each one a message."
               if was else "Pause-all wasn't on.")
        return f"<checkpoint-result>{msg}</checkpoint-result>\n{body}".strip()
    if args:
        if state.is_paused(sid):
            state.clear_paused(sid)
        ctx = core.resume_task(cwd, args[0])
        if ctx.startswith("<checkpoint-resume>"):
            t = tracker.by_slug(cwd, args[0])
            state.update_session(sid, task=t["slug"] if t else args[0])
            return ctx + f"\nYou are picking up this task in a new session: write this session's id ({sid}) after 'Session:' in the tracker."
        return f"<checkpoint-result>{ctx}</checkpoint-result>"
    if state.is_paused(sid):
        return core.resume_here(sid)
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
    return 0


# -- PreToolUse gate --------------------------------------------------------------

def allowed_while_paused(payload: dict[str, Any], cfg: dict[str, Any]) -> bool:
    name = payload.get("tool_name") or ""
    if any(fnmatch.fnmatchcase(name, pat) for pat in cfg["allow_while_paused"]):
        return True
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        inp = payload.get("tool_input") or {}
        fp = inp.get("file_path") or inp.get("notebook_path") or ""
        dirs = _dirs(payload)
        if not fp or not dirs:
            return False
        target = (Path(fp) if os.path.isabs(fp) else Path(payload.get("cwd") or dirs[0]) / fp).resolve()
        for d in dirs:
            try:
                target.relative_to(tracker.work_dir(d).resolve())
                return True
            except ValueError:
                continue
    return False


def check_queue(sid: str, payload: dict[str, Any]) -> bool:
    """Pause if a pause command is waiting in the transcript queue. Records
    the last pause line seen so the shell pre-filter can skip Python."""
    tp = payload.get("transcript_path") or ""
    if not tp:
        return False
    lines = transcript.tail_lines(tp)
    state.touch(state.p("seen", state.safe_id(sid)), transcript.last_pause_line(lines))
    hit = transcript.queued_pause(lines)
    if not hit:
        return False
    op, args = hit
    ts = op.get("timestamp") or ""
    sess = state.load_session(sid)
    if ts and ts <= (sess.get("queue_handled") or ""):
        return False
    state.update_session(sid, queue_handled=ts)
    core.pause(sid, [a for a in args if a != "now"] if "now" in args else args, by="queued command")
    if "now" in args:
        core.kill_foreground(sid)
    return True


def gate(payload: dict[str, Any]) -> dict[str, Any] | None:
    cfg = state.load_config()
    sid = _note_session(payload)
    if not sid:
        return None
    if not state.is_paused(sid):
        check_queue(sid, payload)
        if not state.is_paused(sid):
            return None
    if allowed_while_paused(payload, cfg):
        return None
    sess = state.load_session(sid)
    count = int(sess.get("deny_count") or 0) + 1
    state.update_session(sid, deny_count=count)
    if not sess.get("checkpoint_written"):
        core.write_checkpoint(sid)
    cwd = (_dirs(payload) or [""])[0]
    t = _tracker_for(payload, sid)
    if payload.get("agent_id"):
        reason = ("Paused by the user (checkpoint). Don't call more tools: stop here and "
                  "return a short report of what you did and what is left.")
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
    if count > int(cfg["max_denies"]):
        # continue:false alone would let the tool run (Spike 3): keep the deny.
        out["continue"] = False
        out["stopReason"] = "Paused by checkpoint. Send any message to resume."
    return out


def gate_main() -> int:
    try:
        _emit(gate(_payload()))
    except Exception as e:  # noqa: BLE001
        print(f"checkpoint: gate failed open: {e}", file=sys.stderr)
    return 0


# -- PostToolUse nudge -------------------------------------------------------------

def post(payload: dict[str, Any]) -> dict[str, Any] | None:
    sid = _note_session(payload)
    if not sid:
        return None
    state.touch(state.p("count", state.safe_id(sid)), "0")
    if state.is_paused(sid):
        return None
    cwd = (_dirs(payload) or [""])[0]
    n = state.load_config()["nudge_every"]
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
    return 0


# -- Stop ---------------------------------------------------------------------------

def stop(payload: dict[str, Any]) -> dict[str, Any] | None:
    sid = _note_session(payload)
    if not sid:
        return None
    if state.is_paused(sid):
        core.write_checkpoint(sid)
        state.update_session(sid, stopped_paused_at=state.now_iso())
        return None
    if payload.get("stop_hook_active"):
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
    return 0


# -- watcher (pause now during a running command) -------------------------------------

def watch_fire(sid: str, tool_use_id: str) -> int:
    """Called by hooks/watch.sh when it sees a possible `pause now`. Verifies
    the request, pauses the session and kills its foreground command(s)."""
    sess = state.load_session(sid)
    want = False
    if state.p("now", state.safe_id(sid)).exists():
        want = True
        state.remove(state.p("now", state.safe_id(sid)))
    run_file = state.p("running", state.safe_id(tool_use_id) + ".json")
    now_all = state.p("now-all")
    if now_all.exists() and run_file.exists() and now_all.stat().st_mtime >= run_file.stat().st_mtime:
        want = True
    tp = sess.get("transcript_path") or ""
    if tp:
        lines = transcript.tail_lines(tp)
        hit = transcript.queued_pause(lines)
        if hit and "now" in hit[1] and (hit[0].get("timestamp") or "") > (sess.get("queue_handled") or ""):
            state.update_session(sid, queue_handled=hit[0].get("timestamp") or "")
            args = [a for a in hit[1] if a != "now"]
            core.pause(sid, args, by="queued command (now)")
            want = True
    if not want:
        return 0
    if not state.is_paused(sid):
        state.set_paused(sid, by="pause now")
    killed = core.kill_foreground(sid)
    print(json.dumps({"killed": killed}))
    return 0
