#!/bin/sh
# at_tool.sh STATE N KEYS : wait until the N-th PreToolUse Bash in STATE/events.jsonl, then send KEYS to the relay.
S=$1; N=$2; K=$3; i=0
while [ $i -lt 600 ]; do
  c=$(grep -c '"event": "PreToolUse".*"tool": "Bash"' "$S/events.jsonl" 2>/dev/null); c=${c:-0}
  [ "$c" -ge "$N" ] && break; sleep 0.2; i=$((i+1))
done
sleep 0.5; echo "$K" >> "$S/relay.cmd"
