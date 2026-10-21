#!/usr/bin/env python3
"""Authorization references (0.7.0; brief 5 and 7) in the real TUI: Claude
Code's permission verdicts reach checkpoint's journal through the mod's
tool.check observer and the classic PermissionRequest hook, by name and
decision only, and reconciliation says which still hold.

    pixi exec --spec python=3.12 --spec pyte python tests/acceptance_authz.py [--keep] [--root DIR] [--installed]

A scratch Claude config whose settings allow one command and deny another
(permission rules), a fake Messages API (tests/tui), default permission mode:
the scripted session runs the allowed command (no dialog), the denied one
(refused by the rule), one Claude Code asks about (answered Yes), and one it
asks about that is declined (No). The journal must hold each verdict (the
rule as written, the decision), what came of the two asks (approved: it ran;
declined: it never ran), the permission dialogs, and no reason text; the
summary's Authorizations section names them; reconciliation calls the
refused and declined calls never dispatched and the allow rule still in the
settings (unchanged), and flags it once the rule is removed.

The plugin is loaded from a copy (--plugin-dir) or, with --installed,
installed normally (check@checkpoint from a local git-source marketplace:
accept_install.py; the permission rules are merged into the config's
settings.json next to enabledPlugins), its runtime pre-built into the
config's plugin data folder (env.sh ensure). Nothing touches the real
Claude config or ~/.config/gateway (tests/tui refuses both). Prints one line
per check and a JSON summary; exits non-zero on any failure.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
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
from accept_install import merge_settings, provide  # noqa: E402

RESULTS: list[tuple[str, bool]] = []
ALLOW_CMD = "touch allowed-by-rule-1.txt"
DENY_CMD = "touch denied-by-rule-2.txt"
ASK_CMD = "touch asked-3.txt"
DECLINE_CMD = "touch declined-4.txt"


def check(name: str, ok: bool) -> bool:
    RESULTS.append((name, bool(ok)))
    print(f"  {'ok  ' if ok else 'FAIL'} {name}", flush=True)
    return bool(ok)


def wait(s: Session, cond, timeout: float, step: float = 0.2) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        s.pump(step)
    return cond()


def bash(cmd: str, desc: str) -> dict:
    return {"tool": "Bash", "input": {"command": cmd, "description": desc}}


def answer_dialog(s: Session, choice: str, needle: str, notes: dict) -> bool:
    """Wait for Claude Code's permission dialog about the command holding
    `needle` and answer it: "yes" (1) or "no" (Escape: "No, and tell Claude
    what to do differently"). A key pressed before the dialog settled is
    pressed again."""
    if not wait(s, lambda: "Do you want to proceed?" in s.text() and needle in s.text(), 40):
        return False
    s.pump(1.5)                     # the dialog takes keys once it has settled
    notes[f"dialog_{needle}"] = s.text()[-900:]
    key = "1" if choice == "yes" else "\x1b"
    for _ in range(3):
        s.send(key)
        if wait(s, lambda: not ("Do you want to proceed?" in s.text() and needle in s.text()), 8):
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", help="scratch root (a fresh folder under a temp base; default: a new temp dir)")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--installed", action="store_true",
                    help="install check@checkpoint from a local git-source marketplace instead of --plugin-dir")
    a = ap.parse_args()
    root = Path(a.root) if a.root else Path(tempfile.mkdtemp(prefix="ck-authz-"))
    root = safety.check_scratch(root, "scratch root")
    root.mkdir(parents=True, exist_ok=True)
    cfg = root / "cfg"
    proj = root / "proj"
    plugin = root / "plugin"
    t0 = time.time()
    summary: dict = {"root": str(root)}
    try:
        plugin, pdirs, data, summary["plugin"] = provide(root, cfg, a.installed)
        b = subprocess.run(["sh", str(plugin / "hooks" / "env.sh"), "ensure"], capture_output=True, text=True,
                           env=dict(os.environ, CLAUDE_PLUGIN_DATA=str(data),
                                    CHECKPOINT_HOME=str(root / "unused-state")), timeout=900)
        check("the plugin's runtime builds (pixi install --locked) into the config's plugin data", b.returncode == 0)
        merge_settings(cfg, {"permissions": {"allow": [f"Bash({ALLOW_CMD})"], "deny": [f"Bash({DENY_CMD})"]}})
        proj.mkdir()
        genv = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        genv.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        for args in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q",
                                      "--allow-empty", "-m", "start"]):
            subprocess.run(["git", "-C", str(proj), *args], check=True, capture_output=True, env=genv)
        home = safety.state_root_for(cfg) / ".local" / "state" / "checkpoint"
        replies = [bash(ALLOW_CMD, "allowed by a rule"), bash(DENY_CMD, "denied by a rule"),
                   bash(ASK_CMD, "asked, yes"), bash(DECLINE_CMD, "asked, no")] + ["done"] * 4
        s = Session(root / "s1", proj, plugin_dirs=pdirs, replies=replies, config_dir=cfg,
                    permission_mode="default", debug_file=True)
        try:
            s.wait_ready(timeout=120)
            check("the session reaches the prompt (footer label: check: recording)",
                  wait(s, lambda: "recording" in s.text(), 30))
            s.submit("make the four files")
            check("the allowed command runs without a dialog (the settings rule)",
                  wait(s, lambda: (proj / "allowed-by-rule-1.txt").exists(), 60))
            check("Claude Code asks about the third command; answered Yes, it runs",
                  answer_dialog(s, "yes", "asked-3", summary) and wait(s, lambda: (proj / "asked-3.txt").exists(), 30))
            check("it asks about the fourth; answered No, it doesn't run",
                  answer_dialog(s, "no", "declined-4", summary)
                  and not wait(s, lambda: (proj / "declined-4.txt").exists(), 5))
            check("...and the denied command never ran (the deny rule)", not (proj / "denied-by-rule-2.txt").exists())
            s.pump(3)
        finally:
            s.close()
        # The journal: what the hooks and the mod spooled, ingested here (a
        # closed session's spool waits for the next Python moment otherwise).
        os.environ["CHECKPOINT_HOME"] = str(home)
        os.environ["CLAUDE_CONFIG_DIR"] = str(cfg)
        os.environ["CHECKPOINT_GATEWAY_CONTRACT"] = ""
        sys.path.insert(0, str(plugin / "src"))
        from checkpoint import capture, journal, recovery, summary as summ  # noqa: E402
        sids = sorted(p.name for p in (home / "spool").iterdir()) if (home / "spool").is_dir() else []
        for sid in sids:
            capture.ingest(sid, sync=True)
        tasks = sorted(p.name for p in (home / "journal").iterdir() if p.is_dir()) if (home / "journal").is_dir() else []
        recs = [r for t in tasks for r in journal.read(t).records]
        task = next((r["task"] for r in recs if r["kind"] == "tool.intent"), tasks[0] if tasks else "")
        summary["task"] = task
        tid = {}
        for r in recs:
            if r["kind"] == "tool.intent":
                cmd = ((r.get("data") or {}).get("input") or {}).get("command") or ""
                tid[cmd] = r["op"]
        refs = [r["data"] for r in recs if r["kind"] == "auth.ref"]
        by = {(d.get("tool_use_id"), d.get("via")): d for d in refs}
        summary["auth_refs"] = len(refs)
        check("every call's intent recorded with its permission mode (default)",
              all(c in tid for c in (ALLOW_CMD, DENY_CMD, ASK_CMD, DECLINE_CMD))
              and all((r.get("data") or {}).get("mode") == "default" for r in recs if r["kind"] == "tool.intent"))
        a1 = by.get((tid.get(ALLOW_CMD), "check")) or {}
        check("the allow rule's verdict reached the journal (the mod's tool.check: decision allow, the rule as written)",
              a1.get("decision") == "allow" and a1.get("rule") == f"Bash({ALLOW_CMD})")
        d2 = by.get((tid.get(DENY_CMD), "check")) or {}
        check("the deny rule's verdict: decision deny, its rule", d2.get("decision") == "deny"
              and d2.get("rule") == f"Bash({DENY_CMD})")
        a2 = by.get((tid.get(DENY_CMD), "answer")) or {}
        check("...and the call came back refused (an error in Claude Code's refusal words: rejected), no outcome",
              a2.get("answered") in ("deny", "error") and (a2.get("answered") == "deny" or a2.get("rejected") is True)
              and not any(r["kind"] == "tool.outcome" and r["op"] == tid.get(DENY_CMD) for r in recs))
        q3 = by.get((tid.get(ASK_CMD), "check")) or {}
        check("the ask: decision ask, then a result (it ran)", q3.get("decision") == "ask"
              and (by.get((tid.get(ASK_CMD), "answer")) or {}).get("answered") == "result")
        q4 = by.get((tid.get(DECLINE_CMD), "check")) or {}
        a4 = (by.get((tid.get(DECLINE_CMD), "answer")) or {}).get("answered")
        summary["declined_answer"] = a4
        check("the declined ask: decision ask, then refused (rejected: Claude Code's words), no outcome",
              q4.get("decision") == "ask" and a4 in ("deny", "error")
              and (a4 == "deny" or (by.get((tid.get(DECLINE_CMD), "answer")) or {}).get("rejected") is True))
        check("the classic PermissionRequest hook recorded the permission dialogs (tool and input hash only)",
              sum(1 for d in refs if d.get("via") == "request" and d.get("input_sha")) >= 2)
        check("no reason text in any auth.ref", not any("reason" in d for d in refs))
        sm = summ.build(task, limit=100000)
        az = sm.text.split("\n## Authorizations")[1].split("\n## ")[0] if "\n## Authorizations" in sm.text else ""
        summary["authorizations"] = az.strip().splitlines()
        check("the summary's Authorizations section: the allow rule, the deny rule, the asks and their answers",
              f"settings rule `Bash({ALLOW_CMD})`: allow" in az and f"settings rule `Bash({DENY_CMD})`: deny" in az
              and "Bash calls put to the person:" in az and "approved" in az and "declined" in az)
        r = recovery.reconcile(task)
        nev = {o.op for o in r.ops if o.cls == "never"}
        check("reconciliation: the denied and the declined calls never dispatched",
              tid.get(DENY_CMD) in nev and tid.get(DECLINE_CMD) in nev)
        check("...the allow rule still in the settings: holds (no need to ask again)",
              r.authz is not None and any(f"Bash({ALLOW_CMD})" in x and "unchanged" in x for x in r.authz.holds))
        merge_settings(cfg, {"permissions": {"deny": [f"Bash({DENY_CMD})"]}})
        r2 = recovery.reconcile(task)
        check("...removed from the settings: flagged in Check first",
              any(f"Bash({ALLOW_CMD})" in t and "Authorization changed" in t for t in r2.check_first()))
        summary["seconds"] = round(time.time() - t0, 1)
    finally:
        if not a.keep:
            shutil.rmtree(root, ignore_errors=True)
    failed = [n for n, ok in RESULTS if not ok]
    summary.update(passed=len(RESULTS) - len(failed), failed=len(failed))
    print(json.dumps(summary))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
