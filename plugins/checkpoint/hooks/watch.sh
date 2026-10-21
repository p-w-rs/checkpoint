#!/bin/sh
# Guards one foreground Bash call.
# Usage: watch.sh SID TOOL_USE_ID CLAUDE_PID TRANSCRIPT [AGENT_ID]
#
# Started detached by fast.sh at PreToolUse(Bash). Every 2 s it looks for a
# `pause now` request: a queued "/check:pause now" (or plain "pause now") in
# the transcript, or now/<sid> (from `pause <session> now` / `pause all now`).
# On a hit it asks Python, which re-checks, pauses the session and kills the
# command. It exits when the call is over: PostToolUse removed
# running/<id>.json, the transcript has the call's tool_result (a cancelled
# or denied call fires no PostToolUse), or Claude is gone.
sid=$1
tid=$2
cpid=$3
tp=$4
aid=$5
case $0 in
*/*) root=${0%/*}/.. ;;
*) root=.. ;;
esac
st=${CHECKPOINT_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/checkpoint}
LC_ALL=C
export LC_ALL
umask 077
. "$root/hooks/lib.sh"
if [ -z "${CHECKPOINT_ENVD:-}" ] && [ -f "$st/rt/$sid" ]; then
    CHECKPOINT_ENVD=$(cat "$st/rt/$sid" 2>/dev/null)
fi
run="$st/running/$tid.json"
seen=""
# A subagent's tool results go to its own transcript, next to the session's.
done_tp=$tp
if [ -n "$aid" ] && [ -n "$tp" ]; then
    done_tp="${tp%.jsonl}/subagents/agent-$aid.jsonl"
fi

fire() {
    (ck_python && ck_exec_py watch-fire "$sid" "$tid")
}

nowline() {
    tail -c 262144 "$tp" 2>/dev/null | grep '"queue-operation"' |
        grep -Ei 'check:pause[^"]*(now|interrupt)|"content":"[[:space:]]*pause[[:space:]]+(all[[:space:]]+)?(now|interrupt)' | tail -n 1
}
# A `pause now` already in the transcript was handled before this call began.
[ -n "$tp" ] && [ -f "$tp" ] && seen=$(nowline)

i=0
while [ $i -lt 43200 ]; do # 24 h at most
    sleep "${CHECKPOINT_WATCH_INTERVAL:-2}"
    i=$((i + 1))
    [ -e "$run" ] || exit 0
    kill -0 "$cpid" 2>/dev/null || {
        rm -f "$run"
        exit 0
    }
    if [ -n "$done_tp" ] && [ -f "$done_tp" ] &&
        tail -c 262144 "$done_tp" 2>/dev/null | grep -q "\"tool_use_id\":[[:space:]]*\"$tid\""; then
        rm -f "$run"
        exit 0
    fi
    hit=""
    [ -e "$st/now/$sid" ] && hit=1
    if [ -n "$tp" ] && [ -f "$tp" ]; then
        line=$(nowline)
        if [ -n "$line" ] && [ "$line" != "$seen" ]; then
            seen=$line
            hit=1
        fi
    fi
    if [ -n "$hit" ]; then
        out=$(fire 2>/dev/null)
        case $out in
        *'"command"'*) exit 0 ;; # killed it; PostToolUseFailure cleans up
        esac
    fi
done
rm -f "$run"
exit 0
