"""Trackers: <cwd>/.work/<task>/TRACKER.md (CHECKPOINT_WORK_DIR), kept by the agent.

Hooks only read trackers (and write checkpoint.json next to them); they never
change what the agent or the user wrote.
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
accepted (the user confirmed it in chat) | interrupted (redo from start of stage)

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
        session = m.group(1)
    return {"title": title, "sections": sections, "stages": stages, "session": session}


def info(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text()
    except OSError:
        text = ""
    d = parse(text)
    d["path"] = str(path)
    d["slug"] = path.parent.name
    d["mtime"] = path.stat().st_mtime if path.exists() else 0
    d["in_progress"] = [s for s in d["stages"] if s["status"].startswith("in progress")]
    d["open"] = [s for s in d["stages"] if not s["status"].startswith(("done", "accepted"))]
    return d


def for_session(cwd: str | Path, sid: str) -> dict[str, Any] | None:
    """The tracker this session works on: one naming its session id (or a
    prefix of it, at least 8 chars), else the most recently changed one with
    a stage in progress."""
    infos = [info(t) for t in trackers(cwd)]
    for d in infos:
        s = d["session"]
        if sid and len(s) >= 8 and sid.startswith(s):
            return d
    active = [d for d in infos if d["in_progress"]]
    return max(active, key=lambda d: d["mtime"]) if active else None


def by_slug(cwd: str | Path, slug: str) -> dict[str, Any] | None:
    path = work_dir(cwd) / slug / "TRACKER.md"
    if path.exists():
        return info(path)
    hits = [t for t in trackers(cwd) if slug.lower() in t.parent.name.lower()]
    return info(hits[0]) if len(hits) == 1 else None
