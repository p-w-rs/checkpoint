# Manual spike checks (need a person typing)

The prototype hook is loaded by `testproj/.claude/settings.json`; nothing
global changes. Every hook call is logged to `spike/state/events.jsonl` with
timestamps, so you only need to type; tell Claude "done" afterwards and it
reads the log. Haiku is fine for all of these.

Test prompt (used below as PROMPT):

    Run ./tick.sh 5 eight times, as eight separate Bash tool calls, one after another (never in parallel). Then say done.

## Desktop app

Start a new Code session with folder `~/projects/hobby/agent-pause/spike/testproj`.

- **D1, slash command while busy:** send PROMPT. When the first tick call
  shows, type `/spike-pause` and press Enter. Note whether it stops after the
  current call or runs all eight. Then send `/spike-resume`.
- **D2, plain text while busy:** send PROMPT. While it's busy, type `pause`
  and press Enter. Expected: it stops after the current call with one line.
  Then send `/spike-resume`.
- **D3, how `continue: false` looks:** in a terminal run

  ```bash
  echo continue-false-now > ~/projects/hobby/agent-pause/spike/state/mode && touch ~/projects/hobby/agent-pause/spike/state/with-deny
  ```

  Send PROMPT and type `pause` while it's busy. Look at how the turn ends
  (any banner, error, spinner left running?). Send `/spike-resume`, then
  `say hi` to check the session still works. Clean up:

  ```bash
  rm ~/projects/hobby/agent-pause/spike/state/mode ~/projects/hobby/agent-pause/spike/state/with-deny
  ```

## CLI (a real terminal)

```bash
cd ~/projects/hobby/agent-pause/spike/testproj && claude --model haiku
```

- **C1:** as D1 (`/spike-pause` + Enter while busy), then `/spike-resume`.
- **C2:** as D2 (`pause` + Enter while busy), then `/spike-resume`.
- **C3:** send PROMPT, type `/spike-pause` and press **ctrl+enter**
  (send now) instead of Enter. Does it pause after the current call?
