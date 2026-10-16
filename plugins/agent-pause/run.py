#!/usr/bin/env python3
"""Single entry point for every agent-pause mode.

Putting src/ on sys.path here means the package imports work the same whether
Claude Code launches the MCP server, a hook, or the CLI. Invoked as:
    python run.py server            # MCP stdio server (.mcp.json)
    python run.py hook-session      # SessionStart
    python run.py hook-prompt       # UserPromptSubmit (via hooks/fast.sh)
    python run.py hook-gate         # PreToolUse (via hooks/fast.sh)
    python run.py hook-post         # PostToolUse, every N calls (via hooks/fast.sh)
    python run.py hook-stop         # Stop (via hooks/fast.sh)
    python run.py watch-fire SID ID # internal: hooks/watch.sh saw `pause now`
    python run.py status | pause ... | resume all   # terminal commands
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "server":
        from agentpause import mcp_server
        mcp_server.main()
        return 0
    if mode.startswith("hook-"):
        from agentpause import hooks
        fn = {"hook-session": hooks.session_main, "hook-prompt": hooks.prompt_main,
              "hook-gate": hooks.gate_main, "hook-post": hooks.post_main,
              "hook-stop": hooks.stop_main}.get(mode)
        return fn() if fn else 0
    if mode == "watch-fire" and len(sys.argv) == 4:
        from agentpause import hooks
        return hooks.watch_fire(sys.argv[2], sys.argv[3])
    from agentpause import cli
    return cli.main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
