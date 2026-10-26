"""Pause, resume and status: the logic behind the hooks, commands and CLI."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

from . import capture, gateway, jobs, journal, procs, recovery, sanitize, state, summary, tracker, transcript


# -- trackers -----------------------------------------------------------------

def session_tracker(sid: str, dirs: Iterable[str]) -> dict[str, Any] | None:
    """The tracker this session works on, looking in each folder: first one
    that names the session, then one with a stage in progress that names no
    session and that this session wrote. Never a tracker that names another
    session, or one only another session wrote (that is its work)."""
    dirs = [d for d in dirs if d]
    for d in dirs:
        t = tracker.owned(d, sid)
        if t:
            return t
    written = state.written_trackers(sid)
    for d in dirs:
        t = tracker.unclaimed(d, written)
        if t:
            return t
    return None


# -- checkpoint ---------------------------------------------------------------

def _git_rc(cwd: str, *args: str) -> tuple[int, str]:
    """Read-only git: GIT_OPTIONAL_LOCKS=0 so `status` never rewrites the
    index, with the user's umask (checkpoint's own is 077). Its exit code
    (-1 when it can't run) and output."""
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5,
                           env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"), umask=state.user_umask())
    except (OSError, subprocess.SubprocessError):
        return -1, ""
    return r.returncode, r.stdout


def _git(cwd: str, *args: str) -> str:
    rc, out = _git_rc(cwd, *args)
    return out if rc == 0 else ""


def git_snapshot(cwd: str) -> dict[str, Any]:
    if not cwd or not _git(cwd, "rev-parse", "--is-inside-work-tree").strip():
        return {}
    status = _git(cwd, "status", "--porcelain").splitlines()
    return {"head": _git(cwd, "rev-parse", "HEAD").strip(),
            "status": status[:200],
            "diff_stat": _git(cwd, "diff", "--stat").strip().splitlines()[-1:] or []}


def checkpoint_path(sid: str) -> Path:
    """Where the session's pause-time checkpoint goes, always in the state
    folder (never the user's project): next to its task's journal
    (journal/<task>/checkpoint-<session>.json: retention and forget remove it
    with the journal), else, while the task has no journal yet,
    checkpoints/<session>.json."""
    task = state.load_session(sid).get("task_id") or ""
    if task and journal.jdir(task).is_dir():
        return journal.jdir(task) / f"checkpoint-{state.safe_id(sid)}.json"
    return state.p("checkpoints", state.safe_id(sid) + ".json")


def tracker_key(path: str) -> str:
    """A tracker's match key: the sha256 of its resolved path. A checkpoint
    keeps it unscrubbed beside its scrubbed `tracker` path, so another
    spelling of the path (a symlink) or a name the scrubber masks still
    finds the checkpoint."""
    try:
        real = str(Path(path).resolve())
    except (OSError, RuntimeError):
        real = path
    return hashlib.sha256(real.encode("utf8", "surrogateescape")).hexdigest()


def tracker_checkpoint(t: dict[str, Any]) -> dict[str, Any] | None:
    """The newest pause-time checkpoint written for this tracker (its
    `tracker_key`), whichever session wrote it, or None."""
    key = tracker_key(t["path"])
    found: list[tuple[float, dict[str, Any]]] = []
    for f in [*state.p("journal").glob("*/checkpoint-*.json"), *state.p("checkpoints").glob("*.json")]:
        cp = state.read_json(f, None)
        if isinstance(cp, dict) and cp.get("tracker_key") == key:
            with contextlib.suppress(OSError):
                found.append((f.stat().st_mtime, cp))
    return max(found, key=lambda x: x[0])[1] if found else None


def _cp_jobs(sid: str) -> list[dict[str, Any]]:
    """The session's jobs as a checkpoint keeps them: what a resume compares
    (id, state) and names, the command scrubbed; no log path or env file."""
    out = []
    for j in jobs.status(session_id=sid).get("jobs", []):
        rec = {k: j[k] for k in ("id", "name", "state", "exit_code", "supervisor", "task") if j.get(k) is not None}
        if j.get("command"):
            rec["command"] = sanitize.clip(sanitize.scrub(j["command"]), 200)
        out.append(rec)
    return out


def write_checkpoint(sid: str, reason: str = "", dirs: Iterable[str] | None = None) -> Path:
    """Plain facts at pause time, in the state folder (checkpoint_path).
    Written by the gate on its first deny and refreshed by Stop (which
    doesn't fire when continue:false ends a turn). A resume puts it into
    Claude's context, so everything in it is scrubbed here (the commands of
    the last calls, the jobs and the interrupted commands; then the whole of
    it once more). `tracker` names this session's own tracker, if any, and
    `tracker_key` (added after the scrub) finds it again; the session record
    keeps the tracker's real path (`tracker`) for forget and purge."""
    sess = state.load_session(sid)
    cwd = sess.get("cwd") or ""
    t = session_tracker(sid, list(dirs) if dirs is not None else [cwd])
    lines = transcript.tail_lines(sess["transcript_path"]) if sess.get("transcript_path") else []
    path = checkpoint_path(sid)
    data = {
        "session_id": sid,
        "cwd": cwd,
        "paused_at": sess.get("paused_at") or state.now_iso(),
        "paused_ts": sess.get("paused_ts") or time.time(),
        "written_at": state.now_iso(),
        "reason": sanitize.clip(sanitize.scrub(reason or sess.get("paused_by") or ""), 200),
        "tracker": t["path"] if t else None,
        "stages_in_progress": [f'{s["n"]}. {s["stage"]}' for s in (t["in_progress"] if t else [])],
        "last_tool_calls": transcript.recent_tool_calls(lines, 10),
        "git": git_snapshot(cwd),
        "jobs": _cp_jobs(sid),
        "interrupted": [dict({k: c[k] for k in ("tool_use_id", "at", "processes", "sigkill", "survivors") if k in c},
                             command=sanitize.clip(sanitize.scrub(c.get("command") or ""), 300))
                        for c in sess.get("interrupted") or [] if isinstance(c, dict)],
    }
    state.write_json(path, sanitize.scrub_value(data) | {"tracker_key": tracker_key(t["path"]) if t else None})
    early = state.p("checkpoints", state.safe_id(sid) + ".json")
    if path != early:
        state.remove(early)       # written before the task had a journal: this one replaces it
    state.update_session(sid, checkpoint=str(path), checkpoint_written=True,
                         task=t["slug"] if t else sess.get("task"),
                         tracker=t["path"] if t else sess.get("tracker"))
    pause_checkpoint(sid)
    return path


def pause_checkpoint(sid: str) -> str:
    """Each pause saves one journal checkpoint (the state a resume is
    reconciled against): once per pause, the first time a paused session's
    checkpoint is written. Returns '' or why it wasn't saved (never raises)."""
    try:
        sess = state.load_session(sid)
        if not state.is_paused(sid) or not sess.get("task_id") and not capture.spool_dir(sid).is_dir():
            return ""
        mark = str(sess.get("paused_ts") or state.flag_info(sid)[2] or "")
        if sess.get("pause_checkpoint_for") == mark:
            return ""
        ok, msg = save(sid, "pause")
        if ok:
            state.update_session(sid, pause_checkpoint_for=mark)
            return ""
        return msg
    except Exception as e:  # noqa: BLE001  the pause holds whatever the journal does
        return f"{type(e).__name__}: {e}"


# -- the journal ----------------------------------------------------------------

def journal_event(sid: str, kind: str, sync: bool = True, **data: Any) -> str:
    """Record a pause, resume or the like in the session's task journal (after
    what waits in its spool). Returns '' or what went wrong (never raises:
    pausing must work even when recording doesn't)."""
    if not sid:
        return ""
    try:
        r = capture.record(sid, [{"kind": kind, "session": sid,
                                  "data": {k: sanitize.clip(sanitize.scrub(v), 300) if isinstance(v, str) else v
                                           for k, v in data.items()}}], sync=sync)
        return r.error
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


def save(sid: str, reason: str = "") -> tuple[bool, str]:
    """Checkpoint now (/check:save): everything waiting in the spool goes to
    the journal, then a checkpoint record (id, the seq range it covers, the
    summary's hash, the hashes of the task's changed files, the git state),
    fsynced before this says it is saved. Never pauses or stops anything."""
    res = capture.ingest(sid)
    task = res.task or capture.task_of(sid)
    if res.error:
        return False, (f"Checkpoint NOT saved: recording failed ({res.error}). What happened since the last "
                       "good write waits in the spool; /check:doctor says more.")
    s = summary.build(task)
    sess = state.load_session(sid)
    cwd = sess.get("cwd") or ""
    files = {p: capture.file_hash(p) for p in capture.changed_files(task)}
    ver = capture.version(cwd) if cwd else {}
    cid = journal.new_id("c")
    first, last = (s.data.get("seq") or [0, 0])
    sha = sanitize.sha256_text(s.text)
    # The summary text beside the journal (the record is the authority; this
    # is what phase 3's list/diff shows).
    try:
        cdir = journal.jdir(task) / "checkpoints"
        cdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        state.write_json(cdir / f"{cid}.json", {"id": cid, "summary": s.text, "summary_sha256": sha})
    except OSError as e:
        return False, f"Checkpoint NOT saved: {e.strerror or e} (writing its summary failed)."
    try:
        line = recovery.line_for(task, last)
    except Exception:  # noqa: BLE001  the checkpoint matters more than its one-liner
        line = ""
    data = {"id": cid, "reason": sanitize.clip(sanitize.scrub(reason or "manual"), 200), "seq_from": first,
            "line": sanitize.clip(sanitize.scrub(line), 200),
            "seq_to": last, "summary_sha256": sha, "summary_chars": len(s.text),
            "files": {sanitize.clip(k, 500): v for k, v in list(files.items())[:300]},
            "git": {k: ver.get(k) for k in ("root", "head", "branch", "dirty_count") if ver.get(k) is not None}
            | {"dirty": {sanitize.clip(k, 500): v for k, v in list((ver.get("files") or {}).items())[:200]}}}
    r = capture.record(sid, [{"kind": "checkpoint", "op": cid, "session": sid, "data": data}], sync=True)
    if r.error:
        return False, f"Checkpoint NOT saved: writing the journal failed ({r.error}). /check:doctor says more."
    head = f", HEAD {str(ver.get('head') or '')[:10]}" if ver.get("head") else ""
    return True, (f"Checkpoint {cid} saved and synced to disk: task {r.task}, journal seq {first}-{last}, "
                  f"{len(files)} file hash(es){head}, summary {sha[:12]}. Work goes on (nothing was paused).")


def list_checkpoints(sid: str, ref: str = "") -> str:
    """The checkpoints of this session's task (or the task `ref` names),
    oldest first, with time, seq, a one-line summary and whether each is
    still current (/check:list, /check-list)."""
    if sid:
        capture.ingest(sid)
    task = find_task(ref, state.load_session(sid).get("cwd") or "" if sid else "", sid) if ref else (
        state.load_session(sid).get("task_id") if sid else "") or ""
    if not task:
        return (f"checkpoint: no task matches '{ref}' (`tasks` lists them)." if ref else
                "checkpoint: this session has no task journal yet.")
    return recovery.list_text(task)


def diff_text(sid: str, args: list[str]) -> str:
    """/check:diff [<checkpoint> [<checkpoint>|now]]: between two checkpoints of
    this session's task, or a checkpoint and now (default: the newest and now)."""
    if sid:
        capture.ingest(sid)
    a = args[0] if args else ""
    b = args[1] if len(args) > 1 else "now"
    task = state.load_session(sid).get("task_id") if sid else ""
    if a and a != "now":
        t2, rec = recovery.find_checkpoint(a, "")
        if rec is not None:
            task = t2
    if not task:
        return "checkpoint: this session has no task journal yet."
    if a == "now":
        a, b = b if b != "now" else "", "now"
    return recovery.diff(task, a, b)[0]


def task_summary(sid: str = "", task: str = "", limit: int = summary.LIMIT) -> str:
    """The recovery summary of a task (or this session's)."""
    if sid:
        capture.ingest(sid)
    task = task or (state.load_session(sid).get("task_id") if sid else "") or ""
    if not task:
        return "checkpoint: no task journal for this session yet."
    return summary.build(task, limit=limit, cwd=state.load_session(sid).get("cwd") or "" if sid else "").text


def find_task(ref: str, cwd: str = "", sid: str = "") -> str:
    """A task id from a reference: a task id or a unique prefix of one, a
    session id (prefix), or `last`: the newest task in this project; asked
    by a session (`sid`), never its own task (resuming that does nothing:
    after a crash the new session's first message starts a task of its
    own, and its first answer makes tool calls), and an interrupted one (no
    clean end, its Claude process gone) before a newer one that wasn't."""
    ref = (ref or "").strip()
    if not ref:
        return ""
    if ref == "last":
        own = (state.load_session(sid).get("task_id") or "") if sid else ""
        rows = [m for m in summary.task_list(cwd, limit=10) if not (sid and own and m["task"] == own)]
        if sid and rows:
            table = procs.table()
            for m in rows:
                if (not cwd or (m.get("cwd") or "") == cwd) and summary.interrupted(m, table):
                    return m["task"]
        return rows[0]["task"] if rows else ""
    ids = journal.tasks()
    if ref in ids:
        return ref
    hits = [t for t in ids if t.startswith(ref)]
    if len(hits) == 1:
        return hits[0]
    s = state.find_session(ref)
    if s and s.get("task_id"):
        return s["task_id"]
    return ""


# -- pause --------------------------------------------------------------------

# Why `pause now` couldn't stop a command: the `why` of an `unstopped` entry.
WHY_UNSTOPPED = {"ps": "the process table couldn't be read (ps failed)",
                 "sigkill": "their processes were still running after SIGKILL"}


def _by_why(calls: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for c in calls:
        out.setdefault(c.get("why") if c.get("why") in WHY_UNSTOPPED else "ps", []).append(c)
    return list(out.items())


def kill_foreground(sid: str, missed: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Kill this session's running foreground Bash commands (the ones its
    watchers guard). Records them in the session for the checkpoint
    (`interrupted`). The guarded commands it couldn't stop, because the
    process table couldn't be read or nothing of theirs died even of
    SIGKILL, are recorded in the session too (`unstopped`, with `why`,
    until it is resumed) and added to `missed`."""
    sess = state.load_session(sid)
    claude_pid = sess.get("claude_pid")
    lines = transcript.tail_lines(sess["transcript_path"]) if sess.get("transcript_path") else []
    finished = transcript.tool_results(lines)
    known = transcript.bash_commands(lines)  # incl. background calls, which must survive
    table = procs.table()
    killed, done, unstopped = [], set(), []
    for f in state.p("running").glob("*.json"):
        rec = state.read_json(f, {}) or {}
        if rec.get("session_id") != sid:
            continue
        if rec.get("tool_use_id") in finished:
            state.remove(f)  # it ended without PostToolUse (cancelled or denied)
            continue
        command = ((rec.get("tool_input") or {}).get("command")) or ""
        if not command or not claude_pid:
            continue
        call = {"command": command[:300], "tool_use_id": rec.get("tool_use_id")}
        if not table:
            # Unknown, not "nothing running": the command may well be running.
            unstopped.append(dict(call, at=state.now_iso(), why="ps"))
            continue
        try:
            since = f.stat().st_mtime  # written at the call's PreToolUse
        except OSError:
            continue
        for shell in procs.foreground_shells(int(claude_pid), command, table, since=since,
                                             others=known):
            if shell in done:
                continue
            done.add(shell)
            # The table just read: a second read could fail, and without it
            # only the shell's own group would be signalled, not the
            # command's processes that lead groups of their own.
            res = procs.kill_tree(shell, grace=state.KILL_GRACE_SECONDS,
                                  protect={int(claude_pid)}, procs=table)
            if res["killed"]:
                killed.append(dict(call, at=state.now_iso(), processes=len(res["killed"]),
                                   sigkill=bool(res.get("needed_sigkill")), survivors=len(res.get("survivors") or [])))
            else:  # all of it survived SIGKILL: it still runs
                unstopped.append(dict(call, at=state.now_iso(), why="sigkill"))
    stopped = {c["tool_use_id"] for c in killed}
    unstopped = [c for c in unstopped if c["tool_use_id"] not in stopped]
    state.add_interrupted(sid, killed)
    state.add_unstopped(sid, unstopped)
    if killed:
        # The journal: these calls were stopped (a resume reconciles them as
        # canceled, never as unknown or done).
        try:
            capture.record(sid, [{"kind": "op.killed", "op": str(c.get("tool_use_id") or ""), "session": sid,
                                  "data": {"command": sanitize.clip(sanitize.scrub(c.get("command") or ""), 300),
                                           "processes": c.get("processes"), "sigkill": c.get("sigkill"),
                                           "survivors": c.get("survivors")}} for c in killed], sync=True)
        except Exception:  # noqa: BLE001  the kill happened either way
            pass
    if missed is not None:
        missed.extend(unstopped)
    return killed


def kill_detail(c: dict[str, Any]) -> str:
    """Exactly what stopping one command did."""
    n = c.get("processes") or 0
    how = "SIGTERM, then SIGKILL for what was left" if c.get("sigkill") else "SIGTERM"
    surv = f"; {c['survivors']} survived SIGKILL" if c.get("survivors") else ""
    return f"`{(c.get('command') or '')[:80]}` ({n} process(es) ended: {how}{surv})"


def running_commands(sid: str) -> list[dict[str, Any]]:
    """This session's guarded foreground Bash calls (running/*.json) that have
    no result yet."""
    out = []
    for f in state.p("running").glob("*.json"):
        rec = state.read_json(f, {}) or {}
        if rec.get("session_id") == sid and not rec.get("agent_id"):
            c = ((rec.get("tool_input") or {}).get("command")) or ""
            if c:
                out.append({"command": c[:300], "tool_use_id": rec.get("tool_use_id")})
    return out


def _cmds(calls: list[dict[str, Any]]) -> str:
    return ", ".join(f"`{c.get('command', '')[:80]}`" for c in calls)


def _clock(iso: str) -> str:
    return (iso or "")[11:19]


def pause_targets_self(sid: str, args: list[str]) -> bool:
    """`pause` with these args pauses the session `sid` itself: no target,
    `all`, or a target that is `sid`."""
    rest = [a for a in args if a not in ("now", "all", "--stop-jobs")]
    if "all" in args or not rest:
        return True
    s = state.find_session(rest[0])
    return bool(s) and s["session_id"] == sid


def pause(sid: str, args: list[str], by: str, echo: dict[str, Any] | None = None,
          killed_out: list[dict[str, Any]] | None = None) -> str:
    """Handle `pause [now] [all] [--stop-jobs] [<session>]`. Returns a short
    result for the user. `echo` is set when this is the command the gate
    already handled while it waited in the queue (it runs again when the
    turn ends): `all` and `<session>` are then not applied a second time,
    so sessions resumed meanwhile stay resumed. `now` kills the running
    foreground commands of every target, and leaves each a now/<sid>
    request for its watchers; the killed commands are added to
    `killed_out`. Commands it couldn't stop (no process table) are said
    again by a later `pause` here while the session stays paused (the
    queued command's echo)."""
    args = ["now" if a == "interrupt" else a for a in args]   # `interrupt`: the same as `now`
    now = "now" in args
    stop_jobs = "--stop-jobs" in args
    rest = [a for a in args if a not in ("now", "all", "--stop-jobs")]
    if echo and ("all" in args or rest):
        return (f"Already done at {_clock(echo.get('at', ''))}: this command waited in the queue "
                "while the session was busy, and it took effect at the next tool call. "
                + (echo.get("result") or "")).strip()
    msgs = []
    unstopped_then: list[dict[str, Any]] = []  # this session's, from before this command
    then_at = 0
    if "all" in args:
        targets = [s["session_id"] for s in state.live_sessions()]
        if sid and sid not in targets:
            targets.append(sid)
        for t in targets:
            state.set_paused(t, by, now=now)
        msgs.append(f"Pausing every running session ({len(targets)}): each stops at its next "
                    "tool call. Idle ones stay paused until you send them a message; sessions "
                    "started later aren't paused.")
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
            msgs.append("Paused: this session stops at its next tool call. Type a message to resume it (any "
                        "non-/check command resumes, and /check:resume).")
            if not now:
                fg = running_commands(sid)
                if fg:
                    msgs.append(f"Left running: {_cmds(fg)} (it ends on its own; `pause now` or `pause interrupt` "
                                "stops it at once).")
        else:
            # Typically the queued command itself, arriving after the gate or
            # watcher already acted on it: say what actually happened.
            msg = "Already paused"
            if (before.get("paused_by") or "").startswith("queued command"):
                msg += (f" (since {_clock(before.get('paused_at') or '')}: it took effect "
                        "at the next tool call while the session was busy)")
            msgs.append(msg + ".")
            earlier = before.get("interrupted") or []
            if earlier:
                msgs.append("Stopped then: " + _cmds(earlier) + ".")
            # E.g. the queued `pause now` the watcher applied without a
            # process table: by the time its echo comes the command has
            # ended, but it was never stopped. (Said below, after the kill.)
            unstopped_then = before.get("unstopped") or []
            then_at = len(msgs)
        targets = [sid]
    for t in targets:
        journal_event(t, "pause", by=by, now=now, all="all" in args)
    missed: list[dict[str, Any]] = []
    gw_lines: list[str] = []
    if now:
        n = 0
        all_killed: list[dict[str, Any]] = []
        for t in targets:
            k = kill_foreground(t, missed=missed)
            n += len(k)
            all_killed.extend(k)
            if killed_out is not None:
                killed_out.extend(dict(c, session_id=t) for c in k)
        if n:
            msgs.append(f"Stopped {n} running command(s) now: " + "; ".join(kill_detail(c) for c in all_killed) + ".")
        for why, calls in _by_why(missed):
            msgs.append(f"Couldn't stop {len(calls)} running command(s) ({_cmds(calls)}): "
                        + WHY_UNSTOPPED[why]
                        + (", so they run until they end; Esc in their session stops them."
                           if why == "ps" else "."))
        if not n and not missed and not any(s.get("interrupted") or s.get("unstopped")
                                            for s in map(state.load_session, targets)):
            msgs.append("No command was running.")
    # One still running and still unstoppable is in the message above instead.
    again = {c.get("tool_use_id") for c in missed}
    then = [c for c in unstopped_then if c.get("tool_use_id") not in again]
    msgs[then_at:then_at] = [f"Couldn't stop then: {_cmds(calls)}: {WHY_UNSTOPPED[why]}."
                             for why, calls in _by_why(then)]
    # The gateway's half (CONTRACT-v1 /v1/pause): each target session's task
    # there, its jobs' on_pause policies, what actually happened.
    for t in targets:
        gp0 = state.load_session(t).get("gateway_paused") or {}
        line = gateway_pause(t, reason=f"checkpoint pause ({by})")
        if line and gp0.get("delivered") and (state.load_session(t).get("gateway_paused") or {}) == gp0:
            # the same pause again (already paused): its first answer, nothing sent now
            line += " (That was when this pause began; nothing was sent to the gateway again now.)"
        if line:
            gw_lines.append(line if len(targets) == 1 else f"Session {t[:8]}: {line}")
    msgs.extend(gw_lines)
    if stop_jobs:
        # The registry's records (jobs.is_live reads their owner: a gateway
        # job's live states and unknown count, not only "running").
        live = [j for j in sorted(jobs.refresh_all().values(), key=lambda j: j.get("started_ts", 0))
                if j.get("session_id") in targets and jobs.is_live(j)]
        results = stop_jobs_at_once(live, "pause --stop-jobs")
        stopped = [j["name"] for j in live if results[j["id"]].get("stopped")]
        failed = [f"{j['name']} ({stop_failure(j, results[j['id']])})" for j in live
                  if not results[j["id"]].get("stopped")]
        msgs.append(f"Stopped jobs: {', '.join(stopped)}." if stopped else
                    ("No job was stopped." if failed else "No running jobs to stop."))
        if failed:
            msgs.append(f"Could not stop: {'; '.join(failed)}.")
    elif now:
        msgs.append("Detached jobs keep running (add --stop-jobs to stop them).")
    return " ".join(msgs)


# -- resume -------------------------------------------------------------------

def _paused_since(cp: dict[str, Any]) -> float | None:
    """When the pause began, as epoch seconds (paused_ts: an epoch, so a
    laptop that changed time zone while paused still gets the right figure)."""
    try:
        return float(cp["paused_ts"]) if cp.get("paused_ts") else None
    except (TypeError, ValueError):
        return None


def _changes_since(cp: dict[str, Any], cwd: str) -> list[str]:
    """How long the pause lasted, and the session's jobs that changed state
    (the reconciliation covers files, git, validations and operations)."""
    out = []
    since = _paused_since(cp)
    if since is not None:
        mins = max(0, int((time.time() - since) / 60))
        out.append(f"Paused for {mins // 60} h {mins % 60} min." if mins >= 60 else f"Paused for {mins} min.")
    before = {j["id"]: j for j in cp.get("jobs") or []}
    now_jobs = {j["id"]: j for j in jobs.status(session_id=cp.get("session_id")).get("jobs", [])}
    for jid, j in now_jobs.items():
        b = before.get(jid, {})
        if b.get("state") != j.get("state") or j.get("state") != "running":
            line = f"Job {j['name']}: {j['state']}"
            if j.get("exit_code") is not None:
                line += f" (exit {j['exit_code']})"
            if j["state"] != "running":
                # Never its log here: this text goes into the model's context
                # as a hook's, where nothing masks it (job_tail is a tool call
                # the gateway's hook masks), and a log can hold what a job's
                # environment exported.
                line += f"; its output: job_tail {j['name']}"
            out.append(line)
    return out


RESUME_LIMIT = 8500   # the reconciliation and the recovery summary in a resume context


def resume_context(t: dict[str, Any] | None, cp: dict[str, Any] | None, cwd: str, sid: str = "",
                   task: str = "", checkpoint: str = "", prompt: str = "", selected: bool = False,
                   why: str = "resume") -> str:
    """The context a resumed task starts from: the reconciliation (what to
    check first, the interrupted operations in their classes, changed files,
    git, stale validations, instructions, lineage) and the recovery summary
    from the journal; then the tracker's notes and the pause-time facts.
    Never says to redo a stage from the start: an interrupted or unknown
    operation is checked before anything is done again."""
    parts = ["<checkpoint-resume>"]
    task = task or (state.load_session(sid).get("task_id") if sid else "") or ""
    if cp:
        ch = _changes_since(cp, cwd)
        if ch:
            parts.append("Since the pause:\n- " + "\n- ".join(ch))
    if task:
        try:
            with sanitize.remembered():     # the summary scrubs the commands reconcile scrubbed
                r = recovery.reconcile(task, sid, checkpoint=checkpoint, cwd=cwd, prompt=prompt, selected=selected)
                if sid:
                    recovery.record(sid, r, why)
                # The summary gets what the reconciliation leaves (it is cut first).
                parts.append(r.text(lambda n: summary.build(task, limit=n, cwd=cwd).text,
                                    limit=RESUME_LIMIT, summary_max=4000))
        except Exception as e:  # noqa: BLE001  the tracker and checkpoint still say something
            parts.append(f"(reconciling with the journal failed: {type(e).__name__}: {e}; check the files and "
                         "anything that was running before going on)")
    if t:
        parts.append(f"Tracker: {t['path']}")
        for sec in ("Now", "Resume notes"):
            if t["sections"].get(sec):
                parts.append(f"## {sec}\n{t['sections'][sec]}")
        if t["open"]:
            parts.append("Open stages: " + "; ".join(f'{s["n"]}. {s["stage"]} ({s["status"]})'
                                                     for s in t["open"]))
    trackers = state.trackers_on()
    if cp and cp.get("interrupted"):
        parts.append(f"`pause now` stopped {_cmds(cp['interrupted'])} before it finished: its partial effects "
                     "are possible. Check what it had done (files, output, remote state) before running any of it "
                     "again" + (", and if a tracker stage covers it, mark that stage 'interrupted' with what you "
                                "found." if trackers else "."))
    if cp:
        parts.append("Checkpoint at pause:\n" + json.dumps(sanitize.scrub_value(
            {k: cp.get(k) for k in ("paused_at", "stages_in_progress", "last_tool_calls", "jobs")}), indent=1)[:3000])
    # CHECKPOINT_TRACKERS=off: nothing asks for a tracker (state.trackers_on).
    parts.append("Resume: do the 'Check first' items above, then continue from the plan" + (
        " (the tracker's 'Next action' if there is one). Keep the tracker current." if trackers else "."))
    parts.append("</checkpoint-resume>")
    return "\n\n".join(parts)


def resume_here(sid: str, dirs: Iterable[str] | None = None, prompt: str = "") -> str:
    """Resume this session (it was paused, or un-paused without a message):
    clear the flag, reconcile, build the context."""
    state.clear_paused(sid)
    journal_event(sid, "resume", by="user")
    gw = gateway_resume(sid)
    sess = state.load_session(sid)
    cwd = sess.get("cwd") or ""
    dirs = [d for d in (list(dirs) if dirs is not None else [cwd]) if d]
    t = None
    for d in dirs:
        t = tracker.owned(d, sid)
        if t:
            break
    if not t and sess.get("task"):
        # The task this session paused on, even if another session has taken
        # its tracker over meanwhile (its id now leads the Session: line).
        for d in dirs:
            t = tracker.by_slug(d, sess["task"])
            if t:
                break
    if not t:
        t = session_tracker(sid, dirs)
    cp = state.read_json(Path(sess["checkpoint"]), None) if sess.get("checkpoint") else None
    if cp and cp.get("session_id") != sid:
        cp = None  # another session's checkpoint is never this session's
    chosen = sess.get("resume_from") or ""
    ctx = resume_context(t, cp, cwd, sid=sid, checkpoint=chosen, prompt=prompt, selected=bool(chosen))
    if gw:
        ctx = ctx.replace("<checkpoint-resume>", "<checkpoint-resume>\n\n" + gw, 1)
    state.update_session(sid, interrupted=[], resume_from="")
    return ctx


def resolve_ref(cwd: str, ref: str, sid: str = "") -> tuple[dict[str, Any] | None, str, str]:
    """What /check:resume <ref> names in session `sid`: (its tracker or
    None, a journal task id or '', a checkpoint id or ''). Reads only."""
    t = tracker.by_slug(cwd, ref)
    task = find_task(ref, cwd, sid)
    chosen = ""
    if not task:
        ctask, rec = recovery.find_checkpoint(ref)
        if rec is not None:
            task, chosen = ctask, rec["data"]["id"]
    return t, task, chosen


def no_match(cwd: str, ref: str) -> str:
    """What /check:resume <ref> says when nothing matches."""
    names = ", ".join(x.parent.name for x in tracker.trackers(cwd)) or "none"
    recent = ", ".join(f"{m['task']} ({sanitize.clip(m.get('goal'), 40)})" for m in summary.task_list(cwd, 5))
    return (f"No tracker, task or checkpoint matches '{ref}' in {tracker.work_dir(cwd)} (trackers: {names}; "
            f"recent tasks: {recent or 'none'}; /check:list shows a task's checkpoints).")


def resume_task(cwd: str, slug: str, sid: str = "", prompt: str = "",
                resolved: tuple[dict[str, Any] | None, str, str] | None = None) -> str:
    """/check:resume <task|checkpoint>: a tracker by its slug, a journal task
    (its id or a prefix, a session id, or `last`), or a checkpoint id (select:
    reconcile against it), which this session then adopts: its records go to
    that task's journal from here on (a finished task is reopened). Nothing
    changes when nothing matches."""
    t, task, chosen = resolved or resolve_ref(cwd, slug, sid)
    if not t and not task:
        return no_match(cwd, slug)
    reopened: dict[str, Any] = {}
    if task and sid:
        reopened = adopt(sid, task)
        if chosen:
            journal_event(sid, "checkpoint.selected", id=chosen)
    cp = tracker_checkpoint(t) if t else None
    ctx = resume_context(t, cp, cwd, sid=sid, task=task, checkpoint=chosen, prompt=prompt, selected=bool(chosen),
                         why="select" if chosen else "resume task")
    if reopened:
        ctx = ctx.replace("<checkpoint-resume>", "<checkpoint-resume>\n\n" + reopen_note(task, reopened), 1)
    return ctx


def reopen_note(task: str, reopened: dict[str, Any]) -> str:
    """The resume context's word on a finished task picked up again."""
    how = "marked complete" if reopened.get("was") == "completed" else "canceled"
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(reopened.get("ts") or 0)))
    return (f"Task {task} was {how} at {when} (journal seq {reopened.get('seq')}); picking it up reopens it (recorded: "
            "task.reopen). Check with the user what is still wanted before going on with its old plan.")


def adopt(sid: str, task: str) -> dict[str, Any]:
    """This session continues `task`: what it recorded so far stays in its
    old task; from now on its records go to `task`, starting with a record
    that names where it came from (an explicit continuation: its parent, the
    task's last session, and whether that one is still alive elsewhere). A
    task marked complete or canceled is reopened (task.reopen: the summary,
    the task list and retention treat it as open again). Returns what was
    reopened ({} when nothing was)."""
    capture.ingest(sid)
    sess = state.load_session(sid)
    old = sess.get("task_id") or ""
    if old != task:
        meta = state.read_json(journal.jdir(task) / "meta.json", {}) or {}
        parent = meta.get("last_session") or ""
        alive = None
        if parent and parent != sid:
            pid = state.load_session(parent).get("claude_pid")
            table = procs.table()
            alive = procs.is_claude(pid, table) if isinstance(pid, int) and table else None
        state.update_session(sid, task_id=task)
        capture.record(sid, [{"kind": "session.adopt", "session": sid,
                              "data": {"from_task": old, "parent": parent, "continuation": True,
                                       "parent_alive": alive,
                                       "cwd": sanitize.clip(sess.get("cwd") or "", 500)}}], sync=True)
    fin = summary.collect(task).finished
    if fin is None:
        return {}
    was = "completed" if fin["kind"] == "task.complete" else "canceled"
    capture.record(sid, [{"kind": "task.reopen", "session": sid,
                          "data": {"was": was, "finished_seq": fin.get("seq"), "by": "resume"}}], sync=True)
    return {"was": was, "ts": fin.get("ts"), "seq": fin.get("seq")}


def select(sid: str, ref: str) -> tuple[bool, str]:
    """Choose a checkpoint to resume from: its task becomes this session's,
    and the next resume reconciles against it. Nothing is restored."""
    task, rec = recovery.find_checkpoint(ref, "")
    if rec is None:
        return False, f"No single checkpoint matches '{ref}' (/check:list shows them)."
    adopt(sid, task)
    cid = rec["data"]["id"]
    state.update_session(sid, resume_from=cid)
    journal_event(sid, "checkpoint.selected", id=cid)
    return True, cid


def resume_now(sid: str, ref: str = "") -> tuple[bool, str]:
    """The immediate /check-resume: lift the pause now (pending: the next
    prompt, which the mod submits, gets the resume context), after
    reconciling. With `ref`, a checkpoint (or task) to resume from. Returns
    (ok, the short report for the person)."""
    capture.ingest(sid)
    if ref:
        ok, cid = select(sid, ref)
        if not ok:
            t2 = find_task(ref, state.load_session(sid).get("cwd") or "", sid)
            if not t2:
                return False, cid
            adopt(sid, t2)
    sess = state.load_session(sid)
    paused = state.is_paused(sid) or state.resume_pending(sid)
    task = sess.get("task_id") or ""
    report = ""
    if task:
        try:
            r = recovery.reconcile(task, sid, checkpoint=sess.get("resume_from") or "", cwd=sess.get("cwd") or "",
                                   selected=bool(sess.get("resume_from")))
            report = r.short()
        except Exception as e:  # noqa: BLE001
            report = f"(reconciling failed: {type(e).__name__}: {e})"
    if not paused and not ref:
        return True, "This session wasn't paused. " + report
    # Lift the flag now; the context goes with the next prompt (pending).
    was = state.is_paused(sid)
    state.clear_paused(sid, pending=True)
    if was:
        journal_event(sid, "resume", by="/check-resume")
    if not was:
        state.touch(state.p("pending", state.safe_id(sid)), state.now_iso())
    gw = gateway_resume(sid)
    return True, ("Resumed: " if was else "Resuming: ") + report + (f" {gw}" if gw else "")


# -- cancel, complete, stop ------------------------------------------------------------

CANCEL_POLICIES = ("interrupt", "detach")


def _new_task_after(sid: str, old: str, why: str) -> str:
    """The session goes on in a fresh task (the old one is finished)."""
    new = journal.new_id("t")
    state.update_session(sid, task_id=new)
    capture.record(sid, [{"kind": "session.adopt", "session": sid,
                          "data": {"from_task": old, "parent": sid, "why": why}}], sync=True)
    return new


def stop_jobs_at_once(js: list[dict[str, Any]], reason: str) -> dict[str, dict[str, Any]]:
    """jobs.stop for each, all at once (one after another, jobs that ignore
    SIGTERM would add a grace period each). {job id: its result}."""
    out: dict[str, dict[str, Any]] = {}
    if not js:
        return out
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, len(js))) as pool:
        for j, res in zip(js, pool.map(lambda j: jobs.stop(j["id"], reason=reason), js)):
            out[j["id"]] = res
    return out


def stop_outcome(r: dict[str, Any]) -> str:
    """stopped, stop_failed or unknown (jobs.stop's `outcome`)."""
    got = r.get("outcome")
    if got in ("stopped", "stop_failed", "unknown"):
        return got
    return "stopped" if r.get("stopped") else "stop_failed"


def stop_failure(j: dict[str, Any], r: dict[str, Any]) -> str:
    """Why a stop didn't stop it, in a few words: the gateway's reason (an
    unknown outcome says it may still run), or the own runner's group."""
    if jobs.owner(j) == "gateway" or r.get("supervisor") == "gateway":
        return sanitize.clip(r.get("reason") or "the gateway didn't confirm it ended", 240)
    return "processes of it are still running after SIGKILL"


def task_jobs(sid: str, task: str) -> list[dict[str, Any]]:
    """Jobs that belong to the task (its id, or one of its sessions for a job
    started while its session had no task yet), not detached, and running or
    possibly running:
    the own runner's, and the gateway's in a live state or unknown (the
    gateway lost it or can't be asked: it may still run, so cancel tries to
    stop it and says what came of that)."""
    meta = (state.read_json(journal.jdir(task) / "meta.json", {}) or {}) if task else {}
    sessions = set(meta.get("sessions") or []) | {sid}
    out = []
    for j in jobs.refresh_all().values():
        if j.get("detached") or not jobs.is_live(j):
            continue
        if (task and j.get("task_id") == task) or (not j.get("task_id") and j.get("session_id") in sessions):
            out.append(j)
    return sorted(out, key=lambda j: j.get("started_ts", 0))


def cancel(sid: str, args: list[str], by: str = "user") -> str:
    """/check:cancel [interrupt|detach]: end this session's task. Its running
    work follows the child-work policy: each job's own `on_cancel` (job_start;
    default interrupt), or the one given here for all: interrupt stops it
    (its process group, SIGTERM then SIGKILL), detach leaves it running and
    releases it from the task. A running foreground command is stopped with
    interrupt, left running with detach. History is kept; the session goes
    on in a new task. Returns exactly what happened."""
    policy = next((a for a in args if a in CANCEL_POLICIES), "")
    note = " ".join(a for a in args if a not in CANCEL_POLICIES)
    capture.ingest(sid)
    sess = state.load_session(sid)
    task = sess.get("task_id") or ""
    if not task:
        return "checkpoint: this session has no task to cancel yet."
    msgs = [f"Task {task} canceled; its history stays in the journal ({journal.jdir(task)})."]
    # The running command first (the turn waits on it; the mod ends the turn
    # once this returns, holding the session's tool calls meanwhile), then
    # the jobs, all at once: one after another, jobs that ignore SIGTERM
    # would add a grace period each.
    fg = running_commands(sid)
    killed: list[dict[str, Any]] = []
    if fg and policy != "detach":
        missed: list[dict[str, Any]] = []
        killed = kill_foreground(sid, missed=missed)
        if killed:
            msgs.append("Stopped the running command: " + "; ".join(kill_detail(c) for c in killed) + ".")
        for why_, calls in _by_why(missed):
            msgs.append(f"Couldn't stop {_cmds(calls)}: {WHY_UNSTOPPED[why_]}.")
    elif fg:
        msgs.append(f"Left running (detach): {_cmds(fg)}.")
    plan = [(j, policy or (j.get("on_cancel") if j.get("on_cancel") in CANCEL_POLICIES else "interrupt"))
            for j in task_jobs(sid, task)]
    stops = [j for j, pol in plan if pol != "detach"]
    stopped = stop_jobs_at_once(stops, f"task {task} canceled")
    outcomes = []
    for j, pol in plan:
        gw = jobs.owner(j) == "gateway"
        where = f"gateway job {j.get('gateway_job')}" if gw else f"pid {j.get('pid')}"
        if pol == "detach":
            r = jobs.detach(j["id"], reason=f"task {task} canceled")
            outcomes.append({"job": j["id"], "policy": "detach", "supervisor": jobs.owner(j),
                             "outcome": "detached" if r.get("ok") else "failed"})
            msgs.append(f"Job {j['name']} ({j['id']}): detached, still running ({where}), no longer part of the task"
                        + (" (checkpoint's records; the gateway keeps running it)." if gw else "."))
        else:
            r = stopped.get(j["id"]) or {}
            got = stop_outcome(r)
            outcomes.append({"job": j["id"], "policy": "interrupt", "supervisor": jobs.owner(j), "outcome": got})
            if gw:
                if got == "stopped":
                    said = ("had already ended (the gateway reports it " + str(r.get("job", {}).get("state")) + ")."
                            if r.get("already_ended") else
                            f"stopped through the gateway ({r.get('how') or 'SIGTERM'}).")
                elif got == "unknown":
                    said = f"NOT confirmed stopped: {stop_failure(j, r)}."
                else:
                    said = f"could NOT be stopped: {stop_failure(j, r)}."
                msgs.append(f"Job {j['name']} ({j['id']}, {where}): {said}")
            else:
                msgs.append(f"Job {j['name']} ({j['id']}): " + ("stopped (SIGTERM; SIGKILL after the grace period)."
                                                                if r.get("stopped") else
                                                                "could NOT be stopped (still running after SIGKILL)."))
    if not outcomes and not fg:
        msgs.append("Nothing of it was running (no job of checkpoint's runner or the gateway, no command).")
    r = capture.record(sid, [{"kind": "task.cancel", "session": sid,
                              "data": {"by": by, "policy": policy or "per job", "jobs": outcomes,
                                       "commands": [{"op": c.get("tool_use_id"), "outcome": "stopped"} for c in killed],
                                       "note": sanitize.clip(sanitize.scrub(note), 300)}}], sync=True)
    if r.error:
        msgs.append(f"(recording the cancel failed: {r.error}; /check:doctor)")
    if state.is_paused(sid):
        state.clear_paused(sid)
        journal_event(sid, "resume", by="cancel")
    gw = gateway_resume(sid)
    if gw:
        msgs.append(gw)
    _new_task_after(sid, task, "canceled")
    msgs.append("The session is not paused; your next message starts a new task.")
    return " ".join(msgs)


def complete(sid: str, args: list[str], by: str = "user") -> str:
    """/check:complete [note]: mark this session's task done (a record, synced);
    the session goes on in a new task. Nothing is stopped."""
    capture.ingest(sid)
    task = state.load_session(sid).get("task_id") or ""
    if not task:
        return "checkpoint: this session has no task to mark complete yet."
    note = " ".join(args)
    r = capture.record(sid, [{"kind": "task.complete", "session": sid,
                              "data": {"by": by, "note": sanitize.clip(sanitize.scrub(note), 300)}}], sync=True)
    if r.error:
        return f"Task {task} NOT marked complete: writing the journal failed ({r.error}). /check:doctor says more."
    still = task_jobs(sid, task)
    _new_task_after(sid, task, "completed")
    return (f"Task {task} marked complete (journal seq {r.last_seq}); its history stays. Your next message starts "
            "a new task." + (f" Still running from it: {', '.join(j['name'] + ' (' + j['id'] + ')' for j in still)} "
                             "(/check:stop <job> stops one)." if still else ""))


def stop_job(sid: str, args: list[str]) -> str:
    """/check:stop <job>: terminate one job: checkpoint's own runner's (its
    process group: SIGTERM, SIGKILL after the grace period), or a gateway
    job through the gateway (its stop, never a signal of checkpoint's own).
    Without a name, lists the running ones."""
    if not args:
        running = [j for j in jobs.refresh_all().values() if jobs.is_live(j)]
        if not running:
            return ("No job checkpoint started is running (its own runner or the gateway). (Claude Code's own "
                    "background shells: its shell list.)")
        return "Running jobs (/check:stop <name or id>): " + "; ".join(
            f"{j['name']} ({j['id']}, session {str(j.get('session_id') or '?')[:8]}"
            + (", outcome unknown: it may still run" if jobs.is_unknown(j) else "") + ")" for j in running)
    ref = args[0]
    j = jobs.find(ref)
    if not j:
        return f"No job matches '{ref}' (/check:stop alone lists the running ones)."
    if j.get("state") == "released":
        return (f"Job {j['name']} ({j['id']}, gateway job {j.get('gateway_job')}) was released (forget-job) after it "
                "was unknown for days with no word from the gateway: checkpoint no longer follows it, and its outcome "
                "is unknown. If it still runs, stop it with the gateway's own tools.")
    if not jobs.is_live(j):
        return f"Job {j['name']} ({j['id']}) isn't running: {j.get('state')}" + (
            f" (exit {j.get('exit_code')})" if j.get("exit_code") is not None else "") + "."
    r = jobs.stop(j["id"], reason="/check:stop by the user")
    got = stop_outcome(r)
    if sid:
        journal_event(sid, "job.stop", job=j["id"], name=j["name"], outcome=got, supervisor=jobs.owner(j),
                      **({"gateway_job": j.get("gateway_job")} if jobs.owner(j) == "gateway" else {}))
    if jobs.owner(j) == "gateway":
        who = f"Job {j['name']} ({j['id']}, gateway job {j.get('gateway_job')})"
        if got == "stopped":
            return (f"{who} stopped through the gateway ({r.get('how') or 'SIGTERM'}); the gateway reports it "
                    f"{r.get('job', {}).get('state')}. Its output: job_tail {j['name']}.")
        if got == "unknown":
            return f"{who} was NOT confirmed stopped: {stop_failure(j, r)}."
        return f"{who} could NOT be stopped: {stop_failure(j, r)}."
    # The outcome only, never the log's last lines: /check:stop answers in a
    # hook's context and /check-stop in the mod's output, both straight into
    # the model's context where nothing masks them, and a job's log can hold
    # what its environment exported (the gateway's env file). The job_tail
    # tool shows the log; the gateway's hook masks that result.
    see = f" Its output: job_tail {j['name']} (the log: {r.get('job', {}).get('log') or j.get('log') or '?'})."
    if r.get("stopped"):
        return (f"Job {j['name']} ({j['id']}) stopped: its process group got SIGTERM (SIGKILL after "
                f"{state.JOB_STOP_GRACE_SECONDS:g} s if needed) and is gone." + see)
    return (f"Job {j['name']} ({j['id']}) could NOT be stopped: processes of it are still running after SIGKILL."
            + see)


def resume_all(except_sid: str = "") -> int:
    """Un-pause every paused session. Each continues when it gets a message,
    and that message still brings the resume context. Returns how many."""
    n = 0
    for f in sorted(state.p("flags").glob("*")):
        sid = state.load_session(f.name).get("session_id") or f.name
        if sid == except_sid:
            continue
        state.clear_paused(sid, pending=True)
        gateway_resume(sid)
        n += 1
    return n


# -- doctor -------------------------------------------------------------------

def doctor_text(sid: str = "") -> str:
    """The runtime half of /check:doctor (hooks/env.sh doctor, which needs no
    Python, so the mod and the shell fallback show the same), plus what only
    Python knows."""
    import platform
    import sys
    from . import __version__
    try:
        r = subprocess.run(["sh", str(state.PLUGIN_ROOT / "hooks" / "env.sh"), "doctor", sid],
                           capture_output=True, text=True, timeout=20, umask=state.user_umask())
        body = r.stdout.strip() or f"(env.sh doctor printed nothing: {r.stderr.strip()[:200]})"
    except (OSError, subprocess.SubprocessError) as e:
        body = f"(env.sh doctor failed: {e})"
    head = (f"checkpoint {__version__} doctor (Python {platform.python_version()} at {sys.executable};"
            " Claude Code's version, surfaces and mods: /check-doctor, which the mod answers)")
    try:
        jl = journal_doctor(sid)
    except Exception as e:  # noqa: BLE001
        jl = [f"journal: the check failed: {type(e).__name__}: {e}"]
    return head + "\n" + body + "\n" + "\n".join(jl + gateway_doctor(sid))


def journal_doctor(sid: str = "") -> list[str]:
    """The journal's half of /check:doctor: this session's task, integrity of
    the recent journals, past failures (current ones: env.sh doctor)."""
    out = []
    tasks = journal.tasks()
    out.append(f"journal: {len(tasks)} task(s) in {state.p('journal')} (schema v{journal.SCHEMA})")
    mine = ""
    if sid:
        r = capture.ingest(sid)
        mine = r.task or state.load_session(sid).get("task_id") or ""
        if mine:
            v = journal.read(mine)
            out.append(f"journal: this session's task {mine}: {len(v.records)} record(s), next seq {v.next_seq}"
                       + ("; problems: " + "; ".join(p.text() for p in v.problems[:3]) if v.problems else
                          "; every record whole (CRC and seq checked)"))
        last = journal.last_failure(sid)
        if last and not journal.failure(sid):
            out.append(f"journal: last recording failure at {last.get('at')} ({last.get('error')}); writes work again")
    recent = [m["task"] for m in summary.task_list(limit=10)]
    damaged = []
    for t in recent:
        if t == mine:
            continue
        v = journal.read(t)
        if v.problems:
            damaged.append(f"{t} ({v.problems[0].text()})")
    out.append("journal: recent journals " + ("with problems: " + "; ".join(damaged[:5]) if damaged else
                                              f"checked ({len(recent)}): whole"))
    return out


# -- status -------------------------------------------------------------------

def status_text(sid: str = "") -> str:
    """Sessions, pause state, jobs; with a session (the caller's), also this
    project's unfinished tasks and its own task's journal state and recovery
    summary (never another session's: without one, none)."""
    lines = []
    table = procs.table()
    rows = []
    live = {s["session_id"] for s in state.live_sessions(table)}
    for s in state.all_sessions():
        ssid = s["session_id"]     # not `sid`: that is the caller's, used below
        paused = state.is_paused(ssid)
        if ssid not in live and not paused:
            continue
        st = "paused" if paused else "working/idle"
        if ssid not in live:
            st += " (process gone)"
        task = s.get("task") or ""
        if not task and s.get("cwd"):
            t = session_tracker(ssid, [s["cwd"]])
            task = t["slug"] if t else ""
        rows.append(f"- {ssid[:8]}  {st:<22} {s.get('cwd', '?')}  {('task: ' + task) if task else ''}".rstrip())
    lines.append("Sessions:" if rows else "No live sessions recorded.")
    lines.extend(rows)
    degraded = [m for m in state.degraded_markers(time.time() - state.DEGRADED_SHOW_HOURS * 3600)
                if m[0] in {state.safe_id(x) for x in live} or state.is_paused(m[0])]
    if degraded:
        # History: this runs in Python, so the runtime answers now.
        lines.append(f"The gate ran degraded in the last {state.DEGRADED_SHOW_HOURS} h (a hook ran without "
                     "checkpoint's Python half; while paused it held every call, otherwise calls went "
                     "ahead). Last time per session; the gate has its runtime again now:")
        lines.extend(f"- {sid_[:8]}  {text}" for sid_, text, _ in degraded[:10])
    day_ago = time.time() - 86400
    g = gateway.discover()
    lines.append(gateway_status_line(g))
    js = [j for j in sorted(jobs.refresh_all().values(), key=lambda j: j.get("started_ts", 0))
          if jobs.is_live(j)
          or float(j.get("started_ts") or state.iso_ts(j.get("started") or "") or 0) > day_ago]
    if js:
        lines.append("Jobs (running or started in the last day):")
        for j in map(jobs.public, js):
            ex = f" exit {j['exit_code']}" if j.get("exit_code") is not None else ""
            if j.get("supervisor") == "gateway":
                why = f" ({j['unknown_why']})" if j.get("unknown_why") else ""
                if j.get("state") == "released":
                    why = " (by you, forget-job: its outcome was never known)"
                pol = f", on_pause {j['on_pause']}" if j.get("on_pause") else ""
                lines.append(f"- {j['name']} [{j['id']}]: {j['state']}{ex}{why}, {j.get('runtime_s', '?')} s; gateway "
                             f"job {j.get('gateway_job')}{pol}")
                continue
            gw = f", gateway env {j['gateway_env']}" if j.get("gateway_env") else ""
            lines.append(f"- {j['name']} [{j['id']}]: {j['state']}{ex}, {j.get('runtime_s', '?')} s; checkpoint's "
                         f"runner{gw}")
    if sid:
        lines.extend(project_tasks_status(sid))
        lines.extend(journal_status(sid))
    return "\n".join(lines)


def unfinished_tasks(cwd: str, own: str = "", limit: int = 5) -> list[dict[str, Any]]:
    """This project's tasks that were neither completed nor canceled, newest
    first, without `own` (the caller's): their listing records (goal,
    last_session, ...) with `interrupted` (no clean end, its Claude process
    gone) set."""
    out: list[dict[str, Any]] = []
    if not cwd:
        return out
    table = None
    for m in summary.task_list(cwd, limit=max(limit * 3, 10)):
        if m.get("task") == own or (m.get("cwd") or "") != cwd or (m.get("finished") or {}).get("how"):
            continue
        if table is None:
            table = procs.table()
        out.append(dict(m, interrupted=summary.interrupted(m, table)))
        if len(out) >= limit:
            break
    return out


def project_tasks_status(sid: str) -> list[str]:
    """/check:status: this project's unfinished tasks (other than this
    session's own), with how to continue each."""
    sess = state.load_session(sid)
    rows = unfinished_tasks(sess.get("cwd") or "", own=sess.get("task_id") or "")
    if not rows:
        return []
    out = ["Unfinished tasks in this project (journal; /check:resume <task> continues one here):"]
    for m in rows:
        how = "interrupted (no clean end)" if m["interrupted"] else "ended without /check:complete"
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(m.get("updated") or 0)))
        resume = f"; `claude --resume {m['last_session']}`" if m.get("last_session") else ""
        out.append(f"- {m['task']}  {when}  {how}: {sanitize.clip(m.get('goal'), 80)}{resume}")
    return out


def journal_status(sid: str, limit: int = 3500) -> list[str]:
    """This session's journal: failures first, then its task's summary."""
    out = []
    res = capture.ingest(sid)
    fail = journal.failure(sid)
    if fail:
        out.append(f"RECORDING FAILED at {fail.get('at')}: {fail.get('error')} (what happened since waits in the "
                   "spool; /check:doctor)")
    sp = state.p("spool-errors", state.safe_id(sid))
    if sp.exists():
        out.append("RECORDING FAILED in the hooks (spool) since " + sp.read_text(errors="replace").strip()[:200])
    elif sp.with_name(sp.name + ".last").exists():
        out.append("Recording failed earlier in the hooks (spool), now recovered: "
                   + sp.with_name(sp.name + ".last").read_text(errors="replace").strip()[:300]
                   + "; that window's events are lost (the summary's recording gap)")
    task = res.task or state.load_session(sid).get("task_id") or ""
    if task:
        out.append("")
        try:
            out.append(summary.build(task, limit=limit, cwd=state.load_session(sid).get("cwd") or "").text)
        except Exception as e:  # noqa: BLE001  status still answers
            out.append(f"(the recovery summary of {task} failed: {type(e).__name__}: {e}; `{state.cli('summary', task)}`)")
    return out


# -- the gateway (CONTRACT-v1): pause, resume, status ----------------------------------

OWN_RUNNER_LIMITS = ("checkpoint's own detached runner: jobs survive pauses, a closed lid and the session's end, but "
                     "it has no per-job pause policy (a pause leaves them running; /check:pause --stop-jobs stops "
                     "them), no terminal (no input, no password prompts), no remote hosts, and nothing survives a "
                     "reboot")


def gateway_status_line(g: gateway.Gateway | None = None) -> str:
    """/check:status's word on the gateway, and where jobs run."""
    g = g or gateway.discover()
    if g.has("jobs"):
        # Whether the daemon serves this caller (this session) is its own
        # question: a daemon left running from another session while the
        # gateway plugin is off here refuses this session's calls.
        served, why = gateway.serves(g)
        if served is False:
            return (f"Gateway: its daemon runs ({g.version}, contract v{gateway.NEEDS}, epoch {g.epoch}) but doesn't "
                    f"serve this session ({why}): it serves only Claude Code sessions with the gateway plugin on (it "
                    "may be off or not loaded here, or this runs outside a session). New jobs from here run in "
                    f"{OWN_RUNNER_LIMITS}.")
        if served is None:
            return (f"Gateway: found ({g.version}, contract v{gateway.NEEDS}, epoch {g.epoch}) but its daemon didn't "
                    f"answer usably ({why}): a new job is tried there first and runs in checkpoint's own runner only "
                    "when the gateway certainly didn't take it and isn't serving this session (a job the gateway "
                    "refuses runs nowhere).")
        return (f"Gateway: available ({g.version}, contract v{gateway.NEEDS}, epoch {g.epoch}): new jobs run there "
                "(per-job pause policies, its terminals); jobs already in checkpoint's own runner stay there.")
    if g.ok:
        return f"Gateway: available ({g.version}) without jobs on its contract. Jobs run in {OWN_RUNNER_LIMITS}."
    return f"Gateway: not available ({g.reason}). Jobs run in {OWN_RUNNER_LIMITS}."


def forget_job(name: str) -> tuple[bool, str]:
    """`checkpoint forget-job <id>` (the person, in a terminal; never a tool
    call): release a gateway job stuck unknown with no word from the gateway
    for over jobs.RELEASE_AFTER_DAYS (jobs.release), and record it in its
    task's journal (`job.released`). Its outcome stays unknown."""
    r = jobs.release(name)
    if not r.get("ok"):
        return False, f"Not released: {r.get('reason')}. Nothing changed."
    j = r["job"]
    task = r.get("task_id") or ""
    note = ""
    if task and journal.jdir(task).is_dir():
        last = j.get("last_seen") or {}
        try:
            journal.append(task, [{"kind": "job.released", "op": f"release:{j['id']}", "session": "",
                                   "data": {"job": j["id"], "name": sanitize.clip(sanitize.scrub(j.get("name")), 80),
                                            "gateway_job": sanitize.clip(str(j.get("gateway_job") or ""), 80),
                                            "by": "user", "unknown_days": r.get("unknown_days"),
                                            "last_state": sanitize.clip(str(last.get("state") or ""), 30),
                                            "last_seen_ts": last.get("at") if isinstance(last.get("at"), (int, float))
                                            else None}}], sync=True)
        except journal.JournalError as e:
            note = f" (its task's journal couldn't record it: {sanitize.clip(str(e), 200)})"
    return True, (f"Released job {j.get('name')} ({j['id']}, gateway job {j.get('gateway_job')}): unknown for "
                  f"{r.get('unknown_days')} day(s) with no word from the gateway. Checkpoint no longer waits for it: it "
                  "no longer keeps its task's journal from retention, doesn't count as running, and the gateway isn't "
                  "asked about it any more. Its outcome stays unknown: if it ran, its effects are there; check them "
                  "before doing its work again."
                  + note)


def stuck_jobs_doctor() -> list[str]:
    """/check:doctor: gateway jobs unknown for over jobs.RELEASE_AFTER_DAYS
    with no word from the gateway, and how the person can release one."""
    try:
        stuck = [j for j in jobs.refresh_all().values() if jobs.releasable(j)]
    except Exception:  # noqa: BLE001  the doctor still answers
        return []
    out = []
    for j in sorted(stuck, key=jobs.unknown_since)[:5]:
        days = (time.time() - jobs.unknown_since(j)) / 86400
        out.append(f"gateway: job {sanitize.clip(j.get('name'), 60)} ({j['id']}) has been unknown for {days:.0f} day(s) "
                   f"with no word from the gateway ({sanitize.clip(j.get('unknown_why') or 'its state is not proven', 120)}):"
                   " it keeps its task's journal from retention and counts as possibly running. If you know it is "
                   "over, "
                   f"`{state.cli('forget-job', j['id'])}` releases it (nothing does that on its own; its outcome stays "
                   "unknown).")
    if len(stuck) > 5:
        out.append(f"gateway: and {len(stuck) - 5} more such job(s) (`{state.cli('status')}` lists them)")
    return out


def gateway_doctor(sid: str = "") -> list[str]:
    """/check:doctor's Python lines on the gateway: discovery, then one call;
    the jobs stuck unknown that the person can release."""
    g = gateway.discover()
    out = ["gateway: " + (g.line()[len("gateway: "):] if g.line().startswith("gateway: ") else g.line())]
    if g.ok:
        try:
            cap = gateway.capabilities(g)
            out.append(f"gateway: capabilities answered (contract_id {sanitize.clip(cap.get('contract_id'), 30)}, "
                       f"features {', '.join(str(f) for f in cap.get('features') or [])[:200]})")
        except gateway.CallError as e:
            out.append(f"gateway: its daemon didn't answer the capabilities call: {e.text()}")
    if sid:
        gp = state.load_session(sid).get("gateway_paused") or {}
        if gp and not state.is_paused(sid):
            out.append("gateway: this session's resume hasn't reached the gateway yet (retried at the next moment "
                       "checkpoint runs)")
        elif gp.get("uncertain") and not gp.get("delivered"):
            out.append("gateway: this session's pause may or may not have reached the gateway (its answer was lost); "
                       "sent again at the next moment checkpoint runs, with the same pause id (applied once)")
    return out + stuck_jobs_doctor()


def _gw_lock(sid: str):
    """One gateway pause/resume decision per session at a time (a flock of
    its own: the state lock isn't held across a network call)."""
    import fcntl

    @contextlib.contextmanager
    def held():
        path = state.p("gateway", state.safe_id(sid) + ".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    return held()


def _gw_names() -> dict[str, dict[str, Any]]:
    return {j["gateway_job"]: j for j in jobs._registry()["jobs"].values()
            if jobs.owner(j) == "gateway" and j.get("gateway_job")}


def _clean_outcomes(raw: Any) -> list[dict[str, Any]]:
    out = []
    for o in raw if isinstance(raw, list) else []:
        if not isinstance(o, dict) or not gateway.JOB_RE.fullmatch(str(o.get("job") or "")):
            continue
        pol = o.get("policy") if o.get("policy") in gateway.ON_PAUSE else "?"
        out.append({"job": o["job"], "policy": pol,
                    "outcome": sanitize.clip(sanitize.scrub(str(o.get("outcome") or "")), 200)})
    return out[:50]


def _outcome_text(outcomes: list[dict[str, Any]]) -> str:
    names = _gw_names()
    parts = []
    for o in outcomes:
        j = names.get(o["job"])
        who = f"{j['name']} ({j['id']}, {o['job']})" if j else o["job"]
        parts.append(f"{who}, on_pause {o['policy']}: {o['outcome']}")
    return "; ".join(parts)


def _pause_mark(sid: str) -> str:
    state.sync_flag(sid)
    sess = state.load_session(sid)
    return str(sess.get("paused_ts") or state.flag_info(sid)[2] or "")


def _record(sid: str, kind: str, data: dict[str, Any], sync: bool = True) -> None:
    with contextlib.suppress(Exception):
        capture.record(sid, [{"kind": kind, "session": sid, "data": data}], sync=sync)


def _task_jobs_elsewhere(sid: str, task: str) -> dict[str, list[dict[str, Any]]]:
    """Live gateway jobs of `task` that other sessions started (the
    registry), by session: the gateway pauses by (session, task_ref), so
    this session's pause alone doesn't reach them."""
    out: dict[str, list[dict[str, Any]]] = {}
    if not task:
        return out
    for j in sorted(jobs._registry()["jobs"].values(), key=lambda j: j.get("started_ts", 0)):
        osid = j.get("session_id") or ""
        # Live by the gateway's last word (an unknown one isn't the gateway's to pause: /check:status says it)
        if (jobs.owner(j) == "gateway" and j.get("gateway_job") and j.get("task_id") == task and osid
                and osid != sid and not j.get("detached") and j.get("state") in gateway.LIVE_STATES):
            out.setdefault(osid, []).append(j)
    return out


def _pause_others(g: gateway.Gateway, sid: str, task: str, reason: str
                  ) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """The task's gateway jobs started from other sessions, at this
    session's pause: (lines for the person, what gateway_resume must undo,
    records). A session whose Claude process has ended gets its own
    /v1/pause scoped to the task (its jobs follow their on_pause policies;
    nothing else of it runs any more). A session still open, or one whose
    liveness can't be read, is left alone (a gateway pause holds the whole
    session there: its other work too) and its jobs are listed as not
    paused; one paused itself is covered by its own pause."""
    lines: list[str] = []
    undo: list[dict[str, Any]] = []
    recs: list[dict[str, Any]] = []
    elsewhere = _task_jobs_elsewhere(sid, task)
    if not elsewhere:
        return lines, undo, recs
    table = procs.table()
    live = {s["session_id"] for s in state.live_sessions(table)} if table else None
    for osid, ojobs in list(elsewhere.items())[:10]:
        names = ", ".join(f"{j['name']} ({j['gateway_job']}, {j.get('state')})" for j in ojobs[:5])
        short = state.safe_id(osid)[:8]
        if state.is_paused(osid):
            lines.append(f"Started from session {short}, paused itself: {names} (its own pause applies to them).")
            recs.append({"session": osid, "outcome": "its own pause"})
            continue
        if live is None or osid in live:
            lines.append(f"NOT paused (started from session {short}, "
                         + ("still open" if live is not None else "maybe still open: no process table")
                         + f"): {names}. Pause that session, or /check:stop <job>.")
            recs.append({"session": osid, "outcome": "not paused (session open)" if live is not None
                         else "not paused (no process table)"})
            continue
        opid = pause_id(sid, _pause_mark(sid), osid)
        try:
            r2 = gateway.pause(g, osid, task, reason, opid)
        except gateway.CallError as e:
            unc = e.kind in ("ambiguous", "protocol")
            if unc:
                undo.append({"session": osid, "task": task, "uncertain": True, "pause_id": opid})
            lines.append(f"Started from session {short} (ended): the pause {'may not have' if unc else 'did not'} "
                         f"reach them ({e.text()}): {names}.")
            recs.append({"session": osid, "outcome": "uncertain" if unc else "not reached",
                         "error": sanitize.clip(e.text(), 200)})
            continue
        o2 = _clean_outcomes(r2.get("outcomes"))
        undo.append({"session": osid, "task": task, "delivered": True, "pause_id": opid})
        lines.append(f"Started from session {short} (ended), paused there for this task: "
                     + (_outcome_text(o2) if o2 else "none of them runs there any more") + ".")
        recs.append({"session": osid, "outcome": "paused", "outcomes": o2})
    return lines, undo, recs


RETRY_WHY = "sent again with the same pause id (the gateway applies a pause once)"


def pause_id(sid: str, mark: str, other: str = "") -> str:
    """The pause's id on the contract (`pause_id`): the same pause (this
    session and its mark; the other session it is sent for) always has the
    same id, so a retry names the pause it repeats and the gateway answers
    it without applying it again."""
    return "ckp-" + hashlib.sha256(f"{sid}\0{mark}\0{other}".encode("utf-8", "surrogatepass")).hexdigest()[:24]


def gateway_pause(sid: str, reason: str = "pause") -> str:
    """CONTRACT-v1 POST /v1/pause for this session and its task, once per
    pause: the gateway records the pause (its dialogs close, no new job or
    terminal input from this session) and applies each of the task's jobs'
    on_pause policy. Returns a line for the person ('' when there is no
    gateway); records gateway.pause. A pause that didn't reach it, or may
    have (the answer lost, or checkpoint gone during the call: the session
    record says "uncertain" before it is sent), is sent again at a later
    moment (gateway_settle), always with the same pause id: the gateway
    answers a pause id it has applied with that pause's first answer and
    signals nothing again (CONTRACT-v1 `pause_id`, which discovery requires)."""
    if not sid or not state.is_paused(sid):
        return ""
    g = gateway.discover()
    if not g.ok:
        return "" if g.status in ("absent", "off") else (
            f"Gateway: not reached ({g.reason}); jobs there follow their own course, unpaused.")
    with _gw_lock(sid):
        if not state.is_paused(sid):
            return ""
        mark = _pause_mark(sid)
        sess = state.load_session(sid)
        gp = sess.get("gateway_paused") or {}
        if gp.get("mark") == mark and gp.get("delivered"):
            return gp.get("text") or ""
        task = sess.get("task_id") or ""
        pid = pause_id(sid, mark)
        again = gp.get("mark") == mark and bool(gp.get("uncertain"))
        # Before it goes out: the session record says this pause may reach the
        # gateway (a checkpoint killed during the call leaves "uncertain",
        # never "not sent").
        tries = int(gp.get("tries") or 0) + 1 if gp.get("mark") == mark else 1
        pending = {"mark": mark, "pause_id": pid, "delivered": False, "uncertain": True, "task": task,
                   "ts": time.time(), "tries": tries}
        state.update_session(sid, gateway_paused=pending)
        try:
            r = gateway.pause(g, sid, task, reason, pid)
        except gateway.CallError as e:
            uncertain = e.kind in ("ambiguous", "protocol") or again
            state.update_session(sid, gateway_paused={**pending, "uncertain": uncertain, "ts": time.time()})
            _record(sid, "gateway.pause", {"ok": False, "error": sanitize.clip(e.text(), 200), "task": task,
                                           "epoch": g.epoch, "uncertain": uncertain, "pause_id": pid,
                                           **({"retry": RETRY_WHY} if again else {})})
            if uncertain:
                return (f"Gateway: the pause may not have reached it ({e.text()}); its jobs' pause policies weren't "
                        "confirmed. checkpoint sends it again at its next moment, with the same pause id: the "
                        "gateway applies a pause once, so no job is signalled twice.")
            if e.kind == "refused":
                # The gateway's decision: nothing of it is done here instead (its jobs are the gateway's).
                return (f"Gateway: it refused the pause ({e.text()}); its jobs' pause policies weren't applied and "
                        "checkpoint signals none of them itself. checkpoint tries again at its next moment.")
            return (f"Gateway: the pause did not reach it ({e.text()}); its jobs' pause policies weren't confirmed. "
                    "checkpoint tries again at its next moment.")
        outcomes = _clean_outcomes(r.get("outcomes"))
        closed = r.get("dialogs_closed") if isinstance(r.get("dialogs_closed"), int) else 0
        if outcomes:
            text = "Gateway jobs of this task: " + _outcome_text(outcomes) + "."
        else:
            text = "Gateway: this session is paused there too (no job of this task runs in it)."
        if r.get("replayed") is True:
            text += " (The gateway had applied this pause already: this is its first answer again, nothing re-sent.)"
        if closed:
            text += f" Its waiting dialog(s) for this session were closed ({closed})."
        # The task's jobs that other sessions started (a task picked up here
        # with /check:resume <task>, a fork): the gateway pauses by session.
        o_lines, o_state, o_rec = _pause_others(g, sid, task, reason)
        if o_lines:
            text += " " + " ".join(o_lines)
        others = [j for j in jobs._registry()["jobs"].values()
                  if jobs.owner(j) == "gateway" and j.get("session_id") == sid and j.get("task_id") != task
                  and jobs.is_live(j) and j.get("gateway_job") not in {o["job"] for o in outcomes}]
        if others:
            text += (" Not this task's, left as they are: "
                     + ", ".join(f"{j['name']} ({j['gateway_job']})" for j in others[:5]) + ".")
        state.update_session(sid, gateway_paused={"mark": mark, "pause_id": pid, "delivered": True, "task": task,
                                                  "epoch": g.epoch, "ts": time.time(), "text": text, "others": o_state})
        _record(sid, "gateway.pause", {"ok": True, "task": task, "epoch": g.epoch, "outcomes": outcomes,
                                       "dialogs_closed": closed, "pause_id": pid,
                                       **({"replayed": True} if r.get("replayed") is True else {}),
                                       **({"retry": RETRY_WHY} if again else {}),
                                       **({"others": o_rec} if o_rec else {})})
    # what the policies did shows in the jobs' states at once
    with contextlib.suppress(Exception):
        jobs.refresh_all()
    return text


def gateway_resume(sid: str) -> str:
    """CONTRACT-v1 POST /v1/resume when this session's pause reached the
    gateway (or may have): suspended jobs continue, its agent may use
    terminals and start jobs again; then the same for the task's jobs of
    ended sessions this pause paused there (_pause_others). Returns a line
    ('' when nothing was paused there); kept for a later moment when the
    gateway can't be reached. A pause that certainly never reached the
    gateway is dropped here and nothing is sent (never a late pause first)."""
    if not sid:
        return ""
    gp = state.load_session(sid).get("gateway_paused") or {}
    if not gp or state.is_paused(sid):
        return ""
    if not _gw_resume_needed(gp):
        # The pause never reached the gateway (it certainly didn't take it):
        # nothing to undo there, so nothing is sent; the late delivery is dropped.
        with _gw_lock(sid):
            gp = state.load_session(sid).get("gateway_paused") or {}
            if gp and not state.is_paused(sid) and not _gw_resume_needed(gp):
                state.update_session(sid, gateway_paused=None)
                _record(sid, "gateway.resume", {"ok": True, "sent": False,
                                                "note": "the pause never reached the gateway: nothing to resume"})
        return ""
    g = gateway.discover()
    if not g.ok:
        return f"Gateway: not reached to resume ({g.reason}); checkpoint tries again at its next moment."
    lines: list[str] = []
    with _gw_lock(sid):
        gp = state.load_session(sid).get("gateway_paused") or {}
        if not gp or state.is_paused(sid):
            return ""
        task = str(gp.get("task") or "")
        if (gp.get("delivered") or gp.get("uncertain")) and not gp.get("own_resumed"):
            try:
                r = gateway.resume(g, sid, task, str(gp.get("pause_id") or ""))
            except gateway.CallError as e:
                _record(sid, "gateway.resume", {"ok": False, "error": sanitize.clip(e.text(), 200), "epoch": g.epoch})
                return f"Gateway: the resume didn't reach it ({e.text()}); checkpoint tries again at its next moment."
            outcomes = _clean_outcomes(r.get("outcomes"))
            gp["own_resumed"] = True
            state.update_session(sid, gateway_paused=gp)
            _record(sid, "gateway.resume", {"ok": True, "epoch": g.epoch, "outcomes": outcomes,
                                            "was_paused": r.get("was_paused") is True})
            lines.append(("Gateway jobs on resume: " + _outcome_text(outcomes) + ".") if outcomes else
                         "Gateway: resumed this session there too.")
        left = []
        for o in gp.get("others") or []:
            if not isinstance(o, dict) or not (o.get("delivered") or o.get("uncertain")):
                continue
            osid = str(o.get("session") or "")
            short = state.safe_id(osid)[:8]
            if not osid or state.is_paused(osid):
                continue    # paused itself meanwhile: its own resume undoes its pause there
            try:
                r = gateway.resume(g, osid, str(o.get("task") or task), str(o.get("pause_id") or ""))
            except gateway.CallError as e:
                left.append(o)
                lines.append(f"Gateway: the resume of this task's jobs from session {short} didn't reach it "
                             f"({e.text()}); checkpoint tries again at its next moment.")
                continue
            o2 = _clean_outcomes(r.get("outcomes"))
            _record(sid, "gateway.resume", {"ok": True, "epoch": g.epoch, "outcomes": o2, "for_session": osid})
            if o2:
                lines.append(f"Gateway jobs of this task from session {short} on resume: {_outcome_text(o2)}.")
        if left:
            gp["others"] = left
            state.update_session(sid, gateway_paused=gp)
        else:
            state.update_session(sid, gateway_paused=None)
    with contextlib.suppress(Exception):
        jobs.refresh_all()
    return " ".join(lines)


def _gw_resume_needed(gp: dict[str, Any]) -> bool:
    """Did this session's pause (or may it have) reach the gateway, for it or
    for the task's jobs of other sessions, and not been resumed there yet?"""
    if (gp.get("delivered") or gp.get("uncertain")) and not gp.get("own_resumed"):
        return True
    return any(isinstance(o, dict) and (o.get("delivered") or o.get("uncertain")) for o in gp.get("others") or [])


def gateway_settle(sid: str) -> str:
    """Bring the gateway in step with this session's pause state when an
    earlier moment couldn't: deliver a pause it hasn't had for this pause,
    or a resume it missed. Cheap when there is nothing to do (the session
    record, then the discovery file only when needed). A prompt calls it
    only after it has decided whether it resumes (hooks.handle_prompt): a
    late pause delivered just before a resume would interrupt the jobs the
    person is resuming."""
    if not sid:
        return ""
    try:
        sess = state.load_session(sid)
        gp = sess.get("gateway_paused") or {}
        if state.is_paused(sid):
            if gp.get("delivered") and gp.get("mark") == str(sess.get("paused_ts") or ""):
                return ""
            if not gateway.discover().ok:
                return ""
            return gateway_pause(sid, reason="checkpoint pause (delivered late)")
        if gp:
            return gateway_resume(sid)
    except Exception:  # noqa: BLE001  a hook never fails over the gateway
        return ""
    return ""
