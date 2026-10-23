#!/usr/bin/env python3
"""Brief 5 and 14.11 in the real TUI on installed copies: concurrent sessions,
duplicate events, a full disk, hook failures and timeouts, a plugin upgrade
under live work, and the startup and recording overhead.

    pixi exec --spec python=3.12 --spec pyte python tests/acceptance_durability.py \\
        --tmp DIR [--keep] [--pixi-cache DIR] [--gateway DIR] \\
        [--only concurrent,duplicate,diskfull,timeout,upgrade,overhead]

Both plugins installed from local git-source marketplaces (tests/tui; the
gateway through the harness's e2e route, checkpoint as check@checkpoint),
runtimes built first, the harness's fake Messages API, bypassPermissions.

  concurrent two sessions on one config at once, each running Bash calls: every
             command ran (managed bash), the gateway's daemon held both
             sessions, checkpoint journaled two tasks, each readable with no
             problem (CRC, gap-free seq), no recording failure
  duplicate  (synthetic: the installed fast.sh fed by hand, a fake Claude pid)
             the same PostToolUse delivered twice and before its PreToolUse:
             one intent and one outcome in the journal
  diskfull   (macOS: a 4 MB disk image, hdiutil, no root) checkpoint's state on
             it, then the disk filled: the session goes on (not paused: fail
             open), the failure is shown (status line "recording FAILED" or the
             hook's message, journal-errors/spool-errors), nothing corrupt; space
             freed: recording resumes and the journal reads clean
  timeout    checkpoint's Python made to hang (CHECKPOINT_PYTHON, a 3 s hook
             deadline): not paused, a call goes ahead and the degraded mark is
             kept; paused, a call is denied (fail closed). The gateway's daemon
             SIGSTOPped: what a Bash call and a protected Read do, and how long
             they take; SIGCONT
  upgrade    a gateway job running, the gateway and checkpoint updated (each to
             <installed major.minor>.1000): a new session on the new versions
             still sees the job running and its journal goes on
  overhead   ten Bash calls in one turn with no plugin, checkpoint alone, and both:
             the per-call round trip (fake model + hooks + command) compared

Prints one line per check and a JSON summary to --tmp/acceptance-durability.json;
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
import acceptance_independence as AI  # noqa: E402
from acceptance_gateway import records, tools_offered, wait, wait_main  # noqa: E402

check, note = AI.check, AI.note
SUMMARY = AI.SUMMARY
GW_JOB = AI.GW_JOB


def bash(cmd: str, desc: str = "d") -> dict:
    return {"tool": "Bash", "input": {"command": cmd, "description": desc}}


def journal_view(box, task: str):
    """checkpoint's own reader (the installed copy's code) on one task."""
    code = ("import json,sys; sys.path.insert(0, sys.argv[1]); from checkpoint import journal; "
            "v = journal.read(sys.argv[2]); "
            "seqs = [r['seq'] for r in v.records]; "
            "print(json.dumps({'n': len(v.records), 'problems': [str(p) for p in v.problems], "
            "'gapfree': seqs == list(range(1, len(seqs) + 1))}))")
    cache = box.h.inst.plugin("check@checkpoint").cache_path
    py = sorted(Path(box.h.inst.plugin("check@checkpoint").data_path).glob("envs/*/.pixi/envs/default/bin/python3"))
    if not py or cache is None:
        return {}
    r = subprocess.run([str(py[-1]), "-I", "-c", code, str(Path(cache) / "src"), task], capture_output=True, text=True,
                       env={**box.ck_env(), "PATH": "/usr/bin:/bin"}, timeout=60)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"error": (r.stdout + r.stderr)[-300:]}


def ingest(box) -> None:
    cache = box.h.inst.plugin("check@checkpoint").cache_path
    for sd in sorted((box.home / "spool").glob("*")) if (box.home / "spool").is_dir() else []:
        envd = (box.home / "rt" / sd.name).read_text().strip() if (box.home / "rt" / sd.name).exists() else ""
        subprocess.run(["sh", str(Path(cache) / "hooks" / "env.sh"), "run", "ingest", sd.name],
                       env={**box.ck_env(), **({"CHECKPOINT_ENVD": envd} if envd else {})}, capture_output=True,
                       timeout=120)


def tasks_by_cwd(box) -> dict[str, str]:
    out = {}
    for r in records(box.home):
        if r.get("kind") == "session.start":
            out[(r.get("data") or {}).get("cwd", "")] = r.get("task", "")
    return out


# ---------------------------------------------------------------------------


def part_concurrent(root: Path, box) -> None:
    print("concurrent: two sessions at once on one config", flush=True)
    pa, pb = root / "conc-a", root / "conc-b"
    pa.mkdir()
    pb.mkdir()
    sa = driver.Session(root / "conc-sa", pa, config_dir=box.cfg, extra_env=dict(box.env),
                        permission_mode="bypassPermissions", debug_file=True, cols=160, rows=45, start=False)
    sb = driver.Session(root / "conc-sb", pb, config_dir=box.cfg, extra_env=dict(box.env),
                        permission_mode="bypassPermissions", debug_file=True, cols=160, rows=45, start=False)
    reg = box.gi.run / "gated-sessions.json"
    peak = 0
    try:
        sa.start()
        sb.start()
        sa.wait_ready(120)
        sb.wait_ready(120)
        for s, tag in ((sa, "a"), (sb, "b")):
            s.api.script([bash(f"printf '%s|%s\\n' {tag}{i} \"${{GW_MANAGED_DIR:+m}}\" >> out.txt; sleep 0.3")
                          for i in range(6)] + [f"{tag} done"])
        sa.submit("work a")
        sb.submit("work b")
        end = time.time() + 180
        while time.time() < end:
            sa.pump(0.2)
            sb.pump(0.2)
            with contextlib.suppress(Exception):
                peak = max(peak, sum(1 for x in json.loads(reg.read_text())["sessions"] if x.get("via") == "ui"))
            if "a done" in sa.all_text() and "b done" in sb.all_text():
                break
        la = (pa / "out.txt").read_text().splitlines() if (pa / "out.txt").exists() else []
        lb = (pb / "out.txt").read_text().splitlines() if (pb / "out.txt").exists() else []
        check("concurrent: both sessions ran all six commands, each in the managed bash",
              la == [f"a{i}|m" for i in range(6)] and lb == [f"b{i}|m" for i in range(6)], f"{la} {lb}")
        check("concurrent: the gateway's daemon held both sessions at once (UI plane registry)", peak >= 2, f"peak {peak}")
        check("concurrent: neither status line says recording FAILED",
              "recording FAILED" not in sa.all_text() and "recording FAILED" not in sb.all_text())
    finally:
        sa.close()
        sb.close()
    ingest(box)
    t = tasks_by_cwd(box)
    ta, tb = t.get(str(pa), ""), t.get(str(pb), "")
    va, vb = journal_view(box, ta), journal_view(box, tb)
    SUMMARY["concurrent_journals"] = {"a": va, "b": vb}
    check("concurrent: checkpoint journaled two separate tasks, each readable with no problem and gap-free",
          ta and tb and ta != tb and va.get("n", 0) > 6 and vb.get("n", 0) > 6 and not va.get("problems")
          and not vb.get("problems") and va.get("gapfree") and vb.get("gapfree"), json.dumps([ta, tb, va, vb])[:500])
    # (`.reserve.<session>`: the reserve a failure on a full disk would use, armed at every session start)
    errs = [p.name for d in ("journal-errors", "spool-errors") for p in (box.home / d).glob("*")
            if not p.name.startswith(".")] if box.home.exists() else []
    check("concurrent: no journal or spool error recorded", not errs, str(errs))


def part_duplicate(root: Path, box) -> None:
    print("duplicate: the same event delivered twice, out of order (synthetic, the installed fast.sh)", flush=True)
    p = box.h.inst.plugin("check@checkpoint")
    proj = root / "dup-proj"
    proj.mkdir()
    fake = subprocess.Popen(["sleep", "600"])
    sid = "dupdupdu-0000-4000-8000-000000000001"
    tp = root / "dup-transcript.jsonl"
    tp.write_text("")
    env = {**box.ck_env(), "CLAUDE_PLUGIN_ROOT": str(p.cache_path), "CLAUDE_PLUGIN_DATA": str(p.data_path),
           "CLAUDE_PID": str(fake.pid), "CHECKPOINT_GATEWAY_CONTRACT": str(root / "none.json")}
    base = {"session_id": sid, "transcript_path": str(tp), "cwd": str(proj)}

    def hook(ev: str, **kw) -> None:
        subprocess.run(["sh", str(Path(p.cache_path) / "hooks" / "fast.sh"), ev],
                       input=json.dumps({**base, "hook_event_name": ev, **kw}), capture_output=True, text=True,
                       timeout=60, env=env)
    try:
        hook("SessionStart", source="startup")
        hook("UserPromptSubmit", prompt="duplicate test", prompt_id="dup0")
        post = {"tool_name": "Bash", "tool_use_id": "toolu_dup_1", "tool_input": {"command": "echo dup"},
                "tool_response": {"stdout": "dup", "stderr": "", "interrupted": False}}
        hook("PostToolUse", **post)
        hook("PostToolUse", **post)
        hook("PreToolUse", tool_name="Bash", tool_use_id="toolu_dup_1", tool_input={"command": "echo dup"})
        hook("PreToolUse", tool_name="Bash", tool_use_id="toolu_dup_1", tool_input={"command": "echo dup"})
        # The call's result in the transcript, as Claude Code writes it: the `pause now` watchers the
        # late PreToolUse started end on it (the synthetic hooks run below the session that runs this
        # test, whose Claude Code they would otherwise wait for; their PostToolUse came first).
        with tp.open("a") as f:
            f.write(json.dumps({"type": "user", "sessionId": sid, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_dup_1", "content": "dup"}]}}) + "\n")
        subprocess.run(["sh", str(Path(p.cache_path) / "hooks" / "env.sh"), "run", "ingest", sid], env=env,
                       capture_output=True, timeout=120)
        recs = [r for r in records(box.home) if r.get("session") == sid]
        ops = [(r.get("kind"), r.get("op")) for r in recs if r.get("op") == "toolu_dup_1"]
        SUMMARY["duplicate_ops"] = ops
        check("duplicate: twice-delivered (and out-of-order) events journal one intent and one outcome",
              ops.count(("tool.intent", "toolu_dup_1")) == 1 and ops.count(("tool.outcome", "toolu_dup_1")) == 1,
              json.dumps(ops))
        task = next((r.get("task") for r in recs), "")
        v = journal_view(box, task)
        check("duplicate: that journal reads clean (no problem, gap-free)", v.get("n") and not v.get("problems")
              and v.get("gapfree"), json.dumps(v))
    finally:
        fake.kill()
        # reaped, not a zombie: `kill -0` succeeds on a zombie, and the watchers the out-of-order PreToolUse
        # started (its PostToolUse came first, so nothing removes their marker) wait for this "Claude" to go
        fake.wait(timeout=10)


def part_diskfull(root: Path, env: dict) -> None:
    print("diskfull: checkpoint's state on a small disk image, filled", flush=True)
    if sys.platform != "darwin":
        note("diskfull: macOS only here (hdiutil); on Linux it needs root for a tmpfs or loop device")
        return
    img, mnt = root / "full.dmg", root / "fullmnt"
    mnt.mkdir()
    r = subprocess.run(["hdiutil", "create", "-size", "4m", "-fs", "HFS+", "-volname", "ckfull", "-layout", "NONE",
                        str(img)], capture_output=True, text=True)
    if r.returncode != 0:
        check("diskfull: a 4 MB disk image is made (hdiutil create)", False, r.stderr[-300:])
        return
    r = subprocess.run(["hdiutil", "attach", "-nobrowse", "-noautoopen", "-mountpoint", str(mnt), str(img) +
                        ("" if img.suffix == ".dmg" else ".dmg")], capture_output=True, text=True)
    if r.returncode != 0:
        check("diskfull: the disk image is attached under the scratch root", False, r.stderr[-300:])
        return
    box = None
    try:
        box = AI.Box(root, "diskfull", env, None, True)
        if not box.build():
            return
        ckhome = mnt / "ckhome"
        ckhome.mkdir(mode=0o700)
        box.home = ckhome
        proj = box.proj
        s = driver.Session(root / "full-s1", proj, config_dir=box.cfg,
                           extra_env={**env, "CHECKPOINT_HOME": str(ckhome)},
                           permission_mode="bypassPermissions", debug_file=True, cols=160, rows=45, start=False)
        try:
            s.start()
            s.wait_ready(120)
            check("diskfull: checkpoint records with its state on the small disk", wait(s, lambda: "check: recording"
                                                                                       in s.text(), 60))
            s.api.script([bash("echo before > before.txt"), "before done"])
            s.submit("before the disk fills")
            wait(s, lambda: "before done" in s.all_text(), 60)
            filler = mnt / "filler"
            subprocess.run(["dd", "if=/dev/zero", f"of={filler}", "bs=64k"], capture_output=True)
            free = shutil.disk_usage(mnt).free
            SUMMARY["diskfull_free_after_fill"] = free
            s.api.script([bash(f"echo full{i} > full{i}.txt") for i in range(3)] + ["full done"])
            s.submit("while the disk is full")
            ran = wait(s, lambda: "full done" in s.all_text(), 90)
            check("diskfull: with the disk full the session goes on (not paused: the calls run)", ran and all(
                (proj / f"full{i}.txt").exists() for i in range(3)))
            s.pump(3)
            shown = wait(s, lambda: "recording FAILED" in s.all_text() or "couldn't record" in s.all_text(), 30)
            SUMMARY["diskfull_status"] = [ln for ln in s.lines() if "check:" in ln][:3]
            check("diskfull: the recording failure is shown (status line or the hook's message)", shown, s.text()[-500:])
            filler.unlink()
            s.api.script([bash("echo after > after.txt"), "after done"])
            s.submit("after the space is back")
            wait(s, lambda: "after done" in s.all_text(), 60)
            s.pump(2)
        finally:
            s.close()
        ingest(box)
        t = tasks_by_cwd(box)
        v = journal_view(box, t.get(str(proj), ""))
        SUMMARY["diskfull_journal"] = v
        errs = [p.name for d in ("journal-errors", "spool-errors") for p in (ckhome / d).glob("*")
                if not p.name.startswith(".")]
        SUMMARY["diskfull_errors"] = errs
        # The failure stays on record after the space came back (it used to leave nothing): its history
        # (.last) and, for lost spool records, a recording.gap record in the journal, named by /check:doctor.
        gaps = [r for r in records(ckhome) if r.get("kind") == "recording.gap"]
        SUMMARY["diskfull_gaps"] = [(r.get("data") or {}).get("recovered") for r in gaps]
        cache = box.h.inst.plugin("check@checkpoint").cache_path
        sid = next((p.name[:-5] for p in (ckhome / "spool-errors").glob("*.last")), "")
        doc = subprocess.run(["sh", str(Path(cache) / "hooks" / "env.sh"), "doctor", sid], capture_output=True,
                             text=True, timeout=60, env={**box.ck_env(), "CHECKPOINT_HOME": str(ckhome)}).stdout
        SUMMARY["diskfull_doctor"] = [ln for ln in doc.splitlines() if "spool:" in ln or "journal:" in ln][:4]
        check("diskfull: after the space came back the failure is still on record: its history (.last), a "
              "recording.gap record in the journal, and /check:doctor names it",
              any(e.endswith(".last") for e in errs) and bool(gaps)
              and "last recording failure (recovered" in doc, json.dumps({"errs": errs, "gaps": len(gaps),
                                                                       "doctor": SUMMARY["diskfull_doctor"]})[:400])
        # A write the full disk cut off mid-record leaves a torn tail that the next writer steps over (a new
        # segment, seq continued): readable, nothing lost or out of order. Anything else is a problem.
        torn_only = all("kind='torn'" in p and "fatal=False" in p for p in v.get("problems") or [])
        SUMMARY["diskfull_torn_tail"] = bool(v.get("problems")) and torn_only
        check("diskfull: after the space came back the journal reads clean (gap-free; at most a torn tail of a write "
              "the full disk cut off, stepped over) and holds the commands from before and after",
              v.get("n") and torn_only and v.get("gapfree")
              and any("after.txt" in json.dumps(r) for r in records(ckhome))
              and any("before.txt" in json.dumps(r) for r in records(ckhome)), json.dumps(v))
        lost = not any("full0.txt" in json.dumps(r) for r in records(ckhome))
        note(f"diskfull: the events of the full-disk window are {'lost (recorded as such)' if lost else 'in the journal'}; "
             f"errors recorded: {errs[:5]}")
    finally:
        if box:
            box.close()
        subprocess.run(["hdiutil", "detach", str(mnt), "-force"], capture_output=True)


def part_timeout(root: Path, box) -> None:
    print("timeout: checkpoint's Python hangs; the gateway's daemon stopped", flush=True)
    hang = root / "hang-python"
    hang.write_text("#!/bin/sh\nsleep 120\n")
    os.chmod(hang, 0o755)
    proj = root / "to-proj"
    proj.mkdir()
    s = driver.Session(root / "to-s1", proj, config_dir=box.cfg,
                       extra_env={**box.env, **{k: v for k, v in box.gi.env().items() if k.startswith("CHECKPOINT_")},
                                  "CHECKPOINT_PYTHON": str(hang), "CHECKPOINT_HOOK_DEADLINE": "3",
                                  # a background ingest (Stop) of the hung runtime: stopped after 5 s
                                  "CHECKPOINT_BG_DEADLINE": "5"},
                       permission_mode="bypassPermissions", debug_file=True, cols=160, rows=45, start=False)
    try:
        s.start()
        s.wait_ready(120)
        s.api.script([bash("echo t1 > t1.txt; echo t2 > t2.txt"), "t done"])
        t0 = time.time()
        s.submit("a call while checkpoint's python hangs")
        ok = wait(s, lambda: (proj / "t1.txt").exists(), 90)
        SUMMARY["timeout_unpaused_s"] = round(time.time() - t0, 1)
        check(f"timeout: not paused, a call goes ahead although checkpoint's Python hangs ({SUMMARY['timeout_unpaused_s']} s)",
              ok)
        check("timeout: ...without waiting out Claude Code's 60 s hook timeout (the first prompt answered within "
              "checkpoint's own hook deadline, 3 s here, plus 20 s of slack)", ok and SUMMARY["timeout_unpaused_s"] < 23,
              f"{SUMMARY['timeout_unpaused_s']} s (debug log: which hook timed out)")
        wait(s, lambda: "t done" in s.all_text(), 60)
        deg = list((box.home / "degraded").glob("*")) if (box.home / "degraded").is_dir() else []
        SUMMARY["timeout_degraded_marks"] = [x.name for x in deg]
        note(f"timeout: not paused, the common path never starts Python (fast.sh), so a hanging Python costs nothing "
             f"there; degraded marks: {len(deg)}")
        texts = [x.read_text(errors="replace") for x in deg]
        SUMMARY["timeout_degraded_texts"] = [t.strip()[:120] for t in texts]
        check("timeout: ...the session start that ran out of time is on record (degraded/<session>: SessionStart: "
              "Python timed out), and said in the session", any("SessionStart: Python timed out" in t for t in texts)
              and "didn't answer the session start" in re.sub(r"\s+", " ", s.all_text()), json.dumps(texts)[:300])
        # paused mid-turn (a typed message would resume: by design): the turn's next call must be held
        s.api.script([bash("sleep 8; echo first > first.txt", "long"), bash("echo held > held.txt", "next"),
                      "held done"])
        s.submit("two calls")
        wait(s, lambda: AI.running("sleep 8"), 60)
        t0 = time.time()
        s.submit("/check-pause")
        paused = wait(s, lambda: "check: paused" in s.text(), 20)
        SUMMARY["timeout_pause_ack_s"] = round(time.time() - t0, 2)
        wait(s, lambda: (proj / "first.txt").exists(), 30)
        s.pump(12)
        check("timeout: paused mid-turn while checkpoint's Python hangs, the turn's next call is denied (fail closed: "
              f"the mod's gate, and fast.sh's 3 s deadline beneath it; pause acked in {SUMMARY['timeout_pause_ack_s']} s)",
              paused and not (proj / "held.txt").exists(), s.text()[-400:])
    finally:
        s.close()
    # gateway: its daemon stopped (SIGSTOP)
    pids = AI.gated_pids(box)
    proj2 = root / "to-proj2"
    proj2.mkdir()
    s = driver.Session(root / "to-s2", proj2, config_dir=box.cfg, extra_env=dict(box.env),
                       permission_mode="bypassPermissions", debug_file=True, cols=160, rows=45, start=False)
    try:
        s.start()
        s.wait_ready(120)
        pids = AI.gated_pids(box) or pids
        wait(s, lambda: AI.gated_pids(box), 60)
        pids = AI.gated_pids(box)
        for pid in pids:
            os.kill(pid, signal.SIGSTOP)
        priv = box.gi.gateway_home / "private" / "vault.age"
        s.api.script([bash("echo stopped > stopped.txt"), {"tool": "Read", "input": {"file_path": str(priv)}},
                      "stop done"])
        t0 = time.time()
        s.submit("calls while the gateway's daemon is stopped")
        ran = wait(s, lambda: (proj2 / "stopped.txt").exists(), 180)
        SUMMARY["gated_stopped_bash_s"] = round(time.time() - t0, 1)
        done = wait(s, lambda: "stop done" in s.all_text(), 180)
        SUMMARY["gated_stopped_turn_s"] = round(time.time() - t0, 1)
        reqs = s.api.main_requests()
        from tui.fakeapi import result_text, tool_results
        res = ["\n".join(result_text(b) for b in tool_results(r)) for r in reqs[-3:]]
        SUMMARY["gated_stopped_results"] = [x[:200] for x in res]
        check(f"timeout: with the gateway's daemon stopped a plain Bash call still runs (in {SUMMARY['gated_stopped_bash_s']} s)",
              ran)
        check("timeout: ...and the Read of the vault stays refused (the file guard doesn't need the daemon)",
              done and any("gateway" in x and "private" in x for x in res) and not any("age-encryption" in x for x in res),
              json.dumps(res)[:500])
    finally:
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGCONT)
        s.close()


def part_upgrade(root: Path, box, gsrc: Path) -> None:
    print("upgrade: both plugins updated with a gateway job running", flush=True)
    proj = root / "up-proj"
    proj.mkdir()
    marker = AI.mark(90)
    s = driver.Session(root / "up-s1", proj, config_dir=box.cfg, extra_env=dict(box.env),
                       permission_mode="bypassPermissions", debug_file=True, cols=160, rows=45, start=False)
    try:
        s.start()
        s.wait_ready(120)
        ok = AI.offer(s, GW_JOB)
        if ok:
            n0 = len(s.api.main_requests())
            s.api.script([{"tool": GW_JOB, "input": {"command": marker}}, "job up"])
            s.submit("start a long job")
            wait_main(s, n0 + 2, 120)
        check("upgrade: a gateway job runs before the update", ok and wait(s, lambda: AI.running(marker), 30))
    finally:
        s.close()
    n_before = len(records(box.home))
    prev_g, prev_c = box.gi.plugin.version, box.p.version
    gv, cv = AI.newer(prev_g, 1000), AI.newer(prev_c, 1000)   # newer than the installed ones, whatever they are
    gi2 = box.h.update_gateway_e2e(gsrc, version=gv)
    ck2 = AI.patched_checkpoint(REPO, root / "ck-src-up", cv)
    p2 = box.h.update_from(ck2, repo="checkpoint", plugin="check")
    box.gi = gi2
    check(f"upgrade: both updated (gate {gv}, check {cv}: newer than the installed {prev_g} and {prev_c})",
          gi2.plugin.version == gv and p2.version == cv
          and AI.vkey(gv) > AI.vkey(prev_g) and AI.vkey(cv) > AI.vkey(prev_c))
    s = driver.Session(root / "up-s2", proj, config_dir=box.cfg, extra_env=dict(box.env),
                       permission_mode="bypassPermissions", debug_file=True, cols=160, rows=45, start=False)
    try:
        s.start()
        s.wait_ready(120)
        check("upgrade: the job still runs after the update and the new session (supervisor kept it)",
              AI.running(marker))
        s.submit("/gate-doctor")
        wait(s, lambda: "Gateway daemon" in s.all_text(), 30)
        doc = [ln.strip() for ln in s.all_text().splitlines() if "daemon" in ln.lower()][:4]
        SUMMARY["upgrade_doctor_daemon"] = doc
        wait(s, lambda: "check: recording" in s.text(), 60)
        s.api.script([bash("echo upgraded > up.txt"), "up done"])
        s.submit("after the upgrade")
        wait(s, lambda: "up done" in s.all_text(), 60)
    finally:
        s.close()
    ingest(box)
    check("upgrade: checkpoint's journal goes on across the update (records kept and added)",
          len(records(box.home)) > n_before)
    AI.kill_marks()


def part_overhead(root: Path, env: dict, box) -> None:
    print("overhead: ten Bash calls per turn; no plugin, checkpoint alone, both", flush=True)
    results = {}
    ckonly = AI.Box(root, "ovh-ck", env, None, True)
    try:
        ckonly.build()
        cfgs = {"none": (root / "ovh-none-cfg", {}), "checkpoint": (ckonly.cfg, {}), "both": (box.cfg, dict(box.env))}
        (root / "ovh-none-cfg").mkdir()
        for name, (cfg, extra) in cfgs.items():
            proj = root / f"ovh-{name}"
            proj.mkdir()
            s = driver.Session(root / f"ovh-s-{name}", proj, config_dir=cfg, extra_env={**env, **extra},
                               permission_mode="bypassPermissions", cols=160, rows=45, start=False)
            try:
                t0 = time.time()
                s.start()
                s.wait_ready(120)
                ready = round(time.time() - t0, 2)
                if name != "none":
                    wait(s, lambda: "check: recording" in s.text(), 60)
                s.pump(2)
                s.api.script([bash(f"echo {i} >> n.txt") for i in range(10)] + ["ten done"])
                n0 = len(s.api.main_requests())
                s.submit("ten calls")
                wait(s, lambda: "ten done" in s.all_text(), 180, 0.1)
                ts = [r.get("t") for r in s.api.main_requests()[n0:n0 + 11]]
                gaps = sorted(round(b - a, 3) for a, b in zip(ts, ts[1:]) if a and b)
                results[name] = {"to_prompt_s": ready, "per_call_median_s": gaps[len(gaps) // 2] if gaps else None,
                                 "per_call_max_s": gaps[-1] if gaps else None, "calls": len(gaps)}
            finally:
                s.close()
    finally:
        ckonly.close()
    SUMMARY["overhead"] = results
    note("overhead (per Bash call round trip, median of 10; fake model, includes the command): "
         + ", ".join(f"{k} {v.get('per_call_median_s')} s (prompt in {v.get('to_prompt_s')} s)" for k, v in results.items()))
    b, n = (results.get("both") or {}).get("per_call_median_s"), (results.get("none") or {}).get("per_call_median_s")
    check("overhead: each Bash call with both plugins costs under 1 s more than without (median)",
          b is not None and n is not None and b - n < 1.0, json.dumps(results))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tmp", default=tempfile.gettempdir())
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--pixi-cache", default=None)
    ap.add_argument("--gateway", default=str(REPO.parent / "gateway"))
    ap.add_argument("--only", default="concurrent,duplicate,diskfull,timeout,upgrade,overhead")
    a = ap.parse_args(argv)
    before = safety.real_settings_fingerprint()
    root = Path(tempfile.mkdtemp(prefix="ck-dur-", dir=a.tmp))
    safety.check_scratch(root, "acceptance root")
    print(f"acceptance (durability): root {root}", flush=True)
    env = {"PIXI_CACHE_DIR": a.pixi_cache or str(root / "pixi-cache")}
    only = set(a.only.split(","))
    gsrc = Path(a.gateway).resolve()
    t0 = time.time()
    box = None
    try:
        if only & {"concurrent", "duplicate", "timeout", "upgrade", "overhead"}:
            box = AI.Box(root, "dur", env, gsrc, True)
            if box.build():
                for name, fn in (("concurrent", lambda: part_concurrent(root, box)),
                                 ("duplicate", lambda: part_duplicate(root, box)),
                                 ("timeout", lambda: part_timeout(root, box)),
                                 ("overhead", lambda: part_overhead(root, env, box)),
                                 ("upgrade", lambda: part_upgrade(root, box, gsrc))):
                    if name in only:
                        try:
                            fn()
                        except Exception as e:  # noqa: BLE001
                            import traceback
                            traceback.print_exc()
                            check(f"part {name} ran to its end ({type(e).__name__}: {str(e)[:300]})", False)
        if "diskfull" in only:
            part_diskfull(root, env)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(f"the run went to its end ({type(e).__name__}: {str(e)[:300]})", False)
    finally:
        AI.kill_marks()
        driver._close_all()
        if box:
            box.close()
    time.sleep(1.0)

    def leftovers() -> list[str]:
        # the root's long prefix shortened, so the script's name shows
        return [f"{pid} {cmd.replace(str(root), '<root>')[:200]}" for pid, cmd in AI.ps_cmds()
                if (str(root) in cmd or cmd in AI.MARKS) and "ps -A" not in cmd]

    first = leftovers()
    left = first
    end = time.time() + 15       # a session's end hooks may still be finishing (SessionEnd ingests, 5 s at most)
    while left and time.time() < end:
        time.sleep(0.5)
        left = leftovers()
    if first and not left:
        note("processes that ended within 15 s after the last session closed: " + "; ".join(first)[:600])
    check("no process left naming the scratch root or a job's marker", not left, "\n".join(left))
    check("real ~/.config/claude settings and plugin registry unchanged", before == safety.real_settings_fingerprint())
    failed = [r for r in AI.RESULTS if not r["ok"]]
    summary = {"when": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "seconds": round(time.time() - t0),
               "claude": subprocess.run([driver.find_claude(), "--version"], capture_output=True,
                                        text=True).stdout.strip(), "platform": sys.platform,
               "passed": len(AI.RESULTS) - len(failed), "failed": len(failed), "checks": AI.RESULTS,
               "notes": AI.NOTES, **SUMMARY}
    out = Path(a.tmp) / "acceptance-durability.json"
    out.write_text(json.dumps(summary, indent=1, default=str))
    print(f"\n{summary['passed']} passed, {summary['failed']} failed in {summary['seconds']} s; {out}")
    if not a.keep:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
