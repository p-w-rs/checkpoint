"""The gateway contract client (CONTRACT-v1, `gateway.v1`; PLAN-v4 phase 4).

Checkpoint is the consumer: the gateway (another plugin) owns hosts, accounts,
terminals, jobs and execution state; checkpoint owns task history, pause
intent and recovery. Everything here is optional: every call is short and
timed out, and a failure only means "the gateway isn't available now", said
with its reason, never guessed. Hooks on the common path (not paused, nothing
queued) never get here: fast.sh doesn't start Python for them.

Discovery (docs/CONTRACT-v1.md "Discovery"): `~/.config/gateway/run/
contract.json` of the password database's home (as the gateway finds it;
CHECKPOINT_GATEWAY_CONTRACT overrides it in tests, and set but empty turns
the integration off). A missing file, a dead `pid`, an unknown `contract`, a
`versions` list without 1, or `features` without one checkpoint needs
(NEEDS_FEATURES) is "not available", with the reason;
`hooks/env.sh doctor` makes the same checks in sh (tests compare the two).
The file must be a regular file of this user that nobody else can write, and
its socket a socket of this user: commands and session ids go there.

Calls: HTTP/1.1 over the Unix stream socket, JSON both ways, every answer
carrying `"contract": 1` (unknown fields ignored). A call that never
connected ("unreachable"), or that the gateway answered with an error
("refused"), certainly did nothing; one that timed out or broke after the
request was sent ("ambiguous") may have: jobs.start retries such a start once
with the same idempotency key and never starts the job elsewhere meanwhile.
A refusal is the gateway's decision: only one that says it doesn't serve
this caller at all (`not_served`: plane, not_registered, peer, uid, no_route)
lets work go elsewhere; any other (policy, session, paused, an unknown code)
is reported as refused by the gateway and never turned into a local action.

Events (`sync`): the session's cursor (`CHECKPOINT_HOME/gateway/<session>.json`:
epoch, next seq, the jobs, terminals and dialog requests it follows) reads
`/v1/events?since=&epoch=`; a new epoch (or `reset`) re-reads the snapshot.
What concerns the session's task becomes journal records (sanitized again:
ids, enums, counts and validated names only), keyed by `gw:<epoch>:<seq>` so a
duplicate delivery is dropped, in seq order. The MCP server follows events in
a thread (long poll); `run.py ingest` and the explicit operations sync too.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import os
import pwd
import re
import socket
import stat
import time
from dataclasses import dataclass, field
from typing import Any

from . import sanitize, state

CONTRACT_ID = "gateway.v1"
NEEDS = 1
CONNECT_TIMEOUT = 0.5      # seconds to reach the socket
CALL_TIMEOUT = 2.0         # seconds for an answer (a long poll adds its wait); CHECKPOINT_GATEWAY_TIMEOUT
EVENTS_WAIT_MAX = 25.0     # the contract's long-poll ceiling
JOB_RE = re.compile(r"j_[0-9A-HJKMNP-TV-Z]{26}")
SAFE = re.compile(r"[A-Za-z0-9_.:@/+-]{0,120}")     # the gateway's own events.SAFE
LIVE_STATES = ("starting", "running", "waiting_input", "waiting_auth", "suspended", "disconnected")
FINAL_STATES = ("exited", "failed", "canceled")
ON_PAUSE = ("continue", "interrupt", "suspend", "detach")
# Features checkpoint needs; a daemon that doesn't list one is a contract
# mismatch (checkpoint works alone). `pause_id`: the gateway takes the
# pause's id on /v1/pause and answers a pause id it has applied with that
# pause's outcomes, never signalling again, so an uncertain pause is simply
# sent again with the same id (core.gateway_pause).
NEEDS_FEATURES = ("pause_id",)
PAUSE_ID_RE = re.compile(r"ckp-[0-9a-f]{24}")
FOLLOW_MAX = 200           # jobs / terminals / requests a cursor remembers
# Event kinds and the fields a journal record keeps (the contract's v1 list;
# a kind not here is skipped, a field not here dropped).
EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "daemon.started": ("version", "pid"),
    "daemon.stopping": ("reason",),
    "host.state": ("host", "name", "state", "reachable", "auth", "runtime", "identity"),
    "auth.waiting": ("request", "session", "dialog", "terminal"),
    "auth.supplied": ("request", "outcome"),
    "pause.applied": ("session", "paused", "task", "replayed"),
    # the rights of an entry changed (widened after the person's OK,
    # narrowed, removed), and an account's state (connected, expired ...)
    "rights.changed": ("entry", "change"),
    "account.state": ("account", "state"),
    "job.started": ("job", "host", "session", "task", "agent"),
    "job.state": ("job", "state"),
    "job.exited": ("job", "exit", "signal", "state"),
    "job.output": ("job", "bytes"),
    "terminal.opened": ("terminal", "host", "session", "agent", "by"),
    "terminal.owner": ("terminal", "owner", "agent", "by"),
    "terminal.closed": ("terminal", "reason"),
}


def contract_path() -> str:
    """Where the gateway's daemon writes its discovery file; '' when the
    integration is turned off (CHECKPOINT_GATEWAY_CONTRACT set but empty)."""
    env = os.environ.get("CHECKPOINT_GATEWAY_CONTRACT")
    if env is not None:
        return os.path.abspath(env) if env else ""
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        home = os.path.expanduser("~")
    return os.path.join(home, ".config", "gateway", "run", "contract.json")


@dataclass
class Gateway:
    """What discovery found. `status`: available, off, absent, unreadable,
    unsafe, dead, unknown_contract, mismatch."""
    status: str
    reason: str
    path: str = ""
    doc: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "available"

    @property
    def socket(self) -> str:
        return str(self.doc.get("socket") or "")

    @property
    def pid(self) -> int:
        p = self.doc.get("pid")
        return p if isinstance(p, int) and not isinstance(p, bool) else 0

    @property
    def epoch(self) -> int | None:
        e = self.doc.get("epoch")
        return e if isinstance(e, int) and not isinstance(e, bool) else None

    @property
    def version(self) -> str:
        return sanitize.clip(sanitize.scrub(str(self.doc.get("gateway_version") or "?")), 40)

    @property
    def machine_id(self) -> str:
        m = str(self.doc.get("machine_id") or "")
        return m if SAFE.fullmatch(m) and sanitize.scrub(m) == m else ""

    @property
    def features(self) -> list[str]:
        f = self.doc.get("features")
        return [str(x) for x in f if isinstance(x, str)] if isinstance(f, list) else []

    def has(self, feature: str) -> bool:
        return self.ok and feature in self.features

    def line(self) -> str:
        """One line for /check:status and /check:doctor."""
        if self.ok:
            return (f"gateway: available (gateway {self.version}, contract v{NEEDS}, epoch {self.epoch}, "
                    f"pid {self.pid}; features: {', '.join(self.features) or 'none'})")
        return f"gateway: not available: {self.reason}"


def _versions(doc: dict[str, Any]) -> list[Any]:
    v = doc.get("versions")
    return v if isinstance(v, list) else []


def discover() -> Gateway:
    """Read and check the discovery file (no network; microseconds)."""
    path = contract_path()
    if not path:
        return Gateway("off", "turned off (CHECKPOINT_GATEWAY_CONTRACT is set but empty)")
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return Gateway("absent", f"no contract file ({path}): the gateway isn't running or isn't installed", path)
    except OSError as e:
        return Gateway("unreadable", f"its contract file can't be checked ({e.strerror or e})", path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return Gateway("unsafe", f"its contract file ({path}) isn't a regular file", path)
    if st.st_uid != os.getuid() or st.st_mode & 0o022:
        return Gateway("unsafe", f"its contract file ({path}) isn't yours alone (owner or mode): not used", path)
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.loads(f.read(65536))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        return Gateway("unreadable", f"its contract file can't be read ({type(e).__name__})", path)
    if not isinstance(doc, dict):
        return Gateway("unreadable", "its contract file isn't a JSON object", path)
    pid = doc.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return Gateway("dead", "its contract file names no daemon pid", path, doc)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return Gateway("dead", f"its contract file is stale: the daemon (pid {pid}) isn't running", path, doc)
    except PermissionError:
        return Gateway("dead", f"its contract file is stale: pid {pid} is another user's process", path, doc)
    except OSError as e:
        return Gateway("dead", f"its daemon (pid {pid}) can't be checked ({e.strerror or e})", path, doc)
    cid = doc.get("contract")
    if cid != CONTRACT_ID:
        shown = sanitize.clip(sanitize.scrub(str(cid)), 40) if cid is not None else "none"
        return Gateway("unknown_contract", f"unknown contract '{shown}' (needs {CONTRACT_ID})", path, doc)
    vs = _versions(doc)
    if NEEDS not in vs:
        has = ", ".join(sanitize.clip(sanitize.scrub(str(v)), 10) for v in vs[:8]) or "none"
        return Gateway("mismatch", f"gateway contract mismatch (has {has}, needs {NEEDS}); checkpoint works alone",
                       path, doc)
    feats = doc.get("features") if isinstance(doc.get("features"), list) else []
    missing = [f for f in NEEDS_FEATURES if f not in feats]
    if missing:
        return Gateway("mismatch", f"gateway contract mismatch (its features lack {', '.join(missing)}: update the "
                                   "gateway); checkpoint works alone", path, doc)
    sock = str(doc.get("socket") or "")
    try:
        sst = os.stat(sock) if os.path.isabs(sock) else None
    except OSError:
        sst = None
    if sst is None or not stat.S_ISSOCK(sst.st_mode):
        shown = sanitize.clip(sanitize.scrub(sock), 120) or "none"
        return Gateway("dead", f"its socket ({shown}) is missing", path, doc)
    if sst.st_uid != os.getuid():
        return Gateway("unsafe", "its socket isn't yours: not used", path, doc)
    return Gateway("available", "", path, doc)


# -- calls ------------------------------------------------------------------------------

class CallError(Exception):
    """kind: unreachable (never connected: nothing was done), refused (an
    error answer: nothing was done), ambiguous (sent, then no whole answer:
    it may have been done), protocol (an answer that isn't CONTRACT-v1).
    `rule`: a policy refusal's rule as the gateway names it."""

    def __init__(self, kind: str, message: str, status: int = 0, code: str = "", rule: str = ""):
        super().__init__(message)
        self.kind, self.message, self.status, self.code, self.rule = kind, message, status, code, rule

    def text(self) -> str:
        return self.message + (f" (HTTP {self.status}{', ' + self.code if self.code else ''})" if self.status else "")


# The gateway's refusals that mean it doesn't serve this caller at all, by
# (code, HTTP status), as its daemon sends them (gateway daemon/planes.py and
# server.py): `plane` (the contract plane: no Claude Code session the gateway
# knows above the caller), `not_registered` (its agent planes: the same),
# `peer` (it couldn't identify the caller, or the caller isn't one of its own
# programs), `uid` (another user's daemon) and `no_route` (404/405: a daemon
# without that route). Every other refusal (`policy`, `session`, `paused`,
# `too_many`, `bad_*`, a code checkpoint doesn't know, or none) is the gateway,
# serving this session, deciding no: never something to do some other way.
NOT_SERVED: dict[str, tuple[int, ...]] = {"plane": (403,), "not_registered": (403,), "peer": (403,),
                                          "uid": (403,), "no_route": (404, 405)}


def not_served(e: CallError) -> bool:
    """A refusal that says the gateway doesn't serve this caller (NOT_SERVED):
    the only refusal after which work may go elsewhere."""
    return e.kind == "refused" and e.status in NOT_SERVED.get(e.code, ())


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path
        self.connected = False

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.settimeout(CONNECT_TIMEOUT)
            s.connect(self._path)
            s.settimeout(self.timeout)
        except BaseException:
            s.close()
            raise
        self.sock = s
        self.connected = True


def call_timeout() -> float:
    """Seconds an ordinary call may take (CHECKPOINT_GATEWAY_TIMEOUT, tests
    make it short); a start, a pause and a long poll get multiples of it."""
    try:
        return max(0.1, min(30.0, float(os.environ.get("CHECKPOINT_GATEWAY_TIMEOUT") or CALL_TIMEOUT)))
    except ValueError:
        return CALL_TIMEOUT


def call(g: Gateway, method: str, path: str, body: dict[str, Any] | None = None,
         timeout: float | None = None) -> dict[str, Any]:
    """One contract call; the answer's JSON object, or CallError."""
    timeout = call_timeout() if timeout is None else timeout
    if not g.ok:
        raise CallError("unreachable", f"the gateway isn't available: {g.reason}")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    conn = _UnixConnection(g.socket, timeout)
    sent = False
    try:
        try:
            conn.connect()
        except OSError as e:
            raise CallError("unreachable", f"the gateway's socket didn't answer ({e.strerror or type(e).__name__})") from None
        headers = {"Content-Type": "application/json", "Content-Length": str(len(data or b""))}
        try:
            conn.request(method, path, body=data, headers=headers)
            sent = True
            resp = conn.getresponse()
            raw = resp.read(4 * 1024 * 1024 + 1)
        except (OSError, http.client.HTTPException) as e:
            kind = "ambiguous" if sent or method == "POST" else "unreachable"
            raise CallError(kind, f"the gateway didn't answer in time ({type(e).__name__})") from None
    finally:
        with contextlib.suppress(Exception):
            conn.close()
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise CallError("protocol", "the gateway's answer isn't JSON", resp.status) from None
    if not isinstance(obj, dict) or obj.get("contract") != NEEDS:
        raise CallError("protocol", "the gateway's answer isn't a CONTRACT-v1 answer", resp.status)
    if resp.status != 200:
        code = str(obj.get("error") or "")
        msg = sanitize.clip(sanitize.scrub(str(obj.get("message") or "the gateway refused the call")), 300)
        rule = sanitize.clip(sanitize.scrub(obj["rule"]), 200) if isinstance(obj.get("rule"), str) else ""
        raise CallError("refused", msg, resp.status, code if SAFE.fullmatch(code) else "", rule)
    return obj


def capabilities(g: Gateway) -> dict[str, Any]:
    return call(g, "GET", "/v1/capabilities")


def serves(g: Gateway) -> tuple[bool | None, str]:
    """Does the daemon serve this caller now? One capabilities call: True
    when it answered; False when it refused the caller as not served
    (`not_served`, e.g. 403 plane: the daemon serves only processes of
    Claude Code sessions it knows, so a daemon still running from another
    session while the gateway plugin is off, or not loaded, in this one
    serves nothing here, and a job_start falls back to the own runner);
    None, with why, when it couldn't be asked or refused otherwise. A
    discovery file and a live pid alone don't say this (acceptance: the
    status said "available" while the jobs ran in the own runner)."""
    if not g.ok:
        return None, g.reason
    try:
        capabilities(g)
        return True, ""
    except CallError as e:
        if not_served(e):
            return False, e.text()
        return None, e.text()


def snapshot(g: Gateway) -> dict[str, Any]:
    return call(g, "GET", "/v1/snapshot")


def events(g: Gateway, since: int, wait: float = 0.0, epoch: int | None = None) -> dict[str, Any]:
    wait = max(0.0, min(EVENTS_WAIT_MAX, float(wait)))
    q = f"since={max(0, int(since))}&wait={wait:g}" + (f"&epoch={int(epoch)}" if epoch is not None else "")
    return call(g, "GET", f"/v1/events?{q}", timeout=call_timeout() + wait)


def job_start(g: Gateway, body: dict[str, Any]) -> dict[str, Any]:
    return call(g, "POST", "/v1/jobs", body, timeout=call_timeout() * 2.5)


def _job_path(job: str) -> str:
    if not JOB_RE.fullmatch(job or ""):
        raise CallError("refused", f"not a gateway job id: {sanitize.clip(job, 40)!r}")
    return f"/v1/jobs/{job}"


def job_get(g: Gateway, job: str) -> dict[str, Any]:
    return call(g, "GET", _job_path(job))


def job_tail(g: Gateway, job: str, nbytes: int = 8192) -> dict[str, Any]:
    n = max(1, min(65536, int(nbytes)))
    return call(g, "GET", f"{_job_path(job)}/tail?bytes={n}")


def job_signal(g: Gateway, job: str, sig: str) -> dict[str, Any]:
    return call(g, "POST", f"{_job_path(job)}/signal", {"signal": sig}, timeout=call_timeout() * 2)


def pause(g: Gateway, session: str, task_ref: str = "", reason: str = "", pause_id: str = "") -> dict[str, Any]:
    # interrupt waits up to a second per job in the gateway before it answers
    body = {"session": session, "reason": sanitize.clip(reason, 120)}
    if task_ref:
        body["task_ref"] = task_ref
    if pause_id:
        body["pause_id"] = pause_id
    return call(g, "POST", "/v1/pause", body, timeout=call_timeout() * 5)


def resume(g: Gateway, session: str, task_ref: str = "", pause_id: str = "") -> dict[str, Any]:
    body: dict[str, Any] = {"session": session}
    if task_ref:
        body["task_ref"] = task_ref
    if pause_id:
        body["pause_id"] = pause_id
    return call(g, "POST", "/v1/resume", body, timeout=call_timeout() * 2.5)


# -- what crosses into checkpoint's records ------------------------------------------------

def _safe(v: Any) -> Any:
    """A value as the gateway's own event allowlist would keep it, else None;
    a string of the allowed characters that is token-shaped (or names a
    secret: `token:abc`) comes back scrubbed (sanitize.scrub), never as is:
    the allowlist's character set alone lets a token through."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and abs(v) < 1e15:
        return v
    if isinstance(v, str) and SAFE.fullmatch(v):
        clean = sanitize.scrub(v)
        return v if clean == v else clean
    return None


def clean_event(ev: Any) -> dict[str, Any] | None:
    """A gateway event as checkpoint records it: its kind's fields only, each
    a safe value (the gateway allowlists them too: this is the second layer),
    or None for an unknown kind or a malformed event."""
    if not isinstance(ev, dict):
        return None
    kind = ev.get("kind")
    fields = EVENT_FIELDS.get(kind) if isinstance(kind, str) else None
    seq, epoch = ev.get("seq"), ev.get("epoch")
    if fields is None or not isinstance(seq, int) or isinstance(seq, bool) or not isinstance(epoch, int):
        return None
    out: dict[str, Any] = {"kind": kind, "seq": seq, "epoch": epoch}
    ts = ev.get("ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        out["ts"] = float(ts)
    for k in fields:
        if k in ev:
            v = _safe(ev[k])
            if v is not None or ev[k] is None:
                out[k] = v
    return out


def clean_job(j: Any) -> dict[str, Any] | None:
    """A job from a snapshot or GET /v1/jobs/<id>: ids, state, exit, times,
    host; the command scrubbed again and clipped (the gateway scrubs it)."""
    if not isinstance(j, dict) or not JOB_RE.fullmatch(str(j.get("job") or "")):
        return None
    out: dict[str, Any] = {"job": j["job"]}
    for k in ("state", "host", "host_name", "session", "task_ref", "workspace", "owner", "on_pause", "signal"):
        v = _safe(j.get(k))
        if isinstance(v, str):
            out[k] = v
    for k in ("exit", "started", "ended"):
        v = j.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v
    for k in ("gone", "detached"):
        if j.get(k) is True:
            out[k] = True
    if isinstance(j.get("command"), str):
        out["command"] = sanitize.clip(sanitize.scrub(j["command"]), 300)
    if isinstance(j.get("cwd"), str):
        out["cwd"] = sanitize.clip(sanitize.scrub(j["cwd"]), 300)
    return out


def clean_host(h: Any) -> dict[str, Any] | None:
    if not isinstance(h, dict):
        return None
    hid, name = _safe(h.get("host")), _safe(h.get("name"))
    if not isinstance(name, str) or not name:
        return None
    out: dict[str, Any] = {"host": hid if isinstance(hid, str) else "", "name": name,
                           "local": h.get("local") is True}
    # identity and the states the gateway reports: enums and a machine token;
    # a field it didn't send stays absent. reachable and auth come as the
    # gateway's words (ok, down, refused, locked, unknown) or as CONTRACT-v1's
    # true / false / null: one vocabulary here (state_word).
    for k in ("identity", "reachable", "auth", "runtime", "machine", "runtime_version", "platform", "waiting"):
        if k in ("reachable", "auth") and (h.get(k) is None or isinstance(h.get(k), bool)) and k in h:
            out[k] = state_word(k, h[k])
            continue
        v = _safe(h.get(k))
        if isinstance(v, str) and v:
            out[k] = v
    for k in ("same_machine", "stale"):
        if isinstance(h.get(k), bool):
            out[k] = h[k]
    if isinstance(h.get("rights"), dict):
        # the effective decision per action (read, exec, write, elevation,
        # credential, network, forwarding, env_import, provision)
        out["rights"] = _rights(h["rights"])
    if isinstance(h.get("address"), str):
        out["address"] = sanitize.clip(sanitize.scrub(h["address"]), 120)
    return out


def state_word(kind: str, v: Any) -> str:
    """A host's reachable / auth as one word: the contract's true / false /
    null (unknown), or the gateway's own word as it came (its snapshots and
    host.state events send words)."""
    if v is True:
        return "ok"
    if v is False:
        return "down" if kind == "reachable" else "failed"
    if v is None:
        return "unknown"
    return str(v)


def _rights(r: Any) -> dict[str, Any]:
    """An entry's rights (action -> allow / ask / deny): names and
    enums only."""
    out = {}
    if isinstance(r, dict):
        for verb, v in list(r.items())[:20]:
            if isinstance(verb, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", verb) and (
                    isinstance(v, bool) or v in ("ask", "true", "false", "allow", "deny", "off")):
                out[verb] = v
    return out


def clean_account(a: Any) -> dict[str, Any] | None:
    """An account of the snapshot: its name, kind, state and
    rights; never a credential."""
    if not isinstance(a, dict):
        return None
    name = _safe(a.get("account"))
    if not isinstance(name, str) or not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
        return None
    out: dict[str, Any] = {"account": name}
    for k in ("kind", "state"):
        v = _safe(a.get(k))
        if isinstance(v, str) and v:
            out[k] = v
    if isinstance(a.get("rights"), dict):
        out["rights"] = _rights(a["rights"])
    return out


def clean_terminal(t: Any) -> dict[str, Any] | None:
    if not isinstance(t, dict) or not re.fullmatch(r"t_[0-9A-HJKMNP-TV-Z]{26}", str(t.get("terminal") or "")):
        return None
    out: dict[str, Any] = {"terminal": t["terminal"]}
    for k in ("host", "host_name", "state", "owner", "session", "task_ref"):
        v = _safe(t.get(k))
        if isinstance(v, str):
            out[k] = v
    return out


def job_class(gstate: str, exit_code: Any = None, gone: bool = False) -> str:
    """A gateway job state as one of recovery's classes."""
    if gstate in LIVE_STATES:
        return "running"
    if gstate == "exited":
        return "completed" if exit_code == 0 else "failed"
    if gstate in ("failed", "canceled"):
        return "failed"
    return "unknown"


# -- the session's cursor and the journal ---------------------------------------------

def cursor_path(sid: str):
    return state.p("gateway", state.safe_id(sid) + ".json")


def load_cursor(sid: str) -> dict[str, Any]:
    c = state.read_json(cursor_path(sid), {}) or {}
    return c if isinstance(c, dict) else {}


def save_cursor(sid: str, c: dict[str, Any]) -> None:
    for k in ("jobs", "terminals", "requests"):
        if isinstance(c.get(k), list):
            c[k] = c[k][-FOLLOW_MAX:]
    state.write_json(cursor_path(sid), c)


def _follow(c: dict[str, Any], key: str, value: Any) -> None:
    if isinstance(value, str) and value:
        lst = c.setdefault(key, [])
        if value not in lst:
            lst.append(value)


@dataclass
class SyncResult:
    ok: bool = False
    status: str = ""
    reason: str = ""
    recorded: int = 0
    epoch_changed: bool = False
    error: str = ""


def _known_jobs(sid: str, task: str) -> set[str]:
    """Gateway jobs checkpoint started for this session or task (jobs.json)."""
    from . import jobs
    out = set()
    for j in jobs._registry()["jobs"].values():
        if j.get("owner") == "gateway" and j.get("gateway_job") and (
                j.get("session_id") == sid or (task and j.get("task_id") == task)):
            out.add(j["gateway_job"])
    return out


def _relevant(ev: dict[str, Any], sid: str, task: str, c: dict[str, Any], mine: set[str]) -> bool:
    """Does this event concern the session's task? Updates what the cursor follows."""
    k = ev["kind"]
    if k in ("daemon.started", "daemon.stopping", "host.state", "rights.changed", "account.state"):
        return True
    if k == "job.started":
        if ev.get("session") == sid or (task and ev.get("task") == task) or ev.get("job") in mine:
            _follow(c, "jobs", ev.get("job"))
            return True
        return False
    if k in ("job.state", "job.exited", "job.output"):
        return ev.get("job") in mine or ev.get("job") in (c.get("jobs") or [])
    if k == "terminal.opened":
        if ev.get("session") == sid:
            _follow(c, "terminals", ev.get("terminal"))
            return True
        return False
    if k in ("terminal.owner", "terminal.closed"):
        return ev.get("terminal") in (c.get("terminals") or [])
    if k == "pause.applied":
        return ev.get("session") == sid
    if k == "auth.waiting":
        if ev.get("session") == sid or ev.get("terminal") in (c.get("terminals") or []):
            _follow(c, "requests", ev.get("request"))
            return True
        return False
    if k == "auth.supplied":
        return ev.get("request") in (c.get("requests") or [])
    return False


def _snapshot_records(g: Gateway, snap: dict[str, Any], sid: str, task: str, c: dict[str, Any],
                      mine: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(the gateway.snapshot record, the jobs in it that are the session's or task's)."""
    hosts = [h for h in map(clean_host, snap.get("hosts") or []) if h][:50]
    accounts = [a for a in map(clean_account, snap.get("accounts") or []) if a][:50]
    ws = [w for w in (_safe(x.get("workspace")) for x in (snap.get("workspaces") or []) if isinstance(x, dict))
          if isinstance(w, str) and w][:50]
    jl = [j for j in map(clean_job, snap.get("jobs") or []) if j]
    tl = [t for t in map(clean_terminal, snap.get("terminals") or []) if t]
    my_jobs = [j for j in jl if j["job"] in mine or j.get("session") == sid or (task and j.get("task_ref") == task)]
    my_terms = [t for t in tl if t.get("session") == sid]
    for j in my_jobs:
        _follow(c, "jobs", j["job"])
    for t in my_terms:
        _follow(c, "terminals", t["terminal"])
    epoch = snap.get("epoch") if isinstance(snap.get("epoch"), int) else g.epoch
    seq = snap.get("seq") if isinstance(snap.get("seq"), int) else 0
    data = {"epoch": epoch, "seq": seq, "gateway_version": g.version, "machine_id": g.machine_id,
            "hosts": hosts, "workspaces": ws, **({"accounts": accounts} if "accounts" in snap else {}),
            "jobs": [{k: j.get(k) for k in ("job", "state", "exit", "signal", "host", "host_name", "task_ref",
                                            "on_pause", "gone") if j.get(k) is not None} for j in my_jobs[:50]],
            "terminals": [{k: t.get(k) for k in ("terminal", "state", "owner", "host") if t.get(k)}
                          for t in my_terms[:50]]}
    return [{"kind": "gateway.snapshot", "session": sid, "op": f"gw:{epoch}:snap:{seq}", "data": data}], my_jobs


def _update_jobs(found: list[dict[str, Any]], epoch: int | None, lost: set[str] | None = None) -> None:
    """The registry's view of gateway jobs from what the gateway said."""
    from . import jobs
    jobs.note_gateway_states({j["job"]: j for j in found}, epoch, lost or set())


def sync(sid: str, wait: float = 0.0, g: Gateway | None = None, task: str = "") -> SyncResult:
    """Bring the session's journal up to date with the gateway: the snapshot
    after a new epoch (or the first time), then the events since the cursor.
    Never raises; a transition to "not available" is recorded once."""
    from . import capture
    res = SyncResult()
    if not sid:
        return res
    g = g or discover()
    res.status, res.reason = g.status, g.reason
    c = load_cursor(sid)
    task = task or state.load_session(sid).get("task_id") or ""
    recs: list[dict[str, Any]] = []
    try:
        if not g.ok:
            if c.get("status") == "available":
                # it was there: what it ran is unknown from now on, never done
                recs.append({"kind": "gateway.unavailable", "session": sid, "op": f"gw:down:{int(time.time())}",
                             "data": {"status": g.status, "reason": sanitize.clip(sanitize.scrub(g.reason), 200),
                                      "epoch": c.get("epoch")}})
            c.update(status=g.status, synced_ts=time.time())
            return res
        mine = _known_jobs(sid, task)
        epoch = g.epoch
        new_epoch = c.get("epoch") != epoch or c.get("pid") != g.pid
        ev: dict[str, Any] = {}
        if not new_epoch:
            try:
                ev = events(g, int(c.get("next") or 0), wait, epoch)
            except CallError as e:
                res.error = e.text()
                return res
            if ev.get("epoch") != epoch or ev.get("reset"):
                new_epoch = True
                if isinstance(ev.get("epoch"), int):
                    epoch = ev["epoch"]
        if new_epoch:
            try:
                snap = snapshot(g)
            except CallError as e:
                res.error = e.text()
                return res
            if isinstance(snap.get("epoch"), int):
                epoch = snap["epoch"]
            if c.get("epoch") is not None and c.get("epoch") != epoch:
                res.epoch_changed = True
                recs.append({"kind": "gateway.epoch", "session": sid, "op": f"gw:epoch:{epoch}",
                             "data": {"from": c.get("epoch"), "to": epoch, "gateway_version": g.version}})
            srecs, my_jobs = _snapshot_records(g, snap, sid, task, c, mine)
            recs.extend(srecs)
            # A job checkpoint followed that the new epoch's snapshot doesn't
            # list is lost to the gateway: unknown, never done.
            listed = {j["job"] for j in (clean_job(x) for x in snap.get("jobs") or []) if j}
            _update_jobs(my_jobs, epoch, lost=(mine - listed) if res.epoch_changed else set())
            c.update(epoch=epoch, pid=g.pid, next=int(snap.get("seq") or 0))
            if not ev or ev.get("epoch") != epoch or ev.get("reset"):
                ev = {}
        evs = sorted((e for e in map(clean_event, ev.get("events") or []) if e and e["epoch"] == epoch),
                     key=lambda e: e["seq"])
        nxt = int(c.get("next") or 0)
        states: dict[str, dict[str, Any]] = {}
        last_output: dict[str, dict[str, Any]] = {}
        for e in evs:
            if e["seq"] <= nxt and not new_epoch:
                continue   # already seen (a duplicate delivery, or an overlap)
            if not _relevant(e, sid, task, c, mine):
                continue
            if e["kind"] == "job.output":
                last_output[e.get("job") or ""] = e     # counts only; one per job per batch
                continue
            if e["kind"] in ("job.state", "job.exited") and isinstance(e.get("job"), str):
                states[e["job"]] = {"job": e["job"], "state": e.get("state"),
                                    **({"exit": e["exit"]} if isinstance(e.get("exit"), int) else {}),
                                    **({"signal": e["signal"]} if isinstance(e.get("signal"), str) else {})}
            recs.append({"kind": "gateway.event", "session": sid, "op": f"gw:{e['epoch']}:{e['seq']}",
                         "data": {k: v for k, v in e.items() if k != "ts"}, **({"ts": e["ts"]} if "ts" in e else {})})
        for e in last_output.values():
            recs.append({"kind": "gateway.event", "session": sid, "op": f"gw:{e['epoch']}:{e['seq']}",
                         "data": {k: v for k, v in e.items() if k != "ts"}})
        if states:
            _update_jobs([s for s in states.values() if s.get("state")], epoch)
        if isinstance(ev.get("next"), int) and ev.get("epoch") == epoch:
            c["next"] = max(nxt if not new_epoch else 0, ev["next"])
        c.update(status="available", synced_ts=time.time())
        res.ok = True
        return res
    except Exception as e:  # noqa: BLE001  never let the gateway break a hook or a tool
        res.error = f"{type(e).__name__}: {e}"
        return res
    finally:
        if recs:
            try:
                r = capture.record(sid, recs)
                res.recorded = r.records
                if r.error:
                    res.error = res.error or f"recording failed: {r.error}"
            except Exception as e:  # noqa: BLE001
                res.error = res.error or f"recording failed: {type(e).__name__}"
        with contextlib.suppress(Exception):
            save_cursor(sid, c)


def recently_synced(sid: str, seconds: float) -> bool:
    try:
        return time.time() - float(load_cursor(sid).get("synced_ts") or 0) < seconds
    except (TypeError, ValueError):
        return False
