#!/bin/sh
# checkpoint's runtime launcher (POSIX sh; runs before Python exists).
#
#   env.sh ensure            build the env unless ready (the async SessionStart
#                            hook, and the background build fast.sh starts)
#   env.sh mcp               the MCP server: the env's python on run.py server
#                            (starts a build and waits up to 25 s for it first)
#   env.sh cli ARGS...       run.py ARGS... (the terminal helper; waits for a build)
#   env.sh run ARGS...       run.py ARGS... now, or exit 75 when no runtime is ready (the mod)
#   env.sh doctor [SESSION]  the runtime half of /check:doctor, without Python
#   env.sh state             ready | older | building | failed | nopixi | missing
#   env.sh contract-path     where the gateway's discovery file is (the mod; nothing reads it)
#   env.sh flag pause SESSION [WHO]   pause flag, written whole (the mod)
#   env.sh flag now SESSION           `pause now` request for the session's watcher
#   env.sh flag noresume SESSION      the next prompt doesn't resume (the mod: isMeta,
#                                     scheduled, automation); flag clear-noresume undoes it
#   env.sh spool SESSION EVENT        stdin (one JSON object) as a record of the session's
#                                     spool (the mod's events; hooks/lib.sh ck_spool)
#
# Paths and the env layout: hooks/lib.sh. Everything written under
# CHECKPOINT_HOME is private (umask 077: folders 0700, files 0600).
case $0 in
*/*) root=${0%/*}/.. ;;
*) root=.. ;;
esac
st=${CHECKPOINT_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/checkpoint}
CHECKPOINT_USER_UMASK=${CHECKPOINT_USER_UMASK:-$(umask)}
export CHECKPOINT_USER_UMASK
umask 077
. "$root/hooks/lib.sh"
mode=${1:-}
[ $# -gt 0 ] && shift

case $mode in
ensure)
    ck_ensure 0 && exit 0
    ck_lock_alive && exit 0 # another process builds
    exit 1
    ;;

mcp | cli)
    if [ "$mode" = cli ] && [ "${1:-}" = doctor ]; then
        shift
        exec sh "$root/hooks/env.sh" doctor "$@"
    fi
    if ! ck_python; then
        ck_kick retry
        i=0
        if [ "$mode" = mcp ]; then n=25; else n=180; fi
        [ "$mode" = cli ] && echo "checkpoint: setting up its runtime (first run; usually under a minute)..." >&2
        s=
        while [ "$i" -lt "$n" ] && ! ck_python; do
            # No pixi: nothing to wait for. A failed build: give the retry
            # just started a moment to take the lock.
            s=$(ck_state)
            case $s in
            nopixi) break ;;
            failed) [ "$i" -ge 3 ] && break ;;
            esac
            sleep 1
            i=$((i + 1))
        done
        if ! ck_python; then
            # No pixi stays the reason: the build kicked above may hold its
            # lock by now ("building"), and it can only fail.
            [ "$s" = nopixi ] || s=$(ck_state)
            case $s in
            nopixi) echo "checkpoint: pixi isn't installed, so checkpoint can't set itself up; install it from https://pixi.sh and start a new session" >&2 ;;
            failed) echo "checkpoint: setting up its runtime failed (log: $ENVD/install.log); it is retried at the next session start" >&2 ;;
            *) echo "checkpoint: still setting up its runtime (first run); the job tools come back once it is done: reconnect jobs in /mcp, or start a new session" >&2 ;;
            esac
            exit 1
        fi
    fi
    if [ "$mode" = mcp ]; then ck_exec_py server; fi
    ck_exec_py "$@"
    ;;

run)
    ck_python || exit 75
    ck_exec_py "$@"
    ;;

state)
    ck_state
    ;;

doctor)
    sid=
    [ -n "${1:-}" ] && sid=$(ck_safe "$1")
    ck_env
    ver=$(sed -n 's/^[[:space:]]*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$root/.claude-plugin/plugin.json" 2>/dev/null | head -n 1)
    echo "plugin: check ${ver:-?} at $(cd "$root" 2>/dev/null && pwd -P)"
    if p=$(ck_find_pixi); then
        pv=$(ck_pixi_version "$p")
        # checkpoint supports the latest pixi; one older than requires-pixi is called out
        verdict=
        if [ -z "$pv" ]; then
            verdict="; version unknown"
        elif ck_pixi_older "$pv"; then
            verdict="; older than this version needs ($CK_PIXI_MIN): update it with $CK_PIXI_UPDATE"
        fi
        echo "pixi: $p (${pv:-?}$verdict)"
    else
        echo "pixi: not found (install the latest from https://pixi.sh)"
    fi
    case $(ck_state) in
    ready) rs="ready" ;;
    older)
        # ck_state says older whatever this version's build is doing
        if ck_lock_alive; then
            rs="building a new env for this version; the previous one runs meanwhile ($(ck_older))"
        elif [ -f "$ENVD/install.log" ]; then
            rs="this version's env failed to build: $(ck_fail_line) (log: $ENVD/install.log; retried at the next session start); the previous one runs meanwhile ($(ck_older))"
        else
            rs="this version's env isn't built yet; the previous one runs meanwhile ($(ck_older))"
        fi
        ;;
    building) rs="building (first run; log: $ENVD/install.log)" ;;
    failed) rs="setup failed: $(ck_fail_line) (log: $ENVD/install.log; retried at the next session start)" ;;
    nopixi) rs="missing, and pixi isn't installed" ;;
    *) rs="not built yet" ;;
    esac
    echo "runtime: $rs"
    echo "env: $ENVD"
    if [ -n "${CHECKPOINT_PYTHON:-}" ]; then
        echo "python: $CHECKPOINT_PYTHON (CHECKPOINT_PYTHON)"
    elif ck_python; then
        echo "python: $CKPY ($("$CKPY" -I -S -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo '?'))"
    fi
    # degraded/<session> is history (it stays until the session is pruned):
    # whether the gate is degraded now is whether a runtime answers now.
    mark=
    if [ -n "$sid" ] && [ -f "$st/degraded/$sid" ]; then
        mark=$({ LC_ALL=C tr -c '[:print:]' ' ' < "$st/degraded/$sid"; } 2>/dev/null | sed 's/ *$//' | cut -c 1-200)
    fi
    if ck_python; then
        if [ -n "$mark" ]; then
            echo "gate: normal (the runtime is ready); this session's gate last ran degraded at $mark"
        else
            echo "gate: normal"
        fi
    else
        echo "gate: degraded (the runtime isn't ready: $(ck_state)): a paused session's tool calls are held, others go ahead; queued pause commands pause at the next tool call${mark:+; last recorded for this session: $mark}"
    fi
    if [ -d "$st" ]; then
        m=$(ls -ld "$st" 2>/dev/null | cut -c 1-10)
        case $m in
        drwx------) echo "state: $st (private)" ;;
        *) echo "state: $st ($m; the next session start makes it private)" ;;
        esac
    else
        echo "state: $st (not created yet)"
    fi
    if [ -n "$sid" ] && [ -f "$st/flags/$sid" ]; then echo "pause: this session is paused"; fi
    # The journal's shell half: what waits in the spool, and failures.
    if [ -n "$sid" ]; then
        n=0
        [ -f "$st/spool/$sid/index" ] && n=$(ls "$st/spool/$sid" 2>/dev/null | grep -c '\.ev$')
        echo "spool: $n record(s) of this session waiting to be ingested ($st/spool/$sid)"
        ck_body "$sid"
        [ -n "$CK_BODY" ] && echo "spool: events are kept without their contents ($CK_BODY): which tool ran, its ids and sizes"
        if [ -f "$st/spool-errors/$sid" ]; then
            echo "spool: RECORDING FAILED since $({ LC_ALL=C tr -c '[:print:]' ' ' < "$st/spool-errors/$sid"; } 2>/dev/null | sed 's/ *$//' | cut -c 1-200)"
        elif [ -f "$st/spool-errors/$sid.last" ]; then
            # History: the window's events are lost (the journal's recording.gap says so too).
            echo "spool: last recording failure (recovered; its events are lost): $({ LC_ALL=C tr -c '[:print:]' ' ' < "$st/spool-errors/$sid.last"; } 2>/dev/null | sed 's/ *$//' | cut -c 1-300)"
        fi
        if [ -f "$st/journal-errors/$sid" ]; then
            echo "journal: WRITING FAILED: $(sed -n 's/.*"text": *"\([^"]*\)".*/\1/p' "$st/journal-errors/$sid" | head -n 1 | cut -c 1-200)"
        fi
    fi
    # The gateway's discovery file (CONTRACT-v1), checked as Python's
    # gateway.discover checks it (tests compare the verdicts): set but empty
    # turns the integration off.
    echo "gateway contract: $(ck_contract "$(ck_contract_path)")"
    ;;

contract-path)
    # The path alone (the mod's pause asks: mods can't read the password
    # database); '' when the integration is off. Nothing looks at the file.
    ck_contract_path
    echo
    ;;

flag)
    what=${1:-}
    sid=$(ck_safe "${2:-}")
    [ -n "$sid" ] || exit 2
    case $what in
    pause)
        [ -e "$st/flags/$sid" ] || ck_write "$st/flags/$sid" "$(ck_now) ${3:-mod}" || exit 1
        ;;
    now) ck_write "$st/now/$sid" "$(ck_now)" || exit 1 ;;
    noresume) ck_write "$st/noresume/$sid" "$(ck_now)" || exit 1 ;;
    clear-noresume) rm -f "$st/noresume/$sid" ;;
    *) exit 2 ;;
    esac
    ;;

spool)
    LC_ALL=C
    export LC_ALL
    sid=$(ck_safe "${1:-}")
    case ${2:-} in '' | *[!A-Za-z0-9._-]*) exit 2 ;; esac
    input=$(cat)
    [ -n "$sid" ] && [ -n "$input" ] || exit 2
    ck_spool "$sid" "$2" || { [ -n "$CK_SPOOL_SAY" ] && echo "$CK_SPOOL_SAY" >&2; exit 1; }
    ;;

*)
    echo "usage: env.sh ensure | mcp | cli ARGS | doctor [SESSION] | state | contract-path | flag ... | spool SESSION EVENT" >&2
    exit 2
    ;;
esac
exit 0
