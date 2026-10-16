"""Reading the session transcript (JSONL) that Claude Code passes to hooks.

Two uses:
- Queued commands. When the user sends a message while the session is busy,
  Claude Code appends {"type":"queue-operation","operation":"enqueue",
  "content":"/agent-pause:pause",...} at once, then "dequeue" when the turn
  ends and it runs (or "remove" when plain text is injected mid-turn). This
  format is undocumented (DESIGN.md, Spike 1); tests pin it with a fixture.
- Checkpoints: the last few tool calls.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

TAIL_BYTES = 262144  # keep in sync with hooks/fast.sh

COMMAND_RE = re.compile(r"^/agent-pause:(pause|resume|status|help)(?:\s+(.*))?$", re.S)
KEYWORD_RE = re.compile(r"^pause(?:\s+(now|all|all\s+now|now\s+all))?[.!]?$", re.I)


def tail_lines(path: str | Path, nbytes: int = TAIL_BYTES) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf8", "replace").splitlines()
    if size > nbytes and lines:
        lines = lines[1:]  # first line is cut
    return lines


def parse_command(text: str) -> tuple[str, list[str]] | None:
    """('pause', ['now']) for '/agent-pause:pause now' or the plain keyword
    'pause now'; None for anything else."""
    text = (text or "").strip()
    m = COMMAND_RE.match(text)
    if m:
        return m.group(1), (m.group(2) or "").split()
    m = KEYWORD_RE.match(text)
    if m:
        return "pause", (m.group(1) or "").lower().split()
    return None


def queue_ops(lines: list[str]) -> list[dict[str, Any]]:
    ops = []
    for line in lines:
        if '"queue-operation"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") == "queue-operation":
            ops.append(rec)
    return ops


def pending_queue(lines: list[str]) -> list[dict[str, Any]]:
    """Replay the queue: enqueue appends, dequeue pops the front, remove drops
    by content. What's left is waiting for the turn to end."""
    pending: list[dict[str, Any]] = []
    for op in queue_ops(lines):
        kind = op.get("operation")
        content = (op.get("content") or "").strip()
        if kind == "enqueue":
            pending.append(op)
        elif kind == "dequeue" and pending:
            pending.pop(0)
        elif kind == "remove":
            pending = [q for q in pending if (q.get("content") or "").strip() != content]
    return pending


def queued_pause(lines: list[str]) -> tuple[dict[str, Any], list[str]] | None:
    """The newest pending pause command or keyword, with its args."""
    hit = None
    for op in pending_queue(lines):
        cmd = parse_command(op.get("content") or "")
        if cmd and cmd[0] == "pause":
            hit = (op, cmd[1])
    return hit


def last_pause_line(lines: list[str]) -> str:
    """The raw text of the last queue-operation line mentioning a pause; the
    shell pre-filter compares this with seen/<sid> to skip Python."""
    last = ""
    for line in lines:
        if '"queue-operation"' in line and re.search(r'agent-pause:pause|"content":"\s*[Pp]ause', line):
            last = line
    return last


def recent_tool_calls(lines: list[str], n: int = 10) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for line in lines:
        if '"tool_use"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") != "assistant":
            continue
        for part in (rec.get("message") or {}).get("content") or []:
            if isinstance(part, dict) and part.get("type") == "tool_use":
                inp = part.get("input") or {}
                brief = (inp.get("command") or inp.get("file_path") or inp.get("pattern")
                         or inp.get("description") or json.dumps(inp)[:120])
                calls.append({"tool": part.get("name"), "input": str(brief)[:200],
                              "at": rec.get("timestamp")})
    return calls[-n:]
