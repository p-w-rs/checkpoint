#!/usr/bin/env python3
"""Brief 4 and 14.2 in the real TUI: checkpoint alone, the gateway alone, both,
either disabled, an incompatible contract version and upgraded versions, all
installed the normal way.

    pixi exec --spec python=3.12 --spec pyte python tests/acceptance_independence.py \\
        --tmp DIR [--keep] [--pixi-cache DIR] [--gateway DIR] [--only ckalone,gwalone,both,ckoff,gwoff,incompat,upgrade]

Scratch Claude configs, plugins installed from local git-source marketplaces
(tests/tui install.Hermetic; the gateway through the harness's e2e route,
checkpoint as check@checkpoint), runtimes built before the sessions, the
harness's fake Messages API, bypassPermissions. "The model" is scripted: a
Bash command that says whether it ran in the gateway's managed bash, a
checkpoint `job_start` (which supervisor took it: checkpoint's registry), a
gateway `job_start` (the gw tool). checkpoint's view of the gateway is read
with its own CLI (`env.sh run gateway`, what /check:status and /check:doctor
print) under the session's environment.

  ckalone   only checkpoint installed: records, says the gateway is not
            available and why, runs jobs in its own runner (its limits said)
  gwalone   only the gateway installed: managed bash, a gw job runs, its
            daemon serves the contract; no checkpoint anywhere
  both      both: checkpoint sees the gateway (contract v1, features) and
            hands its job to the gateway (owner gateway)
  ckoff     both installed, checkpoint disabled: the gateway works alone
            (managed bash, a gw job), no checkpoint status line, nothing
            journaled for that session; enabled again: recording
  gwoff     both installed, the gateway disabled: (a) its daemon still running
            from the earlier sessions (it idles for 30 min): what checkpoint
            does with it; (b) the daemon stopped: checkpoint says not
            available, runs its job itself, Bash runs unmanaged; enabled again
  incompat  the gateway updated to a build whose contract is v2 only
            (CONTRACT_VERSIONS = [2]): the gateway works, checkpoint says
            "gateway contract mismatch (has 2, needs 1)" and runs its job itself
  upgrade   the gateway updated again to a compatible newer version and
            checkpoint to a newer version: checkpoint sees the new gateway and
            delegates again

Prints one line per check and a JSON summary to
--tmp/acceptance-independence.json; exits non-zero on any failure.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
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
from acceptance_gateway import (offered_or_deferred, prebuild_checkpoint, prebuild_gateway, records,  # noqa: E402
                                registry, tools_offered, wait, wait_main)

RESULTS: list[dict] = []
NOTES: list[str] = []
SUMMARY: dict = {}
CK_JOB = "mcp__plugin_check_jobs__job_start"
GW_JOB = "mcp__plugin_gate_gw__job_start"
MARKS: list[str] = []


def check(name: str, ok, detail: str = "") -> bool:
    ok = bool(ok)
    RESULTS.append({"name": name, "ok": ok, **({"detail": detail[:800]} if detail and not ok else {})})
    print(f"  {'ok  ' if ok else 'FAIL'} {name}", flush=True)
    if detail and not ok:
        print("       (" + detail[:800].replace("\n", " | ") + ")", flush=True)
    return ok


def note(text: str) -> None:
    NOTES.append(text)
    print(f"  note {text}", flush=True)


def ps_cmds() -> list[tuple[int, str]]:
    out = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,command="], capture_output=True, text=True).stdout
    rows = []
    for ln in out.splitlines():
        pid, _, cmd = ln.strip().partition(" ")
        if pid.isdigit():
            rows.append((int(pid), cmd.strip()))
    return rows


def running(marker: str) -> bool:
    return any(cmd == marker for _p, cmd in ps_cmds())


def kill_marks() -> None:
    for pid, cmd in ps_cmds():
        if cmd in MARKS:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


def mark(n: int) -> str:
    m = f"sleep {7000 + n}"
    MARKS.append(m)
    return m


class Box:
    """One scratch config with the plugins it holds."""

    def __init__(self, root: Path, name: str, env: dict, gateway_src: Path | None, checkpoint: bool):
        self.root, self.name, self.env = root, name, env
        self.h = install.Hermetic(root / name, extra_env=env)
        self.h.__enter__()
        self.gi = self.h.install_gateway_e2e(gateway_src) if gateway_src else None
        self.p = self.h.install_from(REPO, repo="checkpoint", plugin="check") if checkpoint else None
        self.cfg = self.h.inst.config_dir
        self.home = safety.state_root_for(self.cfg) / ".local" / "state" / "checkpoint"
        self.proj = root / f"{name}-proj"
        self.proj.mkdir()
        self.n = 0

    def build(self) -> bool:
        ok = True
        if self.gi:
            g_ok, d = prebuild_gateway(self.root, self.h, self.gi, self.env)
            ok &= check(f"[{self.name}] the gateway's env is built before the sessions", g_ok, d)
        if self.p:
            c_ok, d = prebuild_checkpoint(self.root, self.h, self.p, self.env)
            ok &= check(f"[{self.name}] checkpoint's runtime is built before the sessions", c_ok, d)
        return ok

    def session(self, label: str) -> driver.Session:
        self.n += 1
        extra = dict(self.env)
        if self.gi:
            # checkpoint looks where the gateway's daemon really is, also while the gateway plugin is
            # disabled (in real use: ~/.config/gateway/run/contract.json; here the e2e home's)
            extra.update({k: v for k, v in self.gi.env().items() if k.startswith("CHECKPOINT_")})
        return driver.Session(self.root / f"{self.name}-s{self.n}-{label}", self.proj, config_dir=self.cfg,
                              extra_env=extra, permission_mode="bypassPermissions", debug_file=True,
                              cols=170, rows=50)

    def latest_sid(self) -> str:
        rts = sorted((self.home / "rt").glob("*"), key=lambda p: p.stat().st_mtime) if (self.home / "rt").is_dir() else []
        return rts[-1].name if rts else ""

    def ck_env(self, s: driver.Session | None = None) -> dict:
        e = dict(os.environ)
        e.update({"CHECKPOINT_HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.cfg)})
        # Never the real ~/.config/gateway, also without a session at hand: the scratch contract and env
        # file sessions on this config get (the gateway e2e home's when the gateway is installed).
        st = safety.state_env(safety.state_root_for(self.cfg))
        keep = ("CHECKPOINT_GATEWAY_CONTRACT", "CHECKPOINT_GATEWAY_ENV")
        e.update({k: st[k] for k in keep})
        if self.gi:
            e.update({k: v for k, v in self.gi.env().items() if k in keep})
        if s is not None:
            for k in ("CHECKPOINT_GATEWAY_CONTRACT", "CHECKPOINT_GATEWAY_ENV"):
                if k in s.env:
                    e[k] = s.env[k]
        if self.p:
            e["CLAUDE_PLUGIN_DATA"] = str(self.p.data_path)
        return e

    def ck_cli(self, s: driver.Session, *args: str) -> str:
        p = self.h.inst.plugin("check@checkpoint")
        if p.cache_path is None:
            return ""
        r = subprocess.run(["sh", str(Path(p.cache_path) / "hooks" / "env.sh"), "run", *args],
                           env=self.ck_env(s), capture_output=True, text=True, timeout=60)
        return r.stdout + r.stderr

    def close(self) -> None:
        """Remove the gateway's run dir (Hermetic.close): its daemon notices the socket is gone at its
        next tick, stops its tmux server and exits (the documented way; a SIGTERM would leave the
        tmux server behind, unreachable once the run dir is gone)."""
        self.h.close()
        if self.gi:
            end = time.time() + 45
            while time.time() < end and any(str(self.gi.root) in cmd for _p, cmd in ps_cmds()):
                time.sleep(0.5)
            left = [cmd[:120] for _p, cmd in ps_cmds() if str(self.gi.root) in cmd]
            if left:
                note(f"[{self.name}] still running 45 s after the gateway's run dir was removed: {left[:3]}")


def offer(s: driver.Session, tool: str, tries: int = 6) -> bool:
    """Wait until the model is offered `tool` (MCP servers connect in the background)."""
    for i in range(tries):
        n0 = len(s.api.main_requests())
        s.api.script([f"ready {i}"])
        s.submit(f"tools there? ({i})")
        if wait_main(s, n0 + 1, 60) and offered_or_deferred(s.api.main_requests()[-1], tool):
            return True
        s.pump(4)
    return False


def managed_probe(s: driver.Session, proj: Path, tag: str) -> str:
    f = proj / f"m-{tag}.txt"
    n0 = len(s.api.main_requests())
    s.api.script([{"tool": "Bash", "input": {
        "command": f"printf '%s|%s' \"${{GW_MANAGED_DIR:+managed}}\" \"$BASH_VERSION\" > m-{tag}.txt",
        "description": "probe"}}, "probed"])
    s.submit(f"probe {tag}")
    wait(s, lambda: f.exists(), 90)
    wait_main(s, n0 + 2, 60)
    return f.read_text() if f.exists() else ""


def ck_view(s: driver.Session, box: Box, tag: str, *args: str) -> str:
    """checkpoint's CLI (`env.sh run ARGS`) run by a scripted Bash call inside the session, so it descends
    from the session's Claude Code the way /check:status and the jobs server do: the gateway's daemon
    serves a caller by that ancestry (it refuses the test process, so `box.ck_cli` can't tell whether it
    serves the session). The checkpoint variables are named on the command line: the gateway's managed bash
    doesn't hand the session's environment on, and the real state and contract must never be read."""
    p = box.h.inst.plugin("check@checkpoint")
    if p.cache_path is None:
        return ""
    out = box.proj / f"ckview-{tag}.txt"
    e = box.ck_env(s)
    envs = " ".join(f"{k}={shlex.quote(e[k])}" for k in ("CHECKPOINT_HOME", "CHECKPOINT_GATEWAY_CONTRACT",
                                                         "CHECKPOINT_GATEWAY_ENV", "CLAUDE_PLUGIN_DATA",
                                                         "CLAUDE_CONFIG_DIR") if e.get(k))
    if "CHECKPOINT_HOME=" not in envs or "CHECKPOINT_GATEWAY_CONTRACT=" not in envs:
        return "(not run: the hermetic checkpoint variables are missing)"
    cmd = (f"env {envs} sh {shlex.quote(str(Path(p.cache_path) / 'hooks' / 'env.sh'))} run "
           f"{' '.join(shlex.quote(a) for a in args)} > {shlex.quote(str(out))} 2>&1")
    n0 = len(s.api.main_requests())
    s.api.script([{"tool": "Bash", "input": {"command": cmd, "description": "checkpoint's view"}}, f"viewed {tag}"])
    s.submit(f"checkpoint's view {tag}")
    wait(s, lambda: out.exists() and out.stat().st_size > 0, 90)
    wait_main(s, n0 + 2, 60)
    return out.read_text() if out.exists() else ""


def ck_job(s: driver.Session, box: Box, n: int) -> dict | None:
    m = mark(n)
    before = set(registry(box.home))
    n0 = len(s.api.main_requests())
    s.api.script([{"tool": CK_JOB, "input": {"name": f"ind{n}", "command": m, "redo_hint": "rerun"}}, "job started"])
    s.submit(f"start job {n}")
    wait_main(s, n0 + 2, 120)
    wait(s, lambda: set(registry(box.home)) - before, 30)
    new = [r for k, r in registry(box.home).items() if k not in before]
    return new[-1] if new else None


def gw_job(s: driver.Session, n: int) -> bool:
    m = mark(n)
    n0 = len(s.api.main_requests())
    s.api.script([{"tool": GW_JOB, "input": {"command": m}}, "gw job started"])
    s.submit(f"start gw job {n}")
    wait_main(s, n0 + 2, 120)
    return wait(s, lambda: running(m), 30)


def gated_pids(box: Box) -> list[int]:
    if not box.gi:
        return []
    return [pid for pid, cmd in ps_cmds() if "gated" in cmd and str(box.gi.root) in cmd]


def stop_gated(box: Box) -> bool:
    for pid in gated_pids(box):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    end = time.time() + 15
    while time.time() < end and gated_pids(box):
        time.sleep(0.3)
    return not gated_pids(box)


def new_sessions(box: Box, before: set[str]) -> set[str]:
    sp = {p.name for p in (box.home / "spool").glob("*")} if (box.home / "spool").is_dir() else set()
    js = {r.get("session") for r in records(box.home) if r.get("kind") == "session.start"}
    return (sp | js) - before


def known_sessions(box: Box) -> set[str]:
    return new_sessions(box, set())


def patched_gateway(src: Path, dest: Path) -> Path:
    install.snapshot_tree(src, dest)
    init = dest / "plugins" / "gateway" / "src" / "gateway" / "daemon" / "__init__.py"
    t = init.read_text()
    t2 = t.replace("CONTRACT_VERSIONS = [1]", "CONTRACT_VERSIONS = [2]")   # the same contract id, v2 only
    if t2 == t:
        raise RuntimeError("the contract constants were not found in daemon/__init__.py")
    init.write_text(t2)
    return dest


def patched_checkpoint(src: Path, dest: Path, version: str) -> Path:
    install.snapshot_tree(src, dest)
    pj = dest / "plugins" / "checkpoint" / ".claude-plugin" / "plugin.json"
    d = json.loads(pj.read_text())
    d["version"] = version
    pj.write_text(json.dumps(d, indent=2) + "\n")
    init = dest / "plugins" / "checkpoint" / "src" / "checkpoint" / "__init__.py"
    init.write_text(re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', init.read_text()))
    return dest


# ---------------------------------------------------------------------------


def part_ckalone(root: Path, env: dict) -> None:
    print("ckalone: only checkpoint installed", flush=True)
    box = Box(root, "ckalone", env, None, True)
    try:
        if not box.build():
            return
        s = box.session("alone")
        try:
            s.wait_ready(120)
            check("[ckalone] checkpoint records (status line)", wait(s, lambda: "check: recording" in s.text(), 60))
            gw = box.ck_cli(s, "gateway")
            SUMMARY["ckalone_gateway"] = gw.splitlines()[:2]
            check("[ckalone] checkpoint says the gateway is not available and why, and where jobs run",
                  "Gateway: not available" in gw and "own detached runner" in gw, gw[:400])
            if check("[ckalone] its job tool is offered", offer(s, CK_JOB)):
                rec = ck_job(s, box, 1)
                check("[ckalone] job_start runs in checkpoint's own runner (registry owner checkpoint), the job runs",
                      rec is not None and rec.get("owner", "checkpoint") == "checkpoint" and wait(
                          s, lambda: running(MARKS[-1]), 20), json.dumps(rec)[:300])
            m = managed_probe(s, box.proj, "ckalone")
            check("[ckalone] Bash runs (no gateway: the user's own shell)", m.startswith("|"), m)
        finally:
            s.close()
    finally:
        box.close()


def part_gwalone(root: Path, env: dict, gsrc: Path) -> None:
    print("gwalone: only the gateway installed", flush=True)
    box = Box(root, "gwalone", env, gsrc, False)
    try:
        if not box.build():
            return
        s = box.session("alone")
        try:
            s.wait_ready(120)
            contract = box.gi.gateway_home / "run" / "contract.json"
            check("[gwalone] the gateway's daemon is up (contract.json)", wait(s, contract.is_file, 120, 0.5))
            m = managed_probe(s, box.proj, "gwalone")
            check("[gwalone] Bash runs in the gateway's managed bash", m.startswith("managed|5."), m)
            if check("[gwalone] the gw job tool is offered", offer(s, GW_JOB)):
                check("[gwalone] a gw job_start runs (the gateway supervises it alone)", gw_job(s, 2))
            check("[gwalone] no checkpoint status line, no checkpoint state anywhere",
                  "check:" not in s.text() and not box.home.exists())
            dbg = s.debug_path.read_text(errors="replace") if s.debug_path.exists() else ""
            check("[gwalone] no gateway hook failed in the debug log (it doesn't need checkpoint)",
                  not re.search(r"plugins/cache/gateway/gate/[^\n]*(exit code [12]|failed)", dbg))
        finally:
            s.close()
    finally:
        box.close()


def part_both_and_more(root: Path, env: dict, gsrc: Path, only: set[str]) -> None:
    box = Box(root, "both", env, gsrc, True)
    try:
        if not box.build():
            return
        pid_c, pid_g = "check@checkpoint", box.gi.plugin.id
        if "both" in only:
            print("both: both installed", flush=True)
            s = box.session("both")
            try:
                s.wait_ready(120)
                contract = box.gi.gateway_home / "run" / "contract.json"
                wait(s, contract.is_file, 120, 0.5)
                wait(s, lambda: "check: recording" in s.text(), 60)
                gw = ck_view(s, box, "both", "gateway")
                SUMMARY["both_gateway"] = gw.splitlines()[:1]
                check("[both] checkpoint sees the gateway serving this session (available, contract v1, jobs feature; "
                      "asked from inside the session)",
                      "Gateway: available" in gw and "contract v1" in gw and "new jobs run there" in gw, gw[:300])
                out = box.ck_cli(s, "gateway")
                SUMMARY["both_gateway_outside"] = out.splitlines()[:1]
                check("[both] ...asked from outside any session (the test process), it says the daemon doesn't serve "
                      "that caller instead of 'available'", "doesn't serve this session" in out
                      and "new jobs run there" not in out, out[:300])
                if check("[both] checkpoint's job tool is offered", offer(s, CK_JOB)):
                    rec = ck_job(s, box, 3)
                    check("[both] checkpoint's job_start is handed to the gateway (owner gateway, a j_ id)",
                          rec is not None and rec.get("owner") == "gateway"
                          and str(rec.get("gateway_job", "")).startswith("j_"), json.dumps(rec)[:300])
                m = managed_probe(s, box.proj, "both")
                check("[both] Bash runs in the managed bash", m.startswith("managed|5."), m)
            finally:
                s.close()
            kill_marks()
        if "ckoff" in only:
            print("ckoff: checkpoint disabled", flush=True)
            r = box.h.inst.disable(pid_c, check=False)
            check("[ckoff] `claude plugin disable check@checkpoint`", r.ok, str(r)[:300])
            before = known_sessions(box)
            s = box.session("ckoff")
            try:
                s.wait_ready(120)
                m = managed_probe(s, box.proj, "ckoff")
                check("[ckoff] the gateway works alone: managed bash", m.startswith("managed|5."), m)
                if offer(s, GW_JOB):
                    check("[ckoff] ...and a gw job runs", gw_job(s, 4))
                check("[ckoff] no checkpoint status line in the session", "check:" not in s.text())
            finally:
                s.close()
            check("[ckoff] nothing was spooled or journaled by checkpoint for that session",
                  not new_sessions(box, before), str(new_sessions(box, before)))
            r = box.h.inst.enable(pid_c, check=False)
            s = box.session("ckon")
            try:
                s.wait_ready(120)
                check("[ckoff] enabled again: checkpoint records", r.ok and wait(
                    s, lambda: "check: recording" in s.text(), 60))
            finally:
                s.close()
            kill_marks()
        if "gwoff" in only:
            print("gwoff: the gateway disabled", flush=True)
            r = box.h.inst.disable(pid_g, check=False)
            check("[gwoff] `claude plugin disable gate@gateway`", r.ok, str(r)[:300])
            alive = bool(gated_pids(box))
            SUMMARY["gwoff_daemon_alive_after_disable"] = alive
            s = box.session("gwoff-daemon")
            try:
                s.wait_ready(120)
                wait(s, lambda: "check: recording" in s.text(), 60)
                gw = ck_view(s, box, "gwoff-a", "gateway")
                SUMMARY["gwoff_a_gateway"] = gw.splitlines()[:1]
                m = managed_probe(s, box.proj, "gwoff-a")
                check("[gwoff a] Bash runs unmanaged (the gateway's mod is off)", m.startswith("|"), m)
                if offer(s, CK_JOB):
                    rec = ck_job(s, box, 5)
                    SUMMARY["gwoff_a_job"] = rec
                    owner = (rec or {}).get("owner")
                    check("[gwoff a] checkpoint's job still starts somewhere and runs", rec is not None and wait(
                        s, lambda: running(MARKS[-1]), 20), json.dumps(rec)[:300])
                    if alive and owner == "gateway":
                        note("gwoff a: with the gateway plugin disabled but its daemon still alive from earlier "
                             "sessions (idle exit 30 min), checkpoint handed its job to that daemon (owner gateway)")
                    else:
                        note(f"gwoff a: daemon alive after disable: {alive}; checkpoint's job owner: {owner}; "
                             f"checkpoint said: {gw.splitlines()[:1]}")
                    # What checkpoint says must match where its job went (it said "available: new jobs run
                    # there" while the daemon refused the session and the job ran in its own runner).
                    said_gw = "Gateway: available" in gw and "new jobs run there" in gw
                    check("[gwoff a] checkpoint's word on the gateway matches where its job ran (owner "
                          f"{owner}): 'available' only when the gateway took it",
                          (owner == "gateway") == said_gw and (owner == "gateway" or "Gateway: available" not in gw),
                          gw[:300])
                    if alive and owner != "gateway":
                        check("[gwoff a] ...the daemon still runs but doesn't serve this session: said so, jobs in "
                              "checkpoint's own runner", "doesn't serve this session" in gw
                              and "own detached runner" in gw, gw[:300])
            finally:
                s.close()
            kill_marks()
            check("[gwoff b] the gateway's daemon is stopped (SIGTERM)", stop_gated(box))
            contract = box.gi.gateway_home / "run" / "contract.json"
            check("[gwoff b] ...and removed its contract.json", not contract.exists())
            s = box.session("gwoff-nodaemon")
            try:
                s.wait_ready(120)
                check("[gwoff b] checkpoint records with the gateway disabled",
                      wait(s, lambda: "check: recording" in s.text(), 60))
                gw = box.ck_cli(s, "gateway")
                check("[gwoff b] checkpoint says the gateway is not available, jobs in its own runner",
                      "Gateway: not available" in gw and "own detached runner" in gw, gw[:300])
                if offer(s, CK_JOB):
                    rec = ck_job(s, box, 6)
                    check("[gwoff b] its job runs in its own runner", rec is not None and rec.get("owner", "checkpoint")
                          == "checkpoint" and wait(s, lambda: running(MARKS[-1]), 20), json.dumps(rec)[:300])
                check("[gwoff b] the disabled gateway's daemon was not started again", not gated_pids(box))
            finally:
                s.close()
            kill_marks()
            r = box.h.inst.enable(pid_g, check=False)
            s = box.session("gwon")
            try:
                s.wait_ready(120)
                m = managed_probe(s, box.proj, "gwon")
                check("[gwoff] enabled again: the managed bash is back", r.ok and m.startswith("managed|5."), m)
            finally:
                s.close()
            kill_marks()
        if "incompat" in only:
            print("incompat: the gateway updated to a contract-v2-only build", flush=True)
            stop_gated(box)
            src2 = patched_gateway(gsrc, root / "gw-src-v2")
            gi2 = box.h.update_gateway_e2e(src2, version="0.9.2000")
            check("[incompat] the gateway updates to 0.9.2000 (contract v2 only)", gi2.plugin.version == "0.9.2000")
            box.gi = gi2
            s = box.session("incompat")
            try:
                s.wait_ready(120)
                contract = gi2.gateway_home / "run" / "contract.json"
                up = wait(s, lambda: contract.is_file() and json.loads(contract.read_text()).get("versions") == [2],
                          120, 0.5)
                check("[incompat] its daemon serves contract v2 only (contract.json versions [2])", up)
                m = managed_probe(s, box.proj, "incompat")
                check("[incompat] the gateway itself works (managed bash)", m.startswith("managed|5."), m)
                gw = box.ck_cli(s, "gateway")
                SUMMARY["incompat_gateway"] = gw.splitlines()[:1]
                check("[incompat] checkpoint says: gateway contract mismatch (has 2, needs 1); works alone",
                      "gateway contract mismatch (has 2, needs 1)" in gw, gw[:300])
                if offer(s, CK_JOB):
                    rec = ck_job(s, box, 7)
                    check("[incompat] checkpoint runs its job in its own runner", rec is not None
                          and rec.get("owner", "checkpoint") == "checkpoint", json.dumps(rec)[:300])
                st = box.ck_cli(s, "status", "--session", box.latest_sid())
                SUMMARY["incompat_status_gateway"] = [ln for ln in st.splitlines() if "ateway" in ln][:3]
                check("[incompat] /check:status names the mismatch",
                      "contract mismatch" in st or "Gateway: not available" in st, "\n".join(
                          ln for ln in st.splitlines() if "ateway" in ln)[:400])
            finally:
                s.close()
            kill_marks()
        if "upgrade" in only:
            print("upgrade: compatible newer gateway, newer checkpoint", flush=True)
            stop_gated(box)
            gi3 = box.h.update_gateway_e2e(gsrc, version="0.9.3000")
            ck2 = patched_checkpoint(REPO, root / "ck-src-0.7.3000", "0.7.3000")
            p3 = box.h.update_from(ck2, repo="checkpoint", plugin="check")
            check("[upgrade] gate 0.9.3000 and check 0.7.3000 installed",
                  gi3.plugin.version == "0.9.3000" and p3.version == "0.7.3000")
            box.gi = gi3
            s = box.session("upgrade")
            try:
                s.wait_ready(120)
                contract = gi3.gateway_home / "run" / "contract.json"
                wait(s, lambda: contract.is_file() and json.loads(contract.read_text()).get("versions") == [1], 120, 0.5)
                wait(s, lambda: "check: recording" in s.text(), 60)
                gw = ck_view(s, box, "upgrade", "gateway")
                SUMMARY["upgrade_gateway"] = gw.splitlines()[:1]
                check("[upgrade] checkpoint sees the upgraded gateway (available, 0.9.3000, contract v1)",
                      "Gateway: available (0.9.3000" in gw, gw[:300])
                if offer(s, CK_JOB):
                    rec = ck_job(s, box, 8)
                    check("[upgrade] and hands its job to it again (owner gateway)", rec is not None
                          and rec.get("owner") == "gateway", json.dumps(rec)[:300])
            finally:
                s.close()
            kill_marks()
    finally:
        box.close()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tmp", default=tempfile.gettempdir())
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--pixi-cache", default=None)
    ap.add_argument("--gateway", default=str(REPO.parent / "gateway"))
    ap.add_argument("--only", default="ckalone,gwalone,both,ckoff,gwoff,incompat,upgrade")
    a = ap.parse_args(argv)
    before = safety.real_settings_fingerprint()
    root = Path(tempfile.mkdtemp(prefix="ck-ind-", dir=a.tmp))
    safety.check_scratch(root, "acceptance root")
    print(f"acceptance (independence): root {root}", flush=True)
    env = {"PIXI_CACHE_DIR": a.pixi_cache or str(root / "pixi-cache")}
    only = set(a.only.split(","))
    gsrc = Path(a.gateway).resolve()
    t0 = time.time()
    try:
        if "ckalone" in only:
            part_ckalone(root, env)
        if "gwalone" in only:
            part_gwalone(root, env, gsrc)
        if only & {"both", "ckoff", "gwoff", "incompat", "upgrade"}:
            part_both_and_more(root, env, gsrc, only)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(f"the run went to its end ({type(e).__name__}: {str(e)[:300]})", False)
    finally:
        kill_marks()
        driver._close_all()
        for pid, cmd in ps_cmds():
            if str(root) in cmd and "gated" in cmd:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM)
    time.sleep(1.0)
    left = [f"{pid} {cmd[:150]}" for pid, cmd in ps_cmds() if (str(root) in cmd or cmd in MARKS) and "ps -A" not in cmd]
    check("no process left naming the scratch root or a job's marker", not left, "\n".join(left))
    check("real ~/.config/claude settings and plugin registry unchanged", before == safety.real_settings_fingerprint())
    failed = [r for r in RESULTS if not r["ok"]]
    summary = {"when": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "seconds": round(time.time() - t0),
               "claude": subprocess.run([driver.find_claude(), "--version"], capture_output=True,
                                        text=True).stdout.strip(),
               "passed": len(RESULTS) - len(failed), "failed": len(failed), "checks": RESULTS, "notes": NOTES,
               **SUMMARY}
    out = Path(a.tmp) / "acceptance-independence.json"
    out.write_text(json.dumps(summary, indent=1, default=str))
    print(f"\n{summary['passed']} passed, {summary['failed']} failed in {summary['seconds']} s; {out}")
    if not a.keep:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
