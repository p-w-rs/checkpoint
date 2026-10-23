"""Minimal MCP stdio server for checkpoint jobs, pure standard library.

Implements just enough of the Model Context Protocol (initialize, tools/list,
tools/call, ping) to expose the job tools. Same shape as gateway's.

With the gateway available (CONTRACT-v1), job_start delegates to its job
routes (jobs.start) and a thread follows the gateway's events for the
session (gateway.sync with a long poll), so the journal records its jobs'
and terminals' events as they happen; without it the thread only looks for
the discovery file now and then.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from typing import Any

from . import __version__, capture, core, gateway, jobs, journal, sanitize, state

SERVER_STARTED = time.time()

NOTE_KINDS = ("constraint", "decision", "question", "criterion", "note")

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
            "dies with the session. Give exactly one of `command` (a shell string) or "
            "`argv`. With the gateway plugin running, the job is started and supervised by "
            "the gateway (its managed bash, its job records; job_status says `supervisor`), "
            "and `on_pause` says what a pause of this task does to it; a start the gateway "
            "refuses (its policy, a pause) runs nowhere, and the answer gives the gateway's "
            "reason. Without the gateway, "
            "checkpoint's own detached runner runs it (/bin/sh; a pause leaves it running). "
            "Add a row to the tracker's Jobs table, and say in "
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
                "on_cancel": {"type": "string", "enum": ["interrupt", "detach"],
                              "description": ("what /check:cancel does with this job if the user cancels the task: "
                                              "interrupt (default: stop it) or detach (leave it running, detached "
                                              "from the task)")},
                "on_pause": {"type": "string", "enum": list(gateway.ON_PAUSE),
                             "description": ("what pausing the task does to this job (gateway jobs): continue "
                                             "(keeps running; the default unless the task's policy says otherwise), "
                                             "interrupt (SIGINT, then SIGTERM after 10 s), suspend (SIGSTOP until "
                                             "resumed), detach (keeps running, detached from the task)")},
            },
            "required": ["name", "redo_hint"],
        },
    },
    {
        "name": "job_status",
        "description": ("State of one job (running / exited N / killed / vanished / "
                        "stop_failed, runtime, exit code, whether the gateway's environment "
                        "was applied), or of all this session's jobs when "
                        "name is omitted (all_sessions=true for every job on the machine)."),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "job name or id"},
                           "all_sessions": {"type": "boolean", "default": False}},
        },
    },
    {
        "name": "job_tail",
        "description": ("The last lines of a job's output log (a log keeps its last 32-64 MB; "
                        "`log_truncated` says when earlier output was dropped)."),
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
                        "period. Records the reason and returns the last log lines. Fails "
                        "with stop_failed if processes of the job are still running."),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"},
                           "reason": {"type": "string"}},
            "required": ["name", "reason"],
        },
    },
    {
        "name": "task_policy",
        "description": (
            "Set the current task's default child-work policies (recorded in checkpoint's journal): on_pause "
            "for the jobs it starts from now on through the gateway (continue, interrupt, suspend, detach) and "
            "on_cancel (interrupt or detach) for what /check:cancel does with its jobs. A job_start that names "
            "its own on_pause/on_cancel overrides it. Use it when the user says how pausing or canceling should "
            "treat the task's running work. Without arguments: shows the current policy."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "on_pause": {"type": "string", "enum": list(gateway.ON_PAUSE)},
                "on_cancel": {"type": "string", "enum": ["interrupt", "detach"]},
            },
        },
    },
    {
        "name": "checkpoint_note",
        "description": (
            "Record a constraint, decision, open question or completion criterion of the current task "
            "in checkpoint's journal, so it survives crashes, compaction and new sessions: it shows in "
            "the task's recovery summary under 'Constraints & decisions'. Use it when the user states a "
            "constraint (e.g. 'never touch prod'), when you settle a decision worth keeping, when a "
            "question stays open, or to say what 'finished' means. One short, specific fact per call; "
            "never put secrets in it (it is scrubbed anyway). Allowed while paused."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(NOTE_KINDS)},
                "text": {"type": "string", "description": "the constraint, decision, question or criterion"},
            },
            "required": ["kind", "text"],
        },
    },
]
# Offered to the model directly, not deferred behind ToolSearch: Claude Code
# 2.1.304 defers plugin MCP tools unless .mcp.json sets "alwaysLoad" for the
# server (probed on the Debian VM: with it 19 tools in the request, the six
# job tools among them; without it 13, the job tools only named as deferred;
# 2.1.301 offered them directly either way). Each tool says so too, as the
# gateway's gw server does (the server's flag alone sufficed on 2.1.304).
# About 4.5 KB of schemas per request.
ALWAYS = {"anthropic/alwaysLoad": True}
for _t in TOOLS:
    _t["_meta"] = dict(ALWAYS)


def session_id() -> str:
    """The session this call belongs to, worked out per call: the server
    starts with the session id of its launch in CLAUDE_CODE_SESSION_ID, but
    /clear starts a new session in the same Claude process. SessionStart
    records every session with its Claude pid, so the newest record for our
    CLAUDE_PID is the current one. Records older than this server (a dead
    process whose pid was reused) only count if they are the launch session."""
    env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID") or ""
    try:
        cpid = int(os.environ.get("CLAUDE_PID") or 0)
    except ValueError:
        cpid = 0
    if cpid:
        recs = [s for s in state.all_sessions() if s.get("claude_pid") == cpid
                and (s["session_id"] == env_sid or state.started_ts(s) >= SERVER_STARTED - 300)]
        if recs:
            return max(recs, key=state.started_ts)["session_id"]
    return env_sid


def note(args: dict[str, Any]) -> dict[str, Any]:
    """checkpoint_note: a note record in the session's task journal, synced."""
    kind = str(args.get("kind") or "note")
    if kind not in NOTE_KINDS:
        return {"ok": False, "reason": f"kind must be one of {', '.join(NOTE_KINDS)}"}
    text = sanitize.clip(sanitize.scrub(str(args.get("text") or "").strip()), 1000)
    if not text:
        return {"ok": False, "reason": "text is empty"}
    sid = session_id()
    if not sid:
        return {"ok": False, "reason": "no session to record it in"}
    r = capture.record(sid, [{"kind": "note", "session": sid, "data": {"kind": kind, "text": text}}], sync=True)
    if r.error:
        return {"ok": False, "reason": f"recording failed: {r.error} (/check:doctor)"}
    return {"ok": True, "task": r.task, "seq": r.last_seq, "kind": kind}


def policy_path(task: str):
    return journal.jdir(task) / "policy.json"


def task_policy(task: str) -> dict[str, str]:
    """The task's default child-work policies ({} when none was set)."""
    if not task:
        return {}
    p = state.read_json(policy_path(task), {}) or {}
    return {k: v for k, v in p.items() if (k == "on_pause" and v in gateway.ON_PAUSE)
            or (k == "on_cancel" and v in jobs.CANCEL_POLICIES)} if isinstance(p, dict) else {}


def set_policy(args: dict[str, Any]) -> dict[str, Any]:
    """task_policy: record the task's defaults (journal record task.policy,
    synced; policy.json beside the journal is what job_start reads)."""
    sid = session_id()
    if not sid:
        return {"ok": False, "reason": "no session to record it in"}
    capture.ingest(sid)
    task = state.load_session(sid).get("task_id") or ""
    if not task:
        return {"ok": False, "reason": "this session has no task journal yet"}
    cur = task_policy(task)
    new = dict(cur)
    for k, allowed in (("on_pause", gateway.ON_PAUSE), ("on_cancel", jobs.CANCEL_POLICIES)):
        v = args.get(k)
        if v in (None, ""):
            continue
        if v not in allowed:
            return {"ok": False, "reason": f"{k} is one of {', '.join(allowed)}"}
        new[k] = v
    if new == cur:
        return {"ok": True, "task": task, "policy": cur or {"on_pause": "continue", "on_cancel": "interrupt"},
                "note": "unchanged" if cur else "defaults (nothing set)"}
    r = capture.record(sid, [{"kind": "task.policy", "session": sid, "data": dict(new)}], sync=True)
    if r.error:
        return {"ok": False, "reason": f"recording failed: {r.error} (/check:doctor)"}
    state.write_json(policy_path(task), new)
    return {"ok": True, "task": task, "policy": new, "seq": r.last_seq}


def _job_record(sid: str, op: str, out: dict[str, Any], args: dict[str, Any], on_pause: str, on_cancel: str) -> None:
    """job.started in the journal: which supervisor owns it and its ids
    (the gateway's job id, the idempotency key), never its output."""
    j = out.get("job") or {}
    data: dict[str, Any] = {"job": j.get("id"), "name": sanitize.clip(str(args.get("name") or ""), 80),
                            "supervisor": out.get("supervisor") or j.get("supervisor") or "checkpoint",
                            "command": sanitize.clip(sanitize.scrub(str(args.get("command") or " ".join(
                                map(str, args.get("argv") or [])))), 300),
                            "on_cancel": on_cancel or "interrupt", "ok": bool(out.get("ok"))}
    if data["supervisor"] == "gateway":
        rec = jobs._registry()["jobs"].get(j.get("id") or "", {})
        data.update(gateway_job=j.get("gateway_job"), on_pause=on_pause or "continue",
                    idempotency_key=rec.get("idempotency_key") or out.get("idempotency_key"),
                    epoch=rec.get("seen_epoch"),
                    machine_id=rec.get("machine_id"), host=rec.get("host") or "localhost")
        if out.get("outcome"):
            data["outcome"] = out["outcome"]
        if out.get("outcome") == "refused":
            # refused by the gateway (its policy, the session's pause, ...): started nowhere (jobs._refused)
            data.update(refused_by="gateway", code=sanitize.clip(str(out.get("code") or ""), 60),
                        status=out.get("status") if isinstance(out.get("status"), int) else None)
            if out.get("rule"):
                data["rule"] = sanitize.clip(sanitize.scrub(str(out["rule"])), 200)
    elif j.get("why_own"):
        data["why_own"] = sanitize.clip(j["why_own"], 200)
    if not out.get("ok"):
        data["reason"] = sanitize.clip(sanitize.scrub(str(out.get("reason") or "")), 300)
    capture.record(sid, [{"kind": "job.started", "session": sid, "op": op or None, "data": data}], sync=True)


def dispatch(name: str, args: dict[str, Any], meta: dict[str, Any] | None = None) -> dict[str, Any]:
    if name == "checkpoint_note":
        return note(args)
    if name == "task_policy":
        return set_policy(args)
    if name == "job_start":
        sid = session_id()
        tid = ""
        if sid:
            try:  # the journal task this job belongs to (/check:cancel applies its policy to it)
                capture.ingest(sid)
                tid = state.load_session(sid).get("task_id") or ""
            except Exception:  # noqa: BLE001  the job starts either way
                tid = ""
        pol = task_policy(tid)
        on_pause = str(args.get("on_pause") or pol.get("on_pause") or "")
        on_cancel = str(args.get("on_cancel") or pol.get("on_cancel") or "")
        op = str((meta or {}).get("claudecode/toolUseId") or "")
        out = jobs.start(args["name"], command=args.get("command"), argv=args.get("argv"),
                         cwd=args.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(),
                         redo_hint=args.get("redo_hint", ""), task=args.get("task", ""),
                         session_id=sid, task_id=tid, on_cancel=on_cancel, on_pause=on_pause, op=op,
                         agent=str((meta or {}).get("claudecode/agentId") or ""))
        if sid and (out.get("ok") or out.get("supervisor") == "gateway"):
            try:
                _job_record(sid, op, out, args, on_pause, on_cancel)
            except Exception as e:  # noqa: BLE001  the job runs whatever the journal does
                out["journal"] = f"recording the start failed: {type(e).__name__}"
        return out
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
                "serverInfo": {"name": "checkpoint", "version": __version__},
            })
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            self.result(rid, {"tools": TOOLS})
        elif method == "tools/call":
            try:
                # A Python moment: what waits in the session's spool goes to the journal first.
                capture.ingest(session_id())
            except Exception as e:  # noqa: BLE001
                print(f"checkpoint: ingesting the spool failed: {e}", file=sys.stderr)
            try:
                meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
                out = dispatch(params.get("name", ""), params.get("arguments") or {}, meta)
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


def follow_gateway(stop: threading.Event) -> None:
    """The gateway's events into this session's journal as they happen (a
    long poll), and its pause state kept in step (gateway_settle). Without
    a gateway it only looks for the discovery file every few seconds."""
    try:
        wait = max(0.0, min(gateway.EVENTS_WAIT_MAX, float(os.environ.get("CHECKPOINT_GATEWAY_POLL") or 20)))
    except ValueError:
        wait = 20.0
    idle = max(0.2, min(30.0, wait / 4 or 1.0))
    while not stop.is_set():
        try:
            sid = session_id()
            if not sid:
                stop.wait(idle)
                continue
            g = gateway.discover()
            r = gateway.sync(sid, wait=wait if g.ok else 0, g=g)
            core.gateway_settle(sid)
            if not r.ok:
                stop.wait(idle)
        except Exception:  # noqa: BLE001  the thread never dies over the gateway
            stop.wait(idle)


def main() -> None:
    signal.signal(signal.SIGCHLD, jobs.reap_started)
    stop = threading.Event()
    if os.environ.get("CHECKPOINT_GATEWAY_FOLLOW", "1") != "0":
        threading.Thread(target=follow_gateway, args=(stop,), daemon=True, name="gateway-events").start()
    try:
        Server().run()
    finally:
        stop.set()
