#!/usr/bin/env python3
"""Single entry point for every checkpoint mode.

Putting src/ on sys.path here means the package imports work the same whether
Claude Code launches the MCP server, a hook, or the CLI. Invoked as:
    python run.py server            # MCP stdio server (.mcp.json)
    python run.py hook-session      # SessionStart
    python run.py hook-prompt       # UserPromptSubmit (via hooks/fast.sh)
    python run.py hook-gate         # PreToolUse (via hooks/fast.sh)
    python run.py hook-post         # PostToolUse, every N calls (via hooks/fast.sh)
    python run.py hook-stop         # Stop (via hooks/fast.sh)
    python run.py watch-fire SID ID # internal: hooks/watch.sh saw `pause now`
    python run.py status | pause ... | resume all | doctor   # terminal commands
    python run.py sync-pause SID    # internal: the mod paused SID; record it, write the checkpoint
    python run.py ingest SID [--sync] | snap SID TOOL_USE_ID   # internal: spool -> journal (capture.py)
    python run.py summary [TASK] | tasks | save SID | list SID # the journal (cli.py)

Normally started by hooks/fast.sh, hooks/env.sh or hooks/watch.sh with the
plugin's own env (hooks/lib.sh). Everything checkpoint writes is private:
umask 077 here (state folders 0700, files 0600); the umask the user had is
kept in CHECKPOINT_USER_UMASK for the jobs and git commands it starts.
"""
import os
import sys
from pathlib import Path

_USER_UMASK = os.umask(0o077)
os.environ.setdefault("CHECKPOINT_USER_UMASK", "%04o" % _USER_UMASK)
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "server":
        from checkpoint import mcp_server
        mcp_server.main()
        return 0
    if mode.startswith("hook-"):
        from checkpoint import hooks
        fn = {"hook-session": hooks.session_main, "hook-prompt": hooks.prompt_main,
              "hook-gate": hooks.gate_main, "hook-post": hooks.post_main,
              "hook-stop": hooks.stop_main}.get(mode)
        return fn() if fn else 0
    if mode == "watch-fire" and len(sys.argv) == 4:
        from checkpoint import hooks
        return hooks.watch_fire(sys.argv[2], sys.argv[3])
    if mode == "sync-pause" and len(sys.argv) == 3:
        from checkpoint import hooks
        return hooks.sync_pause(sys.argv[2])
    from checkpoint import cli
    return cli.main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
