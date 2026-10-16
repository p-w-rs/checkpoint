#!/bin/sh
# Slow test command: prints a tick every second for $1 seconds (default 30).
n=${1:-30}; i=1
while [ $i -le $n ]; do echo "tick $i"; sleep 1; i=$((i+1)); done
echo "tick.sh finished"
