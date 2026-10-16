"""Detached jobs: long work that survives pauses, closed lids and sessions.

Each job runs in its own session/process group under a tiny sh wrapper that
writes the exit code to a file. Claude's own run_in_background Bash dies with
the Claude process (DESIGN.md, Spike 4); these don't.

Registry: <home>/jobs.json (under the state lock); per job <home>/jobs/<id>/
holding output.log and exit_code.
"""
from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from . import procs, state


def _registry() -> dict[str, Any]:
    reg = state.read_json(state.p("jobs.json"), {}) or {}
    reg.setdefault("jobs", {})
    return reg


def _save(reg: dict[str, Any]) -> None:
    state.write_json(state.p("jobs.json"), reg)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")[:40] or "job"


def start(name: str, command: str | None = None, argv: list[str] | None = None,
          cwd: str | None = None, redo_hint: str = "", task: str = "",
          session_id: str = "") -> dict[str, Any]:
    if bool(command) == bool(argv):
        return {"ok": False, "reason": "give exactly one of command (a shell string) or argv (a list)"}
    cwd = cwd or os.getcwd()
    if not Path(cwd).is_dir():
        return {"ok": False, "reason": f"cwd does not exist: {cwd}"}
    cmdline = command if command else shlex.join(argv or [])
    with state.lock():
        reg = _registry()
        jid = f"{_slug(name)}-{time.strftime('%Y%m%d-%H%M%S')}"
        n = 2
        while jid in reg["jobs"]:
            jid = f"{_slug(name)}-{time.strftime('%Y%m%d-%H%M%S')}-{n}"
            n += 1
        jdir = state.p("jobs", jid)
        jdir.mkdir(parents=True, exist_ok=True)
        log = jdir / "output.log"
        exitf = jdir / "exit_code"
        # The command runs in a nested sh so its own `exit` can't skip the
        # exit-code write. The job id is in the wrapper's argv, so status can
        # tell our process from an unrelated one that reused the pid.
        wrapper = '/bin/sh -c "$1"; rc=$?; echo $rc > "$2"; exit $rc'
        with open(log, "ab") as out:
            proc = subprocess.Popen(["/bin/sh", "-c", wrapper, f"agent-pause-job-{jid}",
                                     cmdline, str(exitf)],
                                    cwd=cwd, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        rec = {"id": jid, "name": name, "command": cmdline, "cwd": cwd,
               "pid": proc.pid, "pgid": proc.pid, "log": str(log), "exit_file": str(exitf),
               "started": state.now_iso(), "started_ts": time.time(),
               "session_id": session_id, "task": task, "redo_hint": redo_hint,
               "state": "running"}
        reg["jobs"][jid] = rec
        _save(reg)
    return {"ok": True, "job": _public(rec)}


def _refresh(rec: dict[str, Any], ptable: dict[int, dict[str, Any]]) -> dict[str, Any]:
    if rec.get("state") in ("exited", "killed", "vanished"):
        return rec
    exitf = Path(rec["exit_file"])
    if exitf.exists():
        try:
            rec["exit_code"] = int(exitf.read_text().strip())
        except ValueError:
            rec["exit_code"] = None
        rec["state"] = "exited"
        rec.setdefault("ended", time.strftime("%Y-%m-%dT%H:%M:%S%z",
                                              time.localtime(exitf.stat().st_mtime)))
        return rec
    p = ptable.get(rec["pid"])
    if p and rec["id"] in p["cmd"]:
        rec["state"] = "running"
        return rec
    rec["state"] = "killed" if rec.get("stop_reason") else "vanished"
    rec.setdefault("ended", state.now_iso())
    return rec


def _public(rec: dict[str, Any]) -> dict[str, Any]:
    out = {k: rec.get(k) for k in ("id", "name", "state", "exit_code", "command", "cwd",
                                   "started", "ended", "task", "redo_hint", "stop_reason",
                                   "log")}
    if rec.get("started_ts"):
        end = rec.get("ended_ts") or time.time()
        out["runtime_s"] = round(end - rec["started_ts"], 1)
    return {k: v for k, v in out.items() if v not in (None, "")}


def refresh_all() -> dict[str, dict[str, Any]]:
    ptable = procs.table()
    with state.lock():
        reg = _registry()
        for rec in reg["jobs"].values():
            before = rec.get("state")
            _refresh(rec, ptable)
            if before == "running" and rec["state"] != "running":
                rec["ended_ts"] = time.time()
        _save(reg)
    return reg["jobs"]


def find(name: str) -> dict[str, Any] | None:
    jobs = refresh_all()
    if name in jobs:
        return jobs[name]
    hits = [j for j in jobs.values() if j["name"] == name]
    return max(hits, key=lambda j: j.get("started_ts", 0)) if hits else None


def status(name: str | None = None, session_id: str | None = None) -> dict[str, Any]:
    if name:
        rec = find(name)
        return {"ok": True, "job": _public(rec)} if rec else {"ok": False, "reason": f"no job named {name}"}
    jobs = refresh_all().values()
    if session_id:
        jobs = [j for j in jobs if j.get("session_id") == session_id]
    return {"ok": True, "jobs": [_public(j) for j in sorted(jobs, key=lambda j: j.get("started_ts", 0))]}


def tail(name: str, lines: int = 40) -> dict[str, Any]:
    rec = find(name)
    if not rec:
        return {"ok": False, "reason": f"no job named {name}"}
    lines = max(1, min(int(lines or 40), 500))
    try:
        with open(rec["log"], "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200_000))
            text = f.read().decode("utf8", "replace")
    except OSError:
        text = ""
    return {"ok": True, "job": _public(rec), "tail": "\n".join(text.splitlines()[-lines:])}


def stop(name: str, reason: str = "", grace: float = 5.0) -> dict[str, Any]:
    rec = find(name)
    if not rec:
        return {"ok": False, "reason": f"no job named {name}"}
    if rec["state"] != "running":
        return {"ok": True, "job": _public(rec), "note": "not running"}
    try:
        os.killpg(rec["pgid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.time() + grace
    while time.time() < deadline and procs.alive(rec["pid"]) and not Path(rec["exit_file"]).exists():
        time.sleep(0.1)
    if procs.alive(rec["pid"]):
        try:
            os.killpg(rec["pgid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
    with state.lock():
        reg = _registry()
        r = reg["jobs"][rec["id"]]
        r["stop_reason"] = reason or "stopped"
        r["state"] = "killed"
        r["ended"] = state.now_iso()
        r["ended_ts"] = time.time()
        _save(reg)
    out = tail(rec["id"], 10)
    out["stopped"] = True
    return out
