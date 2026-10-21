"""Detached jobs: long work that survives pauses, closed lids and sessions.

Each job runs in its own session/process group under a tiny sh wrapper that
writes the exit code to a file. Claude's own run_in_background Bash dies with
the Claude process (DESIGN.md, Spike 4); these don't.

Registry: <home>/jobs.json (under the state lock); per job <home>/jobs/<id>/
holding output.log, exit_code and gateway_env (see below), all private
(0600: exit_code and gateway_env are made empty before the job starts, and
the job's shell fills them in; an empty exit_code means it hasn't exited).
The job itself runs with the umask the user's session had. States: running, exited, killed (by
job_stop), vanished (gone without an exit code), stop_failed (job_stop could
not end every process of its group). A job counts as running while its
wrapper or any process of its group runs; with no process table (ps failed)
states are left as they were. Finished jobs are pruned after
state.JOB_RETENTION_DAYS.

When the gateway plugin is installed, a job runs in the gateway's environment
too, like Claude's Bash: its shell sources the gateway's env.sh before the
command (see gateway_env and WRAPPER). A job's `gateway_env` records how that
went: applied, failed (the job ran without it), refused: <why> (the file
isn't safe to source, so it wasn't), or nothing when there is no env file.

Two supervisors, never both for one job (PLAN-v4 phase 4): with the gateway
available (CONTRACT-v1 discovery, feature "jobs") a job is started through
its contract (`POST /v1/jobs`: task_ref = the journal task, the session, the
idempotency key `ck:<op>` from the tool call, on_pause from the call or the
task's policy) and its registry record says `owner: "gateway"` with the
gateway's job id (`gateway_job`); the gateway supervises it, and its state,
tail and stop go through the contract. Otherwise the own runner above starts
it (`owner: "checkpoint"`; records without an owner, 0.1-0.5's, are the own
runner's too). A start that reached the gateway but got no whole answer is
retried once with the same key (it returns the job if the first one started
it) and never handed to the own runner: that could run the command twice.
Once any attempt may have started it (ambiguous, or an answer that isn't
CONTRACT-v1), whatever the retry gets (no answer, the socket gone, a
refusal) the start's outcome is unknown, said with the key; only a start
every attempt of which certainly did nothing because the gateway doesn't
serve this caller (never connected, or refused as not served:
gateway.not_served) falls back to the own runner. Any other refusal (403
policy, 403 session, 409 paused, a code checkpoint doesn't know, ...) is
the gateway's decision: the job is refused, said with the gateway's reason,
and started nowhere (0.7.2: before, any 403 ran it in the own runner,
around the gateway's policy). A gateway job's stored `state` is the last one the gateway reported
(`seen_ts`, `seen_epoch`); while the gateway isn't available it reads as
`unknown` (with the last state seen), and one that a new gateway epoch no
longer lists is stored as `unknown` (`gone`). `unknown`, gone or not, is
never done and never stopped: such a job counts as possibly running
(is_live), is not final (kept out of pruning and of journal retention until
the gateway says what became of it), and a stop of it says "outcome
unknown, it may still run" with the reason.
"""
from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

from . import gateway, procs, sanitize, state

# Jobs this process started. The MCP server reaps them as they exit (see
# reap_started), so they don't linger as zombies while its session is open.
STARTED: set[int] = set()


def reap_started(*_: object) -> None:
    for pid in list(STARTED):
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                STARTED.discard(pid)
        except ChildProcessError:
            STARTED.discard(pid)


def _registry() -> dict[str, Any]:
    reg = state.read_json(state.p("jobs.json"), {}) or {}
    reg.setdefault("jobs", {})
    return reg


def _save(reg: dict[str, Any]) -> None:
    state.write_json(state.p("jobs.json"), reg)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")[:40] or "job"


# Set by `pixi run` when it activates our own environment (the MCP server runs
# under it). A job must not inherit them: PIXI_PROJECT_MANIFEST alone makes
# any `pixi run` in the job use checkpoint's pixi.toml instead of its own.
_PIXI_ACTIVATION = ("PIXI_PROJECT_MANIFEST", "PIXI_PROJECT_ROOT", "PIXI_PROJECT_NAME",
                    "PIXI_PROJECT_VERSION", "PIXI_ENVIRONMENT_NAME",
                    "PIXI_ENVIRONMENT_PLATFORMS", "PIXI_IN_SHELL", "PIXI_PROMPT", "PIXI_EXE",
                    "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "CONDA_SHLVL")


def job_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The environment the user's session had: our own pixi activation removed
    (its variables, and our .pixi/envs bin dirs from PATH). Another project's
    activation (the session itself running under `pixi shell`) is kept."""
    env = dict(os.environ if environ is None else environ)
    ours = str(state.PLUGIN_ROOT)
    root = env.get("PIXI_PROJECT_ROOT")
    if root and os.path.realpath(root) == os.path.realpath(ours):
        for k in _PIXI_ACTIVATION:
            env.pop(k, None)
        if env.get("INIT_CWD") == root:
            env.pop("INIT_CWD")
    pixi_dirs = (os.path.join(ours, ".pixi") + os.sep, os.path.join(os.path.realpath(ours), ".pixi") + os.sep)
    if "PATH" in env:
        env["PATH"] = os.pathsep.join(d for d in env["PATH"].split(os.pathsep)
                                      if not d.startswith(pixi_dirs))
    return env


def _unsafe(st: os.stat_result, uid: int) -> str:
    if st.st_uid != uid:
        return "not owned by you"
    if st.st_mode & 0o022:
        return "group- or other-writable"
    return ""


def gateway_env() -> tuple[str, str]:
    """(path, refusal) for the gateway's env file (state.gateway_env_path):
    ("", "") when there is none (gateway not installed, or turned off), (path,
    "") when a job may source it, (path, why) when it exists but isn't safe to
    source. Safe: a regular file (not a symlink) you own that nobody else can
    write, in a folder you own that nobody else can write (so the file can't
    be swapped between this check and the job's shell sourcing it)."""
    path = state.gateway_env_path()
    if not path:
        return "", ""
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "", ""
    except OSError as e:
        return path, f"can't be checked ({e.strerror or e})"
    if stat.S_ISLNK(st.st_mode):
        return path, "a symlink"
    if not stat.S_ISREG(st.st_mode):
        return path, "not a regular file"
    uid = os.getuid()
    why = _unsafe(st, uid)
    if why:
        return path, why
    try:
        why = _unsafe(os.stat(os.path.dirname(path)), uid)
    except OSError as e:
        return path, f"its folder can't be checked ({e.strerror or e})"
    return path, f"its folder is {why}" if why else ""


# The job's shell: $0 checkpoint-job-<id> (so status can tell our process from
# an unrelated one that reused the pid), $1 the command, $2 the exit-code file,
# $3 the gateway env file or "", $4 where to write whether it was applied.
# The command runs in a nested sh, so its own `exit` can't skip the exit-code
# write. The env file is first sourced in a subshell: dash exits on a syntax
# error, a failing special builtin or a missing file in a sourced file, and
# bash goes on after a syntax error with half of it applied. Only a file that
# sources cleanly (no error output, the shell survives) is applied for real;
# otherwise the job says so in its log and runs without it (fails open, like
# the rest of checkpoint). The probe's error text follows that line.
WRAPPER = """\
checkpoint_cmd=$1 checkpoint_exit=$2
if [ -n "$3" ]; then
  checkpoint_err=$(. "$3" 2>&1 >/dev/null </dev/null; echo checkpoint-ok)
  if [ "$checkpoint_err" = checkpoint-ok ]; then
    . "$3" >/dev/null 2>&1 </dev/null
    echo applied > "$4"
  else
    echo failed > "$4"
    echo "checkpoint: could not apply the gateway environment ($3); the job runs without it." >&2
    checkpoint_err=${checkpoint_err%?checkpoint-ok}
    [ -z "$checkpoint_err" ] || printf '%s\\n' "$checkpoint_err" >&2
  fi
fi
/bin/sh -c "$checkpoint_cmd"; rc=$?; echo $rc > "$checkpoint_exit"; exit $rc
"""


CANCEL_POLICIES = ("interrupt", "detach")
OWN_LIVE = ("running", "stop_failed")
OWN_FINAL = ("exited", "killed", "vanished")


def owner(rec: dict[str, Any]) -> str:
    """Which supervisor runs it: "gateway" or "checkpoint" (the default: the
    records of 0.1-0.5 have no owner)."""
    return "gateway" if rec.get("owner") == "gateway" else "checkpoint"


def is_unknown(rec: dict[str, Any]) -> bool:
    """A gateway job whose state the gateway can't prove (unreachable, or
    lost in a restart: `gone`): it may still run."""
    return owner(rec) == "gateway" and rec.get("state") == "unknown"


def is_live(rec: dict[str, Any]) -> bool:
    """Running, or possibly running: the own runner's running / stop_failed;
    a gateway job in a live state, or unknown (gone or not: the contract's
    unknown is never finished, so it may still run)."""
    if owner(rec) == "gateway":
        return rec.get("state") in gateway.LIVE_STATES or is_unknown(rec)
    return rec.get("state") in OWN_LIVE


def is_final(rec: dict[str, Any]) -> bool:
    """Certainly ended: the own runner's exited / killed / vanished; a
    gateway job the gateway reported exited, failed or canceled. Never an
    unknown one."""
    if owner(rec) == "gateway":
        return rec.get("state") in gateway.FINAL_STATES
    return rec.get("state") in OWN_FINAL


def start(name: str, command: str | None = None, argv: list[str] | None = None,
          cwd: str | None = None, redo_hint: str = "", task: str = "",
          session_id: str = "", task_id: str = "", on_cancel: str = "", on_pause: str = "",
          op: str = "", agent: str = "") -> dict[str, Any]:
    if bool(command) == bool(argv):
        return {"ok": False, "reason": "give exactly one of command (a shell string) or argv (a list)"}
    cwd = cwd or os.getcwd()
    if not Path(cwd).is_dir():
        return {"ok": False, "reason": f"cwd does not exist: {cwd}"}
    if on_pause and on_pause not in gateway.ON_PAUSE:
        return {"ok": False, "reason": f"on_pause is one of {', '.join(gateway.ON_PAUSE)}"}
    cmdline = command if command else shlex.join(argv or [])
    g = gateway.discover()
    fallback = ""
    if g.has("jobs"):
        out = _start_gateway(g, name, command, argv, cwd, redo_hint, task, session_id, task_id, on_cancel,
                             on_pause or "continue", op, agent)
        if "fallback" not in out:
            return out
        fallback = out["fallback"]
    elif g.ok:
        fallback = f"the gateway ({g.version}) doesn't offer jobs on its contract"
    else:
        fallback = g.reason
    res = _start_own(name, cmdline, cwd, redo_hint, task, session_id, task_id, on_cancel, on_pause, fallback,
                     gateway_status=g.status)
    return res


def _start_own(name: str, cmdline: str, cwd: str, redo_hint: str, task: str, session_id: str, task_id: str,
               on_cancel: str, on_pause: str, fallback: str, gateway_status: str = "") -> dict[str, Any]:
    with state.lock():
        reg = _registry()
        jid = _new_id(reg, name)
        jdir = state.p("jobs", jid)
        jdir.mkdir(parents=True, exist_ok=True)
        log = jdir / "output.log"
        exitf = jdir / "exit_code"
        gwf, refused = gateway_env()
        use = gwf if gwf and not refused else ""
        # Made here (0600, under our umask 077) so that the job's shell, which
        # runs with the user's umask, writes into private files.
        for f in [exitf] + ([jdir / "gateway_env"] if use else []):
            f.write_bytes(b"")
        with open(log, "ab") as out:
            proc = subprocess.Popen(["/bin/sh", "-c", WRAPPER, f"checkpoint-job-{jid}",
                                     cmdline, str(exitf), use, str(jdir / "gateway_env") if use else ""],
                                    cwd=cwd, env=job_env(), stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=True,
                                    umask=state.user_umask())
        STARTED.add(proc.pid)
        rec = {"id": jid, "name": name, "command": cmdline, "cwd": cwd,
               "pid": proc.pid, "pgid": proc.pid, "log": str(log), "exit_file": str(exitf),
               "started": state.now_iso(), "started_ts": time.time(),
               "session_id": session_id, "task": task, "redo_hint": redo_hint,
               "state": "running"}
        if task_id:
            rec["task_id"] = task_id   # the journal task it belongs to (/check:cancel applies its policy)
        rec["on_cancel"] = on_cancel if on_cancel in CANCEL_POLICIES else "interrupt"
        rec["owner"] = "checkpoint"
        if fallback:
            rec["why_own"] = sanitize.clip(fallback, 300)
        if gwf:
            rec["gateway_env"] = f"refused: {refused}" if refused else "pending"
            rec["gateway_env_file"] = gwf
        reg["jobs"][jid] = rec
        _save(reg)
    out: dict[str, Any] = {"ok": True, "job": public(rec), "supervisor": "checkpoint"}
    if on_pause and on_pause != "continue":
        out["note"] = (f"on_pause {on_pause} applies to gateway jobs only: checkpoint's own runner keeps a job "
                       "running when the task is paused (/check:pause --stop-jobs stops them)")
    if gateway_status and gateway_status not in ("absent", "off"):
        out["gateway"] = f"not used: {sanitize.clip(fallback, 200)}"
    return out


def _new_id(reg: dict[str, Any], name: str) -> str:
    jid = f"{_slug(name)}-{time.strftime('%Y%m%d-%H%M%S')}"
    n = 2
    while jid in reg["jobs"]:
        jid = f"{_slug(name)}-{time.strftime('%Y%m%d-%H%M%S')}-{n}"
        n += 1
    return jid


def _start_gateway(g: gateway.Gateway, name: str, command: str | None, argv: list[str] | None, cwd: str,
                   redo_hint: str, task: str, session_id: str, task_id: str, on_cancel: str, on_pause: str,
                   op: str, agent: str) -> dict[str, Any]:
    """Start the job through the gateway's contract. Returns the tool's
    answer, or {"fallback": why} when the gateway certainly didn't start it
    and doesn't serve this caller (never connected, or refused as not
    served: gateway.not_served) on the first attempt: only then may the own
    runner run it. Any other refusal is the gateway deciding no: refused
    (`outcome: refused`, the gateway's reason and code), started nowhere.
    Once an attempt may have started it (sent, then no whole answer, or an
    answer that isn't CONTRACT-v1), the outcome is unknown whatever the
    retry gets: never a fallback (the command would run twice)."""
    key = "ck:" + (op if re.fullmatch(r"[A-Za-z0-9_.:@/+=-]{1,190}", op or "") else _fresh_op())
    body: dict[str, Any] = {"session": session_id, "cwd": cwd, "idempotency_key": key, "on_pause": on_pause}
    if command:
        body["command"] = command
    else:
        body["argv"] = list(argv or [])
    if task_id:
        body["task_ref"] = task_id
    if agent:
        body["agent"] = sanitize.clip(agent, 80)
    answer: dict[str, Any] | None = None
    errors: list[gateway.CallError] = []
    maybe_started = False     # an attempt reached the gateway and may have started it
    for _attempt in range(2):
        try:
            answer = gateway.job_start(g, body)
            break
        except gateway.CallError as e:
            errors.append(e)
            if e.kind in ("ambiguous", "protocol"):
                maybe_started = True
                continue      # it may have started; the same key returns it if it did
            if maybe_started:
                break         # the retry got nowhere, but the first try may have started it
            if e.kind == "unreachable":
                return {"fallback": f"the gateway couldn't be reached ({e.text()})"}
            if gateway.not_served(e):
                return {"fallback": f"the gateway doesn't serve this session ({e.text()})"}
            return _refused(e, key)
    if answer is None or not gateway.JOB_RE.fullmatch(str(answer.get("job") or "")):
        if not errors:
            why = "its answer named no job"
        elif len(errors) == 1:
            why = errors[0].text()
        else:
            why = f"{errors[0].text()}; the retry with the same key: {errors[-1].text()}"
        return {"ok": False, "supervisor": "gateway", "outcome": "unknown", "idempotency_key": key,
                "reason": (f"the start reached the gateway but got no whole answer ({why}): the job may or may not "
                           f"have started there (idempotency key {key}). It was NOT started anywhere else. Check "
                           "with the gateway's job list (job_status with all_sessions, or the gateway's job_list) "
                           "before starting it again.")}
    gj = answer["job"]
    shown = sanitize.clip(sanitize.scrub(command if command else shlex.join(argv or [])), 500)
    with state.lock():
        reg = _registry()
        for r in reg["jobs"].values():
            if r.get("gateway_job") == gj:      # the same start again (idempotent): one record
                return {"ok": True, "job": public(r), "supervisor": "gateway", "new": False}
        jid = _new_id(reg, name)
        rec = {"id": jid, "name": name, "owner": "gateway", "gateway_job": gj, "command": shown, "cwd": cwd,
               "started": state.now_iso(), "started_ts": time.time(), "session_id": session_id, "task": task,
               "redo_hint": redo_hint, "state": str(answer.get("state") or "starting"), "seen_ts": time.time(),
               "seen_epoch": g.epoch, "machine_id": g.machine_id, "gateway_version": g.version,
               "on_pause": on_pause, "on_cancel": on_cancel if on_cancel in CANCEL_POLICIES else "interrupt",
               "idempotency_key": key, "host": "localhost"}
        if task_id:
            rec["task_id"] = task_id
        if isinstance(answer.get("op"), str) and gateway.SAFE.fullmatch(answer["op"]):
            rec["gateway_op"] = answer["op"]
        reg["jobs"][jid] = rec
        _save(reg)
    return {"ok": True, "job": public(rec), "supervisor": "gateway", "new": answer.get("new") is not False}


def _refused(e: gateway.CallError, key: str) -> dict[str, Any]:
    """A start the gateway refused while serving this session (its policy,
    the session's pause, a bad request, a code checkpoint doesn't know): the
    gateway's decision, with its reason. A refusal certainly started
    nothing there, and nothing starts here: running it in the own runner
    would go around the gateway (0.7.2). The journal records it
    (mcp_server._job_record: job.started, ok false, outcome refused)."""
    how = f"HTTP {e.status}" + (f", {e.code}" if e.code else ", no refusal code")
    out: dict[str, Any] = {
        "ok": False, "supervisor": "gateway", "outcome": "refused", "refused_by": "gateway",
        "code": e.code or "", "status": e.status, "idempotency_key": key,
        "reason": (f"refused by the gateway: {e.message} ({how})"
                   + (f" [rule: {e.rule}]" if e.rule else "")
                   + ". The job was NOT started: not by the gateway, and not by checkpoint's own runner "
                     "(checkpoint never runs a job the gateway refused).")}
    if e.rule:
        out["rule"] = e.rule
    return out


def _fresh_op() -> str:
    from . import journal
    return journal.new_id("o")


def note_gateway_states(found: dict[str, dict[str, Any]], epoch: int | None, lost: set[str]) -> None:
    """Store what the gateway said about its jobs (cleaned: gateway.clean_job,
    or an event's state) on their registry records; `lost`: jobs a new epoch's
    snapshot doesn't list (unknown and gone, never done)."""
    if not found and not lost:
        return
    with state.lock():
        reg = _registry()
        changed = False
        for rec in reg["jobs"].values():
            gj = rec.get("gateway_job")
            if owner(rec) != "gateway" or not gj or is_final(rec):
                continue
            if gj in found:
                changed = _apply(rec, found[gj], epoch) or changed
            elif gj in lost:
                rec.update(state="unknown", gone=True, seen_ts=time.time(), seen_epoch=epoch,
                           unknown_why="the gateway restarted and no longer lists it")
                changed = True
        if changed:
            _save(reg)


def _apply(rec: dict[str, Any], j: dict[str, Any], epoch: int | None) -> bool:
    before = dict(rec)
    st_ = j.get("state")
    if isinstance(st_, str) and st_:
        rec["state"] = st_
    for k in ("exit", "signal"):
        if j.get(k) is not None:
            rec["exit_code" if k == "exit" else "signal"] = j[k]
    if isinstance(j.get("ended"), (int, float)) and rec.get("state") in gateway.FINAL_STATES:
        rec["ended_ts"] = float(j["ended"])
        rec["ended"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(float(j["ended"])))
    if j.get("gone"):
        rec["gone"] = True
    else:
        rec.pop("gone", None)      # listed again (re-adopted): its state is the gateway's word again
    if j.get("detached"):
        rec["gateway_detached"] = True
    if isinstance(j.get("host"), str):
        rec["host_id"] = j["host"]
    if isinstance(j.get("host_name"), str):
        rec["host"] = j["host_name"]
    rec["seen_ts"] = time.time()
    rec["seen_epoch"] = epoch
    rec.pop("unknown_why", None)
    return rec != before


def _gateway_view(reg: dict[str, Any]) -> tuple[dict[str, dict[str, Any]] | None, int | None, str]:
    """(the gateway's jobs by id, its epoch, '') from one snapshot, asked
    only when the registry has gateway jobs that aren't final; (None, None,
    why) when it can't be asked; (None, None, '') when there is nothing to
    ask. Called outside the state lock (it is a network call)."""
    if not any(owner(r) == "gateway" and r.get("gateway_job") and not is_final(r) for r in reg["jobs"].values()):
        return None, None, ""
    g = gateway.discover()
    if not g.ok:
        return None, None, g.reason
    try:
        snap = gateway.snapshot(g)
    except gateway.CallError as e:
        return None, None, e.text()
    listed = {j["job"]: j for j in (gateway.clean_job(x) for x in snap.get("jobs") or []) if j}
    return listed, snap.get("epoch") if isinstance(snap.get("epoch"), int) else g.epoch, ""


def _apply_view(reg: dict[str, Any], listed: dict[str, dict[str, Any]], epoch: int | None) -> bool:
    changed = False
    for rec in reg["jobs"].values():
        if owner(rec) != "gateway" or not rec.get("gateway_job") or is_final(rec):
            continue
        j = listed.get(rec["gateway_job"])
        if j is not None:
            changed = _apply(rec, j, epoch) or changed
        elif is_unknown(rec) and rec.get("gone"):
            continue    # already lost (with its reason); unknown until a snapshot lists it again
        elif rec.get("seen_epoch") is not None and rec.get("seen_epoch") != epoch:
            rec.update(state="unknown", gone=True, seen_ts=time.time(), seen_epoch=epoch,
                       unknown_why="the gateway restarted and no longer lists it")
            changed = True
        else:
            # Same epoch, no record of it (kept 24 h there): its outcome can't
            # be read any more; unknown, never done.
            rec.update(state="unknown", gone=True, seen_ts=time.time(),
                       unknown_why="the gateway has no record of it any more")
            changed = True
    return changed


def _gateway_refresh(rec: dict[str, Any]) -> None:
    """Pick up what the job's shell reported about the gateway env file."""
    if rec.get("gateway_env") != "pending":
        return
    try:
        said = Path(rec["exit_file"]).with_name("gateway_env").read_text().strip()
    except (KeyError, OSError):
        said = ""
    if said in ("applied", "failed"):
        rec["gateway_env"] = said
    elif rec.get("state") in ("exited", "killed", "vanished"):
        rec["gateway_env"] = "unknown"   # ended before its shell got that far


def _refresh(rec: dict[str, Any], ptable: dict[int, dict[str, Any]]) -> dict[str, Any]:
    _refresh_state(rec, ptable)
    _gateway_refresh(rec)
    return rec


def _refresh_state(rec: dict[str, Any], ptable: dict[int, dict[str, Any]]) -> dict[str, Any]:
    if rec.get("state") in ("exited", "killed", "vanished"):
        return rec
    if rec.get("state") == "stop_failed":
        # job_stop couldn't end every process of the group; done once it's empty.
        if not procs.group_alive(rec["pgid"], ptable):
            rec["state"] = "killed"
            rec["ended"] = state.now_iso()
            rec["ended_ts"] = time.time()
        return rec
    exitf = Path(rec["exit_file"])
    code = _exit_text(exitf)
    if code is not None:
        try:
            rec["exit_code"] = int(code)
        except ValueError:
            rec["exit_code"] = None
        rec["state"] = "exited"
        end = exitf.stat().st_mtime
        rec.setdefault("ended", time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(end)))
        rec.setdefault("ended_ts", end)
        return rec
    if not ptable:
        return rec  # no process table (ps failed): can't tell, so leave it as it was
    p = ptable.get(rec["pid"])
    if p and rec["id"] in p["cmd"]:
        rec["state"] = "running"
        return rec
    if p and not p["stat"].startswith("Z") and abs(float(p.get("start") or 0) - float(rec.get("started_ts") or 0)) < 2:
        # A live process at the wrapper's pid that started when the job did is
        # the wrapper, its command line not readable yet: just after Popen,
        # Linux can show it as "[sh]" (exec still setting up its arguments).
        # Calling that vanished would be final and wrong.
        rec["state"] = "running"
        return rec
    if (not p or p["stat"].startswith("Z")) and procs.group_alive(rec["pgid"], ptable):
        # The wrapper is gone (killed on its own) but the command it started
        # still runs in the job's process group: job_stop can still stop it.
        # (A live process at the wrapper's pid that isn't ours means the pid,
        # and so the whole group, is long gone.)
        rec["state"] = "running"
        return rec
    rec["state"] = "killed" if rec.get("stop_reason") else "vanished"
    rec.setdefault("ended", state.now_iso())
    return rec


def _exit_text(exitf: Path) -> str | None:
    """The exit code the job's shell wrote, or None while it hasn't (no file,
    or the empty one start() made)."""
    try:
        text = exitf.read_text().strip()
    except OSError:
        return None
    return text or None


def public(rec: dict[str, Any]) -> dict[str, Any]:
    out = {k: rec.get(k) for k in ("id", "name", "state", "exit_code", "command", "cwd",
                                   "started", "ended", "task", "redo_hint", "stop_reason",
                                   "log", "gateway_env", "gateway_env_file", "on_cancel", "detached")}
    out["supervisor"] = owner(rec)
    if owner(rec) == "gateway":
        for k in ("gateway_job", "on_pause", "signal", "host", "unknown_why", "last_seen", "gone"):
            out[k] = rec.get(k)
    elif rec.get("why_own"):
        out["why_own"] = rec["why_own"]
    if rec.get("started_ts"):
        end = rec.get("ended_ts") or time.time()
        out["runtime_s"] = round(end - rec["started_ts"], 1)
    return {k: v for k, v in out.items() if v not in (None, "")}


def refresh_all(gateway_too: bool = True) -> dict[str, dict[str, Any]]:
    """Every job, refreshed: the own runner's from the process table and
    their exit files, the gateway's from one snapshot (when it has live
    ones). A gateway job that can't be asked now reads as unknown (the
    stored record keeps its last seen state)."""
    ptable = procs.table()
    listed, epoch, unreachable = _gateway_view(_registry()) if gateway_too else (None, None, "")
    with state.lock():
        reg = _registry()
        changed = False
        for rec in reg["jobs"].values():
            if owner(rec) == "gateway":
                continue
            before = dict(rec)
            _refresh(rec, ptable)
            if before.get("state") == "running" and rec["state"] != "running":
                rec.setdefault("ended_ts", time.time())
            changed = changed or rec != before
        if listed is not None:
            changed = _apply_view(reg, listed, epoch) or changed
        if changed:
            _save(reg)
    if unreachable:
        for rec in reg["jobs"].values():
            if owner(rec) == "gateway" and not is_final(rec) and not is_unknown(rec):
                rec["last_seen"] = {"state": rec.get("state"), "at": rec.get("seen_ts")}
                rec["state"] = "unknown"
                rec["unknown_why"] = f"the gateway isn't available now ({sanitize.clip(unreachable, 160)})"
    return reg["jobs"]


def find(name: str) -> dict[str, Any] | None:
    jobs = refresh_all()
    if name in jobs:
        return jobs[name]
    hits = [j for j in jobs.values() if j["name"] == name or (name and j.get("gateway_job") == name)]
    return max(hits, key=lambda j: j.get("started_ts", 0)) if hits else None


def status(name: str | None = None, session_id: str | None = None) -> dict[str, Any]:
    if name:
        rec = find(name)
        if rec and owner(rec) == "gateway":
            return _status_gateway(rec)
        return {"ok": True, "job": public(rec)} if rec else {"ok": False, "reason": f"no job named {name}"}
    jobs = refresh_all().values()
    if session_id:
        jobs = [j for j in jobs if j.get("session_id") == session_id]
    return {"ok": True, "jobs": [public(j) for j in sorted(jobs, key=lambda j: j.get("started_ts", 0))]}


def _status_gateway(rec: dict[str, Any]) -> dict[str, Any]:
    """One gateway job, asked of the gateway now (GET /v1/jobs/<id>)."""
    g = gateway.discover()
    if not g.ok or is_final(rec):
        return {"ok": True, "job": public(rec)}
    try:
        j = gateway.clean_job(gateway.job_get(g, rec["gateway_job"]))
    except gateway.CallError as e:
        if is_unknown(rec) and rec.get("gone"):
            return {"ok": True, "job": public(rec)}     # still lost: its own reason says more
        out = dict(rec, last_seen={"state": rec.get("state"), "at": rec.get("seen_ts")}, state="unknown",
                   unknown_why=f"the gateway couldn't be asked ({e.text()})")
        return {"ok": True, "job": public(out)}
    if j:
        note_gateway_states({j["job"]: j}, g.epoch, set())
        rec = _registry()["jobs"].get(rec["id"], rec)
    return {"ok": True, "job": public(rec)}


def tail(name: str, lines: int = 40, rec: dict[str, Any] | None = None) -> dict[str, Any]:
    rec = rec or find(name)
    if not rec:
        return {"ok": False, "reason": f"no job named {name}"}
    lines = max(1, min(int(lines or 40), 500))
    if owner(rec) == "gateway":
        return _tail_gateway(rec, lines)
    try:
        with open(rec["log"], "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200_000))
            text = f.read().decode("utf8", "replace")
    except OSError:
        text = ""
    return {"ok": True, "job": public(rec), "tail": "\n".join(text.splitlines()[-lines:])}


def _tail_gateway(rec: dict[str, Any], lines: int) -> dict[str, Any]:
    """The gateway's tail of its job (scrubbed there, at most 64 KiB), cut to
    the last `lines` lines."""
    g = gateway.discover()
    if not g.ok:
        return {"ok": False, "job": public(rec), "reason": f"the gateway isn't available ({g.reason}): its output "
                "can't be read now"}
    try:
        t = gateway.job_tail(g, rec["gateway_job"], min(65536, max(4096, lines * 256)))
    except gateway.CallError as e:
        return {"ok": False, "job": public(rec), "reason": f"the gateway couldn't give its output ({e.text()})"}
    text = t.get("text") if isinstance(t.get("text"), str) else ""
    parts = text.split("\n")
    cut = len(parts) > lines + 1
    return {"ok": True, "job": public(rec), "tail": "\n".join(parts[-(lines + 1):] if cut else parts),
            "truncated": bool(t.get("truncated")) or cut, "supervisor": "gateway"}


def _wait_group(pgid: int, seconds: float) -> bool:
    """Wait until no process of the group is left. True if it emptied."""
    deadline = time.time() + seconds
    while True:
        reap_started()
        if not procs.group_alive(pgid):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.2)


def _signal_group(pgid: int, sig: int) -> None:
    # EPERM on macOS: only a zombie is left, so there is nothing to signal.
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def stop(name: str, reason: str = "", grace: float | None = None) -> dict[str, Any]:
    """SIGTERM the job's process group; SIGKILL whatever is still in it after
    the grace period. Reported as stopped only once the group is empty: the
    wrapper sh dies at the first SIGTERM, but a child that ignores TERM
    doesn't."""
    rec = find(name)
    if not rec:
        return {"ok": False, "reason": f"no job named {name}"}
    if owner(rec) == "gateway":
        return _stop_gateway(rec, reason, state.JOB_STOP_GRACE_SECONDS if grace is None else grace)
    if rec["state"] not in ("running", "stop_failed"):
        return {"ok": True, "job": public(rec), "note": "not running", "outcome": "stopped", "already_ended": True}
    pgid = rec["pgid"]
    _signal_group(pgid, signal.SIGTERM)
    gone = _wait_group(pgid, state.JOB_STOP_GRACE_SECONDS if grace is None else grace)
    if not gone:
        _signal_group(pgid, signal.SIGKILL)
        gone = _wait_group(pgid, 2.0)
    with state.lock():
        reg = _registry()
        r = reg["jobs"][rec["id"]]
        r["stop_reason"] = reason or "stopped"
        if gone:
            r["state"] = "killed"
            r["ended"] = state.now_iso()
            r["ended_ts"] = time.time()
        else:
            r["state"] = "stop_failed"
        _save(reg)
    out = tail(rec["id"], 10)
    out["stopped"] = gone
    out["outcome"] = "stopped" if gone else "stop_failed"
    if not gone:
        out["ok"] = False
        out["reason"] = (f"stop_failed: processes of job {rec['id']} (process group {pgid}) "
                         "are still running after SIGKILL")
    return out


def _gw_wait(g: gateway.Gateway, gj: str, seconds: float) -> dict[str, Any] | None:
    """Poll the gateway until the job isn't live (or the time is up); its last view."""
    deadline = time.time() + seconds
    last = None
    while True:
        try:
            last = gateway.clean_job(gateway.job_get(g, gj)) or last
        except gateway.CallError:
            pass
        if last and last.get("state") not in gateway.LIVE_STATES:
            return last
        if time.time() >= deadline:
            return last
        time.sleep(0.2)


def _unknown_stop(rec: dict[str, Any], why: str) -> dict[str, Any]:
    """A stop whose effect can't be known: never reported stopped."""
    return {"ok": False, "stopped": False, "outcome": "unknown", "job": public(rec), "supervisor": "gateway",
            "reason": f"outcome unknown, it may still run: {why}"}


def _stop_gateway(rec: dict[str, Any], reason: str, grace: float) -> dict[str, Any]:
    """Terminate a gateway job through the contract: TERM to its process
    group, KILL after the grace if it still runs. Stopped only once the
    gateway says it ended; `outcome` is stopped, stop_failed (the gateway
    reports it still running, or refused the signal) or unknown (the
    gateway can't be reached, lost the job, or can't prove its state)."""
    gj = rec["gateway_job"]
    if is_final(rec):
        return {"ok": True, "job": public(rec), "note": "not running", "stopped": True, "outcome": "stopped",
                "already_ended": True, "supervisor": "gateway"}
    if is_unknown(rec) and rec.get("gone"):
        return _unknown_stop(rec, f"{rec.get('unknown_why') or 'the gateway no longer lists it'}, so it can't be "
                                  f"stopped through the gateway ({rec['id']}, {gj}); check on its host")
    g = gateway.discover()
    if not g.ok:
        return _unknown_stop(rec, f"the gateway isn't available ({g.reason}): {rec['id']} ({gj}) couldn't be "
                                  "stopped")
    how = "SIGTERM"
    try:
        gateway.job_signal(g, gj, "TERM")
    except gateway.CallError as e:
        if e.kind == "refused" and e.status == 404:
            return _unknown_stop(rec, f"the gateway has no record of {gj} any more ({e.text()})")
        if e.kind == "refused":
            # The gateway's decision (another session's job, the user holds it,
            # its policy...): never turned into a signal of checkpoint's own.
            return {"ok": False, "stopped": False, "outcome": "stop_failed", "job": public(rec),
                    "supervisor": "gateway", "refused_by": "gateway", "code": e.code or "",
                    "reason": (f"refused by the gateway: the stop ({e.text()}); it was not stopped (checkpoint never "
                               "signals a gateway job itself) and still runs there")}
        return _unknown_stop(rec, f"the stop didn't get a whole answer from the gateway ({e.text()})")
    last = _gw_wait(g, gj, grace)
    if last is None or last.get("state") in gateway.LIVE_STATES:
        how = "SIGTERM, then SIGKILL after the grace period"
        with contextlib.suppress(gateway.CallError):
            gateway.job_signal(g, gj, "KILL")
        last = _gw_wait(g, gj, 2.0) or last
    st_ = (last or {}).get("state")
    gone = st_ in gateway.FINAL_STATES
    with state.lock():
        reg = _registry()
        r = reg["jobs"].get(rec["id"], rec)
        r["stop_reason"] = reason or "stopped"
        if last:
            _apply(r, last, g.epoch)
        _save(reg)
        rec = r
    out: dict[str, Any] = {"ok": gone, "stopped": gone, "job": public(rec), "how": how, "supervisor": "gateway",
                           "outcome": "stopped" if gone else ("stop_failed" if st_ in gateway.LIVE_STATES
                                                              else "unknown")}
    if out["outcome"] == "stop_failed":
        out["reason"] = f"stop_failed: the gateway still reports {gj} as {st_} after {how}"
    elif not gone:
        why = rec.get("unknown_why") or "it can't prove its state"
        out["reason"] = (f"outcome unknown, it may still run: after {how} the gateway "
                         + (f"reports {gj} as {st_} ({why})" if st_ else f"couldn't be asked about {gj}"))
    return out


def detach(name: str, reason: str = "") -> dict[str, Any]:
    """Release a running job from its task (/check:cancel detach): it keeps
    running, is no longer the task's child work, and stays visible and
    stoppable (job_status, /check:stop)."""
    rec = find(name)
    if not rec:
        return {"ok": False, "reason": f"no job named {name}"}
    with state.lock():
        reg = _registry()
        r = reg["jobs"][rec["id"]]
        r["detached"] = True
        r["detached_from"] = r.get("task_id") or r.get("session_id") or ""
        r["detach_reason"] = reason or "detached"
        _save(reg)
    out = {"ok": True, "job": public(reg["jobs"][rec["id"]])}
    if owner(rec) == "gateway":
        # CONTRACT-v1 has no call to release a job from its task: checkpoint's
        # records do; the gateway keeps running and listing it as it was started.
        out["note"] = ("released from the task in checkpoint's records; it keeps running in the gateway, which still "
                       "lists it under its old task_ref")
    return out


def _ended_ts(rec: dict[str, Any]) -> float:
    if rec.get("ended_ts"):
        return float(rec["ended_ts"])
    for key in ("exit_file", "log"):
        try:
            return os.path.getmtime(rec[key])
        except (KeyError, OSError, TypeError):
            continue
    return float(rec.get("started_ts") or 0)


def prune(days: float) -> int:
    """Forget finished jobs that ended more than `days` ago, and delete their
    folders (log and exit code). Running jobs, recent ones and gateway jobs
    whose outcome is unknown (they may still run) are kept."""
    reg_jobs = refresh_all()
    cutoff = time.time() - days * 86400
    old = [jid for jid, j in reg_jobs.items() if is_final(j) and _ended_ts(j) < cutoff]
    if not old:
        return 0
    with state.lock():
        reg = _registry()
        for jid in old:
            reg["jobs"].pop(jid, None)
        _save(reg)
    base = state.p("jobs").resolve()
    for jid in old:
        d = state.p("jobs", state.safe_id(jid)).resolve()
        if d.parent == base and d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
    return len(old)
