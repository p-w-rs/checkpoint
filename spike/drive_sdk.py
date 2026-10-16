#!/usr/bin/env python3
"""Spike 1 via the stream-json input path the desktop app uses.

Usage: drive_sdk.py STATE_DIR PROMPT INTERJECTION [after_n_bash]
Sends PROMPT, waits for the n-th PreToolUse Bash, sends INTERJECTION as a
second user message while the turn is busy, then waits for quiet and closes.
"""
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
state, prompt, interject = sys.argv[1], sys.argv[2], sys.argv[3]
after_n = int(sys.argv[4]) if len(sys.argv) > 4 else 1
os.makedirs(state, exist_ok=True)
events = os.path.join(state, "events.jsonl")
dlog = open(os.path.join(state, "driver.log"), "a")


def note(msg):
    dlog.write(time.strftime("%H:%M:%S") + " " + msg + "\n")
    dlog.flush()


def evs():
    if not os.path.exists(events):
        return []
    return [json.loads(l) for l in open(events) if l.strip()]


env = dict(os.environ, SPIKE_STATE=state)
p = subprocess.Popen(
    ["claude", "-p", "--model", "haiku", "--input-format", "stream-json",
     "--output-format", "stream-json", "--verbose",
     "--allowedTools", "Bash(./tick.sh *)", "Bash(echo *)"],
    cwd=os.path.join(HERE, "testproj"), env=env,
    stdin=subprocess.PIPE, stdout=open(os.path.join(state, "out.jsonl"), "w"),
    stderr=subprocess.STDOUT, text=True)


def send(text):
    p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
    p.stdin.flush()


note("sending prompt")
send(prompt)
deadline = time.time() + 120
while time.time() < deadline:
    if sum(1 for e in evs() if e["event"] == "PreToolUse" and e.get("tool") == "Bash") >= after_n:
        break
    time.sleep(0.2)
time.sleep(0.5)
note("sending interjection %r" % interject)
send(interject)

last, quiet_since, deadline = len(evs()), time.time(), time.time() + 240
while time.time() < deadline:
    time.sleep(1)
    cur = len(evs())
    if cur != last:
        last, quiet_since = cur, time.time()
    elif time.time() - quiet_since > 20:
        break
note("settled; closing stdin")
p.stdin.close()
p.wait(timeout=60)
note("exit %s" % p.returncode)
