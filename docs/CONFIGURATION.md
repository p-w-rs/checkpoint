# Configuration

checkpoint has no settings file and adds nothing to Claude Code's
`settings.json`. Everything is optional, set as environment variables where
Claude Code starts (your shell profile, or the line that starts `claude`).
Restart Claude Code after changing one.

| Variable | Default | What it does |
|---|---|---|
| `CHECKPOINT_TRACKERS` | on | `off` (or `0`, `no`, `false`) turns trackers off: no tracker instructions for Claude, no reminders, no "update the tracker" before it stops. The journal records everything either way. |
| `CHECKPOINT_KEEP_DAYS` | `30` | Days a finished task's journal (completed or canceled) is kept after its last activity. `0` or `never`: forever. |
| `CHECKPOINT_KEEP_OPEN_DAYS` | `180` | The same for unfinished tasks. |
| `CHECKPOINT_HOME` | `~/.local/state/checkpoint` (`$XDG_STATE_HOME/checkpoint` when that is set) | The state folder: journals (with the pause-time checkpoints), session records, jobs, exports, the terminal helper. |
| `CHECKPOINT_WORK_DIR` | `.work` | The folder in each project where Claude keeps trackers (`<project>/.work/<task>/TRACKER.md`). Not under `.claude/`: Claude Code asks before every write there. |
| `CHECKPOINT_NUDGE_EVERY` | `25` | Remind Claude to update its tracker every N tool calls. |
| `CHECKPOINT_HOOK_DEADLINE` | `20` | Seconds a hook waits for checkpoint's Python before it goes on without it. While paused, a call it couldn't check is held; otherwise it goes ahead and the miss is shown in `/check:status`. Claude Code stops checkpoint's tool-call hooks after 30 s (its session-start, prompt and Stop hooks after 60 s), as checkpoint's `hooks.json` sets; keep this below 30. |
| `CHECKPOINT_BG_DEADLINE` | `300` | Seconds a background ingest may run before it is stopped. |
| `CHECKPOINT_GATEWAY_CONTRACT` | `~/.config/gateway/run/contract.json` | Where the [gateway](https://github.com/p-w-rs/gateway)'s daemon says how to reach it. Set but empty: checkpoint doesn't use the gateway. |
| `CHECKPOINT_GATEWAY_ENV` | `~/.config/gateway/env.sh` | The file jobs of checkpoint's own runner source when the gateway is installed but doesn't run them. Set but empty: none. |

The two gateway paths are in your home folder as the system's user database
names it, not `$HOME`, the same as the gateway uses.

Other `CHECKPOINT_*` variables are internal (checkpoint sets them itself)
or for its tests; don't set them.
