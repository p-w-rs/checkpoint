"""Finding the Claude process and its running foreground command, and killing it.

From DESIGN.md Spike 4: a hook's parent is the Claude process. Each Bash tool
call is a child `bash -c source <snapshot> ... && eval '<command>' ...` that
leads its own process group, and the command inside may lead another, so we
kill the whole descendant tree group by group.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Any

SHELLS = {"sh", "bash", "zsh", "dash", "fish", "python", "python3", "pixi", "env", "timeout"}


def table() -> dict[int, dict[str, Any]]:
    try:
        out = subprocess.run(["ps", "-ax", "-ww", "-o", "pid=,ppid=,pgid=,command="],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    procs: dict[int, dict[str, Any]] = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        procs[pid] = {"pid": pid, "ppid": ppid, "pgid": pgid,
                      "cmd": parts[3] if len(parts) > 3 else ""}
    return procs


def _exe(cmd: str) -> str:
    first = cmd.split()[0] if cmd.split() else ""
    return os.path.basename(first).lstrip("-")


def find_claude(start: int, procs: dict[int, dict[str, Any]] | None = None) -> int | None:
    """Walk up from `start` (a hook's parent) to the first process that isn't
    a shell, python or pixi and mentions claude."""
    procs = procs if procs is not None else table()
    pid, hops = start, 0
    while pid in procs and pid > 1 and hops < 20:
        cmd = procs[pid]["cmd"]
        exe = _exe(cmd)
        if not exe.startswith("python") and exe not in SHELLS and "claude" in cmd.lower():
            return pid
        pid, hops = procs[pid]["ppid"], hops + 1
    return None


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:  # reap it if it's our own exited child (a job started by this process)
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def descendants(root: int, procs: dict[int, dict[str, Any]]) -> list[int]:
    kids: dict[int, list[int]] = {}
    for pid, rec in procs.items():
        kids.setdefault(rec["ppid"], []).append(pid)
    out, todo = [], list(kids.get(root, []))
    while todo:
        pid = todo.pop()
        out.append(pid)
        todo.extend(kids.get(pid, []))
    return out


def quoted(command: str) -> str:
    """How the Bash tool embeds a command: eval '<cmd>' with ' -> '\\''."""
    return "eval '" + command.replace("'", "'\\''") + "'"


def foreground_shells(claude_pid: int, command: str,
                      procs: dict[int, dict[str, Any]] | None = None) -> list[int]:
    """Children of Claude running exactly this Bash tool command."""
    procs = procs if procs is not None else table()
    needle = quoted(command)
    return [pid for pid, rec in procs.items()
            if rec["ppid"] == claude_pid and needle in rec["cmd"]]


def kill_tree(root: int, grace: float = 3.0, protect: set[int] | None = None) -> dict[str, Any]:
    """SIGTERM the process groups of `root` and its descendants, then SIGKILL
    whatever is left after `grace` seconds. Never signals a protected pid's
    group (Claude, ourselves)."""
    procs = table()
    protect = set(protect or ()) | {os.getpid()}
    safe_groups = {procs[q]["pgid"] for q in protect if q in procs} | {os.getpgrp()}
    victims = [root] + descendants(root, procs)
    victims = [v for v in victims if v in procs and v not in protect]
    groups = {procs[v]["pgid"] for v in victims} - safe_groups
    desc = [{"pid": v, "cmd": procs[v]["cmd"][:200]} for v in victims]

    def send(sig: int) -> None:
        for g in groups:
            try:
                os.killpg(g, sig)
            except (ProcessLookupError, PermissionError):
                pass
        for v in victims:
            if procs[v]["pgid"] in safe_groups:
                try:
                    os.kill(v, sig)
                except (ProcessLookupError, PermissionError):
                    pass

    send(signal.SIGTERM)
    deadline = time.time() + grace
    while time.time() < deadline and any(alive(v) for v in victims):
        time.sleep(0.1)
    left = [v for v in victims if alive(v)]
    if left:
        send(signal.SIGKILL)
    return {"killed": desc, "needed_sigkill": bool(left)}
