#!/bin/sh
# Guards one foreground Bash call. Usage: watch.sh SID TOOL_USE_ID CLAUDE_PID ROOT TRANSCRIPT
#
# Started detached by fast.sh at PreToolUse(Bash); exits when PostToolUse
# removes running/<id>.json or Claude goes away. Every 2 s it looks for a
# `pause now` request: a queued "/agent-pause:pause now" (or plain "pause now")
# in the transcript, now/<sid>, or now-all newer than this call. On a hit it
# asks Python, which re-checks, pauses the session and kills the command.
sid=$1
tid=$2
cpid=$3
root=$4
tp=$5
st=${AGENT_PAUSE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/agent-pause}
run="$st/running/$tid.json"
seen=""

fire() {
    if [ -n "$AGENT_PAUSE_PYTHON" ]; then
        "$AGENT_PAUSE_PYTHON" "$root/run.py" watch-fire "$sid" "$tid"
    else
        pixi run --manifest-path "$root/pixi.toml" --frozen python "$root/run.py" watch-fire "$sid" "$tid"
    fi
}

i=0
while [ $i -lt 43200 ]; do   # 24 h at most
    sleep "${AGENT_PAUSE_WATCH_INTERVAL:-2}"
    i=$((i + 1))
    [ -e "$run" ] || exit 0
    kill -0 "$cpid" 2>/dev/null || { rm -f "$run"; exit 0; }
    hit=""
    [ -e "$st/now/$sid" ] && hit=1
    [ -e "$st/now-all" ] && [ "$st/now-all" -nt "$run" ] && hit=1
    if [ -n "$tp" ] && [ -f "$tp" ]; then
        line=$(tail -c 262144 "$tp" 2>/dev/null | grep '"queue-operation"' |
            grep -Ei 'agent-pause:pause[^"]*now|"content":"[[:space:]]*pause[[:space:]]+(all[[:space:]]+)?now' | tail -n 1)
        if [ -n "$line" ] && [ "$line" != "$seen" ]; then
            seen=$line
            hit=1
        fi
    fi
    if [ -n "$hit" ]; then
        out=$(fire 2>/dev/null)
        case $out in
            *'"command"'*) exit 0 ;;   # killed it; PostToolUse cleans up
        esac
    fi
done
exit 0
