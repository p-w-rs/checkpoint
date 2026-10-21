# The gateway integration (PLAN-v4 phase 4): CONTRACT-v1 discovery (Python and
# sh agree), calls, delegated jobs (one supervisor per job, idempotency keys,
# no duplicate start after a lost answer), pause/resume/cancel through the
# gateway (per-job on_pause outcomes, once per pause, late delivery), the
# journal (snapshots, events: duplicates, out of order, epochs) and recovery
# (gateway job states; unknown, never done, when the gateway is gone or a new
# epoch doesn't list a job), independence (checkpoint alone; versions [2]
# only; the daemon dead; the gateway disabled mid-session). Run by
# tests/run_tests.py after tests/recovery_tests.py, in the same namespace
# (check(), hook(), ctx(), denied(), mcp(), jbase(), jrecs(), task_of(),
# section(), scan_for(), fake_claude(), TMP, HENV ...). A fake gateway
# (tests/fake_gateway.py) serves CONTRACT-v1 on a Unix socket in a thread of
# this process; nothing here reaches the real ~/.config/gateway (the suite's
# CHECKPOINT_GATEWAY_CONTRACT points into TMP). Canaries are generated here
# and never printed.
# ruff: noqa: F821  (names come from run_tests.py, journal_tests.py, recovery_tests.py)
import sys as _sys
import threading  # noqa: E402

_sys.path.insert(0, str(ROOT / "tests"))
import fake_gateway as fg  # noqa: E402

from checkpoint import gateway  # noqa: E402

GT = TMP / "gateway-tests"
GT.mkdir()
GWC_PATH = Path(os.environ["CHECKPOINT_GATEWAY_CONTRACT"])
check("hermetic: the gateway's discovery file is the suite's temp one, never the real ~/.config/gateway",
      str(GWC_PATH).startswith(str(TMP)) and gateway.contract_path() == str(GWC_PATH))
_GW_TIMEOUT_BEFORE = os.environ.get("CHECKPOINT_GATEWAY_TIMEOUT")
os.environ["CHECKPOINT_GATEWAY_TIMEOUT"] = "0.4"   # starts give up after 1 s, pauses after 2 s
FAKES = []


def gwfake(**kw):
    g = fg.FakeGateway(GWC_PATH, **kw)
    FAKES.append(g)
    return g


def both(env=None):
    """(Python's discovery, the sh doctor's verdict word and line)."""
    if env is not None:
        old = {k: os.environ.get(k) for k in env}
        os.environ.update({k: v for k, v in env.items()})
    try:
        g = gateway.discover()
    finally:
        if env is not None:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    d = subprocess.run(["sh", str(ROOT / "hooks" / "env.sh"), "doctor"], capture_output=True, text=True, timeout=30,
                       env=dict(os.environ, **(env or {})))
    line = next((ln for ln in d.stdout.splitlines() if ln.startswith("gateway contract: ")), "")
    rest = line[len("gateway contract: "):]
    return g, rest.split(":", 1)[0], rest


def dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


GW_CL = {}   # session -> its fake Claude process (kill it: the session has ended)


def gw_session(tag):
    """A session with a task (SessionStart through the real hook), its env for
    hooks and for the MCP server. The servers mcall starts here don't follow
    the gateway's events (CHECKPOINT_GATEWAY_FOLLOW=0): a follow thread
    would sync, and settle the pause state, on its own schedule, racing the
    checks; the follow thread has its own section below."""
    sid = f"sess-gw-{tag}"
    proj = GT / f"proj {tag}"
    proj.mkdir()
    cl = fake_claude()
    GW_CL[sid] = cl
    eh = {"CLAUDE_PID": str(cl.pid), **HENV}
    tp = GT / f"{tag}.jsonl"
    tp.write_text("")
    hook("SessionStart", jbase(sid, proj, tp, source="startup"), eh)
    capture.ingest(sid)
    em = dict(os.environ, CLAUDE_CODE_SESSION_ID=sid, CLAUDE_PID=str(cl.pid), CHECKPOINT_GATEWAY_FOLLOW="0")
    return sid, proj, tp, eh, em


def mcall(name, args, env, tid=None):
    params = {"name": name, "arguments": args}
    if tid:
        params["_meta"] = {"claudecode/toolUseId": tid}
    r = mcp([{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}], env)[1]
    return json.loads(r["result"]["content"][0]["text"]), r["result"]["isError"]


def reg_rec(name):
    return next((j for j in jobs._registry()["jobs"].values() if j.get("name") == name), None)


def gw_recs(task, kind="gateway.event"):
    return [r for r in jrecs(task) if r["kind"] == kind]


# --- discovery ---------------------------------------------------------------------------
print("gateway: discovery (CONTRACT-v1), in Python and in sh (the mod's /check-doctor):")
GWC_PATH.unlink(missing_ok=True)
g_, w_, line_ = both()
check("no contract file: not available (absent), with the reason, in Python and in sh",
      g_.status == "absent" and w_ == "absent" and "isn't running or isn't installed" in g_.reason)
g_, w_, line_ = both({"CHECKPOINT_GATEWAY_CONTRACT": ""})
check("CHECKPOINT_GATEWAY_CONTRACT set but empty: turned off in both (no path at all, not the real home)",
      g_.status == "off" and w_ == "off")
D1 = gwfake().start()
g_, w_, line_ = both()
check("a live daemon with contract gateway.v1, versions [1], its socket: available in both, with version and epoch",
      g_.ok and w_ == "available" and "gateway 0.8.0" in line_ and "epoch 1" in line_ and g_.epoch == 1
      and g_.has("jobs"))
D1.write_contract(pid=dead_pid())
g_, w_, line_ = both()
check("a dead pid: not available (dead: the contract file is stale), in both", g_.status == "dead" and w_ == "dead"
      and "stale" in g_.reason)
D1.write_contract(contract="gateway.v9")
g_, w_, line_ = both()
check("an unknown contract id: not available (unknown_contract), in both", g_.status == "unknown_contract"
      and w_ == "unknown_contract" and "gateway.v9" in g_.reason)
D1.write_contract(versions=[2])
g_, w_, line_ = both()
check("versions [2] only: 'gateway contract mismatch (has 2, needs 1)' in both, and checkpoint works alone",
      g_.status == "mismatch" and w_ == "mismatch" and "gateway contract mismatch (has 2, needs 1)" in g_.reason
      and "gateway contract mismatch (has 2, needs 1)" in line_)
D1.write_contract(versions=[1, 2])
g_, w_, line_ = both()
check("versions [1, 2]: available (1 is in the list)", g_.ok and w_ == "available")
os.chmod(GWC_PATH, 0o620)
g_, w_, line_ = both()
check("a group-writable contract file: not used (unsafe), in both", g_.status == "unsafe" and w_ == "unsafe")
os.chmod(GWC_PATH, 0o600)
_real = GWC_PATH.with_name("elsewhere.json")
os.replace(GWC_PATH, _real)
GWC_PATH.symlink_to(_real)
g_, w_, line_ = both()
check("a symlinked contract file: not used (unsafe), in both", g_.status == "unsafe" and w_ == "unsafe")
GWC_PATH.unlink()
os.replace(_real, GWC_PATH)
D1.write_contract(socket=str(D1.dir / "missing.sock"))
g_, w_, line_ = both()
check("its socket missing: not available (dead), in both", g_.status == "dead" and w_ == "dead")
D1.write_contract()
check("...and back: available", gateway.discover().ok)

# --- calls ---------------------------------------------------------------------------------
print("gateway: calls over the Unix socket (HTTP/1.1, JSON, contract 1):")
G = gateway.discover()
cap = gateway.capabilities(G)
check("capabilities: contract_id gateway.v1 and the features", cap.get("contract_id") == "gateway.v1"
      and "jobs" in cap.get("features"))
snap = gateway.snapshot(G)
check("snapshot: seq, epoch, hosts", isinstance(snap.get("seq"), int) and snap.get("epoch") == 1
      and snap["hosts"][0]["name"] == "localhost")
D1.raw_answer["GET /v1/capabilities"] = {"ok": True, "contract_id": "gateway.v1"}   # no "contract": 1
try:
    gateway.capabilities(G)
    _kind = ""
except gateway.CallError as e:
    _kind = e.kind
check("an answer without \"contract\": 1 is a protocol error, not taken as v1", _kind == "protocol")
D1.raw_answer.clear()
D1.refuse["GET /v1/snapshot"] = (403, "plane")
try:
    gateway.snapshot(G)
    _err = None
except gateway.CallError as e:
    _err = e
check("an error answer: refused, with its status and code", _err is not None and _err.kind == "refused"
      and _err.status == 403 and _err.code == "plane")
D1.refuse.clear()
_t0 = time.time()
D1.hang["GET /v1/snapshot"] = 1
D1.hang_seconds = 3.0
try:
    gateway.snapshot(G)
    _kind = ""
except gateway.CallError as e:
    _kind = e.kind
check("a daemon that doesn't answer: the call gives up quickly (CHECKPOINT_GATEWAY_TIMEOUT), not 'done'",
      _kind in ("unreachable", "ambiguous") and time.time() - _t0 < 2.0)
D1.hang_seconds = 1.5
time.sleep(1.6)
try:
    gateway.job_get(G, "../../v1/pause")
    _kind = ""
except gateway.CallError as e:
    _kind = e.kind
check("a job id that isn't j_<ulid> never reaches a path", _kind == "refused"
      and not [r for r in D1.calls() if ".." in r["path"]])

# --- jobs: one supervisor each ----------------------------------------------------------
print("gateway: job_start delegates to the gateway's job routes; one supervisor per job:")
SJ, PJ, TPJ, EHJ, EMJ = gw_session("jobs")
TJ = task_of(SJ)
out, err = mcall("job_start", {"name": "build", "command": "make build", "cwd": str(PJ), "redo_hint": "rerun",
                               "on_pause": "interrupt"}, EMJ, tid="toolu_gwjob_1")
post = D1.calls("POST", "/v1/jobs")
check("job_start with the gateway available: POST /v1/jobs with the session, task_ref = the journal task, "
      "idempotency_key ck:<the call's tool_use_id>, on_pause, cwd, command",
      not err and len(post) == 1 and post[0]["body"]["session"] == SJ and post[0]["body"]["task_ref"] == TJ
      and post[0]["body"]["idempotency_key"] == "ck:toolu_gwjob_1" and post[0]["body"]["on_pause"] == "interrupt"
      and post[0]["body"]["cwd"] == str(PJ) and post[0]["body"]["command"] == "make build")
RB = reg_rec("build")
GJB = (RB or {}).get("gateway_job")
check("...the registry says owner gateway with the gateway's job id; no own-runner process or folder for it",
      RB is not None and RB["owner"] == "gateway" and gateway.JOB_RE.fullmatch(GJB or "") is not None
      and "pid" not in RB and not state.p("jobs", RB["id"]).exists())
check("...the tool's answer names the supervisor and the gateway's job id",
      out.get("supervisor") == "gateway" and out["job"]["gateway_job"] == GJB and out["job"]["supervisor"] == "gateway")
JS1 = [r for r in jrecs(TJ) if r["kind"] == "job.started"]
check("...the journal records job.started: supervisor gateway, the gateway job id, the idempotency key, on_pause "
      "(op = the call's tool_use_id)",
      len(JS1) == 1 and JS1[0]["op"] == "toolu_gwjob_1" and JS1[0]["data"]["gateway_job"] == GJB
      and JS1[0]["data"]["supervisor"] == "gateway" and JS1[0]["data"]["idempotency_key"] == "ck:toolu_gwjob_1"
      and JS1[0]["data"]["on_pause"] == "interrupt")
out2, _ = mcall("job_start", {"name": "build", "command": "make build", "cwd": str(PJ), "redo_hint": "rerun"},
                EMJ, tid="toolu_gwjob_1")
check("the same call again (same tool_use_id, so the same idempotency key): the original job, one record",
      out2.get("ok") and out2["job"]["gateway_job"] == GJB
      and sum(1 for j in jobs._registry()["jobs"].values() if j.get("gateway_job") == GJB) == 1
      and len(D1.jobs) == 1)
pol, _ = mcall("task_policy", {"on_pause": "suspend", "on_cancel": "detach"}, EMJ)
mcall("job_start", {"name": "train", "command": "python train.py", "cwd": str(PJ), "redo_hint": "resumes"},
      EMJ, tid="toolu_gwjob_2")
check("task_policy sets the task's defaults (journal task.policy); a job_start without on_pause uses them",
      pol.get("ok") and D1.calls("POST", "/v1/jobs")[-1]["body"]["on_pause"] == "suspend"
      and reg_rec("train")["on_cancel"] == "detach"
      and any(r["kind"] == "task.policy" and r["data"]["on_pause"] == "suspend" for r in jrecs(TJ)))
mcall("job_start", {"name": "serve", "command": "npm start", "cwd": str(PJ), "redo_hint": "rerun",
                    "on_pause": "continue", "on_cancel": "interrupt"}, EMJ, tid="toolu_gwjob_3")
check("...a job_start that names its own on_pause / on_cancel overrides the task's policy",
      D1.calls("POST", "/v1/jobs")[-1]["body"]["on_pause"] == "continue" and reg_rec("serve")["on_cancel"] == "interrupt")
# A start whose answer is lost: retried once with the same key, never handed to the own runner.
D1.hang["POST /v1/jobs"] = 1
before_jobs = len(D1.jobs)
out3, err3 = mcall("job_start", {"name": "slowstart", "command": "make slow", "cwd": str(PJ), "redo_hint": "r"},
                   EMJ, tid="toolu_gwjob_4")
keys = [r["body"]["idempotency_key"] for r in D1.calls("POST", "/v1/jobs") if r["body"].get("command") == "make slow"]
check("a start answered after the client gave up: retried once with the same key, which returns that job: one job",
      out3.get("ok") and keys == ["ck:toolu_gwjob_4", "ck:toolu_gwjob_4"] and len(D1.jobs) == before_jobs + 1
      and sum(1 for j in jobs._registry()["jobs"].values() if j.get("name") == "slowstart") == 1)
D1.hang["POST /v1/jobs"] = 2
out4, err4 = mcall("job_start", {"name": "lost", "command": "make lost", "cwd": str(PJ), "redo_hint": "r"},
                   EMJ, tid="toolu_gwjob_5")
time.sleep(1.6)
check("both answers lost: outcome unknown, said so with the key; NOT started by checkpoint's own runner meanwhile",
      err4 and out4.get("outcome") == "unknown" and "NOT started anywhere else" in out4.get("reason", "")
      and reg_rec("lost") is None and not any(j.get("name") == "lost" for j in jobs._registry()["jobs"].values()))
check("...and the journal says its start's outcome is unknown (job.started, ok false, outcome unknown)",
      any(r["kind"] == "job.started" and r["op"] == "toolu_gwjob_5" and r["data"].get("outcome") == "unknown"
          for r in jrecs(TJ)))


def _after_first_start(cmd, then):
    """Once the gateway has the first POST /v1/jobs for `cmd` (applied; its
    answer hangs past the client's timeout), run `then` before the retry."""
    def run():
        end = time.time() + 20
        while time.time() < end and not [r for r in D1.calls("POST", "/v1/jobs") if r["body"].get("command") == cmd]:
            time.sleep(0.01)
        time.sleep(0.15)
        then()
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


# The first try reached the gateway (applied, its answer lost); then the daemon
# goes away before the retry: the retry can't connect. That is no proof the
# job didn't start: never the own runner (the command would run twice).
D1.hang["POST /v1/jobs"] = 1
_th = _after_first_start("make dup", lambda: D1.stop(remove_contract=False))
out7, err7 = mcall("job_start", {"name": "dupstart", "command": "make dup", "cwd": str(PJ), "redo_hint": "r"}, EMJ,
                   tid="toolu_gwjob_8")
_th.join(10)
check("a start whose first try reached the gateway, and whose retry finds the daemon gone: outcome unknown with the "
      "key, NOT handed to checkpoint's own runner (the gateway applied it once; no own-runner record or process)",
      err7 and out7.get("outcome") == "unknown" and out7.get("supervisor") == "gateway"
      and "ck:toolu_gwjob_8" in out7.get("reason", "") and "NOT started anywhere else" in out7.get("reason", "")
      and sum(1 for j in D1.jobs.values() if j["command"] == "make dup") == 1
      and reg_rec("dupstart") is None
      and not [r for r in procs.table().values() if "make dup" in r["cmd"]])
check("...the journal: job.started ok false, outcome unknown, with the idempotency key",
      any(r["kind"] == "job.started" and r["op"] == "toolu_gwjob_8" and r["data"].get("outcome") == "unknown"
          and r["data"].get("idempotency_key") == "ck:toolu_gwjob_8" and r["data"].get("ok") is False
          for r in jrecs(TJ)))
D1.start()
time.sleep(0.6)          # the hung answer's thread ends (hang_seconds 1.5)
# The same, but the retry is refused as not served (403): the first try may
# still have started it, so no fallback either.
D1.hang["POST /v1/jobs"] = 1
_th = _after_first_start("make dup2", lambda: D1.refuse.__setitem__("POST /v1/jobs", (403, "not_registered")))
out8, err8 = mcall("job_start", {"name": "dupstart2", "command": "make dup2", "cwd": str(PJ), "redo_hint": "r"}, EMJ,
                   tid="toolu_gwjob_9")
_th.join(10)
D1.refuse.clear()
check("...and whose retry is refused as not served (403): outcome unknown too, never a fallback to the own runner",
      err8 and out8.get("outcome") == "unknown" and "403" in out8.get("reason", "")
      and sum(1 for j in D1.jobs.values() if j["command"] == "make dup2") == 1 and reg_rec("dupstart2") is None)
time.sleep(0.6)
D1.refuse["POST /v1/jobs"] = (403, "not_registered")
out5, _ = mcall("job_start", {"name": "fallback", "command": "true", "cwd": str(PJ), "redo_hint": "r"}, EMJ,
                tid="toolu_gwjob_6")
RF = reg_rec("fallback")
check("the gateway can't serve this session (403): checkpoint's own runner runs it, owner checkpoint, and says why",
      out5.get("ok") and out5.get("supervisor") == "checkpoint" and RF and RF["owner"] == "checkpoint"
      and "403" in RF.get("why_own", "") and RF.get("pid"))
D1.refuse["POST /v1/jobs"] = (400, "bad_command")
out6, err6 = mcall("job_start", {"name": "badone", "command": "x", "cwd": str(PJ), "redo_hint": "r"}, EMJ,
                   tid="toolu_gwjob_7")
check("a request the gateway refuses as bad (400): an error, and nothing started anywhere",
      err6 and "refused" in out6.get("reason", "") and reg_rec("badone") is None)
D1.refuse.clear()
# A refusal is the gateway's decision (0.7.2; before, any 403 ran the job in
# the own runner, around the gateway's policy). Only the codes that say it
# doesn't serve this session at all (gateway.NOT_SERVED: plane,
# not_registered, peer, uid; no_route for 404/405, as the gateway's daemon
# sends them) hand a job to the own runner; any other refusal (403 policy,
# 403 session, 409 paused, a 404 that isn't no_route, a code checkpoint
# doesn't know, none at all) is reported as refused by the gateway and runs
# nowhere. Each job here would leave a file if anything ran it.
_POLICY_MSG = "gateway: running a command on localhost is denied by [policy] exec = deny (gateway.toml line 3)"
_POLICY_RULE = '[policy] exec = "deny" (gateway.toml line 3)'


def _start_refused(tag, refusal):
    """job_start while POST /v1/jobs answers `refusal`: (answer, isError, the
    file the job would make, how many POSTs it sent, its registry record)."""
    mark = GT / f"ran-{tag}"
    D1.refuse["POST /v1/jobs"] = refusal
    n0 = len(D1.calls("POST", "/v1/jobs"))
    try:
        out, err = mcall("job_start", {"name": f"ref-{tag}", "command": f"touch {shlex.quote(str(mark))}",
                                       "cwd": str(PJ), "redo_hint": "r"}, EMJ, tid=f"toolu_ref_{tag}")
    finally:
        D1.refuse.clear()
    return out, err, mark, len(D1.calls("POST", "/v1/jobs")) - n0, reg_rec(f"ref-{tag}")


def _never_ran(mark):
    time.sleep(1.0)          # the own runner would have touched it by now
    return not mark.exists()


_o, _e, _m, _n, _r = _start_refused("policy", (403, "policy", {"message": _POLICY_MSG, "rule": _POLICY_RULE,
                                                                "decision": "deny"}))
check("a start the gateway refuses by policy (403 policy): refused by the gateway with its reason and rule, NOT run "
      "by checkpoint's own runner (no record, nothing ran), and not retried",
      _e and _o.get("ok") is False and _o.get("outcome") == "refused" and _o.get("supervisor") == "gateway"
      and _o.get("code") == "policy" and _o.get("rule") == _POLICY_RULE
      and _o.get("reason", "").startswith("refused by the gateway: " + _POLICY_MSG)
      and "HTTP 403, policy" in _o["reason"] and "NOT started" in _o["reason"]
      and _r is None and _n == 1 and _never_ran(_m))
_jr = [r for r in jrecs(TJ) if r["kind"] == "job.started" and r["op"] == "toolu_ref_policy"]
check("...the journal records it as refused (job.started: ok false, outcome refused, by the gateway, its code, "
      "rule and reason)",
      len(_jr) == 1 and _jr[0]["data"].get("ok") is False and _jr[0]["data"].get("outcome") == "refused"
      and _jr[0]["data"].get("refused_by") == "gateway" and _jr[0]["data"].get("code") == "policy"
      and _jr[0]["data"].get("status") == 403 and _jr[0]["data"].get("rule") == _POLICY_RULE
      and "denied by" in _jr[0]["data"].get("reason", "") and _jr[0]["data"].get("supervisor") == "gateway")
_sm = summary.build(TJ, limit=100000).text
check("...and the task's summary says it was refused by the gateway and started nowhere",
      "REFUSED by the gateway (policy), not started anywhere" in _sm and "ref-policy" in _sm)
_o, _e, _m, _n, _r = _start_refused("unknown", (403, "forbidden_by_site"))
check("a 403 with a refusal code checkpoint doesn't know: refused, never a fallback to the own runner",
      _e and _o.get("outcome") == "refused" and _o.get("code") == "forbidden_by_site"
      and "refused by the gateway" in _o.get("reason", "") and _r is None and _n == 1 and _never_ran(_m))
_o, _e, _m, _n, _r = _start_refused("nocode", (403, ""))
check("a 403 without any refusal code: refused, never a fallback",
      _e and _o.get("outcome") == "refused" and "no refusal code" in _o.get("reason", "") and _r is None
      and _never_ran(_m))
_o, _e, _m, _n, _r = _start_refused("session", (403, "session"))
check("a 403 session (the gateway serves the caller, under another session): refused, never a fallback",
      _e and _o.get("outcome") == "refused" and _o.get("code") == "session" and _r is None and _never_ran(_m))
_o, _e, _m, _n, _r = _start_refused("paused", (409, "paused"))
check("a 409 paused (the gateway holds this session's new jobs): refused, never a fallback",
      _e and _o.get("outcome") == "refused" and _o.get("code") == "paused" and _r is None and _never_ran(_m))
_o, _e, _m, _n, _r = _start_refused("nohost", (404, "no_host"))
check("a 404 that isn't no_route (no_host): refused, never a fallback", _e and _o.get("outcome") == "refused"
      and _r is None and _never_ran(_m))
_o, _e, _m, _n, _r = _start_refused("plane", (403, "plane"))
check("a 403 plane (no Claude Code session the gateway knows above the caller): not served, so checkpoint's own "
      "runner runs it and says why", _o.get("ok") and _o.get("supervisor") == "checkpoint" and _r is not None
      and _r["owner"] == "checkpoint" and "doesn't serve this session" in _r.get("why_own", "")
      and "HTTP 403, plane" in _r.get("why_own", "") and wait_for(lambda: _m.exists(), 10))
_o, _e, _m, _n, _r = _start_refused("noroute", (404, "no_route"))
check("a 404 no_route (a daemon without the route): not served, the own runner runs it",
      _o.get("ok") and _o.get("supervisor") == "checkpoint" and _r is not None and wait_for(lambda: _m.exists(), 10))
check("gateway.NOT_SERVED holds exactly the gateway daemon's not-served refusals (planes.py, server.py)",
      gateway.NOT_SERVED == {"plane": (403,), "not_registered": (403,), "peer": (403,), "uid": (403,),
                             "no_route": (404, 405)})
D1.refuse.clear()
# /check:status asks whether the daemon serves this session, not only whether
# its discovery file is there (acceptance: the gateway plugin disabled, its
# daemon still alive from another session, checkpoint said "available: new
# jobs run there" while its job ran in the own runner).
_sl = core.gateway_status_line()
check("the gateway status line: a daemon that answers this caller is available, new jobs run there",
      _sl.startswith("Gateway: available (") and "new jobs run there" in _sl)
D1.refuse["GET /v1/capabilities"] = (403, "plane")
_sl = core.gateway_status_line()
_doc = core.gateway_doctor()
D1.refuse.clear()
check("...one that refuses this caller (403: not a session it knows) is NOT called available: it doesn't serve this "
      "session, new jobs run in checkpoint's own runner (its limits said)",
      "doesn't serve this session" in _sl and "HTTP 403, plane" in _sl and "own detached runner" in _sl
      and "new jobs run there" not in _sl and "available" not in _sl)
check("...and /check:doctor's gateway lines show the refusal", any("403" in x for x in _doc))
D1.refuse["GET /v1/capabilities"] = (403, "policy")
_sl = core.gateway_status_line()
D1.refuse.clear()
check("...one refusing it with another code (403 policy): not called 'doesn't serve this session' (its new jobs "
      "wouldn't run in the own runner); a job it refuses runs nowhere",
      "doesn't serve this session" not in _sl and "New jobs from here run in" not in _sl
      and "refuses runs nowhere" in _sl and "HTTP 403, policy" in _sl)
_hs = D1.hang_seconds
D1.hang["GET /v1/capabilities"] = 1
D1.hang_seconds = 1.5
_sl = core.gateway_status_line()
time.sleep(1.6)
D1.hang_seconds = _hs
check("...one that doesn't answer in time: found, didn't answer; a job is tried there first, the own runner only "
      "when the gateway certainly didn't take it", "didn't answer" in _sl and "tried there first" in _sl
      and "new jobs run there" not in _sl)
st_, _ = mcall("job_status", {"name": "build"}, EMJ)
check("job_status of a gateway job asks the gateway (GET /v1/jobs/<id>): state, supervisor, on_pause",
      st_["job"]["state"] == "running" and st_["job"]["supervisor"] == "gateway" and st_["job"]["on_pause"] == "interrupt"
      and D1.calls("GET", f"/v1/jobs/{GJB}"))
D1.jobs[GJB]["tail"] = "one\ntwo\nthree\nfour\n"
tl, _ = mcall("job_tail", {"name": "build", "lines": 2}, EMJ)
check("job_tail of a gateway job: the gateway's (scrubbed) tail, cut to the lines asked",
      tl.get("ok") and tl["tail"].strip().splitlines() == ["three", "four"] and tl.get("supervisor") == "gateway")
D1.set_job(GJB, state="exited", exit=0)
st_, _ = mcall("job_status", {"name": "build"}, EMJ)
check("...an exit the gateway reports: exited 0", st_["job"]["state"] == "exited" and st_["job"]["exit_code"] == 0)
sp_, sp_err = mcall("job_stop", {"name": "serve", "reason": "done with it"}, EMJ)
check("job_stop of a gateway job: SIGTERM through the contract, stopped once the gateway says it ended",
      sp_.get("stopped") and any(r["body"].get("signal") == "TERM" and r["path"].startswith("/v1/jobs/" + reg_rec("serve")["gateway_job"])
                                 for r in D1.calls("POST", "/v1/jobs/"))
      and reg_rec("serve")["state"] == "canceled" and reg_rec("serve")["stop_reason"] == "done with it")
GJT = reg_rec("train")["gateway_job"]
D1.ignore_term.add(GJT)
_t0 = time.time()
r_ = jobs.stop("train", reason="test", grace=0.5)
check("a gateway job that ignores SIGTERM: SIGKILL after the grace period, then stopped",
      r_.get("stopped") and "SIGKILL" in r_.get("how", "") and
      [x["body"]["signal"] for x in D1.calls("POST", f"/v1/jobs/{GJT}/signal")] == ["TERM", "KILL"])
# A stop the gateway refuses is its decision too: never a signal of
# checkpoint's own (a gateway job has no process group in its records).
GJS = reg_rec("slowstart")["gateway_job"]
D1.refuse["POST /v1/jobs/{id}/signal"] = (403, "other_session")
try:
    r_ = jobs.stop("slowstart", reason="test", grace=0.3)
finally:
    D1.refuse.clear()
check("a stop the gateway refuses (403 other_session): stop_failed, refused by the gateway with its reason; no KILL "
      "after it, nothing signalled by checkpoint itself (no pid in the record), the job runs on there",
      r_.get("ok") is False and r_.get("outcome") == "stop_failed" and r_.get("refused_by") == "gateway"
      and r_.get("code") == "other_session" and r_.get("reason", "").startswith("refused by the gateway")
      and [x["body"]["signal"] for x in D1.calls("POST", f"/v1/jobs/{GJS}/signal")] == ["TERM"]
      and "pid" not in reg_rec("slowstart") and "pgid" not in reg_rec("slowstart")
      and D1.jobs[GJS]["state"] == "running")

# The own runner and its old records stay readable and stoppable.
LEG = "legacy-20261019-090000"
LEGD = state.p("jobs", LEG)
LEGD.mkdir(parents=True)
(LEGD / "exit_code").write_bytes(b"")
_lp = subprocess.Popen(["/bin/sh", "-c", '/bin/sh -c "$1"; rc=$?; echo $rc > "$2"; exit $rc', f"checkpoint-job-{LEG}",
                        "sleep 300", str(LEGD / "exit_code")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       start_new_session=True)
CHILDREN.append(_lp)
time.sleep(0.3)
with state.lock():
    _reg = jobs._registry()
    _reg["jobs"][LEG] = {"id": LEG, "name": "legacy", "command": "sleep 300", "cwd": str(PJ), "pid": _lp.pid,
                         "pgid": _lp.pid, "log": str(LEGD / "output.log"), "exit_file": str(LEGD / "exit_code"),
                         "started": state.now_iso(), "started_ts": time.time() - 0.3, "session_id": "sess-old",
                         "task": "", "redo_hint": "", "state": "running"}
    jobs._save(_reg)
_lr = jobs.refresh_all()[LEG]
check("a 0.1-0.5 registry record (no owner): the own runner's, still running by the process table, never asked of "
      "the gateway", jobs.owner(_lr) == "checkpoint" and _lr["state"] == "running" and jobs.is_live(_lr)
      and not D1.calls("GET", "/v1/jobs/" + LEG))
_stop = core.stop_job("", [LEG])
check("...and /check:stop still stops it (its process group), the gateway available or not",
      "stopped" in _stop and wait_for(lambda: _lp.poll() is not None, 10))
check("checkpoint never runs a gateway job's command itself: no wrapper process names a gateway job's record",
      not [r for r in procs.table().values() if any(f"checkpoint-job-{j['id']}" in r["cmd"]
                                                     for j in jobs._registry()["jobs"].values() if jobs.owner(j) == "gateway")])

# --- pause, resume, cancel through the gateway ------------------------------------------
print("gateway: /check:pause, resume and cancel through CONTRACT-v1 (/v1/pause, /v1/resume, signals):")
SP, PP, TPP, EHP, EMP = gw_session("pause")
TP_ = task_of(SP)
for nm, pol_ in (("intr", "interrupt"), ("cont", "continue"), ("susp", "suspend")):
    mcall("job_start", {"name": nm, "command": f"run {nm}", "cwd": str(PP), "redo_hint": "r", "on_pause": pol_}, EMP,
          tid=f"toolu_p_{nm}")
SO, PO, TPO, EHO, EMO = gw_session("other")
mcall("job_start", {"name": "otherjob", "command": "run other", "cwd": str(PO), "redo_hint": "r",
                    "on_pause": "interrupt"}, EMO, tid="toolu_o_1")
GI, GC, GS = (reg_rec(n)["gateway_job"] for n in ("intr", "cont", "susp"))
GO = reg_rec("otherjob")["gateway_job"]
out = hook("UserPromptSubmit", jbase(SP, PP, TPP, prompt="/check:pause"), EHP)
pz = D1.calls("POST", "/v1/pause")
check("/check:pause: POST /v1/pause with this session and its task (task_ref)",
      len(pz) == 1 and pz[0]["body"]["session"] == SP and pz[0]["body"]["task_ref"] == TP_)
txt = ctx(out)
check("...the result names each job's policy and what actually happened (interrupt exited, continue still running, "
      "suspend suspended)",
      "Gateway jobs of this task:" in txt and "intr" in txt and "interrupted: exited (signal INT)" in txt
      and "still running (on_pause continue)" in txt and "suspended (SIGSTOP)" in txt)
check("...scoped: another session's job with on_pause interrupt runs on", D1.jobs[GO]["state"] == "running"
      and GO not in txt)
check("...the journal records gateway.pause with the outcomes; the session knows the pause reached the gateway",
      any(r["kind"] == "gateway.pause" and r["data"]["ok"] and len(r["data"]["outcomes"]) == 3 for r in jrecs(TP_))
      and (state.load_session(SP).get("gateway_paused") or {}).get("delivered"))
check("...the registry shows what the policies did (intr exited, susp suspended)",
      reg_rec("intr")["state"] == "exited" and reg_rec("susp")["state"] == "suspended")
hook("UserPromptSubmit", jbase(SP, PP, TPP, prompt="/check:pause"), EHP)
check("pausing again (already paused): no second /v1/pause (a second interrupt would signal the jobs again)",
      len(D1.calls("POST", "/v1/pause")) == 1)
for tool, want_denied in (("mcp__plugin_gate_gw__terminal_read", False), ("mcp__plugin_gate_gw__job_status", False),
                          ("mcp__plugin_gate_gw__job_tail", False), ("mcp__plugin_gate_gw__terminal_list", False),
                          ("mcp__plugin_gate_gw__job_list", False), ("mcp__plugin_gate_gw__terminal_send", True),
                          ("mcp__plugin_gate_gw__job_start", True), ("mcp__plugin_gate_gw__job_signal", True)):
    o_ = hook("PreToolUse", jbase(SP, PP, TPP, tool_name=tool, tool_use_id=f"pt-{tool[-12:]}", tool_input={"id": "x"}),
              EHP)
    check(f"while paused, the gate {'holds' if want_denied else 'lets through'} the gateway's {tool.rsplit('__', 1)[1]}"
          + (" (inspection never waits behind a pause)" if not want_denied else ""),
          denied(o_) == want_denied)
out = hook("UserPromptSubmit", jbase(SP, PP, TPP, prompt="carry on with the build"), EHP)
rz = D1.calls("POST", "/v1/resume")
txt = ctx(out)
check("a typed message resumes: POST /v1/resume (session, task); the resume context says what it did "
      "(the suspended job continued)",
      len(rz) == 1 and rz[0]["body"]["session"] == SP and rz[0]["body"]["task_ref"] == TP_
      and "Gateway jobs on resume:" in txt and "resumed (SIGCONT)" in txt and not state.is_paused(SP)
      and not state.load_session(SP).get("gateway_paused") and D1.jobs[GS]["state"] == "running")
check("...the journal records gateway.resume", any(r["kind"] == "gateway.resume" and r["data"]["ok"] for r in jrecs(TP_)))
out = hook("UserPromptSubmit", jbase(SP, PP, TPP, prompt="/check:cancel"), EHP)
txt = ctx(out)
check("/check:cancel: each live gateway job follows its policy through the gateway (interrupt: SIGTERM), history kept",
      "stopped through the gateway" in txt and D1.jobs[GC]["state"] == "canceled" and D1.jobs[GS]["state"] == "canceled"
      and any(r["kind"] == "task.cancel" and {o["supervisor"] for o in r["data"]["jobs"]} == {"gateway"}
              for r in jrecs(TP_)))
check("...the other session's job is untouched", D1.jobs[GO]["state"] == "running")
# detach: released in checkpoint's records, left running in the gateway
SD, PD, TPD, EHD, EMD = gw_session("detach")
mcall("job_start", {"name": "keepme", "command": "run keep", "cwd": str(PD), "redo_hint": "r", "on_cancel": "detach"},
      EMD, tid="toolu_d_1")
GK = reg_rec("keepme")["gateway_job"]
out = hook("UserPromptSubmit", jbase(SD, PD, TPD, prompt="/check:cancel"), EHD)
check("/check:cancel with a job's on_cancel detach: left running in the gateway, released in checkpoint's records "
      "(and said so)", D1.jobs[GK]["state"] == "running" and reg_rec("keepme").get("detached")
      and "the gateway keeps running it" in ctx(out)
      and not [r for r in D1.calls("POST", f"/v1/jobs/{GK}/signal")])
# The mod's path: /check-pause writes the flag, then gw-pause and sync-pause run side by side.
SM_, PM_, TPM_, EHM_, EMM_ = gw_session("mod")
mcall("job_start", {"name": "modjob", "command": "run mod", "cwd": str(PM_), "redo_hint": "r", "on_pause": "interrupt"},
      EMM_, tid="toolu_m_1")
subprocess.run(["sh", str(ROOT / "hooks" / "env.sh"), "flag", "pause", SM_, "/check-pause"], check=True, timeout=30)
_n0 = len(D1.calls("POST", "/v1/pause"))
_pa = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "gw-pause", SM_], stdout=subprocess.PIPE, text=True)
_pb = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "sync-pause", SM_], stdout=subprocess.PIPE, text=True)
_oa, _ = _pa.communicate(timeout=60)
_ob, _ = _pb.communicate(timeout=60)
check("the mod's /check-pause: gw-pause and sync-pause side by side deliver ONE /v1/pause (the session's gateway "
      "lock), and gw-pause's line (for the ack) names the outcome",
      len(D1.calls("POST", "/v1/pause")) == _n0 + 1 and "interrupted: exited" in json.loads(_oa)["text"])
# Late delivery: a pause made while the gateway was away reaches it at the next paused tool call, once.
SL, PL, TPL, EHL, EML = gw_session("late")
mcall("job_start", {"name": "latejob", "command": "run late", "cwd": str(PL), "redo_hint": "r", "on_pause": "suspend"},
      EML, tid="toolu_l_1")
D1.stop(remove_contract=True)
out = hook("UserPromptSubmit", jbase(SL, PL, TPL, prompt="/check:pause"), EHL)
check("a pause while the gateway is away (no contract file): paused here, no gateway line, nothing claimed there",
      state.is_paused(SL) and "Gateway" not in ctx(out))
D1.start()
_n0 = len(D1.calls("POST", "/v1/pause"))
o1 = hook("PreToolUse", jbase(SL, PL, TPL, tool_name="Bash", tool_use_id="late-1", tool_input={"command": "ls"}), EHL)
o2 = hook("PreToolUse", jbase(SL, PL, TPL, tool_name="Bash", tool_use_id="late-2", tool_input={"command": "ls"}), EHL)
check("...back: the next paused tool call delivers it (the job suspended), once; the calls stay denied",
      denied(o1) and denied(o2) and len(D1.calls("POST", "/v1/pause")) == _n0 + 1
      and D1.calls("POST", "/v1/pause")[-1]["body"]["session"] == SL
      and D1.jobs[reg_rec("latejob")["gateway_job"]]["state"] == "suspended")
D1.stop(remove_contract=False)          # the daemon dies; its contract file stays behind
out = hook("UserPromptSubmit", jbase(SL, PL, TPL, prompt="go on"), EHL)
check("a resume while the gateway's daemon is dead (stale contract file): resumed here, the context says the gateway "
      "wasn't reached, and checkpoint remembers to tell it", not state.is_paused(SL)
      and "not reached to resume" in ctx(out) and state.load_session(SL).get("gateway_paused"))
D1.start()
_r0 = len(D1.calls("POST", "/v1/resume"))
subprocess.run([sys.executable, str(ROOT / "run.py"), "ingest", SL], capture_output=True, timeout=60)
check("...back: checkpoint's next moment (ingest) delivers the missed resume once (the suspended job continues)",
      len(D1.calls("POST", "/v1/resume")) == _r0 + 1 and not state.load_session(SL).get("gateway_paused")
      and D1.jobs[reg_rec("latejob")["gateway_job"]]["state"] == "running")
subprocess.run([sys.executable, str(ROOT / "run.py"), "ingest", SL], capture_output=True, timeout=60)
check("...and not again", len(D1.calls("POST", "/v1/resume")) == _r0 + 1)
# pause --stop-jobs reaches gateway jobs too
out = hook("UserPromptSubmit", jbase(SL, PL, TPL, prompt="/check:pause --stop-jobs"), EHL)
check("/check:pause --stop-jobs stops the session's gateway jobs too (through the contract)",
      "Stopped jobs: latejob" in ctx(out) and D1.jobs[reg_rec("latejob")["gateway_job"]]["state"] == "canceled")
hook("UserPromptSubmit", jbase(SL, PL, TPL, prompt="ok"), EHL)
# --stop-jobs reads the registry's records: every live gateway state counts
# (a job the pause itself suspended, one waiting for a password), and a stop
# the gateway can't confirm says the gateway's reason.
SK, PK, TPK, EHK, EMK = gw_session("stopjobs")
for nm, pol_ in (("sjsusp", "suspend"), ("sjauth", "continue"), ("sjstuck", "continue")):
    mcall("job_start", {"name": nm, "command": f"run {nm}", "cwd": str(PK), "redo_hint": "r", "on_pause": pol_}, EMK,
          tid=f"toolu_sj_{nm}")
GSJS, GSJA, GSJK = (reg_rec(n)["gateway_job"] for n in ("sjsusp", "sjauth", "sjstuck"))
D1.set_job(GSJA, state="waiting_auth")
D1.unkillable.add(GSJK)
txt = ctx(hook("UserPromptSubmit", jbase(SK, PK, TPK, prompt="/check:pause --stop-jobs"), EHK))
check("/check:pause --stop-jobs: a gateway job the pause suspended and one waiting for authentication are live "
      "(the registry's records, not only 'running') and stopped through the contract",
      "Stopped jobs: sjsusp, sjauth." in txt and D1.jobs[GSJS]["state"] == "canceled"
      and D1.jobs[GSJA]["state"] == "canceled" and "No running jobs to stop" not in txt)
check("...one the gateway still reports running after SIGTERM and SIGKILL: 'Could not stop' with the gateway's word "
      "(never the own runner's 'still running after SIGKILL' text for it)",
      "Could not stop: sjstuck (stop_failed: the gateway still reports" in txt
      and "processes of it are still running after SIGKILL" not in txt)
D1.unkillable.discard(GSJK)
hook("UserPromptSubmit", jbase(SK, PK, TPK, prompt="ok"), EHK)
# A pause the gateway never had, then a typed message that resumes: the late
# pause must not reach the gateway first (on_pause interrupt would stop the
# job the person is resuming).
SR, PR, TPR, EHR, EMR = gw_session("staleresume")
mcall("job_start", {"name": "srjob", "command": "run sr", "cwd": str(PR), "redo_hint": "r", "on_pause": "interrupt"},
      EMR, tid="toolu_sr_1")
GSR = reg_rec("srjob")["gateway_job"]
D1.remove_contract()                     # the gateway is away (its daemon restarting, say)
hook("UserPromptSubmit", jbase(SR, PR, TPR, prompt="/check:pause"), EHR)
D1.write_contract()                      # and back
_np, _nr = len(D1.calls("POST", "/v1/pause")), len(D1.calls("POST", "/v1/resume"))
out = hook("UserPromptSubmit", jbase(SR, PR, TPR, prompt="ok, go on with the training"), EHR)
check("a pause made while the gateway was away, the gateway back, then a typed message that resumes: no late "
      "/v1/pause before the resume (the on_pause interrupt job keeps running), and nothing to resume there",
      not state.is_paused(SR) and len(D1.calls("POST", "/v1/pause")) == _np
      and len(D1.calls("POST", "/v1/resume")) == _nr and D1.jobs[GSR]["state"] == "running"
      and "interrupted" not in ctx(out) and "<checkpoint-resume>" in ctx(out))
# A pause the gateway refused (certainly not applied there): the resume sends nothing.
D1.refuse["POST /v1/pause"] = (500, "internal")
hook("UserPromptSubmit", jbase(SR, PR, TPR, prompt="/check:pause"), EHR)
D1.refuse.clear()
_gp = state.load_session(SR).get("gateway_paused") or {}
_np, _nr = len(D1.calls("POST", "/v1/pause")), len(D1.calls("POST", "/v1/resume"))
hook("UserPromptSubmit", jbase(SR, PR, TPR, prompt="go on"), EHR)
check("a pause the gateway refused: the typed resume sends neither a late pause nor a resume, forgets it, and the "
      "journal says nothing was sent", _gp.get("delivered") is False and not _gp.get("uncertain")
      and len(D1.calls("POST", "/v1/pause")) == _np and len(D1.calls("POST", "/v1/resume")) == _nr
      and not state.load_session(SR).get("gateway_paused") and D1.jobs[GSR]["state"] == "running"
      and any(r["kind"] == "gateway.resume" and r["data"].get("sent") is False for r in jrecs(task_of(SR))))
# A pause the gateway refuses by its own decision (403 session): said as
# refused, and its on_pause interrupt job isn't signalled by anyone.
_si0 = D1.sigints.get(GSR, 0)
D1.refuse["POST /v1/pause"] = (403, "session")
try:
    _txt = ctx(hook("UserPromptSubmit", jbase(SR, PR, TPR, prompt="/check:pause"), EHR))
finally:
    D1.refuse.clear()
check("a pause the gateway refuses (403 session): said as refused by the gateway, its on_pause interrupt job not "
      "signalled (no SIGINT, runs on there)", "it refused the pause" in _txt and "HTTP 403, session" in _txt
      and D1.jobs[GSR]["state"] == "running" and D1.sigints.get(GSR, 0) == _si0)
hook("UserPromptSubmit", jbase(SR, PR, TPR, prompt="go on"), EHR)
# The task's jobs that another session started (B picks up A's task): the
# gateway pauses by session, so B's pause alone wouldn't reach them.
SFA, PFA, TPFA, EHFA, EMFA = gw_session("forkA")
TFK = task_of(SFA)
mcall("job_start", {"name": "forkjob", "command": "run fork", "cwd": str(PFA), "redo_hint": "r", "on_pause": "suspend"},
      EMFA, tid="toolu_fk_1")
GFK = reg_rec("forkjob")["gateway_job"]
SFB, PFB, TPFB, EHFB, EMFB = gw_session("forkB")
hook("UserPromptSubmit", jbase(SFB, PFB, TPFB, prompt=f"/check:resume {TFK}"), EHFB)
check("(setup) session B continues A's task (/check:resume <task>)", task_of(SFB) == TFK)
_n0 = len(D1.calls("POST", "/v1/pause"))
txt = ctx(hook("UserPromptSubmit", jbase(SFB, PFB, TPFB, prompt="/check:pause"), EHFB))
check("a pause in B while A (which started the task's gateway job) is still open: A isn't paused in the gateway "
      "(that would hold A's other work there); the job is listed as NOT paused, its state said",
      [c["body"]["session"] for c in D1.calls("POST", "/v1/pause")[_n0:]] == [SFB]
      and D1.jobs[GFK]["state"] == "running" and "NOT paused (started from session" in txt and "forkjob" in txt
      and "still open" in txt)
hook("UserPromptSubmit", jbase(SFB, PFB, TPFB, prompt="go on"), EHFB)
GW_CL[SFA].kill()                        # A's Claude process ends
GW_CL[SFA].wait()
_n0 = len(D1.calls("POST", "/v1/pause"))
txt = ctx(hook("UserPromptSubmit", jbase(SFB, PFB, TPFB, prompt="/check:pause"), EHFB))
_bodies = [(c["body"]["session"], c["body"].get("task_ref")) for c in D1.calls("POST", "/v1/pause")[_n0:]]
check("...once A has ended: B's pause also pauses A's jobs of this task there (/v1/pause with A's session and the "
      "task_ref): on_pause suspend applied, and said",
      _bodies == [(SFB, TFK), (SFA, TFK)] and D1.jobs[GFK]["state"] == "suspended"
      and "(ended), paused there for this task: forkjob" in txt and "suspended (SIGSTOP)" in txt)
check("...the journal's gateway.pause says what happened for session A",
      any(r["kind"] == "gateway.pause" and any(o.get("session") == SFA and o.get("outcome") == "paused"
                                               for o in r["data"].get("others") or []) for r in jrecs(TFK)))
_r0 = len(D1.calls("POST", "/v1/resume"))
out = hook("UserPromptSubmit", jbase(SFB, PFB, TPFB, prompt="carry on"), EHFB)
_rb = [(c["body"]["session"], c["body"].get("task_ref")) for c in D1.calls("POST", "/v1/resume")[_r0:]]
check("...B's resume resumes them too (/v1/resume for B, then for A with the task_ref): the job runs again, said in "
      "the resume context; nothing is left to resume",
      _rb == [(SFB, TFK), (SFA, TFK)] and D1.jobs[GFK]["state"] == "running"
      and not state.load_session(SFB).get("gateway_paused") and "from session" in ctx(out)
      and "resumed (SIGCONT)" in ctx(out))

# --- the journal: snapshots and events ----------------------------------------------------
print("gateway: its events into the journal (sanitized; duplicates, out of order, epochs):")
SE, PE, TPE, EHE, EME = gw_session("events")
TE = task_of(SE)
mcall("job_start", {"name": "evjob", "command": "run ev", "cwd": str(PE), "redo_hint": "r"}, EME, tid="toolu_e_1")
GE = reg_rec("evjob")["gateway_job"]
r1 = gateway.sync(SE)
check("the first sync reads the snapshot: gateway.snapshot with the hosts and this task's jobs (no command text)",
      r1.ok and any(r["data"]["hosts"][0]["name"] == "localhost" and [j["job"] for j in r["data"]["jobs"]] == [GE]
                    and "command" not in json.dumps(r["data"]["jobs"]) for r in gw_recs(TE, "gateway.snapshot")))
TERM_E = D1.open_terminal(SE)
D1.take_over(TERM_E, "user")
D1.set_job(GE, state="waiting_auth")
D1.emit("job.output", job=GE, bytes=100)
D1.emit("job.output", job=GE, bytes=250)
D1.open_terminal(SO)                      # another session's terminal
OPAQUE = _rand(28)
TOKEN = "ghp_" + _rand(36)
D1.inject({"seq": D1.seq + 1, "epoch": D1.epoch, "ts": time.time(), "kind": "job.state", "job": GE, "state": "running",
           "note": OPAQUE, "command": f"deploy --token {TOKEN}"})
r2 = gateway.sync(SE)
evk = [(r["data"]["kind"], r["data"].get("owner") or r["data"].get("state")) for r in gw_recs(TE)]
check("terminal.opened and terminal.owner (the user's takeover) of this session's terminal are recorded",
      ("terminal.opened", None) in [(k, v if k != "terminal.opened" else None) for k, v in evk]
      and ("terminal.owner", "user") in evk)
check("...its job's states are recorded (waiting_auth, running); job.output only once per batch (counts only)",
      ("job.state", "waiting_auth") in evk and sum(1 for k, _v in evk if k == "job.output") == 1)
check("...another session's terminal isn't", sum(1 for k, _v in evk if k == "terminal.opened") == 1)
check("...fields an event kind doesn't allow are dropped (a canary in them is nowhere in checkpoint's state)",
      not scan_for([OPAQUE, TOKEN], Path(os.environ["CHECKPOINT_HOME"]))
      and not any("note" in r["data"] or "command" in r["data"] for r in gw_recs(TE)))
TOKR = "ghp_" + _rand(36)
OPQR = _rand(24)
D1.emit("daemon.stopping", reason=TOKR)
D1.emit("host.state", host=D1.machine_id, name="token:" + OPQR, state="reachable")
gateway.sync(SE)
check("a token-shaped value in a field its event kind allows (daemon.stopping's reason; a host.state name that names "
      "a token): scrubbed before the journal (the allowlist's characters alone let it through), nowhere in "
      "checkpoint's state; the events are kept",
      not scan_for([TOKR, OPQR], Path(os.environ["CHECKPOINT_HOME"]))
      and any(r["data"]["kind"] == "daemon.stopping" and r["data"].get("reason") == "***" for r in gw_recs(TE))
      and any(r["data"]["kind"] == "host.state" and r["data"].get("name") == "token:***" for r in gw_recs(TE)))
_n = len(gw_recs(TE))
dup = dict(D1.events[-1])
D1.inject(dup, dup)
gateway.sync(SE)
check("a duplicate delivery (the same epoch and seq again) adds nothing", len(gw_recs(TE)) == _n)
# Out of order: the owner change (seq n+2) arrives before the terminal's opening (seq n+1).
_s = D1.seq
T2 = fg.ulid("t")
D1.inject({"seq": _s + 2, "epoch": D1.epoch, "kind": "terminal.owner", "terminal": T2, "owner": "user", "by": "ui"},
          {"seq": _s + 1, "epoch": D1.epoch, "kind": "terminal.opened", "terminal": T2, "session": SE, "by": "agent"})
gateway.sync(SE)
new = [r for r in gw_recs(TE) if r["data"].get("terminal") == T2]
check("events out of order are taken in seq order: the opening first, so the takeover of that terminal is kept",
      [r["data"]["kind"] for r in new] == ["terminal.opened", "terminal.owner"]
      and new[0]["seq"] < new[1]["seq"])
# A new epoch: the snapshot is read again; a job it no longer lists is unknown (gone), never done.
mcall("job_start", {"name": "lostjob", "command": "run lost2", "cwd": str(PE), "redo_hint": "r"}, EME, tid="toolu_e_2")
GL = reg_rec("lostjob")["gateway_job"]
gateway.sync(SE)
D1.restart(lose=[GL])
r3 = gateway.sync(SE)
check("a gateway restart (new epoch): gateway.epoch recorded and the snapshot read again", r3.ok and r3.epoch_changed
      and any(r["data"]["to"] == D1.epoch for r in gw_recs(TE, "gateway.epoch")))
check("...a job the new epoch's snapshot doesn't list: unknown and gone in the registry, never exited; a re-adopted "
      "one has the state the gateway reports for it",
      reg_rec("lostjob")["state"] == "unknown" and reg_rec("lostjob").get("gone")
      and reg_rec("evjob")["state"] == D1.jobs[GE]["state"] and D1.jobs[GE]["state"] in fg.LIVE)
LR = reg_rec("lostjob")
check("...that job counts as possibly running (is_live) and never as finished (is_final): unknown is never done",
      jobs.is_live(LR) and not jobs.is_final(LR))
_s0 = len(D1.calls("POST", f"/v1/jobs/{GL}/signal"))
_stop_txt = core.stop_job(SE, ["lostjob"])
_stop_r = jobs.stop("lostjob", reason="t")
check("/check:stop and job_stop of a job lost in a gateway restart: NOT reported stopped (nor 'isn't running'); "
      "'outcome unknown, it may still run' with the reason; no signal sent for it",
      "NOT confirmed stopped" in _stop_txt and "outcome unknown, it may still run" in _stop_txt
      and "no longer lists it" in _stop_txt and "isn't running" not in _stop_txt
      and _stop_r.get("stopped") is False and _stop_r.get("outcome") == "unknown"
      and "it may still run" in _stop_r.get("reason", "") and len(D1.calls("POST", f"/v1/jobs/{GL}/signal")) == _s0)
check("...the journal's job.stop says outcome unknown; /check:stop alone lists it, as outcome unknown",
      any(r["kind"] == "job.stop" and r["data"].get("job") == LR["id"] and r["data"].get("outcome") == "unknown"
          for r in jrecs(TE))
      and re.search(r"lostjob \([^)]*outcome unknown: it may still run\)", core.stop_job(SE, [])) is not None)
D1.set_job(GE, state="exited", exit=0)
gateway.sync(SE)
check("reconnected after the epoch change: new events are recorded under the new epoch",
      any(r["op"] == f"gw:{D1.epoch}:{D1.seq}" or (r["data"]["kind"] == "job.exited" and r["data"]["epoch"] == D1.epoch)
          for r in gw_recs(TE)))
rc_ = recovery.reconcile(TE, SE)
cls = {j["id"]: (j["cls"], j["why"]) for j in rc_.jobs}
check("reconcile: the exited job (exit 0) is confirmed completed by the gateway; the lost one is outcome unknown, "
      "never done", cls[reg_rec("evjob")["id"]][0] == "completed"
      and cls[reg_rec("lostjob")["id"]][0] == "unknown" and "no longer lists it" in cls[reg_rec("lostjob")["id"]][1])
sm_ = summary.build(TE, limit=100000)
jg = section(sm_.text, "Jobs & gateway")
check("the summary's Jobs & gateway section: supervisor and gateway job id, the last observed state, hosts, the "
      "takeover, the restart", "the gateway, job " + GE in jg and "last reported exited (exit 0)" in jg
      and "hosts the gateway listed" in jg and "input taken by the user" in jg and "the gateway restarted" in jg)
# Gateway disabled mid-session: the contract goes away.
mcall("job_start", {"name": "midjob", "command": "run mid", "cwd": str(PE), "redo_hint": "r"}, EME, tid="toolu_e_3")
gateway.sync(SE)
D1.stop(remove_contract=True)
gateway.sync(SE)
gateway.sync(SE)
check("the gateway disabled mid-session (contract gone): gateway.unavailable recorded once",
      len(gw_recs(TE, "gateway.unavailable")) == 1)
rc_ = recovery.reconcile(TE, SE)
cls = {j["id"]: (j["cls"], j["why"]) for j in rc_.jobs}
check("...a running gateway job reconciles as outcome unknown (the gateway isn't available), never done; the "
      "check items say so", cls[reg_rec("midjob")["id"]][0] == "unknown"
      and "isn't available" in cls[reg_rec("midjob")["id"]][1]
      and any("The gateway isn't available" in t for t in rc_.check_first()))
check(".../check:status shows it unknown, and where jobs run now (checkpoint's own runner, its limits)",
      "midjob" in core.status_text(SE) and "unknown" in core.status_text(SE)
      and "Gateway: not available" in core.status_text(SE) and "no per-job pause policy" in core.status_text(SE))
check("...the stored record keeps the last state the gateway reported (running), unknown only in what is shown",
      jobs._registry()["jobs"][reg_rec("midjob")["id"]]["state"] == "running")
r_ = jobs.stop("midjob", reason="t")
check("...stopping it is refused with the reason (not reported stopped)", not r_.get("stopped")
      and "isn't available" in r_.get("reason", ""))
D1.start()
D1.stop(remove_contract=False)
rc_ = recovery.reconcile(TE, SE)
check("contract file present but the daemon dead: the same (unknown, never done)",
      {j["id"]: j["cls"] for j in rc_.jobs}[reg_rec("midjob")["id"]] == "unknown")
D1.start()
D1.write_contract(machine_id=fg.ulid("m"))
rc_ = recovery.reconcile(TE, SE)
check("the gateway on another machine than the task's gateway jobs ran on: a check item says so",
      any("another machine" in t for t in rc_.check_first()))
D1.write_contract()

# A job lost in a gateway restart at /check:cancel, at pruning and at retention.
SU, PU, TPU, EHU, EMU = gw_session("unknowncancel")
TU = task_of(SU)
mcall("job_start", {"name": "lostcx", "command": "run lostcx", "cwd": str(PU), "redo_hint": "r"}, EMU, tid="toolu_u_1")
GLX = reg_rec("lostcx")["gateway_job"]
gateway.sync(SU)
D1.restart(lose=[GLX])
gateway.sync(SU)
txt = ctx(hook("UserPromptSubmit", jbase(SU, PU, TPU, prompt="/check:cancel"), EHU))
check("/check:cancel with a job lost in a gateway restart: listed as NOT confirmed stopped (outcome unknown, it may "
      "still run), never 'Nothing of it was running'; task.cancel records outcome unknown",
      "lostcx" in txt and "NOT confirmed stopped: outcome unknown, it may still run" in txt
      and "Nothing of it was running" not in txt
      and any(r["kind"] == "task.cancel" and [o.get("outcome") for o in r["data"]["jobs"]] == ["unknown"]
              for r in jrecs(TU)))
with state.lock():
    _reg = jobs._registry()
    _reg["jobs"]["old-unknown-1"] = {"id": "old-unknown-1", "name": "old-unknown", "owner": "gateway",
                                     "gateway_job": fg.ulid("j"), "state": "unknown", "gone": True,
                                     "unknown_why": "the gateway restarted and no longer lists it",
                                     "started_ts": 1000.0, "session_id": "sess-old-x", "task_id": "t_old_unknown"}
    _reg["jobs"]["old-exited-1"] = {"id": "old-exited-1", "name": "old-exited", "owner": "gateway",
                                    "gateway_job": fg.ulid("j"), "state": "exited", "exit_code": 0,
                                    "started_ts": 900.0, "ended_ts": 1000.0, "session_id": "sess-old-x",
                                    "task_id": "t_old_exited"}
    jobs._save(_reg)
jobs.prune(30)
_kept = jobs._registry()["jobs"]
check("job pruning keeps a gateway job whose outcome is unknown however old (it may still run); a finished one that "
      "old goes", "old-unknown-1" in _kept and "old-exited-1" not in _kept)
for _t in ("t_old_unknown", "t_old_plain"):
    journal.append(_t, [{"kind": "prompt", "op": f"{_t}-1", "session": "sess-old-x",
                         "data": {"text": "an old task", "user": True}}])
_gone = recovery.prune_tasks(now=time.time() + 400 * 86400, dry_run=True)
check("...and journal retention keeps the task of such a job (until the gateway says what became of it); the same "
      "task without it would go", "t_old_unknown" not in _gone and "t_old_plain" in _gone)
with state.lock():
    _reg = jobs._registry()
    _reg["jobs"].pop("old-unknown-1", None)
    jobs._save(_reg)

# --- independence and compatibility -----------------------------------------------------
print("gateway: checkpoint alone, and with an incompatible gateway:")
D1.stop(remove_contract=True)
SA_, PA_, TPA_, EHA_, EMA_ = gw_session("alone")
oa, _ = mcall("job_start", {"name": "alonejob", "command": "true", "cwd": str(PA_), "redo_hint": "r"}, EMA_,
              tid="toolu_a_1")
check("checkpoint alone (no gateway): job_start runs in its own runner, quietly (no gateway note)",
      oa.get("ok") and oa.get("supervisor") == "checkpoint" and "gateway" not in oa
      and reg_rec("alonejob")["owner"] == "checkpoint")
out = hook("UserPromptSubmit", jbase(SA_, PA_, TPA_, prompt="/check:pause"), EHA_)
check("...pause and resume work with no gateway line and no gateway call",
      state.is_paused(SA_) and "Gateway" not in ctx(out))
hook("UserPromptSubmit", jbase(SA_, PA_, TPA_, prompt="resume"), EHA_)
check("...status names the own runner's limits", "no per-job pause policy" in core.status_text(SA_))
D2 = gwfake(versions=[2]).start()
om, _ = mcall("job_start", {"name": "mismatchjob", "command": "true", "cwd": str(PA_), "redo_hint": "r"}, EMA_,
              tid="toolu_a_2")
check("versions [2] only: job_start works alone (own runner) and says why the gateway wasn't used",
      om.get("ok") and om.get("supervisor") == "checkpoint" and "mismatch" in om.get("gateway", "")
      and not D2.calls("POST", "/v1/jobs"))
check(".../check:status and /check:doctor report the mismatch", "gateway contract mismatch (has 2, needs 1)"
      in core.status_text(SA_) and "gateway contract mismatch (has 2, needs 1)" in core.doctor_text(SA_))
out = hook("UserPromptSubmit", jbase(SA_, PA_, TPA_, prompt="/check:pause"), EHA_)
check("...pause works alone, and says the gateway wasn't reached (why)", state.is_paused(SA_)
      and "gateway contract mismatch" in ctx(out) and not D2.calls("POST", "/v1/pause"))
hook("UserPromptSubmit", jbase(SA_, PA_, TPA_, prompt="resume"), EHA_)
D2.close()

# --- the MCP server follows the gateway's events -----------------------------------------
print("gateway: the MCP server's thread records the gateway's events as they happen:")
D3 = gwfake().start()
SF_, PF_, TPF_, EHF_, EMF_ = gw_session("follow")
TF_ = task_of(SF_)
srv = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       text=True, env=dict(EMF_, CHECKPOINT_GATEWAY_POLL="0.4", CHECKPOINT_GATEWAY_FOLLOW="1"))
try:
    check("...it reads the snapshot at its start", wait_for(lambda: bool(gw_recs(TF_, "gateway.snapshot")), 15))
    TF = D3.open_terminal(SF_)
    D3.take_over(TF, "user")
    check("...a takeover in a terminal of its session reaches the journal within seconds, with no tool call",
          wait_for(lambda: any(r["data"]["kind"] == "terminal.owner" and r["data"].get("terminal") == TF
                               for r in gw_recs(TF_)), 15))
    D3.restart()
    TF2 = D3.open_terminal(SF_)
    # The new terminal reaches the journal as an event, or in the new epoch's
    # snapshot when the thread reads it after the terminal opened.
    check("...and after a gateway restart it follows the new epoch (gateway.epoch; the new terminal in its "
          "snapshot or as its event)",
          wait_for(lambda: any(r["data"].get("to") == D3.epoch for r in gw_recs(TF_, "gateway.epoch"))
                   and (any(r["data"].get("terminal") == TF2 for r in gw_recs(TF_))
                        or any(r["data"].get("epoch") == D3.epoch
                               and TF2 in [t.get("terminal") for t in r["data"].get("terminals") or []]
                               for r in gw_recs(TF_, "gateway.snapshot"))), 15))
finally:
    srv.stdin.close()
    srv.wait(timeout=30)

# --- nothing secret crosses -------------------------------------------------------------
TOK2 = "ghp_" + _rand(36)
SX_, PX_, TPX_, EHX_, EMX_ = gw_session("canary")
mcall("job_start", {"name": "canaryjob", "command": f"GITHUB_TOKEN={TOK2} ./deploy.sh", "cwd": str(PX_),
                    "redo_hint": "r"}, EMX_, tid="toolu_x_1")
gateway.sync(SX_)
recovery.reconcile(task_of(SX_), SX_)
summary.build(task_of(SX_))
check("a token in a gateway job's command: nowhere in checkpoint's state (registry, journal, cursor), raw or escaped",
      reg_rec("canaryjob")["owner"] == "gateway" and not scan_for([TOK2], Path(os.environ["CHECKPOINT_HOME"])))

for _f in FAKES:
    _f.close()
GWC_PATH.unlink(missing_ok=True)
if _GW_TIMEOUT_BEFORE is None:
    os.environ.pop("CHECKPOINT_GATEWAY_TIMEOUT", None)
else:
    os.environ["CHECKPOINT_GATEWAY_TIMEOUT"] = _GW_TIMEOUT_BEFORE
check("the fake gateways are gone (no socket folder left under /tmp)", not any(f.dir.exists() for f in FAKES))
