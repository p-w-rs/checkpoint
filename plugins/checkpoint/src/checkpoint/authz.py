"""Authorization references (brief §5 and §7): which rights and approvals a
task's operations used, by name and decision only, and whether they still
hold before a resume.

What is recorded (capture.py; never a reason text, a value or a command):

- `auth.ref` with `via: check`, from the mod's `tool.check` observer: Claude
  Code's permission verdict for a real call that a settings rule, a hook or
  an ask decided (a plain allow by the mode or the tool's own check isn't a
  reference): the decision (allow, ask, deny), the settings rule as written
  (scrubbed), the classic hook event that decided, and, when the gateway's
  approval hook decided, the gateway rights it names ("ssh on build", "sudo
  (root) on build", "sudo on this machine"), read from its fixed wording by
  `gateway_rights` and validated (anything else is dropped).
- `auth.ref` with `via: answer`, from the mod's `tool.call` after an ask or a
  deny: whether the call came back refused, as an error, or with a result.
  With the call's own outcome (PostToolUse) that says approved or declined.
- `auth.ref` with `via: request`, from the classic PermissionRequest hook: a
  permission dialog was put to the person (without mods the only sign of an
  ask; linked to its call by the tool and the input's hash).
- a `tool.outcome` with `denied: permission` (PermissionDenied: auto mode's
  classifier refused the call), and `mode` on every `tool.intent` (the
  payload's permission_mode).
- the gateway's own events, already in the journal: `auth.waiting` (a dialog
  of a kind, for a terminal) and `auth.supplied` (its outcome), by request.

Before a resume (`check`): a gateway right is checked against the gateway's
snapshot now (its host still configured; the right's value when the
snapshot lists rights; the host's identity and authentication), a settings
rule against the settings files Claude Code reads (user, project, local),
and the rest is said for what it was (a one-time answer covers that call
only; hooks and auto mode decide each call again). Unchanged ones are said
to hold, so nobody asks the person again for them; revoked or changed ones
go into "Check first".
"""
from __future__ import annotations

import json
import os
import pwd
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import sanitize

DECISIONS = ("allow", "ask", "deny")
RIGHT_RE = re.compile(r"[a-z][a-z0-9_-]{0,30}(?: \(root\))? on (?:[A-Za-z0-9][A-Za-z0-9_.-]{0,62}|this machine)")
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
# The gateway's rights words (its approval hook's: a host's ssh / sudo, an
# account's use) and its policy actions (the snapshot's `rights`): a right
# compares with the snapshot under its action.
VERB_ACTION = {"ssh": "exec", "sudo": "elevation", "scp": "write", "sftp": "write", "rsync": "write", "use": "credential"}
# the gateway's policy refusal names the action by what it covers (policy.WHAT)
_WHAT_ACTION = (("running commands", "exec"), ("reading files", "read"), ("writing outside", "write"),
                ("network paths", "network"), ("sudo (root)", "elevation"), ("the gateway handing a stored credential",
                                                                            "credential"),
                ("importing host folders", "env_import"), ("staging the gateway's managed runtime", "provision"),
                ("ssh agent forwarding", "forward_agent"), ("x11 forwarding", "forward_x11"),
                ("remote port forwarding", "forward_remote"), ("stdio forwarding", "forward_stdio"))
HOOK_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,40}")
MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")
# The gateway approval hook's fixed wording (gateway hooks_bash.py and
# consent.py): its rights are the only names taken from a reason text. A
# right a per-mode table turns off names the mode, by Claude Code's name for
# it (gateway 0.11, modes.DISPLAY): "ssh is off for box in Auto mode (gateway.toml)".
_GW_MODE = r"(?: in (?:Manual|Plan|Accept edits|Auto|Bypass permissions|Don't ask) mode)?"
_GW_ASK = re.compile(r'^gateway: (.+?) (?:is|are) set to "ask"')
_GW_OFF = re.compile(r"^gateway: ([a-z][a-z0-9_-]{0,30}) is off for ([A-Za-z0-9][A-Za-z0-9_.-]{0,62})"
                     + _GW_MODE + r" \(gateway\.toml\)")
_GW_SUDO_OFF = re.compile(r"^gateway: sudo is off on this machine" + _GW_MODE + r" \(gateway\.toml\)")
_GW_JOB = re.compile(r'^gateway: a job can\'t use the "ask" rights? for (.+?) \(nobody can be asked')
_GW_POLICY = re.compile(r'^gateway: (.+?) on ([A-Za-z0-9][A-Za-z0-9_.-]{0,62}|this machine) is '
                        r'(?:"ask" \((.*?)\); only|denied by (.+?)(?:;|\.$))')
_RULE_KEY = re.compile(r'(?:^|[\s\].])([a-z][a-z0-9_]{0,30}) = "?(?:allow|ask|deny|true|false|off)\b')


def _phrases(s: str) -> list[str]:
    parts = re.split(r", | and ", s)
    return [p for p in (x.strip() for x in parts) if RIGHT_RE.fullmatch(p)][:8]


def gateway_rights(reason: Any) -> tuple[str, list[str]]:
    """(how the gateway decided: ask / off / job / '' , the rights named) from
    a permission reason; ('', []) for a reason that isn't the gateway's
    approval hook's (other hooks' texts are never kept)."""
    r = str(reason or "")[:600]
    if not r.startswith("gateway:"):
        return "", []
    m = _GW_ASK.match(r)
    if m:
        return "ask", _phrases(m.group(1))
    m = _GW_OFF.match(r)
    if m:
        name = f"{m.group(1)} on {m.group(2)}"
        return "off", [name] if RIGHT_RE.fullmatch(name) else []
    if _GW_SUDO_OFF.match(r):
        return "off", ["sudo on this machine"]
    m = _GW_JOB.match(r)
    if m:
        return "job", _phrases(m.group(1))
    m = _GW_POLICY.match(r)
    if m:
        # the policy wording: "<what the action covers> on <target> is denied by
        # <rule>" / "is \"ask\" (<rule>)": the action from the rule's key, else
        # from what it covers; never the rule's text
        what, target, rule = m.group(1).lower(), m.group(2), m.group(3) or m.group(4) or ""
        km = _RULE_KEY.search(rule)
        action = km.group(1) if km else next((a for w, a in _WHAT_ACTION if what.startswith(w)), "")
        name = f"{action} on {target}"
        return ("ask" if m.group(3) is not None else "off"), ([name] if action and RIGHT_RE.fullmatch(name) else [])
    return "rule", []


def right_host(name: str) -> tuple[str, str]:
    """('ssh', 'build') for "ssh on build"; ('sudo', 'localhost') for "sudo
    on this machine"; the verb drops "(root)"."""
    verb, _, host = name.partition(" on ")
    return verb.replace(" (root)", ""), ("localhost" if host == "this machine" else host)


def check_record(p: dict[str, Any]) -> dict[str, Any] | None:
    """The journal's `auth.ref` data for the mod's spooled tool.check verdict
    (None when it says nothing worth keeping)."""
    dec = p.get("decision") if p.get("decision") in DECISIONS else "?"
    data: dict[str, Any] = {"via": "check", "decision": dec}
    rule = p.get("rule")
    if isinstance(rule, str) and rule:
        data["rule"] = sanitize.clip(sanitize.scrub(rule), 200)
    hook = p.get("hook")
    if isinstance(hook, str) and HOOK_RE.fullmatch(hook):
        data["hook"] = hook
    how, rights = gateway_rights(p.get("reason"))
    if how:
        data["gateway"] = how
        if rights:
            data["rights"] = rights
    return data


def answer_record(p: dict[str, Any]) -> dict[str, Any]:
    a = p.get("answered")
    out: dict[str, Any] = {"via": "answer", "answered": a if a in ("deny", "error", "result") else "?"}
    if p.get("rejected") is True:
        out["rejected"] = True      # an error result in Claude Code's own refusal words (the mod checked)
    return out


# -- what the journal holds ---------------------------------------------------------

def refused_ops(st: Any) -> dict[str, str]:
    """op -> why, for calls that never ran because Claude Code refused them,
    and no outcome was recorded: the mod's tool.call came back refused
    (`answered: deny`), or as an error in Claude Code's own refusal words
    (`rejected`: a deny rule or hook, the person's No, an ask no one could answer), or as an
    error after a deny verdict. The verdict alone isn't enough (a hook above
    the observer may change it), nor an error after an ask (the call may
    have run and been cut off)."""
    checks: dict[str, dict[str, Any]] = {}
    answers: dict[str, tuple[dict[str, Any], int]] = {}
    for r in st.records:
        if r["kind"] != "auth.ref":
            continue
        d = r.get("data") or {}
        tid = str(d.get("tool_use_id") or "")
        if not tid:
            continue
        if d.get("via") == "check":
            checks.setdefault(tid, d)
        elif d.get("via") == "answer":
            answers[tid] = (d, r["seq"])
    out = {}
    for tid, (a, seq) in answers.items():
        o = st.ops.get(tid) or {}
        if o.get("outcome") is not None:
            continue
        d = checks.get(tid) or {}
        if not (a.get("answered") == "deny" or (a.get("answered") == "error" and (
                a.get("rejected") is True or d.get("decision") == "deny"))):
            continue
        if d.get("decision") == "ask":
            how = "declined when Claude Code asked"
        elif d.get("gateway"):
            how = "refused by the gateway's approval hook"
        elif d.get("rule"):
            how = f"refused by the settings rule `{d['rule']}`"
        elif d.get("hook"):
            how = f"refused by a {d['hook']} hook"
        else:
            how = "refused by Claude Code's permission check"
        out[tid] = f"{how} (observed, seq {seq}): it never ran"
    return out


@dataclass
class Ref:
    """One authorization reference: a kind, a name and its decisions."""
    kind: str               # rule, gateway, hook, ask, classifier, dialog
    name: str
    decisions: dict[str, int] = field(default_factory=dict)
    tools: list[str] = field(default_factory=list)
    seqs: list[int] = field(default_factory=list)
    how: str = ""           # for a gateway right: ask / off / job / rule
    verdict: str = ""       # for a settings rule: its list then (the latest verdict: allow, ask or deny)

    def add(self, decision: str, tool: str, seq: int, verdict: str = "") -> None:
        if verdict in DECISIONS:
            self.verdict = verdict
        self.decisions[decision] = self.decisions.get(decision, 0) + 1
        if tool and tool not in self.tools and len(self.tools) < 6:
            self.tools.append(tool)
        if len(self.seqs) < 200:
            self.seqs.append(seq)

    def decided(self) -> str:
        order = ("allow", "approved", "used", "ask", "declined", "deny", "denied", "refused", "widened", "narrowed",
                 "removed", "ok", "cancelled", "timeout", "failed", "unavailable", "paused", "no answer recorded")
        keys = sorted(self.decisions, key=lambda k: order.index(k) if k in order else 99)
        return ", ".join(f"{k} x{self.decisions[k]}" if self.decisions[k] > 1 else k for k in keys)

    def label(self) -> str:
        return {"rule": f"settings rule `{self.name}`", "gateway": f"gateway right \"{self.name}\"",
                "account": f"gateway account {self.name}", "rights": f"the gateway's rights for {self.name}",
                "hook": f"a {self.name} hook's decision", "ask": f"{self.name} calls put to the person",
                "classifier": f"auto mode's classifier on {self.name} calls",
                "dialog": f"gateway dialog ({self.name})"}.get(self.kind, self.name)

    def seq_text(self) -> str:
        s = sorted(set(self.seqs))
        if not s:
            return ""
        return f"seq {s[0]}" if len(s) == 1 else f"seq {s[0]}-{s[-1]}, {len(s)} records"


@dataclass
class Refs:
    items: list[Ref] = field(default_factory=list)
    modes: list[tuple[str, int, int]] = field(default_factory=list)    # (mode, first seq, last seq)

    def get(self, kind: str, name: str) -> Ref:
        for r in self.items:
            if r.kind == kind and r.name == name:
                return r
        r = Ref(kind, name)
        self.items.append(r)
        return r


def collect(st: Any) -> Refs:
    """The task's authorization references from its journal."""
    out = Refs()
    intents: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, dict[str, Any]] = {}
    for o in st.ops.values():
        if o.get("intent"):
            intents[o["op"]] = o["intent"]
        if o.get("outcome"):
            outcomes[o["op"]] = o["outcome"]
    checks: dict[str, dict[str, Any]] = {}
    answers: dict[str, dict[str, Any]] = {}
    requests: list[dict[str, Any]] = []
    for r in st.records:
        k, d = r["kind"], r.get("data") or {}
        if k == "auth.ref":
            tid = str(d.get("tool_use_id") or "")
            if d.get("via") == "check" and tid:
                checks.setdefault(tid, r)
            elif d.get("via") == "answer" and tid:
                answers[tid] = r
            elif d.get("via") == "request":
                requests.append(r)
        elif k == "tool.intent":
            m = d.get("mode")
            if m in MODES:
                if out.modes and out.modes[-1][0] == m:
                    out.modes[-1] = (m, out.modes[-1][1], r["seq"])
                else:
                    out.modes.append((m, r["seq"], r["seq"]))
    # A permission dialog the classic hook saw: its call is the latest intent
    # of the same session, tool and input before it.
    by_input: dict[tuple[Any, Any, Any], list[tuple[int, str]]] = {}
    for op, it in intents.items():
        idd = it.get("data") or {}
        if idd.get("input_sha"):
            by_input.setdefault((it.get("session"), idd.get("tool"), idd.get("input_sha")), []).append((it["seq"], op))
    for lst in by_input.values():
        lst.sort()
    for rq in requests:
        d = rq.get("data") or {}
        cands = [op for seq, op in by_input.get((rq.get("session"), d.get("tool"), d.get("input_sha")), [])
                 if seq < rq["seq"]] if d.get("input_sha") else []
        tid = cands[-1] if cands else ""
        if tid and tid in checks:
            continue        # the mod saw the same ask (with more)
        key = tid or f"pr:{rq['seq']}"
        checks[key] = {"seq": rq["seq"], "session": rq.get("session"), "kind": "auth.ref",
                       "data": {"via": "request", "decision": "ask", "tool": d.get("tool")}}

    def answer_of(tid: str) -> str:
        oc = outcomes.get(tid)
        od = (oc or {}).get("data") or {}
        if oc is not None and not od.get("denied"):
            return "approved"           # it ran: PostToolUse or PostToolUseFailure came
        a = ((answers.get(tid) or {}).get("data") or {}).get("answered")
        if a in ("deny", "error") or od.get("denied"):
            return "declined"
        return "no answer recorded"

    for tid, r in sorted(checks.items(), key=lambda x: x[1]["seq"]):
        d = r.get("data") or {}
        tool = str(d.get("tool") or ((intents.get(tid) or {}).get("data") or {}).get("tool") or "?")
        dec = str(d.get("decision") or "?")
        seq = r["seq"]
        final = answer_of(tid) if dec == "ask" else dec
        if d.get("gateway"):
            names = d.get("rights") or []
            for n in names:
                ref = out.get("gateway", n)
                ref.how = ref.how or str(d.get("gateway"))
                ref.add(final if dec == "ask" else ("off" if d["gateway"] == "off" else
                                                     "denied (in a job)" if d["gateway"] == "job" else dec), tool, seq)
            if not names:
                out.get("hook", "gateway approval").add(final, tool, seq)
            continue
        if d.get("rule"):
            # (an ask rule answered Yes is "approved", and still an ask rule then)
            out.get("rule", str(d["rule"])).add(final, tool, seq, verdict=dec)
        elif d.get("hook"):
            out.get("hook", str(d["hook"])).add(final, tool, seq)
        elif dec == "ask":
            out.get("ask", tool).add(final, tool, seq)
        elif dec == "deny":
            out.get("hook", "permission check").add("deny", tool, seq)
    for op, oc in outcomes.items():
        od = oc.get("data") or {}
        if od.get("denied") == "permission" and op not in checks:
            out.get("classifier", str(od.get("tool") or "?")).add("denied", str(od.get("tool") or "?"), oc["seq"])
    # Accounts the task's gateway calls named (the http and download tools'
    # `account`): the gateway hands their credential on its own decision.
    for op, it in sorted(intents.items(), key=lambda x: x[1]["seq"]):
        idd = it.get("data") or {}
        acc = (idd.get("input") or {}).get("account")
        if isinstance(acc, str) and NAME_RE.fullmatch(acc) and str(idd.get("tool") or "").startswith("mcp__"):
            oc = outcomes.get(op)
            od = (oc or {}).get("data") or {}
            dec = "used" if oc is not None and od.get("ok") else ("refused" if oc is not None else "no result recorded")
            out.get("account", acc).add(dec, str(idd.get("tool") or "").rsplit("__", 1)[-1], it["seq"])
    waiting: dict[str, dict[str, Any]] = {}
    for r in getattr(st, "gw_events", []):
        d = r.get("data") or {}
        if d.get("kind") == "rights.changed" and isinstance(d.get("entry"), str) and NAME_RE.fullmatch(d["entry"]):
            out.get("rights", d["entry"]).add(str(d.get("change") or "changed"), "", r["seq"])
            continue
        if d.get("kind") == "auth.waiting" and d.get("request"):
            waiting[str(d["request"])] = r
        elif d.get("kind") == "auth.supplied" and d.get("request"):
            w = waiting.pop(str(d["request"]), None)
            kind = str(((w or {}).get("data") or {}).get("dialog") or "dialog")
            out.get("dialog", sanitize.clip(kind, 30)).add(str(d.get("outcome") or "?"), "", r["seq"])
    for req, w in waiting.items():
        kind = str((w.get("data") or {}).get("dialog") or "dialog")
        out.get("dialog", sanitize.clip(kind, 30)).add("no answer recorded", "", w["seq"])
    return out


def summary_lines(refs: Refs) -> list[str]:
    """The recovery summary's "Authorizations" lines (observed)."""
    L = []
    for r in refs.items[:30]:
        tools = f" ({', '.join(r.tools)})" if r.tools and r.kind not in ("ask", "classifier") else ""
        L.append(f"- {r.label()}: {r.decided()}{tools} (observed: "
                 + ("the gateway's events" if r.kind in ("dialog", "rights") else
                    "the gateway tools' calls" if r.kind == "account" else
                    "the PreToolUse payloads" if r.kind == "classifier" else "Claude Code's permission check")
                 + f", {r.seq_text()})")
    if refs.modes:
        L.append("- permission mode: " + ", ".join(f"{m} (seq {a}-{b})" if a != b else f"{m} (seq {a})"
                                                   for m, a, b in refs.modes[-6:]) + " (observed)")
    return L


# -- before a resume ------------------------------------------------------------------

def settings_files(cwd: str, root: str = "") -> list[Path]:
    """The settings files Claude Code reads permission rules from (user,
    project, project-local); the managed tier isn't read here."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        user = Path(cfg) / "settings.json"
    else:
        try:
            home = pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:
            home = os.path.expanduser("~")
        user = Path(home) / ".claude" / "settings.json"
    out = [user]
    for base in dict.fromkeys(x for x in (root, cwd) if x):
        out += [Path(base) / ".claude" / "settings.json", Path(base) / ".claude" / "settings.local.json"]
    return out


def settings_rules(files: list[Path]) -> tuple[dict[str, str], list[str]]:
    """(rule -> its effective list now: deny over ask over allow, the files
    that could be read)."""
    found: dict[str, set[str]] = {}
    read = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                doc = json.loads(fh.read(2_000_000))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        read.append(str(f))
        perms = doc.get("permissions") if isinstance(doc, dict) else None
        if not isinstance(perms, dict):
            continue
        for lst in DECISIONS:
            for rule in perms.get(lst) or []:
                if isinstance(rule, str):
                    found.setdefault(sanitize.clip(sanitize.scrub(rule), 200), set()).add(lst)
    eff = {r: ("deny" if "deny" in s else "ask" if "ask" in s else "allow") for r, s in found.items()}
    return eff, read


def _rights_of(h: dict[str, Any]) -> dict[str, Any] | None:
    rights = h.get("rights")
    return rights if isinstance(rights, dict) else None


def _right_word(v: Any) -> str:
    return {True: "allow", "true": "allow", "allow": "allow", "ask": "ask", False: "off", "false": "off",
            "off": "off", "deny": "off"}.get(v if not isinstance(v, str) else v.lower(), str(v))


def _right_now(rights: dict[str, Any] | None, verb: str) -> str:
    """The right's value now from an entry's `rights` (by its own name, or
    its action), '' when the snapshot doesn't say."""
    if rights is None:
        return ""
    for k in (verb, VERB_ACTION.get(verb, "")):
        if k and k in rights:
            return _right_word(rights[k])
    return ""


ACCOUNT_BAD = ("expired", "revoked", "reauthentication_required", "missing")


@dataclass
class Check:
    holds: list[str] = field(default_factory=list)       # unchanged: still valid
    flagged: list[str] = field(default_factory=list)     # revoked or changed
    said: list[str] = field(default_factory=list)        # one-time answers, per-call deciders
    unknown: list[str] = field(default_factory=list)     # couldn't be compared now


def check(refs: Refs, snap_hosts: list[dict[str, Any]] | None, gw_why: str, host_states: dict[str, Any],
          cwd: str, root: str = "", snap_accounts: list[dict[str, Any]] | None = None) -> Check:
    """Each reference against now. `snap_hosts` / `snap_accounts`: the
    gateway's hosts and accounts now (cleaned; None when it couldn't be
    asked, `gw_why` says why; accounts None too for a snapshot that lists none);
    `host_states`: name -> hosts.HostNow (identity, authentication)."""
    out = Check()
    rules_now: dict[str, str] | None = None
    read: list[str] = []
    by_name = {h.get("name"): h for h in snap_hosts or []}
    by_acc = {a.get("account"): a for a in snap_accounts or []}
    # what changed on an entry after the task used it (rights.changed)
    narrowed: dict[str, tuple[str, int]] = {}
    for r in refs.items:
        if r.kind == "rights":
            for d in ("removed", "narrowed"):
                if d in r.decisions:
                    narrowed[r.name] = (d, max(r.seqs) if r.seqs else 0)
                    break
    for r in refs.items:
        used_ok = any(k in r.decisions for k in ("allow", "approved", "used"))
        what = f"{r.label()} ({r.decided()})"
        if r.kind == "gateway":
            verb, host = right_host(r.name)
            entry = by_name.get(host) or (next((x for x in snap_hosts or [] if x.get("local")), None)
                                          if host == "localhost" else None)
            acc = by_acc.get(host) if entry is None else None
            rec = entry or acc
            if not used_ok and (snap_hosts is None or _right_now(_rights_of(rec or {}), verb) == ""):
                out.said.append(f"{what}: refused then; the gateway decides again at each use (ask the person if it "
                                "should be on)")
                continue
            if snap_hosts is None:
                out.unknown.append(f"{what}: not compared ({gw_why}); the gateway checks it again at each use")
                continue
            if rec is None:
                out.flagged.append(f"{what}: REVOKED: {host} is no longer in the gateway's configuration (as a "
                                   "host or an account)")
                continue
            problems = []
            hn = host_states.get(host)
            if hn is not None and getattr(hn, "changed", ""):
                problems.append(f"the host's identity changed ({hn.changed})")
            if hn is not None:
                for b in hn.blocked():
                    if "auth" in b or "LOCKED" in b:
                        problems.append(f"its {b} now")
            if acc is not None and acc.get("state") in ACCOUNT_BAD:
                problems.append(f"the account is {acc['state']} now")
            if host in narrowed:
                problems.append(f"its rights were {narrowed[host][0]} at seq {narrowed[host][1]} (the gateway's event)")
            now_w = _right_now(_rights_of(rec), verb)
            then_w = "ask" if r.how == "ask" else "off" if r.how == "off" else ""
            if now_w and then_w and now_w != then_w:
                problems.insert(0, "REVOKED: it is off now" if used_ok and now_w == "off" else
                                f"CHANGED: {then_w} then, {now_w} now")
            if problems:
                out.flagged.append(f"{what}: " + "; ".join(problems))
            elif not now_w:
                # only the host's presence was compared: not the right itself
                out.unknown.append(f"{what}: not compared: " + (
                    f"the gateway's snapshot lists no rights for {host}"
                    if _rights_of(rec) is None else
                    f"the gateway's snapshot doesn't list this right for {host}")
                    + f"; {host} is still configured, and the gateway checks the right again at each use")
            else:
                out.holds.append(f"{what}: unchanged")
        elif r.kind == "account":
            if snap_hosts is None:
                out.unknown.append(f"{what}: not compared ({gw_why}); the gateway checks it again at each use")
            elif snap_accounts is None:
                out.unknown.append(f"{what}: not compared (the gateway's snapshot lists no accounts)")
            elif r.name not in by_acc:
                out.flagged.append(f"{what}: REVOKED: the account is no longer in the gateway's configuration")
            else:
                a = by_acc[r.name]
                cred = _right_now(_rights_of(a), "credential")
                problems = []
                if a.get("state") in ACCOUNT_BAD:
                    problems.append(f"its state is {a['state']} now (the gateway asks for it again)")
                if cred == "off":
                    problems.append("its credential right is deny now")
                if r.name in narrowed:
                    problems.append(f"its rights were {narrowed[r.name][0]} at seq {narrowed[r.name][1]}")
                if problems:
                    out.flagged.append(f"{what}: " + "; ".join(problems))
                else:
                    out.holds.append(f"{what}: state {a.get('state') or 'not reported'}"
                                     + (f", credential {cred}" if cred else "") + " now")
        elif r.kind == "rule":
            if rules_now is None:
                rules_now, read = settings_rules(settings_files(cwd, root))
            now = rules_now.get(r.name)
            # its list then is the verdict's decision, never what came of the call
            was = r.verdict or ("allow" if "allow" in r.decisions else "deny" if "deny" in r.decisions else "")
            if now is None:
                where = ", ".join(sanitize.clip_path(x, 60) for x in read) or "no settings file could be read"
                out.flagged.append(f"{what}: no longer in the settings checkpoint can read ({where}): Claude Code "
                                   "will decide again (an ask, or the rule from another tier)")
            elif not was:
                out.unknown.append(f"{what}: not compared (its decision then wasn't recorded); {now} now in your "
                                   "settings")
            elif now != was:
                out.flagged.append(f"{what}: CHANGED: {was} then, {now} now in your settings"
                                   + (" (revoked)" if was == "allow" and now == "deny" else ""))
            else:
                out.holds.append(f"{what}: unchanged in your settings")
        elif r.kind == "ask":
            out.said.append(f"{what}: each answer covered that call only; a new call is asked again")
        elif r.kind in ("hook", "classifier"):
            out.said.append(f"{what}: decided again at each call")
        elif r.kind == "dialog":
            out.said.append(f"{what}: the gateway asks again when it needs the credential")
        elif r.kind == "rights":
            out.said.append(f"{what} while the task ran (the gateway's events)")
    return out
