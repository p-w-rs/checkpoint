"""Deleting recorded history when the person asks: `forget <task>` (the
terminal helper, and /check:forget) removes one task, `purge [--dry-run]`
everything in the state folder (CHECKPOINT_HOME) but what a live or paused
session or a running job still needs.

Retention (state.prune, recovery.prune_tasks) removes old records on its own
while the plugin runs; this is the way to remove them now, and before an
uninstall. Both read the process table first and refuse without one
(invariant 11: without a table nothing is known to be dead). The projects'
own folders (`<project>/.work/`: trackers and checkpoint.json) belong to
the person: never touched here, only named, with the command that removes
them.
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import jobs, journal, procs, sanitize, state

# Per-session files (state.py's layout): <folder>/<session>.
SESSION_FILES = ("now", "pending", "seen", "queued", "trackers", "count", "turn", "turn-start", "rt",
                 "degraded", "noresume", "flags")
# Top-level files that are history or caches, never needed by a live session.
TOP_FILES = ("pruned", "degraded-seen", "session-start.err", "paused-all", "now-all")
# Kept: the terminal helper (rewritten at every session start), the state
# lock, the marker that the folder's modes were made private.
TOP_KEEP = ("checkpoint", ".lock", ".modes-v1")
# A spool with no session record may belong to a session that started
# before any runtime could record it: kept while this recent.
SPOOL_RECENT_SECONDS = 86400


@dataclass
class Live:
    running: set[str]                       # sessions whose Claude process runs
    paused: set[str]                        # sessions paused (flags/<session>)
    jobs: list[dict[str, Any]] = field(default_factory=list)   # jobs that run (or may: unknown)


def _live() -> tuple[Live | None, str]:
    """What still runs, or (None, why) when that can't be known."""
    table = procs.table()
    if not table:
        return None, "the process table couldn't be read, so it can't tell which sessions still run"
    running = {s["session_id"] for s in state.live_sessions(table)}
    paused = {s["session_id"] for s in state.all_sessions() if state.is_paused(s["session_id"])}
    try:
        paused |= {f.name for f in state.p("flags").iterdir()}
    except OSError:
        pass
    try:
        live_jobs = [j for j in jobs.refresh_all().values() if jobs.is_live(j)]
    except Exception as e:  # noqa: BLE001
        return None, f"the job registry couldn't be read ({type(e).__name__}), so it can't tell which jobs run"
    return Live(running, paused, live_jobs), ""


def _meta(task: str) -> dict[str, Any]:
    return state.read_json(journal.jdir(task) / "meta.json", {}) or {}


def _session_paths(name: str) -> list[Path]:
    """Every file and folder of one session (a safe id) in the state folder
    (none for a name that isn't one: never a folder itself)."""
    if not name or name.startswith(".") or "/" in name:
        return []
    out = [state.p("sessions", name + ".json"), state.p("checkpoints", name + ".json"),
           state.p("gateway", name + ".json"), state.p("gateway", name + ".lock"), state.p("spool", name)]
    out += [state.p(d, name) for d in SESSION_FILES]
    for d in ("journal-errors", "spool-errors"):
        out += [state.p(d, name), state.p(d, name + ".last"), state.reserve_path(d, name)]
    return out


def _running_files(names: set[str], keep: bool) -> list[Path]:
    """running/<tool_use_id>.json (a guarded Bash call's payload) of these
    sessions (keep=False), or of every session but these (keep=True)."""
    out = []
    for f in state.p("running").glob("*.json"):
        rec = state.read_json(f, {}) or {}
        name = state.safe_id(str(rec.get("session_id") or ""))
        if (name in names) != keep:
            out.append(f)
    return out


def _gone(paths: list[Path], dry_run: bool) -> list[Path]:
    """Remove what exists of `paths` (folders whole); the ones there were."""
    there = [p for p in paths if p.exists() or p.is_symlink()]
    if not dry_run:
        for p in there:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    p.unlink()
                except OSError:
                    pass
    return there


def _drop_jobs(ids: list[str], dry_run: bool) -> None:
    """Finished jobs out of the registry, and their folders (log, exit code)."""
    if not ids or dry_run:
        return
    with state.lock():
        reg = jobs._registry()
        for jid in ids:
            reg["jobs"].pop(jid, None)
        jobs._save(reg)
    base = state.p("jobs").resolve()
    for jid in ids:
        d = state.p("jobs", state.safe_id(jid)).resolve()
        if d.parent == base and d.is_dir():
            shutil.rmtree(d, ignore_errors=True)


def _work_folders(recs: list[dict[str, Any]]) -> list[str]:
    """Tracker folders in the projects (checkpoint.json beside a tracker)
    that these sessions wrote: named, never removed."""
    home = state.home().resolve()
    out = []
    for s in recs:
        cp = s.get("checkpoint") or ""
        if not cp:
            continue
        p = Path(cp).parent
        try:
            p.resolve().relative_to(home)
            continue      # checkpoints/<session>.json in the state folder
        except (ValueError, OSError):
            pass
        if p.is_dir() and str(p) not in out:
            out.append(str(p))
    return out


def _task_ref(ref: str) -> str:
    """A task id, or a unique prefix of one (at least 4 characters)."""
    ids = journal.tasks()
    if ref in ids:
        return ref
    hits = [t for t in ids if t.startswith(ref)] if len(ref) >= 4 else []
    return hits[0] if len(hits) == 1 else ""


def forget(ref: str, sid: str = "", cwd: str = "", dry_run: bool = False) -> tuple[bool, str]:
    """Remove one task's history: its journal (records, checkpoints, the
    listing cache), its exports in the state folder, its finished jobs and
    their logs, and the records of its sessions that belong to no other task
    (with their spools and pause-time checkpoints). A session it shares with
    another task keeps its record and its spool, which is journaled first
    (it may hold the other task's events). Refused while one of its
    sessions runs or is paused, or one of its jobs runs. (ok, the report)."""
    ref = (ref or "").strip()
    if not ref:
        from . import core
        rows = core.unfinished_tasks(cwd, limit=8) if cwd else []
        names = "; ".join(f"{m['task']} ({sanitize.clip(m.get('goal'), 40)})" for m in rows)
        return False, ("Say which task to forget: its id (`tasks` in the terminal helper lists them)"
                       + (f"; this project's unfinished ones: {names}" if names else "") + ". Nothing was removed.")
    task = _task_ref(ref)
    if not task:
        return False, f"No task matches '{sanitize.clip(ref, 80)}' (a task id or a unique prefix of one). Nothing was removed."
    live, why = _live()
    if live is None:
        return False, f"Not forgotten: {why}. Nothing was removed."
    meta = _meta(task)
    recs = [s for s in state.all_sessions() if s.get("task_id") == task]
    sessions = set(meta.get("sessions") or []) | {s["session_id"] for s in recs}
    blockers = []
    for s in sorted(sessions):
        if s in live.running:
            blockers.append(f"session {s[:8]} runs" + (" (this one)" if s == sid else ""))
        elif state.safe_id(s) in live.paused or s in live.paused:
            blockers.append(f"session {s[:8]} is paused")
    for j in live.jobs:
        if j.get("task_id") == task or j.get("session_id") in sessions:
            blockers.append(f"job {j.get('name')} ({j.get('id')}) runs")
    if blockers:
        return False, (f"Not forgotten: task {task} is still in use ({'; '.join(blockers)}). End or resume it there, "
                       "stop its jobs (/check:stop), then try again. Nothing was removed.")
    # Sessions that belong to no other task go whole; the others keep their
    # record (another task's lineage names them) and their spool: what waits
    # there may be the other task's, so it is ingested first (each event to
    # the session's task now; this task's then goes with its journal), and
    # kept as it is when that fails.
    others: set[str] = set()
    for t in journal.tasks():
        if t != task:
            others |= set(_meta(t).get("sessions") or [])
    whole = sorted(s for s in sessions if s not in others)
    shared = [s for s in sorted(sessions) if s not in whole and state.p("spool", state.safe_id(s)).is_dir()]
    ingested, kept_spools = [], []
    if not dry_run:
        from . import capture
        for s in shared:
            r = capture.ingest(s, sync=True)
            (kept_spools if r.error else ingested).append((s, r.error))
    v = journal.read(task)
    n_cp = sum(1 for r in v.records if r["kind"] == "checkpoint")
    paths = [journal.jdir(task)]
    paths += sorted(state.p("exports").glob(f"checkpoint-{state.safe_id(task)}-*.json"))
    for s in sorted(sessions):
        name = state.safe_id(s)
        if s in whole:
            paths += _session_paths(name)
        elif (state.load_session(s).get("task_id") or task) == task:
            paths.append(state.p("checkpoints", name + ".json"))   # its pause-time checkpoint was this task's
    paths += _running_files({state.safe_id(s) for s in whole}, keep=False)
    try:
        done = [j["id"] for j in jobs.refresh_all().values()
                if j.get("task_id") == task and jobs.is_final(j)]
    except Exception:  # noqa: BLE001
        done = []
    work = _work_folders(recs)
    gone = _gone(paths, dry_run)
    _drop_jobs(done, dry_run)
    exports = [p for p in gone if p.parent == state.p("exports")]
    n_sess = len([s for s in whole if state.p("sessions", state.safe_id(s) + ".json") in gone])
    verb = "Would remove" if dry_run else "Forgot"
    head = (f"{verb} task {task}" + (f" (\"{sanitize.clip(meta.get('goal'), 60)}\")" if meta.get("goal") else "")
            + (": its journal" if dry_run else ": removed its journal")
            + f" ({len(v.records)} record(s), {n_cp} checkpoint(s))"
            + (f", {len(done)} finished job(s) with their logs" if done else "")
            + (f", {len(exports)} export(s) in the state folder" if exports else "")
            + (f", {n_sess} session record(s) with their spools" if n_sess else "") + ".")
    tail = ""
    if shared and dry_run:
        tail += (f" It would first journal what waits in the spool of {len(shared)} session(s) it shares with another "
                 f"task ({', '.join(s[:8] for s in shared[:5])}), and keep those spools.")
    elif ingested:
        tail += (f" First journaled what waited in the spool of {len(ingested)} session(s) it shares with another "
                 f"task ({', '.join(s[:8] for s, _e in ingested[:5])}; those spools stay).")
    if kept_spools:
        tail += (" Kept as they were, since journaling them failed: the spools of "
                 + "; ".join(f"{s[:8]} ({sanitize.clip(e, 80)})" for s, e in kept_spools[:5]) + ".")
    if work:
        tail += (" Left alone (your project's tracker folders, with their checkpoint.json): "
                 + ", ".join(work[:5]) + "; `rm -rf <folder>` removes one.")
    return True, head + tail


def purge(dry_run: bool = False) -> tuple[bool, str]:
    """Remove everything in the state folder that no live or paused session
    and no running job needs: journals, session records and their files,
    spools, finished jobs and logs (but those of a kept session or task),
    exports, markers. Lists what it removes
    (with dry_run: would remove) and how to remove the projects' .work/
    folders; the terminal helper stays. (ok, the report)."""
    home = state.home()
    if not home.is_dir():
        return True, f"Nothing to purge: {home} doesn't exist."
    live, why = _live()
    if live is None:
        return False, f"Not purged: {why}. Nothing was removed."
    keep_sids = live.running | live.paused | {str(j.get("session_id")) for j in live.jobs if j.get("session_id")}
    keep_names = {state.safe_id(s) for s in keep_sids}
    recs = state.all_sessions()
    known = {state.safe_id(s["session_id"]) for s in recs}
    kept_notes = []
    for s in sorted(keep_sids):
        what = "runs" if s in live.running else "is paused" if s in live.paused or state.safe_id(s) in live.paused \
            else "has a running job"
        kept_notes.append(f"session {s[:8]} ({what})")
    projects = sorted({s.get("cwd") for s in recs if s.get("cwd")}
                      | {_meta(t).get("cwd") for t in journal.tasks() if _meta(t).get("cwd")})
    # Journals: all but those a kept session or a running job belongs to.
    keep_tasks = {t for t in journal.tasks()
                  if set(_meta(t).get("sessions") or []) & keep_sids}
    keep_tasks |= {s.get("task_id") for s in recs if s["session_id"] in keep_sids and s.get("task_id")}
    keep_tasks |= {j.get("task_id") for j in live.jobs if j.get("task_id")}
    drop_tasks = [t for t in journal.tasks() if t not in keep_tasks]
    goals = {t: _meta(t).get("goal") or "" for t in drop_tasks}
    paths: list[Path] = [journal.jdir(t) for t in drop_tasks]
    for t in sorted(keep_tasks):
        kept_notes.append(f"task {t}")
    # Sessions: every file of each session not kept, with or without a record.
    names = set(known)
    for d in SESSION_FILES + ("spool", "journal-errors", "spool-errors", "checkpoints", "gateway"):
        try:
            for f in state.p(d).iterdir():
                n = f.name
                for suffix in (".json", ".lock", ".last"):
                    n = n[:-len(suffix)] if n.endswith(suffix) else n
                n = n[len(".reserve."):] if n.startswith(".reserve.") else n
                if n and not n.startswith("."):     # a temp file or a bare suffix is no session
                    names.add(n)
        except OSError:
            continue
    recent_spools = []
    for n in sorted(names - keep_names):
        sp = state.p("spool", n)
        if n not in known and sp.is_dir():
            try:
                fresh = time.time() - sp.stat().st_mtime < SPOOL_RECENT_SECONDS
            except OSError:
                fresh = False
            if fresh:
                recent_spools.append(n)
                continue   # maybe a session that started before a runtime could record it
        paths += _session_paths(n)
    paths += _running_files(keep_names, keep=True)
    drop_sessions = [n for n in sorted(names - keep_names) if n in known]
    # Jobs: finished ones and their logs; folders the registry no longer names.
    try:
        reg = jobs.refresh_all()
    except Exception:  # noqa: BLE001
        reg = {}
    # A kept session's (or kept task's) finished jobs stay: a paused one's
    # checkpoint.json lists them, its resume compares them.
    done = [jid for jid, j in reg.items() if not jobs.is_live(j)
            and str(j.get("session_id") or "") not in keep_sids and (j.get("task_id") or "") not in keep_tasks]
    kept_done = [jid for jid, j in reg.items() if not jobs.is_live(j) and jid not in done]
    live_ids = {state.safe_id(str(j.get("id"))) for j in live.jobs}
    try:
        orphans = [d for d in state.p("jobs").iterdir()
                   if d.is_dir() and d.name not in live_ids and d.name not in {state.safe_id(x) for x in reg}]
    except OSError:
        orphans = []
    paths += orphans
    exports = sorted(state.p("exports").glob("*")) if state.p("exports").is_dir() else []
    paths += exports
    paths += [home / n for n in TOP_FILES]
    paths += [p for p in home.glob(".py.*.out") if _older(p, 3600)]
    gone = _gone(paths, dry_run)
    _drop_jobs(done, dry_run)
    for j in live.jobs:
        kept_notes.append(f"job {j.get('name')} ({j.get('id')}, running)")
    if kept_done:
        kept_notes.append(f"{len(kept_done)} finished job(s) of those sessions and tasks")
    left = sorted(p.name for p in home.iterdir()) if home.is_dir() else []
    known_dirs = set(SESSION_FILES) | {"sessions", "spool", "journal", "journal-errors", "spool-errors", "checkpoints",
                                       "gateway", "running", "jobs", "exports"}
    strangers = [n for n in left if n not in known_dirs and n not in TOP_KEEP and n not in TOP_FILES
                 and n != "jobs.json" and not n.startswith(".py.")]
    verb = "Would remove" if dry_run else "Removed"
    small = len([p for p in gone if p.parent.name in SESSION_FILES + ("journal-errors", "spool-errors", "gateway",
                                                                        "running", "checkpoints")
                 or p.parent == home])
    spools = [p.name for p in gone if p.parent == state.p("spool")]
    lines = [f"{'Purge (dry run)' if dry_run else 'Purged'} checkpoint's history in {home}:"]
    lines.append(f"- {verb} {len(drop_tasks)} task journal(s)" + (": " + "; ".join(
        f"{t} ({sanitize.clip(goals[t], 40)})" if goals[t] else t for t in drop_tasks[:30]) if drop_tasks else "")
        + (f"; +{len(drop_tasks) - 30} more" if len(drop_tasks) > 30 else ""))
    lines.append(f"- {verb} {len(drop_sessions)} session record(s)" + (": " + ", ".join(
        s[:8] for s in drop_sessions[:30]) if drop_sessions else "") + (f" (+{len(drop_sessions) - 30} more)"
                                                                         if len(drop_sessions) > 30 else ""))
    lines.append(f"- {verb} {len(spools)} spool(s) (events not yet in a journal, possibly unscrubbed)"
                 + (": " + ", ".join(s[:8] for s in spools[:30]) if spools else ""))
    lines.append(f"- {verb} {len(done)} finished job(s) with their logs"
                 + (": " + ", ".join(f"{reg[j].get('name')} ({j})" for j in done[:30]) if done else "")
                 + (f", and {len(orphans)} job folder(s) the registry no longer names" if orphans else ""))
    lines.append(f"- {verb} {len(exports)} export(s) in the state folder"
                 + (": " + ", ".join(p.name for p in exports[:10]) if exports else ""))
    lines.append(f"- {verb} {small} small file(s): per-session markers and counters, the guarded calls' "
                 "payloads (running/), error and degraded marks, caches")
    if kept_notes:
        lines.append("- Kept, still needed: " + "; ".join(kept_notes[:20]) + " (end the session or stop the job, "
                     "then purge again)")
    if recent_spools:
        lines.append(f"- Kept {len(recent_spools)} spool(s) written in the last day with no session record (a session "
                     "that may still run): " + ", ".join(s[:8] for s in recent_spools[:10]))
    lines.append(f"- Kept: the terminal helper ({home / 'checkpoint'}; rewritten at every session start)"
                 + (f"; not checkpoint's, left alone: {', '.join(strangers[:10])}" if strangers else ""))
    lines.append("Your projects' .work/ folders (trackers and checkpoint.json) are yours and stay: remove one with "
                 "`rm -rf <project>/.work`, and the `.work/` line from the clone's exclude file "
                 "(`git rev-parse --git-path info/exclude`)"
                 + (f"; projects seen: {', '.join(projects[:10])}" if projects else "")
                 + ".")
    lines.append(f"To remove everything, the helper too, with no Claude Code session running: rm -rf '{home}'"
                 " (and uninstall the plugin, or the next session start creates it again).")
    return True, "\n".join(lines)


def _older(p: Path, seconds: float) -> bool:
    try:
        return time.time() - p.stat().st_mtime > seconds
    except OSError:
        return False
