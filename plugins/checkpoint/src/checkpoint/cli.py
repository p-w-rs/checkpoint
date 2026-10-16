"""Terminal commands, for when no session is idle to type into:

    pixi run status                 sessions, pause state, jobs
    pixi run pause all [now]        pause every session
    pixi run pause <session> [now]  pause one (id prefix from status)
    pixi run resume all             clear pause-all
    pixi run resume <session>       un-pause one (it still needs a message to continue)
"""
from __future__ import annotations

import sys

from . import core, state

USAGE = __doc__


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE.strip())
        return 0
    cmd, args = argv[0], argv[1:]
    if cmd == "status":
        print(core.status_text())
        return 0
    if cmd == "pause":
        if not [a for a in args if a not in ("now", "--stop-jobs")]:
            print("Say which: `pause all` or `pause <session>` (see `pixi run status`).", file=sys.stderr)
            return 2
        print(core.pause("", args, by="terminal"))
        return 0
    if cmd == "resume":
        if args[:1] == ["all"]:
            print("Pause-all cleared." if state.clear_paused_all() else "Pause-all wasn't on.")
            print("Each paused session continues when you send it a message.")
            return 0
        if args:
            s = state.find_session(args[0])
            if not s:
                print(f"No single session matches '{args[0]}'.", file=sys.stderr)
                return 2
            state.clear_paused(s["session_id"])
            print(f"Session {s['session_id'][:8]} un-paused; send it a message to continue.")
            return 0
        print("Say which: `resume all` or `resume <session>`.", file=sys.stderr)
        return 2
    print(USAGE.strip(), file=sys.stderr)
    return 2
