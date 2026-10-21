#!/usr/bin/env python3
"""Checkpoint with the gateway, both installed the normal way, in the real TUI
(CONTRACT-v1; PLAN-v4 phase 4; brief 4, 6, 14.2, 14.4).

    pixi exec --spec python=3.12 --spec pyte python tests/acceptance_gateway.py \\
        --tmp DIR [--keep] [--pixi-cache DIR] [--gateway DIR]

A scratch Claude config into which both plugins are installed from their
checkouts through a local git-source marketplace (tests/tui): the gateway
through the harness's e2e route (its marker keeps every gateway location in
a scratch root; `--gateway`, default ../gateway next to this repository),
checkpoint the plain way (`check@checkpoint`). Both runtimes are built
before the session (the gateway's pixi env with bash and tmux, checkpoint's
`env.sh ensure` into its plugin data folder). The session runs against the
harness's fake Messages API, in bypassPermissions mode; nothing touches the
real Claude config or ~/.config/gateway (tests/tui refuses both).

The model (scripted) starts three jobs with checkpoint's `job_start`, each
with an `on_pause` policy: interrupt (`sleep 6001`), continue (`sleep 6002`)
and suspend (`sleep 6003`). Then:

  start    each job is started and supervised by the gateway (supervisor
           gateway, a gateway job id; checkpoint's registry says owner
           gateway; no process of checkpoint's own runner), and runs
  pause    /check-pause (the mod, immediate): the gateway applies each
           policy (POST /v1/pause through checkpoint's gw-pause): the
           interrupt job ends (SIGINT), the continue job runs on, the
           suspend job is stopped (process state T); the ack (or the note
           after it) and the journal's gateway.pause say what happened to
           each; /check:status shows the jobs' states
  resume   a typed message: the resume context (what the model gets) says
           what the gateway did on resume; the suspended job runs again
  cancel   /check-cancel: the task's live gateway jobs are stopped through
           the gateway (TERM), the journal's task.cancel says so per job

Prints one line per check and a JSON summary to --tmp/acceptance-gateway.json;
exits non-zero on any failure.
"""
from __future__ import annotations

import argparse
import contextlib
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
REPO = PLUGIN.parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
from tui import driver, install, safety  # noqa: E402
from tui.fakeapi import body_json, result_text, tool_results  # noqa: E402

RESULTS: list[dict] = []
NOTES: list[str] = []
SUMMARY: dict = {}
PREBUILD_TIMEOUT = 1800
JOB = "mcp__plugin_check_jobs__job_start"
MARK = {"intr": "sleep 6001", "cont": "sleep 6002", "susp": "sleep 6003"}
POLICY = {"intr": "interrupt", "cont": "continue", "susp": "suspend"}


def check(name: str, ok, detail: str = "") -> bool:
    ok = bool(ok)
    RESULTS.append({"name": name, "ok": ok, **({"detail": detail[:600]} if detail and not ok else {})})
    print(f"  {'ok  ' if ok else 'FAIL'} {name}", flush=True)
    if detail and not ok:
        print("       (" + detail[:600].replace("\n", " | ") + ")", flush=True)
    return ok


def note(text: str) -> None:
    NOTES.append(text)
    print(f"  note {text}", flush=True)


def wait(s: driver.Session, cond, timeout: float, step: float = 0.2) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001  a file mid-write: try again
            pass
        s.pump(step)
    try:
        return bool(cond())
    except Exception:  # noqa: BLE001
        return False


def has(s: driver.Session, needle: str) -> bool:
    """The screen history holds `needle`, ignoring whitespace (rows wrap mid-word)."""
    return re.sub(r"\s+", "", needle) in re.sub(r"\s+", "", s.all_text())


def ps_rows() -> list[tuple[int, str, str]]:
    out = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,stat=,command="], capture_output=True, text=True).stdout
    rows = []
    for ln in out.splitlines():
        parts = ln.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and "ps -A" not in parts[2]:
            rows.append((int(parts[0]), parts[1], parts[2]))
    return rows


def proc(marker: str) -> tuple[int, str] | None:
    """(pid, state) of the process whose command is exactly `marker` (the job's sleep)."""
    for pid, st, cmd in ps_rows():
        if cmd.strip() == marker:
            return pid, st
    return None


def kill_marked() -> None:
    for pid, _st, cmd in ps_rows():
        if cmd.strip() in MARK.values():
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


def records(home: Path) -> list[dict]:
    out = []
    for seg in sorted(home.glob("journal/*/seg-*.jsonl")):
        for line in seg.read_text(errors="replace").splitlines():
            with contextlib.suppress(ValueError):
                out.append(json.loads(line))
    return out


def registry(home: Path) -> dict:
    try:
        return json.loads((home / "jobs.json").read_text()).get("jobs") or {}
    except (OSError, ValueError):
        return {}


def request_text(rec: dict) -> str:
    b = body_json(rec) or {}
    return json.dumps({"system": b.get("system"), "messages": b.get("messages")}, ensure_ascii=False)


def tools_offered(rec: dict) -> list[str]:
    return [t.get("name", "") for t in (body_json(rec) or {}).get("tools") or [] if isinstance(t, dict)]


def offered_or_deferred(rec: dict, tool: str) -> bool:
    """The tool is in the request's tools, or Claude Code names it as a deferred tool (2.1.304 lists MCP
    tools without alwaysLoad only by name, behind ToolSearch; a scripted tool_use of it still runs)."""
    if tool in tools_offered(rec):
        return True
    b = body_json(rec) or {}
    return "ToolSearch" in tools_offered(rec) and tool in json.dumps({"s": b.get("system"), "m": b.get("messages")})


def wait_main(s: driver.Session, n: int, timeout: float) -> bool:
    """At least n main-loop requests, pumping the terminal meanwhile: Claude Code writes to its
    terminal synchronously, so a test that only sleeps lets the pty fill up and stalls the session."""
    return wait(s, lambda: len(s.api.main_requests()) >= n, timeout, 0.2)


def type_cmd(s: driver.Session, text: str) -> float:
    s.type(text, 0.005)
    s.pump(0.3)
    t = time.time()
    s.send("\r")
    return t


def prebuild_gateway(root: Path, h, gi, env: dict) -> tuple[bool, str]:
    """The gateway's pixi env (bash, tmux) before any session (its own launcher)."""
    plugin = Path(gi.plugin.cache_path)
    for d in ("b-home", "b-tmp"):
        (root / d).mkdir(exist_ok=True)
    benv = safety.hermetic_env(config_dir=h.inst.config_dir, home=root / "b-home", tmpdir=root / "b-tmp",
                               extra={**env, **gi.env()})
    t0 = time.time()
    try:
        r = subprocess.run(["/bin/sh", str(plugin / "hooks" / "launch.sh"), "ensure-env"], env=benv,
                           cwd=root / "b-tmp", capture_output=True, text=True, timeout=PREBUILD_TIMEOUT)
        rc, err = r.returncode, r.stderr[-400:]
    except subprocess.TimeoutExpired:
        rc, err = None, f"no answer in {PREBUILD_TIMEOUT} s"
    ready = sorted(gi.root.glob("data/envs/*/.ready"))
    tmux = sorted(gi.root.glob("data/envs/*/.pixi/envs/default/bin/tmux"))
    return (bool(ready) and bool(tmux) and rc == 0,
            f"launch.sh ensure-env: rc={rc} in {time.time() - t0:.0f} s; tmux {'in' if tmux else 'NOT in'} the env; "
            f"{err}")


def prebuild_checkpoint(root: Path, h, p, env: dict) -> tuple[bool, str]:
    """Checkpoint's runtime (`env.sh ensure`: pixi install --locked into its plugin data folder)."""
    for d in ("c-home", "c-tmp"):
        (root / d).mkdir(exist_ok=True)
    cenv = safety.hermetic_env(config_dir=h.inst.config_dir, home=root / "c-home", tmpdir=root / "c-tmp",
                               extra={**env, "CLAUDE_PLUGIN_DATA": str(p.data_path),
                                      "CLAUDE_PLUGIN_ROOT": str(p.cache_path)})
    t0 = time.time()
    r = subprocess.run(["sh", str(Path(p.cache_path) / "hooks" / "env.sh"), "ensure"], env=cenv, cwd=root / "c-tmp",
                       capture_output=True, text=True, timeout=PREBUILD_TIMEOUT)
    return r.returncode == 0, f"env.sh ensure: rc={r.returncode} in {time.time() - t0:.0f} s; {r.stderr[-300:]}"


def gated_pid(gi) -> int | None:
    try:
        return int(json.loads((gi.gateway_home / "run" / "contract.json").read_text())["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tmp", default=tempfile.gettempdir())
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--pixi-cache", default=None)
    ap.add_argument("--gateway", default=str(REPO.parent / "gateway"))
    a = ap.parse_args(argv)
    before = safety.real_settings_fingerprint()
    root = Path(tempfile.mkdtemp(prefix="ck-gw-", dir=a.tmp))
    safety.check_scratch(root, "acceptance root")
    print(f"acceptance (checkpoint with the gateway): root {root}", flush=True)
    env = {"PIXI_CACHE_DIR": a.pixi_cache or str(root / "pixi-cache")}
    t0 = time.time()
    gi_box: list = []
    try:
        run(root, env, Path(a.gateway).resolve(), gi_box)
    except Exception as e:  # noqa: BLE001  a failure, reported; the cleanup still runs
        check(f"the run went to its end ({type(e).__name__}: {str(e)[:300]})", False)
    finally:
        kill_marked()
        driver._close_all()
        if gi_box:
            pid = gated_pid(gi_box[0])
            if pid:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM)
    time.sleep(1.0)
    left = [f"{pid} {cmd}" for pid, _st, cmd in ps_rows() if str(root) in cmd or cmd.strip() in MARK.values()]
    if left:
        note(f"{len(left)} process(es) still named the scratch root or a job's marker; stopped: "
             + "; ".join(x.split()[0] + " ..." + x[-110:] for x in left[:3]))
        for x in left:
            with contextlib.suppress(Exception):
                os.kill(int(x.split()[0]), signal.SIGTERM)
        time.sleep(1.0)
        left = [f"{pid} {cmd}" for pid, _st, cmd in ps_rows() if str(root) in cmd or cmd.strip() in MARK.values()]
    check("no process left naming the scratch root or a job's marker (after stopping the e2e gated)", not left,
          "\n".join(left))
    check("real ~/.config/claude settings and plugin registry unchanged", before == safety.real_settings_fingerprint())
    failed = [r for r in RESULTS if not r["ok"]]
    summary = {"when": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "seconds": round(time.time() - t0),
               "claude": subprocess.run([driver.find_claude(), "--version"], capture_output=True,
                                        text=True).stdout.strip(),
               "passed": len(RESULTS) - len(failed), "failed": len(failed), "checks": RESULTS, "notes": NOTES,
               **SUMMARY}
    out = Path(a.tmp) / "acceptance-gateway.json"
    out.write_text(json.dumps(summary, indent=1))
    print(f"\n{summary['passed']} passed, {summary['failed']} failed in {summary['seconds']} s; {out}")
    if not a.keep:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if failed else 0


def run(root: Path, env: dict, gateway_src: Path, gi_box: list) -> None:
    with install.Hermetic(root / "h", extra_env=env) as h:
        gi = h.install_gateway_e2e(gateway_src)
        gi_box.append(gi)
        check("the gateway is installed through the e2e route (marker proven)", gi.proof is not None)
        p = h.install_from(REPO, repo="checkpoint", plugin="check")
        check("checkpoint is installed the normal way (check@checkpoint, a git-source marketplace)",
              p.cache_path is not None and (Path(p.cache_path) / "src" / "checkpoint" / "gateway.py").is_file())
        SUMMARY["versions"] = {"check": p.version, "gate": gi.plugin.version}
        print("prebuild: the gateway's pixi env and checkpoint's runtime", flush=True)
        ok, detail = prebuild_gateway(root, h, gi, env)
        if not check("the gateway's pixi env, with tmux, is built before any session", ok, detail):
            return
        note(detail.splitlines()[0])
        ok, detail = prebuild_checkpoint(root, h, p, env)
        if not check("checkpoint's runtime is built (env.sh ensure) before any session", ok, detail):
            return
        home = safety.state_root_for(h.inst.config_dir) / ".local" / "state" / "checkpoint"
        proj = root / "proj"
        proj.mkdir()
        s = driver.Session(root / "s1", proj, config_dir=h.inst.config_dir, extra_env=dict(env),
                           permission_mode="bypassPermissions", debug_file=True, cols=170, rows=50, start=False)
        try:
            s.start()
            s.wait_ready(180)
            check("the session runs the gateway redirected (e2e) next to checkpoint", s.gateway_mode == "e2e")
            steps(s, gi, home)
        finally:
            s.close()


def steps(s: driver.Session, gi, home: Path) -> None:
    contract = gi.gateway_home / "run" / "contract.json"
    check("the gateway's daemon is up (its CONTRACT-v1 discovery file in the e2e home)",
          wait(s, contract.is_file, 120, 0.5))
    check("checkpoint is recording (its footer label)", wait(s, lambda: "recording" in s.text(), 60))
    # checkpoint's MCP server connects in the background: wait until its job tool is offered.
    offered = False
    for i in range(6):
        n0 = len(s.api.main_requests())
        s.api.script([f"ready {i}"])
        s.submit(f"are the tools there? ({i})")
        if wait_main(s, n0 + 1, 60) and offered_or_deferred(s.api.main_requests()[-1], JOB):
            offered = True
            break
        s.pump(5)
    if not check("checkpoint's job_start tool is offered to the model", offered):
        return
    # start: three jobs with their on_pause policies
    n0 = len(s.api.main_requests())
    s.api.script([{"tool": JOB, "input": {"name": n, "command": MARK[n], "redo_hint": "rerun",
                                          "on_pause": POLICY[n]}} for n in ("intr", "cont", "susp")]
                 + ["three jobs started"])
    t_start = time.time()
    s.submit("Start the three jobs")
    wait_main(s, n0 + 4, 300)
    # how long each tool call took, from the request log (a slow one is said, not hidden)
    ts = [r.get("t") for r in s.api.main_requests()[n0:n0 + 4]]
    SUMMARY["job_start_turn_s"] = round(time.time() - t_start, 1)
    SUMMARY["job_start_gaps_s"] = [round(b - a, 1) for a, b in zip(ts, ts[1:]) if a and b]
    if any(g > 30 for g in SUMMARY["job_start_gaps_s"]):
        note(f"a job_start round trip took {max(SUMMARY['job_start_gaps_s'])} s (gaps between the model's requests: "
             f"{SUMMARY['job_start_gaps_s']}); Claude Code's debug log says which hook held it")
    res = {}
    for i, n in enumerate(("intr", "cont", "susp")):
        reqs = s.api.main_requests()
        trs = tool_results(reqs[n0 + 1 + i]) if len(reqs) > n0 + 1 + i else []
        text = result_text(trs[-1]) if trs else ""
        j = text.find("{")
        try:
            res[n] = json.loads(text[j:]) if j >= 0 else {"raw": text}
        except ValueError:
            res[n] = {"raw": text}
    gj = {n: str((r.get("job") or {}).get("gateway_job") or "") for n, r in res.items()}
    check("job_start with the gateway running: each job is the gateway's (supervisor gateway, a gateway job id)",
          all(r.get("supervisor") == "gateway" and gj[n].startswith("j_") for n, r in res.items()),
          json.dumps({n: {k: r.get(k) for k in ("ok", "supervisor", "reason", "gateway")} for n, r in res.items()}))
    mine = {r.get("name"): r for r in registry(home).values()}
    check("...checkpoint's registry: owner gateway with the gateway's id and the policy; no process of its own runner",
          all(mine.get(n, {}).get("owner") == "gateway" and mine[n].get("gateway_job") == gj[n]
              and mine[n].get("on_pause") == POLICY[n] for n in POLICY)
          and not [c for _p, _s, c in ps_rows() if "checkpoint-job-" in c and any(n in c for n in POLICY)])
    check("...the three run (in the gateway's managed bash)",
          wait(s, lambda: all(proc(m) for m in MARK.values()), 30))
    started = [r for r in records(home) if r["kind"] == "job.started" and (r.get("data") or {}).get("supervisor") ==
               "gateway"]
    check("...the journal records each job.started with the gateway's job id and its idempotency key",
          {(r["data"].get("gateway_job"), bool(r["data"].get("idempotency_key"))) for r in started}
          >= {(gj[n], True) for n in POLICY})
    s.pump(2)
    # pause: /check-pause, immediate; the gateway applies each policy
    t0 = type_cmd(s, "/check-pause")
    acked = wait(s, lambda: "paused this session" in s.all_text(), 15, 0.05)
    SUMMARY["ack_s"] = round(time.time() - t0, 2)
    check(f"/check-pause: acknowledged at once ({SUMMARY['ack_s']} s)", acked and SUMMARY["ack_s"] < 3)
    told = wait(s, lambda: any(r["kind"] == "gateway.pause" and (r.get("data") or {}).get("ok")
                               for r in records(home)), 30, 0.5)
    gp = next((r["data"] for r in records(home) if r["kind"] == "gateway.pause" and r["data"].get("ok")), {})
    outc = {o.get("job"): (o.get("policy"), o.get("outcome") or "") for o in gp.get("outcomes") or []}
    check("...the gateway was told (journal gateway.pause): each job's policy and what actually happened",
          told and outc.get(gj["intr"], ("", ""))[0] == "interrupt" and "interrupted" in outc[gj["intr"]][1]
          and outc.get(gj["cont"], ("", ""))[0] == "continue" and "still running" in outc[gj["cont"]][1]
          and outc.get(gj["susp"], ("", ""))[0] == "suspend" and "suspended" in outc[gj["susp"]][1],
          json.dumps(gp)[:500])
    shown = wait(s, lambda: has(s, "on_pause interrupt: interrupted") and has(s, "on_pause suspend: suspended"), 20)
    check("...and the person sees it (the ack, or the note right after it): interrupted, still running, suspended",
          shown and has(s, "on_pause continue: still running"))
    check("...interrupt: the job's process is gone (SIGINT)", wait(s, lambda: proc(MARK["intr"]) is None, 15))
    check("...continue: it runs on", proc(MARK["cont"]) is not None)
    check("...suspend: its process is stopped (state T)",
          wait(s, lambda: (proc(MARK["susp"]) or (0, ""))[1].startswith("T"), 15), str(proc(MARK["susp"])))
    # /check:status while paused, typed: the mod answers it without a model turn (its Python runs under
    # Claude Code, so the gateway's contract plane answers it; a process outside the session is refused).
    type_cmd(s, "/check:status")
    # Its output is long and the top scrolls away in the TUI; the recovery summary's
    # "Jobs & gateway" section at its end has each job with the gateway's id and the last
    # state the gateway reported.
    check("/check:status while paused shows each job with the gateway's job id and the state the gateway reported "
          "(interrupt: exited by SIGINT, suspend: suspended) and the pause's outcomes",
          wait(s, lambda: has(s, f"the gateway, job {gj['susp']}") and has(s, "last reported suspended by the gateway")
               and has(s, f"the gateway, job {gj['intr']}")
               and has(s, "last reported exited (signal INT) by the gateway")
               and has(s, f"gateway pause"), 30), s.all_text()[-1500:])
    with contextlib.suppress(OSError):
        (s.root / "screen-after-status.txt").write_text(s.all_text())
    # resume: a typed message
    s.api.script(["Resuming the task."])
    n0 = len(s.api.main_requests())
    s.submit("ok, carry on")
    resumed = wait(s, lambda: "resumed this session (reconciled first)" in s.all_text(), 30)
    wait_main(s, n0 + 1, 60)
    rq = request_text(s.api.main_requests()[-1]) if len(s.api.main_requests()) > n0 else ""
    check("a typed message resumes; the resume context (what the model gets) says what the gateway did on resume",
          resumed and "<checkpoint-resume>" in rq and "Gateway jobs on resume:" in rq and "resumed (SIGCONT)" in rq,
          rq[rq.find("Gateway"):][:400] if "Gateway" in rq else rq[:300])
    check("...the suspended job runs again (state no longer T); the continue job still runs",
          wait(s, lambda: not (proc(MARK["susp"]) or (0, "T"))[1].startswith("T"), 15)
          and proc(MARK["cont"]) is not None, str(proc(MARK["susp"])))
    check("...the journal records gateway.resume", any(r["kind"] == "gateway.resume" and r["data"].get("ok")
                                                       for r in records(home)))
    s.pump(2)
    # cancel: /check-cancel, immediate; the task's live gateway jobs stopped through the gateway
    type_cmd(s, "/check-cancel")
    canceled = wait(s, lambda: any(r["kind"] == "task.cancel" for r in records(home)), 60, 0.5)
    tc = next((r["data"] for r in records(home) if r["kind"] == "task.cancel"), {})
    mine = {r.get("name"): r for r in registry(home).values()}
    per = {o.get("job"): (o.get("supervisor"), o.get("outcome")) for o in tc.get("jobs") or []}
    ids = {n: mine[n]["id"] for n in POLICY if n in mine}
    check("/check-cancel: the task's live gateway jobs (continue, suspend) stopped through the gateway; task.cancel "
          "says so per job", canceled and per.get(ids.get("cont")) == ("gateway", "stopped")
          and per.get(ids.get("susp")) == ("gateway", "stopped"), json.dumps(tc)[:500])
    check("...their processes are gone", wait(s, lambda: proc(MARK["cont"]) is None and proc(MARK["susp"]) is None,
                                              20))
    check("...and the person is told (stopped through the gateway)",
          wait(s, lambda: has(s, "stopped through the gateway"), 20))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
