"""The recovery summary: built from a task's journal alone, deterministically.

No model is asked: the same journal and the same files give the same text.
Every line says where it comes from. "observed" lines are facts the hooks and
the mod recorded (with the journal seq as evidence); "inferred" lines are
conclusions drawn from them (counts, staleness) and say so; an operation whose
intent was recorded without an outcome is "outcome unknown", never done.

Sections (ORDER): Goal, Latest instructions, Constraints & decisions, In
flight / unknown, Plan, Validation, Failures, Files changed, Done, Children,
Jobs & gateway (with the hosts the task used), Authorizations (the rights and
approvals its calls used, by name and decision), Lineage, Evidence. Each is bounded; when the whole is over `limit` the last
sections are left out and named. The header (the task, journal problems, the pause, a finished or reopened task, recording gaps, the last
checkpoint) is never left out.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import authz, capture, journal, procs, sanitize, state, tracker, transcript
from . import hosts as hosts_mod

LIMIT = 8000
# Sections in the order shown, most important first: when the summary must
# be shorter, the last ones are left out (and named).
ORDER = ("Goal", "Latest instructions", "Constraints & decisions", "In flight / unknown", "Plan", "Validation",
         "Failures (observed)", "Files changed", "Done (observed outcomes)", "Children", "Jobs & gateway",
         "Authorizations", "Lineage", "Evidence")
READ_ONLY_TOOLS = ("Read", "Glob", "Grep", "LS", "TaskList", "TaskGet", "WebSearch", "WebFetch",
                   "ListAgents", "ToolSearch", "CronList")


def _short(s: str, n: int = 8) -> str:
    return (s or "")[:n]


def _clock(ts: float | None) -> str:
    if not ts:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _h(v: Any) -> str:
    """A file state for display: the first 12 hex digits of a sha256, or the word."""
    v = str(v or "")
    return v[:12] if len(v) == 64 else (v or "?")


def _cmd(data: dict[str, Any], n: int = 120) -> str:
    inp = data.get("input") or {}
    for k in ("command", "file_path", "notebook_path", "subject", "description", "url", "query", "pattern",
              "summary"):
        if inp.get(k):
            text = sanitize.scrub(str(inp[k])).replace("\n", " ")
            # A path is cut in the middle: its file name is what tells edits apart.
            return sanitize.clip_path(text, n) if k in ("file_path", "notebook_path") else sanitize.clip(text, n)
    return ""


def _ext(tool: str, data: dict[str, Any]) -> str:
    """'' or the kind of effect outside the repository a call may have had
    (recovery.external_effect)."""
    from . import recovery  # recovery imports this module
    return recovery.external_effect(tool, data.get("input") or {})


def _cmdq(data: dict[str, Any], n: int = 120) -> str:
    """' `<what it did>`' for a line, or '' when there is nothing to show."""
    c = _cmd(data, n)
    return f" `{c}`" if c else ""


# How an outcome that is only a start reads (capture.py's `background`).
_BG_TEXT = {"background": "started in the background",
            "user": "moved to the background by the user (Ctrl+B)",
            "turn_abort": "moved to the background when its turn was aborted",
            "deliver_message": "moved to the background so a message could reach the model",
            "timeout": "moved to the background at its timeout",
            "async_launched": "started in the background (an async subagent)",
            "remote_launched": "started remotely (a cloud agent)",
            "teammate_spawned": "started as a teammate"}


def _background(od: dict[str, Any], d: dict[str, Any] | None = None) -> str:
    """'' for an outcome that is the call's result; else why it is only a
    start (its work went on in the background). Journals written before this
    was recorded say it through the result (background_id, async) or the
    input (run_in_background)."""
    if od.get("background"):
        return str(od["background"])
    res = od.get("result") or {}
    if res.get("background_id"):
        return str(res.get("background_why") or "background")
    if res.get("async") or res.get("status") in sanitize.AGENT_LAUNCHED:
        return str(res.get("status") or "async_launched")
    if ((d or {}).get("input") or {}).get("background") and od.get("ok") and not od.get("denied"):
        return "background"
    return ""


def bg_result(st: "TaskState", op: str, od: dict[str, Any]) -> dict[str, Any] | None:
    """What became of work a call started in the background, as observed
    later (a task notification, TaskOutput, TaskStop): the last final
    record for its tool_use_id or background/agent id, else the last notice.
    None when nothing was observed."""
    res = od.get("result") or {}
    keys = [k for k in (op, res.get("background_id"), res.get("agentId")) if k]
    recs = sorted((r for k in keys for r in st.bg_results.get(str(k), [])), key=lambda r: r["seq"])
    finals = [r for r in recs if (r.get("data") or {}).get("final")]
    return finals[-1] if finals else (recs[-1] if recs else None)


def bg_text(rec: dict[str, Any]) -> str:
    """How an observed background result reads."""
    d = rec.get("data") or {}
    via = "Claude Code's task notification" if d.get("via") == "task-notification" else d.get("via") or "?"
    ex = f" (exit {d['exit']})" if d.get("exit") is not None else ""
    if not d.get("final"):
        what = {"waiting_input": "was waiting for input", "still_running": "was still running"}.get(
            d.get("notice") or "", "was reported on")
        return f"{what} at seq {rec['seq']} (observed: {via}; no end reported yet)"
    st_ = d.get("status")
    what = {"completed": "completed", "failed": "FAILED", "killed": "was stopped"}.get(st_, st_ or "?")
    req = " (a stop the model asked for)" if d.get("requested") else ""
    return f"{what}{ex}{req}, reported at seq {rec['seq']} (observed: {via})"


def bg_ok(rec: dict[str, Any] | None) -> bool | None:
    """True/False for an observed end that did / didn't succeed; None when no end was observed."""
    d = (rec or {}).get("data") or {}
    if not d.get("final"):
        return None
    if d.get("status") == "completed":
        return d.get("exit") in (None, 0)
    return False


def _started(od: dict[str, Any], why: str, seq: int) -> str:
    res = od.get("result") or {}
    bid = res.get("background_id") or res.get("agentId") or ""
    return f"{_BG_TEXT.get(why, 'started in the background')} (observed, seq {seq}{', id ' + bid if bid else ''})"


@dataclass
class TaskState:
    task: str
    records: list[dict[str, Any]] = field(default_factory=list)
    problems: list[journal.Problem] = field(default_factory=list)
    goal: dict[str, Any] | None = None
    instructions: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, dict[str, Any]] = field(default_factory=dict)
    todos: dict[str, Any] | None = None
    ops: dict[str, dict[str, Any]] = field(default_factory=dict)
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    runs: list[dict[str, Any]] = field(default_factory=list)   # each session.start / session.adopt, with its end
    children: dict[str, dict[str, Any]] = field(default_factory=dict)
    compactions: list[dict[str, Any]] = field(default_factory=list)
    pauses: list[dict[str, Any]] = field(default_factory=list)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    repo: dict[str, Any] | None = None
    continued_from: str = ""
    # background.result records (task notifications, TaskOutput/TaskStop),
    # by the key they name: the start's tool_use_id, or its task/agent id.
    bg_results: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    finished: dict[str, Any] | None = None      # the last task.complete / task.cancel (cleared by task.reopen)
    reopened: dict[str, Any] | None = None      # the task.reopen that cleared it (a finished task picked up again)
    concurrent: list[dict[str, Any]] = field(default_factory=list)   # session.concurrent records
    kills: dict[str, dict[str, Any]] = field(default_factory=dict)   # op -> its op.killed record (pause now)
    # jobs (job.started: which supervisor, the gateway's job id) and what the
    # gateway reported (CONTRACT-v1: snapshots, events, pause/resume outcomes)
    jobs: list[dict[str, Any]] = field(default_factory=list)
    gw_events: list[dict[str, Any]] = field(default_factory=list)
    gw_snapshots: list[dict[str, Any]] = field(default_factory=list)
    gw_marks: list[dict[str, Any]] = field(default_factory=list)   # gateway.pause/resume/epoch/unavailable
    policy: dict[str, Any] | None = None
    table: dict[int, dict[str, Any]] | None = None   # the process table, read once per build (_tbl)
    gaps: list[dict[str, Any]] = field(default_factory=list)    # recording.gap: windows the hooks couldn't record


def _tbl(st: "TaskState") -> dict[int, dict[str, Any]]:
    """The process table, read once for a summary or reconciliation ({} when
    it can't be read: unknown, never "nothing runs")."""
    if st.table is None:
        st.table = procs.table()
    return st.table


def collect(task: str, view: journal.View | None = None) -> TaskState:
    v = view or journal.read(task)
    st = TaskState(task, records=v.records, problems=v.problems)
    for r in v.records:
        k, d, op = r["kind"], r.get("data") or {}, r.get("op") or ""
        sid = r.get("session") or ""
        if sid and sid not in st.sessions:
            st.sessions[sid] = {"sid": sid, "first_seq": r["seq"], "start": None, "end": None}
        if k in ("session.start", "session.adopt"):
            if k == "session.start" and st.sessions[sid]["start"] is None:
                st.sessions[sid]["start"] = r
            st.runs.append({"sid": sid, "rec": r, "end": None})
        elif k == "session.end":
            st.sessions[sid]["end"] = r
            for run in reversed(st.runs):
                if run["sid"] == sid:
                    if run["end"] is None:
                        run["end"] = r
                    break
        elif k == "repo":
            st.repo = r
        elif k == "prompt":
            if not d.get("user") or d.get("command"):
                continue
            if st.goal is None:
                st.goal = r
            else:
                st.instructions.append(r)
        elif k == "note":
            st.notes.append(r)
        elif k in ("tool.intent", "tool.outcome", "validation.version"):
            o = st.ops.setdefault(op, {"op": op, "intent": None, "outcome": None, "version": None})
            slot = {"tool.intent": "intent", "tool.outcome": "outcome", "validation.version": "version"}[k]
            if o[slot] is None:
                o[slot] = r
        elif k == "plan.event":
            sid_ = d.get("id") or ""
            if sid_:
                s = st.plan.setdefault(sid_, {"id": sid_, "subject": d.get("subject") or "", "status": "pending",
                                              "seq": r["seq"], "how": "TaskCreated"})
                if d.get("event") == "completed":
                    s.update(status="completed", seq=r["seq"], how="TaskCompleted")
        elif k == "subagent.start":
            a = d.get("agent_id") or r.get("agent") or op
            st.children.setdefault(a, {"id": a, "type": d.get("agent_type") or "", "start": r, "stop": None})
        elif k == "subagent.stop":
            a = d.get("agent_id") or r.get("agent") or op
            c = st.children.setdefault(a, {"id": a, "type": d.get("agent_type") or "", "start": None, "stop": None})
            c["stop"] = r
        elif k == "compact":
            st.compactions.append(r)
        elif k in ("pause", "resume"):
            st.pauses.append(r)
        elif k == "recording.gap":
            st.gaps.append(r)
        elif k == "checkpoint":
            st.checkpoints.append(r)
        elif k == "task.continued":
            st.continued_from = d.get("from") or ""
        elif k == "background.result":
            for key in {d.get("tool_use_id") or "", d.get("task_id") or ""} - {""}:
                st.bg_results.setdefault(key, []).append(r)
        elif k in ("task.complete", "task.cancel"):
            st.finished = r
            st.reopened = None
        elif k == "task.reopen":
            st.finished = None
            st.reopened = r
        elif k == "session.concurrent":
            st.concurrent.append(r)
        elif k == "op.killed":
            st.kills[op] = r
        elif k == "job.started":
            st.jobs.append(r)
        elif k == "gateway.event":
            st.gw_events.append(r)
        elif k == "gateway.snapshot":
            st.gw_snapshots.append(r)
        elif k in ("gateway.pause", "gateway.resume", "gateway.epoch", "gateway.unavailable"):
            st.gw_marks.append(r)
        elif k == "task.policy":
            st.policy = r
    # Plan steps from the task tools, in the order their calls came.
    for o in sorted(st.ops.values(), key=lambda o: (o["intent"] or o["outcome"] or {"seq": 0})["seq"]):
        intent, outcome = o["intent"], o["outcome"]
        rec = intent or outcome
        tool = ((rec or {}).get("data") or {}).get("tool")
        ok = bool(outcome and (outcome.get("data") or {}).get("ok"))
        idata = ((intent or outcome or {}).get("data") or {}).get("input") or {}
        odata = ((outcome or {}).get("data") or {}).get("result") or {}
        if tool == "TaskCreate":
            sid_ = odata.get("id") or f"?{o['op'][-6:]}"
            s = st.plan.setdefault(sid_, {"id": sid_, "status": "pending"})
            s.setdefault("subject", idata.get("subject") or odata.get("subject") or "")
            s["subject"] = s["subject"] or idata.get("subject") or ""
            s.setdefault("seq", rec["seq"])
            s.setdefault("how", "TaskCreate")
            if not ok:
                s["note"] = "creation not confirmed (no result recorded)" if not outcome else "creation failed"
        elif tool == "TaskUpdate":
            sid_ = idata.get("taskId") or odata.get("taskId")
            if not sid_:
                continue
            s = st.plan.setdefault(sid_, {"id": sid_, "subject": "", "status": "pending", "seq": rec["seq"]})
            if idata.get("subject"):
                s["subject"] = idata["subject"]
            new = odata.get("to") or idata.get("status")
            if ok and new:
                s.update(status=new, seq=(outcome or rec)["seq"], how="TaskUpdate")
                s.pop("note", None)
            elif new and not outcome:
                s["note"] = f"change to {new} requested at seq {rec['seq']}, outcome unknown"
        elif tool == "TodoWrite" and (ok or not outcome):
            st.todos = {"todos": idata.get("todos") or [], "seq": rec["seq"], "confirmed": ok}
    return st


def _run_of(st: TaskState, sid: str, seq: int) -> dict[str, Any] | None:
    """The run (session.start or adopt) of `sid` a record at `seq` belongs to."""
    best = None
    for run in st.runs:
        if run["sid"] == sid and run["rec"]["seq"] <= seq:
            best = run
    return best


def _run_alive(st: TaskState, run: dict[str, Any] | None, sid: str) -> bool | None:
    """Is this run still going? False once it has an end or a later run of
    the same session started (it was killed or crashed); else its process."""
    if run is not None:
        if run["end"] is not None:
            return False
        if any(r["sid"] == sid and r["rec"]["seq"] > run["rec"]["seq"] for r in st.runs):
            return False
        pid = (run["rec"].get("data") or {}).get("claude_pid")
        if isinstance(pid, int):
            table = _tbl(st)
            return procs.is_claude(pid, table) if table else None
    return _session_alive(st, sid)


def _session_alive(st: TaskState, sid: str) -> bool | None:
    """True/False when the session's Claude process is known running/gone;
    None when it can't be told."""
    s = st.sessions.get(sid) or {}
    if s.get("end"):
        return False
    pid = ((s.get("start") or {}).get("data") or {}).get("claude_pid") or state.load_session(sid).get("claude_pid")
    if not isinstance(pid, int):
        return None
    table = _tbl(st)
    if not table:
        return None
    return procs.is_claude(pid, table)


def _transcript_results(st: TaskState) -> dict[str, tuple[bool, str]]:
    """tool_use_id -> (is_error, text) for results in the sessions'
    transcripts (a result hooks never saw). Supplementary evidence."""
    out: dict[str, tuple[bool, str]] = {}
    for sid, s in st.sessions.items():
        tp = ((s.get("start") or {}).get("data") or {}).get("transcript") or state.load_session(sid).get(
            "transcript_path") or ""
        if not tp:
            continue
        for line in transcript.tail_lines(tp):
            if '"tool_result"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id"):
                        body = c.get("content")
                        if isinstance(body, list):
                            body = " ".join(str(x.get("text", "")) for x in body if isinstance(x, dict))
                        out[c["tool_use_id"]] = (bool(c.get("is_error")), str(body or "")[:200])
    return out


def _staleness(ver: dict[str, Any] | None, later_paths: list[str], root_hint: str) -> tuple[str, list[str]]:
    """('current' | 'stale' | 'unknown', reasons) for a validation version now.
    Every recorded file hash is compared with the file now, in a git
    repository or not; HEAD and `git status` only where there is one."""
    if not ver:
        return "unknown", ["its version wasn't captured"]
    reasons = []
    files = ver.get("files") or {}
    for p, h in files.items():
        now = capture.file_hash(p)
        if now != h:
            reasons.append(f"{p} changed ({_h(h)} -> {_h(now)})")
    known = {capture.real(k) for k in files}
    root = ver.get("root") or root_hint
    if root and os.path.isdir(root):
        rid = capture.repo_identity(root)
        if ver.get("head") and rid.get("head") and rid["head"] != ver["head"]:
            reasons.insert(0, f"HEAD moved {_h(ver['head'])[:10]} -> {_h(rid['head'])[:10]}")
        for stt, rel in capture.git_dirty(root):
            p = os.path.join(root, rel)
            if capture.real(p) not in known:
                reasons.append(f"{p} changed since (git status {stt.strip() or '?'})")
                known.add(capture.real(p))
    elif ver.get("root"):
        reasons.insert(0, f"its repository {ver['root']} is gone")
    known |= {capture.real(r.split(" ")[0]) for r in reasons}
    for p in later_paths:
        if capture.real(p) not in known:
            reasons.append(f"{p} was edited after it ran")
    return ("stale" if reasons else "current"), reasons


@dataclass
class Summary:
    task: str
    text: str
    data: dict[str, Any]


def _t(text: Any, n: int) -> str:
    """Recorded text for a summary line: scrubbed again (the journal already
    is; this holds for records an older or broken writer left), then clipped,
    so a cut never splits a secret's shape before the scrub sees it."""
    return sanitize.clip(sanitize.scrub(text), n)


def build(task: str, limit: int = LIMIT, cwd: str = "", view: journal.View | None = None) -> Summary:
    view = view if view is not None else journal.read(task)
    st = collect(task, view)
    lines: list[str] = []   # the header
    sec: dict[str, list[str]] = {}
    data: dict[str, Any] = {"task": task, "records": len(st.records)}
    first = st.records[0]["seq"] if st.records else 0
    last = st.records[-1]["seq"] if st.records else 0
    data["seq"] = [first, last]
    lines.append(f"Recovery summary for task {task} (from its journal, seq {first}-{last}, built "
                 f"{_clock(time.time())}). Lines marked observed come from recorded events; "
                 "inferred ones are conclusions from them. Nothing here was written by a model.")
    if st.problems:
        lines.append("Journal problems (records after them may be missing): "
                     + "; ".join(p.text() for p in st.problems[:3]))
    if st.continued_from:
        lines.append(f"This task continues {st.continued_from}, whose journal is damaged.")
    if st.pauses and st.pauses[-1]["kind"] == "pause":
        lines.append(f"Paused at {_clock(st.pauses[-1]['ts'])} (observed, seq {st.pauses[-1]['seq']}).")
    for gp in st.gaps[-3:]:
        gd = gp.get("data") or {}
        since, _, why = str(gd.get("failed") or "?").partition(" ")
        lines.append(f"Recording gap: the hooks couldn't record events from {since} until "
                     f"{gd.get('recovered') or '?'}{' (' + why + ')' if why else ''}; what happened in that window "
                     f"is missing from this journal (observed, seq {gp['seq']}).")
    if len(st.gaps) > 3:
        lines.append(f"({len(st.gaps) - 3} earlier recording gap(s) not shown.)")
    if st.checkpoints:
        # In the header, which is never left out: the checkpoint a resume can
        # name. It used to be in Evidence, the last section, the first left out
        # when the text must be short (a resume context under long paths).
        c = st.checkpoints[-1]["data"]
        lines.append(f"Last checkpoint: {c.get('id')} at {_clock(st.checkpoints[-1]['ts'])} (journal seq "
                     f"{c.get('seq_from')}-{c.get('seq_to')}; observed, seq {st.checkpoints[-1]['seq']}).")
    if st.finished:
        fd = st.finished.get("data") or {}
        how = "Marked complete" if st.finished["kind"] == "task.complete" else "Canceled"
        lines.append(f"{how} at {_clock(st.finished['ts'])} by the user (observed, seq {st.finished['seq']})"
                     + (f": {sanitize.clip(fd.get('note'), 200)}" if fd.get("note") else "") + ".")
        data["finished"] = {"how": st.finished["kind"], "seq": st.finished["seq"]}
    elif st.reopened:
        rd = st.reopened.get("data") or {}
        lines.append(f"Reopened at {_clock(st.reopened['ts'])} (observed, seq {st.reopened['seq']}): it had been "
                     f"{'marked complete' if rd.get('was') == 'completed' else 'canceled'}"
                     f"{' (seq ' + str(rd['finished_seq']) + ')' if rd.get('finished_seq') else ''}, and a session "
                     "picked it up again; it is open.")
        data["reopened"] = st.reopened["seq"]

    # Goal and instructions.
    L = sec.setdefault("Goal", [])
    if st.goal:
        g = st.goal["data"]
        L.append(f"{_t(g.get('text'), 800)}\n(observed: the task's first user prompt, seq "
                     f"{st.goal['seq']}, {_clock(st.goal['ts'])})")
        data["goal"] = g.get("text")
    else:
        L.append("(no user prompt recorded)")
    L = sec.setdefault("Latest instructions", [])
    if st.instructions:
        for r in st.instructions[-3:]:
            L.append(f"- {_t(r['data'].get('text'), 400)} (observed, seq {r['seq']})")
        data["instructions"] = [r["data"].get("text") for r in st.instructions[-3:]]
    else:
        L.append("(none after the first prompt)")

    # Constraints & decisions.
    L = sec.setdefault("Constraints & decisions", [])
    nl = []
    for r in st.notes[-12:]:
        d = r["data"]
        nl.append(f"- {_t(d.get('kind', 'note'), 30)}: {_t(d.get('text'), 300)} (observed: checkpoint_note, seq {r['seq']})")
    data["notes"] = [{"kind": r["data"].get("kind"), "text": r["data"].get("text")} for r in st.notes]
    tk = _tracker_notes(st, cwd)
    nl.extend(tk)
    L.extend(nl or ["(none recorded: no checkpoint_note calls and no tracker notes)"])

    # Plan.
    L = sec.setdefault("Plan", [])
    steps = sorted(st.plan.values(), key=lambda s: s.get("seq", 0))
    if steps:
        for s in steps[:20]:
            extra = f"; {s['note']}" if s.get("note") else ""
            L.append(f"- [{s.get('status')}] {_t(s.get('subject') or '(no subject)', 160)} "
                         f"(#{s['id']}; observed: {s.get('how', '?')}, seq {s.get('seq')}{extra})")
        done = sum(1 for s in steps if s.get("status") == "completed")
        L.append(f"Progress (inferred from the steps' recorded statuses): {done} of {len(steps)} completed.")
    if st.todos:
        for t in (st.todos.get("todos") or [])[:20]:
            L.append(f"- [{t.get('status')}] {_t(t.get('content'), 160)} (observed: TodoWrite, seq "
                         f"{st.todos['seq']}{'' if st.todos.get('confirmed') else ', result not recorded'})")
    if not steps and not st.todos:
        L.append("(no plan steps recorded: no TaskCreate/TaskUpdate/TodoWrite calls)")
    data["plan"] = [{k: s.get(k) for k in ("id", "subject", "status", "note")} for s in steps]

    # Ops: done, failed, in flight.
    t_results = None
    done_l, fail_l, open_l = [], [], []
    files: dict[str, dict[str, Any]] = {}
    validations = []
    launched: dict[str, dict[str, Any]] = {}   # subagent id -> the Agent outcome that started it
    alive_cache: dict[str, bool | None] = {}
    refused = authz.refused_ops(st)     # Claude Code refused them: they never ran
    for o in sorted(st.ops.values(), key=lambda o: (o["intent"] or o["outcome"] or o["version"] or {"seq": 0})["seq"]):
        intent, outcome = o["intent"], o["outcome"]
        rec = intent or outcome
        if rec is None:
            continue
        if outcome is None and o["op"] in refused:
            continue  # never ran: not in flight, not an edit, not a failure (Authorizations names the refusal)
        d = rec["data"]
        tool = d.get("tool") or "?"
        od = (outcome or {}).get("data") or {}
        bgw = _background(od, d) if outcome else ""
        who = f" (subagent {_short(rec.get('agent') or '', 10)})" if rec.get("agent") else ""
        if tool in capture.EDIT_TOOLS and (d.get("path") or od.get("path")):
            p = d.get("path") or od.get("path")
            f = files.setdefault(p, {"path": p, "n": 0, "first_before": None, "last_after": None, "seqs": [],
                                      "unknown": 0, "failed": 0})
            f["n"] += 1
            f["seqs"].append(rec["seq"])
            before = (intent or {}).get("data", {}).get("before")
            if f["first_before"] is None and before:
                f["first_before"] = before
            if outcome and od.get("ok") and od.get("after"):
                # Only an edit that worked wrote the file (a failed one's
                # after-hash is the file as the failure left it).
                f["last_after"] = od["after"]
                f["last_seq"] = outcome["seq"]
            elif outcome and not od.get("ok") and not od.get("denied"):
                f["failed"] += 1
            if not outcome:
                f["unknown"] += 1
        if d.get("validation") or (intent or {}).get("data", {}).get("validation"):
            validations.append(o)
        if outcome is None:
            # No PostToolUse: a call whose turn was aborted goes on in the
            # background without one (measured, 2.1.301); its end may still
            # have been reported by a task notification.
            br = bg_result(st, o["op"], {})
            ok_ = bg_ok(br)
            if ok_ is not None:
                line = (f"- {tool}{who}{_cmdq(d)}: no result came back to the call (its turn ended first); then "
                        f"{bg_text(br)}")
                (done_l if ok_ else fail_l).append(line)
                continue
            sid = rec.get("session") or ""
            run = _run_of(st, sid, rec["seq"])
            key = f"{sid}:{(run or {}).get('rec', {}).get('seq', 0)}"
            if key not in alive_cache:
                alive_cache[key] = _run_alive(st, run, sid)
            alive = alive_cache[key]
            if t_results is None:
                t_results = _transcript_results(st)
            tr = t_results.get(o["op"])
            pids = []
            if tool == "Bash":
                tbl = _tbl(st)
                pids = procs.command_shells(str((d.get("input") or {}).get("command") or ""), tbl,
                                            since=float(rec.get("ts") or 0)) if tbl else []
            if pids:
                why = (f"still running: a process runs it now (pid {', '.join(map(str, pids[:3]))}, started after its "
                       "intent; process table)")
            elif alive is False:
                why = "outcome unknown: its session ended or its process is gone before a result was recorded"
            elif alive is True:
                why = "no result yet: its session is still running (running, or waiting)"
            else:
                why = "outcome unknown"
            if br is not None:
                why += f"; it {bg_text(br)}"
            if tr is not None:
                err, body = tr
                if body.lstrip().startswith(("[Tool call interrupted", "[Request interrupted")):
                    why += "; the transcript marks it interrupted (Claude Code's marker, not its result)"
                else:
                    why += (f"; the transcript holds {'an error' if err else 'a'} result hooks never saw: "
                            f"{sanitize.clip(sanitize.scrub(body).replace(chr(10), ' '), 100)} (supplementary)")
            open_l.append((_ext(tool, d), f"- {tool}{who}{_cmdq(d)}: {why} (observed intent, seq {rec['seq']}, op {o['op']})"))
            continue
        if bgw:
            # The call returned, its work went on: started is all that is known,
            # unless its end was observed later (a task notification).
            started = _started(od, bgw, outcome["seq"])
            aid = (od.get("result") or {}).get("agentId") or ""
            child = st.children.get(aid) if aid else None
            if aid:
                launched.setdefault(aid, {"rec": outcome, "desc": _cmd(d, 80)})
            br = bg_result(st, o["op"], od)
            ok_ = bg_ok(br)
            if ok_ is True:
                done_l.append(f"- {tool}{who}{_cmdq(d)}: {started}; then {bg_text(br)}")
            elif ok_ is False:
                fail_l.append(f"- {tool}{who}{_cmdq(d)}: {started}; then {bg_text(br)}")
            elif child and child.get("stop"):
                done_l.append(f"- {tool}{who}{_cmdq(d)}: {started}; its subagent's stop was recorded "
                              f"(observed, seq {child['stop']['seq']}; see Children)")
            else:
                later = f"; it {bg_text(br)}" if br else ""
                open_l.append((_ext(tool, d), f"- {tool}{who}{_cmdq(d)}: {started}{later}; its result was not observed (op {o['op']})"))
            continue
        if not od.get("ok"):
            what = (f"denied ({od.get('denied')})" if od.get("denied") else
                    "interrupted" if od.get("interrupted") else
                    f"exit {od['exit']}" if od.get("exit") is not None else "failed")
            err = _t((od.get("error") or "").replace("\n", " "), 160)
            if od.get("denied"):
                continue  # never ran: not a failure of the work
            fail_l.append(f"- {tool}{who}{_cmdq(d)}: {what}{(': ' + err) if err else ''} "
                          f"(observed, seq {outcome['seq']})")
            continue
        if (tool in READ_ONLY_TOOLS or tool in capture.TASK_TOOLS or tool in capture.EDIT_TOOLS
                or tool.endswith("__checkpoint_note")):
            continue
        if d.get("validation"):
            continue
        res = od.get("result") or {}
        ex = f" exit {od['exit']}" if od.get("exit") is not None else ""
        interp = f" ({sanitize.clip(od['interpretation'], 80)})" if od.get("interpretation") else ""
        agent = f" agent {res.get('agentId')}" if res.get("agentId") else ""
        done_l.append(f"- {tool}{who}{_cmdq(d)}:{ex or ' ok'}{interp}{agent} (observed, seq {outcome['seq']})")

    L = sec.setdefault("Done (observed outcomes)", [])
    completed = [s for s in steps if s.get("status") == "completed"]
    for s in completed[:10]:
        L.append(f"- step #{s['id']} {sanitize.clip(s.get('subject'), 120)}: marked completed (observed, seq {s.get('seq')})")
    nfiles = sum(1 for f in files.values() if f.get("last_after"))
    if nfiles:
        L.append(f"- {nfiles} file(s) written (inferred from their edits' successful outcomes; see Files changed)")
    L.extend(done_l[-12:])
    if not (completed or done_l or nfiles):
        L.append("(no completed work recorded yet)")
    if fail_l:
        L = sec.setdefault("Failures (observed)", [])
        L.extend(fail_l[-8:])
    L = sec.setdefault("In flight / unknown", [])
    if open_l:
        # One that may have changed state outside the repository (a push, a
        # deploy) is never the one cut: those first, then the latest others.
        ext = [f"{line}; may have changed state outside the repository ({k}): check it before any retry"
               for k, line in open_l if k]
        rest = [line for k, line in open_l if not k]
        cap = 12 if limit < 20000 else 1000   # the CLI's full summary shows them all
        shown = ext + (rest[-(cap - len(ext)):] if len(ext) < cap else [])
        L.extend(shown)
        if len(open_l) > len(shown):
            L.append(f"- ...and {len(open_l) - len(shown)} earlier one(s) (`{state.cli('summary', task, '--limit', '100000')}`)")
        L.append("Before redoing any of these, check whether it already took effect or is still running (files, "
                 "processes, remote state): an unknown outcome is not a failure, and a start is not a result.")
    else:
        L.append("(nothing: every recorded intent has an outcome)")
    data["in_flight"] = [line for _k, line in open_l]
    data["failures"] = fail_l

    # Files.
    L = sec.setdefault("Files changed", [])
    data["files"] = []
    if files:
        for p, f in list(files.items())[:30]:
            now = capture.file_hash(p)
            last_rec = f.get("last_after")
            if last_rec and now == last_rec:
                cur = "unchanged since (current hash matches)"
            elif last_rec:
                cur = f"CHANGED since its last recorded edit (now {_h(now)})"
            else:
                cur = f"no successful edit recorded (now {_h(now)})"
            unk = f"; {f['unknown']} edit(s) with outcome unknown" if f["unknown"] else ""
            fl = f"; {f['failed']} failed" if f["failed"] else ""
            L.append(f"- {p}: {f['n']} edit(s){fl}, before {_h(f['first_before'])}, after {_h(last_rec)}; "
                     f"{cur}{unk} (observed hashes; seq {', '.join(map(str, f['seqs'][:6]))})")
            data["files"].append({"path": p, "before": f["first_before"], "after": last_rec, "now": now,
                                  "edits": f["n"], "failed": f["failed"]})
    else:
        L.append("(no edits recorded)")

    # Validation.
    L = sec.setdefault("Validation", [])
    data["validations"] = []
    if validations:
        root_hint = (st.repo or {}).get("data", {}).get("root") or ""
        for o in validations[-6:]:
            intent, outcome, ver = o["intent"], o["outcome"], o["version"]
            d = (intent or outcome)["data"]
            od = (outcome or {}).get("data") or {}
            bgw = _background(od, d) if outcome else ""
            seq = (intent or outcome)["seq"]
            obs = f" (observed, seq {seq})"
            if outcome is None:
                res = "outcome unknown"
            elif bgw:
                br = bg_result(st, o["op"], od)
                ok_ = bg_ok(br)
                bd = (br or {}).get("data") or {}
                if ok_ is True:
                    res = (f"passed{' (exit ' + str(bd['exit']) + ')' if bd.get('exit') is not None else ''}: "
                           f"{_started(od, bgw, outcome['seq'])}, then {bg_text(br)}")
                elif ok_ is False:
                    res = f"FAILED: {_started(od, bgw, outcome['seq'])}, then {bg_text(br)}"
                else:
                    res = f"{_started(od, bgw, outcome['seq'])}; its result was not observed"
                obs = ""
            elif od.get("ok"):
                if od.get("interpretation"):
                    res = (f"ended without an error but said \"{sanitize.clip(od['interpretation'], 80)}\" (an exit "
                           "Claude Code doesn't count as an error; not a clear pass)")
                elif od.get("exit") is not None:
                    res = f"passed (exit {od['exit']})"
                else:
                    res = "passed (no error reported)"
            else:
                res = (f"FAILED (exit {od['exit']})" if od.get("exit") is not None else
                       "interrupted" if od.get("interrupted") else f"failed ({od.get('denied') or 'error'})")
            vd = (ver or {}).get("data")
            later = [p for p, f in files.items() if any(s > seq for s in f["seqs"])]
            status, reasons = _staleness(vd, later, root_hint)
            uncertain = bool(vd and vd.get("head_moved"))
            if vd:
                when = (f"~{vd['lag_ms']} ms after it started" if isinstance(vd.get("lag_ms"), (int, float))
                        else "a moment after it started")
                br = f" on {vd['branch']}" if vd.get("branch") else ""
                if "hook_head" in vd:
                    hb = f" on {vd['hook_branch']}" if vd.get("hook_branch") else ""
                    where = (f"started on HEAD {_h(vd['hook_head'])[:10] if vd['hook_head'] else '(no commit yet)'}"
                             f"{hb} (read by the hook before the command ran: observed)")
                    if uncertain:
                        where += (f", but HEAD was {_h(vd.get('head'))[:10] or '(none)'}{br} by its snapshot {when}: "
                                  "which one it ran against is uncertain")
                elif vd.get("head"):
                    where = f"HEAD {_h(vd.get('head'))[:10]}{br} (read {when}: inferred to be what it started on)"
                else:
                    where = "no git commit (not a git repository, or none yet)"
                nf = len(vd.get("files") or {})
                vtxt = (f"{where}; {nf} file hash(es) (changed, untracked or edited by the task) taken {when} "
                        f"(seq {ver['seq']}: inferred to be what it ran against)")
            else:
                vtxt = "its version wasn't captured"
            what = "HEAD and those hashes" if (vd or {}).get("head") or (vd or {}).get("hook_head") else (
                "those file hashes (no git: only files the task edited are compared)")
            stale = (f"still current{' as of its snapshot' if uncertain else ''} (inferred: {what} unchanged now)"
                     if status == "current" else
                     f"STALE (inferred: {'; '.join(reasons[:4])})" if status == "stale" else "staleness unknown")
            L.append(f"- {d.get('validation')} `{_cmd(d)}`: {res}{obs}; {vtxt}; {stale}")
            data["validations"].append({"command": _cmd(d, 300), "result": res, "status": status,
                                        "background": bool(bgw), "uncertain": uncertain,
                                        "head": (vd or {}).get("hook_head", (vd or {}).get("head")),
                                        "files": (vd or {}).get("files"), "reasons": reasons})
    else:
        L.append("(no test, build or check commands recorded)")

    # Children.
    L = sec.setdefault("Children", [])
    if st.children or launched:
        for a, c in list(st.children.items())[:10]:
            if c.get("stop"):
                msg = _t(c["stop"]["data"].get("last_message"), 160)
                state_ = f"stopped (observed, seq {c['stop']['seq']}){': ' + msg if msg else ''}"
            else:
                since = f" (observed, seq {c['start']['seq']})" if c.get("start") else ""
                state_ = f"started{since}; no stop recorded, so its result was not observed"
            L.append(f"- subagent {a} ({c.get('type') or '?'}): {state_}")
        for a, x in list(launched.items())[:10]:
            if a in st.children:
                continue
            L.append(f"- subagent {a} (Agent{' `' + x['desc'] + '`' if x['desc'] else ''}): started in the background "
                     f"(observed, seq {x['rec']['seq']}); no start or stop recorded for it, so its result was not "
                     "observed")
    else:
        L.append("(no subagents recorded)")
    data["children"] = list(st.children) + [a for a in launched if a not in st.children]

    # Jobs & gateway.
    jl = _jobs_lines(st, data)
    if jl:
        sec["Jobs & gateway"] = jl

    # Authorizations: which rights and approvals the task's calls used, by
    # name and decision (authz.py); whether they still hold is the
    # reconciliation's question.
    refs = authz.collect(st)
    al = authz.summary_lines(refs)
    if al:
        sec["Authorizations"] = al
    data["authorizations"] = [{"kind": r.kind, "name": r.name, "decisions": r.decisions} for r in refs.items[:30]]

    # Lineage.
    L = sec.setdefault("Lineage", [])
    for run in st.runs[-10:]:
        sid, r = run["sid"], run["rec"]
        rd = r.get("data") or {}
        if r["kind"] == "session.adopt":
            how = (f"adopted this task (/check:resume){', continuing ' + _short(rd['parent']) if rd.get('parent') else ''}")
        else:
            par = f", parent {_short(rd['parent'])} ({rd.get('parent_how')})" if rd.get("parent") else ""
            how = f"{rd.get('source') or '?'}{par}"
        if run["end"] is not None:
            end = f"ended ({run['end']['data'].get('reason') or '?'}, seq {run['end']['seq']})"
        else:
            alive = _run_alive(st, run, sid)
            end = ("running" if alive else "no end recorded: killed or crashed (process gone)" if alive is False
                   else "no end recorded")
        L.append(f"- session {sid} ({how}): from seq {r['seq']}; {end}")
    if not st.runs:
        L.append("(no session start recorded)")
    for c in st.concurrent[-3:]:
        cd = c.get("data") or {}
        L.append(f"- {_short(c.get('session') or '')} {cd.get('what') or 'ran alongside another session'}"
                 f" (observed, seq {c['seq']})")
    if st.compactions:
        L.append(f"- compacted {sum(1 for c in st.compactions if c['data'].get('phase') in ('pre', 'mod'))} "
                     f"time(s), last at seq {st.compactions[-1]['seq']}")
    data["sessions"] = list(st.sessions)

    # Evidence.
    L = sec.setdefault("Evidence", [])
    L.append(f"- journal: {journal.jdir(task)} (seq {first}-{last}; `{state.cli('summary', task)}` or "
                 f"/check:status rebuilds this)")
    for sid, s in list(st.sessions.items())[:4]:
        tp = ((s.get("start") or {}).get("data") or {}).get("transcript")
        if tp:
            L.append(f"- transcript of {_short(sid)}: {tp}")
    # Most important first; what doesn't fit is cut line by line (a section
    # keeps its first lines) or left out, and either way named, never
    # silently dropped.
    full = state.cli("summary", task)
    head_text = "\n".join(lines)

    def assemble(room: int) -> tuple[str, list[str], list[str]]:
        parts = [head_text]
        used = len(head_text)
        omitted_: list[str] = []
        cut_: list[str] = []
        for name in ORDER:
            if name not in sec:
                continue
            block = f"\n## {name}\n" + "\n".join(sec[name])
            if used + len(block) + 1 > room:
                keep: list[str] = []
                size = len(f"\n## {name}\n") + 48
                for ln in sec[name]:
                    if used + size + len(ln) + 1 > room:
                        break
                    keep.append(ln)
                    size += len(ln) + 1
                if not keep:
                    omitted_.append(name)
                    continue
                block = (f"\n## {name}\n" + "\n".join(keep)
                         + f"\n- ...(+{len(sec[name]) - len(keep)} line(s) cut for length)")
                cut_.append(name)
            parts.append(block)
            used += len(block) + 1
        out = "\n".join(parts)
        if omitted_ or cut_:
            out += (f"\n\n(left out to stay under {limit} characters: {', '.join(omitted_) or 'nothing'}"
                    + (f"; cut: {', '.join(cut_)}" if cut_ else "") + f"; the full summary: `{full}`)")
        return out, omitted_, cut_

    room = limit - 100 - len(full)
    for _ in range(24):   # the note's own length depends on what it names (and on how long `full` is)
        text, omitted, cut = assemble(room)
        if len(text) <= limit:
            break
        room -= max(16, len(text) - limit)
    if len(text) > limit:
        # Still too long: cut the body, never the note that says what was
        # left out or cut and where the full summary is.
        at = text.rfind("\n\n(left out to stay under ")
        note = text[at:] if at >= 0 else f"\n...(summary cut at {limit} characters; the full one: `{full}`)"
        body = text[:at] if at >= 0 else text
        text = body[: max(0, limit - len(note) - 32)].rstrip() + "\n...(cut at the limit)" + note
    data["omitted"] = omitted
    data["cut"] = cut
    return Summary(task, text, data)


def _jobs_lines(st: TaskState, data: dict[str, Any]) -> list[str]:
    """The task's jobs (who supervises each, the gateway's job id) with the
    last state the journal observed, the hosts the gateway listed, its
    terminals' takeovers and its pause outcomes. Observed only: whether a
    job still runs now is the reconciliation's question (it asks the
    gateway, or the process table)."""
    L: list[str] = []
    last: dict[str, dict[str, Any]] = {}
    for r in st.gw_events:
        d = r.get("data") or {}
        if d.get("kind") in ("job.state", "job.exited") and d.get("job"):
            last[d["job"]] = r
    data["jobs"] = []
    for r in st.jobs[-12:]:
        d = r.get("data") or {}
        if d.get("outcome") == "refused":
            # refused by the gateway (its policy, a pause): started nowhere (jobs._refused)
            why = _t(d.get("reason"), 220) or "no reason recorded"
            L.append(f"- job {_t(d.get('name'), 60)}: `{_t(d.get('command'), 80)}` REFUSED by the gateway"
                     f"{' (' + _t(d.get('code'), 40) + ')' if d.get('code') else ''}, not started anywhere "
                     f"(observed, seq {r['seq']}): {why}")
            data["jobs"].append({"id": None, "supervisor": "gateway", "gateway_job": None, "refused": True})
            continue
        if not d.get("ok") and d.get("outcome") != "unknown":
            continue
        gj = d.get("gateway_job") or ""
        if d.get("supervisor") == "gateway":
            pol = f"; on_pause {d.get('on_pause')}" if d.get("on_pause") else ""
            where = f"the gateway, job {gj or '?'} on {d.get('host') or 'localhost'}{pol}"
        else:
            where = "checkpoint's own runner" + (f" ({_t(d.get('why_own'), 100)})" if d.get("why_own") else "")
        if d.get("outcome") == "unknown":
            state_ = "its start's outcome is unknown (the gateway didn't answer); it was not started elsewhere"
        elif gj and gj in last:
            e = last[gj]["data"]
            ex = f" (exit {e['exit']})" if e.get("exit") is not None else (f" (signal {e['signal']})" if e.get("signal") else "")
            state_ = f"last reported {e.get('state')}{ex} by the gateway (observed, seq {last[gj]['seq']})"
        else:
            state_ = "no end recorded in the journal"
        L.append(f"- job {_t(d.get('name'), 60)} ({d.get('job') or '?'}; {where}): `{_t(d.get('command'), 80)}` "
                 f"started (observed, seq {r['seq']}); {state_}")
        data["jobs"].append({"id": d.get("job"), "supervisor": d.get("supervisor"), "gateway_job": gj or None})
    if st.policy:
        pd = st.policy.get("data") or {}
        L.append(f"- task policy: on_pause {pd.get('on_pause') or 'continue'}, on_cancel {pd.get('on_cancel') or 'interrupt'}"
                 f" (observed: task_policy, seq {st.policy['seq']})")
    # The hosts the task used (hosts.py) and what the gateway last reported
    # of each in the journal; their state now is the reconciliation's.
    data["hosts_used"] = []
    for u in hosts_mod.used(st)[:12]:
        last = u.last
        if last and any(last.get(k) for k in ("reachable", "auth", "runtime", "identity")):
            said = ", ".join(f"{k} {_t(last[k], 30)}" for k in ("reachable", "auth", "runtime", "identity")
                             if last.get(k))
            seen = f"last reported {said} ({last.get('how')}, seq {last.get('seq')})"
        else:
            seen = "no state reported for it in the journal"
        via = "; ".join(u.via[:3])
        L.append(f"- host {u.label}: used by {via} (observed, seq {u.seq}"
                 f"{', inferred from the command' if u.inferred else ''}); {seen}")
        data["hosts_used"].append({"host": u.host, "name": u.name, "inferred": u.inferred})
    if st.gw_snapshots:
        r = st.gw_snapshots[-1]
        d = r.get("data") or {}
        hosts = d.get("hosts") or []
        if hosts:
            hl = ", ".join(f"{_t(h.get('name'), 40)}{' (this machine)' if h.get('local') else ''} "
                           f"[{_t(h.get('host'), 40) or '?'}]" for h in hosts[:8])
            L.append(f"- hosts the gateway listed (observed: its snapshot, epoch {d.get('epoch')}, seq {r['seq']}): {hl}"
                     + (f" and {len(hosts) - 8} more" if len(hosts) > 8 else ""))
        if d.get("workspaces"):
            L.append(f"- workspaces: {', '.join(_t(w, 40) for w in d['workspaces'][:8])} (observed, seq {r['seq']})")
        data["hosts"] = [h.get("host") for h in hosts]
    for r in [e for e in st.gw_events if (e.get("data") or {}).get("kind") in ("terminal.opened", "terminal.owner",
                                                                                "terminal.closed")][-6:]:
        d = r["data"]
        what = {"terminal.opened": "opened", "terminal.closed": f"closed ({d.get('reason') or '?'})",
                "terminal.owner": "input taken by the user" if d.get("owner") == "user" else "input handed back to Claude"
                }[d["kind"]]
        L.append(f"- terminal {d.get('terminal')}: {what} (observed: gateway event, seq {r['seq']})")
    for r in st.gw_marks[-6:]:
        d = r.get("data") or {}
        if r["kind"] == "gateway.pause":
            if d.get("ok"):
                outs = "; ".join(f"{o.get('job')} ({o.get('policy')}): {_t(o.get('outcome'), 80)}"
                                 for o in (d.get("outcomes") or [])[:5]) or "no job of the task ran there"
                L.append(f"- gateway pause (observed, seq {r['seq']}): {outs}")
            else:
                L.append(f"- gateway pause NOT confirmed (observed, seq {r['seq']}): {_t(d.get('error'), 120)}")
        elif r["kind"] == "gateway.resume":
            L.append(f"- gateway resume (observed, seq {r['seq']}): " + (
                "; ".join(f"{o.get('job')}: {_t(o.get('outcome'), 60)}" for o in (d.get("outcomes") or [])[:5])
                or "done" if d.get("ok") else f"NOT confirmed: {_t(d.get('error'), 120)}"))
        elif r["kind"] == "gateway.epoch":
            L.append(f"- the gateway restarted (epoch {d.get('from')} -> {d.get('to')}, observed, seq {r['seq']}): "
                     "job states from before were read again")
        elif r["kind"] == "gateway.unavailable":
            L.append(f"- the gateway became unavailable (observed, seq {r['seq']}: {_t(d.get('reason'), 120)}): its "
                     "jobs' states after that are unknown until it is back, never done")
    return L


def _tracker_notes(st: TaskState, cwd: str) -> list[str]:
    """Constraints, decisions and assumptions from the session's tracker, if
    one names a session of this task (model-written notes, labelled so)."""
    dirs = [d for d in {cwd} | {((s.get("start") or {}).get("data") or {}).get("cwd") or ""
                                 for s in st.sessions.values()} if d]
    out = []
    for d in dirs:
        for sid in st.sessions:
            t = tracker.owned(d, sid)
            if not t:
                continue
            for sec in ("Constraints", "Decisions", "Resume notes"):
                body = (t.get("sections") or {}).get(sec)
                if body:
                    out.append(f"- tracker {sec}: {sanitize.clip(sanitize.scrub(body).replace(chr(10), ' / '), 300)} "
                               f"(from {t['path']}, written by the agent)")
            return out
    return out


def task_list(cwd: str = "", limit: int = 10) -> list[dict[str, Any]]:
    """Recent tasks (from the listing cache), newest first; in this project
    first when cwd is given."""
    out = []
    for t in journal.tasks():
        meta = state.read_json(journal.jdir(t) / "meta.json", {}) or {}
        if not meta.get("goal"):
            continue
        meta.setdefault("task", t)
        out.append(meta)
    out.sort(key=lambda m: -float(m.get("updated") or 0))
    if cwd:
        out.sort(key=lambda m: 0 if (m.get("cwd") or "") == cwd else 1)
    return out[:limit]


def previous_run_ended(task: str, sid: str) -> bool:
    """Did this session's run before its latest session.start end cleanly
    (a session.end between the two)? True when there was no earlier run."""
    starts, ends = [], []
    for r in journal.read(task).records:
        if r.get("session") != sid:
            continue
        if r["kind"] == "session.start":
            starts.append(r["seq"])
        elif r["kind"] == "session.end":
            ends.append(r["seq"])
    if len(starts) < 2:
        return True
    prev, latest = starts[-2], starts[-1]
    return any(prev < e < latest for e in ends)


def session_ended(task: str, sid: str) -> bool:
    """Did this session's latest run in the task's journal end (a
    session.end after its last session.start)?"""
    last = ""
    for r in journal.read(task).records:
        if r.get("session") == sid and r["kind"] in ("session.start", "session.end"):
            last = r["kind"]
    return last == "session.end"


def interrupted(meta: dict[str, Any]) -> bool:
    """A task whose last session has no recorded end and whose Claude
    process is gone (or unknown)."""
    sid = meta.get("last_session") or ""
    if not sid or sid in (meta.get("ended") or {}) or (meta.get("finished") or {}).get("how"):
        return False
    pid = state.load_session(sid).get("claude_pid")
    if not isinstance(pid, int):
        return True
    table = procs.table()
    return bool(table) and not procs.is_claude(pid, table)
