"""Shared state on disk: sessions, pause flags, counters, fixed settings.

Layout under home() (CHECKPOINT_HOME, default
${XDG_STATE_HOME:-~/.local/state}/checkpoint):

    sessions/<sid>.json   {session_id, cwd, claude_pid, transcript_path,
                           created_ts, started_ts, paused, paused_ts, ...}
    flags/<sid>           exists = this session is paused (the shell pre-filter reads it)
    now/<sid>             "kill the running foreground command" request for its watcher
    pending/<sid>         un-paused without a message (resume all, terminal):
                          the next user prompt still gets the resume context
    running/<tool_use_id>.json   foreground Bash calls a watcher is guarding
    seen/<sid>            last queued pause line already handled (pre-filter compares it)
    queued/<sid>          queued pause commands were applied early (the session's
                          queued_pauses); every user prompt is checked for their
                          echoes while there are any (the pre-filter sends it to Python)
    trackers/<sid>        trackers this session wrote (one path per line; fast.sh)
    count/<sid>           tool calls since the tracker was last written
    turn/<sid>, turn-start/<sid>  tool calls this turn, and when it started
    checkpoints/<sid>.json  checkpoint for sessions with no tracker
    jobs.json, jobs/<id>/   job registry and logs (jobs.py; a gateway job's record names its
                          supervisor and the gateway's job id, and has no folder)
    gateway/<sid>.json    the session's cursor on the gateway's events (gateway.py: epoch,
                          next seq, what it follows); gateway/<sid>.lock: one pause/resume
                          decision at a time
    spool/<sid>/          hook and mod events waiting for Python (capture.py; fast.sh writes it)
    journal/<task>/       the durable journal of a task (journal.py); removed after
                          CHECKPOINT_KEEP_DAYS (finished) / CHECKPOINT_KEEP_OPEN_DAYS (open)
    exports/              /check:export bundles (recovery.export; never pruned)
    journal-errors/<sid>  the last journal write failure a session hit (.last: recovered)
    spool-errors/<sid>    "<time> <why>": since when a session's hooks can't spool (fast.sh;
                          .last once recording works again, and a recording.gap record);
                          .reserve.<sid> in both: what a failure on a full disk becomes
    rt/<sid>              the runtime env folder SessionStart chose (fast.sh; the mod reads it)
    degraded/<sid>        "<time> <why>": a hook last ran without its Python half (fast.sh)
    noresume/<sid>        the mod says the prompt now being submitted isn't the user's
                          (isMeta, scheduled, automation): it must not resume (fresh ones only)
    checkpoint            the terminal helper (fast.sh writes it at every session start)
    pruned                when old records were last pruned
    session-start.err     stderr of the last SessionStart (shown to the user if it failed)

The shell pre-filter (hooks/fast.sh, hooks/watch.sh, hooks/lib.sh) and the
mod (hooks/mod/) use the same paths, so keep them in sync. Everything here is
private: folders 0700, files 0600 (run.py and the hooks run under umask 077;
fix_modes() converts an older state folder once). Nothing is stored in the
plugin folder: installed copies live in a versioned cache folder that an
update replaces; the runtime env lives in Claude Code's plugin data folder
(hooks/lib.sh).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pwd
import re
import shlex
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

PLUGIN_ROOT = Path(__file__).resolve().parents[2]

# Fixed settings. There is deliberately no settings file: a file in the plugin
# folder is replaced on update, and the user wants no settings.json entries.
MAX_DENIES = 3               # denied calls while paused before continue:false is added
KILL_GRACE_SECONDS = 3.0     # `pause now`: SIGTERM, then SIGKILL after this
JOB_STOP_GRACE_SECONDS = 5.0
SESSION_RETENTION_DAYS = 7   # dead, un-paused session records older than this are pruned
JOB_RETENTION_DAYS = 14      # finished jobs (record and log) older than this are pruned
# A queued pause command applied early is matched with its echo (the same
# command run when the turn ends) for at most this long, and only when the
# transcript can't say whether the echo already came.
QUEUED_ECHO_MAX_AGE_SECONDS = 6 * 3600
# The mod's noresume/<sid> covers the prompt being submitted only; an older one
# (a mod that died mid-prompt) is ignored, so the user's message still resumes.
NORESUME_MAX_AGE_SECONDS = 120
ENV_KEEP_DAYS = 7            # runtime envs unused this long are removed (hooks/lib.sh)
DEGRADED_SHOW_HOURS = 24     # degraded/<sid> markers this recent are mentioned at SessionStart
ALLOW_WHILE_PAUSED = (
    "Read", "Glob", "Grep", "TodoWrite", "TaskList", "TaskGet", "TaskCreate", "TaskUpdate",
    "mcp__ccd_turn__*",
    "mcp__plugin_check_jobs__job_status",
    "mcp__plugin_check_jobs__job_tail",
    "mcp__plugin_check_jobs__job_stop",
    "mcp__plugin_check_jobs__checkpoint_note",
    "mcp__plugin_check_jobs__task_policy",
    # The gateway's read-only tools (CONTRACT-v1 partner): inspecting its
    # terminals and jobs never waits behind a pause (its writes, new jobs and
    # signals do: the gateway refuses those for a paused session itself).
    "mcp__plugin_gate_gw__terminal_read",
    "mcp__plugin_gate_gw__terminal_list",
    "mcp__plugin_gate_gw__job_status",
    "mcp__plugin_gate_gw__job_tail",
    "mcp__plugin_gate_gw__job_list",
)
NON_USER_PROMPT_PREFIXES = (
    "<task-notification>", "[Subagent hand-back]", "<side-sessions-event>",
    "<ci-monitor-event>", "<local-command-", "<command-message>",
)


# Journal retention: a task's journal is removed once it was last updated
# this many days ago (finished: marked complete or canceled; open: the
# rest), never while one of its sessions is alive or paused or one of its
# jobs runs. Environment variables (no settings file: invariant 7), 0 keeps
# them forever. README "Data, retention and backup".
JOURNAL_KEEP_DAYS = 30
JOURNAL_KEEP_OPEN_DAYS = 180


def _days(name: str, default: int) -> int:
    v = (os.environ.get(name) or "").strip().lower()
    if v in ("never", "forever", "off"):
        return 0
    try:
        return max(0, int(float(v))) if v else default
    except ValueError:
        return default


def journal_keep_days() -> int:
    """CHECKPOINT_KEEP_DAYS: finished tasks' journals (default 30; 0 = forever)."""
    return _days("CHECKPOINT_KEEP_DAYS", JOURNAL_KEEP_DAYS)


def journal_keep_open_days() -> int:
    """CHECKPOINT_KEEP_OPEN_DAYS: unfinished tasks' journals (default 180; 0 = forever)."""
    return _days("CHECKPOINT_KEEP_OPEN_DAYS", JOURNAL_KEEP_OPEN_DAYS)


def nudge_every() -> int:
    """Tracker reminder every N tool calls. hooks/fast.sh reads the same
    variable with the same default, so the two always agree."""
    try:
        return max(1, int(os.environ.get("CHECKPOINT_NUDGE_EVERY") or 25))
    except ValueError:
        return 25


def work_dir_name() -> str:
    """Trackers live in <project>/<this>/<task>/TRACKER.md. Not under .claude/:
    Claude Code asks before every write there, and no rule or hook can lift
    that. An env var, because hooks/fast.sh reads it too."""
    return os.environ.get("CHECKPOINT_WORK_DIR") or ".work"


def home() -> Path:
    env = os.environ.get("CHECKPOINT_HOME")
    if env:
        return Path(env)
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "checkpoint"


def user_umask() -> int:
    """The umask the user's session had (run.py and the hooks set 077 for
    checkpoint's own files): jobs and git run with it."""
    try:
        return int(os.environ.get("CHECKPOINT_USER_UMASK") or "", 8) & 0o777
    except ValueError:
        return 0o022


def ensure_home() -> Path:
    """CHECKPOINT_HOME, created private (0700), and made private if an
    older version left it readable."""
    h = home()
    h.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        if os.stat(h).st_mode & 0o077:
            os.chmod(h, 0o700)
    return h


def fix_modes() -> int:
    """Once per state folder: make what versions before 0.3 wrote private
    (folders 0700, files 0600; the terminal helper 0700). Symlinks are left
    alone. Returns how many modes changed."""
    h = ensure_home()
    mark = h / ".modes-v1"
    if mark.exists():
        return 0
    n = 0
    for dirpath, dirnames, filenames in os.walk(h):
        for name, want in [(d, 0o700) for d in dirnames] + [(f, 0o600) for f in filenames]:
            path = os.path.join(dirpath, name)
            if dirpath == str(h) and name == "checkpoint":
                want = 0o700
            try:
                st_ = os.lstat(path)
            except OSError:
                continue
            if os.path.islink(path) or (st_.st_mode & 0o777) == want:
                continue
            with contextlib.suppress(OSError):
                os.chmod(path, want)
                n += 1
    touch(mark, now_iso())
    return n


def gateway_env_path() -> str:
    """The gateway plugin's env file, which jobs apply when it is there
    (jobs.gateway_env). The gateway writes the text of its CLAUDE_ENV_FILE
    there at every session start, in ~/.config/gateway of the password
    database's home, not $HOME (as the gateway finds it). CHECKPOINT_GATEWAY_ENV
    overrides it (tests); set but empty, jobs never apply one."""
    env = os.environ.get("CHECKPOINT_GATEWAY_ENV")
    if env is not None:
        return os.path.abspath(env) if env else ""
    try:
        user_home = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:  # no passwd entry: the gateway falls back the same way
        user_home = os.path.expanduser("~")
    return os.path.join(user_home, ".config", "gateway", "env.sh")


def safe_id(s: str) -> str:
    """A session or tool-use id as a file name (same rule as fast.sh)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", s or "")[:128] or "_"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def iso_ts(s: str) -> float | None:
    """Epoch seconds for a now_iso() string (offset included), else None."""
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except (TypeError, ValueError):
        return None


def p(*parts: str) -> Path:
    return home().joinpath(*parts)


def cli(*args: str) -> str:
    """A command line for the terminal helper (`checkpoint` in the state
    folder, written at every session start), for texts that point at a CLI
    command: unlike a pixi task, it works wherever the plugin is installed
    (in a user's pixi project, `pixi run` with checkpoint's task names would
    run that project's own tasks)."""
    path = str(p("checkpoint"))
    user = os.path.expanduser("~")
    if path.startswith(user + "/") and shlex.quote(path[len(user):]) == path[len(user):]:
        shown = "~" + path[len(user):]
    else:
        shown = shlex.quote(path)
    return " ".join([shown, *(shlex.quote(a) for a in args)])


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data: Any) -> None:
    """Write whole (temp file, then rename), private (0600) whatever the umask."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as f:
            f.write(json.dumps(data, indent=2) + "\n")
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    os.replace(tmp, path)


# A reserve per session for each failure marker (spool-errors/, journal-errors/):
# RESERVE_BYTES of spaces written while there is room (session start, hooks/
# lib.sh ck_arm = CK_RESERVE_BYTES; and after a recovery). A failure on a full
# disk can't create its marker as a new file, but it can rename the reserve
# into place and overwrite it (no new blocks on most filesystems), so the
# failure is still on record once space is back.
RESERVE_BYTES = 1024


def reserve_path(folder: str, sid: str) -> Path:
    return p(folder, ".reserve." + safe_id(sid))


def arm_reserve(folder: str, sid: str) -> None:
    """Write the reserve for `folder`/<session> unless it is there (best effort)."""
    r = reserve_path(folder, sid)
    if r.exists():
        return
    with contextlib.suppress(OSError):
        r.parent.mkdir(parents=True, exist_ok=True)
        tmp = r.with_name(f"..reserve.{safe_id(sid)}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, b" " * RESERVE_BYTES)
        finally:
            os.close(fd)
        os.replace(tmp, r)


def write_reserved(path: Path, folder: str, sid: str, text: str) -> bool:
    """Put `text` (cut to RESERVE_BYTES - 1 bytes) at `path` through the
    session's reserve: renamed into place, then overwritten in place (the rest
    stays spaces: still valid JSON after a JSON text). False when there is no
    reserve or it can't be moved."""
    try:
        os.replace(reserve_path(folder, sid), path)
    except OSError:
        return False
    data = text.encode("utf-8", "replace")[: RESERVE_BYTES - 1] + b"\n"
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_WRONLY)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    return True


def touch(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf8")


def remove(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


@contextlib.contextmanager
def lock() -> Iterator[None]:
    ensure_home()
    with open(home() / ".lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# -- sessions -----------------------------------------------------------------

def session_path(sid: str) -> Path:
    return p("sessions", safe_id(sid) + ".json")


def load_session(sid: str) -> dict[str, Any]:
    rec = read_json(session_path(sid), {}) or {}
    rec.setdefault("session_id", sid)
    return rec


def update_session(sid: str, **fields: Any) -> dict[str, Any]:
    with lock():
        rec = load_session(sid)
        rec.update(fields)
        write_json(session_path(sid), rec)
    return rec


def all_sessions() -> list[dict[str, Any]]:
    out = []
    for f in sorted(p("sessions").glob("*.json")):
        rec = read_json(f)
        if isinstance(rec, dict) and rec.get("session_id"):
            out.append(rec)
    return out


def find_session(prefix: str) -> dict[str, Any] | None:
    """A session by full id or unique prefix (as /status prints them)."""
    hits = [s for s in all_sessions() if s["session_id"].startswith(prefix)]
    return hits[0] if len(hits) == 1 else None


def started_ts(rec: dict[str, Any]) -> float:
    return float(rec.get("started_ts") or rec.get("created_ts") or 0)


def live_sessions(table: dict[int, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Sessions whose Claude process is running: for each live Claude pid,
    the newest session record (after /clear one process has had several).
    Without a process table (ps failed), procs.is_claude asks the OS about
    each pid instead."""
    from . import procs
    table = procs.table() if table is None else table
    best: dict[int, dict[str, Any]] = {}
    for s in all_sessions():
        pid = s.get("claude_pid")
        if not isinstance(pid, int) or not procs.is_claude(pid, table):
            continue
        if pid not in best or started_ts(s) > started_ts(best[pid]):
            best[pid] = s
    return list(best.values())


def claim_queue(sid: str, stamps: list[str]) -> list[str]:
    """Mark the queued commands enqueued at these timestamps as handled, all
    at once, and return the ones this call claimed: those newer than the
    last one handled. The gate and a watcher may see them at once; each is
    claimed by one of them only."""
    with lock():
        rec = load_session(sid)
        handled = rec.get("queue_handled") or ""
        mine = [ts for ts in stamps if not ts or ts > handled]
        newest = max((ts for ts in mine if ts), default="")
        if newest:
            rec["queue_handled"] = newest
            write_json(session_path(sid), rec)
    return mine


# The queued pause commands applied early, each waiting for its echo (the
# same command run as a prompt), oldest first: {content, ts (its enqueue
# timestamp), at, at_ts, result}. queued/<sid> exists while there is one, so
# the pre-filter sends every prompt to Python.

def queued_pauses(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """The remembered queued pause commands in a session record, oldest first."""
    q = rec.get("queued_pauses")
    out = [e for e in q if isinstance(e, dict)] if isinstance(q, list) else []
    return sorted(out, key=lambda e: (str(e.get("ts") or ""), float(e.get("at_ts") or 0)))


def _same_entry(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return a.get("ts") == b.get("ts") and a.get("at_ts") == b.get("at_ts")


def _write_queued(sid: str, rec: dict[str, Any], q: list[dict[str, Any]]) -> None:
    """Under lock(): store the list and keep queued/<sid> in step with it."""
    rec["queued_pauses"] = q
    rec.pop("queued_pause", None)  # the single entry of an unreleased 0.2 build
    write_json(session_path(sid), rec)
    if q:
        touch(p("queued", safe_id(sid)), now_iso())
    else:
        remove(p("queued", safe_id(sid)))


def add_queued(sid: str, *entries: dict[str, Any]) -> None:
    """Remember queued pause commands before they are applied (all at once):
    an echo can come while they are still being applied (`now` kills a
    command, and Claude Code runs an absorbed prompt as soon as that call
    ends)."""
    stamps = {e.get("ts") for e in entries if e.get("ts")}
    with lock():
        rec = load_session(sid)
        q = [e for e in queued_pauses(rec) if e.get("ts") not in stamps]
        _write_queued(sid, rec, q + list(entries))


def update_queued(sid: str, entry: dict[str, Any], **fields: Any) -> None:
    """Fill in a remembered entry (its result), unless its echo already came
    and took it."""
    with lock():
        rec = load_session(sid)
        q = queued_pauses(rec)
        for e in q:
            if _same_entry(e, entry):
                e.update(fields)
                _write_queued(sid, rec, q)
                return


def drop_queued(sid: str, entries: list[dict[str, Any]] | None = None) -> None:
    """Forget these remembered entries (None: all of them). Entries added
    meanwhile are kept."""
    with lock():
        rec = load_session(sid)
        q = [] if entries is None else [
            e for e in queued_pauses(rec) if not any(_same_entry(e, d) for d in entries)]
        _write_queued(sid, rec, q)


def add_interrupted(sid: str, items: list[dict[str, Any]]) -> None:
    """Record commands `pause now` killed (once per tool call, even when two
    processes killed the same one). A killed command is no longer one it
    couldn't stop."""
    if not items:
        return
    with lock():
        rec = load_session(sid)
        have = rec.get("interrupted") or []
        ids = {c.get("tool_use_id") for c in have}
        rec["interrupted"] = have + [c for c in items if c.get("tool_use_id") not in ids]
        killed = {c.get("tool_use_id") for c in items}
        if rec.get("unstopped"):
            rec["unstopped"] = [c for c in rec["unstopped"] if c.get("tool_use_id") not in killed]
        write_json(session_path(sid), rec)


def add_unstopped(sid: str, items: list[dict[str, Any]]) -> None:
    """Record running commands `pause now` couldn't stop because the process
    table couldn't be read (once per tool call), so that a later reply, such
    as the queued command's echo when the turn ends, still says so instead
    of "No command was running". Cleared when the session is resumed."""
    if not items:
        return
    with lock():
        rec = load_session(sid)
        have = rec.get("unstopped") or []
        ids = {c.get("tool_use_id") for c in have + (rec.get("interrupted") or [])}
        new = [c for c in items if c.get("tool_use_id") not in ids]
        if new:
            rec["unstopped"] = have + new
            write_json(session_path(sid), rec)


def written_trackers(sid: str) -> set[str]:
    """Trackers this session wrote (recorded by fast.sh at PostToolUse)."""
    try:
        text = p("trackers", safe_id(sid)).read_text(encoding="utf8")
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


# -- pause flags --------------------------------------------------------------

def is_paused(sid: str) -> bool:
    return p("flags", safe_id(sid)).exists()


def resume_pending(sid: str) -> bool:
    return p("pending", safe_id(sid)).exists()


def flag_info(sid: str) -> tuple[str, str, float]:
    """(when, by, epoch) from flags/<sid>, as the shell and the mod write it
    ("<iso time> <who>"); Python's own flags hold the time alone."""
    path = p("flags", safe_id(sid))
    try:
        text = path.read_text(encoding="utf8").strip()
        ts = path.stat().st_mtime
    except OSError:
        return "", "", 0.0
    when, _, by = text.partition(" ")
    return when, by.strip(), ts


def sync_flag(sid: str) -> bool:
    """A pause the shell fallback or the mod recorded (flags/<sid> only) gets
    its session record, as set_paused would have written it. True if it did.
    A flag newer than the record's pause is another pause (the shell's
    fallback resumed without Python, then paused again): nothing rewrites a
    flag that is there, so its time changes only with a new pause."""
    if not is_paused(sid):
        return False
    rec = load_session(sid)
    if rec.get("paused") and float(rec.get("paused_ts") or 0) >= flag_info(sid)[2] - 1:
        return False
    when, by, ts = flag_info(sid)
    update_session(sid, paused=True, paused_at=when or now_iso(), paused_ts=ts or time.time(),
                   paused_by=by or "flag", deny_count=0, checkpoint_written=False)
    return True


def noresume(sid: str) -> bool:
    """The mod marked the prompt now being submitted as not the user's own.
    Consumed here (removed); a stale marker counts as none."""
    path = p("noresume", safe_id(sid))
    try:
        fresh = time.time() - path.stat().st_mtime < NORESUME_MAX_AGE_SECONDS
    except OSError:
        return False
    remove(path)
    return fresh


def degraded_markers(since: float = 0.0) -> list[tuple[str, str, float]]:
    """(session, "<time> <why>", mtime) of degraded/<sid> markers newer than
    `since`, newest first."""
    out = []
    for f in p("degraded").glob("*"):
        try:
            out.append((f.name, f.read_text(encoding="utf8").strip()[:300], f.stat().st_mtime))
        except OSError:
            continue
    return sorted((m for m in out if m[2] > since), key=lambda m: -m[2])


def set_paused(sid: str, by: str, now: bool = False) -> bool:
    """Pause one session. Returns False if it was already paused. A flag
    that is already there is left as it is (like env.sh's `flag pause` and
    fast.sh's fallbacks): its time is when this pause began, which sync_flag
    reads, so a second pause command during the same pause (typed again, a
    queued command's echo, `pause all` from another session) is the same
    pause, with the same mark and gateway pause id (core.pause_id)."""
    was = is_paused(sid)
    if not was:
        touch(p("flags", safe_id(sid)), now_iso())
        update_session(sid, paused=True, paused_at=now_iso(), paused_ts=time.time(),
                       paused_by=by, deny_count=0, checkpoint_written=False)
    if now:
        touch(p("now", safe_id(sid)), now_iso())
    return not was


def clear_paused(sid: str, pending: bool = False) -> bool:
    """Resume one session. Returns False if it wasn't paused. With pending
    (no message from the user yet), its next prompt still gets the resume
    context."""
    was = is_paused(sid)
    remove(p("flags", safe_id(sid)))
    remove(p("now", safe_id(sid)))
    if pending and was:
        touch(p("pending", safe_id(sid)), now_iso())
    elif not pending:
        remove(p("pending", safe_id(sid)))
    # unstopped belongs to this pause only; interrupted stays for the
    # checkpoint until the resume context has used it (core.resume_here).
    update_session(sid, paused=False, resumed_at=now_iso(), resumed_ts=time.time(),
                   deny_count=0, unstopped=[])
    return was


def migrate_legacy() -> None:
    """Version 0.1 kept `pause all` as one global paused-all file, which never
    cleared itself and paused sessions started later. Turn it into flags for
    the live sessions it still covered, then drop it."""
    path = p("paused-all")
    if not path.exists():
        return
    pa = read_json(path, {}) or {}
    ts = float(pa.get("ts") or 0)
    for s in live_sessions():
        if float(s.get("resumed_ts") or 0) < ts and float(s.get("created_ts") or 0) < ts:
            set_paused(s["session_id"], by=str(pa.get("by") or "pause all"))
    remove(path)
    remove(p("now-all"))


# -- pruning ------------------------------------------------------------------

def _prune_spool(name: str) -> None:
    """A pruned session's spool: ingested first (its records belong in the
    journal), then removed; left alone if ingesting fails."""
    d = p("spool", name)
    if not d.is_dir():
        return
    from . import capture
    rec = load_session(name)
    if any(d.glob("*.ev")) and capture.ingest(rec.get("session_id") or name, sync=True).error:
        return
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def prune(force: bool = False) -> dict[str, int]:
    """Drop records nobody needs any more (at most every 6 hours unless
    forced): sessions whose Claude process is gone, that aren't paused and
    were last active more than SESSION_RETENTION_DAYS ago; running/ files of
    dead sessions or older than a day; finished jobs and their logs older than
    JOB_RETENTION_DAYS."""
    from . import jobs, procs
    stamp = p("pruned")
    now = time.time()
    try:
        if not force and now - stamp.stat().st_mtime < 6 * 3600:
            return {}
    except OSError:
        pass
    table = procs.table()
    if not table:
        return {}  # no process table (ps failed): every session would look dead
    touch(stamp, now_iso())
    out = {"sessions": 0, "running": 0, "jobs": 0}
    cutoff = now - SESSION_RETENTION_DAYS * 86400
    dead: set[str] = set()   # sessions whose recorded Claude process is gone
    doomed = []
    for f in p("sessions").glob("*.json"):
        rec = read_json(f, {}) or {}
        sid = rec.get("session_id") or f.stem
        pid = rec.get("claude_pid")
        live = isinstance(pid, int) and procs.is_claude(pid, table)
        if isinstance(pid, int) and not live:
            dead.add(sid)
        if live or is_paused(sid):
            continue
        try:
            last = max(f.stat().st_mtime, started_ts(rec))
        except OSError:
            continue
        if last < cutoff:
            doomed.append((sid, f))
    for f in p("running").glob("*.json"):
        rec = read_json(f, {}) or {}
        rsid = rec.get("session_id") or ""
        try:
            old = now - f.stat().st_mtime > 86400
        except OSError:
            continue
        if old or rsid in dead:
            remove(f)
            out["running"] += 1
    for sid, f in doomed:
        name = safe_id(sid)
        for d in ("count", "turn", "turn-start", "seen", "now", "pending", "queued", "trackers",
                  "rt", "degraded", "noresume", "spool-errors", "journal-errors"):
            remove(p(d, name))
        remove(p("journal-errors", name + ".last"))
        remove(p("spool-errors", name + ".last"))
        for d in ("spool-errors", "journal-errors"):
            remove(reserve_path(d, name))
        _prune_spool(name)
        remove(p("checkpoints", name + ".json"))
        remove(f)
        out["sessions"] += 1
    out["jobs"] = jobs.prune(JOB_RETENTION_DAYS)
    from . import recovery
    out["tasks"] = len(recovery.prune_tasks(now))
    return out
