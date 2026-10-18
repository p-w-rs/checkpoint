# Architecture

Hooks, an MCP server and slash commands, sharing `src/checkpoint/` through
`run.py` (pixi's Python), plus a POSIX `sh` pre-filter in front of every
hook. Why it's built this way, and the spikes behind it: [DESIGN.md](DESIGN.md).

```
user prompt ──▶ UserPromptSubmit ─ fast.sh ─▶ hooks.handle_prompt
                  /check:pause|resume|status, plain "pause", or any
                  message in a paused session (= resume)
tool call ────▶ PreToolUse(*) ──── fast.sh ─▶ hooks.gate
                  flag file or queued pause in the transcript tail? else exit
                  foreground Bash: write running/<id>.json, start watch.sh
tool result ──▶ PostToolUse(*) / PostToolUseFailure ─ fast.sh
                  remove running/<id>.json; count calls; every N ─▶ hooks.post
turn end ─────▶ Stop ───────────── fast.sh ─▶ hooks.stop (paused, or trackers exist)
session start ▶ SessionStart ───────────────▶ hooks.session_main
job tools ────▶ MCP server "jobs" (mcp_server.py ─▶ jobs.py)
```

## Modules

| File | Does |
|---|---|
| `hooks/fast.sh` | Pre-filter for all hooks. Reads `session_id`, `transcript_path`, `cwd`, `tool_name` with grep/sed; starts Python only when there's work. Not paused + nothing queued costs a `tail \| grep` of the transcript. |
| `hooks/watch.sh` | One per running foreground Bash call. Every 2 s: a queued `pause now`, `now/<sid>` or `now-all`? Then `run.py watch-fire`. Exits when PostToolUse removes `running/<id>.json` or Claude exits. |
| `state.py` | State dir layout (see its docstring), flock, atomic JSON writes, pause flags. `pause all` is a global flag with a timestamp; a session is paused if its flag exists or it hasn't been resumed since that timestamp. |
| `transcript.py` | Transcript tail; replays `queue-operation` enqueue/dequeue/remove to find a pending pause; recent tool calls for the checkpoint. |
| `tracker.py` | Finds and parses `.work/<task>/TRACKER.md` (stages table, sections, `Session:` id). Read-only. |
| `core.py` | Pause (incl. `now`, `all`, `--stop-jobs`, another session), checkpoint, resume context, status. |
| `procs.py` | `ps` table, finding Claude from a hook's parent, matching a Bash tool shell by its `eval '<command>'`, killing a tree group by group. |
| `jobs.py` | Detached jobs: `sh` wrapper in its own session writing `exit_code`; states running / exited / killed / vanished. The MCP server reaps the jobs it started (SIGCHLD), and `procs.alive` treats an unreaped zombie as dead, so another session can stop a job cleanly. Jobs get `job_env()`: the environment minus checkpoint's own pixi activation. |
| `mcp_server.py` | Hand-rolled stdio JSON-RPC (initialize, tools/list, tools/call, ping). Session id from `CLAUDE_CODE_SESSION_ID`. |
| `cli.py` | `pixi run status / pause / resume`. |

## The gate

1. Not paused: if the transcript tail has a pause line not in `seen/<sid>`,
   replay the queue. A pending pause newer than `queue_handled` pauses the
   session. When the turn ends the queued command runs too, and just says
   "Already paused".
2. Paused: allow `allow_while_paused` tools and Write/Edit inside
   `<cwd>/.work/`; deny everything else. The first deny writes the
   checkpoint. After `max_denies`, deny **plus** `continue: false` (alone it
   would let the tool run). Subagents get "return a short report".
3. Any exception: no output, so the call goes ahead (fail open).

## Not paused-proof on purpose

The gate steers a cooperative agent. It's not a sandbox, and nothing stops
Bash from writing to `.work/` or the state dir.
