"""Recovery (PLAN-v4 phase 3): reconciliation before a resume, checkpoint
management (list, diff, select, complete, cancel, export, retention) and
lineage. Deterministic, from the journal, the files and the process table;
no model is asked, and nothing here ever writes the user's files, the
repository, or Claude Code's transcripts.

Reconciliation compares what a checkpoint (or, without one, the journal's
latest record of each file) saved with what is there now:

- files: the saved hashes against the files now. A file whose hash differs
  and that no edit the task recorded explains was changed outside the task
  (the user, another process): it is listed so the work goes on from it,
  never overwritten or reverted.
- git: HEAD moved (a descendant of the saved one, or not), the branch, and
  files dirty now that weren't.
- validations: a result whose file hashes or HEAD no longer match is STALE.
- instructions: what was in force at the checkpoint, what was said since,
  and the message that resumes it.
- operations: every interrupted one in one of five classes, from the
  journal, the transcript (supplementary), the process table and the own
  job runner: never dispatched (denied), confirmed completed, still running,
  confirmed failed or canceled, outcome unknown. An unknown one that may have
  changed something outside the repository (a push, a deploy, a remote
  command) is investigated first and never repeated blindly; nothing here
  ever says to redo a stage from the start.
- lineage: the task's original session (`claude --resume <id>`, and whether
  it is alive elsewhere), and other live sessions on the same task.
- the gateway (CONTRACT-v1, when the task used it): its jobs' states asked of
  the gateway now (running, waiting, suspended / exited / unknown); with the
  gateway gone, or a new gateway epoch that no longer lists a job, the job is
  unknown, never done. The machine the gateway runs on against the one the
  task's jobs ran on, and the hosts the task used against its host list.
"""
from __future__ import annotations

import bisect
import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import authz, capture, gateway, jobs, journal, procs, sanitize, state, summary
from . import hosts as hosts_mod

CLASS_TEXT = {"unknown": "outcome unknown", "running": "still running", "failed": "confirmed failed or canceled",
              "never": "never dispatched", "completed": "confirmed completed"}
CLASS_ORDER = ("unknown", "running", "failed", "never", "completed")
NEVER_SHOWN = 5          # denied calls listed (the most recent)
OPS_MAX = 200            # operations listed at most, even in the full text
SUMMARY_FLOOR = 1200     # the resume context keeps at least this much of the summary (when it has one)

# Commands whose effect may land outside the repository: a remote, a
# registry, a cluster, a database, the system. A start without a recorded
# result of one of these is investigated, never repeated blindly.
EXTERNAL_RE = re.compile(r"""(?x)(?:^|[\s;&|(`'"]|/)(?:
    (?P<push>git\s+(?:-C\s+\S+\s+)?(?:push|send-email)|git\s+(?:-C\s+\S+\s+)?remote\s+(?:set-url|add|remove))
  | (?P<remote>ssh|scp|sftp|rsync\s.*\S+:|mosh)\b
  | (?P<http>curl\s.*(?:-X\s*(?:POST|PUT|PATCH|DELETE)|--request\s+(?:POST|PUT|PATCH|DELETE)|\s-d\b|--data|-F\b|--form|-T\b|--upload-file)
      |wget\s.*--(?:post|method)|https?\s+(?:POST|PUT|PATCH|DELETE)\b|xh\s+(?:post|put|patch|delete)\b)
  | (?P<deploy>kubectl\s+(?:apply|create|delete|patch|replace|scale|rollout|set|edit|annotate|label|drain)
      |helm\s+(?:install|upgrade|uninstall|rollback)|terraform\s+(?:apply|destroy|import)|pulumi\s+(?:up|destroy)
      |ansible-playbook|docker\s+(?:push|compose\s+up|run|stack\s+deploy)|podman\s+push|fly\s+deploy|flyctl\s+deploy
      |vercel|netlify\s+deploy|heroku\b|gcloud\b|aws\b|az\s|doctl\b|[\w-]*deploy[\w-]*(?:\.sh|\.py|\.rb|\.js)?(?![\w.]))
  | (?P<publish>npm\s+publish|yarn\s+publish|pnpm\s+publish|twine\s+upload|cargo\s+publish|gem\s+push|pixi\s+upload
      |gh\s+(?:release|pr|issue|repo|api|workflow|secret|variable)\s+(?:create|merge|close|edit|delete|upload|run|set|comment|-X)
      |glab\s+\w+\s+(?:create|merge|close|delete))
  | (?P<db>psql\s.*(?:-c|--command|-f)|mysql\s.*(?:-e|--execute)|mongosh?\s.*--eval|redis-cli\s+\S+|sqlite3\s+\S+\s+["']?(?:insert|update|delete|drop|create|alter))
  | (?P<system>sudo\b|systemctl\s+(?:start|stop|restart|enable|disable)|launchctl\s+(?:load|unload|bootstrap|kickstart)|crontab\b)
)""", re.I)

EXTERNAL_HINT = {
    "push": "whether the push landed: `git fetch` then `git status -sb`, or `git ls-remote <remote> <branch>`",
    "remote": "the remote host's state (the files or services the command changes there)",
    "http": "whether the service applied the request (look the resource up; don't send it again)",
    "deploy": "the target's current state (`kubectl get`, `helm status`, `terraform plan`, the service's console)",
    "publish": "whether that version or item already exists (the registry, the PR or release list)",
    "db": "whether the change is already in the database (query it)",
    "system": "the system state it changes (the service, the file, the schedule)",
}

READ_ONLY_MCP = re.compile(r"(?:^|_)(?:get|list|read|search|status|tail|show|describe|find|query|fetch|view|check|note)"
                           r"(?:_|$)", re.I)


def external_effect(tool: str, inp: dict[str, Any] | None) -> str:
    """'' or the kind of effect outside the repository a call may have had."""
    inp = inp or {}
    if tool == "Bash" or tool.endswith("__job_start"):
        m = EXTERNAL_RE.search(str(inp.get("command") or ""))
        return next((k for k, v in m.groupdict().items() if v), "") if m else ""
    if tool.startswith("mcp__") and not READ_ONLY_MCP.search(tool.rsplit("__", 1)[-1]):
        return "mcp"
    return ""


def _clock(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "?"


def _h(v: Any) -> str:
    return summary._h(v)


def _q(s: str, n: int = 100, path: bool = False) -> str:
    """A command (clipped at its end) or a path (clipped in the middle, so
    its file name stays) in backquotes."""
    s = str(s or "").replace("\n", " ")
    return "`" + (sanitize.clip_path(s, n) if path else sanitize.clip(s, n)) + "`"


def cli_reconcile(task: str) -> str:
    """The terminal command that prints a task's whole reconciliation."""
    return state.cli("reconcile", task)


def cli_summary(task: str) -> str:
    """The terminal command that prints a task's whole recovery summary."""
    return state.cli("summary", task, "--limit", "100000")


# -- finding checkpoints ------------------------------------------------------------

def checkpoints(st: summary.TaskState) -> list[dict[str, Any]]:
    return list(st.checkpoints)


def find_checkpoint(ref: str, task: str = "") -> tuple[str, dict[str, Any] | None]:
    """(task, checkpoint record) for a checkpoint id or a unique prefix of
    one, in `task` or any task (by its sidecar file, then the journal)."""
    ref = (ref or "").strip()
    if not ref:
        return task, None
    tasks = [task] if task else []
    if not tasks:
        hits = sorted(state.p("journal").glob(f"*/checkpoints/{state.safe_id(ref)}*.json"))
        tasks = sorted({h.parent.parent.name for h in hits})
    found: list[tuple[str, dict[str, Any]]] = []
    for t in tasks:
        for r in journal.read(t).records:
            if r["kind"] == "checkpoint" and str((r.get("data") or {}).get("id") or "").startswith(ref):
                found.append((t, r))
    exact = [f for f in found if (f[1].get("data") or {}).get("id") == ref]
    pick = exact or found
    if len(pick) == 1 or (pick and len({(f[1].get("data") or {}).get("id") for f in pick}) == 1):
        return pick[-1]
    return (tasks[0] if len(tasks) == 1 else task), None


def checkpoint_text(task: str, cid: str) -> str:
    """The summary text saved beside a checkpoint ('' when gone)."""
    d = state.read_json(journal.jdir(task) / "checkpoints" / f"{state.safe_id(cid)}.json", {}) or {}
    return str(d.get("summary") or "")


# -- reconciliation -----------------------------------------------------------------

@dataclass
class Op:
    op: str
    cls: str
    tool: str
    what: str
    why: str
    seq: int
    session: str = ""
    external: str = ""
    check: str = ""
    after_cp: bool = False
    end_seq: int = 0        # what ended it (its outcome, a kill, a later report), else its intent
    hosts: list[str] = field(default_factory=list)   # the gateway hosts it named (hosts.call_hosts)

    def q(self, n: int = 100) -> str:
        """What it did, in backquotes (an edit's path keeps its file name)."""
        return _q(self.what, n, path=self.tool in capture.EDIT_TOOLS)


@dataclass
class Recon:
    task: str
    base: dict[str, Any]
    files: list[dict[str, Any]] = field(default_factory=list)
    git: dict[str, Any] = field(default_factory=dict)
    validations: list[dict[str, Any]] = field(default_factory=list)
    instructions: dict[str, Any] = field(default_factory=dict)
    ops: list[Op] = field(default_factory=list)
    jobs: list[dict[str, Any]] = field(default_factory=list)
    lineage: dict[str, Any] = field(default_factory=dict)
    gateway: dict[str, Any] = field(default_factory=dict)
    hosts: list[Any] = field(default_factory=list)       # hosts.HostNow: each host the task used, now
    authz: Any = None                                     # authz.Check: its authorizations against now
    problems: list[str] = field(default_factory=list)
    table_ok: bool = True
    # Unknown outcomes with a possible external effect that an earlier
    # resume already reported (the work went on after it): one line in
    # "Check first", in full in the summary.
    earlier: list[Op] = field(default_factory=list)
    horizon: int = 0        # seq of that earlier reconciliation (0: none)
    left: int = 0           # interrupted operations left to the summary (resolved, or reported before)

    def counts(self) -> dict[str, Any]:
        c: dict[str, Any] = {k: 0 for k in CLASS_ORDER}
        for o in self.ops:
            c[o.cls] += 1
        for j in self.jobs:
            c[j["cls"]] += 1
        c["external_unknown"] = sum(1 for o in self.ops if o.cls in ("unknown", "running") and o.external)
        c["earlier_external_unknown"] = len(self.earlier)
        c["left_to_summary"] = self.left
        c["files_changed_outside"] = sum(1 for f in self.files if f["how"] == "outside")
        c["files_changed_by_task"] = sum(1 for f in self.files if f["how"] == "task")
        c["validations_stale"] = sum(1 for v in self.validations if v.get("status") == "stale")
        c["head_moved"] = bool(self.git.get("head_moved"))
        c["hosts_changed"] = sum(1 for h in self.hosts if h.changed)
        c["hosts_blocked"] = sum(1 for h in self.hosts if h.blocked())
        c["authz_flagged"] = len(self.authz.flagged) if self.authz else 0
        return c

    def _host_for(self, name_or_id: str) -> Any:
        for h in self.hosts:
            if name_or_id and name_or_id in (h.use.name, h.use.host):
                return h
        return None

    def host_mark(self, o: "Op") -> str:
        """'; needs host X: <what the gateway reports>' for an operation whose
        host is unreachable, unauthenticated, gone or of changed identity
        now (or whose state is unknown), else ''."""
        for n in o.hosts:
            h = self._host_for(n)
            if h is None:
                continue
            if h.changed:
                return f"; needs host {h.use.label}, whose IDENTITY changed ({h.changed})"
            if not h.listed:
                return f"; needs host {h.use.label}, no longer in the gateway's host list"
            b = h.blocked()
            if b:
                return f"; needs host {h.use.label}: {', '.join(b)} now (the gateway)"
            if h.now is None:
                return f"; needs host {h.use.label}: its state now is unknown ({h.why_unknown})"
        return ""

    def check_items(self) -> list[tuple[int, str]]:
        """What to check before going on, riskiest first, each with its rank
        for when the text must be shorter: 0 is never cut (an unknown outcome
        that may have changed state outside the repository), 1 goes last
        (what runs now, files changed outside, stale results, the repository,
        other sessions), 2 first (other unknowns and interruptions)."""
        out: list[tuple[int, str]] = []
        hinted: set[str] = set()
        for o in self.ops:
            if o.cls == "unknown" and o.external:
                if o.external in hinted:
                    out.append((0, f"{o.tool} {o.q()} (op {o.op}): outcome unknown too, and it may have changed state "
                                   f"outside this repository ({o.external}): check it the same way; do not run it again "
                                   "until you know."))
                    continue
                hinted.add(o.external)
                out.append((0, f"{o.tool} {o.q()} (op {o.op}): its outcome is unknown and it may have changed "
                               f"state outside this repository. Check {EXTERNAL_HINT.get(o.external, 'that state')} "
                               "before deciding anything; do not run it again until you know (a second run could "
                               "repeat its effect)."))
        if self.earlier:
            names = ", ".join(o.q(60) for o in self.earlier[:3])
            more = f" and {len(self.earlier) - 3} more" if len(self.earlier) > 3 else ""
            out.append((0, f"An earlier resume (seq {self.horizon}) already reported {len(self.earlier)} operation(s) "
                           f"whose outcome is still unknown and that may have changed state outside this repository: "
                           f"{names}{more} (the recovery summary lists them under In flight). If they weren't checked "
                           "then, check them now; do not run one again until you know whether it took effect."))
        for o in self.ops:
            if o.cls == "running":
                out.append((1, f"{o.tool} {o.q()} is still running ({o.why}): don't start a second copy; wait for "
                               "it, or stop it first."))
        for j in self.jobs:
            sup = j.get("where") or "checkpoint's job runner"
            if j["cls"] == "running":
                out.append((1, f"job {j['name']} ({j['id']}) is still running ({sup}): job_status / "
                               "job_tail before doing its work again."))
            elif j["cls"] == "unknown":
                out.append((2 if not j.get("gateway") else 1,
                            f"job {j['name']} ({j['id']}, {sup}): {j['why']}: look at its output (job_tail) and its "
                            "effects before running it again; do not treat it as done."))
        gw = self.gateway
        if gw.get("machine_changed"):
            out.append((1, f"The gateway now runs on another machine ({gw.get('machine_now')}) than the one this "
                           f"task's gateway jobs ran on ({gw.get('machine_then')}): their hosts, files and processes "
                           "aren't here; don't take this machine's state for theirs."))
        if gw.get("unavailable") and any(j.get("gateway") for j in self.jobs):
            out.append((1, f"The gateway isn't available ({gw.get('why')}): the state of this task's gateway jobs is "
                           "unknown until it is back (/check:doctor)."))
        for h in gw.get("hosts_gone") or []:
            out.append((1, f"Host {h} that this task's jobs used is no longer in the gateway's host list."))
        for h in self.hosts:
            if h.changed:
                out.append((0, f"Host {h.use.label}: {h.changed}. Stop and check: run nothing on it, and don't count "
                               "on what this task did there, until the person has confirmed the host in the gateway's "
                               "dialog (or checked it); it may not be the machine the task worked on."))
            elif not h.listed:
                out.append((1, f"Host {h.use.label}, which this task used, is no longer in the gateway's host list "
                               "(removed or renamed): nothing can run there through the gateway now."))
            elif h.blocked():
                need = [o for o in self.ops if any(n in (h.use.name, h.use.host) for n in o.hosts)
                        and o.cls in ("unknown", "running", "failed")]
                need += [j for j in self.jobs if j.get("host") and j.get("host") == h.use.host and j["cls"] != "completed"]
                what = ", ".join((f"{o.tool} {o.q(50)}" if isinstance(o, Op) else f"job {o['name']}") for o in need[:4])
                more = f" and {len(need) - 4} more" if len(need) > 4 else ""
                out.append((1, f"Host {h.use.label}: {', '.join(h.blocked())} now (the gateway's snapshot)"
                               + (f": {len(need)} interrupted operation(s) need it ({what}{more}); their outcome can't "
                                  "be checked there until that changes. Don't run them again meanwhile, nor elsewhere."
                                  if need else ": work that needs it waits until that changes.")))
        for f in (self.authz.flagged if self.authz else []):
            out.append((1, f"Authorization changed since the task used it: {f}. Don't rely on it: an operation that "
                           "needs it gets the person's OK again (Claude Code's or the gateway's own prompt)."))
        for o in self.ops:
            if o.cls == "unknown" and not o.external:
                out.append((2, f"{o.tool} {o.q()} (op {o.op}): outcome unknown; "
                               f"{o.check or 'check its effects before running it again'}"))
        for o in self.ops:
            if o.cls == "failed" and o.check:
                out.append((2, f"{o.tool} {o.q()}: {o.why}; {o.check}"))
        n_out = [f for f in self.files if f["how"] in ("outside", "deleted")]
        if n_out:
            names = ", ".join(sanitize.clip_path(f["path"], 80) for f in n_out[:4])
            more = f" and {len(n_out) - 4} more" if len(n_out) > 4 else ""
            out.append((1, f"{len(n_out)} file(s) changed outside this task since {self.base_name()}: {names}{more} "
                           "(listed under Files): they are the current truth; re-read them before editing, and never "
                           "revert them."))
        st_ = [v for v in self.validations if v.get("status") == "stale"]
        if st_:
            cmds = list(dict.fromkeys(str(v.get("command") or "") for v in st_))
            names = ", ".join(_q(c, 60) for c in cmds[:3])
            more = f" and {len(cmds) - 3} more" if len(cmds) > 3 else ""
            out.append((1, f"{len(st_)} validation result(s) are STALE: {names}{more} (why: under Validation): rerun "
                           "before relying on them."))
        if self.git.get("head_moved") or self.git.get("branch_changed"):
            out.append((1, "The repository moved (see Git): check what the new commits or the other branch change."))
        lin = self.lineage
        if lin.get("original_alive"):
            out.append((1, f"The task's original session {lin['original']} is still running in another Claude process "
                           f"(pid {lin.get('original_pid')}): continue there, or make sure only one session works on "
                           "it."))
        if lin.get("concurrent"):
            out.append((1, "Other live sessions record into this task too: " + ", ".join(
                f"{c['session'][:8]} (pid {c.get('pid')})" for c in lin["concurrent"][:4])
                + ". Coordinate: two sessions changing the same files overwrite each other."))
        return out

    def check_first(self) -> list[str]:
        """What to check before going on, riskiest first."""
        return [t for _r, t in self.check_items()]

    def base_name(self) -> str:
        b = self.base
        return f"checkpoint {b['id']}" if b.get("id") else "the last recorded state"

    def short(self) -> str:
        """One paragraph for the person (an ack, /check:resume's result)."""
        c = self.counts()
        parts = []
        if c["files_changed_outside"]:
            parts.append(f"{c['files_changed_outside']} file(s) changed outside the task (kept, nothing overwritten)")
        if c["files_changed_by_task"]:
            parts.append(f"{c['files_changed_by_task']} changed by the task since")
        if c["head_moved"]:
            parts.append("HEAD moved")
        if self.git.get("branch_changed"):
            parts.append("the branch changed")
        if c["validations_stale"]:
            parts.append(f"{c['validations_stale']} validation result(s) stale")
        opsd = [f"{c[k]} {CLASS_TEXT[k]}" for k in CLASS_ORDER if c[k]]
        if opsd:
            parts.append("operations: " + ", ".join(opsd))
        if c["external_unknown"]:
            parts.append(f"{c['external_unknown']} may have changed state outside the repository: check before any retry")
        if self.lineage.get("original_alive"):
            parts.append(f"its original session {self.lineage['original'][:8]} is alive elsewhere")
        if c["hosts_changed"]:
            parts.append(f"{c['hosts_changed']} host(s) of changed identity: stop and check")
        if c["hosts_blocked"]:
            parts.append(f"{c['hosts_blocked']} host(s) unreachable or unauthenticated now")
        if c["authz_flagged"]:
            parts.append(f"{c['authz_flagged']} authorization(s) revoked or changed")
        b = self.base
        when = f" (saved {_clock(b.get('ts'))}, seq {b.get('seq_to')})" if b.get("id") else ""
        return (f"Reconciled task {self.task} against {self.base_name()}{when}: "
                + ("; ".join(parts) if parts else "nothing changed, nothing in flight") + ".")

    def op_items(self) -> list[tuple[int, str]]:
        """The interrupted operations, by class, with their rank (as
        check_items: 0 an unknown outcome with a possible external effect,
        never cut; 1 what still runs; 2 the rest)."""
        out: list[tuple[int, str]] = []
        for cls in CLASS_ORDER:
            for o in [o for o in self.ops if o.cls == cls]:
                tag = f"[{CLASS_TEXT[cls]}]"
                ext = f"; may have changed state outside the repository ({o.external})" if o.external and cls in (
                    "unknown", "running") else ""
                aft = " (after the checkpoint)" if o.after_cp else ""
                rank = 0 if cls == "unknown" and o.external else 1 if cls == "running" else 2
                out.append((rank, f"- {tag} {o.tool} {o.q()}{aft}: {o.why}{ext}{self.host_mark(o)} (seq {o.seq}, "
                                  f"op {o.op})"))
        for j in self.jobs:
            where = j.get("where") or "checkpoint's job runner"
            out.append((1 if j["cls"] == "running" else 2,
                        f"- [{CLASS_TEXT[j['cls']]}] job {j['name']} ({j['id']}, {where}): {j['why']}"))
        return out[:OPS_MAX]

    def text(self, summary_text: str | Callable[[int], str] = "", limit: int = 9000, summary_max: int = 3500) -> str:
        """The resume context for the model, in about `limit` characters for
        the whole: the reconciliation, then the recovery summary. "Check
        first" and "Interrupted operations" are never left out: when it must
        be shorter, their items are cut (every unknown outcome that may have
        changed state outside the repository stays, then what still runs,
        then the rest, and "+N more" says what was cut), the other sections
        lose items from the end (a section is left out, and named, only when
        not even its first item fits), and the summary (a text, or a function
        that builds one in so many characters) gets what is left, at most
        `summary_max` and at least SUMMARY_FLOOR: it is cut before anything
        of the reconciliation."""
        b = self.base
        full = cli_reconcile(self.task)
        head = (f"Reconciled before resuming task {self.task}, against {self.base_name()}"
                + (f" (saved {_clock(b.get('ts'))}, journal seq {b.get('seq_from')}-{b.get('seq_to')}"
                   f"{', reason: ' + sanitize.clip(b.get('reason'), 80) if b.get('reason') else ''})" if b.get("id") else
                   " (no checkpoint: the journal's latest record of each file)")
                + ". From the journal, the files, git and the process table now; nothing was redone, reverted "
                  "or overwritten.")
        if b.get("selected"):
            head += (" You chose this checkpoint: work recorded after it is listed as such; it was not undone (the "
                     "files are as they are now).")
        if self.left:
            head += (f" {self.left} older interrupted operation(s) (resolved since, or reported at an earlier resume) "
                     "are left to the recovery summary.")
        foot = ("\nGo on from the plan once the checks above are done. Treat an unknown outcome as unknown "
                "(not as failed, not as done), and a start as a start; never repeat an operation with an "
                "external effect without first finding out whether it already took effect.")
        # (name, [(rank, line)], placeholder when empty, required)
        secs: list[tuple[str, list[tuple[int, str]], str, bool]] = []
        secs.append(("Check first", self.check_items(),
                     "(nothing: no unknown outcomes, nothing running, no changed files or stale results)", True))
        secs.append(("Interrupted operations", self.op_items(), "(none: every recorded call has an outcome)", True))
        secs.append(("Files", [(3, f"- {sanitize.clip_path(f['path'], 160)}: {f['text']}") for f in self.files[:200]],
                     "(no saved file hashes to compare)", False))
        g = self.git
        if g:
            L = []
            if g.get("head_moved"):
                L.append(f"- HEAD moved {_h(g.get('saved_head'))[:10]} -> {_h(g.get('head'))[:10]}"
                         f" ({g.get('relation') or 'relation unknown'})")
            elif g.get("head"):
                L.append(f"- HEAD unchanged ({_h(g.get('head'))[:10]})")
            if g.get("branch_changed"):
                L.append(f"- branch {g.get('saved_branch') or '?'} -> {g.get('branch') or '?'}")
            if g.get("newly_dirty"):
                L.append("- dirty now, not at the checkpoint: " + ", ".join(
                    sanitize.clip_path(x, 100) for x in g["newly_dirty"][:10]))
            if g.get("cleaned"):
                L.append("- dirty at the checkpoint, clean now (committed or reverted): " + ", ".join(
                    sanitize.clip_path(x, 100) for x in g["cleaned"][:10]))
            if g.get("gone"):
                L.append(f"- the repository {g.get('root')} is gone")
            secs.append(("Git", [(3, x) for x in (L or ["- unchanged"])], "", False))
        L = []
        for v in self.validations[-6:]:
            mark = "STALE" if v.get("status") == "stale" else ("current" if v.get("status") == "current" else "unknown")
            rs = f" ({'; '.join(v.get('reasons')[:3])})" if v.get("reasons") and mark == "STALE" else ""
            L.append(f"- {mark}: {_q(v.get('command'), 80)}: {sanitize.clip(v.get('result'), 120)}{sanitize.clip(rs, 400)}")
        secs.append(("Validation", [(3, x) for x in L], "(no test, build or check recorded)", False))
        ins = self.instructions
        L = []
        if ins.get("now"):
            L.append(f"- the message that resumes the task (the newest instruction): {_q(ins['now'], 300)}")
        for x in reversed(ins.get("since") or []):
            L.append(f"- given since (newer; it takes precedence where they differ): {_q(x['text'], 300)} (seq {x['seq']})")
        if ins.get("at_checkpoint"):
            L.append(f"- in force at the checkpoint: {_q(ins['at_checkpoint']['text'], 300)} (seq {ins['at_checkpoint']['seq']})")
        for x in ins.get("notes_since") or []:
            L.append(f"- {x['kind']} recorded since: {_q(x['text'], 200)} (seq {x['seq']})")
        if ins.get("latest") and not ins.get("since") and not ins.get("at_checkpoint"):
            L.append(f"- latest: {_q(ins['latest']['text'], 300)} (seq {ins['latest']['seq']})")
        secs.append(("Instructions", [(3, x) for x in L], "(no user instruction recorded)", False))
        lin = self.lineage
        L = []
        if lin.get("text"):
            L.append("- " + lin["text"])
        for c in lin.get("concurrent") or []:
            L.append(f"- also live on this task: session {c['session']} (pid {c.get('pid')})")
        if L:
            secs.append(("Lineage", [(3, x) for x in L], "", False))
        gw = self.gateway
        if gw.get("lines"):
            secs.append(("Gateway", [(3, f"- {x}") for x in gw["lines"]], "", False))
        if self.hosts:
            secs.append(("Hosts", [(3, f"- {h.line()}") for h in self.hosts[:20]], "", False))
        az = self.authz
        if az and (az.holds or az.said or az.unknown):
            L = [f"- still holds (unchanged: carry on within it; no need to ask the person again): {x}" for x in az.holds]
            L += [f"- {x}" for x in az.said]
            L += [f"- {x}" for x in az.unknown]
            secs.append(("Authorizations", [(3, x) for x in L[:30]], "", False))
        if self.problems:
            secs.append(("Limits", [(3, f"- {p}") for p in self.problems], "", False))

        # What may be used: the limit, less the summary's floor and room for
        # the "+N more" lines and the note naming what was left out.
        has_summary = bool(summary_text) and summary_max > 0
        more_len = 60 + len(full)
        budget = (limit - len(head) - len(foot) - (min(summary_max, SUMMARY_FLOOR) if has_summary else 0)
                  - more_len * 3 - 120)
        used = sum(len(f"\n## {n}\n") + (len(ph) if not items else 0) for n, items, ph, req in secs if req)
        take: dict[tuple[int, int], bool] = {}

        def add(si: int, ii: int, force: bool = False) -> None:
            nonlocal used
            if (si, ii) in take:
                return
            name, items, _ph, req = secs[si]
            cost = len(items[ii][1]) + 8 + (len(f"\n## {name}\n")
                                            if not req and not any(k[0] == si for k in take) else 0)
            if force or used + cost <= budget:
                take[(si, ii)] = True
                used += cost

        req_i = [i for i, x in enumerate(secs) if x[3]]
        opt_i = [i for i, x in enumerate(secs) if not x[3]]
        for rank in (0, 1):    # every rank-0 item, whatever the length; then rank 1
            for si in req_i:
                for ii, (r, _t) in enumerate(secs[si][1]):
                    if r == rank:
                        add(si, ii, force=rank == 0)
        for si in opt_i:       # each other section's first item
            if secs[si][1]:
                add(si, 0)
        for si in req_i:       # the rest of the required sections
            for ii, (r, _t) in enumerate(secs[si][1]):
                if r >= 2:
                    add(si, ii)
        for si in opt_i:       # the rest of the others, in order
            for ii in range(len(secs[si][1])):
                add(si, ii)

        parts = [head]
        left_out = []
        for si, (name, items, ph, req) in enumerate(secs):
            kept = [t for ii, (_r, t) in enumerate(items) if (si, ii) in take]
            cut = len(items) - len(kept)
            if not kept and not req and items:
                left_out.append(name)
                continue
            if name == "Check first":
                kept = [f"{i}. {t}" for i, t in enumerate(kept, 1)]
            lines = kept or ([] if cut else [ph])
            if cut:
                lines.append(f"- ...and {cut} more (all of them: `{full}`)" if req else f"- ...and {cut} more")
            parts.append(f"\n## {name}\n" + "\n".join(lines))
        if left_out:
            parts.append(f"\n(left out for length: {', '.join(left_out)}; `{full}` shows all)")
        parts.append(foot)
        out = "\n".join(parts)
        if has_summary:
            room = max(min(summary_max, limit - len(out) - 2), min(summary_max, SUMMARY_FLOOR))
            if callable(summary_text):
                stext = summary_text(room)
            else:
                stext = summary_text
                if len(stext) > room:
                    pointer = f"\n...(the recovery summary is cut here for length; the full one: `{cli_summary(self.task)}`)"
                    cut_at = stext.rfind("\n", 0, max(0, room - len(pointer)))
                    stext = stext[:cut_at if cut_at > 0 else max(0, room - len(pointer))] + pointer
            out += "\n\n" + stext
        return out

    def data(self) -> dict[str, Any]:
        return {"task": self.task, "base": {k: self.base.get(k) for k in ("id", "seq_from", "seq_to", "ts", "selected")},
                "counts": self.counts(), "horizon": self.horizon,
                "earlier": [o.op for o in self.earlier[:20]],
                "ops": [{"op": o.op, "class": o.cls, "tool": o.tool, "external": o.external} for o in self.ops[:50]],
                "files": [{"path": f["path"], "how": f["how"]} for f in self.files[:100]],
                "jobs": [{"id": j["id"], "class": j["cls"], **({"gateway_job": j["gateway"]} if j.get("gateway") else {})}
                         for j in self.jobs],
                "gateway": {k: self.gateway.get(k) for k in ("status", "epoch", "machine_changed", "unavailable")
                            if self.gateway.get(k) is not None},
                "hosts": [{"host": h.use.host, "name": h.use.name, "listed": h.listed, "changed": bool(h.changed),
                           **({k: v for k, v in h.states().items() if isinstance(v, str)} if h.listed else {})}
                          for h in self.hosts[:20]],
                "authz": ({"holds": len(self.authz.holds), "flagged": len(self.authz.flagged),
                           "unknown": len(self.authz.unknown)} if self.authz else {}),
                "lineage": {k: self.lineage.get(k) for k in ("original", "original_alive", "original_pid")},
                "concurrent": [c["session"] for c in self.lineage.get("concurrent") or []]}


def _last_task_after(st: summary.TaskState) -> dict[str, tuple[str, int]]:
    """path -> (after-hash, seq) of the task's last successful edit of it."""
    out: dict[str, tuple[str, int]] = {}
    for o in st.ops.values():
        rec = o["intent"] or o["outcome"]
        oc = o["outcome"]
        if rec is None or oc is None:
            continue
        d, od = rec.get("data") or {}, oc.get("data") or {}
        p = d.get("path") or od.get("path")
        if (d.get("tool") or od.get("tool")) in capture.EDIT_TOOLS and p and od.get("ok") and od.get("after"):
            if p not in out or oc["seq"] > out[p][1]:
                out[p] = (od["after"], oc["seq"])
    return out


def _files(st: summary.TaskState, cp: dict[str, Any] | None) -> list[dict[str, Any]]:
    last = _last_task_after(st)
    cpd = (cp or {}).get("data") or {}
    cp_seq = int(cpd.get("seq_to") or 0) if cp else 0
    saved: dict[str, str] = {}
    if cp:
        saved.update({p: h for p, h in ((cpd.get("git") or {}).get("dirty") or {}).items()})
        saved.update(cpd.get("files") or {})
    else:
        saved.update({p: h for p, (h, _s) in last.items()})
    paths = list(saved) + [p for p, (_h_, s) in last.items() if p not in saved and s > cp_seq]
    out = []
    seen: set[str] = set()
    for p in paths:
        rp = capture.real(p)
        if rp in seen:
            continue
        seen.add(rp)
        now = capture.file_hash(p)
        sh = saved.get(p)
        lt = last.get(p)
        if sh is not None and now == sh:
            if lt and lt[1] > cp_seq and lt[0] != sh:
                # Edited after the checkpoint, and back to the saved state since.
                out.append({"path": p, "how": "outside", "saved": sh, "now": now,
                            "text": f"back to its checkpoint state ({_h(now)}) although the task edited it after "
                                    f"(seq {lt[1]}): undone outside the task; keep it as it is"})
            continue
        if lt and lt[1] > cp_seq and now == lt[0]:
            out.append({"path": p, "how": "task", "saved": sh, "now": now,
                        "text": f"changed by this task after the checkpoint (its edit at seq {lt[1]}, now {_h(now)})"})
        elif now == "absent":
            out.append({"path": p, "how": "deleted", "saved": sh, "now": now,
                        "text": f"deleted since (saved {_h(sh)}); it wasn't the task's recorded doing: keep it deleted unless "
                                "the user says otherwise"})
        elif sh is None:
            out.append({"path": p, "how": "outside", "saved": lt[0] if lt else None, "now": now,
                        "text": f"changed after the task's last recorded edit (seq {lt[1] if lt else '?'}: {_h(lt[0] if lt else '')}"
                                f" -> {_h(now)}): changed outside this task (the user or another process); keep it"})
        else:
            out.append({"path": p, "how": "outside", "saved": sh, "now": now,
                        "text": f"changed outside this task since ({_h(sh)} -> {_h(now)}; no recorded edit of the task "
                                "explains it: the user's or another process's change): keep it, re-read before editing"})
    return out


def _git(st: summary.TaskState, cp: dict[str, Any] | None, cwd: str, files: list[dict[str, Any]]) -> dict[str, Any]:
    cpd = (cp or {}).get("data") or {}
    sg = cpd.get("git") or {}
    root = sg.get("root") or ((st.repo or {}).get("data") or {}).get("root") or ""
    saved_head = sg.get("head")
    saved_branch = sg.get("branch")
    if not cp:
        rd = (st.repo or {}).get("data") or {}
        saved_head, saved_branch = rd.get("head"), rd.get("branch")
        for o in sorted(st.ops.values(), key=lambda o: (o["version"] or {"seq": 0})["seq"]):
            vd = (o["version"] or {}).get("data") or {}
            if vd.get("head"):
                saved_head, saved_branch = vd.get("head"), vd.get("branch") or saved_branch
    if not root:
        rid = capture.repo_identity(cwd) if cwd else {}
        root = rid.get("root") or ""
    if not root:
        return {}
    if not os.path.isdir(root):
        return {"root": root, "gone": True}
    rid = capture.repo_identity(root)
    g: dict[str, Any] = {"root": root, "head": rid.get("head"), "branch": rid.get("branch"),
                         "saved_head": saved_head, "saved_branch": saved_branch}
    if saved_head and rid.get("head") and saved_head != rid["head"]:
        g["head_moved"] = True
        r = subprocess.run(["git", "merge-base", "--is-ancestor", saved_head, rid["head"]], cwd=root,
                           capture_output=True, timeout=10, env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
                           umask=state.user_umask()) if _have_git() else None
        g["relation"] = ("new commits on top of the saved HEAD" if r is not None and r.returncode == 0 else
                         "the saved HEAD is not an ancestor: rebased, reset or another branch" if r is not None
                         and r.returncode == 1 else "relation unknown")
    if saved_branch and rid.get("branch") and saved_branch != rid["branch"]:
        g["branch_changed"] = True
    if cp:
        was = {capture.real(p) for p in (sg.get("dirty") or {})}
        known = {capture.real(f["path"]) for f in files}
        now_dirty = [os.path.join(root, rel) for _s, rel in capture.git_dirty(root)]
        g["newly_dirty"] = [p for p in now_dirty if capture.real(p) not in was and capture.real(p) not in known][:50]
        nd = {capture.real(p) for p in now_dirty}
        g["cleaned"] = [p for p in (sg.get("dirty") or {}) if capture.real(p) not in nd][:50]
    return g


def _have_git() -> bool:
    from shutil import which
    return which("git") is not None


def _instructions(st: summary.TaskState, cp: dict[str, Any] | None, prompt: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    allp = ([st.goal] if st.goal else []) + list(st.instructions)
    if allp:
        out["latest"] = {"text": allp[-1]["data"].get("text") or "", "seq": allp[-1]["seq"]}
    if cp:
        cps = int((cp.get("data") or {}).get("seq_to") or 0)
        before = [r for r in allp if r["seq"] <= cps]
        if before:
            out["at_checkpoint"] = {"text": before[-1]["data"].get("text") or "", "seq": before[-1]["seq"]}
        out["since"] = [{"text": r["data"].get("text") or "", "seq": r["seq"]} for r in allp if r["seq"] > cps][-4:]
        out["notes_since"] = [{"kind": r["data"].get("kind") or "note", "text": r["data"].get("text") or "",
                               "seq": r["seq"]} for r in st.notes if r["seq"] > cps][-6:]
    p = (prompt or "").strip()
    if p and not p.startswith("/check"):
        out["now"] = sanitize.clip(sanitize.scrub(p), 600)
    return out


def _edit_evidence(d: dict[str, Any]) -> str:
    p, before = d.get("path"), d.get("before")
    if not p or not before:
        return "re-read the file before editing it again"
    now = capture.file_hash(p)
    shown = sanitize.clip_path(p, 120)
    if now == before:
        return f"{shown} still has its hash from before the edit ({_h(now)}): it looks unapplied; re-read it, then edit"
    return f"{shown} changed since its before-hash ({_h(before)} -> {_h(now)}): it may have been applied; re-read it first"


def _ops(st: summary.TaskState, cp: dict[str, Any] | None, me: str, table: dict[int, dict[str, Any]]) -> list[Op]:
    cps = int(((cp or {}).get("data") or {}).get("seq_to") or 0) if cp else 0
    t_results = None
    alive_cache: dict[str, bool | None] = {}
    never: list[Op] = []
    out: list[Op] = []
    sess_killed = {c.get("tool_use_id"): c for c in (state.load_session(me).get("interrupted") or [])} if me else {}
    refused = authz.refused_ops(st)
    for o in sorted(st.ops.values(), key=lambda o: (o["intent"] or o["outcome"] or {"seq": 0})["seq"]):
        intent, outcome = o["intent"], o["outcome"]
        rec = intent or outcome
        if rec is None:
            continue
        d = rec.get("data") or {}
        tool = d.get("tool") or "?"
        od = (outcome or {}).get("data") or {}
        inp = d.get("input") or {}
        what = summary._cmd(d, 200) or tool
        if tool in summary.READ_ONLY_TOOLS or tool in capture.TASK_TOOLS or tool.endswith("__checkpoint_note"):
            continue
        ext = external_effect(tool, inp)
        sid = rec.get("session") or ""
        after = bool(cp) and rec["seq"] > cps
        oh = hosts_mod.call_hosts(tool, inp)
        mk = lambda cls, why, check="": Op(o["op"], cls, tool, what, why, rec["seq"], sid, ext, check, after,  # noqa: E731
                                           hosts=oh)
        if outcome is None and o["op"] in refused:
            never.append(mk("never", refused[o["op"]]))
            continue
        if outcome is None:
            kill = st.kills.get(o["op"]) or sess_killed.get(o["op"])
            if kill:
                out.append(mk("failed", "stopped by `pause now` before it finished (killed); partial effects are possible",
                              "check what it had done before running it again"))
                continue
            br = summary.bg_result(st, o["op"], {})
            ok_ = summary.bg_ok(br)
            if ok_ is not None:
                # Its turn ended before a result came back (an aborted turn moves a
                # running Bash call to the background without a PostToolUse);
                # Claude Code reported its end later.
                out.append(mk("completed" if ok_ else "failed",
                              f"no result came back to the call (its turn ended first); then {summary.bg_text(br)}",
                              "" if ok_ else "check what it changed before running it again"))
                continue
            if t_results is None:
                t_results = summary._transcript_results(st)
            tr = t_results.get(o["op"])
            if tr is not None:
                err, body = tr
                if body.lstrip().startswith(("[Tool call interrupted", "[Request interrupted")):
                    out.append(mk("failed", "Claude Code marks it interrupted in the transcript (its marker, not a "
                                  "result); partial effects are possible", "check its effects before running it again"))
                else:
                    out.append(mk("failed" if err else "completed",
                                  f"the transcript holds {'an error' if err else 'a'} result hooks never saw: "
                                  f"{sanitize.clip(sanitize.scrub(body).replace(chr(10), ' '), 100)} (supplementary)"))
                continue
            if tool == "Bash" and table:
                pids = procs.command_shells(str(inp.get("command") or ""), table, since=float(rec.get("ts") or 0))
                if pids:
                    out.append(mk("running", f"a process runs it now (pid {', '.join(map(str, pids[:3]))}, started after "
                                  "its intent: process table)"))
                    continue
            run = summary._run_of(st, sid, rec["seq"])
            key = f"{sid}:{(run or {}).get('rec', {}).get('seq', 0)}"
            if key not in alive_cache:
                alive_cache[key] = summary._run_alive(st, run, sid)
            alive = alive_cache[key]
            if alive is True and sid != me and tool != "Bash":
                out.append(mk("running", "its session is still running (running, or waiting for an answer)"))
                continue
            if alive is True and sid == me and tool != "Bash":
                out.append(mk("unknown", "no result was recorded in this session's run (it may have been cut off "
                              "by the pause)", _edit_evidence(d) if tool in capture.EDIT_TOOLS else ""))
                continue
            why = ("its session ended or its process is gone before a result was recorded" if alive is False else
                   "no result was recorded")
            if tool == "Bash":
                why += ("; no process runs it now (process table)" if table else
                        "; the process table couldn't be read, so it may still run")
            check = _edit_evidence(d) if tool in capture.EDIT_TOOLS else (
                "check its effects (the files it writes, processes) before running it again")
            out.append(mk("unknown", why, check))
            continue
        if od.get("denied"):
            never.append(mk("never", f"denied ({od.get('denied')}): it never ran"))
            continue
        bgw = summary._background(od, d)
        if bgw:
            br = summary.bg_result(st, o["op"], od)
            ok_ = summary.bg_ok(br)
            started = summary._started(od, bgw, outcome["seq"])
            if ok_ is True:
                out.append(mk("completed", f"{started}; then {summary.bg_text(br)}"))
                continue
            if ok_ is False:
                out.append(mk("failed", f"{started}; then {summary.bg_text(br)}",
                              "check what it changed before running it again" if ext else ""))
                continue
            aid = (od.get("result") or {}).get("agentId") or ""
            child = st.children.get(aid) if aid else None
            if child and child.get("stop"):
                out.append(mk("completed", f"{started}; its subagent's stop was recorded (seq {child['stop']['seq']})"))
                continue
            later = f"; it {summary.bg_text(br)}" if br else ""
            if tool == "Bash" and table:
                pids = procs.command_shells(str(inp.get("command") or ""), table, since=float(rec.get("ts") or 0))
                if pids:
                    out.append(mk("running", f"{started}{later}; a process runs it now (pid "
                                  f"{', '.join(map(str, pids[:3]))}: process table)"))
                    continue
            run = summary._run_of(st, sid, rec["seq"])
            alive = summary._run_alive(st, run, sid)
            if tool != "Bash" and alive is True:
                out.append(mk("running", f"{started}{later}; its session still runs, so it may still be working"))
                continue
            why = f"{started}{later}; its end was not observed"
            if tool == "Bash":
                why += "; no process runs it now (process table)" if table else "; the process table couldn't be read"
            out.append(mk("unknown", why, "look at its output and effects before running it again"))
            continue
        if not od.get("ok") and (od.get("interrupted") or o["op"] in st.kills):
            k = st.kills.get(o["op"])
            how = "stopped by `pause now`" if k else "interrupted"
            out.append(mk("failed", f"{how} before it finished (observed, seq {outcome['seq']}); partial effects are "
                          "possible", "check what it had done before running it again"))
            continue
        if cp and intent is not None and intent["seq"] <= cps < outcome["seq"]:
            # In flight at the checkpoint, ended after it.
            out.append(mk("completed" if od.get("ok") else "failed",
                          f"in flight at the checkpoint; {'ended without an error' if od.get('ok') else 'failed'} "
                          f"afterwards (observed, seq {outcome['seq']})"))
    out.extend(never)
    return out


def _work(st: summary.TaskState) -> tuple[Callable[[int], bool], dict[str, int], list[int]]:
    """(paused_at(seq), the last consequential call of each session, every
    one's seq): a call that ran (not denied), not a read or the task tools,
    and not while the task was paused (a paused session may still read and
    write its tracker)."""
    marks = sorted((r["seq"], r["kind"]) for r in st.pauses)
    seqs = [m[0] for m in marks]

    def paused_at(seq: int) -> bool:
        i = bisect.bisect_right(seqs, seq)
        return i > 0 and marks[i - 1][1] == "pause"

    last: dict[str, int] = {}
    every: list[int] = []
    for o in st.ops.values():
        intent, outcome = o["intent"], o["outcome"]
        if intent is None or ((outcome or {}).get("data") or {}).get("denied"):
            continue
        tool = (intent.get("data") or {}).get("tool") or "?"
        if tool in summary.READ_ONLY_TOOLS or tool in capture.TASK_TOOLS or tool.endswith("__checkpoint_note"):
            continue
        if paused_at(intent["seq"]):
            continue
        sid = intent.get("session") or ""
        last[sid] = max(last.get(sid, 0), intent["seq"])
        every.append(intent["seq"])
    return paused_at, last, every


def _horizon(st: summary.TaskState, paused_at: Callable[[int], bool], work: list[int]) -> int:
    """The seq of the last reconciliation the work went on from (a resume
    whose context reported what was interrupted, followed by a consequential
    call), else 0. One made while the session stayed paused (a session start
    of a paused session) doesn't count."""
    latest = max(work, default=0)
    for r in reversed(st.records):
        if r["kind"] != "reconcile" or r["seq"] >= latest:
            continue
        if (r.get("data") or {}).get("paused") or paused_at(r["seq"]):
            continue
        return r["seq"]
    return 0


def _end_seq(st: summary.TaskState, op: str) -> int:
    """The seq of what ended a call: its outcome, a kill, a background end
    reported later; else its intent."""
    o = st.ops.get(op) or {}
    seqs = [r["seq"] for r in (o.get("outcome"), st.kills.get(op)) if r]
    br = summary.bg_result(st, op, ((o.get("outcome") or {}).get("data") or {}))
    if br:
        seqs.append(br["seq"])
    rec = o.get("intent") or o.get("outcome") or {}
    return max(seqs) if seqs else int(rec.get("seq") or 0)


def _successes(st: summary.TaskState) -> dict[tuple[str, str], int]:
    """(tool, what it did) -> the seq of the latest call of it that
    succeeded (a result without an error, or a background end that did)."""
    out: dict[tuple[str, str], int] = {}
    for o in st.ops.values():
        rec = o["intent"] or o["outcome"]
        if rec is None:
            continue
        d = rec.get("data") or {}
        od = (o["outcome"] or {}).get("data") or {}
        tool = d.get("tool") or "?"
        if o["outcome"] is not None and od.get("ok") and not od.get("denied") and not summary._background(od, d):
            seq = o["outcome"]["seq"]
        else:
            br = summary.bg_result(st, o["op"], od)
            if not summary.bg_ok(br):
                continue
            seq = br["seq"]
        key = (tool, summary._cmd(d, 200) or tool)
        out[key] = max(out.get(key, 0), seq)
    return out


def _scope(st: summary.TaskState, ops: list[Op]) -> tuple[list[Op], list[Op], int, int]:
    """(the operations a resume lists, the earlier unknown ones with a
    possible external effect, how many were left to the summary, the
    horizon). The task's whole history isn't what a resume checks: a call
    that failed or was interrupted is left out once a later call doing the
    same succeeded, or once its session went on with other work (its
    conversation holds the failure); an unknown outcome once a later call
    doing the same succeeded; anything an earlier resume already reported,
    once the work went on from there (the horizon), except what still runs
    and the unknown outcomes with a possible external effect, which stay
    named in one line. The summary keeps them all."""
    paused_at, last, every = _work(st)
    horizon = _horizon(st, paused_at, every)
    ok = _successes(st)
    kept: list[Op] = []
    earlier: list[Op] = []
    left = 0
    for o in ops:
        o.end_seq = _end_seq(st, o.op)
        if o.cls == "running":
            kept.append(o)
            continue
        if o.cls in ("failed", "unknown") and ok.get((o.tool, o.what), 0) > o.seq:
            left += 1
            continue
        if o.cls == "failed" and last.get(o.session, 0) > o.end_seq:
            left += 1
            continue
        if (o.end_seq if o.cls != "unknown" else o.seq) <= horizon:
            if o.cls == "unknown" and o.external:
                earlier.append(o)
            else:
                left += 1
            continue
        kept.append(o)
    never = [o for o in kept if o.cls == "never"]
    if len(never) > NEVER_SHOWN:
        drop = {id(o) for o in never[:-NEVER_SHOWN]}
        kept = [o for o in kept if id(o) not in drop]
    return kept, earlier, left, horizon


def _gw_job_class(j: dict[str, Any]) -> tuple[str, str]:
    """(class, why) for a gateway job as the gateway reports it now (the
    registry record refresh_all just refreshed; "unknown" with a reason when
    the gateway couldn't be asked)."""
    s_ = j.get("state") or "unknown"
    gj = j.get("gateway_job")
    ex = j.get("exit_code")
    if s_ in gateway.LIVE_STATES:
        words = {"suspended": "suspended (paused there; resumes with the task)", "waiting_auth":
                 "waiting for a password or other authentication (the gateway's dialog)",
                 "waiting_input": "waiting for input", "disconnected": "its host is disconnected (it may still run there)",
                 "starting": "starting"}.get(s_, "running")
        return "running", f"{words} (the gateway, job {gj}, asked now)"
    if s_ == "exited":
        return ("completed" if ex == 0 else "failed"), (
            f"exited {ex if ex is not None else '(no code)'}" + (f", signal {j.get('signal')}" if j.get("signal") else "")
            + f" (the gateway, job {gj})")
    if s_ in ("canceled", "failed"):
        return "failed", f"{s_} (the gateway, job {gj}{', ' + str(j.get('stop_reason')) if j.get('stop_reason') else ''})"
    why = j.get("unknown_why") or "the gateway can't prove its state"
    last = j.get("last_seen") or {}
    seen = f"; last seen {last.get('state')} at {_clock(last.get('at'))}" if last.get("state") else ""
    return "unknown", f"outcome unknown: {why}{seen} (job {gj})"


def _task_jobs(task: str, st: summary.TaskState) -> list[dict[str, Any]]:
    """The task's jobs, own runner's and the gateway's (not detached ones)."""
    sessions = set(st.sessions)
    out = []
    try:
        reg = jobs.refresh_all()
    except Exception:  # noqa: BLE001  a registry that can't be read: no jobs to report
        return out
    for j in sorted(reg.values(), key=lambda j: j.get("started_ts", 0)):
        if j.get("detached"):
            continue
        if j.get("task_id") != task and not (j.get("session_id") in sessions and not j.get("task_id")):
            continue
        s_ = j.get("state")
        if jobs.owner(j) == "gateway":
            cls, why = _gw_job_class(j)
            out.append({"id": j.get("id"), "name": j.get("name"), "cls": cls, "why": why, "state": s_,
                        "gateway": j.get("gateway_job"), "where": f"the gateway, job {j.get('gateway_job')}",
                        "host": j.get("host_id") or "", "machine": j.get("machine_id") or ""})
            continue
        ex = j.get("exit_code")
        if s_ in ("running", "stop_failed"):
            cls, why = "running", ("running (its process group is alive)" if s_ == "running" else
                                   "still running after a stop failed (SIGKILL didn't empty its group)")
        elif s_ == "exited":
            cls = "completed" if ex == 0 else "failed"
            why = f"exited {ex}"
        elif s_ == "killed":
            cls, why = "failed", f"stopped ({j.get('stop_reason') or 'killed'})"
        else:
            cls, why = "unknown", f"{s_}: its processes are gone and it wrote no exit code"
        out.append({"id": j.get("id"), "name": j.get("name"), "cls": cls, "why": why, "state": s_})
    return out


def _task_registry(task: str, st: summary.TaskState) -> list[dict[str, Any]]:
    """The registry's records of the task's gateway jobs (their hosts)."""
    sessions = set(st.sessions)
    try:
        reg = jobs._registry()["jobs"].values()
    except Exception:  # noqa: BLE001
        return []
    return [j for j in reg if jobs.owner(j) == "gateway" and not j.get("detached")
            and (j.get("task_id") == task or (j.get("session_id") in sessions and not j.get("task_id")))]


def _gateway_view(st: summary.TaskState, task_jobs: list[dict[str, Any]], uses: list[Any] | None = None,
                  need_snapshot: bool = False) -> tuple[dict[str, Any], list[Any], list[dict[str, Any]] | None, str]:
    """The gateway as the task knew it (its journal: job starts, snapshots,
    events) against now: available or not, the machine, and each host the
    task used (`uses`, hosts.used) from one fresh snapshot (hosts.now; every
    state unknown, with the reason, when it can't be asked). Returns (the
    view, the hosts now, the snapshot's hosts cleaned or None, why not)."""
    uses = uses or []
    then_machine = ""
    used_hosts: dict[str, str] = {}
    events = 0
    takeovers = []
    for r in st.records:
        k, d = r["kind"], r.get("data") or {}
        if k == "job.started" and d.get("supervisor") == "gateway":
            then_machine = d.get("machine_id") or then_machine
        elif k == "gateway.snapshot":
            then_machine = d.get("machine_id") or then_machine
        elif k == "gateway.event":
            events += 1
            if d.get("kind") == "terminal.owner":
                takeovers.append((r["seq"], d))
    for j in task_jobs:
        if j.get("gateway"):
            then_machine = j.get("machine") or then_machine
            if j.get("host"):
                used_hosts[j["host"]] = j["host"]
    if (not then_machine and not used_hosts and not events and not any(j.get("gateway") for j in task_jobs)
            and not uses and not need_snapshot):
        return {}, [], None, ""
    g = gateway.discover()
    out: dict[str, Any] = {"status": g.status, "epoch": g.epoch}
    lines = []
    snap: dict[str, Any] | None = None
    why = ""
    if g.ok:
        lines.append(f"available now ({g.version}, epoch {g.epoch}, machine {g.machine_id or '?'})")
        if then_machine and g.machine_id and then_machine != g.machine_id:
            out.update(machine_changed=True, machine_then=then_machine, machine_now=g.machine_id)
        if used_hosts or uses or need_snapshot:
            try:
                snap = gateway.snapshot(g)
                have = {h["host"] for h in (gateway.clean_host(x) for x in snap.get("hosts") or []) if h}
                in_uses = {u.host for u in uses if u.host}
                gone = [h for h in used_hosts if h not in have and h != g.machine_id and h not in in_uses]
                if gone:
                    out["hosts_gone"] = gone
            except gateway.CallError as e:
                why = f"the gateway's snapshot couldn't be read ({e.text()})"
                lines.append(f"its host list couldn't be read ({e.text()})")
    else:
        out.update(unavailable=True, why=g.reason)
        why = f"the gateway isn't available: {g.reason}"
        lines.append(f"not available now: {g.reason}; the task's gateway jobs read as unknown")
    for seq, d in takeovers[-3:]:
        lines.append(f"terminal {d.get('terminal')}: input {'taken by the user' if d.get('owner') == 'user' else 'back with Claude'}"
                     f" (observed, seq {seq})")
    out["lines"] = lines
    nows = hosts_mod.now(uses, g, snap, why)
    snap_hosts = [h for h in (gateway.clean_host(x) for x in (snap or {}).get("hosts") or []) if h] if snap else None
    out["accounts"] = ([a for a in (gateway.clean_account(x) for x in snap.get("accounts") or []) if a]
                       if snap and "accounts" in snap else None)
    return out, nows, snap_hosts, why


def lineage(st: summary.TaskState, task: str, me: str, table: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    """The task's original session (the last one other than `me` that ran
    it), whether it is alive elsewhere, how to resume it, and the other
    live sessions recording into the task."""
    table = procs.table() if table is None else table
    others = [r for r in st.runs if r["sid"] != me]
    out: dict[str, Any] = {}
    if others:
        run = others[-1]
        orig = run["sid"]
        rd = (run["rec"].get("data") or {})
        pid = rd.get("claude_pid") if run["rec"]["kind"] == "session.start" else state.load_session(orig).get("claude_pid")
        cwd = rd.get("cwd") or state.load_session(orig).get("cwd") or ""
        alive = summary._run_alive(st, run, orig)
        if run["rec"]["kind"] == "session.adopt" and isinstance(state.load_session(orig).get("claude_pid"), int) and table:
            alive = procs.is_claude(state.load_session(orig)["claude_pid"], table) and run["end"] is None
        mine = state.load_session(me).get("claude_pid") if me else None
        here = isinstance(pid, int) and pid == mine   # /clear: a new session in the same Claude process
        if here:
            alive = False
        out.update(original=orig, original_alive=alive is True, original_pid=pid, cwd=cwd, same_process=here)
        cmd = f"cd {shlex.quote(cwd)} && claude --resume {orig}" if cwd else f"claude --resume {orig}"
        out["resume_command"] = cmd
        if here:
            out["text"] = (f"the task's original session {orig} ran in this same Claude process (its conversation was "
                           f"cleared or replaced here). `{cmd}` reopens it with its whole conversation; this session "
                           f"continues the task as an explicit continuation (recorded with {orig[:8]} as its parent).")
        elif alive is True:
            out["text"] = (f"the task's original session {orig} is still running in another Claude process (pid {pid}): "
                           "continue it there (its terminal) rather than here; both working on it would change the same "
                           "files twice.")
        else:
            out["text"] = (f"the task's original session {orig} is not running{' (unknown: no process table)' if alive is None else ''}. "
                           f"`{cmd}` continues it with its whole conversation; "
                           + ("this session continues the task as an explicit continuation instead (recorded with "
                              f"{orig[:8]} as its parent)." if me else "or continue it in a session with /check:resume "
                              f"{task} (an explicit continuation that records its parent)."))
    elif st.runs and me:
        out.update(original=me, original_alive=False, own=True,
                   text="this session is the task's own: it continues here (its conversation is this one).")
    live = state.live_sessions(table) if table else []
    out["concurrent"] = [{"session": s["session_id"], "pid": s.get("claude_pid")} for s in live
                         if s.get("task_id") == task and s["session_id"] != me]
    return out


def reconcile(task: str, sid: str = "", checkpoint: str = "", cwd: str = "", prompt: str = "",
              selected: bool = False) -> Recon:
    """Compare the task's saved state (a checkpoint: the one named, else the
    latest; without one, the journal's latest record of each file) with
    now. Reads only."""
    view = journal.read(task)
    st = summary.collect(task, view)
    cp = None
    if checkpoint:
        cp = next((r for r in st.checkpoints if (r.get("data") or {}).get("id") == checkpoint), None)
    elif st.checkpoints:
        cp = st.checkpoints[-1]
    cpd = (cp or {}).get("data") or {}
    base = ({"id": cpd.get("id"), "seq_from": cpd.get("seq_from"), "seq_to": cpd.get("seq_to"), "ts": cp["ts"],
             "reason": cpd.get("reason"), "selected": selected} if cp else {})
    cwd = cwd or (state.load_session(sid).get("cwd") if sid else "") or ""
    r = Recon(task, base)
    if view.problems:
        r.problems.append("journal problems (records after them may be missing): "
                          + "; ".join(p.text() for p in view.problems[:3]))
    table = procs.table()
    st.table = table        # one read for every liveness question below
    r.table_ok = bool(table)
    if not table:
        r.problems.append("the process table couldn't be read: whether anything still runs is unknown")
    r.files = _files(st, cp)
    r.git = _git(st, cp, cwd, r.files)
    try:
        sm = summary.build(task, limit=100000, cwd=cwd)
        r.validations = sm.data.get("validations") or []
    except Exception as e:  # noqa: BLE001  the rest still says something
        r.problems.append(f"validation staleness couldn't be computed ({type(e).__name__})")
    r.instructions = _instructions(st, cp, prompt)
    r.ops, r.earlier, r.left, r.horizon = _scope(st, _ops(st, cp, sid, table))
    r.jobs = _task_jobs(task, st)
    refs = authz.collect(st)
    snap_hosts: list[dict[str, Any]] | None = None
    gw_why = "the gateway wasn't asked"
    try:
        uses = hosts_mod.used(st, _task_registry(task, st))
        r.gateway, r.hosts, snap_hosts, gw_why = _gateway_view(
            st, r.jobs, uses, need_snapshot=any(x.kind in ("gateway", "account") for x in refs.items))
    except Exception as e:  # noqa: BLE001  the rest still says something
        r.problems.append(f"the gateway couldn't be compared ({type(e).__name__})")
    try:
        if refs.items:
            root = ((st.repo or {}).get("data") or {}).get("root") or ""
            r.authz = authz.check(refs, snap_hosts, gw_why, {h.use.name: h for h in r.hosts if h.use.name},
                                  cwd, root, (r.gateway or {}).get("accounts"))
    except Exception as e:  # noqa: BLE001
        r.problems.append(f"the task's authorizations couldn't be compared ({type(e).__name__})")
    r.lineage = lineage(st, task, sid, table)
    return r


def record(sid: str, r: Recon, why: str) -> str:
    """A `reconcile` record in the journal (what was found, counts only)."""
    try:
        return capture.record(sid, [{"kind": "reconcile", "session": sid,
                                     "data": {"why": sanitize.clip(why, 60), "paused": state.is_paused(sid),
                                              **r.data()}}], sync=True).error
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {e}"


# -- list, diff -----------------------------------------------------------------------

def _cp_line(st_at: summary.TaskState, cpd: dict[str, Any]) -> str:
    """A one-line summary of a task at a checkpoint (stored as `line` since 0.5.0)."""
    if cpd.get("line"):
        return str(cpd["line"])
    steps = list(st_at.plan.values())
    done = sum(1 for s in steps if s.get("status") == "completed")
    files = len(cpd.get("files") or {})
    last = [x for x in ([st_at.goal] + list(st_at.instructions)) if x]
    instr = sanitize.clip((last[-1]["data"].get("text") if last else "") or "", 60)
    bits = []
    if steps:
        bits.append(f"{done}/{len(steps)} steps done")
    bits.append(f"{files} file(s)")
    if instr:
        bits.append(f"latest: {instr}")
    return "; ".join(bits)


def line_for(task: str, upto: int) -> str:
    """The one-line summary a checkpoint saved now gets."""
    v = journal.read(task)
    st = summary.collect(task, _view_upto(v, upto))
    open_ = sum(1 for o in st.ops.values() if o["intent"] and not o["outcome"])
    base = _cp_line(st, {"files": {p: 1 for p in capture.changed_files(task, st.records)}})
    return base + (f"; {open_} in flight" if open_ else "")


def _view_upto(v: journal.View, seq: int) -> journal.View:
    return journal.View(v.task, [r for r in v.records if r["seq"] <= seq], v.problems, v.next_seq, v.stopped)


def staleness(cpd: dict[str, Any]) -> tuple[str, list[str]]:
    """('current' | 'stale', reasons): the checkpoint's saved hashes and HEAD against now."""
    reasons = []
    changed = [p for p, h in (cpd.get("files") or {}).items() if capture.file_hash(p) != h]
    changed += [p for p, h in ((cpd.get("git") or {}).get("dirty") or {}).items()
                if p not in (cpd.get("files") or {}) and capture.file_hash(p) != h]
    if changed:
        reasons.append(f"{len(changed)} file(s) changed since")
    g = cpd.get("git") or {}
    if g.get("root") and os.path.isdir(g["root"]):
        rid = capture.repo_identity(g["root"])
        if g.get("head") and rid.get("head") and rid["head"] != g["head"]:
            reasons.append("HEAD moved")
        if g.get("branch") and rid.get("branch") and rid["branch"] != g["branch"]:
            reasons.append("branch changed")
    elif g.get("root"):
        reasons.append("its repository is gone")
    return ("stale" if reasons else "current"), reasons


def list_data(task: str) -> dict[str, Any]:
    v = journal.read(task)
    st = summary.collect(task, v)
    rows = []
    for i, r in enumerate(st.checkpoints):
        d = r.get("data") or {}
        stt, why = staleness(d)
        rows.append({"id": d.get("id"), "ts": r["ts"], "time": _clock(r["ts"]), "seq_from": d.get("seq_from"),
                     "seq_to": d.get("seq_to"), "reason": d.get("reason") or "",
                     "line": _cp_line(summary.collect(task, _view_upto(v, int(d.get("seq_to") or 0))), d),
                     "staleness": stt, "stale_why": why, "newest": i == len(st.checkpoints) - 1,
                     "head": str((d.get("git") or {}).get("head") or "")[:12], "files": len(d.get("files") or {})})
    meta = state.read_json(journal.jdir(task) / "meta.json", {}) or {}
    return {"task": task, "goal": meta.get("goal") or (st.goal or {}).get("data", {}).get("text") or "",
            "records": len(v.records), "checkpoints": rows, "finished": meta.get("finished"),
            "problems": [p.text() for p in v.problems[:3]]}


def list_text(task: str) -> str:
    d = list_data(task)
    fin = d.get("finished") or {}
    lines = [f"Checkpoints of task {task} ({d['records']} journal records; goal: {sanitize.clip(d['goal'], 70)})"
             + (f" [{fin.get('how')} {_clock(fin.get('ts'))}]" if fin else "") + ":"]
    if not d["checkpoints"]:
        lines.append("- none yet (/check:save or /check-save makes one; each pause saves one too)")
    for r in d["checkpoints"][-30:]:
        stale = "current" if r["staleness"] == "current" else "stale: " + ", ".join(r["stale_why"])
        lines.append(f"- {r['id']}  {r['time']}  seq {r['seq_from']}-{r['seq_to']}  {r['line']}  [{stale}]"
                     f"{'  (newest)' if r['newest'] else ''}  ({r['reason']})")
    if d["checkpoints"]:
        lines.append("Diff: /check:diff [<checkpoint> [<checkpoint>|now]]. Resume from one: /check:resume <checkpoint>.")
    if d["problems"]:
        lines.append("Journal problems: " + "; ".join(d["problems"]))
    return "\n".join(lines)


def _side(task: str, v: journal.View, ref: str) -> dict[str, Any]:
    """One side of a diff: a checkpoint, or now."""
    if ref in ("", "now"):
        st = summary.collect(task, v)
        last = _last_task_after(st)
        files = {p: capture.file_hash(p) for p in set(capture.changed_files(task, v.records)) | set(last)}
        seq = v.records[-1]["seq"] if v.records else 0
        g = {}
        root = ((st.repo or {}).get("data") or {}).get("root") or ""
        if root and os.path.isdir(root):
            rid = capture.repo_identity(root)
            dirty = capture.git_dirty(root)
            g = {"head": rid.get("head"), "branch": rid.get("branch"), "dirty_count": len(dirty)}
            for _s, rel in dirty[:capture.DIRTY_MAX]:
                files.setdefault(os.path.join(root, rel), capture.file_hash(os.path.join(root, rel)))
        return {"name": "now", "seq": seq, "st": st, "files": files, "git": g, "ts": time.time()}
    st_all = summary.collect(task, v)
    cp = next((r for r in st_all.checkpoints if str((r.get("data") or {}).get("id") or "").startswith(ref)), None)
    if cp is None:
        raise KeyError(ref)
    d = cp["data"]
    seq = int(d.get("seq_to") or 0)
    files = dict((d.get("git") or {}).get("dirty") or {})
    files.update(d.get("files") or {})
    return {"name": f"checkpoint {d.get('id')}", "seq": seq, "st": summary.collect(task, _view_upto(v, seq)),
            "files": files, "git": {k: (d.get("git") or {}).get(k) for k in ("head", "branch", "dirty_count")},
            "ts": cp["ts"]}


def diff(task: str, a: str = "", b: str = "now") -> tuple[str, dict[str, Any]]:
    """What changed between two checkpoints, or a checkpoint and now: files,
    plan steps, operations, validation, git, instructions."""
    v = journal.read(task)
    st = summary.collect(task, v)
    if not a:
        if not st.checkpoints:
            return f"Task {task} has no checkpoint to compare (/check:save makes one).", {}
        a = st.checkpoints[-1]["data"]["id"]
    try:
        A, B = _side(task, v, a), _side(task, v, b)
    except KeyError as e:
        return f"No checkpoint {e.args[0]} in task {task} (/check:list shows them).", {}
    if A["seq"] > B["seq"]:
        A, B = B, A
    data: dict[str, Any] = {"a": A["name"], "b": B["name"], "seq": [A["seq"], B["seq"]]}
    L = [f"Diff of task {task}: {A['name']} (seq {A['seq']}, {_clock(A['ts'])}) -> {B['name']} (seq {B['seq']}, "
         f"{_clock(B['ts'])})"]
    # Files.
    fa, fb = A["files"], B["files"]
    keys = sorted(set(fa) | set(fb))
    ch = []
    for p in keys:
        ha = fa.get(p)
        hb = fb.get(p) if p in fb else capture.file_hash(p) if B["name"] == "now" else None
        if ha == hb:
            continue
        if ha is None:
            ch.append(f"- {p}: new in {B['name']} ({_h(hb)})")
        elif hb is None:
            ch.append(f"- {p}: not recorded in {B['name']} (was {_h(ha)})")
        else:
            ch.append(f"- {p}: {_h(ha)} -> {_h(hb)}")
    data["files"] = len(ch)
    L.append("## Files\n" + ("\n".join(ch[:40]) if ch else "(no difference in the recorded file hashes)"))
    # Plan.
    pa, pb = A["st"].plan, B["st"].plan
    pl = []
    for sid_, s in pb.items():
        old = pa.get(sid_)
        if old is None:
            pl.append(f"- new step #{sid_} [{s.get('status')}] {sanitize.clip(s.get('subject'), 100)}")
        elif old.get("status") != s.get("status"):
            pl.append(f"- step #{sid_} {sanitize.clip(s.get('subject'), 100)}: {old.get('status')} -> {s.get('status')}")
    data["plan"] = len(pl)
    L.append("## Plan\n" + ("\n".join(pl) if pl else "(no plan step changed)"))
    # Operations between the two.
    ops = []
    for o in sorted(st.ops.values(), key=lambda o: (o["intent"] or o["outcome"] or {"seq": 0})["seq"]):
        rec = o["intent"] or o["outcome"]
        if rec is None or not (A["seq"] < rec["seq"] <= B["seq"]):
            continue
        d = rec.get("data") or {}
        tool = d.get("tool") or "?"
        if tool in summary.READ_ONLY_TOOLS or tool in capture.TASK_TOOLS:
            continue
        oc = o["outcome"] if o["outcome"] and o["outcome"]["seq"] <= B["seq"] else None
        od = (oc or {}).get("data") or {}
        if oc is None:
            res = "no outcome by then" if B["name"] != "now" else "outcome unknown or still running"
        elif od.get("denied"):
            res = f"denied ({od['denied']}): never ran"
        elif summary._background(od, d):
            br = summary.bg_result(st, o["op"], od)
            res = "started in the background" + (f"; then {summary.bg_text(br)}" if br and br["seq"] <= B["seq"] else "")
        elif od.get("ok"):
            res = "ok" + (f" (exit {od['exit']})" if od.get("exit") is not None else "")
        else:
            res = "FAILED" + (f" (exit {od['exit']})" if od.get("exit") is not None else "")
        ops.append(f"- {tool} {_q(summary._cmd(d, 100) or tool)}: {res} (seq {rec['seq']})")
    data["operations"] = len(ops)
    L.append("## Operations\n" + ("\n".join(ops[-30:]) if ops else "(no tool calls between them)")
             + (f"\n(the last 30 of {len(ops)})" if len(ops) > 30 else ""))
    # Validation.
    va = []
    for o in sorted(st.ops.values(), key=lambda o: (o["intent"] or o["outcome"] or {"seq": 0})["seq"]):
        rec = o["intent"]
        if rec is None or not (rec.get("data") or {}).get("validation") or not (A["seq"] < rec["seq"] <= B["seq"]):
            continue
        od = (o["outcome"] or {}).get("data") or {}
        res = ("no outcome" if not o["outcome"] else "background start" if summary._background(od, rec["data"]) else
               "passed" if od.get("ok") else "FAILED")
        va.append(f"- {rec['data']['validation']} {_q(summary._cmd(rec['data'], 100))}: {res} (seq {rec['seq']})")
    data["validations"] = len(va)
    L.append("## Validation\n" + ("\n".join(va) if va else "(no test, build or check between them)"))
    # Git.
    ga, gb = A["git"], B["git"]
    gl = []
    if ga.get("head") != gb.get("head") and (ga.get("head") or gb.get("head")):
        gl.append(f"- HEAD {_h(ga.get('head'))[:10]} -> {_h(gb.get('head'))[:10]}")
    if ga.get("branch") != gb.get("branch") and (ga.get("branch") or gb.get("branch")):
        gl.append(f"- branch {ga.get('branch') or '?'} -> {gb.get('branch') or '?'}")
    if ga.get("dirty_count") != gb.get("dirty_count"):
        gl.append(f"- dirty files {ga.get('dirty_count')} -> {gb.get('dirty_count')}")
    L.append("## Git\n" + ("\n".join(gl) if gl else "(unchanged)"))
    # Instructions.
    ins = [r for r in ([st.goal] if st.goal else []) + st.instructions if A["seq"] < r["seq"] <= B["seq"]]
    L.append("## Instructions\n" + ("\n".join(f"- {_q(r['data'].get('text'), 200)} (seq {r['seq']})" for r in ins[-6:])
                                    if ins else "(none given between them)"))
    return "\n".join(L), data


# -- export ---------------------------------------------------------------------------

EXPORT_FORMAT = "checkpoint-export/1"


def _rescrub(obj: Any, depth: int = 0) -> Any:
    """Every string scrubbed again (the journal already is; this is the
    export's own guarantee, whatever an older version wrote)."""
    if depth > 40:
        return "(cut)"
    if isinstance(obj, str):
        return sanitize.scrub(obj)
    if isinstance(obj, dict):
        return {sanitize.scrub(str(k)): _rescrub(v, depth + 1) for k, v in obj.items()
                if not sanitize.sensitive_name(str(k))}
    if isinstance(obj, list):
        return [_rescrub(x, depth + 1) for x in obj]
    return obj


def export(task: str, out: str = "", records: int = 5000) -> tuple[bool, str]:
    """A sanitized bundle of the task: its recovery summary, its checkpoints
    (records and saved summaries), a journal excerpt (the last `records`
    records) and references (paths, ids: never contents, never credentials).
    Every string goes through the sanitizer again. Written 0600; returns
    (ok, path or why not)."""
    v = journal.read(task)
    if not v.records:
        return False, f"task {task} has no journal records"
    # Everything below is built from the records scrubbed again, never the raw ones.
    v = journal.View(v.task, _rescrub(v.records), v.problems, v.next_seq, v.stopped)
    st = summary.collect(task, v)
    meta = state.read_json(journal.jdir(task) / "meta.json", {}) or {}
    sm = summary.build(task, limit=100000, view=v)
    cps = []
    for r in st.checkpoints:
        d = r.get("data") or {}
        # A checkpoint's saved summary text is replaced by one built now from
        # the scrubbed records up to it (an old text may have been built from
        # unscrubbed ones): the same sections, the same seq range.
        upto = _view_upto(v, int(d.get("seq_to") or 0))
        cps.append({"record": r, "summary": summary.build(task, limit=100000, view=upto).text,
                    "saved_summary_sha256": d.get("summary_sha256")})
    refs = {"journal": str(journal.jdir(task)), "sessions": list(st.sessions),
            "transcripts": sorted({((s.get("start") or {}).get("data") or {}).get("transcript") or ""
                                   for s in st.sessions.values()} - {""}),
            "repository": {k: ((st.repo or {}).get("data") or {}).get(k) for k in ("root", "branch", "head")},
            "resume": [f"claude --resume {s}" for s in list(st.sessions)[-3:]]}
    bundle = {"format": EXPORT_FORMAT, "exported": time.time(), "exported_at": state.now_iso(), "task": task,
              "meta": {k: meta.get(k) for k in ("goal", "created", "updated", "cwd", "finished", "last_checkpoint")},
              "summary": sm.text, "checkpoints": cps, "references": refs,
              "journal": {"records": v.records[-records:], "total": len(v.records),
                          "problems": [p.text() for p in v.problems]},
              "note": ("Sanitized task state and references only. It holds no file contents, transcripts or "
                       "credentials (moving credentials is the gateway's encrypted export).")}
    bundle = _rescrub(bundle)
    # The sha256 of the bundle as written without this field.
    bundle["sha256"] = sanitize.sha256_text(json.dumps(bundle, indent=1, sort_keys=True, default=str))
    if out:
        path = Path(out).expanduser()
        if path.is_dir():
            path = path / f"checkpoint-{task}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    else:
        d = state.p("exports")
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = d / f"checkpoint-{task}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    try:
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(bundle, indent=1, sort_keys=True, default=str))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        return False, f"writing {path} failed: {e.strerror or e}"
    return True, str(path)


# -- retention ------------------------------------------------------------------------

def prune_tasks(now: float | None = None, dry_run: bool = False) -> list[str]:
    """Remove journals past retention (state.journal_keep_days: finished
    tasks; journal_keep_open_days: the rest), never one with a live or
    paused session or a running job of checkpoint's runner. Returns the
    tasks removed (or that would be)."""
    now = time.time() if now is None else now
    keep_done, keep_open = state.journal_keep_days(), state.journal_keep_open_days()
    if not keep_done and not keep_open:
        return []
    table = procs.table()
    if not table:
        return []   # every session would look dead
    live = {s["session_id"] for s in state.live_sessions(table)}
    try:
        _all = jobs.refresh_all()
        running = {j.get("task_id") for j in _all.values() if jobs.is_live(j)}
        running_sessions = {j.get("session_id") for j in _all.values() if jobs.is_live(j)}
    except Exception:  # noqa: BLE001
        return []
    out = []
    import shutil
    for t in journal.tasks():
        meta = state.read_json(journal.jdir(t) / "meta.json", {}) or {}
        updated = float(meta.get("updated") or 0)
        if not updated:
            try:
                updated = max(p.stat().st_mtime for p in journal.jdir(t).iterdir())
            except (OSError, ValueError):
                continue
        days = keep_done if meta.get("finished") else keep_open
        if not days or now - updated < days * 86400:
            continue
        sessions = set(meta.get("sessions") or [])
        if sessions & live or any(state.is_paused(s) for s in sessions) or t in running or sessions & running_sessions:
            continue
        out.append(t)
        if not dry_run:
            shutil.rmtree(journal.jdir(t), ignore_errors=True)
    return out
