# Sourced by fast.sh, watch.sh and env.sh (POSIX sh; defines functions and
# constants only). The caller sets $root (the plugin folder) and $st
# (CHECKPOINT_HOME, as state.home()).
#
# The runtime (docs/dev/ARCHITECTURE.md, "The runtime"): a pixi env built from a copy
# of pixi.toml + pixi.lock with `pixi install --locked`, in
#   $DATA/envs/<first 12 hex digits of sha256(pixi.lock)>/
#     pixi.toml, pixi.lock, install.log, .pixi/envs/default/, .ready, .warm, .used
# and run directly (no pixi on the hot path) once .ready exists. DATA is
# $CLAUDE_PLUGIN_DATA (Claude Code gives it to hooks and MCP servers), else
# $CHECKPOINT_PLUGIN_DATA (the terminal helper and the mod pass what
# SessionStart recorded), else ${CLAUDE_CONFIG_DIR:-~/.claude}/plugins/data/
# check-checkpoint (Claude Code's own folder for an install from the
# checkpoint marketplace). Never the plugin folder: an installed copy is
# replaced on update and may be read-only.

CK_ENV_KEEP_DAYS=7          # an env unused this long is removed (state.ENV_KEEP_DAYS)
CK_PIXI_MIN=0.68.0          # pixi.toml requires-pixi; oldest pixi that reads pixi.lock (format 7)
CK_PIXI_TESTED=0.82.0       # newest pixi `pixi install --locked` was tried with (docs/dev/ARCHITECTURE.md)

# The user's home from the password database (as state.gateway_env_path()):
# Claude Code may run with HOME pointing elsewhere.
ck_user_home() {
    _cku=$(id -un 2>/dev/null) || _cku=
    _ckh=
    case $_cku in
    '' | -* | *[!A-Za-z0-9._-]*) ;;
    *) eval "_ckh=~$_cku" ;;
    esac
    case $_ckh in
    /*) ;;
    *) _ckh=${HOME:-/nonexistent} ;;
    esac
    printf '%s' "$_ckh"
}

# A session or tool-use id as a file name (state.safe_id).
ck_safe() { printf '%s' "$1" | tr -c 'A-Za-z0-9_-' '_' | cut -c 1-128; }

ck_now() { date +%Y-%m-%dT%H:%M:%S%z; }

# Write FILE whole (temp file, then rename), under the caller's umask 077.
ck_write() {
    _ckd=${1%/*}
    mkdir -p "$_ckd" 2>/dev/null || return 1
    _ckt="$_ckd/.${1##*/}.$$.tmp"
    # (stderr first: a failed redirection's message, a full disk, stays out of the hook's stderr)
    if printf '%s\n' "$2" 2>/dev/null > "$_ckt" && mv -f "$_ckt" "$1" 2>/dev/null; then
        return 0
    fi
    rm -f "$_ckt" 2>/dev/null
    return 1
}

# Sets SHA: the first 12 hex digits of sha256(FILE); nolock000000 without one.
ck_sha12() {
    SHA=nolock000000
    [ -f "$1" ] || return 0
    if command -v sha256sum >/dev/null 2>&1; then _cks=$(sha256sum "$1")
    elif command -v openssl >/dev/null 2>&1; then _cks=$(openssl dgst -sha256 -r "$1")
    elif command -v shasum >/dev/null 2>&1; then _cks=$(shasum -a 256 "$1")
    else return 0; fi
    _cks=${_cks%%[!0-9a-f]*}
    [ "${#_cks}" -ge 12 ] || return 0
    while [ "${#_cks}" -gt 12 ]; do _cks=${_cks%?}; done
    SHA=$_cks
}

# Sets DATA, ENVD and PY (once). CHECKPOINT_ENVD (what SessionStart recorded
# in $st/rt/<session>) saves hashing the lock file again.
ck_env() {
    [ -n "${ENVD:-}" ] && return 0
    if [ -n "${CLAUDE_PLUGIN_DATA:-}" ]; then
        DATA=$CLAUDE_PLUGIN_DATA
    elif [ -n "${CHECKPOINT_PLUGIN_DATA:-}" ]; then
        DATA=$CHECKPOINT_PLUGIN_DATA
    elif [ -n "${CHECKPOINT_ENVD:-}" ]; then
        DATA=${CHECKPOINT_ENVD%/envs/*}
    else
        DATA="${CLAUDE_CONFIG_DIR:-${HOME:-/nonexistent}/.claude}/plugins/data/check-checkpoint"
    fi
    case ${CHECKPOINT_ENVD:-} in
    "$DATA"/envs/?*) ENVD=$CHECKPOINT_ENVD ;;
    *)
        ck_sha12 "$root/pixi.lock"
        ENVD="$DATA/envs/$SHA"
        ;;
    esac
    PY="$ENVD/.pixi/envs/default/bin/python3"
}

ck_ready() { [ -x "$PY" ] && [ -e "$ENVD/.ready" ]; }

# The newest other env that finished building, or "": while an update's env
# builds, the code (stdlib only) runs on the previous one.
ck_older() {
    _ckb=
    for _ckd in "$DATA/envs"/*; do
        [ -d "$_ckd" ] || continue
        [ "$_ckd" = "$ENVD" ] && continue
        case ${_ckd##*/} in *.*) continue ;; esac
        [ -x "$_ckd/.pixi/envs/default/bin/python3" ] && [ -e "$_ckd/.ready" ] || continue
        if [ -z "$_ckb" ] || [ -n "$(find "$_ckd/.ready" -newer "$_ckb/.ready" 2>/dev/null)" ]; then
            _ckb=$_ckd
        fi
    done
    printf '%s' "$_ckb"
}

# Sets CKPY to the interpreter that runs run.py, CKPX=1 for an env's (run
# isolated, its bytecode cached in DATA); returns 1 when none is ready.
# CHECKPOINT_PYTHON (tests) wins.
ck_python() {
    CKPY=
    CKPX=
    if [ -n "${CHECKPOINT_PYTHON:-}" ]; then
        CKPY=$CHECKPOINT_PYTHON
        return 0
    fi
    ck_env
    if ck_ready; then
        CKPY=$PY
    else
        _cko=$(ck_older)
        [ -n "$_cko" ] || return 1
        CKPY="$_cko/.pixi/envs/default/bin/python3"
        : > "$_cko/.used" 2>/dev/null
    fi
    CKPX=1
    return 0
}

# exec run.py ARGS... with CKPY (after ck_python). It replaces the shell:
# call it in a subshell or a pipeline, or as the last step.
ck_exec_py() {
    CHECKPOINT_USER_UMASK=${CHECKPOINT_USER_UMASK:-$(umask)}
    export CHECKPOINT_USER_UMASK
    if [ -n "$CKPX" ]; then
        exec "$CKPY" -I -S -X "pycache_prefix=$DATA/pycache" "$root/run.py" "$@"
    fi
    exec "$CKPY" "$root/run.py" "$@"
}

# -- building the env -----------------------------------------------------------
# One builder at a time: the lock is the folder $ENVD.building holding the
# builder's pid (pid) and pixi's (pixi). A lock whose processes are all gone
# (the builder was killed) is stale at once, never after a timeout; a folder
# without a pid file yet is a builder starting (stale after a minute).

ck_pid_ours() {
    kill -0 "$1" 2>/dev/null || return 1
    _ckc=$(ps -o command= -p "$1" 2>/dev/null) || return 0
    [ -n "$_ckc" ] || return 0
    case $_ckc in *env.sh* | *lib.sh* | *pixi* | *fast.sh*) return 0 ;; esac
    return 1
}

ck_lock_alive() {
    _ckl="$ENVD.building"
    [ -d "$_ckl" ] || return 1
    for _ckf in "$_ckl/pid" "$_ckl/pixi"; do
        _ckp=$(cat "$_ckf" 2>/dev/null) || continue
        case $_ckp in '' | *[!0-9]*) continue ;; esac
        ck_pid_ours "$_ckp" && return 0
    done
    [ -f "$_ckl/pid" ] && return 1
    [ -z "$(find "$_ckl" -prune -mmin +1 2>/dev/null)" ]
}

ck_take_lock() {
    _ckl="$ENVD.building"
    mkdir -p "$DATA/envs" 2>/dev/null || return 1
    if mkdir "$_ckl" 2>/dev/null; then
        echo $$ > "$_ckl/pid"
        return 0
    fi
    ck_lock_alive && return 1
    # Stale: break it under a short mutex, checking again inside, so two
    # processes that both saw it stale never both build.
    _ckm="$ENVD.break"
    if [ -d "$_ckm" ] && [ -n "$(find "$_ckm" -prune -mmin +1 2>/dev/null)" ]; then
        rmdir "$_ckm" 2>/dev/null
    fi
    mkdir "$_ckm" 2>/dev/null || return 1
    _ckr=1
    if ! ck_lock_alive; then
        rm -rf "$_ckl"
        if mkdir "$_ckl" 2>/dev/null; then
            echo $$ > "$_ckl/pid"
            _ckr=0
        fi
    fi
    rmdir "$_ckm" 2>/dev/null
    return $_ckr
}

ck_find_pixi() { sh "$root/hooks/pixi.sh" --check 2>/dev/null; }

# A finished attempt left its log but no .ready, and nobody builds now.
ck_build_failed() { [ -f "$ENVD/install.log" ] && [ ! -e "$ENVD/.ready" ] && ! ck_lock_alive; }

# Load the new env once: macOS checks each new binary and library at its first
# load (seconds); this pays it here instead of in a hook. Marked by .warm.
ck_warm() {
    [ -e "$ENVD/.warm" ] && return 0
    if "$PY" -I -S -X "pycache_prefix=$DATA/pycache" -c 'import sys; sys.path.insert(0, sys.argv[1]); import checkpoint.hooks, checkpoint.mcp_server, checkpoint.cli' \
        "$root/src" </dev/null >/dev/null 2>&1; then
        : > "$ENVD/.warm" 2>/dev/null
    fi
    return 0
}

# Remove envs unused for CK_ENV_KEEP_DAYS (the current one is kept).
ck_cleanup() {
    : > "$ENVD/.used" 2>/dev/null
    for _ckd in "$DATA/envs"/*; do
        [ -d "$_ckd" ] || continue
        [ "$_ckd" = "$ENVD" ] && continue
        case ${_ckd##*/} in *.*) continue ;; esac
        if [ -e "$_ckd/.used" ]; then _ckf="$_ckd/.used"; else _ckf=$_ckd; fi
        if [ -n "$(find "$_ckf" -prune -mtime +"$CK_ENV_KEEP_DAYS" 2>/dev/null)" ]; then
            rm -rf "$_ckd" "$_ckd.building" 2>/dev/null
        fi
    done
    return 0
}

# Holding the lock: build. pixi runs as a child whose pid is in the lock, so a
# killed builder whose pixi still runs keeps the lock until pixi is done.
ck_build() {
    _ckl="$ENVD.building"
    _ckpx=
    trap 'rm -rf "$_ckl"' EXIT
    trap '[ -n "$_ckpx" ] && kill "$_ckpx" 2>/dev/null; rm -rf "$_ckl"; exit 1' INT TERM HUP
    _ckrc=1
    mkdir -p "$ENVD" 2>/dev/null
    # The copies keep the plugin's modes: from a read-only plugin folder they
    # are read-only, and a later attempt (after a failed or killed one) can't
    # copy over them. So remove them first, and make the new ones writable.
    rm -f "$ENVD/pixi.toml" "$ENVD/pixi.lock" 2>/dev/null
    if ! _ckpi=$(ck_find_pixi); then
        echo "checkpoint: pixi not found on PATH or in the usual install folders; install it from https://pixi.sh" > "$ENVD/install.log"
    elif cp "$root/pixi.toml" "$root/pixi.lock" "$ENVD/" 2>"$ENVD/install.log" &&
        chmod u+w "$ENVD/pixi.toml" "$ENVD/pixi.lock" 2>>"$ENVD/install.log"; then
        "$_ckpi" install --locked --manifest-path "$ENVD/pixi.toml" > "$ENVD/install.log" 2>&1 </dev/null &
        _ckpx=$!
        echo "$_ckpx" > "$_ckl/pixi"
        wait "$_ckpx"
        _ckrc=$?
        _ckpx=
        if [ "$_ckrc" -eq 0 ] && [ -x "$PY" ]; then
            : > "$ENVD/.ready"
        else
            _ckrc=1
        fi
    fi
    rm -rf "$_ckl"
    trap - EXIT INT TERM HUP
    if [ "$_ckrc" -eq 0 ]; then
        ck_warm
        ck_cleanup
    fi
    return "$_ckrc"
}

# Build unless ready; with WAIT > 0, wait up to WAIT seconds for another
# builder. 0 once ready.
ck_ensure() {
    ck_env
    if ck_ready; then
        ck_warm
        ck_cleanup
        return 0
    fi
    _ckw=${1:-0}
    _cki=0
    while :; do
        if ck_take_lock; then
            ck_build
            return $?
        fi
        [ "$_cki" -ge "$_ckw" ] && return 1
        sleep 1
        _cki=$((_cki + 1))
        ck_ready && return 0
    done
}

# Start a build in the background, detached, unless the env is ready or a
# builder runs. With "retry", also after a failed attempt (else only when no
# attempt was made yet).
ck_kick() {
    [ -n "${CHECKPOINT_PYTHON:-}" ] && return 0
    ck_env
    ck_ready && return 0
    ck_lock_alive && return 0
    if [ "${1:-}" != retry ] && [ -f "$ENVD/install.log" ]; then return 0; fi
    nohup sh "$root/hooks/env.sh" ensure </dev/null >/dev/null 2>&1 &
    return 0
}

# What the runtime is doing, one word: ready, older (an update builds; the
# previous env runs), building, failed, nopixi, missing.
ck_state() {
    ck_env
    if ck_ready; then echo ready; return; fi
    if ck_lock_alive; then
        if [ -n "$(ck_older)" ]; then echo older; else echo building; fi
        return
    fi
    if [ -n "$(ck_older)" ]; then echo older; return; fi
    if ! ck_find_pixi >/dev/null; then echo nopixi; return; fi
    if [ -f "$ENVD/install.log" ]; then echo failed; return; fi
    echo missing
}

# Why the build failed, one printable line for a message: the log's first
# line that starts with "error" (pixi's summary, "Error:  x lock file not
# up-to-date ..."; its output ends with blank lines), else its last line that
# isn't blank (our own lines: no pixi, a failed copy).
ck_fail_line() {
    _ckt=$({ LC_ALL=C tr -c '[:print:]\n' ' ' < "$ENVD/install.log"; } 2>/dev/null | LC_ALL=C tr -s ' ' |
        sed 's/^ //; s/ $//' | grep -v '^$')
    _ckl=$(printf '%s\n' "$_ckt" | sed -n '/^[Ee][Rr][Rr][Oo][Rr]/{p;q;}')
    [ -n "$_ckl" ] || _ckl=$(printf '%s\n' "$_ckt" | tail -n 1)
    printf '%s' "${_ckl:-no output}" | tr -d '"\\' | cut -c 1-200
}

# -- the spool: capture without Python (capture.py ingests it) --------------------
# Every hook payload becomes one record of spool/<session>/: the payload in a
# file of its own (named after this process and a count of its records,
# created with noclobber, so no two writers ever share one), then one line
# naming it appended to
# spool/<session>/index. A line stays far below 1 KiB, and an append that
# small is one write() under sh, bash and dash (measured: 1 KiB lines from 16
# writers at once never mixed; 1025 bytes did), so the index stays whole.
# Python reads a file only once its index line exists. Line:
#   1 <TAB> EVENT <TAB> FILE <TAB> BYTES [<TAB> h=HASH] [<TAB> cut=BYTES] [<TAB> meta=1]
CK_SPOOL_MAX=262144         # payload bytes kept per record (capture.SPOOL_MAX)
CK_HASH_MAX=16777216        # files hashed for before/after (capture.HASH_MAX)
CK_SPOOL_SAY=               # set when a record couldn't be written: the hook says so
# A session's spool holds at most CK_SPOOL_KEEP_COUNT records and about
# CK_SPOOL_KEEP_BYTES stored bytes (capture.SPOOL_KEEP_COUNT, SPOOL_KEEP_BYTES;
# CHECKPOINT_SPOOL_KEEP_COUNT and _BYTES make them small for tests): past
# either, the oldest are dropped down to half, with a recording.gap record
# (ck_spool_trim). A spool nothing was written to for CK_SPOOL_KEEP_DAYS whose
# session's Claude process is gone is removed at a session start
# (ck_spool_prune; capture.SPOOL_KEEP_DAYS).
CK_SPOOL_KEEP_COUNT=10000
CK_SPOOL_KEEP_BYTES=52428800
CK_SPOOL_KEEP_DAYS=7
CK_BODY_BUILD_MIN=60        # a build running longer no longer counts as a runtime on its way (ck_body)
# What a record without its contents keeps (ck_spool, while ck_body says no):
# these top-level members, the hooks' and the mod's (names, ids, decisions).
CK_META_KEYS='|session_id|transcript_path|cwd|hook_event_name|permission_mode|tool_name|tool_use_id|agent_id|agent_type|prompt_id|source|notification_type|trigger|turnId|tool|decision|answered|phase|'
CK_BODY_SET=                # ck_body ran in this process (CK_BODY holds its answer)
CK_TRIMMING=                # ck_spool_trim runs: its own record doesn't trim again

# Whether payloads are kept whole: sets CK_BODY (once per process) to '' when
# they are, else to why not. A payload waits in the spool, unscrubbed, for
# Python to turn it into journal records, so it is kept only while Python is
# there or on its way: a runtime is ready (or an update's older env runs), a
# build has run for less than CK_BODY_BUILD_MIN minutes, or none was tried
# yet (the session start starts one). Otherwise (no pixi, a failed build, a
# build hung for an hour) a record keeps the event, the tool, the ids, the
# session's paths and the payload's size, never its contents. With a runtime
# ready it costs no process (rt/<session> read with `read`, two file tests).
ck_body() {
    [ -n "$CK_BODY_SET" ] && return 0
    CK_BODY_SET=1
    CK_BODY=
    [ -n "${CHECKPOINT_PYTHON:-}" ] && return 0
    if [ -z "${ENVD:-}" ] && [ -z "${CHECKPOINT_ENVD:-}" ] && [ -n "$1" ] && [ -f "$st/rt/$1" ]; then
        { IFS= read -r CHECKPOINT_ENVD < "$st/rt/$1"; } 2>/dev/null
    fi
    ck_env
    ck_ready && return 0
    if [ -d "$ENVD.building" ] &&
        [ -n "$(find "$ENVD.building" -prune -mmin -"$CK_BODY_BUILD_MIN" 2>/dev/null)" ]; then
        return 0
    fi
    [ -n "$(ck_older)" ] && return 0
    if [ -d "$ENVD.building" ]; then
        CK_BODY="no runtime: its setup has run for over an hour"
    elif [ -f "$ENVD/install.log" ]; then
        _ckl=
        { IFS= read -r _ckl < "$ENVD/install.log"; } 2>/dev/null
        case $_ckl in
        'checkpoint: pixi not found'*) CK_BODY="no runtime: pixi not found" ;;
        *) CK_BODY="no runtime: its setup failed" ;;
        esac
    fi
    return 0
}

# ck_spool SESSION EVENT [EXTRA]: spool $input (the hook payload); while
# ck_body says no, only what the payload says about itself (meta=1).
ck_spool() {
    [ -n "$1" ] || return 0
    ck_body "$1" # first: what it calls (ck_env, ck_older) sets _ck* variables of its own
    _cks="$st/spool/$1"
    if [ ! -d "$_cks" ]; then
        mkdir -p "$_cks" 2>/dev/null || { ck_spool_failed "$1" "can't create $_cks"; return 1; }
    fi
    CK_SPOOL_N=$((${CK_SPOOL_N:-0} + 1)) # records of this process (one per hook; env.sh spool too)
    _ckn="$$.$CK_SPOOL_N"
    _ckb=${#input}
    _ckx=
    if [ -n "$CK_BODY" ] && [ "$2" != recording.gap ]; then
        # The payload's top-level string members named in CK_META_KEYS (the
        # payload cut into JSON tokens, strings whole, so nothing nested in
        # tool_input or a result is ever taken for one; a value over 300
        # bytes is left out) and its size: no tool input or result, prompt or
        # message. Our recording.gap records stay whole.
        _ckp=$(printf '%s' "$input" | grep -oE '"([^"\\]|\\.)*"|[][{}:,]' | awk -v keys="$CK_META_KEYS" '
            $0 == "{" || $0 == "[" { d++; if (d == 1) want = 1; if (d == 2) hk = 0; next }
            $0 == "}" || $0 == "]" { d--; next }
            d != 1 { next }
            $0 == "," { want = 1; hk = 0; next }
            $0 == ":" { next }
            want { k = substr($0, 2, length($0) - 2); want = 0; hk = 1; next }
            hk { hk = 0; if (length($0) <= 302 && index(keys, "|" k "|")) printf "\"%s\":%s,", k, $0 }')
        _ckp="{$_ckp\"checkpoint_meta\":{\"bytes\":$_ckb,\"why\":\"$CK_BODY\"}}"
        _ckx="	meta=1"
    fi
    _ckf=
    _ckt=0
    set -C
    while [ "$_ckt" -lt 8 ]; do
        if [ -e "$_cks/$_ckn.ev" ]; then # a dead hook's file with our pid
            _ckt=$((_ckt + 1))
            _ckn="$$.$CK_SPOOL_N.$_ckt"
            continue
        fi
        if [ -n "$_ckx" ]; then
            { printf '%s\n' "$_ckp" > "$_cks/$_ckn.ev"; } 2>/dev/null && _ckf=$_ckn.ev
        elif [ "$_ckb" -gt "$CK_SPOOL_MAX" ] && [ "$2" != recording.gap ]; then
            { printf '%s' "$input" | head -c "$CK_SPOOL_MAX" > "$_cks/$_ckn.ev"; } 2>/dev/null && _ckf=$_ckn.ev
        else
            { printf '%s\n' "$input" > "$_cks/$_ckn.ev"; } 2>/dev/null && _ckf=$_ckn.ev
        fi
        break
    done
    set +C
    if [ -z "$_ckf" ]; then
        rm -f "$_cks/$_ckn.ev" 2>/dev/null
        ck_spool_failed "$1" "can't write to $_cks (disk full?)"
        return 1
    fi
    _ckc=
    if [ -n "$_ckx" ]; then
        _ckw=$((${#_ckp} + 1))
    elif [ "$_ckb" -gt "$CK_SPOOL_MAX" ] && [ "$2" != recording.gap ]; then
        _ckc="	cut=$_ckb"
        _ckw=$CK_SPOOL_MAX
    else
        _ckw=$((_ckb + 1))
    fi
    if ! printf '1\t%s\t%s\t%s%s%s%s\n' "$2" "$_ckf" "$_ckb" "${3:+	$3}" "$_ckc" "$_ckx" >> "$_cks/index" 2>/dev/null; then
        ck_spool_failed "$1" "can't append to $_cks/index (disk full?)"
        return 1
    fi
    # The spool's size (records, stored bytes) in spool/<session>/.size, kept
    # without a process: read, add, write. Hooks at once can lose an update
    # (close enough for a cap); Python writes what is left after each ingest.
    # A read racing a write can see a torn line ("2099 06216"): leading zeros
    # go (sh arithmetic reads 078 as octal and kills the hook, before the
    # paused deny), anything else not a number counts as 0.
    _ckq=
    _ckz=
    { read -r _ckq _ckz < "$_cks/.size"; } 2>/dev/null
    case $_ckq in '' | *[!0-9]*) _ckq=0 ;; *) _ckq=${_ckq#"${_ckq%%[!0]*}"} ;; esac
    case $_ckz in '' | *[!0-9]*) _ckz=0 ;; *) _ckz=${_ckz#"${_ckz%%[!0]*}"} ;; esac
    _ckq=$((${_ckq:-0} + 1))
    _ckz=$((${_ckz:-0} + _ckw))
    printf '%s %s\n' "$_ckq" "$_ckz" 2>/dev/null > "$_cks/.size"
    if [ -z "$CK_TRIMMING" ]; then
        _ckm=${CHECKPOINT_SPOOL_KEEP_COUNT:-$CK_SPOOL_KEEP_COUNT}
        _ckmb=${CHECKPOINT_SPOOL_KEEP_BYTES:-$CK_SPOOL_KEEP_BYTES}
        case $_ckm in '' | *[!0-9]* | 0) _ckm=$CK_SPOOL_KEEP_COUNT ;; esac
        case $_ckmb in '' | *[!0-9]* | 0) _ckmb=$CK_SPOOL_KEEP_BYTES ;; esac
        if [ "$_ckq" -gt "$_ckm" ] || [ "$_ckz" -gt "$_ckmb" ]; then
            # The count can be torn (it read "152482 4993" under 16 writers):
            # counted for real before anything is dropped.
            ck_spool_count "$1"
            if [ "$_ckq" -gt "$_ckm" ] || [ "$_ckz" -gt "$_ckmb" ]; then
                ck_spool_trim "$1"
            fi
        fi
    fi
    [ -e "$st/spool-errors/$1" ] && ck_spool_recovered "$1"
    return 0
}

# The spool's real size (its payload files and their bytes, as capture's
# _write_size counts them) into _ckq and _ckz and spool/<session>/.size: when
# the running count says the cap is passed (two processes, then; never on the
# common path).
ck_spool_count() {
    _ckd="$st/spool/$1"
    set -- "$_ckd"/*.ev
    _ckq=$#
    [ -e "$1" ] || _ckq=0
    _ckz=$(cd "$_ckd" 2>/dev/null && ls -ln 2>/dev/null | awk '$NF ~ /\.ev$/ { s += $5 } END { printf "%d", s }')
    case $_ckz in '' | *[!0-9]*) _ckz=0 ;; esac
    printf '%s %s\n' "$_ckq" "$_ckz" 2>/dev/null > "$_ckd/.size"
}

# The session's spool passed its cap (called by ck_spool, which set _ckq and
# _ckz to its size, _ckm and _ckmb to the caps): drop the oldest payloads, in
# index order, until it holds half of each. A recording.gap record naming
# them goes in first (Python counts as dropped the ones it then finds gone),
# then they are removed; our own recording.gap records stay. One trimmer at
# a time (spool/<session>/.trim; one a killed hook left is broken after a
# minute). Its cost follows what it drops, not the session's whole history:
# with no runtime (nothing else rotates the index) the index is moved to
# index.s<n> after each trim (Python reads those first, capture.py), and a
# rotated index read to its end (everything it names dropped or ingested) is
# skipped from then on (spool/<session>/.trimdone: the highest such n).
ck_spool_trim() {
    _ckd="$st/spool/$1"
    if ! mkdir "$_ckd/.trim" 2>/dev/null; then
        [ -n "$(find "$_ckd/.trim" -prune -mmin +1 2>/dev/null)" ] || return 0
        rm -rf "${_ckd:?}/.trim" 2>/dev/null
        mkdir "$_ckd/.trim" 2>/dev/null || return 0
    fi
    CK_TRIMMING=1
    _ckwc=$((_ckm / 2))
    _ckwb=$((_ckmb / 2))
    _ckdn=0
    _ckdb=0
    _ckhw=
    { read -r _ckhw < "$_ckd/.trimdone"; } 2>/dev/null
    case $_ckhw in '' | *[!0-9]*) _ckhw=0 ;; *) _ckhw=${_ckhw#"${_ckhw%%[!0]*}"} ;; esac
    _ckhw=${_ckhw:-0}
    _ckh0=$_ckhw
    _ckhi=$_ckhw # the highest index.s<n> there is (or was: never reused below .trimdone)
    _ckstop=
    : > "$_ckd/.trim/list"
    for _cki in "$_ckd"/index.* "$_ckd/index"; do
        [ -f "$_cki" ] || continue
        _ckg=
        case ${_cki##*/} in
        index.s[0-9]*)
            _ckg=${_cki##*/index.s}
            while :; do case $_ckg in 0?*) _ckg=${_ckg#0} ;; *) break ;; esac; done
            case $_ckg in *[!0-9]*) _ckg= ;; esac
            ;;
        esac
        if [ -n "$_ckg" ]; then
            [ "$_ckg" -gt "$_ckhi" ] && _ckhi=$_ckg
            [ "$_ckg" -le "$_ckhw" ] && continue
        fi
        [ -n "$_ckstop" ] && continue # (still counting the rotated ones' numbers)
        while IFS='	' read -r _ckv _cke _ckf _ckb _ckr; do
            if [ "$_ckq" -le "$_ckwc" ] && [ "$_ckz" -le "$_ckwb" ]; then
                _ckstop=1
                break
            fi
            case $_ckf in */* | '') continue ;; *.ev) ;; *) continue ;; esac
            [ "$_cke" = recording.gap ] && continue
            [ -e "$_ckd/$_ckf" ] || continue
            case $_ckb in '' | *[!0-9]*) _ckb=0 ;; esac
            case $_ckr in
            *meta=1*) _ckw=512 ;;
            *) if [ "$_ckb" -gt "$CK_SPOOL_MAX" ]; then _ckw=$CK_SPOOL_MAX; else _ckw=$((_ckb + 1)); fi ;;
            esac
            printf '%s\n' "$_ckf" >> "$_ckd/.trim/list"
            _ckdn=$((_ckdn + 1))
            _ckdb=$((_ckdb + _ckw))
            _ckq=$((_ckq - 1))
            _ckz=$((_ckz - _ckw))
        done < "$_cki"
        # Read to its end (and every index before it): nothing in it is left to drop.
        [ -z "$_ckstop" ] && [ -n "$_ckg" ] && _ckhw=$_ckg
    done
    _ckstop=
    if [ "$_ckdn" -gt 0 ]; then
        _ckrq=$_ckq
        _ckrz=$_ckz
        _cksv=$input
        input="{\"hook_event_name\":\"recording.gap\",\"session_id\":\"$1\",\"what\":\"dropped\",\"events\":$_ckdn,\"bytes\":$_ckdb,\"at\":\"$(ck_now)\",\"why\":\"over the spool's cap ($_ckm records or $_ckmb bytes)\",\"files\":[$(sed 's/.*/"&"/' "$_ckd/.trim/list" | paste -s -d , -)]}"
        _ckgs=$((${#input} + 1))
        ck_spool "$1" recording.gap
        input=$_cksv
        _ckd="$st/spool/$1" # (ck_spool's recovery path, ck_arm, uses _ckd too)
        (cd "$_ckd" && xargs rm -f < .trim/list) 2>/dev/null
        [ "$_ckrq" -lt 0 ] && _ckrq=0
        [ "$_ckrz" -lt 0 ] && _ckrz=0
        printf '%s %s\n' "$((_ckrq + 1))" "$((_ckrz + _ckgs))" 2>/dev/null > "$_ckd/.size"
    else
        # Nothing left to drop (only our own gap records): look again half a cap later.
        printf '%s %s\n' "$_ckwc" "$_ckwb" 2>/dev/null > "$_ckd/.size"
    fi
    if [ -n "$CK_BODY" ] && [ -s "$_ckd/index" ]; then
        # No runtime (ck_body): the index goes to the next index.s<n>.
        _ckhi=$((_ckhi + 1))
        mv -f "$_ckd/index" "$_ckd/$(printf 'index.s%06d' "$_ckhi")" 2>/dev/null
    fi
    [ "$_ckhw" != "$_ckh0" ] && printf '%s\n' "$_ckhw" 2>/dev/null > "$_ckd/.trimdone"
    rm -rf "${_ckd:?}/.trim" 2>/dev/null
    CK_TRIMMING=
    return 0
}

# At a session start: remove the spools nothing was written to for
# CK_SPOOL_KEEP_DAYS (a folder's time moves with every record: each is a new
# file) whose session's Claude process is gone, also when no runtime will
# ever ingest them (with one, the SessionStart hook's capture.ingest_dead has
# had them first). ck_spool_prune SESSION: the one starting, never removed.
ck_spool_prune() {
    [ -d "$st/spool" ] || return 0
    find "$st/spool" -mindepth 1 -maxdepth 1 -type d -mmin +$((CK_SPOOL_KEEP_DAYS * 1440)) 2>/dev/null |
        while IFS= read -r _ckd; do
            _ckn=${_ckd##*/}
            [ "$_ckn" = "$1" ] && continue
            _ckp=$(grep -o '"claude_pid": *[0-9][0-9]*' "$st/sessions/$_ckn.json" 2>/dev/null | grep -o '[0-9]*$')
            if [ -n "$_ckp" ] && kill -0 "$_ckp" 2>/dev/null; then
                # Its pid runs: still that session, unless ps says it is something else now.
                case $(ps -o command= -p "$_ckp" 2>/dev/null) in
                '' | *claude*) continue ;;
                esac
            fi
            rm -rf "${_ckd:?}"
        done
    return 0
}

# A record couldn't be written: spool-errors/<session> ("<time> <why>": since
# when recording fails) for /check:doctor and the status line, and
# CK_SPOOL_SAY for the hook's own output: once, when the marker is written
# (every time while nothing can record it). On a full disk the marker can't
# be written as a new file, so the reserve armed at session start becomes it
# (a rename and an overwrite in place: no new blocks on most filesystems).
ck_spool_failed() {
    _ckm="$st/spool-errors/$1"
    [ -e "$_ckm" ] && return 0 # recorded since the window's first failure: said then
    CK_SPOOL_SAY="checkpoint: couldn't record this event ($2); its record is lost and recording stays behind until that is fixed. /check:doctor says more."
    ck_write "$_ckm" "$(ck_now) $2" && return 0
    _ckr="$st/spool-errors/.reserve.$1"
    if [ -f "$_ckr" ] && mv -f "$_ckr" "$_ckm" 2>/dev/null; then
        printf '%s\n' "$(ck_now) $2" 1<>"$_ckm" 2>/dev/null
    fi
    return 0
}

# Recording works again after a failure: the failure becomes history
# (spool-errors/<session>.last: since when, until when, why; /check:doctor),
# the journal gets a record of the window whose events are lost (a spooled
# `recording.gap` event), and the reserve is armed again. The marker stays
# when its history can't be written yet (the next record tries again).
ck_spool_recovered() {
    _ckm="$st/spool-errors/$1"
    _ckl=
    { IFS= read -r _ckl < "$_ckm"; } 2>/dev/null
    _ckl=$(printf '%s' "$_ckl" | LC_ALL=C tr -cd '[:print:]' | tr -d '"\\' | sed 's/ *$//' | cut -c 1-300)
    _ckw=$(ck_now)
    ck_write "$st/spool-errors/$1.last" "${_ckl:-?}; recording worked again at $_ckw" || return 0
    rm -f "$_ckm" 2>/dev/null
    ck_arm "$1"
    _cksv=$input
    input=$(printf '{"hook_event_name":"recording.gap","session_id":"%s","failed":"%s","recovered":"%s","what":"spool"}' \
        "$1" "$_ckl" "$_ckw")
    ck_spool "$1" recording.gap
    input=$_cksv
}

# The reserves for spool-errors/<session> and journal-errors/<session>
# (state.RESERVE_BYTES of spaces, written while there is room: session start
# and after a recovery) that a failure on a full disk turns into its marker.
CK_RESERVE_BYTES=1024
ck_arm() {
    [ -n "$1" ] || return 0
    for _ckd in "$st/spool-errors" "$st/journal-errors"; do
        [ -e "$_ckd/.reserve.$1" ] && continue
        mkdir -p "$_ckd" 2>/dev/null || continue
        _ckt="$_ckd/..reserve.$1.$$.tmp"
        if printf "%${CK_RESERVE_BYTES}s" '' 2>/dev/null > "$_ckt" && mv -f "$_ckt" "$_ckd/.reserve.$1" 2>/dev/null; then
            continue
        fi
        rm -f "$_ckt" 2>/dev/null
    done
    return 0
}

# ck_hash FILE: the file's state for a before/after record: a sha256, absent,
# dir, unreadable, big:<bytes> (over CK_HASH_MAX) or nohash (no tool for it).
ck_hash() {
    if [ ! -e "$1" ]; then printf absent; return; fi
    if [ -d "$1" ]; then printf dir; return; fi
    if [ ! -f "$1" ] || [ ! -r "$1" ]; then printf unreadable; return; fi
    _ckz=$(wc -c < "$1" 2>/dev/null)
    _ckz=${_ckz##*[!0-9]}
    if [ "${_ckz:-0}" -gt "$CK_HASH_MAX" ]; then printf 'big:%s' "$_ckz"; return; fi
    if command -v sha256sum >/dev/null 2>&1; then _ckh=$(sha256sum < "$1" 2>/dev/null)
    elif [ -x /sbin/sha256sum ]; then _ckh=$(/sbin/sha256sum < "$1" 2>/dev/null)
    elif command -v openssl >/dev/null 2>&1; then _ckh=$(openssl dgst -sha256 -r < "$1" 2>/dev/null)
    elif command -v shasum >/dev/null 2>&1; then _ckh=$(shasum -a 256 < "$1" 2>/dev/null)
    else printf nohash; return; fi
    _ckh=${_ckh%%[!0-9a-f]*}
    if [ "${#_ckh}" -eq 64 ]; then printf '%s' "$_ckh"; else printf unreadable; fi
}

# Record that a hook ran without its Python half (the gate degraded):
# degraded/<session> holds "<time> <why>" for /check:status, /check:doctor and
# the next SessionStart.
ck_degraded() {
    [ -n "$1" ] || return 0
    ck_write "$st/degraded/$1" "$(ck_now) $2"
}

# Where the gateway's daemon writes its discovery file (CONTRACT-v1), as
# Python's gateway.contract_path: CHECKPOINT_GATEWAY_CONTRACT (set but empty:
# '', the integration is off), else ~/.config/gateway/run/contract.json in the
# password database's home (the gateway's; Claude Code may run with HOME
# elsewhere). Computes the path only (the mod asks env.sh contract-path).
ck_contract_path() {
    if [ "${CHECKPOINT_GATEWAY_CONTRACT+set}" = set ]; then
        case $CHECKPOINT_GATEWAY_CONTRACT in
        '' | /*) printf '%s' "$CHECKPOINT_GATEWAY_CONTRACT" ;;
        *) printf '%s/%s' "$(pwd)" "$CHECKPOINT_GATEWAY_CONTRACT" ;;
        esac
    else
        printf '%s/.config/gateway/run/contract.json' "$(ck_user_home)"
    fi
}

# ck_contract PATH: the gateway's discovery file (CONTRACT-v1) as one line,
# "<status>: <words>", with the statuses and order of checks of Python's
# gateway.discover (off, absent, unsafe, unreadable, dead, unknown_contract,
# mismatch, available). sh can't parse JSON: it reads the few fields with
# sed; a file Python can't parse may get another "not available" status here.
ck_contract() {
    _f=$1
    if [ -z "$_f" ]; then
        echo "off: turned off (CHECKPOINT_GATEWAY_CONTRACT is set but empty)"
        return 0
    fi
    if [ ! -e "$_f" ] && [ ! -L "$_f" ]; then
        echo "absent: no contract file ($_f): the gateway isn't running or isn't installed; jobs use checkpoint's own runner"
        return 0
    fi
    if [ -L "$_f" ] || [ ! -f "$_f" ]; then
        echo "unsafe: its contract file ($_f) isn't a regular file"
        return 0
    fi
    _m=$(ls -ln "$_f" 2>/dev/null | cut -c 1-10)
    case $_m in
    ?????w???? | ????????w?) _w=1 ;;
    *) _w= ;;
    esac
    if [ ! -O "$_f" ] || [ -n "$_w" ]; then
        echo "unsafe: its contract file ($_f) isn't yours alone (owner or mode): not used"
        return 0
    fi
    _t=$(dd bs=65536 count=1 < "$_f" 2>/dev/null | tr '\n\r\t' '   ')
    case $_t in
    *'{'*'}'*) ;;
    *)
        echo "unreadable: its contract file can't be read"
        return 0
        ;;
    esac
    _pid=$(printf '%s' "$_t" | sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -n 1)
    if [ -z "$_pid" ] || [ "$_pid" = 0 ]; then
        echo "dead: its contract file names no daemon pid"
        return 0
    fi
    if ! kill -0 "$_pid" 2>/dev/null; then
        echo "dead: its contract file is stale: the daemon (pid $_pid) isn't running"
        return 0
    fi
    _cid=$(printf '%s' "$_t" | sed -n 's/.*"contract"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
    if [ "$_cid" != gateway.v1 ]; then
        echo "unknown_contract: unknown contract '${_cid:-none}' (needs gateway.v1)"
        return 0
    fi
    _v=$(printf '%s' "$_t" | sed -n 's/.*"versions"[[:space:]]*:[[:space:]]*\[\([^]]*\)\].*/\1/p' | head -n 1 | tr -d ' ')
    case ",$_v," in
    *,1,*) ;;
    *)
        _h=$(printf '%s' "$_v" | sed 's/,/, /g')
        echo "mismatch: gateway contract mismatch (has ${_h:-none}, needs 1); checkpoint works alone"
        return 0
        ;;
    esac
    _s=$(printf '%s' "$_t" | sed -n 's/.*"socket"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
    if [ -z "$_s" ] || [ ! -S "$_s" ]; then
        echo "dead: its socket (${_s:-none}) is missing"
        return 0
    fi
    if [ ! -O "$_s" ]; then
        echo "unsafe: its socket isn't yours: not used"
        return 0
    fi
    _gv=$(printf '%s' "$_t" | sed -n 's/.*"gateway_version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
    _ep=$(printf '%s' "$_t" | sed -n 's/.*"epoch"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -n 1)
    echo "available: gateway ${_gv:-?}, contract v1, pid $_pid, epoch ${_ep:-?} ($_f); new jobs go to the gateway when its daemon serves the session (/check:status says whether it does)"
}
