#!/bin/sh
# Runs pixi, found even when Claude Code was started with a minimal PATH
# (a launchd app gets /usr/bin:/bin:/usr/sbin:/sbin). Used by hooks/fast.sh,
# hooks/watch.sh and .mcp.json.
#   sh pixi.sh ARGS...    exec pixi ARGS...
#   sh pixi.sh --check    print pixi's path; exit 1 if there is none
pixi=$(command -v pixi 2>/dev/null)
if [ -z "$pixi" ] || [ ! -x "$pixi" ]; then
    pixi=
    for p in "${PIXI_HOME:-$HOME/.pixi}/bin/pixi" "$HOME/.pixi/bin/pixi" "$HOME/.local/bin/pixi" \
        /opt/homebrew/bin/pixi /usr/local/bin/pixi /opt/local/bin/pixi \
        /home/linuxbrew/.linuxbrew/bin/pixi; do
        if [ -x "$p" ]; then
            pixi=$p
            break
        fi
    done
fi
if [ "$1" = --check ]; then
    [ -n "$pixi" ] || exit 1
    printf '%s\n' "$pixi"
    exit 0
fi
if [ -z "$pixi" ]; then
    echo "checkpoint: pixi not found on PATH or in the usual install folders; install it from https://pixi.sh" >&2
    exit 127
fi
exec "$pixi" "$@"
