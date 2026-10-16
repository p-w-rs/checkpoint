"""Minimal MCP stdio server for agent-pause jobs, pure standard library.

Implements just enough of the Model Context Protocol (initialize, tools/list,
tools/call, ping) to expose the job tools. Same shape as net-gateway's.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from . import __version__, jobs

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS: list[dict[str, Any]] = [
    {
        "name": "job_start",
        "description": (
            "Start long-running work (anything likely to take more than ~2 minutes: "
            "builds, training, test suites, downloads, servers) as a DETACHED job that "
            "survives pauses, a closed laptop lid, and the end of this session. Output "
            "goes to a log (job_tail). Use INSTEAD OF Bash with run_in_background, which "
            "dies with the session. Give exactly one of `command` (a shell string, run "
            "with /bin/sh) or `argv`. Add a row to the tracker's Jobs table, and say in "
            "redo_hint how to redo or resume it if it dies (e.g. 'rerun; resumes from "
            "checkpoints/' or 'rerun from scratch')."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "short name, e.g. 'train' or 'tests'"},
                "command": {"type": "string", "description": "shell command line"},
                "argv": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string", "description": "working directory (default: the session's)"},
                "redo_hint": {"type": "string"},
                "task": {"type": "string", "description": "tracker task slug this job belongs to"},
            },
            "required": ["name", "redo_hint"],
        },
    },
    {
        "name": "job_status",
        "description": ("State of one job (running / exited N / killed / vanished, runtime, "
                        "exit code), or of all this session's jobs when name is omitted "
                        "(all_sessions=true for every job on the machine)."),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "job name or id"},
                           "all_sessions": {"type": "boolean", "default": False}},
        },
    },
    {
        "name": "job_tail",
        "description": "The last lines of a job's output log.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"},
                           "lines": {"type": "integer", "default": 40}},
            "required": ["name"],
        },
    },
    {
        "name": "job_stop",
        "description": ("Stop a job: SIGTERM its process group, SIGKILL after a grace "
                        "period. Records the reason and returns the last log lines."),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"},
                           "reason": {"type": "string"}},
            "required": ["name", "reason"],
        },
    },
]


def session_id() -> str:
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID") or ""


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "job_start":
        return jobs.start(args["name"], command=args.get("command"), argv=args.get("argv"),
                          cwd=args.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(),
                          redo_hint=args.get("redo_hint", ""), task=args.get("task", ""),
                          session_id=session_id())
    if name == "job_status":
        if args.get("name"):
            return jobs.status(args["name"])
        return jobs.status(session_id=None if args.get("all_sessions") else session_id() or None)
    if name == "job_tail":
        return jobs.tail(args["name"], args.get("lines", 40))
    if name == "job_stop":
        return jobs.stop(args["name"], args.get("reason", ""))
    return {"ok": False, "reason": f"unknown tool {name}"}


class Server:
    def __init__(self, stdin=None, stdout=None):
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout

    def send(self, obj: dict[str, Any]) -> None:
        self.stdout.write(json.dumps(obj) + "\n")
        self.stdout.flush()

    def result(self, rid: Any, result: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": rid, "result": result})

    def handle(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            self.result(rid, {
                "protocolVersion": asked if asked in SUPPORTED_VERSIONS else PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agent-pause", "version": __version__},
            })
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            self.result(rid, {"tools": TOOLS})
        elif method == "tools/call":
            try:
                out = dispatch(params.get("name", ""), params.get("arguments") or {})
            except Exception as e:  # noqa: BLE001
                out = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
            self.result(rid, {"content": [{"type": "text", "text": json.dumps(out, indent=2)}],
                              "isError": not out.get("ok", False)})
        elif method == "ping":
            self.result(rid, {})
        elif rid is not None and method is not None:
            self.send({"jsonrpc": "2.0", "id": rid,
                       "error": {"code": -32601, "message": f"method not found: {method}"}})

    def run(self) -> None:
        for line in self.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.handle(msg)


def main() -> None:
    Server().run()
