# agent-pause

A Claude Code plugin to **pause** running agents without breaking their work,
and resume them later, in the same session or a new one.

- **Pause, not stop:** each agent finishes its current step, updates its
  progress tracker at the next tool call, and stops cleanly. Then you can
  close the laptop.
- **Trackers:** agents keep a `TRACKER.md` per task (stages, what's done vs.
  accepted, the exact next action), so any session can pick the work up.
- **Jobs:** long commands run detached and survive pauses and sessions.

Status: **design only.** See [docs/DESIGN.md](docs/DESIGN.md).

Sister project: [net-gateway](../net-gateway). Same requirements: Claude Code
and pixi only.
