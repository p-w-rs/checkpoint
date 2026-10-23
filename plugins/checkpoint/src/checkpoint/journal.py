"""The durable journal: append-only JSONL segments per task (PLAN-v4 phase 2).

Layout (CHECKPOINT_HOME, folders 0700, files 0600):

    journal/<task>/seg-000001.jsonl, seg-000002.jsonl, ...   the records
    journal/<task>/.lock        flock: writers exclusive, readers shared
    journal/<task>/head.json    the writers' cache (per segment: size, mtime, next
                                seq and the CRC32 of its whole records, checked
                                against the bytes at every writer's start; recent
                                keys); rebuilt from the segments whenever it
                                disagrees with them
    journal/<task>/meta.json    a listing cache (capture.py); never authoritative
    journal-errors/<session>    the last recording failure a session hit (a
                                `.last` one once a later write worked)

A record is one line of canonical JSON (sorted keys, no spaces, ASCII):

    {"v": 1, "seq": 41, "op": "toolu_...", "ts": 1792450000.123, "kind": "tool.intent",
     "session": "<session id>", "agent": "<subagent id>", "task": "t_...",
     "data": {...sanitized...}, "crc": <crc32 of the same record without crc>}

`seq` counts from 1 per task, without gaps. `op` is the operation the record
belongs to (a tool_use_id, a prompt id, or a fresh id): an intent and its
outcome share it, and a second record of the same kind for the same op is a
duplicate delivery and is dropped (the writer keeps the last RECENT keys).

Reading stops at the first line that isn't a whole record with the right CRC
and seq (a torn line, a bad CRC, a seq out of step), reports it, and never
rewrites it. A writer that finds such a tail starts a new segment, so the
records after it continue the seq and stay readable; a gap between segments
(a segment missing, or out of step, as after damage in an earlier one) stops
reading there for good, and a writer refuses to append behind it
(JournalDamaged: capture.py continues in a new task that names this one).
Writers notice damage at rest in any segment, not only the one they append
to: a segment whose whole records no longer match their cached CRC is read
again (about 6 ms per writer for a 25 MB journal, measured).

Durability: one os.write per batch (O_APPEND), fsync every FSYNC_EVERY
records, and on request (`sync=True`: checkpoints, pause, Stop, SessionEnd,
and every ingest that then deletes spool payloads), where macOS gets
F_FULLFSYNC. A new segment's folder is synced too. Nothing
is reported saved before that returns; write errors (ENOSPC, EIO, EFBIG) are
raised as JournalError and recorded for /check:doctor, the status line and the
next hook output (capture.py, record_failure).
"""
from __future__ import annotations

import contextlib
import errno as errno_mod
import fcntl
import json
import os
import secrets
import sys
import time
import zlib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import state

SCHEMA = 1
SEG_PREFIX = "seg-"
SEG_SUFFIX = ".jsonl"
RECENT = 4000


def seg_max() -> int:
    """Segment size limit (bytes); tests make it small."""
    try:
        return max(512, int(os.environ.get("CHECKPOINT_SEGMENT_BYTES") or 1 << 20))
    except ValueError:
        return 1 << 20


def fsync_every() -> int:
    try:
        return max(1, int(os.environ.get("CHECKPOINT_FSYNC_EVERY") or 32))
    except ValueError:
        return 32


class JournalError(Exception):
    """A write to the journal failed (disk full, I/O error, permissions)."""

    def __init__(self, msg: str, err: int = 0):
        super().__init__(msg)
        self.errno = err


class JournalDamaged(JournalError):
    """Readers stop before the end of this journal (a seq gap between
    segments): appending would write records nobody can read."""


def new_id(prefix: str) -> str:
    """A sortable unique id: <prefix>_<ms since epoch, base32><random>."""
    ms = int(time.time() * 1000)
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    t = ""
    while ms:
        ms, r = divmod(ms, 32)
        t = alphabet[r] + t
    return f"{prefix}_{t.rjust(10, '0')}{''.join(secrets.choice(alphabet) for _ in range(10))}"


def jdir(task: str) -> Path:
    return state.p("journal", state.safe_id(task))


def canonical(rec: dict[str, Any]) -> bytes:
    return json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False, default=str).encode("ascii")


def crc_of(rec: dict[str, Any]) -> int:
    return zlib.crc32(canonical({k: v for k, v in rec.items() if k != "crc"})) & 0xFFFFFFFF


def encode(rec: dict[str, Any]) -> bytes:
    rec = dict(rec)
    rec.pop("crc", None)
    rec["crc"] = crc_of(rec)
    return canonical(rec) + b"\n"


def segments(d: Path) -> list[Path]:
    try:
        return sorted(p for p in d.iterdir()
                      if p.name.startswith(SEG_PREFIX) and p.name.endswith(SEG_SUFFIX))
    except OSError:
        return []


def seg_name(n: int) -> str:
    return f"{SEG_PREFIX}{n:06d}{SEG_SUFFIX}"


def seg_number(p: Path) -> int:
    try:
        return int(p.name[len(SEG_PREFIX):-len(SEG_SUFFIX)])
    except ValueError:
        return 0


@dataclass
class Problem:
    segment: str
    offset: int
    kind: str          # torn | bad-json | bad-crc | bad-record | schema | seq-gap | unreadable
    detail: str = ""
    bytes_after: int = 0
    fatal: bool = False   # reading stopped here for good (seq-gap, unreadable)

    def text(self) -> str:
        where = f"{self.segment} at byte {self.offset}"
        rest = f"; {self.bytes_after} bytes after it not read" if self.bytes_after else ""
        return f"{self.kind} in {where}" + (f" ({self.detail})" if self.detail else "") + rest


@dataclass
class Scan:
    records: list[dict[str, Any]]
    good_end: int              # byte offset just after the last good line
    next_seq: int | None       # seq the next record must have
    problem: Problem | None
    size: int
    crc: int = 0               # CRC32 of bytes 0..good_end (given crc0, the CRC of 0..start)


def scan_segment(path: Path, start: int = 0, expect: int | None = None,
                 keep: bool = True, crc0: int = 0) -> Scan:
    """The whole records of one segment from byte `start`, stopping at the
    first bad line. `expect`: the seq the first one must have (None: any).
    `crc0`: the CRC32 of the bytes before `start` (Scan.crc continues it)."""
    try:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read()
    except OSError as e:
        return Scan([], start, expect, Problem(path.name, start, "unreadable", str(e), fatal=True), start, crc0)
    size = start + len(data)
    recs: list[dict[str, Any]] = []
    pos = 0
    problem = None
    while pos < len(data):
        nl = data.find(b"\n", pos)
        if nl < 0:
            problem = Problem(path.name, start + pos, "torn", "the last line has no newline",
                              bytes_after=len(data) - pos)
            break
        line = data[pos:nl]
        try:
            rec = json.loads(line)
        except ValueError:
            problem = Problem(path.name, start + pos, "bad-json", bytes_after=len(data) - nl - 1)
            break
        if not isinstance(rec, dict) or not isinstance(rec.get("crc"), int) or crc_of(rec) != rec["crc"]:
            problem = Problem(path.name, start + pos, "bad-crc", bytes_after=len(data) - nl - 1)
            break
        if rec.get("v") != SCHEMA:
            problem = Problem(path.name, start + pos, "schema",
                              f"record version {rec.get('v')!r}, this reader knows {SCHEMA}",
                              bytes_after=len(data) - nl - 1)
            break
        seq = rec.get("seq")
        if not isinstance(seq, int) or not isinstance(rec.get("kind"), str):
            problem = Problem(path.name, start + pos, "bad-record", bytes_after=len(data) - nl - 1)
            break
        if expect is not None and seq != expect:
            # Out of step at the very start of a segment: a segment is missing
            # or damaged, and nothing after it can be trusted (fatal). Inside a
            # segment it ends the segment like a bad CRC does.
            problem = Problem(path.name, start + pos, "seq-gap", f"seq {seq}, expected {expect}",
                              bytes_after=len(data) - pos, fatal=start + pos == 0)
            break
        expect = seq + 1
        if keep:
            recs.append(rec)
        pos = nl + 1
    return Scan(recs, start + pos, expect, problem, size, zlib.crc32(data[:pos], crc0) & 0xFFFFFFFF)


@dataclass
class View:
    task: str
    records: list[dict[str, Any]] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    next_seq: int = 1
    stopped: bool = False      # a fatal problem: records after it can't be read

    @property
    def damaged(self) -> bool:
        return bool(self.problems)


@contextlib.contextmanager
def _flock(d: Path, mode: int) -> Iterator[None]:
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(d / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, mode)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read(task: str, since: int = 0) -> View:
    """Every readable record of a task (seq > since), oldest first, and the
    problems met (under the shared lock, so never a write in progress)."""
    d = jdir(task)
    v = View(task)
    if not d.is_dir():
        return v
    with _flock(d, fcntl.LOCK_SH):
        expect: int | None = 1
        for p in segments(d):
            s = scan_segment(p, 0, expect)
            v.records.extend(r for r in s.records if r["seq"] > since)
            if s.next_seq is not None:
                expect = s.next_seq
            if s.problem:
                v.problems.append(s.problem)
                if s.problem.fatal:
                    v.stopped = True
                    break
        v.next_seq = expect or 1
    return v


def _file_crc(path: Path, n: int) -> int | None:
    """CRC32 of a file's first n bytes (None when it can't be read)."""
    try:
        with open(path, "rb") as f:
            return zlib.crc32(f.read(n)) & 0xFFFFFFFF
    except OSError:
        return None


def _durable(fd: int, full: bool) -> None:
    if full and sys.platform == "darwin" and hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass  # not supported by this filesystem: plain fsync
    os.fsync(fd)


def _sync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Writer:
    """Holds the task's journal lock (exclusive) while open:

        with Writer(task) as w:
            w.append([{"kind": ..., "op": ..., "session": ..., "data": {...}}], sync=True)
    """

    def __init__(self, task: str):
        self.task = task
        self.d = jdir(task)
        self._lockfd: int | None = None
        self._fd: int | None = None
        self._seg: Path | None = None
        self._size = 0
        self._good = 0               # bytes of the current segment that are whole records
        self._dir_dirty = False
        self.next_seq = 1
        self.appendable = False      # the last segment can take more records
        self.problems: list[Problem] = []
        self.keys: deque[str] = deque(maxlen=RECENT)
        self.keyset: set[str] = set()
        self.unsynced = 0
        self._head: dict[str, Any] = {}

    # -- open / close ---------------------------------------------------------------
    def __enter__(self) -> "Writer":
        try:
            self.d.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._lockfd = os.open(self.d / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self._lockfd, fcntl.LOCK_EX)
            self._load()
        except OSError as e:
            self.close()
            raise JournalError(f"can't open the journal of {self.task}: {e.strerror or e}", e.errno or 0) from e
        except JournalError:
            self.close()
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None
        if self._lockfd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._lockfd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(self._lockfd)
            self._lockfd = None

    def _remember(self, kind: str, op: str) -> None:
        if not op:
            return
        k = f"{kind}|{op}"
        if len(self.keys) == self.keys.maxlen:
            self.keyset.discard(self.keys[0])
        self.keys.append(k)
        self.keyset.add(k)

    def _load(self) -> None:
        """Verify the segments (incrementally, from the head cache), find the
        next seq, whether the last segment can be appended to, and the recent
        keys. Raises JournalDamaged behind a seq gap."""
        head = state.read_json(self.d / "head.json", {}) or {}
        cached = head.get("segs") if isinstance(head.get("segs"), dict) else {}
        segs = segments(self.d)
        expect: int | None = 1
        new_cache: dict[str, Any] = {}
        all_cached = bool(cached)
        scanned: list[dict[str, Any]] = []
        last_problem: Problem | None = None
        for i, p in enumerate(segs):
            try:
                st = p.stat()
            except OSError as e:
                raise JournalError(f"can't read {p}: {e.strerror or e}", e.errno or 0) from e
            c = cached.get(p.name) if isinstance(cached.get(p.name), dict) else None
            last = i == len(segs) - 1
            if c and (not isinstance(c.get("bcrc"), int)
                      or c["bcrc"] != _file_crc(p, int(c.get("good") or 0))):
                # A segment changed under its whole records (an edit, a flipped
                # bit at rest; size and mtime can stay the same), or its CRC
                # isn't cached: read it again rather than trust the cache. In
                # any segment, not only the last: damage in an older one makes
                # every later record unreadable, so appending would write
                # behind it.
                c = None
            if (c and c.get("size") == st.st_size and c.get("mtime_ns") == st.st_mtime_ns
                    and c.get("first") in (None, expect)):
                s_next, good, prob = c.get("next"), c.get("good", st.st_size), c.get("problem")
                problem = Problem(**prob) if isinstance(prob, dict) else None
                first = c.get("first")
                bcrc = c["bcrc"]
            elif (c and last and not c.get("problem") and st.st_size > c.get("size", 0)
                  and c.get("good") == c.get("size") and c.get("first") in (None, expect)):
                s = scan_segment(p, int(c["good"]), c.get("next"), crc0=c["bcrc"])
                scanned.extend(s.records)
                s_next, good, problem, bcrc = s.next_seq, s.good_end, s.problem, s.crc
                first = c.get("first") if c.get("first") is not None else (s.records[0]["seq"] if s.records else None)
            else:
                all_cached = False
                s = scan_segment(p, 0, expect)
                scanned.extend(s.records)
                s_next, good, problem, bcrc = s.next_seq, s.good_end, s.problem, s.crc
                first = s.records[0]["seq"] if s.records else None
            if problem and problem.fatal:
                raise JournalDamaged(
                    f"journal {self.task} is damaged: {problem.text()}; records after it can't be "
                    "read, so nothing is appended to it")
            new_cache[p.name] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "first": first,
                                 "next": s_next, "good": good, "bcrc": bcrc,
                                 "problem": problem.__dict__ if problem else None}
            if s_next is not None:
                expect = s_next
            last_problem = problem
            if problem:
                self.problems.append(problem)
        self.next_seq = expect or 1
        if segs:
            lastc = new_cache[segs[-1].name]
            self._seg = segs[-1]
            self._size = lastc["size"]
            self._good = lastc["good"]
            # Append only to a clean last segment that is still small enough.
            self.appendable = last_problem is None and lastc["good"] == lastc["size"]
        keys = head.get("keys") if all_cached and isinstance(head.get("keys"), list) else None
        if keys is None:
            # Rebuild the recent keys from the segments (the cache was
            # missing, or a segment changed behind the writers' backs; rare).
            recent: deque[dict[str, Any]] = deque(maxlen=RECENT)
            for p in segs:
                recent.extend(scan_segment(p, 0, None).records)
            for r in recent:
                self._remember(r.get("kind", ""), r.get("op") or "")
        else:
            for k in keys[-RECENT:]:
                kind, _, op = str(k).partition("|")
                self._remember(kind, op)
            for r in scanned:
                self._remember(r.get("kind", ""), r.get("op") or "")
        self.unsynced = int(head.get("unsynced") or 0) if all_cached else 0
        self._head = {"segs": new_cache}

    # -- writing --------------------------------------------------------------------
    def _open_segment(self, new: bool) -> None:
        if self._fd is not None and not new:
            return
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if new or self._seg is None:
            n = seg_number(self._seg) + 1 if self._seg else 1
            p = self.d / seg_name(n)
            while p.exists():
                n += 1
                p = self.d / seg_name(n)
            self._fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o600)
            self._seg = p
            self._size = 0
            self._good = 0
            self._dir_dirty = True
            self._head["segs"][p.name] = {"size": 0, "mtime_ns": 0, "first": self.next_seq,
                                          "next": self.next_seq, "good": 0, "problem": None}
            self.appendable = True
        else:
            self._fd = os.open(self._seg, os.O_WRONLY | os.O_APPEND)

    def _write(self, data: bytes) -> None:
        assert self._fd is not None
        view = memoryview(data)
        done = 0
        try:
            while done < len(data):
                n = os.write(self._fd, view[done:])
                if n <= 0:
                    raise OSError(errno_mod.EIO, "short write")
                done += n
        except OSError:
            # Whatever reached the file is a torn tail now: the next writer
            # (this one included) starts a new segment after it.
            self.appendable = False
            self._size += done
            raise
        finally:
            view.release()
        self._size += len(data)
        self._good = self._size

    def append(self, recs: list[dict[str, Any]], sync: bool = False) -> list[dict[str, Any]]:
        """Append records (kind, op, session, data; agent and ts optional),
        dropping duplicates (same kind and op as a recent record). Returns the
        records written, with seq. With sync, returns only after they are on
        disk (fsync; F_FULLFSYNC on macOS)."""
        out: list[dict[str, Any]] = []
        try:
            batch = bytearray()
            for r in recs:
                kind = str(r.get("kind") or "")
                op = str(r.get("op") or "") or new_id("o")
                if f"{kind}|{op}" in self.keyset:
                    continue
                rec: dict[str, Any] = {"v": SCHEMA, "seq": self.next_seq, "op": op,
                                       "ts": float(r.get("ts") or time.time()), "kind": kind,
                                       "session": str(r.get("session") or ""), "task": self.task,
                                       "data": r.get("data") or {}}
                if r.get("agent"):
                    rec["agent"] = str(r["agent"])
                line = encode(rec)
                if not self.appendable or (self._size + len(batch) + len(line) > seg_max()
                                           and self._size + len(batch) > 0):
                    if batch:
                        self._open_segment(False)
                        self._write(bytes(batch))
                        batch.clear()
                    if self._fd is not None and self.unsynced + len(out):
                        _durable(self._fd, False)   # the old segment first
                    with contextlib.suppress(OSError):
                        self._seal()                # its cache entry (CRC included) as it is left
                    self._open_segment(True)
                rec["crc"] = crc_of(rec)
                batch += line
                self.next_seq += 1
                self._remember(kind, op)
                out.append(rec)
            if batch:
                self._open_segment(False)
                self._write(bytes(batch))
            self.unsynced += len(out)
            if self._fd is not None and (sync or self.unsynced >= fsync_every()) and self.unsynced:
                _durable(self._fd, sync)
                if self._dir_dirty:
                    _sync_dir(self.d)
                    self._dir_dirty = False
                self.unsynced = 0
            elif sync and self._fd is None and self._seg is not None:
                # Nothing new, but the caller wants what is there on disk.
                fd = os.open(self._seg, os.O_RDONLY)
                try:
                    _durable(fd, True)
                finally:
                    os.close(fd)
                self.unsynced = 0
        except OSError as e:
            raise JournalError(f"writing the journal of {self.task} failed: {e.strerror or e}",
                               e.errno or 0) from e
        self._save_head()
        return out

    def _seal(self) -> None:
        """The cache entry of the segment being written, as it is on disk now:
        size, mtime, whole-record bytes, next seq, and `bcrc`, the CRC32 of its
        whole records (the next writer compares it, so a change under them is
        noticed in any segment)."""
        if self._seg is None:
            return
        st = self._seg.stat()
        c = self._head["segs"].setdefault(self._seg.name, {"first": None, "problem": None})
        c.update(size=st.st_size, mtime_ns=st.st_mtime_ns, next=self.next_seq, good=self._good,
                 bcrc=_file_crc(self._seg, self._good))
        if self._good != st.st_size and not c.get("problem"):
            c["problem"] = Problem(self._seg.name, self._good, "torn", "a write failed",
                                   bytes_after=st.st_size - self._good).__dict__

    def _save_head(self) -> None:
        """The cache for the next writer (best effort: it is rebuilt when it
        disagrees with the segments)."""
        try:
            self._seal()
            self._head["keys"] = list(self.keys)
            self._head["unsynced"] = self.unsynced
            self._head["v"] = SCHEMA
            state.write_json(self.d / "head.json", self._head)
        except (OSError, ValueError):
            pass


def append(task: str, recs: list[dict[str, Any]], sync: bool = False) -> list[dict[str, Any]]:
    with Writer(task) as w:
        return w.append(recs, sync=sync)


def tasks() -> list[str]:
    try:
        return sorted(p.name for p in state.p("journal").iterdir() if p.is_dir())
    except OSError:
        return []


# -- failures, for /check:doctor, the status line and the next hook output ----------

def failure_path(sid: str) -> Path:
    return state.p("journal-errors", state.safe_id(sid or "_"))


def record_failure(sid: str, task: str, err: BaseException) -> bool:
    """Remember that recording failed for this session (best effort: on a full
    disk this can fail too; the caller says so in its own output)."""
    code = getattr(err, "errno", 0) or 0
    name = errno_mod.errorcode.get(code, "") if code else ""
    text = f"{state.now_iso()} {name or 'error'} task {task}: {err}"
    rec = {"at": state.now_iso(), "ts": time.time(), "errno": name, "task": task, "error": str(err)[:300],
           "text": text[:400]}
    try:
        state.write_json(failure_path(sid), rec)
        return True
    except OSError:
        pass
    # A full disk: the reserve armed at session start becomes the marker (a
    # rename and an overwrite in place), so the failure is on record later.
    small = dict(rec, error=str(err)[:200], text=text[:240])
    return state.write_reserved(failure_path(sid), "journal-errors", sid, json.dumps(small))


def clear_failure(sid: str) -> None:
    """A write worked again: the failure becomes history (`.last`), and the
    reserve a failure on a full disk would use is armed again."""
    p = failure_path(sid)
    if p.exists():
        with contextlib.suppress(OSError):
            os.replace(p, p.with_name(p.name + ".last"))
        state.arm_reserve("journal-errors", sid)


def failure(sid: str) -> dict[str, Any] | None:
    rec = state.read_json(failure_path(sid), None)
    return rec if isinstance(rec, dict) else None


def last_failure(sid: str) -> dict[str, Any] | None:
    rec = state.read_json(failure_path(sid).with_name(failure_path(sid).name + ".last"), None)
    return rec if isinstance(rec, dict) else None
