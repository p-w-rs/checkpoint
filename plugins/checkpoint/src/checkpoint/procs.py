"""Finding the Claude process and its running foreground command, and killing it.

The process table comes from /proc on Linux and from ps on macOS (/bin/ps
first; the next ps is tried when one gives no rows); an empty table means
"unknown" (ps failed), never "nothing is running".

From DESIGN.md Spike 4: a hook's parent is the Claude process. Each Bash tool
call is a child shell, `bash -c 'source <snapshot> ... && eval '<command>'
< /dev/null && pwd -P >| <file>'`, that leads its own process group, and the
command inside may lead another, so we kill the whole descendant tree group
by group.

The command inside `eval` is shell-quoted by Claude Code ('"'"' for a single
quote), and macOS `ps` shows a newline as \\012 without escaping
backslashes. So the command is never matched as text:
we read the shell's exact argv (/proc on Linux, sysctl on macOS), parse the
argument of its `eval` the way the shell would, and compare that.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import time
from typing import Any, Iterator

SHELLS = {"sh", "bash", "zsh", "dash", "fish", "python", "python3", "pixi", "env", "timeout"}
# A ps STAT field: the state letter (BSD and Linux), then flag characters.
PS_STAT_RE = re.compile(r"[DIKPRSTUWXZt][+<>AELNSVWXls]*")


def _etime(s: str) -> float:
    """ps etime ([[dd-]hh:]mm:ss) in seconds."""
    days, _, rest = s.rpartition("-")
    secs = 0
    for part in rest.split(":"):
        secs = secs * 60 + int(part)
    return secs + (int(days) * 86400 if days else 0)


def table() -> dict[int, dict[str, Any]]:
    """Every process: pid, ppid, pgid, stat, start (epoch), cmd.

    On Linux read from /proc (procps' ps is missing from minimal images); on
    macOS from /bin/ps, part of the base system (then any other ps). An
    empty dict means the table could not be read: callers treat that as
    "unknown", never as "no process is running"."""
    if sys.platform.startswith("linux"):
        t = proc_table()
        if t:
            return t
    for attempt in range(2):  # a transient failure (timeout on a loaded machine)
        t = _ps_table()
        if t:
            return t
        if attempt == 0:
            time.sleep(0.2)
    return {}


def _ps_candidates() -> list[str]:
    """The ps programs to try, in order: on macOS the base system's /bin/ps
    first; elsewhere the one on PATH first (NixOS and others keep it outside
    /bin), then the usual places. Each found once."""
    on_path = shutil.which("ps")
    order = (["/bin/ps", on_path, "/usr/bin/ps"] if sys.platform == "darwin"
             else [on_path, "/bin/ps", "/usr/bin/ps"])
    out, seen = [], set()
    for ps in order:
        if ps and os.path.realpath(ps) not in seen:
            seen.add(os.path.realpath(ps))
            out.append(ps)
    return out


def _ps_outputs(args: list[str], timeout: float) -> Iterator[tuple[int, str]]:
    """Run each ps in turn with these args, yielding (exit code, output); the
    caller stops at the first usable one, so a missing or broken ps (a
    wrapper earlier on PATH) just moves on to the next. A timeout ends it:
    on a machine that loaded, the next one would time out too."""
    for ps in _ps_candidates():
        try:
            r = subprocess.run([ps, *args], capture_output=True, text=True, timeout=timeout,
                               env=dict(os.environ, LC_ALL="C"))
        except subprocess.TimeoutExpired:
            return
        except (OSError, subprocess.SubprocessError):
            continue
        yield r.returncode, r.stdout


def _ps_table() -> dict[int, dict[str, Any]]:
    for _code, out in _ps_outputs(["-A", "-ww", "-o", "pid=,ppid=,pgid=,stat=,etime=,command="], 10):
        rows = _parse_ps(out)
        if rows:
            return rows
    return {}


def _parse_ps(out: str) -> dict[int, dict[str, Any]]:
    now = time.time()
    procs: dict[int, dict[str, Any]] = {}
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 5:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
            start = now - _etime(parts[4])
        except ValueError:
            continue
        procs[pid] = {"pid": pid, "ppid": ppid, "pgid": pgid, "stat": parts[3],
                      "start": start, "cmd": parts[5] if len(parts) > 5 else ""}
    return procs


def _proc_stat(root: str, pid: int | str) -> tuple[str, list[str]] | None:
    """(comm, fields after comm) from /proc/<pid>/stat; comm may hold spaces
    and parentheses, so split at the last ')'."""
    try:
        with open(f"{root}/{pid}/stat", "rb") as f:
            raw = f.read().decode("utf8", "surrogateescape")
    except OSError:
        return None
    lo, hi = raw.find("("), raw.rfind(")")
    if lo < 0 or hi < lo:
        return None
    return raw[lo + 1:hi], raw[hi + 1:].split()


def proc_table(root: str = "/proc") -> dict[int, dict[str, Any]]:
    """The process table from /proc: the same fields as ps gives. Start
    times come from the uptime clock (to the clock tick), not ps's whole
    seconds."""
    try:
        names = os.listdir(root)
        with open(f"{root}/uptime") as f:
            uptime = float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return {}
    try:
        hz = os.sysconf("SC_CLK_TCK") or 100
    except (OSError, ValueError, AttributeError):
        hz = 100
    now = time.time()
    procs: dict[int, dict[str, Any]] = {}
    for name in names:
        if not name.isdigit():
            continue
        st = _proc_stat(root, name)
        if not st:
            continue
        comm, f = st
        try:
            # fields after comm: state ppid pgrp session tty tpgid flags minflt
            # cminflt majflt cmajflt utime stime cutime cstime priority nice
            # num_threads itrealvalue starttime ...
            stat, ppid, pgid, ticks = f[0], int(f[1]), int(f[2]), int(f[19])
        except (IndexError, ValueError):
            continue
        try:
            with open(f"{root}/{name}/cmdline", "rb") as fh:
                raw = fh.read()
        except OSError:
            raw = b""
        args = ([a.decode("utf8", "surrogateescape") for a in raw.rstrip(b"\0").split(b"\0")]
                if raw.strip(b"\0") else [])  # empty for kernel threads and zombies
        pid = int(name)
        procs[pid] = {"pid": pid, "ppid": ppid, "pgid": pgid, "stat": stat,
                      "start": now - (uptime - ticks / hz),
                      "cmd": " ".join(args) if args else f"[{comm}]"}
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


def is_claude(pid: int | None, procs: dict[int, dict[str, Any]]) -> bool:
    """`pid` is a running Claude process (not a zombie, not a reused pid
    that now belongs to something else). With an empty table (ps failed),
    asks the OS about this one pid."""
    if not pid:
        return False
    if not procs:
        a = argv(pid)
        return a is not None and alive(pid) and "claude" in " ".join(a).lower()
    rec = procs.get(pid)
    return bool(rec) and not rec["stat"].startswith("Z") and "claude" in rec["cmd"].lower()


def claude_pid(start: int) -> int | None:
    """This session's Claude process: CLAUDE_PID, which Claude Code exports to
    hooks and MCP servers, else found by walking up from `start`."""
    try:
        env = int(os.environ.get("CLAUDE_PID") or 0)
    except ValueError:
        env = 0
    if env:
        a = argv(env)
        if a is not None and "claude" in " ".join(a).lower():
            return env
    procs = table()
    if env and is_claude(env, procs):
        return env
    return find_claude(start, procs)


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
    return not zombie(pid)


def zombie(pid: int) -> bool:
    """Exited but not yet reaped by its parent (e.g. a job whose starting
    session is still open). It accepts signal 0 but is gone, and signalling
    its group fails with EPERM on macOS."""
    if sys.platform.startswith("linux"):
        st = _proc_stat("/proc", pid)
        return bool(st and st[1] and st[1][0].startswith("Z"))
    for _code, out in _ps_outputs(["-o", "stat=", "-p", str(pid)], 5):
        # Like _ps_table: only an output that is a process state counts (the
        # exit code doesn't); anything else moves on to the next ps.
        stat = out.split()[0] if out.split() else ""
        if PS_STAT_RE.fullmatch(stat):
            return stat.startswith("Z")
    return False


def alive_in(pid: int, procs: dict[int, dict[str, Any]]) -> bool:
    rec = procs.get(pid)
    return bool(rec) and not rec["stat"].startswith("Z")


def group_alive(pgid: int, procs: dict[int, dict[str, Any]] | None = None) -> bool:
    """Any process of group `pgid` still running (zombies don't count)."""
    try:
        os.killpg(pgid, 0)
        signalled = True
    except ProcessLookupError:
        return False
    except PermissionError:
        signalled = False  # macOS: only a zombie is left; the table says for sure
    procs = table() if procs is None else procs
    if not procs:
        return signalled  # no process table: trust the signal
    return any(r["pgid"] == pgid and not r["stat"].startswith("Z") for r in procs.values())


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


# -- what a Bash tool shell is running -----------------------------------------

def argv(pid: int) -> list[str] | None:
    """A process's exact argument vector, or None when the OS won't say."""
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
        except OSError:
            return None
        if not raw:
            return None
        return [a.decode("utf8", "surrogateescape") for a in raw.rstrip(b"\0").split(b"\0")]
    if sys.platform == "darwin":
        return _argv_darwin(pid)
    return None


def _argv_darwin(pid: int) -> list[str] | None:
    """sysctl KERN_PROCARGS2: int argc, the exec path, NUL padding, argv..."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        data = buf.raw[:size.value]
        argc = struct.unpack_from("i", data)[0]
        rest = data[4:]
        rest = rest[rest.index(b"\0"):].lstrip(b"\0")
        parts = rest.split(b"\0")
        if argc <= 0 or len(parts) < argc:
            return None
        return [a.decode("utf8", "surrogateescape") for a in parts[:argc]]
    except (OSError, ValueError, AttributeError, struct.error):
        return None


def ps_decode(cmd: str) -> str:
    """Undo macOS ps's \\ooo escapes (newline is \\012). Ambiguous with a
    literal backslash-digits text, so only a fallback when argv() fails."""
    return re.sub(r"\\([0-3][0-7]{2})", lambda m: chr(int(m.group(1), 8)), cmd)


_CMD_START = {None, "&&", "||", ";", "|", "&", "(", "{", "then", "do", "else", "!"}


def eval_command(script: str) -> str | None:
    """The command a Bash tool shell script runs: the argument of its first
    `eval` in command position, unquoted as the shell would. None if the
    script has no such eval or doesn't parse."""
    if "eval" not in script:
        return None
    lex = shlex.shlex(script, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    prev = None
    try:
        tokens = list(lex)
    except ValueError:
        return None
    for i, tok in enumerate(tokens):
        if tok == "eval" and prev in _CMD_START and i + 1 < len(tokens):
            return tokens[i + 1]
        prev = tok
    return None


def tool_command(pid: int, rec: dict[str, Any]) -> str | None:
    """What a Bash tool shell process runs, or None if it isn't one."""
    a = argv(pid)
    if a is not None:
        scripts = [a[i + 1] for i in range(len(a) - 1) if a[i] == "-c"]
    else:
        cmd = ps_decode(rec.get("cmd", ""))
        i = cmd.find(" -c ")
        scripts = [cmd[i + 4:]] if i >= 0 else []
    for s in scripts:
        c = eval_command(s)
        if c is not None:
            return c
    return None


def foreground_shells(claude_pid: int, command: str,
                      procs: dict[int, dict[str, Any]] | None = None,
                      since: float | None = None,
                      others: set[str] | frozenset[str] = frozenset()) -> list[int]:
    """The Bash tool shells under Claude running this command (topmost only:
    the shell's own subshells carry the same argv).

    `since` is the call's PreToolUse time: its shell starts after that, so an
    older shell running the same text (a background call) is left alone.
    Matched by the exact command. If no shell runs that command at all
    (Claude Code changed how it wraps commands), fall back to Bash tool
    shells started since then, except those running one of `others`, the
    session's other known calls."""
    procs = procs if procs is not None else table()
    want = command.strip()
    shells: dict[int, str] = {}
    for pid in descendants(claude_pid, procs):
        if "eval" not in procs[pid]["cmd"]:
            continue
        c = tool_command(pid, procs[pid])
        if c is not None:
            shells[pid] = c.strip()

    def recent(pid: int) -> bool:
        # ps start times come from whole-second clocks: allow a second.
        return since is None or procs[pid]["start"] >= since - 1.0

    exact = [pid for pid, c in shells.items() if c == want]
    if exact:
        hits = [pid for pid in exact if recent(pid)]
    elif since is not None:
        skip = {o.strip() for o in others} - {want}
        hits = [pid for pid, c in shells.items() if recent(pid) and c not in skip]
    else:
        hits = []
    chosen = set(hits)

    def nested(pid: int) -> bool:
        q, hops = procs[pid]["ppid"], 0
        while q in procs and q != claude_pid and hops < 50:
            if q in chosen:
                return True
            q, hops = procs[q]["ppid"], hops + 1
        return False

    return sorted(pid for pid in hits if not nested(pid))


def command_shells(command: str, procs: dict[int, dict[str, Any]] | None = None,
                   since: float | None = None) -> list[int]:
    """Bash tool shells anywhere in the table running exactly this command,
    started since `since` (a call's intent time; ps start times are whole
    seconds, so a second of slack), topmost only. For reconciling after a
    crash: Claude may be gone and its shells adopted by init, so they are
    not looked for under a Claude pid. A pid alone proves nothing: the
    command and the start time must both match."""
    want = (command or "").strip()
    if not want:
        return []
    procs = procs if procs is not None else table()
    hits = []
    for pid, rec in procs.items():
        if "eval" not in rec.get("cmd", "") or pid == os.getpid():
            continue
        if since is not None and rec.get("start", 0) < since - 1.0:
            continue
        c = tool_command(pid, rec)
        if c is not None and c.strip() == want:
            hits.append(pid)
    chosen = set(hits)
    return sorted(p for p in hits if procs[p]["ppid"] not in chosen)


def _pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def kill_tree(root: int, grace: float = 3.0, protect: set[int] | None = None,
              procs: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    """SIGTERM the process groups of `root` and its descendants, then SIGKILL
    whatever is left after `grace` seconds. Never signals a protected pid's
    group (Claude, ourselves).

    `procs` is the caller's process table (read here when not given). With
    no table (ps failed) the descendants are unknown: `root` is signalled
    directly, with its group if it leads one that isn't protected. A table
    read that fails while waiting never means "all gone": each process is
    then asked about on its own. Returns the processes that are gone
    (`killed`) and those still running after SIGKILL (`survivors`)."""
    procs = table() if procs is None else procs
    protect = set(protect or ()) | {os.getpid()}
    pgids: dict[int, int | None] = {}  # victim -> its group (None: signal the pid alone)
    cmds: dict[int, str] = {}
    if procs:
        safe_groups = {procs[q]["pgid"] for q in protect if q in procs} | {os.getpgrp()}
        for v in [root] + descendants(root, procs):
            if v in procs and v not in protect:
                pgids[v], cmds[v] = procs[v]["pgid"], procs[v]["cmd"]
    else:
        safe_groups = {g for g in map(_pgid, protect) if g is not None} | {os.getpgrp()}
        if root not in protect and alive(root):
            pgids[root] = root if _pgid(root) == root else None
            cmds[root] = " ".join(argv(root) or [])
    victims = list(pgids)
    groups = {g for g in pgids.values() if g is not None} - safe_groups

    def send(sig: int) -> None:
        for g in groups:
            try:
                os.killpg(g, sig)
            except (ProcessLookupError, PermissionError):
                pass
        for v in victims:
            if pgids[v] not in groups:  # in a protected group, or its group unknown
                try:
                    os.kill(v, sig)
                except (ProcessLookupError, PermissionError):
                    pass

    def left() -> list[int]:
        now = table()
        return [v for v in victims if (alive_in(v, now) if now else alive(v))]

    def wait(seconds: float) -> list[int]:
        deadline = time.time() + seconds
        rest = left()
        while rest and time.time() < deadline:
            time.sleep(0.1)
            rest = left()
        return rest

    send(signal.SIGTERM)
    rest = wait(grace)
    needed_sigkill = bool(rest)
    if rest:
        send(signal.SIGKILL)
        rest = wait(2.0)

    def desc(pids: list[int]) -> list[dict[str, Any]]:
        return [{"pid": v, "cmd": cmds[v][:200]} for v in pids]

    return {"killed": desc([v for v in victims if v not in rest]), "survivors": desc(rest),
            "needed_sigkill": needed_sigkill}
