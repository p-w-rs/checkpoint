# Build the agent-pause plugin
Goal: the plugin in plugins/agent-pause works end to end (tracker hooks, pause/resume, jobs, pause now, status, installer), with green hermetic tests; not installed into the user's settings.
Started: 2026-10-15  Session: 7c46462d  Updated: 2026-10-15

## Stages
| # | Stage | Status | Notes / details |
|---|-------|--------|-----------------|
| 1 | Skeleton: marketplace, plugin.json, pixi, run.py, tests | done | |
| 2 | Tracker convention hooks | done | SessionStart note, PostToolUse nudge, Stop nudge |
| 3 | Pause/resume gate, queue detection, commands | done | |
| 4 | Jobs MCP server | done | |
| 5 | pause now (watcher + kill tree) | done | watch.sh per foreground Bash call |
| 6 | status, install.py, README, help | in progress | install.py + rules done; README, ARCHITECTURE, CLAUDE.md left |
| 7 | Live test with claude -p --plugin-dir | todo | |

## Now
Current step: live test with claude -p --plugin-dir
Next action: run the live checks, then README/ARCHITECTURE/CLAUDE.md

## Jobs
| Job | Command | Started | State | Redo / resume |
|-----|---------|---------|-------|---------------|

## Resume notes
- Check first: git status; `cd plugins/agent-pause && pixi run test`.
- Assumptions: marketplace named "agent-pause" (net-gateway's is "local-tools"; same name would clash).
