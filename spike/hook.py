#!/usr/bin/env python3
"""Throwaway spike hook for agent-pause. Logs every event; gates tools on a flag.

Usage (from settings.json): python3 hook.py <EventName>
State lives in spike/state/ (override with SPIKE_STATE):
  events.jsonl      one line per hook call
  paused-<sid>      per-session flag      paused-all  global flag
  denies-<sid>      deny counter
  mode              optional: "continue-false-after-N" (e.g. continue-false-after-2)
                    or "continue-false-subagent" (continue:false only inside subagents)
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("SPIKE_STATE", os.path.join(HERE, "state"))


def proc_table():
    out = subprocess.run(["ps", "-ax", "-o", "pid=,ppid=,pgid=,command="],
                         capture_output=True, text=True).stdout
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) >= 3:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
            table[pid] = (ppid, pgid, parts[3] if len(parts) > 3 else "")
    return table


def chain(table):
    """Parent chain of this hook process, up to pid 1."""
    res, pid = [], os.getpid()
    while pid in table and pid > 1 and len(res) < 15:
        ppid, pgid, cmd = table[pid]
        res.append({"pid": pid, "pgid": pgid, "cmd": cmd[:120]})
        pid = ppid
    return res


def find_claude(ch):
    """First ancestor that isn't a shell or python and mentions claude."""
    for p in ch[1:]:
        exe = os.path.basename(p["cmd"].split()[0]) if p["cmd"] else ""
        if exe.lstrip("-") in ("sh", "bash", "zsh", "python3", "python"):
            continue
        if "claude" in p["cmd"].lower():
            return p["pid"]
    return None


def descendants(table, root):
    kids = {}
    for pid, (ppid, _pgid, _cmd) in table.items():
        kids.setdefault(ppid, []).append(pid)
    res, todo = [], list(kids.get(root, []))
    while todo:
        pid = todo.pop()
        res.append(pid)
        todo.extend(kids.get(pid, []))
    return res


def log(rec):
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, "events.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")


def main():
    event = sys.argv[1]
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError:
        data = {"_raw": raw[:500]}
    sid = data.get("session_id", "nosid")
    table = proc_table()
    ch = chain(table)
    claude_pid = find_claude(ch)
    rec = {
        "t": time.strftime("%H:%M:%S") + ".%03d" % (int(time.time() * 1000) % 1000),
        "event": event,
        "sid": sid[:8],
        "agent_id": data.get("agent_id"),
        "agent_type": data.get("agent_type"),
        "tool": data.get("tool_name"),
        "input": json.dumps(data.get("tool_input"))[:160] if "tool_input" in data else None,
        "prompt": data.get("prompt"),
        "stop_hook_active": data.get("stop_hook_active"),
        "keys": sorted(data.keys()),
        "claude_pid": claude_pid,
    }
    if event == "PostToolUse" or event == "PostToolUseFailure":
        rec["response"] = json.dumps(data.get("tool_response", data.get("error")))[:600]
    if event in ("SessionStart", "UserPromptSubmit") or (event == "PreToolUse" and data.get("tool_name") == "Bash"):
        rec["chain"] = ch
    if claude_pid and event == "UserPromptSubmit":
        rec["claude_descendants"] = [
            {"pid": p, "pgid": table[p][1], "cmd": table[p][2][:120]}
            for p in descendants(table, claude_pid) if p in table and p not in [c["pid"] for c in ch]
        ]

    flag = os.path.join(STATE, "paused-" + sid)
    flag_all = os.path.join(STATE, "paused-all")
    out = None

    if event == "UserPromptSubmit":
        prompt = (data.get("prompt") or "").strip()
        if prompt.startswith("/spike-pause") or prompt.lower().rstrip(".!") in ("pause", "pause now", "pause all"):
            open(flag, "w").close()
            rec["action"] = "flag set"
            out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                   "additionalContext": "spike: pause flag set at " + rec["t"]}}
            if "now" in prompt.split()[1:] and claude_pid:
                rec["killed"] = kill_foreground(table, claude_pid, ch)
        elif prompt.startswith("/spike-resume"):
            for p in (flag, flag_all):
                if os.path.exists(p):
                    os.remove(p)
            rec["action"] = "flag cleared"

    elif event == "PreToolUse" and (os.path.exists(flag) or os.path.exists(flag_all)):
        cnt_path = os.path.join(STATE, "denies-" + sid)
        n = int(open(cnt_path).read() or 0) + 1 if os.path.exists(cnt_path) else 1
        with open(cnt_path, "w") as f:
            f.write(str(n))
        mode = open(os.path.join(STATE, "mode")).read().strip() if os.path.exists(os.path.join(STATE, "mode")) else ""
        cf = False
        if mode.startswith("continue-false-after-"):
            cf = n > int(mode.rsplit("-", 1)[1])
        elif mode == "continue-false-subagent":
            cf = bool(data.get("agent_id"))
        elif mode == "continue-false-now":
            cf = True
        if cf:
            out = {"continue": False, "stopReason": "Paused by agent-pause spike (deny #%d)" % n}
            rec["action"] = "continue:false #%d" % n
            if os.path.exists(os.path.join(STATE, "with-deny")):
                out["hookSpecificOutput"] = {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": "Paused by the user (agent-pause spike)."}
                rec["action"] += "+deny"
        else:
            out = {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Paused by the user (agent-pause spike). Do not call any more tools. End your turn now with one line saying you are paused."}}
            rec["action"] = "deny #%d" % n

    log(rec)
    if out is not None:
        print(json.dumps(out))


def kill_foreground(table, claude_pid, ch):
    """Kill shell children of claude (other than this hook's own chain)."""
    mine = set(c["pid"] for c in ch)
    killed = []
    for pid, (ppid, pgid, cmd) in table.items():
        if ppid == claude_pid and pid not in mine:
            exe = os.path.basename(cmd.split()[0]) if cmd else ""
            if exe in ("bash", "zsh", "sh", "-bash", "-zsh"):
                try:
                    os.killpg(pgid, 15) if pgid == pid else os.kill(pid, 15)
                    killed.append({"pid": pid, "pgid": pgid, "cmd": cmd[:120], "own_group": pgid == pid})
                except OSError as e:
                    killed.append({"pid": pid, "err": str(e)})
    return killed


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # fail open
        log({"event": "hook-error", "err": repr(e)})
