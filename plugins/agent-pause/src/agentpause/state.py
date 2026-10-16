"""Shared state on disk: sessions, pause flags, counters, config.

Layout under home() (AGENT_PAUSE_HOME, default
${XDG_STATE_HOME:-~/.local/state}/agent-pause):

    sessions/<sid>.json   {session_id, cwd, claude_pid, transcript_path, paused, ...}
    flags/<sid>           exists = this session is paused (the shell pre-filter reads it)
    paused-all            exists = every session is paused; JSON {at, by}
    now/<sid>, now-all    "kill the running foreground command" requests for watchers
    running/<tool_use_id>.json   foreground Bash calls a watcher is guarding
    seen/<sid>            last queued pause line already handled (pre-filter compares it)
    count/<sid>           tool calls since the tracker was last written
    turn/<sid>            tool calls this turn (reset by each user prompt)
    checkpoints/<sid>.json  checkpoint for sessions with no tracker
    jobs.json, jobs/<id>/   job registry and logs (jobs.py)

The shell pre-filter (hooks/fast.sh) uses the same paths, so keep them in sync.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterator

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def work_dir_name() -> str:
    """Trackers live in <project>/<this>/<task>/TRACKER.md. Not under .claude/:
    Claude Code asks before every write there, and no rule or hook can lift
    that. An env var, not config, because hooks/fast.sh reads it too."""
    return os.environ.get("AGENT_PAUSE_WORK_DIR") or ".work"

DEFAULT_CONFIG: dict[str, Any] = {
    "nudge_every": 25,
    "max_denies": 3,
    "allow_while_paused": [
        "Read", "Glob", "Grep", "TodoWrite", "TaskList", "TaskGet",
        "mcp__ccd_turn__*",
        "mcp__plugin_agent-pause_jobs__job_status",
        "mcp__plugin_agent-pause_jobs__job_tail",
        "mcp__plugin_agent-pause_jobs__job_stop",
    ],
    "non_user_prompt_prefixes": [
        "<task-notification>", "[Subagent hand-back]", "<side-sessions-event>",
        "<ci-monitor-event>", "<local-command-", "<command-message>",
    ],
    "kill_grace_seconds": 3,
}


def home() -> Path:
    env = os.environ.get("AGENT_PAUSE_HOME")
    if env:
        return Path(env)
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "agent-pause"


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    path = Path(os.environ.get("AGENT_PAUSE_CONFIG") or PLUGIN_ROOT / "config" / "settings.json")
    try:
        cfg.update({k: v for k, v in json.loads(path.read_text()).items()
                    if not k.startswith("_")})
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        import sys
        print(f"agent-pause: ignoring unreadable {path}: {e}", file=sys.stderr)
    return cfg


def safe_id(s: str) -> str:
    """A session or tool-use id as a file name (same rule as fast.sh)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", s or "")[:128] or "_"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def p(*parts: str) -> Path:
    return home().joinpath(*parts)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def touch(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def remove(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


@contextlib.contextmanager
def lock() -> Iterator[None]:
    home().mkdir(parents=True, exist_ok=True)
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


# -- pause flags --------------------------------------------------------------

def paused_all() -> dict[str, Any] | None:
    path = p("paused-all")
    if not path.exists():
        return None
    return read_json(path, {}) or {"at": ""}


def is_paused(sid: str) -> bool:
    if p("flags", safe_id(sid)).exists():
        return True
    pa = paused_all()
    if pa is None:
        return False
    # A user message in this session after `pause all` resumes just this one.
    return float(load_session(sid).get("resumed_ts") or 0) < float(pa.get("ts") or 0)


def set_paused(sid: str, by: str, now: bool = False) -> bool:
    """Pause one session. Returns False if it was already paused."""
    was = is_paused(sid)
    touch(p("flags", safe_id(sid)), now_iso())
    if not was:
        update_session(sid, paused=True, paused_at=now_iso(), paused_by=by,
                       deny_count=0, checkpoint_written=False)
    if now:
        touch(p("now", safe_id(sid)), now_iso())
    return not was


def clear_paused(sid: str) -> bool:
    """Resume one session. Returns False if it wasn't paused."""
    was = is_paused(sid)
    remove(p("flags", safe_id(sid)))
    remove(p("now", safe_id(sid)))
    update_session(sid, paused=False, resumed_at=now_iso(), resumed_ts=time.time(),
                   deny_count=0)
    return was


def set_paused_all(by: str, now: bool = False) -> None:
    write_json(p("paused-all"), {"at": now_iso(), "ts": time.time(), "by": by})
    if now:
        touch(p("now-all"), now_iso())


def clear_paused_all() -> bool:
    was = p("paused-all").exists()
    remove(p("paused-all"))
    remove(p("now-all"))
    return was
