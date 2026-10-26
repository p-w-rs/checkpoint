"""Capture: hook and mod events into the journal (PLAN-v4 phase 2).

The every-call path stays without Python: hooks/fast.sh (and the mod, through
`env.sh spool`) put each event's payload in the session's spool
(hooks/lib.sh ck_spool):

    spool/<session>/<pid>.ev        one payload (as Claude Code sent it, cut at SPOOL_MAX; while no
                                    runtime is there or on its way, only what it says about itself:
                                    `checkpoint_meta`, meta=1)
    spool/<session>/index           one line per payload, appended in one write:
                                    1 TAB EVENT TAB FILE TAB BYTES [TAB h=HASH] [TAB cut=BYTES] [TAB meta=1]
    spool/<session>/index.<n>       an index rotated away, kept until nothing more can land in it
    spool/<session>/index.s<n>      one the shell rotated away at a trim while no runtime was there (read
                                    before the current index; `.trimdone`: the highest one the trim finished)
    spool/<session>/cursor          how far the index files were ingested (and the inode of the index
                                    file its offset is in)
    spool/<session>/.size           "<records> <bytes>" the shell keeps for its cap (written here after
                                    each ingest: what is left)
    spool/<session>/.lock           one ingester per spool at a time

The shell caps each spool (SPOOL_KEEP_COUNT records, SPOOL_KEEP_BYTES): past
either it drops the oldest payloads, after a recording.gap record that names
them; their index lines then point at nothing, and they are counted, not
reported as problems. While no runtime is there it also moves the index to
index.s<n> at each trim (nothing else would rotate it), and those are read
here before the current index. It also removes, at a session start, spools nothing was
written to for SPOOL_KEEP_DAYS whose session is gone.

Python ingests it at the moments it runs anyway: SessionStart, Stop and
SessionEnd, PreCompact/PostCompact, the every-N PostToolUse, a pause, the
/check:* commands, the MCP server's tool calls, and the mod's timer. Ingesting
turns events into sanitized records (sanitize.py), appends them to the task's
journal (journal.py), and only then deletes the payload files and moves the
cursor: a write that fails leaves the spool as it was (the next moment tries
again) and is recorded for /check:doctor, the status line and the hook's
output. Re-ingesting after a crash between the two is harmless: records carry
the event's op (tool_use_id, prompt id, or a hash of the payload and its
time) and the journal drops a second record of the same kind and op.

Tasks: a session's records go to its task's journal. A session started fresh
(`startup`, `clear`) begins a task; `resume`, `fork` and `compact` continue
the parent's when it is known (the same session id, the transcript's earlier
session id, or the previous session of the same Claude process for `clear`,
which is lineage only). `/check:resume <task>` makes a session adopt a task.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import authz, journal, sanitize, state

SPOOL_MAX = 262144          # hooks/lib.sh CK_SPOOL_MAX
HASH_MAX = 16 * 1024 * 1024  # hooks/lib.sh CK_HASH_MAX
SPOOL_KEEP_COUNT = 10000     # hooks/lib.sh CK_SPOOL_KEEP_COUNT: records a session's spool holds at most
SPOOL_KEEP_BYTES = 50 << 20  # hooks/lib.sh CK_SPOOL_KEEP_BYTES: and stored bytes
SPOOL_KEEP_DAYS = 7          # hooks/lib.sh CK_SPOOL_KEEP_DAYS: a dead session's quiet spool is removed then
INDEX_ROTATE = 256 * 1024   # the index is rotated once fully ingested past this size
ROTATED_KEEP_SECONDS = 30   # a rotated index is dropped once quiet this long and ingested
ORPHAN_AGE_SECONDS = 60     # a payload whose index line never came (its hook died)
PROMPT_MAX = 4000
DIRTY_MAX = 200             # files hashed for a validation's version

EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
TASK_TOOLS = ("TaskCreate", "TaskUpdate", "TodoWrite")

# Commands that look like tests, builds and checks: their results are
# validation evidence (with the git state they ran against). fast.sh's glob
# is wider, so every one of these gets its snapshot.
VALIDATION_RE = re.compile(r"""(?x)(?:^|[\s;&|(`'"]|/)(?:
    (?P<test>pytest|py\.test|tox|nox|nosetests|unittest|go\s+test|cargo\s+(?:test|nextest)
      |(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test|npx\s+(?:jest|vitest|mocha|playwright)|jest|vitest|mocha
      |rspec|phpunit|ctest|meson\s+test|dotnet\s+test|swift\s+test|deno\s+test|bun\s+test
      |mvn\s+(?:\S+\s+)*(?:test|verify)|gradlew?\s+(?:\S+\s+)*(?:test|check)|rake\s+(?:test|spec)
      |pixi\s+run\s+[\w-]*test[\w-]*|make\s+(?:\S+\s+)*(?:test|check)\b
      |[\w./-]*(?:run_tests|runtests|test_\w+|tests?)(?:\.sh|\.py)?(?=\s|$|;|&))
    |(?P<build>make|ninja|cmake\s+--build|cargo\s+(?:build|check)|go\s+(?:build|vet)|tsc
      |(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:build|compile)|dotnet\s+build|swift\s+build|xcodebuild
      |gradlew?\s+(?:\S+\s+)*(?:build|assemble)|mvn\s+(?:\S+\s+)*(?:package|compile|install)
      |pixi\s+run\s+[\w-]*build[\w-]*|python3?\s+-m\s+build)
    |(?P<check>mypy|ruff|pyright|flake8|pylint|eslint|shellcheck|cargo\s+clippy|black\s+--check
      |(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:lint|typecheck|check)|pixi\s+run\s+[\w-]*(?:lint|check)[\w-]*
      |claude\s+plugin\s+(?:test|validate))
    )\b""")


def validation_kind(command: str) -> str:
    """'test', 'build', 'check' or '' for a Bash command."""
    m = VALIDATION_RE.search(command or "")
    if not m:
        return ""
    return next(k for k in ("test", "build", "check") if m.group(k))


def spool_dir(sid: str) -> Path:
    return state.p("spool", state.safe_id(sid))


@dataclass
class Event:
    event: str
    payload: dict[str, Any] | None
    ts: float
    name: str = ""
    raw: str = ""            # the cut payload's text, when it isn't whole JSON
    nbytes: int = 0
    cut: bool = False
    hash: str = ""           # h=... from fast.sh: the edited file's state
    extras: dict[str, str] = field(default_factory=dict)   # the index line's other KEY=VALUE fields
    orphan: bool = False
    src: str = ""
    meta: str = ""           # the shell kept only what the payload says about itself: why (no runtime ...)
    dropped: tuple[int, int] | None = None   # a recording.gap of dropped payloads: (events, bytes) found gone

    def get(self, key: str, default: Any = None) -> Any:
        if self.payload is not None:
            return self.payload.get(key, default)
        m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"((?:\\.|[^"\\])*)"', self.raw)
        if not m:
            return default
        try:
            return json.loads('"' + m.group(1) + '"')
        except ValueError:
            return m.group(1)


@dataclass
class IngestResult:
    sid: str
    task: str = ""
    events: int = 0
    records: int = 0
    error: str = ""
    new_failure: bool = False   # this ingest failed and the session hadn't been told yet
    started: bool = False       # a SessionStart event was among them
    last_seq: int = 0           # seq of the last record written (the `extra` ones come last)
    problems: list[str] = field(default_factory=list)


@contextlib.contextmanager
def _spool_lock(d: Path) -> Iterator[None]:
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(d / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_lines(path: Path, offset: int, info: dict[str, int] | None = None) -> tuple[list[tuple[int, str]], int]:
    """Complete lines of an index file from `offset`: [(offset, line)], end.
    `info` gets the inode of the file read ("ino")."""
    try:
        with open(path, "rb") as f:
            if info is not None:
                info["ino"] = os.fstat(f.fileno()).st_ino
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    out = []
    pos = 0
    while True:
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        out.append((offset + pos, data[pos:nl].decode("utf-8", "replace")))
        pos = nl + 1
    return out, offset + pos


def _load_event(d: Path, line: str, src: str,
                missing: dict[str, tuple[str, int]] | None = None) -> tuple[Event | None, str, str]:
    """(event, payload file name, problem) for one index line. A payload
    that is gone goes to `missing` (name: (problem, bytes)) instead: the
    shell may have dropped it (a recording.gap in the same ingest says)."""
    parts = line.split("\t")
    if len(parts) < 4 or parts[0] != "1":
        return None, "", f"unreadable spool line at {src}"
    ev, name, nbytes = parts[1], parts[2], parts[3]
    extras = dict(p.split("=", 1) for p in parts[4:] if "=" in p)
    if "/" in name or not name.endswith(".ev"):
        return None, "", f"bad payload name at {src}"
    path = d / name
    try:
        st = path.stat()
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        prob = f"payload {name} named at {src} is missing"
        if missing is None:
            return None, name, prob
        missing[name] = (prob, int(nbytes) if nbytes.isdigit() else 0)
        return None, name, ""
    try:
        n = int(nbytes)
    except ValueError:
        n = len(text)
    cut = "cut" in extras
    payload = None
    if not cut:
        try:
            payload = json.loads(text)
            if not isinstance(payload, dict):
                payload = None
        except ValueError:
            payload = None
    meta = ""
    if payload is not None and isinstance(payload.get("checkpoint_meta"), dict):
        meta = sanitize.clip(str(payload["checkpoint_meta"].get("why") or "no runtime"), 120)
    elif "meta" in extras:
        meta = "no runtime"
    e = Event(event=ev, payload=payload, ts=st.st_mtime, name=name, raw="" if payload is not None else text,
              nbytes=n, cut=(cut or payload is None) and not meta, hash=extras.get("h", ""), extras=extras, src=src,
              meta=meta)
    return e, name, ("" if payload is not None or cut else f"payload {name} at {src} isn't whole JSON")


def _orphans(d: Path, seen: set[str]) -> list[Event]:
    out = []
    now = time.time()
    for p in d.glob("*.ev"):
        if p.name in seen:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if now - st.st_mtime < ORPHAN_AGE_SECONDS:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            payload = json.loads(text)
        except (OSError, ValueError):
            payload, text = None, ""
        if not isinstance(payload, dict):
            payload = None
        ev = (payload or {}).get("hook_event_name") or "unknown"
        out.append(Event(event=str(ev), payload=payload, ts=st.st_mtime, name=p.name, raw="" if payload else text,
                         cut=payload is None, orphan=True, src=f"orphan {p.name}"))
    return sorted(out, key=lambda e: e.ts)


# -- tasks and lineage ------------------------------------------------------------

def _transcript_parent(tp: str, sid: str) -> str:
    """The session a resumed or forked transcript came from: the first
    sessionId in it that isn't this one (supplementary evidence)."""
    try:
        with open(tp, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 200:
                    break
                m = re.search(r'"sessionId"\s*:\s*"([^"]+)"', line)
                if m and m.group(1) != sid:
                    return m.group(1)
    except OSError:
        pass
    return ""


def _process_parent(sid: str, pid: Any) -> str:
    """The previous session of the same Claude process (before a /clear)."""
    if not isinstance(pid, int):
        return ""
    me = state.load_session(sid)
    mine = state.started_ts(me) or time.time()
    best = None
    for s in state.all_sessions():
        if s["session_id"] == sid or s.get("claude_pid") != pid:
            continue
        if state.started_ts(s) <= mine and (best is None or state.started_ts(s) > state.started_ts(best)):
            best = s
    return best["session_id"] if best else ""


def task_of(sid: str) -> str:
    """The session's task, making one when it has none yet."""
    rec = state.load_session(sid)
    t = rec.get("task_id")
    if isinstance(t, str) and t:
        return t
    t = journal.new_id("t")
    state.update_session(sid, task_id=t)
    return t


def start_task(sid: str, source: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    """At a SessionStart: (task, parent session, how the parent is known).
    resume/fork/compact continue the parent's task; startup and clear start
    a new one (clear names the previous session as its parent, for lineage)."""
    rec = state.load_session(sid)
    have = rec.get("task_id") if isinstance(rec.get("task_id"), str) else ""
    parent, how = rec.get("parent_session") or "", rec.get("parent_how") or ""
    if not parent:
        if source in ("resume", "fork"):
            parent = _transcript_parent(payload.get("transcript_path") or rec.get("transcript_path") or "", sid)
            how = "transcript" if parent else ""
        elif source == "clear":
            parent = _process_parent(sid, rec.get("claude_pid"))
            how = "same Claude process" if parent else ""
    if have:
        task = have
    elif parent and source in ("resume", "fork", "compact"):
        task = state.load_session(parent).get("task_id") or journal.new_id("t")
    else:
        task = journal.new_id("t")
    fields: dict[str, Any] = {}
    if task != have:
        fields["task_id"] = task
    if parent and parent != rec.get("parent_session"):
        fields.update(parent_session=parent, parent_how=how)
    if fields:
        state.update_session(sid, **fields)
    return task, parent, how


# -- git and files ------------------------------------------------------------------

def _git(cwd: str, *args: str, timeout: float = 5) -> str | None:
    """Read-only git (GIT_OPTIONAL_LOCKS=0: never rewrites the index), the
    user's umask. None when it fails."""
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=timeout,
                           env=dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C"),
                           umask=state.user_umask())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return r.stdout.decode("utf-8", "surrogateescape") if r.returncode == 0 else None


def real(path: str | Path) -> str:
    """A path with symlinks resolved, for comparing a tool's path with git's
    (git names files below its own resolved root)."""
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return str(path)


def file_hash(path: str | Path) -> str:
    """A file's state as fast.sh's ck_hash gives it: sha256, absent, dir,
    unreadable, big:<bytes>."""
    p = Path(path)
    try:
        st = p.stat()
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable"
    if p.is_dir():
        return "dir"
    if st.st_size > HASH_MAX:
        return f"big:{st.st_size}"
    h = hashlib.sha256()
    try:
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return "unreadable"
    return h.hexdigest()


def repo_identity(cwd: str) -> dict[str, Any]:
    """Root, branch, HEAD and worktree of the git repository at cwd ({} outside one)."""
    if not cwd or not os.path.isdir(cwd):
        return {}
    out = _git(cwd, "rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir")
    if not out:
        return {}
    lines = out.splitlines()
    root = lines[0] if lines else ""
    gitdir = lines[1] if len(lines) > 1 else ""
    common = lines[2] if len(lines) > 2 else ""
    if common and not os.path.isabs(common):
        common = os.path.normpath(os.path.join(cwd, common))
    head = (_git(cwd, "rev-parse", "--verify", "-q", "HEAD") or "").strip()
    branch = (_git(cwd, "symbolic-ref", "-q", "--short", "HEAD") or "").strip()
    return {"root": root, "head": head, "branch": branch or ("(detached)" if head else ""),
            "worktree": bool(gitdir and common and os.path.realpath(gitdir) != os.path.realpath(common))}


def git_dirty(root: str) -> list[tuple[str, str]]:
    """(status, path) of changed and untracked files (porcelain v1, -z)."""
    out = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
    if out is None:
        return []
    items, parts, i = [], out.split("\0"), 0
    while i < len(parts):
        e = parts[i]
        i += 1
        if len(e) < 4:
            continue
        st, path = e[:2], e[3:]
        if st[0] in "RC":
            i += 1  # the rename's source follows
        items.append((st, path))
    return items


def version(cwd: str, extra: list[str] | None = None) -> dict[str, Any]:
    """The git state a validation runs against: HEAD, branch, and a hash of
    every changed or untracked file (DIRTY_MAX at most), plus `extra` files
    (the task's changed files outside git)."""
    rid = repo_identity(cwd)
    out: dict[str, Any] = {"at": time.time()}
    files: dict[str, str] = {}
    if rid:
        out.update(root=rid["root"], head=rid["head"], branch=rid["branch"])
        dirty = git_dirty(rid["root"])
        out["dirty_count"] = len(dirty)
        for st, rel in dirty[:DIRTY_MAX]:
            files[os.path.join(rid["root"], rel)] = file_hash(os.path.join(rid["root"], rel))
        if len(dirty) > DIRTY_MAX:
            out["dirty_cut"] = len(dirty) - DIRTY_MAX
    have = {real(k) for k in files}
    for p in extra or []:
        if real(p) not in have and len(files) < DIRTY_MAX + 50:
            files[p] = file_hash(p)
            have.add(real(p))
    out["files"] = files
    return out


# -- events -> records ------------------------------------------------------------------

def _op(e: Event, prefix: str) -> str:
    """A stable op for an event without an id of its own: the same payload
    at the same time is the same event (a duplicate delivery)."""
    body = json.dumps(e.payload, sort_keys=True, default=str) if e.payload is not None else e.raw
    h = hashlib.sha256(f"{e.event}|{int(e.ts)}|{body}".encode("utf-8", "surrogatepass")).hexdigest()[:20]
    return f"{prefix}:{h}"


def _is_user_prompt(e: Event, text: str) -> bool:
    src = e.get("source")
    if src not in (None, "", "user", "sdk"):
        return False
    t = text.lstrip()
    return not any(t.startswith(x) for x in state.NON_USER_PROMPT_PREFIXES)


def input_sha(inp: Any) -> str:
    """A short hash of a tool call's input as the journal keeps it (already
    sanitized: hashing the raw input would let a guess at a masked secret be
    checked against it): what links a permission dialog to its call."""
    try:
        body = json.dumps(inp, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        body = repr(inp)
    return hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _parse_exit(err: str) -> int | None:
    m = sanitize.EXIT_RE.match(err or "")
    return int(m.group(1)) if m else None


def _edit_path(tool: str, inp: Any, cwd: str) -> str:
    if not isinstance(inp, dict):
        return ""
    fp = inp.get("notebook_path") if tool == "NotebookEdit" else inp.get("file_path")
    if not isinstance(fp, str) or not fp:
        return ""
    return fp if os.path.isabs(fp) else os.path.join(cwd or "", fp)


# Claude Code 2.1.301's task notification (a prompt of its own, or queued
# into a running turn; UserPromptSubmit sees both, measured):
#   <task-notification>\n<task-id>ID</task-id>\n<tool-use-id>toolu_..</tool-use-id>
#   [\n<task-type>..</task-type>][\n<output-file>PATH</output-file>]
#   [\n<status>completed|failed|killed|blocked</status>]\n<summary>..</summary>...
# A background Bash's summary is `Background command "<description>" completed
# (exit code N)` / `failed with exit code N` / `was stopped`; a subagent's
# `Agent "<description>" finished`. One without a status is a check-in (still
# running, or waiting for input), not an end.
_NOTE_RE = re.compile(r"<task-notification>(.*?)(?:</task-notification>|$)", re.S)
_TAG_RE = re.compile(r"<(task-id|tool-use-id|task-type|status|summary)>([^<]{0,2000})</\1>")
_EXIT_IN_SUMMARY = re.compile(r"\(exit code (-?\d+)|with exit code (-?\d+)")
FINAL_STATUSES = ("completed", "failed", "killed")


def notification_records(raw: str, base: dict[str, Any], note: dict[str, Any]) -> list[dict[str, Any]]:
    """`background.result` records for the task notifications in a prompt:
    what Claude Code says became of work a call started in the background,
    keyed by its tool_use_id (or task id) so the summary links it to the
    start. Only the status, a stated exit code and a scrubbed summary line
    are kept (never the output file's contents or a subagent's result)."""
    out = []
    for m in _NOTE_RE.finditer(raw):
        tags = {k: v.strip() for k, v in _TAG_RE.findall(m.group(1))}
        tid, task_id = tags.get("tool-use-id", ""), tags.get("task-id", "")
        if not (tid or task_id):
            continue
        status = tags.get("status", "").lower()
        summ = tags.get("summary", "")
        data: dict[str, Any] = {"tool_use_id": sanitize.clip(tid, 80), "task_id": sanitize.clip(task_id, 80),
                                "status": sanitize.clip(status or "notice", 20), "final": status in FINAL_STATUSES,
                                "via": "task-notification",
                                "summary": sanitize.clip(sanitize.scrub(summ), 200), **note}
        if tags.get("task-type"):
            data["task_type"] = sanitize.clip(tags["task-type"], 40)
        ex = _EXIT_IN_SUMMARY.search(summ)
        if ex:
            data["exit"] = int(ex.group(1) or ex.group(2))
        if not status:
            data["notice"] = ("waiting_input" if "waiting for interactive input" in summ else
                              "still_running" if "still running" in summ else "notice")
        key = tid or task_id
        h = hashlib.sha256(m.group(0).encode("utf-8", "surrogatepass")).hexdigest()[:10]
        out.append(dict(base, kind="background.result", op=f"bgr:{key}:{status or 'notice'}:{h}", data=data))
    return out


# Claude Code's tool that stops work started in the background (its ends
# come as the notifications above): its result says a status, maybe an exit
# code, for a task id (`task_id`; `shell_id` is the name it still accepts).
BG_STOP_TOOLS = ("TaskStop",)


def _bg_tool_records(tool: str, inp: Any, resp: Any, base: dict[str, Any], op: str) -> list[dict[str, Any]]:
    """A `background.result` for a TaskStop call's result, keyed by the task
    id it names (the start's background id)."""
    inp = inp if isinstance(inp, dict) else {}
    key = next((str(inp[k]) for k in ("task_id", "shell_id") if inp.get(k)), "")
    if not key:
        return []
    r = resp if isinstance(resp, dict) else {}
    t = r.get("task") if isinstance(r.get("task"), dict) else r
    status = str(t.get("status") or r.get("status") or "").lower()
    status = "killed" if status in ("", "killed", "stopped", "success", "ok") else status
    data: dict[str, Any] = {"task_id": sanitize.clip(key, 80), "status": sanitize.clip(status or "notice", 20),
                            "final": status in FINAL_STATUSES, "via": tool}
    ex = sanitize.stated_exit(t) if isinstance(t, dict) else None
    if ex is None:
        ex = sanitize.stated_exit(r)
    if ex is not None:
        data["exit"] = ex
    data["requested"] = True   # the model asked for the stop; the call's own outcome says if it worked
    return [dict(base, kind="background.result", op=f"bgr:{key}:{status or 'notice'}:{op}", data=data)]


def records_for(e: Event, sid: str) -> list[dict[str, Any]]:
    """The journal records one spooled event becomes (sanitized)."""
    base: dict[str, Any] = {"ts": e.ts, "session": sid}
    agent = e.get("agent_id")
    if agent:
        base["agent"] = sanitize.clip(agent, 80)
    note: dict[str, Any] = {}
    if e.cut:
        note["payload_cut"] = e.nbytes
    if e.meta:
        # The shell kept the event, the tool, the ids and the size only (no
        # runtime then): nothing of the input, the result or the text.
        note["payload_dropped"] = e.nbytes
    if e.orphan:
        note["orphan"] = True
    ev = e.event
    if ev == "SessionStart":
        return []  # start_records() handles it (lineage needs Python's view)
    if ev == "UserPromptSubmit":
        raw = e.get("prompt") or ""
        text = sanitize.clip(sanitize.scrub(raw), PROMPT_MAX)
        pid = e.get("prompt_id") or ""
        data = {"text": text, "chars": len(raw), "user": _is_user_prompt(e, raw), **note}
        if raw.strip().startswith("/check:") or raw.strip().startswith("/check-"):
            data["command"] = raw.strip().split()[0]
        if e.get("source"):
            data["source"] = sanitize.clip(e.get("source"), 30)
        if pid:
            data["prompt_id"] = sanitize.clip(pid, 80)
        # A prompt typed while a turn runs carries that turn's prompt_id
        # (measured, 2.1.301): the text's hash keeps it apart from the first,
        # while the same delivery twice is still one op.
        th = hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:12]
        out = [dict(base, kind="prompt", op=f"p:{pid}:{th}" if pid else _op(e, "p"), data=data)]
        if raw.lstrip().startswith("<task-notification>"):
            out.extend(notification_records(raw, base, note))
        return out
    if ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied"):
        tool = str(e.get("tool_name") or "?")
        tid = e.get("tool_use_id") or ""
        op = str(tid) if tid else _op(e, "t")
        inp = e.payload.get("tool_input") if e.payload is not None else None
        cwd = e.get("cwd") or ""
        data: dict[str, Any] = {"tool": sanitize.clip(tool, 120), **note}
        if e.meta:
            data["input"] = {}
        elif e.payload is not None:
            data["input"] = sanitize.tool_input(tool, inp)
        else:  # cut: what the prefix still says
            data["input"] = {k: sanitize.clip(sanitize.scrub(v), 500) for k in ("file_path", "notebook_path",
                             "command", "subject", "taskId", "status") if (v := e.get(k))}
        if tool in EDIT_TOOLS:
            path = _edit_path(tool, inp, cwd) if e.payload is not None else (e.get("file_path") or e.get("notebook_path") or "")
            if path:
                data["path"] = sanitize.clip(path, 500)
            h = e.hash
            if h == "escaped" and path and ev != "PreToolUse" and time.time() - e.ts < 30:
                # The shell couldn't spell the path (JSON escapes in it): the
                # file as it is now, moments later, and said so.
                h = file_hash(path)
                data["after_at"] = "ingest"
            if h and h != "escaped":
                data["before" if ev == "PreToolUse" else "after"] = h
        vk = ""
        if tool == "Bash":
            vk = validation_kind((inp or {}).get("command", "") if isinstance(inp, dict) else e.get("command") or "")
        if vk and ev == "PreToolUse":
            data["validation"] = vk
            if cwd and len(cwd) <= 1000 and sanitize.scrub(cwd) == cwd:
                data["cwd"] = cwd   # where it runs: its snapshot looks there
            g = e.extras.get("g", "")
            if re.fullmatch(r"[0-9a-f]{40,64}|none", g):
                # HEAD as the hook read it (fast.sh, without git), before the
                # command ran: what it starts on.
                data["head_at_start"] = {"head": "" if g == "none" else g,
                                         "branch": sanitize.clip(e.extras.get("b", ""), 200)}
        if ev == "PreToolUse":
            # Authorization references (authz.py): the permission mode the
            # call was made in, and the input's hash a permission dialog
            # (PermissionRequest, no tool_use_id) is linked back by.
            mode = e.get("permission_mode")
            if isinstance(mode, str) and mode in authz.MODES:
                data["mode"] = mode
            if e.payload is not None and not e.meta:
                data["input_sha"] = input_sha(data["input"])
            return [dict(base, kind="tool.intent", op=op, data=data)]
        if ev == "PermissionDenied":
            data.update(ok=False, denied="permission")
            return [dict(base, kind="tool.outcome", op=op, data=data)]
        if ev == "PostToolUse":
            resp = e.payload.get("tool_response") if e.payload is not None else None
            if e.meta:
                # It returned, but without its result nobody knows whether
                # its work went on in the background: a start, never done.
                # (A Bash one is read as returned, its result unkept, never
                # as an unknown outcome: recovery.returned_unrecorded.)
                bg = "unknown" if tool in ("Bash", "Agent", "Task") else ""
            elif e.payload is not None:
                data["result"] = sanitize.tool_response(tool, resp, output=bool(vk))
                bg = sanitize.background_of(tool, resp, inp)
            else:  # cut: only what the prefix still says
                bg = "background" if tool in ("Bash", "Agent", "Task") and re.search(
                    r'"backgroundTaskId"\s*:\s*"[^"]|"status"\s*:\s*"(?:' + "|".join(sanitize.AGENT_LAUNCHED)
                    + r')"|"run_in_background"\s*:\s*true', e.raw) else ""
            if bg:
                # The call returned, but its work goes on (run_in_background,
                # Ctrl+B, moved to the background, an async subagent): its
                # result isn't this. Neither ok nor an exit code.
                data["background"] = bg
            elif tool == "Bash" and isinstance(resp, dict) and resp.get("interrupted"):
                data["ok"] = False
                data["interrupted"] = True
            else:
                data["ok"] = True
                ex = sanitize.stated_exit(resp)
                if ex is not None:
                    data["exit"] = ex
                if isinstance(resp, dict) and resp.get("returnCodeInterpretation"):
                    # A non-zero exit Claude Code doesn't count as an error
                    # (grep's 1: "No matches found"): its words, no exit code.
                    data["interpretation"] = sanitize.clip(sanitize.scrub(resp["returnCodeInterpretation"]), 200)
        else:
            err = e.get("error") or ""
            data["ok"] = False
            if tool == "Bash" and not vk:
                # Claude Code's error is "Exit code N" and then the output so
                # far: a command that isn't a test, build or check keeps its
                # first line (the exit code, an interrupt) and the size only.
                mark = sanitize.failure_mark(err)
                if mark:
                    data["error"] = mark
                if err:
                    data["error_chars"] = len(err)
            elif tool == "Bash":
                data["error"] = sanitize.tail(sanitize.scrub(err), 500)   # a validation's evidence
            else:
                # Another tool's reason, without what follows it (Edit's
                # string, an MCP server's dump).
                data["error"] = sanitize.clip(sanitize.scrub(err.split("\n", 1)[0]), 200)
            ex = _parse_exit(err)
            if ex is not None:
                data["exit"] = ex
            if e.get("is_interrupt"):
                data["interrupted"] = True
        if e.get("duration_ms") is not None:
            data["duration_ms"] = e.get("duration_ms")
        out = [dict(base, kind="tool.outcome", op=op, data=data)]
        if ev == "PostToolUse" and e.payload is not None and tool in BG_STOP_TOOLS:
            out.extend(_bg_tool_records(tool, inp, e.payload.get("tool_response"), base, op))
        return out
    if ev == "Stop":
        return [dict(base, kind="stop", op=_op(e, "e"),
                     data={"last_message": sanitize.clip(sanitize.scrub(e.get("last_assistant_message") or ""), 300),
                           **note})]
    if ev == "SubagentStart":
        return [dict(base, kind="subagent.start", op=f"a:{e.get('agent_id') or _op(e, 'a')}:start",
                     data={"agent_id": sanitize.clip(e.get("agent_id") or "", 80),
                           "agent_type": sanitize.clip(e.get("agent_type") or "", 80), **note})]
    if ev == "SubagentStop":
        return [dict(base, kind="subagent.stop", op=f"a:{e.get('agent_id') or _op(e, 'a')}:stop:{int(e.ts)}",
                     data={"agent_id": sanitize.clip(e.get("agent_id") or "", 80),
                           "agent_type": sanitize.clip(e.get("agent_type") or "", 80),
                           "last_message": sanitize.clip(sanitize.scrub(e.get("last_assistant_message") or ""), 300),
                           **note})]
    if ev == "Notification":
        return [dict(base, kind="notification", op=_op(e, "e"),
                     data={"type": sanitize.clip(e.get("notification_type") or "", 60),
                           "message": sanitize.clip(sanitize.scrub(e.get("message") or ""), 300), **note})]
    if ev in ("PreCompact", "PostCompact"):
        data = {"phase": "pre" if ev == "PreCompact" else "post", "trigger": sanitize.clip(e.get("trigger") or "", 20),
                **note}
        if ev == "PreCompact" and e.get("custom_instructions"):
            data["instructions"] = sanitize.clip(sanitize.scrub(e.get("custom_instructions")), 300)
        if ev == "PostCompact":
            summ = e.get("compact_summary") or ""
            data["summary_chars"] = len(summ)
            data["summary_excerpt"] = sanitize.clip(sanitize.scrub(summ), 600)
        return [dict(base, kind="compact", op=_op(e, "e"), data=data)]
    if ev == "SessionEnd":
        # The spool file's name too: two ends within one second are two ends
        # (a re-ingest of the same file is still one).
        return [dict(base, kind="session.end", op=f"end:{sid}:{int(e.ts)}{':' + e.name if e.name else ''}",
                     data={"reason": sanitize.clip(e.get("reason") or "", 40), **note})]
    if ev in ("TaskCreated", "TaskCompleted"):
        return [dict(base, kind="plan.event", op=_op(e, "e"),
                     data={"event": "created" if ev == "TaskCreated" else "completed",
                           "id": sanitize.clip(e.get("task_id") or "", 40),
                           "subject": sanitize.clip(sanitize.scrub(e.get("task_subject") or ""), 200), **note})]
    if ev == "mod.turn.start":
        return [dict(base, kind="turn.start", op=f"turn:{e.get('turnId') or _op(e, 'x')}:start",
                     data={"turn_id": sanitize.clip(e.get("turnId") or "", 80),
                           "chars": e.get("chars") if isinstance(e.get("chars"), int) else None})]
    if ev == "mod.turn.complete":
        return [dict(base, kind="turn.complete", op=f"turn:{e.get('turnId') or _op(e, 'x')}:end",
                     data={"turn_id": sanitize.clip(e.get("turnId") or "", 80),
                           "reason": sanitize.clip(e.get("reason") or "", 20),
                           "duration_ms": e.get("durationMs") if isinstance(e.get("durationMs"), (int, float)) else None})]
    if ev == "mod.compact":
        return [dict(base, kind="compact", op=_op(e, "e"),
                     data={"phase": sanitize.clip(e.get("phase") or "mod", 20),
                           "trigger": sanitize.clip(e.get("trigger") or "", 20),
                           **({"result": sanitize.clip(e.get("result"), 60)} if e.get("result") else {})})]
    if ev == "PermissionRequest":
        # A permission dialog was put to the person (without mods the only
        # sign of an ask): the tool and the input's hash, never the input.
        tool = str(e.get("tool_name") or "?")
        inp = e.payload.get("tool_input") if e.payload is not None else None
        sug = e.payload.get("permission_suggestions") if e.payload is not None else None
        return [dict(base, kind="auth.ref", op=_op(e, "pr"),
                     data={"via": "request", "decision": "ask", "tool": sanitize.clip(tool, 120),
                           **({"input_sha": input_sha(sanitize.tool_input(tool, inp))} if e.payload is not None else {}),
                           **({"suggestions": len(sug)} if isinstance(sug, list) else {}), **note})]
    if ev in ("mod.tool.check", "mod.tool.answer"):
        # The mod's view of Claude Code's permission verdict (tool.check) and
        # what came of an ask (tool.call): names and decisions only.
        tid = sanitize.clip(e.get("tool_use_id") or "", 80)
        if not tid:
            return []
        p = e.payload or {}
        data = authz.check_record(p) if ev == "mod.tool.check" else authz.answer_record(p)
        data.update(tool=sanitize.clip(e.get("tool") or "?", 120), tool_use_id=tid)
        return [dict(base, kind="auth.ref", op=f"auth:{tid}:{data['via']}", data=data)]
    if ev == "recording.gap" and e.get("what") == "dropped":
        # hooks/lib.sh ck_spool_trim: the spool passed its cap and the shell
        # dropped its oldest payloads (the ones this ingest found gone).
        if not e.dropped or not e.dropped[0]:
            return []
        return [dict(base, kind="recording.gap", op=_op(e, "gap"),
                     data={"what": "dropped", "events": e.dropped[0], "bytes": e.dropped[1],
                           "at": sanitize.clip(str(e.get("at") or ""), 40),
                           "why": sanitize.clip(sanitize.scrub(str(e.get("why") or "")), 200)})]
    if ev == "recording.gap":
        # hooks/lib.sh ck_spool_recovered: the hooks couldn't spool from
        # `failed` ("<time> <why>") until `recovered`; that window's events are
        # lost. Said by the summary, never filled in.
        failed = sanitize.clip(sanitize.scrub(str(e.get("failed") or "")), 300)
        return [dict(base, kind="recording.gap", op=_op(e, "gap"),
                     data={"what": "spool", "failed": failed,
                           "recovered": sanitize.clip(str(e.get("recovered") or ""), 40)})]
    if ev == "mod.tool.denied":
        tid = e.get("tool_use_id") or ""
        return [dict(base, kind="tool.outcome", op=str(tid) if tid else _op(e, "t"),
                     data={"tool": sanitize.clip(e.get("tool") or "?", 120), "ok": False,
                           "denied": sanitize.clip(e.get("why") or "paused", 40), "by": "mod"})]
    return [dict(base, kind="event", op=_op(e, "e"),
                 data={"event": sanitize.clip(ev, 60), "keys": sorted(str(k) for k in (e.payload or {}))[:20], **note})]


def start_records(sid: str, e: Event | None, source: str = "") -> tuple[str, list[dict[str, Any]]]:
    """(task, records) for a session start: lineage and repository identity."""
    payload = (e.payload if e and e.payload else {}) or {}
    source = source or payload.get("source") or "startup"
    task, parent, how = start_task(sid, source, payload)
    sess = state.load_session(sid)
    cwd = payload.get("cwd") or sess.get("cwd") or ""
    ts = e.ts if e else time.time()
    data: dict[str, Any] = {"source": sanitize.clip(source, 20), "cwd": sanitize.clip(cwd, 500)}
    for k in ("model", "agent_type"):
        if payload.get(k):
            data[k] = sanitize.clip(payload[k], 80)
    if sess.get("claude_pid"):
        data["claude_pid"] = sess["claude_pid"]
    if payload.get("transcript_path") or sess.get("transcript_path"):
        data["transcript"] = sanitize.clip(payload.get("transcript_path") or sess.get("transcript_path"), 500)
    if parent:
        data["parent"] = parent
        data["parent_how"] = how
    if e and e.orphan:
        data["orphan"] = True
    # The spool file's name too: two starts of one session within one second
    # (a quick exit and `--resume`) are two runs; a re-ingest is still one.
    uniq = f":{e.name}" if e and e.name else ""
    recs = [{"kind": "session.start", "op": f"start:{sid}:{source}:{int(ts)}{uniq}", "ts": ts, "session": sid,
             "data": data}]
    rid = repo_identity(cwd)
    if rid:
        recs.append({"kind": "repo", "op": f"repo:{sid}:{int(ts)}{uniq}", "ts": ts, "session": sid,
                     "data": {k: sanitize.clip(v, 500) if isinstance(v, str) else v for k, v in rid.items()}})
    return task, recs


# -- ingest -------------------------------------------------------------------------

def _append(sid: str, task: str, recs: list[dict[str, Any]], sync: bool) -> tuple[str, list[dict[str, Any]]]:
    """Append to the session's task; a damaged journal (readers stop before
    its end) is left as it is and the session continues in a new task that
    names it. Returns (task, written)."""
    try:
        return task, journal.append(task, recs, sync=sync)
    except journal.JournalDamaged as e:
        new = journal.new_id("t")
        state.update_session(sid, task_id=new)
        cont = {"kind": "task.continued", "op": f"cont:{task}:{new}", "session": sid,
                "data": {"from": task, "why": sanitize.clip(str(e), 300)}}
        return new, journal.append(new, [cont] + recs, sync=sync)


def _say_failure(sid: str, task: str, err: BaseException) -> bool:
    """Record a failure; True if this session hadn't been told about one yet."""
    told = journal.failure(sid) is not None
    journal.record_failure(sid, task, err)
    return not told


def ingest(sid: str, sync: bool = False, extra: list[dict[str, Any]] | None = None) -> IngestResult:
    """Move the session's spool into its task's journal; `extra` records
    (Python's own: a pause, a note, a checkpoint) are appended after it in
    the same write. With sync, fsync before returning; spool payloads are
    deleted only after a sync either way (they are the records' only other
    copy)."""
    res = IngestResult(sid)
    if not sid:
        return res
    d = spool_dir(sid)
    try:
        lock = _spool_lock(d) if d.is_dir() or extra is not None else contextlib.nullcontext()
        with lock:
            return _ingest_locked(sid, d, sync, extra or [], res)
    except OSError as e:
        res.error = f"{e.strerror or e}"
        res.new_failure = _say_failure(sid, res.task or task_of(sid), e)
        return res


def _ingest_locked(sid: str, d: Path, sync: bool, extra: list[dict[str, Any]], res: IngestResult) -> IngestResult:
    cursor = state.read_json(d / "cursor", {}) if d.is_dir() else {}
    if not isinstance(cursor, dict):
        cursor = {}
    offset = int(cursor.get("offset") or 0)
    rotated = {str(k): int(v) for k, v in (cursor.get("rotated") or {}).items()} if isinstance(
        cursor.get("rotated"), dict) else {}
    cur_ino = cursor.get("ino") if isinstance(cursor.get("ino"), int) else 0
    # Indexes the shell rotated away (index.s<n>: hooks/lib.sh ck_spool_trim,
    # while no runtime was there) that the cursor doesn't know yet: read from
    # the start, or from the cursor's offset when one is the index the cursor
    # was in (the same inode); the current index is then a new one.
    index = d / "index"
    for p in sorted(d.glob("index.s*")) if d.is_dir() else []:
        g = p.name[len("index."):]
        if g in rotated:
            continue
        try:
            ino = p.stat().st_ino
        except OSError:
            continue
        if cur_ino and ino == cur_ino:
            rotated[g], offset = offset, 0
        else:
            rotated[g] = 0
    events: list[Event] = []
    names: list[str] = []
    seen: set[str] = set()
    missing: dict[str, tuple[str, int]] = {}
    new_rot: dict[str, int] = {}
    # Rotated indexes first (older: Python's own, then the shell's), then the current one.
    for g in sorted(rotated, key=lambda x: (0, int(x), "") if x.isdigit() else (1, 0, x)):
        p = d / f"index.{g}"
        lines, end = _read_lines(p, rotated[g])
        for off, line in lines:
            e, name, prob = _load_event(d, line, f"index.{g}:{off}", missing)
            if name:
                names.append(name)
                seen.add(name)
            if prob:
                res.problems.append(prob)
            if e:
                events.append(e)
        new_rot[g] = end
    try:
        ist = index.stat()
        size, ino = ist.st_size, ist.st_ino
    except OSError:
        size = ino = 0
    if cur_ino and ino and ino != cur_ino:
        offset = 0      # not the index the cursor was in (that one was rotated away)
    if size < offset:
        res.problems.append(f"spool index shorter than its cursor ({size} < {offset}); read from the start")
        offset = 0
    info: dict[str, int] = {}
    lines, end = _read_lines(index, offset, info)
    for off, line in lines:
        e, name, prob = _load_event(d, line, f"index:{off}", missing)
        if name:
            names.append(name)
            seen.add(name)
        if prob:
            res.problems.append(prob)
        if e:
            events.append(e)
    # Payloads the shell dropped over its cap (its recording.gap names them)
    # are counted there; any other missing one is a problem.
    dropped: set[str] = set()
    for e in events:
        files = (e.payload or {}).get("files") if e.event == "recording.gap" else None
        if isinstance(files, list) and (e.payload or {}).get("what") == "dropped":
            gone = [f for f in files if isinstance(f, str) and f in missing]
            e.dropped = (len(gone), sum(missing[f][1] for f in gone))
            dropped.update(gone)
    res.problems.extend(prob for name, (prob, _n) in missing.items() if name not in dropped)
    orphans = _orphans(d, seen) if d.is_dir() else []
    events.extend(orphans)
    names.extend(e.name for e in orphans)
    res.events = len(events)
    if not events and not extra:
        res.task = state.load_session(sid).get("task_id") or ""
        _tidy(d, index, end, new_rot, rotated, info.get("ino", 0))
        if sync and res.task and journal.jdir(res.task).is_dir():
            try:  # nothing new, but what was written unsynced goes to disk now
                journal.append(res.task, [], sync=True)
            except journal.JournalError as e:
                res.error = str(e)
                res.new_failure = _say_failure(sid, res.task, e)
        return res

    task = ""
    recs: list[dict[str, Any]] = []
    for e in events:
        if e.event == "SessionStart":
            t, r = start_records(sid, e)
            task = t
            res.started = True
            recs.extend(r)
        else:
            recs.extend(records_for(e, sid))
    if res.problems:
        recs.append({"kind": "spool.problem", "session": sid,
                     "data": {"problems": [sanitize.clip(p, 200) for p in res.problems[:20]]}})
    metas = [e for e in events if e.meta]
    if metas:
        # One gap for the events the shell kept without their contents (no
        # runtime then): what they did is not in the journal beyond their names.
        recs.append({"kind": "recording.gap", "op": f"gap:meta:{metas[0].name}:{len(metas)}", "session": sid,
                     "data": {"what": "payloads", "events": len(metas), "from_ts": metas[0].ts,
                              "until_ts": metas[-1].ts, "why": metas[0].meta}})
    recs.extend(extra)
    task = task or task_of(sid)
    res.task = task
    try:
        # The payloads are deleted below: their records must be on disk
        # first (synced, F_FULLFSYNC on macOS), or a power loss after the
        # unlink reached the disk could lose both (one sync per ingest).
        task, written = _append(sid, task, recs, sync or bool(names))
    except journal.JournalError as e:
        res.task = task
        res.error = str(e)
        res.new_failure = _say_failure(sid, task, e)
        return res  # the spool stays as it is: the next moment tries again
    res.task = task
    res.records = len(written)
    res.last_seq = written[-1]["seq"] if written else 0
    journal.clear_failure(sid)
    _update_meta(task, sid, written)
    # Only now: the payloads are in the journal, on disk.
    for n in names:
        with contextlib.suppress(OSError):
            (d / n).unlink()
    _tidy(d, index, end, new_rot, rotated, info.get("ino", 0))
    _write_size(d)
    return res


def _write_size(d: Path) -> None:
    """spool/<session>/.size for the shell's cap: the payloads left (records
    and bytes; hooks/lib.sh ck_spool adds to it, ck_spool_trim reads it)."""
    n = size = 0
    try:
        for p in d.iterdir():
            if p.name.endswith(".ev"):
                with contextlib.suppress(OSError):
                    size += p.stat().st_size
                    n += 1
    except OSError:
        return
    with contextlib.suppress(OSError):
        state.touch(d / ".size", f"{n} {size}\n")


def _tidy(d: Path, index: Path, end: int, new_rot: dict[str, int], rotated: dict[str, int], ino: int = 0) -> None:
    """Keep the spool bounded: drop rotated indexes that are ingested and quiet,
    rotate the index once it is ingested past INDEX_ROTATE, store the cursor
    (with the inode of the index file it was read in: the shell may rotate
    that file away before the next ingest)."""
    if not d.is_dir():
        return
    rot_out: dict[str, int] = {}
    now = time.time()
    for g, off in new_rot.items():
        p = d / f"index.{g}"
        try:
            st = p.stat()
        except OSError:
            continue
        if off >= st.st_size and now - st.st_mtime > ROTATED_KEEP_SECONDS:
            with contextlib.suppress(OSError):
                p.unlink()
            continue
        rot_out[g] = off
    if end >= INDEX_ROTATE and end == _size(index):
        g = str(max([int(x) for x in list(rot_out) + list(rotated) if x.isdigit()] + [0]) + 1)
        with contextlib.suppress(OSError):
            os.replace(index, d / f"index.{g}")
            rot_out[g] = end
            end, ino = 0, 0
    with contextlib.suppress(OSError):
        state.write_json(d / "cursor", {"offset": end, "rotated": rot_out, **({"ino": ino} if ino and end else {})})


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return -1


def _update_meta(task: str, sid: str, written: list[dict[str, Any]]) -> None:
    """The listing cache: goal, sessions, cwd, last activity (best effort)."""
    if not written:
        return
    path = journal.jdir(task) / "meta.json"
    try:
        meta = state.read_json(path, {}) or {}
        meta.setdefault("task", task)
        meta.setdefault("created", written[0]["ts"])
        meta["updated"] = written[-1]["ts"]
        meta["last_seq"] = written[-1]["seq"]
        sessions = meta.setdefault("sessions", [])
        if sid and sid not in sessions:
            sessions.append(sid)
        meta["last_session"] = sid
        for r in written:
            d = r.get("data") or {}
            if r["kind"] == "prompt" and d.get("user") and not d.get("command") and not meta.get("goal"):
                meta["goal"] = sanitize.clip(d.get("text") or "", 200)
            elif r["kind"] == "session.start" and not meta.get("cwd"):
                meta["cwd"] = d.get("cwd") or ""
            elif r["kind"] == "session.end":
                meta.setdefault("ended", {})[r["session"]] = d.get("reason") or ""
            elif r["kind"] == "checkpoint":
                meta["last_checkpoint"] = d.get("id")
            elif r["kind"] in ("task.complete", "task.cancel"):
                meta["finished"] = {"how": "completed" if r["kind"] == "task.complete" else "canceled",
                                    "ts": r["ts"], "session": r.get("session") or ""}
            elif r["kind"] == "task.reopen":
                meta.pop("finished", None)
        state.write_json(path, meta)
    except (OSError, ValueError):
        pass


def ingest_dead(except_sid: str = "") -> list[IngestResult]:
    """Spools of sessions whose Claude process is gone (killed, crashed):
    nobody else would ingest them. Done at a session start."""
    from . import procs
    out = []
    root = state.p("spool")
    try:
        dirs = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return out
    table = procs.table()
    for d in dirs:
        sid = d.name
        if sid == state.safe_id(except_sid):
            continue
        if not any(d.glob("*.ev")):
            continue
        rec = state.read_json(state.session_path(sid), {}) or {}
        pid = rec.get("claude_pid")
        if isinstance(pid, int) and table and procs.is_claude(pid, table):
            continue  # alive: its own moments ingest it
        out.append(ingest(rec.get("session_id") or sid, sync=True))
    return out


def record(sid: str, recs: list[dict[str, Any]], sync: bool = False) -> IngestResult:
    """Python's own records (pause, resume, note, checkpoint), after
    whatever waits in the spool, so the order holds."""
    return ingest(sid, sync=sync, extra=recs)


def snap(sid: str, tool_use_id: str) -> IngestResult:
    """A Bash command that looks like a validation is starting: ingest, and
    record the git state and file hashes it runs against. This runs in the
    background a moment after the hook (fast.sh never waits for it), so the
    command may already have moved HEAD or changed files: the record says how
    long after the command's intent it was taken (`lag_ms`), keeps the HEAD
    the hook read before the command ran (`hook_head`, `hook_branch`), and
    says when the two differ (`head_moved`)."""
    res = ingest(sid)
    sess = state.load_session(sid)
    task = res.task or sess.get("task_id") or ""
    intent: dict[str, Any] = {}
    changed: list[str] = []
    if task:
        recs = journal.read(task).records
        intent = next((r for r in recs if r["kind"] == "tool.intent" and r.get("op") == tool_use_id), {})
        changed = changed_files(task, recs)
    cwd = (intent.get("data") or {}).get("cwd") or sess.get("cwd") or os.getcwd()
    v = version(cwd, extra=changed)
    v["files"] = {sanitize.clip(k, 500): val for k, val in v.get("files", {}).items()}
    if intent:
        v["lag_ms"] = max(0, round((v["at"] - float(intent.get("ts") or v["at"])) * 1000))
        hs = (intent.get("data") or {}).get("head_at_start")
        if isinstance(hs, dict):
            v["hook_head"] = hs.get("head") or ""
            v["hook_branch"] = hs.get("branch") or ""
            if (v.get("head") or "") != v["hook_head"] or (
                    v["hook_branch"] and v.get("branch") and v["branch"] != v["hook_branch"]):
                v["head_moved"] = True
    return record(sid, [{"kind": "validation.version", "op": tool_use_id, "session": sid,
                         "data": {"captured": "after start", **v}}])


def changed_files(task: str, records: list[dict[str, Any]] | None = None) -> list[str]:
    """Paths the task's edit tools wrote (from its journal)."""
    if not task:
        return []
    out: list[str] = []
    for r in (journal.read(task).records if records is None else records):
        p = (r.get("data") or {}).get("path")
        if r["kind"] in ("tool.intent", "tool.outcome") and isinstance(p, str) and p and p not in out:
            out.append(p)
    return out[-200:]
