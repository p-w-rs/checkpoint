#!/usr/bin/env python3
"""Brief 14.4 (responsive pause) and 14.5 (recovery) in the real TUI.

    pixi exec --spec python=3.12 --spec pyte python tests/acceptance_recovery.py [--keep] [--root DIR] [--only a,b,c]
        [--installed]

A scratch Claude config and a fake Messages API (tests/tui); the plugin
loaded from a copy (--plugin-dir) or, with --installed, installed normally
(check@checkpoint from a local git-source marketplace: accept_install.py), its runtime pre-built into the config's
plugin data folder (`env.sh ensure`), one state root per config shared by
every session and restart. Nothing touches the real Claude config or
~/.config/gateway (tests/tui refuses both).

a (14.4) pause during model work (a slow streamed reply whose tool call never
  runs), during a long command (left running and said so) with queued tool
  and input work (neither acts), truthful in-flight status (/check:status
  while paused), a child result (the long command's notification) that
  doesn't resume, `pause now` (the command's process tree stopped), then
  SIGKILL and a restart: still paused, and a typed message resumes after
  reconciling.
b (14.4) a scheduled callback (CronCreate, one-shot) and a plugin reload
  (/reload-plugins) while paused: no resume, no tool call.
c (14.5) a SIGKILL right after a remote write (git push to a local bare
  repo, its result lost), then a user edit: a new session's /check:resume
  reports the push as outcome unknown (an external effect: check first, never
  run again), the changed file kept, the test STALE; nothing repeated (the
  remote saw one push); `claude --resume <killed>` gets the reconciliation;
  after /compact the context comes back; after /clear the old task is picked
  up again as an explicit continuation in the same process.
d the Checkpoints pane (/check-list, a native pane): its rows, "diff with now",
  "resume from here" (the task goes on, reconciled against that checkpoint).

Prints one line per check and a JSON summary; exits non-zero on any failure.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
sys.path.insert(0, str(HERE))
from tui import install, safety  # noqa: E402
from tui.driver import Session  # noqa: E402
from tui.fakeapi import body_json  # noqa: E402
from accept_install import cli_env, kill_marked, kill_started, note_marked, provide  # noqa: E402

RESULTS: list[tuple[str, bool]] = []
SUMMARY: dict = {}


def check(name: str, ok) -> bool:
    ok = bool(ok)
    RESULTS.append((name, ok))
    print(f"  {'ok  ' if ok else 'FAIL'} {name}", flush=True)
    return ok


def request_text(rec: dict) -> str:
    b = body_json(rec) or {}
    return json.dumps({"system": b.get("system"), "messages": b.get("messages")}, ensure_ascii=False)


def wait(s: Session, cond, timeout: float, step: float = 0.1) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        s.pump(step)
    return bool(cond())


def wait_main(s: Session, n: int, timeout: float) -> bool:
    """At least n main-loop requests, reading the terminal meanwhile. Every wait after a submit must
    pump the pty (wait() does): Claude Code writes to its terminal synchronously, so a test that only
    sleeps (FakeAPI.wait_main) lets the pty fill up, Claude Code's main thread blocks in write(), and
    the session "stalls" for exactly as long as the test waits (the first prompt after --resume
    "stalled" 60 s in 0.7.3's verification: 60 s was this wait, not Claude Code)."""
    return wait(s, lambda: len(s.api.main_requests()) >= n, timeout, 0.05)


def ps_lines() -> list[str]:
    out = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,command="], capture_output=True, text=True).stdout
    return [ln for ln in out.splitlines() if "ps -A" not in ln]


def running(marker: str) -> bool:
    return any(marker in ln for ln in ps_lines())


def flat(s: Session) -> str:
    """The whole screen history as one line (wrapped rows joined, runs of spaces folded)."""
    return re.sub(r"\s+", " ", s.all_text())


def has(s: Session, needle: str) -> bool:
    """Whether the screen history holds `needle`, ignoring all whitespace (the
    terminal wraps long rows mid-word)."""
    return re.sub(r"\s+", "", needle) in re.sub(r"\s+", "", s.all_text())


def records(home: Path) -> list[dict]:
    out = []
    for seg in sorted(home.glob("journal/*/seg-*.jsonl")):
        for line in seg.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def status_text(E: "Env", sid: str) -> str:
    """What /check:status shows (the mod runs `env.sh run status --session`)."""
    r = subprocess.run(["sh", str(E.plugin / "hooks" / "env.sh"), "run", "status", "--session", sid],
                       capture_output=True, text=True, timeout=60, env=E.cli_env(sid))
    return r.stdout


def session_in(home: Path, cwd: Path) -> str:
    """The latest session whose start was recorded in this project folder
    (the state root is shared by every part and session on the config)."""
    sids = [r["session"] for r in records(home) if r["kind"] == "session.start"
            and (r.get("data") or {}).get("cwd") == str(cwd)]
    return sids[-1] if sids else ""


def paused(home: Path, sid: str) -> bool:
    return bool(sid) and sid in paused_flags(home)


def paused_flags(home: Path) -> list[str]:
    d = home / "flags"
    return sorted(p.name for p in d.glob("*")) if d.is_dir() else []


# Each step scripts its own replies with api.script (it replaces the queue):
# a turn the pause aborted leaves its unused replies behind.


def type_cmd(s: Session, text: str) -> float:
    """Type a slash command or prompt and press Enter; returns the time of Enter."""
    s.type(text, 0.005)
    s.pump(0.3)
    t = time.time()
    s.send("\r")
    return t


def last_request(s: Session, n0: int, timeout: float = 60) -> str:
    wait_main(s, n0 + 1, timeout)
    reqs = s.api.main_requests()
    return request_text(reqs[-1]) if len(reqs) > n0 else ""


class Env:
    def __init__(self, root: Path):
        self.root = root
        self.cfg = root / "cfg"
        self.plugin = root / "plugin"
        self.pdirs: list[Path] = [self.plugin]
        self.data = self.cfg / "plugins" / "data" / "check-inline"   # provide() names the real one
        self.home = safety.state_root_for(self.cfg) / ".local" / "state" / "checkpoint"
        self.genv = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.genv.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    def cli_env(self, sid: str) -> dict:
        """The environment for checkpoint's CLI run from here (outside a session): the sessions' scratch
        state and plugin data folder (a caller's own CLAUDE_PLUGIN_DATA would win over CHECKPOINT_ENVD),
        and the gateway's contract and env file named as the sessions get them. Unset, they would fall
        back to the real ~/.config/gateway (gateway.discover stats the contract file; a status or an
        ingest then talks to whatever daemon it names). accept_install.cli_env."""
        envd = (self.home / "rt" / sid).read_text().strip() if (self.home / "rt" / sid).exists() else ""
        return cli_env(self.cfg, self.data, self.home, envd)

    def git(self, repo: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                              check=True, capture_output=True, text=True, env=self.genv).stdout.strip()

    def project(self, name: str) -> Path:
        p = self.root / name
        p.mkdir()
        (p / "widget.py").write_text("def widget():\n    return 1\n")
        (p / "run_tests.sh").write_text("#!/bin/sh\necho '2 passed'\nexit 0\n")
        self.git(p, "init", "-q", "-b", "main")
        self.git(p, "add", "-A")
        self.git(p, "commit", "-qm", "start")
        return p

    def session(self, name: str, cwd: Path, **kw) -> Session:
        return Session(self.root / name, cwd, plugin_dirs=self.pdirs, config_dir=self.cfg,
                       permission_mode="bypassPermissions", debug_file=True, **kw)


def part_a(E: Env) -> None:
    print("a: pause during model work, a long command, queued work; child result; pause now; restart", flush=True)
    P = E.project("proj-a")
    s = E.session("a1", P)
    sid = ""
    try:
        s.wait_ready(timeout=90)
        check("a session reaches the prompt with checkpoint recording", wait(s, lambda: "recording" in s.text(), 30))
        # 1. During model work: a slow stream that ends in a tool call.
        s.api.script([{"blocks": [{"type": "text", "text": "Streaming my plan slowly. " * 10},
                               {"type": "tool_use", "name": "Bash",
                                "input": {"command": "echo streamed > streamed.txt", "description": "streamed"}}],
                    "chunk_delay": 0.15}, "after the stream"])
        s.submit("Plan the widget work")
        wait(s, lambda: "Streaming my plan" in s.text(), 20)
        t0 = type_cmd(s, "/check-pause")
        acked = wait(s, lambda: "paused this session" in s.all_text(), 15, 0.05)
        SUMMARY["ack_stream_s"] = round(time.time() - t0, 2)
        sid = session_in(E.home, P)
        check(f"pause while the model streams: acknowledged at once ({SUMMARY['ack_stream_s']} s), the flag on disk",
              acked and SUMMARY["ack_stream_s"] < 3 and paused(E.home, sid))
        n_req = len(s.api.main_requests())
        s.pump(4)
        check("...the streamed reply's tool call never ran (no file, no intent recorded), no further model call",
              not (P / "streamed.txt").exists() and len(s.api.main_requests()) == n_req
              and not any("echo streamed" in json.dumps(r) for r in records(E.home) if r["kind"] == "tool.intent"))
        check("...the status line says paused", wait(s, lambda: "check: paused" in s.text(), 10))
        # 2. A typed message resumes (reconciled); then a long command, queued work, pause.
        s.api.script([{"tool": "Bash", "input": {"command": "sleep 25; echo long-done > long.txt", "description": "long"}},
                   {"tool": "Bash", "input": {"command": "echo after-long > after.txt", "description": "queued call"}},
                   "done"])
        n0 = len(s.api.main_requests())
        s.submit("carry on")
        resumed = wait(s, lambda: "resumed this session (reconciled first)" in s.all_text(), 30)
        rq = last_request(s, n0)
        check("a typed message resumes, with the reconciliation in its context",
              resumed and "Reconciled before resuming" in rq and not paused(E.home, sid))
        check("...the long command starts", wait(s, lambda: running("echo long-done"), 40))
        note_marked("echo long-done")       # its shell and its `sleep` child, ended at the end
        s.type("also mention the changelog", 0.005)
        s.send("\r")
        s.pump(0.8)
        t0 = type_cmd(s, "/check-pause")
        acked = wait(s, lambda: s.all_text().count("paused this session") >= 2, 15, 0.05)
        SUMMARY["ack_long_s"] = round(time.time() - t0, 2)
        scr = flat(s)
        if "left running" not in scr:
            i = scr.rfind("paused this session")
            print("       (ack: " + scr[max(0, i - 200):i + 700] + ")")
        check(f"pause during a long command: acknowledged at once ({SUMMARY['ack_long_s']} s); it says the command "
              "is left running", acked and SUMMARY["ack_long_s"] < 3 and has(s, "left running")
              and has(s, "sleep 25; echo long-done"))
        s.pump(6)
        check("queued tool and input work: the next call never ran (queued input's turn was held too), still paused",
              not (P / "after.txt").exists() and paused(E.home, sid) and running("echo long-done"))
        # /check:status while paused (the mod answers it; Claude Code folds a long
        # output row, so its text is read the way the mod gets it).
        s.submit("/check:status")
        s.pump(2)
        st_txt = status_text(E, sid)
        st_ok = re.search(r"sleep 25; echo long-done > long\.txt`: still running: a process runs it now", st_txt) is not None
        if not st_ok:
            print("       (status: " + st_txt[st_txt.find("## In flight"):][:800] + ")")
        check("truthful in-flight status while paused: /check:status lists the long command as still running (its "
              "process, from the process table), the session paused",
              st_ok and "paused" in st_txt and paused(E.home, sid))
        # 3. Its result comes back as a task notification: a child result, not the person.
        s.api.script([{"tool": "Bash", "input": {"command": "echo acted > child.txt", "description": "child act"}}, "ok"])
        done = wait(s, lambda: (P / "long.txt").exists(), 40)
        s.pump(8)
        bg = [r for r in records(E.home) if r["kind"] == "background.result"]
        check("the long command ran to its end (left running) and its notification came back",
              done and wait(s, lambda: has(s, "completed (exit code 0)"), 20))
        check("...a child result never resumes: still paused, its turn's tool call denied",
              paused(E.home, sid) and not (P / "child.txt").exists())
        subprocess.run(["sh", str(E.plugin / "hooks" / "env.sh"), "run", "ingest", sid],
                       env=E.cli_env(sid), capture_output=True)
        bg = [r for r in records(E.home) if r["kind"] == "background.result"]
        check("...and the journal links it to its start: background.result, completed, exit 0",
              any(r["data"].get("status") == "completed" and r["data"].get("exit") == 0 for r in bg))
        # 4. pause now: the running command's process tree is stopped.
        s.api.script([{"tool": "Bash", "input": {"command": "sleep 40; echo never > never.txt", "description": "doomed"}}, "ok"])
        s.submit("next step")
        wait(s, lambda: "resumed this session" in s.all_text(), 20)
        check("resumed again, the next long command runs", wait(s, lambda: running("echo never > never.txt"), 40))
        note_marked("echo never > never.txt")
        t0 = type_cmd(s, "/check-pause now")
        acked = wait(s, lambda: "(pause now" in s.all_text(), 15, 0.05)
        SUMMARY["ack_now_s"] = round(time.time() - t0, 2)
        gone = wait(s, lambda: not running("echo never > never.txt"), 5)
        scr = flat(s)
        check(f"pause now: acknowledged ({SUMMARY['ack_now_s']} s) with exactly what happened (processes ended, "
              "which signal), the command's processes gone", acked and gone and has(s, "process(es) ended"))
        s.pump(3)
        check("...its output never written; the kill is in the journal (op.killed)",
              not (P / "never.txt").exists()
              and wait(s, lambda: any(r["kind"] == "op.killed" for r in records(E.home)), 10))
        # 5. SIGKILL while paused, then a restart.
        os.kill(s.pid, signal.SIGKILL)
        wait(s, lambda: not s.alive(), 10)
    finally:
        for m in ("echo long-done", "echo never > never.txt"):
            kill_marked(m)                  # while each shell still holds its `sleep` child
        s.close()
        kill_marked("echo long-done")
        kill_marked("echo never > never.txt")
    SUMMARY["a_session"] = sid
    s2 = E.session("a2", P, args=["--resume", sid])
    try:
        s2.wait_ready(timeout=90)
        check("after a SIGKILL and a restart (claude --resume): still paused (status line, flag); no automatic resume",
              wait(s2, lambda: "check: paused" in s2.text(), 30) and paused(E.home, sid))
        s2.api.script(["Resuming from the reconciliation."])
        n0 = len(s2.api.main_requests())
        s2.submit("ok, resume")
        t0 = time.time()
        rq = last_request(s2, n0)
        SUMMARY["a_resume_s"] = round(time.time() - t0, 2)   # Enter to the request (a test not reading: 60 s)
        check("a typed message resumes after the restart: reconciled, the killed command listed as stopped by "
              "`pause now` (canceled, not unknown, never 'redo from the start')",
              "Reconciled before resuming" in rq and "stopped by `pause now`" in rq and "from the start" not in rq
              and not paused(E.home, sid))
    finally:
        s2.close()


def part_b(E: Env) -> None:
    print("b: a scheduled callback and a plugin reload while paused", flush=True)
    P = E.project("proj-b")
    s = E.session("b1", P)
    try:
        s.wait_ready(timeout=90)
        wait(s, lambda: "recording" in s.text(), 30)
        # Never a :00 or :30 minute: Claude Code fires a one-shot task for those up to 90 s early
        # (2.1.304: oneShotMinuteMod 30, oneShotMaxMs 90 s), which once landed before n0 was taken.
        fire = datetime.datetime.now() + datetime.timedelta(minutes=2)
        if fire.minute % 30 == 0:
            fire += datetime.timedelta(minutes=1)
        cron = f"{fire.minute} {fire.hour} {fire.day} {fire.month} *"
        s.api.script([{"tool": "CronCreate", "input": {"cron": cron, "prompt": "keep going with the task please",
                                                    "recurring": False}}, "scheduled"])
        s.submit("Schedule a check-in")
        check("a one-shot CronCreate is scheduled", wait(s, lambda: "scheduled" in s.text(), 40))
        # the reply to the scheduled prompt, and the count before it, right away: pausing and
        # reloading call no model, so the next main request is the scheduled prompt whenever it fires
        s.api.script([{"tool": "Bash", "input": {"command": "echo cron > cron.txt", "description": "cron step"}}, "ok"])
        n0 = len(s.api.main_requests())
        sid = session_in(E.home, P)
        type_cmd(s, "/check-pause")
        check("paused", wait(s, lambda: "paused this session" in s.all_text(), 15) and paused(E.home, sid))
        type_cmd(s, "/reload-plugins")
        s.pump(6)
        check("after /reload-plugins: still paused (the mod reloaded; the pause lives on disk)",
              paused(E.home, sid) and wait(s, lambda: "check: paused" in s.text(), 20))
        fired = wait(s, lambda: len(s.api.main_requests()) > n0, 200, 1.0)
        s.pump(6)
        SUMMARY["cron_fired"] = fired
        check("the scheduled prompt fired while paused, and it didn't resume: its tool call denied, still paused",
              fired and not (P / "cron.txt").exists() and paused(E.home, sid))
    finally:
        s.close()


def part_c(E: Env) -> None:
    print("c: crash after a remote write, user edits, stale tests; --resume, /compact, /clear", flush=True)
    P = E.project("proj-c")
    remote = E.root / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True, env=E.genv)
    counter = E.root / "pushes.log"
    hook = remote / "hooks" / "post-receive"
    hook.write_text(f"#!/bin/sh\necho push >> '{counter}'\n")
    hook.chmod(0o755)
    E.git(P, "remote", "add", "origin", str(remote))
    w = str(P / "widget.py")
    PUSH = "git push -q origin HEAD:refs/heads/staging && echo pushed && sleep 300"
    s = E.session("c1", P, replies=[
        {"tool": "Read", "input": {"file_path": w}},
        {"tool": "Edit", "input": {"file_path": w, "old_string": "return 1", "new_string": "return 2"}},
        {"tool": "Bash", "input": {"command": "sh run_tests.sh", "description": "run the tests"}},
        {"tool": "Bash", "input": {"command": PUSH, "description": "ship to staging"}},
    ] + ["ok"] * 4)
    old = ""
    try:
        s.wait_ready(timeout=90)
        wait(s, lambda: "recording" in s.text(), 30)
        s.submit("Make widget() return 2, test it, and ship it to staging.")
        pushed = wait(s, lambda: counter.exists() and running("echo pushed && sleep 300"), 90)
        check("the remote write happened (the bare repo's post-receive counted one push) and the command still runs",
              pushed and counter.read_text().count("push") == 1)
        note_marked("echo pushed && sleep 300")
        old = session_in(E.home, P)
        os.kill(s.pid, signal.SIGKILL)
        check("claude SIGKILLed before the push's result was recorded", wait(s, lambda: not s.alive(), 10))
    finally:
        kill_marked("echo pushed && sleep 300")   # while its shell still holds its `sleep 300` child
        s.close()
        kill_marked("echo pushed && sleep 300")
    (P / "widget.py").write_text("def widget():\n    return 'the user changed this'\n")
    user_text = (P / "widget.py").read_text()
    SUMMARY["c_session"] = old
    s3 = E.session("c2", P, replies=["I will check the remote first."] * 3)
    try:
        s3.wait_ready(timeout=90)
        s3.submit("/check:resume last")
        wait_main(s3, 1, 60)
        rq = next((request_text(r) for r in reversed(s3.api.main_requests()) if "Reconciled before resuming" in request_text(r)), "")
        SUMMARY["c_resume_chars"] = len(rq)
        check("/check:resume last in a new session: the reconciliation reaches the model", bool(rq))
        check("...the push whose result was lost: outcome unknown, an external effect (push)",
              "[outcome unknown] Bash `git push -q origin HEAD:refs/heads/staging" in rq
              and "may have changed state outside the repository (push)" in rq)
        cf = rq.split("## Check first", 1)[1].split("## ", 1)[0] if "## Check first" in rq else ""
        check("...'Check first' starts with it: check whether the push landed, don't run it again until known",
              re.search(r"1\. Bash `git push", cf) is not None and "git ls-remote" in cf
              and "do not run it again until you know" in cf)
        check("...the user's edit of widget.py is listed as changed outside the task (kept)",
              "widget.py: changed" in rq and "keep it" in rq)
        check("...the test result is STALE", "STALE: `sh run_tests.sh`" in rq)
        check("...and never 'redo the stage from the start'", "from the start" not in rq)
        s3.pump(3)
        check("nothing was repeated: the remote still saw exactly one push; the user's edit is untouched",
              counter.read_text().count("push") == 1 and (P / "widget.py").read_text() == user_text)
    finally:
        s3.close()
    s4 = E.session("c3", P, args=["--resume", old], replies=["ok"] * 2)
    try:
        s4.wait_ready(timeout=90)
        s4.submit("go on")
        wait_main(s4, 1, 60)
        rq = request_text(s4.api.main_requests()[0]) if s4.api.main_requests() else ""
        check("claude --resume <the killed session>: its start brings the reconciliation and the summary",
              "<checkpoint-recovery>" in rq and "Reconciled before resuming" in rq and "outcome unknown" in rq)
        # /compact, then the next prompt.
        s4.api.script(["A summary of the conversation so far.", "ok after compact"])
        type_cmd(s4, "/compact")
        wait(s4, lambda: "ompact" in s4.all_text() and s4.state()[0] == "ready", 60, 0.5)
        s4.pump(3)
        n0 = len(s4.api.main_requests())
        s4.submit("continue please")
        rq = last_request(s4, n0)
        check("after /compact: the next request carries the recovery context again (reconciled, with the summary)",
              "<checkpoint-recovery>" in rq and "Recovery summary for task" in rq)
        # /clear, then pick the old task up in the same process.
        type_cmd(s4, "/clear")
        s4.pump(3)
        task = next((r["task"] for r in records(E.home) if r["session"] == old), "")
        s4.api.script(["Continuing the old task."])
        n0 = len(s4.api.main_requests())
        s4.submit(f"/check:resume {task}")
        rq = last_request(s4, n0)
        check("after /clear: /check:resume <old task> continues it as an explicit continuation; lineage says its "
              "original session ran in this same process and how to reopen it",
              "Reconciled before resuming" in rq and "ran in this same Claude process" in rq
              and f"claude --resume {old}" in rq)
    finally:
        s4.close()
    adopt = [r for r in records(E.home) if r["kind"] == "session.adopt" and r["data"].get("continuation")]
    check("the continuation is recorded with its parent (session.adopt)", any(r["data"].get("parent") for r in adopt))


def part_d(E: Env) -> None:
    print("d: the Checkpoints pane (/check-list): diff with now, resume from here", flush=True)
    P = E.project("proj-d")
    s = E.session("d1", P, cols=160, rows=50)
    try:
        s.wait_ready(timeout=90)
        wait(s, lambda: "recording" in s.text(), 30)
        s.api.script([{"tool": "Write", "input": {"file_path": str(P / "widget.py"), "content": "def widget():\n    return 3\n"}},
                      "wrote it"])
        s.submit("Make widget() return 3")
        wait(s, lambda: "wrote it" in s.text(), 60)
        s.submit("/check-save before the pane")
        wait(s, lambda: has(s, "saved and synced"), 30)
        sid = session_in(E.home, P)
        type_cmd(s, "/check-pause")
        wait(s, lambda: paused(E.home, sid), 15)
        (P / "widget.py").write_text("def widget():\n    return 'changed by hand'\n")
        s.submit("/check-list")
        opened = wait(s, lambda: "[ diff with now ]" in s.text() and "[ resume from here ]" in s.text(), 30)
        check("/check-list opens the native Checkpoints pane: each checkpoint's row (staleness) with its buttons",
              opened and has(s, "stale: 1 file(s) changed since"))
        xy = s.click_text("[ diff with now ]", dx=2)
        shown = wait(s, lambda: "## Files" in s.text() and "-> now" in s.text(), 5)
        if not shown:   # a pane that doesn't hold the focus yet takes the first click as focus
            xy = s.click_text("[ diff with now ]", dx=2)
            shown = wait(s, lambda: "## Files" in s.text() and "-> now" in s.text(), 15)
        if not shown:
            print(f"       (clicked at {xy}; pane: " + " / ".join(ln[80:].strip() for ln in s.lines() if ln[80:].strip())[:1500] + ")")
        check("...'diff with now' shows the diff in the pane (the file changed by hand)", shown and has(s, "widget.py"))
        check("...still paused (inspection never resumes)", paused(E.home, sid))
        s.api.script(["Going on from the checkpoint."])
        n0 = len(s.api.main_requests())
        s.click_text("[ resume from here ]", dx=2)
        if not wait(s, lambda: len(s.api.main_requests()) > n0, 5):
            s.click_text("[ resume from here ]", dx=2)
        rq = last_request(s, n0, timeout=30)
        check("...'resume from here' lifts the pause, and the task goes on with the reconciliation against that "
              "checkpoint in its context", not paused(E.home, sid) and "Reconciled before resuming" in rq
              and "You chose this checkpoint" in rq)
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--only", default="a,b,c,d")
    ap.add_argument("--installed", action="store_true",
                    help="install check@checkpoint from a local git-source marketplace instead of --plugin-dir")
    a = ap.parse_args()
    root = Path(a.root) if a.root else Path(tempfile.mkdtemp(prefix="ck-recov-"))
    root = safety.check_scratch(root, "scratch root")
    root.mkdir(parents=True, exist_ok=True)
    E = Env(root)
    t0 = time.time()
    try:
        E.plugin, E.pdirs, E.data, SUMMARY["plugin"] = provide(root, E.cfg, a.installed)
        b = subprocess.run(["sh", str(E.plugin / "hooks" / "env.sh"), "ensure"], capture_output=True, text=True,
                           env=dict(os.environ, CLAUDE_PLUGIN_DATA=str(E.data), CHECKPOINT_HOME=str(root / "unused")),
                           timeout=900)
        check("the plugin's runtime builds (pixi install --locked) into the config's plugin data", b.returncode == 0)
        only = set(a.only.split(","))
        for name, fn in (("a", part_a), ("b", part_b), ("c", part_c), ("d", part_d)):
            if name in only:
                try:
                    fn(E)
                except Exception as e:  # noqa: BLE001  report it as a failure, go on with the next part
                    check(f"part {name} ran to its end ({type(e).__name__}: {str(e)[:300]})", False)
        SUMMARY["seconds"] = round(time.time() - t0, 1)
    finally:
        for m in ("echo long-done", "echo never > never.txt", "echo pushed && sleep 300"):
            kill_marked(m)
        left = kill_started()
        check("nothing the scripted commands started is left running (the long commands' shells and their "
              "`sleep` children)", not left)
        if left:
            print("       (left running: " + "; ".join(left) + ")")
        if not a.keep:
            shutil.rmtree(root, ignore_errors=True)
    failed = [n for n, ok in RESULTS if not ok]
    SUMMARY.update(root=str(root), passed=len(RESULTS) - len(failed), failed=len(failed))
    print(json.dumps(SUMMARY))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
