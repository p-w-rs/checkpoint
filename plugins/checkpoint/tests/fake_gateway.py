"""A fake gateway daemon for checkpoint's tests: CONTRACT-v1 (gateway.v1) over
a Unix socket, in a thread of the test process. Stdlib only.

It serves what checkpoint's client calls (docs/CONTRACT-v1.md "Calls"):
capabilities, snapshot, events (long poll, epochs, reset), jobs (start with
idempotency keys, status, tail, signal; a start's `env` is kept as the job's
`env` while it lists the feature job_env) and pause/resume with per-job
on_pause policies, and writes the discovery file the way the gateway does
(0600, indent=1, versions, pid, socket, epoch, features). Jobs are records
whose state the test moves (`set_job`); nothing runs. Knobs for the failure
modes:
`hang` (the next N calls of a route answer after the client gave up),
`refuse` (an error answer: status, code, extra fields such as a policy
refusal's `rule`), `restart()` (a new epoch: jobs it "lost" are
gone), `inject()` (raw events: duplicates, out of order, extra fields),
`stop()` (the daemon gone; the contract file left behind or removed),
`ignore_term` (only KILL ends a job), `unkillable` (no signal ends it).
`sigints` counts the SIGINTs each job got from pauses (a retry must never
send a second); a pause id it has applied is answered with that pause's
outcomes and applied nowhere again (CONTRACT-v1 `pause_id`). Its defaults
are the current gateway's: every feature, accounts listed.

The socket lives in a short folder under /tmp (AF_UNIX paths are limited to
about 104 bytes, and the suite's temp root may be long); `close()` removes
it.
"""
from __future__ import annotations

import json
import os
import random
import re
import socketserver
import string
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
LIVE = ("starting", "running", "waiting_input", "waiting_auth", "suspended", "disconnected")


def ulid(prefix: str) -> str:
    return prefix + "_" + "".join(random.choice(CROCKFORD) for _ in range(26))


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeGateway:
    def __init__(self, contract_path: Path, *, versions: list[Any] | None = None, contract: str = "gateway.v1",
                 features: list[str] | None = None, epoch: int = 1, machine_id: str = "",
                 version: str = "0.10.0", hosts: list[dict[str, Any]] | None = None):
        self.contract_path = Path(contract_path)
        self.dir = Path(tempfile.mkdtemp(prefix="ckgw-", dir="/tmp"))
        os.chmod(self.dir, 0o700)
        self.sock = self.dir / "gated.sock"
        self.versions = [1] if versions is None else versions
        self.contract = contract
        self.features = features if features is not None else [
            "hosts", "events", "pause", "jobs", "terminals", "remote", "workspaces", "accounts", "pause_id",
            "job_env"]
        self.epoch = epoch
        self.machine_id = machine_id or ulid("m")
        self.version = version
        self.hosts = hosts if hosts is not None else [
            {"host": self.machine_id, "name": "localhost", "local": True, "address": "", "identity": "machine"}]
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.events: list[dict[str, Any]] = []
        self.seq = 0
        self.jobs: dict[str, dict[str, Any]] = {}
        self.terminals: dict[str, dict[str, Any]] = {}
        self.idem: dict[str, str] = {}
        self.paused: set[str] = set()
        self.requests: list[dict[str, Any]] = []
        self.hang: dict[str, int] = {}       # route -> how many calls to hang
        self.hang_seconds = 6.0
        # route -> (status, code) or (status, code, extra fields: a policy refusal's rule, a message)
        self.refuse: dict[str, tuple] = {}
        self.ignore_term: set[str] = set()   # jobs that only a KILL ends
        self.unkillable: set[str] = set()    # jobs no signal ends (the gateway keeps reporting them running)
        self.raw_answer: dict[str, Any] = {}  # route -> an answer sent as is (protocol errors)
        self.sigints: dict[str, int] = {}     # job -> SIGINTs sent by pauses
        self.pause_ids: dict[str, dict[str, Any]] = {}   # pause id -> its answer
        self.accounts: list[dict[str, Any]] | None = []   # snapshot accounts; None: not listed
        self.server: _Server | None = None
        self.pid = os.getpid()

    # -- lifecycle ------------------------------------------------------------------
    def start(self, write_contract: bool = True) -> "FakeGateway":
        handler = type("H", (_Handler,), {"gw": self})
        self.server = _Server(str(self.sock), handler)
        os.chmod(self.sock, 0o600)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        if write_contract:
            self.write_contract()
        self.emit("daemon.started", version=self.version, pid=self.pid)
        return self

    def write_contract(self, **override: Any) -> None:
        doc = {"contract": self.contract, "versions": self.versions, "gateway_version": self.version,
               "socket": str(self.sock), "pid": self.pid, "started": round(time.time(), 3), "epoch": self.epoch,
               "machine_id": self.machine_id, "features": self.features}
        doc.update(override)
        self.contract_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.contract_path.parent, 0o700)
        tmp = self.contract_path.with_name(".contract.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, indent=1)     # as the gateway writes it: one value per line
        os.replace(tmp, self.contract_path)

    def remove_contract(self) -> None:
        try:
            self.contract_path.unlink()
        except FileNotFoundError:
            pass

    def stop(self, remove_contract: bool = True) -> None:
        """The daemon goes away (its socket with it)."""
        if self.server is not None:
            srv, self.server = self.server, None
            with self.cond:
                self.cond.notify_all()      # long polls end
            srv.shutdown()
            srv.server_close()
        try:
            self.sock.unlink()
        except FileNotFoundError:
            pass
        if remove_contract:
            self.remove_contract()

    def restart(self, lose: list[str] | None = None) -> None:
        """A new daemon epoch: events start over at seq 0; the jobs in `lose`
        are gone (tmux lost them), the rest are re-adopted with their ids."""
        self.stop(remove_contract=False)
        with self.lock:
            self.epoch += 1
            self.seq = 0
            self.events = []
            for j in lose or []:
                self.jobs.pop(j, None)
        self.start()

    def close(self) -> None:
        self.stop(remove_contract=True)
        try:
            for p in self.dir.iterdir():
                p.unlink()
            self.dir.rmdir()
        except OSError:
            pass

    # -- state the test drives ------------------------------------------------------
    def emit(self, kind: str, **fields: Any) -> dict[str, Any]:
        with self.cond:
            self.seq += 1
            ev = {"seq": self.seq, "epoch": self.epoch, "ts": round(time.time(), 3), "kind": kind, **fields}
            self.events.append(ev)
            self.cond.notify_all()
            return ev

    def inject(self, *events: dict[str, Any]) -> None:
        """Raw events as given (duplicates, out-of-order seqs, extra fields)."""
        with self.cond:
            for e in events:
                self.events.append(dict(e))
                self.seq = max(self.seq, int(e.get("seq") or 0))
            self.cond.notify_all()

    def set_job(self, job: str, **fields: Any) -> None:
        with self.lock:
            j = self.jobs[job]
            old = j["state"]
            j.update(fields)
            if j["state"] not in LIVE and j.get("ended") is None:
                j["ended"] = round(time.time(), 3)
            if j["state"] != old:
                if j["state"] in LIVE:
                    self.emit("job.state", job=job, state=j["state"])
                else:
                    self.emit("job.exited", job=job, exit=j.get("exit"), signal=j.get("signal"), state=j["state"])

    def open_terminal(self, session: str, task_ref: str = "") -> str:
        t = ulid("t")
        with self.lock:
            self.terminals[t] = {"terminal": t, "host": self.machine_id, "host_name": "localhost", "state": "running",
                                 "owner": "agent", "session": session, "cwd": "/", "created": time.time(),
                                 "task_ref": task_ref}
        self.emit("terminal.opened", terminal=t, host=self.machine_id, session=session, agent=None, by="agent")
        return t

    def take_over(self, terminal: str, owner: str = "user") -> None:
        with self.lock:
            self.terminals[terminal]["owner"] = owner
        self.emit("terminal.owner", terminal=terminal, owner=owner, agent=None, by="ui" if owner == "user" else "ui")

    def calls(self, method: str = "", path_prefix: str = "") -> list[dict[str, Any]]:
        with self.lock:
            return [r for r in self.requests if (not method or r["method"] == method)
                    and r["path"].startswith(path_prefix)]

    def view(self, j: dict[str, Any]) -> dict[str, Any]:
        out = {k: j.get(k) for k in ("job", "state", "exit", "signal", "started", "ended", "host", "host_name", "cwd",
                                     "command", "owner", "on_pause", "session", "task_ref", "workspace")}
        if j.get("detached"):
            out["detached"] = True
        if j["state"] == "unknown":
            out["ended"] = None
        return out


class _Handler(BaseHTTPRequestHandler):
    gw: FakeGateway
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def address_string(self) -> str:
        return "local"

    def _send(self, status: int, body: Any, raw: bool = False) -> None:
        data = (body if isinstance(body, bytes) else json.dumps(body).encode()) if raw else json.dumps(
            {"contract": 1, **body}).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass
        self.close_connection = True

    def _route(self, method: str) -> None:
        g = self.gw
        url = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(url.query))
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        path = url.path
        m = re.fullmatch(r"/v1/jobs/(j_[0-9A-HJKMNP-TV-Z]{26})(/tail|/signal)?", path)
        route = f"{method} " + (f"/v1/jobs/{{id}}{m.group(2) or ''}" if m else path)
        with g.lock:
            g.requests.append({"method": method, "path": path, "query": q, "body": body, "route": route,
                               "ts": time.time()})
            hang = g.hang.get(route, 0)
            if hang:
                g.hang[route] = hang - 1
            refuse = g.refuse.get(route)
            raw_answer = g.raw_answer.get(route)
        if hang:
            # Applied (a start: the job exists), but answered after the client gave up.
            result = self._apply(route, m, q, body)
            time.sleep(g.hang_seconds)
            self._send(200, result)
            return
        if raw_answer is not None:
            self._send(200, raw_answer, raw=True)
            return
        if refuse:
            answer = {"ok": False, "error": refuse[1], "message": f"gateway: refused ({refuse[1]})"}
            answer.update(refuse[2] if len(refuse) > 2 else {})
            self._send(refuse[0], answer)
            return
        if route == "GET /v1/events":
            self._events(q)
            return
        result = self._apply(route, m, q, body)
        if result is None:
            self._send(404, {"ok": False, "error": "no_route", "message": "gateway: no such route"})
        elif isinstance(result, tuple):
            self._send(result[0], result[1])
        else:
            self._send(200, result)

    def _events(self, q: dict[str, str]) -> None:
        g = self.gw
        since = int(q.get("since") or 0)
        wait = min(25.0, float(q.get("wait") or 0))
        epoch = int(q["epoch"]) if "epoch" in q else None
        deadline = time.monotonic() + wait
        with g.cond:
            reset = (epoch is not None and epoch != g.epoch) or since > g.seq
            if reset:
                since = 0
            while g.seq <= since and time.monotonic() < deadline and g.server is not None:
                g.cond.wait(max(0.01, deadline - time.monotonic()))
            evs = [e for e in g.events if int(e.get("seq") or 0) > since]
            body = {"ok": True, "events": evs, "next": g.seq, "epoch": g.epoch, "truncated": False, "reset": reset}
        self._send(200, body)

    def _apply(self, route: str, m: Any, q: dict[str, str], body: dict[str, Any]) -> Any:
        g = self.gw
        with g.lock:
            if route == "GET /v1/capabilities":
                return {"ok": True, "contract_id": "gateway.v1", "gateway_version": g.version, "features": g.features,
                        "epoch": g.epoch, "limits": {"events_wait_max": 25.0}}
            if route == "GET /v1/snapshot":
                return {"ok": True, "seq": g.seq, "epoch": g.epoch, "hosts": g.hosts, "workspaces": [],
                        **({"accounts": g.accounts} if g.accounts is not None else {}),
                        "terminals": list(g.terminals.values()), "jobs": [g.view(j) for j in g.jobs.values()]}
            if route == "POST /v1/jobs":
                key = body.get("idempotency_key")
                if not key:
                    return 400, {"ok": False, "error": "bad_request", "message": "gateway: a job needs an idempotency_key"}
                if body.get("session") in g.paused:
                    return 409, {"ok": False, "error": "paused", "message": "gateway: the task is paused"}
                k = f"{body.get('session')}|{key}"
                if k in g.idem and g.idem[k] in g.jobs:
                    j = g.jobs[g.idem[k]]
                    return {"ok": True, "job": j["job"], "op": ulid("o"), "state": j["state"], "new": False}
                jid = ulid("j")
                cmd = body.get("command") if body.get("command") else " ".join(body.get("argv") or [])
                j = {"job": jid, "state": "running", "exit": None, "signal": None, "started": round(time.time(), 3),
                     "ended": None, "host": g.machine_id, "host_name": "localhost", "cwd": body.get("cwd") or "/",
                     "command": cmd, "owner": "agent", "on_pause": body.get("on_pause") or "continue",
                     "session": body.get("session") or "", "task_ref": body.get("task_ref") or "",
                     "workspace": None, "tail": "", "key": key}
                # the environment the job would run with (feature job_env); a daemon that doesn't
                # list the feature ignores the field
                if "job_env" in g.features and isinstance(body.get("env"), dict):
                    j["env"] = dict(body["env"])
                g.jobs[jid] = j
                g.idem[k] = jid
                g.emit("job.started", job=jid, host=g.machine_id, session=j["session"] or None,
                       task=j["task_ref"] or None, agent=None)
                return {"ok": True, "job": jid, "op": ulid("o"), "state": "running", "new": True}
            if route.startswith(("GET /v1/jobs/{id}", "POST /v1/jobs/{id}")):
                j = g.jobs.get(m.group(1)) if m else None
                if j is None:
                    return 404, {"ok": False, "error": "no_job", "message": "gateway: no such job"}
                if route == "GET /v1/jobs/{id}":
                    return {"ok": True, **g.view(j)}
                if route == "GET /v1/jobs/{id}/tail":
                    n = int(q.get("bytes") or 8192)
                    t = j.get("tail") or ""
                    return {"ok": True, "job": j["job"], "text": t[-n:], "truncated": len(t) > n, "offset": 0,
                            "state": j["state"]}
                if route == "POST /v1/jobs/{id}/signal":
                    sig = body.get("signal")
                    if sig not in ("INT", "TERM", "KILL", "STOP", "CONT"):
                        return 400, {"ok": False, "error": "bad_request", "message": "gateway: bad signal"}
                    if j["state"] in LIVE and j["job"] in g.unkillable:
                        pass
                    elif j["state"] in LIVE:
                        if sig == "STOP":
                            g.set_job(j["job"], state="suspended")
                        elif sig == "CONT":
                            g.set_job(j["job"], state="running")
                        elif sig == "KILL" or (sig in ("TERM", "INT") and j["job"] not in g.ignore_term):
                            g.set_job(j["job"], state="canceled" if sig in ("TERM", "KILL") else "exited",
                                      signal=sig, exit=None)
                    return {"ok": True, "job": j["job"], "state": j["state"], "exit": j.get("exit")}
            if route in ("POST /v1/pause", "POST /v1/resume"):
                sid = body.get("session")
                if not isinstance(sid, str) or not sid:
                    return 400, {"ok": False, "error": "bad_request", "message": "gateway: name a session"}
                task = body.get("task_ref") or None
                out = []
                if route == "POST /v1/pause":
                    pid = body.get("pause_id")
                    if isinstance(pid, str) and pid in g.pause_ids:
                        g.emit("pause.applied", session=sid, paused=True, task=body.get("task_ref") or None,
                               replayed=True)
                        return {**g.pause_ids[pid], "replayed": True}
                    g.paused.add(sid)
                    for j in list(g.jobs.values()):
                        if j["session"] != sid or j["state"] not in LIVE or j.get("detached"):
                            continue
                        if task and j["task_ref"] != task:
                            continue
                        pol = j["on_pause"]
                        if pol == "continue":
                            o = "still running (on_pause continue)"
                        elif pol == "detach":
                            j["detached"] = True
                            j["task_ref"] = ""
                            o = "detached from the task; still running"
                        elif pol == "suspend":
                            g.set_job(j["job"], state="suspended")
                            j["paused_as"] = "suspended"
                            o = "suspended (SIGSTOP)"
                        else:
                            g.sigints[j["job"]] = g.sigints.get(j["job"], 0) + 1
                            if j["job"] in g.ignore_term:
                                j["paused_as"] = "interrupted"
                                o = "SIGINT sent; still running, SIGTERM follows in 10 s if it doesn't exit"
                            else:
                                g.set_job(j["job"], state="exited", signal="INT", exit=None)
                                o = "interrupted: exited (signal INT)"
                        out.append({"job": j["job"], "policy": pol, "outcome": o})
                    g.emit("pause.applied", session=sid, paused=True, task=task)
                    answer = {"ok": True, "paused": True, "outcomes": out, "dialogs_closed": 0}
                    if isinstance(pid, str) and pid:
                        g.pause_ids[pid] = answer
                    return answer
                was = sid in g.paused
                g.paused.discard(sid)
                for j in list(g.jobs.values()):
                    if j["session"] != sid or (task and j["task_ref"] != task and not j.get("detached")):
                        continue
                    if j["state"] not in LIVE:
                        out.append({"job": j["job"], "policy": j["on_pause"], "outcome": "ended (exit unknown)"})
                    elif j.get("paused_as") == "suspended":
                        j["paused_as"] = ""
                        g.set_job(j["job"], state="running")
                        out.append({"job": j["job"], "policy": j["on_pause"], "outcome": "resumed (SIGCONT)"})
                    else:
                        out.append({"job": j["job"], "policy": j["on_pause"], "outcome": "running"})
                g.emit("pause.applied", session=sid, paused=False, task=task)
                return {"ok": True, "paused": False, "was_paused": was, "outcomes": out}
        return None

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")


def token(n: int = 24) -> str:
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))
