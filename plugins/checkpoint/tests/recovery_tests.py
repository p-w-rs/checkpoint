# Recovery (PLAN-v4 phase 3): reconciliation before resume, the controls
# (/check:resume [checkpoint], resume-now, cancel, stop, the paused gate),
# checkpoint management (list, diff, select, complete, export, retention),
# lineage, and background completions linked to their starts. Run by
# tests/run_tests.py after tests/journal_tests.py, in the same namespace
# (check(), hook(), ctx(), jbase(), jgit(), jrecs(), task_of(), section(),
# scan_for(), SECRETS/CORPUS, fake_claude(), claude_wrap(), TMP, HENV ...).
# Synthetic secrets are generated there and never printed.
# ruff: noqa: F821  (names come from run_tests.py and journal_tests.py)
from checkpoint import recovery  # noqa: E402

RT = TMP / "recovery-tests"
RT.mkdir()


def rpay(sid, cwd, tp, **kw):
    return jbase(sid, cwd, tp, **kw)


def flags_of(sid):
    return state.is_paused(sid)


def end_group(proc):
    """A shell started in a session of its own, with what it started (its sleep)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.wait()


# --- capture: mid-turn prompts, task notifications, background-output tools ----------
print("capture: prompts typed mid-turn, task notifications linked to their starts:")
BP = RT / "bg proj"
BP.mkdir()
jgit(BP, "init", "-q")
(BP / "app.py").write_text("x = 1\n")
jgit(BP, "add", "-A")
jgit(BP, "commit", "-qm", "start")
CB = fake_claude()
EB = {"CLAUDE_PID": str(CB.pid), **HENV}
SBG = "sess-bg-1111"
TBG = RT / "bg.jsonl"
TBG.write_text("")
hook("SessionStart", rpay(SBG, BP, TBG, source="startup"), EB)
hook("UserPromptSubmit", rpay(SBG, BP, TBG, prompt="Run the slow suite in the background", prompt_id="pq1"), EB)
# Typed while that turn runs: the same prompt_id (measured on 2.1.301).
hook("UserPromptSubmit", rpay(SBG, BP, TBG, prompt="and also lint it", prompt_id="pq1"), EB)
capture.ingest(SBG)
TBGT = task_of(SBG)
prompts = [r["data"]["text"] for r in jrecs(TBGT) if r["kind"] == "prompt"]
check("a prompt typed mid-turn (same prompt_id as the turn's) is recorded too, not dropped as a duplicate",
      lambda: prompts == ["Run the slow suite in the background", "and also lint it"])
hook("UserPromptSubmit", rpay(SBG, BP, TBG, prompt="and also lint it", prompt_id="pq1"), EB)
capture.ingest(SBG)
check("...the same delivery twice is still one record",
      lambda: [r["data"]["text"] for r in jrecs(TBGT) if r["kind"] == "prompt"].count("and also lint it") == 1)
BG_CMD = "pytest -q tests/slow"
hook("PreToolUse", rpay(SBG, BP, TBG, tool_name="Bash", tool_use_id="toolu_bg1",
                        tool_input={"command": BG_CMD, "run_in_background": True}), EB)
hook("PostToolUse", rpay(SBG, BP, TBG, tool_name="Bash", tool_use_id="toolu_bg1",
                         tool_input={"command": BG_CMD, "run_in_background": True},
                         tool_response={"stdout": "", "stderr": "", "interrupted": False, "backgroundTaskId": "b7x2"}), EB)
hook("PreToolUse", rpay(SBG, BP, TBG, tool_name="Bash", tool_use_id="toolu_bg2",
                        tool_input={"command": "make deploy-staging", "run_in_background": True}), EB)
hook("PostToolUse", rpay(SBG, BP, TBG, tool_name="Bash", tool_use_id="toolu_bg2",
                         tool_input={"command": "make deploy-staging", "run_in_background": True},
                         tool_response={"stdout": "", "backgroundTaskId": "b8y3"}), EB)
hook("PreToolUse", rpay(SBG, BP, TBG, tool_name="Bash", tool_use_id="toolu_bg3",
                        tool_input={"command": "npm run watch", "run_in_background": True}), EB)
hook("PostToolUse", rpay(SBG, BP, TBG, tool_name="Bash", tool_use_id="toolu_bg3",
                         tool_input={"command": "npm run watch", "run_in_background": True},
                         tool_response={"stdout": "", "backgroundTaskId": "b9z4"}), EB)
capture.ingest(SBG)
s0 = summary.build(TBGT)
check("before any notification: every background start reads 'its result was not observed', the test never 'passed'",
      lambda: s0.text.count("its result was not observed") >= 3 and "passed" not in section(s0.text, "Validation"))


def note(tid, task_id, status, summ, extra=""):
    st_ = f"\n<status>{status}</status>" if status else ""
    return (f"<task-notification>\n<task-id>{task_id}</task-id>\n<tool-use-id>{tid}</tool-use-id>\n"
            f"<output-file>/tmp/x/tasks/{task_id}.output</output-file>{st_}\n<summary>{summ}</summary>\n"
            f"</task-notification>{extra}")


hook("UserPromptSubmit", rpay(SBG, BP, TBG, prompt=note("toolu_bg1", "b7x2", "completed",
                                                         'Background command "slow suite" completed (exit code 0)')), EB)
hook("UserPromptSubmit", rpay(SBG, BP, TBG, prompt=note("toolu_bg2", "b8y3", "failed",
                                                         'Background command "deploy" failed with exit code 3')), EB)
hook("UserPromptSubmit", rpay(SBG, BP, TBG, prompt=note(
    "toolu_bg3", "b9z4", "", 'Background command "watch" appears to be waiting for interactive input',
    " Last output: Continue? [y/N]")), EB)
capture.ingest(SBG)
s1 = summary.build(TBGT)
bgr = [r for r in jrecs(TBGT) if r["kind"] == "background.result"]
check("task notifications become background.result records keyed by the start's tool_use_id: status, the exit code "
      "they state, final or not; never the output file's contents or the raw tail",
      lambda: {(r["data"]["tool_use_id"], r["data"]["status"], r["data"].get("exit"), r["data"]["final"]) for r in bgr}
      == {("toolu_bg1", "completed", 0, True), ("toolu_bg2", "failed", 3, True), ("toolu_bg3", "notice", None, False)}
      and not any("Continue?" in json.dumps(r) for r in bgr))
check("the summary links them: the background test 'passed (exit 0)', reported by the notification (observed)",
      lambda: "passed (exit 0)" in section(s1.text, "Validation") and "task notification" in section(s1.text, "Validation"))
check("...the failed deploy is a failure, with its exit code", lambda: "make deploy-staging" in section(s1.text, "Failures")
      and "FAILED (exit 3)" in section(s1.text, "Failures"))
check("...a check-in (no status) is not an end: still 'its result was not observed', 'was waiting for input'",
      lambda: "npm run watch" in section(s1.text, "In flight") and "was waiting for input" in section(s1.text, "In flight"))
hook("PreToolUse", rpay(SBG, BP, TBG, tool_name="TaskOutput", tool_use_id="to1", tool_input={"task_id": "b9z4"}), EB)
hook("PostToolUse", rpay(SBG, BP, TBG, tool_name="TaskOutput", tool_use_id="to1", tool_input={"task_id": "b9z4"},
                         tool_response={"task": {"status": "killed", "exitCode": 143}}), EB)
capture.ingest(SBG)
s2 = summary.build(TBGT)
check("a TaskOutput-like result naming the background id links too (here: stopped, exit 143)",
      lambda: "npm run watch" in section(s2.text, "Failures") and "was stopped (exit 143)" in s2.text)

# An aborted turn's running Bash call goes on in the background with no
# PostToolUse at all (measured, 2.1.301): only its intent and, later, the
# notification of its end.
LIVE = "sleep 61; echo aborted-turn"
hook("PreToolUse", rpay(SBG, BP, TBG, tool_name="Bash", tool_use_id="toolu_ab1", tool_input={"command": LIVE}), EB)
time.sleep(1.1)
ab_sh = subprocess.Popen([SHELL, "-c", claude_wrap(LIVE)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
CHILDREN.append(ab_sh)
wait_for(lambda: bool(procs.command_shells(LIVE)), 10)
capture.ingest(SBG)
check("a call whose turn was aborted (no outcome) while its process runs: the summary says 'still running: a "
      "process runs it now' (process table), not 'outcome unknown'",
      lambda: "still running: a process runs it now" in section(summary.build(TBGT).text, "In flight"))
end_group(ab_sh)
hook("UserPromptSubmit", rpay(SBG, BP, TBG, prompt=note("toolu_ab1", "bab1", "completed",
                                                         'Background command "x" completed (exit code 0)')), EB)
capture.ingest(SBG)
check("...its notification later: done, linked to the intent (no result came back to the call; then completed, exit 0)",
      lambda: "aborted-turn" in section(summary.build(TBGT).text, "Done")
      and "no result came back to the call" in summary.build(TBGT).text
      and {o.op: o.cls for o in recovery.reconcile(TBGT, "").ops}.get("toolu_ab1") == "completed")

# --- reconciliation ------------------------------------------------------------------------
print("reconciliation before resume (files, git, validation, instructions, operations, lineage):")
RP = RT / "recon proj"
RP.mkdir()
REMOTE = RT / "remote.git"
subprocess.run(["git", "init", "-q", "--bare", str(REMOTE)], check=True, capture_output=True, env=JGIT)
jgit(RP, "init", "-q", "-b", "main")
for n in ("a.py", "b.py", "c.py"):
    (RP / n).write_text(f"{n} = 1\n")
jgit(RP, "add", "-A")
jgit(RP, "commit", "-qm", "v1")
jgit(RP, "remote", "add", "origin", str(REMOTE))
H1 = subprocess.run(["git", "-C", str(RP), "rev-parse", "HEAD"], capture_output=True, text=True, env=JGIT).stdout.strip()
CR = fake_claude()
ER = {"CLAUDE_PID": str(CR.pid), **HENV}
SR1 = "sess-recon-aaaa"
TR1 = RT / "recon.jsonl"
TR1.write_text("")
hook("SessionStart", rpay(SR1, RP, TR1, source="startup"), ER)
hook("UserPromptSubmit", rpay(SR1, RP, TR1, prompt="Refactor a.py and ship it", prompt_id="r1"), ER)


def edit(sid, tid, path, new, env, tp=TR1, cwd=RP, outcome=True):
    p = {"file_path": str(path), "old_string": "x", "new_string": "y"}
    hook("PreToolUse", rpay(sid, cwd, tp, tool_name="Edit", tool_use_id=tid, tool_input=p), env)
    if outcome:
        Path(path).write_text(new)
        hook("PostToolUse", rpay(sid, cwd, tp, tool_name="Edit", tool_use_id=tid, tool_input=p,
                                 tool_response={"type": "update"}), env)


edit(SR1, "re1", RP / "a.py", "a.py = 2\n", ER)
hook("PreToolUse", rpay(SR1, RP, TR1, tool_name="Bash", tool_use_id="rt1", tool_input={"command": "pytest -q"}), ER)
hook("PostToolUse", rpay(SR1, RP, TR1, tool_name="Bash", tool_use_id="rt1", tool_input={"command": "pytest -q"},
                         tool_response={"stdout": "3 passed", "stderr": "", "interrupted": False}), ER)
ok_, msg_ = core.save(SR1, "before shipping")
C1 = (msg_.split("Checkpoint ", 1)[1].split(" ", 1)[0]) if ok_ else ""
check("a checkpoint saved before the risky part (its record carries a one-line summary)",
      lambda: ok_ and C1.startswith("c_")
      and next(r for r in jrecs(task_of(SR1)) if r["kind"] == "checkpoint")["data"].get("line"))
# After the checkpoint: the task edits b.py, runs a push whose result is lost, starts an edit
# of c.py that never reports back, a long command, and a call the gate denies.
edit(SR1, "re2", RP / "b.py", "b.py = 2\n", ER)
hook("UserPromptSubmit", rpay(SR1, RP, TR1, prompt="Only push to the staging branch", prompt_id="r2"), ER)
PUSH = "git push origin HEAD:refs/heads/staging"
hook("PreToolUse", rpay(SR1, RP, TR1, tool_name="Bash", tool_use_id="rpush", tool_input={"command": PUSH}), ER)
subprocess.run(["git", "-C", str(RP), "push", "-q", "origin", "HEAD:refs/heads/staging"], check=True,
               capture_output=True, env=JGIT)   # the remote write happened; its result never comes back
edit(SR1, "re3", RP / "c.py", "c.py = 2\n", ER, outcome=False)
LONG = "sleep 87; echo still-going"
hook("PreToolUse", rpay(SR1, RP, TR1, tool_name="Bash", tool_use_id="rlong", tool_input={"command": LONG}), ER)
time.sleep(1.1)
long_sh = subprocess.Popen([SHELL, "-c", claude_wrap(LONG)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           start_new_session=True)
CHILDREN.append(long_sh)
wait_for(lambda: bool(procs.command_shells(LONG)), 10)
hook("PreToolUse", rpay(SR1, RP, TR1, tool_name="Bash", tool_use_id="rgone",
                        tool_input={"command": "rm -rf build && make"}), ER)
# A shell running the same command that started BEFORE the call: not the call's.
OLDER = "sleep 89; echo started-earlier"
older_sh = subprocess.Popen([SHELL, "-c", claude_wrap(OLDER)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
CHILDREN.append(older_sh)
wait_for(lambda: bool(procs.command_shells(OLDER)), 10)
time.sleep(2.2)
hook("PreToolUse", rpay(SR1, RP, TR1, tool_name="Bash", tool_use_id="rolder", tool_input={"command": OLDER}), ER)
TRK1 = task_of(SR1)
capture.ingest(SR1)
capture.record(SR1, [{"kind": "tool.outcome", "op": "rdeny", "session": SR1,
                      "data": {"tool": "Bash", "ok": False, "denied": "paused"}}])
capture.record(SR1, [{"kind": "tool.intent", "op": "rdeny", "session": SR1,
                      "data": {"tool": "Bash", "input": {"command": "make release"}}}])
CR.kill()       # SIGKILL: no Stop, no outcome for the push, the edit, the long command
CR.wait()
# Meanwhile the user changes a.py, commits, and leaves an untracked file.
(RP / "a.py").write_text("a.py = 'the user wrote this'\n")
jgit(RP, "commit", "-qam", "user commit")
(RP / "notes.txt").write_text("todo\n")
H2 = subprocess.run(["git", "-C", str(RP), "rev-parse", "HEAD"], capture_output=True, text=True, env=JGIT).stdout.strip()
remote_before = subprocess.run(["git", "--git-dir", str(REMOTE), "rev-parse", "refs/heads/staging"],
                               capture_output=True, text=True, env=JGIT).stdout.strip()
r1 = recovery.reconcile(TRK1, "", checkpoint=C1, cwd=str(RP), selected=True)
cls = {o.op: o.cls for o in r1.ops}
fhow = {Path(f["path"]).name: f["how"] for f in r1.files}
txt = r1.text()
check("files: a.py changed outside the task since the checkpoint (kept), b.py changed by the task after it",
      lambda: fhow.get("a.py") == "outside" and fhow.get("b.py") == "task")
check("...the outside change is described as kept and never reverted",
      lambda: "keep it" in next(f["text"] for f in r1.files if f["path"].endswith("a.py"))
      and "never revert them" in txt)
check("git: HEAD moved, new commits on top of the saved one; the untracked file is listed as dirty now",
      lambda: r1.git.get("head_moved") and r1.git.get("relation", "").startswith("new commits on top")
      and any(p.endswith("notes.txt") for p in r1.git.get("newly_dirty") or []))
check("validation: the pytest result is STALE (its HEAD and hashes no longer match)",
      lambda: any(v["status"] == "stale" and "pytest" in v["command"] for v in r1.validations)
      and "STALE: `pytest -q`" in txt)
check("the remote push whose result was lost: outcome unknown, flagged as an external effect (push)",
      lambda: cls.get("rpush") == "unknown" and next(o for o in r1.ops if o.op == "rpush").external == "push")
check("...'Check first' puts it first: check whether the push landed, and don't run it again until known",
      lambda: section(txt, "Check first").strip().startswith("1. Bash `git push origin HEAD:refs/heads/staging`")
      and "git ls-remote" in section(txt, "Check first") and "do not run it again until you know" in txt)
check("the long command: still running (a process with that exact command, started after its intent)",
      lambda: cls.get("rlong") == "running" and "process table" in next(o.why for o in r1.ops if o.op == "rlong"))
check("the edit of c.py with no result: outcome unknown, with the file's evidence (it still has its before-hash)",
      lambda: cls.get("re3") == "unknown" and "looks unapplied" in next(o.check for o in r1.ops if o.op == "re3"))
check("a process with the same command that started before the call is not the call's (a command line alone "
      "proves nothing): outcome unknown, not running", lambda: cls.get("rolder") == "unknown")
check("a call with no result and no process now: outcome unknown, 'no process runs it now'",
      lambda: cls.get("rgone") == "unknown" and "no process runs it now" in next(o.why for o in r1.ops if o.op == "rgone"))
check("a call the gate denied: never dispatched", lambda: cls.get("rdeny") == "never")
check("instructions: the one in force at the checkpoint and the one given since (newer, takes precedence)",
      lambda: r1.instructions["at_checkpoint"]["text"] == "Refactor a.py and ship it"
      and [x["text"] for x in r1.instructions["since"]] == ["Only push to the staging branch"]
      and "takes precedence" in section(txt, "Instructions"))
check("lineage: the original session is gone, and the exact `claude --resume <id>` is shown",
      lambda: r1.lineage.get("original") == SR1 and not r1.lineage.get("original_alive")
      and f"claude --resume {SR1}" in section(txt, "Lineage"))
check("never 'redo the stage from the start' (the old guidance), and unknowns are not failures",
      lambda: "from the start" not in txt and "redo" not in txt.lower().replace("redone", "")
      and "Treat an unknown outcome as unknown" in txt)
check("reconciling only reads: no file, the repository or the remote changed",
      lambda: (RP / "a.py").read_text() == "a.py = 'the user wrote this'\n" and (RP / "c.py").read_text() == "c.py = 1\n"
      and subprocess.run(["git", "--git-dir", str(REMOTE), "rev-parse", "refs/heads/staging"], capture_output=True,
                         text=True, env=JGIT).stdout.strip() == remote_before
      and subprocess.run(["git", "-C", str(RP), "rev-parse", "HEAD"], capture_output=True, text=True,
                         env=JGIT).stdout.strip() == H2)
end_group(long_sh)
end_group(older_sh)
r1b = recovery.reconcile(TRK1, "", checkpoint=C1, cwd=str(RP))
check("once the long command's process is gone: outcome unknown (a saved command alone proves nothing)",
      lambda: {o.op: o.cls for o in r1b.ops}.get("rlong") == "unknown")
_rt = procs.table
procs.table = lambda: {}
try:
    r1c = recovery.reconcile(TRK1, "", checkpoint=C1, cwd=str(RP))
finally:
    procs.table = _rt
check("without a process table: nothing is called running or gone for sure; the limit is said",
      lambda: {o.op: o.cls for o in r1c.ops}.get("rgone") == "unknown"
      and "couldn't be read, so it may still run" in next(o.why for o in r1c.ops if o.op == "rgone")
      and any("process table" in p for p in r1c.problems))

# A new session picks the task up from the checkpoint (select): /check:resume <checkpoint>.
CR2 = fake_claude()
ER2 = {"CLAUDE_PID": str(CR2.pid), **HENV}
SR2 = "sess-recon-bbbb"
TR2 = RT / "recon2.jsonl"
TR2.write_text("")
hook("SessionStart", rpay(SR2, RP, TR2, source="startup"), ER2)
out = hook("UserPromptSubmit", rpay(SR2, RP, TR2, prompt=f"/check:resume {C1}", prompt_id="s1"), ER2)
rc = ctx(out)
check("/check:resume <checkpoint> in a new session: adopts its task (an explicit continuation naming its parent) "
      "and reconciles against that checkpoint",
      lambda: task_of(SR2) == TRK1 and f"against checkpoint {C1}" in rc and "You chose this checkpoint" in rc
      and any(r["kind"] == "session.adopt" and r["data"].get("parent") == SR1 and r["data"].get("continuation")
              for r in jrecs(TRK1)))
check("...the resume context: Check first, the classes, files, git, validation, instructions, then the summary",
      lambda: all(f"## {s}" in rc for s in ("Check first", "Interrupted operations", "Files", "Git", "Validation",
                                            "Instructions", "Lineage"))
      and "Recovery summary for task" in rc and "[outcome unknown] Bash `git push" in rc)
check("...recorded: checkpoint.selected and a reconcile record with the counts",
      lambda: any(r["kind"] == "checkpoint.selected" and r["data"].get("id") == C1 for r in jrecs(TRK1))
      and any(r["kind"] == "reconcile" and r["data"]["counts"]["unknown"] >= 3
              and r["data"]["counts"]["external_unknown"] >= 1 for r in jrecs(TRK1)))
check("...nothing was repeated: the remote still has exactly the one push",
      lambda: subprocess.run(["git", "--git-dir", str(REMOTE), "rev-parse", "refs/heads/staging"],
                             capture_output=True, text=True, env=JGIT).stdout.strip() == remote_before)

# The original session alive in another process: say so, and where.
CR3 = fake_claude()
SR3 = "sess-alive-cccc"
TR3 = RT / "alive.jsonl"
TR3.write_text("")
hook("SessionStart", rpay(SR3, RP, TR3, source="startup"), {"CLAUDE_PID": str(CR3.pid), **HENV})
hook("UserPromptSubmit", rpay(SR3, RP, TR3, prompt="A live task", prompt_id="l1"), {"CLAUDE_PID": str(CR3.pid), **HENV})
capture.ingest(SR3)
CR4 = fake_claude()
SR4 = "sess-other-dddd"
TR4 = RT / "other.jsonl"
TR4.write_text("")
hook("SessionStart", rpay(SR4, RP, TR4, source="startup"), {"CLAUDE_PID": str(CR4.pid), **HENV})
out = hook("UserPromptSubmit", rpay(SR4, RP, TR4, prompt=f"/check:resume {task_of(SR3)}", prompt_id="o1"),
           {"CLAUDE_PID": str(CR4.pid), **HENV})
check("the task's original session still running elsewhere: 'Check first' says continue there; lineage names its pid",
      lambda: f"{SR3} is still running in another Claude process (pid {CR3.pid})" in ctx(out)
      and any(c["session"] == SR3 for c in recovery.reconcile(task_of(SR3), SR4).lineage["concurrent"]))

EXT = {"git push origin main": "push", "git -C repo push": "push", "ssh host 'systemctl restart x'": "remote",
       "scp a.txt host:/srv/": "remote", "curl -X POST https://api.example/x": "http", "kubectl apply -f k.yaml": "deploy",
       "make deploy-staging": "deploy", "./deploy.sh prod": "deploy", "npm publish": "publish",
       "gh pr create --fill": "publish", "psql -c 'delete from t'": "db", "sudo systemctl stop x": "system",
       "pytest -q": "", "cat deploy.md": "", "curl https://example.com": "", "sshd -t": "", "ls -la": "",
       "rm -rf build && make": ""}
bad = {c: recovery.external_effect("Bash", {"command": c}) for c, want in EXT.items()
       if recovery.external_effect("Bash", {"command": c}) != want}
check("external effects judged from the command (push, remote, HTTP write, deploy, publish, database, system), "
      "not from a file name or a read", lambda: not bad)
if bad:
    print(f"       ({bad})")
check("...an MCP tool that isn't a read counts; one that reads doesn't",
      lambda: recovery.external_effect("mcp__srv__create_issue", {}) == "mcp"
      and recovery.external_effect("mcp__srv__list_issues", {}) == "")

# --- pause, resume in place ------------------------------------------------------------------
print("pause saves a checkpoint; a typed message resumes after reconciling against it:")
PP = RT / "pause proj"
PP.mkdir()
jgit(PP, "init", "-q")
(PP / "m.py").write_text("m = 1\n")
jgit(PP, "add", "-A")
jgit(PP, "commit", "-qm", "m")
CP5 = fake_claude()
EP = {"CLAUDE_PID": str(CP5.pid), **HENV}
SP5 = "sess-pause-eeee"
TP5 = RT / "pause.jsonl"
TP5.write_text("")
hook("SessionStart", rpay(SP5, PP, TP5, source="startup"), EP)
hook("UserPromptSubmit", rpay(SP5, PP, TP5, prompt="Tune m.py", prompt_id="m1"), EP)
edit(SP5, "pm1", PP / "m.py", "m = 2\n", EP, tp=TP5, cwd=PP)
hook("UserPromptSubmit", rpay(SP5, PP, TP5, prompt="/check:pause", prompt_id="m2"), EP)
hook("PreToolUse", rpay(SP5, PP, TP5, tool_name="Bash", tool_use_id="pb1", tool_input={"command": "ls"}), EP)
hook("Stop", rpay(SP5, PP, TP5), EP)
hook("Stop", rpay(SP5, PP, TP5), EP)
pcps = [r for r in jrecs(task_of(SP5)) if r["kind"] == "checkpoint"]
check("a pause saves one journal checkpoint (reason 'pause'), once per pause however often its checkpoint is refreshed",
      lambda: [r["data"]["reason"] for r in pcps] == ["pause"])
for cmd, args in (("/check:list", ""), ("/check:diff", ""), ("/check:stop", ""), ("/check:status", ""),
                  ("/check:export", ""), ("/check:save", " while paused")):
    out = hook("UserPromptSubmit", rpay(SP5, PP, TP5, prompt=cmd + args, prompt_id=f"g{cmd}"), EP)
    check(f"while paused, {cmd} answers and doesn't resume (no gate blocks the recovery controls)",
          lambda out=out: "<checkpoint-result>" in ctx(out) and state.is_paused(SP5))
(PP / "m.py").write_text("m = 'changed while paused'\n")
out = hook("UserPromptSubmit", rpay(SP5, PP, TP5, prompt="ok, carry on", prompt_id="m3"), EP)
rcx = ctx(out)
check("a typed message resumes, after reconciling against the pause's checkpoint: m.py changed while paused (kept)",
      lambda: not state.is_paused(SP5) and "Reconciled before resuming" in rcx and "m.py" in section(rcx, "Files")
      and "changed outside this task" in section(rcx, "Files")
      and "the message that resumes the task (the newest instruction): `ok, carry on`" in rcx)
check("...and a journal record of the reconciliation", lambda: any(r["kind"] == "reconcile" for r in jrecs(task_of(SP5))))

# The old guidance: `pause now` killed a command -> check, never redo from the start.
cp_k = {"session_id": SP5, "paused_ts": time.time() - 60, "interrupted": [{"command": "make all", "tool_use_id": "k9"}],
        "jobs": []}
kctx = core.resume_context(None, cp_k, str(PP), sid=SP5)
check("a command `pause now` stopped: the resume says partial effects are possible and to check before running "
      "any of it again; never 'redo it from the start of the stage'",
      lambda: "`pause now` stopped `make all`" in kctx and "Check what it had done" in kctx
      and "from the start" not in kctx)
check("...and the tracker template the model gets says the same of an 'interrupted' stage (no 'redo from start')",
      lambda: "redo from start" not in tracker.TEMPLATE and "check what it already did before redoing" in tracker.TEMPLATE)

hook("PreToolUse", rpay(SP5, PP, TP5, tool_name="Bash", tool_use_id="pk1", tool_input={"command": "make all"}), EP)
capture.ingest(SP5)
capture.record(SP5, [{"kind": "op.killed", "op": "pk1", "session": SP5,
                      "data": {"command": "make all", "processes": 2, "sigkill": False}}])
check("an operation `pause now` stopped (op.killed, no outcome): confirmed failed or canceled, never unknown",
      lambda: {o.op: o.cls for o in recovery.reconcile(task_of(SP5), SP5).ops}.get("pk1") == "failed"
      and "stopped by `pause now`" in next(o.why for o in recovery.reconcile(task_of(SP5), SP5).ops if o.op == "pk1"))

# --- resume-now (the mod's /check-resume) ---------------------------------------------------
print("resume-now (the immediate /check-resume): lifts the pause, the next prompt gets the context:")
hook("UserPromptSubmit", rpay(SP5, PP, TP5, prompt="pause", prompt_id="m4"), EP)
check("paused again (plain pause)", lambda: state.is_paused(SP5))
r = subprocess.run([sys.executable, str(ROOT / "run.py"), "resume-now", SP5], capture_output=True, text=True,
                   env=dict(os.environ, **EP), timeout=60)
check("run.py resume-now: the flag is lifted at once, the resume pending, the person told what reconciling found",
      lambda: r.returncode == 0 and not state.is_paused(SP5) and state.resume_pending(SP5)
      and r.stdout.startswith("Resumed: Reconciled task"))
out = hook("UserPromptSubmit", rpay(SP5, PP, TP5, prompt="Resume the paused task (I typed /check-resume).",
                                    prompt_id="m5"), EP)
check("...the prompt the mod sends then carries the resume context", lambda: "<checkpoint-resume>" in ctx(out)
      and not state.resume_pending(SP5))
r = subprocess.run([sys.executable, str(ROOT / "run.py"), "resume-now", SP5], capture_output=True, text=True,
                   env=dict(os.environ, **EP), timeout=60)
check("resume-now when not paused: says so, sets nothing pending", lambda: r.stdout.startswith("This session wasn't paused")
      and not state.resume_pending(SP5))

# --- pause policies for an in-flight command -------------------------------------------------
print("per-command pause policy: plain pause leaves a running command and says so; interrupt is now:")
check("`pause interrupt` and `/check:pause interrupt` parse as `now`",
      lambda: transcript.parse_command("pause interrupt") == ("pause", ["now"])
      and transcript.parse_command("/check:pause interrupt") == ("pause", ["now"]))
state.p("running").mkdir(parents=True, exist_ok=True)
state.write_json(state.p("running", "pol1.json"), {"session_id": "sess-pol", "tool_use_id": "pol1",
                                                   "tool_input": {"command": "sleep 300; make all"}})
msg = core.pause("sess-pol", [], by="command")
check("a plain pause with a foreground command running: 'Left running: `sleep 300; make all`', and how to stop it",
      lambda: "Left running: `sleep 300; make all`" in msg and "pause interrupt" in msg)
state.remove(state.p("running", "pol1.json"))
state.clear_paused("sess-pol")
check("kill details say exactly what happened (processes, SIGTERM or SIGKILL, survivors)",
      lambda: core.kill_detail({"command": "make", "processes": 3, "sigkill": True, "survivors": 1})
      == "`make` (3 process(es) ended: SIGTERM, then SIGKILL for what was left; 1 survived SIGKILL)")

# --- list, diff --------------------------------------------------------------------------------
print("checkpoint list and diff:")
lst = core.list_checkpoints(SR2)
check("/check:list: every checkpoint of the task with time, seq range, its one-line summary and staleness",
      lambda: C1 in lst and "seq 1-" in lst and "stale:" in lst and "file(s) changed since" in lst
      and "HEAD moved" in lst and "(newest)" in lst)
ok2, msg2 = core.save(SR2, "after the user's commit")
C2 = msg2.split("Checkpoint ", 1)[1].split(" ", 1)[0] if ok2 else ""
lst2 = core.list_checkpoints(SR2)
check("...a fresh checkpoint reads current, the older one stale",
      lambda: f"{C2}" in lst2 and lst2.split(C2, 1)[1].split("\n")[0].count("[current]") == 1)
dtx, dd = recovery.diff(TRK1, C1, C2)
check("/check:diff <a> <b>: files, plan, operations, validation, git and instructions between them",
      lambda: all(f"## {s}" in dtx for s in ("Files", "Plan", "Operations", "Validation", "Git", "Instructions"))
      and "a.py" in section(dtx, "Files") and "git push origin" in section(dtx, "Operations")
      and "Only push to the staging branch" in section(dtx, "Instructions") and "HEAD" in section(dtx, "Git"))
(RP / "b.py").write_text("b.py = 'edited after C2'\n")
dnow = core.diff_text(SR2, [C2])
check("/check:diff <checkpoint>: against now (b.py changed since)", lambda: "-> now" in dnow and "b.py" in dnow)
out = hook("UserPromptSubmit", rpay(SR2, RP, TR2, prompt=f"/check:diff {C1} {C2}", prompt_id="d1"), ER2)
check("...through the prompt hook too", lambda: "## Files" in ctx(out))

# --- cancel, complete, stop ----------------------------------------------------------------------
print("cancel (child-work policy), complete, stop:")
CJ = fake_claude()
EJ = {"CLAUDE_PID": str(CJ.pid), **HENV}
SJ = "sess-cancel-ffff"
TJ = RT / "cancel.jsonl"
TJ.write_text("")
hook("SessionStart", rpay(SJ, PP, TJ, source="startup"), EJ)
hook("UserPromptSubmit", rpay(SJ, PP, TJ, prompt="Train two models", prompt_id="j1"), EJ)
capture.ingest(SJ)
TJT = task_of(SJ)
j1 = jobs.start("train-a", command="sleep 120", cwd=str(PP), session_id=SJ, task_id=TJT)["job"]
j2 = jobs.start("train-b", command="sleep 121", cwd=str(PP), session_id=SJ, task_id=TJT, on_cancel="detach")["job"]
j3 = jobs.start("unrelated", command="sleep 122", cwd=str(PP), session_id="someone-else")["job"]
hook("UserPromptSubmit", rpay(SJ, PP, TJ, prompt="/check:pause", prompt_id="j2"), EJ)
out = hook("UserPromptSubmit", rpay(SJ, PP, TJ, prompt="/check:cancel", prompt_id="j3"), EJ)
cres = ctx(out)
reg = jobs.refresh_all()
check("/check:cancel: each job's own policy: train-a (interrupt, the default) stopped, train-b (detach) left running "
      "and released; an unrelated job untouched",
      lambda: reg[j1["id"]]["state"] == "killed" and reg[j2["id"]]["state"] == "running"
      and reg[j2["id"]].get("detached") and reg[j3["id"]]["state"] == "running"
      and "train-a" in cres and "stopped" in cres and "train-b" in cres and "detached, still running" in cres)
crec = next((r for r in jrecs(TJT) if r["kind"] == "task.cancel"), None)
check("...recorded (task.cancel with each job's outcome, synced); history kept; the model told not to go on",
      lambda: crec and {j["job"]: j["outcome"] for j in crec["data"]["jobs"]} == {j1["id"]: "stopped", j2["id"]: "detached"}
      and "don't continue its work" in cres and journal.read(TJT).records)
check("...the session is no longer paused and goes on in a new task; the old one reads canceled",
      lambda: not state.is_paused(SJ) and task_of(SJ) != TJT
      and (state.read_json(journal.jdir(TJT) / "meta.json", {}).get("finished") or {}).get("how") == "canceled"
      and "Canceled at" in summary.build(TJT).text)
r = subprocess.run([sys.executable, str(ROOT / "run.py"), "tasks"], capture_output=True, text=True, timeout=60)
check("`tasks` lists it as canceled", lambda: any(TJT in ln and "canceled" in ln for ln in r.stdout.splitlines()))
hook("UserPromptSubmit", rpay(SJ, PP, TJ, prompt="New thing: train c", prompt_id="j4"), EJ)
capture.ingest(SJ)
TJ2 = task_of(SJ)
j4 = jobs.start("train-c", command="sleep 123", cwd=str(PP), session_id=SJ, task_id=TJ2)["job"]
msg = core.cancel(SJ, ["detach"])
check("/check:cancel detach: every job of the task left running and released, said so",
      lambda: jobs.refresh_all()[j4["id"]]["state"] == "running" and jobs.refresh_all()[j4["id"]].get("detached")
      and "detached, still running" in msg)
out = hook("UserPromptSubmit", rpay(SJ, PP, TJ, prompt="/check:stop", prompt_id="j5"), EJ)
check("/check:stop alone lists the running jobs", lambda: "train-b" in ctx(out) and "train-c" in ctx(out))
hook("UserPromptSubmit", rpay(SJ, PP, TJ, prompt="pause", prompt_id="j6"), EJ)
out = hook("UserPromptSubmit", rpay(SJ, PP, TJ, prompt="/check:stop train-b", prompt_id="j7"), EJ)
check("/check:stop <job> while paused: the job's process group is stopped, said exactly; the session stays paused",
      lambda: jobs.refresh_all()[j2["id"]]["state"] == "killed" and "stopped: its process group got SIGTERM" in ctx(out)
      and state.is_paused(SJ))
check("...and the stop is in the journal", lambda: any(r["kind"] == "job.stop" and r["data"]["job"] == j2["id"]
                                                       for r in jrecs(task_of(SJ))))
jobs.stop(j3["id"], "test cleanup")
jobs.stop(j4["id"], "test cleanup")
state.clear_paused(SJ)
hook("UserPromptSubmit", rpay(SJ, PP, TJ, prompt="Write the report", prompt_id="j8"), EJ)
capture.ingest(SJ)
TJ3 = task_of(SJ)
out = hook("UserPromptSubmit", rpay(SJ, PP, TJ, prompt="/check:complete report sent", prompt_id="j9"), EJ)
check("/check:complete: task.complete recorded (synced), the session goes on in a new task, the old reads complete",
      lambda: "marked complete" in ctx(out) and task_of(SJ) != TJ3
      and any(r["kind"] == "task.complete" and r["data"]["note"] == "report sent" for r in jrecs(TJ3))
      and "Marked complete at" in summary.build(TJ3).text)
CJ.kill()
CJ.wait()
CN = fake_claude()
out = hook("SessionStart", rpay("sess-after-done", PP, TJ, source="startup"), {"CLAUDE_PID": str(CN.pid), **HENV})
check("a later session start doesn't offer completed or canceled tasks as unfinished",
      lambda: TJ3 not in ctx(out) and TJT not in ctx(out))

# --- job_start records its task and policy ----------------------------------------------------
resp = mcp([{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "job_start", "arguments": {
    "name": "viamcp", "command": "true", "cwd": str(PP), "redo_hint": "", "on_cancel": "detach"}}}],
    dict(os.environ, CLAUDE_PID=str(CN.pid), CLAUDE_CODE_SESSION_ID="sess-after-done"))
jm = jobs.find("viamcp") or {}
check("job_start (MCP) records the journal task it belongs to and its on_cancel policy",
      lambda: jm.get("task_id") == task_of("sess-after-done") and jm.get("on_cancel") == "detach")

# --- the shell fallback never resumes for a management command --------------------------------
print("without Python, the management commands never resume a paused session:")
SFB = "sess-fallback-gggg"
state.set_paused(SFB, by="test")
for cmd in ("/check:list", "/check:diff", "/check:stop x", "/check:cancel", "/check:complete", "/check:export",
            "/check:save", "pause interrupt"):
    out = hook("UserPromptSubmit", {"session_id": SFB, "cwd": str(PP), "prompt": cmd},
               env={"CHECKPOINT_PYTHON": str(FAILPY)})
    check(f"fallback: {cmd} keeps the session paused", lambda out=out: state.is_paused(SFB) and ctx(out))
state.clear_paused(SFB)

# --- export ----------------------------------------------------------------------------------
print("export: a sanitized bundle (journal excerpt, summaries, references), never credentials:")
SX = "sess-export-hhhh"
CX = fake_claude()
EX = {"CLAUDE_PID": str(CX.pid), **HENV}
TX = RT / "export.jsonl"
TX.write_text("")
hook("SessionStart", rpay(SX, PP, TX, source="startup"), EX)
for i, t_ in enumerate(CORPUS):
    hook("UserPromptSubmit", rpay(SX, PP, TX, prompt=f"step {i}: {t_}", prompt_id=f"x{i}"), EX)
hook("PreToolUse", rpay(SX, PP, TX, tool_name="Bash", tool_use_id="xb1", tool_input={"command": CORPUS[1]}), EX)
capture.ingest(SX)
TXT = task_of(SX)
# Records an older or buggy writer might have left unsanitized: written raw, past the sanitizer.
journal.append(TXT, [{"kind": "note", "session": SX, "data": {"kind": "note", "text": t_}} for t_ in CORPUS]
               + [{"kind": "tool.intent", "op": f"raw{i}", "session": SX,
                   "data": {"tool": "Bash", "input": {"command": t_, "api_key": S["openai"]}}}
                  for i, t_ in enumerate(CORPUS[:8])])
core.save(SX, "for export")
out = hook("UserPromptSubmit", rpay(SX, PP, TX, prompt="/check:export", prompt_id="xe"), EX)
path = ctx(out).split("Exported task ", 1)[1].split(" to ", 1)[1].split(" (0600)", 1)[0] if "Exported task" in ctx(out) else ""
bundle = jread(path) if path else {}
check("/check:export writes a bundle (0600) in the state folder: format, summary, checkpoints, journal excerpt, references",
      lambda: path and (Path(path).stat().st_mode & 0o777) == 0o600 and bundle.get("format") == recovery.EXPORT_FORMAT
      and bundle.get("summary", "").startswith("Recovery summary") and bundle["checkpoints"]
      and bundle["journal"]["records"] and bundle["references"]["journal"] and bundle.get("sha256"))
leaked = scan_for(list(SECRETS.values()), Path(path).parent) if path else ["no export"]
check(f"no synthetic secret of the corpus ({len(CORPUS)} contexts) in the export, also from raw records written past "
      "the sanitizer (every string scrubbed again; sensitive keys dropped)", lambda: not leaked)
if leaked:
    print(f"       (files holding a canary: {leaked})")
check("...and no transcript or file contents: references only (paths, ids)",
      lambda: "transcripts" in bundle["references"] and "content" not in json.dumps(bundle["references"]))
ok3, p3 = recovery.export(TXT, str(RT))
check("export to a folder the person names", lambda: ok3 and Path(p3).parent == RT and Path(p3).exists())

# --- retention --------------------------------------------------------------------------------
print("retention (CHECKPOINT_KEEP_DAYS, CHECKPOINT_KEEP_OPEN_DAYS):")


def old_task(name, days, finished=False, sessions=()):
    t = f"t_ret_{name}"
    journal.append(t, [{"kind": "note", "session": "x", "data": {"text": name}}])
    meta = {"task": t, "updated": time.time() - days * 86400, "sessions": list(sessions), "goal": name}
    if finished:
        meta["finished"] = {"how": "completed", "ts": meta["updated"]}
    state.write_json(journal.jdir(t) / "meta.json", meta)
    return t


t_done_old = old_task("done-old", 40, finished=True)
t_done_new = old_task("done-new", 5, finished=True)
t_open_mid = old_task("open-mid", 40)
t_open_old = old_task("open-old", 200)
t_live = old_task("live", 400, sessions=[SR2])   # SR2's fake Claude still runs
t_paused = old_task("paused", 400, sessions=["sess-ret-paused"])
state.set_paused("sess-ret-paused", by="test")
dry = recovery.prune_tasks(dry_run=True)
check("dry run: names what is past retention, removes nothing",
      lambda: set(dry) >= {t_done_old, t_open_old} and journal.jdir(t_done_old).exists())
gone = recovery.prune_tasks()
check("finished tasks go after 30 days, open ones after 180; never one with a live or paused session",
      lambda: not journal.jdir(t_done_old).exists() and not journal.jdir(t_open_old).exists()
      and all(journal.jdir(t).exists() for t in (t_done_new, t_open_mid, t_live, t_paused)))
os.environ["CHECKPOINT_KEEP_DAYS"] = "0"
os.environ["CHECKPOINT_KEEP_OPEN_DAYS"] = "never"
t_forever = old_task("forever", 999, finished=True)
check("CHECKPOINT_KEEP_DAYS=0 / CHECKPOINT_KEEP_OPEN_DAYS=never keep journals forever",
      lambda: recovery.prune_tasks() == [] and journal.jdir(t_forever).exists())
os.environ.pop("CHECKPOINT_KEEP_DAYS")
os.environ.pop("CHECKPOINT_KEEP_OPEN_DAYS")
os.environ["CHECKPOINT_KEEP_OPEN_DAYS"] = "30"
check("...and a shorter window is honoured (CHECKPOINT_KEEP_OPEN_DAYS=30)",
      lambda: t_open_mid in recovery.prune_tasks() and not journal.jdir(t_open_mid).exists())
os.environ.pop("CHECKPOINT_KEEP_OPEN_DAYS")
state.clear_paused("sess-ret-paused")
_rt = procs.table
procs.table = lambda: {}
try:
    old_task("noproc", 999, finished=True)
    check("without a process table nothing is pruned (every session would look dead)",
          lambda: recovery.prune_tasks() == [] and journal.jdir("t_ret_noproc").exists())
finally:
    procs.table = _rt

# --- lineage: transitions and concurrent sessions ----------------------------------------------
print("lineage: a session opened twice, concurrent sessions on one task:")
CL1 = fake_claude()
SL = "sess-twice-iiii"
TL = RT / "twice.jsonl"
TL.write_text("")
hook("SessionStart", rpay(SL, PP, TL, source="startup"), {"CLAUDE_PID": str(CL1.pid), **HENV})
hook("UserPromptSubmit", rpay(SL, PP, TL, prompt="Long task", prompt_id="w1"), {"CLAUDE_PID": str(CL1.pid), **HENV})
CL2 = fake_claude()
out = hook("SessionStart", rpay(SL, PP, TL, source="resume"), {"CLAUDE_PID": str(CL2.pid), **HENV})
check("`claude --resume <id>` while that session still runs in another process: said (context and a message) and "
      "recorded (session.concurrent)",
      lambda: f"also open in another Claude process (pid {CL1.pid})" in ctx(out)
      and "also open in another Claude process" in (out or {}).get("systemMessage", "")
      and any(r["kind"] == "session.concurrent" and r["data"].get("twin_pid") == CL1.pid for r in jrecs(task_of(SL))))
check("...and the summary's lineage shows it", lambda: "opened again while another Claude process" in summary.build(task_of(SL)).text)
SLT = RT / "fork.jsonl"
SLT.write_text(json.dumps({"sessionId": SL, "type": "user"}) + "\n")
CL3 = fake_claude()
out = hook("SessionStart", rpay("sess-fork-jjjj", PP, SLT, source="fork"), {"CLAUDE_PID": str(CL3.pid), **HENV})
check("a fork of a live session: both now record into one task, said and recorded",
      lambda: task_of("sess-fork-jjjj") == task_of(SL) and "Other live sessions record into this task" in ctx(out)
      and any(r["kind"] == "session.concurrent" and r["session"] == "sess-fork-jjjj" for r in jrecs(task_of(SL))))
CL4 = fake_claude()
out = hook("SessionStart", rpay("sess-fresh-kkkk", PP, RT / "fresh.jsonl", source="startup"),
           {"CLAUDE_PID": str(CL4.pid), **HENV})
check("a fresh start offers recent tasks with their original session's `claude --resume <id>`",
      lambda: "claude --resume" in ctx(out) and "prefer its original session" in ctx(out))
for c in (CL1, CL2, CL3, CL4, CN, CX, CP5, CR2, CR3, CR4, CB):
    c.kill()
CL5 = fake_claude()
E5 = {"CLAUDE_PID": str(CL5.pid), **HENV}
SCa, SCb = "sess-clear-llll", "sess-clear-mmmm"
TCa = RT / "clear.jsonl"
TCa.write_text("")
hook("SessionStart", rpay(SCa, PP, TCa, source="startup"), E5)
hook("UserPromptSubmit", rpay(SCa, PP, TCa, prompt="Before the clear", prompt_id="c1"), E5)
capture.ingest(SCa)
hook("SessionEnd", rpay(SCa, PP, TCa, reason="clear"), E5)
hook("SessionStart", rpay(SCb, PP, TCa, source="clear"), E5)
out = hook("UserPromptSubmit", rpay(SCb, PP, TCa, prompt=f"/check:resume {task_of(SCa)}", prompt_id="c2"), E5)
lin = recovery.reconcile(task_of(SCa), SCb).lineage
check("after /clear, picking the old task up again: its original session ran in this same process (not 'alive "
      "elsewhere'); `claude --resume <id>` reopens it",
      lambda: lin.get("same_process") and not lin.get("original_alive") and "ran in this same Claude process" in ctx(out)
      and f"claude --resume {SCa}" in ctx(out))
CL5.kill()

# --- verification fixes of 0.5.0 ----------------------------------------------------------------
print("stop never puts a job's log into the model's context (the gateway masks job_tail, not a hook's context):")
VTOK = SECRETS["github classic"]   # token-shaped: the sanitizer would catch it
VOPQ = _rand(28)                   # opaque: only leaving the log out keeps it out
gw_write(f"DEPLOY_TOKEN='{VTOK}'; export DEPLOY_TOKEN\nSVC_KEY='{VOPQ}'; export SVC_KEY\n")
VK = fake_claude()
EVK = {"CLAUDE_PID": str(VK.pid), **HENV}
SVK = "sess-stoplog-nnnn"
TVK = RT / "stoplog.jsonl"
TVK.write_text("")
hook("SessionStart", rpay(SVK, PP, TVK, source="startup"), EVK)
hook("UserPromptSubmit", rpay(SVK, PP, TVK, prompt="Deploy the workers", prompt_id="v1"), EVK)
capture.ingest(SVK)
VLOGCMD = 'printf "using %s\\n" "$DEPLOY_TOKEN"; printf "key %s\\n" "$SVC_KEY"; sleep 120'
vjobs = {n: jobs.start(n, command=VLOGCMD, cwd=str(PP), session_id=SVK, task_id=task_of(SVK))["job"]
         for n in ("vdeploy-a", "vdeploy-b", "vdeploy-c")}
check("(setup) the jobs' logs hold both values, from the gateway's env file",
      lambda: wait_for(lambda: all(VTOK in read_or_empty(Path(j["log"])) and VOPQ in read_or_empty(Path(j["log"]))
                                   for j in vjobs.values()), 15))
vout = ctx(hook("UserPromptSubmit", rpay(SVK, PP, TVK, prompt="/check:stop vdeploy-a", prompt_id="v2"), EVK))
check("/check:stop <job>: stopped, said so, pointing at job_tail; neither the token-shaped nor the opaque value of "
      "its log is in the hook's context (nothing masks that context)",
      lambda: "vdeploy-a" in vout and "stopped: its process group got SIGTERM" in vout and "job_tail vdeploy-a" in vout
      and VTOK not in vout and VOPQ not in vout and "Last lines" not in vout)
vrun = subprocess.run([sys.executable, str(ROOT / "run.py"), "stop-job", SVK, "vdeploy-b"], capture_output=True,
                      text=True, env=dict(os.environ, **EVK), timeout=60)
check("/check-stop (the mod shows what `run stop-job` prints): the same, no value from the log",
      lambda: vrun.returncode == 0 and "stopped" in vrun.stdout and VTOK not in vrun.stdout
      and VOPQ not in vrun.stdout)
hook("UserPromptSubmit", rpay(SVK, PP, TVK, prompt="pause", prompt_id="v3"), EVK)
hook("PreToolUse", rpay(SVK, PP, TVK, tool_name="Bash", tool_use_id="vb1", tool_input={"command": "ls"}), EVK)
jobs.stop(vjobs["vdeploy-c"]["id"], "test: it ends while the session is paused")
vres = ctx(hook("UserPromptSubmit", rpay(SVK, PP, TVK, prompt="go on", prompt_id="v4"), EVK))
check("...nor the resume context: a job that ended while paused is named, its output left to job_tail",
      lambda: "<checkpoint-resume>" in vres and "Job vdeploy-c: killed" in vres and "job_tail vdeploy-c" in vres
      and VTOK not in vres and VOPQ not in vres)
vleak = [k for k, v in list(SECRETS.items()) + [("opaque", VOPQ)] if any(v in t for t in (vout, vrun.stdout, vres))]
check("the corpus scan over the three texts (/check:stop, /check-stop, the resume) finds nothing", lambda: not vleak)
if vleak:
    print(f"       (leaked: {vleak})")
check("...and nothing of the log is in the task's journal (job.stop records the outcome only)",
      lambda: not scan_for([VTOK, VOPQ], journal.jdir(task_of(SVK))))
gw_rm(GW_ENV)
VK.kill()

print("the resume context never drops what to check first (many unknowns, long paths); the summary is cut first:")
VLP = RT / ("long-" + "d" * 70) / ("deeper-" + "e" * 70)
VLP.mkdir(parents=True)
VT = "t_many_unknowns"
vrecs = [{"kind": "prompt", "op": "vp1", "session": "sv1", "data": {"text": "Deploy the release to the fleet", "user": True}},
         {"kind": "tool.intent", "op": "vpush", "session": "sv1",
          "data": {"tool": "Bash", "input": {"command": "git push origin release/2026-10-20"}}}]
for i in range(14):
    cmd = f"ssh deploy@web{i:02d}.internal.example 'sudo systemctl restart app-worker@{i} && journalctl -u app -n 50'"
    vrecs += [{"kind": "tool.intent", "op": f"vbg{i}", "session": "sv1",
               "data": {"tool": "Bash", "input": {"command": cmd, "background": True}}},
              {"kind": "tool.outcome", "op": f"vbg{i}", "session": "sv1",
               "data": {"tool": "Bash", "background": "background", "result": {"background_id": f"b{i}"}}}]
for i in range(30):
    vf = VLP / f"module_{i:02d}_with_a_long_name.py"
    vf.write_text(f"x = {i}\n")
    vrecs.append({"kind": "tool.intent", "op": f"ved{i}", "session": "sv1",
                  "data": {"tool": "Edit", "path": str(vf), "before": capture.file_hash(str(vf)),
                           "input": {"file_path": str(vf)}}})
journal.append(VT, vrecs)
vr = recovery.reconcile(VT, "", cwd="")
vtxt = vr.text(lambda n: summary.build(VT, limit=n).text, limit=7000, summary_max=3000)
vcf = section(vtxt, "Check first")
check("15 unknown outcomes with an external effect and 30 unknown edits under a long path: 'Check first' and "
      "'Interrupted operations' are both there", lambda: "## Check first" in vtxt and "## Interrupted operations" in vtxt)
check("...the lost push is item 1, with what to check and 'do not run it again until you know'",
      lambda: vcf.strip().startswith("1. Bash `git push origin release/2026-10-20`") and "git ls-remote" in vcf
      and "do not run it again until you know" in vcf)
check("...every one with an external effect is named under 'Check first' (never cut), each saying not to run it again",
      lambda: all(f"web{i:02d}.internal" in vcf for i in range(14)) and vcf.count("do not run it again") >= 15)
check("...the rest is cut item by item, and says how many and where all of them are (the installed CLI)",
      lambda: re.search(r"- \.\.\.and [0-9]+ more \(all of them: `[^`]*checkpoint reconcile t_many_unknowns`\)", vcf)
      and "pixi run" not in vtxt)
_vmust = sum(len(t) + 8 for _r, t in vr.check_items() + vr.op_items() if _r == 0)
check("...the summary comes after the reconciliation, cut to what is left (never the reconciliation); only the "
      "items never cut take it past the limit",
      lambda: "Recovery summary for task t_many_unknowns" in vtxt
      and vtxt.index("Recovery summary") > vtxt.index("## Interrupted operations")
      and "cut: In flight" in vtxt and len(vtxt) <= 7000 + _vmust)
# No check may depend on the temp path's length: a long state path makes the
# CLI pointer long, and the summary's note (what was left out or cut, where
# the full summary is) must survive at every limit.
_cli0 = state.cli
try:
    state.cli = lambda *a: "/" + "/".join(f"a-very-long-folder-name-{i}" for i in range(8)) + "/checkpoint " + " ".join(a)
    _vlong = {n: summary.build(VT, limit=n).text for n in (1200, 1500, 2000, 3000)}
finally:
    state.cli = _cli0
check("the summary under a long state path (a ~230-character CLI pointer): at every limit it fits, and its note "
      "says what was cut and where the full summary is (the body is cut, never the note)",
      lambda: all(len(t) <= n and "cut: In flight" in t and "the full summary: `/a-very-long" in t
                  for n, t in _vlong.items()))
check("...and under 'Interrupted operations' too, each with its possible external effect",
      lambda: all(f"web{i:02d}.internal" in section(vtxt, "Interrupted operations") for i in range(14))
      and "git push origin release/2026-10-20" in section(vtxt, "Interrupted operations"))
_v4 = vr.text("", limit=4000)
check("...at a smaller limit too: 'Check first' starts with the push, and every one with an external effect is "
      "still in both sections (only the rest is cut)",
      lambda: section(_v4, "Check first").strip().startswith("1. Bash `git push origin release")
      and all(f"web{i:02d}.internal" in section(_v4, "Check first")
              and f"web{i:02d}.internal" in section(_v4, "Interrupted operations") for i in range(14)))
check("the summary's In flight lists the ones with an external effect first (the push isn't the one cut)",
      lambda: "git push origin release/2026-10-20" in section(summary.build(VT).text, "In flight"))
# The last checkpoint is in the header, never left out: in Evidence (the last
# section) it was the first thing a short text lost, and a resume context
# under long paths ended "(left out ...: Evidence ...)" with no checkpoint id
# (acceptance_journal on macOS's long scratch paths).
journal.append(VT, [{"kind": "checkpoint", "op": "c_vtlong01", "session": "s-vt",
                     "data": {"id": "c_vtlong01", "seq_from": 1, "seq_to": 40, "reason": "before the kill"}}], sync=True)
try:
    state.cli = lambda *a: "/" + "/".join(f"a-very-long-folder-name-{i}" for i in range(8)) + "/checkpoint " + " ".join(a)
    _vcp = {n: summary.build(VT, limit=n).text for n in (1200, 2000, 4000)}
    _vrc = recovery.reconcile(VT, "", cwd="").text(lambda n: summary.build(VT, limit=n).text, limit=8500,
                                                    summary_max=4000)
finally:
    state.cli = _cli0
check("the last checkpoint is in the summary's header: kept at every limit, also when Evidence is left out under a "
      "long state path",
      lambda: all("Last checkpoint: c_vtlong01" in t.split("\n## ")[0] for t in _vcp.values())
      and "Evidence" in _vcp[1200].split("(left out to stay under")[-1])
check("...and in a resume context whose summary is cut to fit (the checkpoint a resume names)",
      lambda: "Last checkpoint: c_vtlong01" in _vrc and "left out to stay under" in _vrc)
_lp = "/private/tmp/" + "x" * 150 + "/scratchpad/m.py"
check("a path is clipped in the middle: its file name stays (the summary's commands and the reconciliation's files)",
      lambda: sanitize.clip_path(_lp, 80).endswith("/m.py") and len(sanitize.clip_path(_lp, 80)) <= 80
      and summary._cmd({"input": {"file_path": _lp}}, 80).endswith("/m.py")
      and sanitize.clip_path("/a/b.py", 80) == "/a/b.py")
check("texts the model reads point at the installed terminal helper, never `pixi run` (in a user's pixi project "
      "that runs the project's own task)",
      lambda: state.cli("summary", VT) in summary.build(VT, limit=600).text
      and "pixi run" not in summary.build(VT, limit=600).text
      and not [p.name for p in (ROOT / "src" / "checkpoint").glob("*.py")
               if re.search(r"`pixi run (summary|reconcile)", p.read_text())])

print("reconciliation lists what is still open, not the task's whole history:")
VHP = RT / "history proj"
VHP.mkdir()
jgit(VHP, "init", "-q")
(VHP / "h.py").write_text("h = 1\n")
jgit(VHP, "add", "-A")
jgit(VHP, "commit", "-qm", "h")
VH = fake_claude()
EVH = {"CLAUDE_PID": str(VH.pid), **HENV}
SVH = "sess-history-uuuu"
TVH = RT / "history.jsonl"
TVH.write_text("")
hook("SessionStart", rpay(SVH, VHP, TVH, source="startup"), EVH)
hook("UserPromptSubmit", rpay(SVH, VHP, TVH, prompt="Port the build to the new CI", prompt_id="h1"), EVH)
for i in range(12):
    vi = {"command": f"npm run build -- --filter=pkg-{i:02d}"}
    hook("PreToolUse", rpay(SVH, VHP, TVH, tool_name="Bash", tool_use_id=f"hx{i}", tool_input=vi), EVH)
    hook("PostToolUseFailure", rpay(SVH, VHP, TVH, tool_name="Bash", tool_use_id=f"hx{i}", tool_input=vi,
                                    error="Interrupted by user", is_interrupt=True), EVH)
    hook("PreToolUse", rpay(SVH, VHP, TVH, tool_name="Bash", tool_use_id=f"hy{i}", tool_input=vi), EVH)
    hook("PostToolUse", rpay(SVH, VHP, TVH, tool_name="Bash", tool_use_id=f"hy{i}", tool_input=vi,
                             tool_response={"stdout": "built", "stderr": "", "interrupted": False}), EVH)
hook("PreToolUse", rpay(SVH, VHP, TVH, tool_name="Bash", tool_use_id="hdocs", tool_input={"command": "make docs"}), EVH)
hook("PostToolUseFailure", rpay(SVH, VHP, TVH, tool_name="Bash", tool_use_id="hdocs", tool_input={"command": "make docs"},
                                error="Interrupted by user", is_interrupt=True), EVH)
hook("PreToolUse", rpay(SVH, VHP, TVH, tool_name="Bash", tool_use_id="hls", tool_input={"command": "ls -la"}), EVH)
hook("PostToolUse", rpay(SVH, VHP, TVH, tool_name="Bash", tool_use_id="hls", tool_input={"command": "ls -la"},
                         tool_response={"stdout": "x", "stderr": "", "interrupted": False}), EVH)
edit(SVH, "hed", VHP / "h.py", "h = 2\n", EVH, tp=TVH, cwd=VHP, outcome=False)
hook("PreToolUse", rpay(SVH, VHP, TVH, tool_name="Bash", tool_use_id="hpush", tool_input={"command": "git push origin ci-port"}), EVH)
hook("UserPromptSubmit", rpay(SVH, VHP, TVH, prompt="pause", prompt_id="h2"), EVH)
vh1 = ctx(hook("UserPromptSubmit", rpay(SVH, VHP, TVH, prompt="go on", prompt_id="h3"), EVH))
check("12 builds interrupted (Esc) and each rerun fine, a `make docs` interrupted before other work went on: none "
      "is listed again as interrupted; the lost push is item 1 of 'Check first'",
      lambda: section(vh1, "Check first").strip().startswith("1. Bash `git push origin ci-port`")
      and "interrupted before it finished" not in section(vh1, "Check first")
      and "npm run build" not in section(vh1, "Interrupted operations")
      and "make docs" not in section(vh1, "Interrupted operations"))
check("...it says they were left to the recovery summary, which still has them",
      lambda: re.search(r"[0-9]+ older interrupted operation\(s\) .* are left to the recovery summary", vh1)
      and "make docs" in summary.build(task_of(SVH), limit=100000).text)
check("...the edit with no result is still listed (open since nothing reported it)",
      lambda: "h.py" in section(vh1, "Interrupted operations"))
hook("PreToolUse", rpay(SVH, VHP, TVH, tool_name="Bash", tool_use_id="hnext", tool_input={"command": "echo next"}), EVH)
hook("PostToolUse", rpay(SVH, VHP, TVH, tool_name="Bash", tool_use_id="hnext", tool_input={"command": "echo next"},
                         tool_response={"stdout": "next", "stderr": "", "interrupted": False}), EVH)
hook("UserPromptSubmit", rpay(SVH, VHP, TVH, prompt="pause", prompt_id="h4"), EVH)
vh2 = ctx(hook("UserPromptSubmit", rpay(SVH, VHP, TVH, prompt="go on again", prompt_id="h5"), EVH))
check("after a resume that reported them and further work: the unknown edit is left to the summary; the push "
      "(possible external effect) stays named in one line, still not to be run again unchecked",
      lambda: "h.py" not in section(vh2, "Interrupted operations")
      and re.search(r"An earlier resume \(seq [0-9]+\) already reported 1 operation\(s\) .*`git push origin ci-port`",
                    section(vh2, "Check first"))
      and "do not run one again until you know" in section(vh2, "Check first")
      and "git push origin ci-port" in section(summary.build(task_of(SVH)).text, "In flight"))
VH.kill()

print("/check:resume <a reference that matches nothing> leaves a paused session paused:")
VTP = RT / "typo proj"
VTP.mkdir()
VTY = fake_claude()
EVT = {"CLAUDE_PID": str(VTY.pid), **HENV}
SVT = "sess-typo-oooo"
TVT = RT / "typo.jsonl"
TVT.write_text("")
hook("SessionStart", rpay(SVT, VTP, TVT, source="startup"), EVT)
hook("UserPromptSubmit", rpay(SVT, VTP, TVT, prompt="Refactor m.py", prompt_id="y1"), EVT)
hook("PreToolUse", rpay(SVT, VTP, TVT, tool_name="Bash", tool_use_id="ypush", tool_input={"command": "git push origin main"}), EVT)
hook("UserPromptSubmit", rpay(SVT, VTP, TVT, prompt="/check:pause", prompt_id="y2"), EVT)
capture.ingest(SVT)
ybefore = len(jrecs(task_of(SVT)))
yout = ctx(hook("UserPromptSubmit", rpay(SVT, VTP, TVT, prompt="/check:resume tpyo", prompt_id="y3"), EVT))
capture.ingest(SVT)
check("a typo: still paused (nothing pending), and the result says so; no resume context",
      lambda: state.is_paused(SVT) and not state.resume_pending(SVT) and "STILL PAUSED" in yout
      and "No tracker, task or checkpoint matches 'tpyo'" in yout and "<checkpoint-resume>" not in yout)
check("...no resume or reconcile record was written",
      lambda: not [r for r in jrecs(task_of(SVT))[ybefore:] if r["kind"] in ("resume", "reconcile")])
check("...and the next tool call (the push again) is denied",
      lambda: denied(hook("PreToolUse", rpay(SVT, VTP, TVT, tool_name="Bash", tool_use_id="ypush2",
                                             tool_input={"command": "git push origin main"}), EVT)))
yok = ctx(hook("UserPromptSubmit", rpay(SVT, VTP, TVT, prompt=f"/check:resume {task_of(SVT)}", prompt_id="y4"), EVT))
capture.ingest(SVT)
check("/check:resume <its task>: resumes after reconciling, and the journal records the resume",
      lambda: not state.is_paused(SVT) and "<checkpoint-resume>" in yok and "git push origin main" in section(yok, "Check first")
      and any(r["kind"] == "resume" and "/check:resume" in r["data"].get("by", "") for r in jrecs(task_of(SVT))[ybefore:])
      and any(r["kind"] == "reconcile" for r in jrecs(task_of(SVT))[ybefore:]))
VTY.kill()

print("`claude --resume` after a clean exit reconciles too (said only when something changed):")
VCP = RT / "clean proj"
VCP.mkdir()
jgit(VCP, "init", "-q")
(VCP / "a.py").write_text("x = 1\n")
jgit(VCP, "add", "-A")
jgit(VCP, "commit", "-qm", "a")
VCA = fake_claude()
SVC = "sess-clean-pppp"
TVC = RT / "clean.jsonl"
TVC.write_text("")
hook("SessionStart", rpay(SVC, VCP, TVC, source="startup"), {"CLAUDE_PID": str(VCA.pid), **HENV})
hook("UserPromptSubmit", rpay(SVC, VCP, TVC, prompt="Make a.py return 2, keep tests green", prompt_id="c1"),
     {"CLAUDE_PID": str(VCA.pid), **HENV})
edit(SVC, "ce1", VCP / "a.py", "x = 2\n", {"CLAUDE_PID": str(VCA.pid), **HENV}, tp=TVC, cwd=VCP)
hook("PreToolUse", rpay(SVC, VCP, TVC, tool_name="Bash", tool_use_id="ct1", tool_input={"command": "pytest -q"}),
     {"CLAUDE_PID": str(VCA.pid), **HENV})
hook("PostToolUse", rpay(SVC, VCP, TVC, tool_name="Bash", tool_use_id="ct1", tool_input={"command": "pytest -q"},
                         tool_response={"stdout": "3 passed", "stderr": "", "interrupted": False}),
     {"CLAUDE_PID": str(VCA.pid), **HENV})
hook("Stop", rpay(SVC, VCP, TVC), {"CLAUDE_PID": str(VCA.pid), **HENV})
hook("SessionEnd", rpay(SVC, VCP, TVC, reason="prompt_input_exit"), {"CLAUDE_PID": str(VCA.pid), **HENV})
VCA.kill()
VCA.wait()
VCB = fake_claude()
vsilent = hook("SessionStart", rpay(SVC, VCP, TVC, source="resume"), {"CLAUDE_PID": str(VCB.pid), **HENV})
vrec1 = [r for r in jrecs(task_of(SVC)) if r["kind"] == "reconcile"]
check("nothing changed since the clean exit: no recovery text, but reconciled and recorded",
      lambda: "<checkpoint-recovery>" not in ctx(vsilent) and vrec1
      and "after a clean exit" in vrec1[-1]["data"]["why"])
hook("SessionEnd", rpay(SVC, VCP, TVC, reason="prompt_input_exit"), {"CLAUDE_PID": str(VCB.pid), **HENV})
VCB.kill()
VCB.wait()
(VCP / "a.py").write_text("x = 3  # the user's edit\n")
VCC = fake_claude()
vchg = ctx(hook("SessionStart", rpay(SVC, VCP, TVC, source="resume"), {"CLAUDE_PID": str(VCC.pid), **HENV}))
check("two exits and three starts of one session, quickly: every one is in the journal (their op ids carry the "
      "spool file, not only the second)",
      lambda: [r["kind"] for r in jrecs(task_of(SVC)) if r["kind"] in ("session.start", "session.end")]
      == ["session.start", "session.end", "session.start", "session.end", "session.start"])
_ve1 = capture.Event("SessionEnd", {"session_id": "s", "reason": "x"}, 1000.2, name="11.1.ev")
_ve2 = capture.Event("SessionEnd", {"session_id": "s", "reason": "x"}, 1000.7, name="12.1.ev")
check("...two session ends in one second are two records; the same spool file ingested twice is still one",
      lambda: capture.records_for(_ve1, "s")[0]["op"] != capture.records_for(_ve2, "s")[0]["op"]
      and capture.records_for(_ve1, "s")[0]["op"] == capture.records_for(_ve1, "s")[0]["op"])
check("the user edited a.py after the clean exit: the resumed session hears it (changed outside the task, kept) and "
      "that the test result is STALE",
      lambda: "after a clean exit" in vchg and "a.py" in section(vchg, "Check first")
      and "changed outside this task" in section(vchg, "Check first") and "STALE" in vchg
      and len([r for r in jrecs(task_of(SVC)) if r["kind"] == "reconcile"]) == len(vrec1) + 1)
VCC.kill()

print("a finished task picked up again is reopened:")
VRP = RT / "reopen proj"
VRP.mkdir()
VRA = fake_claude()
SVR = "sess-done-qqqq"
TVR = RT / "reopen.jsonl"
TVR.write_text("")
hook("SessionStart", rpay(SVR, VRP, TVR, source="startup"), {"CLAUDE_PID": str(VRA.pid), **HENV})
hook("UserPromptSubmit", rpay(SVR, VRP, TVR, prompt="Ship feature Y", prompt_id="q1"), {"CLAUDE_PID": str(VRA.pid), **HENV})
capture.ingest(SVR)
VRT = task_of(SVR)
hook("UserPromptSubmit", rpay(SVR, VRP, TVR, prompt="/check:cancel", prompt_id="q2"), {"CLAUDE_PID": str(VRA.pid), **HENV})
check("(setup) canceled", lambda: (state.read_json(journal.jdir(VRT) / "meta.json", {}).get("finished") or {}).get("how")
      == "canceled")
VRB = fake_claude()
SVR2 = "sess-pick-vvvv"
TVR2 = RT / "reopen2.jsonl"
TVR2.write_text("")
hook("SessionStart", rpay(SVR2, VRP, TVR2, source="startup"), {"CLAUDE_PID": str(VRB.pid), **HENV})
vro = ctx(hook("UserPromptSubmit", rpay(SVR2, VRP, TVR2, prompt=f"/check:resume {VRT}", prompt_id="r1"),
               {"CLAUDE_PID": str(VRB.pid), **HENV}))
hook("UserPromptSubmit", rpay(SVR2, VRP, TVR2, prompt="Finish Y after all", prompt_id="r2"), {"CLAUDE_PID": str(VRB.pid), **HENV})
capture.ingest(SVR2)
vmeta = state.read_json(journal.jdir(VRT) / "meta.json", {}) or {}
check("/check:resume <a canceled task>: a task.reopen record; the resume context says it was canceled and is reopened",
      lambda: task_of(SVR2) == VRT and any(r["kind"] == "task.reopen" and r["data"].get("was") == "canceled"
                                           for r in jrecs(VRT))
      and "was canceled" in vro and "reopens it" in vro)
check("...the listing cache and the summary treat it as open (no 'Canceled at'; 'Reopened at')",
      lambda: "finished" not in vmeta and "Canceled at" not in summary.build(VRT).text
      and "Reopened at" in summary.build(VRT).text)
VRA.kill()
VRA.wait()
VRB.kill()
VRB.wait()
check("...retention keeps it as an open task: not removed after 40 days (finished ones go after 30)",
      lambda: VRT not in recovery.prune_tasks(now=time.time() + 40 * 86400, dry_run=True)
      and VRT in recovery.prune_tasks(now=time.time() + 200 * 86400, dry_run=True))
VRC = fake_claude()
vfresh = ctx(hook("SessionStart", rpay("sess-fresh-wwww", VRP, RT / "fresh2.jsonl", source="startup"),
                  {"CLAUDE_PID": str(VRC.pid), **HENV}))
check("...and a fresh session start offers it again among the unfinished tasks", lambda: VRT in vfresh)
VRC.kill()

print("a session resumed elsewhere after /clear is not 'open twice':")
VWA = fake_claude()
EWA = {"CLAUDE_PID": str(VWA.pid), **HENV}
SW1, SW2 = "sess-clr-rrrr", "sess-clr-ssss"
TW1, TW2 = RT / "clr1.jsonl", RT / "clr2.jsonl"
TW1.write_text("")
TW2.write_text("")
hook("SessionStart", rpay(SW1, PP, TW1, source="startup"), EWA)
hook("UserPromptSubmit", rpay(SW1, PP, TW1, prompt="Build feature X", prompt_id="x1"), EWA)
hook("SessionEnd", rpay(SW1, PP, TW1, reason="clear"), EWA)
hook("SessionStart", rpay(SW2, PP, TW2, source="clear"), EWA)
VWB = fake_claude()
vtw = hook("SessionStart", rpay(SW1, PP, TW1, source="resume"), {"CLAUDE_PID": str(VWB.pid), **HENV})
check("/clear in process A, then `claude --resume <the cleared session>` in process B: no 'also open in another "
      "Claude process' (A holds the new session), nothing recorded as concurrent",
      lambda: "also open in another Claude process" not in ctx(vtw)
      and "also open in another Claude process" not in (vtw or {}).get("systemMessage", "")
      and not [r for r in jrecs(task_of(SW1)) if r["kind"] == "session.concurrent"])
# Each half on its own: a /clear whose SessionEnd never reached the journal
# (A holds a newer session), and a session that ended while its process is
# still alive with no newer one (shutting down).
SW3, SW4 = "sess-clr-xxxx", "sess-clr-yyyy"
TW3, TW4 = RT / "clr3.jsonl", RT / "clr4.jsonl"
TW3.write_text("")
TW4.write_text("")
VWC = fake_claude()
EWC = {"CLAUDE_PID": str(VWC.pid), **HENV}
hook("SessionStart", rpay(SW3, PP, TW3, source="startup"), EWC)
hook("UserPromptSubmit", rpay(SW3, PP, TW3, prompt="Build feature Z", prompt_id="z1"), EWC)
time.sleep(1.1)   # started_ts tells the newer session apart
hook("SessionStart", rpay(SW4, PP, TW4, source="clear"), EWC)   # no SessionEnd for SW3 (lost)
vtw2 = hook("SessionStart", rpay(SW3, PP, TW3, source="resume"), {"CLAUDE_PID": str(VWB.pid), **HENV})
check("...also when the cleared session's SessionEnd was lost: its old process holds a newer session, so not 'open "
      "twice'", lambda: "also open in another Claude process" not in ctx(vtw2)
      and not [r for r in jrecs(task_of(SW3)) if r["kind"] == "session.concurrent"])
SW5 = "sess-end-zzzz"
TW5 = RT / "end5.jsonl"
TW5.write_text("")
VWD = fake_claude()
EWD = {"CLAUDE_PID": str(VWD.pid), **HENV}
hook("SessionStart", rpay(SW5, PP, TW5, source="startup"), EWD)
hook("UserPromptSubmit", rpay(SW5, PP, TW5, prompt="Build feature W", prompt_id="e1"), EWD)
hook("SessionEnd", rpay(SW5, PP, TW5, reason="prompt_input_exit"), EWD)   # its process lingers a moment
vtw3 = hook("SessionStart", rpay(SW5, PP, TW5, source="resume"), {"CLAUDE_PID": str(VWB.pid), **HENV})
check("...and a session that ended while its process lingers (no newer session there): not 'open twice' either",
      lambda: "also open in another Claude process" not in ctx(vtw3)
      and not [r for r in jrecs(task_of(SW5)) if r["kind"] == "session.concurrent"])
for _c in (VWA, VWB, VWC, VWD):
    _c.kill()

print("/check:cancel stops the task's jobs all at once:")
VXA = fake_claude()
SVX = "sess-cancel-tttt"
TVX = RT / "cancel2.jsonl"
TVX.write_text("")
hook("SessionStart", rpay(SVX, PP, TVX, source="startup"), {"CLAUDE_PID": str(VXA.pid), **HENV})
hook("UserPromptSubmit", rpay(SVX, PP, TVX, prompt="Run the workers", prompt_id="w1"), {"CLAUDE_PID": str(VXA.pid), **HENV})
capture.ingest(SVX)
for _i in range(2):
    jobs.start(f"vstubborn-{_i}", command="trap '' TERM; sleep 300", cwd=str(PP), session_id=SVX, task_id=task_of(SVX))
time.sleep(0.5)
_t0 = time.time()
vcan = core.cancel(SVX, [])
_el = time.time() - _t0
check(f"two jobs that ignore SIGTERM: both stopped in about one grace period ({_el:.1f} s; one after the other "
      f"would take two, {2 * state.JOB_STOP_GRACE_SECONDS:g} s and more)",
      lambda: vcan.count("stopped (SIGTERM") == 2 and _el < state.JOB_STOP_GRACE_SECONDS * 1.6 + 1)
VXA.kill()
