#!/usr/bin/env python3
"""Brief 14.3 (automatic tracking) in the real TUI: checkpoint's journal
survives a SIGKILL mid-task, with no Stop and no handoff.

    pixi exec --spec python --spec pyte python tests/acceptance_journal.py [--keep] [--root DIR] [--installed]

A scratch Claude config and a fake Messages API (tests/tui): a scripted
session gets a goal, records a constraint (checkpoint_note), creates and
updates task steps (TaskCreate/TaskUpdate), edits a file, runs a test
command, starts a subagent, and runs two long commands (one the subagent's);
/check-save is typed while they run; then the claude process is killed with
SIGKILL. A new session on the same config (same CHECKPOINT_HOME) must show
the person the crash notice on screen (0.8.0: the task, its goal, `claude
--resume <old session>` and `/check:resume <task>`), offer the task as
interrupted, and its recovery summary (/check:resume last, then `claude
--resume <old session>`, where no crash notice shows) must hold the goal, the
constraint, the progress, the file with its hash, the validation result with
the version it ran against, the subagent, and the in-flight operations as
outcome unknown. Then (0.8.0) the task is finished with /check:complete;
/check:forget of it is refused while one of its sessions runs, and in a new
session (no crash notice for a finished task) it removes the task's journal.

The plugin is loaded from a copy (--plugin-dir; Claude Code writes types
into a mod folder it loads), or with --installed from a normal installation
(check@checkpoint from a local git-source marketplace into the scratch
config: accept_install.py); its runtime pre-built into the config's plugin
data folder with `env.sh ensure` (pixi install --locked). Nothing touches the
real Claude config or ~/.config/gateway (tests/tui refuses both). Prints one
line per check and a JSON summary; exits non-zero on any failure.
"""
from __future__ import annotations

import argparse
import hashlib
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
from accept_install import kill_marked, kill_started, note_marked, provide  # noqa: E402

RESULTS: list[tuple[str, bool]] = []


def check(name: str, ok: bool) -> bool:
    RESULTS.append((name, bool(ok)))
    print(f"  {'ok  ' if ok else 'FAIL'} {name}", flush=True)
    return bool(ok)


def request_text(rec: dict) -> str:
    """Everything a request sent the model (system and messages), as text."""
    b = body_json(rec) or {}
    return json.dumps({"system": b.get("system"), "messages": b.get("messages")}, ensure_ascii=False)


def wait(s: Session, cond, timeout: float, step: float = 0.2) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        s.pump(step)
    return cond()


def wait_main(s: Session, n: int, timeout: float) -> bool:
    """At least n main-loop requests, pumping the pty meanwhile (never FakeAPI.wait_main, which only
    sleeps: Claude Code then blocks writing to its terminal; acceptance_recovery.wait_main)."""
    return bool(wait(s, lambda: len(s.api.main_requests()) >= n, timeout, 0.05))


def has(s: Session, needle: str) -> bool:
    """Whether the screen history holds `needle`, ignoring all whitespace (the terminal wraps long rows
    mid-word; acceptance_recovery.has)."""
    return re.sub(r"\s+", "", needle) in re.sub(r"\s+", "", s.all_text())


def submit_for_request(s: Session, text: str, timeout: float = 60) -> str:
    """Submit `text` and return the text of the main-loop request it led to ('' if none came): a
    /check:* skill's hook result (<checkpoint-result>) reaches the model there, while the screen shows
    only the scripted reply."""
    n0 = len(s.api.main_requests())
    s.submit(text)
    if not wait_main(s, n0 + 1, timeout):
        return ""
    return request_text(s.api.main_requests()[-1])


def running(marker: str) -> bool:
    out = subprocess.run(["ps", "-A", "-ww", "-o", "command="], capture_output=True, text=True).stdout
    return any(marker in ln and "ps -A" not in ln for ln in out.splitlines())


def journal_records(home: Path) -> list[dict]:
    out = []
    for seg in sorted(home.glob("journal/*/seg-*.jsonl")):
        for line in seg.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", help="scratch root (a fresh folder under a temp base; default: a new temp dir)")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--installed", action="store_true",
                    help="install check@checkpoint from a local git-source marketplace instead of --plugin-dir")
    a = ap.parse_args()
    root = Path(a.root) if a.root else Path(tempfile.mkdtemp(prefix="ck-accept-"))
    root = safety.check_scratch(root, "scratch root")
    root.mkdir(parents=True, exist_ok=True)
    cfg = root / "cfg"
    proj = root / "proj dir ü"           # the workspace: a space and a non-ASCII letter
    plugin = root / "plugin"
    t0 = time.time()
    summary: dict = {"root": str(root)}
    try:
        # The plugin, as a copy of what `git add -A` would commit (or installed from it).
        plugin, pdirs, data, summary["plugin"] = provide(root, cfg, a.installed)
        # Its runtime, built the way the async SessionStart hook builds it.
        b = subprocess.run(["sh", str(plugin / "hooks" / "env.sh"), "ensure"], capture_output=True, text=True,
                           env=dict(os.environ, CLAUDE_PLUGIN_DATA=str(data),
                                    CHECKPOINT_HOME=str(root / "unused-state")), timeout=900)
        check("the plugin's runtime builds (pixi install --locked) into the config's plugin data", b.returncode == 0)
        summary["runtime_build_s"] = round(time.time() - t0, 1)
        # A project in git with a file to edit and a test script.
        proj.mkdir()
        genv = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        genv.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        (proj / "widget.py").write_text("def widget():\n    return 1\n")
        (proj / "run_tests.sh").write_text("#!/bin/sh\necho '2 passed'\nexit 0\n")
        for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                                       "commit", "-qm", "start"]):
            subprocess.run(["git", "-C", str(proj), *args], check=True, capture_output=True, env=genv)
        head = subprocess.run(["git", "-C", str(proj), "rev-parse", "HEAD"], capture_output=True, text=True,
                              env=genv).stdout.strip()
        home = safety.state_root_for(cfg) / ".local" / "state" / "checkpoint"
        env = {"CLAUDE_CODE_ENABLE_TODO_TOOLS": "1"}    # TaskCreate/TaskUpdate (off by default on 2.1.301)
        w = str(proj / "widget.py")
        replies = [
            {"tool": "mcp__plugin_check_jobs__checkpoint_note",
             "input": {"kind": "constraint", "text": "Never touch prod"}},
            {"tool": "TaskCreate", "input": {"subject": "Write the widget", "description": "widget() returns 2",
                                             "activeForm": "Writing the widget"}},
            {"tool": "TaskCreate", "input": {"subject": "Run the tests", "activeForm": "Running the tests"}},
            {"tool": "TaskUpdate", "input": {"taskId": "1", "status": "in_progress"}},
            {"tool": "Edit", "input": {"file_path": w, "old_string": "return 1", "new_string": "return 2"}},
            {"tool": "TaskUpdate", "input": {"taskId": "1", "status": "completed"}},
            {"tool": "TaskUpdate", "input": {"taskId": "2", "status": "in_progress"}},
            {"tool": "Bash", "input": {"command": "sh run_tests.sh", "description": "run the tests"}},
            {"tool": "Bash", "input": {"command": "sleep 6; echo stage-one", "description": "a pause to save in"}},
            {"tool": "Agent", "input": {"description": "Review the docs", "prompt": "Review the README",
                                        "subagent_type": "general-purpose"}},
            # The main loop and the subagent (it runs in the background) each
            # take one of these, in whichever order their requests come. (No
            # test-like words in them: those start a snapshot, which ingests.)
            # Not a leading `sleep`: Claude Code 2.1.310's Bash refuses a
            # foreground command whose first part is `sleep N` with N >= 25
            # ("Blocked: sleep 300 followed by: ...; use Monitor"), so `true; `
            # comes first (the summary check below matches from `sleep` on).
            {"tool": "Bash", "input": {"command": "true; sleep 300; echo deploy-wait-a", "description": "long a"}},
            {"tool": "Bash", "input": {"command": "true; sleep 301; echo deploy-wait-b", "description": "long b"}},
        ] + ["ok"] * 6
        print("session 1: the scripted task, then SIGKILL", flush=True)
        s1 = Session(root / "s1", proj, plugin_dirs=pdirs, replies=replies, config_dir=cfg,
                     permission_mode="bypassPermissions", extra_env=env, debug_file=True)
        try:
            s1.wait_ready(timeout=90)
            check("session 1 reaches the prompt (footer label: check: recording)",
                  wait(s1, lambda: "recording" in s1.text(), 30))
            s1.submit("Build the widget so widget() returns 2. Constraint: never touch prod.")
            check("the scripted steps run up to a 6 s command", wait(s1, lambda: running("echo stage-one"), 90))
            # Checkpoint now, mid-turn (the immediate command; its row shows
            # when the turn ends, so the journal is what proves it).
            s1.submit("/check-save before the long part")
            saved = wait(s1, lambda: any(r["kind"] == "checkpoint" for r in journal_records(home)), 30)
            check("/check-save mid-turn writes a checkpoint record (synced) while the command keeps running",
                  saved and running("echo stage-one"))
            both = wait(s1, lambda: running("deploy-wait-a") and running("deploy-wait-b"), 120)
            check("then a subagent and two long commands (one the subagent's)", both)
            note_marked("deploy-wait-")      # their shells and their `sleep` children, ended at the end
            old_sid = next((r["session"] for r in journal_records(home) if r["kind"] == "session.start"), "")
            waiting = len(list((home / "spool" / old_sid).glob("*.ev"))) if old_sid else 0
            summary["spool_waiting_at_kill"] = waiting
            os.kill(s1.pid, signal.SIGKILL)
            dead = wait(s1, lambda: not s1.alive(), 15)
            check("claude killed with SIGKILL (no Stop, no SessionEnd, no handoff)", dead)
            recs = journal_records(home)
            check("...no Stop or session end was recorded for it",
                  not any(r["kind"] in ("stop", "session.end") and r["session"] == old_sid for r in recs))
            check("...its last events (the subagent, the long commands) were still only in its spool, not the journal",
                  waiting > 0 and not any("deploy-wait" in json.dumps(r) for r in recs))
        finally:
            kill_marked("deploy-wait-")      # while each shell still holds its `sleep` child
            s1.close()
            kill_marked("deploy-wait-")
        task = next((r["task"] for r in journal_records(home) if r["session"] == old_sid), "")
        summary.update(task=task, old_session=old_sid)

        print("session 2: a new session in the same project", flush=True)
        s2 = Session(root / "s2", proj, plugin_dirs=pdirs, replies=["Resuming from the summary."] * 4,
                     config_dir=cfg, permission_mode="bypassPermissions", extra_env=env, debug_file=True)
        try:
            s2.wait_ready(timeout=90)
            # 0.8.0: the crash notice, for the person (the SessionStart context reaches only the model): the
            # killed task, its goal (clipped at 60 characters), and both ways to continue it, on screen.
            notice = bool(task) and wait(s2, lambda: has(s2, f"checkpoint: task {task} was interrupted"), 30)
            check("the new session shows the crash notice on screen: the killed task, its goal, `claude --resume "
                  "<killed session>` and `/check:resume <task>`",
                  notice and has(s2, '("Build the widget so widget() returns')
                  and has(s2, f"claude --resume {old_sid}") and has(s2, f"/check:resume {task} picks it up here"))
            if not notice:
                print("       (screen: " + re.sub(r"\s+", " ", s2.all_text())[-1500:] + ")")
            s2.submit("/check:resume last")
            got = wait_main(s2, 1, 60)
            reqs = s2.api.main_requests()
            first = request_text(reqs[0]) if reqs else ""
            check("its session start offers the killed task as interrupted (SessionStart context)",
                  got and task in first and "interrupted (no clean end)" in first)
            rq = next((request_text(r) for r in reversed(reqs) if "Recovery summary for task" in request_text(r)), "")
            summary["resume_request_chars"] = len(rq)
            hsh = hashlib.sha256((proj / "widget.py").read_bytes()).hexdigest()[:12]
            check("/check:resume last: the recovery summary reaches the model", bool(rq))
            check("...the goal", "Build the widget so widget() returns 2" in rq)
            check("...the constraint (checkpoint_note)", "constraint: Never touch prod" in rq)
            check("...the progress (steps and their recorded status)",
                  "[completed] Write the widget" in rq and "[in_progress] Run the tests" in rq
                  and "1 of 2 completed" in rq)
            check("...the file with its hash", "widget.py" in rq and hsh in rq)
            check("...the validation result with the version it ran against",
                  "sh run_tests.sh`: passed (no error reported)" in rq and f"started on HEAD {head[:10]}" in rq)
            check("...the in-flight operations as outcome unknown (never done)",
                  "sleep 300; echo deploy-wait-a" in rq and "sleep 301; echo deploy-wait-b" in rq
                  and "outcome unknown" in rq)
            check("...the subagent, its outcome unknown", "subagent " in rq and "no stop recorded" in rq)
            # The summary's header names it (0.7.1; it was in Evidence, which a
            # long scratch path pushed out of the resume context).
            check("...the checkpoint taken before the kill", "Last checkpoint: c_" in rq)
            if not rq:
                print(first[:3000])
        finally:
            s2.close()

        print("session 3: claude --resume <the killed session>", flush=True)
        s3 = Session(root / "s3", proj, plugin_dirs=pdirs, replies=["ok"] * 6, config_dir=cfg,
                     permission_mode="bypassPermissions", extra_env=env, args=["--resume", old_sid])
        try:
            s3.wait_ready(timeout=90)
            s3.submit("go on")
            wait_main(s3, 1, 60)
            reqs = s3.api.main_requests()
            txt = request_text(reqs[0]) if reqs else ""
            check("resuming the killed session brings its recovery summary in at the start (<checkpoint-recovery>)",
                  "<checkpoint-recovery>" in txt and "Build the widget so widget() returns 2" in txt
                  and "outcome unknown" in txt)
            # The notice is for fresh starts (startup, /clear), never for the session being resumed.
            check("...and no crash notice on screen when the killed session itself is resumed",
                  not has(s3, "was interrupted"))
            # 0.8.0: /check:forget. First the task is finished (/check:complete), then forgetting it is
            # refused while one of its sessions (this one) runs: nothing removed.
            rq = submit_for_request(s3, "/check:complete")
            check("/check:complete marks the task complete (the hook's result reaches the model)",
                  bool(task) and f"Task {task} marked complete" in rq)
            rq = submit_for_request(s3, f"/check:forget {task}")
            check("/check:forget <task> while one of its sessions runs (this one): refused, nothing removed",
                  bool(task) and "Not forgotten" in rq and "runs (this one)" in rq and "Nothing was removed" in rq
                  and (home / "journal" / task).is_dir())
        finally:
            s3.close()

        print("session 4: a new session forgets the finished task", flush=True)
        s4 = Session(root / "s4", proj, plugin_dirs=pdirs, replies=["ok"] * 4, config_dir=cfg,
                     permission_mode="bypassPermissions", extra_env=env, debug_file=True)
        try:
            s4.wait_ready(timeout=90)
            mine = [r for r in journal_records(home) if r.get("task") == task]
            n_recs, n_cp = len(mine), sum(1 for r in mine if r.get("kind") == "checkpoint")
            rq = submit_for_request(s4, f"/check:forget {task}")
            # (the first request carries the SessionStart context, so the start's hooks, the notice's
            # included, had answered by then)
            check("a fresh session after /check:complete: no crash notice for the finished task",
                  bool(task) and "This session's id:" in rq and not has(s4, f"checkpoint: task {task} was interrupted"))
            summary["forget_result"] = (rq[rq.find("Forgot task"):][:400] if "Forgot task" in rq else rq[-400:])
            check("/check:forget <the finished task> from another session: it says it removed the task's journal "
                  "(exactly the records and checkpoints it held)",
                  bool(task) and f"Forgot task {task}" in rq and "removed its journal" in rq
                  and f"({n_recs} record(s), {n_cp} checkpoint(s))" in rq)
            check("...its journal folder is gone and no record of it is left in any journal",
                  bool(task) and not (home / "journal" / task).exists()
                  and not any(r.get("task") == task for r in journal_records(home)))
            check("...the files in the project are untouched (widget.py as the task left it)",
                  (proj / "widget.py").read_text() == "def widget():\n    return 2\n")
        finally:
            s4.close()
        summary["seconds"] = round(time.time() - t0, 1)
    finally:
        kill_marked("deploy-wait-")
        kill_marked("echo stage-one")
        left = kill_started()
        check("nothing the scripted commands started is left running (the long commands' shells and their "
              "`sleep` children)", not left)
        if left:
            print("       (left running: " + "; ".join(left) + ")")
        if not a.keep:
            shutil.rmtree(root, ignore_errors=True)
    failed = [n for n, ok in RESULTS if not ok]
    summary.update(passed=len(RESULTS) - len(failed), failed=len(failed))
    print(json.dumps(summary))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
