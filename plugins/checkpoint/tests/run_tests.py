#!/usr/bin/env python3
"""Self-contained tests for checkpoint. Pure stdlib; run with:
    pixi run test
Hermetic: state goes to a temp CHECKPOINT_HOME (removed at the end), hooks
run through the real hooks/fast.sh with CHECKPOINT_PYTHON pointing at this
interpreter (through a wrapper that counts calls), and process tests use
their own fake Claude processes. CHECKPOINT_TEST_SH picks the shell that runs
the hooks (default sh; the dash section runs some under dash too).
"""
import atexit
import contextlib
import io
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
sys.path.insert(0, str(ROOT / "src"))

TMP = Path(tempfile.mkdtemp(prefix="checkpoint-test-"))
for k in ("CLAUDE_PROJECT_DIR", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
          "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA", "CHECKPOINT_WORK_DIR", "PIXI_HOME"):
    os.environ.pop(k, None)  # hooks run inside Claude Code get these
# Hook and server processes compile their modules once, into the temp dir
# (with PYTHONDONTWRITEBYTECODE every hook would recompile all of them).
os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
os.environ["PYTHONPYCACHEPREFIX"] = str(TMP / "pycache")
os.environ["CHECKPOINT_HOME"] = str(TMP / "state")
# The runtime's plugin data folder and Claude Code's config folder (the
# fallback for it) point into the temp dir: no test builds or looks for an
# env in the user's real ~/.claude (or CLAUDE_CONFIG_DIR) plugin data.
os.environ["CLAUDE_PLUGIN_DATA"] = str(TMP / "plugin-data")
os.environ["CLAUDE_CONFIG_DIR"] = str(TMP / "claude-config")
# /check:doctor tests whether the gateway's contract file exists; never the
# user's real ~/.config/gateway (live secrets).
os.environ["CHECKPOINT_GATEWAY_CONTRACT"] = str(TMP / "gateway" / "run" / "contract.json")
# Jobs would source the gateway's real env file (~/.config/gateway/env.sh, the
# user's live gateway): every job here, and every server and hook started
# from here, looks at a temp path instead (absent unless a test writes it).
os.environ["CHECKPOINT_GATEWAY_ENV"] = str(TMP / "gateway" / "env.sh")
os.environ["CHECKPOINT_NUDGE_EVERY"] = "3"
os.environ["CHECKPOINT_WATCH_INTERVAL"] = "0.2"
PYCALLS = TMP / "pycalls"
WRAP = TMP / "py.sh"
WRAP.write_text(f'#!/bin/sh\necho "$*" >> "{PYCALLS}"\nexec "{sys.executable}" "$@"\n')
WRAP.chmod(0o755)
os.environ["CHECKPOINT_PYTHON"] = str(WRAP)
SH = os.environ.get("CHECKPOINT_TEST_SH") or "sh"

# Fake Claude processes: anything whose command line says "claude".
FAKE_BIN = TMP / "bin"
FAKE_BIN.mkdir()
(FAKE_BIN / "claude").symlink_to("/bin/sleep")
CHILDREN: list[subprocess.Popen] = []


def fake_claude() -> subprocess.Popen:
    p = subprocess.Popen([str(FAKE_BIN / "claude"), "900"])
    CHILDREN.append(p)
    return p


def cleanup() -> None:
    for c in CHILDREN:
        try:
            c.kill()
            c.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
    shutil.rmtree(TMP, ignore_errors=True)


atexit.register(cleanup)
MAIN_CLAUDE = fake_claude()
os.environ["CLAUDE_PID"] = str(MAIN_CLAUDE.pid)  # what Claude Code exports to hooks

from checkpoint import __version__, core, gateway, hooks, jobs, mcp_server, procs, state, tracker, transcript  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    """Record one check. `cond` may be a callable: an exception in it counts
    as a failure instead of ending the run (later checks still report)."""
    global PASS, FAIL
    if callable(cond):
        try:
            cond = cond()
        except Exception as e:  # noqa: BLE001
            print(f"       ({type(e).__name__}: {e})")
            cond = False
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def pycalls():
    return len(PYCALLS.read_text().splitlines()) if PYCALLS.exists() else 0


def background_py():
    """This suite's background ingests (fast.sh's bg_py at Stop, PreCompact,
    PostCompact and a test-like Bash call): run.py ingest/snap processes
    (started as <plugin>/hooks/../run.py), and the forked hook shells about to
    become one."""
    out = []
    for r in procs.table().values():
        c = r["cmd"]
        if str(ROOT) not in c or r["pid"] == os.getpid():
            continue
        if re.search(r"/run\.py (?:ingest|snap) ", c) or re.match(r"^\S*\b(?:sh|dash|bash) \S*/hooks/fast\.sh \w+$", c):
            out.append(r)
    return out


def settle_background(seconds=15.0):
    """Wait for the background ingests to end, so a later pycalls() count or
    journal read doesn't race them."""
    deadline = time.time() + seconds
    while time.time() < deadline and background_py():
        time.sleep(0.05)


def hook(event, payload, env=None, sh=None):
    """Run the real pre-filter. Returns parsed JSON output or None. Waits for
    any background ingest it started."""
    r = subprocess.run([sh or SH, str(ROOT / "hooks" / "fast.sh"), event],
                       input=json.dumps(payload, ensure_ascii=False), capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, **(env or {})))
    if event in ("Stop", "PreCompact", "PostCompact") or (
            event == "PreToolUse" and isinstance(payload, dict) and payload.get("tool_name") == "Bash"):
        settle_background()
    out = r.stdout.strip()
    return json.loads(out) if out else None


def ctx(out):
    return ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""


def decision(out):
    return ((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision")


def denied(out):
    return decision(out) == "deny"


def allowed(out):
    return decision(out) == "allow"


def reason(out):
    return ((out or {}).get("hookSpecificOutput") or {}).get("permissionDecisionReason") or ""


def jread(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def cp_file(sid):
    """Where the session's pause-time checkpoint was written (its record says)."""
    return Path(state.load_session(sid).get("checkpoint") or "/nonexistent/checkpoint.json")


def job(name):
    return jobs.status(name).get("job") or {}


def wait_for(cond, seconds=10.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


def watchers(tid):
    return [r for r in procs.table().values() if "watch.sh" in r["cmd"] and f" {tid} " in r["cmd"]]


def clear_running():
    for f in state.p("running").glob("*.json"):
        f.unlink()  # watchers started by these calls exit on their own


# A project with a tracker, and a transcript file.
PROJ = TMP / "proj"
(PROJ / ".work" / "train-model").mkdir(parents=True)
TRK = PROJ / ".work" / "train-model" / "TRACKER.md"
TRK.write_text("""# Train the model
Goal: a trained model.
Started: 2026-10-15  Session: sess-aaaa1111  Updated: now

## Stages
| # | Stage | Status | Notes / details |
|---|-------|--------|-----------------|
| 1 | Data prep | accepted | |
| 2 | Training | in progress | |
| 3 | Eval | todo | |

## Now
Current step: training epoch 3
Next action: check the loss curve

## Jobs
| Job | Command | Started | State | Redo / resume |
|-----|---------|---------|-------|---------------|

## Resume notes
- Check first: is the train job still running?
""")
TRANSCRIPT = TMP / "transcript.jsonl"
SID = "sess-aaaa1111-2222"


def tline(obj, path=TRANSCRIPT):
    with open(path, "a") as f:
        f.write(json.dumps(obj, separators=(",", ":")) + "\n")


def enqueue(content, ts, path=TRANSCRIPT):
    tline({"type": "queue-operation", "operation": "enqueue", "timestamp": ts,
           "sessionId": SID, "content": content}, path)


def enqueue_all(items, path=TRANSCRIPT):
    """Several enqueue records in one write: typed one after the other before
    any hook or watcher looks."""
    with open(path, "a") as f:
        f.write("".join(json.dumps({"type": "queue-operation", "operation": "enqueue", "timestamp": ts,
                                    "sessionId": SID, "content": content}, separators=(",", ":")) + "\n"
                        for content, ts in items))


def dequeue(ts, path=TRANSCRIPT):
    tline({"type": "queue-operation", "operation": "dequeue", "timestamp": ts, "sessionId": SID}, path)


def remove_op(content, ts, path=TRANSCRIPT):
    tline({"type": "queue-operation", "operation": "remove", "timestamp": ts, "sessionId": SID,
           "content": content, "reason": "absorbed_mid_turn"}, path)


tline({"type": "user", "message": {"role": "user", "content": "train it"}, "timestamp": "2026-10-16T01:00:00.000Z"})
tline({"type": "assistant", "timestamp": "2026-10-16T01:00:01.000Z", "message": {"content": [
    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "python train.py --epochs 3"}}]}})


def base(**kw):
    d = {"session_id": SID, "transcript_path": str(TRANSCRIPT), "cwd": str(PROJ)}
    d.update(kw)
    return d


def bash(cmd="ls", tid="tu1", **kw):
    return base(hook_event_name="PreToolUse", tool_name="Bash", tool_use_id=tid,
                tool_input={"command": cmd}, **kw)


# --- the plugin's files ----------------------------------------------------------
# Every file the configs and hook scripts start is in the plugin folder (checked
# first: a missing one would otherwise surface as a crash further down).
print("the plugin's files are all there:")
REFS = set()


def read_or_empty(path):
    try:
        return path.read_text()
    except OSError:
        return ""


def plugin_refs(v):
    if isinstance(v, str):
        REFS.update(re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([^\s\"']+)", v))
    elif isinstance(v, dict):
        for x in v.values():
            plugin_refs(x)
    elif isinstance(v, list):
        for x in v:
            plugin_refs(x)


_hooks_json = json.loads(read_or_empty(ROOT / "hooks" / "hooks.json") or "{}")
plugin_refs(_hooks_json)
plugin_refs(json.loads(read_or_empty(ROOT / ".mcp.json") or "{}"))
REFS.update("hooks/" + m.lstrip("./") for m in _hooks_json.get("modules") or [])
for script in ("fast.sh", "watch.sh", "env.sh", "lib.sh"):
    REFS.update(re.findall(r"\$root/([A-Za-z0-9_./-]+)", read_or_empty(ROOT / "hooks" / script)))
REFS.update(re.findall(r"\$\{\$\.plugin\.root\}/([A-Za-z0-9_./-]+)", read_or_empty(ROOT / "hooks" / "mod" / "register.tsx")))
missing = sorted(r for r in REFS if not (ROOT / r).exists())
if missing:
    print(f"       (missing: {', '.join(missing)})")
check("every file hooks.json, .mcp.json, the hook scripts and the mod start is in the plugin folder",
      not missing and {"hooks/fast.sh", "hooks/watch.sh", "hooks/pixi.sh", "hooks/env.sh", "hooks/lib.sh",
                       "hooks/mod/register.tsx", "run.py", "pixi.toml", "pixi.lock"} <= REFS)
skill_names = set(re.search(r"\(([a-z|]+)\)", transcript.COMMAND_RE.pattern).group(1).split("|"))
skill_names |= set(re.findall(r"/check:([a-z]+)", read_or_empty(ROOT / "skills" / "help" / "SKILL.md")
                              + read_or_empty(REPO / "README.md")))


def skill_ok(n):
    f = ROOT / "skills" / n / "SKILL.md"
    return f.is_file() and re.search(r"^name: *(\S+) *$", f.read_text(), re.M).group(1) == n


check("every /check: command (README, help, the prompt hook) has skills/<name>/SKILL.md named so",
      lambda: skill_names >= {"pause", "resume", "status", "help"} and all(map(skill_ok, skill_names)))
# The slash menu shows this line when the person picks how to pause: typed
# while busy, /check:pause waits for the turn's end on 2.1.301 (ARCHITECTURE,
# the responsiveness table), so it must not promise an immediate stop.
_pause_desc = re.search(r"^description: *(.*)$", read_or_empty(ROOT / "skills" / "pause" / "SKILL.md"), re.M)
check("/check:pause's description says typed while busy it waits for the turn, and names the immediate routes",
      lambda: "waits until the turn ends" in _pause_desc.group(1) and "/check-pause" in _pause_desc.group(1)
      and "type pause" in _pause_desc.group(1) and "at once (the running turn stops)" not in _pause_desc.group(1))


def job_tool_names():
    """Full names Claude Code gives the job tools: mcp__plugin_<plugin>_<server>__<tool>."""
    plugin = json.loads(read_or_empty(ROOT / ".claude-plugin" / "plugin.json") or "{}").get("name")
    servers = json.loads(read_or_empty(ROOT / ".mcp.json") or "{}").get("mcpServers") or {}
    return {f"mcp__plugin_{plugin}_{s}__{t['name']}" for s in servers for t in mcp_server.TOOLS}


# The gateway's and pixi-guard's hooks match these exact names (their
# hooks.json; README "Jobs and the gateway"), as do fast.sh and state.py.
JOB_TOOLS = {f"mcp__plugin_check_jobs__job_{t}" for t in ("start", "status", "tail", "stop")}
NOTE_TOOL = "mcp__plugin_check_jobs__checkpoint_note"   # the journal's note tool (same server)
POLICY_TOOL = "mcp__plugin_check_jobs__task_policy"     # the task's child-work policy (same server; runs nothing)
check("job tools are named as the gateway, pixi-guard, fast.sh and state.py match them (and the note and policy "
      "tools)",
      lambda: job_tool_names() == JOB_TOOLS | {NOTE_TOOL, POLICY_TOOL}
      and set(re.findall(r"mcp__plugin_check_jobs__(?:job_[a-z]+|checkpoint_note|task_policy)",
                         read_or_empty(ROOT / "hooks" / "fast.sh")))
      | {t for t in state.ALLOW_WHILE_PAUSED if "jobs__" in t} <= JOB_TOOLS | {NOTE_TOOL, POLICY_TOOL})
_job_docs = {"README.md": read_or_empty(REPO / "README.md"),
             "help": read_or_empty(ROOT / "skills" / "help" / "SKILL.md"),
             "DESIGN.md": read_or_empty(REPO / "docs" / "dev" / "DESIGN.md")}
for _doc, _text in _job_docs.items():
    _flat = " ".join(_text.split())
    check(f"{_doc}: what the gateway and pixi-guard check at job_start, the masking, and where to read more",
          all(s in _flat for s in ("`job_start`", "curl", "private folder", "transcripts", "set to", '"ask"',
                                   "never approves a job by itself", "pixi-guard refuses Python outside pixi",
                                   "masks", "job_tail", "job_status", "a failed `job_stop`", "warns",
                                   "With checkpoint", "pixi-guard's README")))
for _doc in ("README.md", "help"):
    _flat = " ".join(_job_docs[_doc].split())
    check(f"{_doc}: the gateway as 0.11 has it (no sudo or curl/wget refusal by default: network commands are asked "
          "about, so a job's are refused unless [network] allows them), and a job it runs gets the session's "
          "environment minus credential variables",
          "sudo on this computer is" not in _flat and "curl`/`wget" not in _flat and "curl/wget" not in _flat
          and "network command" in _flat and "`[network]`" in _flat and "minus credential variables" in _flat)
check("README links the gateway's 'With checkpoint' section and pixi-guard's README",
      "](https://github.com/p-w-rs/gateway#with-checkpoint)" in _job_docs["README.md"]
      and "](https://github.com/p-w-rs/pixi-guard" in _job_docs["README.md"])
# A job reparented away from Claude Code fails the gateway's Claude-ancestor
# check, so only entries with scripts = true still give it credentials. A
# background Bash command loses its tie to Claude at the session's end too,
# so the docs offer no Bash tool way around it.
_orphan = ("a job still running after its claude session ends can no longer use the gateway's keys, passwords"
           " or git credentials, except from entries with `scripts = true`, because the gateway can't tell it"
           " came from claude any more; keep the session open until such a job is done.")
_limits = _job_docs["README.md"].partition("\n## Limits\n")[2].partition("\n## ")[0]
_where = {"README.md": ("Limits", next((b for b in _limits.split("\n- ") if "session ends" in b), "")),
          "help": ("Jobs", _job_docs["help"].partition("- **Jobs:**")[2].partition("\n- **")[0]),
          "DESIGN.md": ("Other plugins check jobs too",
                        _job_docs["DESIGN.md"].partition("- Other plugins check jobs too")[2].partition("\n\n")[0])}
for _doc, (_section, _text) in _where.items():
    _flat = " ".join(_text.split()).lower()
    check(f"{_doc}: a job outliving its session gets the gateway's keys, passwords or git credentials only"
          f" from entries with scripts = true, and no Bash tool way around it ({_section})",
          _orphan in _flat and "bash tool" not in _flat and "run_in_background" not in _flat)


def doc_links(path):
    """Markdown link targets in a doc, outside code blocks."""
    return re.findall(r"\]\(([^)\s]+)\)", re.sub(r"(?ms)^```.*?^```", "", read_or_empty(path)))


def link_ok(doc, target):
    if re.match(r"https?://|#", target):
        return True
    p = (doc.parent / target.split("#")[0]).resolve()
    return p.is_relative_to(REPO.resolve()) and p.exists()


# Every Markdown file at the top (README, CLAUDE, CHANGELOG, SECURITY) and under docs/ (docs/dev/ too).
_docs = [*sorted(REPO.glob("*.md")), *sorted((REPO / "docs").rglob("*.md")),
         *sorted((ROOT / "skills").glob("*/SKILL.md"))]
_bad = [f"{d.relative_to(REPO)}: {t}" for d in _docs for t in doc_links(d) if not link_ok(d, t)]
if _bad:
    print(f"       (broken: {', '.join(_bad)})")
check("doc links stay in the repo or are URLs (a ../gateway link is broken on GitHub)",
      not _bad and "[gateway](https://github.com/p-w-rs/gateway)" in _job_docs["README.md"])
_req = _job_docs["README.md"].partition("\n## Requirements\n")[2].partition("\n## ")[0]
_user_docs = [_job_docs["README.md"], *(p.read_text() for p in sorted((ROOT / "skills").glob("*/SKILL.md")))]
_upg = " ".join(_job_docs["README.md"].partition("\n**Upgrading.**")[2].partition("\n\n")[0].split())
check("the README asks for the latest Claude Code and pixi, with no old minimum or rollback recipe (nor do the "
      "skills name an old Claude Code); its upgrade note keeps the privacy caution about older records",
      "The latest Claude Code" in _req and "The latest pixi" in _req
      and not any(re.search(r"\d+\.\d+\.\d+(\+| or newer)", d) for d in _user_docs)
      and "**Rolling back.**" not in _job_docs["README.md"] and "#v0." not in _job_docs["README.md"]
      and "before 0.8.0 were scrubbed with fewer rules" in _upg and "weren't scrubbed at all" in _upg
      and "exports made earlier stay as they were" in _upg)
# Claude Code refuses a foreground command whose first part is `sleep N` with N >= 25 (2.1.310): a long wait
# is a job or run_in_background, never a long leading `sleep` in what the docs and skills have Claude run.


def long_sleeps(text):
    """Each `sleep N` (N >= 25) that starts a code span or a line (fenced and indented code too), with any
    spaces or tabs between: Claude Code takes any blanks there."""
    return [m[0] for m in re.finditer(r"(?m)(?:^[ \t]*|`)sleep[ \t]+(\d+(?:\.\d*)?)(?![\w.])", text)
            if float(m[1]) >= 25]


_flag = ["run `sleep 60`", "Run:\n\n```sh\nsleep 60\n```\n", "Run:\n\n    sleep 60\n", "run `sleep  60`",
         "run `sleep\t60`", "run `sleep 25.`", "run `sleep 60; echo done`"]
_pass = ["run `true; sleep 60`", "run `sleep 1m`", "run `sleep 20`", "run `sleep 24.9`",
         "a job survives pauses, sleep and the end of the session; 30 s", "a sleep 30 s"]
check("the long-sleep doc check finds a `sleep 25` or more in a code span, a fenced or an indented block, with "
      "two blanks or a tab; not `true; sleep 60`, `sleep 1m`, `sleep 20` or the word in prose",
      [t for t in _flag if not long_sleeps(t)] == [] and [t for t in _pass if long_sleeps(t)] == [])
_long_sleeps = [s for d in [*_user_docs, read_or_empty(REPO / "docs" / "CONFIGURATION.md")]
                for s in long_sleeps(d)]
check("no user doc or skill has Claude run a leading `sleep` of 25 s or more (Claude Code refuses it)",
      not _long_sleeps)

# --- parsing --------------------------------------------------------------------
print("command and keyword parsing:")
check("/check:pause", transcript.parse_command("/check:pause") == ("pause", []))
check("/check:pause now --stop-jobs",
      transcript.parse_command("/check:pause now --stop-jobs") == ("pause", ["now", "--stop-jobs"]))
check("keyword 'pause'", transcript.parse_command(" Pause! ") == ("pause", []))
check("keyword 'pause all now'", transcript.parse_command("pause all now") == ("pause", ["all", "now"]))
check("a sentence is not a keyword", transcript.parse_command("pause the video then continue") is None)
check("other plugin command ignored", transcript.parse_command("/gate:secret list") is None)
check("/check:resume build", transcript.parse_command("/check:resume build") == ("resume", ["build"]))

print("transcript queue replay (format pinned from Spike 1):")
SPIKE = [
    '{"type":"queue-operation","operation":"enqueue","timestamp":"2026-10-16T01:05:22.438Z","sessionId":"6bb8","content":"/spike-pause"}',
    '{"type":"queue-operation","operation":"dequeue","timestamp":"2026-10-16T01:06:01.167Z","sessionId":"6bb8"}',
    '{"type":"queue-operation","operation":"enqueue","timestamp":"2026-10-16T00:52:36.292Z","sessionId":"a205","content":"Note from the user"}',
    '{"type":"queue-operation","operation":"remove","timestamp":"2026-10-16T00:52:41.378Z","sessionId":"a205","content":"Note from the user","reason":"absorbed_mid_turn"}',
]
check("consumed and removed entries leave nothing pending", transcript.pending_queue(SPIKE) == [])
check("a removed entry is reported as removed", [o["content"] for o in transcript.replay_queue(SPIKE)[1]]
      == ["Note from the user"])
q = [SPIKE[0].replace("/spike-pause", "/check:pause now")]
hits = transcript.queued_pauses(q)
check("pending pause found with args", [h[1] for h in hits] == [["now"]])
q2 = [q[0], SPIKE[2], q[0].replace("now", "all").replace("01:05:22", "01:05:23")]
check("every pending pause is found, oldest first (not only the newest)",
      [h[1] for h in transcript.queued_pauses(q2)] == [["now"], ["all"]])
check("FIFO: dequeue pops the oldest", transcript.pending_queue(
    [q[0], q[0].replace("now", "all"), SPIKE[1]])[0]["content"] == "/check:pause all")
check("recent tool calls from the transcript",
      transcript.recent_tool_calls(transcript.tail_lines(TRANSCRIPT))[0]["input"] == "python train.py --epochs 3")

print("tracker parsing:")
t = tracker.info(TRK)
check("stages parsed", [s["status"] for s in t["stages"]] == ["accepted", "in progress", "todo"])
check("Now section", "training epoch 3" in t["sections"]["Now"])
_t2 = TMP / "t2" / "TRACKER.md"
_t2.parent.mkdir()
_t2.write_text("## Stages\n| # | S | Status | N |\n|---|---|---|---|\n| 1 | a | done (agent thinks it's finished) | |\n| 2 | b | in progress | |\n")
check("'done (agent thinks...)' counts as finished", [x["n"] for x in tracker.info(_t2)["open"]] == ["2"])
check("tracker found by session id", lambda: tracker.for_session(PROJ, SID)["slug"] == "train-model")
check("a session id in markdown (`id`) still names the session",
      tracker.parse("Started: x  Session: `abcdef12-34`  Updated: y")["session"] == "abcdef12-34")
check("by slug, partial", lambda: tracker.by_slug(PROJ, "train")["slug"] == "train-model")

# --- hooks through the pre-filter -------------------------------------------------
print("SessionStart:")
out = hook("SessionStart", base(hook_event_name="SessionStart", source="startup"))
check("injects the tracker note with the session id", SID in ctx(out) and "TRACKER.md" in ctx(out))
check("lists unfinished trackers", "train-model" in ctx(out))
check("session recorded", state.load_session(SID).get("cwd") == str(PROJ))
check("Claude pid taken from CLAUDE_PID", state.load_session(SID).get("claude_pid") == MAIN_CLAUDE.pid)
check("note: keep .work/ out of commits via the clone's local exclude file",
      "git rev-parse --git-path info/exclude" in ctx(out) and "git check-ignore -q .work/" in ctx(out))

print("pre-filter fast path:")
n0 = pycalls()
check("not paused: no output", hook("PreToolUse", bash()) is None)
check("not paused, nothing queued: Python not started", pycalls() == n0)
for _ in range(2):
    hook("PostToolUse", bash())
check("PostToolUse below the nudge count: Python not started", pycalls() == n0)
check("ordinary prompt: Python not started",
      hook("UserPromptSubmit", base(prompt="please continue")) is None and pycalls() == n0)
clear_running()

print("job tools (no settings.json rules needed):")
n0 = pycalls()
for tool in ("job_status", "job_tail", "job_stop"):
    out = hook("PreToolUse", base(tool_name=f"mcp__plugin_check_jobs__{tool}", tool_input={"name": "x"}))
    check(f"{tool}: allowed by the shell pre-filter", allowed(out))
check("...without starting Python", pycalls() == n0)
check("job_start: no decision (left to Claude Code's permissions)",
      hook("PreToolUse", base(tool_name="mcp__plugin_check_jobs__job_start",
                              tool_input={"name": "x", "command": "curl example.com"})) is None)
check("a tool_name nested in tool_input can't pose as a job tool (after the real one)",
      hook("PreToolUse", base(tool_name="mcp__other__run",
                              tool_input={"tool_name": "mcp__plugin_check_jobs__job_status"})) is None)
_nested = {"session_id": SID, "transcript_path": str(TRANSCRIPT), "cwd": str(PROJ),
           "tool_input": {"tool_name": "mcp__plugin_check_jobs__job_tail"}, "tool_name": "mcp__other__run"}
check("...nor before it", hook("PreToolUse", _nested) is None)

print("PostToolUse tracker reminder:")
out = hook("PostToolUse", bash())
check("third call: reminder names the tracker", "TRACKER.md" in ctx(out) and "Now" in ctx(out))
check("reminder counts with the same N as the shell (CHECKPOINT_NUDGE_EVERY)", "3+ tool calls" in ctx(out))
check("counter reset after reminder", state.p("count", state.safe_id(SID)).read_text() == "0")
hook("PostToolUse", bash())
hook("PostToolUse", base(tool_name="Edit", tool_use_id="e1",
                         tool_input={"file_path": str(TRK)}))
check("a tracker write resets the counter", state.p("count", state.safe_id(SID)).read_text().strip() == "0")

print("pause by command while idle:")
out = hook("UserPromptSubmit", base(prompt="/check:pause"))
check("command handled, result attached", "<checkpoint-result>Paused" in ctx(out))
check("flag file set", state.p("flags", state.safe_id(SID)).exists())
out = hook("PreToolUse", bash("rm -rf build"))
check("Bash denied while paused", denied(out))
check("deny names the tracker", str(TRK) in reason(out))
cp = jread(cp_file(SID))
_task_sid = state.load_session(SID).get("task_id") or "?"
check("checkpoint written on the first deny in the state folder, next to the task's journal "
      "(journal/<task>/checkpoint-<session>.json), naming the tracker; nothing written into the project",
      cp.get("session_id") == SID and cp.get("stages_in_progress") == ["2. Training"]
      and cp.get("tracker") == str(TRK)
      and cp_file(SID) == state.p("journal", state.safe_id(_task_sid), f"checkpoint-{state.safe_id(SID)}.json")
      and sorted(p.name for p in TRK.parent.iterdir()) == ["TRACKER.md"])
check("checkpoint has the last tool calls", lambda: cp["last_tool_calls"][-1]["tool"] == "Bash")
check("checkpoint records the pause time as an epoch", lambda: abs(cp["paused_ts"] - time.time()) < 60)
out = hook("PreToolUse", base(tool_name="Edit", tool_input={"file_path": str(TRK)}))
check("Edit to the tracker allowed outright (no permission prompt while away)",
      allowed(out) and "tracker" in reason(out))
DRIFT = str(TRK.parent)  # the shell cd'd into .work/train-model
out = hook("PreToolUse", bash(cwd=DRIFT))
check("after a cd: deny still names the project's tracker", denied(out) and str(TRK) in reason(out))
check("after a cd: Edit to the tracker still allowed", allowed(hook("PreToolUse", base(
    cwd=DRIFT, tool_name="Edit", tool_input={"file_path": str(TRK)}))))
check("after a cd: session keeps its project folder", state.load_session(SID).get("cwd") == str(PROJ))
check("Write elsewhere denied", denied(hook("PreToolUse", base(
    tool_name="Write", tool_input={"file_path": str(PROJ / "x.py")}))))
check("../ escape out of the work dir denied", denied(hook("PreToolUse", base(
    tool_name="Write", tool_input={"file_path": str(PROJ / ".work/../x.py")}))))
check("Read allowed (no decision: normal permissions)",
      hook("PreToolUse", base(tool_name="Read", tool_input={"file_path": "/etc/hosts"})) is None)
check("job_status allowed", allowed(hook("PreToolUse", base(tool_name="mcp__plugin_check_jobs__job_status"))))
check("job_start denied", denied(hook("PreToolUse", base(tool_name="mcp__plugin_check_jobs__job_start"))))
for tool in ("TaskCreate", "TaskUpdate", "TodoWrite"):
    check(f"{tool} allowed (keeping the task list isn't new work)",
          hook("PreToolUse", base(tool_name=tool, tool_input={"subject": "x"})) is None)
check("Agent denied", denied(hook("PreToolUse", base(tool_name="Agent", tool_input={"prompt": "x"}))))
check("Skill (e.g. the resume command) denied", denied(hook("PreToolUse", base(
    tool_name="Skill", tool_input={"skill": "check:resume"}))))
out = hook("PreToolUse", bash())
check("after max denies: deny AND continue:false", denied(out) and out.get("continue") is False)
out = hook("PreToolUse", base(tool_name="Bash", tool_input={"command": "ls"}, agent_id="sub1", agent_type="general-purpose"))
check("subagent told to report back", denied(out) and "report" in reason(out))

print("Stop while paused:")
old = cp.get("written_at")
time.sleep(1.1)
check("Stop lets the paused session stop", hook("Stop", base(stop_hook_active=False)) is None)
check("Stop refreshes the checkpoint", jread(cp_file(SID)).get("written_at") not in (None, old))

print("non-user prompts don't resume:")
_tn = hook("UserPromptSubmit", base(prompt="<task-notification>\n<task-id>x</task-id>"))
check("task notification doesn't resume (the model is told the session is paused)",
      state.is_paused(SID) and "<checkpoint-resume>" not in ctx(_tn) and "didn't resume" in ctx(_tn))
check("pause typed again: already paused", "Already paused" in ctx(
    hook("UserPromptSubmit", base(prompt="/check:pause"))))

print("resume by any message:")
out = hook("UserPromptSubmit", base(prompt="ok I'm back, carry on"))
check("resumed, flag cleared", not state.is_paused(SID) and not state.p("flags", state.safe_id(SID)).exists())
check("resume context has Now and Resume notes",
      "<checkpoint-resume>" in ctx(out) and "check the loss curve" in ctx(out) and "Check first" in ctx(out))
check("user sees a 'resumed' line", "resumed" in (out or {}).get("systemMessage", ""))
check("tools allowed again", hook("PreToolUse", bash()) is None)
clear_running()

print("a second pause writes a fresh checkpoint:")
hook("UserPromptSubmit", base(prompt="/check:pause"))
before = jread(cp_file(SID)).get("written_at")
time.sleep(1.1)
hook("PreToolUse", bash("make"))
check("first deny of the new pause rewrites the checkpoint",
      jread(cp_file(SID)).get("written_at") not in (None, before))
hook("UserPromptSubmit", base(prompt="back"))

print("pause from the queue (typed while busy):")
enqueue("/check:pause", "2026-10-16T02:00:00.000Z")
out = hook("PreToolUse", bash(tid="tu2"))
check("queued /check:pause: next tool call denied", denied(out))
check("session paused by the queued command", state.load_session(SID).get("paused_by") == "queued command")
dequeue("2026-10-16T02:00:05.000Z")
out = hook("UserPromptSubmit", base(prompt="/check:pause"))   # it runs when the turn ends
check("(a) its echo says it already took effect while busy", "took effect at the next tool call" in ctx(out))
hook("UserPromptSubmit", base(prompt="continue"))
n0 = pycalls()
check("after resume: the consumed queue entry doesn't re-pause", hook("PreToolUse", bash(tid="tu3")) is None)
check("seen line recorded: later calls skip Python", hook("PreToolUse", bash(tid="tu4")) is None
      and pycalls() == n0)
clear_running()

print("resume after another session took the tracker over (d):")
hook("UserPromptSubmit", base(prompt="/check:pause"))
hook("PreToolUse", bash(tid="tu5"))   # first deny writes the checkpoint, records task
_orig = TRK.read_text()
TRK.write_text(_orig.replace("Session: sess-aaaa1111", "Session: other-session-9999 (earlier: sess-aaaa1111)")
               .replace("| 2 | Training | in progress |", "| 2 | Training | done |"))
out = hook("UserPromptSubmit", base(prompt="back again"))
check("(d) resume still shows the task's tracker", "Tracker: " + str(TRK) in ctx(out))
TRK.write_text(_orig)
clear_running()

print("plain keyword:")
out = hook("UserPromptSubmit", base(prompt="pause"))
check("'pause' pauses and tells the agent to stop", state.is_paused(SID) and "end your turn" in ctx(out))
hook("UserPromptSubmit", base(prompt="/check:resume"))
check("/check:resume resumes", not state.is_paused(SID))

# --- two sessions in one project ------------------------------------------------------
print("two sessions in one project (another session's tracker is never used):")
PROJ2 = TMP / "proj2"
(PROJ2 / ".work" / "task-a").mkdir(parents=True)
TRKA = PROJ2 / ".work" / "task-a" / "TRACKER.md"
TRKA.write_text(_orig.replace("Session: sess-aaaa1111", "Session: aaaaaaaa-1111")
                .replace("check the loss curve", "TASK-A NEXT ACTION"))
TA = TMP / "ta.jsonl"
TA.write_text("")
CLAUDE_B, CLAUDE_C = fake_claude(), fake_claude()
SIDB, SIDC = "bbbbbbbb-2222", "cccccccc-3333"


def pb(**kw):
    return dict({"session_id": SIDB, "transcript_path": str(TA), "cwd": str(PROJ2)}, **kw)


def pc(**kw):
    return dict({"session_id": SIDC, "transcript_path": str(TA), "cwd": str(PROJ2)}, **kw)


EB, EC = {"CLAUDE_PID": str(CLAUDE_B.pid)}, {"CLAUDE_PID": str(CLAUDE_C.pid)}
hook("SessionStart", pb(source="startup"), env=EB)
hook("UserPromptSubmit", pb(prompt="/check:pause"), env=EB)
out = hook("PreToolUse", pb(tool_name="Read", tool_input={"file_path": "x"}), env=EB)
out = hook("PreToolUse", pb(tool_name="Write", tool_input={"file_path": str(PROJ2 / "y.py")}), env=EB)
check("B's deny doesn't send it to A's tracker", denied(out) and str(TRKA) not in reason(out)
      and "<task-slug>/TRACKER.md" in reason(out))
check("B's checkpoint names no tracker (A's isn't B's), in the state folder",
      jread(cp_file(SIDB)).get("tracker", 1) is None and jread(cp_file(SIDB)).get("session_id") == SIDB
      and state.home() in cp_file(SIDB).parents and not (TRKA.parent / "checkpoint.json").exists())
check("B's task isn't set to A's", state.load_session(SIDB).get("task") != "task-a")
out = hook("UserPromptSubmit", pb(prompt="back"), env=EB)
check("B's resume doesn't hand it A's next action", "<checkpoint-resume>" in ctx(out)
      and "TASK-A NEXT ACTION" not in ctx(out))
hook("UserPromptSubmit", pc(prompt="do something"), env=EC)
time.sleep(0.05)
hook("PostToolUse", pc(tool_name="Read", tool_use_id="c1", tool_input={"file_path": "x"}), env=EC)
check("unpaused C, no tracker of its own: no Stop block about A's tracker",
      hook("Stop", pc(stop_hook_active=False), env=EC) is None)
check("A itself still finds its tracker",
      lambda: core.session_tracker("aaaaaaaa-1111-full", [str(PROJ2)])["slug"] == "task-a")
(PROJ2 / ".work" / "forgot-id").mkdir()
UNCL = PROJ2 / ".work" / "forgot-id" / "TRACKER.md"
UNCL.write_text(tracker.TEMPLATE.replace("| 1 | ...   | in progress | |", "| 1 | build | in progress | |"))
check("a tracker naming no session that nobody wrote here isn't taken by anyone",
      core.session_tracker(SIDB, [str(PROJ2)]) is None and core.session_tracker(SIDC, [str(PROJ2)]) is None)
for _ in range(2):
    hook("PostToolUse", pb(tool_name="Write", tool_use_id="bw1", tool_input={"file_path": str(UNCL)},
                           tool_response={"filePath": str(UNCL)}), env=EB)
check("fast.sh records the trackers a session writes (once each)",
      lambda: state.p("trackers", SIDB).read_text().splitlines() == [str(UNCL)])
check("a tracker with no session id (template placeholder) is the fallback for the session that wrote it",
      lambda: core.session_tracker(SIDB, [str(PROJ2)])["slug"] == "forgot-id")
(PROJ2 / ".work" / "c-notes").mkdir()
CNOTES = PROJ2 / ".work" / "c-notes" / "TRACKER.md"
CNOTES.write_text("# C's notes\nSession: cccccccc-3333\n")
hook("PostToolUse", pc(tool_name="Edit", tool_use_id="cw1", tool_input={"file_path": str(CNOTES)}), env=EC)
check("...but not for another session in the project (that wrote only its own tracker)",
      state.written_trackers(SIDC) == {str(CNOTES)} and core.session_tracker(SIDC, [str(PROJ2)])["slug"] == "c-notes"
      and tracker.unclaimed(PROJ2, state.written_trackers(SIDC)) is None)
hook("UserPromptSubmit", pc(prompt="next thing"), env=EC)
time.sleep(0.05)
hook("PostToolUse", pc(tool_name="Read", tool_use_id="c2", tool_input={"file_path": "x"}), env=EC)
CNOTES.unlink()
check("...so that session gets no Stop block about it", hook("Stop", pc(stop_hook_active=False), env=EC) is None)
UNCL.unlink()

# --- pause all ------------------------------------------------------------------------
print("pause all / resume all:")
CLAUDE_2 = fake_claude()
SID2 = "sess-bbbb"
T2 = TMP / "t2.jsonl"   # each session has its own transcript
T2.write_text("")
E2 = {"CLAUDE_PID": str(CLAUDE_2.pid)}


def b2(**kw):
    return base(session_id=SID2, transcript_path=str(T2), **kw)


hook("SessionStart", b2(source="startup"), env=E2)
dead = subprocess.Popen(["true"])
dead.wait()
SIDD = "sess-dead"
state.update_session(SIDD, cwd=str(PROJ), claude_pid=dead.pid, started_ts=time.time())
out = hook("UserPromptSubmit", base(prompt="/check:pause all"))
check("pause all: both running sessions paused", state.is_paused(SID) and state.is_paused(SID2))
check("...but not a session whose process is gone", not state.is_paused(SIDD))
check("...and no global flag that new sessions would inherit", not state.p("paused-all").exists())
SID4 = "sess-later"
T4 = TMP / "t4.jsonl"
T4.write_text("")
E4 = {"CLAUDE_PID": str(fake_claude().pid)}
hook("SessionStart", base(session_id=SID4, transcript_path=str(T4), source="startup"), env=E4)
n0 = pycalls()
check("a session started after pause all isn't paused", hook("PreToolUse", base(
    session_id=SID4, transcript_path=str(T4), tool_name="Read", tool_input={"file_path": "x"}), env=E4) is None
    and not state.is_paused(SID4))
check("...and its hooks stay on the fast path", pycalls() == n0)
hook("UserPromptSubmit", b2(prompt="hi again"), env=E2)
check("a message resumes only that session", not state.is_paused(SID2) and state.is_paused(SID))
n0 = pycalls()
hook("PreToolUse", b2(tool_name="Read", tool_input={"file_path": "x"}), env=E2)
hook("UserPromptSubmit", b2(prompt="go on"), env=E2)
check("a resumed session is back on the fast path (no Python per tool call)", pycalls() == n0)
hook("UserPromptSubmit", b2(prompt="/check:pause"), env=E2)
out = hook("UserPromptSubmit", base(prompt="/check:resume all"))
check("resume all: every session un-paused", not state.is_paused(SID) and not state.is_paused(SID2))
check("...this one resumes with its context", "<checkpoint-resume>" in ctx(out))
out = hook("UserPromptSubmit", b2(prompt="carry on"), env=E2)
check("...the others get their resume notes with their next message", "<checkpoint-resume>" in ctx(out))
check("...once", hook("UserPromptSubmit", b2(prompt="more"), env=E2) is None)

print("queued pause all, run again at the end of the turn:")
enqueue("/check:pause all", "2026-10-16T04:00:00.000Z")
check("the gate applies it at the next tool call", denied(hook("PreToolUse", bash(tid="tq1")))
      and state.is_paused(SID2))
hook("UserPromptSubmit", b2(prompt="I'm here"), env=E2)
dequeue("2026-10-16T04:00:09.000Z")
out = hook("UserPromptSubmit", base(prompt="/check:pause all"))
check("its echo doesn't re-pause a session resumed meanwhile", not state.is_paused(SID2))
check("...and says it was already done", "Already done" in ctx(out))
check("...with what it did then", "Pausing every running session" in ctx(out))
hook("UserPromptSubmit", base(prompt="back"))
enqueue("pause all", "2026-10-16T04:10:00.000Z")
hook("PreToolUse", bash(tid="tq2"))
remove_op("pause all", "2026-10-16T04:10:02.000Z")  # absorbed into the turn: no echo will come
hook("UserPromptSubmit", base(prompt="back"))
hook("UserPromptSubmit", b2(prompt="back"), env=E2)
hook("UserPromptSubmit", base(prompt="pause all"))
check("an absorbed queued 'pause all' doesn't swallow a later one", state.is_paused(SID2) and state.is_paused(SID))
hook("UserPromptSubmit", base(prompt="/check:resume all"))
hook("UserPromptSubmit", b2(prompt="ok"), env=E2)
clear_running()

print("a pause command queued in a session that is already paused (and still busy):")
hook("UserPromptSubmit", b2(prompt=f"/check:pause {SID}"), env=E2)   # paused from elsewhere
check("paused from another session", state.is_paused(SID) and not state.is_paused(SID2))
enqueue(f"/check:pause {SID2}", "2026-10-16T04:20:00.000Z")   # typed into it while busy
out = hook("PreToolUse", bash(tid="tq3"))
check("its next tool call is denied, and the queued command is applied then, not at the turn's end",
      denied(out) and state.is_paused(SID2))
hook("UserPromptSubmit", b2(prompt="back"), env=E2)   # resumed meanwhile
dequeue("2026-10-16T04:20:07.000Z")
out = hook("UserPromptSubmit", base(prompt=f"/check:pause {SID2}"))
check("...its echo at the turn's end says it was already done, and doesn't apply it again",
      "Already done" in ctx(out) and not state.is_paused(SID2))
check("...the session stays paused (a pause command doesn't resume it)", state.is_paused(SID))
_real_check_queue = hooks.check_queue


def gate_with_broken_queue():
    hooks.check_queue = lambda *a: 1 / 0
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return denied(hooks.gate(bash(tid="tq4")))
    finally:
        hooks.check_queue = _real_check_queue


check("...an error while applying queued commands still denies the paused session's call",
      gate_with_broken_queue)
hook("UserPromptSubmit", base(prompt="back"))
clear_running()

print("a queued pause's echo is remembered only until it comes:")
SIDQ = "sess-qqqq"
TQ = TMP / "tq.jsonl"
TQ.write_text("")
EQ = {"CLAUDE_PID": str(fake_claude().pid)}


def pq(**kw):
    return dict({"session_id": SIDQ, "transcript_path": str(TQ), "cwd": str(PROJ)}, **kw)


def qread(**kw):
    return pq(tool_name="Read", tool_use_id="rq", tool_input={"file_path": "x"}, **kw)


def remembered():
    return bool(state.queued_pauses(state.load_session(SIDQ))) or state.p("queued", SIDQ).exists()


def remember(**entry):
    """The memory as if the gate had applied this queued command earlier."""
    state.drop_queued(SIDQ)
    state.add_queued(SIDQ, dict({"content": "/check:pause all", "at": state.now_iso(), "result": "r"}, **entry))


def resume_everyone():
    core.resume_all()
    for f in state.p("pending").glob("*"):
        f.unlink()


hook("SessionStart", pq(source="startup"), env=EQ)
# Observed in 2.1.301: text absorbed into the turn fires UserPromptSubmit
# about 7 ms before Claude Code writes the queue's remove record.
enqueue("pause all", "2026-10-16T05:00:00.000Z", TQ)
check("queued 'pause all' applied at the next tool call", denied(hook("PreToolUse", pq(
    tool_name="Bash", tool_use_id="bq", tool_input={"command": "ls"}), env=EQ)) and state.is_paused(SID2))
check("...and remembered (queued/<sid> sends the next prompt to Python)",
      state.p("queued", SIDQ).exists())
hook("UserPromptSubmit", b2(prompt="I'm back"), env=E2)   # resumed meanwhile
out = hook("UserPromptSubmit", pq(prompt="pause all"), env=EQ)   # the absorbed text as a prompt
remove_op("pause all", "2026-10-16T05:00:00.050Z", TQ)
check("absorbed text (its prompt comes before the remove record) is the echo: not applied again",
      "Already done" in ctx(out) and not state.is_paused(SID2))
check("...and then forgotten", not remembered())
hook("UserPromptSubmit", pq(prompt="back"), env=EQ)
out = hook("UserPromptSubmit", pq(prompt="pause all"), env=EQ)
check("the same command typed later is applied", state.is_paused(SID2) and "Already done" not in ctx(out))
resume_everyone()

enqueue("/check:pause all", "2026-10-16T05:10:00.000Z", TQ)
hook("PreToolUse", qread(), env=EQ)
remove_op("/check:pause all", "2026-10-16T05:10:01.000Z", TQ)   # it never comes back as a prompt
hook("UserPromptSubmit", pq(prompt="go on"), env=EQ)
check("another prompt first: the queued command is forgotten", not remembered())
resume_everyone()
FILLER = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "x" * 2000}]}}
for _ in range(150):
    tline(FILLER, TQ)   # the queue records leave the 256 KB tail
out = hook("UserPromptSubmit", pq(prompt="/check:pause all"), env=EQ)
check("...so typing it again later pauses again (not 'Already done')",
      state.is_paused(SID2) and "Already done" not in ctx(out))
resume_everyone()

enqueue("/check:pause sess-bbbb", "2026-10-16T05:20:00.000Z", TQ)
check("queued 'pause <other session>': that session is paused, this one goes on",
      hook("PreToolUse", qread(), env=EQ) is None and state.is_paused(SID2) and not state.is_paused(SIDQ))
remove_op("/check:pause sess-bbbb", "2026-10-16T05:20:01.000Z", TQ)
n0 = pycalls()
hook("UserPromptSubmit", pq(prompt="hello"), env=EQ)
check("an ordinary prompt in this unpaused session still reaches Python (queued/<sid>)",
      pycalls() > n0 and not remembered())
n0 = pycalls()
hook("UserPromptSubmit", pq(prompt="and more"), env=EQ)
check("...after which prompts are back on the fast path", pycalls() == n0)
hook("UserPromptSubmit", b2(prompt="back"), env=E2)
out = hook("UserPromptSubmit", pq(prompt="/check:pause sess-bbbb"), env=EQ)
check("...and the same command typed later pauses that session again",
      state.is_paused(SID2) and "Already done" not in ctx(out))
resume_everyone()

enqueue("first this", "2026-10-16T05:30:00.000Z", TQ)
enqueue("/check:pause all", "2026-10-16T05:30:01.000Z", TQ)
hook("PreToolUse", qread(), env=EQ)
hook("UserPromptSubmit", b2(prompt="I'm here"), env=E2)
dequeue("2026-10-16T05:30:09.000Z", TQ)
hook("UserPromptSubmit", pq(prompt="first this"), env=EQ)
check("a prompt queued ahead of it doesn't make it forget", remembered())
dequeue("2026-10-16T05:30:20.000Z", TQ)
out = hook("UserPromptSubmit", pq(prompt="/check:pause all"), env=EQ)
check("...its echo still comes next and isn't applied again",
      "Already done" in ctx(out) and not state.is_paused(SID2))
resume_everyone()

enqueue("/check:pause all", "2026-10-16T05:40:00.000Z", TQ)
hook("PreToolUse", qread(), env=EQ)
hook("SessionStart", pq(source="resume"), env=EQ)   # a new Claude process: the old queue is gone
check("a restarted session forgets it", not remembered())
resume_everyone()
out = hook("UserPromptSubmit", pq(prompt="/check:pause all"), env=EQ)
check("...and the same command typed then is applied", state.is_paused(SID2) and "Already done" not in ctx(out))
resume_everyone()

remember(ts="2026-01-01T00:00:00.000Z", at_ts=time.time() - 60)
out = hook("UserPromptSubmit", pq(prompt="/check:pause all"), env=EQ)
check("no trace of it in the transcript: a recent one still counts as the echo",
      "Already done" in ctx(out) and not state.is_paused(SID2))
remember(ts="2026-01-01T00:00:00.000Z", at_ts=time.time() - state.QUEUED_ECHO_MAX_AGE_SECONDS - 60)
out = hook("UserPromptSubmit", pq(prompt="/check:pause all"), env=EQ)
check("...an old one doesn't (QUEUED_ECHO_MAX_AGE_SECONDS)",
      state.is_paused(SID2) and "Already done" not in ctx(out))
resume_everyone()
enqueue("never ends", "2026-10-16T05:50:00.000Z", TQ)   # a queue record that never got its dequeue
remember(ts="2026-10-16T05:50:00.000Z", at_ts=time.time() - state.QUEUED_ECHO_MAX_AGE_SECONDS - 60)
hook("UserPromptSubmit", pq(prompt="something else"), env=EQ)
check("an old memory whose queue entry never ended is dropped at the next prompt", not remembered())
clear_running()
resume_everyone()

print("two queued pause commands in one turn are each remembered:")
enqueue("/check:pause sess-bbbb", "2026-10-16T06:00:00.000Z", TQ)
hook("PreToolUse", qread(), env=EQ)                        # applied: sess-bbbb paused
hook("UserPromptSubmit", b2(prompt="I'm here"), env=E2)    # ...and resumed by its user
enqueue("pause all", "2026-10-16T06:00:05.000Z", TQ)
hook("PreToolUse", qread(), env=EQ)                        # applied: every session paused
check("both applied while busy, both remembered",
      state.is_paused(SIDQ) and len(state.queued_pauses(state.load_session(SIDQ))) == 2)
hook("UserPromptSubmit", b2(prompt="I'm here again"), env=E2)
dequeue("2026-10-16T06:00:30.000Z", TQ)
out = hook("UserPromptSubmit", pq(prompt="/check:pause sess-bbbb"), env=EQ)
check("the first one's echo isn't applied again (the second didn't replace its memory)",
      "Already done" in ctx(out) and not state.is_paused(SID2))
check("...the second is still remembered (queued/<sid> kept)",
      len(state.queued_pauses(state.load_session(SIDQ))) == 1 and state.p("queued", SIDQ).exists())
dequeue("2026-10-16T06:00:31.000Z", TQ)
out = hook("UserPromptSubmit", pq(prompt="pause all"), env=EQ)
check("...and its echo isn't applied again either", "Already done" in ctx(out) and not state.is_paused(SID2))
check("...after which nothing is remembered (queued/<sid> gone)", not remembered())
resume_everyone()
for i in (0, 5):   # the same command queued twice, sess-bbbb resumed in between
    enqueue("/check:pause sess-bbbb", f"2026-10-16T06:10:0{i}.000Z", TQ)
    hook("PreToolUse", qread(), env=EQ)
    hook("UserPromptSubmit", b2(prompt="back"), env=E2)
for i in (0, 1):
    dequeue(f"2026-10-16T06:10:3{i}.000Z", TQ)
    out = hook("UserPromptSubmit", pq(prompt="/check:pause sess-bbbb"), env=EQ)
    check(f"the same command queued twice: echo {i + 1} isn't applied again",
          "Already done" in ctx(out) and not state.is_paused(SID2))
check("...and both are then forgotten", not remembered())
resume_everyone()

print("several pause commands queued before one tool call are all applied and remembered:")
enqueue_all([("pause", "2026-10-16T06:15:00.000Z"), ("/check:pause sess-bbbb", "2026-10-16T06:15:01.000Z")], TQ)
out = hook("PreToolUse", pq(tool_name="Bash", tool_use_id="bq2", tool_input={"command": "ls"}), env=EQ)
check("'pause' then 'pause <other>': both applied (not only the newest)",
      denied(out) and state.is_paused(SIDQ) and state.is_paused(SID2))
check("...each remembered", [e["content"] for e in state.queued_pauses(state.load_session(SIDQ))]
      == ["pause", "/check:pause sess-bbbb"])
dequeue("2026-10-16T06:15:30.000Z", TQ)
out1 = hook("UserPromptSubmit", pq(prompt="pause"), env=EQ)
dequeue("2026-10-16T06:15:31.000Z", TQ)
out2 = hook("UserPromptSubmit", pq(prompt="/check:pause sess-bbbb"), env=EQ)
check("...their echoes say so", "Already paused" in ctx(out1) and "Already done" in ctx(out2)
      and not remembered())
resume_everyone()
clear_running()
enqueue_all([("/check:pause sess-bbbb", "2026-10-16T06:16:00.000Z"),
             ("/check:pause sess-bbbb", "2026-10-16T06:16:05.000Z")], TQ)   # retyped: nothing seemed to happen
hook("PreToolUse", qread(), env=EQ)
check("the same command queued twice before one tool call: applied, both remembered",
      state.is_paused(SID2) and len(state.queued_pauses(state.load_session(SIDQ))) == 2)
hook("UserPromptSubmit", b2(prompt="back"), env=E2)   # sess-bbbb's user resumes it
for i in (0, 1):
    dequeue(f"2026-10-16T06:16:3{i}.000Z", TQ)
    out = hook("UserPromptSubmit", pq(prompt="/check:pause sess-bbbb"), env=EQ)
    check(f"...echo {i + 1} doesn't re-pause the session resumed meanwhile",
          "Already done" in ctx(out) and not state.is_paused(SID2))
check("...and both are then forgotten", not remembered())
resume_everyone()

print("an echo that comes while the queued command is still being applied:")
QOP = {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-10-16T06:20:00.000Z",
       "content": "/check:pause all now"}
enqueue(QOP["content"], QOP["timestamp"], TQ)
early = {}
_real_pause = core.pause


def pause_then_echo(sid_, args_, **kw):
    # Claude Code runs an absorbed prompt as soon as the killed call ends,
    # while `pause now` may still be stopping other sessions' commands.
    if kw.get("echo") is None and "out" not in early:
        early["out"] = hook("UserPromptSubmit", pq(prompt=QOP["content"]), env=EQ)
    return _real_pause(sid_, args_, **kw)


core.pause = pause_then_echo
try:
    hooks._pause_queued(SIDQ, [(QOP, ["all", "now"])], by="queued command")
finally:
    core.pause = _real_pause
check("it finds the memory (written before the command is applied): not applied twice",
      "Already done" in ctx(early.get("out")))
check("...the command itself is applied, once",
      state.is_paused(SID2) and state.load_session(SID2).get("paused_by") == "queued command (now)")
check("...and the memory isn't brought back when it finishes", not remembered())
resume_everyone()


def pause_fails(*a, **kw):
    raise RuntimeError("boom")


core.pause = pause_fails
try:
    hooks._pause_queued(SIDQ, [(dict(QOP, timestamp="2026-10-16T06:30:00.000Z"), ["all"])], by="queued command")
except RuntimeError:
    pass
finally:
    core.pause = _real_pause
check("a queued command that failed to apply is forgotten (its echo applies it)", not remembered())
_calls = []


def pause_fails_second(*a, **kw):
    _calls.append(1)
    if len(_calls) == 2:
        raise RuntimeError("boom")
    return _real_pause(*a, **kw)


core.pause = pause_fails_second
try:
    hooks._pause_queued(SIDQ, [(dict(QOP, timestamp="2026-10-16T06:40:00.000Z"), ["all"]),
                               (dict(QOP, timestamp="2026-10-16T06:40:01.000Z"), ["all"])],
                        by="queued command")
except RuntimeError:
    pass
finally:
    core.pause = _real_pause
check("...of two, the one applied stays remembered and the one that failed is forgotten",
      [e["ts"] for e in state.queued_pauses(state.load_session(SIDQ))] == ["2026-10-16T06:40:00.000Z"])
state.drop_queued(SIDQ)
resume_everyone()

print("no reader of older versions' state:")
state.write_json(state.p("paused-all"), {"at": state.now_iso(), "ts": time.time(), "by": "command"})
n0 = pycalls()
out = hook("PreToolUse", b2(tool_name="Bash", tool_use_id="tm1",
                              tool_input={"command": "ls"}), env=E2)
check("a leftover global paused-all file (version 0.1's) pauses nothing: the call goes ahead on the fast path, "
      "no session is paused and the file is left as it is",
      out is None and pycalls() == n0 and not state.is_paused(SID2) and not state.is_paused(SID)
      and state.p("paused-all").exists())
state.remove(state.p("paused-all"))

print("status:")
out = hook("UserPromptSubmit", base(prompt="/check:status"))
check("status result attached", "<checkpoint-result>" in ctx(out) and SID[:8] in ctx(out))

print("Stop nudge:")
hook("UserPromptSubmit", base(prompt="do the next bit"))
time.sleep(0.05)
hook("PostToolUse", bash(tid="tu9"))
out = hook("Stop", base(stop_hook_active=False))
check("in-progress tracker not touched this turn: block once", (out or {}).get("decision") == "block")
check("stop_hook_active: no second block", hook("Stop", base(stop_hook_active=True)) is None)
TRK.write_text(TRK.read_text().replace("epoch 3", "epoch 4"))
check("tracker updated this turn: no block", hook("Stop", base(stop_hook_active=False)) is None)
hook("UserPromptSubmit", base(prompt="just a question"))
check("a turn with no tool calls: no block", hook("Stop", base(stop_hook_active=False)) is None)

print("resume <task> in a new session:")
SID3 = "sess-cccc"
E3 = {"CLAUDE_PID": str(fake_claude().pid)}
out = hook("UserPromptSubmit", base(session_id=SID3, prompt="/check:resume train-model"), env=E3)
check("context from the tracker and checkpoint", "check the loss curve" in ctx(out)
      and "Checkpoint at pause" in ctx(out) and SID3 in ctx(out))
out = hook("UserPromptSubmit", base(session_id=SID3, prompt="/check:resume nope"), env=E3)
check("unknown task: lists trackers", "train-model" in ctx(out))

print("paused time survives a time zone change:")
cp_tz = {"paused_at": "2026-10-18T22:00:00+0900", "paused_ts": time.time() - 600, "session_id": "nobody"}
check("epoch paused_ts: 'Paused for 10 min.'", "Paused for 10 min." in core._changes_since(cp_tz, ""))
_local = datetime.now().astimezone().utcoffset() or timedelta(0)
_away = timedelta(hours=((_local.total_seconds() // 3600 + 17) % 24) - 12)   # 5 h east of here
west = datetime.now(timezone(_away)) - timedelta(minutes=90)
cp_old = {"paused_at": west.strftime("%Y-%m-%dT%H:%M:%S%z"), "session_id": "nobody"}
check("a checkpoint with only paused_at (as before 0.3; no epoch): no pause length is made up from its local time",
      not any(x.startswith("Paused for") for x in core._changes_since(cp_old, "")))

# --- jobs ----------------------------------------------------------------------------
print("jobs:")
r = jobs.start("quick", command="echo hello; echo err >&2; exit 3", cwd=str(PROJ),
               redo_hint="rerun", session_id=SID)
check("job_start ok", lambda: r["ok"] and r["job"]["state"] == "running")
time.sleep(1.5)
s = job("quick")
check("exit code captured", s.get("state") == "exited" and s.get("exit_code") == 3)
check("runtime ends at exit, not at status check", s.get("runtime_s", 9) < 1.0)
check("tail has stdout and stderr", "hello" in jobs.tail("quick").get("tail", "")
      and "err" in jobs.tail("quick").get("tail", ""))
r = jobs.start("long", argv=["sleep", "30"], cwd=str(PROJ), redo_hint="rerun", session_id=SID)
check("argv job running", job("long").get("state") == "running")
check("job is in its own process group", lambda: os.getpgid(jobs.find("long")["pid"]) != os.getpgrp())
r = jobs.stop("long", "test")
check("job_stop kills it", r.get("stopped") and job("long").get("state") == "killed")
r = jobs.start("stubborn", command='trap "" TERM; sleep 60 & wait; sleep 60', cwd=str(PROJ),
               redo_hint="", session_id=SID)
time.sleep(0.5)
pg = (jobs.find("stubborn") or {}).get("pgid", 0)
r = jobs.stop("stubborn", "test", grace=0.5)
check("a job that ignores SIGTERM: SIGKILL after the grace period, then 'killed'",
      r.get("stopped") and job("stubborn").get("state") == "killed")
check("...and nothing of its process group is left running", pg and not procs.group_alive(pg))
if pg and procs.group_alive(pg):
    os.killpg(pg, signal.SIGKILL)
r = jobs.start("sticky", argv=["sleep", "30"], cwd=str(PROJ), redo_hint="", session_id=SID)
_real = procs.group_alive
procs.group_alive = lambda *a, **k: True   # as if SIGKILL couldn't end it
try:
    r = jobs.stop("sticky", "test", grace=0.1)
finally:
    procs.group_alive = _real
check("job_stop that can't empty the group reports stop_failed, not killed",
      lambda: r.get("ok") is False and not r.get("stopped") and "stop_failed" in r.get("reason", "")
      and jobs._registry()["jobs"][r["job"]["id"]]["state"] == "stop_failed")
check("...and becomes 'killed' once the group is really gone",
      wait_for(lambda: job("sticky").get("state") == "killed", 5))
r = jobs.start("gone", argv=["sleep", "30"], cwd=str(PROJ), redo_hint="", session_id=SID)
os.killpg(jobs.find("gone")["pgid"], signal.SIGKILL)
time.sleep(0.3)
check("killed from outside: vanished", job("gone").get("state") == "vanished")
# Just after Popen the wrapper's command line may not be readable yet (Linux
# can show "[sh]" while exec sets up its arguments): a live process at its pid
# that started when the job did is the wrapper, so it is running, never
# (finally) vanished; one that started long before is a reused pid.
_frec = {"id": "fresh-20261020-000000", "pid": 999999, "pgid": 999999, "exit_file": str(TMP / "no-exit-file"),
         "state": "running", "started_ts": time.time()}
_fpt = {999999: {"pid": 999999, "ppid": 1, "pgid": 999999, "stat": "S", "start": time.time() - 0.01, "cmd": "[sh]"}}
check("a job whose wrapper's command line isn't readable yet (just started, '[sh]'): running, not vanished",
      jobs._refresh_state(dict(_frec), _fpt)["state"] == "running")
check("...but a live process at that pid that started long before the job (a reused pid): vanished",
      jobs._refresh_state(dict(_frec), {999999: dict(_fpt[999999], start=time.time() - 3600)})["state"] == "vanished")
check("session filter", {j["name"] for j in jobs.status(session_id=SID)["jobs"]}
      == {"quick", "long", "stubborn", "sticky", "gone"})
# A job's log is capped (here 4 KiB, looked at every 0.2 s): the wrapper moves
# a log past half of it to output.log.1, so a verbose job can't fill the disk.
os.environ.update(CHECKPOINT_JOB_LOG_KEEP="4096", CHECKPOINT_JOB_LOG_CHECK="0.2")
_burst = "b() { i=0; while [ $i -lt 30 ]; do echo line-$1-" + "0123456789" * 9 + "; i=$((i+1)); done; }; "
r = jobs.start("capped", command=_burst + "b 1; sleep 1; b 2; sleep 1; b 3; sleep 1; b 4; sleep 1; echo LAST-LINE; "
               "sleep 0.5", cwd=str(PROJ), redo_hint="", session_id="sess-capped")
del os.environ["CHECKPOINT_JOB_LOG_KEEP"], os.environ["CHECKPOINT_JOB_LOG_CHECK"]
_cap = jobs.find("capped") or {}
check("a verbose job's log is capped: rotated into output.log.1 while it runs, neither file past the cap and one "
      "look's output", lambda: wait_for(lambda: job("capped").get("state") == "exited", 15)
      and Path(_cap["log"]).stat().st_size <= 2048 + 3200 and Path(_cap["log"] + ".1").stat().st_size <= 2048 + 3200
      and int((Path(_cap["log"]).parent / "log_rotated").read_text().split()[0]) >= 2)
check("...job_status says its earliest output was dropped; job_tail still ends with its last line",
      lambda: "earliest" in job("capped").get("log_truncated", "") and "were dropped" in job("capped")["log_truncated"]
      and jobs.tail("capped", 5)["tail"].endswith("LAST-LINE")
      and "log_truncated" in jobs.tail("capped", 5)["job"] and "log_truncated" not in job("quick"))
check("...and the watcher ends with the job: nothing of its process group is left",
      lambda: _cap.get("pgid") and wait_for(lambda: not procs.group_alive(_cap["pgid"]), 5))
check("both command and argv refused", not jobs.start("x", command="a", argv=["b"])["ok"])
check("missing cwd refused", not jobs.start("x", command="a", cwd=str(TMP / "nope"))["ok"])

print("jobs don't inherit checkpoint's own pixi activation:")
OURS = str(ROOT)
OUR_BIN = f"{OURS}/.pixi/envs/default/bin"
ACT = {"PIXI_PROJECT_MANIFEST": f"{OURS}/pixi.toml", "PIXI_PROJECT_ROOT": OURS,
       "PIXI_PROJECT_NAME": "checkpoint", "PIXI_ENVIRONMENT_NAME": "default", "PIXI_IN_SHELL": "1",
       "CONDA_PREFIX": f"{OURS}/.pixi/envs/default", "CONDA_DEFAULT_ENV": "checkpoint",
       "INIT_CWD": OURS, "PIXI_HOME": "/keep/pixi-home",
       "PATH": os.pathsep.join([OUR_BIN, "/usr/bin", "/bin"])}
saved = {k: os.environ.get(k) for k in ACT}
os.environ.update(ACT)
try:
    jobs.start("envjob", command="env", cwd=str(PROJ), redo_hint="", session_id=SID)
finally:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
time.sleep(1)
jenv = dict(line.split("=", 1) for line in Path(jobs.find("envjob")["log"]).read_text().splitlines()
            if "=" in line)
check("(c) job env: no PIXI_PROJECT_MANIFEST/ROOT, CONDA_PREFIX, PIXI_IN_SHELL",
      not {"PIXI_PROJECT_MANIFEST", "PIXI_PROJECT_ROOT", "CONDA_PREFIX", "PIXI_IN_SHELL",
           "CONDA_DEFAULT_ENV", "INIT_CWD"} & set(jenv))
check("(c) job env: our .pixi bin dir gone from PATH, the rest kept",
      jenv.get("PATH", "").split(os.pathsep) == ["/usr/bin", "/bin"])
check("job env: the user's own pixi settings (PIXI_HOME) kept", jenv.get("PIXI_HOME") == "/keep/pixi-home")
other = dict(ACT, PIXI_PROJECT_ROOT="/elsewhere/proj", PIXI_PROJECT_MANIFEST="/elsewhere/proj/pixi.toml")
check("job env: another project's activation (session under pixi shell) kept",
      jobs.job_env(other).get("PIXI_PROJECT_MANIFEST") == "/elsewhere/proj/pixi.toml")

print("jobs started by another, still-running process (test 5 bug):")
STARTER = f"""
import sys, time
sys.path.insert(0, {str(ROOT / "src")!r})
from checkpoint import jobs
jobs.start("orphan", argv=["sleep", "30"], cwd={str(PROJ)!r}, redo_hint="", session_id="other")
print("started", flush=True)
time.sleep(20)   # stays alive and never reaps, like an open session's MCP server
"""
starter = subprocess.Popen([sys.executable, "-c", STARTER], stdout=subprocess.PIPE, text=True)
starter.stdout.readline()
t0 = time.time()
try:
    r = jobs.stop("orphan", "test")
    err = None
except Exception as e:  # noqa: BLE001
    r, err = {}, e
check("(c) job_stop from another process: no error", err is None and r.get("stopped"))
check("(c) ...recorded as killed with its reason", job("orphan").get("state") == "killed"
      and job("orphan").get("stop_reason") == "test")
check("(c) ...without waiting out the grace period", time.time() - t0 < 4)
starter.kill()

print("no process table (ps missing or failing):")
r = jobs.start("nops", command="sleep 33; true", cwd=str(PROJ), redo_hint="", session_id=SID)
time.sleep(0.5)
pg = jobs.find("nops")["pgid"]
sleeper = subprocess.Popen(["sleep", "30"])
state.touch(state.p("running", "np-live.json"), json.dumps({"session_id": SID, "tool_use_id": "np-live"}))
state.touch(state.p("pruned"), "")
os.utime(state.p("pruned"), (time.time() - 86400, time.time() - 86400))
_real_table = procs.table
procs.table = lambda: {}
try:
    np_state = job("nops").get("state")
    np_claude = procs.is_claude(MAIN_CLAUDE.pid, {}), procs.is_claude(sleeper.pid, {}), procs.is_claude(dead.pid, {})
    np_live = {s_["claude_pid"] for s_ in state.live_sessions({})}
    np_pruned = state.prune(force=True)
    np_group = procs.group_alive(pg, {})
    np_stop = jobs.stop("nops", "test", grace=1)
    np_gone = procs.group_alive(pg, {})
finally:
    procs.table = _real_table
check("a running job isn't marked vanished", np_state == "running")
check("...group_alive trusts the signal: alive, then gone", np_group and not np_gone)
check("...job_stop still stops it, and nothing of its group is left",
      np_stop.get("stopped") and job("nops").get("state") == "killed" and not procs.group_alive(pg))
check("is_claude asks the OS about the one pid instead", np_claude == (True, False, False))
check("live sessions are still found", MAIN_CLAUDE.pid in np_live and CLAUDE_2.pid in np_live)
check("pruning waits for a process table", np_pruned == {} and state.p("running", "np-live.json").exists()
      and time.time() - state.p("pruned").stat().st_mtime > 3600)
state.remove(state.p("running", "np-live.json"))
sleeper.kill()
r = jobs.start("lostwrapper", command="sleep 34; true", cwd=str(PROJ), redo_hint="", session_id=SID)
time.sleep(0.5)
rec = jobs.find("lostwrapper")
os.kill(rec["pid"], signal.SIGKILL)   # only the wrapper: its command goes on in the group
time.sleep(0.3)
check("wrapper killed on its own while its command runs: still 'running'",
      job("lostwrapper").get("state") == "running")
r = jobs.stop("lostwrapper", "test", grace=1)
check("...so job_stop stops what's left of it", r.get("stopped") and not procs.group_alive(rec["pgid"]))

print("MCP server:")
env = dict(os.environ, CLAUDE_CODE_SESSION_ID="mcp-sess")
env.pop("CLAUDE_PID")
msgs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "job_start", "arguments": {
        "name": "mcpjob", "command": "echo from-mcp", "cwd": str(PROJ), "redo_hint": "rerun"}}},
    {"jsonrpc": "2.0", "id": 4, "method": "nope"},
]


def mcp(messages, env):
    p = subprocess.run([sys.executable, str(ROOT / "run.py"), "server"],
                       input="\n".join(json.dumps(m) for m in messages) + "\n",
                       capture_output=True, text=True, env=env, timeout=30)
    return {r["id"]: r for r in map(json.loads, p.stdout.splitlines())}


resp = mcp(msgs, env)
check("initialize", lambda: resp[1]["result"]["serverInfo"]["name"] == "checkpoint")
check("serverInfo version is the package version",
      lambda: resp[1]["result"]["serverInfo"]["version"] == __version__)
check("four job tools, the journal's note tool and the task's policy tool",
      lambda: sorted(t["name"] for t in resp[2]["result"]["tools"])
      == ["checkpoint_note", "job_start", "job_status", "job_stop", "job_tail", "task_policy"])
check("job_start via MCP records the session", lambda: jobs.find("mcpjob")["session_id"] == "mcp-sess"
      and not resp[3]["result"]["isError"])
check("unknown method -> error", lambda: resp[4]["error"]["code"] == -32601)
CLAUDE_M = fake_claude()
state.update_session("mcp-launch", claude_pid=CLAUDE_M.pid, started_ts=time.time() - 30)
state.update_session("mcp-cleared", claude_pid=CLAUDE_M.pid, started_ts=time.time() - 5)
state.update_session("mcp-stale", claude_pid=CLAUDE_M.pid, started_ts=time.time() - 9 * 86400 + 5)
envm = dict(os.environ, CLAUDE_CODE_SESSION_ID="mcp-launch", CLAUDE_PID=str(CLAUDE_M.pid))
mcp([{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "job_start", "arguments": {
    "name": "afterclear", "command": "true", "cwd": str(PROJ), "redo_hint": ""}}}], envm)
check("after /clear (same Claude pid, newer session record): jobs get the current session",
      lambda: jobs.find("afterclear")["session_id"] == "mcp-cleared")
CLAUDE_S = fake_claude()
state.update_session("mcp-stale2", claude_pid=CLAUDE_S.pid, started_ts=time.time() - 2 * 86400)
envs = dict(os.environ, CLAUDE_CODE_SESSION_ID="mcp-other", CLAUDE_PID=str(CLAUDE_S.pid))
mcp([{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "job_start", "arguments": {
    "name": "norecord", "command": "true", "cwd": str(PROJ), "redo_hint": ""}}}], envs)
check("only an old record for this Claude pid (pid reused): falls back to CLAUDE_CODE_SESSION_ID",
      lambda: jobs.find("norecord")["session_id"] == "mcp-other")
srv = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "server"], stdin=subprocess.PIPE,
                       stdout=subprocess.PIPE, text=True, env=env)
srv.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
    "name": "job_start", "arguments": {"name": "quickexit", "command": "true", "cwd": str(PROJ),
                                       "redo_hint": ""}}}) + "\n")
srv.stdin.flush()
srv.stdout.readline()
time.sleep(1)
zombies = [r for r in procs.table().values() if r["ppid"] == srv.pid and procs.zombie(r["pid"])]
check("(c) the server reaps its finished jobs (no zombie while it runs)", not zombies)
srv.kill()

# --- the gateway's environment --------------------------------------------------------
print("jobs run in the gateway's environment (its env.sh) when it is installed:")
GWD = TMP / "gateway"   # the folder CHECKPOINT_GATEWAY_ENV points into (absent until written)
GW_ENV = GWD / "env.sh"
GW_DEFAULT = os.environ["CHECKPOINT_GATEWAY_ENV"]
REAL_GW = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".config", "gateway", "env.sh")
check("hermetic: CHECKPOINT_GATEWAY_ENV is a temp path, never the real ~/.config/gateway",
      GW_DEFAULT == str(GW_ENV) and state.gateway_env_path() == str(GW_ENV) and REAL_GW != GW_DEFAULT)
_home = os.environ.get("HOME")
os.environ.pop("CHECKPOINT_GATEWAY_ENV")
os.environ["HOME"] = str(TMP / "not-home")
try:
    gw_default = state.gateway_env_path()   # computes the path only; nothing looks at the file
finally:
    os.environ["CHECKPOINT_GATEWAY_ENV"] = GW_DEFAULT
    if _home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = _home
check("default: <password database home>/.config/gateway/env.sh, not $HOME (like the gateway)",
      gw_default == REAL_GW)
GW_BIN = TMP / "gw-bin"
GW_BIN.mkdir()
for _n, _body in (("ssh", "echo stand-in ssh"), ("gw-hello", "echo hello from a gateway stand-in")):
    (GW_BIN / _n).write_text(f"#!/bin/sh\n{_body}\n")
    (GW_BIN / _n).chmod(0o755)
# Shaped like the gateway's own env.sh: its stand-ins first on PATH, the user's
# agent and tokens unset, nothing at all once the stand-in folder is gone.
GW_TEXT = f"""# fake gateway env.sh
GW_BIN='{GW_BIN}'
if [ -d "$GW_BIN" ]; then
  case ":${{PATH-}}:" in *":$GW_BIN:"*) ;; *) PATH="$GW_BIN${{PATH:+:$PATH}}"; export PATH;; esac
  CHECKPOINT_TEST_GW_MARKER=from-gateway; export CHECKPOINT_TEST_GW_MARKER
  unset SSH_AUTH_SOCK GITHUB_TOKEN
fi
"""
CREDS = {"SSH_AUTH_SOCK": str(TMP / "agent.sock"), "GITHUB_TOKEN": "user-token"}
GW_SID = "gw-sess"


def gw_rm(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif os.path.lexists(path):
        path.unlink()


def gw_write(text=GW_TEXT, mode=0o600, folder_mode=0o700):
    GWD.mkdir(exist_ok=True)
    GWD.chmod(folder_mode)
    gw_rm(GW_ENV)
    GW_ENV.write_text(text)
    GW_ENV.chmod(mode)


def gw_start(name, env=None, **kw):
    """Start a job as the MCP server would, with the user's agent and a token
    in its environment (plus `env`). Returns the name."""
    extra = dict(CREDS, **(env or {}))
    saved_ = {k: os.environ.get(k) for k in extra}
    os.environ.update(extra)
    try:
        jobs.start(name, cwd=str(PROJ), redo_hint="", session_id=GW_SID, **kw)
    finally:
        for k, v in saved_.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return name


def gw_rec(name):
    """The job's registry record once it has ended."""
    wait_for(lambda: job(name).get("state") not in ("running", None), 10)
    return jobs._registry()["jobs"].get((jobs.find(name) or {}).get("id"), {})


def gw_log(rec):
    return read_or_empty(Path(rec.get("log") or TMP / "none"))


def env_of(rec):
    return dict(ln.split("=", 1) for ln in gw_log(rec).splitlines() if "=" in ln)


def gw_without(rec, why):
    """Refused (with why) and the job ran without the env file."""
    e = env_of(rec)
    return (rec.get("gateway_env") == f"refused: {why}" and rec.get("gateway_env_file") == str(GW_ENV)
            and rec.get("exit_code") == 0 and "CHECKPOINT_TEST_GW_MARKER" not in e
            and e.get("SSH_AUTH_SOCK") == CREDS["SSH_AUTH_SOCK"])


gw_write()
check("a safe env file is found", jobs.gateway_env() == (str(GW_ENV), ""))
rec = gw_rec(gw_start("gwjob", env=ACT, command='env; echo "ssh is $(command -v ssh)"; exit 7'))
jenv = env_of(rec)
check("(c) the job sees what the env file exports", jenv.get("CHECKPOINT_TEST_GW_MARKER") == "from-gateway")
check("(c) ...its stand-in folder first on PATH (and checkpoint's own pixi bin dir still gone)",
      jenv.get("PATH", "").split(os.pathsep) == [str(GW_BIN), "/usr/bin", "/bin"])
check("(c) ...and neither the user's agent nor the token it unsets",
      not {"SSH_AUTH_SOCK", "GITHUB_TOKEN"} & set(jenv))
check("...so commands find the stand-ins (ssh is the gateway's)", f"ssh is {GW_BIN}/ssh" in gw_log(rec))
check("...checkpoint's pixi activation is removed before it is sourced",
      not {"PIXI_PROJECT_MANIFEST", "PIXI_PROJECT_ROOT", "CONDA_PREFIX"} & set(jenv))
check("...the exit code is still captured", rec.get("state") == "exited" and rec.get("exit_code") == 7)
check("(c) registry: gateway_env 'applied', and which file",
      rec.get("gateway_env") == "applied" and rec.get("gateway_env_file") == str(GW_ENV))
check("...job_status shows it", job("gwjob").get("gateway_env") == "applied"
      and job("gwjob").get("gateway_env_file") == str(GW_ENV))
check("...and checkpoint status", any("gwjob" in ln and ln.endswith("gateway env applied")
                                      for ln in core.status_text().splitlines()))
check("...nothing about it in the job's log", "checkpoint:" not in gw_log(rec))
rec = gw_rec(gw_start("gwargv", argv=["gw-hello"]))
check("(c) argv jobs too: a stand-in found on the gateway's PATH",
      rec.get("exit_code") == 0 and "hello from a gateway stand-in" in gw_log(rec)
      and rec.get("gateway_env") == "applied")
gw_start("gwlong", argv=["sleep", "30"])
time.sleep(0.5)
rec = jobs.find("gwlong") or {}
check("a running job: its wrapper is still marked checkpoint-job-<id>, so it counts as running",
      lambda: rec["state"] == "running" and f"checkpoint-job-{rec['id']}" in procs.table()[rec["pid"]]["cmd"])
check("...gateway_env 'applied' while it runs", rec.get("gateway_env") == "applied")
r = jobs.stop("gwlong", "test", grace=1)
check("...job_stop stops it", r.get("stopped") and job("gwlong").get("state") == "killed")

gw_write(mode=0o620)
check("(c) a group-writable env file: refused, the job runs without it",
      gw_without(gw_rec(gw_start("gwgrp", command="env")), "group- or other-writable"))
gw_write(mode=0o602)
check("(c) an other-writable one: refused",
      gw_without(gw_rec(gw_start("gwoth", command="env")), "group- or other-writable"))
gw_write(folder_mode=0o770)
check("(c) one in a group-writable folder: refused",
      gw_without(gw_rec(gw_start("gwdirg", command="env")), "its folder is group- or other-writable"))
gw_write(folder_mode=0o707)
check("(c) one in an other-writable folder: refused",
      gw_without(gw_rec(gw_start("gwdiro", command="env")), "its folder is group- or other-writable"))
GW_REAL = TMP / "gw-real"
GW_REAL.mkdir(mode=0o700)
(GW_REAL / "env.sh").write_text(GW_TEXT)
(GW_REAL / "env.sh").chmod(0o600)
gw_write()
gw_rm(GW_ENV)
GW_ENV.symlink_to(GW_REAL / "env.sh")
check("(c) a symlink, even to a safe file: refused",
      gw_without(gw_rec(gw_start("gwlink", command="env")), "a symlink"))
gw_rm(GW_ENV)
os.mkfifo(GW_ENV, 0o600)
check("a FIFO (sourcing it would hang): refused as not a regular file",
      gw_without(gw_rec(gw_start("gwfifo", command="env")), "not a regular file"))
gw_write()
_uid = os.getuid
os.getuid = lambda: _uid() + 1   # as if the file and its folder were another user's
try:
    gw_notmine = jobs.gateway_env()
    gw_start("gwnotmine", command="env")
finally:
    os.getuid = _uid
check("(c) an env file someone else owns: refused, the job runs without it",
      gw_notmine == (str(GW_ENV), "not owned by you")
      and gw_without(gw_rec("gwnotmine"), "not owned by you"))
_stat = os.stat


def _folder_not_mine(path, *a, **kw):
    st_ = _stat(path, *a, **kw)
    if os.fspath(path) != str(GWD):
        return st_
    return os.stat_result((st_.st_mode, st_.st_ino, st_.st_dev, st_.st_nlink, st_.st_uid + 1,
                           st_.st_gid, st_.st_size, int(st_.st_atime), int(st_.st_mtime),
                           int(st_.st_ctime)))


os.stat = _folder_not_mine
try:
    gw_dir_notmine = jobs.gateway_env()
finally:
    os.stat = _stat
check("(c) your file in a folder someone else owns: refused",
      gw_dir_notmine == (str(GW_ENV), "its folder is not owned by you"))

gw_rm(GW_ENV)
rec = gw_rec(gw_start("gwnone", command="env"))
jenv = env_of(rec)
check("(c) no env file (gateway not installed): the job runs as before",
      rec.get("exit_code") == 0 and "CHECKPOINT_TEST_GW_MARKER" not in jenv
      and jenv.get("SSH_AUTH_SOCK") == CREDS["SSH_AUTH_SOCK"] and jenv.get("GITHUB_TOKEN") == "user-token"
      and jenv.get("PATH") == jobs.job_env().get("PATH"))
check("...with no gateway_env in its record, job_status or checkpoint status",
      not {"gateway_env", "gateway_env_file"} & set(rec) and "gateway_env" not in job("gwnone")
      and not Path(rec["exit_file"]).with_name("gateway_env").exists()
      and not any("gwnone" in ln and "gateway" in ln for ln in core.status_text().splitlines()))
gw_absent = []
for _p in (TMP / "nowhere" / "env.sh", GW_BIN / "ssh" / "env.sh"):
    os.environ["CHECKPOINT_GATEWAY_ENV"] = str(_p)
    try:
        gw_absent.append(jobs.gateway_env())
    finally:
        os.environ["CHECKPOINT_GATEWAY_ENV"] = GW_DEFAULT
check("no gateway folder, or a file where its folder would be: no env file",
      gw_absent == [("", "")] * 2)
gw_write()
os.environ["CHECKPOINT_GATEWAY_ENV"] = ""
try:
    gw_off = jobs.gateway_env()
    gw_start("gwoff", command="env")
finally:
    os.environ["CHECKPOINT_GATEWAY_ENV"] = GW_DEFAULT
rec = gw_rec("gwoff")
check("CHECKPOINT_GATEWAY_ENV set but empty: never applied, even with a safe file there",
      gw_off == ("", "") and "gateway_env" not in rec and "CHECKPOINT_TEST_GW_MARKER" not in env_of(rec))

gw_write(GW_TEXT + "if then fi\n")   # bash runs the lines before a syntax error; dash none
rec = gw_rec(gw_start("gwbroken", command="echo still-ran; env; exit 4"))
check("(c) an env file with a syntax error: the job still runs, its exit code captured",
      rec.get("exit_code") == 4 and "still-ran" in gw_log(rec))
check("(c) ...the log says it ran without the gateway environment",
      f"checkpoint: could not apply the gateway environment ({GW_ENV}); the job runs without it."
      in gw_log(rec))
check("(c) ...gateway_env 'failed'", rec.get("gateway_env") == "failed")
check("(c) ...and none of the file applied (not half of it, whatever /bin/sh is)",
      "CHECKPOINT_TEST_GW_MARKER" not in env_of(rec) and env_of(rec).get("SSH_AUTH_SOCK") == CREDS["SSH_AUTH_SOCK"])
check("...status says so", any("gwbroken" in ln and ln.endswith("gateway env failed")
                               for ln in core.status_text().splitlines()))
gw_write("exit 3\n")
rec = gw_rec(gw_start("gwexit", command="echo still-ran"))
check("an env file that exits: the job still runs, 'failed'",
      rec.get("exit_code") == 0 and "still-ran" in gw_log(rec) and rec.get("gateway_env") == "failed")
r_unknown = {"gateway_env": "pending", "exit_file": str(TMP / "no-such-job" / "exit_code"), "state": "exited"}
r_pending = dict(r_unknown, state="running")
jobs._gateway_refresh(r_unknown)
jobs._gateway_refresh(r_pending)
check("ended before its shell reported: 'unknown'; still starting: 'pending'",
      r_unknown["gateway_env"] == "unknown" and r_pending["gateway_env"] == "pending")

# The wrapper itself under each shell /bin/sh may be (bash on macOS, dash on Debian).
WD = TMP / "gw-wrap"
WD.mkdir(mode=0o700)
WRAP_SHELLS = []
for _s in ("/bin/sh", shutil.which(SH) or SH, "/bin/dash"):
    if os.path.exists(_s) and _s not in WRAP_SHELLS:
        WRAP_SHELLS.append(_s)
WRAP_CASES = (("that sources cleanly", GW_TEXT, "applied"),
              ("with a syntax error at its end", GW_TEXT + "if then fi\n", "failed"),
              ("with a failing special builtin", GW_TEXT + "export 1abc=x\n", "failed"),
              ("that exits", "exit 3\n", "failed"),
              ("gone by the time the job starts", None, "failed"),
              ("none", "", ""))
for _s in WRAP_SHELLS:
    for _label, _text, _want in WRAP_CASES:
        wf, wx, ws = WD / "env.sh", WD / "exit_code", WD / "gateway_env"
        for _p in (wf, wx, ws):
            gw_rm(_p)
        if _text:
            wf.write_text(_text)
        _r = subprocess.run([_s, "-c", jobs.WRAPPER, "checkpoint-job-t",
                             'echo "ran marker=${CHECKPOINT_TEST_GW_MARKER-none}"; exit 5', str(wx),
                             str(wf) if _text != "" else "", str(ws) if _text != "" else ""],
                            capture_output=True, text=True, timeout=30)
        check(f"wrapper under {_s}, env file {_label}: the job runs, exit code kept"
              + (f", '{_want}'" if _want else ""),
              _r.returncode == 5 and read_or_empty(wx).strip() == "5"
              and read_or_empty(ws).strip() == _want
              and f"ran marker={'from-gateway' if _want == 'applied' else 'none'}" in _r.stdout
              and ("could not apply the gateway environment" in _r.stderr) == (_want == "failed"))

gw_write()
env_gw = dict(os.environ, CLAUDE_CODE_SESSION_ID=GW_SID)
env_gw.pop("CLAUDE_PID")
mcp([{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "job_start", "arguments": {
    "name": "gwmcp", "command": "echo marker=${CHECKPOINT_TEST_GW_MARKER-none}", "cwd": str(PROJ),
    "redo_hint": ""}}}], env_gw)
rec = gw_rec("gwmcp")
resp_gw = mcp([{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
    "name": "job_status", "arguments": {"name": "gwmcp"}}}], env_gw)
check("(c) job_start through the MCP server: applied, and job_status says so",
      lambda: "marker=from-gateway" in gw_log(rec) and json.loads(
          resp_gw[1]["result"]["content"][0]["text"])["job"]["gateway_env"] == "applied")
gw_rm(GWD)   # later tests run with no env file again
check("hermetic: every job's env file was a temp one (the real ~/.config/gateway never used)",
      all(not j.get("gateway_env_file") or j["gateway_env_file"].startswith(str(TMP))
          for j in jobs._registry()["jobs"].values()))

# --- processes ------------------------------------------------------------------------
print("processes:")
fc = fake_claude()
hk = subprocess.Popen(["/bin/sh", "-c", "sleep 30"])
table = procs.table()
check("find_claude walks past shells to 'claude'", procs.find_claude(fc.pid, table) == fc.pid)
check("find_claude: none above a plain shell", procs.find_claude(hk.pid, table) in (None,) or
      "claude" in table.get(procs.find_claude(hk.pid, table) or 0, {}).get("cmd", "").lower())
# macOS ps shows a process that has just started as `?` or `U` for a moment: read until it settles.
wait_for(lambda: (table.update(procs.table()) or table.get(hk.pid, {}).get("stat", "?")[:1] in "RS"), 5)
check("table has process states and start times", lambda: table[hk.pid]["stat"][:1] in "RS"
      and abs(table[hk.pid]["start"] - time.time()) < 30)
hk.kill()
BADPS = TMP / "badps"
GARBLED = TMP / "garbled"
for d, body in ((BADPS, 'echo "ps: broken" >&2\nexit 1'), (GARBLED, 'echo "usage: ps [nothing useful]"')):
    d.mkdir()
    (d / "ps").write_text(f"#!/bin/sh\n{body}\n")
    (d / "ps").chmod(0o755)
REAL_PS = shutil.which("ps")
RC1PS = TMP / "rc1ps"   # prints real rows, then exits 1
RC1PS.mkdir()
(RC1PS / "ps").write_text(f'#!/bin/sh\n"{REAL_PS or "/bin/ps"}" "$@"\nexit 1\n')
(RC1PS / "ps").chmod(0o755)
zp = subprocess.Popen(["true"])
time.sleep(0.5)   # exited and not reaped yet: a zombie
_path = os.environ["PATH"]
os.environ["PATH"] = f"{BADPS}{os.pathsep}{_path}"
try:
    bp_table = procs._ps_table()
    bp_zombie = procs.zombie(zp.pid)
finally:
    os.environ["PATH"] = _path
check("a failing ps earlier on PATH: the process table is still read", MAIN_CLAUDE.pid in bp_table)
check("...and a zombie still recognized", bp_zombie)
if REAL_PS:
    _cands = procs._ps_candidates
    try:
        procs._ps_candidates = lambda: [str(BADPS / "ps"), str(GARBLED / "ps"), REAL_PS]
        bp_next = procs._ps_table()
        bp_next_z = procs.zombie(zp.pid), procs.zombie(MAIN_CLAUDE.pid)
        procs._ps_candidates = lambda: [str(RC1PS / "ps")]
        rc1_z = procs.zombie(zp.pid), procs.zombie(MAIN_CLAUDE.pid)
    finally:
        procs._ps_candidates = _cands
    check("a ps that fails or prints no rows: the next one is tried", MAIN_CLAUDE.pid in bp_next)
    check("...zombie() too: garbled output from a ps that exits 0 isn't taken for a state",
          bp_next_z == (True, False))
    check("...and a ps that prints a real state but exits 1 is believed", rc1_z == (True, False))
zp.wait()
FP = TMP / "fakeproc"
FP.mkdir()
(FP / "uptime").write_text("1000.50 2000.00\n")
(FP / "self").mkdir()   # non-numeric entries are skipped
HZ = os.sysconf("SC_CLK_TCK")


def fakeproc(pid, comm, st_, ppid, pgid, ticks, argv):
    d = FP / str(pid)
    d.mkdir()
    fields = [st_, str(ppid), str(pgid)] + ["0"] * 16 + [str(ticks)] + ["0"] * 10
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n")
    (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in argv))


fakeproc(10, "bash", "S", 1, 10, 90000, ["/bin/bash", "-c", "eval 'echo hi\nthere'"])
fakeproc(11, "we (ird) name)", "R", 10, 10, 95000, ["python3", "x y"])
fakeproc(12, "sh", "Z", 10, 10, 96000, [])
pt = procs.proc_table(str(FP))
check("/proc table (Linux, no ps needed): every process, nothing else", sorted(pt) == [10, 11, 12])
check("...ppid, pgid, state and args (a comm with spaces and parentheses)",
      lambda: (pt[11]["ppid"], pt[11]["pgid"], pt[11]["stat"], pt[11]["cmd"]) == (10, 10, "R", "python3 x y"))
check("...a zombie (no cmdline) shows as [comm]", lambda: (pt[12]["stat"], pt[12]["cmd"]) == ("Z", "[sh]"))
check("...start time from the uptime clock", lambda: abs(pt[10]["start"] - (time.time() - (1000.5 - 90000 / HZ))) < 5)
check("...newlines in args kept (the eval argument parses)",
      lambda: procs.eval_command(pt[10]["cmd"].split(" -c ", 1)[1]) == "echo hi\nthere")
SIDH = "sess-helpers"
check("claim_queue: a queued command is handled once",
      state.claim_queue(SIDH, ["2026-10-16T09:00:00.000Z"]) == ["2026-10-16T09:00:00.000Z"]
      and state.claim_queue(SIDH, ["2026-10-16T09:00:00.000Z"]) == []
      and state.claim_queue(SIDH, ["2026-10-16T08:59:59.000Z"]) == [])
check("...several at once, each by its own timestamp: only those not handled yet",
      state.claim_queue(SIDH, ["2026-10-16T09:00:00.000Z", "2026-10-16T09:00:01.000Z",
                               "2026-10-16T09:00:02.000Z"])
      == ["2026-10-16T09:00:01.000Z", "2026-10-16T09:00:02.000Z"]
      and state.claim_queue(SIDH, ["2026-10-16T09:00:01.000Z", "2026-10-16T09:00:02.000Z"]) == [])
state.add_interrupted(SIDH, [{"tool_use_id": "x1", "command": "make"}])
state.add_interrupted(SIDH, [{"tool_use_id": "x1", "command": "make"}, {"tool_use_id": "x2", "command": "ls"}])
check("add_interrupted: a command killed twice (gate and watcher) is recorded once",
      [c["tool_use_id"] for c in state.load_session(SIDH)["interrupted"]] == ["x1", "x2"])
state.add_unstopped(SIDH, [{"tool_use_id": "x1", "command": "make"}, {"tool_use_id": "x3", "command": "cat"}])
state.add_unstopped(SIDH, [{"tool_use_id": "x3", "command": "cat"}])
check("add_unstopped: once per call, and never one already killed",
      [c["tool_use_id"] for c in state.load_session(SIDH)["unstopped"]] == ["x3"])
state.add_interrupted(SIDH, [{"tool_use_id": "x3", "command": "cat"}])
check("...a command killed later (ps works again) is no longer one it couldn't stop",
      state.load_session(SIDH)["unstopped"] == [])

print("the Bash tool's real command format (Claude Code 2.1.29x):")
# Observed with `ps -ww -o command= -p $$` from a Bash tool call on macOS:
# ' becomes '"'"' inside eval '...', and ps shows a newline as \012.
PS_SEEN = ("/opt/local/bin/bash -c source /Users/u/.config/claude/shell-snapshots/snapshot-bash-1-x.sh "
           "2>/dev/null || true && shopt -u extglob 2>/dev/null || true && { \\builtin unalias -- "
           "'unsetenv'; \\builtin unset -f -- 'unsetenv'; } >/dev/null 2>&1 || true && eval "
           "'echo '\"'\"'single quote marker'\"'\"'\\012ps -ww -o command= -p $PPID' < /dev/null "
           "&& pwd -P >| /tmp/claude-fc47-cwd")
check("ps line decoded: the eval argument is the exact command",
      procs.eval_command(procs.ps_decode(PS_SEEN).split(" -c ", 1)[1])
      == "echo 'single quote marker'\nps -ww -o command= -p $PPID")
check("older '\\'' quoting decodes too", procs.eval_command("true && eval 'it'\\''s here' < /dev/null") == "it's here")
check("an eval inside the command isn't taken for the wrapper's",
      procs.eval_command("true && eval 'echo eval '\"'\"'x'\"'\"'' < /dev/null") == "echo eval 'x'")

PREFIX = ("source /nonexistent/snapshot-bash.sh 2>/dev/null || true && "
          "{ \\builtin unalias -- 'unsetenv'; \\builtin unset -f -- 'unsetenv'; } >/dev/null 2>&1 || true && ")
SHELL = shutil.which("bash") or "/bin/sh"


def claude_wrap(command):
    """How Claude Code 2.1.29x runs a Bash tool command."""
    return PREFIX + "eval '" + command.replace("'", "'\"'\"'") + "' < /dev/null && pwd -P >| " + str(TMP / "cwd-out")


TREE = FAKE_BIN / "claude-tree.py"
TREE.write_text(f"""#!{sys.executable}
# A fake Claude process: runs each argument as a Bash tool call ("@N": wait
# N seconds first), then waits.
import subprocess, sys, time
for script in sys.argv[1:]:
    if script.startswith("@"):
        time.sleep(float(script[1:]))
        continue
    subprocess.Popen([{SHELL!r}, '-c', script], start_new_session=True)
    time.sleep(0.2)
time.sleep(60)
""")
TREE.chmod(0o755)
CMDS = {
    "single quotes": "sleep 41; echo 'done'",
    "multi-line": "echo start\nsleep 42\necho \"end\" 'x'",
    "backslash and double quotes": "printf '%s\\n' \"a\\\\b\"; sleep 43",
}
OTHER = "sleep 44; echo 'a background command'"
since = time.time()
tree = subprocess.Popen([str(TREE)] + [claude_wrap(c) for c in CMDS.values()] + [claude_wrap(OTHER)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
CHILDREN.append(tree)
def tree_sleeps(root):
    """pid -> command of the sleeps running under a fake Claude (its commands)."""
    t = procs.table()
    return {p: t[p]["cmd"] for p in procs.descendants(root, t) if t[p]["cmd"].startswith("sleep ")}


wait_for(lambda: len(tree_sleeps(tree.pid)) >= 4, 10)
sleeps = tree_sleeps(tree.pid)
table = procs.table()
for label, c in CMDS.items():
    found = procs.foreground_shells(tree.pid, c, table)
    check(f"foreground shell found by its exact command ({label})",
          len(found) == 1 and procs.tool_command(found[0], table[found[0]]) == c)
check("a different command isn't matched", procs.foreground_shells(tree.pid, "sleep 99", table) == [])
check("fallback: a command Claude Code reworded is found by start time",
      len(procs.foreground_shells(tree.pid, "reworded", table, since=since)) == 4)
check("...but not shells started before the call", procs.foreground_shells(
    tree.pid, "reworded", table, since=time.time() + 3) == [])
check("...nor a shell running one of the session's other known calls (e.g. a background one)",
      len(procs.foreground_shells(tree.pid, "reworded", table, since=since, others={OTHER})) == 3)
check("an exact match that started before the call isn't taken (a background call, same text)",
      procs.foreground_shells(tree.pid, CMDS["single quotes"], table, since=time.time() + 3) == [])
SIDK = "sess-kill"
state.update_session(SIDK, claude_pid=tree.pid, cwd=str(PROJ))
for i, c in enumerate(CMDS.values()):
    state.touch(state.p("running", f"tuK{i}.json"), json.dumps(
        {"session_id": SIDK, "tool_use_id": f"tuK{i}", "tool_input": {"command": c}}))
    # written at the call's PreToolUse, before its shell started
    os.utime(state.p("running", f"tuK{i}.json"), (since - 1, since - 1))
killed = core.kill_foreground(SIDK)
time.sleep(0.3)
table = procs.table()
check("kill_foreground kills each command tree (quotes, newlines, backslashes)",
      len(killed) == 3 and sorted(c for p, c in sleeps.items() if procs.alive_in(p, table))
      == ["sleep 44"])
check("...but not the session's other (background) command", "sleep 44" in tree_sleeps(tree.pid).values())
check("...nor Claude itself", procs.alive(tree.pid))
check("interrupted commands recorded for the checkpoint",
      sorted(c["command"] for c in state.load_session(SIDK).get("interrupted") or []) == sorted(CMDS.values()))
for f in state.p("running").glob("tuK*.json"):
    f.unlink()
for sh_ in procs.foreground_shells(tree.pid, OTHER):
    procs.kill_tree(sh_, grace=1, protect={tree.pid})
tree.kill()

print("pause now leaves a background shell running the same command alone:")
SAME = "sleep 45; echo 'same text'"
t_start = time.time()
tree3 = subprocess.Popen([str(TREE), claude_wrap(SAME), "@4", claude_wrap(SAME)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
CHILDREN.append(tree3)
wait_for(lambda: len(tree_sleeps(tree3.pid)) >= 2, 15)
table = procs.table()
bg, fg = sorted(tree_sleeps(tree3.pid), key=lambda q: table[q]["start"])
SIDBG = "sess-bgsame"
state.update_session(SIDBG, claude_pid=tree3.pid, cwd=str(PROJ))
state.touch(state.p("running", "tuBG.json"), json.dumps(
    {"session_id": SIDBG, "tool_use_id": "tuBG", "tool_input": {"command": SAME}}))
os.utime(state.p("running", "tuBG.json"), (t_start + 3, t_start + 3))   # between the two shells
killed = core.kill_foreground(SIDBG)
time.sleep(0.3)
table = procs.table()
check("the foreground call's shell (started after its PreToolUse) is killed",
      len(killed) == 1 and not procs.alive_in(fg, table))
check("...the older background shell with the same command keeps running", procs.alive_in(bg, table))
state.remove(state.p("running", "tuBG.json"))
tree3.kill()
TRW = TMP / "trw.jsonl"
BGCMD = "sleep 46; echo 'started in the background'"
tline({"type": "assistant", "timestamp": "2026-10-16T06:30:00.000Z", "message": {"content": [
    {"type": "tool_use", "id": "tbg", "name": "Bash",
     "input": {"command": BGCMD, "run_in_background": True}}]}}, TRW)
t_start = time.time()
tree4 = subprocess.Popen([str(TREE), claude_wrap(BGCMD), claude_wrap("sleep 47")],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
CHILDREN.append(tree4)
wait_for(lambda: len(tree_sleeps(tree4.pid)) >= 2, 10)
SIDRW = "sess-reworded"
state.update_session(SIDRW, claude_pid=tree4.pid, cwd=str(PROJ), transcript_path=str(TRW))
state.touch(state.p("running", "tuRW.json"), json.dumps(
    {"session_id": SIDRW, "tool_use_id": "tuRW", "tool_input": {"command": "sleep 47 # as the model wrote it"}}))
os.utime(state.p("running", "tuRW.json"), (t_start - 1, t_start - 1))
killed = core.kill_foreground(SIDRW)
time.sleep(0.3)
left = sorted(tree_sleeps(tree4.pid).values())
check("command reworded by Claude Code: the fallback kills the shell started since the call...",
      len(killed) == 1 and "sleep 47" not in left)
check("...but not a background call from the transcript started meanwhile", "sleep 46" in left)
state.remove(state.p("running", "tuRW.json"))
tree4.kill()

print("watcher (pause now while a command runs):")
CMD2 = "sleep 32; echo 'it'\"'\"'s done'"
t_call = time.time()   # the call's PreToolUse comes before its shell starts
tree2 = subprocess.Popen([str(TREE), claude_wrap(CMD2)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
CHILDREN.append(tree2)
wait_for(lambda: bool(tree_sleeps(tree2.pid)), 10)
SIDW = "sess-watch"
TW = TMP / "tw.jsonl"
TW.write_text("")
state.update_session(SIDW, claude_pid=tree2.pid, cwd=str(PROJ), transcript_path=str(TW))
payload = {"session_id": SIDW, "transcript_path": str(TW), "cwd": str(PROJ), "tool_name": "Bash",
           "tool_use_id": "tuW", "tool_input": {"command": CMD2}}
hook("PreToolUse", payload)
check("watcher's running file written", state.p("running", "tuW.json").exists())
os.utime(state.p("running", "tuW.json"), (t_call, t_call))
time.sleep(0.5)
with open(TW, "a") as f:
    f.write(json.dumps({"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-10-16T03:00:00.000Z",
                        "sessionId": SIDW, "content": "/check:pause now"}, separators=(",", ":")) + "\n")
wait_for(lambda: not procs.foreground_shells(tree2.pid, CMD2), 10)
check("queued 'pause now' kills the running command", not procs.foreground_shells(tree2.pid, CMD2))
check("...and pauses the session", state.is_paused(SIDW))
# The watcher records the kill a moment after the shell is gone (kill_tree
# waits for the whole tree): the echo below must come after that, as it does
# in Claude Code (the queued command runs once the killed call has ended).
wait_for(lambda: bool(state.load_session(SIDW).get("interrupted")), 10)
hook("PostToolUseFailure", payload)
check("PostToolUseFailure removes the running file", not state.p("running", "tuW.json").exists())
out = hook("UserPromptSubmit", {"session_id": SIDW, "cwd": str(PROJ), "prompt": "/check:pause now"})
check("(a) queued 'pause now' echo names the killed command", "Stopped then: `sleep 32" in ctx(out)
      and "No command was running" not in ctx(out))
state.update_session(SIDW, checkpoint=str(core.write_checkpoint(SIDW)))
out = hook("UserPromptSubmit", {"session_id": SIDW, "cwd": str(PROJ), "prompt": "back"})
check("(b) resume names the stopped command: partial effects possible, check before running it again (never "
      "'redo the stage from the start')",
      "`pause now` stopped `sleep 32" in ctx(out) and "Check what it had done" in ctx(out)
      and "from the start" not in ctx(out))
tree2.kill()

print("a queued 'pause all now' or 'pause <session> now' stops the other sessions' commands:")


def two_sessions(tag, cmd_a, cmd_b):
    """Sessions A and B, each with a fake Claude; A runs cmd_a (if any) and
    B runs cmd_b as guarded foreground Bash calls (running file + watcher)."""
    t0 = time.time()
    trees, out = {}, {}
    for side, cmd in (("a", cmd_a), ("b", cmd_b)):
        tr = subprocess.Popen([str(TREE)] + ([claude_wrap(cmd)] if cmd else []),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        CHILDREN.append(tr)
        trees[side] = tr
    for side, cmd in (("a", cmd_a), ("b", cmd_b)):
        if cmd:
            wait_for(lambda: bool(tree_sleeps(trees[side].pid)), 10)
        sid, tp_ = f"sess-{tag}-{side}", TMP / f"t-{tag}-{side}.jsonl"
        tp_.write_text("")
        env_ = {"CLAUDE_PID": str(trees[side].pid)}
        pay = {"session_id": sid, "transcript_path": str(tp_), "cwd": str(PROJ)}
        hook("SessionStart", dict(pay, source="startup"), env=env_)
        if cmd:
            hook("PreToolUse", dict(pay, tool_name="Bash", tool_use_id=f"tu-{tag}-{side}",
                                    tool_input={"command": cmd}), env=env_)
            os.utime(state.p("running", f"tu-{tag}-{side}.json"), (t0, t0))
        out[side] = (sid, tp_, env_, pay, trees[side], cmd)
    return out["a"], out["b"]


A, B = two_sessions("all", None, "sleep 62; echo \"b's\"")
sida, tpa, enva, pa, treea, _ = A
sidb, tpb, envb, pb_, treeb, cmdb = B
enqueue("/check:pause all now", "2026-10-16T07:00:00.000Z", tpa)
out = hook("PreToolUse", dict(pa, tool_name="Bash", tool_use_id="ra1", tool_input={"command": "ls"}), env=enva)
check("queued 'pause all now': the typing session's next tool call is denied", denied(out))
check("...another session's running command is stopped too",
      wait_for(lambda: not procs.foreground_shells(treeb.pid, cmdb), 10))
check("...that session is paused, and its command recorded once for its checkpoint",
      state.is_paused(sidb) and [c["tool_use_id"] for c in state.load_session(sidb).get("interrupted") or []]
      == ["tu-all-b"])
hook("PostToolUseFailure", dict(pb_, tool_name="Bash", tool_use_id="tu-all-b", tool_input={"command": cmdb}), env=envb)
dequeue("2026-10-16T07:00:30.000Z", tpa)
out = hook("UserPromptSubmit", dict(pa, prompt="/check:pause all now"), env=enva)
check("...and its echo at the end of the turn says it was already done", "Already done" in ctx(out))
treea.kill()
treeb.kill()
resume_everyone()

A, B = two_sessions("one", "sleep 63; echo 'a'", "sleep 64; echo 'b'")
sida, tpa, enva, pa, treea, cmda = A
sidb, tpb, envb, pb_, treeb, cmdb = B
check("both calls guarded by a watcher", wait_for(lambda: bool(watchers("tu-one-a") and watchers("tu-one-b")), 5))
enqueue(f"/check:pause {sidb} now", "2026-10-16T07:10:00.000Z", tpa)
check("queued 'pause <other> now' while this session's command runs: the other's command is stopped",
      wait_for(lambda: not procs.foreground_shells(treeb.pid, cmdb), 10))
check("...and the other session is paused", state.is_paused(sidb))
time.sleep(0.6)
check("...this session isn't paused, and its own command keeps running",
      not state.is_paused(sida) and procs.foreground_shells(treea.pid, cmda))
check("...still guarded by its watcher", bool(watchers("tu-one-a")))
for side in ("a", "b"):
    state.remove(state.p("running", f"tu-one-{side}.json"))
for sh_ in procs.foreground_shells(treea.pid, cmda):
    procs.kill_tree(sh_, grace=1, protect={treea.pid})
treea.kill()
treeb.kill()
resume_everyone()

print("several pause commands queued while a command runs: the watcher applies them all:")
A, B = two_sessions("many", "sleep 65; echo 'a'", "sleep 66; echo 'b'")
sida, tpa, enva, pa, treea, cmda = A
sidb, tpb, envb, pb_, treeb, cmdb = B
check("both calls guarded by a watcher", wait_for(lambda: bool(watchers("tu-many-a") and watchers("tu-many-b")), 5))
enqueue_all([(f"/check:pause {sidb} now", "2026-10-16T07:20:00.000Z"),
             ("pause", "2026-10-16T07:20:01.000Z")], tpa)
check("'pause <other> now' then 'pause': the older one (with now) stops the other's command",
      wait_for(lambda: not procs.foreground_shells(treeb.pid, cmdb), 10) and state.is_paused(sidb))
check("...the newer one is applied too: this session is paused", wait_for(lambda: state.is_paused(sida), 5))
time.sleep(0.6)
check("...as typed (it has no 'now'): this session's own command keeps running",
      bool(procs.foreground_shells(treea.pid, cmda)))
check("...both remembered for their echoes",
      len(state.queued_pauses(state.load_session(sida))) == 2)
for side in ("a", "b"):
    state.remove(state.p("running", f"tu-many-{side}.json"))
for sh_ in procs.foreground_shells(treea.pid, cmda):
    procs.kill_tree(sh_, grace=1, protect={treea.pid})
treea.kill()
treeb.kill()
resume_everyone()
A, B = two_sessions("nowall", "sleep 67; echo 'a'", None)
sida, tpa, enva, pa, treea, cmda = A
sidb = B[0]
check("the call is guarded by a watcher", wait_for(lambda: bool(watchers("tu-nowall-a")), 5))
enqueue_all([("pause now", "2026-10-16T07:30:00.000Z"), ("pause all", "2026-10-16T07:30:01.000Z")], tpa)
check("'pause now' then 'pause all' (the newest without now): the running command is stopped",
      wait_for(lambda: not procs.foreground_shells(treea.pid, cmda), 10))
check("...and then every session is paused", wait_for(lambda: state.is_paused(sida) and state.is_paused(sidb), 5))
state.remove(state.p("running", "tu-nowall-a.json"))
treea.kill()
B[4].kill()
resume_everyone()

print("pause now without a process table:")
NPCMD = "sleep 48; echo 'no table'"
t_start = time.time()
tree5 = subprocess.Popen([str(TREE), claude_wrap(NPCMD)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
CHILDREN.append(tree5)
wait_for(lambda: bool(tree_sleeps(tree5.pid)), 10)
SIDNP = "sess-notable"
state.update_session(SIDNP, claude_pid=tree5.pid, cwd=str(PROJ))
state.touch(state.p("running", "tuNP.json"), json.dumps(
    {"session_id": SIDNP, "tool_use_id": "tuNP", "tool_input": {"command": NPCMD}}))
os.utime(state.p("running", "tuNP.json"), (t_start - 1, t_start - 1))
_real_table = procs.table
procs.table = lambda: {}
try:
    np_res = core.pause(SIDNP, ["now"], by="test")
finally:
    procs.table = _real_table
check("a guarded command is running: says it couldn't stop it, not 'No command was running'",
      "Couldn't stop 1 running command" in np_res and "process table couldn't be read" in np_res
      and "No command was running" not in np_res)
check("...it isn't recorded as interrupted (it still runs)",
      not state.load_session(SIDNP).get("interrupted") and "sleep 48" in tree_sleeps(tree5.pid).values())
check("...but as one it couldn't stop",
      [c["tool_use_id"] for c in state.load_session(SIDNP).get("unstopped") or []] == ["tuNP"])
procs.table = lambda: {}
try:
    np_res = core.pause(SIDNP, ["now"], by="test")
finally:
    procs.table = _real_table
check("'pause now' again while it still runs: named once, as still running",
      np_res.count("sleep 48") == 1 and "Couldn't stop 1 running command" in np_res
      and "Couldn't stop then" not in np_res and "No command was running" not in np_res)
state.remove(state.p("running", "tuNP.json"))
state.clear_paused(SIDNP)
check("resuming forgets the commands it couldn't stop", not state.load_session(SIDNP).get("unstopped"))
procs.table = lambda: {}
try:
    np_res = core.pause(SIDNP, ["now"], by="test")
finally:
    procs.table = _real_table
check("...no guarded call at all: 'No command was running' still holds", "No command was running" in np_res)
state.clear_paused(SIDNP)

print("a queued 'pause now' applied without a process table: its echo says so too:")
# The usual way `pause now` arrives: typed while the command runs, applied by
# the watcher; the command then runs to its end, and the queued text runs as
# a prompt of its own (fast.sh drops the running file first).
TNP = TMP / "tnp.jsonl"
TNP.write_text("")
state.update_session(SIDNP, transcript_path=str(TNP))
pnp = {"session_id": SIDNP, "transcript_path": str(TNP), "cwd": str(PROJ)}
state.touch(state.p("running", "tuNP2.json"), json.dumps(
    dict(pnp, tool_name="Bash", tool_use_id="tuNP2", tool_input={"command": NPCMD})))
os.utime(state.p("running", "tuNP2.json"), (t_start - 1, t_start - 1))
enqueue("pause now", "2026-10-16T08:00:00.000Z", TNP)
procs.table = lambda: {}
try:
    hooks.watch_fire(SIDNP, "tuNP2")
finally:
    procs.table = _real_table
np_ent = state.queued_pauses(state.load_session(SIDNP))
check("the watcher pauses the session; the command still runs (no process table)",
      state.is_paused(SIDNP) and "sleep 48" in tree_sleeps(tree5.pid).values())
check("...the remembered result says it couldn't stop it",
      len(np_ent) == 1 and "Couldn't stop 1 running command" in (np_ent[0].get("result") or ""))
for sh_ in procs.foreground_shells(tree5.pid, NPCMD):
    procs.kill_tree(sh_, grace=1, protect={tree5.pid})   # the command ends by itself
hook("PostToolUse", dict(pnp, tool_name="Bash", tool_use_id="tuNP2", tool_input={"command": NPCMD}))
dequeue("2026-10-16T08:00:30.000Z", TNP)
out = hook("UserPromptSubmit", dict(pnp, prompt="pause now"))
check("its echo says the command couldn't be stopped, not 'No command was running'",
      "Already paused" in ctx(out) and "Couldn't stop then: `sleep 48" in ctx(out)
      and "No command was running" not in ctx(out))
hook("UserPromptSubmit", dict(pnp, prompt="back"))
check("...a message resumes it and forgets it", not state.is_paused(SIDNP)
      and not state.load_session(SIDNP).get("unstopped"))
tree5.kill()

print("killing a command when ps fails mid-kill, or survives SIGKILL:")


@contextlib.contextmanager
def ps_fails():
    """Every process table read fails meanwhile: no ps gives rows, no /proc."""
    cands, ptab = procs._ps_candidates, procs.proc_table
    procs._ps_candidates = lambda: [str(BADPS / "ps")]
    procs.proc_table = lambda root="/proc": {}
    try:
        yield
    finally:
        procs._ps_candidates, procs.proc_table = cands, ptab


def running(cmd):
    """A live process runs exactly this command line (`cmd`: or a test on its record)."""
    hit = cmd if callable(cmd) else (lambda r: r["cmd"] == cmd)
    return any(hit(r) and not r["stat"].startswith("Z") for r in procs.table().values())


def our_child(script, new_group=True):
    """A shell of ours running this script (in a group of its own)."""
    p = subprocess.Popen([SHELL, "-c", script], start_new_session=new_group,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    CHILDREN.append(p)
    return p


def end_all(*cmds):
    """Clean up after a check that failed: SIGKILL what still runs these."""
    for r in procs.table().values():
        if any(c(r) if callable(c) else r["cmd"] == c for c in cmds):
            with contextlib.suppress(OSError):
                os.kill(r["pid"], signal.SIGKILL)


p1 = our_child("trap '' TERM; sleep 71; echo 'ignores TERM'")
wait_for(lambda: running("sleep 71"), 5)
t1 = procs.table()
with ps_fails():
    k1_failed = procs.table() == {}
    k1 = procs.kill_tree(p1.pid, grace=0.5, procs=t1)
check("(ps really fails meanwhile)", k1_failed)
check("ps fails after the caller's read: a TERM-ignoring command still gets SIGKILL (no read means 'ask "
      "each pid', not 'all gone')", k1["needed_sigkill"] and not running("sleep 71") and not procs.alive(p1.pid))
check("...and it's reported killed once it's gone", lambda: k1["killed"][0]["pid"] == p1.pid
      and not k1["survivors"])
end_all("sleep 71")
p2 = our_child("trap '' TERM; sleep 72; echo 'ignores TERM'")
wait_for(lambda: running("sleep 72"), 5)
with ps_fails():
    k2 = procs.kill_tree(p2.pid, grace=0.5)
check("no process table at all: the known pid is still signalled, with the group it leads",
      k2["needed_sigkill"] and not running("sleep 72") and not procs.alive(p2.pid)
      and [d["pid"] for d in k2["killed"]] == [p2.pid])
end_all("sleep 72")
p3 = our_child("sleep 73 & sleep 74; echo 'leads the group'")
wait_for(lambda: running("sleep 73") and running("sleep 74"), 5)
s73 = next(r["pid"] for r in procs.table().values() if r["cmd"] == "sleep 73")
with ps_fails():
    k3 = procs.kill_tree(s73, grace=0.5)
check("...one that doesn't lead its group: only that pid, not the group (no table to say who's in it)",
      not running("sleep 73") and running("sleep 74") and procs.alive(p3.pid)
      and [d["pid"] for d in k3["killed"]] == [s73])
end_all("sleep 73", "sleep 74")
p4 = our_child("sleep 75; echo 'protected child'")
wait_for(lambda: running("sleep 75"), 5)
s75 = next(r["pid"] for r in procs.table().values() if r["cmd"] == "sleep 75")
with ps_fails():
    procs.kill_tree(p4.pid, grace=0.5, protect={s75})
check("...nor a group a protected process is in (it keeps running)",
      not procs.alive(p4.pid) and running("sleep 75"))
end_all("sleep 75")
p5 = our_child("sleep 76; echo 'unkillable'")
wait_for(lambda: running("sleep 76"), 5)
t5 = procs.table()
_alive_in = procs.alive_in
procs.alive_in = lambda pid, t: pid == p5.pid or _alive_in(pid, t)   # as if SIGKILL couldn't end it
try:
    k5 = procs.kill_tree(p5.pid, grace=0.3, procs=t5)
finally:
    procs.alive_in = _alive_in
check("a process still there after SIGKILL is a survivor, not killed",
      lambda: [d["pid"] for d in k5["survivors"]] == [p5.pid]
      and p5.pid not in [d["pid"] for d in k5["killed"]])
end_all("sleep 76")

# Through pause now: the command ignores TERM, and its python leads a group of
# its own; ps works for kill_foreground's read, then fails.
SIDTI = "sess-termignore"
TICMD = (f"trap '' TERM; '{sys.executable}' -c 'import os, time; os.setpgid(0, 0); time.sleep(77)'; "
         "echo 'ignores TERM'")
t_start = time.time()
tree6 = subprocess.Popen([str(TREE), claude_wrap(TICMD)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
CHILDREN.append(tree6)


def py77(r):
    return r["cmd"].startswith(f"{sys.executable} -c ") and "time.sleep(77)" in r["cmd"]


wait_for(lambda: running(lambda r: py77(r) and r["pgid"] == r["pid"]), 10)   # in its own group
state.update_session(SIDTI, claude_pid=tree6.pid, cwd=str(PROJ))
state.touch(state.p("running", "tuTI.json"), json.dumps(
    {"session_id": SIDTI, "tool_use_id": "tuTI", "tool_input": {"command": TICMD}}))
os.utime(state.p("running", "tuTI.json"), (t_start - 1, t_start - 1))
_real_table, _grace = procs.table, state.KILL_GRACE_SECONDS
reads = []


def table_once():
    reads.append(1)
    return _real_table() if len(reads) == 1 else {}


procs.table, state.KILL_GRACE_SECONDS = table_once, 0.5
try:
    ti_res = core.pause(SIDTI, ["now"], by="test")
finally:
    procs.table, state.KILL_GRACE_SECONDS = _real_table, _grace
check("pause now, ps failing after its first read: the TERM-ignoring command is killed, all of it "
      "(the descendant in its own group too: the table read before is used)",
      not running(py77) and not procs.foreground_shells(tree6.pid, TICMD))
check("...said and recorded as stopped", "Stopped 1 running command" in ti_res
      and [c["tool_use_id"] for c in state.load_session(SIDTI).get("interrupted") or []] == ["tuTI"])
end_all(py77)
state.remove(state.p("running", "tuTI.json"))
state.clear_paused(SIDTI)
tree6.kill()

# Nothing of the command dies even of SIGKILL (another user's process, stuck
# in the kernel): kill_tree says so, and it's a command it couldn't stop.
SVCMD = "sleep 78; echo 'survives'"
t_start = time.time()
tree7 = subprocess.Popen([str(TREE), claude_wrap(SVCMD)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
CHILDREN.append(tree7)
wait_for(lambda: running("sleep 78"), 10)
SIDSV = "sess-survives"
state.update_session(SIDSV, claude_pid=tree7.pid, cwd=str(PROJ))
state.touch(state.p("running", "tuSV.json"), json.dumps(
    {"session_id": SIDSV, "tool_use_id": "tuSV", "tool_input": {"command": SVCMD}}))
os.utime(state.p("running", "tuSV.json"), (t_start - 1, t_start - 1))
_kill_tree = procs.kill_tree
procs.kill_tree = lambda root, **kw: {"killed": [], "survivors": [{"pid": root, "cmd": "sh"}],
                                      "needed_sigkill": True}
try:
    sv_res = core.pause(SIDSV, ["now"], by="test")
finally:
    procs.kill_tree = _kill_tree
check("nothing of it killed: said as one it couldn't stop, with why, not 'Stopped'",
      "Couldn't stop 1 running command(s) (`sleep 78" in sv_res and "still running after SIGKILL" in sv_res
      and "Stopped" not in sv_res and "process table" not in sv_res and "No command was running" not in sv_res)
check("...recorded as unstopped, not interrupted", not state.load_session(SIDSV).get("interrupted")
      and [(c["tool_use_id"], c.get("why")) for c in state.load_session(SIDSV).get("unstopped") or []]
      == [("tuSV", "sigkill")])
state.remove(state.p("running", "tuSV.json"))
sv_echo = core.pause(SIDSV, ["now"], by="test")
check("...and the queued command's echo says so, with the same why",
      "Already paused" in sv_echo and "Couldn't stop then: `sleep 78" in sv_echo
      and "still running after SIGKILL" in sv_echo and "No command was running" not in sv_echo)
state.clear_paused(SIDSV)
for sh_ in procs.foreground_shells(tree7.pid, SVCMD):
    procs.kill_tree(sh_, grace=1, protect={tree7.pid})
tree7.kill()
DDCMD = "sleep 79; echo 'twice'"
t_start = time.time()
tree8 = subprocess.Popen([str(TREE), claude_wrap(DDCMD), claude_wrap(DDCMD)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
CHILDREN.append(tree8)
wait_for(lambda: len(procs.foreground_shells(tree8.pid, DDCMD)) == 2, 10)
SIDDD = "sess-twoshells"
state.update_session(SIDDD, claude_pid=tree8.pid, cwd=str(PROJ))
state.touch(state.p("running", "tuDD.json"), json.dumps(
    {"session_id": SIDDD, "tool_use_id": "tuDD", "tool_input": {"command": DDCMD}}))
os.utime(state.p("running", "tuDD.json"), (t_start - 1, t_start - 1))
dd_calls = []


def kill_first_only(root, **kw):
    dd_calls.append(root)
    gone = [{"pid": root, "cmd": "sh"}]
    return {"killed": gone if len(dd_calls) == 1 else [], "survivors": [] if len(dd_calls) == 1 else gone,
            "needed_sigkill": False}


procs.kill_tree = kill_first_only
try:
    dd_res = core.pause(SIDDD, ["now"], by="test")
finally:
    procs.kill_tree = _kill_tree
check("one call, two shells, one of them killed: said once, as stopped (as the session records it)",
      len(dd_calls) == 2 and "Stopped 1 running command" in dd_res and "Couldn't stop" not in dd_res
      and not state.load_session(SIDDD).get("unstopped"))
state.remove(state.p("running", "tuDD.json"))
state.clear_paused(SIDDD)
for sh_ in procs.foreground_shells(tree8.pid, DDCMD):
    procs.kill_tree(sh_, grace=1, protect={tree8.pid})
tree8.kill()

print("watchers end with their call, even without PostToolUse:")
SIDL = "sess-leak"
TL = TMP / "tl.jsonl"
TL.write_text("")
CLAUDE_L = fake_claude()
EL = {"CLAUDE_PID": str(CLAUDE_L.pid)}
hook("SessionStart", {"session_id": SIDL, "transcript_path": str(TL), "cwd": str(PROJ), "source": "startup"}, env=EL)


def lpay(tid, **kw):
    return dict({"session_id": SIDL, "transcript_path": str(TL), "cwd": str(PROJ), "tool_name": "Bash",
                 "tool_use_id": tid, "tool_input": {"command": "make long"}}, **kw)


hook("PreToolUse", lpay("toolu_cancel1"), env=EL)
check("a watcher guards the call", wait_for(lambda: bool(watchers("toolu_cancel1")), 5))
tline({"type": "user", "message": {"role": "user", "content": [{"tool_use_id": "toolu_cancel1", "type": "tool_result",
       "content": "[Request interrupted by user for tool use]", "is_error": True}]}}, TL)
check("cancelled (tool_result, no PostToolUse): the watcher exits",
      wait_for(lambda: not watchers("toolu_cancel1"), 5))
check("...and removes its running file", not state.p("running", "toolu_cancel1.json").exists())
hook("PreToolUse", lpay("toolu_sub1", agent_id="ag1", agent_type="general-purpose"), env=EL)
wait_for(lambda: bool(watchers("toolu_sub1")), 5)
SUBT = TMP / "tl" / "subagents" / "agent-ag1.jsonl"
SUBT.parent.mkdir(parents=True)
tline({"type": "user", "message": {"role": "user", "content": [{"tool_use_id": "toolu_sub1", "type": "tool_result",
       "content": "[Request interrupted by user for tool use]", "is_error": True}]}}, SUBT)
check("a subagent's cancelled call: its watcher reads the subagent transcript and exits",
      wait_for(lambda: not watchers("toolu_sub1"), 5))
hook("PreToolUse", lpay("toolu_main2"), env=EL)
hook("PreToolUse", lpay("toolu_sub2", agent_id="ag2", agent_type="general-purpose"), env=EL)
state.touch(state.p("running", "toolu_else.json"), json.dumps({"session_id": "someone-else", "tool_use_id": "toolu_else"}))
hook("UserPromptSubmit", {"session_id": SIDL, "transcript_path": str(TL), "cwd": str(PROJ), "prompt": "hello"}, env=EL)
check("a new prompt drops the session's stale running file",
      not state.p("running", "toolu_main2.json").exists())
check("...but not a subagent's, or another session's", state.p("running", "toolu_sub2.json").exists()
      and state.p("running", "toolu_else.json").exists())
check("...and the watcher goes with it", wait_for(lambda: not watchers("toolu_main2"), 5))
hook("PreToolUse", lpay("toolu_main3"), env=EL)
hook("Stop", {"session_id": SIDL, "transcript_path": str(TL), "cwd": str(TMP), "stop_hook_active": False}, env=EL)
check("Stop drops them too", not state.p("running", "toolu_main3.json").exists())
for f in ("toolu_sub2", "toolu_else"):
    state.remove(state.p("running", f + ".json"))

print("shell and Python read the same transcript tail:")
TB = TMP / "big.jsonl"
SIDT = "sess-tail"
cutline = ('{"timestamp":"2026-10-16T05:00:00.000Z","sessionId":"x",'
           '"type":"queue-operation","operation":"enqueue","content":"/check:pause"}')
filler_line = '{"type":"assistant","message":"' + "a" * 200 + '"}\n'
need = transcript.TAIL_BYTES - 80   # the window starts 79 bytes before the end of cutline
filler = filler_line * (need // len(filler_line))
filler += '{"type":"x","p":"' + "b" * (need - len(filler) - 20) + '"}\n'
assert len(filler) == need
TB.write_text(cutline + "\n" + filler)
assert '"queue-operation"' in cutline[-79:] and "check:pause" in cutline[-79:]
n0 = pycalls()
for _ in range(2):
    hook("PreToolUse", {"session_id": SIDT, "transcript_path": str(TB), "cwd": str(PROJ),
                        "tool_name": "Read", "tool_use_id": "r1", "tool_input": {"file_path": "x"}})
check("a pause line cut at the start of the tail: the shell skips it like Python (no Python)",
      pycalls() == n0)
TU = TMP / "u2028.jsonl"
TU.write_text(json.dumps({"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-10-16T06:00:00.000Z",
                          "content": "/check:pause", "note": "a b"}, ensure_ascii=False, separators=(",", ":"))
              + "\n" + json.dumps({"type": "queue-operation", "operation": "dequeue",
                                   "timestamp": "2026-10-16T06:00:01.000Z"}) + "\n")
SIDU = "sess-u2028"
pu = {"session_id": SIDU, "transcript_path": str(TU), "cwd": str(PROJ), "tool_name": "Read",
      "tool_use_id": "r2", "tool_input": {"file_path": "x"}}
hook("PreToolUse", pu)
n0 = pycalls()
hook("PreToolUse", pu)
check("a handled pause line with a U+2028 in it: recorded so later calls skip Python", pycalls() == n0)

print("pruning old records:")
now = time.time()
old = now - 9 * 86400
for sid, pid, paused in (("old-dead", dead.pid, False), ("old-dead-paused", dead.pid, True),
                         ("old-live", MAIN_CLAUDE.pid, False), ("new-dead", dead.pid, False)):
    state.update_session(sid, cwd=str(PROJ), claude_pid=pid,
                         started_ts=now - 3600 if sid == "new-dead" else old)
    for d in ("count", "turn", "seen"):
        state.touch(state.p(d, sid), "1")
    state.touch(gateway.cursor_path(sid), "{}")             # the gateway's event cursor
    state.touch(state.p("gateway", sid + ".lock"), "")      # its pause/resume lock (core._gw_lock)
    if paused:
        state.set_paused(sid, by="test")
    if sid != "new-dead":
        os.utime(state.session_path(sid), (old, old))
state.update_session("old-live", started_ts=now)   # newest record for MAIN_CLAUDE stays live
os.utime(state.session_path("old-live"), (old, old))
state.write_json(state.p("checkpoints", "old-dead.json"), {})
state.touch(state.p("running", "stale-dead.json"), json.dumps({"session_id": "old-dead"}))
state.touch(state.p("running", "stale-old.json"), json.dumps({"session_id": SID}))
os.utime(state.p("running", "stale-old.json"), (now - 2 * 86400, now - 2 * 86400))
state.touch(state.p("running", "current.json"), json.dumps({"session_id": SID}))
for jid, st_, ended in (("oldjob-1", "exited", now - 15 * 86400), ("newjob-1", "exited", now - 86400),
                        ("oldrun-1", "running", None)):
    d = state.p("jobs", jid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "output.log").write_text("log\n")
    if st_ == "exited":
        (d / "exit_code").write_text("0\n")
        os.utime(d / "exit_code", (ended, ended))
    with state.lock():
        reg = jobs._registry()
        reg["jobs"][jid] = {"id": jid, "name": jid, "command": "x", "cwd": str(PROJ), "pid": dead.pid,
                            "pgid": dead.pid, "log": str(d / "output.log"), "exit_file": str(d / "exit_code"),
                            "started": state.now_iso(), "started_ts": now - 20 * 86400, "state": st_,
                            **({"ended_ts": ended} if ended else {})}
        jobs._save(reg)
res = state.prune(force=True)
check("an old session whose process is gone is pruned, with its files",
      not state.session_path("old-dead").exists() and not state.p("count", "old-dead").exists()
      and not state.p("checkpoints", "old-dead.json").exists())
check("...its gateway cursor and lock too (gateway/<sid>.json, .lock)",
      not gateway.cursor_path("old-dead").exists() and not state.p("gateway", "old-dead.lock").exists())
check("...but not a paused one, a live one, or a recent one (nor their gateway cursors)",
      all(state.session_path(s).exists() and gateway.cursor_path(s).exists()
          for s in ("old-dead-paused", "old-live", "new-dead")))
check("running files of dead sessions, or older than a day, are pruned",
      not state.p("running", "stale-dead.json").exists() and not state.p("running", "stale-old.json").exists()
      and state.p("running", "current.json").exists())
check("a job that ended 15 days ago is forgotten and its log deleted",
      "oldjob-1" not in jobs._registry()["jobs"] and not state.p("jobs", "oldjob-1").exists())
check("...a recent one is kept, with its log", "newjob-1" in jobs._registry()["jobs"]
      and state.p("jobs", "newjob-1", "output.log").exists())
check("...and so is one that was running until now (its end is recorded now)",
      "oldrun-1" in jobs._registry()["jobs"])
state.remove(state.p("running", "current.json"))
state.remove(state.p("pruned"))
hook("SessionStart", base(session_id="sess-prune", source="startup"), env={"CLAUDE_PID": str(fake_claude().pid)})
check("SessionStart prunes (at most every few hours)", state.p("pruned").exists())

# --- pixi launcher ----------------------------------------------------------------------
print("finding pixi with a minimal PATH:")
MINPATH = "/usr/bin:/bin:/usr/sbin:/sbin"
FH = TMP / "home"
(FH / ".pixi" / "bin").mkdir(parents=True)
FAKEPIXI = FH / ".pixi" / "bin" / "pixi"
FAKEPIXI.write_text('#!/bin/sh\necho "fake-pixi $*"\n')
FAKEPIXI.chmod(0o755)
envp = {"HOME": str(FH), "PATH": MINPATH, "LC_ALL": "C"}
r = subprocess.run(["sh", str(ROOT / "hooks" / "pixi.sh"), "--check"], env=envp, capture_output=True, text=True)
check("~/.pixi/bin/pixi found without PATH", r.stdout.strip() == str(FAKEPIXI))
PH = TMP / "pixihome"
(PH / "bin").mkdir(parents=True)
(PH / "bin" / "pixi").write_text('#!/bin/sh\necho "pixi-home $*"\n')
(PH / "bin" / "pixi").chmod(0o755)
r = subprocess.run(["sh", str(ROOT / "hooks" / "pixi.sh"), "run", "x"], env=dict(envp, PIXI_HOME=str(PH)),
                   capture_output=True, text=True)
check("$PIXI_HOME/bin/pixi preferred, and arguments passed through", r.stdout.strip() == "pixi-home run x")
mcpcfg = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["jobs"]
check(".mcp.json starts the server through the runtime launcher (env.sh mcp: the env's python, no pixi run)",
      mcpcfg["command"] == "sh" and mcpcfg["args"] == ["${CLAUDE_PLUGIN_ROOT}/hooks/env.sh", "mcp"])
# Claude Code 2.1.304 deferred the job tools behind ToolSearch (acceptance's
# tools probe): the server and each tool ask to be loaded up front, as the
# gateway's gw server does.
check(".mcp.json loads the jobs server up front (alwaysLoad), and every tool it lists says so too "
      "(_meta anthropic/alwaysLoad in its tools/list answer)", lambda: mcpcfg.get("alwaysLoad") is True
      and len(resp[2]["result"]["tools"]) == 6
      and all((t.get("_meta") or {}).get("anthropic/alwaysLoad") is True for t in resp[2]["result"]["tools"]))


def plugin_copy(dest, nopixi=False):
    """A copy of the plugin (hooks, run.py, src, manifests) to run the shell
    half on its own: with nopixi, pixi.sh's fixed folders point into the temp
    dir, so no pixi is found anywhere (with a minimal PATH and an empty HOME)."""
    dest = Path(dest)
    (dest / "hooks").mkdir(parents=True)
    for f in ("fast.sh", "watch.sh", "env.sh", "lib.sh", "pixi.sh"):
        shutil.copy(ROOT / "hooks" / f, dest / "hooks" / f)
    for f in ("pixi.toml", "pixi.lock", "run.py"):
        shutil.copy(ROOT / f, dest / f)
    shutil.copytree(ROOT / "src", dest / "src", ignore=shutil.ignore_patterns("__pycache__"))
    (dest / ".claude-plugin").mkdir()
    shutil.copy(ROOT / ".claude-plugin" / "plugin.json", dest / ".claude-plugin" / "plugin.json")
    if nopixi:
        text = (dest / "hooks" / "pixi.sh").read_text()
        for d in re.findall(r"(/(?:opt|usr|home)/[\w./-]*/pixi)\b", text):
            text = text.replace(d, str(TMP / "none") + d)
        (dest / "hooks" / "pixi.sh").write_text(text)
    return dest


def run_sh(script, args, env, payload=None, timeout=60):
    return subprocess.run(["sh", str(script), *args], input=json.dumps(payload) if payload is not None else "",
                          env=env, capture_output=True, text=True, timeout=timeout)


def sys_msg(r):
    try:
        return json.loads(r.stdout or "{}").get("systemMessage", "")
    except ValueError:
        return ""


# Without any pixi: no env can be built; the hooks say so and stay fail-safe.
NOPIXI = plugin_copy(TMP / "nopixi", nopixi=True)
envn = {"HOME": str(TMP / "emptyhome"), "PATH": MINPATH, "CHECKPOINT_HOME": str(TMP / "state-np"),
        "CHECKPOINT_GATEWAY_CONTRACT": os.environ["CHECKPOINT_GATEWAY_CONTRACT"]}
r = run_sh(NOPIXI / "hooks" / "fast.sh", ["SessionStart"], envn, base())
msg = sys_msg(r)
check("no pixi anywhere: SessionStart says so, names https://pixi.sh, and that pausing still works",
      "pixi not found" in msg and "Install pixi from https://pixi.sh" in msg and "pausing still works" in msg)
check("...the env would live in the fallback data folder (no CLAUDE_PLUGIN_DATA: ~/.claude/plugins/data/check-checkpoint)",
      (TMP / "state-np" / "rt" / state.safe_id(SID)).read_text().strip().startswith(
          str(TMP / "emptyhome" / ".claude" / "plugins" / "data" / "check-checkpoint" / "envs") + "/"))
r = run_sh(NOPIXI / "hooks" / "fast.sh", ["PreToolUse"], envn, bash())
check("...a call of a session that isn't paused goes ahead (exit 0, no output)", r.returncode == 0 and r.stdout == "")
BROKEN = TMP / "brokenhome"
(BROKEN / ".pixi" / "bin").mkdir(parents=True)
(BROKEN / ".pixi" / "bin" / "pixi").write_text('#!/bin/sh\necho "error: lock file is broken" >&2\nexit 1\n')
(BROKEN / ".pixi" / "bin" / "pixi").chmod(0o755)
envb = dict(envn, HOME=str(BROKEN), CHECKPOINT_HOME=str(TMP / "state-broken"))
r = run_sh(NOPIXI / "hooks" / "env.sh", ["ensure"], envb)
check("pixi there but the build fails: env.sh ensure exits 1", r.returncode == 1)
r = run_sh(NOPIXI / "hooks" / "fast.sh", ["SessionStart"], envb, base())
msg = sys_msg(r)
check("...SessionStart shows the error, that it's retried, and that pausing still works",
      "setting up its runtime failed" in msg and "lock file is broken" in msg and "retried" in msg
      and "Pausing still works" in msg)
# pixi's real error (a pixi.toml changed without its lock; pixi 0.81.0, these
# exact bytes) ends with a blank line: the reason is its "Error" line, not the
# last line (an empty "( )" once).
MULTI = TMP / "multihome"
(MULTI / ".pixi" / "bin").mkdir(parents=True)
(MULTI / ".pixi" / "bin" / "pixi").write_text(
    "#!/bin/sh\n[ \"$1\" = --version ] && { echo 'pixi 0.81.0'; exit 0; }\n"
    "echo ' WARN some progress first' >&2\n"
    "printf 'Error:   \\303\\227 lock file not up-to-date with the workspace\\n\\n' >&2\nexit 1\n")
(MULTI / ".pixi" / "bin" / "pixi").chmod(0o755)
envm = dict(envn, HOME=str(MULTI), CHECKPOINT_HOME=str(TMP / "state-multi"))


def no_build_running(home):
    """SessionStart retries a failed build in the background: wait it out."""
    return wait_for(lambda: not list(Path(home).glob(".claude/plugins/data/check-checkpoint/envs/*.building")), 20)


r = run_sh(NOPIXI / "hooks" / "env.sh", ["ensure"], envm)
msg = sys_msg(run_sh(NOPIXI / "hooks" / "fast.sh", ["SessionStart"], envm, base()))
no_build_running(MULTI)
doc = run_sh(NOPIXI / "hooks" / "env.sh", ["doctor"], envm).stdout
check("pixi's multi-line error ending in a blank line: SessionStart and doctor give its Error line as the reason",
      r.returncode == 1
      and "setting up its runtime failed (Error: lock file not up-to-date with the workspace)" in msg
      and "runtime: setup failed: Error: lock file not up-to-date with the workspace (log: " in doc)
(MULTI / ".pixi" / "bin" / "pixi").write_text("#!/bin/sh\n[ \"$1\" = --version ] && exit 0\n"
                                              "printf 'one\\nlast words here\\n\\n   \\n' >&2\nexit 1\n")
run_sh(NOPIXI / "hooks" / "env.sh", ["ensure"], envm)
check("...with no Error line: its last line that isn't blank",
      "runtime: setup failed: last words here (log: " in run_sh(NOPIXI / "hooks" / "env.sh", ["doctor"], envm).stdout)

# --- the runtime env (brief section 2) ---------------------------------------------------
print("the runtime env in Claude Code's plugin data folder, built with pixi install --locked:")
FAKE = TMP / "fakepixi"
(FAKE / "bin").mkdir(parents=True)
(FAKE / "bin" / "pixi").write_text(f"""#!/bin/sh
# A fake pixi: `install --locked --manifest-path M` makes M's env with this
# suite's Python as its python3. It logs every call.
echo "$*" >> "$FAKE_PIXI_LOG"
if [ "$1" = --version ]; then echo "pixi ${{FAKE_PIXI_VERSION:-0.83.0}}"; exit 0; fi
[ -n "${{FAKE_PIXI_SLEEP:-}}" ] && sleep "$FAKE_PIXI_SLEEP"
if [ -n "${{FAKE_PIXI_FAIL:-}}" ]; then echo "error: $FAKE_PIXI_FAIL" >&2; exit 1; fi
case "$1 $2 $3" in "install --locked --manifest-path") ;; *) echo "unexpected: $*" >&2; exit 2 ;; esac
d=$(dirname "$4")/.pixi/envs/default/bin
mkdir -p "$d" && ln -sf {shlex.quote(sys.executable)} "$d/python3"
""")
(FAKE / "bin" / "pixi").chmod(0o755)
LOCK_SHA = __import__("hashlib").sha256((ROOT / "pixi.lock").read_bytes()).hexdigest()[:12]
LOCK_BYTES = (ROOT / "pixi.lock").read_bytes()
RT = plugin_copy(TMP / "rt plugin ü", nopixi=True)   # a folder with a space and a non-ASCII letter


def rt_env(tag, **kw):
    data = TMP / f"rt data é {tag}"
    env = {"HOME": str(TMP / "emptyhome"), "PATH": f"{FAKE / 'bin'}:{MINPATH}", "LC_ALL": "C",
           "CHECKPOINT_HOME": str(TMP / f"rt-state-{tag}"), "CLAUDE_PLUGIN_DATA": str(data),
           "FAKE_PIXI_LOG": str(TMP / f"pixi-{tag}.log"),
           "CHECKPOINT_GATEWAY_ENV": os.environ["CHECKPOINT_GATEWAY_ENV"],
           "CHECKPOINT_GATEWAY_CONTRACT": os.environ["CHECKPOINT_GATEWAY_CONTRACT"],
           "CHECKPOINT_NUDGE_EVERY": "3", "CHECKPOINT_WATCH_INTERVAL": "0.2"}
    env.update({k: str(v) for k, v in kw.items()})
    return env, data / "envs" / LOCK_SHA


def pixi_calls(env):
    try:
        return Path(env["FAKE_PIXI_LOG"]).read_text().splitlines()
    except OSError:
        return []


E1, ENVD1 = rt_env("a")
r = run_sh(RT / "hooks" / "env.sh", ["ensure"], E1)
calls = [c for c in pixi_calls(E1) if c.startswith("install")]
check("env.sh ensure builds $CLAUDE_PLUGIN_DATA/envs/<sha12 of pixi.lock> (exit 0)",
      r.returncode == 0 and (ENVD1 / ".ready").exists() and (ENVD1 / ".pixi/envs/default/bin/python3").exists())
check("...with `pixi install --locked --manifest-path <its own copy of pixi.toml>` (never the plugin's)",
      calls == [f"install --locked --manifest-path {ENVD1}/pixi.toml"]
      and (ENVD1 / "pixi.toml").read_text() == (ROOT / "pixi.toml").read_text()
      and (ENVD1 / "pixi.lock").read_bytes() == LOCK_BYTES)
check("...the plugin's pixi.lock unchanged, and nothing built in the plugin folder",
      (RT / "pixi.lock").read_bytes() == LOCK_BYTES and not (RT / ".pixi").exists())
check("...warmed once (.warm), the build lock gone, a bytecode cache in the data folder, none in the plugin",
      (ENVD1 / ".warm").exists() and not Path(f"{ENVD1}.building").exists()
      and (ENVD1.parent.parent / "pycache").is_dir() and not list(RT.rglob("__pycache__")))
r = run_sh(RT / "hooks" / "env.sh", ["ensure"], E1)
check("a second ensure builds nothing (idempotent)",
      r.returncode == 0 and len([c for c in pixi_calls(E1) if c.startswith("install")]) == 1)
r = run_sh(RT / "hooks" / "env.sh", ["state"], E1)
check("env.sh state: ready", r.stdout.strip() == "ready")
# The TUI acceptance scripts run checkpoint's CLI from the test process with accept_install.cli_env: the
# sessions' plugin data folder, state and scratch gateway files, whatever the caller's environment says. A
# caller's own CLAUDE_PLUGIN_DATA (lib.sh takes it before CHECKPOINT_ENVD) sent acceptance_recovery's
# `env.sh run status` to an env that isn't there (exit 75, nothing printed; the VM, 2026-10-21).
sys.path.insert(0, str(ROOT / "tests"))
import accept_install  # noqa: E402  (tests/tui: stdlib only)
from tui import safety as tui_safety  # noqa: E402
ACC_CFG = TMP / "acc cfg"
caller = dict(E1, CLAUDE_PLUGIN_DATA=str(TMP / "caller data"), CHECKPOINT_HOME=str(TMP / "caller state"),
              CHECKPOINT_ENVD=str(TMP / "caller data" / "envs" / LOCK_SHA),
              CHECKPOINT_GATEWAY_CONTRACT=str(TMP / "caller gw" / "run" / "contract.json"),
              CHECKPOINT_GATEWAY_ENV=str(TMP / "caller gw" / "env.sh"))
ce = accept_install.cli_env(ACC_CFG, ENVD1.parent.parent, Path(E1["CHECKPOINT_HOME"]), str(ENVD1), base=caller)
st_acc = tui_safety.state_env(tui_safety.state_root_for(ACC_CFG))
r_caller = run_sh(RT / "hooks" / "env.sh", ["run", "status"], caller)
r_cli = run_sh(RT / "hooks" / "env.sh", ["run", "status"], ce)
acc_ok = (r_caller.returncode == 75 and r_caller.stdout == "" and r_cli.returncode == 0 and r_cli.stdout.strip() != ""
          and ce["CLAUDE_PLUGIN_DATA"] == str(ENVD1.parent.parent) and ce["CHECKPOINT_HOME"] == E1["CHECKPOINT_HOME"]
          and ce["CHECKPOINT_ENVD"] == str(ENVD1)
          and ce["CHECKPOINT_GATEWAY_CONTRACT"] == st_acc["CHECKPOINT_GATEWAY_CONTRACT"]
          and ce["CHECKPOINT_GATEWAY_ENV"] == st_acc["CHECKPOINT_GATEWAY_ENV"])
if not acc_ok:
    print(f"       (caller's env: exit {r_caller.returncode}; cli_env: exit {r_cli.returncode} {r_cli.stderr[-200:]!r})")
check("the TUI acceptance scripts' CLI env (accept_install.cli_env) names the sessions' plugin data folder, state "
      "and scratch gateway files over the caller's own: `env.sh run status` reaches the runtime (exit 0), where the "
      "caller's CLAUDE_PLUGIN_DATA gives exit 75 with nothing printed", acc_ok)
# The acceptance scripts end what their scripted commands start (0.7.2): on Linux acceptance_journal and
# acceptance_recovery left `sleep 300` behind, because killing the shell whose command line names the
# marker leaves its `sleep` child (which names nothing) running, reparented.


def _reap_case(tag):
    mk = f"ck-reap-{tag}-{os.getpid()}"
    sh_ = subprocess.Popen(["/bin/sh", "-c", f"sleep 300; echo {mk}"], start_new_session=True,
                           stdout=subprocess.DEVNULL)
    CHILDREN.append(sh_)
    kid, end = 0, time.time() + 10
    while not kid and time.time() < end:
        t_ = procs.table()
        kid = next((p for p in procs.descendants(sh_.pid, t_) if t_[p]["cmd"].startswith("sleep 300")), 0)
        time.sleep(0.05)
    return mk, sh_, kid


def _proc_gone(pid):
    r = procs.table().get(pid)
    return pid > 0 and (r is None or r["stat"].startswith("Z"))


_mk, _sh, _kid = _reap_case("now")
_left = accept_install.kill_marked(_mk)
check("the acceptance scripts' cleanup (accept_install.kill_marked) ends a marked shell AND its `sleep 300` child, "
      "whose command line names no marker", _kid > 0 and not _left and wait_for(lambda: _sh.poll() is not None, 5)
      and wait_for(lambda: _proc_gone(_kid), 5))
_mk, _sh, _kid = _reap_case("orphan")
_noted = accept_install.note_marked(_mk)
_sh.kill()      # the shell dies first (as when the session's cleanup got it): its child is orphaned
_sh.wait(5)
_orphan = _kid > 0 and not _proc_gone(_kid)
_left = accept_install.kill_started()
check("...and a tree noted while it ran (note_marked) is ended at the end (kill_started) even after its shell died "
      "first: the orphaned `sleep` too", _kid in _noted and _orphan and not _left
      and wait_for(lambda: _proc_gone(_kid), 5))
r = run_sh(RT / "hooks" / "fast.sh", ["PreToolUse"], E1, base(tool_name="Read", tool_use_id="rd1",
                                                               tool_input={"file_path": "x"}))
check("runtime ready, not paused: the call goes ahead with no degraded mark",
      r.stdout.strip() == "" and not (Path(E1["CHECKPOINT_HOME"]) / "degraded").exists())

E2, ENVD2 = rt_env("b", FAKE_PIXI_SLEEP=1)
procs2 = [subprocess.Popen(["sh", str(RT / "hooks" / "env.sh"), "ensure"], env=E2, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) for _ in range(3)]
rcs = [p.wait(timeout=60) for p in procs2]
check("three concurrent ensures: one builds, the others see its live lock and exit 0",
      rcs == [0, 0, 0] and len([c for c in pixi_calls(E2) if c.startswith("install")]) == 1)
check("...and the env is ready afterwards", wait_for(lambda: (ENVD2 / ".ready").exists(), 5))

# A builder killed hard (sleep, a crash, Claude Code's timeout) is detected at once.
E3, ENVD3 = rt_env("c", FAKE_PIXI_SLEEP=30)
p3 = subprocess.Popen(["sh", str(RT / "hooks" / "env.sh"), "ensure"], env=E3, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, start_new_session=True)
lock3 = Path(f"{ENVD3}.building")
check("a slow build holds the lock with the builder's and pixi's pids",
      wait_for(lambda: (lock3 / "pid").exists() and (lock3 / "pixi").exists(), 10)
      and (lock3 / "pid").read_text().strip() == str(p3.pid))
# The lock names pixi's pid as soon as it is forked: let the fake pixi log its call before the kill.
wait_for(lambda: any(c.startswith("install") for c in pixi_calls(E3)), 10)
os.killpg(p3.pid, signal.SIGKILL)
p3.wait(timeout=10)
E3b = dict(E3)
E3b.pop("FAKE_PIXI_SLEEP")
t0 = time.time()
r = run_sh(RT / "hooks" / "env.sh", ["ensure"], E3b)
took = time.time() - t0
check(f"...killed with its pixi: the next ensure takes over at once ({took:.1f} s) and builds",
      r.returncode == 0 and took < 10 and (ENVD3 / ".ready").exists()
      and len([c for c in pixi_calls(E3) if c.startswith("install")]) == 2)

E4, ENVD4 = rt_env("d", FAKE_PIXI_SLEEP=2)
p4 = subprocess.Popen(["sh", str(RT / "hooks" / "env.sh"), "ensure"], env=E4, stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL)
lock4 = Path(f"{ENVD4}.building")
wait_for(lambda: (lock4 / "pixi").exists(), 10)
p4.kill()          # the builder alone: its pixi runs on
p4.wait(timeout=10)
E4b = dict(E4)
E4b.pop("FAKE_PIXI_SLEEP")
r = run_sh(RT / "hooks" / "env.sh", ["ensure"], E4b)
check("a builder killed while its pixi still runs: the lock stays live (no second pixi meanwhile)",
      r.returncode == 0 and len([c for c in pixi_calls(E4) if c.startswith("install")]) == 1)
check("...once that pixi is gone, the next ensure takes over and finishes",
      wait_for(lambda: not Path(f"{ENVD4}.building/pixi").exists() or
               subprocess.run(["sh", "-c", f"kill -0 $(cat '{ENVD4}.building/pixi') 2>/dev/null"]).returncode != 0, 10)
      and run_sh(RT / "hooks" / "env.sh", ["ensure"], E4b).returncode == 0 and (ENVD4 / ".ready").exists())

E5, ENVD5 = rt_env("e")
lock5 = Path(f"{ENVD5}.building")
lock5.mkdir(parents=True)
(lock5 / "pid").write_text("999999\n")   # no such process
r = run_sh(RT / "hooks" / "env.sh", ["ensure"], E5)
check("a lock left by a dead pid is broken at once", r.returncode == 0 and (ENVD5 / ".ready").exists())

# A read-only plugin folder (invariant 8: an installed copy may be one). The
# build's copies of pixi.toml and pixi.lock take the plugin's modes, so they
# were read-only and every attempt after a failed or killed first one failed
# at its cp, for good.
RO = plugin_copy(TMP / "ro plug ü" / "checkpoint", nopixi=True)
subprocess.run(["chmod", "-R", "a-w", str(RO)], check=True)
try:
    ERO, ENVDRO = rt_env("ro", FAKE_PIXI_FAIL="network unreachable")   # offline, say
    r1 = run_sh(RO / "hooks" / "env.sh", ["ensure"], ERO)
    ERO.pop("FAKE_PIXI_FAIL")
    r2 = run_sh(RO / "hooks" / "env.sh", ["ensure"], ERO)
    check("read-only plugin folder: after a failed first build the next one succeeds (no cp over read-only copies)",
          r1.returncode == 1 and r2.returncode == 0 and (ENVDRO / ".ready").exists()
          and "Permission denied" not in read_or_empty(ENVDRO / "install.log")
          and len([c for c in pixi_calls(ERO) if c.startswith("install")]) == 2)
    check("...its copies are the plugin's files, writable in the data folder; the plugin folder unchanged",
          (ENVDRO / "pixi.lock").read_bytes() == LOCK_BYTES and os.access(ENVDRO / "pixi.lock", os.W_OK)
          and not os.access(RO / "pixi.lock", os.W_OK) and not (RO / ".pixi").exists())
    ERK, ENVDRK = rt_env("ro-kill", FAKE_PIXI_SLEEP=30)
    pk = subprocess.Popen(["sh", str(RO / "hooks" / "env.sh"), "ensure"], env=ERK, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, start_new_session=True)
    wait_for(lambda: Path(f"{ENVDRK}.building/pixi").exists(), 10)
    os.killpg(pk.pid, signal.SIGKILL)   # the builder and its pixi, mid-build
    pk.wait(timeout=10)
    ERK.pop("FAKE_PIXI_SLEEP")
    r = run_sh(RO / "hooks" / "env.sh", ["ensure"], ERK)
    check("...and after a first build killed mid-way, the next ensure takes over and succeeds",
          r.returncode == 0 and (ENVDRK / ".ready").exists()
          and run_sh(RO / "hooks" / "env.sh", ["state"], ERK).stdout.strip() == "ready")
finally:
    subprocess.run(["chmod", "-R", "u+w", str(RO)])

# Old envs: unused for ENV_KEEP_DAYS, removed after a build or an ensure.
OLD = ENVD1.parent / "aaaaaaaaaaaa"
NEW = ENVD1.parent / "bbbbbbbbbbbb"
for d in (OLD, NEW):
    (d / ".pixi").mkdir(parents=True)
    (d / ".ready").write_text("")
    (d / ".used").write_text("")
os.utime(OLD / ".used", (time.time() - 10 * 86400,) * 2)
r = run_sh(RT / "hooks" / "env.sh", ["ensure"], E1)
check(f"envs unused for {state.ENV_KEEP_DAYS} days are removed, recent ones and the current one kept",
      not OLD.exists() and NEW.exists() and ENVD1.exists())
check("lib.sh and state.py agree on that age",
      f"CK_ENV_KEEP_DAYS={state.ENV_KEEP_DAYS} " in (ROOT / "hooks" / "lib.sh").read_text())

print("the session start never blocks on a build:")
E6, ENVD6 = rt_env("f", FAKE_PIXI_SLEEP=3)
t0 = time.time()
r = run_sh(RT / "hooks" / "fast.sh", ["SessionStart"], E6, base(source="startup"))
took = time.time() - t0
msg = sys_msg(r)
check(f"no env yet: SessionStart answers at once ({took:.2f} s) and says first-time setup is running",
      took < 2.5 and "first-time setup is running" in msg and "Pausing works meanwhile" in msg)
check("...with no fixed time (0.7.3: it said 'about a minute'; seconds on a fast network, longer on a slow one)",
      "about a minute" not in msg and "usually under a minute" in msg
      and not [f for f in ("hooks/fast.sh", "hooks/env.sh", "skills/help/SKILL.md", "hooks/mod/register.tsx")
               if "about a minute" in (ROOT / f).read_text() or "in a minute" in (ROOT / f).read_text()])
check("...and starts the build in the background (fake pixi called with the data folder's manifest)",
      wait_for(lambda: any(c.startswith(f"install --locked --manifest-path {ENVD6}/") for c in pixi_calls(E6)), 10))
check("...rt/<session> records the env for the other hooks and the mod",
      (Path(E6["CHECKPOINT_HOME"]) / "rt" / state.safe_id(SID)).read_text().strip() == str(ENVD6))
HELPER = Path(E6["CHECKPOINT_HOME"]) / "checkpoint"
check("...and writes the terminal helper (0700) pointing at this copy and its data folder",
      HELPER.is_file() and (HELPER.stat().st_mode & 0o777) == 0o700
      and str(RT.resolve() / "hooks" / "env.sh") in HELPER.read_text() and str(ENVD6.parent.parent) in HELPER.read_text())
check("the build finishes in the background", wait_for(lambda: (ENVD6 / ".ready").exists(), 20))
r = run_sh(RT / "hooks" / "fast.sh", ["SessionStart"], dict(E6, CLAUDE_PID=str(MAIN_CLAUDE.pid)),
           base(source="startup"))
check("the next SessionStart runs Python from the env directly (the tracker note)",
      "TRACKER.md" in ctx(json.loads(r.stdout or "{}")) and (ENVD6 / ".used").exists())
r = subprocess.run([str(HELPER), "status"], env=dict(E6, CLAUDE_PLUGIN_DATA=""), capture_output=True, text=True,
                   timeout=60)
check("the terminal helper runs the CLI with that env (status)", r.returncode == 0 and "Sessions:" in r.stdout)
r = subprocess.run([str(HELPER), "doctor"], env=dict(E6, CLAUDE_PLUGIN_DATA=""), capture_output=True, text=True,
                   timeout=60)
check("...and doctor (runtime ready, the same env)", "runtime: ready" in r.stdout and str(ENVD6) in r.stdout)


def mcp_init(cmd, env, timeout=30):
    p = subprocess.run(cmd, input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                         "clientInfo": {"name": "t", "version": "0"}}}) + "\n",
                       env=env, capture_output=True, text=True, timeout=timeout)
    try:
        return p, json.loads(p.stdout.splitlines()[0])
    except (ValueError, IndexError):
        return p, {}


p, resp = mcp_init(["sh", str(RT / "hooks" / "env.sh"), "mcp"], E1)
check("env.sh mcp: the MCP server on the env's python, answering initialize (stdout is protocol only)",
      resp.get("result", {}).get("serverInfo", {}).get("name") is not None and p.stdout.lstrip().startswith("{"))
En, _ = rt_env("g")
En["PATH"] = MINPATH
t0 = time.time()
p, resp = mcp_init(["sh", str(NOPIXI / "hooks" / "env.sh"), "mcp"], dict(En, CHECKPOINT_HOME=str(TMP / "state-np")), 60)
check("env.sh mcp without pixi: exits 1 with the reason on stderr, nothing on stdout",
      p.returncode == 1 and p.stdout == "" and "pixi isn't installed" in p.stderr)

print("the pause gate fails closed while paused (CLAUDE.md invariant 1):")
E7, _ = rt_env("h")
E7["PATH"] = MINPATH   # no pixi, no env: Python can't answer
S7 = Path(E7["CHECKPOINT_HOME"])
T7 = TMP / "t7.jsonl"
T7.write_text("")
run_sh(NOPIXI / "hooks" / "fast.sh", ["SessionStart"], E7, base(source="startup", transcript_path=str(T7)))
r = run_sh(NOPIXI / "hooks" / "fast.sh", ["PreToolUse"], E7, bash("ls", tid="np1", transcript_path=str(T7)))
check("no runtime, not paused: the call goes ahead (no output)", r.stdout.strip() == "")
deg = S7 / "degraded" / state.safe_id(SID)
check("...not silently: degraded/<session> records that the gate ran without its runtime",
      deg.exists() and "PreToolUse while not paused: its runtime isn't ready" in deg.read_text()
      and "the call went ahead" in deg.read_text())
deg.unlink(missing_ok=True)
(S7 / "flags").mkdir(parents=True, exist_ok=True)
(S7 / "flags" / state.safe_id(SID)).write_text("2026-10-19T10:00:00+0000 test\n")
r = run_sh(NOPIXI / "hooks" / "fast.sh", ["PreToolUse"], E7, bash("ls", tid="np2", transcript_path=str(T7)))
out = json.loads(r.stdout or "{}")
check("no runtime, paused: fast.sh itself denies, with the reason",
      denied(out) and reason(out).startswith(
          "checkpoint: this session is paused; checkpoint's runtime is not ready, so every tool call is held"))
check("...and records degraded/<session>", deg.exists() and "PreToolUse while paused" in deg.read_text())
r = run_sh(NOPIXI / "hooks" / "fast.sh", ["PreToolUse"], E7,
           dict(bash("ls", tid="np3", transcript_path=str(T7)), agent_id="agent-7"))
check("...a subagent's call too (its own wording)", "return a short report" in reason(json.loads(r.stdout or "{}")))
r = run_sh(NOPIXI / "hooks" / "fast.sh", ["PreToolUse"], E7,
           base(tool_name="mcp__plugin_check_jobs__job_status", tool_input={}))
check("...job_status/tail/stop stay allowed while paused", allowed(json.loads(r.stdout or "{}")))
# A typed message resumes even without Python; a pause typed is recorded.
r = run_sh(NOPIXI / "hooks" / "fast.sh", ["UserPromptSubmit"], E7, base(prompt="carry on please"))
check("no runtime: a message the user typed resumes (the flag cleared, said so)",
      not (S7 / "flags" / state.safe_id(SID)).exists() and "resumed" in sys_msg(r))
r = run_sh(NOPIXI / "hooks" / "fast.sh", ["UserPromptSubmit"], E7, base(prompt="/check:pause"))
check("no runtime: /check:pause still pauses (the fallback writes the flag)",
      (S7 / "flags" / state.safe_id(SID)).exists() and "Paused" in ctx(json.loads(r.stdout or "{}")))
r = run_sh(NOPIXI / "hooks" / "fast.sh", ["UserPromptSubmit"], E7,
           base(prompt="<task-notification>\n<task-id>x</task-id>"))
check("...a task notification doesn't resume it", (S7 / "flags" / state.safe_id(SID)).exists())
(S7 / "noresume").mkdir(exist_ok=True)
(S7 / "noresume" / state.safe_id(SID)).write_text("x")
r = run_sh(NOPIXI / "hooks" / "fast.sh", ["UserPromptSubmit"], E7, base(prompt="scheduled: check the build"))
check("...nor a prompt the mod marked as not the user's (noresume)",
      (S7 / "flags" / state.safe_id(SID)).exists() and "didn't resume" in ctx(json.loads(r.stdout or "{}")))
r = run_sh(NOPIXI / "hooks" / "fast.sh", ["UserPromptSubmit"], E7, base(prompt="/check:doctor"))
check("no runtime: /check:doctor answers from the shell (runtime, gate degraded now, and the last mark)",
      "runtime:" in ctx(json.loads(r.stdout or "{}"))
      and "gate: degraded (the runtime isn't ready: nopixi)" in ctx(json.loads(r.stdout or "{}"))
      and "last recorded for this session: " in ctx(json.loads(r.stdout or "{}")))
(S7 / "flags" / state.safe_id(SID)).unlink()
# A pause command waiting in the queue, and no Python to apply it: paused here.
TQ7 = TMP / "tq7.jsonl"
tline({"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-10-19T10:00:00.000Z",
       "sessionId": SID, "content": "/check:pause"}, TQ7)
r = run_sh(NOPIXI / "hooks" / "fast.sh", ["PreToolUse"], E7, bash("ls", tid="np4", transcript_path=str(TQ7)))
check("no runtime, a pause queued: denied, and this session is paused here",
      denied(json.loads(r.stdout or "{}")) and (S7 / "flags" / state.safe_id(SID)).exists()
      and "waiting in the queue" in reason(json.loads(r.stdout or "{}")))
(S7 / "flags" / state.safe_id(SID)).unlink()
TQ8 = TMP / "tq8.jsonl"
tline({"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-10-19T10:00:00.000Z",
       "sessionId": SID, "content": "/check:pause zz-other-session"}, TQ8)
r = run_sh(NOPIXI / "hooks" / "fast.sh", ["PreToolUse"], E7, bash("ls", tid="np5", transcript_path=str(TQ8)))
check("...one for another session: this call goes ahead, recorded as degraded",
      r.stdout.strip() == "" and not (S7 / "flags" / state.safe_id(SID)).exists()
      and "another session" in deg.read_text())

print("when Python fails or hangs:")
FAILPY = TMP / "failpy.sh"
FAILPY.write_text("#!/bin/sh\ncat >/dev/null\necho boom >&2\nexit 3\n")
FAILPY.chmod(0o755)
HANGPY = TMP / "hangpy.sh"
HANGPY.write_text("#!/bin/sh\nexec sleep 60\n")
HANGPY.chmod(0o755)
SF = TMP / "state-fail"
EF = {"CHECKPOINT_HOME": str(SF), "CHECKPOINT_PYTHON": str(FAILPY)}
(SF / "flags").mkdir(parents=True)
(SF / "flags" / state.safe_id(SID)).write_text("x\n")
out = hook("PreToolUse", bash("ls", tid="f1"), env=EF)
check("paused, Python crashes: denied", denied(out) and "every tool call is held" in reason(out))
t0 = time.time()
out = hook("PreToolUse", bash("ls", tid="f2"), env=dict(EF, CHECKPOINT_PYTHON=str(HANGPY), CHECKPOINT_HOOK_DEADLINE="1"))
took = time.time() - t0
check(f"paused, Python hangs: denied at the deadline ({took:.1f} s), before Claude Code's own timeout",
      denied(out) and took < 5 and "Python timed out" in (SF / "degraded" / state.safe_id(SID)).read_text())
(SF / "flags" / state.safe_id(SID)).unlink()
out = hook("PostToolUse", base(tool_name="Read", tool_use_id="f3"), env=dict(EF, CHECKPOINT_NUDGE_EVERY="1"))
check("not paused, Python crashes in the reminder: nothing shown, degraded/<session> says so",
      out is None and "PostToolUse: Python failed (exit 3)" in (SF / "degraded" / state.safe_id(SID)).read_text())

# Every hook that waits for Python waits through the deadline, not only the
# gate: a hung runtime at SessionStart once held the first prompt for Claude
# Code's whole 60 s and left the hook's shell and the hung child behind
# (acceptance_durability's timeout part). HANG61 is told apart from other
# sleeps by its argument.
HANG61 = TMP / "hangpy61.sh"
HANG61.write_text("#!/bin/sh\nexec sleep 61\n")
HANG61.chmod(0o755)
EH = dict(EF, CHECKPOINT_PYTHON=str(HANG61), CHECKPOINT_HOOK_DEADLINE="1")
SH61 = "sess-hang-61"


def hung_left():
    return [r for r in procs.table().values() if r["cmd"].strip() == "sleep 61"]


def timed_hook(event, payload, env):
    """fast.sh with a hung Python: (output, seconds); None output when it ran
    past 20 s (Claude Code's own timeout would have stopped it)."""
    t0 = time.time()
    try:
        r = subprocess.run([SH, str(ROOT / "hooks" / "fast.sh"), event], input=json.dumps(payload),
                           capture_output=True, text=True, timeout=20, env=dict(os.environ, **env))
        out = json.loads(r.stdout) if r.stdout.strip() else {}
    except subprocess.TimeoutExpired:
        out = None
    return out, time.time() - t0


deg61 = SF / "degraded" / state.safe_id(SH61)
out, took = timed_hook("SessionStart", base(session_id=SH61, source="startup"), EH)
check(f"SessionStart, Python hangs: the hook answers at the deadline ({took:.1f} s), not at Claude Code's 60 s, "
      "and says why", out is not None and took < 6 and "didn't answer the session start within 1 s"
      in out.get("systemMessage", ""))
check("...degraded/<session> records the timeout, and the hung child was stopped (no process left behind)",
      lambda: deg61.exists() and "SessionStart: Python timed out" in deg61.read_text() and not hung_left())
(SF / "flags" / state.safe_id(SH61)).write_text("x\n")
out, took = timed_hook("Stop", base(session_id=SH61), EH)
check(f"Stop while paused, Python hangs: the hook ends at the deadline ({took:.1f} s), nothing left running "
      "(no background ingest started after the timeout)",
      lambda: out is not None and took < 6 and "Stop: Python timed out" in deg61.read_text()
      and (time.sleep(0.3) or not hung_left()))
(SF / "flags" / state.safe_id(SH61)).unlink()
out, took = timed_hook("PostToolUse", base(session_id=SH61, tool_name="Read", tool_use_id="h61"),
                       dict(EH, CHECKPOINT_NUDGE_EVERY="1"))
check(f"PostToolUse's reminder, Python hangs: ends at the deadline ({took:.1f} s), recorded, nothing left",
      lambda: out is not None and took < 6 and "PostToolUse: Python timed out" in deg61.read_text()
      and not hung_left())
# A project without a tracker, so Stop takes the background path (with one it
# asks Python in the foreground, as above).
NT61 = TMP / "no-tracker-61"
NT61.mkdir()
out, took = timed_hook("Stop", base(session_id=SH61, cwd=str(NT61)), dict(EH, CHECKPOINT_BG_DEADLINE="2"))
_bg_started = wait_for(lambda: bool(hung_left()), 1.5)
check(f"not paused: Stop's background ingest started, and never holds the hook ({took:.1f} s)",
      out is not None and took < 1.5 and _bg_started)
check("...and a hung background ingest is stopped at CHECKPOINT_BG_DEADLINE (2 s here): no process left",
      lambda: wait_for(lambda: not hung_left(), 8))


def gate_main_with(payload, gate_fn):
    real, real_in, real_out = hooks.gate, sys.stdin, sys.stdout
    hooks.gate = gate_fn
    sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
    try:
        rc = hooks.gate_main()
        return rc, sys.stdout.getvalue()
    finally:
        hooks.gate, sys.stdin, sys.stdout = real, real_in, real_out


def _boom(payload):
    raise RuntimeError("synthetic gate failure")


state.set_paused("sess-gatefail", by="test")
with contextlib.redirect_stderr(io.StringIO()):
    rc, text = gate_main_with({"session_id": "sess-gatefail", "tool_name": "Bash"}, _boom)
check("the Python gate failing while paused denies (fails closed)", rc == 0 and '"deny"' in text)
with contextlib.redirect_stderr(io.StringIO()):
    rc, text = gate_main_with({"session_id": "sess-gate-ok", "tool_name": "Bash"}, _boom)
check("...not paused: no answer, exit 3 (fast.sh lets the call go ahead and records it)", rc == 3 and text == "")
state.clear_paused("sess-gatefail")

print("a pause the mod or the shell recorded (flags/<session> only):")
SID_M = "sess-mod-0001"
EM = {"CLAUDE_PID": str(fake_claude().pid)}
TM = TMP / "tm.jsonl"
TM.write_text("")
pm = {"session_id": SID_M, "transcript_path": str(TM), "cwd": str(PROJ)}
hook("SessionStart", dict(pm, source="startup"), EM)
r = subprocess.run(["sh", str(ROOT / "hooks" / "env.sh"), "flag", "pause", SID_M, "/check-pause"],
                   capture_output=True, text=True, env=os.environ)
fl = state.p("flags", state.safe_id(SID_M))
check("env.sh flag pause writes flags/<session> whole, private (0600), '<time> <who>'",
      r.returncode == 0 and fl.exists() and (fl.stat().st_mode & 0o777) == 0o600
      and fl.read_text().split()[1:] == ["/check-pause"])
first = fl.read_text()
subprocess.run(["sh", str(ROOT / "hooks" / "env.sh"), "flag", "pause", SID_M, "again"], env=os.environ)
check("...and keeps the first pause's time when already paused", fl.read_text() == first)
out = hook("PreToolUse", dict(pm, tool_name="Bash", tool_use_id="m1", tool_input={"command": "ls"}), EM)
rec = state.load_session(SID_M)
check("the Python gate denies, and gives the session record the pause (by whom, since when)",
      denied(out) and rec.get("paused") and rec.get("paused_by") == "/check-pause")
r = subprocess.run([sys.executable, str(ROOT / "run.py"), "sync-pause", SID_M], capture_output=True, text=True)
check("run.py sync-pause (the mod's bookkeeping) writes the checkpoint",
      r.returncode == 0 and json.loads(r.stdout).get("paused") is True
      and state.load_session(SID_M).get("checkpoint_written"))
state.touch(state.p("noresume", state.safe_id(SID_M)), "x")
out = hook("UserPromptSubmit", dict(pm, prompt="routine: nightly check"), EM)
check("a prompt the mod marked noresume doesn't resume (and the marker is used up)",
      state.is_paused(SID_M) and "didn't resume" in ctx(out)
      and not state.p("noresume", state.safe_id(SID_M)).exists())
# A scheduled task or /loop firing a slash command: its prompt is the
# command's line, which the classic hook alone would take as the user's
# (only /check:* commands don't resume). The mod marks it noresume too.
for _line in ("/vfy-skill", "/check:resume"):
    state.touch(state.p("noresume", state.safe_id(SID_M)), "x")
    out = hook("UserPromptSubmit", dict(pm, prompt=_line), EM)
    check(f"...nor a slash command the mod marked noresume ({_line}, as a scheduled task or /loop fires it)",
          state.is_paused(SID_M) and state.p("flags", state.safe_id(SID_M)).exists() and "didn't resume" in ctx(out))
state.touch(state.p("noresume", state.safe_id(SID_M)), "x")
os.utime(state.p("noresume", state.safe_id(SID_M)), (time.time() - 600,) * 2)
out = hook("UserPromptSubmit", dict(pm, prompt="I'm back, go on"), EM)
check("...a stale marker (a mod that died mid-prompt) doesn't stop the user's own message resuming",
      not state.is_paused(SID_M) and "<checkpoint-resume>" in ctx(out))

print("the gate's degraded state is shown, not silent:")
state.touch(state.p("degraded", state.safe_id(SID_M)), "2026-10-19T10:00:00+0000 PreToolUse: its runtime isn't ready (building)")
check("/check:status lists a live session's degraded gate, as history (Python answers it, so it has a runtime)",
      "The gate ran degraded in the last 24 h" in core.status_text()
      and "the gate has its runtime again now" in core.status_text()
      and "runtime isn't ready (building)" in core.status_text())
out = hook("SessionStart", dict(pm, source="resume"), EM)
check("...and the next SessionStart says so once (context and a line for the user)",
      "ran degraded" in ctx(out) and "gate ran degraded" in (out or {}).get("systemMessage", ""))
out = hook("SessionStart", dict(pm, source="resume"), EM)
check("...only once", "ran degraded" not in ctx(out))

print("/check:doctor:")
DOC = subprocess.run(["sh", str(ROOT / "hooks" / "env.sh"), "doctor", SID_M], capture_output=True, text=True,
                     env=dict(E1, CHECKPOINT_HOME=os.environ["CHECKPOINT_HOME"]))
check("env.sh doctor: plugin version, pixi and its version, runtime, env, python, gate, state, contract, journal",
      all(s in DOC.stdout for s in (f"plugin: check {__version__}", "pixi: ", "(0.83.0)",
                                    "runtime: ready", f"env: {ENVD1}", "python: ", "gate: normal", "state: ",
                                    "gateway contract: absent", "spool: ")))
# degraded/<session> stays (until the session is pruned): with the runtime
# ready it is the last time the gate ran degraded, not the gate's state now.
check("...a degraded mark left from before the runtime was ready is history, not the current gate state",
      "gate: normal (the runtime is ready); this session's gate last ran degraded at 2026-10-19T10:00:00+0000 "
      "PreToolUse: its runtime isn't ready (building)" in DOC.stdout and "gate: degraded" not in DOC.stdout)
for ver, want in (("0.82.9", "(0.82.9; older than this version needs (0.83.0): update it with pixi self-update, "
                             "or your package manager (Homebrew, MacPorts))"),
                  ("0.99.1", "(0.99.1)\n")):
    d = subprocess.run(["sh", str(ROOT / "hooks" / "env.sh"), "doctor"], capture_output=True, text=True,
                       env=dict(E1, FAKE_PIXI_VERSION=ver))
    check(f"...pixi {ver}: {want.strip()} (the latest pixi is the supported one: no tested range, no 'newer "
          "than tested')", want in d.stdout and "tested" not in d.stdout)
GWC = Path(os.environ["CHECKPOINT_GATEWAY_CONTRACT"])
GWC.parent.mkdir(parents=True, exist_ok=True)
GWC.write_text("{}")
os.chmod(GWC, 0o600)
d = subprocess.run(["sh", str(ROOT / "hooks" / "env.sh"), "doctor"], capture_output=True, text=True, env=E1)
check("...the gateway's contract file (CHECKPOINT_GATEWAY_CONTRACT in tests) is checked as Python's discovery "
      "does: '{}' names no daemon pid, so not available (dead); tests/gateway_tests.py covers every status",
      "gateway contract: dead: its contract file names no daemon pid" in d.stdout)
GWC.unlink()
# env.sh contract-path: where the mod's pause looks for the gateway (0.7.3; it
# used $HOME, so with HOME elsewhere the ack said the gateway wasn't told). It
# only prints the path (mods can't read the password database).


def contract_path_sh(env, cwd=None):
    return subprocess.run([SH, str(ROOT / "hooks" / "env.sh"), "contract-path"], capture_output=True, text=True,
                          env=env, cwd=cwd)


_cp = contract_path_sh(E1)
check("env.sh contract-path: CHECKPOINT_GATEWAY_CONTRACT when set (the tests' scratch path), as gateway.contract_path",
      _cp.returncode == 0 and _cp.stdout == os.environ["CHECKPOINT_GATEWAY_CONTRACT"] + "\n"
      and gateway.contract_path() == os.environ["CHECKPOINT_GATEWAY_CONTRACT"])
_cp = contract_path_sh(dict(E1, CHECKPOINT_GATEWAY_CONTRACT=""))
check("...set but empty: an empty line (the integration is off)", _cp.returncode == 0 and _cp.stdout == "\n")
_cp = contract_path_sh(dict(E1, CHECKPOINT_GATEWAY_CONTRACT="rel dir/c.json"), cwd=str(TMP))
check("...a relative one: from the current folder (Python's abspath)",
      _cp.stdout.strip() == os.path.join(os.path.realpath(TMP), "rel dir", "c.json")
      or _cp.stdout.strip() == os.path.join(str(TMP), "rel dir", "c.json"))
_e = {k: v for k, v in E1.items() if k != "CHECKPOINT_GATEWAY_CONTRACT"}
_e["HOME"] = str(TMP / "not-home")
_cp = contract_path_sh(_e)
_gwc, _home = os.environ.pop("CHECKPOINT_GATEWAY_CONTRACT"), os.environ.get("HOME")
os.environ["HOME"] = str(TMP / "not-home")
try:
    _py = gateway.contract_path()   # computes the path only; nothing looks at the file
finally:
    os.environ["CHECKPOINT_GATEWAY_CONTRACT"] = _gwc
    if _home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = _home
_real_cp = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".config", "gateway", "run", "contract.json")
check("...unset, with HOME elsewhere: ~/.config/gateway/run/contract.json in the password database's home, the "
      "same path as gateway.contract_path (and the gateway), not $HOME's (computed only)",
      _cp.returncode == 0 and _cp.stdout.strip() == _real_cp == _py and "not-home" not in _cp.stdout)
check("...the mod asks env.sh contract-path (no $HOME guess left in it)",
      "'contract-path'" in (ROOT / "hooks" / "mod" / "register.tsx").read_text()
      and "/.config/gateway/run/contract.json" not in (ROOT / "hooks" / "mod" / "register.tsx").read_text())
out = hook("UserPromptSubmit", dict(pm, prompt="/check:doctor"), EM)
check("/check:doctor through the hook: the same report, with Python's own line",
      "checkpoint " + __version__ + " doctor" in ctx(out) and "runtime:" in ctx(out))
toml = (ROOT / "pixi.toml").read_text()
check("pixi.toml requires-pixi >=0.83.0 with no upper bound (the pixi that made the lock), as lib.sh's CK_PIXI_MIN; "
      "python unpinned (the newest from conda-forge)",
      re.search(r'^requires-pixi = ">=0\.83\.0"$', toml, re.M) is not None
      and "CK_PIXI_MIN=0.83.0" in (ROOT / "hooks" / "lib.sh").read_text()
      and re.search(r'^python = "\*"$', toml, re.M) is not None)
shipped = [p for p in ROOT.rglob("*") if p.is_file() and ".pixi" not in p.parts and "tests" not in p.parts
           and "__pycache__" not in p.parts and ".claude-plugin/types" not in str(p)]
check("--locked builds the shipped env; nothing shipped uses --frozen or `pixi run`",
      "install --locked --manifest-path" in (ROOT / "hooks" / "lib.sh").read_text()
      and not [p for p in shipped if "--frozen" in read_or_empty(p)]
      and not [p for p in shipped if p.suffix in (".sh", ".json") and "pixi.sh\" run" in read_or_empty(p)])

print("checkpoint's state folder is private:")
SP = TMP / "state-private"
SP.mkdir(mode=0o755)        # made by someone else, readable: made private
EP ={"CHECKPOINT_HOME": str(SP), "CLAUDE_PID": str(fake_claude().pid)}
SIDP = "sess-private"
pp = {"session_id": SIDP, "transcript_path": str(TM), "cwd": str(PROJ)}
hook("SessionStart", dict(pp, source="startup"), EP)
hook("UserPromptSubmit", dict(pp, prompt="/check:pause"), EP)
hook("PreToolUse", dict(pp, tool_name="Bash", tool_use_id="p1", tool_input={"command": "ls"}), EP)
hook("UserPromptSubmit", dict(pp, prompt="back"), EP)
for i in range(3):
    hook("PostToolUse", dict(pp, tool_name="Read", tool_use_id=f"p{i}"), EP)
WDP = TMP / "jobwd-private"
WDP.mkdir()
srv = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, text=True, env=dict(os.environ, CHECKPOINT_HOME=str(SP),
                                                                      CHECKPOINT_USER_UMASK="0022"))
for msg_ in ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "job_start", "arguments": {
                 "name": "modes", "command": "echo made > made.txt; echo out", "cwd": str(WDP), "redo_hint": "x"}}}):
    srv.stdin.write(json.dumps(msg_) + "\n")
    srv.stdin.flush()
    srv.stdout.readline()
wait_for(lambda: (WDP / "made.txt").exists(), 10)
time.sleep(0.5)
srv.stdin.close()
srv.wait(timeout=10)
bad = []
for dirpath, dirnames, filenames in os.walk(SP):
    for n in dirnames:
        if (os.stat(os.path.join(dirpath, n)).st_mode & 0o777) != 0o700:
            bad.append(os.path.join(dirpath, n))
    for n in filenames:
        want = 0o700 if (dirpath == str(SP) and n == "checkpoint") else 0o600
        if (os.lstat(os.path.join(dirpath, n)).st_mode & 0o777) != want:
            bad.append(f"{os.path.join(dirpath, n)} {oct(os.lstat(os.path.join(dirpath, n)).st_mode & 0o777)}")
if bad:
    print("       (" + ", ".join(bad[:6]) + ")")
check("CHECKPOINT_HOME 0700, every folder 0700, every file 0600 (the helper 0700), after hooks and a job",
      (SP.stat().st_mode & 0o777) == 0o700 and not bad)
jlogs = list(SP.glob("jobs/*/output.log"))
check("...the job's log 0600, its exit code written into the private file",
      jlogs and all((p.stat().st_mode & 0o777) == 0o600 for p in jlogs)
      and all((p.parent / "exit_code").read_text().strip() == "0" for p in jlogs))
check("...while the job itself ran with the user's umask (its file 0644 under umask 022)",
      ((WDP / "made.txt").stat().st_mode & 0o777) == 0o644)

print("the mod (hooks/mod/), checked against the Python side:")
LOGIC = read_or_empty(ROOT / "hooks" / "mod" / "logic.ts")
_allow_ts = re.search(r"ALLOW_WHILE_PAUSED: readonly string\[\] = \[(.*?)\]", LOGIC, re.S)
check("the mod's ALLOW_WHILE_PAUSED is state.ALLOW_WHILE_PAUSED",
      _allow_ts is not None and re.findall(r"'([^']+)'", _allow_ts.group(1)) == list(state.ALLOW_WHILE_PAUSED))
check("...MAX_DENIES too", f"MAX_DENIES = {state.MAX_DENIES}" in LOGIC)
check("...and safeId is state.safe_id's rule", "replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 128) || '_'" in LOGIC)
check("hooks.json loads the mod and keeps every classic hook",
      _hooks_json.get("modules") == ["./mod/register.tsx"]
      and set(_hooks_json["hooks"]) >= {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
                                         "PostToolUseFailure", "Stop"})
REG = read_or_empty(ROOT / "hooks" / "mod" / "register.tsx")
check("the mod registers /check-pause and /check-doctor as immediate commands, and its gate has a .catch that denies",
      "name: 'check-pause'" in REG and "name: 'check-doctor'" in REG and REG.count("immediate: true") >= 2
      and ".catch(($, e, next) => (next.called ? next(e) : { deny: GATE_FAILED }))" in REG)
check("the mod's state paths are state.py's (flags/, rt/, jobs.json; CHECKPOINT_HOME, XDG_STATE_HOME)",
      all(s in REG for s in ("/flags/", "/rt/", "/jobs.json", "'CHECKPOINT_HOME'", "'XDG_STATE_HOME'",
                             "/.local/state/checkpoint")))
CLAUDE = shutil.which("claude")
if CLAUDE is None:
    print("  note: claude not found; `claude plugin test` and `claude plugin validate` not run")
else:
    # Never the real Claude config: a scratch CLAUDE_CONFIG_DIR, no network.
    CC = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}
    CC.update(CLAUDE_CONFIG_DIR=str(TMP / "claude-config-plugin-test"), ANTHROPIC_BASE_URL="http://127.0.0.1:9",
              CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", CHECKPOINT_HOME=str(TMP / "state-cc"))
    r = subprocess.run([CLAUDE, "plugin", "test", str(ROOT)], capture_output=True, text=True, timeout=300, env=CC)
    tail = (r.stdout + r.stderr).strip().splitlines()[-4:]
    if r.returncode != 0:
        print("       (" + " / ".join(tail) + ")")
    check("`claude plugin test` runs the mod's tests (tests/mod/*.test.ts): all pass",
          r.returncode == 0 and " 0 fail" in r.stdout + r.stderr)
    for target, what in ((ROOT, "the plugin"), (REPO, "the marketplace")):
        r = subprocess.run([CLAUDE, "plugin", "validate", str(target)], capture_output=True, text=True,
                           timeout=120, env=CC)
        check(f"`claude plugin validate` passes for {what}", r.returncode == 0 and "Validation passed" in r.stdout)

# --- dash ------------------------------------------------------------------------------
if Path("/bin/dash").exists() and SH != "/bin/dash":
    print("the hooks under dash:")
    SIDS = "sess-dash"
    ED = {"CLAUDE_PID": str(fake_claude().pid)}
    TS = TMP / "ts.jsonl"
    TS.write_text("")
    pd = {"session_id": SIDS, "transcript_path": str(TS), "cwd": str(PROJ)}
    check("SessionStart", "TRACKER.md" in ctx(hook("SessionStart", dict(pd, source="startup"), ED, "/bin/dash")))
    n0 = pycalls()
    check("fast path: no output, no Python", hook("PreToolUse", dict(pd, tool_name="Read", tool_use_id="d1",
          tool_input={"file_path": "x"}), ED, "/bin/dash") is None and pycalls() == n0)
    check("job tool allowed", allowed(hook("PreToolUse", dict(pd, tool_name="mcp__plugin_check_jobs__job_tail"),
                                           ED, "/bin/dash")))
    hook("UserPromptSubmit", dict(pd, prompt="/check:pause"), ED, "/bin/dash")
    check("pause, then Bash denied", denied(hook("PreToolUse", dict(pd, tool_name="Bash", tool_use_id="d2",
          tool_input={"command": "ls"}), ED, "/bin/dash")))
    check("any message resumes", "<checkpoint-resume>" in ctx(hook("UserPromptSubmit", dict(pd, prompt="go"),
                                                                    ED, "/bin/dash")))
    for _ in range(3):
        out = hook("PostToolUse", dict(pd, tool_name="Read", tool_use_id="d3"), ED, "/bin/dash")
    check("PostToolUse counts to the reminder", "tool calls" in ctx(out))
    clear_running()

# --- terminal and files ---------------------------------------------------------------
print("terminal CLI:")
r = subprocess.run([sys.executable, str(ROOT / "run.py"), "pause", "now"], capture_output=True, text=True)
check("`pause now` without a target refused", r.returncode == 2)
r = subprocess.run([sys.executable, str(ROOT / "run.py"), "pause", "all"], capture_output=True, text=True)
check("`pause all` from the terminal pauses the running sessions", r.returncode == 0 and state.is_paused(SID2))
r = subprocess.run([sys.executable, str(ROOT / "run.py"), "resume", "all"], capture_output=True, text=True)
check("`resume all` from the terminal", r.returncode == 0 and not state.is_paused(SID2)
      and state.resume_pending(SID2))

print("plugin files:")
manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
check("plugin.json version matches the package's __version__", manifest["version"] == __version__)
check("plugin.json names its repository", manifest.get("repository", "").startswith("https://github.com/"))
hooks_cfg = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
check("PreToolUse matcher is '*'", hooks_cfg["PreToolUse"][0]["matcher"] == "*")
check("PostToolUseFailure hooked (killed commands report as failures)", "PostToolUseFailure" in hooks_cfg)
check("every hook runs fast.sh in exec form (sh + args, no shell quoting); SessionStart also builds the env (async)",
      all(h["command"] == "sh" and (h["args"] == ["${CLAUDE_PLUGIN_ROOT}/hooks/fast.sh", ev]
                                    or (ev == "SessionStart" and h.get("async") is True
                                        and h["args"] == ["${CLAUDE_PLUGIN_ROOT}/hooks/env.sh", "ensure"]))
          for ev, groups in hooks_cfg.items() for g in groups for h in g["hooks"])
      and any(h.get("async") for h in hooks_cfg["SessionStart"][0]["hooks"]))
for c in ("pause", "resume", "status", "help"):
    check(f"/check:{c} is a skill with disable-model-invocation",
          lambda c=c: "disable-model-invocation: true" in (ROOT / "skills" / c / "SKILL.md").read_text())
check("pre-filter tail size matches transcript.TAIL_BYTES",
      f"tail -c {transcript.TAIL_BYTES}" in (ROOT / "hooks" / "fast.sh").read_text())
check("no settings files, installer or bin/ in the plugin",
      not any((ROOT / x).exists() for x in ("config", "install.py", "bin", "settings.json"))
      and not (REPO / "settings.rules.json").exists())
check("no install task in pixi.toml", "install" not in (ROOT / "pixi.toml").read_text().split("[tasks]")[1])

# --- release check ------------------------------------------------------------------
# Installs get only what is committed: an untracked hooks/pixi.sh or skills/ once
# sat in a finished working tree that `git commit -a` would have shipped without
# them. tests/release_check.py (pixi run release-check) says whether the index
# holds the tested tree. Checked on throwaway repos; git here never reads the
# user's config (hooks, excludes) and never touches this repo's index.
print("release check (a commit ships only what is in the index):")
RC = ROOT / "tests" / "release_check.py"
GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
GIT_ENV.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_CEILING_DIRECTORIES=str(TMP.resolve()))


def release_check(where=None, env=GIT_ENV):
    r = subprocess.run([sys.executable, str(RC)] + ([str(where)] if where else []),
                       capture_output=True, text=True, timeout=60, env=env)
    return r.returncode, r.stdout + r.stderr


def tgit(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=30, env=GIT_ENV)


if shutil.which("git") is None:
    print("  note: git not found; release check not tested")
else:
    RREPO = TMP / "release-repo"
    RP = RREPO / "plugins" / "checkpoint"
    for rel, text in {".claude-plugin/marketplace.json": "{}\n", "README.md": "readme\n",
                      "plugins/checkpoint/.gitignore": ".pixi/\n__pycache__/\n*.pyc\n",
                      "plugins/checkpoint/.mcp.json": "{}\n", "plugins/checkpoint/hooks/fast.sh": "exit 0\n",
                      "plugins/checkpoint/commands/help.md": "old\n", "plugins/checkpoint/install.py": "old\n",
                      ".gitignore": ".work/\n.DS_Store\n"}.items():
        (RREPO / rel).parent.mkdir(parents=True, exist_ok=True)
        (RREPO / rel).write_text(text)
    tgit(RREPO, "init", "-q")
    tgit(RREPO, "add", "-A")
    code, out = release_check(RP)
    check("everything staged: ready (exit 0), found from a folder inside the repo", code == 0 and "ready" in out)
    # The release this guards: new files the plugin starts, and old files it dropped.
    (RP / "hooks" / "pixi.sh").write_text("exec pixi \"$@\"\n")
    (RP / "skills" / "help").mkdir(parents=True)
    (RP / "skills" / "help" / "SKILL.md").write_text("---\nname: help\n---\n")
    (RP / "commands" / "help.md").unlink()
    (RP / "install.py").unlink()
    (RP / ".mcp.json").write_text('{"mcpServers": {}}\n')
    code, out = release_check(RREPO)
    check("untracked hooks/pixi.sh and skills/: not ready (exit 1), each named, with the git add fix",
          code == 1 and "untracked" in out and "plugins/checkpoint/hooks/pixi.sh" in out
          and "plugins/checkpoint/skills/help/SKILL.md" in out and "git add -A" in out)
    check("...unstaged deletions named (a commit would still ship commands/, install.py)",
          "deleted, not staged" in out and "plugins/checkpoint/commands/help.md" in out
          and "plugins/checkpoint/install.py" in out)
    check("...unstaged changes named", "modified, not staged" in out and "plugins/checkpoint/.mcp.json" in out)
    check("...a deleted file isn't also listed as modified",
          lambda: "commands/help.md" not in next(ln for ln in out.splitlines() if "modified, not staged" in ln))
    tgit(RREPO, "add", "plugins/checkpoint/hooks/pixi.sh", "plugins/checkpoint/skills")
    code, out = release_check(RREPO)
    check("new files added but deletions and changes left unstaged (git add <new files> alone): still not ready",
          code == 1 and "untracked" not in out and "deleted, not staged" in out and "modified, not staged" in out)
    tgit(RREPO, "add", "-u")
    _index = (RREPO / ".git" / "index").read_bytes()
    code, out = release_check(RREPO)
    check("after git add -A (here: git add <new> + git add -u): ready", code == 0)
    # What Claude Code writes when it loads the mod from the folder (git-ignored there).
    (RP / ".gitignore").write_text(".pixi/\n__pycache__/\n*.pyc\n.claude-plugin/types/\n/tsconfig.json\n")
    tgit(RREPO, "add", "-A")
    _index = (RREPO / ".git" / "index").read_bytes()
    # Caches pixi and Python rebuild, Finder's .DS_Store and .work/ are ignored on purpose.
    for junk in (".pixi/envs/default/bin/python", "src/checkpoint/__pycache__/core.cpython-312.pyc",
                 "skills/.DS_Store"):
        (RP / junk).parent.mkdir(parents=True, exist_ok=True)
        (RP / junk).write_text("x")
    (RREPO / ".work" / "t").mkdir(parents=True)
    (RREPO / ".work" / "t" / "TRACKER.md").write_text("x")
    os.utime(RP / "hooks" / "fast.sh", (1, 1))   # stat-dirty only: tempts git into refreshing the index
    (RP / ".claude-plugin" / "types" / "claude-code").mkdir(parents=True)
    (RP / ".claude-plugin" / "types" / "claude-code" / "index.d.ts").write_text("x")
    (RP / "tsconfig.json").write_text("{}")
    code, out = release_check(RREPO)
    check("ignored caches (.pixi/, __pycache__/, *.pyc), .DS_Store, .work/ and Claude Code's mod types "
          "(.claude-plugin/types/, the plugin's tsconfig.json) don't count", code == 0)
    check("the check never writes the index (not even a stat refresh)",
          (RREPO / ".git" / "index").read_bytes() == _index)
    # A file the plugin needs that a .gitignore hides: git add -A would skip it.
    (RP / ".gitignore").write_text(".pixi/\n__pycache__/\n*.pyc\nhooks/local*.sh\n")
    tgit(RREPO, "add", "-A")
    (RP / "hooks" / "local-pixi.sh").write_text("x")
    code, out = release_check(RREPO)
    check("a non-cache file ignored in the plugin folder: not ready, named, with the .gitignore hint",
          code == 1 and "plugins/checkpoint/hooks/local-pixi.sh" in out and ".gitignore" in out)
    (TMP / "not-a-repo").mkdir()
    code, out = release_check(TMP / "not-a-repo")
    check("outside a git work tree (an installed copy): can't tell (exit 2), not 'ready'",
          code == 2 and "ready" not in out.replace("not ready", ""))
    # A note for whoever commits this repo (read-only; never fails the run).
    code, out = release_check(env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    if code == 1:
        print("  note: " + "\n        ".join(out.strip().splitlines()))
        print("        (`pixi run release-check` before committing)")

# --- the pause-time checkpoint: in the state folder, never in the user's project -----------
# It goes into Claude's context at a resume: nothing unscrubbed goes in. The
# user's repository is never written: no file next to the tracker, no line in
# the clone's exclude file. Synthetic secrets, never printed.
print("the pause-time checkpoint: in the state folder, scrubbed; the user's repository untouched:")
if shutil.which("git") is None:
    print("  note: git not found; the repository checks not run")
else:
    import secrets as _sec
    import string as _str

    def _canary(n):
        return "".join(_sec.choice(_str.ascii_letters + _str.digits) for _ in range(n))

    CPJ_GHP, CPJ_PW, CPJ_TOK, CPJ_JOB, CPJ_INT = ("ghp_" + _canary(36), _canary(16), _canary(24), "ghp_" + _canary(36),
                                                  _canary(16))
    CPJ_ALL = (CPJ_GHP, CPJ_PW, CPJ_TOK, CPJ_JOB, CPJ_INT)
    # git in the hook never reads the user's config.
    CPJ_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CEILING_DIRECTORIES": str(TMP.resolve())}

    def cpj_project(name, sid):
        proj = TMP / name
        (proj / ".work" / "deploy").mkdir(parents=True)
        tgit(proj, "init", "-q")
        (proj / ".work" / "deploy" / "TRACKER.md").write_text(
            f"# Deploy\nSession: {sid}\n\n## Stages\n| # | Stage | Status | N |\n|---|---|---|---|\n"
            "| 1 | Release | in progress | |\n")
        tp = TMP / f"{name}.jsonl"
        tp.write_text("")
        for i, (tool, inp) in enumerate((("Bash", {"command": f"export GH_TOKEN={CPJ_GHP}"}),
                                         ("Bash", {"command": f"mysql -u admin -p{CPJ_PW} prod < m.sql"}),
                                         ("mcp__other__call", {"token": CPJ_TOK, "q": "deploy"}))):
            tline({"type": "assistant", "timestamp": f"2026-10-16T02:00:0{i}.000Z", "message": {"content": [
                {"type": "tool_use", "id": f"{name}-{i}", "name": tool, "input": inp}]}}, tp)
        return proj, tp

    def cpj_pause(proj, tp, sid, before=None):
        pl = {"session_id": sid, "transcript_path": str(tp), "cwd": str(proj)}
        hook("SessionStart", dict(pl, hook_event_name="SessionStart", source="startup"), CPJ_ENV)
        if before:
            before()
        hook("UserPromptSubmit", dict(pl, prompt="/check:pause"), CPJ_ENV)
        return hook("PreToolUse", dict(pl, hook_event_name="PreToolUse", tool_name="Bash", tool_use_id=f"{sid}-x",
                                       tool_input={"command": "ls"}), CPJ_ENV)

    def excl(proj):
        p = proj / ".git" / "info" / "exclude"
        return p.read_text() if p.exists() else ""

    def tree(proj):
        """Every file in the project but .git's own (what the hooks could have written)."""
        return sorted(str(p.relative_to(proj)) for p in proj.rglob("*") if p.is_file() and ".git" not in p.parts)

    CPJ_SID = "sess-cpjson-5555-6666"
    CPJ, CPJ_TP = cpj_project("cpjson-proj", CPJ_SID)
    _excl0, _tree0 = excl(CPJ), tree(CPJ)
    _cpj_job = jobs.start("cpj-deploy", command=f"API_TOKEN={CPJ_JOB} sleep 30", cwd=str(CPJ), session_id=CPJ_SID)
    # What `pause now` records of a command it stopped (core.kill_foreground).
    _out = cpj_pause(CPJ, CPJ_TP, CPJ_SID, lambda: state.add_interrupted(CPJ_SID, [{
        "command": f"curl -u ops:{CPJ_INT} https://deploy.example/run", "tool_use_id": "cpj-k1",
        "at": state.now_iso(), "processes": 1}]))
    CPJ_FILE = cp_file(CPJ_SID)
    _txt = CPJ_FILE.read_text() if CPJ_FILE.exists() else ""
    _cp = jread(CPJ_FILE)
    _cpj_task = state.load_session(CPJ_SID).get("task_id") or "?"
    check("a pause writes the checkpoint in the state folder, next to the task's journal "
          "(journal/<task>/checkpoint-<session>.json), naming the session's tracker",
          lambda: denied(_out) and _cp.get("session_id") == CPJ_SID
          and CPJ_FILE == state.p("journal", state.safe_id(_cpj_task), f"checkpoint-{state.safe_id(CPJ_SID)}.json")
          and _cp.get("tracker") == str(CPJ / ".work" / "deploy" / "TRACKER.md")
          and _cp.get("tracker_key") == core.tracker_key(str(CPJ / ".work" / "deploy" / "TRACKER.md"))
          and state.load_session(CPJ_SID).get("tracker") == str(CPJ / ".work" / "deploy" / "TRACKER.md"))
    check("...and nothing in the user's repository: no file in the project (the tracker folder holds only the "
          "tracker), the clone's exclude file as git init left it",
          lambda: tree(CPJ) == _tree0 == [".work/deploy/TRACKER.md"] and excl(CPJ) == _excl0
          and ".work" not in excl(CPJ))
    check("...with no secret in it: not the last calls' commands, an MCP call's input, the jobs' commands or the "
          "interrupted ones (raw or JSON-escaped)",
          lambda: _txt and not any(v in _txt or json.dumps(v)[1:-1] in _txt for v in CPJ_ALL))
    check("...and what a resume reads stays: the calls, the job and the interrupted command, scrubbed",
          lambda: [c["input"] for c in _cp["last_tool_calls"]][-3:-1] == ["export GH_TOKEN=***",
                                                                           "mysql -u admin -p*** prod < m.sql"]
          and _cp["last_tool_calls"][-2]["tool"] == "Bash"
          and any(j.get("name") == "cpj-deploy" and j.get("command") == "API_TOKEN=*** sleep 30" and j.get("state")
                  for j in _cp["jobs"])
          and not any("log" in j for j in _cp["jobs"])
          and _cp["interrupted"][0]["command"] == "curl -u ops:*** https://deploy.example/run"
          and _cp["interrupted"][0]["tool_use_id"] == "cpj-k1")
    time.sleep(1.1)
    hook("Stop", {"session_id": CPJ_SID, "transcript_path": str(CPJ_TP), "cwd": str(CPJ), "stop_hook_active": False},
         CPJ_ENV)
    check("...refreshed in place (Stop while paused), the repository still untouched",
          lambda: jread(CPJ_FILE).get("written_at") != _cp.get("written_at") and cp_file(CPJ_SID) == CPJ_FILE
          and tree(CPJ) == _tree0 and excl(CPJ) == _excl0)
    _resumed = hook("UserPromptSubmit", {"session_id": CPJ_SID, "transcript_path": str(CPJ_TP), "cwd": str(CPJ),
                                         "prompt": "go on"}, CPJ_ENV)
    check("the resume context reads the scrubbed checkpoint (the interrupted command, the last calls)",
          lambda: "curl -u ops:***" in ctx(_resumed) and "mysql -u admin -p***" in ctx(_resumed)
          and not any(v in ctx(_resumed) for v in CPJ_ALL))
    # The tracker's checkpoint for a session that picks the tracker up by its slug.
    CPJ_SID2 = "sess-cpjson-9999-0000"
    state.update_session(CPJ_SID2, cwd=str(CPJ), claude_pid=fake_claude().pid, created_ts=time.time())
    _picked = core.resume_task(str(CPJ), "deploy", sid=CPJ_SID2)
    check("/check:resume <tracker> in another session: the tracker's pause-time checkpoint comes along from the "
          "state folder (its interrupted command, scrubbed)",
          lambda: "Tracker: " + str(CPJ / ".work" / "deploy" / "TRACKER.md") in _picked
          and "curl -u ops:***" in _picked and "Checkpoint at pause" in _picked
          and not any(v in _picked for v in CPJ_ALL))
    # Two sessions paused on one tracker (it names no session; both wrote it), one of them through a
    # symlinked path, in a project whose name the scrubber masks.
    from checkpoint import purge as _cpk_purge
    CPK_REAL = TMP / "cpk-real" / "password=reset"
    CPK_TRK = CPK_REAL / ".work" / "ship" / "TRACKER.md"
    CPK_TRK.parent.mkdir(parents=True)
    CPK_TRK.write_text("# Ship\nSession: <session id>\n\n## Stages\n| # | Stage | Status | N |\n|---|---|---|---|\n"
                       "| 1 | Ship | in progress | |\n")
    (TMP / "cpk-link").symlink_to(TMP / "cpk-real")
    CPK_OLD, CPK_NEW = "sess-cpkey-old-1111", "sess-cpkey-new-2222"
    _cpk = {}
    for _s, _proj in ((CPK_OLD, CPK_REAL), (CPK_NEW, TMP / "cpk-link" / "password=reset")):
        state.update_session(_s, cwd=str(_proj), created_ts=time.time())
        state.touch(state.p("trackers", _s), str(_proj / ".work" / "ship" / "TRACKER.md") + "\n")
        state.set_paused(_s, by="test")
        _cpk[_s] = core.write_checkpoint(_s)
    _cpk_t = tracker.by_slug(str(CPK_REAL), "ship")

    def _cpk_age(sid, seconds):
        os.utime(_cpk[sid], (time.time() - seconds, time.time() - seconds))
        return (core.tracker_checkpoint(_cpk_t) or {}).get("session_id") if _cpk_t else None

    _cpk_cps = {s: jread(f) for s, f in _cpk.items()}
    check("/check:resume <tracker>: the newest of two sessions' checkpoints for it, found by the resolved path's "
          "key (core.tracker_key) though one session wrote it through a symlink and both paths are scrubbed",
          lambda: _cpk_age(CPK_OLD, 60) == CPK_NEW
          and all(c.get("tracker_key") == core.tracker_key(str(CPK_TRK)) and c.get("tracker")
                  and "reset" not in c["tracker"] for c in _cpk_cps.values()))
    check("...and the other session's once it is the newer", lambda: _cpk_age(CPK_NEW, 120) == CPK_OLD)
    check("...forget and purge name the tracker folder once, by its real path (the session record's `tracker`)",
          lambda: _cpk_purge._work_folders([state.load_session(s) for s in _cpk]) == [str(CPK_TRK.parent)])
    for _s in _cpk:
        state.clear_paused(_s)
    _early =state.p("checkpoints", state.safe_id("sess-cpjson-early") + ".json")
    state.update_session("sess-cpjson-early", cwd=str(CPJ), paused=True)
    state.touch(state.p("flags", "sess-cpjson-early"), state.now_iso())
    _p_early = core.write_checkpoint("sess-cpjson-early")
    state.update_session("sess-cpjson-early", task_id=_cpj_task)
    _p_later = core.write_checkpoint("sess-cpjson-early")
    check("a session whose task has no journal yet: checkpoints/<session>.json; once it has one, the checkpoint "
          "moves next to the journal and the early file goes",
          lambda: _p_early == _early and _p_later.parent == state.p("journal", state.safe_id(_cpj_task))
          and not _early.exists() and _p_later.exists())
    state.clear_paused("sess-cpjson-early")
    if _cpj_job.get("ok"):
        jobs.stop(_cpj_job["job"]["id"], "test")
    state.clear_paused(CPJ_SID)

# --- the journal (PLAN-v4 phase 2): its own file, sharing this suite's helpers ----------
print("the journal (tests/journal_tests.py):")
exec(compile((ROOT / "tests" / "journal_tests.py").read_text(), str(ROOT / "tests" / "journal_tests.py"), "exec"))
# --- recovery (PLAN-v4 phase 3): its own file, after the journal's (it uses its helpers) -------
print("recovery (tests/recovery_tests.py):")
exec(compile((ROOT / "tests" / "recovery_tests.py").read_text(), str(ROOT / "tests" / "recovery_tests.py"), "exec"))
# --- the gateway integration (PLAN-v4 phase 4, CONTRACT-v1): after recovery's (it uses its helpers) -----
print("the gateway integration (tests/gateway_tests.py):")
exec(compile((ROOT / "tests" / "gateway_tests.py").read_text(), str(ROOT / "tests" / "gateway_tests.py"), "exec"))

print("hosts, authorizations and pause retries (tests/hosts_authz_tests.py):")
exec(compile((ROOT / "tests" / "hosts_authz_tests.py").read_text(), str(ROOT / "tests" / "hosts_authz_tests.py"),
             "exec"))

# --- the user-facing fixes of the 0.7.3 review: last (it uses only this file's helpers) ---------
print("the spool without a runtime, status, crash notice, forget/purge, trackers off (tests/ux_tests.py):")
exec(compile((ROOT / "tests" / "ux_tests.py").read_text(), str(ROOT / "tests" / "ux_tests.py"), "exec"))

check("hermetic, at the end too: no job of this run had an env file outside the temp dir",
      all(not j.get("gateway_env_file") or j["gateway_env_file"].startswith(str(TMP))
          for j in jobs._registry()["jobs"].values())
      and os.environ.get("CHECKPOINT_GATEWAY_ENV") == GW_DEFAULT)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
