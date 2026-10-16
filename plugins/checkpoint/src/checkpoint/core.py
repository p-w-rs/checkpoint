"""Pause, resume and status: the logic behind the hooks, commands and CLI."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from . import jobs, procs, state, tracker, transcript


# -- checkpoint ---------------------------------------------------------------

def _git(cwd: str, *args: str) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def git_snapshot(cwd: str) -> dict[str, Any]:
    if not cwd or not _git(cwd, "rev-parse", "--is-inside-work-tree").strip():
        return {}
    status = _git(cwd, "status", "--porcelain").splitlines()
    return {"head": _git(cwd, "rev-parse", "HEAD").strip(),
            "status": status[:200],
            "diff_stat": _git(cwd, "diff", "--stat").strip().splitlines()[-1:] or []}


def checkpoint_path(sid: str, cwd: str) -> Path:
    t = tracker.for_session(cwd, sid) if cwd else None
    if t:
        return Path(t["path"]).parent / "checkpoint.json"
    return state.p("checkpoints", state.safe_id(sid) + ".json")


def write_checkpoint(sid: str, reason: str = "") -> Path:
    """Plain facts at pause time. Written by the gate on its first deny and
    refreshed by Stop (which doesn't fire when continue:false ends a turn)."""
    sess = state.load_session(sid)
    cwd = sess.get("cwd") or ""
    t = tracker.for_session(cwd, sid) if cwd else None
    lines = transcript.tail_lines(sess["transcript_path"]) if sess.get("transcript_path") else []
    data = {
        "session_id": sid,
        "cwd": cwd,
        "paused_at": sess.get("paused_at") or state.now_iso(),
        "written_at": state.now_iso(),
        "reason": reason or sess.get("paused_by") or "",
        "tracker": t["path"] if t else None,
        "stages_in_progress": [f'{s["n"]}. {s["stage"]}' for s in (t["in_progress"] if t else [])],
        "last_tool_calls": transcript.recent_tool_calls(lines, 10),
        "git": git_snapshot(cwd),
        "jobs": jobs.status(session_id=sid).get("jobs", []),
        "interrupted": sess.get("interrupted") or [],
    }
    path = checkpoint_path(sid, cwd)
    state.write_json(path, data)
    state.update_session(sid, checkpoint=str(path), checkpoint_written=True,
                         task=t["slug"] if t else sess.get("task"))
    return path


# -- pause --------------------------------------------------------------------

def kill_foreground(sid: str) -> list[dict[str, Any]]:
    """Kill this session's running foreground Bash commands (the ones its
    watchers guard). Records them in the session for the checkpoint."""
    sess = state.load_session(sid)
    claude_pid = sess.get("claude_pid")
    grace = float(state.load_config()["kill_grace_seconds"])
    killed = []
    for f in state.p("running").glob("*.json"):
        rec = state.read_json(f, {}) or {}
        if rec.get("session_id") != sid:
            continue
        command = ((rec.get("tool_input") or {}).get("command")) or ""
        cpid = rec.get("claude_pid") or claude_pid
        if not command or not cpid:
            continue
        for shell in procs.foreground_shells(int(cpid), command):
            res = procs.kill_tree(shell, grace=grace, protect={int(cpid)})
            killed.append({"command": command[:300], "tool_use_id": rec.get("tool_use_id"),
                           "at": state.now_iso(), "processes": len(res["killed"])})
    if killed:
        sess = state.load_session(sid)
        state.update_session(sid, interrupted=(sess.get("interrupted") or []) + killed)
    return killed


def pause(sid: str, args: list[str], by: str) -> str:
    """Handle `pause [now] [all] [--stop-jobs] [<session>]`. Returns a short
    result for the user."""
    now = "now" in args
    stop_jobs = "--stop-jobs" in args
    rest = [a for a in args if a not in ("now", "all", "--stop-jobs")]
    msgs = []
    if "all" in args:
        state.set_paused_all(by, now=now)
        msgs.append("Pausing every session: each stops at its next tool call. "
                    "Idle sessions stay paused until you send them a message.")
        targets = [s["session_id"] for s in state.all_sessions()]
    elif rest:
        s = state.find_session(rest[0])
        if not s:
            return f"No single session matches '{rest[0]}'. /check:status lists them."
        state.set_paused(s["session_id"], by, now=now)
        msgs.append(f"Pausing session {s['session_id'][:8]} at its next tool call.")
        targets = [s["session_id"]]
    else:
        before = state.load_session(sid)
        fresh = state.set_paused(sid, by, now=now)
        if fresh:
            msgs.append("Paused: this session stops at its next tool call. Any message resumes it.")
        else:
            # Typically the queued command itself, arriving after the gate or
            # watcher already acted on it: say what actually happened.
            msg = "Already paused"
            if (before.get("paused_by") or "").startswith("queued command"):
                msg += (f" (since {(before.get('paused_at') or '')[11:19]}: it took effect "
                        "at the next tool call while the session was busy)")
            msgs.append(msg + ".")
            earlier = before.get("interrupted") or []
            if earlier:
                msgs.append("Stopped then: " + ", ".join(
                    f"`{c.get('command', '')[:80]}`" for c in earlier) + ".")
        targets = [sid]
    if now:
        n = sum(len(kill_foreground(t)) for t in targets)
        if n:
            msgs.append(f"Stopped {n} running command(s) now.")
        elif not any(state.load_session(t).get("interrupted") for t in targets):
            msgs.append("No command was running.")
    if stop_jobs:
        stopped = []
        for t in targets:
            for j in jobs.status(session_id=t).get("jobs", []):
                if j["state"] == "running":
                    jobs.stop(j["id"], reason="pause --stop-jobs")
                    stopped.append(j["name"])
        msgs.append(f"Stopped jobs: {', '.join(stopped)}." if stopped else "No running jobs to stop.")
    elif now:
        msgs.append("Detached jobs keep running (add --stop-jobs to stop them).")
    return " ".join(msgs)


# -- resume -------------------------------------------------------------------

def _changes_since(cp: dict[str, Any], cwd: str) -> list[str]:
    out = []
    try:
        paused = time.mktime(time.strptime(cp.get("paused_at", "")[:19], "%Y-%m-%dT%H:%M:%S"))
        mins = int((time.time() - paused) / 60)
        out.append(f"Paused for {mins // 60} h {mins % 60} min." if mins >= 60 else f"Paused for {mins} min.")
    except ValueError:
        pass
    before = {j["id"]: j for j in cp.get("jobs") or []}
    now_jobs = {j["id"]: j for j in jobs.status(session_id=cp.get("session_id")).get("jobs", [])}
    for jid, j in now_jobs.items():
        b = before.get(jid, {})
        if b.get("state") != j.get("state") or j.get("state") != "running":
            line = f"Job {j['name']}: {j['state']}"
            if j.get("exit_code") is not None:
                line += f" (exit {j['exit_code']})"
            if j["state"] != "running":
                t = jobs.tail(jid, 5).get("tail", "")
                if t:
                    line += "; last lines:\n    " + t.replace("\n", "\n    ")
            out.append(line)
    g = cp.get("git") or {}
    if g:
        now = git_snapshot(cwd)
        if now.get("head") and now["head"] != g.get("head"):
            out.append(f"Git HEAD moved: {g.get('head', '')[:10]} -> {now['head'][:10]}.")
        changed = sorted(set(now.get("status", [])) - set(g.get("status", [])))
        if changed:
            out.append("Files changed since the pause (git status):\n    " + "\n    ".join(changed[:30]))
    return out


def resume_context(t: dict[str, Any] | None, cp: dict[str, Any] | None, cwd: str) -> str:
    parts = ["<checkpoint-resume>"]
    if t:
        parts.append(f"Tracker: {t['path']}")
        for sec in ("Now", "Resume notes"):
            if t["sections"].get(sec):
                parts.append(f"## {sec}\n{t['sections'][sec]}")
        if t["open"]:
            parts.append("Open stages: " + "; ".join(f'{s["n"]}. {s["stage"]} ({s["status"]})'
                                                     for s in t["open"]))
    if cp:
        parts.append("Checkpoint at pause:\n" + json.dumps(
            {k: cp.get(k) for k in ("paused_at", "stages_in_progress", "last_tool_calls",
                                    "interrupted", "jobs")}, indent=1)[:4000])
        changes = _changes_since(cp, cwd)
        if changes:
            parts.append("What changed since:\n- " + "\n- ".join(changes))
        if cp.get("interrupted"):
            cmds = ", ".join(f"`{c.get('command', '')[:80]}`" for c in cp["interrupted"])
            parts.append(f"`pause now` killed {cmds}. Unless the tracker says otherwise (e.g. "
                         "the kill was intended or the work is already redone), mark that "
                         "stage 'interrupted' and redo it from the start of the stage.")
    parts.append("Resume: check the tracker's 'Check first' items and the changes above, "
                 "then continue from 'Next action'. Keep the tracker current.")
    parts.append("</checkpoint-resume>")
    return "\n\n".join(parts)


def resume_here(sid: str) -> str:
    """Resume this session (it was paused): clear the flag, build the context."""
    state.clear_paused(sid)
    sess = state.load_session(sid)
    cwd = sess.get("cwd") or ""
    t = tracker.for_session(cwd, sid) if cwd else None
    if not t and cwd and sess.get("task"):
        # Another session may have taken the tracker over meanwhile (its id
        # now leads the Session: line); the task we paused on still applies.
        t = tracker.by_slug(cwd, sess["task"])
    cp = state.read_json(Path(sess["checkpoint"]), None) if sess.get("checkpoint") else None
    state.update_session(sid, interrupted=[])
    return resume_context(t, cp, cwd)


def resume_task(cwd: str, slug: str) -> str:
    t = tracker.by_slug(cwd, slug)
    if not t:
        names = ", ".join(x.parent.name for x in tracker.trackers(cwd)) or "none"
        return f"No tracker matches '{slug}' in {tracker.work_dir(cwd)} (trackers: {names})."
    cp = state.read_json(Path(t["path"]).parent / "checkpoint.json", None)
    return resume_context(t, cp, cwd)


# -- status -------------------------------------------------------------------

def status_text() -> str:
    lines = []
    pa = state.paused_all()
    if pa:
        lines.append(f"PAUSE ALL is on (since {pa.get('at', '?')}). /check:resume all clears it.")
    rows = []
    for s in state.all_sessions():
        sid = s["session_id"]
        live = procs.alive(s.get("claude_pid"))
        if not live and not state.is_paused(sid):
            continue
        st = "paused" if state.is_paused(sid) else "working/idle"
        if not live:
            st += " (process gone)"
        task = s.get("task") or ""
        if not task and s.get("cwd"):
            t = tracker.for_session(s["cwd"], sid)
            task = t["slug"] if t else ""
        rows.append(f"- {sid[:8]}  {st:<22} {s.get('cwd', '?')}  {('task: ' + task) if task else ''}".rstrip())
    lines.append("Sessions:" if rows else "No live sessions recorded.")
    lines.extend(rows)
    js = [j for j in jobs.status().get("jobs", []) if j["state"] == "running" or
          time.time() - time.mktime(time.strptime(j.get("started", "1970-01-01T00:00:00")[:19],
                                                  "%Y-%m-%dT%H:%M:%S")) < 86400]
    if js:
        lines.append("Jobs (running or started in the last day):")
        for j in js:
            ex = f" exit {j['exit_code']}" if j.get("exit_code") is not None else ""
            lines.append(f"- {j['name']} [{j['id']}]: {j['state']}{ex}, {j.get('runtime_s', '?')} s")
    return "\n".join(lines)
