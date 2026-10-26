"""Trackers: <cwd>/.work/<task>/TRACKER.md (CHECKPOINT_WORK_DIR), kept by the agent.

Hooks only read trackers; they never change what the agent or the user wrote
(the pause-time checkpoint lives in the state folder: core.checkpoint_path).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import state

TEMPLATE = """\
# <Task title>
Goal: <one or two sentences; what "finished" means>
Started: <date>  Session: <session id>  Updated: <timestamp>

## Stages
| # | Stage | Status | Notes / details |
|---|-------|--------|-----------------|
| 1 | ...   | in progress | |
| 2 | ...   | todo     | |

Status: todo | in progress | done (agent thinks it's finished) |
accepted (the user confirmed it in chat) | interrupted (stopped before it finished:
check what it already did before redoing any of it)

## Now
Current step: <exactly what is being done>
Next action: <the very next concrete thing to do>

## Jobs
| Job | Command | Started | State | Redo / resume |
|-----|---------|---------|-------|---------------|

## Resume notes
- Check first: <things that may have changed or died while paused>
- Assumptions: <anything a new session wouldn't know>
"""

# What a session id written after "Session:" looks like (at least 8 chars of
# it). Anything else, such as the template's "<session id>", claims nothing.
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,}")


def work_dir(cwd: str | Path) -> Path:
    return Path(cwd) / state.work_dir_name()


def trackers(cwd: str | Path) -> list[Path]:
    return sorted(work_dir(cwd).glob("*/TRACKER.md"))


def parse(text: str) -> dict[str, Any]:
    title = ""
    m = re.search(r"^#\s+(.+)$", text, re.M)
    if m:
        title = m.group(1).strip()
    sections: dict[str, str] = {}
    for sm in re.finditer(r"^##\s+(.+?)\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S):
        sections[sm.group(1).strip()] = sm.group(2).strip()
    stages = []
    for row in sections.get("Stages", "").splitlines():
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0] not in ("#", "") and not set(cells[0]) <= set("-: "):
            stages.append({"n": cells[0], "stage": cells[1], "status": cells[2].lower(),
                           "notes": cells[3] if len(cells) > 3 else ""})
    session = ""
    m = re.search(r"Session:\s*(\S+)", text)
    if m:
        session = m.group(1).strip("`*_\"'()[],;.")  # markdown around the id
    return {"title": title, "sections": sections, "stages": stages, "session": session}


def info(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf8")
    except OSError:
        text = ""
    d = parse(text)
    d["path"] = str(path)
    d["slug"] = path.parent.name
    d["mtime"] = path.stat().st_mtime if path.exists() else 0
    d["in_progress"] = [s for s in d["stages"] if s["status"].startswith("in progress")]
    d["open"] = [s for s in d["stages"] if not s["status"].startswith(("done", "accepted"))]
    return d


def claimed(d: dict[str, Any]) -> bool:
    """The tracker names a session (whichever) after 'Session:'."""
    return bool(_SESSION_ID.fullmatch(d["session"]))


def owned_by(d: dict[str, Any], sid: str) -> bool:
    """The tracker names this session (its id, or a prefix of 8+ chars)."""
    return bool(sid) and claimed(d) and sid.startswith(d["session"])


def owned(cwd: str | Path, sid: str) -> dict[str, Any] | None:
    for t in trackers(cwd):
        d = info(t)
        if owned_by(d, sid):
            return d
    return None


def _same_file(path: str, written: set[str]) -> bool:
    try:
        real = str(Path(path).resolve())
    except OSError:
        real = path
    for w in written:
        try:
            if w == path or str(Path(w).resolve()) == real:
                return True
        except OSError:
            continue
    return False


def unclaimed(cwd: str | Path, written: set[str] | None = None) -> dict[str, Any] | None:
    """The most recently changed tracker with a stage in progress that names
    no session (the agent forgot to fill in 'Session:') and that this
    session wrote (`written`: paths of its tracker writes). A tracker that
    names another session, or that only another session wrote, is never
    returned: it is that session's work."""
    if not written:
        return None
    active = [d for d in (info(t) for t in trackers(cwd))
              if d["in_progress"] and not claimed(d) and _same_file(d["path"], written)]
    return max(active, key=lambda d: d["mtime"]) if active else None


def for_session(cwd: str | Path, sid: str, written: set[str] | None = None) -> dict[str, Any] | None:
    """The tracker this session works on: one naming it, else an unclaimed
    one with a stage in progress that it wrote."""
    return owned(cwd, sid) or unclaimed(cwd, written)


def by_slug(cwd: str | Path, slug: str) -> dict[str, Any] | None:
    path = work_dir(cwd) / slug / "TRACKER.md"
    if path.exists():
        return info(path)
    hits = [t for t in trackers(cwd) if slug.lower() in t.parent.name.lower()]
    return info(hits[0]) if len(hits) == 1 else None
