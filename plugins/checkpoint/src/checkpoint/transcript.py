"""Reading the session transcript (JSONL) that Claude Code passes to hooks.

Two uses:
- Queued commands. When the user sends a message while the session is busy,
  Claude Code appends {"type":"queue-operation","operation":"enqueue",
  "content":"/check:pause",...} at once, then "dequeue" when the turn
  ends and it runs (or "remove" when plain text is injected mid-turn). This
  format is undocumented (DESIGN.md, Spike 1); tests pin it with a fixture.
- Checkpoints: the last few tool calls.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import sanitize

TAIL_BYTES = 262144  # keep in sync with hooks/fast.sh

COMMAND_RE = re.compile(r"^/check:(pause|resume|status|help|doctor|save|list|diff|cancel|stop|complete|export|forget)"
                        r"(?:\s+(.*))?$", re.S)
# `pause interrupt` is `pause now` (the in-flight command is stopped at once).
KEYWORD_RE = re.compile(r"^pause(?:\s+(now|interrupt|all|all\s+now|now\s+all|all\s+interrupt|interrupt\s+all))?[.!]?$",
                        re.I)


def tail_lines(path: str | Path, nbytes: int = TAIL_BYTES) -> list[str]:
    """The last `nbytes` of the transcript as lines, the cut first line
    dropped. Split on "\n" only, exactly like the shell pre-filter's
    `tail -c | sed 1d` (str.splitlines would also split on U+2028 and
    friends, and the two would disagree on what a line is)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            data = f.read()
    except OSError:
        return []
    raw = data.split(b"\n")
    if raw and raw[-1] == b"":
        raw.pop()
    if size > nbytes and raw:
        raw = raw[1:]  # first line is cut
    return [line.decode("utf8", "replace") for line in raw]


def parse_command(text: str) -> tuple[str, list[str]] | None:
    """('pause', ['now']) for '/check:pause now' or the plain keyword
    'pause now'; None for anything else."""
    text = (text or "").strip()
    m = COMMAND_RE.match(text)
    if m:
        args = (m.group(2) or "").split()
        if m.group(1) == "pause":   # `interrupt` is `now`
            args = ["now" if a.lower() == "interrupt" else a for a in args]
        return m.group(1), args
    m = KEYWORD_RE.match(text)
    if m:
        return "pause", ["now" if a == "interrupt" else a for a in (m.group(1) or "").lower().split()]
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


def replay_queue(lines: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Replay the queue: enqueue appends, dequeue pops the front, remove drops
    by content. Returns (still pending, removed) ops. Pending ones wait for
    the turn to end; removed ones were absorbed mid-turn as plain text and
    never run as a prompt of their own."""
    pending: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for op in queue_ops(lines):
        kind = op.get("operation")
        content = (op.get("content") or "").strip()
        if kind == "enqueue":
            pending.append(op)
        elif kind == "dequeue" and pending:
            pending.pop(0)
        elif kind == "remove":
            removed.extend(q for q in pending if (q.get("content") or "").strip() == content)
            pending = [q for q in pending if (q.get("content") or "").strip() != content]
    return pending, removed


def queue_lines(path: str | Path) -> list[str]:
    """Every queue-operation line of the whole transcript (not just the
    tail): for the rare check of what became of one queued command after a
    long turn. Reads the file once, keeping only those lines."""
    out = []
    try:
        with open(path, "rb") as f:
            for raw in f:
                if b'"queue-operation"' in raw:
                    out.append(raw.rstrip(b"\n").decode("utf8", "replace"))
    except OSError:
        return []
    return out


def queue_fates(lines: list[str], stamps: list[str]) -> dict[str, str]:
    """What became of the commands enqueued at these timestamps: 'pending'
    (still queued), 'dequeued' (run as a prompt when a turn ended),
    'removed' (absorbed into a turn as plain text). One not in these lines
    is left out."""
    want = {ts for ts in stamps if ts}
    fates: dict[str, str] = {}
    pending: list[dict[str, Any]] = []
    for op in queue_ops(lines):
        kind = op.get("operation")
        content = (op.get("content") or "").strip()
        if kind == "enqueue":
            pending.append(op)
            if op.get("timestamp") in want:
                fates[op["timestamp"]] = "pending"
        elif kind == "dequeue" and pending:
            ts = pending.pop(0).get("timestamp")
            if ts in want:
                fates[ts] = "dequeued"
        elif kind == "remove":
            for q in pending:
                if q.get("timestamp") in want and (q.get("content") or "").strip() == content:
                    fates[q["timestamp"]] = "removed"
            pending = [q for q in pending if (q.get("content") or "").strip() != content]
    return fates


def bash_commands(lines: list[str]) -> set[str]:
    """Commands of the Bash tool calls in these transcript lines (foreground
    and background)."""
    out: set[str] = set()
    for line in lines:
        if '"tool_use"' not in line or '"Bash"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        for part in (rec.get("message") or {}).get("content") or []:
            if isinstance(part, dict) and part.get("type") == "tool_use" and part.get("name") == "Bash":
                cmd = (part.get("input") or {}).get("command")
                if isinstance(cmd, str) and cmd.strip():
                    out.add(cmd.strip())
    return out


def pending_queue(lines: list[str]) -> list[dict[str, Any]]:
    return replay_queue(lines)[0]


def queued_pauses(lines: list[str]) -> list[tuple[dict[str, Any], list[str]]]:
    """Every pending pause command or keyword, with its args, oldest first
    (several can be typed before the next tool call)."""
    out = []
    for op in pending_queue(lines):
        cmd = parse_command(op.get("content") or "")
        if cmd and cmd[0] == "pause":
            out.append((op, cmd[1]))
    return out


def last_pause_line(lines: list[str]) -> str:
    """The raw text of the last queue-operation line mentioning a pause; the
    shell pre-filter compares this with seen/<sid> to skip Python."""
    last = ""
    for line in lines:
        if '"queue-operation"' in line and re.search(r'check:pause|"content":"\s*[Pp]ause', line):
            last = line
    return last


def tool_results(lines: list[str]) -> set[str]:
    """tool_use ids that already have a tool_result (finished, failed,
    cancelled or denied)."""
    out: set[str] = set()
    for line in lines:
        if '"tool_result"' in line:
            out.update(re.findall(r'"tool_use_id":\s*"([^"]+)"', line))
    return out


def recent_tool_calls(lines: list[str], n: int = 10) -> list[dict[str, Any]]:
    """The last n tool calls, each its tool and a brief of its input as the
    journal keeps it (sanitize.tool_input: scrubbed, never file contents or
    an unknown tool's raw input): a resume puts the checkpoint into Claude's
    context."""
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
                tool = str(part.get("name") or "")
                inp = sanitize.tool_input(tool, part.get("input") or {})
                brief = next((inp[k] for k in ("command", "file_path", "notebook_path", "path", "pattern", "url",
                                               "query", "description", "subject", "skill", "summary")
                              if isinstance(inp.get(k), str) and inp[k]), "")
                calls.append({"tool": sanitize.clip(tool, 120), "input": sanitize.clip(brief, 200),
                              "at": rec.get("timestamp")})
    return calls[-n:]
