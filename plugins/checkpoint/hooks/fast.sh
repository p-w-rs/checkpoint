#!/bin/sh
# Shell pre-filter for every checkpoint hook. Usage: fast.sh EVENT
#
# PreToolUse and PostToolUse run on every tool call, so the common path (not
# paused, nothing queued) must not start Python. This script checks flag files
# and counters and only calls `run.py hook-<event>` when there is something
# to do. It always exits 0. What it prints is Python's answer, plus answers of
# its own: allow for the job tools that only read or stop jobs; the session
# start while the runtime isn't ready (hooks/lib.sh, env.sh); and, when
# Python can't answer (no runtime yet, crashed, timed out), the fail-safe
# half: while paused (or a pause command waits in the queue) every tool call
# is denied here, a pause command typed is recorded here and a message the
# user typed resumes; otherwise the call goes ahead and degraded/<session>
# records that the gate ran without Python (CLAUDE.md invariant 1).
# POSIX sh (dash, bash, BSD and GNU tools); paths agree with state.py.
ev=$1
case $0 in
*/*) root=${0%/*}/.. ;;
*) root=.. ;;
esac
st=${CHECKPOINT_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/checkpoint}
work=${CHECKPOINT_WORK_DIR:-.work}
LC_ALL=C
export LC_ALL
CHECKPOINT_USER_UMASK=${CHECKPOINT_USER_UMASK:-$(umask)}
CHECKPOINT_HOOK_PPID=$PPID
export CHECKPOINT_USER_UMASK CHECKPOINT_HOOK_PPID
umask 077
. "$root/hooks/lib.sh"
out=
errf=
CKPY=
# How long any hook waits for Python (Claude Code's own timeouts in
# hooks.json are 30 s, 60 s for SessionStart, UserPromptSubmit and Stop; tests
# shorten this). Every hook that waits for Python waits through py_to: a hung
# runtime costs a hook this long at most, never Claude Code's whole timeout
# (which would also leave the hook's processes behind).
deadline=${CHECKPOINT_HOOK_DEADLINE:-20}
case $deadline in '' | *[!0-9]* | 0) deadline=20 ;; esac
# How long a background Python moment (bg_py: ingest, snap) may run before it
# is stopped (CHECKPOINT_BG_DEADLINE, seconds): a hung one never piles up.
bgdeadline=${CHECKPOINT_BG_DEADLINE:-300}
case $bgdeadline in '' | *[!0-9]* | 0) bgdeadline=300 ;; esac
input=$(cat)

# First "key":"value" string in the payload (Claude Code puts session_id,
# transcript_path, cwd first; tool_input comes later).
field() {
    printf '%s' "$input" | grep -o "\"$1\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -n 1 |
        sed 's/^[^:]*:[[:space:]]*"//; s/"$//'
}

# Text for a JSON string: strips what would break it.
esc() { printf '%s' "$1" | tr -d '"\\' | tr '\n\t' '  '; }

# A message for the user (hook JSON).
say() { printf '{"systemMessage":"%s"}\n' "$(esc "$1")"; }

# Context for the model at UserPromptSubmit, and a message for the user.
prompt_ctx() {
    if [ -n "${2:-}" ]; then
        printf '{"systemMessage":"%s","hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"%s"}}\n' \
            "$(esc "$2")" "$(esc "$1")"
    else
        printf '{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"%s"}}\n' "$(esc "$1")"
    fi
}

deny() {
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "$(esc "$1")"
}

# The env SessionStart recorded for this session (saves hashing pixi.lock).
rt_env() {
    if [ -z "${CHECKPOINT_ENVD:-}" ] && [ -f "$st/rt/$sid" ]; then
        # read, not $(cat): no process on the every-call path
        { IFS= read -r CHECKPOINT_ENVD < "$st/rt/$sid"; } 2>/dev/null
    fi
}

# run.py ARGS with the payload on stdin, with a deadline (seconds); 75 when
# no runtime is ready, 124 on timeout. Claude Code would kill this hook at
# its own timeout and take no answer as allow (and leave the hung Python and
# this script's subshell behind), so the hook stops Python itself first.
py_to() {
    _s=$1
    shift
    if [ -z "$CKPY" ]; then
        rt_env
        ck_python || return 75
    fi
    mkdir -p "$st" 2>/dev/null
    _o="$st/.py.$$.out"
    printf '%s' "$input" | (ck_exec_py "$@") > "$_o" 2>"${errf:-/dev/null}" &
    _p=$!
    _n=0
    _max=$((_s * 50))
    while kill -0 "$_p" 2>/dev/null; do
        if [ "$_n" -ge "$_max" ]; then
            kill "$_p" 2>/dev/null
            rm -f "$_o"
            return 124
        fi
        sleep 0.02
        _n=$((_n + 1))
    done
    wait "$_p"
    _rc=$?
    cat "$_o" 2>/dev/null
    rm -f "$_o"
    return $_rc
}

# Why Python gave no answer, for degraded/<session> and messages.
why() {
    case $1 in
    75) echo "its runtime isn't ready ($(ck_state))" ;;
    124) echo "Python timed out" ;;
    *) echo "Python failed (exit $1)" ;;
    esac
}

# The transcript tail exactly as transcript.tail_lines reads it: the last
# 262144 bytes (TAIL_BYTES), minus the first line when the cut splits it.
tail_tp() {
    sz=$(wc -c < "$1" 2>/dev/null)
    sz=${sz##*[!0-9]}
    if [ "${sz:-0}" -gt 262144 ]; then
        tail -c 262144 "$1" 2>/dev/null | sed 1d
    else
        cat "$1" 2>/dev/null
    fi
}

# Forget this session's guarded foreground calls: a new prompt or the end of
# the turn means none of the main agent's is still running (a cancelled call
# fires no PostToolUse). Subagents' calls are left alone.
clean_running() {
    for f in "$st/running"/*.json; do
        [ -f "$f" ] || continue
        grep -q "\"session_id\"[[:space:]]*:[[:space:]]*\"$sid\"" "$f" 2>/dev/null || continue
        grep -q '"agent_id"[[:space:]]*:' "$f" 2>/dev/null && continue
        rm -f "$f"
    done
}

# POSIX single quotes for the terminal helper.
q() {
    case $1 in
    *\'*) printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")" ;;
    *) printf "'%s'" "$1" ;;
    esac
}

# $st/checkpoint: the terminal helper (status, pause all, resume all, doctor
# with no idle session to type into), pointing at this copy of the plugin.
write_helper() {
    _r=$(cd "$root" 2>/dev/null && pwd -P) || return 0
    ck_write "$st/checkpoint" "#!/bin/sh
# checkpoint's terminal helper (rewritten at every Claude Code session start):
#   $st/checkpoint status | pause all [now] | pause <session> [now] | resume all | resume <session> | doctor
CHECKPOINT_HOME=$(q "$st"); CHECKPOINT_PLUGIN_DATA=$(q "$DATA"); export CHECKPOINT_HOME CHECKPOINT_PLUGIN_DATA
if [ -f $(q "$_r/hooks/env.sh") ]; then exec sh $(q "$_r/hooks/env.sh") cli \"\$@\"; fi
echo 'checkpoint: the plugin this helper points at is gone (uninstalled, or updated since the last session); start a Claude Code session with checkpoint to renew it' >&2
exit 1" && chmod 700 "$st/checkpoint" 2>/dev/null
}

sid=$(ck_safe "$(field session_id)")
paused() { [ -e "$st/flags/$sid" ]; }

# The tool of a tool event, only when the payload has exactly one tool_name
# key, so nothing nested in tool_input can pose as one.
tool=
tool_of() {
    _names=$(printf '%s' "$input" | grep -o '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"')
    case $_names in
    *'
'*) tool= ;;
    *)
        tool=${_names##*:}
        tool=${tool#*\"}
        tool=${tool%\"}
        ;;
    esac
}

# h=<state> of the file an edit tool writes, for its before/after record.
edit_hash() {
    case $tool in
    Write | Edit | MultiEdit) _fp=$(field file_path) ;;
    NotebookEdit) _fp=$(field notebook_path) ;;
    *) return 0 ;;
    esac
    [ -n "$_fp" ] || return 0
    case $_fp in
    *\\*) # JSON escapes in it: this shell can't spell the path (Python looks)
        printf 'h=escaped'
        return 0
        ;;
    /*) ;;
    *) _fp="$(field cwd)/$_fp" ;;
    esac
    printf 'h=%s' "$(ck_hash "$_fp")"
}

# The HEAD of the git repository holding directory $1, read the way git keeps
# it (.git/HEAD, then the ref's file or packed-refs; a worktree's commondir)
# without starting git: "g=<sha>|none<TAB>b=<branch>" for the spool's index
# line, nothing outside a repository. Only for a command that looks like a
# test or build: the HEAD it starts on, before it runs (the snapshot of its
# files comes a moment later, in the background).
git_head() {
    _d=$1
    _g=
    case $_d in /*) ;; *) return 0 ;; esac
    case $_d in *\\*) return 0 ;; esac # JSON escapes: this shell can't spell it
    while [ -n "$_d" ]; do
        if [ -d "$_d/.git" ]; then
            _g=$_d/.git
            break
        elif [ -f "$_d/.git" ]; then
            _l=
            { IFS= read -r _l < "$_d/.git"; } 2>/dev/null
            case $_l in
            'gitdir: '?*) _g=${_l#gitdir: } ;;
            *) return 0 ;;
            esac
            case $_g in /*) ;; *) _g=$_d/$_g ;; esac
            break
        fi
        case $_d in */*) _d=${_d%/*} ;; *) _d= ;; esac
    done
    [ -n "$_g" ] || return 0
    _h=
    { IFS= read -r _h < "$_g/HEAD"; } 2>/dev/null
    [ -n "$_h" ] || return 0
    case $_h in
    'ref: '*)
        _r=${_h#ref: }
        _c=$_g
        if [ -f "$_g/commondir" ]; then
            _cd=
            { IFS= read -r _cd < "$_g/commondir"; } 2>/dev/null
            case $_cd in '') ;; /*) _c=$_cd ;; *) _c=$_g/$_cd ;; esac
        fi
        _s=
        { IFS= read -r _s < "$_g/$_r"; } 2>/dev/null
        [ -n "$_s" ] || { IFS= read -r _s < "$_c/$_r"; } 2>/dev/null
        if [ -z "$_s" ] && [ -f "$_c/packed-refs" ]; then
            _s=$(awk -v r="$_r" '$2 == r { print $1; exit }' "$_c/packed-refs" 2>/dev/null)
        fi
        _b=${_r#refs/heads/}
        ;;
    *)
        _s=$_h
        _b='(detached)'
        ;;
    esac
    case $_s in '' | *[!0-9a-f]*) _s=none ;; esac
    # The index line stays far below 1 KiB (one write): no long or odd names.
    case $_b in *'	'* | *'\'*) _b= ;; esac
    [ "${#_b}" -le 200 ] || _b=
    printf 'g=%s\tb=%s' "$_s" "$_b"
}

# Ingest this session's spool into the journal in the background (a Python
# moment that must not hold up the hook): Stop, PreCompact, PostCompact; and
# `snap TOOL_USE_ID` when a Bash command looks like a test or build, so the
# git state it runs against is pinned. Nothing when no runtime is ready (the
# next Python moment ingests).
bg_py() {
    [ -n "$sid" ] || return 0
    rt_env
    ck_python || return 0
    (
        trap '' HUP
        (ck_exec_py "$@") &
        _bp=$!
        _bn=0 # tenths of a second
        # Polled (not a sleeping watchdog, which would outlive a quick
        # ingest): every 0.1 s for the first 2 s, then every second; stopped
        # at bgdeadline.
        while kill -0 "$_bp" 2>/dev/null; do
            if [ "$_bn" -ge $((bgdeadline * 10)) ]; then
                kill "$_bp" 2>/dev/null
                break
            fi
            if [ "$_bn" -lt 20 ]; then
                sleep 0.1
                _bn=$((_bn + 1))
            else
                sleep 1
                _bn=$((_bn + 10))
            fi
        done
        wait "$_bp" 2>/dev/null
    ) </dev/null >/dev/null 2>&1 &
}

# The hook's own output, plus a note when its record couldn't be spooled.
emit() {
    if [ -n "$out" ]; then
        printf '%s\n' "$out"
    elif [ -n "$CK_SPOOL_SAY" ]; then
        say "$CK_SPOOL_SAY"
    fi
}

# Python couldn't answer while this session is paused: hold every call.
deny_paused() {
    ck_degraded "$sid" "PreToolUse while paused: $(why "$1")"
    ck_kick
    if [ -n "$(field agent_id)" ]; then
        deny "checkpoint: this session is paused; checkpoint's runtime is not ready, so every tool call is held. Don't call more tools: stop here and return a short report of what you did and what is left."
    else
        deny "checkpoint: this session is paused; checkpoint's runtime is not ready, so every tool call is held. Don't start new work: end your turn with one line saying you're paused."
    fi
}

# Does a queued pause command (its text) pause this session? No target,
# `now`, `all`, or a target this session's id starts with.
queued_targets_me() {
    _c=$(printf '%s' "$1" | tr 'A-Z' 'a-z' | sed 's#^[[:space:]]*/check:pause##; s#^[[:space:]]*pause##; s/[.!]*[[:space:]]*$//')
    _rawsid=$(field session_id)
    for _w in $_c; do
        case $_w in
        now | interrupt | all | --stop-jobs) ;;
        *) case $_rawsid in "$_w"*) ;; *) return 1 ;; esac ;;
        esac
    done
    return 0
}

# The session's start while no runtime is ready: start the build in the
# background and say so once; pausing works meanwhile (this script).
degraded_session() {
    ck_kick retry
    case $(ck_state) in
    nopixi) msg="checkpoint: pixi not found, so checkpoint can't set itself up (no trackers or job tools; pausing still works; until then it records which tools ran, not their contents). Install pixi from https://pixi.sh, then restart Claude Code." ;;
    failed) msg="checkpoint: setting up its runtime failed ($(ck_fail_line)); it is retried at every session start. Pausing still works; until then it records which tools ran, not their contents. /check:doctor has details (log: $ENVD/install.log)." ;;
    *) msg="checkpoint: first-time setup is running in the background (usually under a minute). Pausing works meanwhile; trackers and the job tools start once it is done." ;;
    esac
    ctx="checkpoint (plugin) is still setting up its runtime, so its tracker notes and job tools aren't available yet. If a tool call is denied because the session is paused, don't start new work: end your turn with one line saying you're paused."
    paused && ctx="$ctx This session is PAUSED. The user's next message resumes it."
    printf '{"systemMessage":"%s","hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' \
        "$(esc "$msg")" "$(esc "$ctx")"
}

# UserPromptSubmit when Python can't answer: pause and resume still work.
prompt_fallback() {
    ck_degraded "$sid" "UserPromptSubmit: $(why "$1")"
    ck_kick
    p=$(field prompt | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | tr 'A-Z' 'a-z')
    nr=
    if [ -f "$st/noresume/$sid" ]; then
        # Only a fresh one: a mod that died mid-prompt must not leave the
        # user's next message unable to resume (state.NORESUME_MAX_AGE_SECONDS).
        [ -n "$(find "$st/noresume/$sid" -mmin -2 2>/dev/null)" ] && nr=1
        rm -f "$st/noresume/$sid"
    fi
    case $p in
    '<task-notification>'* | '[subagent hand-back]'* | '<side-sessions-event>'* | '<ci-monitor-event>'* | '<local-command-'* | '<command-message>'*)
        nr=1
        ;;
    esac
    note="checkpoint's runtime isn't ready ($(ck_state)), so this was handled by its fallback"
    case $p in
    /check:pause | '/check:pause now' | '/check:pause interrupt' | pause | pause. | pause! | 'pause now' | 'pause now.' | \
        'pause now!' | 'pause interrupt' | 'pause interrupt.' | 'pause interrupt!')
        [ -n "$nr" ] && return 0
        paused || ck_write "$st/flags/$sid" "$(ck_now) fallback"
        prompt_ctx "<checkpoint-result>Paused: this session stops at its next tool call ($note; a running command keeps running). Type a message to resume it (any non-/check command resumes).</checkpoint-result> Don't start anything new: end your turn with one line saying you're paused."
        ;;
    '/check:pause '* | 'pause all'* | 'pause now all'* | 'pause interrupt all'*)
        [ -n "$nr" ] && return 0
        if queued_targets_me "$p"; then
            paused || ck_write "$st/flags/$sid" "$(ck_now) fallback"
            prompt_ctx "<checkpoint-result>Paused this session only ($note: other sessions weren't paused).</checkpoint-result> Don't start anything new: end your turn with one line saying you're paused."
        else
            prompt_ctx "<checkpoint-result>Nothing was paused: $note, and pausing another session needs it. /check:doctor says why.</checkpoint-result>"
        fi
        ;;
    /check:doctor*)
        prompt_ctx "<checkpoint-result>$(CHECKPOINT_ENVD=${CHECKPOINT_ENVD:-} sh "$root/hooks/env.sh" doctor "$(field session_id)" 2>/dev/null | tr '\n' ';')</checkpoint-result>"
        ;;
    /check:status* | /check:help*)
        prompt_ctx "<checkpoint-result>$note: sessions and jobs can't be listed yet. /check:doctor says why.</checkpoint-result>"
        ;;
    /check:list* | /check:diff* | /check:stop* | /check:cancel* | /check:complete* | /check:export* | /check:save* | \
        /check:forget*)
        # Inspection and management never resume, also without Python.
        prompt_ctx "<checkpoint-result>Not done: $note, and this command needs it. /check:doctor says why.</checkpoint-result>"
        ;;
    /check:resume*)
        if paused && [ -z "$nr" ]; then
            rm -f "$st/flags/$sid" "$st/now/$sid"
            prompt_ctx "<checkpoint-result>Resumed ($note: no resume notes this time; read the tracker before going on).</checkpoint-result>" "checkpoint: resumed this session."
        else
            prompt_ctx "<checkpoint-result>This session wasn't paused ($note).</checkpoint-result>"
        fi
        ;;
    *)
        paused || return 0
        if [ -n "$nr" ]; then
            prompt_ctx "This session is paused (checkpoint): this message didn't resume it; only a message the user types does. Don't start any work."
        else
            rm -f "$st/flags/$sid" "$st/now/$sid"
            prompt_ctx "<checkpoint-resume>Resumed ($note: no resume notes this time). Read the tracker's Now section before going on.</checkpoint-resume>" "checkpoint: resumed this session."
        fi
        ;;
    esac
}

case $ev in
SessionStart)
    mkdir -p "$st" 2>/dev/null
    chmod 700 "$st" 2>/dev/null
    # The env first: the spool asks whether a runtime is there (ck_body), and
    # must not take the one an earlier run recorded in rt/<session> (an update
    # brings a new one).
    ck_env
    ck_spool "$sid" SessionStart
    ck_arm "$sid" # the reserves a recording failure on a full disk becomes
    ck_write "$st/rt/$sid" "$ENVD"
    CHECKPOINT_ENVD=$ENVD
    write_helper
    errf="$st/session-start.err"
    if ck_python; then
        [ -n "$CKPX" ] && ! ck_ready && ck_kick retry # an update's env: built meanwhile
        [ -n "$CKPX" ] && ck_ready && : > "$ENVD/.used" 2>/dev/null
        out=$(py_to "$deadline" hook-session)
        rc=$?
        if [ -n "$out" ]; then
            emit
        elif [ "$rc" -eq 124 ]; then
            # A hung runtime: the session starts now, without the resume
            # notes, instead of waiting out Claude Code's 60 s.
            ck_degraded "$sid" "SessionStart: Python timed out after $deadline s"
            say "checkpoint: its Python runtime didn't answer the session start within $deadline s, so this session starts without checkpoint's notes (resume context, degraded gate); events are still spooled and pausing still works. /check:doctor says more."
        else
            ck_degraded "$sid" "SessionStart: Python did not start"
            why=$(tail -n 1 "$errf" 2>/dev/null | tr -c '[:print:]' ' ' | cut -c 1-200)
            say "checkpoint: its Python runtime did not start (${why:-no error output}), so trackers and job tools are off this session; pausing still works. /check:doctor says more."
        fi
    else
        degraded_session
    fi
    # Spools of sessions gone for a week (after Python ingested what it could).
    ck_spool_prune "$sid"
    ;;

UserPromptSubmit)
    ck_spool "$sid" UserPromptSubmit
    mkdir -p "$st/turn" "$st/turn-start" 2>/dev/null
    echo 0 > "$st/turn/$sid" 2>/dev/null
    : > "$st/turn-start/$sid" 2>/dev/null
    clean_running
    # queued/<sid>: queued pause commands were applied early; Python checks
    # whether this prompt is one's echo (and forgets the ones done with).
    # noresume/<sid>: the mod says this prompt isn't the user's (isMeta,
    # scheduled, automation), so it must not resume.
    if paused || [ -e "$st/pending/$sid" ] || [ -e "$st/queued/$sid" ] || [ -e "$st/noresume/$sid" ] ||
        printf '%s' "$input" | grep -Eqi \
            '"prompt"[[:space:]]*:[[:space:]]*"[[:space:]]*(/check:|pause([[:space:]]+(now|interrupt|all|all[[:space:]]+now|now[[:space:]]+all|all[[:space:]]+interrupt|interrupt[[:space:]]+all))?[.!]?[[:space:]]*")'; then
        out=$(py_to "$deadline" hook-prompt)
        rc=$?
        if [ -n "$out" ]; then
            emit
        elif [ "$rc" -ne 0 ]; then
            prompt_fallback "$rc"
        fi
    fi
    [ -z "$out" ] && [ "${rc:-0}" -eq 0 ] && emit
    ;;

PreToolUse)
    # job_status, job_tail and job_stop only read or stop jobs, and
    # checkpoint_note and task_policy only write the journal: allow them without a prompt
    # (also while paused). Only when the payload has exactly
    # one tool_name key, so nothing nested in tool_input can pose as one.
    # job_start is left to Claude Code's permissions: it runs any command.
    tool_of
    # A command that looks like a test, build or check: its record carries the
    # HEAD it starts on, and a snapshot of the git state and file hashes it
    # runs against follows (capture.VALIDATION_RE decides; this glob only has
    # to be wider).
    vlike=
    extra=
    if [ "$tool" = "Bash" ]; then
        case $(field command) in
        *test* | *build* | *make* | *lint* | *check* | *tox* | *nox* | *cargo* | *mypy* | *ruff* | *tsc* | *jest* | \
            *vitest* | *mocha* | *rspec* | *gradle* | *mvn* | *xcodebuild* | *spec* | *ctest* | *verify* | *validate*)
            vlike=1
            extra=$(git_head "$(field cwd)")
            ;;
        esac
    else
        extra=$(edit_hash)
    fi
    ck_spool "$sid" PreToolUse "$extra"
    case $tool in
    mcp__plugin_check_jobs__job_status | mcp__plugin_check_jobs__job_tail | mcp__plugin_check_jobs__job_stop | \
        mcp__plugin_check_jobs__checkpoint_note | mcp__plugin_check_jobs__task_policy)
        printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"checkpoint job tool"}}'
        exit 0
        ;;
    esac
    tp=$(field transcript_path)
    rc= # set once Python was asked
    if paused; then
        out=$(py_to "$deadline" hook-gate)
        rc=$?
        if [ -z "$out" ] && [ "$rc" -ne 0 ]; then
            deny_paused "$rc"
            exit 0
        fi
    elif [ -n "$tp" ] && [ -f "$tp" ]; then
        # A pause command typed while busy waits in Claude Code's queue, which
        # is written to the transcript at once (DESIGN.md, Spike 1).
        line=$(tail_tp "$tp" | grep '"queue-operation"' |
            grep -E 'check:pause|"content":"[[:space:]]*[Pp]ause' | tail -n 1)
        if [ -n "$line" ] && [ "$line" != "$(cat "$st/seen/$sid" 2>/dev/null)" ]; then
            out=$(py_to "$deadline" hook-gate)
            rc=$?
            if [ -z "$out" ] && [ "$rc" -ne 0 ]; then
                c=$(printf '%s' "$line" | grep -o '"content":"[^"]*"' | head -n 1 | sed 's/^"content":"//; s/"$//')
                if queued_targets_me "$c"; then
                    ck_write "$st/flags/$sid" "$(ck_now) fallback queued"
                    ck_degraded "$sid" "PreToolUse with a queued pause: $(why "$rc")"
                    ck_kick
                    deny "checkpoint: a pause command is waiting in the queue; checkpoint's runtime is not ready, so this session is paused here and every tool call is held. Don't start new work: end your turn with one line saying you're paused."
                    exit 0
                fi
                ck_degraded "$sid" "PreToolUse: a queued pause for another session couldn't be applied: $(why "$rc")"
            fi
        fi
    fi
    if [ -n "$out" ]; then
        emit
        exit 0
    fi
    # Not paused, nothing queued: the call goes ahead without Python. With no
    # runtime ready that is recorded too (degraded/<session>: the gate ran
    # without its Python half), and a build is started if none was tried (an
    # env deleted mid-session). Ready, this costs no process.
    if [ -z "$rc" ] && ! paused; then
        rt_env
        if ! ck_python; then
            ck_degraded "$sid" "PreToolUse while not paused: $(why 75); the call went ahead"
            ck_kick
        fi
    fi
    # Guard a foreground Bash call so `pause now` can stop it while it runs.
    if [ "$tool" = "Bash" ] &&
        ! printf '%s' "$input" | grep -Eq '"run_in_background"[[:space:]]*:[[:space:]]*true'; then
        tid=$(ck_safe "$(field tool_use_id)")
        if [ -n "$tid" ] && [ "$tid" != "_" ]; then
            mkdir -p "$st/running" 2>/dev/null
            # The Claude pid SessionStart recorded (fall back to our parent).
            cpid=$(grep -o '"claude_pid": *[0-9][0-9]*' "$st/sessions/$sid.json" 2>/dev/null | grep -o '[0-9]*$')
            aid=$(field agent_id)
            [ -n "$aid" ] && aid=$(ck_safe "$aid")
            # Separate statements: `a && b &` would background a subshell that
            # keeps this hook's stdout open, and Claude Code would wait on it.
            if printf '%s' "$input" > "$st/running/$tid.json" 2>/dev/null; then
                nohup sh "$root/hooks/watch.sh" "$sid" "$tid" "${cpid:-$PPID}" "$tp" "$aid" \
                    </dev/null >/dev/null 2>&1 &
            fi
        fi
    fi
    # The snapshot of a test, build or check (in the background: the hook
    # never waits for it).
    [ -n "$vlike" ] && bg_py snap "$sid" "$(ck_safe "$(field tool_use_id)")"
    emit
    ;;

PostToolUse | PostToolUseFailure)
    tool_of
    ck_spool "$sid" "$ev" "$(edit_hash)"
    tid=$(ck_safe "$(field tool_use_id)")
    [ -n "$tid" ] && rm -f "$st/running/$tid.json" 2>/dev/null
    mkdir -p "$st/count" "$st/turn" 2>/dev/null
    # Counters other hooks of the session may be writing at the same moment: a
    # torn read is no number (0), and never one with a leading zero (sh
    # arithmetic would read it as octal, and 08 kills the hook).
    t=$(cat "$st/turn/$sid" 2>/dev/null)
    case $t in '' | *[!0-9]*) t=0 ;; *) t=${t#"${t%%[!0]*}"} ;; esac
    echo $((${t:-0} + 1)) > "$st/turn/$sid" 2>/dev/null
    # A write to a tracker resets the reminder counter.
    if printf '%s' "$input" | grep -Eq '"tool_name"[[:space:]]*:[[:space:]]*"(Write|Edit|MultiEdit)"' &&
        printf '%s' "$input" | grep -Eq '"file_path"[[:space:]]*:[[:space:]]*"[^"]*TRACKER\.md"'; then
        echo 0 > "$st/count/$sid"
        # Remember the trackers this session wrote: one that names no session
        # is only ever taken as the tracker of a session that wrote it.
        fp=$(field file_path)
        case $fp in
        */TRACKER.md)
            mkdir -p "$st/trackers" 2>/dev/null
            grep -qxF -e "$fp" "$st/trackers/$sid" 2>/dev/null ||
                printf '%s\n' "$fp" >> "$st/trackers/$sid" 2>/dev/null
            ;;
        esac
        emit
        exit 0
    fi
    n=$(cat "$st/count/$sid" 2>/dev/null)
    case $n in '' | *[!0-9]*) n=0 ;; *) n=${n#"${n%%[!0]*}"} ;; esac
    n=$((${n:-0} + 1))
    echo $n > "$st/count/$sid" 2>/dev/null
    # Same variable and default as state.nudge_every().
    every=${CHECKPOINT_NUDGE_EVERY:-25}
    case $every in '' | *[!0-9]*) every=25 ;; esac
    [ "$every" -lt 1 ] && every=1
    if [ "$n" -ge "$every" ]; then
        out=$(py_to "$deadline" hook-post)
        rc=$?
        [ -z "$out" ] && [ "$rc" -ne 0 ] && ck_degraded "$sid" "PostToolUse: $(why "$rc")"
    fi
    emit
    ;;

Stop)
    ck_spool "$sid" Stop
    clean_running
    cwd=$(field cwd)
    if paused || ls "$cwd/$work"/*/TRACKER.md >/dev/null 2>&1 ||
        ls "${CLAUDE_PROJECT_DIR:-$cwd}/$work"/*/TRACKER.md >/dev/null 2>&1; then
        out=$(py_to "$deadline" hook-stop) # it ingests the spool too
        rc=$?
        if [ -z "$out" ] && [ "$rc" -ne 0 ]; then
            ck_degraded "$sid" "Stop: $(why "$rc")"
            # Not after a timeout: a runtime that just hung would hang again.
            [ "$rc" -ne 124 ] && bg_py ingest "$sid" --sync
        fi
    else
        bg_py ingest "$sid" --sync
    fi
    emit
    ;;

SessionEnd)
    # The last Python moment of the session: ingest and fsync now (Claude
    # Code is exiting, so not in the background; a kill mid-way leaves a torn
    # tail at worst, which the next writer steps over).
    ck_spool "$sid" SessionEnd
    rt_env
    if ck_python; then
        py_to 5 ingest "$sid" --sync >/dev/null
    fi
    emit
    ;;

PreCompact | PostCompact)
    ck_spool "$sid" "$ev"
    bg_py ingest "$sid" --sync
    emit
    ;;

*)
    # SubagentStart, SubagentStop, Notification, PermissionDenied,
    # PermissionRequest, TaskCreated, TaskCompleted: recorded, nothing to
    # decide (a PermissionRequest answered with no decision leaves the
    # dialog to the person).
    ck_spool "$sid" "$ev"
    emit
    ;;
esac
exit 0
