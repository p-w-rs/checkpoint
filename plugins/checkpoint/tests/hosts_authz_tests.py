# 0.7.0, checkpoint's last build step: reconciliation against the gateway's
# hosts (reachable, authenticated, runtime ready now; operations that need a
# host that isn't; a changed identity stops), authorization references (which
# rights and approvals the task's calls used, by name and decision only; still
# valid or revoked/changed before a resume), and idempotent pause retries (a
# pause whose answer was lost is never applied twice: the gateway's evidence,
# a pause id it keeps, or nothing sent while an on_pause interrupt job could
# get a second SIGINT). Run by tests/run_tests.py after tests/gateway_tests.py,
# in the same namespace (gw_session(), mcall(), reg_rec(), gw_recs(), hook(),
# ctx(), denied(), jbase(), jrecs(), task_of(), section(), scan_for(), check(),
# capture, core, gateway, recovery, summary, state, fg ...). A fake gateway
# (tests/fake_gateway.py) of its own; nothing reaches the real
# ~/.config/gateway or the real Claude config (settings files are read from a
# scratch CLAUDE_CONFIG_DIR and project folders under TMP). Canaries are made
# here and never printed.
# ruff: noqa: F821  (names come from run_tests.py and the test files before it)
from checkpoint import authz, hosts as hosts_mod  # noqa: E402

os.environ["CHECKPOINT_GATEWAY_TIMEOUT"] = "0.4"
HA_FAKES = []


def hafake(**kw):
    g = fg.FakeGateway(GWC_PATH, **kw)
    HA_FAKES.append(g)
    return g


def modspool(sid, event, data):
    """One of the mod's events into the session's spool, as the mod sends it
    (hooks/env.sh spool, the payload on stdin)."""
    subprocess.run(["sh", str(ROOT / "hooks" / "env.sh"), "spool", sid, event],
                   input=json.dumps({"hook_event_name": event, "session_id": sid, **data}), text=True,
                   check=True, timeout=30)


def call(sid, proj, tp, eh, tid, tool, inp, outcome=None, mode=None):
    """A tool call through the real hooks: its intent, and an outcome when
    `outcome` is "ok" or "fail" (none: its result never came)."""
    extra = {"permission_mode": mode} if mode else {}
    hook("PreToolUse", jbase(sid, proj, tp, tool_name=tool, tool_use_id=tid, tool_input=inp, **extra), eh)
    if outcome == "ok":
        hook("PostToolUse", jbase(sid, proj, tp, tool_name=tool, tool_use_id=tid, tool_input=inp,
                                  tool_response={"stdout": "", "stderr": "", "interrupted": False}, **extra), eh)
    elif outcome == "fail":
        hook("PostToolUseFailure", jbase(sid, proj, tp, tool_name=tool, tool_use_id=tid, tool_input=inp,
                                         error="Exit code 1", **extra), eh)


M_LOCAL = fg.ulid("m")
M_BUILD, M_DB = fg.ulid("m"), fg.ulid("m")


def host_recs(build=None, db=None):
    b = {"host": M_BUILD, "name": "build", "local": False, "address": "build.example.invalid",
         "identity": "verified", "machine": "tokbuild1", "same_machine": False, "reachable": "ok", "auth": "ok",
         "runtime": "ready"}
    d = {"host": M_DB, "name": "db", "local": False, "address": "db.example.invalid", "identity": "verified",
         "machine": "tokdb1", "same_machine": False, "reachable": "ok", "auth": "ok", "runtime": "ready"}
    b.update(build or {})
    d.update(db or {})
    return [{"host": M_LOCAL, "name": "localhost", "local": True, "address": "", "identity": "machine",
             "runtime": "ready"}, *(x for x in (b, d) if x.get("name"))]


HG = hafake(machine_id=M_LOCAL, hosts=host_recs()).start()

# --- hosts the task used, against the gateway now ----------------------------------------
print("0.7.0: reconciliation against the gateway's hosts (reachable, authenticated, runtime; identity):")
HS_, HP_, HTP_, HEH_, HEM_ = gw_session("hosts")
HT_ = task_of(HS_)
gateway.sync(HS_)                                        # the snapshot names build and db
call(HS_, HP_, HTP_, HEH_, "h-term-1", "mcp__plugin_gate_gw__terminal_open", {"host": "build", "cwd": "/srv/app"})
call(HS_, HP_, HTP_, HEH_, "h-ssh-1", "Bash", {"command": "ssh -p 2222 deploy@db 'systemctl restart app'"})
call(HS_, HP_, HTP_, HEH_, "h-ssh-x", "Bash", {"command": "ssh nosuchhost uptime"}, outcome="ok")
capture.ingest(HS_)
r0 = recovery.reconcile(HT_, HS_)
names0 = [h.use.name for h in r0.hosts]
check("the hosts a task used: a gateway tool's `host` (terminal_open on build) and `ssh ... db` (a host the gateway "
      "lists; inferred from the command); a name the gateway doesn't list isn't one",
      names0 == ["build", "db"] and r0.hosts[1].use.inferred and not r0.hosts[0].use.inferred)
check("...all reachable, authenticated and ready now: said per host from the fresh snapshot, nothing flagged",
      all("reachable, authenticated, runtime ready" in h.line() for h in r0.hosts)
      and not any("Host " in t for t in r0.check_first()))
HG.hosts = host_recs(build={"reachable": "down", "auth": "unknown"}, db={"auth": "refused"})
HG.emit("host.state", host=M_BUILD, name="build", state="unknown", reachable="down", auth="unknown",
        runtime="unknown", identity="verified")
gateway.sync(HS_)
r1 = recovery.reconcile(HT_, HS_)
cf1 = r1.check_first()
txt1 = r1.text("", limit=20000)
dump("hosts-reconcile", txt1)
check("build unreachable now (the gateway's snapshot): 'Check first' names it and the interrupted terminal_open that "
      "needs it (don't run it again meanwhile, nor elsewhere)",
      any("Host build [" in t and "UNREACHABLE now" in t and "terminal_open" in t and "nor elsewhere" in t for t in cf1))
check("db's authentication refused now: its `ssh db` operation is marked too",
      any("Host db [" in t and "authentication REFUSED" in t and "ssh -p 2222" in t for t in cf1))
check("...the interrupted operations carry the mark ('needs host build: UNREACHABLE now (the gateway)')",
      "needs host build [" in txt1 and "UNREACHABLE now (the gateway)" in txt1 and "## Hosts" in txt1)
check("...the Hosts section says what was last observed in the journal too (host.state)",
      "last observed in the journal: reachable down" in section(txt1, "Hosts"))
HG.hosts = host_recs(db={"reachable": None, "auth": None, "runtime": None})
for h_ in HG.hosts:
    for k_ in [k for k, v in h_.items() if v is None]:
        del h_[k_]
r2 = recovery.reconcile(HT_, HS_)
dbl = next(h.line() for h in r2.hosts if h.use.name == "db")
check("a state the gateway didn't report is 'not reported', never guessed (no reachable, auth or runtime for db)",
      "reachable not reported" in dbl and "auth not reported" in dbl and "runtime not reported" in dbl
      and "authenticated" not in dbl and not r2.hosts[1].blocked())
HG.stop(remove_contract=True)
r3 = recovery.reconcile(HT_, HS_)
check("the gateway not available: every host's reachable, authentication and runtime 'unknown' with the reason; "
      "none is claimed reachable or unreachable, and the operations say their host's state is unknown",
      all("unknown (the gateway isn't available" in h.line() for h in r3.hosts)
      and not any("UNREACHABLE" in h.line() or "REFUSED" in h.line() for h in r3.hosts)
      and "its state now is unknown" in r3.text("", limit=20000)
      and not any(h.blocked() for h in r3.hosts))
HG.start()
HG.hosts = host_recs(build={"machine": "tokbuild2"})
r4 = recovery.reconcile(HT_, HS_)
items4 = r4.check_items()
check("build's machine token changed since the task used it (the gateway's snapshot): a rank-0 'Check first' item, "
      "stop and check, never cut for length",
      any(rank == 0 and "Host build [" in t and "machine token changed" in t and "Stop and check" in t
          for rank, t in items4)
      and "machine token changed" in r4.text("", limit=900))
HG.hosts = host_recs()
HG.emit("host.state", host=M_DB, name="db", state="ready", reachable="ok", auth="ok", runtime="ready",
        identity="changed_key")
gateway.sync(HS_)
r5 = recovery.reconcile(HT_, HS_)
check("db's host key changed per the gateway (its last host.state, not yet confirmed): stop and check",
      any(rank == 0 and "Host db [" in t and "host key changed" in t for rank, t in r5.check_items()))
HG.restart()                                            # a new epoch: its snapshot (identity "verified") is recorded
gateway.sync(HS_)
r5b = recovery.reconcile(HT_, HS_)
check("...a later snapshot saying 'verified' doesn't clear it (the gateway's snapshot says so for any host it has "
      "met, also while a change waits for the person)",
      sum(1 for r in jrecs(HT_) if r["kind"] == "gateway.snapshot") >= 2
      and any(rank == 0 and "Host db [" in t and "host key changed" in t for rank, t in r5b.check_items()))
HG.emit("host.state", host=M_DB, name="db", state="unknown", reachable="down", auth="unknown", runtime="unknown",
        identity="unknown")
gateway.sync(HS_)
r5c = recovery.reconcile(HT_, HS_)
check("...nor does the restarted gateway's first host.state for it (identity 'unknown' in a new epoch: its fresh "
      "status for every host, not the person's OK)",
      any(rank == 0 and "Host db [" in t and "host key changed" in t for rank, t in r5c.check_items()))
HG.emit("host.state", host=M_DB, name="db", state="ready", reachable="ok", auth="ok", runtime="ready",
        identity="verified")
gateway.sync(HS_)
r6 = recovery.reconcile(HT_, HS_)
check("...once the gateway says verified again (the person confirmed it), no identity item",
      not any("Host db [" in t and "host key" in t for t in r6.check_first()))
HG.emit("host.state", host=M_DB, name="db", state="ready", reachable="ok", auth="ok", runtime="ready",
        identity="changed_key")
HG.emit("host.state", host=M_DB, name="db", state="ready", reachable="ok", auth="ok", runtime="ready",
        identity="unknown")
gateway.sync(HS_)
r6b = recovery.reconcile(HT_, HS_)
check("a change, then identity 'unknown' in the same epoch (the gateway's accept path: the person trusted the new key "
      "in its dialog): no identity item",
      not any("Host db [" in t and "host key" in t for t in r6b.check_first()))
HG.emit("host.state", host=M_DB, name="db", state="ready", reachable="ok", auth="ok", runtime="ready",
        identity="changed_machine")
gateway.sync(HS_)
HG.hosts = host_recs(db={"name": ""})
r6c = recovery.reconcile(HT_, HS_)
dbl6 = next(h.line() for h in r6c.hosts if h.use.name == "db")
check("a used host whose identity changed and that the gateway no longer lists: still the rank-0 stop-and-check item "
      "(not only 'no longer in the host list'), its Hosts line says so, its operations are marked",
      any(rank == 0 and "Host db [" in t and "another machine now" in t and "Stop and check" in t
          for rank, t in r6c.check_items())
      and "NOT in the gateway's host list" in dbl6 and "IDENTITY: it is another machine now" in dbl6
      and "needs host db [" in r6c.text("", limit=20000) and "whose IDENTITY changed" in r6c.text("", limit=20000))
HG.hosts = host_recs()
HG.emit("host.state", host=M_DB, name="db", state="ready", reachable="ok", auth="ok", runtime="ready",
        identity="verified")
gateway.sync(HS_)
HG.hosts = host_recs(build={"name": ""})
r7 = recovery.reconcile(HT_, HS_)
check("a used host the gateway no longer lists: said (removed or renamed), never assumed fine",
      any("Host build [" in t and "no longer in the gateway's host list" in t for t in r7.check_first()))
HG.hosts = host_recs()
recovery.record(HS_, r1, "test")
rec_ = [r for r in jrecs(HT_) if r["kind"] == "reconcile"][-1]
check("the reconcile record keeps each host's states as the gateway reported them (enums only)",
      {h["name"]: h.get("reachable") for h in rec_["data"]["hosts"]} == {"build": "down", "db": "ok"}
      and rec_["data"]["counts"]["hosts_blocked"] == 2)
smh = section(summary.build(HT_, limit=100000).text, "Jobs & gateway")
check("the summary (journal only) names each host the task used, how, and the gateway's last report of it",
      "host build [" in smh and "terminal_open call" in smh and "inferred from the command" in smh
      and "host.state, seq" in smh)
check("commands' hosts: ssh options skipped (-p 2222, user@), scp/rsync's NAME:, nothing from plain words",
      hosts_mod.command_hosts("ssh -o BatchMode=yes -p 22 me@build 'make'; scp a.txt db:/tmp/; rsync -a x/ "
                              "web:x/; echo build") == ["build", "db", "web"]
      and hosts_mod.command_hosts("FOO=1 sudo ssh -J jump build ls") == ["build"])
HG.hosts = host_recs(build={"reachable": False, "auth": None}, db={"reachable": True, "auth": False,
                                                                  "runtime": "staging"})
r8 = recovery.reconcile(HT_, HS_)
l8 = {h.use.name: h.line() for h in r8.hosts}
check("the contract's true / false / null states read like the gateway's words: build unreachable (false) with authentication "
      "unknown (null); db reachable (true), NOT authenticated (false), its runtime being staged",
      "UNREACHABLE, authentication unknown" in l8["build"] and "reachable, NOT authenticated, runtime being staged"
      in l8["db"] and any("Host db [" in t and "NOT authenticated now" in t for t in r8.check_first()))
HG.hosts = host_recs()
check("a token-shaped host field never crosses into checkpoint's state (clean_host scrubs it)",
      gateway.clean_host({"host": M_BUILD, "name": "build", "reachable": "token:" + "q" * 30})
      .get("reachable", "") != "token:" + "q" * 30)
# The gateway's own job_start runs its command on its `host` (localhost when it
# names none): an interrupted one needs that host. Checkpoint's own job_start
# has no host: its command's `ssh NAME` counts, as a Bash command's does.
HJ_, HJP_, HJTP_, HJEH_, _hjem = gw_session("hostjob")
HJT_ = task_of(HJ_)
gateway.sync(HJ_)
call(HJ_, HJP_, HJTP_, HJEH_, "h-gwjob-1", "mcp__plugin_gate_gw__job_start",
     {"host": "build", "command": "make deploy", "on_pause": "interrupt"})
call(HJ_, HJP_, HJTP_, HJEH_, "h-gwjob-2", "mcp__plugin_gate_gw__job_start", {"command": "make lint"})
call(HJ_, HJP_, HJTP_, HJEH_, "h-ownjob-1", "mcp__plugin_check_jobs__job_start",
     {"name": "up", "command": "ssh db uptime", "redo_hint": "r"})
capture.ingest(HJ_)
_hji = {r["op"]: r["data"]["input"] for r in jrecs(HJT_) if r["kind"] == "tool.intent"}
check("the journal keeps the host a job_start names (the gateway's job_start without one: localhost, its default; "
      "checkpoint's own has none)",
      _hji["h-gwjob-1"].get("host") == "build" and _hji["h-gwjob-2"].get("host") == "localhost"
      and "host" not in _hji["h-ownjob-1"]
      and "host" not in sanitize.tool_input("mcp__plugin_gate_gw__job_start",
                                            {"host": "ghp_" + _rand(36), "command": "x"}))
HG.hosts = host_recs(build={"reachable": "down", "auth": "unknown"})
rj = recovery.reconcile(HJT_, HJ_)
txtj = rj.text("", limit=20000)
dump("hosts-gateway-job", txtj)
usej = {h.use.name: h.use for h in rj.hosts}
check("an interrupted gateway job_start on build, which is UNREACHABLE now: marked 'needs host build' and named "
      "under the host's 'Check first' item; build used by the job_start call",
      any("job_start `make deploy`" in ln and "needs host build [" in ln and "UNREACHABLE now (the gateway)" in ln
          for ln in txtj.splitlines())
      and any("Host build [" in t and "UNREACHABLE now" in t and "make deploy" in t for t in rj.check_first())
      and "job_start call" in usej["build"].via and not usej["build"].inferred)
check("...the one without a host ran on this machine (localhost); checkpoint's own job_start's `ssh db` counts as "
      "inferred from its command",
      usej.get("localhost") is not None and usej["localhost"].local
      and usej.get("db") is not None and usej["db"].inferred
      and hosts_mod.call_hosts("mcp__plugin_gate_gw__job_start", {"command": "make deploy"}) == []
      and hosts_mod.call_hosts("mcp__plugin_check_jobs__job_start", {"command": "ssh db uptime"}) == ["db"]
      and hosts_mod.call_hosts("mcp__plugin_gate_gw__job_start", {"host": "build", "command": "ssh db x"})
      == ["build"])
HG.hosts = host_recs()

# --- authorization references --------------------------------------------------------------
print("0.7.0: authorization references (by name and decision; still valid or revoked/changed before a resume):")
SZ, PZ, TPZ, EHZ, EMZ = gw_session("authz")
TZ = task_of(SZ)
gateway.sync(SZ)
CANARY_R = _rand(30)
TOKEN_R = "ghp_" + _rand(36)
modspool(SZ, "mod.tool.check", {"tool_use_id": "az-1", "tool": "Bash", "decision": "ask", "hook": "PreToolUse",
                                "reason": f'gateway: ssh on build is set to "ask"; approve it to run. {CANARY_R}'})
call(SZ, PZ, TPZ, EHZ, "az-1", "Bash", {"command": "ssh build make"}, outcome="ok", mode="default")
modspool(SZ, "mod.tool.answer", {"tool_use_id": "az-1", "tool": "Bash", "answered": "result"})
modspool(SZ, "mod.tool.check", {"tool_use_id": "az-2", "tool": "Bash", "decision": "deny", "hook": "PreToolUse",
                                "reason": "gateway: sudo is off on this machine (gateway.toml); ask the user if it "
                                          "should be on."})
call(SZ, PZ, TPZ, EHZ, "az-2", "Bash", {"command": "sudo -n true"}, mode="default")
modspool(SZ, "mod.tool.answer", {"tool_use_id": "az-2", "tool": "Bash", "answered": "deny"})
modspool(SZ, "mod.tool.check", {"tool_use_id": "az-3", "tool": "Bash", "decision": "allow",
                                "rule": "Bash(git push:*)", "reason": f"rule {CANARY_R}"})
call(SZ, PZ, TPZ, EHZ, "az-3", "Bash", {"command": "git push origin main"}, outcome="ok", mode="default")
modspool(SZ, "mod.tool.check", {"tool_use_id": "az-13", "tool": "Bash", "decision": "ask", "rule": "Bash(make docs)"})
call(SZ, PZ, TPZ, EHZ, "az-13", "Bash", {"command": "make docs"}, outcome="ok", mode="default")
modspool(SZ, "mod.tool.answer", {"tool_use_id": "az-13", "tool": "Bash", "answered": "result"})
modspool(SZ, "mod.tool.check", {"tool_use_id": "az-4", "tool": "Bash", "decision": "ask"})
call(SZ, PZ, TPZ, EHZ, "az-4", "Bash", {"command": "rm -rf build"}, mode="default")
modspool(SZ, "mod.tool.answer", {"tool_use_id": "az-4", "tool": "Bash", "answered": "deny"})
modspool(SZ, "mod.tool.check", {"tool_use_id": "az-9", "tool": "Bash", "decision": "deny",
                                "rule": "Bash(make release:*)"})
call(SZ, PZ, TPZ, EHZ, "az-9", "Bash", {"command": "make release"}, mode="default")
modspool(SZ, "mod.tool.answer", {"tool_use_id": "az-9", "tool": "Bash", "answered": "error", "rejected": True})
modspool(SZ, "mod.tool.check", {"tool_use_id": "az-12", "tool": "Bash", "decision": "ask"})
call(SZ, PZ, TPZ, EHZ, "az-12", "Bash", {"command": "make clean-all"}, mode="default")
modspool(SZ, "mod.tool.answer", {"tool_use_id": "az-12", "tool": "Bash", "answered": "error", "rejected": True})
modspool(SZ, "mod.tool.check", {"tool_use_id": "az-10", "tool": "Bash", "decision": "ask"})
call(SZ, PZ, TPZ, EHZ, "az-10", "Bash", {"command": "make deploy-docs"}, mode="default")
modspool(SZ, "mod.tool.answer", {"tool_use_id": "az-10", "tool": "Bash", "answered": "error"})
modspool(SZ, "mod.tool.check", {"tool_use_id": "az-7", "tool": "Bash", "decision": "allow",
                                "rule": f"Bash(curl -H 'Authorization: Bearer {TOKEN_R}':*)"})
modspool(SZ, "mod.tool.check", {"tool_use_id": "", "tool": "Bash", "decision": "ask", "reason": CANARY_R})
_wr = {"file_path": str(PZ / "notes.md"), "content": "hello"}
call(SZ, PZ, TPZ, EHZ, "az-5", "Write", _wr, mode="auto")
hook("PermissionRequest", jbase(SZ, PZ, TPZ, tool_name="Write", tool_input=_wr, permission_mode="auto",
                                permission_suggestions=[{"type": "addRules"}]), EHZ)
hook("PostToolUse", jbase(SZ, PZ, TPZ, tool_name="Write", tool_use_id="az-5", tool_input=_wr,
                          tool_response={"filePath": str(PZ / "notes.md")}, permission_mode="auto"), EHZ)
call(SZ, PZ, TPZ, EHZ, "az-6", "Bash", {"command": "curl -X POST https://example.invalid/x"}, mode="auto")
hook("PermissionDenied", jbase(SZ, PZ, TPZ, tool_name="Bash", tool_use_id="az-6",
                               tool_input={"command": "curl -X POST https://example.invalid/x"},
                               reason=f"classifier {CANARY_R}"), EHZ)
call(SZ, PZ, TPZ, EHZ, "az-8", "mcp__plugin_gate_gw__http", {"url": "https://api.example.invalid/x",
                                                              "account": "github"}, outcome="ok", mode="auto")
HG.emit("auth.waiting", request="r_" + _rand(10), session=SZ, dialog="secret")
HG.emit("auth.supplied", request=HG.events[-1]["request"], outcome="ok")
gateway.sync(SZ)
capture.ingest(SZ)
refs_ = [r for r in jrecs(TZ) if r["kind"] == "auth.ref"]
check("auth.ref records: the verdicts (decision, rule, deciding hook), the gateway's right names read from its fixed "
      "wording, what came of the asks, the permission dialog; a verdict with no tool_use_id (a query) isn't one",
      {(r["data"]["via"], r["data"].get("tool_use_id")) for r in refs_} >= {("check", "az-1"), ("answer", "az-1"),
                                                                           ("check", "az-2"), ("check", "az-3"),
                                                                           ("check", "az-4"), ("answer", "az-4")}
      and any(r["data"]["via"] == "request" and r["data"].get("input_sha") for r in refs_)
      and next(r for r in refs_ if r["data"].get("tool_use_id") == "az-1" and r["data"]["via"] == "check")
      ["data"].get("rights") == ["ssh on build"]
      and not any(r["data"].get("tool_use_id") == "" for r in refs_))
check("...never a reason text: the canary in the reasons is nowhere in checkpoint's state; a rule's token is scrubbed",
      not scan_for([CANARY_R, TOKEN_R], Path(os.environ["CHECKPOINT_HOME"]))
      and not any("reason" in r["data"] for r in refs_))
TOK_I = "ghp_" + _rand(36)
call(SZ, PZ, TPZ, EHZ, "az-11", "Bash", {"command": f"GITHUB_TOKEN={TOK_I} ./publish.sh"}, mode="auto")
capture.ingest(SZ)
_i11 = next(r for r in jrecs(TZ) if r["kind"] == "tool.intent" and r["op"] == "az-11")
check("...tool intents carry the permission mode and the hash of their sanitized input (never of the raw one, "
      "which would let a guess at the masked token be checked)",
      all(r["data"].get("mode") in ("default", "auto") and len(r["data"].get("input_sha", "")) == 16
          for r in jrecs(TZ) if r["kind"] == "tool.intent")
      and _i11["data"]["input_sha"] == capture.input_sha(sanitize.tool_input(
          "Bash", {"command": f"GITHUB_TOKEN={TOK_I} ./publish.sh"}))
      and _i11["data"]["input_sha"] != capture.input_sha({"command": f"GITHUB_TOKEN={TOK_I} ./publish.sh"}))
smz = summary.build(TZ, limit=100000)
az_ = section(smz.text, "Authorizations")
dump("authz-summary", smz.text)
check("the summary's Authorizations section: each right or approval by name and decision (gateway rights asked and "
      "approved, off; a settings rule; an ask declined; auto mode's classifier; a gateway dialog; the modes)",
      'gateway right "ssh on build": approved' in az_ and 'gateway right "sudo on this machine": off' in az_
      and "settings rule `Bash(git push:*)`: allow" in az_ and "Bash calls put to the person: declined" in az_
      and "Write calls put to the person: approved" in az_ and "auto mode's classifier on Bash calls: denied" in az_
      and "gateway dialog (secret): ok" in az_ and "permission mode: default" in az_ and "auto (seq" in az_)
rz0 = recovery.reconcile(TZ, SZ)
nev = {o.op: o for o in rz0.ops if o.cls == "never"}
check("calls Claude Code refused (the gateway's hook denied sudo; the person declined rm -rf, and make clean-all "
      "with an error in Claude Code's refusal words, as 2.1.301 answers a No; a deny rule's error) never ran: never dispatched, with how, and none is an unknown outcome in the "
      "summary's In flight; an error after an ask without those words stays unknown (it may have run)",
      "az-2" in nev and "refused by the gateway's approval hook" in nev["az-2"].why
      and "az-4" in nev and "declined when Claude Code asked" in nev["az-4"].why
      and "az-9" in nev and "refused by the settings rule `Bash(make release:*)`" in nev["az-9"].why
      and "az-12" in nev and "declined when Claude Code asked" in nev["az-12"].why
      and "az-10" not in nev
      and "sudo -n true" not in section(smz.text, "In flight / unknown")
      and "rm -rf build" not in section(smz.text, "In flight / unknown"))
CFG_A = TMP / "authz-config"
CFG_A.mkdir()
(CFG_A / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(git push:*)"],
                                                                 "ask": ["Bash(make docs)"]}}))
_cfg_before = os.environ.get("CLAUDE_CONFIG_DIR")
os.environ["CLAUDE_CONFIG_DIR"] = str(CFG_A)
try:
    ra = recovery.reconcile(TZ, SZ)
    holds = "\n".join(ra.authz.holds)
    check("before a resume, unchanged ones hold (the allow rule still in your settings; the ask rule the person "
          "approved, still an ask rule), nothing flagged; the resume context says not to ask the person again",
          "settings rule `Bash(git push:*)` (allow): unchanged in your settings" in holds
          and "settings rule `Bash(make docs)` (approved): unchanged in your settings" in holds
          and not any("git push" in f or "ssh on build" in f or "make docs" in f for f in ra.authz.flagged))
    check("...a gateway right the snapshot lists no rights for: not compared, said so, never 'still "
          "holds' (only the host's presence could be compared)",
          any('gateway right "ssh on build" (approved): not compared: the gateway\'s snapshot lists no rights' in u
              and "build is still configured" in u for u in ra.authz.unknown)
          and not any('"ssh on build"' in x for x in ra.authz.holds))
    check("...(the resume context's Authorizations section: 'no need to ask the person again')",
          "no need to ask the person again" in section(ra.text("", limit=20000), "Authorizations")
          and not any("Authorization changed" in t and "git push" in t for t in ra.check_first()))
    (CFG_A / "settings.json").write_text(json.dumps({"permissions": {"deny": ["Bash(git push:*)"],
                                                                     "allow": ["Bash(make docs)"]}}))
    HG.hosts = host_recs(build={"name": ""})
    rb = recovery.reconcile(TZ, SZ)
    flg = "\n".join(rb.authz.flagged)
    check("an ask rule the person approved that is an allow rule now: CHANGED: ask then, allow now",
          "settings rule `Bash(make docs)` (approved): CHANGED: ask then, allow now in your settings" in flg)
    dump("authz-reconcile", rb.text("", limit=20000))
    check("revoked: the rule now denied in your settings, and build gone from the gateway: both in 'Check first' "
          "(don't rely on them; the person's OK again)",
          "Bash(git push:*)` (allow): CHANGED: allow then, deny now in your settings (revoked)" in flg
          and 'gateway right "ssh on build" (approved): REVOKED: build is no longer in the gateway' in flg
          and sum(1 for t in rb.check_first() if t.startswith("Authorization changed")) >= 2)
    (CFG_A / "settings.json").write_text(json.dumps({"permissions": {}}))
    HG.hosts = host_recs(build={"rights": {"exec": "deny", "read": "allow"}})
    rc2 = recovery.reconcile(TZ, SZ)
    check("a rule no longer in any settings file Claude Code reads: flagged (Claude Code decides again); a gateway "
          "right the snapshot now lists as off: REVOKED",
          any("no longer in the settings checkpoint can read" in f for f in rc2.authz.flagged)
          and any('"ssh on build"' in f and "REVOKED: it is off now" in f for f in rc2.authz.flagged))
    HG.hosts = host_recs(build={"rights": {"exec": "ask"}})
    rc3 = recovery.reconcile(TZ, SZ)
    check("...the same right still 'ask' in the snapshot: unchanged (holds)",
          any('"ssh on build"' in x and x.endswith("unchanged") for x in rc3.authz.holds))
    HG.hosts = host_recs(build={"machine": "tokbuildA"})
    rc4 = recovery.reconcile(TZ, SZ)
    check("...a right on a host whose identity changed: flagged (a changed target)",
          any('"ssh on build"' in f and "identity changed" in f for f in rc4.authz.flagged))
    HG.hosts = host_recs()
    HG.stop(remove_contract=True)
    rc5 = recovery.reconcile(TZ, SZ)
    check("the gateway not available: its rights not compared (said), never claimed valid or revoked",
          any('"ssh on build"' in u and "not compared" in u for u in rc5.authz.unknown)
          and not any('"ssh on build"' in x for x in rc5.authz.holds + rc5.authz.flagged))
    HG.start()
finally:
    if _cfg_before is None:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
    else:
        os.environ["CLAUDE_CONFIG_DIR"] = _cfg_before
    HG.accounts = [{"account": "github", "kind": "github", "state": "connected", "rights": {"credential": "allow"}}]
    rd = recovery.reconcile(TZ, SZ)
    check("an account the task's gateway call used (http with account github): still connected with its credential "
          "right: holds",
          any("gateway account github (used)" in x and "state connected" in x for x in rd.authz.holds))
    HG.accounts = [{"account": "github", "kind": "github", "state": "expired", "rights": {"credential": "allow"}}]
    HG.emit("rights.changed", entry="build", change="narrowed")
    HG.emit("account.state", account="github", state="expired")
    gateway.sync(SZ)
    re_ = recovery.reconcile(TZ, SZ)
    check("...expired now: flagged; a rights.changed event (build narrowed) flags the right used on build",
          any("gateway account github" in f and "expired now" in f for f in re_.authz.flagged)
          and any('"ssh on build"' in f and "narrowed at seq" in f for f in re_.authz.flagged)
          and "the gateway's rights for build: narrowed" in section(summary.build(TZ, limit=100000).text,
                                                                   "Authorizations"))
    HG.accounts = []
    rf_ = recovery.reconcile(TZ, SZ)
    check("...gone from the gateway's accounts: REVOKED",
          any("gateway account github" in f and "REVOKED" in f for f in rf_.authz.flagged))
    HG.accounts = None
    rg_ = recovery.reconcile(TZ, SZ)
    check("...a snapshot that lists no accounts: not compared, said so",
          any("gateway account github" in u and "lists no accounts" in u for u in rg_.authz.unknown))
check("the gateway's policy wording: the action from the rule's key (or from what it covers), the target; never "
      "the rule's text",
      authz.gateway_rights('gateway: running commands on the host (ssh HOST CMD, terminals, jobs) on build is denied by '
                           '[hosts.build.policy] exec = "deny" (gateway.toml line 12); ask the user if it should be '
                           'allowed (they change gateway.toml, then approve it with gate check build).')
      == ("off", ["exec on build"])
      and authz.gateway_rights('gateway: sudo (root) on the host on build is "ask" ([hosts.build] sudo = "ask" '
                               '(gateway.toml line 3)); only a command Claude typed can be asked about; run it.')
      == ("ask", ["sudo on build"])
      and authz.gateway_rights('gateway: the gateway handing a stored credential to a command on github is denied by '
                               'policy; ask the user.') == ("off", ["credential on github"]))
check("gateway_rights reads only the gateway's fixed wording; another hook's text gives no names",
      authz.gateway_rights('gateway: ssh on a, sudo (root) on b and git on c are set to "ask"; approve it to run.')
      == ("ask", ["ssh on a", "sudo (root) on b", "git on c"])
      and authz.gateway_rights("gateway: ssh is off for b (gateway.toml); ask") == ("off", ["ssh on b"])
      and authz.gateway_rights("my-plugin: no") == ("", [])
      and authz.gateway_rights('gateway: rm -rf / on x; y is set to "ask"') == ("ask", []))
# Gateway 0.11: a right a per-mode table turns off names the mode the call was
# judged in (hooks_bash._deny_right, modes.DISPLAY), and the right is kept.
_OFF_TAIL = " (gateway.toml); ask the user if it should be on."
_mode_rows = [(f"gateway: ssh is off for homelab in {m} mode" + _OFF_TAIL, ("off", ["ssh on homelab"]))
              for m in ("Manual", "Plan", "Accept edits", "Auto", "Bypass permissions", "Don't ask")]
_mode_rows += [("gateway: sudo is off for build in Auto mode" + _OFF_TAIL, ("off", ["sudo on build"])),
               ("gateway: scp is off for nas-2.lan in Plan mode" + _OFF_TAIL, ("off", ["scp on nas-2.lan"])),
               ("gateway: sudo is off on this machine in Auto mode (gateway.toml); ask the user if it should be on.",
                ("off", ["sudo on this machine"])),
               ("gateway: sudo is off on this machine in Don't ask mode (gateway.toml); ask the user.",
                ("off", ["sudo on this machine"])),
               ("gateway: sudo is off on this machine (gateway.toml); ask the user to turn it on.",
                ("off", ["sudo on this machine"]))]
_mode_bad = [(r, authz.gateway_rights(r)) for r, want in _mode_rows if authz.gateway_rights(r) != want]
if _mode_bad:
    print(f"       ({_mode_bad[:3]})")
check("gateway 0.11's per-mode refusal (\"ssh is off for homelab in Auto mode (gateway.toml)\", every mode's name, "
      "sudo on this machine too): the right is kept, as for a right that is off in every mode", not _mode_bad)
check("...only the gateway's fixed wording: a mode name it never writes, or other words before \"(gateway.toml)\", "
      "gives no right",
      authz.gateway_rights("gateway: ssh is off for homelab in Turbo mode (gateway.toml); ask") == ("rule", [])
      and authz.gateway_rights("gateway: sudo is off on this machine in Turbo mode (gateway.toml); ask")
      == ("rule", [])
      and authz.gateway_rights("gateway: sudo is off on this machine, and more") == ("rule", []))
check("...and a per-mode table in a policy rule's text still names the action by what it covers, never a mode",
      authz.gateway_rights("gateway: running commands on the host (ssh HOST CMD, terminals, jobs) on build is denied "
                           "by [hosts.build.policy] exec = {Manual: ask · Auto: deny} (gateway.toml line 12); ask "
                           "the user if it should be allowed.") == ("off", ["exec on build"]))

# --- pause retries are idempotent ------------------------------------------------------------
print("0.7.0: a pause whose answer was lost is never applied twice:")


def pr_session(tag, policies):
    sid, proj, tp, eh, em = gw_session(tag)
    out = []
    for nm, pol_ in policies:
        mcall("job_start", {"name": nm, "command": f"run {nm}", "cwd": str(proj), "redo_hint": "r", "on_pause": pol_},
              em, tid=f"toolu_{tag}_{nm}")
        out.append(reg_rec(nm)["gateway_job"])
    return sid, proj, tp, eh, out


def pauses(sid):
    return [c for c in HG.calls("POST", "/v1/pause") if c["body"].get("session") == sid]


def wait_hang():
    """The fake's hung pause answers after the client gave up (2 s): let it."""
    time.sleep(HG.hang_seconds - 1.5)


HG.hang_seconds = 3.0
SQ, PQ, TPQ, EHQ, (JQ,) = pr_session("phold", [("intq", "interrupt")])
HG.ignore_term.add(JQ)                      # it ignores SIGINT: a second one is what must not happen
HG.hang["POST /v1/pause"] = 2               # the pause and the prompt hook's own resend (same id) right after it
outq = hook("UserPromptSubmit", jbase(SQ, PQ, TPQ, prompt="/check:pause"), EHQ)
gpq = state.load_session(SQ).get("gateway_paused") or {}
check("a pause whose answer was lost (the gateway applied it: one SIGINT to the interrupt job, which ignored it): "
      "uncertain, with its pause id; the pause says it may not have reached the gateway and goes again with the "
      "same id",
      gpq.get("uncertain") is True and not gpq.get("delivered") and str(gpq.get("pause_id", "")).startswith("ckp-")
      and HG.sigints.get(JQ) == 1 and "may not have reached it" in ctx(outq) and "same pause id" in ctx(outq)
      and pauses(SQ)[0]["body"].get("pause_id") == gpq.get("pause_id"))
check(".../check:doctor says the pause may or may not have reached the gateway and goes again with the same id",
      "sent again at the next moment checkpoint runs, with the same pause id" in core.doctor_text(SQ))
wait_hang()
_n = len(pauses(SQ))
o1 = hook("PreToolUse", jbase(SQ, PQ, TPQ, tool_name="Bash", tool_use_id="pq-1", tool_input={"command": "ls"}), EHQ)
gpq2 = state.load_session(SQ).get("gateway_paused") or {}
pzq = pauses(SQ)
dump("pause-resent", str(gpq2.get("text")))
check("the next paused moment sends it again with the same pause id; the gateway answers it as applied already "
      "(replayed), so the interrupt job got exactly one SIGINT; delivered, the journal says it was a retry; the "
      "call stays denied",
      denied(o1) and len(pzq) == _n + 1 and pzq[-1]["body"].get("pause_id") == gpq.get("pause_id")
      and HG.sigints.get(JQ) == 1 and gpq2.get("delivered") is True and "first answer again" in str(gpq2.get("text"))
      and any(r["kind"] == "gateway.pause" and r["data"].get("ok") and r["data"].get("replayed")
              and r["data"].get("retry") == core.RETRY_WHY for r in jrecs(task_of(SQ))))
hook("PreToolUse", jbase(SQ, PQ, TPQ, tool_name="Bash", tool_use_id="pq-2", tool_input={"command": "ls"}), EHQ)
check("...and once delivered, the moments after send nothing", len(pauses(SQ)) == _n + 1
      and HG.sigints.get(JQ) == 1)
_r0 = len(HG.calls("POST", "/v1/resume"))
outr = hook("UserPromptSubmit", jbase(SQ, PQ, TPQ, prompt="go on"), EHQ)
rz_ = HG.calls("POST", "/v1/resume")
check("a typed resume: /v1/resume with the same pause id",
      len(rz_) == _r0 + 1 and rz_[-1]["body"].get("pause_id") == gpq.get("pause_id") and not state.is_paused(SQ))
# The same for a suspend job.
SS2, PS2, TPS2, EHS2, (JSS,) = pr_session("psafe", [("suss", "suspend")])
HG.hang["POST /v1/pause"] = 1
hook("UserPromptSubmit", jbase(SS2, PS2, TPS2, prompt="/check:pause"), EHS2)
wait_hang()
hook("PreToolUse", jbase(SS2, PS2, TPS2, tool_name="Bash", tool_use_id="ps-1", tool_input={"command": "ls"}), EHS2)
check("a session whose job has on_pause suspend: the uncertain pause is sent again (the same pause id) and "
      "delivered, the job suspended once",
      len(pauses(SS2)) == 2 and (state.load_session(SS2).get("gateway_paused") or {}).get("delivered")
      and pauses(SS2)[0]["body"]["pause_id"] == pauses(SS2)[1]["body"]["pause_id"]
      and HG.jobs[JSS]["state"] == "suspended")
hook("UserPromptSubmit", jbase(SS2, PS2, TPS2, prompt="go on"), EHS2)
# The session record says "uncertain" before the pause goes out: checkpoint
# killed during the call never reads as "not sent".
SC2, PC2, TPC2, EHC2, (JC2,) = pr_session("pcrash", [("intc", "interrupt")])
HG.ignore_term.add(JC2)
subprocess.run(["sh", str(ROOT / "hooks" / "env.sh"), "flag", "pause", SC2, "/check-pause"], check=True, timeout=30)
_seen = {}
_orig_pause = gateway.pause


def _spy(*a, **k):
    _seen.update(state.load_session(SC2).get("gateway_paused") or {})
    raise SystemExit("checkpoint killed during the call")


gateway.pause = _spy
try:
    core.gateway_pause(SC2)
except SystemExit:
    pass
finally:
    gateway.pause = _orig_pause
check("before /v1/pause goes out the session record already says it may reach the gateway (uncertain, its pause id)",
      _seen.get("uncertain") is True and str(_seen.get("pause_id", "")).startswith("ckp-")
      and _seen.get("delivered") is False)
gateway.pause(gateway.discover(), SC2, task_of(SC2), "test", _seen["pause_id"])      # it did reach it
_n = len(pauses(SC2))
core.gateway_settle(SC2)
check("...so after such a death the next moment sends it again with that pause id, and the gateway applies "
      "nothing twice (one SIGINT); delivered",
      len(pauses(SC2)) == _n + 1 and pauses(SC2)[-1]["body"].get("pause_id") == _seen["pause_id"]
      and HG.sigints.get(JC2) == 1 and (state.load_session(SC2).get("gateway_paused") or {}).get("delivered"))
hook("UserPromptSubmit", jbase(SC2, PC2, TPC2, prompt="go on"), EHC2)


# A second pause command during the same pause is the same pause: its mark and
# pause id stay, so a delivered pause isn't sent again and an uncertain one
# goes again only with its own id. (set_paused used to rewrite the flag, and
# sync_flag took the new time for a new pause: a second /v1/pause with another
# id, a second SIGINT to the interrupt job.)
def pause_again(sid, proj, tp, eh):
    """Every route of a second pause command while `sid` is paused, each more
    than a second after the pause (sync_flag's tolerance); returns the
    replies of the typed one and of the queued command's echo."""
    time.sleep(1.3)
    typed = hook("UserPromptSubmit", jbase(sid, proj, tp, prompt="/check:pause"), eh)        # typed again
    time.sleep(1.1)
    echo = core.pause(sid, [], by="queued command",                       # a queued /check:pause's echo
                      echo={"at": state.now_iso(), "result": ""})
    time.sleep(1.1)
    core.pause("", [sid], by="command")                     # `/check:pause <session>` from elsewhere
    _ls = state.live_sessions
    state.live_sessions = lambda *a, **k: [{"session_id": sid}]
    try:
        time.sleep(1.1)
        core.pause("", ["all"], by="command")                  # `pause all` from another session
    finally:
        state.live_sessions = _ls
    subprocess.run(["sh", str(ROOT / "hooks" / "env.sh"), "flag", "pause", sid, "/check-pause"],   # the mod's
                   check=True, timeout=30)
    core.gateway_settle(sid)
    return ctx(typed), echo


SA1, PA1, TPA1, EHA1, (JA1,) = pr_session("pag1d", [("inta1", "interrupt")])
HG.ignore_term.add(JA1)
hook("UserPromptSubmit", jbase(SA1, PA1, TPA1, prompt="/check:pause"), EHA1)
gpa1 = state.load_session(SA1).get("gateway_paused") or {}
mark_a1 = state.load_session(SA1).get("paused_ts")
ty_a1, ec_a1 = pause_again(SA1, PA1, TPA1, EHA1)
gpa1b = state.load_session(SA1).get("gateway_paused") or {}
check("a delivered pause, then a second /check:pause typed, a queued /check:pause's echo, `/check:pause <session>` "
      "and `pause all` from elsewhere and the mod's /check-pause, each over a second later: one /v1/pause, one "
      "SIGINT to the interrupt job (which ignored it), the pause's mark and id unchanged",
      gpa1.get("delivered") is True and len(pauses(SA1)) == 1 and HG.sigints.get(JA1) == 1
      and state.load_session(SA1).get("paused_ts") == mark_a1 and gpa1b.get("pause_id") == gpa1.get("pause_id")
      and core.pause_id(SA1, core._pause_mark(SA1)) == gpa1.get("pause_id"))
check("...the replies say the gateway's answer was the pause's first one, nothing sent again now",
      "Already paused" in ty_a1 and "nothing was sent to the gateway again now" in ty_a1
      and "nothing was sent to the gateway again now" in ec_a1)
hook("UserPromptSubmit", jbase(SA1, PA1, TPA1, prompt="go on"), EHA1)
SA3, PA3, TPA3, EHA3, (JA3,) = pr_session("pag3i", [("inta3", "interrupt")])
HG.ignore_term.add(JA3)
HG.hang["POST /v1/pause"] = 2               # also the prompt hook's own resend (same id) right after it
hook("UserPromptSubmit", jbase(SA3, PA3, TPA3, prompt="/check:pause"), EHA3)
gpa3 = state.load_session(SA3).get("gateway_paused") or {}
wait_hang()
pause_again(SA3, PA3, TPA3, EHA3)
pza3 = pauses(SA3)
check("an uncertain pause (the answer lost, the gateway applied it), then the same second pause commands: the "
      "pause goes again only with its own id (the gateway answers it as applied already), still one SIGINT, "
      "delivered",
      gpa3.get("uncertain") is True and len(pza3) >= 2
      and {c["body"].get("pause_id") for c in pza3} == {gpa3.get("pause_id")}
      and HG.sigints.get(JA3) == 1 and (state.load_session(SA3).get("gateway_paused") or {}).get("delivered"))
hook("UserPromptSubmit", jbase(SA3, PA3, TPA3, prompt="go on"), EHA3)
check("pause ids: the same pause gives the same id, another pause or session another",
      core.pause_id("s", "m1") == core.pause_id("s", "m1") != core.pause_id("s", "m2")
      != core.pause_id("s", "m1", "other") and gateway.PAUSE_ID_RE.fullmatch(core.pause_id("s", "m1")))

for _f in HA_FAKES:
    _f.close()
GWC_PATH.unlink(missing_ok=True)
if _GW_TIMEOUT_BEFORE is None:
    os.environ.pop("CHECKPOINT_GATEWAY_TIMEOUT", None)
else:
    os.environ["CHECKPOINT_GATEWAY_TIMEOUT"] = _GW_TIMEOUT_BEFORE
check("0.7.0's fake gateway is gone", not any(f.dir.exists() for f in HA_FAKES))
