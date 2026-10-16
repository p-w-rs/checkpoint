#!/bin/sh
# Shell pre-filter for every agent-pause hook. Usage: fast.sh EVENT PLUGIN_ROOT
#
# PreToolUse and PostToolUse run on every tool call, so the common path (not
# paused, nothing queued) must not start Python. This script checks flag files
# and counters and only calls `run.py hook-<event>` when there is something
# to do. It always exits 0 and prints only what Python prints (fail open).
ev=$1
root=$2
st=${AGENT_PAUSE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-pause}
work=${AGENT_PAUSE_WORK_DIR:-.work}
input=$(cat)

# First "key":"value" string in the payload (Claude Code puts session_id,
# transcript_path, cwd first; tool_input comes later).
field() {
    printf '%s' "$input" | grep -o "\"$1\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -n 1 |
        sed 's/^[^:]*:[[:space:]]*"//; s/"$//'
}
safe() { printf '%s' "$1" | tr -c 'A-Za-z0-9_-' '_' | cut -c 1-128; }

py() {
    if [ -n "$AGENT_PAUSE_PYTHON" ]; then
        printf '%s' "$input" | AGENT_PAUSE_HOOK_PPID=$PPID "$AGENT_PAUSE_PYTHON" "$root/run.py" "$@" 2>/dev/null
    else
        printf '%s' "$input" | AGENT_PAUSE_HOOK_PPID=$PPID pixi run --manifest-path "$root/pixi.toml" \
            --frozen python "$root/run.py" "$@" 2>/dev/null
    fi
}

sid=$(safe "$(field session_id)")
paused() { [ -e "$st/flags/$sid" ] || [ -e "$st/paused-all" ]; }

case $ev in
SessionStart)
    py hook-session
    ;;

UserPromptSubmit)
    mkdir -p "$st/turn" "$st/turn-start" 2>/dev/null
    echo 0 > "$st/turn/$sid" 2>/dev/null
    : > "$st/turn-start/$sid" 2>/dev/null
    if paused || printf '%s' "$input" | grep -Eqi \
        '"prompt"[[:space:]]*:[[:space:]]*"[[:space:]]*(/agent-pause:|pause([[:space:]]+(now|all|all[[:space:]]+now|now[[:space:]]+all))?[.!]?[[:space:]]*")'; then
        py hook-prompt
    fi
    ;;

PreToolUse)
    if paused; then
        py hook-gate
        exit 0
    fi
    tp=$(field transcript_path)
    if [ -n "$tp" ] && [ -f "$tp" ]; then
        # A pause command typed while busy waits in Claude Code's queue, which
        # is written to the transcript at once (DESIGN.md, Spike 1).
        line=$(tail -c 262144 "$tp" 2>/dev/null | grep '"queue-operation"' |
            grep -E 'agent-pause:pause|"content":"[[:space:]]*[Pp]ause' | tail -n 1)
        if [ -n "$line" ] && [ "$line" != "$(cat "$st/seen/$sid" 2>/dev/null)" ]; then
            py hook-gate
            exit 0
        fi
    fi
    # Guard a foreground Bash call so `pause now` can stop it while it runs.
    if [ "$(field tool_name)" = "Bash" ] &&
        ! printf '%s' "$input" | grep -Eq '"run_in_background"[[:space:]]*:[[:space:]]*true'; then
        tid=$(safe "$(field tool_use_id)")
        if [ -n "$tid" ] && [ "$tid" != "_" ]; then
            mkdir -p "$st/running" 2>/dev/null
            # The Claude pid SessionStart recorded (fall back to our parent).
            cpid=$(grep -o '"claude_pid": *[0-9][0-9]*' "$st/sessions/$sid.json" 2>/dev/null | grep -o '[0-9]*$')
            # Separate statements: `a && b &` would background a subshell that
            # keeps this hook's stdout open, and Claude Code would wait on it.
            if printf '%s' "$input" > "$st/running/$tid.json" 2>/dev/null; then
                nohup sh "$root/hooks/watch.sh" "$sid" "$tid" "${cpid:-$PPID}" "$root" "$tp" \
                    </dev/null >/dev/null 2>&1 &
            fi
        fi
    fi
    ;;

PostToolUse|PostToolUseFailure)
    tid=$(safe "$(field tool_use_id)")
    [ -n "$tid" ] && rm -f "$st/running/$tid.json" 2>/dev/null
    mkdir -p "$st/count" "$st/turn" 2>/dev/null
    t=$(cat "$st/turn/$sid" 2>/dev/null); t=${t:-0}
    echo $((t + 1)) > "$st/turn/$sid" 2>/dev/null
    # A write to a tracker resets the reminder counter.
    if printf '%s' "$input" | grep -Eq '"tool_name"[[:space:]]*:[[:space:]]*"(Write|Edit|MultiEdit)"' &&
        printf '%s' "$input" | grep -Eq '"file_path"[[:space:]]*:[[:space:]]*"[^"]*TRACKER\.md"'; then
        echo 0 > "$st/count/$sid"
        exit 0
    fi
    n=$(cat "$st/count/$sid" 2>/dev/null); n=${n:-0}
    n=$((n + 1))
    echo $n > "$st/count/$sid" 2>/dev/null
    if [ "$n" -ge "${AGENT_PAUSE_NUDGE_EVERY:-25}" ]; then
        py hook-post
    fi
    ;;

Stop)
    cwd=$(field cwd)
    if paused || ls "$cwd/$work"/*/TRACKER.md >/dev/null 2>&1; then
        py hook-stop
    fi
    ;;
esac
exit 0
