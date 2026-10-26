# The user-facing fixes of the 0.7.3 review: the spool without a runtime
# (event contents never kept while no runtime is there or coming, each
# session's spool capped, dead sessions' spools pruned from the shell),
# /check:status showing this session's own summary and this project's
# unfinished tasks, the crash notice at a session start and `/check:resume
# last` after one, the help text, /check:forget and the terminal helper's
# forget and purge, CHECKPOINT_TRACKERS=off, and how a paused session resumes.
# Run by tests/run_tests.py last, in its namespace; it uses only that file's
# helpers (check, hook, ctx, reason, denied, fake_claude, plugin_copy,
# sys_msg, wait_for, read_or_empty, MINPATH, TMP, ROOT, SH) and the
# modules it imported, so a scratch harness can run it alone. Canaries are
# made here and never printed.
# ruff: noqa: F821  (names come from run_tests.py)
import contextlib as _ux_contextlib
import secrets as _ux_secrets
import string as _ux_string

from checkpoint import capture, journal, purge, sanitize, summary  # noqa: E402

UX = TMP / "ux"
UX.mkdir()


def ux_canary(n=24):
    return "".join(_ux_secrets.choice(_ux_string.ascii_letters + _ux_string.digits) for _ in range(n))


def ux_pay(sid, cwd, **kw):
    d = {"session_id": sid, "transcript_path": str(UX / f"{sid}.jsonl"), "cwd": str(cwd)}
    d.update(kw)
    return d


def ux_run(script, args, env, payload=None, timeout=60):
    """The copied plugin's hook scripts under the suite's shell (CHECKPOINT_TEST_SH: dash too)."""
    return subprocess.run([SH, str(script), *args], input=json.dumps(payload) if payload is not None else "",
                          env=env, capture_output=True, text=True, timeout=timeout)


def ux_recs(task):
    return journal.read(task).records if task else []


def ux_hits(values, root):
    """Files under root holding any of the values (raw or JSON-escaped); names only."""
    forms = [v for val in values for v in (val, json.dumps(val)[1:-1])]
    out = []
    for dp, _dn, fns in os.walk(root):
        for fn in fns:
            try:
                data = (Path(dp) / fn).read_bytes().decode("utf-8", "replace")
            except OSError:
                continue
            if any(f in data for f in forms):
                out.append(fn)
    return out


@_ux_contextlib.contextmanager
def ux_env(**kw):
    """os.environ changed for in-process calls, then put back."""
    old = {k: os.environ.get(k) for k in kw}
    os.environ.update({k: str(v) for k, v in kw.items()})
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --- the spool without a runtime (CK-DATA-1, CK-UX-7) -------------------------------------
print("the spool while no runtime is ready: names and sizes only, capped, pruned:")
UXP = plugin_copy(UX / "plugin", nopixi=True)
LOCK_SHA12 = __import__("hashlib").sha256((ROOT / "pixi.lock").read_bytes()).hexdigest()[:12]


def ux_rt(tag, state_word):
    """An environment for the copied hooks with no CHECKPOINT_PYTHON and its
    runtime in the given state: failed (a log, no build running), nopixi
    (the build's own no-pixi line), building (a lock folder: fresh), hung
    (a lock folder two hours old), missing (never tried)."""
    data = UX / f"data-{tag}"
    envd = data / "envs" / LOCK_SHA12
    envd.mkdir(parents=True)
    if state_word == "failed":
        (envd / "install.log").write_text("error: the lock file is broken\n")
    elif state_word == "nopixi":
        (envd / "install.log").write_text("checkpoint: pixi not found on PATH or in the usual install folders\n")
    elif state_word in ("building", "hung"):
        (envd / "install.log").write_text("building\n")
        lock = Path(f"{envd}.building")
        lock.mkdir()
        if state_word == "hung":
            old = time.time() - 2 * 3600
            os.utime(lock, (old, old))
    home = UX / "emptyhome"
    if state_word == "failed":
        # A pixi that fails: the session start's retry fails again (ck_state: failed, not nopixi).
        home = UX / f"home-{tag}"
        (home / ".pixi" / "bin").mkdir(parents=True)
        (home / ".pixi" / "bin" / "pixi").write_text("#!/bin/sh\necho 'error: the lock file is broken' >&2\nexit 1\n")
        (home / ".pixi" / "bin" / "pixi").chmod(0o755)
    env = {"HOME": str(home), "PATH": MINPATH, "LC_ALL": "C", "CHECKPOINT_HOME": str(UX / f"st-{tag}"),
           "CLAUDE_PLUGIN_DATA": str(data), "CHECKPOINT_GATEWAY_CONTRACT": str(UX / "no-gateway"),
           "CHECKPOINT_GATEWAY_ENV": str(UX / "no-gateway.env")}
    return env, Path(env["CHECKPOINT_HOME"])


def ux_spool_files(home, sid):
    d = home / "spool" / sid
    return sorted(d.glob("*.ev")) if d.is_dir() else []


def ux_index(home, sid):
    p = home / "spool" / sid / "index"
    return p.read_text().splitlines() if p.exists() else []


UX_CANARIES = [ux_canary(), "sk_live_" + ux_canary(24), ux_canary(18)]
UXC_FILE, UXC_TOKEN, UXC_PROMPT = UX_CANARIES


def ux_events(env, sid, cwd):
    """A prompt, a Read of a secret file, a Bash call with a token: through the copied fast.sh."""
    ux_run(UXP / "hooks" / "fast.sh", ["UserPromptSubmit"], env, ux_pay(sid, cwd, prompt=f"use {UXC_PROMPT} please",
                                                                      prompt_id="p1"))
    ux_run(UXP / "hooks" / "fast.sh", ["PreToolUse"], env, ux_pay(
        sid, cwd, hook_event_name="PreToolUse", tool_name="Read", tool_use_id="r1",
        tool_input={"file_path": str(cwd / ".env")}))
    ux_run(UXP / "hooks" / "fast.sh", ["PostToolUse"], env, ux_pay(
        sid, cwd, hook_event_name="PostToolUse", tool_name="Read", tool_use_id="r1",
        tool_input={"file_path": str(cwd / ".env")},
        tool_response={"type": "text", "file": {"content": f"DB_PASS={UXC_FILE}\nAPI={UXC_TOKEN}\n"}}))
    ux_run(UXP / "hooks" / "fast.sh", ["PreToolUse"], env, ux_pay(
        sid, cwd, hook_event_name="PreToolUse", tool_name="Bash", tool_use_id="b1",
        tool_input={"command": f"curl -H 'Authorization: Bearer {UXC_TOKEN}' https://example.invalid",
                    "run_in_background": True}))
    ux_run(UXP / "hooks" / "fast.sh", ["PostToolUse"], env, ux_pay(
        sid, cwd, hook_event_name="PostToolUse", tool_name="Bash", tool_use_id="b1",
        tool_input={"command": f"curl -H 'Authorization: Bearer {UXC_TOKEN}' https://example.invalid"},
        tool_response={"stdout": UXC_FILE, "stderr": "", "interrupted": False, "backgroundTaskId": "bg1"}))
    # Keys a record keeps, nested in a tool's input (an MCP server's), and a failure's text: never taken.
    ux_run(UXP / "hooks" / "fast.sh", ["PreToolUse"], env, ux_pay(
        sid, cwd, hook_event_name="PreToolUse", tool_name="mcp__notes__post",
        tool_input={"source": UXC_FILE, "cwd": UXC_TOKEN, "tool_use_id": UXC_PROMPT}, tool_use_id="m1"))
    ux_run(UXP / "hooks" / "fast.sh", ["PostToolUseFailure"], env, ux_pay(
        sid, cwd, hook_event_name="PostToolUseFailure", tool_name="mcp__notes__post", tool_use_id="m1",
        tool_input={"source": UXC_FILE}, error=f"refused: {UXC_TOKEN}"))


UXD = UX / "proj dé"     # a space and a non-ASCII letter
UXD.mkdir()
ENVF, HOMEF = ux_rt("failed", "failed")
SF = "sess-ux-failed-1"
r = ux_run(UXP / "hooks" / "fast.sh", ["SessionStart"], ENVF, ux_pay(SF, UXD, source="startup"))
# Its retry (started in the background) is over before the calls: while a build runs, payloads are kept whole.
wait_for(lambda: not Path(f"{ENVF['CLAUDE_PLUGIN_DATA']}/envs/{LOCK_SHA12}.building").exists(), 20)
check("a failed setup: SessionStart says so, and that until then only which tools ran is recorded, not their contents",
      lambda: "setting up its runtime failed" in sys_msg(r) and "not their contents" in sys_msg(r))
ux_events(ENVF, SF, UXD)
_files = ux_spool_files(HOMEF, SF)
_idx = ux_index(HOMEF, SF)
check("...no event's contents in the spool: not the prompt, the file read, the command or its output "
      "(raw or JSON-escaped)", lambda: _files and not ux_hits(UX_CANARIES, HOMEF / "spool"))
_meta = [json.loads(p.read_text()) for p in _files]
_by_tool = {(m.get("hook_event_name"), m.get("tool_name")): m for m in _meta}
check("...each record keeps the event, the tool, its id and the payload's size (meta=1 on its index line), and says "
      "why", lambda: all(ln.endswith("\tmeta=1") for ln in _idx if "\tSessionStart\t" not in ln)
      and _by_tool[("PostToolUse", "Read")]["tool_use_id"] == "r1"
      and _by_tool[("PostToolUse", "Read")]["checkpoint_meta"]["bytes"] > 100
      and _by_tool[("PreToolUse", "Bash")]["tool_use_id"] == "b1"
      and _by_tool[("PreToolUse", "mcp__notes__post")]["tool_use_id"] == "m1"
      and _by_tool[("PreToolUse", "mcp__notes__post")]["cwd"] == str(UXD)
      and all(m["checkpoint_meta"]["why"] == "no runtime: its setup failed" for m in _meta if "checkpoint_meta" in m)
      and not any("tool_input" in m or "tool_response" in m or "prompt" in m for m in _meta))
# Python comes later (pixi installed): what it makes of them.
r = subprocess.run([sys.executable, str(ROOT / "run.py"), "ingest", SF], capture_output=True, text=True, timeout=60,
                   env=dict(os.environ, CHECKPOINT_HOME=str(HOMEF), CHECKPOINT_GATEWAY_CONTRACT=str(UX / "no-gateway")))
with ux_env(CHECKPOINT_HOME=HOMEF):
    TF = capture.task_of(SF)
    _rf = ux_recs(TF)
    _sumf = summary.build(TF).text if TF else ""
_kinds = [x["kind"] for x in _rf]
_bout = next((x["data"] for x in _rf if x["kind"] == "tool.outcome" and x["op"] == "b1"), {})
check("...ingested later: the calls are in the journal by name (payload_dropped), with one recording.gap of kind "
      "'payloads' that the summary's header names",
      lambda: r.returncode == 0 and "tool.intent" in _kinds and "tool.outcome" in _kinds
      and any(x["kind"] == "tool.intent" and x["op"] == "r1" and x["data"].get("payload_dropped") for x in _rf)
      and [x["data"].get("what") for x in _rf if x["kind"] == "recording.gap"] == ["payloads"]
      and "were kept without their contents (no runtime: its setup failed)" in _sumf)
check("...a Bash call whose result wasn't kept: the journal never says it worked (no ok, no exit code: invariant 15); "
      "the summary says it returned and that what it did isn't known (round 3: no unknown outcome either)",
      lambda: _bout.get("background") == "unknown" and "ok" not in _bout and "exit" not in _bout
      and "- 1 command returned whose command and result weren't recorded (no runtime then), seq " in _sumf
      and "(Bash, op b1): what it did isn't known" in _sumf)
check("...and no canary anywhere in that state folder (journal, spool, markers)",
      lambda: not ux_hits(UX_CANARIES, HOMEF))
check("...the shell's size count goes back to what is left after the ingest",
      lambda: (HOMEF / "spool" / SF / ".size").read_text().split() == ["0", "0"])

ENVN, HOMEN = ux_rt("nopixi", "nopixi")
r = ux_run(UXP / "hooks" / "fast.sh", ["SessionStart"], ENVN, ux_pay("sess-ux-np", UXD, source="startup"))
wait_for(lambda: not Path(f"{ENVN['CLAUDE_PLUGIN_DATA']}/envs/{LOCK_SHA12}.building").exists(), 20)
ux_events(ENVN, "sess-ux-np", UXD)
check("no pixi: SessionStart says only which tools ran is recorded; the records say why (pixi not found), no contents",
      lambda: "not their contents" in sys_msg(r) and not ux_hits(UX_CANARIES, HOMEN / "spool")
      and any("no runtime: pixi not found" in p.read_text() for p in ux_spool_files(HOMEN, "sess-ux-np")))
ENVH, HOMEH = ux_rt("hung", "hung")
ux_events(ENVH, "sess-ux-hung", UXD)
check("a setup running for over an hour: contents not kept either",
      lambda: ux_spool_files(HOMEH, "sess-ux-hung") and not ux_hits(UX_CANARIES, HOMEH / "spool")
      and any("over an hour" in p.read_text() for p in ux_spool_files(HOMEH, "sess-ux-hung")))
ENVB, HOMEB = ux_rt("building", "building")
ux_events(ENVB, "sess-ux-bld", UXD)
check("a setup that just started: the payloads are kept whole (its runtime comes soon and scrubs them)",
      lambda: ux_hits([UXC_FILE], HOMEB / "spool")
      and not any(ln.endswith("\tmeta=1") for ln in ux_index(HOMEB, "sess-ux-bld")))
ENVM, HOMEM = ux_rt("missing", "missing")
ux_run(UXP / "hooks" / "fast.sh", ["UserPromptSubmit"], ENVM, ux_pay("sess-ux-miss", UXD, prompt=f"use {UXC_PROMPT}"))
check("no setup tried yet (a session start or the next call starts one): kept whole too",
      lambda: ux_hits([UXC_PROMPT], HOMEM / "spool"))
# Every one of the test homes above holds canaries only where the payloads are kept whole on purpose.
for _h in (HOMEB, HOMEM):
    shutil.rmtree(_h / "spool", ignore_errors=True)

# The cap: a session's spool never holds more than CK_SPOOL_KEEP_COUNT records or ~_BYTES bytes.
_lib = read_or_empty(ROOT / "hooks" / "lib.sh")
check("the shell's spool caps are capture's (CK_SPOOL_KEEP_COUNT/_BYTES/_DAYS = capture.SPOOL_KEEP_*)",
      lambda: f"CK_SPOOL_KEEP_COUNT={capture.SPOOL_KEEP_COUNT}\n" in _lib
      and f"CK_SPOOL_KEEP_BYTES={capture.SPOOL_KEEP_BYTES}\n" in _lib
      and f"CK_SPOOL_KEEP_DAYS={capture.SPOOL_KEEP_DAYS}\n" in _lib)
SCAP = "sess-ux-cap-1"
ECAP = {"CHECKPOINT_SPOOL_KEEP_COUNT": "8"}
for i in range(20):
    hook("Notification", ux_pay(SCAP, UXD, message=f"note {i}", notification_type="idle"), ECAP)
_cap_files = sorted(capture.spool_dir(SCAP).glob("*.ev"))
_gaps = [json.loads(p.read_text()) for p in _cap_files if '"recording.gap"' in p.read_text()]
check("a spool over its record cap (8 here): the oldest payloads are dropped down to half, never more than the cap "
      "left, each drop named by a recording.gap record (the dropped files listed)",
      lambda: _gaps and len([p for p in _cap_files if '"recording.gap"' not in p.read_text()]) <= 8
      and all(g["what"] == "dropped" and g["files"] and g["events"] == len(g["files"]) for g in _gaps)
      and not any((capture.spool_dir(SCAP) / f).exists() for g in _gaps for f in g["files"]))
_rc = capture.ingest(SCAP)
_capr = ux_recs(capture.task_of(SCAP))
_dropped = sum(x["data"].get("events") or 0 for x in _capr if x["kind"] == "recording.gap"
               and x["data"].get("what") == "dropped")
_notes = [x for x in _capr if x["kind"] == "notification"]
check("...ingested: the dropped events are counted in recording.gap records (no 'payload missing' problem), the "
      "rest are journaled, and the summary names the gap",
      lambda: _dropped + len(_notes) == 20 and _dropped >= 10 and "spool.problem" not in [x["kind"] for x in _capr]
      and "were dropped unrecorded" in summary.build(capture.task_of(SCAP)).text)
SCAPB = "sess-ux-cap-bytes"
for i in range(12):
    hook("Notification", ux_pay(SCAPB, UXD, message="x" * 900, notification_type="idle"),
         {"CHECKPOINT_SPOOL_KEEP_BYTES": "6000"})
_capb = sum(p.stat().st_size for p in capture.spool_dir(SCAPB).glob("*.ev") if '"recording.gap"' not in p.read_text())
check("...and over its byte cap (6000 here): what stays is under it",
      lambda: _capb <= 6000 and any('"dropped"' in p.read_text() for p in capture.spool_dir(SCAPB).glob("*.ev")))

# Pruning, at a session start, from the shell: dead sessions' spools quiet for a week.
_pr_home = Path(ENVF["CHECKPOINT_HOME"])
_old = time.time() - 8 * 86400
_live_cl = fake_claude()
for name, rec in (("sess-ux-old-dead", None), ("sess-ux-old-live", _live_cl.pid), ("sess-ux-recent-dead", None)):
    d = _pr_home / "spool" / name
    d.mkdir(parents=True)
    (d / "1.1.ev").write_text("{}\n")
    (d / "index").write_text("1\tNotification\t1.1.ev\t2\n")
    if rec:
        (_pr_home / "sessions").mkdir(exist_ok=True)
        (_pr_home / "sessions" / f"{name}.json").write_text(json.dumps({"session_id": name, "claude_pid": rec}))
    if "old" in name:
        for p in list(d.iterdir()) + [d]:
            os.utime(p, (_old, _old))
ux_run(UXP / "hooks" / "fast.sh", ["SessionStart"], ENVF, ux_pay("sess-ux-pruner", UXD, source="startup"))
check("a session start prunes the spool of a session gone for over a week (from the shell, no runtime needed)",
      lambda: not (_pr_home / "spool" / "sess-ux-old-dead").exists())
check("...but not one whose Claude process still runs, a recent one, or its own",
      lambda: (_pr_home / "spool" / "sess-ux-old-live").is_dir() and (_pr_home / "spool" / "sess-ux-recent-dead").is_dir()
      and (_pr_home / "spool" / "sess-ux-pruner").is_dir())

# --- /check:status: this session's summary (CK-UX-2) ----------------------------------------
print("/check:status shows this session's own summary, and this project's unfinished tasks:")
UXA, UXB = UX / "proj-a", UX / "proj-b"
UXA.mkdir()
UXB.mkdir()
CL_A, CL_B = fake_claude(), fake_claude()
SA, SB = "aaaaaaaa-0000-4000-8000-000000000001", "bbbbbbbb-0000-4000-8000-000000000002"
for s_, d_, cl, goal in ((SA, UXA, CL_A, "Fix widget A in project A"), (SB, UXB, CL_B, "Deploy service B in project B")):
    e_ = {"CLAUDE_PID": str(cl.pid)}
    hook("SessionStart", ux_pay(s_, d_, source="startup"), e_)
    hook("UserPromptSubmit", ux_pay(s_, d_, prompt=goal, prompt_id=f"g-{s_[:4]}"), e_)
    capture.ingest(s_)
TA, TB = state.load_session(SA).get("task_id"), state.load_session(SB).get("task_id")
_st_a = core.status_text(SA)
check("status for a session whose id sorts first: its own task's summary (not the last session's in the folder)",
      lambda: TA and f"Recovery summary for task {TA}" in _st_a and "Fix widget A" in _st_a
      and "Deploy service B" not in _st_a)
r = subprocess.run([sys.executable, str(ROOT / "run.py"), "status", "--session", SA], capture_output=True, text=True,
                   timeout=60)
check("...the same through the terminal helper's `status --session`",
      lambda: f"Recovery summary for task {TA}" in r.stdout and "Deploy service B" not in r.stdout)
_hk = ctx(hook("UserPromptSubmit", ux_pay(SA, UXA, prompt="/check:status"), {"CLAUDE_PID": str(CL_A.pid)}))
check("...and through /check:status in that session (the hook's answer)",
      lambda: f"Recovery summary for task {TA}" in _hk and "Deploy service B" not in _hk)
_st_none = core.status_text("")
check("status without a session: sessions and jobs, nobody's recovery summary",
      lambda: "Sessions:" in _st_none and "Recovery summary" not in _st_none)

# --- after a crash (CK-UX-3) --------------------------------------------------------------------
print("after a crash: the next session start says so on screen; /check:resume last means that task:")
UXC = UX / "proj-crash"
UXC.mkdir()
CL_K = fake_claude()
SK = "sess-ux-crashed-0001"
EK = {"CLAUDE_PID": str(CL_K.pid)}
hook("SessionStart", ux_pay(SK, UXC, source="startup"), EK)
hook("UserPromptSubmit", ux_pay(SK, UXC, prompt="Fix widget loading in the app\nand keep the API stable",
                                prompt_id="k1"), EK)
hook("PreToolUse", ux_pay(SK, UXC, hook_event_name="PreToolUse", tool_name="Write", tool_use_id="kw1",
                          tool_input={"file_path": str(UXC / "app.py"), "content": "x = 1\n"}), EK)
CL_K.kill()
CL_K.wait()
CL_N = fake_claude()
SN = "sess-ux-after-0002"
EN = {"CLAUDE_PID": str(CL_N.pid)}
_start = hook("SessionStart", ux_pay(SN, UXC, source="startup"), EN)
TK = state.load_session(SK).get("task_id") or ""
_msg = (_start or {}).get("systemMessage") or ""
check("the next session start in the project shows the person one line: the interrupted task, its goal (clipped), "
      "`claude --resume <id>` and `/check:resume <task>`",
      lambda: TK and f"task {TK} was interrupted" in _msg and "Fix widget loading in the app and keep" in _msg
      and f"`claude --resume {SK}`" in _msg and f"/check:resume {TK}" in _msg and "\n" not in _msg)
_stn = ctx(hook("UserPromptSubmit", ux_pay(SN, UXC, prompt="/check:status"), EN))
check("/check:status lists this project's unfinished tasks: the interrupted one, how to continue it",
      lambda: "Unfinished tasks in this project" in _stn and TK in _stn and "interrupted (no clean end)" in _stn
      and f"`claude --resume {SK}`" in _stn)
hook("UserPromptSubmit", ux_pay(SN, UXC, prompt="hi, what happened?", prompt_id="n1"), EN)
_res = ctx(hook("UserPromptSubmit", ux_pay(SN, UXC, prompt="/check:resume last"), EN))
check("`/check:resume last` after a first message (this session's own task has no work yet): the interrupted task, "
      "not this session's new one", lambda: "<checkpoint-resume>" in _res and TK in _res
      and state.load_session(SN).get("task_id") == TK)
CL_W = fake_claude()
SW = "sess-ux-worker-0003"
EW = {"CLAUDE_PID": str(CL_W.pid)}
hook("SessionStart", ux_pay(SW, UXC, source="startup"), EW)
hook("UserPromptSubmit", ux_pay(SW, UXC, prompt="Rename the config loader", prompt_id="w1"), EW)
hook("PreToolUse", ux_pay(SW, UXC, hook_event_name="PreToolUse", tool_name="Read", tool_use_id="wr1",
                          tool_input={"file_path": str(UXC / "app.py")}), EW)
hook("PostToolUse", ux_pay(SW, UXC, hook_event_name="PostToolUse", tool_name="Read", tool_use_id="wr1",
                           tool_input={"file_path": str(UXC / "app.py")}, tool_response={"type": "text"}), EW)
capture.ingest(SW)
TW = state.load_session(SW).get("task_id")
check("...a session whose own task has work: `last` is still another task (resuming its own does nothing); with no "
      "session, the newest", lambda: core.find_task("last", str(UXC), SW) not in ("", TW)
      and core.find_task("last", str(UXC)) == TW)
_other = hook("SessionStart", ux_pay("sess-ux-elsewhere", UX / "proj-a", source="startup"),
              {"CLAUDE_PID": str(fake_claude().pid)})
check("no notice in a project with nothing interrupted, nor at a resume",
      lambda: "was interrupted" not in ((_other or {}).get("systemMessage") or "")
      and "was interrupted" not in ((hook("SessionStart", ux_pay(SN, UXC, source="resume"), EN) or {})
                                    .get("systemMessage") or ""))
_mp = journal.jdir(TK) / "meta.json"
_mt = json.loads(_mp.read_text())
_mt["last_session"] = SK
_mt["updated"] = time.time() - 8 * 86400
_mp.write_text(json.dumps(_mt))
_late = hook("SessionStart", ux_pay("sess-ux-week-later", UXC, source="startup"), {"CLAUDE_PID": str(fake_claude().pid)})
check("...nor for a task interrupted more than a week ago (the context still lists it)",
      lambda: "was interrupted" not in ((_late or {}).get("systemMessage") or ""))

# --- the help text (CK-UX-4, CK-UX-12) ---------------------------------------------------------
print("the help text and command descriptions match what the commands do:")
HELP = read_or_empty(ROOT / "skills" / "help" / "SKILL.md")
check("/check:help never says the pause interrupts nothing (an immediate pause ends the turn)",
      lambda: HELP and "interrupts nothing" not in HELP and "finishes its current step" not in HELP
      and "ends the running turn at once" in HELP and "waits until the turn ends" in HELP)
_stop_desc = re.search(r"^description: *(.*)$", read_or_empty(ROOT / "skills" / "stop" / "SKILL.md"), re.M)
check("/check:stop's description (and /check-stop's) say it stops gateway jobs too",
      lambda: "gateway" in _stop_desc.group(1)
      and "gateway job" in read_or_empty(ROOT / "hooks" / "mod" / "register.tsx").split("name: 'check-stop'")[1]
      .split("})")[0])

# --- forget and purge (CK-UX-1b, CK-UX-6) -------------------------------------------------------
print("forget one task, purge everything (the terminal helper; /check:forget):")
FH = UX / "forget-home"
FENV = {"CHECKPOINT_HOME": str(FH), "CHECKPOINT_GATEWAY_CONTRACT": str(UX / "no-gateway")}
FP = UX / "proj-forget"
(FP / ".work" / "old").mkdir(parents=True)
(FP / ".work" / "old" / "TRACKER.md").write_text("# old\n")


def fcli(*args):
    return subprocess.run([sys.executable, str(ROOT / "run.py"), *args], capture_output=True, text=True, timeout=120,
                          env=dict(os.environ, **FENV))


def fsession(sid, goal, alive=True, work=True):
    cl = fake_claude()
    e_ = dict(FENV, CLAUDE_PID=str(cl.pid))
    hook("SessionStart", ux_pay(sid, FP, source="startup"), e_)
    hook("UserPromptSubmit", ux_pay(sid, FP, prompt=goal, prompt_id=f"f-{sid}"), e_)
    if work:
        hook("PreToolUse", ux_pay(sid, FP, hook_event_name="PreToolUse", tool_name="Read", tool_use_id=f"fr-{sid}",
                                  tool_input={"file_path": str(FP / "a.py")}), e_)
    with ux_env(**FENV):
        capture.ingest(sid)
        if work:   # one more event left in the spool, never ingested
            hook("Notification", ux_pay(sid, FP, message="left in the spool"), e_)
    if not alive:
        cl.kill()
        cl.wait()
    return cl, e_


_f_old, _ = fsession("sess-ux-f-old", "An old task to forget", alive=False)
_f_live, EFL = fsession("sess-ux-f-live", "A task still running")
_f_paused, _ = fsession("sess-ux-f-paused", "A paused task", alive=False)
(FH / "flags").mkdir(exist_ok=True)
(FH / "flags" / "sess-ux-f-paused").write_text("2026-10-22T00:00:00+0000 test\n")
with ux_env(**FENV):
    T_OLD = state.load_session("sess-ux-f-old").get("task_id")
    T_LIVE = state.load_session("sess-ux-f-live").get("task_id")
    T_PAUSED = state.load_session("sess-ux-f-paused").get("task_id")
    # its pause-time checkpoint (in the state folder, next to the journal) named the project's tracker, and
    # the session record keeps the tracker's real path (core.write_checkpoint)
    _f_cp = journal.jdir(T_OLD) / "checkpoint-sess-ux-f-old.json"
    state.write_json(_f_cp, {"session_id": "sess-ux-f-old", "tracker": str(FP / ".work" / "old" / "TRACKER.md")})
    state.update_session("sess-ux-f-old", checkpoint=str(_f_cp), tracker=str(FP / ".work" / "old" / "TRACKER.md"))
    from checkpoint import recovery as _ux_recovery
    _ux_recovery.export(T_OLD, "")
    _jdone = jobs.start("ux-done", command="true", cwd=str(FP), session_id="sess-ux-f-old", task_id=T_OLD)
    wait_for(lambda: jobs.is_final(jobs.refresh_all().get(_jdone["job"]["id"]) or {}), 15)
    _jrun = jobs.start("ux-run", command="sleep 60", cwd=str(FP), session_id="sess-ux-f-x", task_id=T_PAUSED)
r = fcli("forget", T_OLD, "--dry-run")
check("forget --dry-run: says what it would remove (the journal, the finished job, the export, the session record "
      "with its spool), removes nothing",
      lambda: r.returncode == 0 and "Would remove task" in r.stdout and "1 finished job(s)" in r.stdout
      and "1 export(s)" in r.stdout and "1 session record(s)" in r.stdout and journal.jdir(T_OLD).name
      and (FH / "journal" / T_OLD).is_dir() and (FH / "sessions" / "sess-ux-f-old.json").exists())
r = fcli("forget", T_OLD)
with ux_env(**FENV):
    _jobs_after = jobs.refresh_all()
check("forget <task>: its journal, checkpoints (the pause-time one beside it too), export, finished job (record and "
      "log), session record and spool are gone; the report names the tracker folder it left alone",
      lambda: r.returncode == 0 and "Forgot task" in r.stdout and not (FH / "journal" / T_OLD).exists()
      and not _f_cp.exists()
      and not list((FH / "exports").glob(f"*{T_OLD}*")) and _jdone["job"]["id"] not in _jobs_after
      and not (FH / "jobs" / _jdone["job"]["id"]).exists() and not (FH / "sessions" / "sess-ux-f-old.json").exists()
      and not (FH / "spool" / "sess-ux-f-old").exists() and str(FP / ".work" / "old") in r.stdout
      and (FP / ".work" / "old" / "TRACKER.md").exists())
r1, r2, r3 = fcli("forget", T_LIVE), fcli("forget", T_PAUSED), fcli("forget", "t_nosuchtask")
check("forget refuses a task with a live session, a paused one or a running job (nothing removed); an unknown "
      "reference says so",
      lambda: r1.returncode == 1 and "still in use" in r1.stdout and "runs" in r1.stdout
      and r2.returncode == 1 and "is paused" in r2.stdout and "job ux-run" in r2.stdout
      and (FH / "journal" / T_LIVE).is_dir() and (FH / "journal" / T_PAUSED).is_dir()
      and r3.returncode == 1 and "No task matches" in r3.stdout)
_fg = ctx(hook("UserPromptSubmit", ux_pay("sess-ux-f-live", FP, prompt=f"/check:forget {T_LIVE}"), EFL))
check("/check:forget answers in the hook: its own task is in use (this session runs), nothing removed",
      lambda: "<checkpoint-result>" in _fg and "(this one)" in _fg and (FH / "journal" / T_LIVE).is_dir())
check("/check:forget is a management command: it never resumes a paused session (the parsers agree)",
      lambda: transcript.parse_command("/check:forget t_1") == ("forget", ["t_1"])
      and "/check:forget*)" in read_or_empty(ROOT / "hooks" / "fast.sh")
      and "|export|forget)" in read_or_empty(ROOT / "hooks" / "mod" / "logic.ts"))
with ux_env(**FENV):
    _real_table = procs.table
    procs.table = lambda *a, **k: {}
    try:
        _nt = purge.forget(T_PAUSED)
        _np = purge.purge()
    finally:
        procs.table = _real_table
check("forget and purge refuse without a process table (nothing is known to be dead)",
      lambda: not _nt[0] and "process table" in _nt[1] and not _np[0] and "process table" in _np[1]
      and (FH / "journal" / T_PAUSED).is_dir())
_f_dead2, _ = fsession("sess-ux-f-dead2", "Another old task", alive=False)
with ux_env(**FENV):
    T_DEAD2 = state.load_session("sess-ux-f-dead2").get("task_id")
    _ux_recovery.export(T_DEAD2, "")
(FH / ".modes-v1").write_text("")      # a file an earlier version left (no code knows it any more)
r = fcli("purge", "--dry-run")
check("purge --dry-run lists what it would remove and keeps everything",
      lambda: r.returncode == 0 and "Purge (dry run)" in r.stdout and T_DEAD2 in r.stdout
      and (FH / "journal" / T_DEAD2).is_dir() and list((FH / "exports").glob("*")))
check("...an earlier version's leftover is named as a file it doesn't know, never as not checkpoint's",
      lambda: "files it doesn't know (an earlier version's leftovers, or not checkpoint's), left alone: .modes-v1"
      in r.stdout and "; not checkpoint's, left alone" not in r.stdout)
# Names in the per-session folders that are no session (a temp file, a bare suffix): never a whole folder.
for _stray in (FH / "checkpoints" / ".json", FH / "spool-errors" / ".last", FH / "count" / ".x.123.tmp"):
    _stray.parent.mkdir(parents=True, exist_ok=True)
    _stray.write_text("")
with ux_env(**FENV):
    # A finished job of the paused (kept) session, one of a dead session.
    _jkept = jobs.start("ux-kept-done", command="true", cwd=str(FP), session_id="sess-ux-f-paused", task_id=T_PAUSED)
    _jgone = jobs.start("ux-dead-done", command="true", cwd=str(FP), session_id="sess-ux-f-dead2", task_id=T_DEAD2)
    wait_for(lambda: all(jobs.is_final(jobs.refresh_all().get(x["job"]["id"]) or {}) for x in (_jkept, _jgone)), 15)
_two_days = time.time() - 2 * 86400
os.utime(FH / "spool", (_two_days, _two_days))   # (a spool folder itself never counts as a fresh session's)
r = fcli("purge")
check("purge: the dead sessions' journals, records, spools and the exports are gone; the live and paused sessions' "
      "(and the running job's) stay; the helper stays; it says how to remove the projects' .work/ folders",
      lambda: r.returncode == 0 and not (FH / "journal" / T_DEAD2).exists()
      and not (FH / "sessions" / "sess-ux-f-dead2.json").exists() and not (FH / "spool" / "sess-ux-f-dead2").exists()
      and not list((FH / "exports").glob("*")) and (FH / "journal" / T_LIVE).is_dir()
      and (FH / "journal" / T_PAUSED).is_dir() and (FH / "sessions" / "sess-ux-f-live.json").exists()
      and (FH / "flags" / "sess-ux-f-paused").exists() and (FH / "checkpoint").exists()
      and (FH / "spool" / "sess-ux-f-live").is_dir() and (FH / "spool" / "sess-ux-f-paused").is_dir()
      and "rm -rf <project>/.work" in r.stdout and str(FP) in r.stdout and "Kept, still needed" in r.stdout
      and (FP / ".work" / "old" / "TRACKER.md").exists() and (FH / ".modes-v1").exists())
with ux_env(**FENV):
    _reg_after = jobs.refresh_all()
check("...a kept (paused) session's finished job stays, with its folder (its pause-time checkpoint lists it); a "
      "dead session's goes",
      lambda: _jkept["job"]["id"] in _reg_after and (FH / "jobs" / _jkept["job"]["id"]).is_dir()
      and _jgone["job"]["id"] not in _reg_after and not (FH / "jobs" / _jgone["job"]["id"]).exists()
      and "1 finished job(s) of those sessions and tasks" in r.stdout)
with ux_env(**FENV):
    if _jrun.get("ok"):
        jobs.stop(_jrun["job"]["id"], "test")

# --- CHECKPOINT_TRACKERS=off (CK-UX-8) ----------------------------------------------------------
print("CHECKPOINT_TRACKERS=off: no tracker note, nudges or Stop block; the journal still records:")
UXT = UX / "proj-trackers"
(UXT / ".work" / "job").mkdir(parents=True)
CL_T = fake_claude()
ST = "sess-ux-trackers-off"
ETO = {"CLAUDE_PID": str(CL_T.pid), "CHECKPOINT_TRACKERS": "off"}
(UXT / ".work" / "job" / "TRACKER.md").write_text(
    f"# Job\nSession: {ST}\n\n## Stages\n| # | Stage | Status | N |\n|---|---|---|---|\n| 1 | Build | in progress | |\n")
_on = ctx(hook("SessionStart", ux_pay("sess-ux-trackers-on", UXT, source="startup"), {"CLAUDE_PID": str(CL_T.pid)}))
_off = ctx(hook("SessionStart", ux_pay(ST, UXT, source="startup"), ETO))
check("the session note asks for trackers by default, and not with CHECKPOINT_TRACKERS=off (no template, no exclude "
      "edit), which still explains pausing and jobs",
      lambda: "TRACKER.md" in _on and "info/exclude" in _on and "trackers off" in _off and "TRACKER.md" not in _off
      and "info/exclude" not in _off and "Tracker template" not in _off and "job_start" in _off)
time.sleep(0.05)
hook("UserPromptSubmit", ux_pay(ST, UXT, prompt="build it", prompt_id="t1"), ETO)
time.sleep(0.05)
_nudges = []
for i in range(4):
    hook("PreToolUse", ux_pay(ST, UXT, hook_event_name="PreToolUse", tool_name="Read", tool_use_id=f"to{i}",
                              tool_input={"file_path": str(UXT / "x")}), ETO)
    _nudges.append(ctx(hook("PostToolUse", ux_pay(ST, UXT, hook_event_name="PostToolUse", tool_name="Read",
                                                  tool_use_id=f"to{i}", tool_input={"file_path": str(UXT / "x")},
                                                  tool_response={"type": "text"}), ETO)))
_stop = hook("Stop", ux_pay(ST, UXT, stop_hook_active=False), ETO)
check("...no tracker reminder every N calls, no Stop block for a stale in-progress tracker",
      lambda: not any("tool calls" in n or "TRACKER" in n for n in _nudges) and (_stop or {}).get("decision") != "block")
capture.ingest(ST)
check("...and the journal still records every call", lambda: len([x for x in ux_recs(state.load_session(ST).get("task_id"))
                                                                 if x["kind"] == "tool.intent"]) == 4)
hook("UserPromptSubmit", ux_pay(ST, UXT, prompt="/check:pause"), ETO)
_deny = hook("PreToolUse", ux_pay(ST, UXT, hook_event_name="PreToolUse", tool_name="Bash", tool_use_id="tp1",
                                  tool_input={"command": "ls"}), ETO)
check("...paused, the deny asks for no tracker update", lambda: denied(_deny) and "TRACKER" not in reason(_deny)
      and "tracker" not in reason(_deny))
_resumed_off = ctx(hook("UserPromptSubmit", ux_pay(ST, UXT, prompt="go on"), ETO))
check("...and the resume context asks for no tracker upkeep (no 'Next action', no 'Keep the tracker current')",
      lambda: "<checkpoint-resume>" in _resumed_off and "Keep the tracker current" not in _resumed_off
      and "'Next action'" not in _resumed_off and "then continue from the plan." in _resumed_off)

# --- how a paused session resumes (CK-UX-11) -----------------------------------------------------
print("the pause says how to resume: a message (any non-/check command resumes):")
_pz = ctx(hook("UserPromptSubmit", ux_pay("sess-ux-resumewords", UXT, prompt="/check:pause"),
               {"CLAUDE_PID": str(CL_T.pid)}))
check("the pause's answer says a typed message resumes it, and that any non-/check command does",
      lambda: "any non-/check command resumes" in _pz)
hook("UserPromptSubmit", ux_pay("sess-ux-resumewords", UXT, prompt="back"), {"CLAUDE_PID": str(CL_T.pid)})
_npw = ux_run(UXP / "hooks" / "fast.sh", ["UserPromptSubmit"], ENVN, ux_pay("sess-ux-np", UXD, prompt="pause"))
check("...and so does the shell's fallback without a runtime",
      lambda: "any non-/check command resumes" in _npw.stdout)
# --- round 2 of the review -------------------------------------------------------------------
print("round 2: `last`, unrecorded commands, forget's shared spool, the trim, forget-job:")
from checkpoint import recovery as _r2_recovery  # noqa: E402
check("the hooks never edit a clone's exclude file (no core.ensure_excluded; only the session note asks Claude to)",
      lambda: not hasattr(core, "ensure_excluded")
      and not [p.name for p in (ROOT / "src" / "checkpoint").glob("*.py") if "info/exclude" in p.read_text()
               and p.name != "hooks.py"]
      and "info/exclude" in hooks.SESSION_NOTE)

# `/check:resume last` never means the session's own task; an interrupted one first (should-fix 7).
UXL = UX / "proj-last"
UXL.mkdir()


def ux_task(sid, goal, cl, work=True):
    e_ = {"CLAUDE_PID": str(cl.pid)}
    hook("SessionStart", ux_pay(sid, UXL, source="startup"), e_)
    hook("UserPromptSubmit", ux_pay(sid, UXL, prompt=goal, prompt_id=f"l-{sid}"), e_)
    if work:
        hook("PreToolUse", ux_pay(sid, UXL, hook_event_name="PreToolUse", tool_name="Read", tool_use_id=f"lr-{sid}",
                                  tool_input={"file_path": str(UXL / "a.py")}), e_)
    capture.ingest(sid)
    time.sleep(0.02)
    return state.load_session(sid).get("task_id"), e_


CL_L1, CL_L2, CL_L3 = fake_claude(), fake_claude(), fake_claude()
TL1, _el1 = ux_task("sess-ux-last-old", "An older task that was interrupted", CL_L1)
TL2, _ = ux_task("sess-ux-last-newer", "A newer task, still open", CL_L2)
TL3, EL3 = ux_task("sess-ux-last-own", "This session's own task, with work", CL_L3)
CL_L1.kill()
CL_L1.wait()
_last_own = core.find_task("last", str(UXL), "sess-ux-last-own")
_last_none = core.find_task("last", str(UXL))
_mp1 = journal.jdir(TL1) / "meta.json"     # its end recorded after all (the listing cache, its time unchanged)
_m1 = json.loads(_mp1.read_text())
_m1.setdefault("ended", {})["sess-ux-last-old"] = "other"
_mp1.write_text(json.dumps(_m1))
_last_noint = core.find_task("last", str(UXL), "sess-ux-last-own")
check("`last` asked by a session that has done work: never its own task; the interrupted task before a newer one "
      "that wasn't; with none interrupted, the newest other; with no session (the CLI), the newest",
      lambda: TL1 and TL2 and TL3 and _last_own == TL1 and _last_noint == TL2 and _last_none == TL3)
_lres = ctx(hook("UserPromptSubmit", ux_pay("sess-ux-last-own", UXL, prompt="/check:resume last"), EL3))
check("...and through /check:resume last in that session: it picks up another task, never its own",
      lambda: "<checkpoint-resume>" in _lres and state.load_session("sess-ux-last-own").get("task_id") == TL2)

# A command spooled while no runtime was there (should-fix 9).
ENVU, HOMEU = ux_rt("unrec", "nopixi")
SU = "sess-ux-unrecorded"
ux_run(UXP / "hooks" / "fast.sh", ["SessionStart"], ENVU, ux_pay(SU, UXD, source="startup"))
wait_for(lambda: not Path(f"{ENVU['CLAUDE_PLUGIN_DATA']}/envs/{LOCK_SHA12}.building").exists(), 20)
ux_run(UXP / "hooks" / "fast.sh", ["UserPromptSubmit"], ENVU, ux_pay(SU, UXD, prompt="ship it", prompt_id="u1"))
for _tid, _tool, _inp in (("ub1", "Bash", {"command": "git push origin main"}), ("ur1", "Read", {"file_path": "/x/a"})):
    ux_run(UXP / "hooks" / "fast.sh", ["PreToolUse"], ENVU, ux_pay(SU, UXD, hook_event_name="PreToolUse", tool_name=_tool,
                                                                  tool_use_id=_tid, tool_input=_inp))
subprocess.run([sys.executable, str(ROOT / "run.py"), "ingest", SU], capture_output=True, text=True, timeout=60,
               env=dict(os.environ, CHECKPOINT_HOME=str(HOMEU), CHECKPOINT_GATEWAY_CONTRACT=str(UX / "no-gateway")))
with ux_env(CHECKPOINT_HOME=HOMEU):
    TU = capture.task_of(SU)
    _ru = _r2_recovery.reconcile(TU) if TU else None
    _ru_text = _ru.text(limit=100000) if _ru else ""
    _ru_items = _ru.check_items() if _ru else []
    _su = summary.build(TU).text if TU else ""
_su_flight = _su.split("## In flight / unknown", 1)[1].split("\n## ", 1)[0] if "## In flight / unknown" in _su else ""
check("an unknown outcome of a command spooled while no runtime was there: '1 command not recorded (no runtime then), "
      "seq N (Bash, op ub1): check its effects', ranked like a possible external effect (never cut, first in the "
      "summary's In flight)",
      lambda: any(r == 0 and "1 command not recorded (no runtime then), seq " in t and "(Bash, op ub1): check its effects"
                  in t for r, t in _ru_items)
      and _ru.counts()["external_unknown"] == 1
      and _su_flight.splitlines()[1].startswith("- 1 command not recorded (no runtime then), seq ")
      and "(Bash, op ub1): outcome unknown" in _su_flight and "check its effects before any retry" in _su_flight)
check("...but a metadata-only Read is no external effect (not in Check first)",
      lambda: not any("ur1" in t for _r, t in _ru_items))

# Many commands while no runtime was there (round 3, must-fix 2): round 2 ranked every one never-cut, so 30 calls
# that returned made a 20,000-character resume context (120: 79,000, whatever the limit).
ENVF, HOMEF = ux_rt("flood", "nopixi")


def ux_flood(sid, real, clones, post=True):
    """A session with `real` Bash calls through the copied hooks (no runtime: spooled without contents), then
    `clones` more written the way the shell writes them (a real call's spool files, new ids); `post`: each with
    its PostToolUse. Its task, after an ingest."""
    run = lambda ev, tid: ux_run(UXP / "hooks" / "fast.sh", [ev], ENVF, ux_pay(  # noqa: E731
        sid, UXD, hook_event_name=ev, tool_name="Bash", tool_use_id=tid,
        tool_input={"command": f"git push origin b{tid}"}, **({"tool_response": {"stdout": "", "stderr": "",
                                                                                  "interrupted": False}}
                                                              if ev == "PostToolUse" else {})))
    ux_run(UXP / "hooks" / "fast.sh", ["SessionStart"], ENVF, ux_pay(sid, UXD, source="startup"))
    wait_for(lambda: not Path(f"{ENVF['CLAUDE_PLUGIN_DATA']}/envs/{LOCK_SHA12}.building").exists(), 20)
    ux_run(UXP / "hooks" / "fast.sh", ["UserPromptSubmit"], ENVF, ux_pay(sid, UXD, prompt="ship them", prompt_id="f1"))
    for i in range(real):
        run("PreToolUse", f"f{i}")
        if post:
            run("PostToolUse", f"f{i}")
    sp = HOMEF / "spool" / sid
    tmpl = {}
    for ln in (sp / "index").read_text().splitlines():
        f = ln.split("\t")
        if f[1] in ("PreToolUse", "PostToolUse") and ln.endswith("\tmeta=1"):
            tmpl.setdefault(f[1], json.loads((sp / f[2]).read_text()))
    with open(sp / "index", "a") as idx:
        for i in range(clones):
            for ev in ("PreToolUse", "PostToolUse") if post else ("PreToolUse",):
                body = json.dumps(dict(tmpl[ev], tool_use_id=f"c{i}"))
                (sp / f"clone.{i}.{ev}.ev").write_text(body + "\n")
                idx.write(f"1\t{ev}\tclone.{i}.{ev}.ev\t{len(body)}\tmeta=1\n")
    r = subprocess.run([sys.executable, str(ROOT / "run.py"), "ingest", sid], capture_output=True, text=True,
                       timeout=120, env=dict(os.environ, CHECKPOINT_HOME=str(HOMEF),
                                             CHECKPOINT_GATEWAY_CONTRACT=str(UX / "no-gateway")))
    with ux_env(CHECKPOINT_HOME=HOMEF):
        return capture.task_of(sid), r, tmpl


def ux_flood_view(task):
    """(check items, op items, resume texts at 4000 / 7000 / 8500 with the summary, the summary)."""
    with ux_env(CHECKPOINT_HOME=HOMEF):
        rc = _r2_recovery.reconcile(task)
        texts = {n: rc.text(lambda m: summary.build(task, limit=m).text, limit=n, summary_max=4000)
                 for n in (4000, 7000, 8500)}
        return rc.check_items(), rc.op_items(), texts, summary.build(task).text, rc.counts()


_fa_task, _fa_r, _fa_t = ux_flood("sess-ux-flood-30", 30, 0)
_fa_items, _fa_ops, _fa_texts, _fa_sum, _fa_n = ux_flood_view(_fa_task)
check("30 commands spooled while no runtime was there, each of which returned (PreToolUse and PostToolUse): none is "
      "an unknown outcome (nothing in Check first or Interrupted operations), the resume context stays within each "
      "limit (4000, 7000, 8500 with the summary), and the summary says they returned",
      lambda: _fa_r.returncode == 0 and _fa_t.get("PostToolUse", {}).get("checkpoint_meta")
      and not _fa_items and not _fa_ops and _fa_n["unknown"] == 0 and _fa_n["external_unknown"] == 0
      and all(len(x) <= n for n, x in _fa_texts.items())
      and re.search(r"- 30 commands returned whose command and result weren't recorded \(no runtime then\), "
                    r"seq \d+-\d+: what they did isn't known", _fa_sum)
      and "## In flight / unknown\n(nothing" in _fa_sum)
_fb_task, _fb_r, _ = ux_flood("sess-ux-flood-120", 2, 118)
_fb_items, _fb_ops, _fb_texts, _fb_sum, _fb_n = ux_flood_view(_fb_task)
check("...and 120 such calls: still nothing unknown, every resume context within its limit",
      lambda: _fb_r.returncode == 0 and not _fb_items and not _fb_ops and _fb_n["completed"] == 0
      and all(len(x) <= n for n, x in _fb_texts.items()))
_fc_task, _fc_r, _ = ux_flood("sess-ux-flood-open", 3, 117, post=False)
_fc_items, _fc_ops, _fc_texts, _fc_sum, _fc_n = ux_flood_view(_fc_task)
_fc_flight = _fc_sum.split("## In flight / unknown", 1)[1].split("\n## ", 1)[0]
check("120 commands with no outcome at all (only their PreToolUse, no runtime then): one never-cut line in Check "
      "first ('120 commands not recorded (no runtime then), seq a-b: check their effects'), one in Interrupted "
      "operations, one in the summary's In flight; each resume context within its limit and holding that line",
      lambda: _fc_r.returncode == 0 and _fc_n["unknown"] == 120 and _fc_n["external_unknown"] == 120
      and [r for r, t in _fc_items] == [0] and re.search(r"^120 commands not recorded \(no runtime then\), seq \d+-\d+: "
                                                        r"check their effects", _fc_items[0][1])
      and [r for r, t in _fc_ops] == [0] and "120 commands not recorded (no runtime then), seq " in _fc_ops[0][1]
      and _fc_flight.count("not recorded (no runtime then)") == 1 and "- 120 commands not recorded" in _fc_flight
      and all(len(x) <= n and "120 commands not recorded (no runtime then), seq " in x for n, x in _fc_texts.items()))

# forget: the spool of a session shared with another task is journaled first, never deleted (should-fix 11).
CL_SH = fake_claude()
ESH = dict(FENV, CLAUDE_PID=str(CL_SH.pid))
SSH_ = "sess-ux-f-share"
hook("SessionStart", ux_pay(SSH_, FP, source="startup"), ESH)
hook("UserPromptSubmit", ux_pay(SSH_, FP, prompt="Task A, to forget later", prompt_id="sha"), ESH)
_f_b, _ = fsession("sess-ux-f-taskb", "Task B, kept", alive=False, work=False)
with ux_env(**FENV):
    capture.ingest(SSH_)
    T_SA = state.load_session(SSH_).get("task_id")
    T_SB = state.load_session("sess-ux-f-taskb").get("task_id")
    core.adopt(SSH_, T_SB)      # this session goes on with task B
hook("Notification", ux_pay(SSH_, FP, message="waiting in the spool, task B's", notification_type="idle"), ESH)
CL_SH.kill()
CL_SH.wait()
r = fcli("forget", T_SA)
with ux_env(**FENV):
    _sb_notes = [x for x in ux_recs(T_SB) if x["kind"] == "notification" and x.get("session") == SSH_]
check("forget <task>: a session it shares with another task keeps its record, and what waited in its spool is "
      "journaled into that task first (not deleted with the forgotten one)",
      lambda: r.returncode == 0 and not (FH / "journal" / T_SA).exists() and (FH / "journal" / T_SB).is_dir()
      and len(_sb_notes) == 1 and (FH / "sessions" / f"{SSH_}.json").exists()
      and "First journaled what waited in the spool of 1 session(s)" in r.stdout)

# The trim without a runtime: rotated, proportional, and read back by Python (should-fix 12).
ENVR, HOMER = ux_rt("trim", "nopixi")
ENVR["CHECKPOINT_SPOOL_KEEP_COUNT"] = "10"
STR_ = "sess-ux-trim-1"


def ux_calls(env, sid, a, b):
    for i in range(a, b):
        ux_run(UXP / "hooks" / "fast.sh", ["PreToolUse"], env, ux_pay(
            sid, UXD, hook_event_name="PreToolUse", tool_name="Read", tool_use_id=f"tr{i}",
            tool_input={"file_path": f"/x/{i}"}))


def ux_ingest(home, sid):
    return subprocess.run([sys.executable, str(ROOT / "run.py"), "ingest", sid], capture_output=True, text=True,
                          timeout=60, env=dict(os.environ, CHECKPOINT_HOME=str(home),
                                               CHECKPOINT_GATEWAY_CONTRACT=str(UX / "no-gateway")))


def ux_counted(home, sid):
    """(journaled intents, dropped in recording.gap records, spool problems) of the session's task."""
    with ux_env(CHECKPOINT_HOME=home):
        recs = ux_recs(capture.task_of(sid))
    return ({x["op"] for x in recs if x["kind"] == "tool.intent"},
            sum(x["data"].get("events") or 0 for x in recs if x["kind"] == "recording.gap"
                and x["data"].get("what") == "dropped"),
            [x for x in recs if x["kind"] == "spool.problem"])


ux_calls(ENVR, STR_, 0, 40)
_trd = HOMER / "spool" / STR_
_rot = sorted(p.name for p in _trd.glob("index.s*"))
_done = int(read_or_empty(_trd / ".trimdone").strip() or 0)
check("no runtime: each trim moves the index to index.s<n> (nothing else would rotate it), so the next trim starts "
      "after it; the ones a trim read to the end are marked (.trimdone) and never read again",
      lambda: len(_rot) >= 3 and len(ux_index(HOMER, STR_)) <= 11 and 1 <= _done <= len(_rot)
      and all((_trd / f"index.s{k:06d}").exists() for k in range(1, _done + 1)))
# A payload named in a finished index (planted there now): a trim that read that index again would drop it first.
(_trd / "planted.ev").write_text(json.dumps({"session_id": STR_, "hook_event_name": "Notification",
                                             "message": "planted", "notification_type": "idle"}) + "\n")
with open(_trd / f"index.s{_done:06d}", "a") as _f:
    _f.write("1\tNotification\tplanted.ev\t90\n")
_gaps_before = len([p for p in _trd.glob("*.ev") if '"dropped"' in p.read_text()])
ux_calls(ENVR, STR_, 40, 60)
_gaps_after = len([p for p in _trd.glob("*.ev") if '"dropped"' in p.read_text()])
check("...a trim's cost follows what it drops: an index a trim finished is never read again (a payload planted in "
      "one stays while newer ones are dropped)",
      lambda: (_trd / "planted.ev").exists() and _gaps_after > _gaps_before)
_ir = ux_ingest(HOMER, STR_)
_ti, _td, _tp = ux_counted(HOMER, STR_)
check("...Python reads the rotated indexes (index.s<n>) before the current one: every call is journaled by name or "
      "counted as dropped in a recording.gap, no 'payload missing' problem",
      lambda: _ir.returncode == 0 and len(_ti) + _td == 60 and _td >= 40 and not _tp)
ENVI, HOMEI = ux_rt("trim-ino", "nopixi")
ENVI["CHECKPOINT_SPOOL_KEEP_COUNT"] = "10"
SI_ = "sess-ux-trim-ino"
ux_calls(ENVI, SI_, 0, 5)
_i1 = ux_ingest(HOMEI, SI_)        # (a runtime that was there for a while: the cursor is inside the index)
ux_calls(ENVI, SI_, 5, 30)
_i2 = ux_ingest(HOMEI, SI_)
_ii, _id, _ip = ux_counted(HOMEI, SI_)
check("...and an index the cursor was inside when the shell rotated it is read on from where the cursor was (the "
      "same inode): nothing read twice, no problem",
      lambda: _i1.returncode == 0 and _i2.returncode == 0 and len(_ii) + _id == 30 and _id > 0 and not _ip
      and list((HOMEI / "spool" / SI_).glob("index.s*")))
_scap_rot = list(capture.spool_dir(SCAP).glob("index.s*"))
check("...with a runtime ready the shell never rotates the index (Python does, after an ingest)", lambda: not _scap_rot)
# A trim while a spool-error mark is there: the trim's own recording.gap record clears the mark (recording works
# again), whose reserve re-arming (ck_arm) used the trim's folder variable; round 2 resets it after that record.
ENVE, HOMEE = ux_rt("trim-err", "nopixi")
ENVE["CHECKPOINT_SPOOL_KEEP_COUNT"] = "10"
SE_ = "sess-ux-trim-err"
ux_calls(ENVE, SE_, 0, 10)
(HOMEE / "spool-errors").mkdir(parents=True, exist_ok=True)
(HOMEE / "spool-errors" / SE_).write_text("2026-10-22T00:00:00 can't append (disk full?)\n")
ux_calls(ENVE, SE_, 10, 11)
_te_dir = HOMEE / "spool" / SE_
_te_ev = [p_ for p_ in _te_dir.glob("*.ev") if '"recording.gap"' not in p_.read_text()]
check("a trim while a spool-error mark is there drops what it says it drops (the gap record clearing the mark doesn't "
      "send the removal elsewhere), leaves no trim lock behind, and the failure becomes history",
      lambda: len(_te_ev) <= 6 and not (_te_dir / ".trim").exists()
      and not (HOMEE / "spool-errors" / SE_).exists() and (HOMEE / "spool-errors" / f"{SE_}.last").exists()
      and not (HOMEE / "journal-errors" / ".trim").exists())

# A torn read of spool/<session>/.size (a read racing another hook's write, seen under load: "2099 06216"):
# sh arithmetic reads a leading zero as octal and the hook died in ck_spool, before the paused deny.
CL_TS = fake_claude()
ETS = {"CLAUDE_PID": str(CL_TS.pid)}
STS = "sess-ux-torn-size"


def ux_torn(event, payload):
    (capture.spool_dir(STS) / ".size").write_text("078 06216\n")
    r = subprocess.run([SH, str(ROOT / "hooks" / "fast.sh"), event], capture_output=True, text=True, timeout=60,
                       input=json.dumps(payload), env=dict(os.environ, **ETS))
    return r, json.loads(r.stdout.strip() or "{}")


hook("SessionStart", ux_pay(STS, UXD, source="startup"), ETS)
_tn_r, _ = ux_torn("Notification", ux_pay(STS, UXD, hook_event_name="Notification", message="m",
                                          notification_type="idle"))
_ts_size = read_or_empty(capture.spool_dir(STS) / ".size").split()
hook("UserPromptSubmit", ux_pay(STS, UXD, prompt="/check:pause"), ETS)
_ts_r, _ts_out = ux_torn("PreToolUse", ux_pay(STS, UXD, hook_event_name="PreToolUse", tool_name="Bash",
                                              tool_use_id="ts1", tool_input={"command": "ls"}))
check("a torn .size line with leading zeros (078 06216): the hook spools the event and counts on in decimal (79), and, "
      "paused, still denies the call",
      lambda: _tn_r.returncode == 0 and _ts_size[:1] == ["79"] and int(_ts_size[1]) > 6216
      and _ts_r.returncode == 0 and denied(_ts_out)
      and not any(x in r_.stderr for r_ in (_tn_r, _ts_r) for x in ("Illegal number", "too great for base")))
hook("UserPromptSubmit", ux_pay(STS, UXD, prompt="go on"), ETS)
STB = "sess-ux-torn-big"
for _i in range(6):
    hook("Notification", ux_pay(STB, UXD, message=f"n{_i}", notification_type="idle"))
(capture.spool_dir(STB) / ".size").write_text("152482 4993\n")      # read under 16 writers at once
hook("Notification", ux_pay(STB, UXD, message="n6", notification_type="idle"))
_tb_files = list(capture.spool_dir(STB).glob("*.ev"))
check("...a torn count far over the cap (152482) drops nothing: the spool is counted for real first (7 payloads, no "
      "recording.gap), and its true size is written back",
      lambda: len(_tb_files) == 7 and not any('"recording.gap"' in p_.read_text() for p_ in _tb_files)
      and read_or_empty(capture.spool_dir(STB) / ".size").split()[:1] == ["7"])
(Path(os.environ["CHECKPOINT_HOME"]) / "count" / STS).write_text("0\n0\n")    # two writes over each other
(Path(os.environ["CHECKPOINT_HOME"]) / "turn" / STS).write_text("08\n")
_tc_r = subprocess.run([SH, str(ROOT / "hooks" / "fast.sh"), "PostToolUse"], capture_output=True, text=True, timeout=60,
                       input=json.dumps(ux_pay(STS, UXD, hook_event_name="PostToolUse", tool_name="Bash",
                                               tool_use_id="ts2", tool_input={"command": "ls"},
                                               tool_response={"stdout": "", "stderr": "", "interrupted": False})),
                       env=dict(os.environ, **ETS))
check("...and so do the PostToolUse counters (count/, turn/): a torn or zero-led value counts as 0 or in decimal, the "
      "hook goes on",
      lambda: _tc_r.returncode == 0 and "Illegal number" not in _tc_r.stderr and "too great" not in _tc_r.stderr
      and "syntax" not in _tc_r.stderr
      and read_or_empty(Path(os.environ["CHECKPOINT_HOME"]) / "turn" / STS).strip() == "9"
      and read_or_empty(Path(os.environ["CHECKPOINT_HOME"]) / "count" / STS).strip() in ("1", "0"))
CL_TS.kill()

# forget-job: a gateway job stuck unknown, released only by the person (CK-DATA-2's rest).
FJ = UX / "fj-home"
FJENV = {"CHECKPOINT_HOME": str(FJ), "CHECKPOINT_GATEWAY_CONTRACT": str(UX / "no-gateway")}
TFJ = "t_ux_forgetjob_0001"


def fj_job(jid, days, owner="gateway", st="running", seen=None):
    """A job started `days` ago; a gateway one seen unknown since then (unknown_ts) and last reported on `seen`
    days ago (default: a day before that); unknown_ts None: never seen unknown (only the last word's age)."""
    now = time.time()
    rec = {"id": jid, "name": f"deploy-{jid}", "command": "make deploy", "cwd": str(UX), "state": st,
           "started": state.now_iso(), "started_ts": now - (days or seen) * 86400 - 60, "task_id": TFJ,
           "session_id": "sess-ux-fj", "log": str(FJ / "jobs" / jid / "output.log")}
    if owner == "gateway":
        rec.update(owner="gateway", gateway_job=f"gj-{jid}", seen_ts=now - (seen or days + 1) * 86400, seen_epoch=1)
        if days:
            rec["unknown_ts"] = now - days * 86400
    return rec


with ux_env(**FJENV):
    journal.append(TFJ, [{"kind": "session.start", "op": "fj-s", "session": "sess-ux-fj", "data": {"cwd": str(UX)}},
                         {"kind": "job.started", "op": "fj-j", "session": "sess-ux-fj",
                          "data": {"ok": True, "job": "fj-old", "name": "deploy-fj-old", "supervisor": "gateway",
                                   "gateway_job": "gj-fj-old", "command": "make deploy"}}], sync=True)
    with state.lock():
        _reg = jobs._registry()
        for _j in (fj_job("fj-old", 8), fj_job("fj-recent", 2), fj_job("fj-own", 9, owner="checkpoint", st="exited"),
                   fj_job("fj-stale", None, seen=8), fj_job("fj-race", 8), fj_job("fj-clear", 8),
                   fj_job("fj-lost", 8, seen=9), dict(fj_job("fj-legacy", None, seen=8), state="unknown", gone=True,
                                                      unknown_why="the gateway restarted and no longer lists it")):
            _reg["jobs"][_j["id"]] = _j
        jobs._save(_reg)
    _fj_before = jobs.refresh_all()
    _fj_doctor = core.gateway_doctor()


def fjcli(*args):
    return subprocess.run([sys.executable, str(ROOT / "run.py"), *args], capture_output=True, text=True, timeout=120,
                          env=dict(os.environ, **FJENV))


_fj_doctor_py = fjcli("doctor-py", "sess-ux-fj")     # what the mod's /check-doctor adds after its shell report
check("/check:doctor names a gateway job unknown for over 7 days with no word from the gateway, and how to release it "
      "(`forget-job <id>`; also in the lines the mod's /check-doctor gets: doctor-py); not one unknown for 2 days; "
      "until then it counts as possibly running",
      lambda: any("fj-old" in ln and "forget-job fj-old" in ln and "8 day(s)" in ln for ln in _fj_doctor)
      and not any("fj-recent" in ln for ln in _fj_doctor) and jobs.is_live(_fj_before["fj-old"])
      and _fj_doctor_py.returncode == 0 and "forget-job fj-old" in _fj_doctor_py.stdout
      and "journal:" in _fj_doctor_py.stdout and "fj-recent" not in _fj_doctor_py.stdout)
with ux_env(**FJENV):
    _fj_stale_reg = dict(jobs._registry()["jobs"]["fj-stale"])
    _fj_legacy_reg = dict(jobs._registry()["jobs"]["fj-legacy"])
r_stale = fjcli("forget-job", "fj-stale")
check("a job whose last word from the gateway is 8 days old (nobody asked since) and that the gateway can't be asked "
      "about for the first time now: unknown since now, not for 8 days (round 2 measured from the last word and "
      "released it): the registry stores when (unknown_ts), doctor doesn't name it, forget-job refuses, it stays "
      "running in the registry",
      lambda: abs(_fj_stale_reg.get("unknown_ts", 0) - time.time()) < 300 and _fj_stale_reg["state"] == "running"
      and not jobs.releasable(_fj_before["fj-stale"]) and jobs.is_unknown(_fj_before["fj-stale"])
      and not any("fj-stale" in ln for ln in _fj_doctor) and "fj-stale" not in _fj_doctor_py.stdout
      and r_stale.returncode == 1 and "only 0.0 day(s)" in r_stale.stdout and "Nothing changed" in r_stale.stdout)
check("...while one lost in a gateway restart before unknown_ts existed (0.7.2's records: unknown, gone, seen_ts set "
      "when it was lost) is unknown since it was lost, 8 days ago: doctor names it",
      lambda: abs(_fj_legacy_reg.get("unknown_ts", 0) - _fj_legacy_reg["seen_ts"]) < 1
      and jobs.releasable(_fj_before["fj-legacy"]) and any("fj-legacy" in ln for ln in _fj_doctor))
r_recent, r_own, r_old = fjcli("forget-job", "fj-recent"), fjcli("forget-job", "fj-own"), fjcli("forget-job", "fj-old")
r_fjstop = fjcli("stop", "fj-old")
with ux_env(**FJENV):
    _fj_tool_stop = jobs.stop("fj-old", "test")     # what the job_stop tool gets
with ux_env(**FJENV):
    _fj_after = jobs._registry()["jobs"]
    _fj_recs = ux_recs(TFJ)
    _fj_sum = summary.build(TFJ).text
    _fj_rc = _r2_recovery.reconcile(TFJ)
check("forget-job refuses a job unknown for only 2 days and one of checkpoint's own runner; nothing changes",
      lambda: r_recent.returncode == 1 and "only 2.0 day(s)" in r_recent.stdout and r_own.returncode == 1
      and "own runner" in r_own.stdout and _fj_after["fj-recent"]["state"] == "running"
      and _fj_after["fj-own"]["state"] == "exited")
check("forget-job <id> on the 8-day one: released (no longer live, final for checkpoint's records, so it pins no "
      "journal), recorded in its task's journal; the summary and the reconciliation say its outcome was never known",
      lambda: r_old.returncode == 0 and "Released job" in r_old.stdout and _fj_after["fj-old"]["state"] == "released"
      and not jobs.is_live(_fj_after["fj-old"]) and jobs.is_final(_fj_after["fj-old"])
      and any(x["kind"] == "job.released" and x["data"].get("job") == "fj-old" for x in _fj_recs)
      and "its outcome was never known: released by the person" in _fj_sum
      and any(j["id"] == "fj-old" and j["cls"] == "unknown" and "released by the person" in j["why"]
              for j in _fj_rc.jobs))
check("...and a stop of a released job never says it isn't running or was stopped: its outcome is unknown",
      lambda: "not running" not in r_fjstop.stdout and "stopped" not in r_fjstop.stdout.replace("not stopped", "")
      and "unknown" in r_fjstop.stdout and "released" in r_fjstop.stdout
      and _fj_tool_stop.get("outcome") == "unknown" and not _fj_tool_stop.get("stopped")
      and "released" in _fj_tool_stop.get("reason", ""))

# release() looks again under the lock: a word from the gateway landing between its refresh and the lock wins.
with ux_env(**FJENV):
    _fj_real_refresh = jobs.refresh_all

    def _fj_racing(*a, **k):
        view = _fj_real_refresh(*a, **k)
        jobs.note_gateway_states({"gj-fj-race": {"job": "gj-fj-race", "state": "exited", "exit": 0}}, 1, set())
        return view
    jobs.refresh_all = _fj_racing
    try:
        _fj_race = core.forget_job("fj-race")
    finally:
        jobs.refresh_all = _fj_real_refresh
    _fj_race_reg = dict(jobs._registry()["jobs"]["fj-race"])
    _fj_race_recs = [x for x in ux_recs(TFJ) if x["kind"] == "job.released" and x["data"].get("job") == "fj-race"]
check("forget-job when the gateway reports the job exited between release()'s look and its lock: not released, the "
      "registry keeps the gateway's exited, no job.released in the journal",
      lambda: _fj_race[0] is False and "changed while it was being released" in _fj_race[1]
      and _fj_race_reg["state"] == "exited" and _fj_race_reg.get("exit_code") == 0 and not _fj_race_recs)
# The gateway's word clears unknown_ts; a later unreachable moment starts it again from then.
with ux_env(**FJENV):
    jobs.note_gateway_states({"gj-fj-clear": {"job": "gj-fj-clear", "state": "running"}}, 1, set())
    _fj_cleared = dict(jobs._registry()["jobs"]["fj-clear"])
    _fj_clear_view = jobs.refresh_all()["fj-clear"]
    _fj_clear_again = dict(jobs._registry()["jobs"]["fj-clear"])
    # Lost in a gateway restart (a snapshot of a new epoch without it) after 8 days unknown: still unknown since then.
    jobs.note_gateway_states({}, 2, {"gj-fj-lost"})
    _fj_lost = dict(jobs._registry()["jobs"]["fj-lost"])
    _fj_lost_rel = jobs.releasable(jobs.refresh_all()["fj-lost"])
    with state.lock():
        _reg = jobs._registry()
        _reg["jobs"]["fj-new-lost"] = fj_job("fj-new-lost", None, seen=8)
        _changed = jobs._apply_view(_reg, {}, 3)
        _fj_new_lost = dict(_reg["jobs"]["fj-new-lost"])
check("the gateway's word on a job clears when it was first seen unknown; unreachable again later, it is unknown from "
      "then (not releasable); one a new epoch's snapshot no longer lists is unknown from that moment, and one unknown "
      "for 8 days before that keeps its time",
      lambda: "unknown_ts" not in _fj_cleared and _fj_cleared["state"] == "running"
      and abs(_fj_clear_again.get("unknown_ts", 0) - time.time()) < 300 and not jobs.releasable(_fj_clear_view)
      and _fj_lost["state"] == "unknown" and _fj_lost.get("gone") and time.time() - _fj_lost["unknown_ts"] > 7.9 * 86400
      and _fj_lost_rel and _changed and _fj_new_lost.get("gone") and abs(_fj_new_lost.get("unknown_ts", 0) - time.time()) < 300
      and not jobs.releasable(_fj_new_lost))

# --- round 4 (round 3's should-fix 2 and 3) ---------------------------------------------------
print("round 4: release's second look, when a job was first unknown, _scope's guard, returned calls' wording:")


def fj_age(jid, days):
    """The record as it reads `days` later: its seen and unknown times that much earlier."""
    with state.lock():
        reg = jobs._registry()
        for k in ("seen_ts", "unknown_ts"):
            if isinstance(reg["jobs"][jid].get(k), (int, float)):
                reg["jobs"][jid][k] -= days * 86400
        jobs._save(reg)


def fj_rec(jid):
    with state.lock():
        return dict(jobs._registry()["jobs"][jid])


_lost_why = "the gateway restarted and no longer lists it"
with ux_env(**FJENV):
    with state.lock():
        _reg = jobs._registry()
        for _j in (dict(fj_job("fj-race-unk", 8), state="unknown", gone=True, unknown_why=_lost_why),
                   dict(fj_job("fj-race-since", 8), state="unknown", gone=True, unknown_why=_lost_why),
                   fj_job("fj-norec", None, seen=1),
                   dict(fj_job("fj-legacy2", None, seen=8), state="unknown", gone=True, unknown_why=_lost_why)):
            _reg["jobs"][_j["id"]] = _j
        # A snapshot of the same epoch with no record of fj-norec; one that doesn't list the lost fj-legacy2 (from
        # before unknown_ts: its loss time is backfilled from seen_ts).
        jobs._apply_view(_reg, {}, 1)
        _reg["jobs"]["fj-lost2"] = fj_job("fj-lost2", None, seen=1)
        jobs._save(_reg)
    # fj-lost2: lost in a gateway restart.
    jobs.note_gateway_states({}, 2, {"gj-fj-lost2"})
    for _jid in ("fj-lost2", "fj-norec"):
        fj_age(_jid, 8)     # eight days on
    # Then the gateway restarts again and still doesn't list any of the three.
    jobs.note_gateway_states({}, 3, {"gj-fj-lost2", "gj-fj-norec", "gj-fj-legacy2"})
    _fj_kept = {j: fj_rec(j) for j in ("fj-lost2", "fj-norec", "fj-legacy2")}
check("a job's first unknown moment is kept through a later write of its seen time: lost in a restart (or with no "
      "record in the gateway's snapshot, or lost before unknown_ts existed) 8 days ago and lost again in another "
      "restart now, it is releasable (unknown since the first moment, not since the second restart)",
      lambda: all(jobs.releasable(r) and time.time() - r["unknown_ts"] > 7.9 * 86400 and time.time() - r["seen_ts"] < 300
                  for r in _fj_kept.values()))
with ux_env(**FJENV):
    _fj_real_refresh = jobs.refresh_all

    def _fj_unknown_again(*a, **k):
        view = _fj_real_refresh(*a, **k)
        # The gateway's word arrives in between: still `unknown` (the same state), a new seen time.
        jobs.note_gateway_states({"gj-fj-race-unk": {"job": "gj-fj-race-unk", "state": "unknown"}}, 1, set())
        return view

    def _fj_since_moved(*a, **k):
        view = _fj_real_refresh(*a, **k)
        with state.lock():      # a writer that moved only its unknown time (state and seen time unchanged)
            reg = jobs._registry()
            reg["jobs"]["fj-race-since"]["unknown_ts"] = time.time()
            jobs._save(reg)
        return view
    _fj_races = []
    for _fake, _jid in ((_fj_unknown_again, "fj-race-unk"), (_fj_since_moved, "fj-race-since")):
        jobs.refresh_all = _fake
        try:
            _fj_races.append((core.forget_job(_jid), fj_rec(_jid)))
        finally:
            jobs.refresh_all = _fj_real_refresh
check("forget-job looks again under the lock at the seen time and the unknown time, not only the state: the "
      "gateway saying `unknown` again in between (same state, new seen time), or the unknown time moved, releases "
      "nothing",
      lambda: all(res[0] is False and "changed while it was being released" in res[1] and rec["state"] == "unknown"
                  for res, rec in _fj_races))

# _scope never matches a command recorded without its contents to a later success "by name": a background end of
# another such command (both read as "Bash") is not this one's.
T_SC = "t_ux_scope_unrec_01"
with ux_env(**FJENV):
    journal.append(T_SC, [{"kind": "session.start", "op": "sc-s", "session": "sess-ux-sc", "data": {"cwd": str(UX)}},
                          {"kind": "tool.intent", "op": "sc-u1", "session": "sess-ux-sc",
                           "data": {"tool": "Bash", "payload_dropped": True}},
                          {"kind": "tool.intent", "op": "sc-u2", "session": "sess-ux-sc",
                           "data": {"tool": "Bash", "payload_dropped": True}},
                          {"kind": "tool.outcome", "op": "sc-u2", "session": "sess-ux-sc",
                           "data": {"tool": "Bash", "payload_dropped": True, "background": "unknown"}},
                          {"kind": "background.result", "op": "bgr:sc-u2:completed:1", "session": "sess-ux-sc",
                           "data": {"tool_use_id": "sc-u2", "status": "completed", "final": True,
                                    "via": "task-notification"}}], sync=True)
    _real_table = procs.table
    procs.table = lambda *a, **k: {}
    try:
        _sc_items = _r2_recovery.reconcile(T_SC).check_items()
    finally:
        procs.table = _real_table
check("an unrecorded command with no outcome stays in Check first though another unrecorded one's background end "
      "was reported later (round 3's _scope guard; without it both read as `Bash` and the later end hid this one)",
      lambda: any(r == 0 and "1 command not recorded (no runtime then)" in t and "(Bash, op sc-u1)" in t
                  for r, t in _sc_items))
_e_ops = [_r2_recovery.Op(f"e{i}", "unknown", "Bash", "Bash", "no result was recorded", 10 + i, external="unrecorded")
          for i in range(2)]
_e_line = next((t for _r, t in _r2_recovery.Recon("t_ux_earlier", {}, earlier=_e_ops, horizon=9).check_items()
                if "An earlier resume" in t), "")
check("...and an earlier resume's unrecorded commands are named once in its line ('command not recorded (no runtime "
      "then) and 1 more'), not once each",
      lambda: _e_line.count(_r2_recovery.UNRECORDED) == 1 and "reported 2 operation(s)" in _e_line
      and "and 1 more" in _e_line)

# Should-fix 3: the runtime went away mid-call: its PreToolUse was recorded with its command, its PostToolUse came
# without contents. The summary names the command and says what isn't known (never "whose command ... weren't
# recorded"); in flight at a checkpoint it is an unknown outcome, never "confirmed completed".
T_RK = "t_ux_returned_known_01"
with ux_env(**FJENV):
    journal.append(T_RK, [{"kind": "session.start", "op": "rk-s", "session": "sess-ux-rk", "data": {"cwd": str(UX)}},
                          {"kind": "tool.intent", "op": "rk1", "session": "sess-ux-rk",
                           "data": {"tool": "Bash", "input": {"command": "git push origin main"}}}], sync=True)
    _rk_seq = next(x["seq"] for x in ux_recs(T_RK) if x["op"] == "rk1")
    journal.append(T_RK, [{"kind": "checkpoint", "op": "c_rk01", "session": "sess-ux-rk",
                           "data": {"id": "c_rk01", "seq_from": 1, "seq_to": _rk_seq, "reason": "saved mid-call"}},
                          {"kind": "tool.outcome", "op": "rk1", "session": "sess-ux-rk",
                           "data": {"tool": "Bash", "payload_dropped": True, "background": "unknown"}},
                          {"kind": "tool.intent", "op": "rk2", "session": "sess-ux-rk",
                           "data": {"tool": "Bash", "payload_dropped": True}},
                          {"kind": "tool.outcome", "op": "rk2", "session": "sess-ux-rk",
                           "data": {"tool": "Bash", "payload_dropped": True, "background": "unknown"}}], sync=True)
    _rk_sum = summary.build(T_RK).text
    procs.table = lambda *a, **k: {}
    try:
        _rk_rc = _r2_recovery.reconcile(T_RK)
    finally:
        procs.table = _real_table
_rk_op = next((o for o in _rk_rc.ops if o.op == "rk1"), None)
check("a call recorded with its command whose result came without contents: the summary says `git push origin main` "
      "returned and that whether it worked (or went on in the background) isn't known, and gives the one recorded "
      "with neither its own line",
      lambda: re.search(r"- Bash `git push origin main`: returned \(observed, seq \d+\), but its result wasn't recorded "
                        r"\(no runtime then\): whether it worked, or went on in the background, isn't known", _rk_sum)
      and _rk_sum.count("returned whose command and result weren't recorded") == 1
      and "(Bash, op rk2): what it did isn't known" in _rk_sum)
check("...and in flight at the checkpoint it is an unknown outcome with a possible external effect (first in Check "
      "first), never [confirmed completed]",
      lambda: _rk_op is not None and _rk_op.cls == "unknown" and "in flight at the checkpoint; it returned afterwards"
      in _rk_op.why and "went on in the background, isn't known" in _rk_op.why
      and any(r == 0 and "git push origin main" in t for r, t in _rk_rc.check_items())
      and not any("[confirmed completed]" in t and "rk1" in t for _r, t in _rk_rc.op_items()))

for _c in (CL_A, CL_B, CL_N, CL_W, CL_T, _live_cl, _f_live):
    with _ux_contextlib.suppress(Exception):
        _c.kill()
