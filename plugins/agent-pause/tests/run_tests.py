#!/usr/bin/env python3
"""Self-contained tests for agent-pause. Pure stdlib; run with:
    pixi run test
Hermetic: state goes to a temp AGENT_PAUSE_HOME, hooks run through the real
hooks/fast.sh with AGENT_PAUSE_PYTHON pointing at this interpreter (through a
wrapper that counts calls), and process tests use their own fake trees.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TMP = Path(tempfile.mkdtemp(prefix="agent-pause-test-"))
os.environ["AGENT_PAUSE_HOME"] = str(TMP / "state")
os.environ["AGENT_PAUSE_CONFIG"] = str(TMP / "no-config.json")
os.environ["AGENT_PAUSE_NUDGE_EVERY"] = "3"
os.environ["AGENT_PAUSE_WATCH_INTERVAL"] = "0.2"
PYCALLS = TMP / "pycalls"
WRAP = TMP / "py.sh"
WRAP.write_text(f'#!/bin/sh\necho "$*" >> "{PYCALLS}"\nexec "{sys.executable}" "$@"\n')
WRAP.chmod(0o755)
os.environ["AGENT_PAUSE_PYTHON"] = str(WRAP)

from agentpause import core, jobs, mcp_server, procs, state, tracker, transcript  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


def pycalls():
    return len(PYCALLS.read_text().splitlines()) if PYCALLS.exists() else 0


def hook(event, payload):
    """Run the real pre-filter. Returns parsed JSON output or None."""
    r = subprocess.run(["sh", str(ROOT / "hooks" / "fast.sh"), event, str(ROOT)],
                       input=json.dumps(payload), capture_output=True, text=True, timeout=60)
    out = r.stdout.strip()
    return json.loads(out) if out else None


def ctx(out):
    return ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""


def denied(out):
    return ((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"


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


def tline(obj):
    with open(TRANSCRIPT, "a") as f:
        f.write(json.dumps(obj, separators=(",", ":")) + "\n")


def enqueue(content, ts):
    tline({"type": "queue-operation", "operation": "enqueue", "timestamp": ts,
           "sessionId": SID, "content": content})


def dequeue(ts):
    tline({"type": "queue-operation", "operation": "dequeue", "timestamp": ts, "sessionId": SID})


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


# --- parsing --------------------------------------------------------------------
print("command and keyword parsing:")
check("/agent-pause:pause", transcript.parse_command("/agent-pause:pause") == ("pause", []))
check("/agent-pause:pause now --stop-jobs",
      transcript.parse_command("/agent-pause:pause now --stop-jobs") == ("pause", ["now", "--stop-jobs"]))
check("keyword 'pause'", transcript.parse_command(" Pause! ") == ("pause", []))
check("keyword 'pause all now'", transcript.parse_command("pause all now") == ("pause", ["all", "now"]))
check("a sentence is not a keyword", transcript.parse_command("pause the video then continue") is None)
check("other plugin command ignored", transcript.parse_command("/net-gateway:secret list") is None)
check("/agent-pause:resume build", transcript.parse_command("/agent-pause:resume build") == ("resume", ["build"]))

print("transcript queue replay (format pinned from Spike 1):")
SPIKE = [
    '{"type":"queue-operation","operation":"enqueue","timestamp":"2026-10-16T01:05:22.438Z","sessionId":"6bb8","content":"/spike-pause"}',
    '{"type":"queue-operation","operation":"dequeue","timestamp":"2026-10-16T01:06:01.167Z","sessionId":"6bb8"}',
    '{"type":"queue-operation","operation":"enqueue","timestamp":"2026-10-16T00:52:36.292Z","sessionId":"a205","content":"Note from the user"}',
    '{"type":"queue-operation","operation":"remove","timestamp":"2026-10-16T00:52:41.378Z","sessionId":"a205","content":"Note from the user"}',
]
check("consumed and removed entries leave nothing pending", transcript.pending_queue(SPIKE) == [])
q = [SPIKE[0].replace("/spike-pause", "/agent-pause:pause now")]
hit = transcript.queued_pause(q)
check("pending pause found with args", hit is not None and hit[1] == ["now"])
check("FIFO: dequeue pops the oldest", transcript.pending_queue(
    [q[0], q[0].replace("now", "all"), SPIKE[1]])[0]["content"] == "/agent-pause:pause all")
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
check("tracker found by session id", tracker.for_session(PROJ, SID)["slug"] == "train-model")
check("by slug, partial", tracker.by_slug(PROJ, "train")["slug"] == "train-model")

# --- hooks through the pre-filter -------------------------------------------------
print("SessionStart:")
out = hook("SessionStart", base(hook_event_name="SessionStart", source="startup"))
check("injects the tracker note with the session id", SID in ctx(out) and "TRACKER.md" in ctx(out))
check("lists unfinished trackers", "train-model" in ctx(out))
check("session recorded", state.load_session(SID).get("cwd") == str(PROJ))

print("pre-filter fast path:")
n0 = pycalls()
check("not paused: no output", hook("PreToolUse", bash()) is None)
check("not paused, nothing queued: Python not started", pycalls() == n0)
for _ in range(2):
    hook("PostToolUse", bash())
check("PostToolUse below the nudge count: Python not started", pycalls() == n0)
check("ordinary prompt: Python not started",
      hook("UserPromptSubmit", base(prompt="please continue")) is None and pycalls() == n0)
for f in Path(os.environ["AGENT_PAUSE_HOME"]).joinpath("running").glob("*.json"):
    f.unlink()  # watchers started above exit on their own

print("PostToolUse tracker reminder:")
out = hook("PostToolUse", bash())
check("third call: reminder names the tracker", "TRACKER.md" in ctx(out) and "Now" in ctx(out))
check("counter reset after reminder", state.p("count", state.safe_id(SID)).read_text() == "0")
hook("PostToolUse", bash())
hook("PostToolUse", base(tool_name="Edit", tool_use_id="e1",
                         tool_input={"file_path": str(TRK)}))
check("a tracker write resets the counter", state.p("count", state.safe_id(SID)).read_text().strip() == "0")

print("pause by command while idle:")
out = hook("UserPromptSubmit", base(prompt="/agent-pause:pause"))
check("command handled, result attached", "<agent-pause-result>Paused" in ctx(out))
check("flag file set", state.p("flags", state.safe_id(SID)).exists())
out = hook("PreToolUse", bash("rm -rf build"))
check("Bash denied while paused", denied(out))
check("deny names the tracker", str(TRK) in out["hookSpecificOutput"]["permissionDecisionReason"])
cp = json.loads((TRK.parent / "checkpoint.json").read_text())
check("checkpoint written next to the tracker on the first deny",
      cp["session_id"] == SID and cp["stages_in_progress"] == ["2. Training"])
check("checkpoint has the last tool calls", cp["last_tool_calls"][-1]["tool"] == "Bash")
check("Edit to the tracker allowed", hook("PreToolUse", base(
    tool_name="Edit", tool_input={"file_path": str(TRK)})) is None)
check("Write elsewhere denied", denied(hook("PreToolUse", base(
    tool_name="Write", tool_input={"file_path": str(PROJ / "x.py")}))))
check("../ escape out of the work dir denied", denied(hook("PreToolUse", base(
    tool_name="Write", tool_input={"file_path": str(PROJ / ".work/../x.py")}))))
check("Read allowed", hook("PreToolUse", base(tool_name="Read", tool_input={"file_path": "/etc/hosts"})) is None)
check("job_status allowed", hook("PreToolUse", base(tool_name="mcp__plugin_agent-pause_jobs__job_status")) is None)
check("job_start denied", denied(hook("PreToolUse", base(tool_name="mcp__plugin_agent-pause_jobs__job_start"))))
check("Skill (e.g. the resume command) denied", denied(hook("PreToolUse", base(
    tool_name="Skill", tool_input={"skill": "agent-pause:resume"}))))
out = hook("PreToolUse", bash())
check("after max denies: deny AND continue:false", denied(out) and out.get("continue") is False)
out = hook("PreToolUse", base(tool_name="Bash", tool_input={"command": "ls"}, agent_id="sub1", agent_type="general-purpose"))
check("subagent told to report back", denied(out) and "report" in out["hookSpecificOutput"]["permissionDecisionReason"])

print("Stop while paused:")
old = cp["written_at"]
time.sleep(1.1)
check("Stop lets the paused session stop", hook("Stop", base(stop_hook_active=False)) is None)
check("Stop refreshes the checkpoint",
      json.loads((TRK.parent / "checkpoint.json").read_text())["written_at"] != old)

print("non-user prompts don't resume:")
check("task notification ignored", hook("UserPromptSubmit", base(
    prompt="<task-notification>\n<task-id>x</task-id>")) is None and state.is_paused(SID))
check("queued pause re-run at turn end: already paused", "Already paused" in ctx(
    hook("UserPromptSubmit", base(prompt="/agent-pause:pause"))))

print("resume by any message:")
out = hook("UserPromptSubmit", base(prompt="ok I'm back, carry on"))
check("resumed, flag cleared", not state.is_paused(SID) and not state.p("flags", state.safe_id(SID)).exists())
check("resume context has Now and Resume notes",
      "<agent-pause-resume>" in ctx(out) and "check the loss curve" in ctx(out) and "Check first" in ctx(out))
check("user sees a 'resumed' line", "resumed" in (out or {}).get("systemMessage", ""))
check("tools allowed again", hook("PreToolUse", bash()) is None)

print("pause from the queue (typed while busy):")
enqueue("/agent-pause:pause", "2026-10-16T02:00:00.000Z")
out = hook("PreToolUse", bash(tid="tu2"))
check("queued /agent-pause:pause: next tool call denied", denied(out))
check("session paused by the queued command", state.load_session(SID).get("paused_by") == "queued command")
dequeue("2026-10-16T02:00:05.000Z")
hook("UserPromptSubmit", base(prompt="/agent-pause:pause"))   # it runs when the turn ends
hook("UserPromptSubmit", base(prompt="continue"))
n0 = pycalls()
check("after resume: the consumed queue entry doesn't re-pause", hook("PreToolUse", bash(tid="tu3")) is None)
check("seen line recorded: later calls skip Python", hook("PreToolUse", bash(tid="tu4")) is None
      and pycalls() == n0)
for f in state.p("running").glob("*.json"):
    f.unlink()

print("plain keyword:")
out = hook("UserPromptSubmit", base(prompt="pause"))
check("'pause' pauses and tells the agent to stop", state.is_paused(SID) and "end your turn" in ctx(out))
hook("UserPromptSubmit", base(prompt="/agent-pause:resume"))
check("/agent-pause:resume resumes", not state.is_paused(SID))

print("pause all / resume all:")
SID2 = "sess-bbbb"
state.update_session(SID2, cwd=str(PROJ))
out = hook("UserPromptSubmit", base(prompt="/agent-pause:pause all"))
check("pause all: both sessions paused", state.is_paused(SID) and state.is_paused(SID2))
hook("UserPromptSubmit", base(session_id=SID2, prompt="hi again"))
check("a message resumes only that session", not state.is_paused(SID2) and state.is_paused(SID))
hook("UserPromptSubmit", base(prompt="/agent-pause:resume all"))
check("resume all clears the global flag", state.paused_all() is None and not state.is_paused(SID))

print("status:")
out = hook("UserPromptSubmit", base(prompt="/agent-pause:status"))
check("status result attached", "<agent-pause-result>" in ctx(out))

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
out = hook("UserPromptSubmit", base(session_id=SID3, prompt="/agent-pause:resume train-model"))
check("context from the tracker and checkpoint", "check the loss curve" in ctx(out)
      and "Checkpoint at pause" in ctx(out) and SID3 in ctx(out))
out = hook("UserPromptSubmit", base(session_id=SID3, prompt="/agent-pause:resume nope"))
check("unknown task: lists trackers", "train-model" in ctx(out))

# --- jobs ----------------------------------------------------------------------------
print("jobs:")
r = jobs.start("quick", command="echo hello; echo err >&2; exit 3", cwd=str(PROJ),
               redo_hint="rerun", session_id=SID)
check("job_start ok", r["ok"] and r["job"]["state"] == "running")
time.sleep(0.6)
s = jobs.status("quick")["job"]
check("exit code captured", s["state"] == "exited" and s["exit_code"] == 3)
check("tail has stdout and stderr", "hello" in jobs.tail("quick")["tail"] and "err" in jobs.tail("quick")["tail"])
r = jobs.start("long", argv=["sleep", "30"], cwd=str(PROJ), redo_hint="rerun", session_id=SID)
check("argv job running", jobs.status("long")["job"]["state"] == "running")
check("job is in its own process group", os.getpgid(r["job"].get("pid", 0) or jobs.find("long")["pid"]) != os.getpgrp())
r = jobs.stop("long", "test")
check("job_stop kills it", r["stopped"] and jobs.status("long")["job"]["state"] == "killed")
r = jobs.start("gone", argv=["sleep", "30"], cwd=str(PROJ), redo_hint="", session_id=SID)
os.killpg(jobs.find("gone")["pgid"], signal.SIGKILL)
time.sleep(0.3)
check("killed from outside: vanished", jobs.status("gone")["job"]["state"] == "vanished")
check("session filter", {j["name"] for j in jobs.status(session_id=SID)["jobs"]} == {"quick", "long", "gone"})
check("both command and argv refused", not jobs.start("x", command="a", argv=["b"])["ok"])
check("missing cwd refused", not jobs.start("x", command="a", cwd=str(TMP / "nope"))["ok"])

print("MCP server:")
env = dict(os.environ, CLAUDE_CODE_SESSION_ID="mcp-sess")
msgs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "job_start", "arguments": {
        "name": "mcpjob", "command": "echo from-mcp", "cwd": str(PROJ), "redo_hint": "rerun"}}},
    {"jsonrpc": "2.0", "id": 4, "method": "nope"},
]
p = subprocess.run([sys.executable, str(ROOT / "run.py"), "server"], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                   capture_output=True, text=True, env=env, timeout=30)
resp = {r["id"]: r for r in map(json.loads, p.stdout.splitlines())}
check("initialize", resp[1]["result"]["serverInfo"]["name"] == "agent-pause")
check("four job tools", sorted(t["name"] for t in resp[2]["result"]["tools"]) == ["job_start", "job_status", "job_stop", "job_tail"])
check("job_start via MCP records the session", jobs.find("mcpjob")["session_id"] == "mcp-sess"
      and not resp[3]["result"]["isError"])
check("unknown method -> error", resp[4]["error"]["code"] == -32601)

# --- processes ------------------------------------------------------------------------
print("processes:")
fake_bin = TMP / "bin"
fake_bin.mkdir()
(fake_bin / "claude").symlink_to("/bin/sleep")
fc = subprocess.Popen([str(fake_bin / "claude"), "30"])
hk = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], preexec_fn=None)
table = procs.table()
check("find_claude walks past shells to 'claude'", procs.find_claude(fc.pid, table) == fc.pid)
check("find_claude: none above a plain shell", procs.find_claude(hk.pid, table) in (None,) or
      "claude" in table.get(procs.find_claude(hk.pid, table) or 0, {}).get("cmd", "").lower())
fc.kill()
hk.kill()

COMMAND = "sleep 31; echo 'done'"
FAKE_CLAUDE = f"""
import subprocess, time
cmd = "true && " + {procs.quoted(COMMAND)!r} + " < /dev/null && pwd -P >/dev/null"
p = subprocess.Popen(['/bin/sh', '-c', cmd], start_new_session=True)
time.sleep(40)
"""
fake = subprocess.Popen([sys.executable, "-c", FAKE_CLAUDE])
time.sleep(0.5)
shells = procs.foreground_shells(fake.pid, COMMAND)
check("foreground shell found by its eval'd command", len(shells) == 1)
SIDK = "sess-kill"
state.update_session(SIDK, claude_pid=fake.pid, cwd=str(PROJ))
state.touch(state.p("running", "tuK.json"), json.dumps(
    {"session_id": SIDK, "tool_use_id": "tuK", "tool_input": {"command": COMMAND}}))
killed = core.kill_foreground(SIDK)
time.sleep(0.3)
check("kill_foreground kills the command tree", killed and not procs.foreground_shells(fake.pid, COMMAND)
      and not any(r["cmd"].startswith("sleep 31") for r in procs.table().values()))
check("...but not Claude itself", procs.alive(fake.pid))
check("interrupted command recorded for the checkpoint",
      state.load_session(SIDK)["interrupted"][0]["command"] == COMMAND)

print("watcher (pause now while a command runs):")
fake2 = subprocess.Popen([sys.executable, "-c", FAKE_CLAUDE.replace("sleep 31", "sleep 32")])
time.sleep(0.5)
CMD2 = COMMAND.replace("sleep 31", "sleep 32")
SIDW = "sess-watch"
TW = TMP / "tw.jsonl"
TW.write_text("")
state.update_session(SIDW, claude_pid=fake2.pid, cwd=str(PROJ), transcript_path=str(TW))
payload = {"session_id": SIDW, "transcript_path": str(TW), "cwd": str(PROJ), "tool_name": "Bash",
           "tool_use_id": "tuW", "tool_input": {"command": CMD2}}
hook("PreToolUse", payload)
check("watcher's running file written", state.p("running", "tuW.json").exists())
time.sleep(0.5)
with open(TW, "a") as f:
    f.write(json.dumps({"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-10-16T03:00:00.000Z",
                        "sessionId": SIDW, "content": "/agent-pause:pause now"}, separators=(",", ":")) + "\n")
deadline = time.time() + 10
while time.time() < deadline and procs.foreground_shells(fake2.pid, CMD2):
    time.sleep(0.2)
check("queued 'pause now' kills the running command", not procs.foreground_shells(fake2.pid, CMD2))
check("...and pauses the session", state.is_paused(SIDW))
hook("PostToolUseFailure", payload)
check("PostToolUseFailure removes the running file", not state.p("running", "tuW.json").exists())
fake.kill()
fake2.kill()

print("terminal CLI:")
r = subprocess.run([sys.executable, str(ROOT / "run.py"), "pause", "now"], capture_output=True, text=True)
check("`pause now` without a target refused", r.returncode == 2)
r = subprocess.run([sys.executable, str(ROOT / "run.py"), "pause", "all"], capture_output=True, text=True)
check("`pause all` from the terminal", r.returncode == 0 and state.paused_all() is not None)
r = subprocess.run([sys.executable, str(ROOT / "run.py"), "resume", "all"], capture_output=True, text=True)
check("`resume all` from the terminal", state.paused_all() is None)

print("plugin files:")
hooks_cfg = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
check("PreToolUse matcher is '*'", hooks_cfg["PreToolUse"][0]["matcher"] == "*")
check("PostToolUseFailure hooked (killed commands report as failures)", "PostToolUseFailure" in hooks_cfg)
for c in ("pause", "resume", "status", "help"):
    check(f"/{c} has disable-model-invocation", "disable-model-invocation: true" in (ROOT / "commands" / f"{c}.md").read_text())
check("pre-filter tail size matches transcript.TAIL_BYTES",
      f"tail -c {transcript.TAIL_BYTES}" in (ROOT / "hooks" / "fast.sh").read_text())

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
