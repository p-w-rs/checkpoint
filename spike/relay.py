#!/usr/bin/env python3
"""Run interactive `claude` in a pty that both a person and an agent can type into.

Usage: SPIKE_STATE=DIR python3 relay.py [claude args...]
- Your keyboard goes straight to claude; its screen shows in this terminal.
- An agent appends lines to DIR/relay.cmd; each line is typed into claude.
  Tokens: {ENTER} {ESC} {TAB} {CX} (ctrl+x) {CS} (ctrl+s) {CJ} (ctrl+j)
  {CTRLENTER} (kitty CSI-u ctrl+enter) {UP} {DOWN} {WAIT n} (seconds). Text is typed slowly.
- Timeline of agent keystrokes: DIR/relay.log. Raw screen: DIR/screen.raw.
"""
import fcntl
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time
import tty

HERE = os.path.dirname(os.path.abspath(__file__))
state = os.environ.get("SPIKE_STATE", os.path.join(HERE, "state"))
os.makedirs(state, exist_ok=True)
cmd_path = os.path.join(state, "relay.cmd")
open(cmd_path, "a").close()
cmd_pos = os.path.getsize(cmd_path)
log = open(os.path.join(state, "relay.log"), "a")
raw = open(os.path.join(state, "screen.raw"), "ab")

TOKENS = {"ENTER": b"\r", "ESC": b"\x1b", "TAB": b"\t", "CX": b"\x18", "CS": b"\x13",
          "CJ": b"\n", "CTRLENTER": b"\x1b[13;5u",
          "UP": b"\x1b[A", "DOWN": b"\x1b[B"}


def note(msg):
    t = time.time()
    log.write(time.strftime("%H:%M:%S", time.localtime(t)) + ".%03d " % (int(t * 1000) % 1000) + msg + "\n")
    log.flush()


pid, fd = pty.fork()
if pid == 0:
    os.chdir(os.path.join(HERE, "testproj"))
    os.execvp("claude", ["claude"] + sys.argv[1:])


def sync_size(*_):
    try:
        sz = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, sz)
        os.kill(pid, signal.SIGWINCH)
    except OSError:
        pass


sync_size()
signal.signal(signal.SIGWINCH, sync_size)
old = termios.tcgetattr(sys.stdin.fileno())
tty.setraw(sys.stdin.fileno())
queue = []  # pending (time, bytes, label)


def load_cmds():
    global cmd_pos
    size = os.path.getsize(cmd_path)
    if size <= cmd_pos:
        return
    with open(cmd_path, "rb") as f:
        f.seek(cmd_pos)
        data = f.read().decode()
    cmd_pos = size
    at = max([time.time()] + [q[0] for q in queue])
    for line in data.splitlines():
        for part in re.split(r"(\{[A-Z]+(?: [0-9.]+)?\})", line):
            if not part:
                continue
            m = re.fullmatch(r"\{([A-Z]+)(?: ([0-9.]+))?\}", part)
            if m and m.group(1) == "WAIT":
                at += float(m.group(2))
            elif m and m.group(1) in TOKENS:
                at += 0.3
                queue.append((at, TOKENS[m.group(1)], part))
            else:
                for ch in part:
                    at += 0.03
                    queue.append((at, ch.encode(), None))
                queue.append((at, b"", "typed %r" % part))


try:
    while True:
        r, _, _ = select.select([fd, sys.stdin], [], [], 0.05)
        if fd in r:
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            os.write(sys.stdout.fileno(), data)
            raw.write(data)
            raw.flush()
        if sys.stdin in r:
            os.write(fd, os.read(sys.stdin.fileno(), 1024))
        load_cmds()
        now = time.time()
        while queue and queue[0][0] <= now:
            _, b, label = queue.pop(0)
            if b:
                os.write(fd, b)
            if label:
                note(label)
finally:
    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
