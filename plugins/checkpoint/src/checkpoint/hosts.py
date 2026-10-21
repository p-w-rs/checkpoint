"""The gateway's hosts a task used, and what the gateway says of them now
(brief §7 and §10; PLAN-v4 phase 5).

Which hosts (`used`): the task's gateway jobs (checkpoint's registry and its
`job.started` records: the machine the gateway ran them on), the jobs and
terminals the gateway's events and snapshots tie to the task's sessions, and
the gateway tools the task called with a `host`. A Bash command's `ssh NAME`
(or `scp`/`rsync`/`sftp` with `NAME:`) counts only when NAME is a host the
gateway listed, and says it was inferred from the command.

What state (`now`): only what the gateway reported. Before a resume a fresh
snapshot (`GET /v1/snapshot`) gives each host's `reachable`, `auth`,
`runtime`, `identity` and machine token; without the gateway every one of
them is "unknown", with the reason. A field the gateway didn't send is "not
reported", never guessed, and a value checkpoint doesn't know is shown as the
gateway's own word. The journal adds what was last observed (`host.state`
events, earlier snapshots).

A changed identity is a reason to stop and check: the gateway said
changed_key, changed_machine or wrong_host (its last `host.state` for the
host, or the fresh snapshot), it waits for the person to confirm a host
change (`waiting`), or the machine token it reports now differs from the one
it reported while the task used the host (a host the person confirmed as a
new machine is still another machine than the one the task's work ran on).
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from . import gateway, sanitize

# The gateway's words (daemon/remote.py, lifecycle.hosts): anything else is
# shown as it came, and never read as good or bad.
REACH_WORDS = {"ok": "reachable", "down": "UNREACHABLE", "unknown": "reachability unknown"}
AUTH_WORDS = {"ok": "authenticated", "refused": "authentication REFUSED", "failed": "NOT authenticated",
              "locked": "vault LOCKED (no credential until it is unlocked)", "unknown": "authentication unknown"}
RUNTIME_WORDS = {"ready": "runtime ready", "missing": "runtime missing", "checking": "runtime being checked",
                 "staging": "runtime being staged",
                 "failed": "runtime FAILED", "unsupported": "runtime unsupported on its platform",
                 "unknown": "runtime unknown"}
CHANGED_IDENTITY = {"changed_key": "its host key changed", "changed_machine": "it is another machine now",
                    "wrong_host": "it answered as another host"}
FINE_IDENTITY = ("verified", "machine", "assigned", "unknown")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
# ssh options that take an argument (OpenSSH 9): the destination is the
# first word after the options.
_SSH_ARG_OPTS = set("BbcDEeFIiJLlmOoPpQRSWw")
_REMOTE_TOOLS = ("ssh", "mosh", "scp", "sftp", "rsync")


@dataclass
class HostUse:
    """One host the task used."""
    host: str = ""            # the gateway's id (m_...), when known
    name: str = ""            # its display name, when known
    local: bool = False
    via: list[str] = field(default_factory=list)       # how the task used it (words)
    ops: list[str] = field(default_factory=list)       # tool_use_ids / job ids that needed it
    seq: int = 0              # first record that shows the use
    inferred: bool = False    # only from a command's text (`ssh NAME`)
    machine_then: str = ""    # the machine token the gateway reported while the task used it
    last: dict[str, Any] = field(default_factory=dict)  # last observed state (journal)
    ident_event: tuple[str, int] = ("", 0)               # the last host.state identity and its seq
    ident_epoch: Any = None                              # the gateway epoch of that event

    @property
    def label(self) -> str:
        n = self.name or "?"
        if self.local:
            n += " (this machine)"
        return f"{n} [{self.host}]" if self.host else n


def safe_name(v: Any) -> str:
    """A host name as the gateway's form validates it, else ''."""
    s = str(v or "")
    return s if NAME_RE.fullmatch(s) and sanitize.scrub(s) == s else ""


def command_hosts(command: str) -> list[str]:
    """Host names a Bash command reaches with ssh/scp/rsync/sftp/mosh (the
    gateway's stand-ins answer to those names): ssh's destination after its
    options, `NAME:` words of the copy tools; `user@` dropped. Candidates
    only: `used` keeps the ones the gateway lists."""
    out: list[str] = []
    for seg in re.split(r"&&|\|\||[;&|\n(]", command or "")[:40]:
        try:
            words = shlex.split(seg, posix=True)
        except ValueError:
            words = seg.split()
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*|sudo|env|command|exec|time|nohup", words[0]):
            words = words[1:]
        if not words:
            continue
        prog = words[0].rsplit("/", 1)[-1]
        if prog not in _REMOTE_TOOLS:
            continue
        if prog in ("ssh", "mosh"):
            i = 1
            while i < len(words) and words[i].startswith("-") and words[i] != "--":
                w = words[i]
                if len(w) == 2 and w[1] in _SSH_ARG_OPTS:
                    i += 2
                else:
                    i += 1
            if i < len(words) and words[i] == "--":
                i += 1
            if i < len(words):
                dest = words[i]
                if dest.startswith("ssh://"):
                    dest = dest[6:].split("/", 1)[0].split(":", 1)[0]
                n = safe_name(dest.rsplit("@", 1)[-1])
                if n:
                    out.append(n)
            continue
        for w in words[1:]:
            if w.startswith("-") or ":" not in w:
                continue
            host = w.split(":", 1)[0].rsplit("@", 1)[-1]
            n = safe_name(host)
            if n:
                out.append(n)
    return list(dict.fromkeys(out))


GATEWAY_JOB_START = "mcp__plugin_gate_gw__job_start"     # runs its command on its `host` (default localhost)
OWN_JOB_START = "mcp__plugin_check_jobs__job_start"      # checkpoint's own: no host argument


def names_host(tool: str, inp: dict[str, Any] | None) -> bool:
    """Whether a recorded call says by its `host` argument where it ran
    (the gateway's terminal_open, job_start, host_runtime ...), rather than
    by the remote commands in its command text (Bash, checkpoint's own
    job_start)."""
    if tool == GATEWAY_JOB_START:
        return True
    return tool not in ("Bash", OWN_JOB_START) and bool((inp or {}).get("host"))


def call_hosts(tool: str, inp: dict[str, Any] | None) -> list[str]:
    """Host names a recorded call named: a tool's `host` argument
    (`localhost` is this machine; the journal records the gateway's
    job_start with its host, `localhost` when the call named none, so one
    journaled without a host is from before that: nothing is claimed),
    or the remote commands of a Bash command or of checkpoint's own
    job_start (candidates: `used` keeps the ones the gateway lists)."""
    inp = inp or {}
    if names_host(tool, inp):
        n = safe_name(inp.get("host"))
        return [n] if n else []
    if tool == "Bash" or tool.endswith("__job_start"):
        return command_hosts(str(inp.get("command") or ""))
    return []


def _state_of(h: dict[str, Any]) -> dict[str, Any]:
    out = {k: h[k] for k in ("reachable", "auth", "runtime", "identity", "machine", "same_machine", "stale",
                             "waiting", "runtime_version", "platform") if h.get(k) is not None}
    return out


def used(st: Any, registry: list[dict[str, Any]] | None = None) -> list[HostUse]:
    """The hosts the task used, from its journal (and the registry's records
    of its gateway jobs), oldest use first."""
    by_key: dict[str, HostUse] = {}
    names: dict[str, str] = {}          # id -> name, from every snapshot
    ids: dict[str, str] = {}            # name -> id
    local_ids: set[str] = set()
    for r in getattr(st, "gw_snapshots", []):
        for h in (r.get("data") or {}).get("hosts") or []:
            hid, nm = str(h.get("host") or ""), safe_name(h.get("name"))
            if hid and nm:
                names[hid] = nm
                ids[nm] = hid
            if h.get("local") and hid:
                local_ids.add(hid)

    def use(hid: str = "", name: str = "", via: str = "", op: str = "", seq: int = 0, inferred: bool = False,
            local: bool = False) -> None:
        hid = hid if gateway.SAFE.fullmatch(hid or "") else ""
        name = safe_name(name)
        if not hid and name:
            hid = ids.get(name, "")
        if hid and not name:
            name = names.get(hid, "")
        if not hid and not name:
            return
        key = hid or f"name:{name}"
        u = by_key.get(key)
        if u is None:
            u = by_key[key] = HostUse(host=hid, name=name, seq=seq, inferred=inferred)
        elif not inferred:
            u.inferred = False
        if name and not u.name:
            u.name = name
        u.local = u.local or local or (hid in local_ids)
        if via and via not in u.via and len(u.via) < 8:
            u.via.append(via)
        if op and op not in u.ops and len(u.ops) < 50:
            u.ops.append(op)
        if seq and (not u.seq or seq < u.seq):
            u.seq = seq

    inferred_names: list[tuple[str, str, int]] = []
    for r in st.records:
        k, d, seq = r["kind"], r.get("data") or {}, r["seq"]
        if k == "job.started" and d.get("supervisor") == "gateway" and d.get("ok", True):
            use(str(d.get("machine_id") or ""), "localhost" if (d.get("host") or "localhost") == "localhost" else
                str(d.get("host")), f"gateway job {d.get('name') or d.get('job') or ''}".strip(),
                str(d.get("gateway_job") or d.get("job") or ""), seq, local=(d.get("host") or "localhost") == "localhost")
        elif k == "gateway.snapshot":
            for j in d.get("jobs") or []:
                use(str(j.get("host") or ""), str(j.get("host_name") or ""), f"gateway job {j.get('job')}",
                    str(j.get("job") or ""), seq)
            for t in d.get("terminals") or []:
                use(str(t.get("host") or ""), "", f"terminal {t.get('terminal')}", str(t.get("terminal") or ""), seq)
        elif k == "gateway.event":
            ek = d.get("kind")
            if ek == "job.started":
                use(str(d.get("host") or ""), "", f"gateway job {d.get('job')}", str(d.get("job") or ""), seq)
            elif ek == "terminal.opened":
                use(str(d.get("host") or ""), "", f"terminal {d.get('terminal')}", str(d.get("terminal") or ""), seq)
        elif k == "tool.intent":
            tool = str(d.get("tool") or "")
            inp = d.get("input") or {}
            op = r.get("op") or ""
            if not names_host(tool, inp) and (tool == "Bash" or tool.endswith("__job_start")):
                for n in command_hosts(str(inp.get("command") or "")):
                    inferred_names.append((n, op, seq))
            else:
                for n in call_hosts(tool, inp):
                    # (the gateway's `host` takes a name or an id)
                    use(n if n in names else "", "" if n in names else n, f"{tool.rsplit('__', 1)[-1]} call",
                        op, seq, local=n == "localhost")
    for j in registry or []:
        if j.get("owner") == "gateway" or j.get("gateway_job"):
            hn = str(j.get("host") or "")
            use(str(j.get("host_id") or j.get("machine_id") or ""), hn,
                f"gateway job {j.get('name') or j.get('id')}", str(j.get("gateway_job") or ""),
                local=hn in ("", "localhost"))
    known = set(ids) | {u.name for u in by_key.values() if u.name}
    for n, op, seq in inferred_names:
        if n in known:
            use("", n, f"`ssh {n}` (inferred from the command)", op, seq, inferred=True)
    # What the journal last observed of each (its snapshots and host.state
    # events, the newest last), and the machine token the task last knew (a
    # change confirmed while the task went on shows as host.state events).
    seen = sorted([r for r in getattr(st, "gw_snapshots", [])]
                  + [r for r in getattr(st, "gw_events", []) if (r.get("data") or {}).get("kind") == "host.state"],
                  key=lambda r: r["seq"])
    for u in by_key.values():
        for r in seen:
            d = r.get("data") or {}
            if r["kind"] == "gateway.snapshot":
                for h in d.get("hosts") or []:
                    if (u.host and h.get("host") == u.host) or (not u.host and u.name and h.get("name") == u.name):
                        if h.get("machine"):
                            u.machine_then = str(h.get("machine"))
                        s = _state_of(h)
                        if s:
                            u.last = {**s, "seq": r["seq"], "how": "its snapshot"}
                continue
            if (u.host and d.get("host") == u.host) or (u.name and d.get("name") == u.name):
                s = {k: (gateway.state_word(k, d[k]) if k in ("reachable", "auth") else d[k])
                     for k in ("reachable", "auth", "runtime", "identity")
                     if k in d and (d[k] is not None or k in ("reachable", "auth"))}
                if "runtime" not in s and d.get("state") is not None:
                    s["runtime"] = d["state"]
                prev = {k: v for k, v in u.last.items() if k not in ("seq", "how")}
                u.last = {**prev, **s, "seq": r["seq"], "how": "host.state"}
                if isinstance(d.get("identity"), str) and d["identity"]:
                    _ident_seen(u, d["identity"], r["seq"], d.get("epoch"))
    return sorted(by_key.values(), key=lambda u: u.seq or 0)


def _ident_seen(u: HostUse, ident: str, seq: int, epoch: Any) -> None:
    """One host.state identity, in journal order. A change (changed_key,
    changed_machine, wrong_host) stays until the gateway clears it: "verified"
    (it checked the host again), or "unknown" in the same epoch as the change
    (the person accepted it in the gateway's dialog: the gateway forgets the
    identity it checked and looks again). "unknown" in another epoch is a
    restarted gateway's first word on every host, which confirms nothing."""
    pending = u.ident_event[0] in CHANGED_IDENTITY
    if not pending or ident in CHANGED_IDENTITY or ident == "verified" or (
            ident == "unknown" and isinstance(epoch, int) and not isinstance(epoch, bool)
            and epoch == u.ident_epoch):
        u.ident_event, u.ident_epoch = (ident, seq), epoch


def word(kind: str, v: Any) -> str:
    """How one state reads; the gateway's own word when checkpoint doesn't
    know it; "not reported" when it sent none."""
    if v is None or v == "":
        return f"{kind} not reported"
    table = {"reachable": REACH_WORDS, "auth": AUTH_WORDS, "runtime": RUNTIME_WORDS}.get(kind, {})
    s = sanitize.clip(sanitize.scrub(str(v)), 40)
    return table.get(s, f"{kind} {s}")


def bad(kind: str, v: Any) -> bool:
    """A state the gateway reported that blocks work on the host."""
    if v is None or v in ("", "unknown"):
        return False
    if kind == "reachable":
        return v != "ok"
    if kind == "auth":
        return v != "ok"
    if kind == "runtime":
        return v not in ("ready", "checking", "staging")
    return False


def identity_change(now: dict[str, Any] | None, u: HostUse) -> str:
    """'' or why the host's identity is in doubt, per the gateway."""
    if now:
        ident = str(now.get("identity") or "")
        if ident in CHANGED_IDENTITY:
            return f"{CHANGED_IDENTITY[ident]} (the gateway, now)"
        if now.get("waiting"):
            w = sanitize.clip(sanitize.scrub(str(now["waiting"])), 40)
            return f"the gateway waits for the person to confirm a host change ({w})"
        if u.machine_then and now.get("machine") and now["machine"] != u.machine_then:
            return (f"its machine token changed since the task used it ({sanitize.clip(u.machine_then, 16)} -> "
                    f"{sanitize.clip(str(now['machine']), 16)}, the gateway): another machine than the one the "
                    "task's work ran on")
    # The gateway's snapshot says "verified" for any host with a machine
    # token, also while a change waits for the person; its host.state events
    # say when the person confirmed it ("verified" again). So the last event
    # in this task's journal decides, until a later one clears it.
    ident, seq = u.ident_event
    if ident in CHANGED_IDENTITY:
        return (f"{CHANGED_IDENTITY[ident]} (the gateway's last word on it in this task's journal, host.state seq "
                f"{seq}; no later event says it was confirmed)")
    return ""


@dataclass
class HostNow:
    use: HostUse
    now: dict[str, Any] | None      # the fresh snapshot's record; None when it couldn't be asked
    listed: bool = True             # in the fresh snapshot (False: gone from the gateway's hosts)
    why_unknown: str = ""
    changed: str = ""               # identity_change()

    def states(self) -> dict[str, Any]:
        if self.now is None:
            return {"reachable": "unknown", "auth": "unknown", "runtime": "unknown"}
        # (the gateway reports a local host's runtime only: this machine
        # needs no reach or login)
        return {k: self.now.get(k) for k in ("reachable", "auth", "runtime")}

    def blocked(self) -> list[str]:
        """What the gateway reports that stops work needing this host."""
        if self.now is None:
            return []
        s = self.states()
        return [word(k, s.get(k)) for k in ("reachable", "auth") if bad(k, s.get(k))]

    def line(self) -> str:
        u = self.use
        via = "; ".join(u.via[:3]) + (f" and {len(u.via) - 3} more" if len(u.via) > 3 else "")
        head = f"{u.label}: used by {via or 'the task'} (observed, seq {u.seq})"
        if not self.listed:
            return (f"{head}; now: NOT in the gateway's host list any more (removed or renamed)"
                    + (f"; IDENTITY: {self.changed}" if self.changed else ""))
        if self.now is None:
            return f"{head}; now: reachable, authentication and runtime unknown ({self.why_unknown})"
        s = self.states()
        if u.local:
            parts = ["this machine (the gateway reports no reach or login for it)", word("runtime", s.get("runtime"))]
        else:
            parts = [word("reachable", s.get("reachable")), word("auth", s.get("auth")),
                     word("runtime", s.get("runtime"))]
        extra = ""
        if self.now.get("stale"):
            extra = " (the gateway hasn't asked it lately: its own last answer is older)"
        txt = f"{head}; now (the gateway's snapshot): {', '.join(parts)}{extra}"
        if self.changed:
            txt += f"; IDENTITY: {self.changed}"
        last = u.last
        if last and any(last.get(k) for k in ("reachable", "auth", "runtime")):
            ls = ", ".join(f"{k} {last[k]}" for k in ("reachable", "auth", "runtime") if last.get(k))
            txt += f" (last observed in the journal: {ls}, {last.get('how')}, seq {last.get('seq')})"
        return txt


def now(uses: list[HostUse], g: Any = None, snap: dict[str, Any] | None = None, why: str = "") -> list[HostNow]:
    """Each used host against the gateway's snapshot now (`snap`, the raw
    answer; None when it couldn't be read, with `why`)."""
    out: list[HostNow] = []
    hosts = [h for h in (gateway.clean_host(x) for x in (snap or {}).get("hosts") or []) if h] if snap else []
    by_id = {h["host"]: h for h in hosts if h.get("host")}
    by_name = {h["name"]: h for h in hosts}
    for u in uses:
        if snap is None:
            hn = HostNow(u, None, why_unknown=why or "the gateway wasn't asked")
            hn.changed = identity_change(None, u)
            out.append(hn)
            continue
        h = by_id.get(u.host) if u.host else None
        if h is None and u.name:
            h = by_name.get(u.name)
        if h is None and u.local:
            h = next((x for x in hosts if x.get("local")), None)
        hn = HostNow(u, h, listed=h is not None)
        # (a host no longer listed keeps the change its last host.state said)
        hn.changed = identity_change(h, u)
        out.append(hn)
    return out
